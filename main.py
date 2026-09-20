"""TWL orders agent: answers questions about orders, products and inventory (and, for people
allowed to see them, customers), starting with Shopify. Read-only.

It implements the gateway's agent contract v1 (docs/agent-contract.md in twl-gateway). Slack
reaches it only through twl-gateway.
"""

import asyncio

from flask import Flask, jsonify, request

from orders_agent.config import CUSTOMERS_ROLE, USE_ROLE
from orders_agent.runtime import run_agent
from orders_agent.sources import shopify

app = Flask(__name__)

AGENT_ID = "orders"
CONTRACT_VERSION = "1"
MAX_TEXT = 4000
MAX_HISTORY_TURNS = 8
MAX_HISTORY_CHARS = 1500


@app.get("/health")
def health():
    return jsonify(status="ok", service="twl-orders-agent")


@app.get("/v1/describe")
def v1_describe():
    return jsonify(
        agent_id=AGENT_ID,
        name="Orders",
        contract_version=CONTRACT_VERSION,
        read_only=True,
        sources=["shopify"],
    )


def build_prompt(text, history):
    lines = []
    for turn in (history or [])[-MAX_HISTORY_TURNS:]:
        if not isinstance(turn, dict):
            continue
        speaker = "User" if turn.get("role") == "user" else "Assistant"
        lines.append(f"{speaker}: {str(turn.get('text', ''))[:MAX_HISTORY_CHARS]}")

    prompt = ""
    if lines:
        prompt += "Conversation so far:\n" + "\n".join(lines) + "\n\n"
    return prompt + "New message from the user:\n" + text


@app.post("/v1/message")
def v1_message():
    data = request.get_json(silent=True) or {}
    user = data.get("user") or {}
    roles = [str(role) for role in (user.get("roles") or [])]

    # The gateway has already checked this. Check again: this service reads customer data.
    if USE_ROLE not in roles:
        return jsonify(error="user is not allowed to use the orders assistant"), 403

    text = str(data.get("text", "")).strip()
    if not text:
        return jsonify(error="text is required"), 400
    if len(text) > MAX_TEXT:
        return jsonify(error=f"message is longer than {MAX_TEXT} characters"), 400

    try:
        answer = asyncio.run(run_agent(build_prompt(text, data.get("history")), roles))
    except Exception as exc:  # noqa: BLE001
        app.logger.exception("v1/message failed")
        return jsonify(error=str(exc)), 500

    return jsonify(text=answer.strip() or "I couldn't find an answer to that.")


@app.post("/v1/act")
def v1_act():
    # A read-only assistant never has anything to approve or apply.
    return jsonify(error="the orders assistant has no actions"), 400


@app.get("/shopify-test")
def shopify_test():
    """Admin check that the Shopify credentials work. Returns the shop name only."""
    try:
        info = shopify.shop_info()
        return jsonify(status="ok", shop=info.get("name"), currency=info.get("currencyCode"),
                       timezone=info.get("ianaTimezone"), customers_role=CUSTOMERS_ROLE)
    except Exception as exc:  # noqa: BLE001
        return jsonify(status="error", error=str(exc)), 500


if __name__ == "__main__":
    import os

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

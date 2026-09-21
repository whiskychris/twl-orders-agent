"""TWL orders agent: answers questions about orders, products and stock (and, for people allowed
to see them, customers), starting with Shopify. Read-only.

It implements the gateway's agent contract v1 (docs/agent-contract.md in twl-gateway). Slack
reaches it only through twl-gateway. Who may see what is decided here, in code, before any Shopify
data is fetched (orders_agent/authorization.py and docs/authorization.md).
"""

import asyncio
import logging
import uuid

from flask import Flask, jsonify, request

from orders_agent.authorization import (
    CAPABILITIES,
    AuthorizationError,
    AuthorizationUnavailable,
    resolve_context,
)
from orders_agent.config import USE_ROLE
from orders_agent.runtime import run_agent
from orders_agent.sources import shopify

# Cloud Run collects stderr. Without this, INFO lines (including the audit trail) are dropped.
logging.basicConfig(level=logging.INFO, format="%(message)s")

app = Flask(__name__)

AGENT_ID = "orders"
CONTRACT_VERSION = "1"
MAX_TEXT = 4000
MAX_HISTORY_TURNS = 8
MAX_HISTORY_CHARS = 1500


@app.get("/health")
def health():
    return jsonify(status="ok", service="twl-orders-agent", authorization="capabilities-v1")


@app.get("/v1/describe")
def v1_describe():
    return jsonify(
        agent_id=AGENT_ID,
        name="Orders",
        contract_version=CONTRACT_VERSION,
        read_only=True,
        sources=["shopify"],
        capabilities=list(CAPABILITIES),
    )


def build_prompt(text, history, ctx):
    lines = []
    for turn in (history or [])[-MAX_HISTORY_TURNS:]:
        if not isinstance(turn, dict):
            continue
        speaker = "User" if turn.get("role") == "user" else "Assistant"
        lines.append(f"{speaker}: {str(turn.get('text', ''))[:MAX_HISTORY_CHARS]}")

    prompt = ctx.describe() + "\n\n"
    if lines:
        prompt += "Conversation so far:\n" + "\n".join(lines) + "\n\n"
    return prompt + "New message from the user:\n" + text


@app.post("/v1/message")
def v1_message():
    data = request.get_json(silent=True) or {}
    request_id = uuid.uuid4().hex[:12]
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    conversation = data.get("conversation") if isinstance(data.get("conversation"), dict) else {}

    # The gateway has already checked this. Check again: this service reads business data.
    if USE_ROLE not in [str(role) for role in (user.get("roles") or [])]:
        return jsonify(error="user is not allowed to use the orders assistant"), 403

    text = str(data.get("text", "")).strip()
    if not text:
        return jsonify(error="text is required"), 400
    if len(text) > MAX_TEXT:
        return jsonify(error=f"message is longer than {MAX_TEXT} characters"), 400

    # Decide what this person may see, before anything is fetched. Fail closed.
    try:
        ctx = resolve_context(user, conversation, request_id)
    except AuthorizationError as exc:
        # A refusal is an answer, not an error, so the person sees why.
        return jsonify(text=str(exc))
    except AuthorizationUnavailable:
        app.logger.exception("permissions unavailable")
        return jsonify(error="permissions could not be checked, so nothing was looked up"), 503

    try:
        answer = asyncio.run(run_agent(build_prompt(text, data.get("history"), ctx), ctx))
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
        return jsonify(
            status="ok",
            shop=info.get("name"),
            currency=info.get("currencyCode"),
            timezone=info.get("ianaTimezone"),
        )
    except Exception as exc:  # noqa: BLE001
        return jsonify(status="error", error=str(exc)), 500


if __name__ == "__main__":
    import os

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

"""TWL orders agent: answers questions about orders, products and stock (and, for people allowed
to see them, customers), starting with Shopify. Sales can also raise a new order for an existing
customer, which is only created after a person with the approve role presses a button.

It implements the gateway's agent contract v1 (docs/agent-contract.md in twl-gateway). Slack
reaches it only through twl-gateway. Who may see what is decided here, in code, before any Shopify
data is fetched (orders_agent/authorization.py and docs/authorization.md).

The model only ever reads and prepares. The single write path is /v1/act (orders_agent/entry.py):
deterministic code, after an approval the gateway has already checked. See docs/order-entry.md.
"""

import asyncio
import json
import logging
import uuid

from flask import Flask, jsonify, request

from orders_agent import entry, gift, shared
from orders_agent.authorization import (
    ALL_CAPABILITIES,
    AuthorizationError,
    AuthorizationUnavailable,
    resolve_context,
)
from orders_agent.config import USE_ROLE
from orders_agent.runtime import run_agent
from orders_agent.sources import shopify
from orders_agent.tools import RequestState

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
        read_only=False,  # the model only reads and prepares. Creating an order is /v1/act, after approval
        sources=["shopify"],
        capabilities=list(ALL_CAPABILITIES),
    )


def describe_open_draft(open_proposal):
    """The draft order awaiting approval, so the model can apply a requested change by calling
    prepare_draft_order again with the full corrected lines. Business names and ids only."""
    if not isinstance(open_proposal, dict) or open_proposal.get("kind") != "draft_order":
        return ""
    payload = open_proposal.get("payload") or {}
    lines = [
        {
            "variant_id": line.get("variant_id"),
            "product": line.get("title"),
            "quantity": line.get("quantity"),
            "discount_type": (line.get("discount") or {}).get("type"),
            "discount_value": (line.get("discount") or {}).get("value"),
        }
        for line in payload.get("lines") or []
        if isinstance(line, dict)
    ]
    draft = {
        "customer": (payload.get("display") or {}).get("name"),
        "place": (payload.get("display") or {}).get("place"),
        "target": payload.get("target"),
        "lines": lines,
        "note": payload.get("note"),
    }
    return (
        "A draft order is waiting for approval in this thread. If the user is asking to change it, call "
        "prepare_draft_order again with the FULL corrected line list. Current draft: "
        + json.dumps(draft, ensure_ascii=False)
        + "\n\n"
    )


def build_prompt(text, history, ctx, open_proposal=None):
    lines = []
    for turn in (history or [])[-MAX_HISTORY_TURNS:]:
        if not isinstance(turn, dict):
            continue
        speaker = "User" if turn.get("role") == "user" else "Assistant"
        lines.append(f"{speaker}: {str(turn.get('text', ''))[:MAX_HISTORY_CHARS]}")

    prompt = ctx.describe() + "\n\n" + describe_open_draft(open_proposal)
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

    # A handoff back from the invoicing agent, naming exactly which order to mark paid once its Xero
    # invoice was actually sent. Deterministic, no model involved - the order id came from the
    # invoicing agent's own structured context, not parsed from a sentence. See entry.mark_order_paid.
    context = data.get("context") if isinstance(data.get("context"), dict) else {}
    if context.get("action") == "mark_order_paid":
        try:
            result = entry.mark_order_paid(user, conversation, context.get("order_id"), context.get("order_name"), request_id)
        except entry.ActRefused as exc:
            # A refusal is an answer, not an error, so the person sees why.
            return jsonify(text=str(exc))
        except AuthorizationUnavailable:
            app.logger.exception("permissions unavailable")
            return jsonify(error="permissions could not be checked, so the order was not marked paid"), 503
        return jsonify(result)

    # A handoff from the rewards agent, right after Chris or Oliver approved the day's Rewards Member gift
    # list in #rewards: create one gift order per customer. This is the one approval (Chris's decision), so
    # gift.create_gift_orders re-checks both approve roles, order_entry and the channel, and builds every
    # order in code from the customer ids alone. See orders_agent/gift.py.
    if context.get("action") == "create_gift_orders":
        try:
            result = gift.create_gift_orders(user, conversation, context, request_id)
        except entry.ActRefused as exc:
            return jsonify(text=str(exc))
        except AuthorizationUnavailable:
            app.logger.exception("permissions unavailable")
            return jsonify(error="permissions could not be checked, so no gift orders were created"), 503
        return jsonify(result)

    # A self-handoff from _execute_order_edit, right after a successful edit: re-show the invoice
    # decision (Send Invoice / Edit Order / Cancel), re-priced with whatever just changed. Deterministic,
    # no model involved - the order name comes from the handoff's own structured context, not parsed
    # from a sentence. Same shape as the mark_order_paid dispatch above.
    # A handoff from the rewards agent: an approved request to draft an order for a customer (a Rewards
    # Member), with the customer and lines as structured context rather than a sentence to parse. This
    # only PREPARES the draft, exactly as prepare_draft_order does, and posts it with this agent's own
    # approval buttons. Nothing is created until someone with orders.approve presses one.
    if context.get("action") == "prepare_draft_order":
        try:
            ctx = resolve_context(user, conversation, request_id)
        except AuthorizationError as exc:
            return jsonify(text=str(exc))
        except AuthorizationUnavailable:
            app.logger.exception("permissions unavailable")
            return jsonify(error="permissions could not be checked, so nothing was drafted"), 503
        target = {key: context.get(key) for key in ("company_id", "location_id", "customer_id")}
        try:
            result = entry.prepare(ctx, target, context.get("lines"), context.get("note"))
        except (entry.EntryError, shopify.ShopifyError) as exc:
            return jsonify(text=f"I couldn't draft that order: {exc}")
        return jsonify(text=result["text"], proposal=result["proposal"])

    if context.get("action") == "prepare_invoice_confirmation":
        try:
            ctx = resolve_context(user, conversation, request_id)
        except AuthorizationError as exc:
            return jsonify(text=str(exc))
        except AuthorizationUnavailable:
            app.logger.exception("permissions unavailable")
            return jsonify(error="permissions could not be checked, so nothing was looked up"), 503
        try:
            result = entry.prepare_invoice_handoff(ctx, context.get("order_name") or context.get("order_id"))
        except entry.EntryError as exc:
            return jsonify(text=str(exc))
        return jsonify(text=result["text"], proposal=result["proposal"])

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

    state = RequestState()
    try:
        prompt = build_prompt(text, data.get("history"), ctx, data.get("open_proposal"))
        answer = asyncio.run(run_agent(prompt, ctx, state))
    except Exception as exc:  # noqa: BLE001
        app.logger.exception("v1/message failed")
        return jsonify(error=str(exc)), 500

    if state.proposal is not None:
        # A draft order was prepared. Post the text written in code from Shopify's numbers, with the
        # approval buttons. The model's own words are not used, so a total can't be misstated.
        return jsonify(text=state.text, proposal=state.proposal)

    return jsonify(text=answer.strip() or "I couldn't find an answer to that.")


@app.post("/v1/tools")
def v1_tools():
    """The shared, read-only tools this person may call from another agent (through the gateway only).
    Access is worked out exactly as for /v1/message. See orders_agent/shared.py."""
    data = request.get_json(silent=True) or {}
    try:
        ctx = resolve_context(data.get("user"), data.get("conversation"), uuid.uuid4().hex[:12])
    except AuthorizationError:
        return jsonify(tools=[])
    except AuthorizationUnavailable:
        app.logger.exception("permissions unavailable")
        return jsonify(error="permissions could not be checked"), 503
    return jsonify(tools=shared.available(ctx))


@app.post("/v1/tool")
def v1_tool():
    """Run one shared tool for another agent, as the person it is answering (through the gateway only).
    Read only. Access is re-checked here on every call, not just when the tools were listed."""
    data = request.get_json(silent=True) or {}
    request_id = uuid.uuid4().hex[:12]
    try:
        ctx = resolve_context(data.get("user"), data.get("conversation"), request_id)
    except AuthorizationError as exc:
        return jsonify(error=str(exc))
    except AuthorizationUnavailable:
        app.logger.exception("permissions unavailable")
        return jsonify(error="permissions could not be checked, so nothing was looked up"), 503
    name = str(data.get("tool", "")).strip()
    app.logger.info("shared tool %s for %s via %s", name, ctx.user_id, data.get("caller_agent_id"))
    try:
        return jsonify(result=shared.run(ctx, name, data.get("input")))
    except (shared.ToolRefused, entry.EntryError, shopify.ShopifyError) as exc:
        return jsonify(error=str(exc))


@app.post("/v1/act")
def v1_act():
    """Create an approved order. The gateway has checked the approver's role and the button press. This
    re-checks everything and runs in code, with no model. See orders_agent/entry.py."""
    data = request.get_json(silent=True) or {}
    request_id = uuid.uuid4().hex[:12]
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    conversation = data.get("conversation") if isinstance(data.get("conversation"), dict) else {}
    try:
        result = entry.execute(user, conversation, data.get("choice"), data.get("proposal"), request_id)
    except entry.ActRefused as exc:
        # A 4xx tells the gateway nothing was changed (or the draft was left uncompleted).
        return jsonify(error=str(exc)), 400
    except AuthorizationUnavailable:
        app.logger.exception("permissions unavailable")
        return jsonify(error="permissions could not be checked, so nothing was created"), 400
    except Exception:  # noqa: BLE001
        # Anything unexpected may have happened after Shopify was called: a 5xx tells the gateway the
        # outcome is unknown, so the person is told to check Shopify before retrying.
        app.logger.exception("v1/act failed")
        return jsonify(error="something went wrong while creating the order"), 500
    return jsonify(result)


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

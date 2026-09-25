"""Sample orders: bottles taken out of stock for samples, on one of TWL's own two accounts, at $0, and
(usually) marked fulfilled in the same press because they leave on the spot.

Minwoo asks in #dispatch or #inventory (the same `fulfil_channels` list fulfilment uses) with the `samples`
capability. Chris's decisions (Sep 2026):
- Samples only. The order is always for sales@ or events@ (their personal accounts, as TWL's own sample
  orders have always been), always 100% off. Nothing about the account or the price comes from the model:
  it picks "sales" or "events" and the products, and the rest is fixed here.
- One press: "Create & Mark Fulfilled" creates the order and fulfils everything on it. "Create Order Only"
  just creates it. No invoicing, and no payment terms (a $0 order with no terms completes as paid, which is
  what lets it be fulfilled - fulfilment is paid orders only).
- `samples` gives nothing else: no other customers, no discounts other than 100%, no invoicing.

Same wall as order entry: prepare() only prices; execute() (from /v1/act) re-checks the role, the capability
and the channel, re-derives the customer from the account name, re-prices and refuses unless it is still
$0, and is safe to repeat (a tag from the proposal token).
"""

import logging
import time

from . import fulfil
from .authorization import SAMPLES, AuthorizationError, audit, resolve_context
from .config import APPROVE_ROLE
from .entry import (
    GID, MAX_NOTE, TOKEN, ActRefused, EntryError, _one_line, admin_order_url, build_input, check_pricing,
    clean_lines, fmt,
)
from .product_pick import format_eta
from .sources import draft_orders as shop
from .sources.shopify import ShopifyError

log = logging.getLogger("orders_agent.samples")

KIND = "sample_order"
ACCOUNTS = {"sales": "sales@thewhiskylist.com.au", "events": "events@thewhiskylist.com.au"}
CHOICES = [
    {"id": "create_fulfil", "label": "Create & Mark Fulfilled", "style": "primary"},
    {"id": "create_only", "label": "Create Order Only"},
]
CHOICE_IDS = frozenset(choice["id"] for choice in CHOICES)
FULL_DISCOUNT = {"type": "percent", "value": "100.00"}
TAGS = ["smith-sample"]
# Shopify routes a new order to a location in the background, so its fulfillment orders can take a moment.
FULFIL_WAITS = (1, 2, 3, 4)


def _account(raw):
    account = str(raw or "").strip().lower()
    if account not in ACCOUNTS:
        raise EntryError("Sample orders go to 'sales' (sales@) or 'events' (events@). Which one?")
    return account


def _lines(raw):
    """Variant and quantity only, each 100% off. A discount the model sends is ignored, never used."""
    if not isinstance(raw, list):
        raise EntryError("Give me at least one product line.")
    stripped = [
        {"variant_id": item.get("variant_id"), "quantity": item.get("quantity")} if isinstance(item, dict) else item
        for item in raw
    ]
    return [{**line, "discount": dict(FULL_DISCOUNT)} for line in clean_lines(stripped)]


def _subject(account):
    """The account's personal customer record, looked up by its fixed email address."""
    found = shop.find_by_email(ACCOUNTS[account])
    personal = [a for a in found.get("accounts") or [] if a.get("account") == "personal"]
    if not personal:
        raise ShopifyError(f"I couldn't find the {ACCOUNTS[account]} customer in Shopify.")
    return shop.get_customer(personal[0]["customer_id"])


def _price(subject, lines, note, tags):
    calc = shop.calculate(build_input(subject, lines, note, tags))
    check_pricing(lines, calc)
    if calc["total"] != 0:
        raise EntryError(f"Shopify priced this sample at {fmt(calc['total'])}, not 0.00, so I stopped. Nothing was created.")
    return calc


def render(name, lines, calc, warnings):
    rows = []
    for number, (line, got) in enumerate(zip(lines, calc["lines"]), 1):
        tag = f"*[{line['tag']}]* " if line.get("tag") else ""
        rows.append(f"{number}. {tag}{line['quantity']} × {got['title']} @ {fmt(got['unit_price'])}, 100% off → *0.00*")
    parts = [f"*Sample order for {name}*", "\n".join(rows), f"*Total 0.00 {calc['currency'] or ''}*".strip()]
    parts.append("No invoice. *Create & Mark Fulfilled* creates it and marks it all fulfilled straight away.")
    if warnings:
        parts.append("⚠️ " + " ".join(warnings))
    return "\n\n".join(parts)


def prepare(ctx, raw_account, raw_lines, note=None):
    """Price a sample order. Saves nothing. Raises EntryError. Returns {"proposal", "text"}."""
    if not ctx.has(SAMPLES):
        raise EntryError("This user can't raise sample orders here.")
    account = _account(raw_account)
    lines = _lines(raw_lines)
    user_note = _one_line(note, MAX_NOTE)
    try:
        subject = _subject(account)
        calc = _price(subject, lines, user_note, TAGS)
        variants = shop.get_variants([line["variant_id"] for line in lines])
    except ShopifyError as exc:
        raise EntryError(str(exc)) from None

    warnings = []
    payload_lines = []
    for number, (line, got) in enumerate(zip(lines, calc["lines"]), 1):
        info = variants.get(line["variant_id"])
        if not info or info["status"] != "ACTIVE":
            raise EntryError(f"Line {number}: {got['title']} isn't an active product, so it can't be ordered.")
        tag = None
        if info.get("pre_order"):
            tag = "Pre-order" + (f" (ETA {format_eta(info['eta'])})" if info.get("eta") else " (no ETA set)")
        stock = info.get("stock")
        if stock is not None and stock < line["quantity"]:
            warnings.append(f"Line {number}: Shopify may not have enough stock for {line['quantity']}.")
        payload_lines.append({"variant_id": line["variant_id"], "quantity": line["quantity"], "title": got["title"], "tag": tag})

    payload = {
        "version": 1,
        "account": account,
        "customer_id": subject["customer_id"],
        "lines": [{"variant_id": line["variant_id"], "quantity": line["quantity"], "title": line["title"]} for line in payload_lines],
        "note": user_note,
        "requested_by": {"user_id": ctx.user_id, "name": ctx.name},
    }
    items = [{"id": number, "label": f"{line['quantity']} × {line['title']}"[:150]} for number, line in enumerate(payload_lines, 1)]
    proposal = {"kind": KIND, "items": items, "payload": payload, "choices": CHOICES}
    return {"proposal": proposal, "text": render(subject["name"], payload_lines, calc, warnings)}


# --- execute -----------------------------------------------------------------------------------------


def _validate_payload(payload):
    try:
        if payload.get("version") != 1:
            raise ValueError("version")
        account = _account(payload["account"])
        customer_id = str(payload["customer_id"])
        if not GID["customer"].match(customer_id):
            raise ValueError("customer_id")
        lines = _lines(payload["lines"])
        return account, customer_id, lines, _one_line(payload.get("note"), MAX_NOTE)
    except (KeyError, TypeError, ValueError, AttributeError, EntryError):
        raise ActRefused("That sample order is malformed, so nothing was created. Ask me again.") from None


def _authorize(user, conversation, request_id):
    roles = [str(role) for role in ((user or {}).get("roles") or [])]
    if APPROVE_ROLE not in roles:
        raise ActRefused("You don't have permission to create sample orders.")
    try:
        ctx = resolve_context(user, conversation, request_id)
    except AuthorizationError as exc:
        raise ActRefused(str(exc)) from None
    if not ctx.has(SAMPLES):
        raise ActRefused("Sample orders aren't available to you in this conversation.")
    return ctx


def _create(ctx, token, account, customer_id, lines, note, request_id):
    """The order, creating it only if this approval hasn't already. Raises ActRefused."""
    tag = f"smith-{token}"[:40]
    try:
        existing = shop.find_draft_by_tag(tag)
        if existing and existing.get("status") == "COMPLETED" and existing.get("order"):
            return existing["order"], True
        if existing and existing.get("status") != "OPEN":
            raise ActRefused("A draft for this already exists in an unexpected state, so I did nothing. Check Shopify.")
        if existing:
            draft_id = existing["id"]
        else:
            subject = _subject(account)
            if subject["customer_id"] != customer_id:
                raise ActRefused("That sample account has changed in Shopify since this was prepared. Nothing was created.")
            _price(subject, lines, note, TAGS)
            # No payment terms: a $0 order completes as paid, which is what lets it be fulfilled.
            draft_id = shop.create_draft(build_input(subject, lines, note, TAGS + [tag]))["id"]
        order = shop.complete_draft(draft_id)
    except EntryError as exc:
        raise ActRefused(f"{exc} Nothing was created.") from None
    except ShopifyError as exc:
        raise ActRefused(f"{exc} (If a draft was saved it is not completed, and is safe to leave.)") from None
    audit("sample_order", request_id, ctx.user_id, ctx.source, ctx.visibility, action="created", account=account, order=order["name"])
    return order, False


def _fulfil_all(ctx, order, request_id):
    """Mark everything on the new order fulfilled. Returns (done, reason). Never raises: the order already
    exists, so a failure here is reported, not treated as nothing happening."""
    try:
        found = None
        for wait in FULFIL_WAITS:
            found = fulfil.shop.find_order_for_fulfilment(order["name"])
            if found["lines"] or found["fulfillment_status"] == "FULFILLED":
                break
            time.sleep(wait)
        if not found["lines"]:
            if found["fulfillment_status"] == "FULFILLED":
                return True, None
            return False, "Shopify hasn't assigned it to a location yet"
        if found["financial_status"] not in fulfil.PAID_STATUSES:
            return False, "Shopify doesn't show it as paid"
        lines = fulfil.choose_lines(found, {})
        groups = {}
        for line in lines:
            groups.setdefault(line["fulfillment_order_id"], []).append((line["fulfillment_order_line_id"], line["quantity"]))
        fulfil.shop.create_fulfilment(list(groups.items()), None)
    except (EntryError, ShopifyError) as exc:
        log.warning("sample fulfilment failed for %s: %s", order["name"], str(exc)[:300])
        return False, str(exc).rstrip(".")
    audit("fulfilment", request_id, ctx.user_id, ctx.source, ctx.visibility, action="fulfilled", order=order["name"], sample=True)
    return True, None


def execute(user, conversation, choice, proposal, request_id):
    ctx = _authorize(user, conversation, request_id)
    if choice not in CHOICE_IDS:
        raise ActRefused("Use one of the buttons: Create & Mark Fulfilled, or Create Order Only.")
    token = str(proposal.get("token", ""))
    if not TOKEN.match(token):
        raise ActRefused("That sample order has no valid reference, so I did nothing.")
    account, customer_id, lines, note = _validate_payload(proposal.get("payload") or {})

    order, repeated = _create(ctx, token, account, customer_id, lines, note, request_id)
    url = admin_order_url(order["legacyResourceId"])
    prefix = "Already done. " if repeated else ""
    result = {"order": order["name"], "order_id": order["id"], "account": account, "url": url, "fulfilled": False}
    if choice == "create_only":
        return {"status": "ok", "text": f"{prefix}Success: <{url}|Sample order {order['name']}> created.", "result": result}

    done, reason = _fulfil_all(ctx, order, request_id)
    result["fulfilled"] = done
    if done:
        text = f"{prefix}Success: <{url}|Sample order {order['name']}> created and marked fulfilled."
    else:
        text = (
            f"{prefix}<{url}|Sample order {order['name']}> was created, but I couldn't mark it fulfilled ({reason}). "
            f"Ask me `fulfil: {order['name']}` in a moment, or do it in Shopify."
        )
    return {"status": "ok", "text": text, "result": result}

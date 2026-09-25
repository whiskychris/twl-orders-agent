"""Marking an order (or some of its items) fulfilled, for orders that don't go through the usual
dispatch process: picked up, used for TWL's own purposes, or delivered some other way.

Asked for in #dispatch or #inventory (the `fulfil_channels` list in twl-orders-authz) by someone with
the `fulfil` capability. Chris's decisions (Sep 2026):
- Paid orders only. An unpaid order is refused - being paid is what allows an order to be dispatched.
- The customer is never emailed. Tracking is optional and passed to Shopify when given.
- The person who asked may approve it themselves, with the same orders.approve role and the `fulfil`
  capability, re-checked here. The `fulfil` capability gives nothing else: it doesn't let anyone
  approve a new order (that needs `order_entry`, see entry._authorize).

The same wall as the rest of this service:
  prepare()   From the model's tool. Read-only: looks up what's still unfulfilled, works out which
              fulfillment-order lines to use, and renders the preview in code from Shopify's numbers.
  execute()   From /v1/act only, after the button press. Re-checks the role, the capability, the channel
              and the payment status, re-reads the order and refuses if anything it would fulfil changed
              since the preview. Safe to repeat: if exactly the approved quantities have already gone,
              it reports that instead of fulfilling twice.
"""

import re

from .authorization import FULFIL, AuthorizationError, audit, resolve_context
from .config import APPROVE_ROLE
from .entry import ActRefused, EntryError, admin_order_url
from .sources import fulfillment as shop
from .sources.shopify import ShopifyError

KIND = "fulfilment"
CHOICES = [{"id": "approve_fulfilment", "label": "Mark Fulfilled", "style": "primary"}]
CHOICE_IDS = frozenset(choice["id"] for choice in CHOICES)
PAID_STATUSES = frozenset({"PAID", "PARTIALLY_REFUNDED"})
MAX_ITEMS = 50
MAX_QUANTITY = 1000

LINE_ITEM_ID = re.compile(r"^gid://shopify/LineItem/\d+$")
FULFILLMENT_ORDER_ID = re.compile(r"^gid://shopify/FulfillmentOrder/\d+$")
FULFILLMENT_ORDER_LINE_ID = re.compile(r"^gid://shopify/FulfillmentOrderLineItem/\d+$")
ORDER_ID = re.compile(r"^gid://shopify/Order/\d+$")
TRACKING_NUMBER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{2,48}$")

# Shopify builds a tracking link only when the company is named exactly as in its supported list.
# What people type, mapped to that name. Anything else is passed through as typed (no link, but kept).
CARRIERS = {
    "dhl": "DHL Express",
    "dhl express": "DHL Express",
    "dhl ecommerce": "DHL eCommerce",
    "australia post": "Australia Post",
    "auspost": "Australia Post",
    "aus post": "Australia Post",
    "startrack": "StarTrack",
    "star track": "StarTrack",
    "fedex": "FedEx",
    "ups": "UPS",
    "tnt": "TNT",
    "aramex": "Aramex Australia",
    "couriers please": "Couriers Please",
    "couriersplease": "Couriers Please",
    "sendle": "Sendle",
}


# --- tracking -----------------------------------------------------------------------------------------


def clean_tracking(number, company):
    """None, or {"number", "company"}. A company without a number is refused: there'd be nothing to track."""
    number = re.sub(r"\s+", "", str(number or ""))
    company = re.sub(r"\s+", " ", str(company or "")).strip()
    if not number:
        if company:
            raise EntryError("A tracking company was given without a tracking number. Give the number too, or leave tracking out.")
        return None
    if not TRACKING_NUMBER.match(number):
        raise EntryError("That tracking number doesn't look right (letters, digits and dashes only, 3 to 49 characters).")
    if len(company) > 60:
        raise EntryError("That tracking company name is too long.")
    return {"number": number, "company": CARRIERS.get(company.lower(), company) or None}


# --- choosing the lines -------------------------------------------------------------------------------


def _requested(raw_items):
    """{line_item_id: quantity or None (all remaining)}, in the order asked. Empty means everything."""
    if raw_items in (None, []):
        return {}
    if not isinstance(raw_items, list) or len(raw_items) > MAX_ITEMS:
        raise EntryError(f"Give up to {MAX_ITEMS} items, each with a line_item_id from get_fulfillable_items.")
    requested = {}
    for number, item in enumerate(raw_items, 1):
        if not isinstance(item, dict):
            raise EntryError(f"Item {number} isn't valid.")
        line_item_id = str(item.get("line_item_id", "")).strip()
        if not LINE_ITEM_ID.match(line_item_id):
            raise EntryError(f"Item {number}: use a line_item_id that get_fulfillable_items returned.")
        if line_item_id in requested:
            raise EntryError(f"Item {number}: that item is already listed. Combine them.")
        quantity = item.get("quantity")
        if quantity is not None and str(quantity).strip() != "":
            try:
                quantity = int(quantity)
            except (TypeError, ValueError):
                raise EntryError(f"Item {number}: the quantity must be a whole number.") from None
            if not 1 <= quantity <= MAX_QUANTITY:
                raise EntryError(f"Item {number}: the quantity must be at least 1.")
        else:
            quantity = None
        requested[line_item_id] = quantity
    return requested


def choose_lines(order, requested):
    """The fulfillment-order lines to fulfil, as [{fulfillment_order_id, fulfillment_order_line_id,
    line_item_id, title, quantity, remaining, location}]. Raises EntryError with something to fix."""
    if not order["lines"]:
        raise EntryError(f"Order {order['name']} has nothing left to fulfil.")
    ready = [line for line in order["lines"] if line["fulfillable"]]
    held = [line for line in order["lines"] if not line["fulfillable"]]

    chosen = []
    if not requested:
        if held:
            raise EntryError(
                f"Some items on order {order['name']} can't be fulfilled right now ("
                + ", ".join(sorted({f"{line['title']} ({str(line['status']).lower().replace('_', ' ')})" for line in held}))
                + "). Release them in Shopify first, or name the items to fulfil."
            )
        chosen = [{**line, "quantity": line["remaining"]} for line in ready]
    else:
        for line_item_id, quantity in requested.items():
            matches = [line for line in ready if line["line_item_id"] == line_item_id]
            if not matches:
                if any(line["line_item_id"] == line_item_id for line in held):
                    raise EntryError("One of those items is on hold or scheduled in Shopify, so it can't be fulfilled yet.")
                raise EntryError(f"One of those items isn't waiting to be fulfilled on order {order['name']}.")
            available = sum(line["remaining"] for line in matches)
            wanted = available if quantity is None else quantity
            if wanted > available:
                raise EntryError(f"Only {available} × {matches[0]['title']} are left to fulfil on order {order['name']}.")
            for line in matches:
                if wanted <= 0:
                    break
                take = min(wanted, line["remaining"])
                chosen.append({**line, "quantity": take})
                wanted -= take

    locations = sorted({line["location"] for line in chosen})
    if len(locations) > 1:
        raise EntryError(
            f"Those items are at more than one location ({', '.join(locations)}). Ask for one location at a time."
        )
    return chosen


def fulfillable_items(order_number):
    """The model's lookup: what's still waiting to be fulfilled on an order, one entry per order line."""
    order = shop.find_order_for_fulfilment(order_number)
    grouped = {}
    for line in order["lines"]:
        entry = grouped.setdefault(
            line["line_item_id"],
            {"line_item_id": line["line_item_id"], "title": line["title"], "sku": line["sku"],
             "remaining": 0, "locations": [], "on_hold": False},
        )
        entry["remaining"] += line["remaining"]
        if line["location"] not in entry["locations"]:
            entry["locations"].append(line["location"])
        entry["on_hold"] = entry["on_hold"] or not line["fulfillable"]
    return {
        "order": order["name"],
        "paid": order["financial_status"] in PAID_STATUSES,
        "financial_status": order["financial_status"],
        "cancelled": order["cancelled"],
        "items": list(grouped.values()),
    }


# --- prepare -----------------------------------------------------------------------------------------


def render(order_name, lines, tracking, left_over):
    rows = [f"{number}. {line['quantity']} × {line['title']}" for number, line in enumerate(lines, 1)]
    parts = [f"*Mark order {order_name} fulfilled*", "\n".join(rows)]
    details = [f"From: {lines[0]['location']}"]
    if tracking:
        details.append(f"Tracking: {tracking['company'] + ' ' if tracking.get('company') else ''}{tracking['number']}")
    else:
        details.append("Tracking: none")
    if left_over:
        details.append(f"Still unfulfilled afterwards: {left_over}")
    details.append("The customer won't be emailed.")
    parts.append("\n".join(details))
    return "\n\n".join(parts)


def _check_order(order, refuse):
    if order["cancelled"]:
        raise refuse(f"Order {order['name']} is cancelled, so there's nothing to fulfil.")
    if order["financial_status"] not in PAID_STATUSES:
        status = str(order["financial_status"] or "unknown").lower().replace("_", " ")
        raise refuse(
            f"Order {order['name']} isn't paid ({status}), so I can't mark it fulfilled. Only paid orders "
            "can be fulfilled here."
        )


def prepare(ctx, order_number, raw_items=None, tracking_number=None, tracking_company=None):
    """Preview marking an order fulfilled. Nothing changes here. Raises EntryError. Returns
    {"proposal", "text"}."""
    if not ctx.has(FULFIL):
        raise EntryError("This user can't mark orders fulfilled here.")
    requested = _requested(raw_items)
    tracking = clean_tracking(tracking_number, tracking_company)
    try:
        order = shop.find_order_for_fulfilment(order_number)
    except ShopifyError as exc:
        raise EntryError(str(exc)) from None
    _check_order(order, EntryError)
    lines = choose_lines(order, requested)

    left_over = sum(line["remaining"] for line in order["lines"]) - sum(line["quantity"] for line in lines)
    payload = {
        "version": 1,
        "order_id": order["id"],
        "order_name": order["name"],
        "lines": [
            {key: line[key] for key in ("fulfillment_order_id", "fulfillment_order_line_id", "line_item_id", "title", "quantity", "remaining")}
            for line in lines
        ],
        "tracking": tracking,
        "requested_by": ctx.user_id,
    }
    items = [{"id": number, "label": f"{line['quantity']} x {line['title']}"[:150]} for number, line in enumerate(lines, 1)]
    proposal = {"kind": KIND, "items": items, "payload": payload, "choices": CHOICES}
    return {"proposal": proposal, "text": render(order["name"], lines, tracking, left_over)}


# --- execute -----------------------------------------------------------------------------------------


def _validate_payload(payload):
    try:
        if payload.get("version") != 1:
            raise ValueError("version")
        order_id = str(payload["order_id"])
        if not ORDER_ID.match(order_id):
            raise ValueError("order_id")
        order_name = str(payload["order_name"])
        raw_lines = payload["lines"]
        if not isinstance(raw_lines, list) or not 1 <= len(raw_lines) <= MAX_ITEMS * 4:
            raise ValueError("lines")
        lines = []
        for line in raw_lines:
            fulfillment_order_id = str(line["fulfillment_order_id"])
            fulfillment_order_line_id = str(line["fulfillment_order_line_id"])
            if not FULFILLMENT_ORDER_ID.match(fulfillment_order_id) or not FULFILLMENT_ORDER_LINE_ID.match(fulfillment_order_line_id):
                raise ValueError("ids")
            quantity, remaining = int(line["quantity"]), int(line["remaining"])
            if not 1 <= quantity <= remaining:
                raise ValueError("quantity")
            lines.append(
                {"fulfillment_order_id": fulfillment_order_id, "fulfillment_order_line_id": fulfillment_order_line_id,
                 "title": str(line.get("title") or ""), "quantity": quantity, "remaining": remaining}
            )
        raw_tracking = payload.get("tracking")
        tracking = clean_tracking(raw_tracking.get("number"), raw_tracking.get("company")) if raw_tracking else None
        return order_id, order_name, lines, tracking
    except (KeyError, TypeError, ValueError, AttributeError, EntryError):
        raise ActRefused("That fulfilment request is malformed, so nothing was changed. Ask me again.") from None


def _authorize(user, conversation, request_id):
    roles = [str(role) for role in ((user or {}).get("roles") or [])]
    if APPROVE_ROLE not in roles:
        raise ActRefused("You don't have permission to mark orders fulfilled.")
    try:
        ctx = resolve_context(user, conversation, request_id)
    except AuthorizationError as exc:
        raise ActRefused(str(exc)) from None
    if not ctx.has(FULFIL):
        raise ActRefused("Marking orders fulfilled isn't available to you in this conversation.")
    return ctx


def _success(order_id, order_name, lines, tracking, prefix=""):
    url = admin_order_url(order_id.rsplit("/", 1)[-1])
    count = sum(line["quantity"] for line in lines)
    text = f"{prefix}Success: <{url}|Order {order_name}> marked fulfilled ({count} item{'s' if count != 1 else ''}"
    if tracking:
        text += f", tracking {tracking['company'] + ' ' if tracking.get('company') else ''}{tracking['number']}"
    return {
        "status": "ok",
        "text": text + ").",
        "result": {"order": order_name, "order_id": order_id, "items": count, "tracking": bool(tracking), "url": url},
    }


def execute(user, conversation, choice, proposal, request_id):
    """Fulfil an approved request. Raises ActRefused (nothing changed). Any other exception means the
    outcome is unknown."""
    ctx = _authorize(user, conversation, request_id)
    if choice not in CHOICE_IDS:
        raise ActRefused("I need an approval to mark it fulfilled. Use the button.")
    order_id, order_name, lines, tracking = _validate_payload(proposal.get("payload") or {})

    try:
        order = shop.find_order_for_fulfilment(order_name)
    except ShopifyError as exc:
        raise ActRefused(str(exc)) from None
    if order["id"] != order_id:
        raise ActRefused("That order has changed since this was prepared. Ask me again.")
    _check_order(order, ActRefused)

    live = {line["fulfillment_order_line_id"]: line for line in order["lines"]}
    now = [live[line["fulfillment_order_line_id"]]["remaining"] if line["fulfillment_order_line_id"] in live else 0 for line in lines]
    if all(left == line["remaining"] - line["quantity"] for left, line in zip(now, lines)):
        # Exactly what was approved has already gone - a repeated approval. Don't fulfil it twice.
        audit("fulfilment", request_id, ctx.user_id, ctx.source, ctx.visibility, action="already_fulfilled", order=order_name)
        return _success(order_id, order_name, lines, tracking, "Already done. ")
    for left, line in zip(now, lines):
        live_line = live.get(line["fulfillment_order_line_id"])
        if left != line["remaining"] or live_line is None or not live_line["fulfillable"]:
            raise ActRefused(f"Order {order_name} has changed since this was prepared. Nothing was changed - ask me again.")
    if len({live[line["fulfillment_order_line_id"]]["location"] for line in lines}) > 1:
        raise ActRefused("Those items are now at more than one location. Nothing was changed - ask me again.")

    groups = {}
    for line in lines:
        groups.setdefault(line["fulfillment_order_id"], []).append((line["fulfillment_order_line_id"], line["quantity"]))
    try:
        shop.create_fulfilment(list(groups.items()), tracking)
    except ShopifyError as exc:
        raise ActRefused(f"{exc} Check the order in Shopify before asking again.") from None

    audit(
        "fulfilment", request_id, ctx.user_id, ctx.source, ctx.visibility,
        action="fulfilled", order=order_name, items=sum(line["quantity"] for line in lines), tracking=bool(tracking),
    )
    return _success(order_id, order_name, lines, tracking)

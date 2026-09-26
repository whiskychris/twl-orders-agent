"""Rewards allocation orders: one UNPAID order per member who won bottles in an allocation ballot, at full
price, with payment terms "Due on fulfilment" so it's invoiced when the bottles land.

The only caller is the gateway's handoff relay, right after Chris or Ollie approved a ballot in #rewards
(twl-allocations entry.py). That approval is the one approval, as for the Rewards gift, so this stays narrow
and everything that makes an order is decided here in code:
- the approver must hold orders.approve AND allocations.approve, and order_entry here (DM or order_entry_channels),
- the handoff supplies only customer ids, variant ids and quantities; prices are Shopify's own (no discount),
- tags are fixed from the allocation's tag: `<tag>` and `<tag>-<customer number>`, so nobody gets two however
  often this runs: a completed one is reported, an uncompleted one is completed,
- at most MAX_ORDERS per handoff; the allocations service sends the rest in later batches.
One member failing never stops the rest; each is reported.
"""

import re

from .authorization import audit
from .entry import ActRefused, _authorize, admin_order_url, build_input
from .sources import draft_orders as shop
from .sources.shopify import ShopifyError

ALLOCATIONS_APPROVE_ROLE = "allocations.approve"
MAX_ORDERS = 40
MAX_LINES = 20
MAX_QTY = 24
CUSTOMER_GID = re.compile(r"^gid://shopify/Customer/(\d+)$")
VARIANT_GID = re.compile(r"^gid://shopify/ProductVariant/\d+$")
TAG = re.compile(r"^allocation-[a-z0-9-]{1,60}$")


def _orders(raw):
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_ORDERS:
        raise ActRefused("That allocation list is malformed, so no orders were created.")
    orders, seen = [], set()
    for entry in raw:
        entry = entry if isinstance(entry, dict) else {}
        customer_id = str(entry.get("customer_id") or "")
        lines = entry.get("lines")
        if not CUSTOMER_GID.match(customer_id) or customer_id in seen:
            raise ActRefused("That allocation list has an invalid or repeated customer, so no orders were created.")
        if not isinstance(lines, list) or not 1 <= len(lines) <= MAX_LINES:
            raise ActRefused("That allocation list has an order with no lines, so no orders were created.")
        clean = []
        for line in lines:
            line = line if isinstance(line, dict) else {}
            variant_id, qty = str(line.get("variant_id") or ""), line.get("qty")
            if not VARIANT_GID.match(variant_id) or not isinstance(qty, int) or not 1 <= qty <= MAX_QTY:
                raise ActRefused("That allocation list has an invalid line, so no orders were created.")
            clean.append({"variant_id": variant_id, "quantity": qty, "discount": None})
        seen.add(customer_id)
        orders.append({"customer_id": customer_id, "lines": clean})
    return orders


def _link(order):
    return f"<{admin_order_url(order['legacyResourceId'])}|{order['name']}>"


def _one(subject, lines, tag, allocation_id, terms):
    """Create (or find) one member's allocation order. Returns (outcome, text): created, already or failed."""
    own_tag = f"{tag}-{CUSTOMER_GID.match(subject['customer_id']).group(1)}"
    try:
        existing = shop.find_draft_by_tag(own_tag)
        if existing and existing.get("status") == "COMPLETED" and existing.get("order"):
            return "already", f"already has {existing['order']['name']}"
        if existing and existing.get("status") != "OPEN":
            return "failed", f"has an allocation draft ({existing.get('name')}) in an unexpected state. Check Shopify"
        if existing:
            return "created", _link(shop.complete_draft(existing["id"]))
        note = f"Rewards allocation {allocation_id}. Invoice when the bottles arrive."
        draft_input = build_input(subject, lines, note, [tag, own_tag], terms["id"], terms["type"])
        calc = shop.calculate(draft_input)
        if sorted((l["variant_id"], l["quantity"]) for l in calc["lines"]) != sorted(
                (l["variant_id"], l["quantity"]) for l in lines):
            return "failed", "Shopify priced different lines than the allocation, so it wasn't created"
        order = shop.complete_draft(shop.create_draft(draft_input)["id"])
        return "created", f"{_link(order)} ({calc['total']})" + ("" if subject.get("shipping") else " (no address on file)")
    except ShopifyError as exc:
        return "failed", str(exc)[:200]


def create_allocation_orders(user, conversation, context, request_id):
    """Returns {"text", "react"} for the thread. Raises ActRefused (nothing created) before the first order."""
    roles = [str(role) for role in ((user or {}).get("roles") or [])]
    if ALLOCATIONS_APPROVE_ROLE not in roles:
        raise ActRefused("Allocation orders come only from an approved Rewards allocation.")
    ctx = _authorize(user, conversation, request_id)  # orders.approve, order_entry, and the channel
    context = context or {}
    tag = str(context.get("tag") or "")
    allocation_id = " ".join(str(context.get("allocation_id") or "").split())[:60]
    if not TAG.match(tag) or not allocation_id:
        raise ActRefused("That allocation list is malformed, so no orders were created.")
    orders = _orders(context.get("orders"))
    try:
        terms = shop.default_unpaid_terms()
    except ShopifyError as exc:
        raise ActRefused(f"{exc} No orders were created.") from None

    results = {"created": [], "already": [], "failed": []}
    for entry in orders:
        try:
            subject = shop.get_customer(entry["customer_id"])
        except ShopifyError as exc:
            results["failed"].append(f"customer {entry['customer_id'].rsplit('/', 1)[-1]}: {str(exc)[:200]}")
            continue
        outcome, detail = _one(subject, entry["lines"], tag, allocation_id, terms)
        results[outcome].append(f"{subject['name']}: {detail}")

    audit("order_entry", request_id, ctx.user_id, ctx.source, ctx.visibility, action="allocation_orders",
          created=len(results["created"]), already=len(results["already"]), failed=len(results["failed"]))
    parts = []
    if results["created"]:
        count = len(results["created"])
        parts.append(f"*Created {count} unpaid allocation order{'s' if count != 1 else ''}* ({terms['name']}):\n"
                     + "\n".join(f"• {line}" for line in results["created"]))
    if results["already"]:
        parts.append("*Skipped, already has one:*\n" + "\n".join(f"• {line}" for line in results["already"]))
    if results["failed"]:
        parts.append("*Not created:*\n" + "\n".join(f"• {line}" for line in results["failed"])
                     + f"\nFix it, then `@Smith allocations: orders {allocation_id}` creates whatever's still missing.")
    return {"text": "\n\n".join(parts) or "Nothing to create.",
            "react": "warning" if results["failed"] else "white_check_mark"}

"""The Rewards Member gift: one order per approved customer, holding only the "2026 Rewards Member
20-Bottle Gift" at 100% off, with a note to combine it with the customer's next shipment.

The only caller is the gateway's handoff relay, right after Chris or Oliver approved the day's gift list in
#rewards (twl-rewards-agent gift.py). That approval is the only one: Chris decided a gift list needs no
second orders.approve step. So this is kept deliberately narrow, and everything that makes an order comes
from here in code, never from the handoff or a model:
- the approver must hold orders.approve AND rewards.approve, and order_entry here (DM or order_entry_channels),
- the product is found by its exact title, quantity 1, 100% off, and Shopify must price it at 0.00,
- the handoff supplies only customer ids; the note and tags are fixed here,
- each customer's draft carries a tag unique to them (rewards-gift-2026-<id>), so nobody gets two, however
  often this runs: a completed one is reported, an uncompleted one is completed, and a recent order with
  the gift on it counts too.
One customer failing never stops the rest; each is reported.
"""

import re
from decimal import Decimal

from .authorization import audit
from .config import GIFT_PRODUCT_TITLE
from .entry import ActRefused, _authorize, admin_order_url
from .sources import draft_orders as shop
from .sources.shopify import ShopifyError

REWARDS_APPROVE_ROLE = "rewards.approve"
TAG_PREFIX = "rewards-gift-2026"
NOTE = "Rewards Member gift. Combine with this customer's next shipment."
DISCOUNT_TITLE = "Rewards Member gift"
MAX_CUSTOMERS = 40
CUSTOMER_GID = re.compile(r"^gid://shopify/Customer/(\d+)$")


def _customer_ids(raw):
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_CUSTOMERS:
        raise ActRefused("That gift list is malformed, so no orders were created.")
    ids = []
    for value in raw:
        if not CUSTOMER_GID.match(str(value or "")):
            raise ActRefused("That gift list has an invalid customer id, so no orders were created.")
        if value not in ids:
            ids.append(value)
    return ids


def tag_for(customer_id):
    return f"{TAG_PREFIX}-{CUSTOMER_GID.match(customer_id).group(1)}"


def build_input(subject, variant_id, tag):
    draft = {
        "purchasingEntity": {"customerId": subject["customer_id"]},
        "lineItems": [{
            "variantId": variant_id,
            "quantity": 1,
            "appliedDiscount": {"value": 100.0, "valueType": "PERCENTAGE", "title": DISCOUNT_TITLE},
        }],
        "tags": [TAG_PREFIX, tag],
        "note": NOTE,
    }
    if subject.get("shipping"):
        draft["shippingAddress"] = subject["shipping"]
    if subject.get("billing"):
        draft["billingAddress"] = subject["billing"]
    return draft


def _link(order):
    return f"<{admin_order_url(order['legacyResourceId'])}|{order['name']}>"


def _one(subject, variant_id):
    """Create (or find) one customer's gift order. Returns (outcome, text): outcome is created, already or
    failed. Raises nothing for Shopify's refusals: they become 'failed'."""
    customer_id = subject["customer_id"]
    tag = tag_for(customer_id)
    try:
        existing = shop.find_draft_by_tag(tag)
        if existing and existing.get("status") == "COMPLETED" and existing.get("order"):
            return "already", f"already has {existing['order']['name']}"
        if existing and existing.get("status") != "OPEN":
            return "failed", f"has a gift draft ({existing.get('name')}) in an unexpected state. Check Shopify"
        if existing:
            order = shop.complete_draft(existing["id"])
            return "created", _link(order)
        found = shop.order_with_variant(customer_id, variant_id)
        if found:
            return "already", f"already has {found}"
        draft_input = build_input(subject, variant_id, tag)
        calc = shop.calculate(draft_input)
        if len(calc["lines"]) != 1 or calc["lines"][0]["variant_id"] != variant_id or calc["total"] != Decimal("0"):
            return "failed", f"Shopify priced it at {calc['total']} rather than 0.00, so it wasn't created"
        draft = shop.create_draft(draft_input)
        order = shop.complete_draft(draft["id"])
        return "created", _link(order) + ("" if subject.get("shipping") else " (no address on file)")
    except ShopifyError as exc:
        return "failed", str(exc)[:200]


def create_gift_orders(user, conversation, context, request_id):
    """Returns {"text"} for the thread. Raises ActRefused (nothing created) before the first order."""
    roles = [str(role) for role in ((user or {}).get("roles") or [])]
    if REWARDS_APPROVE_ROLE not in roles:
        raise ActRefused("Gift orders come only from an approved Rewards gift list.")
    ctx = _authorize(user, conversation, request_id)  # orders.approve, order_entry, and the channel
    ids = _customer_ids((context or {}).get("customer_ids"))
    try:
        gift = shop.gift_variant(GIFT_PRODUCT_TITLE)
    except ShopifyError as exc:
        raise ActRefused(f"{exc} No orders were created.") from None
    if gift["stock"] is not None and gift["stock"] < len(ids):
        raise ActRefused(
            f"Only {gift['stock']} of the {GIFT_PRODUCT_TITLE} are in stock in Shopify, for {len(ids)} orders. Fix "
            "the inventory in Shopify; tomorrow's list will include everyone again. No orders were created."
        )

    results = {"created": [], "already": [], "failed": []}
    for customer_id in ids:
        try:
            subject = shop.get_customer(customer_id)
        except ShopifyError as exc:
            results["failed"].append(f"customer {customer_id.rsplit('/', 1)[-1]}: {str(exc)[:200]}")
            continue
        outcome, detail = _one(subject, gift["variant_id"])
        results[outcome].append(f"{subject['name']}: {detail}")

    audit("order_entry", request_id, ctx.user_id, ctx.source, ctx.visibility, action="gift_orders",
          created=len(results["created"]), already=len(results["already"]), failed=len(results["failed"]))
    parts = []
    if results["created"]:
        parts.append(f"*Created {len(results['created'])} gift order{'s' if len(results['created']) != 1 else ''}* "
                     f"(0.00, note: \"{NOTE}\"):\n" + "\n".join(f"• {line}" for line in results["created"]))
    if results["already"]:
        parts.append("*Skipped, already has the gift:*\n" + "\n".join(f"• {line}" for line in results["already"]))
    if results["failed"]:
        parts.append("*Not created:*\n" + "\n".join(f"• {line}" for line in results["failed"])
                     + "\nThey'll be on tomorrow's list again if they're still due.")
    # A green tick on the list's root post once every approved customer has the gift (created now or
    # already), a warning if any weren't created. The gateway passes it to Smith (docs/smith-contract.md).
    return {"text": "\n\n".join(parts), "react": "warning" if results["failed"] else "white_check_mark"}

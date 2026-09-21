"""Order entry: prepare a draft order for an existing customer (a company location, or an individual
customer), then, only after a person with
the approve role presses a button, create it in Shopify.

Two phases, with a hard wall between them:

  prepare()   Runs from the model's tool. Read-only: it looks up the company location, asks Shopify to
              PRICE the order (nothing is saved), checks Shopify applied each discount as intended, and
              renders the draft. The text people read is written here in code from Shopify's numbers,
              never by the model, so a total can't be misstated.

  execute()   Runs from /v1/act, after the gateway checked the approver's role and the button press. No
              model is involved. It re-checks the role, the capability and the channel, re-prices the
              order and refuses if the total changed, then creates the draft and completes it. It is
              safe to repeat: the draft carries a tag from the proposal's token, so a second attempt
              finds the first instead of making another.

Paid vs unpaid (a choice made when approving):
  create_paid    complete the draft normally: Shopify records it as paid. In TWL's process "paid" means it
                 was invoiced through Xero. This agent does not touch Xero.
  create_unpaid  put payment terms on the draft (the location's own terms, else "due on fulfilment"), so
                 Shopify creates the order with payment outstanding.
"""

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .authorization import ORDER_ENTRY, AuthorizationError, audit, resolve_context
from .config import APPROVE_ROLE, get_shopify_config
from .sources import draft_orders as shop
from .sources.shopify import ShopifyError

GID = {
    "company": re.compile(r"^gid://shopify/Company/\d+$"),
    "location": re.compile(r"^gid://shopify/CompanyLocation/\d+$"),
    "customer": re.compile(r"^gid://shopify/Customer/\d+$"),
    "variant": re.compile(r"^gid://shopify/ProductVariant/\d+$"),
}
TOKEN = re.compile(r"^[a-z0-9][a-z0-9-]{6,90}$")

MAX_LINES = 30
MAX_QUANTITY = 1000
MAX_NOTE = 300
DISCOUNT_TYPES = ("percent", "per_unit", "line_total")
TOLERANCE = Decimal("0.02")
CENTS = Decimal("0.01")

CHOICES = [
    {"id": "create_paid", "label": "Create order (invoiced, paid)", "style": "primary"},
    {"id": "create_unpaid", "label": "Create order (not invoiced, unpaid)"},
]
CHOICE_IDS = frozenset(choice["id"] for choice in CHOICES)


class EntryError(Exception):
    """The request can't become a draft as asked. The message is safe to show, and tells the model or
    the person what to fix. Nothing was created."""


class ActRefused(Exception):
    """An approval was refused, or failed before anything was completed. The message is safe to show."""


# --- small helpers ----------------------------------------------------------------------------------


def _dec(value, what):
    try:
        number = Decimal(str(value).strip().replace(",", "").lstrip("$"))
    except (InvalidOperation, AttributeError):
        raise EntryError(f"{what} isn't a number.") from None
    if not number.is_finite():
        raise EntryError(f"{what} isn't a number.")
    return number


def fmt(amount):
    return f"{Decimal(amount).quantize(CENTS, rounding=ROUND_HALF_UP):,.2f}"


def _one_line(text, limit):
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


# --- lines and discounts ------------------------------------------------------------------------------


def _clean_discount(number, kind, value):
    kind = str(kind or "").strip().lower()
    empty_value = value is None or str(value).strip() == ""
    if not kind and empty_value:
        return None
    if kind not in DISCOUNT_TYPES or empty_value:
        raise EntryError(
            f"Line {number}: a discount needs a type (percent, per_unit or line_total) and a value."
        )
    amount = _dec(value, f"Line {number}: the discount")
    if amount <= 0:
        raise EntryError(f"Line {number}: the discount must be more than zero.")
    if kind == "percent" and amount > 100:
        raise EntryError(f"Line {number}: a percentage discount can't be more than 100.")
    return {"type": kind, "value": str(amount.quantize(CENTS, rounding=ROUND_HALF_UP))}


def clean_lines(raw):
    if not isinstance(raw, list) or not raw:
        raise EntryError("Give me at least one product line.")
    if len(raw) > MAX_LINES:
        raise EntryError(f"That's more than {MAX_LINES} lines. Split it into two orders.")
    lines = []
    seen = set()
    for number, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise EntryError(f"Line {number} isn't a valid line.")
        variant_id = str(item.get("variant_id", "")).strip()
        if not GID["variant"].match(variant_id):
            raise EntryError(f"Line {number}: use a variant_id that find_variant returned.")
        if variant_id in seen:
            raise EntryError(f"Line {number}: that product is already on the order. Combine the quantities.")
        seen.add(variant_id)
        try:
            quantity = int(item.get("quantity"))
        except (TypeError, ValueError):
            raise EntryError(f"Line {number}: the quantity must be a whole number.") from None
        if not 1 <= quantity <= MAX_QUANTITY:
            raise EntryError(f"Line {number}: the quantity must be between 1 and {MAX_QUANTITY}.")
        lines.append(
            {
                "variant_id": variant_id,
                "quantity": quantity,
                "discount": _clean_discount(number, item.get("discount_type"), item.get("discount_value")),
            }
        )
    return lines


def _discount_input(line):
    """Shopify's appliedDiscount for a line. Fixed amounts are sent as the discount on the WHOLE line, and
    the preview is checked to confirm Shopify applied them that way."""
    discount = line["discount"]
    if not discount:
        return None
    value = Decimal(discount["value"])
    if discount["type"] == "percent":
        return {"value": float(value), "valueType": "PERCENTAGE", "title": "Sales discount"}
    total = value * line["quantity"] if discount["type"] == "per_unit" else value
    return {"value": float(total.quantize(CENTS, rounding=ROUND_HALF_UP)), "valueType": "FIXED_AMOUNT", "title": "Sales discount"}


def _expected_discount(line, unit_price):
    discount = line["discount"]
    value = Decimal(discount["value"])
    if discount["type"] == "percent":
        return (unit_price * line["quantity"] * value / 100).quantize(CENTS, rounding=ROUND_HALF_UP)
    return (value * line["quantity"] if discount["type"] == "per_unit" else value).quantize(CENTS, rounding=ROUND_HALF_UP)


def build_input(subject, lines, note, tags, terms_id=None):
    """The DraftOrderInput for Shopify. `subject` is a company location (priced with the company's own
    price list) or an individual customer (normal prices). Addresses go straight back to Shopify here
    and never reach the model."""
    items = []
    for line in lines:
        item = {"variantId": line["variant_id"], "quantity": line["quantity"]}
        applied = _discount_input(line)
        if applied:
            item["appliedDiscount"] = applied
        items.append(item)
    if subject["kind"] == "company":
        entity = {
            "purchasingCompany": {
                "companyId": subject["company_id"],
                "companyLocationId": subject["location_id"],
                "companyContactId": subject["contact_id"],
            }
        }
    else:
        entity = {"customerId": subject["customer_id"]}
    draft = {"purchasingEntity": entity, "lineItems": items, "tags": list(tags), "note": note}
    if subject.get("shipping"):
        draft["shippingAddress"] = subject["shipping"]
    if subject.get("billing"):
        draft["billingAddress"] = subject["billing"]
    if terms_id:
        draft["paymentTerms"] = {"paymentTermsTemplateId": terms_id}
    return draft


def normalize_target(raw):
    """Who the order is for: a company location, or an individual customer. Exactly one of the two."""
    raw = raw if isinstance(raw, dict) else {}
    company_id, location_id, customer_id = (str(raw.get(key) or "").strip() for key in ("company_id", "location_id", "customer_id"))
    if customer_id and (company_id or location_id):
        raise EntryError("Give either a company and location, or an individual customer, not both.")
    if customer_id:
        if not GID["customer"].match(customer_id):
            raise EntryError("Use a customer_id that find_customer returned.")
        return {"kind": "customer", "customer_id": customer_id}
    if GID["company"].match(company_id) and GID["location"].match(location_id):
        return {"kind": "company", "company_id": company_id, "location_id": location_id}
    raise EntryError("Use the company_id and location_id (or the customer_id) that find_customer returned.")


def resolve_subject(target):
    """Look the customer up in Shopify and check the order can be raised for them."""
    if target["kind"] == "customer":
        return shop.get_customer(target["customer_id"])
    location = shop.get_location(target["location_id"])
    if location["company_id"] != target["company_id"]:
        raise EntryError("That location doesn't belong to that company.")
    if not location["contact_id"]:
        raise EntryError(f"{location['company_name']} has no contact in Shopify, so an order can't be raised for it yet.")
    return location


def _display(subject):
    """What people are shown: a company (and its location) or the customer's name. Nothing else."""
    if subject["kind"] == "company":
        return {"name": subject["company_name"], "place": subject["location_name"]}
    return {"name": subject["name"], "place": None}


def check_pricing(lines, calc):
    """Refuse unless Shopify priced exactly the lines sent, and applied each discount as intended.
    Failing closed here means a discount misunderstanding can never become a wrong order."""
    if len(calc["lines"]) != len(lines):
        raise EntryError("Shopify priced a different number of lines than I sent, so I stopped. Nothing was created.")
    for number, (sent, got) in enumerate(zip(lines, calc["lines"]), 1):
        if got["variant_id"] != sent["variant_id"] or got["quantity"] != sent["quantity"]:
            raise EntryError(f"Line {number}: Shopify priced something different from what I sent, so I stopped.")
        if got["unit_price"] is None or got["line_total"] is None:
            raise EntryError(f"Line {number}: Shopify didn't return a price, so I stopped.")
        if sent["discount"]:
            expected = _expected_discount(sent, got["unit_price"])
            actual = got["discount_amount"]
            if actual is None or abs(actual - expected) > TOLERANCE:
                raise EntryError(
                    f"Line {number}: I expected the discount to come to {fmt(expected)} but Shopify applied "
                    f"{fmt(actual) if actual is not None else 'none'}. I stopped so nothing is created wrongly. "
                    "Tell me the discount another way (for example a percentage), or check the amount."
                )


# --- phase 1: prepare (read-only) -----------------------------------------------------------------------


def prepare(ctx, raw_target, raw_lines, note=None):
    """Check and price a draft. Returns {"proposal", "text", "warnings"}. Raises EntryError. Saves nothing.
    raw_target is {"company_id", "location_id"} or {"customer_id"}."""
    if not ctx.has(ORDER_ENTRY):
        raise EntryError("This user can't raise orders here.")
    target = normalize_target(raw_target)

    lines = clean_lines(raw_lines)
    user_note = _one_line(note, MAX_NOTE)
    subject = resolve_subject(target)

    order_note = f"Raised in Slack by {ctx.name} via Smith." + (f" {user_note}" if user_note else "")
    calc = shop.calculate(build_input(subject, lines, order_note, ["smith-order-entry"]))
    check_pricing(lines, calc)

    variants = shop.get_variants([line["variant_id"] for line in lines], include_inventory=ctx.has("inventory"))
    warnings = []
    if not subject.get("shipping"):
        warnings.append("There is no delivery address on file for this customer, so none was added.")
    for number, (line, got) in enumerate(zip(lines, calc["lines"]), 1):
        info = variants.get(line["variant_id"])
        if not info or info["status"] != "ACTIVE":
            raise EntryError(f"Line {number}: {got['title']} isn't an active product, so it can't be ordered.")
        if info["stock"] is not None and info["stock"] < line["quantity"]:
            warnings.append(f"Line {number}: only {info['stock']} in stock for {line['quantity']} ordered.")

    terms = subject["terms"] or shop.default_unpaid_terms()
    payload = {
        "version": 2,
        "target": target,
        "display": _display(subject),
        "lines": [
            {**line, "title": got["title"], "sku": got["sku"]} for line, got in zip(lines, calc["lines"])
        ],
        "note": user_note,
        "requested_by": {"user_id": ctx.user_id, "name": ctx.name},
        "terms": terms,
        "expected": {
            "currency": calc["currency"],
            "subtotal": str(calc["subtotal"]),
            "tax": str(calc["tax"]),
            "total": str(calc["total"]),
        },
    }
    text = render(payload, calc, warnings)
    items = [
        {"id": number, "label": f"{line['quantity']} × {line['title']}"}
        for number, line in enumerate(payload["lines"], 1)
    ]
    proposal = {"kind": "draft_order", "items": items, "payload": payload, "choices": CHOICES}
    return {"proposal": proposal, "text": text, "warnings": warnings}


def render(payload, calc, warnings):
    """The draft as people read it. Built here, from Shopify's own numbers."""
    company, place = payload["display"]["name"], payload["display"]["place"]
    head = f"*Draft order for {company}*" + (f" ({place})" if place and place != company else "")
    rows = []
    for number, (line, got) in enumerate(zip(payload["lines"], calc["lines"]), 1):
        name = got["title"] + (f" ({got['sku']})" if got["sku"] else "")
        gross = got["unit_price"] * line["quantity"]
        row = f"{number}. {line['quantity']} × {name} @ {fmt(got['unit_price'])} = {fmt(gross)}"
        if got["discount_amount"]:
            row += f", less {fmt(got['discount_amount'])}"
        rows.append(row + f" → *{fmt(got['line_total'])}*")
    currency = calc["currency"] or ""
    parts = [
        head,
        "\n".join(rows),
        f"Subtotal {fmt(calc['subtotal'])} · GST {fmt(calc['tax'])} · *Total {fmt(calc['total'])} {currency}*".strip(),
        f"Priced by Shopify for this customer. If created unpaid, payment terms are *{payload['terms']['name']}*. "
        "No shipping charge is added. Shopify sends its usual order emails to the customer once the order is "
        "created (not while this is a draft).",
    ]
    if warnings:
        parts.append("⚠️ " + " ".join(warnings))
    parts.append(f"Requested by {payload['requested_by']['name']}.")
    return "\n\n".join(parts)


# --- phase 2: execute (only from /v1/act) --------------------------------------------------------------------


def admin_order_url(legacy_id):
    handle = get_shopify_config()["shop"].removesuffix(".myshopify.com")
    return f"https://admin.shopify.com/store/{handle}/orders/{legacy_id}"


def _validate_payload(payload):
    """The payload came from our own proposal, stored by the gateway. Check it anyway before it can
    become a write."""
    try:
        if payload.get("version") != 2:
            raise ValueError("version")
        target = normalize_target(payload["target"])
        if not str(payload["display"]["name"]).strip():
            raise ValueError("display")
        lines = clean_lines(
            [
                {
                    "variant_id": line["variant_id"],
                    "quantity": line["quantity"],
                    "discount_type": (line.get("discount") or {}).get("type"),
                    "discount_value": (line.get("discount") or {}).get("value"),
                }
                for line in payload["lines"]
            ]
        )
        expected = payload["expected"]
        Decimal(expected["total"]), Decimal(expected["subtotal"])
        terms_id = payload["terms"]["id"]
        if not re.match(r"^gid://shopify/PaymentTermsTemplate/\d+$", terms_id):
            raise ValueError("terms")
        return target, lines, expected, terms_id, _one_line(payload.get("note"), MAX_NOTE), payload["requested_by"]
    except (KeyError, TypeError, ValueError, InvalidOperation, EntryError, AttributeError):
        raise ActRefused("That draft is malformed, so nothing was created. Ask me to draft it again.") from None


def _authorize(user, conversation, request_id):
    roles = [str(role) for role in ((user or {}).get("roles") or [])]
    if APPROVE_ROLE not in roles:
        raise ActRefused("You don't have permission to approve new orders.")
    try:
        ctx = resolve_context(user, conversation, request_id)
    except AuthorizationError as exc:
        raise ActRefused(str(exc)) from None
    if not ctx.has(ORDER_ENTRY):
        raise ActRefused("Approving new orders isn't available in this conversation.")
    return ctx


def _result(order, company, paid, note):
    name = order["name"]
    url = admin_order_url(order["legacyResourceId"])
    state = "marked as paid (invoiced)" if paid else "created as unpaid (waiting to be invoiced)"
    text = f"{note}Order *{name}* for *{company}* is {state}.\n<{url}|Open {name} in Shopify>"
    return {
        "status": "ok",
        "text": text,
        "result": {"order": name, "order_id": order["id"], "paid": paid, "url": url},
    }


def execute(user, conversation, choice, proposal, request_id):
    """Create the order for an approved draft. Raises ActRefused (nothing created, or the draft exists but
    was not completed). Any other exception means the outcome is unknown."""
    ctx = _authorize(user, conversation, request_id)
    if choice not in CHOICE_IDS:
        raise ActRefused("I need to know whether to create it as paid or unpaid. Use one of the buttons.")
    if not isinstance(proposal, dict) or proposal.get("kind") != "draft_order":
        raise ActRefused("That isn't an order draft, so I did nothing.")
    token = str(proposal.get("token", ""))
    if not TOKEN.match(token):
        raise ActRefused("That draft has no valid reference, so I did nothing.")

    target, lines, expected, terms_id, note, requester = _validate_payload(proposal.get("payload") or {})
    payload = proposal["payload"]
    customer_name = payload["display"]["name"]
    paid = choice == "create_paid"
    tag = f"smith-{token}"[:40]

    try:
        existing = shop.find_draft_by_tag(tag)
        if existing and existing.get("status") == "COMPLETED" and existing.get("order"):
            audit("order_entry", request_id, ctx.user_id, ctx.source, ctx.visibility, action="already_created")
            return _result(existing["order"], customer_name, paid, "Already done. ")
        if existing and existing.get("status") != "OPEN":
            raise ActRefused("A draft for this already exists in an unexpected state, so I did nothing. Check Shopify.")

        if existing:
            draft_id = existing["id"]
        else:
            subject = resolve_subject(target)
            order_note = f"Raised in Slack by {requester['name']} via Smith, approved by {ctx.name}."
            order_note += f" {'Marked paid: invoiced in Xero.' if paid else 'Not invoiced yet: unpaid.'}"
            if note:
                order_note += f" {note}"

            calc = shop.calculate(build_input(subject, lines, order_note, ["smith-order-entry"]))
            check_pricing(lines, calc)
            if calc["total"] != Decimal(expected["total"]) or calc["subtotal"] != Decimal(expected["subtotal"]):
                raise ActRefused(
                    f"The price changed since the draft: it was {expected['total']} and is now {calc['total']}. "
                    "Nothing was created. Ask me to draft it again."
                )
            draft = shop.create_draft(
                build_input(subject, lines, order_note, ["smith-order-entry", tag], None if paid else terms_id)
            )
            draft_id = draft["id"]

        order = shop.complete_draft(draft_id)
    except EntryError as exc:
        # A pricing check failed before anything was created.
        raise ActRefused(f"{exc} Nothing was created.") from None
    except ShopifyError as exc:
        raise ActRefused(f"{exc} (If a draft was saved it is not completed, and is safe to leave.)") from None

    audit("order_entry", request_id, ctx.user_id, ctx.source, ctx.visibility, action="created", paid=paid, order=order["name"])
    return _result(order, customer_name, paid, "")

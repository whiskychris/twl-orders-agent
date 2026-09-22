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

The Shopify order is ALWAYS created unpaid, on TWL's own "Due on fulfilment" terms - "paid" is not a choice
made here any more. A choice made when approving is only whether to start invoicing:
  approve_send_invoice  create the order, then hand the thread to the invoicing agent, which prepares and
                        (after its own approval) sends a Xero invoice.
  approve_only          create the order the same way, but do not start invoicing. It stays unpaid until
                        someone later asks to invoice it.
Shopify only marks the order paid once its Xero invoice has actually been sent - that is what allows it to
be dispatched. The invoicing agent hands the thread back here for that (see mark_order_paid in main.py),
naming the order to mark paid in structured `context`, not in text a model would have to parse.
"""

import re
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .authorization import ORDER_ENTRY, AuthorizationError, audit, resolve_context
from .config import APPROVE_ROLE, get_shopify_config
from .product_pick import format_eta
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
    {"id": "approve_send_invoice", "label": "Approve & Send Invoice", "style": "primary"},
    {"id": "approve_only", "label": "Approve Order Only"},
]
CHOICE_IDS = frozenset(choice["id"] for choice in CHOICES)
TERMS_TYPES = frozenset({"RECEIPT", "NET", "FIXED", "FULFILLMENT", "UNKNOWN"})


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


def build_input(subject, lines, note, tags, terms_id=None, terms_type=None):
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
        payment_terms = {"paymentTermsTemplateId": terms_id}
        if terms_type == "NET":
            # Net terms are due a number of days after issue, so Shopify needs an issue date to count from.
            payment_terms["paymentSchedules"] = [{"issuedAt": datetime.now(timezone.utc).isoformat()}]
        draft["paymentTerms"] = payment_terms
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

    calc = shop.calculate(build_input(subject, lines, user_note, ["smith-order-entry"]))
    check_pricing(lines, calc)

    variants = shop.get_variants([line["variant_id"] for line in lines])
    warnings = []
    tags = {}  # line number -> "Back Order" or "Pre-order" (with ETA), shown on the line itself
    if subject.get("contact_of"):
        # A company contact ordered as an individual. It is allowed when asked for (typing their email and
        # choosing the personal account), but the approver must see that the company's price list and terms
        # do not apply.
        companies = " and ".join(subject["contact_of"])
        warnings.append(
            f"This is {subject['name']}'s personal account, not the {companies} company account, so the company's "
            "price list and payment terms do not apply."
        )
    if not subject.get("shipping"):
        warnings.append("There is no delivery address on file for this customer, so none was added.")
    for number, (line, got) in enumerate(zip(lines, calc["lines"]), 1):
        info = variants.get(line["variant_id"])
        if not info or info["status"] != "ACTIVE":
            raise EntryError(f"Line {number}: {got['title']} isn't an active product, so it can't be ordered.")
        stock = info.get("stock")
        # A pre-order or an out-of-stock line is tagged on the row itself, not as a separate warning, so
        # it can't be missed. Only people with the inventory capability see an actual low-stock count.
        if info.get("pre_order"):
            eta = f" (ETA {format_eta(info['eta'])})" if info.get("eta") else " (no ETA set)"
            tags[number] = "Pre-order" + eta
        elif stock is not None and stock <= 0:
            tags[number] = "Back Order"
        elif stock is not None and stock < line["quantity"]:
            if ctx.has("inventory"):
                warnings.append(f"Line {number}: only {stock} in stock for {line['quantity']} ordered.")
            else:
                warnings.append(f"Line {number}: there may not be enough stock for {line['quantity']}.")

    # Unpaid orders always use TWL's own "Due on fulfilment" terms, regardless of what's configured for this
    # customer in Shopify: that field isn't otherwise used, and this avoids terms types order entry can't
    # support (a fixed due date has no date to send; net terms need an issue date, handled in build_input).
    terms = shop.default_unpaid_terms()
    payload = {
        "version": 2,
        "target": target,
        "display": _display(subject),
        "lines": [
            {**line, "title": got["title"], "sku": got["sku"], "tag": tags.get(number)}
            for number, (line, got) in enumerate(zip(lines, calc["lines"]), 1)
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
        tag = f"*[{line['tag']}]* " if line.get("tag") else ""
        gross = got["unit_price"] * line["quantity"]
        row = f"{number}. {tag}{line['quantity']} × {got['title']} @ {fmt(got['unit_price'])} = {fmt(gross)}"
        if got["discount_amount"]:
            row += f", less {fmt(got['discount_amount'])}"
        rows.append(row + f" → *{fmt(got['line_total'])}*")
    currency = calc["currency"] or ""
    parts = [
        head,
        "\n".join(rows),
        f"*Total {fmt(calc['total'])} {currency}*".strip(),
    ]
    if warnings:
        parts.append("⚠️ " + " ".join(warnings))
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
        terms_type = payload["terms"].get("type")
        if terms_type is not None and terms_type not in TERMS_TYPES:
            raise ValueError("terms")
        return (
            target, lines, expected, terms_id, terms_type,
            _one_line(payload.get("note"), MAX_NOTE), payload["requested_by"],
        )
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


def _result(order, invoicing, prefix=""):
    name = order["name"]
    url = admin_order_url(order["legacyResourceId"])
    text = f"{prefix}Success: <{url}|Order {name}> created"
    if not invoicing:
        text += " (not yet invoiced)"
    response = {
        "status": "ok",
        "text": text,
        "result": {"order": name, "order_id": order["id"], "invoicing": invoicing, "url": url},
    }
    if invoicing:
        # The invoicing agent takes it from here: it prepares and, after its own approval, sends the
        # Xero invoice, then hands the thread back here (mark_order_paid) once that's done. order_id
        # goes in structured context, not just the text, so invoicing fetches the order by id rather
        # than having to parse it back out of a sentence.
        response["handoff"] = {
            "agent_id": "invoicing",
            "text": f"Prepare a Xero invoice for Shopify order {name}.",
            "context": {"action": "prepare_invoice", "order_id": order["id"], "order_name": name},
        }
    return response


def execute(user, conversation, choice, proposal, request_id):
    """Create the order for an approved draft. Raises ActRefused (nothing created, or the draft exists but
    was not completed). Any other exception means the outcome is unknown."""
    ctx = _authorize(user, conversation, request_id)
    if choice not in CHOICE_IDS:
        raise ActRefused("I need to know whether to approve and invoice, or approve only. Use one of the buttons.")
    if not isinstance(proposal, dict) or proposal.get("kind") != "draft_order":
        raise ActRefused("That isn't an order draft, so I did nothing.")
    token = str(proposal.get("token", ""))
    if not TOKEN.match(token):
        raise ActRefused("That draft has no valid reference, so I did nothing.")

    target, lines, expected, terms_id, terms_type, note, _requester = _validate_payload(proposal.get("payload") or {})
    invoice_now = choice == "approve_send_invoice"
    tag = f"smith-{token}"[:40]

    try:
        existing = shop.find_draft_by_tag(tag)
        if existing and existing.get("status") == "COMPLETED" and existing.get("order"):
            audit("order_entry", request_id, ctx.user_id, ctx.source, ctx.visibility, action="already_created")
            return _result(existing["order"], invoice_now, "Already done. ")
        if existing and existing.get("status") != "OPEN":
            raise ActRefused("A draft for this already exists in an unexpected state, so I did nothing. Check Shopify.")

        if existing:
            draft_id = existing["id"]
        else:
            subject = resolve_subject(target)
            # The Shopify note carries only what the user actually typed - nothing added by this process.
            order_note = note or ""

            calc = shop.calculate(build_input(subject, lines, order_note, ["smith-order-entry"]))
            check_pricing(lines, calc)
            if calc["total"] != Decimal(expected["total"]) or calc["subtotal"] != Decimal(expected["subtotal"]):
                raise ActRefused(
                    f"The price changed since the draft: it was {expected['total']} and is now {calc['total']}. "
                    "Nothing was created. Ask me to draft it again."
                )
            # Always created unpaid, on TWL's own terms - see the module docstring.
            draft = shop.create_draft(
                build_input(subject, lines, order_note, ["smith-order-entry", tag], terms_id, terms_type)
            )
            draft_id = draft["id"]

        order = shop.complete_draft(draft_id)
    except EntryError as exc:
        # A pricing check failed before anything was created.
        raise ActRefused(f"{exc} Nothing was created.") from None
    except ShopifyError as exc:
        raise ActRefused(f"{exc} (If a draft was saved it is not completed, and is safe to leave.)") from None

    audit(
        "order_entry", request_id, ctx.user_id, ctx.source, ctx.visibility,
        action="created", invoicing=invoice_now, order=order["name"],
    )
    return _result(order, invoice_now)


# --- receiving a handoff back from invoicing --------------------------------------------------------

ORDER_ID = re.compile(r"^gid://shopify/Order/\d+$")


def mark_order_paid(user, conversation, order_id, request_id):
    """Mark an existing Shopify order paid. The only caller is the gateway's handoff relay, right
    after the invoicing agent has actually created and sent the Xero invoice for this order - never
    the model, and never from free text: `order_id` comes from the handoff's structured `context`,
    the same Shopify id the invoicing agent read the order by, not parsed from a sentence. Being paid
    is what allows the order to be dispatched. Raises ActRefused; nothing is left ambiguous."""
    roles = [str(role) for role in ((user or {}).get("roles") or [])]
    if APPROVE_ROLE not in roles:
        raise ActRefused("You don't have permission to mark orders paid.")
    try:
        ctx = resolve_context(user, conversation, request_id)
    except AuthorizationError as exc:
        raise ActRefused(str(exc)) from None
    if not ctx.has(ORDER_ENTRY):
        raise ActRefused("Marking orders paid isn't available in this conversation.")
    if not ORDER_ID.match(str(order_id or "")):
        raise ActRefused("That doesn't look like a Shopify order id, so I did nothing.")

    try:
        order = shop.mark_paid(order_id)
    except ShopifyError as exc:
        raise ActRefused(str(exc)) from None

    audit("order_entry", request_id, ctx.user_id, ctx.source, ctx.visibility, action="marked_paid", order=order["name"])
    return {"text": f"Order *{order['name']}* is marked paid and ready to dispatch."}

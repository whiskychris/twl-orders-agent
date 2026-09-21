"""Shopify company lookups, draft order previews, and (from /v1/act only) draft order creation.

Fixed GraphQL operations, validated against Shopify's Admin schema. The model never sends GraphQL.

Two kinds of function live here, and the split matters:
- Reading and previewing (find_companies, find_customers, get_location, calculate): used by the
  model's tools. `draftOrderCalculate` is a mutation in Shopify's schema, but it only calculates
  totals and tax and saves nothing.
- Writing (find_draft_by_tag, create_draft, complete_draft): called ONLY from entry.execute, which
  runs in /v1/act after a person with the approve role pressed a button. No tool reaches these.

Customer personal data never leaves this module toward the model: contact names, emails and phones are
not requested, and addresses are read only to be passed straight back to Shopify on the draft.

Scopes needed: read_companies, read_products, read_inventory, read_draft_orders, write_draft_orders.
Unpaid orders use payment terms on the draft. `draftOrderComplete(paymentPending:)` is deprecated, so it
is not used.
"""

import re
import unicodedata
from decimal import Decimal, InvalidOperation

from .shopify import ShopifyError, clamp, graphql

FIND_COMPANIES = """
query FindCompanies($query: String!, $first: Int!) {
  companies(first: $first, query: $query) {
    nodes {
      id
      name
      mainContact { id }
      contacts(first: 1) { nodes { id } }
      locations(first: 10) { nodes { id name } }
    }
  }
}
"""

FIND_CUSTOMERS = """
query FindCustomers($query: String!, $first: Int!) {
  customers(first: $first, query: $query) {
    nodes { id displayName companyContactProfiles { id } }
  }
}
"""

CUSTOMER = """
query Customer($id: ID!) {
  customer(id: $id) {
    id
    displayName
    companyContactProfiles { id company { name } }
    defaultAddress { address1 address2 city provinceCode zip countryCodeV2 company firstName lastName phone }
  }
}
"""

# Shopify's email search is loose: "chris@x.com" also returns chris+1@x.com, chris+2@x.com and so on. The
# email is fetched only to compare it with what was typed, in code. It is never returned to the model.
CUSTOMER_BY_EMAIL = """
query CustomerByEmail($query: String!) {
  customers(first: 10, query: $query) {
    nodes {
      id
      displayName
      defaultEmailAddress { emailAddress }
      companyContactProfiles {
        id
        company { id name locations(first: 10) { nodes { id name } } }
      }
    }
  }
}
"""

VARIANTS_BY_ID = """
query VariantsById($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on ProductVariant {
      id
      sku
      displayName
      inventoryQuantity
      product {
        status
        title
        tags
        preOrderEta: metafield(namespace: "backendProduct", key: "preOrderEta") { value }
      }
    }
  }
}
"""

LOCATION = """
query Location($id: ID!) {
  companyLocation(id: $id) {
    id
    name
    company {
      id
      name
      mainContact { id }
      contacts(first: 1) { nodes { id } }
    }
    shippingAddress { address1 address2 city zoneCode zip countryCode recipient phone companyName }
    billingAddress { address1 address2 city zoneCode zip countryCode recipient phone companyName }
    buyerExperienceConfiguration { paymentTermsTemplate { id name } }
  }
}
"""

TERMS_TEMPLATES = """
query Terms { paymentTermsTemplates { id name paymentTermsType } }
"""

MONEY = "shopMoney { amount currencyCode }"

CALCULATE = f"""
mutation Calculate($input: DraftOrderInput!) {{
  draftOrderCalculate(input: $input) {{
    calculatedDraftOrder {{
      currencyCode
      subtotalPriceSet {{ {MONEY} }}
      totalTaxSet {{ {MONEY} }}
      totalPriceSet {{ {MONEY} }}
      totalDiscountsSet {{ {MONEY} }}
      lineItems {{
        title
        sku
        quantity
        variant {{ id }}
        originalUnitPriceSet {{ {MONEY} }}
        discountedTotalSet {{ {MONEY} }}
        appliedDiscount {{ value valueType amountSet {{ {MONEY} }} }}
      }}
    }}
    userErrors {{ field message }}
  }}
}}
"""

CREATE_DRAFT = f"""
mutation CreateDraft($input: DraftOrderInput!) {{
  draftOrderCreate(input: $input) {{
    draftOrder {{ id name status totalPriceSet {{ {MONEY} }} }}
    userErrors {{ field message }}
  }}
}}
"""

COMPLETE_DRAFT = f"""
mutation CompleteDraft($id: ID!) {{
  draftOrderComplete(id: $id) {{
    draftOrder {{
      id
      status
      order {{ id name legacyResourceId displayFinancialStatus totalPriceSet {{ {MONEY} }} }}
    }}
    userErrors {{ field message }}
  }}
}}
"""

DRAFT_BY_TAG = """
query DraftByTag($query: String!) {
  draftOrders(first: 1, query: $query) {
    nodes { id name status order { id name legacyResourceId displayFinancialStatus } }
  }
}
"""


def _nodes(connection):
    return (connection or {}).get("nodes") or []


def money(price_set):
    """(Decimal amount, currency) from a MoneyBag, or (None, None)."""
    shop = (price_set or {}).get("shopMoney") or {}
    try:
        return Decimal(str(shop["amount"])), shop.get("currencyCode")
    except (KeyError, InvalidOperation, TypeError):
        return None, None


def _user_errors(payload, what):
    errors = (payload or {}).get("userErrors") or []
    if errors:
        messages = "; ".join(str(error.get("message", error)) for error in errors)
        raise ShopifyError(f"Shopify would not {what}: {messages[:400]}")


def _name_key(text):
    """A name reduced for comparing: lower case, accents folded, punctuation and spacing ignored."""
    folded = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(re.findall(r"[a-z0-9]+", folded))


def _clean_search(text, limit=120):
    """Search terms only. Shopify filter syntax (field:value) is stripped so a company search can't be
    turned into a search of some other field."""
    return re.sub(r"[^\w\s&'.,-]", " ", str(text or ""))[:limit].strip()


# --- reads used by the model's tools ------------------------------------------------------------


def find_companies(query, limit=5):
    """Companies (business customers) matching a name. Returns names and ids only: no contact names,
    emails, phones or addresses."""
    term = _clean_search(query)
    if not term:
        raise ShopifyError("Give me a company name to look for.")
    data = graphql(FIND_COMPANIES, {"query": term, "first": clamp(limit, 1, 10, 5)})
    companies = []
    for node in _nodes(data.get("companies")):
        companies.append(
            {
                "company_id": node["id"],
                "name": node["name"],
                "locations": [
                    {"location_id": loc["id"], "name": loc["name"]} for loc in _nodes(node.get("locations"))
                ],
                "can_order": bool(node.get("mainContact") or _nodes(node.get("contacts"))),
            }
        )
    return {"companies": companies, "note": "Ask which one if more than one matches." if len(companies) > 1 else None}


EMAIL = re.compile(r"[A-Za-z0-9._%+'\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")


def find_by_email(email):
    """Who an email address belongs to, matched EXACTLY (case aside). One email is one customer record, but
    that person may also be a contact at a company, so there can be two accounts to order on: the company
    account (the company's price list and terms) and the personal account (normal prices). Returns names
    and ids only, never the email, phone or address."""
    typed = email.strip().lower()
    data = graphql(CUSTOMER_BY_EMAIL, {"query": f'email:"{typed}"'})
    # Shopify returns near matches too. Keep only the customer whose address is exactly this one.
    exact = [
        node for node in _nodes(data.get("customers"))
        if ((node.get("defaultEmailAddress") or {}).get("emailAddress") or "").strip().lower() == typed
    ]
    if not exact:
        return {
            "accounts": [],
            "note": "No customer has exactly that email address. (Similar addresses were ignored.) New customers "
                    "can't be created here.",
        }
    node = exact[0]
    accounts = []
    for profile in node.get("companyContactProfiles") or []:
        company = profile.get("company") or {}
        if company.get("id"):
            accounts.append({
                "account": "company",
                "name": company["name"],
                "company_id": company["id"],
                "locations": [{"location_id": loc["id"], "name": loc["name"]} for loc in _nodes(company.get("locations"))],
            })
    accounts.append({"account": "personal", "name": node["displayName"], "customer_id": node["id"]})

    companies = [a for a in accounts if a["account"] == "company"]
    if not companies:
        note = (
            f"This email is a personal customer account ({node['displayName']}), with no company account. Use it, and "
            "say which you chose."
        )
    else:
        names = " and ".join(a["name"] for a in companies)
        note = (
            f"This email belongs to {node['displayName']}, who is a contact at {names}. There are two accounts to "
            "order on: the company account (the company's own prices and terms) and their personal account (normal "
            "prices). If the person typing already said which (company or personal), use that. Otherwise ask which "
            "they mean. A company with several locations also needs the location."
        )
    return {"accounts": accounts, "note": note}


def find_customers(query, limit=5):
    """Who an order can be raised for: business customers (companies, with locations) and individual
    customers. Names and ids only: no emails, phones or addresses. Searching by name leaves out a person who
    is a contact at a company, because an order for them by name should go through the company (its price
    list and terms). Searching by their email address (find_by_email) offers both accounts explicitly."""
    typed_email = EMAIL.search(str(query or ""))
    if typed_email:
        return find_by_email(typed_email.group(0))
    term = _clean_search(query)
    if not term:
        raise ShopifyError("Give me a customer name to look for.")
    size = clamp(limit, 1, 10, 5)
    companies = find_companies(term, size)["companies"]
    data = graphql(FIND_CUSTOMERS, {"query": term, "first": size})
    people = [
        {"customer_id": node["id"], "name": node["displayName"]}
        for node in _nodes(data.get("customers"))
        if not node.get("companyContactProfiles")
    ]
    # Shopify's company search is loose: "The Whisky List" also finds The Whisky Company, The Whisky Club and
    # so on. If exactly one customer has exactly the name typed, that is the one, and it goes first.
    wanted = _name_key(term)
    for company in companies:
        company["exact_name_match"] = _name_key(company["name"]) == wanted
    for person in people:
        person["exact_name_match"] = _name_key(person["name"]) == wanted
    companies.sort(key=lambda c: not c["exact_name_match"])
    people.sort(key=lambda p: not p["exact_name_match"])
    exact = [c for c in companies if c["exact_name_match"]] + [p for p in people if p["exact_name_match"]]

    matches = len(companies) + len(people)
    result = {"companies": companies, "individual_customers": people, "note": None}
    if not matches:
        result["note"] = "No existing customer found. New customers can't be created here."
    elif len(exact) == 1:
        only = exact[0]
        result["exact_match"] = {"name": only["name"], "kind": "company" if "company_id" in only else "individual"}
        if "company_id" in only:
            locations = only["locations"]
            if len(locations) == 1:
                result["exact_match"].update(company_id=only["company_id"], location_id=locations[0]["location_id"])
                result["note"] = (
                    f"Exactly one customer is named '{only['name']}' (a company with one location). Use it, and say "
                    "which you chose. The other matches are only similar names."
                )
            else:
                result["note"] = (
                    f"Exactly one customer is named '{only['name']}', but it has {len(locations)} locations. Ask which "
                    "location is meant."
                )
        else:
            result["note"] = (
                f"Exactly one customer is named '{only['name']}' (an individual). Use it, and say which you chose. "
                "The other matches are only similar names."
            )
    elif len(exact) > 1:
        result["note"] = "More than one customer has exactly this name. Ask which is meant."
    elif matches > 1:
        result["note"] = "More than one match, none with exactly that name. Ask which is meant."
    return result


def get_variants(ids):
    """{variant_id: {status, sku, name, stock, pre_order, eta}} for the warnings on a draft. The stock
    number is returned to the caller, which decides what a person is allowed to see of it: a draft always
    says a line is out of stock, but only people with the inventory capability see the count."""
    if not ids:
        return {}
    data = graphql(VARIANTS_BY_ID, {"ids": list(ids)})
    found = {}
    for node in data.get("nodes") or []:
        if node:
            product = node.get("product") or {}
            tags = {tag.lower() for tag in product.get("tags") or []}
            found[node["id"]] = {
                "status": product.get("status"),
                "sku": node.get("sku"),
                "name": node.get("displayName"),
                "stock": node.get("inventoryQuantity"),
                "pre_order": "pre-order" in tags or "[pre-order]" in str(product.get("title", "")).lower(),
                "eta": ((product.get("preOrderEta") or {}).get("value") or "").strip() or None,
            }
    return found


def _address(node):
    """A company address as the MailingAddressInput Shopify wants on a draft order."""
    if not node or not node.get("address1"):
        return None
    address = {
        "address1": node.get("address1"),
        "address2": node.get("address2"),
        "city": node.get("city"),
        "provinceCode": node.get("zoneCode"),
        "zip": node.get("zip"),
        "countryCode": node.get("countryCode"),
        "company": node.get("companyName"),
        "phone": node.get("phone"),
    }
    if node.get("recipient"):
        address["firstName"] = node["recipient"]
    return {key: value for key, value in address.items() if value}


def get_location(location_id):
    """A company location with what a draft order needs. Addresses stay inside this service."""
    data = graphql(LOCATION, {"id": location_id})
    node = data.get("companyLocation")
    if not node:
        raise ShopifyError("I couldn't find that company location in Shopify.")
    company = node.get("company") or {}
    contact = (company.get("mainContact") or {}).get("id")
    if not contact:
        contacts = _nodes(company.get("contacts"))
        contact = contacts[0]["id"] if contacts else None
    template = (node.get("buyerExperienceConfiguration") or {}).get("paymentTermsTemplate")
    return {
        "kind": "company",
        "location_id": node["id"],
        "location_name": node["name"],
        "company_id": company.get("id"),
        "company_name": company.get("name"),
        "contact_id": contact,
        "shipping": _address(node.get("shippingAddress")),
        "billing": _address(node.get("billingAddress")) or _address(node.get("shippingAddress")),
        "terms": {"id": template["id"], "name": template["name"]} if template else None,
    }


def get_customer(customer_id):
    """An individual customer (a personal account) with what a draft order needs. The address stays inside
    this service. If the person is also a contact at a company, `contact_of` names the companies: the order
    is then on their PERSONAL account, at normal prices, and the draft says so. (A search by name never
    offers such a person as an individual. Only an email lookup, or an explicit choice, gets here.)"""
    data = graphql(CUSTOMER, {"id": customer_id})
    node = data.get("customer")
    if not node:
        raise ShopifyError("I couldn't find that customer in Shopify.")
    contact_of = [
        (profile.get("company") or {}).get("name")
        for profile in node.get("companyContactProfiles") or []
        if (profile.get("company") or {}).get("name")
    ]
    address = node.get("defaultAddress") or {}
    mailing = None
    if address.get("address1"):
        mailing = {
            "address1": address.get("address1"),
            "address2": address.get("address2"),
            "city": address.get("city"),
            "provinceCode": address.get("provinceCode"),
            "zip": address.get("zip"),
            "countryCode": address.get("countryCodeV2"),
            "company": address.get("company"),
            "firstName": address.get("firstName"),
            "lastName": address.get("lastName"),
            "phone": address.get("phone"),
        }
        mailing = {key: value for key, value in mailing.items() if value}
    return {
        "kind": "customer",
        "customer_id": node["id"],
        "name": node["displayName"],
        "shipping": mailing,
        "billing": mailing,
        "terms": None,
        "contact_of": contact_of,
    }


def default_unpaid_terms():
    """Payment terms for an unpaid order when the location has none: Due on fulfilment, else Due on
    receipt."""
    data = graphql(TERMS_TEMPLATES)
    templates = data.get("paymentTermsTemplates") or []
    for wanted in ("FULFILLMENT", "RECEIPT"):
        for template in templates:
            if template.get("paymentTermsType") == wanted:
                return {"id": template["id"], "name": template["name"]}
    raise ShopifyError("Shopify has no 'due on fulfilment' or 'due on receipt' payment terms to use.")


def calculate(draft_input):
    """Preview a draft order: totals, tax and each line's price and discount, as Shopify works them
    out (including the company's own price list). Saves nothing."""
    data = graphql(CALCULATE, {"input": draft_input})
    payload = data.get("draftOrderCalculate") or {}
    _user_errors(payload, "price this order")
    calculated = payload.get("calculatedDraftOrder")
    if not calculated:
        raise ShopifyError("Shopify returned no price for this order.")

    subtotal, currency = money(calculated.get("subtotalPriceSet"))
    total, _ = money(calculated.get("totalPriceSet"))
    tax, _ = money(calculated.get("totalTaxSet"))
    discounts, _ = money(calculated.get("totalDiscountsSet"))
    lines = []
    for line in calculated.get("lineItems") or []:
        unit, _ = money(line.get("originalUnitPriceSet"))
        discounted, _ = money(line.get("discountedTotalSet"))
        applied = line.get("appliedDiscount") or {}
        discount_amount, _ = money(applied.get("amountSet"))
        lines.append(
            {
                "variant_id": (line.get("variant") or {}).get("id"),
                "title": line.get("title"),
                "sku": line.get("sku"),
                "quantity": line.get("quantity"),
                "unit_price": unit,
                "line_total": discounted,
                "discount_amount": discount_amount,
            }
        )
    if subtotal is None or total is None:
        raise ShopifyError("Shopify returned an incomplete price for this order.")
    return {
        "currency": currency or calculated.get("currencyCode"),
        "subtotal": subtotal,
        "tax": tax if tax is not None else Decimal("0"),
        "discounts": discounts if discounts is not None else Decimal("0"),
        "total": total,
        "lines": lines,
    }


# --- writes: called only from entry.execute, after an approved button press -----------------------


def find_draft_by_tag(tag):
    """An existing draft order carrying this tag (so a repeated approval never creates two)."""
    data = graphql(DRAFT_BY_TAG, {"query": f'tag:"{tag}"'})
    nodes = _nodes(data.get("draftOrders"))
    return nodes[0] if nodes else None


def create_draft(draft_input):
    data = graphql(CREATE_DRAFT, {"input": draft_input})
    payload = data.get("draftOrderCreate") or {}
    _user_errors(payload, "create the draft order")
    draft = payload.get("draftOrder")
    if not draft:
        raise ShopifyError("Shopify did not return the draft order.")
    return draft


def complete_draft(draft_id):
    data = graphql(COMPLETE_DRAFT, {"id": draft_id})
    payload = data.get("draftOrderComplete") or {}
    _user_errors(payload, "complete the draft order")
    draft = payload.get("draftOrder") or {}
    order = draft.get("order")
    if not order:
        raise ShopifyError("Shopify completed the draft but did not return the order.")
    return order

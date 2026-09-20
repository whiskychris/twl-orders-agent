"""Read-only Shopify Admin GraphQL queries.

Every query here is fixed and reads only. The model never sends GraphQL. It passes a search string
and a few numbers, which go in as GraphQL variables. Customer personal data is fetched only when
`include_customer` is true, using @include directives, so it is never even requested otherwise.

The queries were validated against Shopify's Admin schema. Fields deliberately not requested:
unit costs and margins, payment details, and full billing addresses.
"""

import re
import time
from decimal import Decimal

import requests

from ..config import get_shopify_config

MAX_RETRIES = 3
REQUEST_TIMEOUT = 30

# Search filters that look people up. Without the customers role these are refused. This is a
# best-effort guard to minimise exposure, not a security boundary. The real gate is that customer
# fields are never requested for users without the role.
CUSTOMER_FILTER = re.compile(
    r"\b(email|phone|customer_id|customer|first_name|last_name|billing_address|"
    r"shipping_address|address)\s*:",
    re.IGNORECASE,
)

_token_cache = {"token": None, "expires_at": 0.0}


class ShopifyError(Exception):
    """A Shopify request failed. The message is safe to show to the model."""


class RestrictedError(ShopifyError):
    """The caller lacks the role for this kind of lookup."""


# --- transport ---------------------------------------------------------------------------


def _access_token(config):
    if config.get("access_token"):
        return config["access_token"]

    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]

    try:
        response = requests.post(
            f"https://{config['shop']}/admin/oauth/access_token",
            json={
                "client_id": config["client_id"],
                "client_secret": config["client_secret"],
                "grant_type": "client_credentials",
            },
            timeout=20,
        )
        response.raise_for_status()
        body = response.json()
        token = body["access_token"]
    except (requests.RequestException, KeyError, ValueError) as exc:
        raise ShopifyError(f"Could not get a Shopify access token: {exc}") from exc

    _token_cache["token"] = token
    _token_cache["expires_at"] = now + int(body.get("expires_in", 86399))
    return token


def graphql(query, variables=None):
    config = get_shopify_config()
    url = f"https://{config['shop']}/admin/api/{config['api_version']}/graphql.json"

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.post(
                url,
                json={"query": query, "variables": variables or {}},
                headers={
                    "X-Shopify-Access-Token": _access_token(config),
                    "Content-Type": "application/json",
                },
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ShopifyError(f"Could not reach Shopify: {exc}") from exc

        if response.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
            time.sleep(2 * (attempt + 1))
            continue
        if response.status_code == 401:
            raise ShopifyError("Shopify rejected the access token (401).")
        if response.status_code >= 400:
            raise ShopifyError(f"Shopify returned HTTP {response.status_code}.")

        body = response.json()
        errors = body.get("errors")
        if errors:
            if isinstance(errors, list):
                throttled = any(
                    (error.get("extensions") or {}).get("code") == "THROTTLED"
                    for error in errors
                    if isinstance(error, dict)
                )
                if throttled and attempt < MAX_RETRIES:
                    time.sleep(2 * (attempt + 1))
                    continue
                messages = "; ".join(
                    str(error.get("message", error)) if isinstance(error, dict) else str(error)
                    for error in errors
                )
            else:
                messages = str(errors)
            raise ShopifyError(f"Shopify error: {messages[:500]}")
        return body["data"]

    raise ShopifyError("Shopify kept throttling the request. Try again shortly.")


# --- helpers -----------------------------------------------------------------------------


def clamp(value, low, high, default):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def needs_customer_access(query):
    text = str(query or "")
    return bool(CUSTOMER_FILTER.search(text)) or "@" in text


def _guard(query, include_customer):
    if not include_customer and needs_customer_access(query):
        raise RestrictedError(
            "Looking things up by customer needs the orders.customers role. "
            "Ask the user to request access, or search by order number, SKU or date instead."
        )


def _money(price_set):
    money = (price_set or {}).get("shopMoney") or {}
    if not money:
        return None
    return f"{money.get('amount')} {money.get('currencyCode')}"


def _nodes(connection):
    return (connection or {}).get("nodes") or []


# --- queries -----------------------------------------------------------------------------

SHOP_INFO = """
query ShopInfo {
  shop { name myshopifyDomain currencyCode ianaTimezone }
}
"""

SEARCH_ORDERS = """
query SearchOrders($query: String!, $first: Int!, $reverse: Boolean!, $withCustomer: Boolean!) {
  orders(first: $first, query: $query, sortKey: CREATED_AT, reverse: $reverse) {
    nodes {
      id
      name
      createdAt
      cancelledAt
      displayFinancialStatus
      displayFulfillmentStatus
      currentTotalPriceSet { shopMoney { amount currencyCode } }
      subtotalLineItemsQuantity
      sourceName
      tags
      customer @include(if: $withCustomer) { displayName numberOfOrders }
      shippingAddress @include(if: $withCustomer) { city provinceCode countryCodeV2 }
      lineItems(first: 10) { nodes { title sku quantity } }
    }
    pageInfo { hasNextPage }
  }
}
"""

ORDER_DETAIL = """
query OrderDetail($query: String!, $withCustomer: Boolean!) {
  orders(first: 1, query: $query) {
    nodes {
      id
      name
      createdAt
      processedAt
      cancelledAt
      cancelReason
      closedAt
      displayFinancialStatus
      displayFulfillmentStatus
      note
      tags
      sourceName
      currentSubtotalPriceSet { shopMoney { amount currencyCode } }
      totalShippingPriceSet { shopMoney { amount currencyCode } }
      currentTotalTaxSet { shopMoney { amount currencyCode } }
      totalDiscountsSet { shopMoney { amount currencyCode } }
      currentTotalPriceSet { shopMoney { amount currencyCode } }
      shippingLine { title }
      lineItems(first: 50) { nodes { title variantTitle sku quantity } }
      fulfillments(first: 10) { status createdAt trackingInfo { company number url } }
      customer @include(if: $withCustomer) {
        displayName
        defaultEmailAddress { emailAddress }
        defaultPhoneNumber { phoneNumber }
        numberOfOrders
      }
      shippingAddress @include(if: $withCustomer) {
        name address1 address2 city provinceCode zip countryCodeV2
      }
    }
  }
}
"""

ORDER_TOTALS = """
query OrderTotals($query: String!, $first: Int!, $after: String) {
  orders(first: $first, after: $after, query: $query, sortKey: CREATED_AT) {
    nodes {
      id
      cancelledAt
      displayFinancialStatus
      displayFulfillmentStatus
      currentTotalPriceSet { shopMoney { amount currencyCode } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

SEARCH_PRODUCTS = """
query SearchProducts($query: String!, $first: Int!) {
  products(first: $first, query: $query, sortKey: TITLE) {
    nodes {
      id
      title
      handle
      status
      vendor
      productType
      tracksInventory
      totalInventory
      variants(first: 20) { nodes { sku title price inventoryQuantity } }
    }
    pageInfo { hasNextPage }
  }
}
"""

INVENTORY_BY_SKU = """
query InventoryBySku($query: String!, $first: Int!) {
  inventoryItems(first: $first, query: $query) {
    nodes {
      id
      sku
      tracked
      variants(first: 3) { nodes { displayName product { title status } } }
      inventoryLevels(first: 20) {
        nodes {
          location { name }
          quantities(names: ["available", "on_hand", "committed", "incoming"]) { name quantity }
        }
      }
    }
    pageInfo { hasNextPage }
  }
}
"""

LOW_STOCK = """
query LowStock($query: String!, $first: Int!) {
  productVariants(first: $first, query: $query, sortKey: INVENTORY_QUANTITY) {
    nodes {
      sku
      displayName
      inventoryQuantity
      product { title status }
    }
    pageInfo { hasNextPage }
  }
}
"""

SEARCH_CUSTOMERS = """
query SearchCustomers($query: String!, $first: Int!) {
  customers(first: $first, query: $query) {
    nodes {
      id
      displayName
      defaultEmailAddress { emailAddress }
      defaultPhoneNumber { phoneNumber }
      createdAt
      numberOfOrders
      amountSpent { amount currencyCode }
      lastOrder { name createdAt }
      tags
      defaultAddress { city provinceCode countryCodeV2 }
    }
    pageInfo { hasNextPage }
  }
}
"""


# --- public functions --------------------------------------------------------------------


def shop_info():
    return graphql(SHOP_INFO)["shop"]


def _order_row(node):
    row = {
        "name": node["name"],
        "created_at": node["createdAt"],
        "cancelled": bool(node.get("cancelledAt")),
        "financial_status": node.get("displayFinancialStatus"),
        "fulfillment_status": node.get("displayFulfillmentStatus"),
        "total": _money(node.get("currentTotalPriceSet")),
        "items": node.get("subtotalLineItemsQuantity"),
        "source": node.get("sourceName"),
        "tags": node.get("tags") or [],
        "lines": [
            {"title": line["title"], "sku": line.get("sku"), "quantity": line["quantity"]}
            for line in _nodes(node.get("lineItems"))
        ],
    }
    customer = node.get("customer")
    if customer:
        row["customer"] = {
            "name": customer.get("displayName"),
            "orders": customer.get("numberOfOrders"),
        }
    address = node.get("shippingAddress")
    if address:
        row["ship_to"] = ", ".join(
            part
            for part in (address.get("city"), address.get("provinceCode"), address.get("countryCodeV2"))
            if part
        )
    return row


def search_orders(query, limit=20, oldest_first=False, include_customer=False):
    _guard(query, include_customer)
    data = graphql(
        SEARCH_ORDERS,
        {
            "query": str(query or ""),
            "first": clamp(limit, 1, 50, 20),
            "reverse": not oldest_first,
            "withCustomer": include_customer,
        },
    )
    connection = data["orders"]
    return {
        "orders": [_order_row(node) for node in connection["nodes"]],
        "has_more": connection["pageInfo"]["hasNextPage"],
    }


def get_order(order_name, include_customer=False):
    number = str(order_name or "").strip().lstrip("#")
    if not number:
        raise ShopifyError("An order number is required.")
    data = graphql(ORDER_DETAIL, {"query": f"name:{number}", "withCustomer": include_customer})
    nodes = data["orders"]["nodes"]
    if not nodes:
        return {
            "found": False,
            "note": (
                "No order with that number was found. Orders older than 60 days are only "
                "visible if the store has granted read_all_orders."
            ),
        }

    node = nodes[0]
    order = _order_row({**node, "lineItems": None, "subtotalLineItemsQuantity": None})
    order.pop("lines", None)
    order.update(
        {
            "found": True,
            "processed_at": node.get("processedAt"),
            "closed_at": node.get("closedAt"),
            "cancel_reason": node.get("cancelReason"),
            "note": node.get("note"),
            "subtotal": _money(node.get("currentSubtotalPriceSet")),
            "shipping": _money(node.get("totalShippingPriceSet")),
            "tax": _money(node.get("currentTotalTaxSet")),
            "discounts": _money(node.get("totalDiscountsSet")),
            "shipping_method": (node.get("shippingLine") or {}).get("title"),
            "lines": [
                {
                    "title": line["title"],
                    "variant": line.get("variantTitle"),
                    "sku": line.get("sku"),
                    "quantity": line["quantity"],
                }
                for line in _nodes(node.get("lineItems"))
            ],
            "fulfillments": [
                {
                    "status": fulfillment.get("status"),
                    "created_at": fulfillment.get("createdAt"),
                    "tracking": [
                        {
                            "company": tracking.get("company"),
                            "number": tracking.get("number"),
                            "url": tracking.get("url"),
                        }
                        for tracking in fulfillment.get("trackingInfo") or []
                    ],
                }
                for fulfillment in node.get("fulfillments") or []
            ],
        }
    )
    customer = node.get("customer")
    if customer:
        order["customer"] = {
            "name": customer.get("displayName"),
            "email": (customer.get("defaultEmailAddress") or {}).get("emailAddress"),
            "phone": (customer.get("defaultPhoneNumber") or {}).get("phoneNumber"),
            "orders": customer.get("numberOfOrders"),
        }
    address = node.get("shippingAddress")
    if address:
        order["ship_to"] = {
            key: address.get(key)
            for key in ("name", "address1", "address2", "city", "provinceCode", "zip", "countryCodeV2")
            if address.get(key)
        }
    return order


def summarise_orders(query, include_customer=False, max_pages=4):
    """Count orders and total their value for a search, up to max_pages of 250 orders."""
    _guard(query, include_customer)

    total = Decimal(0)
    count = 0
    cancelled = 0
    currency = None
    by_financial = {}
    by_fulfillment = {}
    cursor = None
    truncated = False

    for _page in range(clamp(max_pages, 1, 8, 4)):
        data = graphql(ORDER_TOTALS, {"query": str(query or ""), "first": 250, "after": cursor})
        connection = data["orders"]
        for node in connection["nodes"]:
            count += 1
            if node.get("cancelledAt"):
                cancelled += 1
            money = (node.get("currentTotalPriceSet") or {}).get("shopMoney") or {}
            if money:
                currency = currency or money.get("currencyCode")
                total += Decimal(str(money.get("amount", "0")))
            financial = node.get("displayFinancialStatus") or "UNKNOWN"
            fulfillment = node.get("displayFulfillmentStatus") or "UNKNOWN"
            by_financial[financial] = by_financial.get(financial, 0) + 1
            by_fulfillment[fulfillment] = by_fulfillment.get(fulfillment, 0) + 1
        info = connection["pageInfo"]
        if not info["hasNextPage"]:
            break
        cursor = info["endCursor"]
    else:
        truncated = True

    return {
        "count": count,
        "cancelled": cancelled,
        "total": f"{total:.2f}",
        "currency": currency,
        "by_financial_status": by_financial,
        "by_fulfillment_status": by_fulfillment,
        "truncated": truncated,
        "note": "Totals are current order totals including tax and shipping."
        + (" Results were cut off at the page limit, so the real figures are higher." if truncated else ""),
    }


def search_products(query, limit=20):
    data = graphql(SEARCH_PRODUCTS, {"query": str(query or ""), "first": clamp(limit, 1, 25, 20)})
    connection = data["products"]
    products = []
    for node in connection["nodes"]:
        products.append(
            {
                "title": node["title"],
                "handle": node.get("handle"),
                "status": node.get("status"),
                "vendor": node.get("vendor"),
                "product_type": node.get("productType"),
                "tracks_inventory": node.get("tracksInventory"),
                "total_inventory": node.get("totalInventory"),
                "variants": [
                    {
                        "sku": variant.get("sku"),
                        "title": variant.get("title"),
                        "price": variant.get("price"),
                        "inventory_quantity": variant.get("inventoryQuantity"),
                    }
                    for variant in _nodes(node.get("variants"))
                ],
            }
        )
    return {"products": products, "has_more": connection["pageInfo"]["hasNextPage"]}


def get_inventory(query, limit=10):
    """Stock by location for inventory items matching a search such as sku:ABC123."""
    data = graphql(INVENTORY_BY_SKU, {"query": str(query or ""), "first": clamp(limit, 1, 25, 10)})
    connection = data["inventoryItems"]
    items = []
    for node in connection["nodes"]:
        variant = (_nodes(node.get("variants")) or [{}])[0]
        product = variant.get("product") or {}
        levels = []
        for level in _nodes(node.get("inventoryLevels")):
            quantities = {q["name"]: q["quantity"] for q in level.get("quantities") or []}
            levels.append(
                {
                    "location": (level.get("location") or {}).get("name"),
                    "available": quantities.get("available"),
                    "on_hand": quantities.get("on_hand"),
                    "committed": quantities.get("committed"),
                    "incoming": quantities.get("incoming"),
                }
            )
        items.append(
            {
                "sku": node.get("sku"),
                "name": variant.get("displayName"),
                "product": product.get("title"),
                "product_status": product.get("status"),
                "tracked": node.get("tracked"),
                "levels": levels,
            }
        )
    return {"items": items, "has_more": connection["pageInfo"]["hasNextPage"]}


def low_stock(threshold=5, limit=50):
    """Active variants whose total inventory is at or below a threshold, lowest first."""
    level = clamp(threshold, 0, 100000, 5)
    data = graphql(
        LOW_STOCK,
        {"query": f"inventory_quantity:<={level}", "first": clamp(limit, 1, 100, 50)},
    )
    connection = data["productVariants"]
    variants = []
    for node in connection["nodes"]:
        product = node.get("product") or {}
        if product.get("status") != "ACTIVE":
            continue
        variants.append(
            {
                "sku": node.get("sku"),
                "name": node.get("displayName"),
                "product": product.get("title"),
                "inventory_quantity": node.get("inventoryQuantity"),
            }
        )
    return {
        "threshold": level,
        "variants": variants,
        "has_more": connection["pageInfo"]["hasNextPage"],
        "note": "Active products only. Inventory is the total across all locations.",
    }


def search_customers(query, limit=10):
    """Customers matching a search. Callers must check the customers role first."""
    data = graphql(SEARCH_CUSTOMERS, {"query": str(query or ""), "first": clamp(limit, 1, 25, 10)})
    connection = data["customers"]
    customers = []
    for node in connection["nodes"]:
        address = node.get("defaultAddress") or {}
        spent = node.get("amountSpent") or {}
        last_order = node.get("lastOrder") or {}
        customers.append(
            {
                "name": node.get("displayName"),
                "email": (node.get("defaultEmailAddress") or {}).get("emailAddress"),
                "phone": (node.get("defaultPhoneNumber") or {}).get("phoneNumber"),
                "customer_since": node.get("createdAt"),
                "orders": node.get("numberOfOrders"),
                "total_spent": (
                    f"{spent.get('amount')} {spent.get('currencyCode')}" if spent else None
                ),
                "last_order": (
                    f"{last_order.get('name')} on {last_order.get('createdAt')}"
                    if last_order
                    else None
                ),
                "tags": node.get("tags") or [],
                "location": ", ".join(
                    part
                    for part in (address.get("city"), address.get("provinceCode"), address.get("countryCodeV2"))
                    if part
                ),
            }
        )
    return {"customers": customers, "has_more": connection["pageInfo"]["hasNextPage"]}

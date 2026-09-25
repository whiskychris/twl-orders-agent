"""Marking an existing order (or some of its items) fulfilled, for orders that leave outside the usual
dispatch process: picked up, used internally, or delivered some other way. Fixed GraphQL only,
validated against Shopify's Admin schema - the model never sends GraphQL, same as order_edit.py.

Shopify fulfils through fulfillment orders, not the order itself: each order has one or more,
each assigned to a location, and each line on one has a `remainingQuantity`. `fulfillmentCreate`
takes (fulfillment order, fulfillment order line, quantity) triples, optional tracking, and whether
to email the customer. It can only fulfil fulfillment orders at one location per call.

find_order_for_fulfilment() is read-only and is used both to build the preview and, again, right
before writing - the approved payload is never trusted for what is still unfulfilled.
"""

from .shopify import ShopifyError, graphql

ORDER_FOR_FULFILMENT = """
query OrderForFulfilment($query: String!) {
  orders(first: 1, query: $query) {
    nodes {
      id
      name
      legacyResourceId
      displayFinancialStatus
      displayFulfillmentStatus
      cancelledAt
      fulfillmentOrders(first: 20, displayable: true) {
        nodes {
          id
          status
          assignedLocation { name }
          supportedActions { action }
          lineItems(first: 100) {
            nodes {
              id
              remainingQuantity
              lineItem { id title sku }
            }
          }
        }
      }
    }
  }
}
"""

CREATE_FULFILMENT = """
mutation CreateFulfilment($fulfillment: FulfillmentInput!) {
  fulfillmentCreate(fulfillment: $fulfillment) {
    fulfillment { id status trackingInfo { company number url } }
    userErrors { field message }
  }
}
"""


def _nodes(connection):
    return (connection or {}).get("nodes") or []


def find_order_for_fulfilment(order_number):
    """The order and every line still waiting to be fulfilled, by order number (1234 or #1234).

    Each entry in `lines` is one fulfillment-order line with something remaining: its ids, the order
    line it belongs to, the title and SKU, how many remain, the location, and whether Shopify will
    let it be fulfilled now (`fulfillable` - false for a fulfillment order on hold or scheduled)."""
    number = str(order_number or "").strip().lstrip("#")
    if not number.isdigit():
        raise ShopifyError("Use a plain order number (for example 1234 or #1234).")
    data = graphql(ORDER_FOR_FULFILMENT, {"query": f"name:{number}"})
    nodes = _nodes(data.get("orders"))
    if not nodes:
        raise ShopifyError(f"I couldn't find order #{number}.")
    order = nodes[0]
    lines = []
    for fulfillment_order in _nodes(order.get("fulfillmentOrders")):
        actions = {item.get("action") for item in fulfillment_order.get("supportedActions") or []}
        location = (fulfillment_order.get("assignedLocation") or {}).get("name") or "an unnamed location"
        for node in _nodes(fulfillment_order.get("lineItems")):
            remaining = int(node.get("remainingQuantity") or 0)
            if remaining <= 0:
                continue
            line_item = node.get("lineItem") or {}
            lines.append(
                {
                    "fulfillment_order_id": fulfillment_order["id"],
                    "fulfillment_order_line_id": node["id"],
                    "line_item_id": line_item.get("id"),
                    "title": line_item.get("title") or "Unnamed item",
                    "sku": line_item.get("sku"),
                    "remaining": remaining,
                    "location": location,
                    "status": fulfillment_order.get("status"),
                    "fulfillable": "CREATE_FULFILLMENT" in actions,
                }
            )
    return {
        "id": order["id"],
        "name": order["name"],
        "legacy_id": order.get("legacyResourceId"),
        "financial_status": order.get("displayFinancialStatus"),
        "fulfillment_status": order.get("displayFulfillmentStatus"),
        "cancelled": bool(order.get("cancelledAt")),
        "lines": lines,
    }


def create_fulfilment(groups, tracking=None):
    """Fulfil the given lines. `groups` is [(fulfillment_order_id, [(fulfillment_order_line_id,
    quantity)])], all at one location. `tracking` is None or {"number", "company"}. The customer is
    never emailed from here (Chris's decision): these orders didn't go through the usual dispatch."""
    fulfillment = {
        "notifyCustomer": False,
        "lineItemsByFulfillmentOrder": [
            {
                "fulfillmentOrderId": fulfillment_order_id,
                "fulfillmentOrderLineItems": [{"id": line_id, "quantity": quantity} for line_id, quantity in lines],
            }
            for fulfillment_order_id, lines in groups
        ],
    }
    if tracking:
        info = {"number": tracking["number"]}
        if tracking.get("company"):
            info["company"] = tracking["company"]
        fulfillment["trackingInfo"] = info
    data = graphql(CREATE_FULFILMENT, {"fulfillment": fulfillment})
    payload = data.get("fulfillmentCreate") or {}
    errors = payload.get("userErrors") or []
    if errors:
        messages = "; ".join(str(error.get("message", error)) for error in errors)
        raise ShopifyError(f"Shopify would not mark it fulfilled: {messages[:400]}")
    created = payload.get("fulfillment")
    if not created:
        raise ShopifyError("Shopify did not return the fulfilment.")
    return created

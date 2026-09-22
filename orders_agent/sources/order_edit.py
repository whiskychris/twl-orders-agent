"""Editing an existing, already-created Shopify order: change a line's quantity (0 removes it), or
add a new product line. Fixed GraphQL only, validated against Shopify's Admin schema - the model
never sends GraphQL, same as draft_orders.py.

Shopify's Order Editing API is a staged, three-part sequence, and that shape is used deliberately as
the prepare/execute wall the rest of this service already has:
  1. orderEditBegin        starts an edit session and returns a CalculatedOrder - nothing on the real
                            order has changed yet.
  2. orderEditSetQuantity / orderEditAddVariant   stage one change each, still against the
                            CalculatedOrder only.
  3. orderEditCommit       the only call that touches the real order.

prepare() in entry.py runs (1) and (2) to build a priced preview from Shopify's own numbers. execute()
(only from /v1/act, after approval) re-runs the ENTIRE sequence from scratch, from the same structured
{variant_id: quantity} map that was approved - it never reuses prepare()'s CalculatedOrder id, the
same reason nothing else in this codebase trusts a stored calculation instead of asking Shopify again
right before writing. There is no explicit way to abandon a CalculatedOrder session; not calling
commit is enough - it never touches the real order.
"""

from .shopify import ShopifyError, graphql

ORDER_FOR_EDIT = """
query OrderForEdit($query: String!) {
  orders(first: 1, query: $query) {
    nodes {
      id
      name
      displayFinancialStatus
      lineItems(first: 50) {
        nodes { id title quantity variant { id } }
      }
    }
  }
}
"""

BEGIN_EDIT = """
mutation BeginEdit($id: ID!) {
  orderEditBegin(id: $id) {
    calculatedOrder { id }
    userErrors { field message }
  }
}
"""

ADD_VARIANT = """
mutation AddVariant($id: ID!, $variantId: ID!, $quantity: Int!) {
  orderEditAddVariant(id: $id, variantId: $variantId, quantity: $quantity) {
    calculatedLineItem { id }
    userErrors { field message }
  }
}
"""

SET_QUANTITY = """
mutation SetQuantity($id: ID!, $lineItemId: ID!, $quantity: Int!) {
  orderEditSetQuantity(id: $id, lineItemId: $lineItemId, quantity: $quantity, restock: true) {
    calculatedLineItem { id }
    userErrors { field message }
  }
}
"""

MONEY = "shopMoney { amount currencyCode }"

CALCULATED_LINE = f"""
id
title
quantity
variant {{ id }}
discountedUnitPriceSet {{ {MONEY} }}
editableSubtotalSet {{ {MONEY} }}
"""

SNAPSHOT = f"""
query CalculatedOrderSnapshot($id: ID!) {{
  node(id: $id) {{
    ... on CalculatedOrder {{
      id
      totalPriceSet {{ {MONEY} }}
      lineItems(first: 50) {{ nodes {{ {CALCULATED_LINE} }} }}
      addedLineItems(first: 50) {{ nodes {{ {CALCULATED_LINE} }} }}
    }}
  }}
}}
"""

COMMIT_EDIT = f"""
mutation CommitEdit($id: ID!, $notifyCustomer: Boolean!) {{
  orderEditCommit(id: $id, notifyCustomer: $notifyCustomer) {{
    order {{ id name legacyResourceId displayFinancialStatus totalPriceSet {{ {MONEY} }} }}
    userErrors {{ field message }}
  }}
}}
"""


def _nodes(connection):
    return (connection or {}).get("nodes") or []


def _user_errors(payload, what):
    errors = (payload or {}).get("userErrors") or []
    if errors:
        messages = "; ".join(str(error.get("message", error)) for error in errors)
        raise ShopifyError(f"Shopify would not {what}: {messages[:400]}")


def find_order_for_edit(order_number):
    """The order to edit, by its Shopify order number (for example 1234 or #1234). Only what editing
    needs: id, name, financial status, and the current lines with their variant ids, so a requested
    change can be matched to an existing line instead of adding a duplicate."""
    number = str(order_number or "").strip().lstrip("#")
    if not number.isdigit():
        raise ShopifyError("Use a plain order number (for example 1234 or #1234).")
    data = graphql(ORDER_FOR_EDIT, {"query": f"name:{number}"})
    nodes = _nodes(data.get("orders"))
    if not nodes:
        raise ShopifyError(f"I couldn't find order #{number}.")
    order = nodes[0]
    return {
        "id": order["id"],
        "name": order["name"],
        "financial_status": order.get("displayFinancialStatus"),
        "lines": [
            {
                "line_item_id": node["id"], "title": node["title"], "quantity": node["quantity"],
                "variant_id": (node.get("variant") or {}).get("id"),
            }
            for node in _nodes(order.get("lineItems"))
        ],
    }


def begin_edit(order_id):
    data = graphql(BEGIN_EDIT, {"id": order_id})
    payload = data.get("orderEditBegin") or {}
    _user_errors(payload, "start editing the order")
    calculated = payload.get("calculatedOrder")
    if not calculated:
        raise ShopifyError("Shopify did not return a calculated order to edit.")
    return calculated["id"]


def add_variant(calculated_order_id, variant_id, quantity):
    data = graphql(ADD_VARIANT, {"id": calculated_order_id, "variantId": variant_id, "quantity": quantity})
    _user_errors(data.get("orderEditAddVariant") or {}, "add that product to the order")


def set_quantity(calculated_order_id, line_item_id, quantity):
    data = graphql(SET_QUANTITY, {"id": calculated_order_id, "lineItemId": line_item_id, "quantity": quantity})
    _user_errors(data.get("orderEditSetQuantity") or {}, "change that line's quantity")


def snapshot(calculated_order_id):
    """The calculated order's current state after staging: every line (existing, with any staged
    change already reflected) and every newly-added line, and the running total."""
    data = graphql(SNAPSHOT, {"id": calculated_order_id})
    node = data.get("node")
    if not node:
        raise ShopifyError("Shopify lost track of the order edit in progress. Start again.")
    return node


def commit_edit(calculated_order_id, notify_customer=False):
    data = graphql(COMMIT_EDIT, {"id": calculated_order_id, "notifyCustomer": notify_customer})
    payload = data.get("orderEditCommit") or {}
    _user_errors(payload, "save the order changes")
    order = payload.get("order")
    if not order:
        raise ShopifyError("Shopify committed the edit but did not return the order.")
    return order

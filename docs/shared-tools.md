# Shared tools

Other agents' models can call some of this agent's **read-only** tools directly, through the gateway, so
they can use Shopify order, product and stock data without asking this agent a question in words (a second
model loop, which is slow). Today that is the rewards agent. The contract is `docs/agent-contract.md` in
twl-gateway, "Shared tools".

## What is shared
`orders_agent/shared.py` holds one definition per tool, used both by this agent's own model (`tools.py`
registers them) and by other agents (`/v1/tools` and `/v1/tool` in `main.py`), so the two can't drift:

| Tool | Capability |
|---|---|
| `search_orders`, `get_order`, `summarise_orders` | `orders` |
| `search_products` | `products` |
| `get_inventory`, `low_stock` | `inventory` |
| `search_customers` | `customers` |
| `find_variant`, `check_price` | `order_entry` |

Nothing that prepares a draft or writes is shared. To draft an order, another agent **hands the thread off**
to this one (below).

## Access
The gateway sends the person the calling agent is answering (from a tool grant it issued, never a user the
calling agent names), with the conversation's visibility. `/v1/tools` and `/v1/tool` run the same
`resolve_context` as `/v1/message`, so the same person sees the same data from Slack or through another
agent: customers and order entry are withheld outside a DM or `order_entry_channels`, stock figures need
`inventory`, and so on. `/v1/tool` re-checks the capability on every call. Each call is audited as a `tool`
line, like any other tool use.

## The new-order handoff from rewards
When a rewards user approves "send this order to Orders", the gateway hands the thread here with
`context = {"action": "prepare_draft_order", "customer_id": "gid://shopify/Customer/…", "lines":
[{"variant_id": "gid://shopify/ProductVariant/…", "quantity": 2}], "note": "…"}`. `main.py` passes it
straight to `entry.prepare` (no model), which checks `order_entry`, prices it and posts the draft with the
usual buttons. Everything after that is ordinary order entry (`docs/order-entry.md`).

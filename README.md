# twl-orders-agent

The orders assistant. The team asks it questions in Slack about **orders, products and stock**, and
(for people allowed to) **customers**. It starts with Shopify, and is built to take other order systems
later. It reads freely. It can also raise a new order for an existing customer, but only as a draft that a
person with approval rights must approve first (see `docs/order-entry.md`).

```
Slack → Smith → twl-gateway → twl-orders-agent → Shopify
```

Slack reaches it only through **twl-gateway**, which owns Slack identity, roles, conversation state and
routing. This service implements the gateway's agent contract v1 and stays stateless.

## What it can do
| Ask | Tool |
|---|---|
| "How many orders yesterday?", "what did we sell this week?" | `summarise_orders` |
| "Status of order 1234?" | `get_order` |
| "Which orders are unfulfilled?" | `search_orders` |
| "How many Ardnahoe do we have?" | `search_products`, `get_inventory` |
| "What's running low?" | `low_stock` |
| "Has Jane Smith ordered before?" (restricted) | `search_customers` |
| "New order for Nicks Wine Merchants: 6 x AH10, 10% off" (needs approval) | `find_company`, `find_variant`, `prepare_draft_order` |

## Who can see what
The gateway proves who is asking and passes a stable `user_id` (for example `twl:chris-ross`) with the
conversation's visibility (`dm` or `channel`). This service owns what each person may see, as a list of
**capabilities** in Secret Manager (`twl-orders-authz`):

| Capability | Gives |
|---|---|
| `orders` | order search, detail and summaries (no customer fields) |
| `products` | product and variant search (no stock quantities) |
| `inventory` | stock by location, low stock, and stock quantities on products |
| `customers` | customer names, emails, phones, addresses, the order note, and free-text order search. **Direct messages only.** |
| `order_entry` | prepare a new order for an existing customer (company name only, never contact details). A DM, or a channel listed in `order_entry_channels`. Creating it also needs the gateway role `orders.approve`. |

Roles from the gateway only open the door (`orders.use`). Anyone not on the list gets nothing. If the list
can't be read, nobody gets anything. Tools for a capability the user lacks are not registered, and fields
they may not see are never requested from Shopify. See `docs/authorization.md`.

The model can never write. The only write is creating an approved order, in `orders_agent/entry.py` from
`/v1/act`, after a button press. It cannot see costs or margins.

## Layout
```
main.py                    Flask app: /health, /v1/describe, /v1/message, /v1/act, /shopify-test
CLAUDE.md                  the assistant's instructions and hard rules
orders_agent/
  config.py                Shopify credentials (Secret Manager) and roles
  runtime.py               runs the model with the caller's tools
  tools.py                 the tools the model can call, built per request from roles
  anthropic_auth.py        Anthropic access via workload identity
  sources/shopify.py       fixed, read-only, schema-validated Shopify queries
docs/                      setup.md, shopify.md
tests/                     unit tests for the pure logic
```

## Adding another system later
1. Write read-only functions under `orders_agent/sources/<system>.py`.
2. Register them as tools in `orders_agent/tools.py` (gate anything sensitive on a role).
3. Update `CLAUDE.md` so the assistant knows when to use them.
No change to the gateway or Smith is needed.

## Rules of the road
- `main` deploys to production. Change it through pull requests, especially `CLAUDE.md`.
- The model never gets arbitrary GraphQL or any write tool. Keep it that way.
- No shipment logic here. Inbound shipments belong to the shipments agent.

## Tests
`python -m unittest discover tests`

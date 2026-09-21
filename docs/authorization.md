# Authorization

Who may see what is decided **in this service, in code, before any Shopify data is fetched**. The model
never decides, and nothing the user types can change it.

## Flow
1. Slack proves who is asking. **twl-gateway** maps the Slack user to a stable `user_id`
   (`twl:chris-ross`), works out whether the conversation is a DM or a channel, and sends both with every
   request. It also checks the coarse role `orders.use`.
2. This service re-checks `orders.use`, then looks `user_id` up in the secret `twl-orders-authz`.
3. The result is an `AuthContext`: the user's capabilities for this request, minus `customers` if the
   conversation is not a DM.
4. The context decides which tools exist and which fields Shopify is asked for. The model is told what it
   can and cannot see, but the enforcement is above, not in the prompt.

## Capabilities
| Capability | Tools | Also controls |
|---|---|---|
| `orders` | `search_orders`, `get_order`, `summarise_orders` | |
| `products` | `search_products` | |
| `inventory` | `get_inventory`, `low_stock` | stock quantities in `search_products` |
| `customers` | `search_customers` | customer, shipping address and order note fields on orders; free-text order search |

Add a capability by adding it to `CAPABILITIES` in `orders_agent/authorization.py`, registering the tools
under it in `tools.py`, and adding tests.

## Rules the code enforces
- **Deny by default.** No `user_id`, no `orders.use`, not on the list, an empty list, or only unknown
  capability names: refused, and nothing is looked up.
- **Fail closed.** If the permissions secret can't be read or is malformed, the request fails with a 503.
  Nothing is served from a stale copy once the five-minute cache expires.
- **Identity is the gateway's `user_id`.** A Slack id, name or email in a message counts for nothing.
- **Customer data is DM only.** Names, emails, phones, addresses and the order note (which often contains
  them) are shown only for a user with `customers`, and only in a DM. Unknown or missing visibility counts
  as a channel.
- **Tools are absent, not refused.** A tool for a capability the user lacks is never registered, and its
  handler re-checks anyway.
- **Fields are not fetched.** Customer, note and stock fields sit behind GraphQL `@include` flags set from
  the context. The tool schemas have no argument the model could use to switch them on.
- **Search can't be used to probe.** Without `customers`, order searches must be made only of allowed
  structured filters (dates, statuses, SKU, tag, order number, source). Free text, emails, names and
  unknown filters are refused, because otherwise "orders for jane@example.com" would confirm a customer
  exists.
- **Same person, same access, whatever the interface.** Access depends on `user_id` and visibility, not on
  the front end.

## The permissions secret
`twl-orders-authz`, readable only by the `twl-orders-agent` service account:
```json
{"users": {"twl:chris-ross": {"name": "Chris Ross", "capabilities": ["orders", "products", "inventory", "customers"]}}}
```
The `name` here is what appears in prompts and logs. Keys must match `user_id` in `twl-gateway-config`.

## Audit log
One JSON line per decision to stdout (Cloud Run logs), with no customer data, message text or order
contents:
```json
{"audit": "request", "request_id": "…", "user_id": "twl:sam-orders", "source": "slack", "visibility": "dm", "granted": ["orders", "products"], "withheld": []}
{"audit": "denied", "request_id": "…", "user_id": "twl:stranger", "reason": "no_capabilities", "source": "slack", "visibility": "dm"}
{"audit": "tool", "request_id": "…", "user_id": "…", "tool": "get_order", "capability": "orders", "allowed": true}
```
Filter in Logs Explorer with `jsonPayload.audit` or the text `"audit"`.

## What this does not do
- Shopify's own staff permissions are not used. The Shopify app has one set of read scopes for everyone,
  and this service applies the per-person limits.
- Order-level or market-level restrictions (a user who sees only some orders) are not modelled.
- Rate and cost limits per user are not implemented yet.

# Shopify access

The agent reads Shopify through the Admin GraphQL API, and (for order entry only) creates approved orders.

## Create the app
Create an app in the Shopify admin for the TWL store (Dev Dashboard app, or a custom app if your store
still offers them), install it on the store, and grant only these scopes:

| Scope | Why |
|---|---|
| `read_orders` | orders (last 60 days) |
| `read_all_orders` | orders older than 60 days. Needs Shopify's approval for the app. Optional. |
| `read_products` | products and variants |
| `read_inventory`, `read_locations` | stock levels by location |
| `read_customers` | customers. Customer data is only shown to users with the `customers` capability, in a DM or the sales channel (see `docs/authorization.md`) |
| `read_assigned_fulfillment_orders`, `read_merchant_managed_fulfillment_orders`, `read_third_party_fulfillment_orders` | fulfilments and tracking on an order |

| `read_companies` | B2B companies, locations and contacts, for order entry |
| `read_draft_orders` | finding an existing draft order, so a repeated approval never makes two |
| `read_publications` | which catalogs (Trade Core, Trade IBs, Trade Special Releases) a product is published to, for product picking |
| `write_draft_orders` | pricing a draft (nothing saved) and, after approval only, creating and completing it |

`write_draft_orders` is the only write scope. No model tool can use it: it is called only from
`orders_agent/entry.py` in `/v1/act`, after a person with the approve role presses a button
(`docs/order-entry.md`). Never add other write scopes without a matching, approval-gated code path.

## Credentials
Store them in Secret Manager as `twl-shopify-config`, either:
```json
{"shop": "your-store.myshopify.com", "access_token": "shpat_...", "api_version": "2026-07"}
```
or, for a Dev Dashboard app that issues client credentials:
```json
{"shop": "your-store.myshopify.com", "client_id": "...", "client_secret": "...", "api_version": "2026-07"}
```
In the second form the service exchanges them for a short-lived token and caches it. Verify this against
your app type after the first deploy. `GET /shopify-test` (with invoker rights) confirms the credentials work
and returns only the shop name.

## API version
`api_version` defaults to `2026-07`. Shopify retires versions after about a year, so bump it deliberately and
re-validate the queries in `orders_agent/sources/shopify.py`.

## What is queried
Fixed queries only, validated against Shopify's Admin schema. The model supplies a search string and a few
numbers, which go in as GraphQL variables. It never writes GraphQL.

Not requested at all: unit costs and margins, payment details, full billing addresses. Customer names, emails,
phones, shipping addresses, tags and the order note are requested only for callers with the `customers`
capability (in a DM or the sales channel), and stock quantities only with `inventory` (all via `@include`
directives), so they are never fetched for anyone else. The Shopify app itself holds one set of scopes for
everyone. The per-person limits are enforced by this service.

### Filtering orders by the customer's tags
Shopify's order search has no filter for the CUSTOMER's tags (only `customer_id`, and the order's own
tags) - confirmed against Shopify's own `OrderConnection` filter list. `search_orders` adds its own
`customer_tag:a,b` (comma means OR), handled entirely in `orders_agent/sources/shopify.py`
(`_extract_customer_tags`): the token is stripped before the rest of the query reaches Shopify, and
matches are found by paging through orders newest-first and checking each one's already-fetched
`customer.tags`, stopping once enough are found or `CUSTOMER_TAG_SCAN_MAX_PAGES` (6 pages of 50 = 300
orders) is hit - reported honestly via a `note` if it gives up early, rather than silently returning
fewer than asked. Needs the `customers` capability, same as any other customer field. TWL's own use:
**trade customers** (bottle shops, online retailers, bars, pubs, restaurants) are tagged `Off-Prem` or
`On-Prem`, so `customer_tag:Off-Prem,On-Prem` means "trade customers" - see `CLAUDE.md`.

## Limits to know about
- Orders older than 60 days are invisible without `read_all_orders`. The assistant says so.
- `summarise_orders` reads up to 2,000 orders (8 pages of 250) and reports if it was cut off.
- Shopify rate-limits by query cost. The client retries throttled requests a few times.
- Shopify "protected customer data" rules apply to customer fields. Custom apps on your own store are
  fine, but check the app's data-protection settings.

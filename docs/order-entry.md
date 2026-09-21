# Order entry

Sales (for example Jimmy, in the sales channel) tag Smith with a new order for an **existing customer**: the
customer, the products, the quantities, and optionally a discount per product. The customer can be a
**company** (a B2B account, priced with its own price list) or an **individual customer** (normal prices),
because not every customer is set up as a company. Smith prepares a draft, posts it in the thread, and only
after someone with approval rights presses a button is the order created in Shopify.

```
"orders: new order for Nicks Wine Merchants: 6 x Arran 10, 12 x GlenAllachie 12 with 10% off"
   -> draft (priced by Shopify, posted with three buttons)
   -> [Create order (invoiced, paid)] [Create order (not invoiced, unpaid)] [Cancel]
   -> "Order #1234 is marked as paid (invoiced). Open #1234 in Shopify" (with a link)
```

## Who can do what
| | Needs |
|---|---|
| Ask for a draft | gateway role `orders.use`, and the `order_entry` capability in `twl-orders-authz` |
| Press Create | gateway role `orders.approve`, and the `order_entry` capability |
| Where | a DM with Smith, or a channel listed in `order_entry_channels` (for example #sales) |

`twl-orders-authz` becomes:
```json
{"users": {"twl:jimmy-shore": {"name": "Jimmy Shore", "capabilities": ["orders", "products", "inventory", "customers", "order_entry"]}},
 "order_entry_channels": ["C0123SALES"]}
```
Anyone with `orders.approve` who presses a button on the draft can approve it. It does not have to be the
person who asked. Customer contact details stay DM-only even for people with `customers`: order entry
shows only the company and location name.

## What happens, and where the safety is
1. **The model prepares. It cannot create.** It has three tools: `find_customer`, `find_variant` and
   `prepare_draft_order`. None writes to Shopify. `prepare_draft_order` asks Shopify to *price* the order
   (`draftOrderCalculate`, which saves nothing), checks the answer, and hands the result to the gateway as a
   proposal.
2. **The draft text is written by code**, from Shopify's own numbers. The model's words are dropped
   when a draft is prepared, so a total can't be misstated.
3. **Pricing is the customer's own.** For a company, the order is raised for the company, location and its main
   contact, so Shopify applies that company's price list and tax. For an individual it is raised for the
   customer at normal prices. Addresses go straight back to Shopify and never reach the model. A person who is
   a contact at a company can't be ordered as an individual, because that would skip the company's prices and
   terms: the search leaves them out and the order has to go through the company. A customer with no delivery
   address on file gets a warning on the draft, not a refusal.
4. **Discounts are verified.** Shopify's preview must show the discount as intended (a per-unit dollar
   amount is sent as the whole-line amount, then checked). If it doesn't match, the draft is refused, never
   quietly created wrong.
5. **Approval is a button.** The proposal carries two named choices, so paid or unpaid is decided in the
   same click as approving. Typed "approve" is refused for this kind of proposal, and typed "cancel" works.
6. **Creating is deterministic and re-checked** (`entry.execute`, from `/v1/act`, no model): the approver's
   role, the capability, the channel, the payload, and the price (if the total changed since the draft,
   nothing is created and you're told to ask again).
7. **Safe to repeat.** The draft carries the tag `smith-<proposal token>`. A repeated approval finds the
   existing draft or order instead of making another.
8. **A clear outcome.** A refusal (4xx) tells the gateway nothing was changed. An unexpected failure (5xx)
   makes the gateway say "the result is unknown, check Shopify before retrying".

## Paid and unpaid
"Paid" means the order was invoiced through Xero. This agent never touches Xero. It records the choice:
- **Create (paid):** the draft is completed normally, so Shopify records the order as paid. The order note
  says "Marked paid: invoiced in Xero" and who approved.
- **Create (unpaid):** the draft gets payment terms first (the company location's own terms, else "Due on
  fulfilment"), so Shopify creates it with payment outstanding. It is your back-order / waiting-to-invoice
  state. The deprecated `paymentPending` argument is not used.

## Customer emails
Customers are emailed by Shopify's usual order notifications when the order is **created**, not while it is a
draft. This agent doesn't send email and doesn't control that: the draft says so, so approvers know that
pressing Create can email the customer. Test orders should be for TWL's own account or a customer whose email
is yours.

## Shopify setup
The app needs these extra scopes (already listed in `shopify.app.toml`): `read_companies`,
`read_draft_orders`, `write_draft_orders` and `read_publications` (product picking reads catalog membership). Update the app's scopes with the Shopify CLI
(`shopify app deploy`) and approve the new access in the store admin. Until then the tools fail with a
clear message and nothing is created.

## Verify on the first real order
A few behaviours can only be confirmed against the live store. Start with a small order for TWL's own
company ("The Whisky List", which has Net 30 terms) and check:
1. **Paid vs unpaid.** Paid shows as paid with no payment terms. Unpaid shows the payment terms and an
   outstanding balance. If a company's default terms make a "paid" order look pending, tell me.
2. **Discounts and totals.** The draft's numbers match what Shopify shows on the order.
3. **An individual customer** (one who isn't a company). Confirm normal prices, and that payment terms can
   be put on their draft. If Shopify refuses terms for individuals, "unpaid" will fail with a clear message.
4. **Addresses** carry over from the company location or the customer's default address.
5. **The customer email** arrives (or doesn't) as Shopify's notification settings say.

## Not covered
New customers, shipping charges, delivery dates, editing or cancelling an existing order, refunds, and
checking the invoice actually exists in Xero (the person choosing "paid" is stating it).

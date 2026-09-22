# Order entry

Sales (for example Jimmy, in the sales channel) tag Smith with a new order for an **existing customer**: the
customer, the products, the quantities, and optionally a discount per product. The customer can be a
**company** (a B2B account, priced with its own price list) or an **individual customer** (normal prices),
because not every customer is set up as a company. Smith prepares a draft, posts it in the thread, and only
after someone with approval rights presses a button is the order created in Shopify.

```
"orders: new order for Nicks Wine Merchants: 6 x Arran 10, 12 x GlenAllachie 12 with 10% off"
   -> draft (priced by Shopify, posted with three buttons)
   -> [Approve & Send Invoice] [Approve Order Only] [Cancel]
   -> "Success: Order #1234 created" (Order #1234 links to the order in Shopify), then, if
      Approve & Send Invoice was pressed, the invoicing agent's own invoice preview follows in the
      same message, for its own approval.
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
person who asked. The draft itself never shows customer contact details, in a DM or a channel, whether
or not the requester also has `customers`: order entry shows only the company and location name (see
"Finding a customer by email address" below for the one exception, matching by an email address the user
already typed). The separate `customers` capability (full customer details and search) works the same
places order entry does - a DM or an `order_entry_channels` channel - and is withheld everywhere else.

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
   a contact at a company is left out of name searches as an individual, so nobody skips the company's prices
   and terms by accident. They are offered by **email address** instead (see below), where the personal account
   is an explicit choice and the draft warns about it. A customer with no delivery address on file gets a
   warning on the draft, not a refusal.
4. **Discounts are verified.** Shopify always treats a line item's fixed-amount discount `value` as an
   amount **per unit**, and multiplies it by the line's quantity itself to get the total applied - a
   `per_unit` discount is sent exactly as given, and a `line_total` discount (dollars off the whole line)
   is divided by quantity before sending, so Shopify's own multiplication lands back on the intended
   total. Found live: an earlier version sent the already-multiplied total for `per_unit`, so Shopify
   multiplied it by quantity again (6x too much on a 6-bottle line) - caught by the check below, so
   nothing was created wrong, just refused. Shopify's preview is always re-checked against what was
   intended; if it doesn't match, the draft is refused, never quietly created wrong.
5. **Approval is a button.** The proposal carries two named choices, so whether to start invoicing is decided
   in the same click as approving. Typed "approve" is refused for this kind of proposal, and typed "cancel"
   works.
6. **Creating is deterministic and re-checked** (`entry.execute`, from `/v1/act`, no model): the approver's
   role, the capability, the channel, the payload, and the price (if the total changed since the draft,
   nothing is created and you're told to ask again).
7. **Safe to repeat.** The draft carries the tag `smith-<proposal token>`. A repeated approval finds the
   existing draft or order instead of making another.
8. **A clear outcome.** A refusal (4xx) tells the gateway nothing was changed. An unexpected failure (5xx)
   makes the gateway say "the result is unknown, check Shopify before retrying".

## Finding a customer by email address
Sales can give an email instead of a name: `orders: new order for chris@thewhiskylist.com.au (company account): 6 x Arran 10`,
or "... personal account". One email is one customer record, but that person may also be a contact at a company,
so there can be two accounts to order on:
- **Company account**: raised for the company and its location, with the company's price list and payment terms.
- **Personal account**: raised for the customer at normal prices. The draft warns that the company's price list
  and terms do not apply.

If both exist and the person didn't say which, the agent asks. If the email is only a personal account it uses
that. A company with several locations needs the location too.

**The match is exact.** Shopify's own email search is loose: searching `chris@thewhiskylist.com.au` also returns
`chris+1@`, `chris+2@` and so on, which are different people. The agent fetches the addresses only to compare them
with what was typed, in code, and keeps the exact one. Lookalikes are ignored, and neither they nor any email
address is ever shown or passed to the model. What comes back is the account type, the name of the person or
company, and ids.

**Privacy note:** an email lookup tells whoever has order entry that the address is a customer, their name and the
companies they are a contact at. Order entry is limited to the people you named and to DMs and #sales, and names
are what the drafts already show.
## The order is always created unpaid
Every order this agent creates is **unpaid** in Shopify, on TWL's own "Due on fulfilment" payment terms -
never the customer's own terms in Shopify, which this process doesn't otherwise use, and never a choice made
here any more. Always using the same terms sidesteps two things a customer's own terms could otherwise need:
a net terms template (for example "Net 30") needs an issue date, and a fixed-due-date template has no due
date to send at all. The deprecated `paymentPending` argument is not used.

**Being paid is what allows an order to be dispatched**, and that only happens once its Xero invoice has
actually been sent - this agent does not decide that for itself. The choice made when approving is only
whether to start invoicing now:
- **Approve & Send Invoice:** creates the order, then hands the Slack thread to the invoicing agent
  (`twl-invoicing-agent`), which prepares a Xero invoice for its own, separate approval, and sends it once
  approved.
- **Approve Order Only:** creates the order and stops there. It stays unpaid until someone later asks to
  invoice it.

Either way, once the invoicing agent has actually sent the invoice, it hands the thread back here, naming
the order to mark paid in structured data (never free text a model would have to parse) - see
`mark_order_paid` in `orders_agent/entry.py`. This agent never touches Xero itself; it only creates the
Shopify order and, on that handoff back, marks it paid.

**`orderMarkAsPaid` currently fails on a Shopify permission, not a bug.** Found live: this mutation needs
`write_orders` (already in `shopify.app.toml`) **and** a separate Shopify staff permission,
`mark_orders_as_paid`, which isn't the store owner's own permission and isn't self-serve from Settings >
Users and permissions - check Settings > Apps and sales channels > Develop apps > TWL Orders Agent for an
additional permission request there, or otherwise treat it as a Shopify support question. Until it's
granted, `mark_order_paid` refuses with a friendly message naming the order and linking straight to it in
the Shopify admin, rather than relaying Shopify's raw API error - the invoice has already been sent by
this point either way, so it's a manual "mark it paid yourself" step, not a lost order.

## Editing an existing order
Sales can ask to change an order that's already been created but not yet paid: `"orders: on #1234, change Arran 10
to 12"`, `"add 3 x GlenAllachie 12 to #1234"`, `"remove Arran 10 from #1234"`. The model has one tool for this,
`prepare_order_edit`, alongside the existing `get_order` (to see current lines) and `find_variant` (to resolve a
product name to the id a change needs). Sending the **same variant id as an existing line** means "change that
line's quantity"; any other variant id means "add a new line". Setting a line's quantity to `0` removes it - there
is no separate remove tool, matching Shopify's own API shape.

1. **Unpaid orders only.** If the order's `displayFinancialStatus` is `PAID`, the request is refused before any
   edit call is made. This keeps editing away from orders that are already invoiced and dispatch-ready.
2. **Priced by Shopify, not the model**, the same as a new order. `prepare_order_edit` uses Shopify's own staged
   Order Editing API (`orderEditBegin` -> `orderEditSetQuantity`/`orderEditAddVariant` -> a snapshot of the
   calculated order) to build the preview, so the draft's new line totals and new order total are Shopify's own
   numbers, not computed here.
3. **Re-checked before committing.** `execute()` never reuses the calculated order from the preview - on approval
   it re-fetches the live order, re-runs the whole begin/stage sequence from the approved `{variant_id: quantity}`
   changes, and re-verifies the total against what was approved before calling `orderEditCommit`. If the order
   moved (someone else edited it, a price changed) the total won't match and nothing is committed.
4. **Silent commit.** The edit commits with `notifyCustomer: false` - the customer is not emailed about the
   change. Shopify's own order-created and payment notifications still apply as usual.
5. **Discounts are out of scope for v1.** This only changes quantities and adds plain lines at Shopify's normal
   price for that line. Up to 30 line changes per request.
6. **Needs the `write_order_edits` scope**, separate from `write_orders` - found live: without it, every
   `orderEditBegin` call is refused. See `docs/shopify.md`.

## Invoicing an existing, unpaid order
For an order that was created with **Approve Order Only** (or has otherwise never been invoiced) and is
still unpaid: `"orders: invoice order 1234"`, `"invoice this order"`. This is the piece that completes the
two-step flow described above - Approve Order Only defers invoicing, and this is how it happens later,
on demand, instead of only at creation time.

The model has one tool, `prepare_invoice_for_order` (`entry.prepare_invoice_handoff`), which:
1. Looks the order up (`order_editing.find_order_for_edit`, extended with pricing - see below) and
   refuses if it's already `PAID` (meaning it's already been invoiced through this system - nothing to
   do).
2. Posts the order's **current lines and total**, priced from Shopify's own numbers - not a blind
   confirmation of an order number - with two buttons:
   - **Send Invoice**: `execute()` re-checks the order is still unpaid (never trusting what `prepare()`
     saw), then hands the thread to `twl-invoicing-agent` with the same `prepare_invoice` handoff order
     creation's **Approve & Send Invoice** choice uses (see `_result` above) - the exact same flow, just
     started later instead of at creation. The invoicing agent then runs its own, separate approval for
     the actual Xero invoice, and hands the thread back here once it's sent, to mark the order paid.
   - **Edit Order**: a detour, not a dead end. `execute()` sends nothing anywhere for this choice - it's
     a plain prompt asking what to change, and the actual edit runs through `prepare_order_edit` exactly
     as in the previous section. Once that edit is *committed* (`_execute_order_edit`), its own success
     reply carries a **self-handoff** back to `orders` (`context.action: "prepare_invoice_confirmation"`),
     which `main.py`'s `/v1/message` dispatches deterministically - no model involved, same shape as the
     existing `mark_order_paid` dispatch - straight back into `prepare_invoice_handoff` for the SAME
     order, now re-priced with whatever just changed. This makes a successful edit on any order always
     end by re-offering to invoice it, since a successful edit only ever leaves an order unpaid (paid
     orders are refused before anything is staged) - not only edits that started from this screen.

     **Edit Order's own reply sets `no_op: true`** (a `twl-gateway` field - see its
     `docs/smith-contract.md`, "No-op actions"), because pressing it writes nothing. Found live: without
     it, the gateway closed the proposal as "applied" like any real write, which made a passive-reply
     channel (for example #sales) stop answering the very next, untagged message ("add 3 x GlenAllachie
     12") with no error at all - it just went silent, because `has_finished_proposal` treats "applied"
     as the thread having concluded. `no_op` tells the gateway this one didn't.

This is deliberately a thin wrapper: no new Xero logic, no new mapping rules - it just gets an
already-built order into the exact same handoff the "new order" flow already uses. Reuses the
`order_entry` capability and the existing `order_editing.find_order_for_edit` lookup, extended with each
line's current price and the order's total (needed for the confirmation preview, not for staging an
edit) so no second Shopify query was needed for either use.

## Customer emails
Customers are emailed by Shopify's usual order notifications when the order is **created**, not while it is a
draft. This agent doesn't send email and doesn't control that. Test orders should be for TWL's own account or
a customer whose email is yours.

## Shopify setup
The app needs these extra scopes (already listed in `shopify.app.toml`): `read_companies`,
`read_draft_orders`, `write_draft_orders`, `read_publications` (product picking reads catalog membership),
`write_payment_terms` (needed to create the order's payment terms; without it Shopify refuses with "The user
must have access to set payment terms") and `write_orders` (needed for `orderMarkAsPaid`, once the invoicing
agent hands a completed order back to be marked paid). Update the app's scopes with the Shopify CLI
(`shopify app deploy`) and approve the new access in the store admin. Until then the tools fail with a
clear message and nothing is created.

## Verify on the first real order
A few behaviours can only be confirmed against the live store. Start with a small order for TWL's own
company ("The Whisky List") and check:
1. **The order is created unpaid**, on "Due on fulfilment" terms, regardless of what terms that company has
   configured in Shopify.
2. **Discounts and totals.** The draft's numbers match what Shopify shows on the order.
3. **An individual customer** (one who isn't a company). Confirm normal prices, and that payment terms can
   be put on their draft. If Shopify refuses terms for individuals, order creation will fail with a clear
   message.
4. **Addresses** carry over from the company location or the customer's default address.
5. **The customer email** arrives (or doesn't) as Shopify's notification settings say - once the order is
   marked paid, not when it's created.
6. **Approve & Send Invoice**, once the invoicing agent is live: the handoff reaches it, its invoice preview
   appears in the same thread, and once that's approved and sent, the Shopify order comes back marked paid.

## Not covered
New customers, shipping charges, delivery dates, discounts on an edit, cancelling an existing order, and
refunds. Editing a **paid** order is refused, not supported. Anything to do with the Xero invoice itself is
`twl-invoicing-agent`'s job, not this agent's.

# Choosing the right product

Sales type product names the way they say them ("Arran 10", "Ardnahoe Bholsa"). A plain search would match a
dozen products for "Arran 10": the core bottling, a different batch, independent bottler casks, samples, a gift
pack. The rules below pick the right one **in code**, not by the model. The model is only told the decision.

Everything that can change is in `orders_agent/data/product_priority.json`. Edit it through a pull request.

## Never offered
Samples (vendor "sample" or a title starting `[SAMPLE]`), gift packs, sets and cards (a title containing
"gift"), bottle splits, inactive products, anything that isn't in one of the four ranges below, and **anything
with no stock unless it is tagged TWL Brand**. Stock is per variant, and zero or negative (oversold) counts as
none.

## Offered, but flagged
- **Out of stock, tagged TWL Brand** (`include_out_of_stock_tags` in the config). It is offered and marked
  `out_of_stock`, the draft says "X is out of stock", and it suggests creating the order as unpaid (waiting to
  be invoiced). Only that tag qualifies: an out-of-stock product that is merely in Our Brands or a trade catalog
  is still left out. In a list of options, in-stock products come first.
- **Pre-order** (a `pre-order` tag or `[PRE-ORDER]` in the title). It is offered and marked `pre_order`, with
  the ETA when the product has one. The ETA is the product metafield `backendProduct.preOrderEta` (a date, shown
  as "16 Oct 2026"). If it isn't set the flag says "no ETA set".
- Everyone sees these flags on the draft. Only people with the inventory capability see stock counts.

## The four ranges (best first)
| | Collection | Catalog | Tag |
|---|---|---|---|
| 1 | Our Brands | Trade Core | TWL Brand (also `productGroup_twl-brand`) |
| 2 | TWL Independent Bottlers | Trade IBs | TWL IB (also `productGroup_twl-ib`) |
| 3 | Special Releases | Trade Special Releases | `productGroup_twl-exclusive` |
| 4 | | | `partnerStore_The Whisky List Shop`, in stock |

Any one of the three is enough for a range. A product in several ranges takes the best one. The collection and
catalog names are looked up in Shopify by name and cached for an hour. **If a name is missing or duplicated
(renamed, deleted), product picking refuses with a clear message** rather than ranking without it.

## Ahead of the ranges
1. **The quick order list**: Arran 10, Arran Sherry, GlenAllachie 12, GlenAllachie 10 Cask Strength, Remnant
   Golden Fleece, Ardnahoe Infinite Loch, Ardnahoe Bholsa (in that order).
2. **Every other product from the priority brands**: Arran, GlenAllachie, Ardnahoe, Ardnamurchan (by the
   `brand_` tag).

Ties are ordered by the Popular tag, then the shorter (plainer) title, then stock.

## What the agent does with a name
| Typed | Result |
|---|---|
| The full name of a quick order entry, in any wording ("Arran 10", "arran 10yo", "Arran 10 Year Old") | **Uses it**, and says so, including any out-of-stock or pre-order flag. If it can't be ordered at all (for example out of stock and not tagged TWL Brand) it does **not** pick a different one: it says so and only offers alternatives to choose from. |
| Part of a name ("arran", "ardnahoe") | **Asks**, with the quick order products first, then the rest of the brand, then the ranges. |
| One clear winner on priority brand and range, with at least two meaningful words typed | **Uses it**. |
| A single matching product | **Uses it**. |
| Several close candidates, or one word ("sherry") | **Asks** (the top five, and how many more there were). |
| Nothing orderable | **None**, with the reason (for example "Matched but out of stock: ..."). |

"Meaningful words" ignore filler such as *year, old, yo, single, malt, scotch, whisky*. Numbers must match a whole
number ("10" is not "2010"). "GlenAllachie" and "Glen Allachie" match each other, and "GA12" is read as "ga 12".

## Editing the quick order list
Each entry has a `name`, `aliases` (other ways to say it) and the exact product, as `handles`, `product_ids`
or (for something that changes) a `title_pattern`. GlenAllachie 10 Cask Strength is pinned to product
`8436830666826` (Batch 13, a pre-order with ETA 16 Oct 2026), so **when the next batch is released, that entry
must be pointed at the new product**, or the old batch stays the answer. A test checks that every entry's own
name is one of its aliases and that no two entries share an alias. If a product can't be found the agent reports
"on the quick order list but I can't find it in Shopify".

### Could the list live in Shopify instead?
Yes, and it would remove the batch problem, because staff could swap the product without a deploy. A **manual
collection** (for example "Sales Quick Order") holds which products are on the list, and its manual sort
order is the priority order. A collection has no place for the short names, so those would be a single-line
product field ("short name", separated by commas) on each product in it, edited in the Shopify admin. Smart
collections won't do: they can't keep a hand-set order.

## Not built yet
Ordering frequency ("what this customer usually orders", "best sellers"). The Popular tag is used as a tie-break
only. Real frequency needs order history, and the agent can only see the last 60 days of orders unless Shopify
approves the optional `read_all_orders` scope.

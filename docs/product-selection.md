# Choosing the right product

Sales type product names the way they say them ("Arran 10", "Ardnahoe Bholsa"). A plain search would match a
dozen products for "Arran 10": the core bottling, a different batch, independent bottler casks, samples, a gift
pack. The rules below pick the right one **in code**, not by the model. The model is only told the decision.

Everything that can change is in `orders_agent/data/product_priority.json`. Edit it through a pull request.

## Never offered
Samples (vendor "sample" or a title starting `[SAMPLE]`), gift packs, sets and cards (a title containing
"gift"), bottle splits, inactive products, **anything with no stock**, and anything that isn't in one of the
four ranges below. Stock is per variant, and zero or negative (oversold) counts as none.

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
| The full name of a quick order entry, in any wording ("Arran 10", "arran 10yo", "Arran 10 Year Old") | **Uses it**, and says so. If that product is out of stock it does **not** pick a different one: it says it is out of stock and only offers alternatives to choose from. |
| Part of a name ("arran", "ardnahoe") | **Asks**, with the quick order products first, then the rest of the brand, then the ranges. |
| One clear winner on priority brand and range, with at least two meaningful words typed | **Uses it**. |
| A single matching product | **Uses it**. |
| Several close candidates, or one word ("sherry") | **Asks** (the top five, and how many more there were). |
| Nothing orderable | **None**, with the reason (for example "Matched but out of stock: ..."). |

"Meaningful words" ignore filler such as *year, old, yo, single, malt, scotch, whisky*. Numbers must match a whole
number ("10" is not "2010"). "GlenAllachie" and "Glen Allachie" match each other, and "GA12" is read as "ga 12".

## Editing the quick order list
Each entry has a `name`, `aliases` (other ways to say it) and either `handles` (the exact product) or a
`title_pattern` (a regular expression, for products that change, such as GlenAllachie 10 Cask Strength whose
batch number rotates). A test checks that every entry's own name is one of its aliases and that no two entries
share an alias. Product handles are stable, but if a product is renamed in a way that changes its handle, update it
here (the agent will report "on the quick order list but I can't find it in Shopify").

## Not built yet
Ordering frequency ("what this customer usually orders", "best sellers"). The Popular tag is used as a tie-break
only. Real frequency needs order history, and the agent can only see the last 60 days of orders unless Shopify
approves the optional `read_all_orders` scope.

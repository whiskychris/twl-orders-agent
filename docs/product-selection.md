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
1. **The quick order list**: the Shopify manual collection **"Popular Trade Products"**, in its own order. Today:
   Arran 10, Arran Sherry, GlenAllachie 12, GlenAllachie 10 Cask Strength, Remnant Golden Fleece, Ardnahoe Infinite
   Loch, Ardnahoe Bholsa. Staff change it in the Shopify admin (add, remove, drag to reorder) and the agent reads
   it live, so there is nothing to deploy. It must be sorted **manually** (a collection sorted any other way is
   refused, because its order would mean nothing) and stay **unpublished**: it is an internal list.
2. **Every other product from the priority brands**: Arran, GlenAllachie, Ardnahoe, Ardnamurchan (by the
   `brand_` tag).

Ties are ordered by the Popular tag, then the shorter (plainer) title, then stock.

If the collection can't be found or read, the agent uses the built-in list in `product_priority.json` instead and
says so. A collection that exists but is empty means an empty list, not the fallback.
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
number ("10" is not "2010"). "GlenAllachie" and "Glen Allachie" match each other, and "GA12" is read as "glenallachie 12".

## How names work (`orders_agent/data/product_names.json`)
Short names are kept in the repo, not in Shopify, because product fields there are overwritten by the CMS. They
rarely change. What people type is normalised first:

| Typed | Read as |
|---|---|
| "year old", "yo", "yr", "single malt", "scotch whisky" | dropped (so "Arran 10", "Arran 10yo" and "Arran 10 Year Old" are the same) |
| `CS` | cask strength ("GlenAllachie 10 CS" = "GlenAllachie 10 Cask Strength") |
| `AR` Arran, `AD` Ardnamurchan, `AH` Ardnahoe, `GA` GlenAllachie, `BA` Bunnahabhain, `LD` Ledaig, `DS` Deanston, `TWJ` The Whisky Jury | the brand, wherever it is typed ("GA 12", "AH Bholsa", "AR 10") |
| `DD` | **any one of** Decadent Drams, Decadent Drinks, Whiskyland, Equinox & Solstice, Old Islay, Old Orkney ("DD Arran 10" finds an Arran 10 from any of them) |

Each product in the collection is known by a short name worked out from its title, with filler and bracketed text
removed: "Arran 10 Year Old Single Malt Scotch Whisky" is "Arran 10", and "GlenAllachie 12 Year Old ... [PRE-ORDER]"
is "GlenAllachie 12". Where that isn't how people say it, `aliases` in the same file adds a name that finds
a product **by words in its title**, not by id. For example "Arran Sherry" finds the member whose title has
arran, sherry and cask, and "GlenAllachie 10 Cask Strength" finds the member with those words. So **when the next
GlenAllachie 10 batch replaces this one in the collection, nothing needs editing**. If two matching products are in
the collection at once, the agent asks which.

Typing a name only picks a product directly if **everything typed** fits that product's title. "Arran 10" is Arran
10, but "DD Arran 10" or "Arran 10 sherry cask" are not, even though "arran 10" is inside them. Those go through
the normal ranking. If nothing has every word but part of it names quick-list products ("Arran 10 sherry"), the
agent offers those as the closest.

To add a code, a bottler to DD, or an alias: edit `product_names.json` in a pull request. Tests check that the
codes don't clash and that every alias names a real short form.
## Not built yet
Ordering frequency ("what this customer usually orders", "best sellers"). The Popular tag is used as a tie-break
only. Real frequency needs order history, and the agent can only see the last 60 days of orders unless Shopify
approves the optional `read_all_orders` scope.

## Product links, not order entry
The `product_link` tool (`entry.find_product_link`) reuses this exact same resolution - `find_for_order`, so a
short name works identically - but for a different purpose: giving someone a Shopify admin link to a product,
when they ask for one. It is **not** part of order entry and never affects a draft. Once `find_for_order` has
picked the product, `pick_twl_variant` (in `entry.py`, mirroring `twl-inventory-agent`'s own `_pick_variant`
exactly) picks **The Whisky List Shop** variant specifically - the product's only variant if it has one, else
the one titled exactly that, else refused with the actual variant titles named. This is deliberately **not**
necessarily the same variant `find_for_order`'s own `choice` would pick for checkout (a sample, a specific
cask, a different pack size): the link is always to TWL's own stock record, never to whatever an order would
actually use. The URL itself is `https://admin.shopify.com/store/{handle}/products/{id}/variants/{id}`, built
from each side's `legacyResourceId` - the same form `admin_order_url` already uses for an order.

## Checking a price (RRP, LUC and Rewards Member)
The `check_price` tool (`entry.check_price`) is for "checking prices for products we sell, primarily to trade
customers" - a distinct capability from order entry, sharing only the product resolution (`find_for_order`) and
the TWL variant rule (`pick_twl_variant`, same as `product_link` above). It needs no new Shopify scope -
`read_products` already covers `Catalog.priceList`/`PriceList.prices`.

- **RRP** is simply `ProductVariant.price` on the TWL variant - the number a retail customer pays. Now fetched
  as part of the same `ProductFields` query used for ranking (`variants { ... price }`), at no extra cost.
- **LUC** is the trade catalog price with GST excluded: `price / 1.1`, rounded to the cent. Found live: **a
  product can be priced on more than one trade catalog at once, and the prices don't always agree** - Arran 10
  is $86.90 on Trade Core but $87.20 on both Trade IBs and Trade Special Releases. So "the trade catalog price"
  needs a rule, not a pick: `product_search.trade_catalog_price` checks Trade Core, then Trade IBs, then Trade
  Special Releases, in that order, and uses the first one that actually prices the variant - the exact same
  "best range wins" precedence `product_pick.py`'s own ranking already uses elsewhere. If the variant isn't
  priced on any of the three, `luc` is `None` - never a guessed or blended number.
- **How a trade price is read.** Each `Catalog` (Trade Core, Trade IBs, Trade Special Releases - the same three
  `resolve_sources` already resolves for ranking) has an associated `PriceList`. `resolve_sources` now also
  resolves each one's `priceList { id }`, from the SAME `catalogs` query already made for ranking - one extra
  field, not a second round trip - and caches it the same hour. `trade_catalog_price` then queries
  `priceList.prices(query: "variant_id:<legacyResourceId>")` for the specific variant, one price list at a
  time in precedence order, stopping at the first with a result.
- **A missing price list is a refusal, not a skip - but only for pricing.** If one of the three catalogs has no
  `priceList` in Shopify, `trade_catalog_price` raises ("Nothing was guessed"), the same fail-loud philosophy
  `resolve_sources` already uses for a missing collection or catalog. This deliberately does **not** flow
  through `resolve_sources`'s own hard failure (which order entry's ranking depends on): a price-list problem
  in Shopify must never break product search or order entry, only pricing itself, when it's actually asked for.
- **Rewards Member price is a tag rule Chris gave directly, not read from Shopify.** Shopify's own automatic
  discounts (`discountNodes`/`automaticDiscountNodes`) need a scope this app doesn't have (`read_discounts`),
  and since eligibility is by customer tag ("Rewards Members"), the real discount is almost certainly
  implemented as a Shopify Function whose actual logic isn't visible through the Admin API either way - checked
  live before deciding this wasn't worth chasing further. `_rewards_member_discount_pct` (`entry.py`) instead
  applies: 10% off a product tagged TWL Brand (tier 1) or TWL Exclusive (tier 3), 20% off one tagged TWL IB /
  Independent Bottler (tier 2), reusing the exact tag lists `product_priority.json` already defines for
  ranking - one source of truth, not a second hardcoded copy. Checked in tier order (1, 2, 3) for the rare
  product tagged into more than one at once. No qualifying tag means no Rewards Member price - `null`, never
  a guessed number. Confirmed live for Arran 10 (tagged TWL Brand) before shipping: RRP $109.00, 10% off is
  exactly $98.10.

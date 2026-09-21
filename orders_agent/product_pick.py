"""Choosing the right product for an order, deterministically. The model never ranks or guesses.

A search for "Arran 10" matches a dozen products: the core bottling, a different batch, independent
bottler casks, samples, a gift pack. TWL's rules (data/product_priority.json) say which one is meant:

  Never offered   samples, gift packs and cards, bottle splits, inactive products, anything with no stock,
                  and anything that isn't in one of the four TWL ranges below.
  Ranges (best first)
    1  Our Brands collection, Trade Core catalog, TWL Brand tag
    2  TWL Independent Bottlers collection, Trade IBs catalog, TWL IB tag
    3  Special Releases collection, Trade Special Releases catalog, productGroup_twl-exclusive tag
    4  tagged partnerStore_The Whisky List Shop (and in stock)
  Ahead of the ranges   the quick order list (the products sales orders most), then every other product
                        from the priority brands (Arran, GlenAllachie, Ardnahoe, Ardnamurchan).

The answer is one of:
  use   one clear winner. The agent uses it and says which it chose.
  ask   several plausible products. The agent lists the numbered options and asks. It never picks.
  none  nothing orderable matches. The agent says so, and why (for example, out of stock).

When it uses the quick order list: if what was typed covers a quick entry's name (for example "Arran 10" or
"Arran 10 Year Old"), that entry wins, and if the entry itself is out of stock the answer is NOT to pick some
other product. Otherwise a winner is only used automatically when it is the single candidate, or is strictly
better than the runner-up on priority brand and range, and enough of a name was typed (two meaningful
words). A partial match on the quick order list only lifts a product in the list of options.
"""

import json
import re
import unicodedata
from pathlib import Path

from .sources import product_search as search
from .sources.shopify import ShopifyError

CONFIG_PATH = Path(__file__).parent / "data" / "product_priority.json"

# Words that say nothing about WHICH product. Dropped from what people type ("Arran 10 Year Old").
STOP = frozenset({"year", "years", "yo", "yr", "yrs", "y", "old", "single", "malt", "scotch", "whisky", "whiskey", "the", "and", "of", "a"})
UNINFORMATIVE_VARIANTS = frozenset({"default title", "the whisky list shop", ""})
MAX_OPTIONS = 5
POOL_SIZE = 50
NOT_FOUND = 99

_config = {"value": None}


# --- configuration ------------------------------------------------------------------------------------


def load_config():
    if _config["value"] is not None:
        return _config["value"]
    raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    for tier in ("1", "2", "3", "4"):
        if tier not in raw["tiers"]:
            raise RuntimeError(f"product_priority.json: tier {tier} is missing")
    for tier in ("1", "2", "3"):
        if not raw["tiers"][tier].get("collection") or not raw["tiers"][tier].get("catalog"):
            raise RuntimeError(f"product_priority.json: tier {tier} needs a collection and a catalog")

    raw["_patterns"] = {reason: re.compile(pattern, re.I) for reason, pattern in raw["exclude"]["title_patterns"].items()}
    raw["_vendors"] = {vendor.lower() for vendor in raw["exclude"]["vendors"]}
    raw["_brands"] = [brand.lower() for brand in raw["priority_brands"]]
    raw["_popular"] = {tag.lower() for tag in raw["popular_tags"]}
    for entry in raw["quick_order"]:
        if not entry.get("handles") and not entry.get("title_pattern"):
            raise RuntimeError(f"product_priority.json: quick order '{entry['name']}' needs handles or a title_pattern")
        entry["_aliases"] = [frozenset(tokens(alias)) for alias in entry["aliases"]]
        entry["_regex"] = re.compile(entry["title_pattern"], re.I) if entry.get("title_pattern") else None
    _config["value"] = raw
    return raw


# --- what people type ---------------------------------------------------------------------------------


def _runs(text):
    folded = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return re.findall(r"[a-z]+|\d+", folded)  # "ga12" -> ga, 12. "10yo" -> 10, yo


def tokens(text):
    """The meaningful words of a name, lower case, without filler like 'year old'. Order kept."""
    seen, result = set(), []
    for run in _runs(text):
        if run not in STOP and run not in seen:
            seen.add(run)
            result.append(run)
    return result


def title_matches(query_tokens, title):
    """True if every meaningful word typed is in the title. Numbers must match a whole number ("10" is
    not "2010"). Words of three letters or more may match part of a word or two joined words, so
    "glen allachie" finds GlenAllachie and "glenallachie" finds Glen Allachie."""
    runs = _runs(title)
    joined = "".join(runs)
    for token in query_tokens:
        if token.isdigit() or len(token) < 3:
            if token not in runs:
                return False
        elif not any(token in run for run in runs) and token not in joined:
            return False
    return True


# --- assessing a product -------------------------------------------------------------------------------


def _lower_tags(product):
    return {tag.lower() for tag in product["tags"]}


def assess(product, config):
    """Whether a product may be offered, in which range, and why. Never raises."""
    tags = _lower_tags(product)
    flags = product["flags"]
    tiers = config["tiers"]

    tier, sources = None, []
    if flags["our_brands"] or flags["trade_core"] or any(t.lower() in tags for t in tiers["1"]["tags"]):
        tier = 1
        sources = [
            name for name, hit in (
                (tiers["1"]["collection"], flags["our_brands"]),
                (tiers["1"]["catalog"], flags["trade_core"]),
                ("TWL Brand", any(t.lower() in tags for t in tiers["1"]["tags"])),
            ) if hit
        ]
    elif flags["ib_collection"] or flags["trade_ibs"] or any(t.lower() in tags for t in tiers["2"]["tags"]):
        tier = 2
        sources = [
            name for name, hit in (
                (tiers["2"]["collection"], flags["ib_collection"]),
                (tiers["2"]["catalog"], flags["trade_ibs"]),
                ("TWL IB", any(t.lower() in tags for t in tiers["2"]["tags"])),
            ) if hit
        ]
    elif flags["special_collection"] or flags["trade_special"] or any(t.lower() in tags for t in tiers["3"]["tags"]):
        tier = 3
        sources = [
            name for name, hit in (
                (tiers["3"]["collection"], flags["special_collection"]),
                (tiers["3"]["catalog"], flags["trade_special"]),
                ("TWL exclusive", any(t.lower() in tags for t in tiers["3"]["tags"])),
            ) if hit
        ]
    elif any(t.lower() in tags for t in tiers["4"]["tags"]):
        tier, sources = 4, ["Stocked by The Whisky List"]

    exclusion = None
    if product["status"] != "ACTIVE":
        exclusion = "inactive"
    elif product["vendor"].lower() in config["_vendors"]:
        exclusion = "sample"
    else:
        for reason, pattern in config["_patterns"].items():
            if pattern.search(product["title"]):
                exclusion = reason
                break
    in_stock = [variant for variant in product["variants"] if variant["stock"] > 0]
    if exclusion is None and tier is None:
        exclusion = "not_in_a_twl_range"
    if exclusion is None and not in_stock:
        exclusion = "no_stock"

    brand_rank = NOT_FOUND
    for tag in tags:
        if tag.startswith("brand_") and tag[6:] in config["_brands"]:
            brand_rank = min(brand_rank, config["_brands"].index(tag[6:]))
    return {
        "product": product,
        "exclusion": exclusion,
        "tier": tier,
        "why": sources,
        "brand_rank": brand_rank,
        "popular": bool(tags & config["_popular"]),
        "variants": in_stock,
    }


def _brand(product):
    for tag in product["tags"]:
        if tag.lower().startswith("brand_"):
            return tag[6:]
    return product["vendor"] or None


def _abv(product):
    for tag in product["tags"]:
        match = re.fullmatch(r"abv_(\d+(?:\.\d+)?)", tag, re.I)
        if match:
            return f"{match.group(1)}%"
    return None


def _display(product, variant):
    name = product["title"]
    if len(product["variants"]) > 1 and variant["title"].strip().lower() not in UNINFORMATIVE_VARIANTS:
        name += f" ({variant['title']})"
    return name


def _option(number, item, include_inventory, quick_name=None):
    product, variant = item["product"], item["variant"]
    why = ([f"Quick order list: {quick_name}"] if quick_name else []) + list(item["why"])
    if item["popular"]:
        why.append("Popular")
    option = {
        "number": number,
        "variant_id": variant["id"],
        "name": _display(product, variant),
        "brand": _brand(product),
        "abv": _abv(product),
        "why": why,
    }
    if "pre-order" in product["title"].lower():
        option["warning"] = "This is a pre-order product."
    if include_inventory:
        option["in_stock"] = variant["stock"]
    return option


# --- quick order list ---------------------------------------------------------------------------------


def _entries(config, query_tokens):
    """(exact, related): quick order entries whose name the user has typed in full, and entries whose name
    contains what they typed. 'arran 10 year old' is exact for Arran 10. 'arran' is related to both Arran
    entries. An entry that is a strict subset of another exact entry is dropped, so the longer name wins."""
    typed = frozenset(query_tokens)
    exact = [e for e in config["quick_order"] if any(alias <= typed for alias in e["_aliases"])]
    keep = []
    for entry in exact:
        mine = max((a for a in entry["_aliases"] if a <= typed), key=len)
        if not any(
            other is not entry and any(mine < a <= typed for a in other["_aliases"]) for other in exact
        ):
            keep.append(entry)
    related = [
        e for e in config["quick_order"]
        if e not in keep and typed and any(typed <= alias for alias in e["_aliases"])
    ]
    return keep, related


def _entry_products(config, entry):
    if entry.get("handles"):
        return search.search(config, search.handles_query(entry["handles"]), 20)
    found = search.search(config, search.title_query(tokens(entry["name"]), in_stock=False), POOL_SIZE)
    return [p for p in found if entry["_regex"].search(p["title"])]


# --- deciding -------------------------------------------------------------------------------------------


def _items(products, config, quick_index):
    """Every orderable (product, variant) with its ranking data, plus a tally of what was left out."""
    items, left_out = [], {}
    seen = set()
    for product in products:
        if product["id"] in seen:
            continue
        seen.add(product["id"])
        assessment = assess(product, config)
        if assessment["exclusion"]:
            left_out.setdefault(assessment["exclusion"], []).append(product["title"])
            continue
        for variant in assessment["variants"]:
            items.append({**assessment, "variant": variant, "quick_rank": quick_index.get(product["id"], NOT_FOUND)})
    return items, left_out


def _sort_key(item):
    return (
        item["quick_rank"],
        item["brand_rank"],
        item["tier"],
        not item["popular"],
        len(_runs(item["product"]["title"])),  # the shorter, plainer name is usually the core bottling
        -item["variant"]["stock"],
    )


def _rank_key(item):
    """What decides whether one candidate is CLEARLY better than another: priority brand, then range.
    Title length, popularity and stock only order equals. The quick order list is deliberately NOT here:
    it lifts a product in the list of options, but only typing an entry's full name (step A in decide)
    lets it choose on its own. Otherwise 'sherry cask' would pick Arran Sherry over Arran 14."""
    return (item["brand_rank"], item["tier"])


def decide(query, config, pool, quick_products, include_inventory=False):
    """Pure. `pool` is the search results for what was typed (in stock). `quick_products` maps a quick
    order entry name to the products it points at. Returns the decision dict."""
    query_tokens = tokens(query)
    if not query_tokens:
        raise ShopifyError("Give me a product name to look for.")
    exact, related = _entries(config, query_tokens)
    order = {entry["name"]: index for index, entry in enumerate(config["quick_order"])}
    quick_index, quick_name = {}, {}
    for entry in exact + related:
        for product in quick_products.get(entry["name"], []):
            quick_index[product["id"]] = min(quick_index.get(product["id"], NOT_FOUND), order[entry["name"]])
            quick_name.setdefault(product["id"], entry["name"])

    unavailable = []
    matched_pool = [p for p in pool if title_matches(query_tokens, p["title"])]

    # A. The user typed a quick order product's name in full.
    if exact:
        chosen = []
        for entry in exact:
            found = quick_products.get(entry["name"], [])
            items, left_out = _items(found, config, {p["id"]: order[entry["name"]] for p in found})
            chosen.extend(items)
            if not items:
                if not found:
                    unavailable.append(f"{entry['name']} is on the quick order list but I can't find it in Shopify.")
                elif set(left_out) == {"no_stock"}:
                    unavailable.append(f"{entry['name']} is out of stock.")
                else:
                    unavailable.append(f"{entry['name']} can't be ordered right now ({', '.join(sorted(left_out))}).")
        if len(exact) == 1 and len(chosen) == 1:
            return _use(chosen[0], include_inventory, quick_name.get(chosen[0]["product"]["id"]), unavailable)
        if chosen:
            chosen.sort(key=_sort_key)
            return _ask(chosen, include_inventory, quick_name, unavailable)
        # The named product can't be ordered. Do not quietly substitute another: offer alternatives to choose from.
        alternatives, _ = _items(matched_pool, config, quick_index)
        alternatives = [a for a in alternatives if a["product"]["id"] not in {p["id"] for e in exact for p in quick_products.get(e["name"], [])}]
        if alternatives:
            alternatives.sort(key=_sort_key)
            return _ask(alternatives, include_inventory, quick_name, unavailable, lead="The one you named isn't available. These are similar:")
        return _none(unavailable)

    # B. Rank everything that matches, quick order products first.
    products = list(matched_pool)
    seen = {p["id"] for p in products}
    for entry in related:
        for product in quick_products.get(entry["name"], []):
            if product["id"] not in seen and title_matches(query_tokens, product["title"]):
                products.append(product)
                seen.add(product["id"])
    items, left_out = _items(products, config, quick_index)
    if not items:
        return _none(unavailable, left_out)
    items.sort(key=_sort_key)

    if len(items) == 1:
        return _use(items[0], include_inventory, quick_name.get(items[0]["product"]["id"]), unavailable)
    clearly_first = _rank_key(items[0]) < _rank_key(items[1])
    if clearly_first and len(query_tokens) >= 2:
        return _use(items[0], include_inventory, quick_name.get(items[0]["product"]["id"]), unavailable)
    return _ask(items, include_inventory, quick_name, unavailable)


def _use(item, include_inventory, quick_name, unavailable):
    return {
        "decision": "use",
        "choice": _option(1, item, include_inventory, quick_name),
        "unavailable": unavailable,
        "guidance": "Use this product. Tell the user which one you chose, in one short line, then continue.",
    }


def _ask(items, include_inventory, quick_name, unavailable, lead=None):
    shown = items[:MAX_OPTIONS]
    return {
        "decision": "ask",
        "options": [_option(i, item, include_inventory, quick_name.get(item["product"]["id"])) for i, item in enumerate(shown, 1)],
        "more_matches": max(0, len(items) - len(shown)),
        "unavailable": unavailable,
        "guidance": (lead + " " if lead else "") + "Ask the user which one they mean. List these options with their numbers, "
        "showing the name, brand and ABV. Do NOT choose for them. If none is right, ask them to say more.",
    }


def _none(unavailable, left_out=None):
    reasons = {
        "no_stock": "out of stock",
        "sample": "a sample",
        "gift": "a gift pack or card",
        "bottle_split": "a bottle split",
        "inactive": "not active",
        "not_in_a_twl_range": "not in one of TWL's ranges",
    }
    for reason, titles in (left_out or {}).items():
        if reason in ("no_stock", "not_in_a_twl_range"):
            names = "; ".join(titles[:3]) + (f" and {len(titles) - 3} more" if len(titles) > 3 else "")
            unavailable.append(f"Matched but {reasons[reason]}: {names}")
    return {
        "decision": "none",
        "unavailable": unavailable,
        "guidance": "Nothing orderable matched. Say so plainly, mention anything unavailable, and ask what else they mean.",
    }


# --- the entry point used by the tool --------------------------------------------------------------------


def find_for_order(query, include_inventory=False):
    """Look up and choose. Reads Shopify. Raises ShopifyError for a bad query or a broken configuration."""
    config = load_config()
    query_tokens = tokens(query)
    if not query_tokens:
        raise ShopifyError("Give me a product name to look for.")

    exact, related = _entries(config, query_tokens)
    quick_products = {entry["name"]: _entry_products(config, entry) for entry in exact + related}
    pool = search.search(config, search.title_query(query_tokens, in_stock=True), POOL_SIZE)

    decision = decide(query, config, pool, quick_products, include_inventory)
    if len(pool) >= POOL_SIZE:
        decision["note"] = "Many products matched, so some may not be shown. A fuller name narrows it."
    if decision["decision"] == "none":
        # Say why: the matches that exist but are out of stock.
        everything = search.search(config, search.title_query(query_tokens, in_stock=False), 20)
        _, left_out = _items([p for p in everything if title_matches(query_tokens, p["title"])], config, {})
        extra = _none([], {k: v for k, v in left_out.items() if k == "no_stock"})["unavailable"]
        decision["unavailable"] = decision["unavailable"] + extra
    return decision

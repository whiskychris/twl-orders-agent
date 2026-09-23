"""Choosing the right product for an order, deterministically. The model never ranks or guesses.

A search for "Arran 10" matches a dozen products: the core bottling, a different batch, independent
bottler casks, samples, a gift pack. TWL's rules (data/product_priority.json) say which one is meant:

  Never offered   samples, gift packs and cards, bottle splits, inactive products, anything that isn't in
                  one of the four TWL ranges below, and anything with no stock EXCEPT products tagged
                  TWL Brand: those are offered but flagged "out of stock" (they are often taken as unpaid
                  or back orders). Pre-order products are offered and flagged, with their ETA.
  Ranges (best first)
    1  Our Brands collection, Trade Core catalog, TWL Brand tag
    2  TWL Independent Bottlers collection, Trade IBs catalog, TWL IB tag
    3  Special Releases collection, Trade Special Releases catalog, productGroup_twl-exclusive tag
    4  tagged partnerStore_The Whisky List Shop (and in stock)
  Ahead of the ranges   the quick order list (the products sales orders most), then every other product
                        from the priority brands (Arran, GlenAllachie, Ardnahoe, Ardnamurchan).

The quick order list is the manual Shopify collection "Popular Trade Products", in its own order, so staff
manage it in the Shopify admin. Each member is known by a short name worked out from its title ("Arran 10
Year Old Single Malt Scotch Whisky" is "Arran 10"), plus the exceptions in data/product_names.json, which
match by words in the title (so a new batch needs no edit). If the collection can't be read, the built-in
list in product_priority.json is used instead.

What people type is normalised first (data/product_names.json): filler like "year old" and "yo" is dropped,
codes like GA, AH and CS are expanded (GA = GlenAllachie, CS = cask strength), and DD stands for any one of
several bottlers.

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
import logging
import re
import unicodedata
from datetime import date
from pathlib import Path

from .sources import product_search as search
from .sources.shopify import ShopifyError

log = logging.getLogger("orders_agent.product_pick")

CONFIG_PATH = Path(__file__).parent / "data" / "product_priority.json"
NAMES_PATH = Path(__file__).parent / "data" / "product_names.json"

# Words that say nothing about WHICH product. Dropped from what people type ("Arran 10 Year Old").
STOP = frozenset({"year", "years", "yo", "yr", "yrs", "y", "old", "single", "malt", "scotch", "whisky", "whiskey", "the", "and", "of", "a"})
UNINFORMATIVE_VARIANTS = frozenset({"default title", "the whisky list shop", ""})
MAX_OPTIONS = 5
POOL_SIZE = 50
NOT_FOUND = 99

_config = {"value": None}
_names = {"value": None}


# --- configuration ------------------------------------------------------------------------------------


def load_names():
    """data/product_names.json: brand codes, group codes and title-word exceptions."""
    if _names["value"] is not None:
        return _names["value"]
    raw = json.loads(NAMES_PATH.read_text(encoding="utf-8"))

    def real(section):
        return {key.lower(): value for key, value in raw[section].items() if not key.startswith("_")}

    abbreviations = {short: _runs(full) for short, full in real("abbreviations").items()}
    groups = {code: [_runs(alternative) for alternative in alternatives] for code, alternatives in real("groups").items()}
    for code, expansion in list(abbreviations.items()) + [(code, None) for code in groups]:
        if expansion is not None and not expansion:
            raise RuntimeError(f"product_names.json: abbreviation '{code}' expands to nothing")
    if set(abbreviations) & set(groups):
        raise RuntimeError("product_names.json: a code can't be both an abbreviation and a group")
    for code, alternatives in groups.items():
        if not alternatives or not all(alternatives):
            raise RuntimeError(f"product_names.json: group '{code}' has an empty alternative")

    aliases = []
    for alias in raw["aliases"]:
        if not alias.get("name") or not alias.get("say") or not alias.get("title_has"):
            raise RuntimeError(f"product_names.json: alias {alias.get('name')!r} needs a name, say and title_has")
        aliases.append({"name": alias["name"], "say": list(alias["say"]), "title_has": [w.lower() for w in alias["title_has"]]})
    _names["value"] = {"abbreviations": abbreviations, "groups": groups, "aliases": aliases}
    return _names["value"]


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
    raw["_oos_tags"] = {tag.lower() for tag in raw.get("include_out_of_stock_tags", [])}
    for entry in raw["quick_order"]:
        if not (entry.get("handles") or entry.get("product_ids") or entry.get("title_pattern")):
            raise RuntimeError(
                f"product_priority.json: quick order '{entry['name']}' needs handles, product_ids or a title_pattern"
            )
        entry["_aliases"] = [frozenset(tokens(alias)) for alias in entry["aliases"]]
        entry["_regex"] = re.compile(entry["title_pattern"], re.I) if entry.get("title_pattern") else None
    _config["value"] = raw
    return raw


# --- what people type ---------------------------------------------------------------------------------


def _runs(text):
    folded = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return re.findall(r"[a-z]+|\d+", folded)  # "ga12" -> ga, 12. "10yo" -> 10, yo


def tokens(text):
    """The meaningful words of a name, lower case, without filler like 'year old', and with brand codes
    expanded ('ga' -> glenallachie, 'cs' -> cask strength). A group code such as 'dd' is kept as it is, and
    is expanded where it is matched. Order kept."""
    abbreviations = load_names()["abbreviations"]
    seen, result = set(), []
    for run in _runs(text):
        if run in STOP:
            continue
        for word in abbreviations.get(run, [run]):
            if word not in seen:
                seen.add(word)
                result.append(word)
    return result


def _word_matches(word, runs, joined):
    if word.isdigit() or len(word) < 3:
        return word in runs
    return any(word in run for run in runs) or word in joined


def title_matches(query_tokens, title):
    """True if every meaningful word typed is in the title. Numbers must match a whole number ("10" is
    not "2010"). Words of three letters or more may match part of a word or two joined words, so
    "glen allachie" finds GlenAllachie and "glenallachie" finds Glen Allachie. A group code such as 'dd'
    matches if the title has any one of its alternatives (Decadent Drams, or Whiskyland, or ...)."""
    groups = load_names()["groups"]
    runs = _runs(title)
    joined = "".join(runs)
    for token in query_tokens:
        alternatives = groups.get(token) or [[token]]
        if not any(all(_word_matches(word, runs, joined) for word in alternative) for alternative in alternatives):
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
    may_be_out_of_stock = bool(tags & config["_oos_tags"])   # TWL Brand: offered, but flagged
    if exclusion is None and tier is None:
        exclusion = "not_in_a_twl_range"
    if exclusion is None and not in_stock and not may_be_out_of_stock:
        exclusion = "no_stock"
    offered = in_stock or (list(product["variants"]) if may_be_out_of_stock else [])

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
        "variants": offered,
        "pre_order": "pre-order" in tags or "[pre-order]" in product["title"].lower(),
        "eta": product.get("pre_order_eta"),
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


def format_eta(value):
    """'2026-10-16' -> '16 Oct 2026'. Anything else is shown as it is."""
    try:
        day = date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return str(value).strip()
    return f"{day.day} {day.strftime('%b %Y')}"


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
    warnings = []
    if item["oos"]:
        option["out_of_stock"] = True
        warnings.append("Out of stock. It can still be ordered (usually as unpaid, waiting to be invoiced).")
    if item["pre_order"]:
        option["pre_order"] = True
        if item["eta"]:
            option["eta"] = format_eta(item["eta"])
        warnings.append("Pre-order product" + (f", ETA {option['eta']}" if item["eta"] else ", no ETA set") + ".")
    if warnings:
        option["warnings"] = warnings
    if include_inventory:
        option["in_stock"] = variant["stock"]
    return option


# --- quick order list ---------------------------------------------------------------------------------


def _entries(entries, query_tokens):
    """(exact, related): quick order entries whose name the user has typed in full, and entries whose name
    contains what they typed. 'arran 10 year old' is exact for Arran 10. 'arran' is related to both Arran
    entries. An entry that is a strict subset of another exact entry is dropped, so the longer name wins."""
    typed = frozenset(query_tokens)
    exact = [e for e in entries if any(alias <= typed for alias in e["_aliases"])]
    keep = []
    for entry in exact:
        mine = max((a for a in entry["_aliases"] if a <= typed), key=len)
        if not any(
            other is not entry and any(mine < a <= typed for a in other["_aliases"]) for other in exact
        ):
            keep.append(entry)
    related = [
        e for e in entries
        if e not in keep and typed and any(typed <= alias for alias in e["_aliases"])
    ]
    return keep, related


_BRACKETED = re.compile(r"\[[^\]]*\]|\([^)]*\)")
_DISPLAY_FILLER = re.compile(r"\b(?:single malt|scotch whisky|whisky|years? old)\b", re.I)


def _short_name(title):
    """A title with the filler taken out, for showing people: 'Arran 10 Year Old Single Malt Scotch Whisky'
    is 'Arran 10'."""
    return " ".join(_DISPLAY_FILLER.sub(" ", _BRACKETED.sub(" ", title)).split())


def collection_entries(members, names=None):
    """Quick order entries from the members of the Popular Trade Products collection, in its order. Each is
    known by the short name worked out from its title, plus any exception in product_names.json whose
    `title_has` words are in the title (that is how 'GlenAllachie 10 Cask Strength' finds Batch 13 today and
    Batch 14 later, with nothing to edit)."""
    names = names or load_names()
    entries, used = [], {}
    for product in members:
        title = product["title"]
        aliases = {frozenset(tokens(_BRACKETED.sub(" ", title)))}
        display = _short_name(title)
        for exception in names["aliases"]:
            if title_matches(exception["title_has"], title):
                aliases.update(frozenset(tokens(phrase)) for phrase in exception["say"])
                display = exception["name"]
        used[display] = used.get(display, 0) + 1
        entries.append({
            "name": display if used[display] == 1 else f"{display} ({used[display]})",
            "_aliases": [alias for alias in aliases if alias],
            "products": [product],
        })
    return entries


def quick_entries(config):
    """(entries, note): the quick order list. It is the manual collection named in the config, read live, so
    staff change it in Shopify. If the collection can't be found or read, the built-in list in the config is
    used, and the note says so. A collection that exists but is empty means an empty list, not the fallback."""
    title = config.get("quick_order_collection")
    if title:
        try:
            return collection_entries(search.collection_products(config, title)), None
        except ShopifyError as exc:
            log.warning("quick order collection unavailable, using the built-in list: %s", exc)
            note = f"The '{title}' collection couldn't be read, so the built-in quick order list was used."
            return config["quick_order"], note
    return config["quick_order"], None


def _entry_products(config, entry):
    pinned = [q for q in (
        search.handles_query(entry["handles"]) if entry.get("handles") else "",
        search.ids_query(entry["product_ids"]) if entry.get("product_ids") else "",
    ) if q]
    if pinned:
        return search.search(config, " OR ".join(pinned), 20)
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
            items.append({
                **assessment,
                "variant": variant,
                "oos": variant["stock"] <= 0,
                "quick_rank": quick_index.get(product["id"], NOT_FOUND),
            })
    return items, left_out


def _sort_key(item):
    # In stock before out of stock, but only after the quick order list: an out-of-stock quick order
    # product is still the one people asked for.
    return (
        item["quick_rank"],
        item["oos"],
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
    lets it choose on its own. Otherwise 'sherry cask' would pick Arran Sherry over Arran 14. Being in
    stock is not here either: it orders the options, but "Arran 14" must not auto-pick the in-stock Palo
    Cortado cask over the core Arran 14, which is out of stock and is probably what was meant."""
    return (item["brand_rank"], item["tier"])


def decide(query, config, pool, quick_products, include_inventory=False, entries=None):
    """Pure. `pool` is the search results for what was typed (in stock). `quick_products` maps a quick
    order entry name to the products it points at. `entries` is the quick order list, in priority order
    (the built-in list if not given). Returns the decision dict."""
    query_tokens = tokens(query)
    if not query_tokens:
        raise ShopifyError("Give me a product name to look for.")
    entries = config["quick_order"] if entries is None else entries
    exact, related = _entries(entries, query_tokens)
    # Typing an entry's name is only an exact match if EVERYTHING typed fits that product. "arran 10" is
    # Arran 10, but "dd arran 10" or "arran 10 sherry cask" are not, even though "arran 10" is in them.
    # An entry whose product can't be found at all stays exact, so the person is told it is missing.
    fits = [
        e for e in exact
        if not quick_products.get(e["name"]) or any(title_matches(query_tokens, p["title"]) for p in quick_products[e["name"]])
    ]
    demoted = [e for e in exact if e not in fits]
    related = related + demoted
    exact = fits
    order = {entry["name"]: index for index, entry in enumerate(entries)}
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
    if not items and demoted:
        # Nothing has every word typed, but part of it names quick order products ("arran 10 sherry" is
        # torn between Arran 10 and Arran Sherry). Offer those as the closest, for the person to choose.
        near, _ = _items([p for e in demoted for p in quick_products.get(e["name"], [])], config, quick_index)
        if near:
            near.sort(key=_sort_key)
            return _ask(near, include_inventory, quick_name, unavailable,
                        lead="Nothing matches all of that. The closest on the quick order list:")
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
        # The raw product (every variant, not just the checkout one chosen above) - used by
        # entry.find_product_link to pick the TWL variant specifically, which is never necessarily
        # the same as the checkout variant "choice" names. Order entry's own flow never reads this.
        "product": item["product"],
        "unavailable": unavailable,
        "guidance": "Use this product. Tell the user which one you chose, in one short line. If it has warnings "
        "(out of stock, or a pre-order with its ETA), say so plainly in that same line, then continue.",
    }


def _ask(items, include_inventory, quick_name, unavailable, lead=None):
    shown = items[:MAX_OPTIONS]
    return {
        "decision": "ask",
        "options": [_option(i, item, include_inventory, quick_name.get(item["product"]["id"])) for i, item in enumerate(shown, 1)],
        "more_matches": max(0, len(items) - len(shown)),
        "unavailable": unavailable,
        "guidance": (lead + " " if lead else "") + "Ask the user which one they mean. List these options with their numbers, "
        "showing the name, brand and ABV, and any warning next to it (out of stock, or pre-order with its ETA). "
        "Do NOT choose for them. If none is right, ask them to say more.",
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
    groups = load_names()["groups"]

    entries, note = quick_entries(config)
    exact, related = _entries(entries, query_tokens)
    # Collection entries already carry their product. The built-in fallback entries are looked up.
    quick_products = {
        entry["name"]: entry["products"] if "products" in entry else _entry_products(config, entry)
        for entry in exact + related
    }
    pool = search.search(config, search.title_query(query_tokens, in_stock=True, groups=groups), POOL_SIZE)
    full = len(pool) >= POOL_SIZE
    if config["include_out_of_stock_tags"]:
        # Products that may be ordered out of stock (TWL Brand) are searched separately, so they are
        # found even when plenty of in-stock products match the same words.
        more = search.search(
            config, search.out_of_stock_query(query_tokens, config["include_out_of_stock_tags"], groups), POOL_SIZE
        )
        full = full or len(more) >= POOL_SIZE
        have = {p["id"] for p in pool}
        pool = pool + [p for p in more if p["id"] not in have]

    decision = decide(query, config, pool, quick_products, include_inventory, entries)
    notes = [text for text in (
        note,
        "Many products matched, so some may not be shown. A fuller name narrows it." if full else None,
    ) if text]
    if notes:
        decision["note"] = " ".join(notes)
    if decision["decision"] == "none":
        # Say why: the matches that exist but are out of stock.
        everything = search.search(config, search.title_query(query_tokens, in_stock=False, groups=groups), 20)
        _, left_out = _items([p for p in everything if title_matches(query_tokens, p["title"])], config, {})
        extra = _none([], {k: v for k, v in left_out.items() if k == "no_stock"})["unavailable"]
        decision["unavailable"] = decision["unavailable"] + extra
    return decision

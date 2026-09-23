"""Product lookups for order entry: candidates with everything needed to rank them.

Read-only, fixed GraphQL validated against Shopify's Admin schema. One query returns each product's
status, vendor, tags, stock, variants, and whether it is in the priority collections and published to the
priority catalogs (Our Brands, Trade Core, and so on, named in data/product_priority.json).

Scopes needed: read_products, and read_publications (to see which catalogs a product is published to).
The collection and catalog NAMES in the config are resolved to ids here, and cached for an hour. If a name
can't be found (renamed or deleted in Shopify), this raises instead of ranking without it: silently
dropping a tier would make the ranking wrong in a way nobody would notice.
"""

import re
import time

from .shopify import ShopifyError, clamp, graphql

SOURCES_TTL = 3600
_sources = {"value": None, "loaded_at": 0.0}

SOURCES = """
query Sources($collections: String!) {
  collections(first: 20, query: $collections) { nodes { id title } }
  catalogs(first: 50) { nodes { title publication { id } } }
}
"""

# Everything the ranking needs to know about a product. Shared by the search and the collection lookup.
PRODUCT_FIELDS = """
fragment ProductFields on Product {
  id
  title
  handle
  status
  vendor
  productType
  tags
  totalInventory
  legacyResourceId
  preOrderEta: metafield(namespace: "backendProduct", key: "preOrderEta") { value }
  inOurBrands: inCollection(id: $ourBrands)
  inIbCollection: inCollection(id: $ibCollection)
  inSpecialCollection: inCollection(id: $specialCollection)
  inTradeCore: publishedOnPublication(publicationId: $tradeCore)
  inTradeIbs: publishedOnPublication(publicationId: $tradeIbs)
  inTradeSpecial: publishedOnPublication(publicationId: $tradeSpecial)
  variants(first: 20) { nodes { id title sku inventoryQuantity legacyResourceId } }
}
"""

CANDIDATES = """
query ProductCandidates(
  $query: String!, $first: Int!,
  $ourBrands: ID!, $ibCollection: ID!, $specialCollection: ID!,
  $tradeCore: ID!, $tradeIbs: ID!, $tradeSpecial: ID!
) {
  products(first: $first, query: $query, sortKey: RELEVANCE) {
    nodes { ...ProductFields }
  }
}
""" + PRODUCT_FIELDS

COLLECTION_PRODUCTS = """
query CollectionProducts(
  $id: ID!, $first: Int!,
  $ourBrands: ID!, $ibCollection: ID!, $specialCollection: ID!,
  $tradeCore: ID!, $tradeIbs: ID!, $tradeSpecial: ID!
) {
  collection(id: $id) {
    title
    sortOrder
    products(first: $first, sortKey: MANUAL) {
      nodes { ...ProductFields }
    }
  }
}
""" + PRODUCT_FIELDS

FIND_COLLECTION = """
query FindCollection($query: String!) {
  collections(first: 5, query: $query) { nodes { id title } }
}
"""

_quick_collection = {}   # title -> (collection id, loaded_at)
COLLECTION_MAX = 50


def _nodes(connection):
    return (connection or {}).get("nodes") or []


def resolve_sources(config):
    """Collection and catalog ids for tiers 1 to 3, by the names in the config. Cached."""
    now = time.time()
    if _sources["value"] is not None and now - _sources["loaded_at"] < SOURCES_TTL:
        return _sources["value"]

    tiers = config["tiers"]
    wanted_collections = [tiers[n]["collection"] for n in ("1", "2", "3")]
    wanted_catalogs = [tiers[n]["catalog"] for n in ("1", "2", "3")]
    search = " OR ".join(f"title:'{title}'" for title in wanted_collections)
    try:
        data = graphql(SOURCES, {"collections": search})
    except ShopifyError as exc:
        if "Access denied" in str(exc):
            raise ShopifyError(
                "Product picking can't read TWL's catalogs, because the Shopify app hasn't been given the "
                "read_publications permission. An admin needs to approve it in Shopify (Apps > TWL Orders Agent). "
                f"Nothing was guessed. ({str(exc)[:200]})"
            ) from exc
        raise

    by_title = {}
    for node in _nodes(data.get("collections")):
        by_title.setdefault(node["title"], []).append(node["id"])
    catalogs = {}
    for node in _nodes(data.get("catalogs")):
        publication = (node.get("publication") or {}).get("id")
        if publication:
            catalogs.setdefault(node["title"], []).append(publication)

    problems = []
    ids = {}
    for label, wanted, found in (("collection", wanted_collections, by_title), ("catalog", wanted_catalogs, catalogs)):
        for name in wanted:
            matches = found.get(name, [])
            if len(matches) != 1:
                problems.append(f"the {label} '{name}' ({'not found' if not matches else 'found more than once'})")
            else:
                ids[(label, name)] = matches[0]
    if problems:
        raise ShopifyError(
            "Product picking isn't set up right: " + "; ".join(problems) + ". Someone needs to check the "
            "names in orders_agent/data/product_priority.json against Shopify. Nothing was guessed."
        )

    value = {
        "ourBrands": ids[("collection", tiers["1"]["collection"])],
        "ibCollection": ids[("collection", tiers["2"]["collection"])],
        "specialCollection": ids[("collection", tiers["3"]["collection"])],
        "tradeCore": ids[("catalog", tiers["1"]["catalog"])],
        "tradeIbs": ids[("catalog", tiers["2"]["catalog"])],
        "tradeSpecial": ids[("catalog", tiers["3"]["catalog"])],
    }
    _sources["value"], _sources["loaded_at"] = value, now
    return value


def clear_cache():
    _sources["value"], _sources["loaded_at"] = None, 0.0


def _product(node):
    return {
        "id": node["id"],
        "title": node["title"],
        "handle": node["handle"],
        "status": node.get("status"),
        "vendor": node.get("vendor") or "",
        "tags": list(node.get("tags") or []),
        # Pre-order products carry an ETA in the metafield backendProduct.preOrderEta (a date), if set.
        "pre_order_eta": ((node.get("preOrderEta") or {}).get("value") or "").strip() or None,
        "flags": {
            "our_brands": bool(node.get("inOurBrands")),
            "ib_collection": bool(node.get("inIbCollection")),
            "special_collection": bool(node.get("inSpecialCollection")),
            "trade_core": bool(node.get("inTradeCore")),
            "trade_ibs": bool(node.get("inTradeIbs")),
            "trade_special": bool(node.get("inTradeSpecial")),
        },
        "variants": [
            {
                "id": variant["id"],
                "title": variant.get("title") or "",
                "sku": variant.get("sku") or "",
                "stock": variant.get("inventoryQuantity") or 0,
                "legacy_id": variant.get("legacyResourceId"),
            }
            for variant in _nodes(node.get("variants"))
        ],
        "legacy_id": node.get("legacyResourceId"),
    }


def search(config, shopify_query, limit=50):
    """Products matching a Shopify search string, with ranking data. Most relevant first."""
    variables = {"query": shopify_query, "first": clamp(limit, 1, 100, 50), **resolve_sources(config)}
    data = graphql(CANDIDATES, variables)
    return [_product(node) for node in _nodes(data.get("products"))]


def collection_products(config, title):
    """The products in a manual collection, in the collection's own order (position 1 first). Used for the
    quick order list, so staff manage it in the Shopify admin. Raises ShopifyError if the collection can't
    be found, is ambiguous, isn't sorted manually (its order would then mean nothing), or can't be read."""
    now = time.time()
    cached = _quick_collection.get(title)
    if cached and now - cached[1] < SOURCES_TTL:
        collection_id = cached[0]
    else:
        wanted = re.sub(r"['\"]", "", title)
        data = graphql(FIND_COLLECTION, {"query": f"title:'{wanted}'"})
        matches = [node["id"] for node in _nodes(data.get("collections")) if node["title"] == title]
        if len(matches) != 1:
            raise ShopifyError(f"The collection '{title}' was {'not found' if not matches else 'found more than once'}.")
        collection_id = matches[0]
        _quick_collection[title] = (collection_id, now)

    variables = {"id": collection_id, "first": COLLECTION_MAX, **resolve_sources(config)}
    data = graphql(COLLECTION_PRODUCTS, variables)
    collection = data.get("collection")
    if not collection:
        _quick_collection.pop(title, None)
        raise ShopifyError(f"The collection '{title}' could not be read.")
    if collection.get("sortOrder") != "MANUAL":
        raise ShopifyError(f"The collection '{title}' must be sorted manually, because its order is the priority order.")
    return [_product(node) for node in _nodes(collection.get("products"))]


_SAFE_TOKEN = re.compile(r"[^a-z0-9]")


def _title_filter(word):
    clean = _SAFE_TOKEN.sub("", str(word).lower())
    return f"title:*{clean}*" if clean else None


def title_query(tokens, in_stock=True, groups=None):
    """A Shopify search for products whose title contains every token. Tokens are reduced to letters and
    digits, so nothing a user types can add a search filter of its own. `groups` maps a token that stands
    for several alternatives (dd = Decadent Drams, Whiskyland, ...) to those alternatives, each a list of
    words: the search then accepts any one of them."""
    parts = []
    for token in tokens:
        alternatives = (groups or {}).get(token)
        if alternatives:
            options = []
            for alternative in alternatives:
                filters = [f for f in (_title_filter(word) for word in alternative) if f]
                if filters:
                    options.append("(" + " AND ".join(filters) + ")")
            if options:
                parts.append("(" + " OR ".join(options) + ")")
        else:
            single = _title_filter(token)
            if single:
                parts.append(single)
    if not parts:
        raise ShopifyError("Give me a product name to look for.")
    parts.append("status:active")
    if in_stock:
        parts.append("inventory_total:>0")
    return " AND ".join(parts)


def out_of_stock_query(tokens, tags, groups=None):
    """A Shopify search for products with no stock (zero or oversold) that carry one of these tags. Used
    for the tags whose products may be ordered out of stock (TWL Brand), so they are found even when
    plenty of in-stock products match the same words."""
    base = title_query(tokens, in_stock=False, groups=groups)
    clean = [re.sub(r"['\"]", "", tag) for tag in tags if tag]
    if not clean:
        raise ShopifyError("No tags are set for out-of-stock products.")
    return base + " AND inventory_total:<=0 AND (" + " OR ".join(f"tag:'{tag}'" for tag in clean) + ")"


def handles_query(handles):
    """A Shopify search for products with these handles (from the quick order list)."""
    clean = [re.sub(r"[^a-z0-9-]", "", handle.lower()) for handle in handles]
    return " OR ".join(f"handle:{handle}" for handle in clean if handle)


def ids_query(product_ids):
    """A Shopify search for products with these ids (gid://shopify/Product/123 or just 123)."""
    numbers = [re.sub(r"\D", "", str(product_id).rsplit("/", 1)[-1]) for product_id in product_ids]
    return " OR ".join(f"id:{number}" for number in numbers if number)

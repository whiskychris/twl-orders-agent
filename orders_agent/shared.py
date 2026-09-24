"""The read-only tools that are also shared: this agent's own model uses them, and other agents' models
can call them through the gateway (docs/agent-contract.md in twl-gateway, "Shared tools"), always as the
person they are answering. One definition for both, so the two can never drift apart.

The same two rules as tools.py apply, and neither depends on a model:
1. A tool is only offered, and only runs, if the caller's AuthContext has its capability. The context
   comes from the same user_id and visibility whether the request came from Slack or from another agent.
2. What a tool fetches (customer fields, the order note, stock quantities) is chosen from the AuthContext,
   never from an argument. The schemas have no such argument.

Only tools that read belong here. Anything that prepares a draft or writes stays in tools.py and entry.py,
behind this agent's own approval.
"""

from dataclasses import dataclass
from typing import Callable

from . import entry, product_pick
from .authorization import ORDER_ENTRY, audit_tool
from .sources import shopify

STRING = {"type": "string"}
INTEGER = {"type": "integer"}
BOOLEAN = {"type": "boolean"}


def _schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required)}


class ToolRefused(Exception):
    """The tool doesn't exist, or this person may not use it here. The message is safe to show."""


@dataclass(frozen=True)
class SharedTool:
    name: str
    capability: str
    description: str
    schema: dict
    run: Callable  # run(ctx, args) -> JSON-serialisable data. Blocking; raises ShopifyError or EntryError


SHARED_TOOLS = (
    SharedTool(
        name="search_orders",
        capability="orders",
        description="List Shopify orders matching a search, newest first. Uses Shopify search syntax, for "
            "example: created_at:>=2026-09-20T00:00:00+10:00, financial_status:paid, "
            "fulfillment_status:unfulfilled, status:open, sku:ABC123, tag:vip, name:1234. Returns "
            "at most 50 orders with status, total, item lines and where they were sold. If the "
            "user has no customer access, only those structured filters work (no free text, "
            "names, emails or addresses). Only orders from the last 60 days are visible unless "
            "the store granted read_all_orders.\n"
            "customer_tag:a,b (comma means OR) filters by the CUSTOMER's tags - this service's own "
            "filter, not Shopify's (Shopify has no direct way to search orders by the customer's "
            "tags), so it costs extra: it scans recent orders rather than a single lookup, and needs "
            "customer access. TWL's 'trade customers' (bottle shops, online retailers, bars, pubs, "
            "restaurants - resellers and hospitality) are tagged Off-Prem (retailers) or On-Prem "
            "(hospitality) on the customer, so 'orders from trade customers' is "
            "customer_tag:Off-Prem,On-Prem. Combine with other filters as usual, for example "
            "customer_tag:Off-Prem,On-Prem financial_status:paid. If a scan can't find enough within "
            "its limit, the result says so and suggests narrowing the search (a date range, for "
            "example) rather than silently under-reporting.",
        schema=_schema(
            {
                "query": {**STRING, "description": "Shopify order search string. Empty means all recent orders."},
                "limit": {**INTEGER, "description": "How many orders, 1 to 50. Default 20."},
                "oldest_first": {**BOOLEAN, "description": "Oldest first instead of newest first."},
            },
            required=["query"],
        ),
        run=lambda ctx, args: shopify.search_orders(
            args.get("query", ""),
            limit=args.get("limit", 20),
            oldest_first=bool(args.get("oldest_first", False)),
            include_customer=ctx.has("customers"),
        ),
    ),
    SharedTool(
        name="get_order",
        capability="orders",
        description="Full detail for one order by its number (for example 1234 or #1234): status, totals, "
            "shipping method, item lines, fulfilments and tracking numbers. Customer details and "
            "the order note are included only if the user has customer access.",
        schema=_schema({"order": {**STRING, "description": "Order number, with or without #."}}, required=["order"]),
        run=lambda ctx, args: shopify.get_order(args.get("order", ""), include_customer=ctx.has("customers")),
    ),
    SharedTool(
        name="summarise_orders",
        capability="orders",
        description="Count orders and total their value for a search (up to 2,000 orders), with counts by "
            "payment and fulfilment status. Use this for 'how many orders' and 'how much did we "
            "sell' questions instead of listing orders. Same search syntax and limits as "
            "search_orders.",
        schema=_schema(
            {
                "query": {**STRING, "description": "Shopify order search string."},
                "max_pages": {**INTEGER, "description": "Pages of 250 orders to read, 1 to 8. Default 4."},
            },
            required=["query"],
        ),
        run=lambda ctx, args: shopify.summarise_orders(
            args.get("query", ""), include_customer=ctx.has("customers"), max_pages=args.get("max_pages", 4)
        ),
    ),
    SharedTool(
        name="search_products",
        capability="products",
        description="Find products and their variants: title, status, vendor, type, SKUs and selling "
            "price. Stock quantities are included only if the user has inventory access. Search "
            "syntax examples: title:*macallan*, vendor:Adelphi, product_type:whisky, "
            "status:active, sku:ABC123, tag:rare. Prices are selling prices. Costs and margins are "
            "not available.",
        schema=_schema(
            {
                "query": {**STRING, "description": "Shopify product search string."},
                "limit": {**INTEGER, "description": "How many products, 1 to 25. Default 20."},
            },
            required=["query"],
        ),
        run=lambda ctx, args: shopify.search_products(
            args.get("query", ""), limit=args.get("limit", 20), include_inventory=ctx.has("inventory")
        ),
    ),
    SharedTool(
        name="get_inventory",
        capability="inventory",
        description="Stock by location for inventory items matching a search, for example sku:ABC123. "
            "Returns available (can be sold), on_hand (physically there), committed (reserved by "
            "open orders) and incoming (on the way in) per location.",
        schema=_schema(
            {
                "query": {**STRING, "description": "Inventory item search, for example sku:ABC123."},
                "limit": {**INTEGER, "description": "How many items, 1 to 25. Default 10."},
            },
            required=["query"],
        ),
        run=lambda ctx, args: shopify.get_inventory(args.get("query", ""), limit=args.get("limit", 10)),
    ),
    SharedTool(
        name="low_stock",
        capability="inventory",
        description="Active product variants whose total inventory is at or below a threshold, lowest "
            "first. Use for 'what is running low' and 'what is out of stock' (threshold 0).",
        schema=_schema(
            {
                "threshold": {**INTEGER, "description": "Inventory at or below this number. Default 5."},
                "limit": {**INTEGER, "description": "How many variants, 1 to 100. Default 50."},
            }
        ),
        run=lambda ctx, args: shopify.low_stock(threshold=args.get("threshold", 5), limit=args.get("limit", 50)),
    ),
    SharedTool(
        name="search_customers",
        capability="customers",
        description="Find customers: name, email, phone, number of orders, total spent, last order, tags "
            "and city. Search syntax examples: email:name@example.com, last_name:Smith, "
            "orders_count:>5, total_spent:>1000, tag:vip, country:AU. Personal data. Share only "
            "what the question needs.",
        schema=_schema(
            {
                "query": {**STRING, "description": "Shopify customer search string."},
                "limit": {**INTEGER, "description": "How many customers, 1 to 25. Default 10."},
            },
            required=["query"],
        ),
        run=lambda ctx, args: shopify.search_customers(args.get("query", ""), limit=args.get("limit", 10)),
    ),
    SharedTool(
        name="find_variant",
        capability=ORDER_ENTRY,
        description="Find the product to order from what the user typed (for example 'Arran 10' or 'Ardnahoe "
            "Bholsa'). TWL's rules choose, not you. The answer has a `decision`: 'use' means one clear winner "
            "(use its variant_id and tell the user which product you chose); 'ask' means several plausible "
            "products (list the numbered options and ask which, and never pick for them); 'none' means nothing "
            "orderable matched (say so, and mention anything in `unavailable`, such as out of stock). Samples, "
            "gift packs, bottle splits and out-of-stock products are never offered. Use only variant_ids this "
            "tool returned. Search by name: SKUs are not usable.",
        schema=_schema(
            {"query": {**STRING, "description": "The product as the user named it, for example 'Arran 10'."}},
            required=["query"],
        ),
        run=lambda ctx, args: product_pick.find_for_order(args.get("query", ""), include_inventory=ctx.has("inventory")),
    ),
    SharedTool(
        name="check_price",
        capability=ORDER_ENTRY,
        description="Look up the RRP, LUC and Rewards Member price for a product - checking prices, not raising "
            "an order. RRP is the price on The Whisky List's own variant. LUC is the trade catalog price "
            "with GST excluded (divided by 1.1); a product not on any trade catalog has no LUC. The "
            "Rewards Member price is RRP less 10% (TWL Brand or TWL Exclusive) or 20% (TWL IB / "
            "Independent Bottler); a product with none of those tags has no Rewards Member price. These "
            "are all selling prices, never a cost or a margin. Returns a `decision`: 'use' means one "
            "product matched, with `rrp`, `luc` (and `trade_catalog`, when there's more than one "
            "candidate, naming which one the LUC came from) and `rewards_member_price`, any of which may "
            "be null when it doesn't apply - say so plainly, never guess a number; 'ask' means several "
            "products matched - list the numbered options and ask which, never pick yourself; 'none' "
            "means nothing matched, or the product has more than one variant and none of them is TWL's "
            "own.",
        schema=_schema(
            {"query": {**STRING, "description": "The product as the user named it, for example 'Arran 10'."}},
            required=["query"],
        ),
        run=lambda ctx, args: entry.check_price(args.get("query", "")),
    ),
)
BY_NAME = {tool.name: tool for tool in SHARED_TOOLS}


def available(ctx):
    """The shared tools this caller may use, as tool definitions for another agent's model."""
    return [
        {"name": tool.name, "description": tool.description, "input_schema": tool.schema}
        for tool in SHARED_TOOLS
        if ctx.has(tool.capability)
    ]


def run(ctx, name, args):
    """Run one shared tool for this caller. Raises ToolRefused, or whatever the tool raises."""
    tool = BY_NAME.get(name)
    if tool is None:
        raise ToolRefused(f"There is no shared tool called {name}.")
    if not ctx.has(tool.capability):
        audit_tool(ctx, name, tool.capability, allowed=False)
        raise ToolRefused(f"This user does not have {tool.capability} access here.")
    audit_tool(ctx, name, tool.capability, allowed=True)
    return tool.run(ctx, args if isinstance(args, dict) else {})

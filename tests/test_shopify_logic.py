"""Unit tests for pure logic. Run with: python -m unittest discover tests

Nothing here calls Shopify. The GraphQL function is replaced with a fake.
"""

import unittest
from unittest import mock

from orders_agent.sources import shopify


class TransportErrorTests(unittest.TestCase):
    def call_with_errors(self, errors):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"errors": errors}
        with mock.patch.object(shopify, "get_shopify_config", return_value={"shop": "x.myshopify.com", "api_version": "2026-07", "access_token": "t"}), \
             mock.patch.object(shopify.requests, "post", return_value=response):
            with self.assertRaises(shopify.ShopifyError) as caught:
                shopify.graphql("query { x }")
        return str(caught.exception)

    def test_the_same_error_reported_for_every_item_is_said_once(self):
        message = self.call_with_errors([{"message": "Access denied for publication field."}] * 14)
        self.assertEqual(message, "Shopify error: Access denied for publication field.")

    def test_different_errors_are_all_kept_in_order(self):
        message = self.call_with_errors([
            {"message": "Access denied for publication field."},
            {"message": "Access denied for publishedOnPublication field."},
            {"message": "Access denied for publication field."},
        ])
        self.assertEqual(message, "Shopify error: Access denied for publication field.; Access denied for publishedOnPublication field.")


class HelperTests(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(shopify.clamp(500, 1, 50, 20), 50)
        self.assertEqual(shopify.clamp(0, 1, 50, 20), 1)
        self.assertEqual(shopify.clamp("abc", 1, 50, 20), 20)
        self.assertEqual(shopify.clamp(None, 1, 50, 20), 20)
        self.assertEqual(shopify.clamp("7", 1, 50, 20), 7)

    def test_structured_order_queries_are_allowed(self):
        for query in (
            "",
            "name:1234",
            "created_at:>=2026-09-21T00:00:00+10:00 created_at:<2026-09-22T00:00:00+10:00",
            "fulfillment_status:unfulfilled sku:ABC123",
            "financial_status:paid AND status:open",
            "(tag:vip OR tag:rare) NOT status:cancelled",
            'tag:"vip customers"',
            "-tag:test",
        ):
            self.assertTrue(shopify.order_query_is_structured(query), query)

    def test_anything_that_could_reveal_a_customer_is_refused(self):
        for query in (
            "smith",                       # free text also matches customer names
            "jane@example.com",
            "email:jane@example.com",
            "phone:0400000000",
            "customer:smith",
            "customer_id:123",
            "last_name:Smith",
            "shipping_address:Sydney",
            "name:1234 smith",             # one structured part does not excuse a free-text part
            '"jane smith"',
            "unknown_filter:x",
            "created_at:",                 # an empty value is not a filter
        ):
            self.assertFalse(shopify.order_query_is_structured(query), query)


class GuardTests(unittest.TestCase):
    def test_orders_search_by_customer_refused_without_role(self):
        with mock.patch.object(shopify, "graphql") as fake:
            with self.assertRaises(shopify.RestrictedError):
                shopify.search_orders("email:jane@example.com", include_customer=False)
            fake.assert_not_called()

    def test_orders_search_by_customer_allowed_with_role(self):
        with mock.patch.object(shopify, "graphql") as fake:
            fake.return_value = {"orders": {"nodes": [], "pageInfo": {"hasNextPage": False}}}
            result = shopify.search_orders("email:jane@example.com", include_customer=True)
            self.assertEqual(result["orders"], [])
            self.assertTrue(fake.call_args[0][1]["withCustomer"])

    def test_customer_fields_not_requested_without_role(self):
        with mock.patch.object(shopify, "graphql") as fake:
            fake.return_value = {"orders": {"nodes": [], "pageInfo": {"hasNextPage": False}}}
            shopify.search_orders("status:open", include_customer=False)
            self.assertFalse(fake.call_args[0][1]["withCustomer"])

    def test_free_text_order_search_refused_without_customer_access(self):
        with mock.patch.object(shopify, "graphql") as fake:
            with self.assertRaises(shopify.RestrictedError):
                shopify.search_orders("smith", include_customer=False)
            with self.assertRaises(shopify.RestrictedError):
                shopify.summarise_orders("smith", include_customer=False)
            fake.assert_not_called()

    def test_order_note_and_customer_are_behind_the_same_flag(self):
        # The order note often holds names and phone numbers, so it is only requested with the
        # customer flag. These checks fail if someone moves a field out from behind it.
        self.assertIn("note @include(if: $withCustomer)", shopify.ORDER_DETAIL)
        self.assertIn("customer @include(if: $withCustomer)", shopify.ORDER_DETAIL)
        self.assertIn("shippingAddress @include(if: $withCustomer)", shopify.ORDER_DETAIL)
        self.assertIn("customer @include(if: $withCustomer)", shopify.SEARCH_ORDERS)

    def test_get_order_omits_note_when_not_fetched(self):
        node = {
            "name": "#1001", "createdAt": "2026-09-21T01:00:00Z", "cancelledAt": None,
            "displayFinancialStatus": "PAID", "displayFulfillmentStatus": "FULFILLED",
            "currentTotalPriceSet": {"shopMoney": {"amount": "10.00", "currencyCode": "AUD"}},
            "lineItems": {"nodes": []}, "fulfillments": [],
        }
        with mock.patch.object(shopify, "graphql", return_value={"orders": {"nodes": [node]}}) as fake:
            order = shopify.get_order("1001", include_customer=False)
        self.assertNotIn("note", order)
        self.assertNotIn("customer", order)
        self.assertFalse(fake.call_args[0][1]["withCustomer"])

    def test_products_stock_only_with_inventory(self):
        node = {
            "title": "Ardnahoe 10", "handle": "a", "status": "ACTIVE", "vendor": "V",
            "productType": "Whisky", "tracksInventory": True, "totalInventory": 12,
            "variants": {"nodes": [{"sku": "AH10", "title": "700ml", "price": "99.00", "inventoryQuantity": 12}]},
        }
        page = {"products": {"nodes": [node], "pageInfo": {"hasNextPage": False}}}
        with mock.patch.object(shopify, "graphql", return_value=page) as fake:
            without = shopify.search_products("title:*ardnahoe*", include_inventory=False)
            self.assertFalse(fake.call_args[0][1]["withInventory"])
            with_stock = shopify.search_products("title:*ardnahoe*", include_inventory=True)
            self.assertTrue(fake.call_args[0][1]["withInventory"])
        self.assertNotIn("total_inventory", without["products"][0])
        self.assertNotIn("inventory_quantity", without["products"][0]["variants"][0])
        self.assertEqual(with_stock["products"][0]["total_inventory"], 12)
        self.assertEqual(with_stock["products"][0]["variants"][0]["inventory_quantity"], 12)
        self.assertIn("@include(if: $withInventory)", shopify.SEARCH_PRODUCTS)


class SummariseTests(unittest.TestCase):
    def order(self, amount, cancelled=False, financial="PAID", fulfillment="UNFULFILLED"):
        return {
            "id": "gid://shopify/Order/1",
            "cancelledAt": "2026-09-21T00:00:00Z" if cancelled else None,
            "displayFinancialStatus": financial,
            "displayFulfillmentStatus": fulfillment,
            "currentTotalPriceSet": {"shopMoney": {"amount": amount, "currencyCode": "AUD"}},
        }

    def test_totals_and_counts(self):
        page = {
            "orders": {
                "nodes": [
                    self.order("100.00"),
                    self.order("50.50", financial="PENDING"),
                    self.order("0.00", cancelled=True, financial="VOIDED"),
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        }
        with mock.patch.object(shopify, "graphql", return_value=page):
            result = shopify.summarise_orders("created_at:>=2026-09-21")
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["cancelled"], 1)
        self.assertEqual(result["total"], "150.50")
        self.assertEqual(result["currency"], "AUD")
        self.assertEqual(result["by_financial_status"], {"PAID": 1, "PENDING": 1, "VOIDED": 1})
        self.assertFalse(result["truncated"])

    def test_truncation_is_reported(self):
        page = {
            "orders": {
                "nodes": [self.order("10.00")],
                "pageInfo": {"hasNextPage": True, "endCursor": "abc"},
            }
        }
        with mock.patch.object(shopify, "graphql", return_value=page) as fake:
            result = shopify.summarise_orders("status:any", max_pages=2)
        self.assertEqual(fake.call_count, 2)
        self.assertEqual(result["count"], 2)
        self.assertTrue(result["truncated"])
        self.assertIn("higher", result["note"])


class OrderRowTests(unittest.TestCase):
    def node(self):
        return {
            "name": "#1001",
            "createdAt": "2026-09-21T01:00:00Z",
            "cancelledAt": None,
            "displayFinancialStatus": "PAID",
            "displayFulfillmentStatus": "FULFILLED",
            "currentTotalPriceSet": {"shopMoney": {"amount": "199.00", "currencyCode": "AUD"}},
            "subtotalLineItemsQuantity": 2,
            "sourceName": "web",
            "tags": [],
            "lineItems": {"nodes": [{"title": "Ardnahoe 10", "sku": "AH10", "quantity": 2}]},
        }

    def test_no_customer_data_when_not_fetched(self):
        row = shopify._order_row(self.node())
        self.assertNotIn("customer", row)
        self.assertNotIn("ship_to", row)
        self.assertEqual(row["total"], "199.00 AUD")
        self.assertEqual(row["lines"][0]["sku"], "AH10")

    def test_customer_data_only_when_present(self):
        node = self.node()
        node["customer"] = {"displayName": "Jane Smith", "numberOfOrders": "3"}
        node["shippingAddress"] = {"city": "Sydney", "provinceCode": "NSW", "countryCodeV2": "AU"}
        row = shopify._order_row(node)
        self.assertEqual(row["customer"]["name"], "Jane Smith")
        self.assertEqual(row["ship_to"], "Sydney, NSW, AU")


class CustomerTagFilterTests(unittest.TestCase):
    """customer_tag: has no Shopify equivalent (only customer_id is searchable on an order), so
    search_orders extracts it and scans in code - see shopify._extract_customer_tags."""

    def order_node(self, name, customer_tags):
        return {
            "name": name, "createdAt": "2026-09-21T01:00:00Z", "cancelledAt": None,
            "displayFinancialStatus": "PAID", "displayFulfillmentStatus": "FULFILLED",
            "currentTotalPriceSet": {"shopMoney": {"amount": "10.00", "currencyCode": "AUD"}},
            "subtotalLineItemsQuantity": 1, "sourceName": "web", "tags": [],
            "customer": {"displayName": "X", "numberOfOrders": 1, "tags": customer_tags},
            "lineItems": {"nodes": []},
        }

    def test_extract_customer_tags_is_case_insensitive_and_strips_only_that_token(self):
        remaining, tags = shopify._extract_customer_tags("customer_tag:Off-Prem,On-Prem financial_status:paid")
        self.assertEqual(remaining, "financial_status:paid")
        self.assertEqual(tags, frozenset({"off-prem", "on-prem"}))

    def test_no_customer_tag_token_means_no_filtering(self):
        remaining, tags = shopify._extract_customer_tags("status:open")
        self.assertEqual(remaining, "status:open")
        self.assertIsNone(tags)

    def test_refused_without_customer_access(self):
        with mock.patch.object(shopify, "graphql") as fake:
            with self.assertRaises(shopify.RestrictedError):
                shopify.search_orders("customer_tag:Off-Prem", include_customer=False)
            fake.assert_not_called()

    def test_finds_matches_and_stops_scanning_once_enough_are_found(self):
        page = {
            "orders": {
                "nodes": [self.order_node("#1", ["VIP"]), self.order_node("#2", ["Off-Prem"]), self.order_node("#3", ["On-Prem"])],
                "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
            }
        }
        with mock.patch.object(shopify, "graphql", return_value=page) as fake:
            result = shopify.search_orders("customer_tag:Off-Prem,On-Prem", limit=2, include_customer=True)
        self.assertEqual([o["name"] for o in result["orders"]], ["#2", "#3"])
        self.assertEqual(fake.call_count, 1)  # stopped after the first page, enough were found
        self.assertNotIn("note", result)

    def test_scans_further_pages_when_the_first_has_no_matches(self):
        page1 = {"orders": {"nodes": [self.order_node("#1", ["VIP"])], "pageInfo": {"hasNextPage": True, "endCursor": "c1"}}}
        page2 = {"orders": {"nodes": [self.order_node("#2", ["Off-Prem"])], "pageInfo": {"hasNextPage": False, "endCursor": None}}}
        with mock.patch.object(shopify, "graphql", side_effect=[page1, page2]) as fake:
            result = shopify.search_orders("customer_tag:Off-Prem", limit=5, include_customer=True)
        self.assertEqual([o["name"] for o in result["orders"]], ["#2"])
        self.assertEqual(fake.call_count, 2)

    def test_gives_up_after_the_scan_cap_and_says_so(self):
        page = {"orders": {"nodes": [self.order_node("#x", ["VIP"])], "pageInfo": {"hasNextPage": True, "endCursor": "c"}}}
        with mock.patch.object(shopify, "graphql", return_value=page) as fake:
            result = shopify.search_orders("customer_tag:Off-Prem", limit=5, include_customer=True)
        self.assertEqual(result["orders"], [])
        self.assertTrue(result["has_more"])
        self.assertIn("note", result)
        self.assertEqual(fake.call_count, shopify.CUSTOMER_TAG_SCAN_MAX_PAGES)

    def test_other_filters_still_reach_shopify_alongside_customer_tag(self):
        page = {"orders": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}
        with mock.patch.object(shopify, "graphql", return_value=page) as fake:
            shopify.search_orders("customer_tag:Off-Prem financial_status:paid", include_customer=True)
        self.assertEqual(fake.call_args[0][1]["query"], "financial_status:paid")

    def test_order_row_carries_customer_tags_when_fetched(self):
        row = shopify._order_row(self.order_node("#1", ["Off-Prem"]))
        self.assertEqual(row["customer"]["tags"], ["Off-Prem"])


class LowStockTests(unittest.TestCase):
    def test_only_active_products_are_returned(self):
        page = {
            "productVariants": {
                "nodes": [
                    {"sku": "A", "displayName": "A", "inventoryQuantity": 0,
                     "product": {"title": "Active thing", "status": "ACTIVE"}},
                    {"sku": "B", "displayName": "B", "inventoryQuantity": 1,
                     "product": {"title": "Old thing", "status": "ARCHIVED"}},
                ],
                "pageInfo": {"hasNextPage": False},
            }
        }
        with mock.patch.object(shopify, "graphql", return_value=page):
            result = shopify.low_stock(threshold=5)
        self.assertEqual([v["sku"] for v in result["variants"]], ["A"])


if __name__ == "__main__":
    unittest.main()

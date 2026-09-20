"""Unit tests for pure logic. Run with: python -m unittest discover tests

Nothing here calls Shopify. The GraphQL function is replaced with a fake.
"""

import unittest
from unittest import mock

from orders_agent.sources import shopify


class HelperTests(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(shopify.clamp(500, 1, 50, 20), 50)
        self.assertEqual(shopify.clamp(0, 1, 50, 20), 1)
        self.assertEqual(shopify.clamp("abc", 1, 50, 20), 20)
        self.assertEqual(shopify.clamp(None, 1, 50, 20), 20)
        self.assertEqual(shopify.clamp("7", 1, 50, 20), 7)

    def test_needs_customer_access(self):
        self.assertTrue(shopify.needs_customer_access("email:jane@example.com"))
        self.assertTrue(shopify.needs_customer_access("jane@example.com"))
        self.assertTrue(shopify.needs_customer_access("last_name:Smith"))
        self.assertTrue(shopify.needs_customer_access("customer_id:123"))
        self.assertFalse(shopify.needs_customer_access("created_at:>=2026-09-21T00:00:00+10:00"))
        self.assertFalse(shopify.needs_customer_access("fulfillment_status:unfulfilled sku:ABC123"))
        self.assertFalse(shopify.needs_customer_access(""))


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

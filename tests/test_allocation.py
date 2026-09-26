"""Rewards allocation orders, handed over from an approved ballot in #rewards. Shopify is faked."""

import unittest
from decimal import Decimal
from unittest import mock

from orders_agent import allocation, authorization, entry

REWARDS = "C0C40L44YKC"
USERS = {"twl:chris-ross": {"name": "Chris Ross", "capabilities": ["orders", "products", "customers", "order_entry"]}}
APPROVER = {"id": "U0JKCTQJ3", "user_id": "twl:chris-ross", "name": "Chris",
            "roles": ["orders.use", "orders.approve", "allocations.use", "allocations.approve"]}
CONVERSATION = {"id": f"slack:{REWARDS}:1758.1", "source": "slack", "visibility": "channel"}
JANE, BOB = "gid://shopify/Customer/1", "gid://shopify/Customer/2"
A, B = "gid://shopify/ProductVariant/11", "gid://shopify/ProductVariant/12"
TERMS = {"id": "gid://shopify/PaymentTermsTemplate/7", "name": "Due on fulfillment", "type": "FULFILLMENT"}


def customer(customer_id, name):
    address = {"address1": "1 Test St", "city": "Sydney"}
    return {"kind": "customer", "customer_id": customer_id, "name": name, "shipping": address, "billing": address,
            "terms": None, "contact_of": []}


def context(orders=None, tag="allocation-dd-nov-2026"):
    return {"action": "create_allocation_orders", "allocation_id": "DD NOV 2026", "tag": tag,
            "orders": orders if orders is not None else [
                {"customer_id": JANE, "lines": [{"variant_id": A, "qty": 1}]},
                {"customer_id": BOB, "lines": [{"variant_id": B, "qty": 2}]}]}


class AllocationOrderTests(unittest.TestCase):
    def setUp(self):
        self.created, self.drafts = [], {}
        patches = [
            mock.patch.object(authorization, "get_authz_config", return_value=USERS),
            mock.patch.object(authorization, "get_order_entry_channels", return_value=frozenset({REWARDS})),
            mock.patch.object(allocation.shop, "default_unpaid_terms", return_value=TERMS),
            mock.patch.object(allocation.shop, "find_draft_by_tag", side_effect=lambda tag: self.drafts.get(tag)),
            mock.patch.object(allocation.shop, "get_customer", side_effect=lambda cid: customer(cid, {JANE: "Jane", BOB: "Bob"}[cid])),
            mock.patch.object(allocation.shop, "calculate", side_effect=self.fake_calculate),
            mock.patch.object(allocation.shop, "create_draft", side_effect=self.fake_create),
            mock.patch.object(allocation.shop, "complete_draft", side_effect=self.fake_complete),
            mock.patch.object(allocation, "admin_order_url", side_effect=lambda legacy: f"https://admin.example/orders/{legacy}"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def fake_calculate(self, draft_input):
        return {"total": Decimal("289.00"), "lines": [{"variant_id": l["variantId"], "quantity": l["quantity"]}
                                                      for l in draft_input["lineItems"]]}

    def fake_create(self, draft_input):
        self.created.append(draft_input)
        return {"id": f"gid://shopify/DraftOrder/{len(self.created)}"}

    def fake_complete(self, draft_id):
        n = draft_id.rsplit("/", 1)[-1]
        return {"id": f"gid://shopify/Order/9{n}", "name": f"#9{n}", "legacyResourceId": f"9{n}"}

    def test_one_unpaid_full_price_order_each_with_tags_and_note(self):
        result = allocation.create_allocation_orders(APPROVER, CONVERSATION, context(), "r1")
        self.assertEqual(len(self.created), 2)
        draft = self.created[1]
        self.assertEqual(draft["purchasingEntity"], {"customerId": BOB})
        self.assertEqual(draft["lineItems"], [{"variantId": B, "quantity": 2}])
        self.assertEqual(draft["paymentTerms"], {"paymentTermsTemplateId": TERMS["id"]})
        self.assertEqual(draft["tags"], ["allocation-dd-nov-2026", "allocation-dd-nov-2026-2"])
        self.assertIn("DD NOV 2026", draft["note"])
        self.assertIn("Created 2 unpaid allocation orders", result["text"])
        self.assertEqual(result["react"], "white_check_mark")

    def test_repeat_is_safe(self):
        self.drafts["allocation-dd-nov-2026-1"] = {"status": "COMPLETED", "order": {"name": "#9001"}}
        result = allocation.create_allocation_orders(APPROVER, CONVERSATION, context(), "r1")
        self.assertEqual(len(self.created), 1)
        self.assertIn("Jane: already has #9001", result["text"])

    def test_needs_allocations_approve(self):
        user = {**APPROVER, "roles": ["orders.use", "orders.approve"]}
        with self.assertRaises(entry.ActRefused):
            allocation.create_allocation_orders(user, CONVERSATION, context(), "r1")

    def test_malformed_lists_create_nothing(self):
        for bad in ([], [{"customer_id": JANE, "lines": []}],
                    [{"customer_id": JANE, "lines": [{"variant_id": A, "qty": 0}]}],
                    [{"customer_id": "Jane", "lines": [{"variant_id": A, "qty": 1}]}],
                    [{"customer_id": JANE, "lines": [{"variant_id": A, "qty": 1}]}] * 2):
            with self.assertRaises(entry.ActRefused):
                allocation.create_allocation_orders(APPROVER, CONVERSATION, context(bad), "r1")
        with self.assertRaises(entry.ActRefused):
            allocation.create_allocation_orders(APPROVER, CONVERSATION, context(tag="rewards-gift-2026"), "r1")
        self.assertEqual(self.created, [])


class AllocationInvoiceTests(unittest.TestCase):
    TAG = "allocation-dd-nov-2026"

    def setUp(self):
        self.orders = {
            "gid://shopify/Order/1": {"id": "gid://shopify/Order/1", "name": "#9001", "legacyResourceId": "1",
                                      "tags": [self.TAG, f"{self.TAG}-1"], "cancelledAt": None,
                                      "displayFinancialStatus": "PENDING"},
            "gid://shopify/Order/2": {"id": "gid://shopify/Order/2", "name": "#9002", "legacyResourceId": "2",
                                      "tags": [self.TAG, f"{self.TAG}-invoiced"], "cancelledAt": None,
                                      "displayFinancialStatus": "PENDING"},
            "gid://shopify/Order/3": {"id": "gid://shopify/Order/3", "name": "#9003", "legacyResourceId": "3",
                                      "tags": ["something-else"], "cancelledAt": None, "displayFinancialStatus": "PENDING"},
        }
        self.sent, self.tagged = [], []

        def fake_graphql(query, variables=None):
            if "orderInvoiceSend" in query:
                self.sent.append(variables["id"])
                return {"orderInvoiceSend": {"userErrors": []}}
            if "tagsAdd" in query:
                self.tagged.append((variables["id"], variables["tags"]))
                return {"tagsAdd": {"userErrors": []}}
            return {"order": self.orders.get(variables["id"])}

        patches = [
            mock.patch.object(authorization, "get_authz_config", return_value=USERS),
            mock.patch.object(authorization, "get_order_entry_channels", return_value=frozenset({REWARDS})),
            mock.patch.object(allocation, "graphql", side_effect=fake_graphql),
            mock.patch.object(allocation, "admin_order_url", side_effect=lambda legacy: f"https://admin.example/orders/{legacy}"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def run_invoices(self, ids, user=APPROVER, tag=TAG):
        return allocation.send_allocation_invoices(user, CONVERSATION, {
            "action": "send_allocation_invoices", "allocation_id": "DD NOV 2026", "tag": tag, "order_ids": ids}, "r1")

    def test_sends_shopify_invoices_once_and_only_for_this_allocation(self):
        result = self.run_invoices(["gid://shopify/Order/1", "gid://shopify/Order/2", "gid://shopify/Order/3"])
        self.assertEqual(self.sent, ["gid://shopify/Order/1"])
        self.assertEqual(self.tagged, [("gid://shopify/Order/1", [f"{self.TAG}-invoiced"])])
        self.assertIn("Sent 1 Shopify invoice", result["text"])
        self.assertIn("was invoiced before", result["text"])
        self.assertIn("isn't one of this allocation's orders", result["text"])
        self.assertEqual(result["react"], "warning")

    def test_needs_allocations_approve_and_a_well_formed_list(self):
        with self.assertRaises(entry.ActRefused):
            self.run_invoices(["gid://shopify/Order/1"], user={**APPROVER, "roles": ["orders.use", "orders.approve"]})
        for bad in ([], ["#9001"]):
            with self.assertRaises(entry.ActRefused):
                self.run_invoices(bad)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()

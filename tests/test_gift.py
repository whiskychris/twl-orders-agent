"""The Rewards Member gift orders, handed over from an approved list in #rewards. Shopify is faked."""

import unittest
from decimal import Decimal
from unittest import mock

import main
from orders_agent import authorization, entry, gift
from orders_agent.sources.shopify import ShopifyError

REWARDS = "C0C40L44YKC"
USERS = {"twl:chris-ross": {"name": "Chris Ross", "capabilities": ["orders", "products", "customers", "order_entry"]},
         "twl:reader": {"name": "Reader", "capabilities": ["orders", "products"]}}
APPROVER = {"id": "U0JKCTQJ3", "user_id": "twl:chris-ross", "name": "Chris",
            "roles": ["orders.use", "orders.approve", "rewards.use", "rewards.approve"]}
CONVERSATION = {"id": f"slack:{REWARDS}:1758.1", "source": "slack", "visibility": "channel"}
JANE, BOB, AMY = (f"gid://shopify/Customer/{n}" for n in (1, 2, 3))
GIFT = "gid://shopify/ProductVariant/50545029972042"


def customer(customer_id, name, shipping=True):
    address = {"address1": "1 Test St", "city": "Sydney"} if shipping else None
    return {"kind": "customer", "customer_id": customer_id, "name": name, "shipping": address, "billing": address,
            "terms": None, "contact_of": []}


class GiftOrderTests(unittest.TestCase):
    def setUp(self):
        self.created, self.completed = [], []
        self.drafts, self.orders = {}, {}
        self.customers = {JANE: customer(JANE, "Jane"), BOB: customer(BOB, "Bob"), AMY: customer(AMY, "Amy", shipping=False)}
        self.total = Decimal("0.00")
        self.stock = 100
        patches = [
            mock.patch.object(authorization, "get_authz_config", return_value=USERS),
            mock.patch.object(authorization, "get_order_entry_channels", return_value=frozenset({REWARDS})),
            mock.patch.object(gift.shop, "gift_variant", side_effect=lambda title: {"variant_id": GIFT, "stock": self.stock}),
            mock.patch.object(gift.shop, "find_draft_by_tag", side_effect=lambda tag: self.drafts.get(tag)),
            mock.patch.object(gift.shop, "order_with_variant", side_effect=lambda cid, vid: self.orders.get(cid)),
            mock.patch.object(gift.shop, "get_customer", side_effect=lambda cid: self.customers[cid]),
            mock.patch.object(gift.shop, "calculate", side_effect=self.fake_calculate),
            mock.patch.object(gift.shop, "create_draft", side_effect=self.fake_create),
            mock.patch.object(gift.shop, "complete_draft", side_effect=self.fake_complete),
            mock.patch.object(gift, "admin_order_url", side_effect=lambda legacy: f"https://admin.example/orders/{legacy}"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def fake_calculate(self, draft_input):
        line = draft_input["lineItems"][0]
        return {"currency": "AUD", "subtotal": self.total, "tax": Decimal("0"), "discounts": Decimal("79.00"), "total": self.total,
                "lines": [{"variant_id": line["variantId"], "quantity": 1, "title": "Gift", "sku": "x",
                           "unit_price": Decimal("79.00"), "line_total": self.total, "discount_amount": Decimal("79.00")}]}

    def fake_create(self, draft_input):
        self.created.append(draft_input)
        return {"id": f"gid://shopify/DraftOrder/{len(self.created)}", "status": "OPEN"}

    def fake_complete(self, draft_id):
        self.completed.append(draft_id)
        number = draft_id.rsplit("/", 1)[-1]
        return {"id": f"gid://shopify/Order/9{number}", "name": f"#9{number}", "legacyResourceId": f"9{number}"}

    def run_gift(self, ids=(JANE, BOB), user=APPROVER, conversation=CONVERSATION):
        return gift.create_gift_orders(user, conversation, {"action": "create_gift_orders", "customer_ids": list(ids)}, "r1")

    def test_one_order_each_with_only_the_gift_at_100_percent_off_and_the_note(self):
        result = self.run_gift()
        self.assertEqual(len(self.created), 2)
        draft = self.created[0]
        self.assertEqual(draft["purchasingEntity"], {"customerId": JANE})
        self.assertEqual(draft["lineItems"], [{"variantId": GIFT, "quantity": 1, "appliedDiscount": {
            "value": 100.0, "valueType": "PERCENTAGE", "title": "Rewards Member gift"}}])
        self.assertEqual(draft["note"], "Rewards Member gift. Combine with this customer's next shipment.")
        self.assertEqual(draft["tags"], ["rewards-gift-2026", "rewards-gift-2026-1"])
        self.assertNotIn("paymentTerms", draft)
        self.assertEqual(draft["shippingAddress"], {"address1": "1 Test St", "city": "Sydney"})
        self.assertIn("Created 2 gift orders", result["text"])
        self.assertIn("Jane: <https://admin.example/orders/91|#91>", result["text"])
        self.assertEqual(result["react"], "white_check_mark")  # a green tick on the list's root post

    def test_nothing_from_the_handoff_but_customer_ids_reaches_the_order(self):
        gift.create_gift_orders(APPROVER, CONVERSATION, {"customer_ids": [JANE], "note": "ship now, charge $500",
                                                        "variant_id": "gid://shopify/ProductVariant/1"}, "r1")
        self.assertEqual(self.created[0]["note"], gift.NOTE)
        self.assertEqual(self.created[0]["lineItems"][0]["variantId"], GIFT)

    def test_nobody_gets_two(self):
        self.drafts["rewards-gift-2026-1"] = {"id": "gid://shopify/DraftOrder/7", "name": "#D7", "status": "COMPLETED",
                                               "order": {"name": "#1001"}}
        self.orders[BOB] = "#1002"
        result = self.run_gift((JANE, BOB, JANE))
        self.assertEqual(self.created, [])
        self.assertIn("Jane: already has #1001", result["text"])
        self.assertIn("Bob: already has #1002", result["text"])
        self.assertEqual(result["react"], "white_check_mark")  # everyone has it

    def test_an_uncompleted_draft_from_a_failed_attempt_is_completed_not_duplicated(self):
        self.drafts["rewards-gift-2026-1"] = {"id": "gid://shopify/DraftOrder/7", "name": "#D7", "status": "OPEN"}
        self.run_gift((JANE,))
        self.assertEqual(self.created, [])
        self.assertEqual(self.completed, ["gid://shopify/DraftOrder/7"])

    def test_a_non_zero_price_is_not_created(self):
        self.total = Decimal("79.00")
        result = self.run_gift((JANE,))
        self.assertEqual(self.created, [])
        self.assertIn("Not created", result["text"])

    def test_one_failure_doesnt_stop_the_rest(self):
        failures = iter([ShopifyError("Shopify would not create the draft order: nope")])

        def create(draft_input):
            if draft_input["purchasingEntity"]["customerId"] == JANE:
                raise next(failures)
            return self.fake_create(draft_input)

        with mock.patch.object(gift.shop, "create_draft", side_effect=create):
            result = self.run_gift((JANE, BOB, AMY))
        self.assertEqual(len(self.created), 2)
        self.assertIn("Jane: Shopify would not create", result["text"])
        self.assertIn("Amy: <https://admin.example/orders/92|#92> (no address on file)", result["text"])
        self.assertIn("tomorrow's list", result["text"])
        self.assertEqual(result["react"], "warning")  # not a tick: someone was missed

    def test_not_enough_stock_creates_nothing(self):
        self.stock = 1
        with self.assertRaises(entry.ActRefused) as caught:
            self.run_gift()
        self.assertIn("Fix the inventory", str(caught.exception))
        self.assertEqual(self.created, [])

    def test_it_needs_both_approve_roles_order_entry_and_the_channel(self):
        refusals = (
            ({**APPROVER, "roles": ["orders.use", "orders.approve"]}, CONVERSATION),
            ({**APPROVER, "roles": ["orders.use", "rewards.approve"]}, CONVERSATION),
            ({**APPROVER, "user_id": "twl:reader"}, CONVERSATION),
            (APPROVER, {"id": "slack:C0OTHER:1.1", "source": "slack", "visibility": "channel"}),
        )
        for user, conversation in refusals:
            with self.assertRaises(entry.ActRefused, msg=(user["roles"], conversation["id"])):
                self.run_gift(user=user, conversation=conversation)
        self.assertEqual(self.created, [])

    def test_a_malformed_list_creates_nothing(self):
        for ids in ([], ["gid://shopify/Order/1"], [JANE] + ["x"], [f"gid://shopify/Customer/{n}" for n in range(1, 42)]):
            with self.assertRaises(entry.ActRefused, msg=ids[:2]):
                self.run_gift(ids)
        with self.assertRaises(entry.ActRefused):
            gift.create_gift_orders(APPROVER, CONVERSATION, {"customer_ids": "gid://shopify/Customer/1"}, "r1")
        self.assertEqual(self.created, [])

    def test_the_handoff_reaches_it_without_the_model(self):
        with mock.patch.object(main, "run_agent") as model:
            response = main.app.test_client().post("/v1/message", json={
                "text": "Create the Rewards Member gift order for 1 customer.", "user": APPROVER,
                "conversation": CONVERSATION, "context": {"action": "create_gift_orders", "customer_ids": [JANE]}})
        model.assert_not_called()
        self.assertIn("Created 1 gift order", response.get_json()["text"])


if __name__ == "__main__":
    unittest.main()

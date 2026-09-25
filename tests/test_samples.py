"""Sample orders: fixed accounts, always $0, where they work, and create-and-fulfil in one press.

Shopify is replaced with fakes. Nothing here touches a network or a secret.
"""

import unittest
from decimal import Decimal
from unittest import mock

from orders_agent import authorization, entry, samples
from orders_agent.authorization import AuthContext, AuthorizationError, resolve_context
from orders_agent.sources.shopify import ShopifyError
from orders_agent.tools import build_server

DISPATCH = "C0DISPATCH"
INVENTORY = "C0INVENTORY"
VARIANT = "gid://shopify/ProductVariant/10"
EVENTS = "gid://shopify/Customer/70"
SALES = "gid://shopify/Customer/50"
USERS = {"twl:minwoo": {"name": "Minwoo", "capabilities": ["samples", "fulfil"]}}


def ctx(capabilities=("samples",)):
    return AuthContext(
        request_id="r1", user_id="twl:minwoo", name="Minwoo", source="slack", visibility="channel",
        capabilities=frozenset(capabilities), channel_id=INVENTORY,
    )


def convo(channel=INVENTORY, visibility="channel"):
    return {"id": f"slack:{channel}:1.1", "source": "slack", "visibility": visibility}


def user(roles=("orders.use", "orders.approve")):
    return {"id": "U9", "user_id": "twl:minwoo", "name": "Minwoo", "roles": list(roles)}


def accounts(email):
    customer = EVENTS if email.startswith("events") else SALES
    return {"accounts": [{"account": "personal", "name": "TWL Events Stock", "customer_id": customer}]}


def customer(customer_id):
    return {"kind": "customer", "customer_id": customer_id, "name": "TWL Events Stock", "shipping": None, "billing": None, "contact_of": []}


def priced(total="0.00", discount="358.00"):
    return {
        "currency": "AUD", "subtotal": Decimal(total), "tax": Decimal("0"), "total": Decimal(total), "discounts": Decimal(discount),
        "lines": [{"variant_id": VARIANT, "title": "Arran 10", "sku": "A10", "quantity": 2, "unit_price": Decimal("179.00"),
                   "line_total": Decimal(total), "discount_amount": Decimal(discount)}],
    }


def fulfilment_view(lines=True, financial="PAID", status="UNFULFILLED"):
    rows = [{
        "fulfillment_order_id": "gid://shopify/FulfillmentOrder/1", "fulfillment_order_line_id": "gid://shopify/FulfillmentOrderLineItem/1",
        "line_item_id": "gid://shopify/LineItem/1", "title": "Arran 10", "sku": "A10", "remaining": 2, "location": "Artarmon",
        "status": "OPEN", "fulfillable": True,
    }] if lines else []
    return {"id": "gid://shopify/Order/9", "name": "#9001", "legacy_id": "9", "financial_status": financial,
            "fulfillment_status": status, "cancelled": False, "lines": rows}


class Fakes(unittest.TestCase):
    def setUp(self):
        for patch in (
            mock.patch.object(authorization, "get_authz_config", return_value=USERS),
            mock.patch.object(authorization, "get_order_entry_channels", return_value=frozenset({"C0SALES"})),
            mock.patch.object(authorization, "get_fulfil_channels", return_value=frozenset({DISPATCH, INVENTORY})),
            mock.patch.object(samples, "admin_order_url", side_effect=lambda legacy: f"https://admin.example/orders/{legacy}"),
            mock.patch.object(samples.shop, "find_by_email", side_effect=accounts),
            mock.patch.object(samples.shop, "get_customer", side_effect=customer),
            mock.patch.object(samples.shop, "get_variants", return_value={VARIANT: {"status": "ACTIVE", "stock": 10}}),
            mock.patch.object(samples.time, "sleep"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.calculate = mock.patch.object(samples.shop, "calculate", return_value=priced())
        self.calculate.start()
        self.addCleanup(self.calculate.stop)


class AuthorizationTests(Fakes):
    def test_samples_work_in_dispatch_and_inventory_only(self):
        for channel in (DISPATCH, INVENTORY):
            self.assertTrue(resolve_context(user(), convo(channel), "r1").has("samples"))
        for conversation in (convo("D0MINWOO", "dm"), convo("C0SALES")):
            with self.assertRaises(AuthorizationError) as caught:
                resolve_context(user(), conversation, "r1")
            self.assertIn("dispatch and inventory", str(caught.exception))

    def test_tools(self):
        _, names = build_server(ctx())
        self.assertIn("prepare_sample_order", names)
        self.assertIn("find_variant", names)            # to pick the products, even without order_entry
        for missing in ("prepare_draft_order", "find_customer", "prepare_invoice_for_order", "search_customers"):
            self.assertNotIn(missing, names)
        _, names = build_server(ctx(("samples", "order_entry")))
        self.assertEqual(names.count("find_variant"), 1)


class PrepareTests(Fakes):
    def test_always_the_fixed_account_and_100_percent_off(self):
        result = samples.prepare(ctx(), "Events", [{"variant_id": VARIANT, "quantity": 2, "discount_type": "percent", "discount_value": 10}])
        sent = samples.shop.calculate.call_args.args[0]
        self.assertEqual(sent["purchasingEntity"], {"customerId": EVENTS})
        self.assertEqual(sent["lineItems"][0]["appliedDiscount"]["value"], 100.0)
        self.assertNotIn("paymentTerms", sent)
        self.assertEqual(result["proposal"]["kind"], "sample_order")
        self.assertEqual([c["id"] for c in result["proposal"]["choices"]], ["create_fulfil", "create_only"])
        self.assertIn("Total 0.00", result["text"])
        samples.shop.find_by_email.assert_called_with("events@thewhiskylist.com.au")

    def test_refusals(self):
        with self.assertRaises(entry.EntryError):
            samples.prepare(ctx(), "a customer", [{"variant_id": VARIANT, "quantity": 1}])
        with self.assertRaises(entry.EntryError):
            samples.prepare(ctx(("orders",)), "sales", [{"variant_id": VARIANT, "quantity": 1}])
        with mock.patch.object(samples.shop, "calculate", return_value=priced(total="12.00")):
            with self.assertRaises(entry.EntryError) as caught:
                samples.prepare(ctx(), "sales", [{"variant_id": VARIANT, "quantity": 2}])
        self.assertIn("not 0.00", str(caught.exception))


class ExecuteTests(Fakes):
    def setUp(self):
        super().setUp()
        self.proposal = {**samples.prepare(ctx(), "events", [{"variant_id": VARIANT, "quantity": 2}])["proposal"], "token": "tok-abcdefgh"}
        self.order = {"id": "gid://shopify/Order/9", "name": "#9001", "legacyResourceId": "9"}

    def run_execute(self, choice="create_fulfil", existing=None, view=None, who=None, conversation=None, proposal=None):
        views = view if isinstance(view, list) else [view or fulfilment_view()]
        with mock.patch.object(samples.shop, "find_draft_by_tag", return_value=existing), \
             mock.patch.object(samples.shop, "create_draft", return_value={"id": "d1"}) as create, \
             mock.patch.object(samples.shop, "complete_draft", return_value=self.order), \
             mock.patch.object(samples.fulfil.shop, "find_order_for_fulfilment", side_effect=views + [views[-1]] * 5), \
             mock.patch.object(samples.fulfil.shop, "create_fulfilment", return_value={"id": "f1"}) as fulfil_call:
            result = entry.execute(who or user(), conversation or convo(), choice, proposal or self.proposal, "req1")
        return result, create, fulfil_call

    def test_create_and_fulfil_in_one_press(self):
        result, create, fulfil_call = self.run_execute()
        self.assertIn("created and marked fulfilled", result["text"])
        self.assertTrue(result["result"]["fulfilled"])
        draft = create.call_args.args[0]
        self.assertNotIn("paymentTerms", draft)
        self.assertIn("smith-tok-abcdefgh", draft["tags"])
        fulfil_call.assert_called_once_with(
            [("gid://shopify/FulfillmentOrder/1", [("gid://shopify/FulfillmentOrderLineItem/1", 2)])], None,
        )

    def test_create_only(self):
        result, _, fulfil_call = self.run_execute(choice="create_only")
        self.assertIn("created", result["text"])
        fulfil_call.assert_not_called()

    def test_waits_for_shopify_to_route_the_order(self):
        result, _, fulfil_call = self.run_execute(view=[fulfilment_view(lines=False), fulfilment_view()])
        self.assertTrue(result["result"]["fulfilled"])
        fulfil_call.assert_called_once()

    def test_a_fulfilment_problem_still_reports_the_created_order(self):
        for view in (fulfilment_view(financial="PENDING"), fulfilment_view(lines=False)):
            result, _, fulfil_call = self.run_execute(view=view)
            self.assertEqual(result["status"], "ok")
            self.assertIn("couldn't mark it fulfilled", result["text"])
            self.assertIn("fulfil: #9001", result["text"])
            fulfil_call.assert_not_called()

    def test_a_repeated_approval_creates_nothing_and_fulfils_nothing_twice(self):
        existing = {"id": "d1", "status": "COMPLETED", "order": self.order}
        result, create, fulfil_call = self.run_execute(existing=existing, view=fulfilment_view(lines=False, status="FULFILLED"))
        self.assertTrue(result["text"].startswith("Already done."))
        create.assert_not_called()
        fulfil_call.assert_not_called()

    def test_refused_without_role_capability_channel_or_button(self):
        for kwargs in (
            dict(who=user(roles=("orders.use",))),
            dict(conversation=convo("C0SALES")),
            dict(conversation=convo("D0MINWOO", "dm")),
            dict(choice="approve_only"),
            dict(choice=None),
        ):
            with self.assertRaises(entry.ActRefused, msg=kwargs):
                self.run_execute(**kwargs)

    def test_a_tampered_account_or_price_is_refused(self):
        tampered = {**self.proposal, "payload": {**self.proposal["payload"], "customer_id": "gid://shopify/Customer/12345"}}
        with self.assertRaises(entry.ActRefused):
            self.run_execute(proposal=tampered)
        with self.assertRaises(entry.ActRefused):
            self.run_execute(proposal={**self.proposal, "payload": {**self.proposal["payload"], "account": "someone"}})
        with mock.patch.object(samples.shop, "calculate", return_value=priced(total="5.00")):
            with self.assertRaises(entry.ActRefused):
                self.run_execute()

    def test_a_shopify_failure_before_creation_is_refused(self):
        with mock.patch.object(samples.shop, "find_draft_by_tag", side_effect=ShopifyError("down")):
            with self.assertRaises(entry.ActRefused):
                entry.execute(user(), convo(), "create_fulfil", self.proposal, "req1")


if __name__ == "__main__":
    unittest.main()

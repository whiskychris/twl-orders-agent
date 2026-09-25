"""Marking an order fulfilled: who may, where, which lines, and the approval executor's safety.

Shopify is replaced with fakes. Nothing here touches a network or a secret.
"""

import unittest
from unittest import mock

from orders_agent import authorization, entry, fulfil
from orders_agent.authorization import AuthContext, AuthorizationError, resolve_context
from orders_agent.sources.shopify import ShopifyError
from orders_agent.tools import build_server

ORDER_ID = "gid://shopify/Order/555"
DISPATCH = "C0DISPATCH"
INVENTORY = "C0INVENTORY"
USERS = {
    "twl:scott": {"name": "Scott", "capabilities": ["fulfil"]},
    "twl:jimmy": {"name": "Jimmy", "capabilities": ["orders", "order_entry", "fulfil"]},
    "twl:reader": {"name": "Reader", "capabilities": ["orders"]},
}


def fo_line(n, title, remaining, line_item=None, fo=1, location="Artarmon", fulfillable=True, status="OPEN"):
    return {
        "fulfillment_order_id": f"gid://shopify/FulfillmentOrder/{fo}",
        "fulfillment_order_line_id": f"gid://shopify/FulfillmentOrderLineItem/{n}",
        "line_item_id": f"gid://shopify/LineItem/{line_item or n}",
        "title": title, "sku": f"SKU{n}", "remaining": remaining, "location": location,
        "status": status, "fulfillable": fulfillable,
    }


def order(lines=None, financial="PAID", cancelled=False):
    return {
        "id": ORDER_ID, "name": "#1234", "legacy_id": "555", "financial_status": financial,
        "fulfillment_status": "UNFULFILLED", "cancelled": cancelled,
        "lines": lines if lines is not None else [fo_line(1, "Arran 10", 2), fo_line(2, "GlenAllachie 12", 1)],
    }


def ctx(capabilities=("fulfil",), channel=DISPATCH):
    return AuthContext(
        request_id="r1", user_id="twl:scott", name="Scott", source="slack", visibility="channel",
        capabilities=frozenset(capabilities), channel_id=channel,
    )


def convo(channel=DISPATCH, visibility="channel"):
    return {"id": f"slack:{channel}:1.1", "source": "slack", "visibility": visibility}


def user(user_id="twl:scott", roles=("orders.use", "orders.approve")):
    return {"id": "U1", "user_id": user_id, "name": "Scott", "roles": list(roles)}


class Patched(unittest.TestCase):
    def setUp(self):
        for patch in (
            mock.patch.object(authorization, "get_authz_config", return_value=USERS),
            mock.patch.object(authorization, "get_order_entry_channels", return_value=frozenset({"C0SALES"})),
            mock.patch.object(authorization, "get_fulfil_channels", return_value=frozenset({DISPATCH, INVENTORY})),
            mock.patch.object(fulfil, "admin_order_url", side_effect=lambda legacy: f"https://admin.example/orders/{legacy}"),
        ):
            patch.start()
            self.addCleanup(patch.stop)


class AuthorizationTests(Patched):
    def test_fulfil_works_in_dispatch_and_inventory(self):
        for channel in (DISPATCH, INVENTORY):
            self.assertTrue(resolve_context(user(), convo(channel), "r1").has("fulfil"), channel)

    def test_fulfil_is_withheld_in_a_dm_and_other_channels(self):
        jimmy = user("twl:jimmy")
        for conversation in (convo("D0SCOTT", "dm"), convo("C0SALES"), convo("C0OTHER"), {"source": "slack"}):
            ctx_ = resolve_context(jimmy, conversation, "r1")
            self.assertFalse(ctx_.has("fulfil"), conversation)
            self.assertIn("fulfil", ctx_.withheld)
            self.assertIn("dispatch and inventory channels", ctx_.describe())

    def test_a_fulfil_only_user_elsewhere_is_told_where_it_works(self):
        with self.assertRaises(AuthorizationError) as caught:
            resolve_context(user(), convo("C0OTHER"), "r1")
        self.assertIn("dispatch and inventory", str(caught.exception))

    def test_a_user_without_the_capability_never_gets_it(self):
        self.assertFalse(resolve_context(user("twl:reader"), convo(DISPATCH), "r1").has("fulfil"))

    def test_tools_exist_only_with_the_capability(self):
        _, names = build_server(ctx())
        self.assertIn("prepare_fulfilment", names)
        self.assertIn("get_fulfillable_items", names)
        self.assertNotIn("prepare_draft_order", names)
        _, names = build_server(ctx(("orders",)))
        self.assertNotIn("prepare_fulfilment", names)


class ChannelListTests(unittest.TestCase):
    def test_the_channel_list_is_read_from_the_secret(self):
        authorization._cache.update({"value": None, "loaded_at": 0.0})
        self.addCleanup(authorization._cache.update, {"value": None, "loaded_at": 0.0})
        secret = {"users": USERS, "fulfil_channels": [" c0dispatch ", "", 5]}
        with mock.patch.object(authorization, "read_secret_json", return_value=secret):
            self.assertEqual(authorization.get_fulfil_channels(), frozenset({"C0DISPATCH", "5"}))
            self.assertEqual(authorization.get_order_entry_channels(), frozenset())


class TrackingTests(unittest.TestCase):
    def test_tracking_is_optional(self):
        self.assertIsNone(fulfil.clean_tracking(None, None))
        self.assertIsNone(fulfil.clean_tracking("  ", ""))

    def test_known_carriers_get_shopifys_name(self):
        self.assertEqual(fulfil.clean_tracking("12344556", "DHL"), {"number": "12344556", "company": "DHL Express"})
        self.assertEqual(fulfil.clean_tracking("AB 123 456", "auspost"), {"number": "AB123456", "company": "Australia Post"})

    def test_other_carriers_are_kept_as_typed(self):
        self.assertEqual(fulfil.clean_tracking("XYZ-99", "Bob's Couriers"), {"number": "XYZ-99", "company": "Bob's Couriers"})
        self.assertEqual(fulfil.clean_tracking("XYZ-99", None), {"number": "XYZ-99", "company": None})

    def test_bad_tracking_is_refused(self):
        for number, company in (("12", None), ("12;DROP", None), (None, "DHL"), ("1" * 60, None)):
            with self.assertRaises(entry.EntryError):
                fulfil.clean_tracking(number, company)


class PrepareTests(Patched):
    def prepare(self, items=None, found=None, **kwargs):
        with mock.patch.object(fulfil.shop, "find_order_for_fulfilment", return_value=found or order()):
            return fulfil.prepare(ctx(), "#1234", items, **kwargs)

    def test_whole_order_by_default(self):
        result = self.prepare()
        lines = result["proposal"]["payload"]["lines"]
        self.assertEqual([(line["title"], line["quantity"]) for line in lines], [("Arran 10", 2), ("GlenAllachie 12", 1)])
        self.assertEqual(result["proposal"]["kind"], "fulfilment")
        self.assertEqual(result["proposal"]["choices"][0]["id"], "approve_fulfilment")
        self.assertIn("Tracking: none", result["text"])
        self.assertIn("won't be emailed", result["text"])
        self.assertNotIn("Still unfulfilled", result["text"])

    def test_some_items_and_tracking(self):
        result = self.prepare(
            [{"line_item_id": "gid://shopify/LineItem/1", "quantity": 1}], tracking_number="12344556", tracking_company="DHL",
        )
        payload = result["proposal"]["payload"]
        self.assertEqual([(line["title"], line["quantity"]) for line in payload["lines"]], [("Arran 10", 1)])
        self.assertEqual(payload["tracking"], {"number": "12344556", "company": "DHL Express"})
        self.assertIn("DHL Express 12344556", result["text"])
        self.assertIn("Still unfulfilled afterwards: 2", result["text"])

    def test_a_line_split_across_fulfillment_orders_is_allocated(self):
        found = order([fo_line(1, "Arran 10", 2, line_item=9, fo=1), fo_line(2, "Arran 10", 3, line_item=9, fo=2)])
        result = self.prepare([{"line_item_id": "gid://shopify/LineItem/9", "quantity": 4}], found=found)
        self.assertEqual([line["quantity"] for line in result["proposal"]["payload"]["lines"]], [2, 2])

    def test_unpaid_orders_are_refused(self):
        for status in ("PENDING", "AUTHORIZED", "PARTIALLY_PAID", "REFUNDED", None):
            with self.assertRaises(entry.EntryError) as caught:
                self.prepare(found=order(financial=status))
            self.assertIn("isn't paid", str(caught.exception))

    def test_partially_refunded_counts_as_paid(self):
        self.assertTrue(self.prepare(found=order(financial="PARTIALLY_REFUNDED"))["proposal"])

    def test_refusals(self):
        cases = [
            (dict(found=order(cancelled=True)), "cancelled"),
            (dict(found=order(lines=[])), "nothing left"),
            (dict(items=[{"line_item_id": "gid://shopify/LineItem/1", "quantity": 3}]), "Only 2"),
            (dict(items=[{"line_item_id": "gid://shopify/LineItem/77"}]), "isn't waiting"),
            (dict(items=[{"line_item_id": "nope"}]), "line_item_id"),
            (dict(items=[{"line_item_id": "gid://shopify/LineItem/1"}, {"line_item_id": "gid://shopify/LineItem/1"}]), "already listed"),
            (dict(found=order([fo_line(1, "A", 1), fo_line(2, "B", 1, fo=2, location="Melbourne Warehouse")])), "more than one location"),
            (dict(found=order([fo_line(1, "A", 1), fo_line(2, "B", 1, fo=2, fulfillable=False, status="ON_HOLD")])), "on hold"),
        ]
        for kwargs, message in cases:
            with self.assertRaises(entry.EntryError, msg=message) as caught:
                self.prepare(**kwargs)
            self.assertIn(message, str(caught.exception))

    def test_needs_the_capability(self):
        with self.assertRaises(entry.EntryError):
            fulfil.prepare(ctx(("orders",)), "#1234")


class ExecuteTests(Patched):
    def setUp(self):
        super().setUp()
        with mock.patch.object(fulfil.shop, "find_order_for_fulfilment", return_value=order()):
            self.proposal = fulfil.prepare(ctx(), "#1234", tracking_number="12344556", tracking_company="DHL")["proposal"]

    def run_execute(self, live=None, who=None, conversation=None, choice="approve_fulfilment", proposal=None):
        with mock.patch.object(fulfil.shop, "find_order_for_fulfilment", return_value=live or order()), \
             mock.patch.object(fulfil.shop, "create_fulfilment", return_value={"id": "f1"}) as create:
            result = entry.execute(who or user(), conversation or convo(), choice, proposal or self.proposal, "req1")
        return result, create

    def test_fulfils_with_tracking_and_reports(self):
        result, create = self.run_execute()
        self.assertEqual(result["status"], "ok")
        self.assertIn("#1234", result["text"])
        self.assertIn("3 items", result["text"])
        self.assertIn("DHL Express 12344556", result["text"])
        create.assert_called_once_with(
            [("gid://shopify/FulfillmentOrder/1", [("gid://shopify/FulfillmentOrderLineItem/1", 2), ("gid://shopify/FulfillmentOrderLineItem/2", 1)])],
            {"number": "12344556", "company": "DHL Express"},
        )

    def test_a_repeated_approval_does_not_fulfil_twice(self):
        result, create = self.run_execute(live=order(lines=[]))
        self.assertTrue(result["text"].startswith("Already done."))
        create.assert_not_called()

    def test_a_change_since_the_preview_refuses(self):
        for live in (
            order([fo_line(1, "Arran 10", 1), fo_line(2, "GlenAllachie 12", 1)]),     # someone fulfilled one
            order([fo_line(1, "Arran 10", 2), fo_line(2, "GlenAllachie 12", 1, fulfillable=False)]),
            {**order(), "id": "gid://shopify/Order/999"},
            order(financial="REFUNDED"),
            order(cancelled=True),
        ):
            with self.assertRaises(entry.ActRefused):
                self.run_execute(live=live)

    def test_needs_the_approve_role_and_the_capability_in_a_listed_channel(self):
        for who, conversation in (
            (user(roles=("orders.use",)), convo()),
            (user("twl:reader"), convo()),
            (user(), convo("C0OTHER")),
            (user("twl:jimmy"), convo("D0JIMMY", "dm")),
        ):
            with self.assertRaises(entry.ActRefused):
                self.run_execute(who=who, conversation=conversation)

    def test_needs_the_button(self):
        for choice in (None, "approve", "approve_edit"):
            with self.assertRaises(entry.ActRefused):
                self.run_execute(choice=choice)

    def test_a_tampered_payload_is_refused(self):
        for change in (
            {"version": 2},
            {"order_id": "123"},
            {"lines": []},
            {"lines": [{**self.proposal["payload"]["lines"][0], "quantity": 5}]},
            {"lines": [{**self.proposal["payload"]["lines"][0], "fulfillment_order_id": "x"}]},
            {"tracking": {"number": "1;2"}},
        ):
            proposal = {**self.proposal, "payload": {**self.proposal["payload"], **change}}
            with self.assertRaises(entry.ActRefused, msg=change):
                self.run_execute(proposal=proposal)

    def test_a_shopify_refusal_is_reported(self):
        with mock.patch.object(fulfil.shop, "find_order_for_fulfilment", return_value=order()), \
             mock.patch.object(fulfil.shop, "create_fulfilment", side_effect=ShopifyError("Shopify would not mark it fulfilled: x")):
            with self.assertRaises(entry.ActRefused):
                entry.execute(user(), convo(), "approve_fulfilment", self.proposal, "req1")

    def test_fulfil_does_not_unlock_order_approvals(self):
        draft = {"kind": "draft_order", "token": "abcdefgh1", "payload": {}}
        with self.assertRaises(entry.ActRefused):
            entry.execute(user(), convo(), "approve_only", draft, "req1")


class ShopifyShapeTests(unittest.TestCase):
    def test_create_never_notifies_and_sends_tracking_only_when_given(self):
        ok = {"fulfillmentCreate": {"fulfillment": {"id": "f1"}, "userErrors": []}}
        with mock.patch.object(fulfil.shop, "graphql", return_value=ok) as call:
            fulfil.shop.create_fulfilment([("fo1", [("l1", 2)])], None)
            fulfil.shop.create_fulfilment([("fo1", [("l1", 2)])], {"number": "123", "company": None})
        first, second = (c.args[1]["fulfillment"] for c in call.call_args_list)
        self.assertIs(first["notifyCustomer"], False)
        self.assertNotIn("trackingInfo", first)
        self.assertEqual(second["trackingInfo"], {"number": "123"})
        self.assertEqual(first["lineItemsByFulfillmentOrder"], [{"fulfillmentOrderId": "fo1", "fulfillmentOrderLineItems": [{"id": "l1", "quantity": 2}]}])

    def test_user_errors_raise(self):
        bad = {"fulfillmentCreate": {"fulfillment": None, "userErrors": [{"message": "nope"}]}}
        with mock.patch.object(fulfil.shop, "graphql", return_value=bad):
            with self.assertRaises(ShopifyError):
                fulfil.shop.create_fulfilment([("fo1", [])], None)

    def test_lookup_keeps_only_lines_with_something_left(self):
        data = {"orders": {"nodes": [{
            "id": ORDER_ID, "name": "#1234", "legacyResourceId": "555", "displayFinancialStatus": "PAID",
            "displayFulfillmentStatus": "PARTIALLY_FULFILLED", "cancelledAt": None,
            "fulfillmentOrders": {"nodes": [{
                "id": "gid://shopify/FulfillmentOrder/1", "status": "IN_PROGRESS", "assignedLocation": {"name": "Artarmon"},
                "supportedActions": [{"action": "CREATE_FULFILLMENT"}],
                "lineItems": {"nodes": [
                    {"id": "gid://shopify/FulfillmentOrderLineItem/1", "remainingQuantity": 0, "lineItem": {"id": "gid://shopify/LineItem/1", "title": "A", "sku": "a"}},
                    {"id": "gid://shopify/FulfillmentOrderLineItem/2", "remainingQuantity": 3, "lineItem": {"id": "gid://shopify/LineItem/2", "title": "B", "sku": "b"}},
                ]},
            }]},
        }]}}
        with mock.patch.object(fulfil.shop, "graphql", return_value=data):
            found = fulfil.shop.find_order_for_fulfilment("1234")
        self.assertEqual([(line["title"], line["remaining"], line["fulfillable"]) for line in found["lines"]], [("B", 3, True)])


if __name__ == "__main__":
    unittest.main()

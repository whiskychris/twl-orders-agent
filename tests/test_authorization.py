"""Tests for who may see what. Nothing here calls Google, Shopify or Anthropic."""

import unittest
from unittest import mock

from orders_agent import authorization
from orders_agent.authorization import (
    AuthorizationError,
    AuthorizationUnavailable,
    resolve_context,
)
from orders_agent.tools import build_server

ALL = ["orders", "products", "inventory", "customers"]

USERS = {
    "twl:chris-ross": {"name": "Chris Ross", "capabilities": ALL},
    "twl:sam-orders": {"name": "Sam", "capabilities": ["orders", "products"]},
    "twl:pat-stock": {"name": "Pat", "capabilities": ["inventory"]},
    "twl:cathy-only": {"name": "Cathy", "capabilities": ["customers"]},
    "twl:nobody": {"name": "Nobody", "capabilities": []},
    "twl:typo": {"name": "Typo", "capabilities": ["orders", "everything", "admin"]},
}


def user(user_id, roles=("orders.use",)):
    return {"id": "U123", "user_id": user_id, "name": "ignored", "roles": list(roles)}


def convo(visibility="dm", source="slack"):
    return {"id": "slack:D1:1", "source": source, "visibility": visibility}


def with_users(users=USERS):
    return mock.patch.object(authorization, "get_authz_config", return_value=users)


class ResolveTests(unittest.TestCase):
    def test_mapped_user_gets_their_capabilities(self):
        with with_users():
            ctx = resolve_context(user("twl:sam-orders"), convo(), "r1")
        self.assertEqual(ctx.capabilities, frozenset({"orders", "products"}))
        self.assertEqual(ctx.name, "Sam")  # the name comes from our config, not from the caller

    def test_unmapped_user_is_denied(self):
        with with_users():
            with self.assertRaises(AuthorizationError):
                resolve_context(user("twl:stranger"), convo(), "r1")

    def test_user_with_no_capabilities_is_denied(self):
        with with_users():
            for who in ("twl:nobody", "twl:typo-missing"):
                with self.assertRaises(AuthorizationError):
                    resolve_context(user(who), convo(), "r1")

    def test_unknown_capability_names_grant_nothing(self):
        with with_users():
            ctx = resolve_context(user("twl:typo"), convo(), "r1")
        self.assertEqual(ctx.capabilities, frozenset({"orders"}))

    def test_missing_identity_or_role_is_denied(self):
        with with_users():
            with self.assertRaises(AuthorizationError):
                resolve_context({"roles": ["orders.use"]}, convo(), "r1")  # no user_id
            with self.assertRaises(AuthorizationError):
                resolve_context(user("twl:chris-ross", roles=["shipments.use"]), convo(), "r1")
            with self.assertRaises(AuthorizationError):
                resolve_context(None, convo(), "r1")

    def test_slack_id_is_not_an_identity(self):
        # Only the trusted user_id from the gateway counts. A Slack id, a name or an email
        # supplied by the caller must not unlock anything.
        with with_users():
            for fake in ("U123", "Chris Ross", "chris@thewhiskylist.com.au", "chris-ross"):
                with self.assertRaises(AuthorizationError):
                    resolve_context({"id": "U123", "user_id": fake, "roles": ["orders.use"]}, convo(), "r1")

    def test_permissions_unreadable_fails_closed(self):
        with mock.patch.object(authorization, "get_authz_config", side_effect=AuthorizationUnavailable("x")):
            with self.assertRaises(AuthorizationUnavailable):
                resolve_context(user("twl:chris-ross"), convo(), "r1")


class CustomerVisibilityTests(unittest.TestCase):
    def test_customers_allowed_in_a_dm(self):
        with with_users():
            ctx = resolve_context(user("twl:chris-ross"), convo("dm"), "r1")
        self.assertTrue(ctx.has("customers"))
        self.assertEqual(ctx.withheld, ())

    def test_customers_withheld_in_a_channel(self):
        with with_users():
            ctx = resolve_context(user("twl:chris-ross"), convo("channel"), "r1")
        self.assertFalse(ctx.has("customers"))
        self.assertEqual(ctx.withheld, ("customers",))
        self.assertTrue(ctx.has("orders"))
        self.assertIn("shared channel", ctx.describe())

    def test_unclear_visibility_is_treated_as_a_channel(self):
        with with_users():
            for visibility in (None, "", "public", 5):
                ctx = resolve_context(user("twl:chris-ross"), {"visibility": visibility}, "r1")
                self.assertFalse(ctx.has("customers"), visibility)
            ctx = resolve_context(user("twl:chris-ross"), None, "r1")
            self.assertFalse(ctx.has("customers"))

    def test_customers_only_user_gets_nothing_in_a_channel(self):
        with with_users():
            with self.assertRaises(AuthorizationError):
                resolve_context(user("twl:cathy-only"), convo("channel"), "r1")
            ctx = resolve_context(user("twl:cathy-only"), convo("dm"), "r1")
        self.assertEqual(ctx.capabilities, frozenset({"customers"}))


class ToolAvailabilityTests(unittest.TestCase):
    def tools_for(self, who, visibility="dm"):
        with with_users():
            ctx = resolve_context(user(who), convo(visibility), "r1")
        _server, names = build_server(ctx)
        return set(names)

    def test_full_access_in_a_dm(self):
        self.assertEqual(
            self.tools_for("twl:chris-ross"),
            {"current_time", "search_orders", "get_order", "summarise_orders",
             "search_products", "get_inventory", "low_stock", "search_customers"},
        )

    def test_no_customer_tool_in_a_channel(self):
        names = self.tools_for("twl:chris-ross", "channel")
        self.assertNotIn("search_customers", names)
        self.assertIn("search_orders", names)

    def test_orders_and_products_only(self):
        self.assertEqual(
            self.tools_for("twl:sam-orders"),
            {"current_time", "search_orders", "get_order", "summarise_orders", "search_products"},
        )

    def test_inventory_only(self):
        self.assertEqual(self.tools_for("twl:pat-stock"), {"current_time", "get_inventory", "low_stock"})

    def test_no_tool_lets_the_model_choose_access(self):
        # Access flags must never be arguments the model can set.
        import inspect
        source = inspect.getsource(build_server)
        # Every schema property the model can fill in is written as `"name": {**TYPE`.
        import re
        properties = set(re.findall(r'"(\w+)": \{\*\*(?:STRING|INTEGER|BOOLEAN)', source))
        self.assertTrue(properties)
        for name in properties:
            self.assertNotIn("customer", name)
            self.assertNotIn("inventory", name)
            self.assertNotIn("capabilit", name)
            self.assertNotIn("include", name)


class EndpointTests(unittest.TestCase):
    """The /v1/message endpoint, with the model replaced by a stub that records the context."""

    def setUp(self):
        import main
        self.main = main
        self.client = main.app.test_client()
        self.seen = []

        async def fake_run_agent(prompt, ctx):
            self.seen.append((prompt, ctx))
            return "stub answer"

        patcher = mock.patch.object(main, "run_agent", fake_run_agent)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, user_obj, conversation=None, text="how many orders yesterday?"):
        return self.client.post(
            "/v1/message",
            json={"conversation_id": "slack:D1:1", "user": user_obj, "conversation": conversation or convo(), "text": text},
        )

    def test_mapped_user_reaches_the_model_with_their_context(self):
        with with_users():
            response = self.post(user("twl:sam-orders"))
        self.assertEqual(response.get_json()["text"], "stub answer")
        prompt, ctx = self.seen[0]
        self.assertEqual(ctx.capabilities, frozenset({"orders", "products"}))
        self.assertIn("Cannot see: inventory, customers", prompt)

    def test_unmapped_user_never_reaches_the_model(self):
        with with_users():
            response = self.post(user("twl:stranger"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.get_json()["text"])
        self.assertEqual(self.seen, [])

    def test_missing_role_is_refused(self):
        with with_users():
            response = self.post(user("twl:chris-ross", roles=["shipments.use"]))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.seen, [])

    def test_unreadable_permissions_fail_closed(self):
        with mock.patch.object(authorization, "get_authz_config", side_effect=AuthorizationUnavailable("x")):
            response = self.post(user("twl:chris-ross"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.seen, [])

    def test_channel_conversation_withholds_customers(self):
        with with_users():
            self.post(user("twl:chris-ross"), conversation=convo("channel"))
        _prompt, ctx = self.seen[0]
        self.assertFalse(ctx.has("customers"))

    def test_the_same_identity_gets_the_same_result_from_any_interface(self):
        # A different front end (source) must not change what a person can see.
        with with_users():
            self.post(user("twl:sam-orders"), conversation=convo("dm", source="slack"))
            self.post(user("twl:sam-orders"), conversation=convo("dm", source="web"))
        first, second = self.seen[0][1], self.seen[1][1]
        self.assertEqual(first.capabilities, second.capabilities)


if __name__ == "__main__":
    unittest.main()

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

    def test_the_model_is_told_where_the_conversation_is(self):
        with with_users():
            dm = resolve_context(user("twl:chris-ross"), convo("dm"), "r1").describe()
            channel = resolve_context(user("twl:chris-ross"), convo("channel"), "r1").describe()
        self.assertIn("This conversation is a direct message.", dm)
        self.assertNotIn("withheld", dm)
        self.assertIn("This conversation is a shared channel.", channel)
        self.assertIn("withheld", channel)

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


class OrderEntryAuthorizationTests(unittest.TestCase):
    USERS = {
        "twl:jimmy": {"name": "Jimmy", "capabilities": ["orders", "customers", "order_entry"]},
        "twl:reader": {"name": "Reader", "capabilities": ["orders"]},
        "twl:entry-only": {"name": "Entry", "capabilities": ["order_entry"]},
    }

    def resolve(self, who, conversation, channels=frozenset({"C0SALES"})):
        with mock.patch.object(authorization, "get_authz_config", return_value=self.USERS), \
             mock.patch.object(authorization, "get_order_entry_channels", return_value=channels):
            return resolve_context(user(who), conversation, "r1")

    def test_order_entry_works_in_a_dm(self):
        ctx = self.resolve("twl:jimmy", {"id": "slack:D1:1.1", "source": "slack", "visibility": "dm"})
        self.assertTrue(ctx.has("order_entry"))
        self.assertTrue(ctx.has("customers"))

    def test_order_entry_and_customers_both_work_in_a_listed_channel(self):
        # order_entry_channels (for example #sales) now gates customer visibility too, the same
        # allowlist reused rather than a second one - see authorization.resolve_context.
        ctx = self.resolve("twl:jimmy", {"id": "slack:C0SALES:1.1", "source": "slack", "visibility": "channel"})
        self.assertTrue(ctx.has("order_entry"))
        self.assertTrue(ctx.has("customers"))
        self.assertEqual(ctx.withheld, ())
        self.assertEqual(ctx.channel_id, "C0SALES")
        self.assertIn("prepare new orders", ctx.describe())

    def test_order_entry_and_customers_are_both_withheld_in_any_other_channel(self):
        for conversation in (
            {"id": "slack:C0OTHER:1.1", "source": "slack", "visibility": "channel"},
            {"id": "garbage", "source": "slack", "visibility": "channel"},
            {"source": "slack", "visibility": "channel"},   # no conversation id at all
        ):
            ctx = self.resolve("twl:jimmy", conversation)
            self.assertFalse(ctx.has("order_entry"), conversation)
            self.assertFalse(ctx.has("customers"), conversation)
            self.assertEqual(set(ctx.withheld), {"order_entry", "customers"}, conversation)
            self.assertIn("not available in this conversation", ctx.describe())

    def test_missing_visibility_is_a_channel_and_the_allowlist_still_applies(self):
        listed = self.resolve("twl:jimmy", {"id": "slack:C0SALES:1.1", "source": "slack"})
        self.assertTrue(listed.has("order_entry"))
        self.assertTrue(listed.has("customers"))       # treated as a channel, but it's on the allowlist
        unlisted = self.resolve("twl:jimmy", {"id": "slack:C0OTHER:1.1", "source": "slack"})
        self.assertFalse(unlisted.has("order_entry"))
        self.assertFalse(unlisted.has("customers"))

    def test_no_allowlist_means_no_channels(self):
        ctx = self.resolve("twl:jimmy", {"id": "slack:C0SALES:1.1", "source": "slack", "visibility": "channel"}, channels=frozenset())
        self.assertFalse(ctx.has("order_entry"))
        self.assertFalse(ctx.has("customers"))

    def test_channel_ids_are_compared_exactly(self):
        for other in ("C0SALES2", "C0SALE", "c0sales-x", "D0SALES"):
            ctx = self.resolve("twl:jimmy", {"id": f"slack:{other}:1.1", "source": "slack", "visibility": "channel"})
            self.assertFalse(ctx.has("order_entry"), other)

    def test_a_user_without_the_capability_never_gets_it_anywhere(self):
        for conversation in (
            {"id": "slack:D1:1.1", "source": "slack", "visibility": "dm"},
            {"id": "slack:C0SALES:1.1", "source": "slack", "visibility": "channel"},
        ):
            self.assertFalse(self.resolve("twl:reader", conversation).has("order_entry"))

    def test_a_user_with_only_order_entry_gets_nothing_in_an_unlisted_channel(self):
        with self.assertRaises(AuthorizationError):
            self.resolve("twl:entry-only", {"id": "slack:C0OTHER:1.1", "source": "slack", "visibility": "channel"})

    def test_an_unreadable_channel_list_fails_closed_only_when_it_matters(self):
        conversation = {"id": "slack:C0SALES:1.1", "source": "slack", "visibility": "channel"}
        with mock.patch.object(authorization, "get_authz_config", return_value=self.USERS), \
             mock.patch.object(authorization, "get_order_entry_channels", side_effect=AuthorizationUnavailable("x")):
            with self.assertRaises(AuthorizationUnavailable):
                resolve_context(user("twl:jimmy"), conversation, "r1")
            # A user who never asked for order entry is unaffected, so reading orders keeps working.
            self.assertTrue(resolve_context(user("twl:reader"), conversation, "r1").has("orders"))

    def test_channel_of(self):
        self.assertEqual(authorization.channel_of({"id": "slack:c0sales:1.2"}), "C0SALES")
        for bad in (None, {}, {"id": ""}, {"id": "slack:C1"}, {"id": "web:C1:2"}):
            self.assertEqual(authorization.channel_of(bad), "")

    def test_the_channel_list_is_read_from_the_secret_and_cleaned(self):
        authorization._cache.update(value=None, channels=None, loaded_at=0.0)
        self.addCleanup(lambda: authorization._cache.update(value=None, channels=None, loaded_at=0.0))
        secret = {"users": {"twl:a": {}}, "order_entry_channels": [" c0sales ", "", "C0OTHER", 5]}
        with mock.patch.object(authorization, "read_secret_json", return_value=secret):
            self.assertEqual(authorization.get_order_entry_channels(), frozenset({"C0SALES", "C0OTHER", "5"}))
        authorization._cache.update(value=None, channels=None, loaded_at=0.0)
        for bad in ({"users": {}}, {"users": {}, "order_entry_channels": "C0SALES"}):
            with mock.patch.object(authorization, "read_secret_json", return_value=bad):
                self.assertEqual(authorization.get_order_entry_channels(), frozenset())
            authorization._cache.update(value=None, channels=None, loaded_at=0.0)


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

    def test_no_customer_tool_in_a_channel_not_on_the_allowlist(self):
        names = self.tools_for("twl:chris-ross", "channel")  # "D1" here, never on order_entry_channels
        self.assertNotIn("search_customers", names)
        self.assertIn("search_orders", names)

    def test_customer_tool_available_in_an_allowlisted_channel(self):
        with with_users(), mock.patch.object(authorization, "get_order_entry_channels", return_value=frozenset({"D1"})):
            ctx = resolve_context(user("twl:chris-ross"), convo("channel"), "r1")
        _server, names = build_server(ctx)
        self.assertIn("search_customers", names)

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
        # Names that would read as an access switch. Lookup ids such as customer_id are fine: they pick
        # WHICH customer, and what may be seen is still decided by the AuthContext, not by the argument.
        forbidden_words = ("include", "capabilit", "permission", "allow", "bypass", "ignore", "override", "admin", "grant", "role")
        forbidden_exact = {"customer", "customers", "inventory", "orders", "products", "order_entry", "approve", "approved", "confirm", "paid"}
        for name in properties:
            self.assertNotIn(name, forbidden_exact)
            for word in forbidden_words:
                self.assertNotIn(word, name)


class EndpointTests(unittest.TestCase):
    """The /v1/message endpoint, with the model replaced by a stub that records the context."""

    def setUp(self):
        import main
        self.main = main
        self.client = main.app.test_client()
        self.seen = []

        async def fake_run_agent(prompt, ctx, state=None):
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
        with with_users():  # "D1" here, never on order_entry_channels
            self.post(user("twl:chris-ross"), conversation=convo("channel"))
        _prompt, ctx = self.seen[0]
        self.assertFalse(ctx.has("customers"))

    def test_an_allowlisted_channel_does_not_withhold_customers(self):
        with with_users(), mock.patch.object(authorization, "get_order_entry_channels", return_value=frozenset({"D1"})):
            self.post(user("twl:chris-ross"), conversation=convo("channel"))
        _prompt, ctx = self.seen[0]
        self.assertTrue(ctx.has("customers"))

    def test_the_same_identity_gets_the_same_result_from_any_interface(self):
        # A different front end (source) must not change what a person can see.
        with with_users():
            self.post(user("twl:sam-orders"), conversation=convo("dm", source="slack"))
            self.post(user("twl:sam-orders"), conversation=convo("dm", source="web"))
        first, second = self.seen[0][1], self.seen[1][1]
        self.assertEqual(first.capabilities, second.capabilities)


if __name__ == "__main__":
    unittest.main()

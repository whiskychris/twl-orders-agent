"""Shared tools: the read-only tools other agents (rewards) call through the gateway, and the new-order
handoff from rewards. Nothing here calls Google, Shopify or Anthropic."""

import unittest
from unittest import mock

from orders_agent import authorization, entry, shared
from orders_agent.authorization import AuthorizationUnavailable, resolve_context
from orders_agent.sources import shopify

ALL = ["orders", "products", "inventory", "customers", "order_entry"]
USERS = {
    "twl:chris-ross": {"name": "Chris Ross", "capabilities": ALL},
    "twl:sam-orders": {"name": "Sam", "capabilities": ["orders", "products"]},
}


def user(user_id, roles=("orders.use",)):
    return {"id": "U123", "user_id": user_id, "name": "ignored", "roles": list(roles)}


def convo(visibility="dm"):
    return {"id": "slack:D1:1" if visibility == "dm" else "slack:C9:1", "source": "slack", "visibility": visibility}


def with_users():
    return mock.patch.object(authorization, "get_authz_config", return_value=USERS)


def context(user_id, visibility="dm"):
    with with_users(), mock.patch.object(authorization, "get_order_entry_channels", return_value=frozenset()):
        return resolve_context(user(user_id), convo(visibility), "r1")


class SharedToolTests(unittest.TestCase):
    def names(self, ctx):
        return {tool["name"] for tool in shared.available(ctx)}

    def test_only_read_tools_are_ever_shared(self):
        names = {tool.name for tool in shared.SHARED_TOOLS}
        self.assertEqual(names, {
            "search_orders", "get_order", "summarise_orders", "search_products", "get_inventory", "low_stock",
            "search_customers", "find_variant", "check_price",
        })
        for name in names:
            self.assertFalse(name.startswith("prepare"), name)

    def test_the_list_follows_the_callers_capabilities(self):
        self.assertEqual(self.names(context("twl:sam-orders")), {"search_orders", "get_order", "summarise_orders", "search_products"})
        self.assertIn("search_customers", self.names(context("twl:chris-ross")))

    def test_customers_and_order_entry_are_withheld_in_a_channel(self):
        names = self.names(context("twl:chris-ross", "channel"))
        self.assertNotIn("search_customers", names)
        self.assertNotIn("find_variant", names)
        self.assertIn("get_order", names)

    def test_running_a_tool_without_its_capability_is_refused_even_if_named(self):
        ctx = context("twl:sam-orders")
        with mock.patch.object(shopify, "search_customers") as search:
            with self.assertRaises(shared.ToolRefused):
                shared.run(ctx, "search_customers", {"query": "tag:'Rewards Member'"})
        search.assert_not_called()

    def test_an_unknown_or_write_tool_name_is_refused(self):
        for name in ("prepare_draft_order", "prepare_order_edit", "nope"):
            with self.assertRaises(shared.ToolRefused):
                shared.run(context("twl:chris-ross"), name, {})

    def test_customer_fields_come_from_the_context_not_the_input(self):
        with mock.patch.object(shopify, "get_order", return_value={"order": "#1"}) as get_order:
            shared.run(context("twl:sam-orders"), "get_order", {"order": "1", "include_customer": True})
        get_order.assert_called_once_with("1", include_customer=False)
        with mock.patch.object(shopify, "get_order", return_value={"order": "#1"}) as get_order:
            shared.run(context("twl:chris-ross"), "get_order", {"order": "1"})
        get_order.assert_called_once_with("1", include_customer=True)

    def test_the_agents_own_model_gets_the_same_definitions(self):
        from orders_agent.tools import build_server
        _server, names = build_server(context("twl:chris-ross"))
        for tool in shared.SHARED_TOOLS:
            self.assertIn(tool.name, names)
        _server, names = build_server(context("twl:sam-orders"))
        self.assertNotIn("search_customers", names)
        self.assertNotIn("find_variant", names)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        import main
        self.client = main.app.test_client()

    def body(self, user_id, visibility="dm", **extra):
        return {"conversation_id": "x", "user": user(user_id), "conversation": convo(visibility),
                "caller_agent_id": "rewards", **extra}

    def test_tools_lists_what_this_person_may_use(self):
        with with_users():
            listed = self.client.post("/v1/tools", json=self.body("twl:sam-orders")).get_json()["tools"]
        self.assertEqual({tool["name"] for tool in listed}, {"search_orders", "get_order", "summarise_orders", "search_products"})
        self.assertTrue(all("input_schema" in tool and tool["description"] for tool in listed))

    def test_a_stranger_gets_no_tools(self):
        with with_users():
            listed = self.client.post("/v1/tools", json=self.body("twl:stranger")).get_json()["tools"]
        self.assertEqual(listed, [])

    def test_tool_runs_and_returns_the_result(self):
        with with_users(), mock.patch.object(shopify, "get_order", return_value={"order": "#1001"}):
            reply = self.client.post("/v1/tool", json=self.body("twl:chris-ross", tool="get_order", input={"order": "1001"})).get_json()
        self.assertEqual(reply, {"result": {"order": "#1001"}})

    def test_tool_errors_come_back_as_errors_not_failures(self):
        with with_users(), mock.patch.object(shopify, "get_order", side_effect=shopify.ShopifyError("No order #9.")):
            response = self.client.post("/v1/tool", json=self.body("twl:chris-ross", tool="get_order", input={"order": "9"}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"error": "No order #9."})
        with with_users():
            reply = self.client.post("/v1/tool", json=self.body("twl:sam-orders", tool="search_customers", input={})).get_json()
        self.assertIn("error", reply)

    def test_unreadable_permissions_fail_closed(self):
        with mock.patch.object(authorization, "get_authz_config", side_effect=AuthorizationUnavailable("x")):
            self.assertEqual(self.client.post("/v1/tools", json=self.body("twl:chris-ross")).status_code, 503)
            self.assertEqual(self.client.post("/v1/tool", json=self.body("twl:chris-ross", tool="get_order")).status_code, 503)


class RewardsHandoffTests(unittest.TestCase):
    """rewards -> orders: an approved 'draft this order for a member' arrives as structured context."""

    def setUp(self):
        import main
        self.client = main.app.test_client()

    def post(self, user_id, context, visibility="dm"):
        with with_users():
            return self.client.post("/v1/message", json={
                "conversation_id": "x", "user": user(user_id), "conversation": convo(visibility),
                "text": "Draft an order for Jane Smith.", "context": context,
            })

    def test_the_draft_is_prepared_from_the_context_and_posted_for_approval(self):
        proposal = {"kind": "draft_order", "items": [{"id": 1, "label": "2 × Arran 10"}], "payload": {}, "choices": []}
        context = {"action": "prepare_draft_order", "customer_id": "gid://shopify/Customer/5",
                   "lines": [{"variant_id": "gid://shopify/ProductVariant/7", "quantity": 2}], "note": "Member order"}
        with mock.patch.object(entry, "prepare", return_value={"proposal": proposal, "text": "Draft for Jane", "warnings": []}) as prepare:
            reply = self.post("twl:chris-ross", context).get_json()
        self.assertEqual(reply, {"text": "Draft for Jane", "proposal": proposal})
        ctx, target, lines, note = prepare.call_args.args
        self.assertEqual(target, {"company_id": None, "location_id": None, "customer_id": "gid://shopify/Customer/5"})
        self.assertEqual(lines, context["lines"])
        self.assertEqual(note, "Member order")
        self.assertEqual(ctx.user_id, "twl:chris-ross")

    def test_a_refusal_is_shown_as_text_and_nothing_is_drafted(self):
        context = {"action": "prepare_draft_order", "customer_id": "gid://shopify/Customer/5", "lines": []}
        reply = self.post("twl:chris-ross", context).get_json()
        self.assertNotIn("proposal", reply)
        self.assertIn("couldn't draft", reply["text"])

    def test_someone_without_order_entry_gets_no_draft(self):
        context = {"action": "prepare_draft_order", "customer_id": "gid://shopify/Customer/5",
                   "lines": [{"variant_id": "gid://shopify/ProductVariant/7", "quantity": 2}]}
        with mock.patch.object(entry, "prepare", wraps=entry.prepare) as prepare:
            reply = self.post("twl:sam-orders", context).get_json()
        self.assertNotIn("proposal", reply)
        self.assertIn("can't raise orders", reply["text"])
        self.assertEqual(prepare.call_count, 1)  # refused inside prepare, before Shopify is touched


if __name__ == "__main__":
    unittest.main()

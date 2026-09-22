"""Order entry: authorization, pricing checks, the approval executor and its safety properties.

Shopify is replaced with fakes. Nothing here touches a network or a secret.
"""

import unittest
from decimal import Decimal
from unittest import mock

from orders_agent import authorization, entry
from orders_agent.authorization import AuthContext
from orders_agent.sources.shopify import ShopifyError
from orders_agent.tools import RequestState, build_server

COMPANY = "gid://shopify/Company/1"
LOCATION = "gid://shopify/CompanyLocation/2"
VARIANT_A = "gid://shopify/ProductVariant/10"
VARIANT_B = "gid://shopify/ProductVariant/11"
TERMS = {"id": "gid://shopify/PaymentTermsTemplate/9", "name": "Due on fulfillment", "type": "FULFILLMENT"}

ALL = ["orders", "products", "inventory", "customers", "order_entry"]
USERS = {
    "twl:jimmy-shore": {"name": "Jimmy Shore", "capabilities": ALL},
    "twl:reader": {"name": "Reader", "capabilities": ["orders", "products"]},
}


def ctx(capabilities=("orders", "order_entry"), visibility="channel", channel="C0SALES"):
    return AuthContext(
        request_id="r1", user_id="twl:jimmy-shore", name="Jimmy Shore", source="slack", visibility=visibility,
        capabilities=frozenset(capabilities), channel_id=channel,
    )


def location(**over):
    base = {
        "kind": "company", "location_id": LOCATION, "location_name": "Nicks Wine Merchants", "company_id": COMPANY,
        "company_name": "Nicks Wine Merchants", "contact_id": "gid://shopify/CompanyContact/3",
        "shipping": {"address1": "1 Test St", "city": "Sydney"}, "billing": {"address1": "1 Test St"}, "terms": TERMS,
    }
    return {**base, **over}


def priced(lines, subtotal="100.00", tax="10.00", total="110.00", discount_amount=None):
    return {
        "currency": "AUD", "subtotal": Decimal(subtotal), "tax": Decimal(tax), "total": Decimal(total),
        "discounts": Decimal("0"),
        "lines": [
            {
                "variant_id": line["variant_id"], "title": f"Product {n}", "sku": f"SKU{n}", "quantity": line["quantity"],
                "unit_price": Decimal("50.00"), "line_total": Decimal("100.00"),
                "discount_amount": discount_amount if line.get("discount") else None,
            }
            for n, line in enumerate(lines, 1)
        ],
    }


def raw(*pairs):
    return [{"variant_id": variant, "quantity": qty} for variant, qty in pairs]


class LineTests(unittest.TestCase):
    def test_valid_lines(self):
        lines = entry.clean_lines([{"variant_id": VARIANT_A, "quantity": "6", "discount_type": "percent", "discount_value": "10"}])
        self.assertEqual(lines[0]["quantity"], 6)
        self.assertEqual(lines[0]["discount"], {"type": "percent", "value": "10.00"})

    def test_bad_lines_are_refused_with_a_fixable_message(self):
        for broken in (
            [], "x", None,
            [{"variant_id": "nope", "quantity": 1}],
            [{"variant_id": VARIANT_A, "quantity": 0}],
            [{"variant_id": VARIANT_A, "quantity": 100000}],
            [{"variant_id": VARIANT_A, "quantity": "many"}],
            [{"variant_id": VARIANT_A, "quantity": 1}, {"variant_id": VARIANT_A, "quantity": 2}],   # duplicate
            [{"variant_id": VARIANT_A, "quantity": 1, "discount_type": "percent"}],                # no value
            [{"variant_id": VARIANT_A, "quantity": 1, "discount_type": "percent", "discount_value": 150}],
            [{"variant_id": VARIANT_A, "quantity": 1, "discount_type": "per_unit", "discount_value": -5}],
            [{"variant_id": VARIANT_A, "quantity": 1, "discount_type": "free", "discount_value": 5}],
            [{"variant_id": VARIANT_A, "quantity": 1, "discount_type": "per_unit", "discount_value": "NaN"}],
            [{"variant_id": f"gid://shopify/ProductVariant/{n}", "quantity": 1} for n in range(31)],
        ):
            with self.assertRaises(entry.EntryError, msg=str(broken)[:60]):
                entry.clean_lines(broken)

    def test_fixed_discounts_are_sent_per_unit_because_shopify_always_multiplies_by_quantity(self):
        # Found live: sending the already-multiplied total for per_unit made Shopify multiply it by
        # quantity AGAIN. Shopify's line-item FIXED_AMOUNT value is always per unit - see _discount_input.
        per_unit = entry.clean_lines([{"variant_id": VARIANT_A, "quantity": 6, "discount_type": "per_unit", "discount_value": 5}])[0]
        self.assertEqual(entry._discount_input(per_unit), {"value": 5.0, "valueType": "FIXED_AMOUNT", "title": "Sales discount"})
        line_total = entry.clean_lines([{"variant_id": VARIANT_A, "quantity": 6, "discount_type": "line_total", "discount_value": 12.5}])[0]
        self.assertAlmostEqual(entry._discount_input(line_total)["value"], 12.5 / 6)
        pct = entry.clean_lines([{"variant_id": VARIANT_A, "quantity": 6, "discount_type": "percent", "discount_value": 7.5}])[0]
        self.assertEqual(entry._discount_input(pct)["valueType"], "PERCENTAGE")

    def test_what_shopify_would_actually_apply_matches_what_we_expect(self):
        # The real regression: multiply _discount_input's sent value back by quantity, the way Shopify's
        # own FIXED_AMOUNT semantics do, and it must land on _expected_discount's number exactly.
        for kind, raw_value, quantity in (("per_unit", 7.70, 6), ("line_total", 46.20, 6), ("line_total", 50, 13)):
            line = entry.clean_lines(
                [{"variant_id": VARIANT_A, "quantity": quantity, "discount_type": kind, "discount_value": raw_value}]
            )[0]
            sent = entry._discount_input(line)["value"]
            shopify_would_apply = Decimal(str(sent)) * quantity
            expected = entry._expected_discount(line, Decimal("999"))  # unit_price unused for fixed types
            self.assertLessEqual(abs(shopify_would_apply - expected), entry.TOLERANCE, (kind, raw_value, quantity))


class PricingCheckTests(unittest.TestCase):
    def test_a_discount_shopify_applied_differently_stops_everything(self):
        lines = entry.clean_lines([{"variant_id": VARIANT_A, "quantity": 2, "discount_type": "per_unit", "discount_value": 5}])
        entry.check_pricing(lines, priced(lines, discount_amount=Decimal("10.00")))      # 5 x 2 = 10: fine
        entry.check_pricing(lines, priced(lines, discount_amount=Decimal("10.01")))      # within a cent
        for actual in (Decimal("5.00"), Decimal("20.00"), None):
            with self.assertRaises(entry.EntryError, msg=str(actual)):
                entry.check_pricing(lines, priced(lines, discount_amount=actual))

    def test_percent_discount_is_checked_against_the_unit_price(self):
        lines = entry.clean_lines([{"variant_id": VARIANT_A, "quantity": 2, "discount_type": "percent", "discount_value": 10}])
        entry.check_pricing(lines, priced(lines, discount_amount=Decimal("10.00")))   # 10% of 50 x 2
        with self.assertRaises(entry.EntryError):
            entry.check_pricing(lines, priced(lines, discount_amount=Decimal("5.00")))

    def test_shopify_pricing_a_different_order_stops_everything(self):
        lines = entry.clean_lines(raw((VARIANT_A, 1), (VARIANT_B, 1)))
        good = priced(lines)
        entry.check_pricing(lines, good)
        for broken in (
            {**good, "lines": good["lines"][:1]},
            {**good, "lines": [{**good["lines"][0], "variant_id": VARIANT_B}, good["lines"][1]]},
            {**good, "lines": [{**good["lines"][0], "quantity": 9}, good["lines"][1]]},
            {**good, "lines": [{**good["lines"][0], "unit_price": None}, good["lines"][1]]},
        ):
            with self.assertRaises(entry.EntryError):
                entry.check_pricing(lines, broken)


def make_prepared():
    """A real prepared proposal (as prepare() builds it), with Shopify faked."""
    def fake_calc(draft_input):
        sent = [
            {"variant_id": item["variantId"], "quantity": item["quantity"], "discount": item.get("appliedDiscount")}
            for item in draft_input["lineItems"]
        ]
        return priced(sent)

    with mock.patch.object(entry.shop, "get_location", return_value=location()), \
         mock.patch.object(entry.shop, "calculate", side_effect=fake_calc), \
         mock.patch.object(entry.shop, "get_variants", return_value={VARIANT_A: {"status": "ACTIVE", "sku": "S", "name": "n", "stock": None}}), \
         mock.patch.object(entry.shop, "default_unpaid_terms", return_value=TERMS):
        return entry.prepare(ctx(), {"company_id": COMPANY, "location_id": LOCATION}, raw((VARIANT_A, 6)), "PO 123")["proposal"]


class PrepareTests(unittest.TestCase):
    def prepare(self, capabilities=("orders", "order_entry"), lines=None, loc=None, variants=None, company=COMPANY):
        lines = lines if lines is not None else raw((VARIANT_A, 6))

        def fake_calc(draft_input):
            self.last_input = draft_input
            sent = [
                {"variant_id": item["variantId"], "quantity": item["quantity"], "discount": item.get("appliedDiscount")}
                for item in draft_input["lineItems"]
            ]
            return priced(sent)

        variants = variants if variants is not None else {VARIANT_A: {"status": "ACTIVE", "sku": "S", "name": "n", "stock": None}}
        with mock.patch.object(entry.shop, "get_location", return_value=loc or location()), \
             mock.patch.object(entry.shop, "calculate", side_effect=fake_calc), \
             mock.patch.object(entry.shop, "get_variants", return_value=variants), \
             mock.patch.object(entry.shop, "default_unpaid_terms", return_value=TERMS):
            return entry.prepare(ctx(capabilities), {"company_id": company, "location_id": LOCATION}, lines, "PO 123")

    def test_a_good_draft_becomes_a_proposal_with_invoice_choices(self):
        result = self.prepare()
        proposal = result["proposal"]
        self.assertEqual(proposal["kind"], "draft_order")
        self.assertEqual([c["id"] for c in proposal["choices"]], ["approve_send_invoice", "approve_only"])
        self.assertEqual(proposal["payload"]["expected"]["total"], "110.00")
        self.assertEqual(proposal["payload"]["requested_by"]["user_id"], "twl:jimmy-shore")
        self.assertIn("Nicks Wine Merchants", result["text"])
        self.assertIn("110.00 AUD", result["text"])

    def test_the_draft_is_priced_for_the_company_and_carries_no_email(self):
        self.prepare()
        entity = self.last_input["purchasingEntity"]["purchasingCompany"]
        self.assertEqual(entity["companyId"], COMPANY)
        self.assertEqual(entity["companyLocationId"], LOCATION)
        self.assertNotIn("email", self.last_input)  # no email field, so no customer notification
        self.assertEqual(self.last_input["note"], "PO 123")  # only what the user typed, nothing added

    def test_the_text_never_contains_contact_details_or_addresses(self):
        import re
        text = self.prepare()["text"]
        for private in ("1 Test St", "Sydney", "CompanyContact", "gid://"):
            self.assertNotIn(private, text)
        self.assertIsNone(re.search(r"\S+@\S+\.\S+", text))   # no email address

    def test_without_the_capability_nothing_is_priced(self):
        with mock.patch.object(entry.shop, "get_location") as get_location:
            with self.assertRaises(entry.EntryError):
                entry.prepare(ctx(("orders",)), {"company_id": COMPANY, "location_id": LOCATION}, raw((VARIANT_A, 1)))
        get_location.assert_not_called()

    def test_refusals(self):
        with self.assertRaises(entry.EntryError):
            self.prepare(company="gid://shopify/Company/999")                       # location belongs to another company
        with self.assertRaises(entry.EntryError):
            self.prepare(loc=location(contact_id=None))                             # nobody to order as
        with self.assertRaises(entry.EntryError):
            self.prepare(variants={VARIANT_A: {"status": "ARCHIVED", "sku": "S", "name": "n", "stock": None}})
        with self.assertRaises(entry.EntryError):
            self.prepare(variants={})

    def test_no_delivery_address_is_a_warning_not_a_refusal(self):
        result = self.prepare(loc=location(shipping=None, billing=None))
        self.assertIn("no delivery address", result["text"])
        self.assertNotIn("shippingAddress", self.last_input)

    def test_low_stock_is_a_warning_not_a_refusal(self):
        result = self.prepare(
            capabilities=("orders", "order_entry", "inventory"),
            variants={VARIANT_A: {"status": "ACTIVE", "sku": "S", "name": "n", "stock": 2}},
        )
        self.assertIn("only 2 in stock", result["text"])

    def test_stock_counts_are_not_disclosed_without_the_inventory_capability(self):
        result = self.prepare(variants={VARIANT_A: {"status": "ACTIVE", "sku": "S", "name": "n", "stock": 2}})
        self.assertIn("there may not be enough stock for 6", result["text"])
        self.assertNotIn("only 2", result["text"])
        self.assertNotIn(" 2 ", result["text"].split("⚠️")[1].split("\n")[0])

    def test_an_out_of_stock_line_is_tagged_on_the_row_for_everyone(self):
        for capabilities in (("orders", "order_entry"), ("orders", "order_entry", "inventory")):
            result = self.prepare(capabilities, variants={VARIANT_A: {"status": "ACTIVE", "sku": "S", "name": "n", "stock": 0}})
            self.assertIn("[Back Order]", result["text"], capabilities)
            self.assertEqual(result["warnings"], [])   # it's on the row, not a separate warning

    def test_an_oversold_line_counts_as_out_of_stock(self):
        result = self.prepare(variants={VARIANT_A: {"status": "ACTIVE", "sku": "S", "name": "n", "stock": -19}})
        self.assertIn("[Back Order]", result["text"])
        self.assertNotIn("-19", result["text"])

    def test_a_pre_order_line_shows_its_eta(self):
        result = self.prepare(variants={VARIANT_A: {"status": "ACTIVE", "sku": "S", "name": "n", "stock": 82, "pre_order": True, "eta": "2026-10-16"}})
        self.assertIn("[Pre-order (ETA 16 Oct 2026)]", result["text"])

    def test_a_pre_order_with_no_eta_says_so(self):
        result = self.prepare(variants={VARIANT_A: {"status": "ACTIVE", "sku": "S", "name": "n", "stock": 82, "pre_order": True, "eta": None}})
        self.assertIn("[Pre-order (no ETA set)]", result["text"])

    def test_a_pre_order_out_of_stock_is_tagged_pre_order_not_back_order(self):
        result = self.prepare(variants={VARIANT_A: {"status": "ACTIVE", "sku": "S", "name": "n", "stock": 0, "pre_order": True, "eta": None}})
        self.assertIn("[Pre-order (no ETA set)]", result["text"])
        self.assertNotIn("Back Order", result["text"])

    def test_plenty_of_stock_and_no_pre_order_means_no_warning(self):
        result = self.prepare(variants={VARIANT_A: {"status": "ACTIVE", "sku": "S", "name": "n", "stock": 500}})
        self.assertEqual(result["warnings"], [])
        self.assertNotIn("⚠️", result["text"])

    def test_flags_never_block_the_draft(self):
        result = self.prepare(variants={VARIANT_A: {"status": "ACTIVE", "sku": "S", "name": "n", "stock": 0, "pre_order": True, "eta": "2026-10-16"}})
        self.assertEqual(result["proposal"]["kind"], "draft_order")
        self.assertEqual(result["warnings"], [])

    def test_unpaid_terms_are_always_the_default_never_the_customers_own(self):
        # The customer's own terms (Net 30, a fixed due date, or anything else) are never used: order entry
        # always stamps unpaid drafts with TWL's own "Due on fulfilment", since Shopify's terms field isn't
        # otherwise used and this sidesteps terms types order entry can't support (net needs an issue date,
        # fixed needs a due date it doesn't have).
        for terms in (
            {"id": "gid://shopify/PaymentTermsTemplate/4", "name": "Net 30", "type": "NET"},
            {"id": "gid://shopify/PaymentTermsTemplate/7", "name": "Due 30 June", "type": "FIXED"},
            None,
        ):
            self.assertEqual(self.prepare(loc=location(terms=terms))["proposal"]["payload"]["terms"], TERMS, terms)


class ExecuteTests(unittest.TestCase):
    """The approval executor. This is the only code that writes to Shopify."""

    APPROVER = {"id": "U1", "user_id": "twl:jimmy-shore", "name": "Jimmy Shore", "roles": ["orders.use", "orders.approve"]}
    CONVERSATION = {"id": "slack:C0SALES:1.1", "source": "slack", "visibility": "channel"}

    def setUp(self):
        self.created = []
        self.completed = []
        prepared = make_prepared()
        self.proposal = {
            "token": "orders-20260921-1500-ab12-v1",
            "kind": prepared["kind"],
            "items": prepared["items"],
            "payload": prepared["payload"],
        }
        self.patches = [
            mock.patch.object(authorization, "get_authz_config", return_value=USERS),
            mock.patch.object(authorization, "get_order_entry_channels", return_value=frozenset({"C0SALES"})),
            mock.patch.object(entry.shop, "find_draft_by_tag", return_value=None),
            mock.patch.object(entry.shop, "get_location", return_value=location()),
            mock.patch.object(entry.shop, "calculate", side_effect=lambda i: priced([
                {"variant_id": item["variantId"], "quantity": item["quantity"], "discount": item.get("appliedDiscount")} for item in i["lineItems"]
            ])),
            mock.patch.object(entry.shop, "create_draft", side_effect=self.fake_create),
            mock.patch.object(entry.shop, "complete_draft", side_effect=self.fake_complete),
            mock.patch.object(entry, "admin_order_url", side_effect=lambda legacy: f"https://admin.example/orders/{legacy}"),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def fake_create(self, draft_input):
        self.created.append(draft_input)
        return {"id": "gid://shopify/DraftOrder/5", "name": "#D5", "status": "OPEN"}

    def fake_complete(self, draft_id):
        self.completed.append(draft_id)
        return {"id": "gid://shopify/Order/9", "name": "#1234", "legacyResourceId": "9", "displayFinancialStatus": "PAID"}

    def run_execute(self, choice="approve_only", user=None, conversation=None, proposal=None):
        return entry.execute(user or self.APPROVER, conversation or self.CONVERSATION, choice, proposal or self.proposal, "req1")

    def test_the_order_is_always_created_unpaid_with_payment_terms(self):
        # Both choices create the order the same way - the only difference is whether invoicing starts.
        for choice in ("approve_send_invoice", "approve_only"):
            self.created.clear()
            self.completed.clear()
            result = self.run_execute(choice)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(self.created[0]["paymentTerms"], {"paymentTermsTemplateId": TERMS["id"]}, choice)
            self.assertEqual(self.completed, ["gid://shopify/DraftOrder/5"], choice)

    def test_approve_only_reports_created_but_not_invoiced_and_has_no_handoff(self):
        result = self.run_execute("approve_only")
        self.assertEqual(result["text"], "Success: <https://admin.example/orders/9|Order #1234> created (not yet invoiced)")
        self.assertFalse(result["result"]["invoicing"])
        self.assertNotIn("handoff", result)

    def test_approve_send_invoice_hands_off_to_invoicing(self):
        result = self.run_execute("approve_send_invoice")
        self.assertEqual(result["text"], "Success: <https://admin.example/orders/9|Order #1234> created")
        self.assertTrue(result["result"]["invoicing"])
        self.assertEqual(
            result["handoff"],
            {
                "agent_id": "invoicing", "text": "Prepare a Xero invoice for Shopify order #1234.",
                "context": {"action": "prepare_invoice", "order_id": "gid://shopify/Order/9", "order_name": "#1234"},
            },
        )

    def test_net_terms_send_an_issue_date(self):
        # Net terms are due a number of days after issue, so Shopify needs an issue date to count from
        # (without it: "An issue date is required with net payment terms").
        self.proposal["payload"]["terms"] = {"id": "gid://shopify/PaymentTermsTemplate/4", "name": "Net 30", "type": "NET"}
        self.run_execute("approve_only")
        terms = self.created[0]["paymentTerms"]
        self.assertEqual(terms["paymentTermsTemplateId"], "gid://shopify/PaymentTermsTemplate/4")
        self.assertEqual(len(terms["paymentSchedules"]), 1)
        self.assertIn("issuedAt", terms["paymentSchedules"][0])

    def test_the_draft_is_tagged_and_the_note_is_only_what_the_user_typed(self):
        # make_prepared() supplies "PO 123" as the user's note. Nothing else - no "Raised in Slack by...",
        # no "approved by...", no paid/unpaid text - is added.
        self.run_execute("approve_only")
        draft = self.created[0]
        self.assertIn("smith-orders-20260921-1500-ab12-v1", draft["tags"])
        self.assertEqual(draft["note"], "PO 123")
        self.assertNotIn("email", draft)

    def test_no_note_typed_means_no_note_at_all(self):
        self.proposal["payload"]["note"] = None
        self.run_execute("approve_only")
        self.assertEqual(self.created[0]["note"], "")

    def test_no_approve_role_writes_nothing(self):
        user = {**self.APPROVER, "roles": ["orders.use"]}
        with self.assertRaises(entry.ActRefused):
            self.run_execute(user=user)
        self.assertEqual(self.created, [])
        self.assertEqual(self.completed, [])

    def test_no_order_entry_capability_writes_nothing(self):
        user = {**self.APPROVER, "user_id": "twl:reader"}
        with self.assertRaises(entry.ActRefused):
            self.run_execute(user=user)
        self.assertEqual(self.created, [])

    def test_an_unlisted_channel_writes_nothing(self):
        with self.assertRaises(entry.ActRefused):
            self.run_execute(conversation={"id": "slack:C0RANDOM:1.1", "source": "slack", "visibility": "channel"})
        self.assertEqual(self.created, [])

    def test_a_dm_is_allowed_without_a_channel_entry(self):
        result = self.run_execute(conversation={"id": "slack:D0JIMMY:1.1", "source": "slack", "visibility": "dm"})
        self.assertEqual(result["status"], "ok")

    def test_an_unknown_choice_writes_nothing(self):
        for choice in (None, "", "approve", "create", "approve_only; drop"):
            with self.assertRaises(entry.ActRefused, msg=str(choice)):
                self.run_execute(choice)
        self.assertEqual(self.created, [])

    def test_a_changed_price_writes_nothing(self):
        self.proposal["payload"]["expected"]["total"] = "99.00"
        with self.assertRaises(entry.ActRefused) as caught:
            self.run_execute()
        self.assertIn("price changed", str(caught.exception))
        self.assertEqual(self.created, [])

    def test_a_malformed_or_tampered_payload_writes_nothing(self):
        for mutate in (
            lambda p: p["target"].update(company_id="not-a-gid"),
            lambda p: p.update(target={"kind": "customer", "customer_id": "gid://shopify/Customer/1", "company_id": COMPANY}),   # both forms
            lambda p: p.update(target={"kind": "customer", "customer_id": "nope"}),
            lambda p: p.update(target={}),
            lambda p: p.pop("display"),
            lambda p: p.update(lines=[]),
            lambda p: p["lines"][0].update(quantity=0),
            lambda p: p["lines"][0].update(variant_id="gid://shopify/Product/1"),
            lambda p: p["terms"].update(id="gid://shopify/Something/1"),
            lambda p: p["terms"].update(type="NONSENSE"),
            lambda p: p.pop("expected"),
            lambda p: p.update(version=1),
        ):
            proposal = {**self.proposal, "payload": __import__("copy").deepcopy(self.proposal["payload"])}
            mutate(proposal["payload"])
            with self.assertRaises(entry.ActRefused):
                self.run_execute(proposal=proposal)
        self.assertEqual(self.created, [])

    def test_only_order_drafts_with_a_valid_token_are_executed(self):
        for proposal in ({**self.proposal, "kind": "shipment_change_set"}, {**self.proposal, "token": "BAD TOKEN"}, {**self.proposal, "token": ""}, None, "text", []):
            with self.assertRaises(entry.ActRefused, msg=str(proposal)[:40]):
                entry.execute(self.APPROVER, self.CONVERSATION, "approve_only", proposal, "req1")
        self.assertEqual(self.created, [])

    def test_a_repeated_approval_returns_the_existing_order(self):
        existing = {"id": "gid://shopify/DraftOrder/5", "status": "COMPLETED",
                    "order": {"id": "gid://shopify/Order/9", "name": "#1234", "legacyResourceId": "9"}}
        with mock.patch.object(entry.shop, "find_draft_by_tag", return_value=existing):
            result = self.run_execute()
        self.assertIn("Already done", result["text"])
        self.assertEqual(self.created, [])
        self.assertEqual(self.completed, [])

    def test_an_uncompleted_draft_from_a_failed_attempt_is_reused_not_duplicated(self):
        existing = {"id": "gid://shopify/DraftOrder/5", "status": "OPEN", "order": None}
        with mock.patch.object(entry.shop, "find_draft_by_tag", return_value=existing):
            result = self.run_execute()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.created, [])
        self.assertEqual(self.completed, ["gid://shopify/DraftOrder/5"])

    def test_shopify_refusing_is_a_clean_refusal(self):
        with mock.patch.object(entry.shop, "create_draft", side_effect=ShopifyError("Shopify would not create the draft order: bad")):
            with self.assertRaises(entry.ActRefused):
                self.run_execute()
        self.assertEqual(self.completed, [])

    def test_an_unexpected_failure_is_not_disguised_as_a_refusal(self):
        # main.py turns this into HTTP 500, which the gateway reports as "outcome unknown".
        with mock.patch.object(entry.shop, "complete_draft", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.run_execute()


class MarkOrderPaidTests(unittest.TestCase):
    """The deterministic handoff-back from invoicing: mark an existing Shopify order paid."""

    APPROVER = {"id": "U1", "user_id": "twl:jimmy-shore", "name": "Jimmy Shore", "roles": ["orders.use", "orders.approve"]}
    CONVERSATION = {"id": "slack:C0SALES:1.1", "source": "slack", "visibility": "channel"}
    ORDER_ID = "gid://shopify/Order/9"

    def setUp(self):
        for patch in (
            mock.patch.object(authorization, "get_authz_config", return_value=USERS),
            mock.patch.object(authorization, "get_order_entry_channels", return_value=frozenset({"C0SALES"})),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def run_mark(self, order_id=None, order_name="#1234", user=None, conversation=None):
        with mock.patch.object(
            entry.shop, "mark_paid",
            return_value={"id": order_id or self.ORDER_ID, "name": "#1234", "legacyResourceId": "9", "displayFinancialStatus": "PAID"},
        ) as mark_paid:
            result = entry.mark_order_paid(
                user or self.APPROVER, conversation or self.CONVERSATION, order_id or self.ORDER_ID, order_name, "req1",
            )
        return result, mark_paid

    def test_marks_the_order_paid(self):
        result, mark_paid = self.run_mark()
        self.assertIn("#1234", result["text"])
        self.assertIn("paid", result["text"])
        mark_paid.assert_called_once_with(self.ORDER_ID)

    def test_no_approve_role_refuses(self):
        user = {**self.APPROVER, "roles": ["orders.use"]}
        with mock.patch.object(entry.shop, "mark_paid") as mark_paid:
            with self.assertRaises(entry.ActRefused):
                entry.mark_order_paid(user, self.CONVERSATION, self.ORDER_ID, "#1234", "req1")
        mark_paid.assert_not_called()

    def test_no_order_entry_capability_refuses(self):
        user = {**self.APPROVER, "user_id": "twl:reader"}
        with mock.patch.object(entry.shop, "mark_paid") as mark_paid:
            with self.assertRaises(entry.ActRefused):
                entry.mark_order_paid(user, self.CONVERSATION, self.ORDER_ID, "#1234", "req1")
        mark_paid.assert_not_called()

    def test_a_malformed_order_id_is_refused_before_calling_shopify(self):
        for bad in (None, "", "1234", "#1234", "gid://shopify/DraftOrder/9", "gid://shopify/Order/abc"):
            with mock.patch.object(entry.shop, "mark_paid") as mark_paid:
                with self.assertRaises(entry.ActRefused, msg=str(bad)):
                    entry.mark_order_paid(self.APPROVER, self.CONVERSATION, bad, "#1234", "req1")
            mark_paid.assert_not_called()

    def test_shopify_refusing_points_at_marking_it_paid_by_hand(self):
        # Found live: orderMarkAsPaid needs a Shopify staff permission this app doesn't have yet.
        # The raw API error isn't actionable on its own, so this should name the order and link
        # straight to it in the Shopify admin, not just relay the API's error text.
        with mock.patch.object(
            entry.shop, "mark_paid",
            side_effect=ShopifyError("Access denied for orderMarkAsPaid field. Required access: write_orders access scope."),
        ), mock.patch.object(entry, "admin_order_url", return_value="https://admin.example/orders/9"):
            with self.assertRaises(entry.ActRefused) as caught:
                entry.mark_order_paid(self.APPROVER, self.CONVERSATION, self.ORDER_ID, "#1234", "req1")
        message = str(caught.exception)
        self.assertIn("#1234", message)
        self.assertIn("https://admin.example/orders/9", message)
        self.assertIn("Access denied", message)

    def test_an_unlisted_channel_refuses(self):
        with mock.patch.object(entry.shop, "mark_paid") as mark_paid:
            with self.assertRaises(entry.ActRefused):
                entry.mark_order_paid(
                    self.APPROVER, {"id": "slack:C0RANDOM:1.1", "source": "slack", "visibility": "channel"},
                    self.ORDER_ID, "#1234", "req1",
                )
        mark_paid.assert_not_called()


ORDER_EDIT_ID = "gid://shopify/Order/9"
LINE_ITEM_A = "gid://shopify/LineItem/1"
MONEY_NODE = lambda amount, currency="AUD": {"shopMoney": {"amount": amount, "currencyCode": currency}}  # noqa: E731


def edit_order(**over):
    base = {
        "id": ORDER_EDIT_ID, "name": "#1234", "financial_status": "PENDING",
        "lines": [{"line_item_id": LINE_ITEM_A, "title": "Arran 10", "quantity": 6, "variant_id": VARIANT_A}],
    }
    return {**base, **over}


def calc_snapshot(line_items=None, added=None, total="782.10"):
    return {
        "totalPriceSet": MONEY_NODE(total),
        "lineItems": {"nodes": line_items or []},
        "addedLineItems": {"nodes": added or []},
    }


def calc_line(variant_id, title, quantity, unit_price="86.90", line_total=None):
    line_total = line_total or str(round(float(unit_price) * quantity, 2))
    return {
        "id": f"gid://shopify/LineItem/{variant_id[-1]}", "title": title, "quantity": quantity,
        "variant": {"id": variant_id}, "discountedUnitPriceSet": MONEY_NODE(unit_price), "editableSubtotalSet": MONEY_NODE(line_total),
    }


class PrepareOrderEditTests(unittest.TestCase):
    def prepare(self, order=None, changes=None, calculated_order_id="calc-1", snap=None):
        changes = changes if changes is not None else [{"variant_id": VARIANT_A, "quantity": 12}]
        snap = snap if snap is not None else calc_snapshot(
            line_items=[calc_line(VARIANT_A, "Arran 10", 12, line_total="1042.80")],
        )
        with mock.patch.object(entry.order_editing, "find_order_for_edit", return_value=order or edit_order()), \
             mock.patch.object(entry.order_editing, "begin_edit", return_value=calculated_order_id) as begin, \
             mock.patch.object(entry.order_editing, "set_quantity") as set_qty, \
             mock.patch.object(entry.order_editing, "add_variant") as add_var, \
             mock.patch.object(entry.order_editing, "snapshot", return_value=snap):
            result = entry.prepare_order_edit(ctx(), "#1234", changes)
        return result, begin, set_qty, add_var

    def test_a_quantity_change_on_an_existing_line_becomes_a_proposal(self):
        result, begin, set_qty, add_var = self.prepare()
        proposal = result["proposal"]
        self.assertEqual(proposal["kind"], "order_edit")
        self.assertEqual([c["id"] for c in proposal["choices"]], ["approve_edit"])
        self.assertEqual(proposal["payload"]["order_id"], ORDER_EDIT_ID)
        self.assertEqual(proposal["payload"]["changes"], [{"variant_id": VARIANT_A, "quantity": 12}])
        self.assertEqual(proposal["payload"]["expected_total"], "782.10")
        set_qty.assert_called_once_with("calc-1", LINE_ITEM_A, 12)
        add_var.assert_not_called()
        self.assertIn("Arran 10: 6 → 12", result["text"])
        self.assertIn("New total 782.10 AUD", result["text"])

    def test_adding_a_new_variant_calls_add_variant_not_set_quantity(self):
        new_variant = "gid://shopify/ProductVariant/99"
        snap = calc_snapshot(
            line_items=[calc_line(VARIANT_A, "Arran 10", 6, line_total="521.40")],
            added=[calc_line(new_variant, "GlenAllachie 12", 3, unit_price="70.00", line_total="210.00")],
            total="731.40",
        )
        result, begin, set_qty, add_var = self.prepare(changes=[{"variant_id": new_variant, "quantity": 3}], snap=snap)
        set_qty.assert_not_called()
        add_var.assert_called_once_with("calc-1", new_variant, 3)
        self.assertIn("Add 3 × GlenAllachie 12", result["text"])

    def test_setting_quantity_to_zero_reads_as_a_removal(self):
        snap = calc_snapshot(line_items=[calc_line(VARIANT_A, "Arran 10", 0, line_total="0.00")], total="0.00")
        result, *_ = self.prepare(changes=[{"variant_id": VARIANT_A, "quantity": 0}], snap=snap)
        self.assertIn("Remove Arran 10 (was 6)", result["text"])

    def test_removing_a_variant_not_on_the_order_refuses(self):
        with self.assertRaises(entry.EntryError):
            self.prepare(changes=[{"variant_id": "gid://shopify/ProductVariant/404", "quantity": 0}])

    def test_a_paid_order_refuses_before_any_edit_call(self):
        with mock.patch.object(entry.order_editing, "find_order_for_edit", return_value=edit_order(financial_status="PAID")), \
             mock.patch.object(entry.order_editing, "begin_edit") as begin:
            with self.assertRaises(entry.EntryError):
                entry.prepare_order_edit(ctx(), "#1234", [{"variant_id": VARIANT_A, "quantity": 12}])
        begin.assert_not_called()

    def test_no_actual_change_refuses(self):
        snap = calc_snapshot(line_items=[calc_line(VARIANT_A, "Arran 10", 6, line_total="521.40")], total="521.40")
        with self.assertRaises(entry.EntryError):
            self.prepare(changes=[{"variant_id": VARIANT_A, "quantity": 6}], snap=snap)

    def test_no_capability_refuses(self):
        with self.assertRaises(entry.EntryError):
            entry.prepare_order_edit(ctx(capabilities=("orders",)), "#1234", [{"variant_id": VARIANT_A, "quantity": 12}])

    def test_malformed_changes_are_refused(self):
        for bad in ([], None, [{"variant_id": "not-a-gid", "quantity": 1}], [{"variant_id": VARIANT_A, "quantity": -1}], [{"variant_id": VARIANT_A, "quantity": "many"}]):
            with mock.patch.object(entry.order_editing, "find_order_for_edit") as find_order:
                with self.assertRaises(entry.EntryError, msg=repr(bad)):
                    entry.prepare_order_edit(ctx(), "#1234", bad)
            find_order.assert_not_called()

    def test_too_many_changes_refuses(self):
        changes = [{"variant_id": f"gid://shopify/ProductVariant/{n}", "quantity": 1} for n in range(31)]
        with mock.patch.object(entry.order_editing, "find_order_for_edit") as find_order:
            with self.assertRaises(entry.EntryError):
                entry.prepare_order_edit(ctx(), "#1234", changes)
        find_order.assert_not_called()

    def test_shopify_refusing_becomes_a_clean_refusal(self):
        with mock.patch.object(entry.order_editing, "find_order_for_edit", side_effect=ShopifyError("not found")):
            with self.assertRaises(entry.EntryError):
                entry.prepare_order_edit(ctx(), "#9999", [{"variant_id": VARIANT_A, "quantity": 1}])


def make_edit_proposal(order=None, changes=None):
    result, *_ = PrepareOrderEditTests().prepare(order=order, changes=changes)
    return {"token": "orders-edit-1-v1", **result["proposal"]}


class ExecuteOrderEditTests(unittest.TestCase):
    APPROVER = {"id": "U1", "user_id": "twl:jimmy-shore", "name": "Jimmy Shore", "roles": ["orders.use", "orders.approve"]}
    CONVERSATION = {"id": "slack:C0SALES:1.1", "source": "slack", "visibility": "channel"}

    def setUp(self):
        for patch in (
            mock.patch.object(authorization, "get_authz_config", return_value=USERS),
            mock.patch.object(authorization, "get_order_entry_channels", return_value=frozenset({"C0SALES"})),
            mock.patch.object(entry, "admin_order_url", side_effect=lambda legacy: f"https://admin.example/orders/{legacy}"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.proposal = make_edit_proposal()

    def run_execute(self, choice="approve_edit", proposal=None, order=None, snap=None, committed=None):
        order = order or edit_order()
        snap = snap or calc_snapshot(line_items=[calc_line(VARIANT_A, "Arran 10", 12, line_total="1042.80")], total="782.10")
        committed = committed or {"id": ORDER_EDIT_ID, "name": "#1234", "legacyResourceId": "9", "displayFinancialStatus": "PENDING", "totalPriceSet": MONEY_NODE("782.10")}
        with mock.patch.object(entry.order_editing, "find_order_for_edit", return_value=order), \
             mock.patch.object(entry.order_editing, "begin_edit", return_value="calc-1") as begin, \
             mock.patch.object(entry.order_editing, "set_quantity") as set_qty, \
             mock.patch.object(entry.order_editing, "add_variant") as add_var, \
             mock.patch.object(entry.order_editing, "snapshot", return_value=snap), \
             mock.patch.object(entry.order_editing, "commit_edit", return_value=committed) as commit:
            result = entry.execute(self.APPROVER, self.CONVERSATION, choice, proposal or self.proposal, "req1")
        return result, commit

    def test_commits_and_reports_success(self):
        result, commit = self.run_execute()
        self.assertEqual(result["status"], "ok")
        self.assertIn("#1234", result["text"])
        self.assertIn("https://admin.example/orders/9", result["text"])
        commit.assert_called_once_with("calc-1", notify_customer=False)

    def test_an_unknown_choice_is_refused(self):
        with mock.patch.object(entry.order_editing, "begin_edit") as begin:
            with self.assertRaises(entry.ActRefused):
                entry.execute(self.APPROVER, self.CONVERSATION, "approve_all", self.proposal, "req1")
        begin.assert_not_called()

    def test_no_approve_role_writes_nothing(self):
        user = {**self.APPROVER, "roles": ["orders.use"]}
        with mock.patch.object(entry.order_editing, "begin_edit") as begin:
            with self.assertRaises(entry.ActRefused):
                entry.execute(user, self.CONVERSATION, "approve_edit", self.proposal, "req1")
        begin.assert_not_called()

    def test_a_paid_order_refuses_and_commits_nothing(self):
        with self.assertRaises(entry.ActRefused):
            self.run_execute(order=edit_order(financial_status="PAID"))

    def test_a_total_mismatch_refuses_and_does_not_commit(self):
        mismatched = calc_snapshot(line_items=[calc_line(VARIANT_A, "Arran 10", 12, line_total="9999.00")], total="9999.00")
        with self.assertRaises(entry.ActRefused):
            self.run_execute(snap=mismatched)

    def test_shopify_refusing_is_a_clean_refusal(self):
        with mock.patch.object(entry.order_editing, "find_order_for_edit", side_effect=ShopifyError("boom")):
            with self.assertRaises(entry.ActRefused):
                entry.execute(self.APPROVER, self.CONVERSATION, "approve_edit", self.proposal, "req1")

    def test_a_repeated_approval_is_a_no_op_second_time(self):
        # Quantities are absolute targets, so re-running the same changes against an order that
        # already matches them stages nothing - idempotent by construction, no dedup token needed.
        already_applied = edit_order(lines=[{"line_item_id": LINE_ITEM_A, "title": "Arran 10", "quantity": 12, "variant_id": VARIANT_A}])
        snap = calc_snapshot(line_items=[calc_line(VARIANT_A, "Arran 10", 12, line_total="1042.80")], total="782.10")
        with mock.patch.object(entry.order_editing, "find_order_for_edit", return_value=already_applied), \
             mock.patch.object(entry.order_editing, "begin_edit", return_value="calc-1"), \
             mock.patch.object(entry.order_editing, "set_quantity") as set_qty, \
             mock.patch.object(entry.order_editing, "snapshot", return_value=snap), \
             mock.patch.object(entry.order_editing, "commit_edit", return_value={"id": ORDER_EDIT_ID, "name": "#1234", "legacyResourceId": "9"}):
            entry.execute(self.APPROVER, self.CONVERSATION, "approve_edit", self.proposal, "req1")
        set_qty.assert_not_called()

    def test_a_malformed_payload_is_refused(self):
        for mutate in (
            lambda p: p.update(version=2),
            lambda p: p.pop("order_id"),
            lambda p: p.update(order_id="gid://shopify/DraftOrder/9"),
            lambda p: p.update(changes=[]),
            lambda p: p["changes"][0].update(quantity=-1),
            lambda p: p.pop("expected_total"),
        ):
            proposal = {**self.proposal, "payload": __import__("copy").deepcopy(self.proposal["payload"])}
            mutate(proposal["payload"])
            with mock.patch.object(entry.order_editing, "begin_edit") as begin:
                with self.assertRaises(entry.ActRefused):
                    entry.execute(self.APPROVER, self.CONVERSATION, "approve_edit", proposal, "req1")
            begin.assert_not_called()


class PrepareInvoiceHandoffTests(unittest.TestCase):
    def test_an_unpaid_order_gets_a_start_invoicing_proposal(self):
        with mock.patch.object(entry.order_editing, "find_order_for_edit", return_value=edit_order()):
            result = entry.prepare_invoice_handoff(ctx(), "#1234")
        proposal = result["proposal"]
        self.assertEqual(proposal["kind"], "invoice_handoff")
        self.assertEqual([c["id"] for c in proposal["choices"]], ["approve_invoice"])
        self.assertEqual(proposal["payload"], {"version": 1, "order_id": ORDER_EDIT_ID, "order_name": "#1234"})
        self.assertIn("#1234", result["text"])

    def test_a_paid_order_refuses(self):
        with mock.patch.object(entry.order_editing, "find_order_for_edit", return_value=edit_order(financial_status="PAID")):
            with self.assertRaises(entry.EntryError):
                entry.prepare_invoice_handoff(ctx(), "#1234")

    def test_no_capability_refuses(self):
        with mock.patch.object(entry.order_editing, "find_order_for_edit") as find_order:
            with self.assertRaises(entry.EntryError):
                entry.prepare_invoice_handoff(ctx(capabilities=("orders",)), "#1234")
        find_order.assert_not_called()

    def test_shopify_refusing_becomes_a_clean_refusal(self):
        with mock.patch.object(entry.order_editing, "find_order_for_edit", side_effect=ShopifyError("not found")):
            with self.assertRaises(entry.EntryError):
                entry.prepare_invoice_handoff(ctx(), "#9999")


def make_invoice_handoff_proposal(order=None):
    with mock.patch.object(entry.order_editing, "find_order_for_edit", return_value=order or edit_order()):
        result = entry.prepare_invoice_handoff(ctx(), "#1234")
    return {"token": "orders-invoice-1-v1", **result["proposal"]}


class ExecuteInvoiceHandoffTests(unittest.TestCase):
    APPROVER = {"id": "U1", "user_id": "twl:jimmy-shore", "name": "Jimmy Shore", "roles": ["orders.use", "orders.approve"]}
    CONVERSATION = {"id": "slack:C0SALES:1.1", "source": "slack", "visibility": "channel"}

    def setUp(self):
        for patch in (
            mock.patch.object(authorization, "get_authz_config", return_value=USERS),
            mock.patch.object(authorization, "get_order_entry_channels", return_value=frozenset({"C0SALES"})),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.proposal = make_invoice_handoff_proposal()

    def run_execute(self, choice="approve_invoice", proposal=None, order=None):
        with mock.patch.object(entry.order_editing, "find_order_for_edit", return_value=order or edit_order()):
            return entry.execute(self.APPROVER, self.CONVERSATION, choice, proposal or self.proposal, "req1")

    def test_hands_off_to_invoicing(self):
        result = self.run_execute()
        self.assertEqual(result["status"], "ok")
        self.assertIn("#1234", result["text"])
        self.assertEqual(
            result["handoff"],
            {
                "agent_id": "invoicing", "text": "Prepare a Xero invoice for Shopify order #1234.",
                "context": {"action": "prepare_invoice", "order_id": ORDER_EDIT_ID, "order_name": "#1234"},
            },
        )

    def test_an_unknown_choice_is_refused(self):
        with mock.patch.object(entry.order_editing, "find_order_for_edit") as find_order:
            with self.assertRaises(entry.ActRefused):
                entry.execute(self.APPROVER, self.CONVERSATION, "approve_all", self.proposal, "req1")
        find_order.assert_not_called()

    def test_no_approve_role_starts_nothing(self):
        user = {**self.APPROVER, "roles": ["orders.use"]}
        with mock.patch.object(entry.order_editing, "find_order_for_edit") as find_order:
            with self.assertRaises(entry.ActRefused):
                entry.execute(user, self.CONVERSATION, "approve_invoice", self.proposal, "req1")
        find_order.assert_not_called()

    def test_refuses_if_the_order_became_paid_since_it_was_prepared(self):
        with self.assertRaises(entry.ActRefused):
            self.run_execute(order=edit_order(financial_status="PAID"))

    def test_refuses_if_the_order_id_changed(self):
        with self.assertRaises(entry.ActRefused):
            self.run_execute(order=edit_order(id="gid://shopify/Order/999"))

    def test_shopify_refusing_is_a_clean_refusal(self):
        with mock.patch.object(entry.order_editing, "find_order_for_edit", side_effect=ShopifyError("boom")):
            with self.assertRaises(entry.ActRefused):
                entry.execute(self.APPROVER, self.CONVERSATION, "approve_invoice", self.proposal, "req1")

    def test_a_malformed_payload_is_refused(self):
        for mutate in (
            lambda p: p.update(version=2),
            lambda p: p.pop("order_id"),
            lambda p: p.update(order_id="gid://shopify/DraftOrder/9"),
            lambda p: p.pop("order_name"),
            lambda p: p.update(order_name=""),
        ):
            proposal = {**self.proposal, "payload": dict(self.proposal["payload"])}
            mutate(proposal["payload"])
            with mock.patch.object(entry.order_editing, "find_order_for_edit") as find_order:
                with self.assertRaises(entry.ActRefused, msg=repr(proposal["payload"])):
                    entry.execute(self.APPROVER, self.CONVERSATION, "approve_invoice", proposal, "req1")
            find_order.assert_not_called()


CUSTOMER = "gid://shopify/Customer/77"


def person(**over):
    base = {"kind": "customer", "customer_id": CUSTOMER, "name": "Pat Example", "shipping": {"address1": "9 Home St"}, "billing": {"address1": "9 Home St"}, "terms": None}
    return {**base, **over}


class IndividualCustomerTests(unittest.TestCase):
    """Not every customer is set up as a company. Individuals get normal prices and default terms."""

    def prepare(self, subject=None):
        def fake_calc(draft_input):
            self.last_input = draft_input
            return priced([{"variant_id": i["variantId"], "quantity": i["quantity"], "discount": i.get("appliedDiscount")} for i in draft_input["lineItems"]])

        with mock.patch.object(entry.shop, "get_customer", return_value=subject or person()), \
             mock.patch.object(entry.shop, "calculate", side_effect=fake_calc), \
             mock.patch.object(entry.shop, "get_variants", return_value={VARIANT_A: {"status": "ACTIVE", "sku": "S", "name": "n", "stock": None}}), \
             mock.patch.object(entry.shop, "default_unpaid_terms", return_value=TERMS):
            return entry.prepare(ctx(), {"customer_id": CUSTOMER}, raw((VARIANT_A, 2)))

    def test_an_individual_is_ordered_as_a_customer_with_no_company(self):
        result = self.prepare()
        self.assertEqual(self.last_input["purchasingEntity"], {"customerId": CUSTOMER})
        payload = result["proposal"]["payload"]
        self.assertEqual(payload["target"], {"kind": "customer", "customer_id": CUSTOMER})
        self.assertEqual(payload["display"], {"name": "Pat Example", "place": None})
        self.assertEqual(payload["terms"], TERMS)      # no company terms, so the default
        self.assertIn("Draft order for Pat Example", result["text"])

    def test_the_customers_address_is_used_but_never_shown(self):
        result = self.prepare()
        self.assertEqual(self.last_input["shippingAddress"], {"address1": "9 Home St"})
        self.assertNotIn("9 Home St", result["text"])

    def test_no_address_on_file_is_a_warning(self):
        result = self.prepare(person(shipping=None, billing=None))
        self.assertIn("no delivery address", result["text"])
        self.assertNotIn("shippingAddress", self.last_input)

    def test_targets_must_be_exactly_one_form(self):
        for bad in (
            {}, None, "x",
            {"customer_id": CUSTOMER, "company_id": COMPANY},
            {"customer_id": CUSTOMER, "location_id": LOCATION},
            {"customer_id": "gid://shopify/Company/1"},
            {"company_id": COMPANY},
            {"location_id": LOCATION},
            {"company_id": LOCATION, "location_id": COMPANY},
        ):
            with self.assertRaises(entry.EntryError, msg=str(bad)):
                entry.normalize_target(bad)
        self.assertEqual(entry.normalize_target({"customer_id": CUSTOMER})["kind"], "customer")
        self.assertEqual(entry.normalize_target({"company_id": COMPANY, "location_id": LOCATION})["kind"], "company")

    def test_a_company_contact_can_be_ordered_as_a_personal_account_and_the_draft_says_so(self):
        # Allowed when asked for (via their email), but the approver must see the company's prices don't apply.
        contact = {"customer": {"id": CUSTOMER, "displayName": "Sam Contact", "companyContactProfiles": [{"id": "x", "company": {"name": "Nicks Wine Merchants"}}], "defaultAddress": None}}
        with mock.patch.object(entry.shop, "graphql", return_value=contact):
            subject = entry.shop.get_customer(CUSTOMER)
        self.assertEqual(subject["contact_of"], ["Nicks Wine Merchants"])
        result = self.prepare(person(name="Sam Contact", contact_of=["Nicks Wine Merchants"]))
        self.assertIn("Sam Contact's personal account, not the Nicks Wine Merchants company account", result["text"])
        self.assertIn("price list and payment terms do not apply", result["text"])
        self.assertEqual(self.last_input["purchasingEntity"], {"customerId": CUSTOMER})       # no company, so normal prices

    def test_an_ordinary_individual_gets_no_personal_account_warning(self):
        self.assertNotIn("personal account", self.prepare()["text"])

    def test_the_personal_account_order_is_created_for_a_company_contact(self):
        prepared = self.prepare(person(name="Sam Contact", contact_of=["Nicks Wine Merchants"]))["proposal"]
        proposal = {"token": "orders-20260922-0900-ef56-v1", "kind": prepared["kind"], "items": prepared["items"], "payload": prepared["payload"]}
        created = []
        approver = {"id": "U1", "user_id": "twl:jimmy-shore", "name": "Jimmy Shore", "roles": ["orders.use", "orders.approve"]}
        with mock.patch.object(authorization, "get_authz_config", return_value=USERS), \
             mock.patch.object(authorization, "get_order_entry_channels", return_value=frozenset({"C0SALES"})), \
             mock.patch.object(entry.shop, "find_draft_by_tag", return_value=None), \
             mock.patch.object(entry.shop, "get_customer", return_value=person(name="Sam Contact", contact_of=["Nicks Wine Merchants"])), \
             mock.patch.object(entry.shop, "calculate", side_effect=lambda i: priced([{"variant_id": x["variantId"], "quantity": x["quantity"], "discount": None} for x in i["lineItems"]])), \
             mock.patch.object(entry.shop, "create_draft", side_effect=lambda i: created.append(i) or {"id": "gid://shopify/DraftOrder/8"}), \
             mock.patch.object(entry.shop, "complete_draft", return_value={"id": "gid://shopify/Order/3", "name": "#2002", "legacyResourceId": "3"}), \
             mock.patch.object(entry, "admin_order_url", side_effect=lambda legacy: f"https://admin.example/orders/{legacy}"):
            result = entry.execute(approver, {"id": "slack:C0SALES:1.1", "source": "slack", "visibility": "channel"}, "approve_only", proposal, "r1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(created[0]["purchasingEntity"], {"customerId": CUSTOMER})

    def test_search_leaves_company_contacts_out_of_the_individuals(self):
        people = {"customers": {"nodes": [
            {"id": "gid://shopify/Customer/1", "displayName": "Pat Example", "companyContactProfiles": []},
            {"id": "gid://shopify/Customer/2", "displayName": "Sam Contact", "companyContactProfiles": [{"id": "c"}]},
        ]}}
        companies = {"companies": {"nodes": [{"id": COMPANY, "name": "Nicks", "mainContact": {"id": "c"}, "contacts": {"nodes": []}, "locations": {"nodes": [{"id": LOCATION, "name": "Nicks"}]}}]}}
        with mock.patch.object(entry.shop, "graphql", side_effect=[companies, people]):
            found = entry.shop.find_customers("pat")
        self.assertEqual([p["name"] for p in found["individual_customers"]], ["Pat Example"])
        self.assertEqual(found["companies"][0]["name"], "Nicks")
        self.assertEqual(set(found["individual_customers"][0]), {"customer_id", "name", "exact_name_match"})   # nothing else leaves
        self.assertIn("More than one", found["note"])

    def test_search_never_returns_contact_details(self):
        with mock.patch.object(entry.shop, "graphql", side_effect=[{"companies": {"nodes": []}}, {"customers": {"nodes": []}}]):
            found = entry.shop.find_customers("nobody")
        self.assertIn("can't be created", found["note"])

    def test_a_name_search_cannot_carry_shopify_filter_syntax(self):
        with mock.patch.object(entry.shop, "graphql", side_effect=[{"companies": {"nodes": []}}, {"customers": {"nodes": []}}]) as graphql:
            entry.shop.find_customers("tag:vip OR total_spent:>1000 nicks")
        for call in graphql.call_args_list:
            sent = call.args[1]["query"]
            self.assertNotIn(":", sent)
            self.assertNotIn(">", sent)

    def test_an_email_can_only_ever_send_that_address_in_a_fixed_email_filter(self):
        for typed in ("jane@example.com", "JANE@Example.COM", 'x" OR email:* jane@example.com', "email:jane@example.com company account"):
            with mock.patch.object(entry.shop, "graphql", return_value={"customers": {"nodes": []}}) as graphql:
                entry.shop.find_customers(typed)
            self.assertEqual(graphql.call_count, 1, typed)
            self.assertEqual(graphql.call_args.args[1], {"query": 'email:"jane@example.com"'}, typed)
        # Characters that could break out of the filter never form an address at all.
        with mock.patch.object(entry.shop, "graphql", side_effect=[{"companies": {"nodes": []}}, {"customers": {"nodes": []}}]) as graphql:
            entry.shop.find_customers('"@evil.com OR email:*')
        self.assertNotIn("email:", graphql.call_args_list[0].args[1]["query"])

    def test_execute_creates_the_order_for_an_individual(self):
        prepared = self.prepare()["proposal"]
        proposal = {"token": "orders-20260921-1500-cd34-v1", "kind": prepared["kind"], "items": prepared["items"], "payload": prepared["payload"]}
        created = []
        approver = {"id": "U1", "user_id": "twl:jimmy-shore", "name": "Jimmy Shore", "roles": ["orders.use", "orders.approve"]}
        with mock.patch.object(authorization, "get_authz_config", return_value=USERS), \
             mock.patch.object(authorization, "get_order_entry_channels", return_value=frozenset({"C0SALES"})), \
             mock.patch.object(entry.shop, "find_draft_by_tag", return_value=None), \
             mock.patch.object(entry.shop, "get_customer", return_value=person()), \
             mock.patch.object(entry.shop, "calculate", side_effect=lambda i: priced([{"variant_id": x["variantId"], "quantity": x["quantity"], "discount": None} for x in i["lineItems"]])), \
             mock.patch.object(entry.shop, "create_draft", side_effect=lambda i: created.append(i) or {"id": "gid://shopify/DraftOrder/8"}), \
             mock.patch.object(entry.shop, "complete_draft", return_value={"id": "gid://shopify/Order/3", "name": "#2001", "legacyResourceId": "3"}), \
             mock.patch.object(entry, "admin_order_url", side_effect=lambda legacy: f"https://admin.example/orders/{legacy}"):
            result = entry.execute(approver, {"id": "slack:C0SALES:1.1", "source": "slack", "visibility": "channel"}, "approve_send_invoice", proposal, "r1")
        self.assertEqual(result["text"], "Success: <https://admin.example/orders/3|Order #2001> created")
        self.assertEqual(result["handoff"]["agent_id"], "invoicing")
        self.assertEqual(created[0]["purchasingEntity"], {"customerId": CUSTOMER})
        self.assertEqual(created[0]["paymentTerms"], {"paymentTermsTemplateId": TERMS["id"]})


def company_node(cid, name, locations=1):
    return {"id": f"gid://shopify/Company/{cid}", "name": name, "mainContact": {"id": "c"}, "contacts": {"nodes": []},
            "locations": {"nodes": [{"id": f"gid://shopify/CompanyLocation/{cid}{n}", "name": f"{name} {n}"} for n in range(locations)]}}


def person_node(cid, name):
    return {"id": f"gid://shopify/Customer/{cid}", "displayName": name, "companyContactProfiles": []}


class CustomerLookupTests(unittest.TestCase):
    """Shopify's company search is loose. An exact name should win."""

    def find(self, query, companies, people):
        answers = [{"companies": {"nodes": companies}}, {"customers": {"nodes": people}}]
        with mock.patch.object(entry.shop, "graphql", side_effect=answers):
            return entry.shop.find_customers(query)

    def test_the_whisky_list_is_found_among_its_lookalikes(self):
        # What happened in #sales: four companies and some individuals came back.
        found = self.find(
            "The Whisky List",
            [company_node(2, "The Whisky Company"), company_node(1, "The Whisky List"), company_node(3, "The Whisky Club"), company_node(4, "The Whisky Experience")],
            [person_node(9, "Whisky Lister")],
        )
        self.assertEqual([c["name"] for c in found["companies"]][0], "The Whisky List")           # the exact match goes first
        self.assertEqual(sum(c["exact_name_match"] for c in found["companies"]), 1)
        self.assertEqual(found["exact_match"]["name"], "The Whisky List")
        self.assertEqual(found["exact_match"]["company_id"], "gid://shopify/Company/1")
        self.assertEqual(found["exact_match"]["location_id"], "gid://shopify/CompanyLocation/10")  # ready to use
        self.assertIn("Use it, and say which you chose", found["note"])

    def test_matching_ignores_case_punctuation_and_spacing(self):
        for typed in ("the whisky list", "The  Whisky   List", "THE WHISKY LIST.", "the whisky-list"):
            found = self.find(typed, [company_node(1, "The Whisky List"), company_node(2, "The Whisky Club")], [])
            self.assertEqual(found["exact_match"]["name"], "The Whisky List", typed)

    def test_an_exact_company_with_several_locations_asks_which_location(self):
        found = self.find("Single Malt Whisky Club", [company_node(5, "Single Malt Whisky Club", locations=2), company_node(6, "Single Malt Club")], [])
        self.assertEqual(found["exact_match"]["name"], "Single Malt Whisky Club")
        self.assertNotIn("location_id", found["exact_match"])
        self.assertIn("Ask which location", found["note"])

    def test_an_exact_individual_is_recognised(self):
        found = self.find("Pat Example", [company_node(7, "Pat Example Wines")], [person_node(3, "Pat Example"), person_node(4, "Patricia Examples")])
        self.assertEqual(found["exact_match"], {"name": "Pat Example", "kind": "individual"})
        self.assertEqual(found["individual_customers"][0]["name"], "Pat Example")

    def test_two_customers_with_exactly_the_same_name_ask(self):
        found = self.find("Nicks Wine", [company_node(8, "Nicks Wine")], [person_node(5, "Nicks Wine")])
        self.assertNotIn("exact_match", found)
        self.assertIn("More than one customer has exactly this name", found["note"])

    def test_no_exact_name_among_several_asks(self):
        found = self.find("Whisky", [company_node(1, "The Whisky List"), company_node(2, "The Whisky Club")], [])
        self.assertNotIn("exact_match", found)
        self.assertIn("none with exactly that name", found["note"])

    def test_a_single_similar_match_is_not_treated_as_exact(self):
        found = self.find("Whisky Lst", [company_node(1, "The Whisky List")], [])
        self.assertNotIn("exact_match", found)
        self.assertIsNone(found["note"])          # one match, nothing to ask: the caller decides

    def test_nothing_found(self):
        found = self.find("Nobody Ltd", [], [])
        self.assertIn("can't be created", found["note"])

    def test_exact_matching_never_exposes_contact_details(self):
        found = self.find("The Whisky List", [company_node(1, "The Whisky List")], [person_node(9, "The Whisky List")])
        text = str(found)
        for private in ("@", "phone", "address", "email"):
            self.assertNotIn(private, text.lower())


def email_node(cid, name, email, companies=()):
    return {
        "id": f"gid://shopify/Customer/{cid}", "displayName": name, "defaultEmailAddress": {"emailAddress": email},
        "companyContactProfiles": [
            {"id": f"gid://shopify/CompanyContact/{cid}{n}",
             "company": {"id": f"gid://shopify/Company/{c}", "name": c_name,
                         "locations": {"nodes": [{"id": f"gid://shopify/CompanyLocation/{c}{k}", "name": f"{c_name} {k}"} for k in range(locs)]}}}
            for n, (c, c_name, locs) in enumerate(companies)
        ],
    }


class EmailLookupTests(unittest.TestCase):
    """Find a customer by email address. Shopify's own email search is loose, so it is matched exactly here."""

    def find(self, typed, nodes):
        with mock.patch.object(entry.shop, "graphql", return_value={"customers": {"nodes": nodes}}):
            return entry.shop.find_customers(typed)

    def similar(self):
        # What Shopify really returned for chris@thewhiskylist.com.au: lookalikes with + addressing.
        return [
            email_node(1, "Chris Ross", "chris@thewhiskylist.com.au", [(10, "The Whisky List", 1)]),
            email_node(2, "Chris Ross", "chris+1@thewhiskylist.com.au"),
            email_node(3, "Toby Axford", "chris+2@thewhiskylist.com.au"),
            email_node(4, "FBA: Harvie Mae PTY LTD", "chris+3@thewhiskylist.com.au"),
        ]

    def test_only_the_exact_address_is_used_never_the_lookalikes(self):
        found = self.find("chris@thewhiskylist.com.au", self.similar())
        text = str(found)
        for stranger in ("Toby Axford", "Harvie Mae", "chris+"):
            self.assertNotIn(stranger, text)
        self.assertEqual({a["account"] for a in found["accounts"]}, {"company", "personal"})
        self.assertEqual(next(a for a in found["accounts"] if a["account"] == "personal")["customer_id"], "gid://shopify/Customer/1")

    def test_a_company_contact_gets_both_a_company_and_a_personal_account(self):
        found = self.find("chris@thewhiskylist.com.au", self.similar())
        company = next(a for a in found["accounts"] if a["account"] == "company")
        self.assertEqual(company["name"], "The Whisky List")
        self.assertEqual(company["company_id"], "gid://shopify/Company/10")
        self.assertEqual(len(company["locations"]), 1)
        self.assertIn("company account", found["note"])
        self.assertIn("personal account", found["note"])
        self.assertIn("Otherwise ask", found["note"])

    def test_someone_who_is_not_a_company_contact_has_just_a_personal_account(self):
        found = self.find("pat@example.com", [email_node(7, "Pat Example", "pat@example.com")])
        self.assertEqual([a["account"] for a in found["accounts"]], ["personal"])
        self.assertIn("no company account", found["note"])
        self.assertIn("Use it", found["note"])

    def test_a_contact_at_two_companies_lists_both(self):
        found = self.find("sam@example.com", [email_node(8, "Sam", "sam@example.com", [(20, "Nicks Wine Merchants", 1), (21, "Cellarbrations", 2)])])
        companies = [a for a in found["accounts"] if a["account"] == "company"]
        self.assertEqual([c["name"] for c in companies], ["Nicks Wine Merchants", "Cellarbrations"])
        self.assertEqual(len(companies[1]["locations"]), 2)
        self.assertIn("Nicks Wine Merchants and Cellarbrations", found["note"])

    def test_no_exact_address_finds_nobody_and_leaks_nothing(self):
        found = self.find("chris@thewhiskylist.com.au", self.similar()[1:])
        self.assertEqual(found["accounts"], [])
        self.assertIn("No customer has exactly that email address", found["note"])
        for stranger in ("Toby Axford", "Harvie Mae", "chris+"):
            self.assertNotIn(stranger, str(found))

    def test_case_and_surrounding_words_do_not_matter(self):
        for typed in ("CHRIS@TheWhiskyList.com.au", "chris@thewhiskylist.com.au company account", "  order for Chris@thewhiskylist.com.au (personal)  "):
            found = self.find(typed, self.similar())
            self.assertEqual(len(found["accounts"]), 2, typed)

    def test_the_email_is_never_returned(self):
        found = self.find("chris@thewhiskylist.com.au", self.similar())
        self.assertNotIn("@", str(found))
        self.assertNotIn("thewhiskylist.com.au", str(found))

    def test_a_typed_address_is_not_mistaken_for_a_different_one(self):
        # A near miss on the address must not resolve to the person.
        nodes = [email_node(1, "Chris Ross", "chris@thewhiskylist.com.au", [(10, "The Whisky List", 1)])]
        for typed in ("chris@thewhiskylist.com", "chri@thewhiskylist.com.au", "chris@thewhiskylist.co.au", "hris@thewhiskylist.com.au"):
            self.assertEqual(self.find(typed, nodes)["accounts"], [], typed)

    def test_a_name_without_an_at_sign_still_searches_by_name(self):
        with mock.patch.object(entry.shop, "graphql", side_effect=[{"companies": {"nodes": []}}, {"customers": {"nodes": []}}]) as graphql:
            entry.shop.find_customers("chris thewhiskylist")
        self.assertEqual(graphql.call_count, 2)


class ToolTests(unittest.TestCase):
    def names(self, capabilities):
        with mock.patch.object(authorization, "get_order_entry_channels", return_value=frozenset({"C0SALES"})):
            _server, names = build_server(ctx(capabilities))
        return set(names)

    def test_order_entry_tools_exist_only_with_the_capability(self):
        self.assertTrue({"find_customer", "find_variant", "prepare_draft_order"} <= self.names(("orders", "order_entry")))
        for tool in ("find_customer", "find_variant", "prepare_draft_order"):
            self.assertNotIn(tool, self.names(("orders", "products", "inventory", "customers")))

    def test_there_is_no_tool_that_writes(self):
        names = self.names(tuple(ALL))
        for name in names:
            for verb in ("create", "complete", "approve", "update", "delete", "cancel"):
                self.assertNotIn(verb, name)

class EndpointTests(unittest.TestCase):
    """/v1/message and /v1/act through the Flask app, with the model and Shopify replaced."""

    CONVERSATION = {"id": "slack:C0SALES:1.1", "source": "slack", "visibility": "channel"}
    JIMMY = {"id": "U1", "user_id": "twl:jimmy-shore", "name": "Jimmy Shore", "roles": ["orders.use", "orders.approve"]}

    def setUp(self):
        import main
        self.main = main
        self.client = main.app.test_client()
        for patch in (
            mock.patch.object(authorization, "get_authz_config", return_value=USERS),
            mock.patch.object(authorization, "get_order_entry_channels", return_value=frozenset({"C0SALES"})),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def message(self, text="new order for Nicks", open_proposal=None, conversation=None, roles=("orders.use",), context=None):
        return self.client.post("/v1/message", json={
            "conversation_id": "slack:C0SALES:1.1", "conversation": conversation or self.CONVERSATION,
            "user": {**self.JIMMY, "roles": list(roles)}, "text": text, "open_proposal": open_proposal,
            "context": context,
        })

    def test_a_prepared_draft_is_posted_as_code_written_text_with_the_proposal(self):
        prepared = make_prepared()

        async def fake_run(prompt, ctx_, state=None):
            state.proposal, state.text = prepared, "CODE-WRITTEN DRAFT"
            return "Sure! The total is $1 million."   # the model's own words must not be used

        with mock.patch.object(self.main, "run_agent", fake_run):
            body = self.message().get_json()
        self.assertEqual(body["text"], "CODE-WRITTEN DRAFT")
        self.assertEqual(body["proposal"]["kind"], "draft_order")
        self.assertEqual([c["id"] for c in body["proposal"]["choices"]], ["approve_send_invoice", "approve_only"])
        self.assertNotIn("million", str(body))

    def test_without_a_draft_the_models_answer_is_used(self):
        async def fake_run(prompt, ctx_, state=None):
            return "Which Nicks: the bottle shop or the bar?"

        with mock.patch.object(self.main, "run_agent", fake_run):
            body = self.message().get_json()
        self.assertEqual(body, {"text": "Which Nicks: the bottle shop or the bar?"})

    def test_the_open_draft_is_shown_to_the_model_so_edits_can_be_applied(self):
        prepared = make_prepared()
        seen = []

        async def fake_run(prompt, ctx_, state=None):
            seen.append(prompt)
            return "ok"

        with mock.patch.object(self.main, "run_agent", fake_run):
            self.message("make it 12 bottles", open_proposal={"kind": "draft_order", "payload": prepared["payload"]})
        self.assertIn("prepare_draft_order again", seen[0])
        self.assertIn(VARIANT_A, seen[0])
        self.assertIn("Nicks Wine Merchants", seen[0])

    def test_order_entry_in_an_unlisted_channel_is_not_offered(self):
        seen = []

        async def fake_run(prompt, ctx_, state=None):
            seen.append(ctx_)
            return "ok"

        with mock.patch.object(self.main, "run_agent", fake_run):
            self.message(conversation={"id": "slack:C0OTHER:1.1", "source": "slack", "visibility": "channel"})
        self.assertFalse(seen[0].has("order_entry"))

    def act(self, choice="approve_only", roles=None, conversation=None):
        prepared = make_prepared()
        return self.client.post("/v1/act", json={
            "conversation_id": "slack:C0SALES:1.1", "conversation": conversation or self.CONVERSATION,
            "user": {**self.JIMMY, "roles": roles or self.JIMMY["roles"]}, "action_id": "approve", "choice": choice,
            "selected_ids": None,
            "proposal": {"token": "orders-20260921-1500-ab12-v1", "kind": "draft_order", "items": prepared["items"], "payload": prepared["payload"]},
        })

    def test_act_success_returns_the_order(self):
        with mock.patch.object(self.main.entry, "execute", return_value={"status": "ok", "text": "Order #1", "result": {}}) as execute:
            response = self.act("approve_send_invoice")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(execute.call_args.args[2], "approve_send_invoice")

    def test_a_refusal_is_a_4xx_so_the_gateway_says_nothing_was_changed(self):
        with mock.patch.object(self.main.entry, "execute", side_effect=entry.ActRefused("no")):
            response = self.act()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "no")

    def test_an_unexpected_failure_is_a_5xx_so_the_gateway_says_outcome_unknown(self):
        with mock.patch.object(self.main.entry, "execute", side_effect=RuntimeError("boom")):
            response = self.act()
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("boom", response.get_data(as_text=True))  # no internals leaked

    def test_unreadable_permissions_are_a_clean_refusal_not_an_unknown_outcome(self):
        with mock.patch.object(authorization, "get_authz_config", side_effect=authorization.AuthorizationUnavailable("x")):
            response = self.act()
        self.assertEqual(response.status_code, 400)

    def test_act_end_to_end_without_the_approve_role_creates_nothing(self):
        with mock.patch.object(entry.shop, "create_draft") as create_draft, \
             mock.patch.object(entry.shop, "find_draft_by_tag", return_value=None):
            response = self.act(roles=["orders.use"])
        self.assertEqual(response.status_code, 400)
        create_draft.assert_not_called()

    def test_a_mark_order_paid_handoff_bypasses_the_model_entirely(self):
        order = {"id": "gid://shopify/Order/9", "name": "#1234", "legacyResourceId": "9", "displayFinancialStatus": "PAID"}
        with mock.patch.object(entry.shop, "mark_paid", return_value=order) as mark_paid, \
             mock.patch.object(self.main, "run_agent") as run_agent:
            response = self.message(
                text="Mark order #1234 as paid.",
                roles=("orders.use", "orders.approve"),
                context={"action": "mark_order_paid", "order_id": "gid://shopify/Order/9", "order_name": "#1234"},
            )
        body = response.get_json()
        self.assertIn("#1234", body["text"])
        self.assertIn("paid", body["text"])
        mark_paid.assert_called_once_with("gid://shopify/Order/9")
        run_agent.assert_not_called()

    def test_a_mark_order_paid_refusal_is_a_plain_answer_not_an_http_error(self):
        with mock.patch.object(entry.shop, "mark_paid") as mark_paid:
            response = self.message(
                roles=("orders.use",),  # no orders.approve
                context={"action": "mark_order_paid", "order_id": "gid://shopify/Order/9"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("permission", response.get_json()["text"])
        mark_paid.assert_not_called()

    def test_a_bogus_context_action_is_ignored_and_falls_through_to_the_model(self):
        with mock.patch.object(self.main, "run_agent") as run_agent:
            async def fake_run(prompt, ctx_, state=None):
                return "ok"
            run_agent.side_effect = fake_run
            response = self.message(context={"action": "delete_everything"})
        self.assertEqual(response.status_code, 200)
        run_agent.assert_called_once()


if __name__ == "__main__":
    unittest.main()

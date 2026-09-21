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
TERMS = {"id": "gid://shopify/PaymentTermsTemplate/9", "name": "Due on fulfillment"}

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

    def test_fixed_discounts_go_to_shopify_as_the_whole_line_amount(self):
        per_unit = entry.clean_lines([{"variant_id": VARIANT_A, "quantity": 6, "discount_type": "per_unit", "discount_value": 5}])[0]
        self.assertEqual(entry._discount_input(per_unit), {"value": 30.0, "valueType": "FIXED_AMOUNT", "title": "Sales discount"})
        line_total = entry.clean_lines([{"variant_id": VARIANT_A, "quantity": 6, "discount_type": "line_total", "discount_value": 12.5}])[0]
        self.assertEqual(entry._discount_input(line_total)["value"], 12.5)
        pct = entry.clean_lines([{"variant_id": VARIANT_A, "quantity": 6, "discount_type": "percent", "discount_value": 7.5}])[0]
        self.assertEqual(entry._discount_input(pct)["valueType"], "PERCENTAGE")


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

    def test_a_good_draft_becomes_a_proposal_with_paid_and_unpaid_choices(self):
        result = self.prepare()
        proposal = result["proposal"]
        self.assertEqual(proposal["kind"], "draft_order")
        self.assertEqual([c["id"] for c in proposal["choices"]], ["create_paid", "create_unpaid"])
        self.assertEqual(proposal["payload"]["expected"]["total"], "110.00")
        self.assertEqual(proposal["payload"]["requested_by"]["user_id"], "twl:jimmy-shore")
        self.assertIn("Nicks Wine Merchants", result["text"])
        self.assertIn("110.00 AUD", result["text"])
        self.assertIn("Due on fulfillment", result["text"])
        self.assertIn("Requested by Jimmy Shore", result["text"])

    def test_the_draft_is_priced_for_the_company_and_carries_no_email(self):
        self.prepare()
        entity = self.last_input["purchasingEntity"]["purchasingCompany"]
        self.assertEqual(entity["companyId"], COMPANY)
        self.assertEqual(entity["companyLocationId"], LOCATION)
        self.assertNotIn("email", self.last_input)  # no email field, so no customer notification
        self.assertIn("Raised in Slack by Jimmy Shore via Smith. PO 123", self.last_input["note"])

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

    def test_stock_is_not_disclosed_without_the_inventory_capability(self):
        with mock.patch.object(entry.shop, "get_variants", return_value={VARIANT_A: {"status": "ACTIVE", "sku": "S", "name": "n", "stock": None}}) as get_variants, \
             mock.patch.object(entry.shop, "get_location", return_value=location()), \
             mock.patch.object(entry.shop, "calculate", side_effect=lambda i: priced([{"variant_id": VARIANT_A, "quantity": 6}])), \
             mock.patch.object(entry.shop, "default_unpaid_terms", return_value=TERMS):
            result = entry.prepare(ctx(("orders", "order_entry")), {"company_id": COMPANY, "location_id": LOCATION}, raw((VARIANT_A, 6)))
        self.assertFalse(get_variants.call_args.kwargs["include_inventory"])
        self.assertNotIn("stock", result["text"])

    def test_the_location_terms_are_used_else_the_default(self):
        net30 = {"id": "gid://shopify/PaymentTermsTemplate/4", "name": "Net 30"}
        self.assertEqual(self.prepare(loc=location(terms=net30))["proposal"]["payload"]["terms"], net30)
        self.assertEqual(self.prepare(loc=location(terms=None))["proposal"]["payload"]["terms"], TERMS)


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

    def run_execute(self, choice="create_paid", user=None, conversation=None, proposal=None):
        return entry.execute(user or self.APPROVER, conversation or self.CONVERSATION, choice, proposal or self.proposal, "req1")

    def test_paid_creates_the_order_with_no_payment_terms(self):
        result = self.run_execute("create_paid")
        self.assertEqual(result["status"], "ok")
        self.assertIn("#1234", result["text"])
        self.assertIn("https://admin.example/orders/9", result["text"])
        self.assertIn("marked as paid", result["text"])
        self.assertTrue(result["result"]["paid"])
        self.assertNotIn("paymentTerms", self.created[0])
        self.assertEqual(self.completed, ["gid://shopify/DraftOrder/5"])

    def test_unpaid_creates_the_order_with_payment_terms(self):
        result = self.run_execute("create_unpaid")
        self.assertIn("unpaid", result["text"])
        self.assertFalse(result["result"]["paid"])
        self.assertEqual(self.created[0]["paymentTerms"], {"paymentTermsTemplateId": TERMS["id"]})

    def test_the_draft_is_tagged_and_carries_who_asked_and_who_approved(self):
        self.run_execute("create_paid")
        draft = self.created[0]
        self.assertIn("smith-orders-20260921-1500-ab12-v1", draft["tags"])
        self.assertIn("approved by Jimmy Shore", draft["note"])
        self.assertIn("invoiced in Xero", draft["note"])
        self.assertNotIn("email", draft)

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
        for choice in (None, "", "approve", "create", "create_paid; drop"):
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
                entry.execute(self.APPROVER, self.CONVERSATION, "create_paid", proposal, "req1")
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
        self.assertIn("usual order emails", result["text"])

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

    def test_a_company_contact_cannot_be_ordered_as_an_individual(self):
        # Ordering "as" a contact would skip the company's price list and terms.
        contact = {"customer": {"id": CUSTOMER, "displayName": "Sam Contact", "companyContactProfiles": [{"id": "x"}], "defaultAddress": None}}
        with mock.patch.object(entry.shop, "graphql", return_value=contact):
            with self.assertRaises(ShopifyError) as caught:
                entry.shop.get_customer(CUSTOMER)
        self.assertIn("company", str(caught.exception))

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
        self.assertEqual(set(found["individual_customers"][0]), {"customer_id", "name"})   # nothing else leaves
        self.assertIn("More than one", found["note"])

    def test_search_never_returns_contact_details(self):
        with mock.patch.object(entry.shop, "graphql", side_effect=[{"companies": {"nodes": []}}, {"customers": {"nodes": []}}]):
            found = entry.shop.find_customers("nobody")
        self.assertIn("can't be created", found["note"])

    def test_search_terms_are_stripped_of_shopify_filter_syntax(self):
        with mock.patch.object(entry.shop, "graphql", side_effect=[{"companies": {"nodes": []}}, {"customers": {"nodes": []}}]) as graphql:
            entry.shop.find_customers("email:jane@example.com")
        sent = graphql.call_args_list[0].args[1]["query"]
        self.assertNotIn(":", sent)
        self.assertNotIn("@", sent)

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
            result = entry.execute(approver, {"id": "slack:C0SALES:1.1", "source": "slack", "visibility": "channel"}, "create_unpaid", proposal, "r1")
        self.assertIn("Pat Example", result["text"])
        self.assertEqual(created[0]["purchasingEntity"], {"customerId": CUSTOMER})
        self.assertEqual(created[0]["paymentTerms"], {"paymentTermsTemplateId": TERMS["id"]})


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

    def message(self, text="new order for Nicks", open_proposal=None, conversation=None, roles=("orders.use",)):
        return self.client.post("/v1/message", json={
            "conversation_id": "slack:C0SALES:1.1", "conversation": conversation or self.CONVERSATION,
            "user": {**self.JIMMY, "roles": list(roles)}, "text": text, "open_proposal": open_proposal,
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
        self.assertEqual([c["id"] for c in body["proposal"]["choices"]], ["create_paid", "create_unpaid"])
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

    def act(self, choice="create_paid", roles=None, conversation=None):
        prepared = make_prepared()
        return self.client.post("/v1/act", json={
            "conversation_id": "slack:C0SALES:1.1", "conversation": conversation or self.CONVERSATION,
            "user": {**self.JIMMY, "roles": roles or self.JIMMY["roles"]}, "action_id": "approve", "choice": choice,
            "selected_ids": None,
            "proposal": {"token": "orders-20260921-1500-ab12-v1", "kind": "draft_order", "items": prepared["items"], "payload": prepared["payload"]},
        })

    def test_act_success_returns_the_order(self):
        with mock.patch.object(self.main.entry, "execute", return_value={"status": "ok", "text": "Order #1", "result": {}}) as execute:
            response = self.act("create_unpaid")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(execute.call_args.args[2], "create_unpaid")

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


if __name__ == "__main__":
    unittest.main()

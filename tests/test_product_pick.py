"""Product picking, tested against products modelled on the real catalog (Arran 10 and friends)."""

import unittest
from unittest import mock

from orders_agent import product_pick as pick
from orders_agent.sources import product_search as search
from orders_agent.sources.shopify import ShopifyError

CONFIG = pick.load_config()
NO_FLAGS = dict(our_brands=False, ib_collection=False, special_collection=False, trade_core=False, trade_ibs=False, trade_special=False)


def product(title, handle, tags=(), stock=10, vendor="twl3.0", status="ACTIVE", variants=None, **flags):
    return {
        "id": f"gid://shopify/Product/{abs(hash(handle)) % 10**9}",
        "title": title, "handle": handle, "status": status, "vendor": vendor, "tags": list(tags),
        "flags": {**NO_FLAGS, **flags},
        "variants": variants if variants is not None else [
            {"id": f"gid://shopify/ProductVariant/{abs(hash(handle)) % 10**9}", "title": "The Whisky List Shop", "sku": "uuid uuid", "stock": stock}
        ],
    }


ARRAN10 = product("Arran 10 Year Old Single Malt Scotch Whisky", "arran-10", ["brand_Arran", "TWL Brand", "Popular", "abv_46"], 524, our_brands=True, trade_core=True)
BARLEY = product('Arran "Arran Barley" 10 Year Old Batch 001 Single Malt Scotch Whisky', "arran-barley", ["brand_Arran", "TWL Brand", "abv_50"], 0, trade_core=True)
BARLEY_IN_STOCK = product('Arran "Arran Barley" 10 Year Old Batch 001 Single Malt Scotch Whisky', "arran-barley", ["brand_Arran", "TWL Brand", "abv_50"], 12, trade_core=True)
ARRAN_SHERRY = product("Arran Sherry Cask Single Malt Scotch Whisky", "arran-sherry-cask-the-bodega-single-malt-scotch-whisky", ["brand_Arran", "TWL Brand", "abv_55"], 165, our_brands=True, trade_core=True)
ARRAN14 = product("Arran 14 Year Old Palo Cortado Sherry Cask Single Malt Scotch Whisky", "arran-14-palo", ["brand_Arran", "TWL Brand"], 247, trade_core=True)
ARRAN_IB = product("Adelphi 2014 Arran Peated 10 Year Old Single Cask Single Malt Scotch Whisky", "adelphi-arran-peated", ["brand_Adelphi", "TWL IB", "Independent Bottler"], 6, trade_ibs=True)
THOMPSON = product("Thompson Bros. 2011 Arran 10 Year Old Honeymoon Cask Single Malt Scotch Whisky", "thompson-arran", ["Independent Bottler"], 0)
SAMPLE_ARRAN10 = product("[SAMPLE] Arran 10 Year Old Single Malt Scotch Whisky", "sample-arran-10", ["baseproduct_x"], vendor="sample",
                         variants=[{"id": "gid://shopify/ProductVariant/1", "title": "50ml", "sku": "", "stock": 40}])
GIFT_PACK = product("Arran 10 Year Old with 2 Glasses Gift Pack Single Malt Scotch Whisky", "arran-gift", ["brand_Arran", "TWL Brand"], 30, trade_core=True)
GIFT_CARD = product("Whisky Gift Card", "gift-card", ["partnerStore_The Whisky List Shop"], 548)
BOTTLE_SPLIT = product("Rewards Members Exclusive Private Cask Bottle Split - Bunnahabhain Staoisha", "split", ["partnerStore_The Whisky List Shop"], 13)
GA12 = product("GlenAllachie 12 Year Old Single Malt Scotch Whisky [PRE-ORDER]", "glenallachie-12-year-old-single-malt-scotch-whisky", ["brand_GlenAllachie", "TWL Brand"], 217, our_brands=True)
GA12_PX = product("GlenAllachie 12 Year Old Pedro Ximenez Wood Single Malt Scotch Whisky", "ga12-px", ["brand_GlenAllachie", "TWL Brand"], 9, trade_core=True)
GA10CS_B6 = product("GlenAllachie 10 Year Old Cask Strength Batch 6 Single Malt Scotch Whisky", "ga10cs-b6", ["brand_GlenAllachie", "TWL Brand"], 0, trade_core=True)
GA10CS_B7 = product("GlenAllachie 10 Year Old Cask Strength Batch 7 Single Malt Scotch Whisky", "ga10cs-b7", ["brand_GlenAllachie", "TWL Brand"], 0, trade_core=True)
REMNANT = product("Remnant Whisky Co. Golden Fleece Australian Single Malt Whisky (500ml)", "remnant-golden-fleece-australian-single-malt-scotch-whisky-500ml", ["TWL Brand"], -19, trade_core=True)
INFINITE = product("Ardnahoe Infinite Loch Single Malt Scotch Whisky", "ardnahoe-infinite-loch-single-malt-scotch-whisky", ["brand_Ardnahoe", "TWL Brand"], 8, our_brands=True)
BHOLSA = product("Ardnahoe Bholsa Single Malt Scotch Whisky", "ardnahoe-bholsa-single-malt-scotch-whisky", ["brand_Ardnahoe", "TWL Brand"], 24, our_brands=True)
NOT_IN_RANGE = product("Random Bourbon 8 Year Old", "random-bourbon", [], 50)
TIER4 = product("Random Bourbon 12 Year Old", "tier4-bourbon", ["partnerStore_The Whisky List Shop"], 20)
SPECIAL = product("Special Edition Rye 15 Year Old", "special-rye", [], 5, trade_special=True)
IB_OTHER = product("Some Bottler Rye 15 Year Old", "ib-rye", ["TWL IB"], 5)
OWN_OTHER = product("Own Brand Rye 15 Year Old", "own-rye", ["TWL Brand"], 5)

BY_HANDLE = {p["handle"]: p for p in (ARRAN10, BARLEY, ARRAN_SHERRY, GA12, REMNANT, INFINITE, BHOLSA)}


def quick(*extra_products, **override):
    """What the quick order entries point at, by entry name."""
    lookup = {
        "Arran 10": [ARRAN10], "Arran Sherry": [ARRAN_SHERRY], "GlenAllachie 12": [GA12],
        "GlenAllachie 10 Cask Strength": [GA10CS_B6, GA10CS_B7], "Remnant Golden Fleece": [REMNANT],
        "Ardnahoe Infinite Loch": [INFINITE], "Ardnahoe Bholsa": [BHOLSA],
    }
    lookup.update(override)
    return lookup


def decide(query, pool, quick_products=None, include_inventory=False):
    return pick.decide(query, CONFIG, pool, quick_products if quick_products is not None else quick(), include_inventory)


def names(decision):
    return [option["name"] for option in decision.get("options", [])]


class TokenTests(unittest.TestCase):
    def test_filler_words_are_dropped(self):
        self.assertEqual(pick.tokens("Arran 10 Year Old"), ["arran", "10"])
        self.assertEqual(pick.tokens("arran 10yo"), ["arran", "10"])
        self.assertEqual(pick.tokens("GA12"), ["ga", "12"])
        self.assertEqual(pick.tokens("The Macallan 18 Single Malt"), ["macallan", "18"])
        self.assertEqual(pick.tokens("  "), [])
        self.assertEqual(pick.tokens("Bunnahabhain's Étoile"), ["bunnahabhain", "s", "etoile"])

    def test_matching(self):
        self.assertTrue(pick.title_matches(["arran", "10"], ARRAN10["title"]))
        self.assertFalse(pick.title_matches(["10"], "Whisky 2010 Edition"))            # a number is a whole number
        self.assertTrue(pick.title_matches(["glen", "allachie"], "GlenAllachie 12"))   # split word
        self.assertTrue(pick.title_matches(["glenallachie"], "Glen Allachie 12"))      # joined word
        self.assertFalse(pick.title_matches(["arran", "12"], ARRAN10["title"]))
        self.assertFalse(pick.title_matches(["ga"], "GlenAllachie 12"))                # short words must match exactly


class AssessTests(unittest.TestCase):
    def assess(self, product_):
        return pick.assess(product_, CONFIG)

    def test_the_core_product_is_tier_one_with_reasons(self):
        result = self.assess(ARRAN10)
        self.assertIsNone(result["exclusion"])
        self.assertEqual(result["tier"], 1)
        self.assertEqual(result["why"], ["Our Brands", "Trade Core", "TWL Brand"])
        self.assertTrue(result["popular"])
        self.assertEqual(result["brand_rank"], 0)

    def test_the_four_ranges(self):
        self.assertEqual(self.assess(OWN_OTHER)["tier"], 1)     # the TWL Brand tag alone
        self.assertEqual(self.assess(IB_OTHER)["tier"], 2)      # the TWL IB tag alone
        self.assertEqual(self.assess(ARRAN_IB)["tier"], 2)      # the Trade IBs catalog
        self.assertEqual(self.assess(SPECIAL)["tier"], 3)       # the Trade Special Releases catalog
        self.assertEqual(self.assess(product("x", "x", ["productGroup_twl-exclusive"]))["tier"], 3)
        self.assertEqual(self.assess(product("x", "x", ["ib"], ib_collection=True))["tier"], 2)
        self.assertEqual(self.assess(product("x", "x", [], special_collection=True))["tier"], 3)
        self.assertEqual(self.assess(TIER4)["tier"], 4)
        self.assertEqual(self.assess(BARLEY)["tier"], 1)

    def test_the_best_range_wins_when_there_are_several(self):
        both = product("x", "x", ["TWL IB", "TWL Brand"])
        self.assertEqual(self.assess(both)["tier"], 1)

    def test_what_is_never_offered(self):
        expected = {
            "sample": SAMPLE_ARRAN10, "gift": GIFT_PACK, "bottle_split": BOTTLE_SPLIT,
            "no_stock": BARLEY, "not_in_a_twl_range": NOT_IN_RANGE,
        }
        for reason, item in expected.items():
            self.assertEqual(self.assess(item)["exclusion"], reason, reason)
        self.assertEqual(self.assess(GIFT_CARD)["exclusion"], "gift")            # in stock (548), but a gift card
        self.assertEqual(self.assess(REMNANT)["exclusion"], "no_stock")           # oversold: -19 is no stock
        self.assertEqual(self.assess(product("x", "x", ["TWL Brand"], status="DRAFT"))["exclusion"], "inactive")
        self.assertEqual(self.assess(product("x", "x", ["TWL Brand"], status="ARCHIVED"))["exclusion"], "inactive")

    def test_a_sample_is_caught_by_vendor_even_without_the_title_tag(self):
        self.assertEqual(self.assess(product("Arran 10", "s", ["TWL Brand"], vendor="Sample"))["exclusion"], "sample")

    def test_only_variants_with_stock_are_offered(self):
        multi = product("Multi", "multi", ["TWL Brand"], variants=[
            {"id": "v1", "title": "700ml", "sku": "", "stock": 5}, {"id": "v2", "title": "1L", "sku": "", "stock": 0}, {"id": "v3", "title": "50ml", "sku": "", "stock": -2}])
        result = self.assess(multi)
        self.assertEqual([v["id"] for v in result["variants"]], ["v1"])

    def test_priority_brands_are_recognised_case_insensitively(self):
        self.assertEqual(self.assess(product("x", "x", ["TWL Brand", "brand_glenallachie"]))["brand_rank"], 1)
        self.assertEqual(self.assess(product("x", "x", ["TWL Brand", "brand_Ardnamurchan"]))["brand_rank"], 3)
        self.assertEqual(self.assess(product("x", "x", ["TWL Brand", "brand_Macallan"]))["brand_rank"], pick.NOT_FOUND)


class QuickOrderTests(unittest.TestCase):
    def test_arran_10_picks_the_core_bottling_even_though_a_dozen_things_match(self):
        pool = [ARRAN_IB, ARRAN10, SAMPLE_ARRAN10, GIFT_PACK, BARLEY_IN_STOCK, ARRAN14]
        for typed in ("Arran 10", "arran 10", "Arran 10 Year Old", "arran 10yo", "ARRAN 10 year old single malt"):
            result = decide(typed, pool)
            self.assertEqual(result["decision"], "use", typed)
            self.assertEqual(result["choice"]["name"], ARRAN10["title"], typed)
            self.assertIn("Quick order list: Arran 10", result["choice"]["why"])

    def test_the_other_arran_10_is_not_picked_just_because_it_is_also_tier_one(self):
        result = decide("Arran 10", [BARLEY_IN_STOCK, ARRAN10])
        self.assertEqual(result["choice"]["name"], ARRAN10["title"])

    def test_arran_sherry_is_the_quick_entry_not_the_other_sherry_arran(self):
        result = decide("Arran Sherry", [ARRAN14, ARRAN_SHERRY])
        self.assertEqual(result["choice"]["name"], ARRAN_SHERRY["title"])

    def test_only_part_of_a_name_asks_and_lists_the_quick_order_products_first(self):
        result = decide("arran", [ARRAN14, ARRAN_IB, ARRAN_SHERRY, ARRAN10])
        self.assertEqual(result["decision"], "ask")
        self.assertEqual(names(result)[:2], [ARRAN10["title"], ARRAN_SHERRY["title"]])   # the quick order list, in its order
        self.assertEqual(names(result)[2], ARRAN14["title"])                              # then other Arran
        self.assertEqual(names(result)[3], ARRAN_IB["title"])                             # then the rest
        self.assertIn("Do NOT choose", result["guidance"])

    def test_a_generic_phrase_that_only_partly_matches_a_quick_entry_does_not_choose(self):
        # "sherry cask" is part of "Arran Sherry", but names no brand: Arran 14 is a sherry cask too.
        result = decide("sherry cask", [ARRAN_SHERRY, ARRAN14])
        self.assertEqual(result["decision"], "ask")
        self.assertEqual(names(result), [ARRAN_SHERRY["title"], ARRAN14["title"]])   # but the quick one is listed first

    def test_two_quick_names_typed_together_ask(self):
        result = decide("arran 10 sherry", [ARRAN10, ARRAN_SHERRY])
        self.assertEqual(result["decision"], "ask")

    def test_ardnahoe_asks_between_the_two_and_bholsa_alone_is_used(self):
        self.assertEqual(decide("ardnahoe", [INFINITE, BHOLSA])["decision"], "ask")
        result = decide("bholsa", [BHOLSA])
        self.assertEqual((result["decision"], result["choice"]["name"]), ("use", BHOLSA["title"]))
        self.assertEqual(decide("Infinite Loch", [INFINITE])["choice"]["name"], INFINITE["title"])

    def test_glenallachie_12_is_used_and_the_pre_order_is_flagged(self):
        result = decide("GlenAllachie 12", [GA12, GA12_PX])
        self.assertEqual(result["decision"], "use")
        self.assertEqual(result["choice"]["warning"], "This is a pre-order product.")
        for typed in ("glen allachie 12", "GA12", "ga 12", "glenallachie 12yo"):
            self.assertEqual(decide(typed, [GA12, GA12_PX])["decision"], "use", typed)

    def test_an_out_of_stock_quick_product_is_reported_and_no_substitute_is_chosen(self):
        for query, entry, pool in (
            ("GlenAllachie 10 Cask Strength", "GlenAllachie 10 Cask Strength", []),
            ("Remnant Golden Fleece", "Remnant Golden Fleece", []),
            ("golden fleece", "Remnant Golden Fleece", []),
        ):
            result = decide(query, pool)
            self.assertEqual(result["decision"], "none", query)
            self.assertIn(f"{entry} is out of stock.", result["unavailable"])
            self.assertNotIn("choice", result)

    def test_alternatives_are_offered_but_never_chosen_when_the_named_one_is_out_of_stock(self):
        result = decide("GlenAllachie 12", [GA12_PX], quick(**{"GlenAllachie 12": [product(GA12["title"], GA12["handle"], GA12["tags"], 0, our_brands=True)]}))
        self.assertEqual(result["decision"], "ask")
        self.assertEqual(names(result), [GA12_PX["title"]])
        self.assertIn("GlenAllachie 12 is out of stock.", result["unavailable"])
        self.assertIn("isn't available", result["guidance"])

    def test_a_quick_entry_that_points_at_nothing_says_so(self):
        result = decide("Arran 10", [], quick(**{"Arran 10": []}))
        self.assertEqual(result["decision"], "none")
        self.assertTrue(any("can't find it in Shopify" in line for line in result["unavailable"]))

    def test_a_batch_that_rotates_is_found_by_pattern(self):
        in_stock = product(GA10CS_B7["title"], "ga10cs-b7", ["brand_GlenAllachie", "TWL Brand"], 30, trade_core=True)
        result = decide("GlenAllachie 10 Cask Strength", [], quick(**{"GlenAllachie 10 Cask Strength": [GA10CS_B6, in_stock]}))
        self.assertEqual(result["decision"], "use")
        self.assertIn("Batch 7", result["choice"]["name"])
        two = product(GA10CS_B6["title"], "ga10cs-b6", ["brand_GlenAllachie", "TWL Brand"], 4, trade_core=True)
        self.assertEqual(decide("GlenAllachie 10 Cask Strength", [], quick(**{"GlenAllachie 10 Cask Strength": [two, in_stock]}))["decision"], "ask")


class RankingTests(unittest.TestCase):
    def test_samples_gift_packs_splits_and_cards_are_never_options(self):
        pool = [SAMPLE_ARRAN10, GIFT_PACK, GIFT_CARD, BOTTLE_SPLIT, NOT_IN_RANGE, BARLEY]
        for typed in ("arran 10", "gift", "split", "bourbon", "sample arran"):
            self.assertEqual(decide(typed, pool, {})["decision"], "none", typed)

    def test_ranges_order_the_options(self):
        pool = [TIER4, IB_OTHER, SPECIAL, OWN_OTHER]
        ranked = decide("rye", pool, {})
        self.assertEqual(ranked["decision"], "ask")                 # one word typed: never auto-pick from a longer list
        self.assertEqual(names(ranked), [OWN_OTHER["title"], IB_OTHER["title"], SPECIAL["title"]])

    def test_a_clear_winner_on_range_is_used_when_enough_was_typed(self):
        result = decide("rye 15", [IB_OTHER, OWN_OTHER, SPECIAL], {})
        self.assertEqual(result["decision"], "use")
        self.assertEqual(result["choice"]["name"], OWN_OTHER["title"])

    def test_equal_candidates_ask_even_with_a_long_query(self):
        a = product("Own Brand Rye 15 Year Old Cask A", "a", ["TWL Brand"], 5)
        b = product("Own Brand Rye 15 Year Old Cask B", "b", ["TWL Brand"], 5)
        result = decide("own brand rye 15", [a, b], {})
        self.assertEqual(result["decision"], "ask")
        self.assertEqual(len(result["options"]), 2)

    def test_priority_brands_come_before_the_ranges(self):
        arran_tier4 = product("Arran Whatever 12 Year Old", "aw", ["brand_Arran", "partnerStore_The Whisky List Shop"], 5)
        other_tier1 = product("Other Whatever 12 Year Old", "ow", ["TWL Brand"], 5)
        result = decide("whatever 12", [other_tier1, arran_tier4], {})
        self.assertEqual(result["decision"], "use")
        self.assertEqual(result["choice"]["name"], arran_tier4["title"])

    def test_a_single_candidate_is_used_even_for_one_word(self):
        self.assertEqual(decide("adelphi", [ARRAN_IB], {})["decision"], "use")

    def test_popular_breaks_a_tie_but_does_not_make_a_winner(self):
        popular = product("Thing 12 Year Old Popular", "p", ["TWL Brand", "Popular"], 5)
        plain = product("Thing 12 Year Old Plain", "q", ["TWL Brand"], 5)
        result = decide("thing 12", [plain, popular], {})
        self.assertEqual(result["decision"], "ask")
        self.assertEqual(names(result)[0], popular["title"])

    def test_options_are_capped_and_the_rest_counted(self):
        pool = [product(f"Widget {n} Bottling", f"w{n}", ["TWL Brand"], 5) for n in range(8)]
        result = decide("widget", pool, {})
        self.assertEqual(len(result["options"]), 5)
        self.assertEqual(result["more_matches"], 3)

    def test_several_sizes_of_one_product_ask_which_size(self):
        sized = product("Sizey Malt", "sizey", ["TWL Brand"], variants=[
            {"id": "a", "title": "700ml", "sku": "", "stock": 5}, {"id": "b", "title": "1L", "sku": "", "stock": 3}])
        result = decide("sizey malt", [sized], {})
        self.assertEqual(result["decision"], "ask")
        self.assertEqual(names(result), ["Sizey Malt (700ml)", "Sizey Malt (1L)"])

    def test_stock_numbers_only_with_the_inventory_capability(self):
        without = decide("Arran 10", [ARRAN10])["choice"]
        self.assertNotIn("in_stock", without)
        with_stock = decide("Arran 10", [ARRAN10], include_inventory=True)["choice"]
        self.assertEqual(with_stock["in_stock"], 524)

    def test_no_query_is_refused(self):
        for empty in ("", "   ", "year old"):
            with self.assertRaises(ShopifyError):
                decide(empty, [])

    def test_the_option_carries_what_helps_a_person_choose(self):
        option = decide("arran", [ARRAN10, ARRAN14])["options"][0]
        self.assertEqual(option["brand"], "Arran")
        self.assertEqual(option["abv"], "46%")
        self.assertIn("Our Brands", option["why"])
        self.assertIn("Popular", option["why"])


class SearchTests(unittest.TestCase):
    def test_a_search_can_only_contain_title_words(self):
        query = search.title_query(["arran", "10)", "x OR title:*", "tag:secret", "é"])
        # Punctuation is stripped and an emptied word is dropped, so nothing typed can add a filter.
        self.assertEqual(
            query,
            "title:*arran* AND title:*10* AND title:*xortitle* AND title:*tagsecret* AND status:active AND inventory_total:>0",
        )
        self.assertNotIn(":secret", query)
        self.assertNotIn(" OR ", query)

    def test_searching_without_stock_or_words(self):
        self.assertNotIn("inventory_total", search.title_query(["arran"], in_stock=False))
        for empty in ([], [""], ["!!"]):
            with self.assertRaises(ShopifyError):
                search.title_query(empty)

    def test_handles_are_cleaned(self):
        self.assertEqual(search.handles_query(["arran-10", "Bad Handle; drop"]), "handle:arran-10 OR handle:badhandledrop")

    def test_sources_are_resolved_by_name_cached_and_a_missing_one_refuses(self):
        search.clear_cache()
        self.addCleanup(search.clear_cache)
        good = {
            "collections": {"nodes": [{"id": "c1", "title": "Our Brands"}, {"id": "c2", "title": "TWL Independent Bottlers"}, {"id": "c3", "title": "Special Releases"}]},
            "catalogs": {"nodes": [{"title": "Trade Core", "publication": {"id": "p1"}}, {"title": "Trade IBs", "publication": {"id": "p2"}}, {"title": "Trade Special Releases", "publication": {"id": "p3"}}]},
        }
        with mock.patch.object(search, "graphql", return_value=good) as graphql:
            first = search.resolve_sources(CONFIG)
            search.resolve_sources(CONFIG)
        self.assertEqual(graphql.call_count, 1)   # cached
        self.assertEqual((first["ourBrands"], first["tradeCore"], first["tradeSpecial"]), ("c1", "p1", "p3"))

        search.clear_cache()
        for broken in (
            {**good, "collections": {"nodes": good["collections"]["nodes"][:2]}},                                    # a collection is gone
            {**good, "catalogs": {"nodes": good["catalogs"]["nodes"][:2]}},                                          # a catalog is gone
            {**good, "collections": {"nodes": good["collections"]["nodes"] + [{"id": "c9", "title": "Our Brands"}]}},  # ambiguous
        ):
            with mock.patch.object(search, "graphql", return_value=broken):
                with self.assertRaises(ShopifyError) as caught:
                    search.resolve_sources(CONFIG)
            self.assertIn("Nothing was guessed", str(caught.exception))
            search.clear_cache()


class FindForOrderTests(unittest.TestCase):
    """The orchestration: which Shopify searches are made, with search faked."""

    def run_find(self, query, pool, by_handle=None, everything=None, include_inventory=False):
        calls = []

        def fake_search(config, shopify_query, limit=50):
            calls.append(shopify_query)
            if shopify_query.startswith("handle:"):
                return [p for p in (by_handle if by_handle is not None else BY_HANDLE).values() if f"handle:{p['handle']}" in shopify_query]
            if "inventory_total:>0" in shopify_query:
                return pool
            return everything if everything is not None else []

        with mock.patch.object(search, "search", side_effect=fake_search):
            return pick.find_for_order(query, include_inventory), calls

    def test_arran_10_end_to_end(self):
        result, calls = self.run_find("Arran 10 Year Old", [ARRAN10, ARRAN_IB, BARLEY_IN_STOCK])
        self.assertEqual(result["decision"], "use")
        self.assertEqual(result["choice"]["name"], ARRAN10["title"])
        self.assertTrue(any(call == "handle:arran-10" for call in calls))                                  # the quick entry by handle
        self.assertTrue(any("title:*arran* AND title:*10*" in call and "inventory_total:>0" in call for call in calls))

    def test_an_unknown_product_says_nothing_matched_and_why(self):
        oos = [product("Macallan 18 Year Old", "mac18", ["TWL Brand"], 0, trade_core=True)]
        result, calls = self.run_find("Macallan 18", [], everything=oos)
        self.assertEqual(result["decision"], "none")
        self.assertTrue(any("Matched but out of stock: Macallan 18 Year Old" in line for line in result["unavailable"]))

    def test_a_pattern_entry_searches_by_name_and_filters_by_title(self):
        by_handle = dict(BY_HANDLE)
        result, calls = self.run_find("glenallachie 10 cs", [], everything=[GA10CS_B6, GA10CS_B7, GA12])
        self.assertEqual(result["decision"], "none")
        self.assertIn("GlenAllachie 10 Cask Strength is out of stock.", result["unavailable"])

    def test_a_full_pool_is_flagged_as_possibly_incomplete(self):
        pool = [product(f"Widget {n}", f"w{n}", ["TWL Brand"], 5) for n in range(pick.POOL_SIZE)]
        result, _ = self.run_find("widget", pool)
        self.assertIn("some may not be shown", result["note"])

    def test_no_words_never_reaches_shopify(self):
        with mock.patch.object(search, "search") as searched:
            with self.assertRaises(ShopifyError):
                pick.find_for_order("year old")
        searched.assert_not_called()


class ConfigTests(unittest.TestCase):
    def test_the_shipped_config_is_complete(self):
        self.assertEqual([e["name"] for e in CONFIG["quick_order"]], [
            "Arran 10", "Arran Sherry", "GlenAllachie 12", "GlenAllachie 10 Cask Strength",
            "Remnant Golden Fleece", "Ardnahoe Infinite Loch", "Ardnahoe Bholsa"])
        self.assertEqual(CONFIG["priority_brands"], ["Arran", "GlenAllachie", "Ardnahoe", "Ardnamurchan"])
        for entry in CONFIG["quick_order"]:
            self.assertTrue(entry["_aliases"], entry["name"])
            self.assertTrue(entry.get("handles") or entry.get("title_pattern"), entry["name"])
            # The entry's own name is always one of its aliases, so typing it exactly always works.
            self.assertIn(frozenset(pick.tokens(entry["name"])), entry["_aliases"], entry["name"])

    def test_aliases_do_not_collide_between_entries(self):
        seen = {}
        for entry in CONFIG["quick_order"]:
            for alias in entry["_aliases"]:
                self.assertNotIn(alias, seen, f"{entry['name']} and {seen.get(alias)} share an alias")
                seen[alias] = entry["name"]

    def test_the_gift_and_split_patterns_do_what_they_should(self):
        patterns = CONFIG["_patterns"]
        self.assertTrue(patterns["gift"].search("Aberlour 12 Gift Set"))
        self.assertTrue(patterns["gift"].search("Whisky Gift Card"))
        self.assertFalse(patterns["gift"].search("Gifford Rye"))
        self.assertTrue(patterns["bottle_split"].search("Private Cask Bottle Split - Bunna"))
        self.assertTrue(patterns["bottle_split"].search("[BOTTLE SPLIT] Something"))
        self.assertFalse(patterns["bottle_split"].search("Jazzed Up Banana Split 26"))
        self.assertTrue(patterns["sample"].search("[SAMPLE] Arran 10"))
        self.assertFalse(patterns["sample"].search("Sampler Pack"))


class ToolTests(unittest.TestCase):
    def test_find_variant_takes_only_a_query_and_asks_for_no_sku(self):
        import inspect
        import re
        from orders_agent.tools import build_server
        source = inspect.getsource(build_server)
        block = source[source.index('"find_variant",\n            "Find the product'):]
        block = block[: block.index("find_variant,\n        )")]
        properties = set(re.findall(r'"(\w+)": \{\*\*STRING', block))
        self.assertEqual(properties, {"query"})
        self.assertIn("SKUs are not usable", block)


if __name__ == "__main__":
    unittest.main()

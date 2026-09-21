"""Product picking, tested against products modelled on the real catalog (Arran 10 and friends)."""

import unittest
from unittest import mock

from orders_agent import product_pick as pick
from orders_agent.sources import product_search as search
from orders_agent.sources.shopify import ShopifyError

CONFIG = pick.load_config()
NO_FLAGS = dict(our_brands=False, ib_collection=False, special_collection=False, trade_core=False, trade_ibs=False, trade_special=False)


def product(title, handle, tags=(), stock=10, vendor="twl3.0", status="ACTIVE", variants=None, pid=None, eta=None, **flags):
    return {
        "id": pid or f"gid://shopify/Product/{abs(hash(handle)) % 10**9}",
        "title": title, "handle": handle, "status": status, "vendor": vendor, "tags": list(tags),
        "pre_order_eta": eta,
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
GA12 = product("GlenAllachie 12 Year Old Single Malt Scotch Whisky [PRE-ORDER]", "glenallachie-12-year-old-single-malt-scotch-whisky", ["brand_GlenAllachie", "TWL Brand", "pre-order"], 217, our_brands=True, eta="2026-10-16")
GA10CS_B13 = product("GlenAllachie 10 Year Old Cask Strength Batch 13 Single Malt Scotch Whisky [PRE-ORDER]", "glenallachie-10-year-old-cask-strength-batch-13-single-malt-scotch-whisky-pre-order",
                     ["brand_GlenAllachie", "TWL Brand", "pre-order", "abv_60"], 82, pid="gid://shopify/Product/8436830666826", eta="2026-10-16")
# In a TWL range (Our Brands) but NOT tagged TWL Brand, so with no stock it is not offered.
OOS_NON_BRAND = product("Ardnamurchan AD 10 Year Old Single Malt Scotch Whisky", "ardnamurchan-ad", ["brand_Ardnamurchan"], 0, our_brands=True)
OOS_BHOLSA = product("Ardnahoe Bholsa Single Malt Scotch Whisky", "bholsa-oos", ["brand_Ardnahoe"], 0, our_brands=True)
OOS_GA12 = product("GlenAllachie 12 Year Old Single Malt Scotch Whisky", "ga12-oos", ["brand_GlenAllachie"], 0, our_brands=True)
GA12_PX = product("GlenAllachie 12 Year Old Pedro Ximenez Wood Single Malt Scotch Whisky", "ga12-px", ["brand_GlenAllachie", "TWL Brand"], 9, trade_core=True)
GA10CS_B6 = product("GlenAllachie 10 Year Old Cask Strength Batch 6 Single Malt Scotch Whisky", "ga10cs-b6", ["brand_GlenAllachie", "TWL Brand"], 0, trade_core=True)
GA10CS_B7 = product("GlenAllachie 10 Year Old Cask Strength Batch 7 Single Malt Scotch Whisky", "ga10cs-b7", ["brand_GlenAllachie", "TWL Brand"], 0, trade_core=True)
REMNANT = product("Remnant Whisky Co. Golden Fleece Australian Single Malt Whisky (500ml)", "remnant-golden-fleece-australian-single-malt-scotch-whisky-500ml", ["TWL Brand"], -19, trade_core=True)
INFINITE = product("Ardnahoe Infinite Loch Single Malt Scotch Whisky", "ardnahoe-infinite-loch-single-malt-scotch-whisky", ["brand_Ardnahoe", "TWL Brand"], 8, our_brands=True)
BHOLSA = product("Ardnahoe Bholsa Single Malt Scotch Whisky", "ardnahoe-bholsa-single-malt-scotch-whisky", ["brand_Ardnahoe", "TWL Brand"], 24, our_brands=True)
NOT_IN_RANGE = product("Random Bourbon 8 Year Old", "random-bourbon", [], 50)
TIER4_OOS = product("Random Rum 5 Year Old", "tier4-rum", ["partnerStore_The Whisky List Shop"], 0)
TIER4 = product("Random Bourbon 12 Year Old", "tier4-bourbon", ["partnerStore_The Whisky List Shop"], 20)
SPECIAL = product("Special Edition Rye 15 Year Old", "special-rye", [], 5, trade_special=True)
IB_OTHER = product("Some Bottler Rye 15 Year Old", "ib-rye", ["TWL IB"], 5)
OWN_OTHER = product("Own Brand Rye 15 Year Old", "own-rye", ["TWL Brand"], 5)

BY_HANDLE = {p["handle"]: p for p in (ARRAN10, BARLEY, ARRAN_SHERRY, GA12, GA10CS_B13, REMNANT, INFINITE, BHOLSA)}


def quick(*extra_products, **override):
    """What the quick order entries point at, by entry name."""
    lookup = {
        "Arran 10": [ARRAN10], "Arran Sherry": [ARRAN_SHERRY], "GlenAllachie 12": [GA12],
        "GlenAllachie 10 Cask Strength": [GA10CS_B13], "Remnant Golden Fleece": [REMNANT],
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
        self.assertEqual(pick.tokens("GA12"), ["glenallachie", "12"])       # ga is a brand code
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
            "no_stock": OOS_NON_BRAND, "not_in_a_twl_range": NOT_IN_RANGE,
        }
        for reason, item in expected.items():
            self.assertEqual(self.assess(item)["exclusion"], reason, reason)
        self.assertEqual(self.assess(GIFT_CARD)["exclusion"], "gift")            # in stock (548), but a gift card
        self.assertEqual(self.assess(product("x", "x", ["TWL Brand"], status="DRAFT"))["exclusion"], "inactive")
        self.assertEqual(self.assess(product("x", "x", ["TWL Brand"], status="ARCHIVED"))["exclusion"], "inactive")

    def test_twl_brand_products_are_offered_out_of_stock_but_only_those(self):
        for out_of_stock in (BARLEY, REMNANT):                     # zero stock, and oversold (-19)
            result = self.assess(out_of_stock)
            self.assertIsNone(result["exclusion"], out_of_stock["title"])
            self.assertEqual(len(result["variants"]), 1)
            self.assertLessEqual(result["variants"][0]["stock"], 0)
        # The same with no stock is NOT offered when it isn't tagged TWL Brand, whatever range it is in.
        for other in (OOS_NON_BRAND, product("x", "x", ["TWL IB"], 0), product("x", "x", [], 0, trade_special=True), TIER4_OOS):
            self.assertEqual(self.assess(other)["exclusion"], "no_stock", other["title"])

    def test_an_in_stock_variant_hides_out_of_stock_ones_even_for_twl_brand(self):
        mixed = product("Mixed", "mixed", ["TWL Brand"], variants=[
            {"id": "a", "title": "700ml", "sku": "", "stock": 5}, {"id": "b", "title": "1L", "sku": "", "stock": 0}])
        self.assertEqual([v["id"] for v in self.assess(mixed)["variants"]], ["a"])

    def test_pre_orders_are_recognised_by_tag_or_title_and_carry_the_eta(self):
        result = self.assess(GA12)
        self.assertTrue(result["pre_order"])
        self.assertEqual(result["eta"], "2026-10-16")
        self.assertTrue(self.assess(product("Thing [PRE-ORDER]", "t", ["TWL Brand"]))["pre_order"])
        self.assertTrue(self.assess(product("Thing", "t", ["TWL Brand", "pre-order"]))["pre_order"])
        self.assertFalse(self.assess(ARRAN10)["pre_order"])

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

    def test_two_quick_names_typed_together_offer_both_as_the_closest(self):
        result = decide("arran 10 sherry", [ARRAN10, ARRAN_SHERRY])
        self.assertEqual(result["decision"], "ask")
        self.assertEqual(names(result), [ARRAN10["title"], ARRAN_SHERRY["title"]])
        self.assertIn("closest on the quick order list", result["guidance"])

    def test_ardnahoe_asks_between_the_two_and_bholsa_alone_is_used(self):
        self.assertEqual(decide("ardnahoe", [INFINITE, BHOLSA])["decision"], "ask")
        result = decide("bholsa", [BHOLSA])
        self.assertEqual((result["decision"], result["choice"]["name"]), ("use", BHOLSA["title"]))
        self.assertEqual(decide("Infinite Loch", [INFINITE])["choice"]["name"], INFINITE["title"])

    def test_glenallachie_12_is_used_and_the_pre_order_is_flagged(self):
        result = decide("GlenAllachie 12", [GA12, GA12_PX])
        self.assertEqual(result["decision"], "use")
        self.assertTrue(result["choice"]["pre_order"])
        self.assertEqual(result["choice"]["eta"], "16 Oct 2026")
        self.assertEqual(result["choice"]["warnings"], ["Pre-order product, ETA 16 Oct 2026."])
        self.assertNotIn("out_of_stock", result["choice"])
        for typed in ("glen allachie 12", "GA12", "ga 12", "glenallachie 12yo"):
            self.assertEqual(decide(typed, [GA12, GA12_PX])["decision"], "use", typed)

    def test_glenallachie_10_cask_strength_is_the_pinned_pre_order_batch(self):
        for typed in ("GlenAllachie 10 Cask Strength", "glenallachie 10 cs", "GA 10 CS", "glen allachie 10 cask strength"):
            result = decide(typed, [])
            self.assertEqual(result["decision"], "use", typed)
            self.assertEqual(result["choice"]["name"], GA10CS_B13["title"])
            self.assertEqual(result["choice"]["eta"], "16 Oct 2026")
            self.assertNotIn("out_of_stock", result["choice"])          # 82 allocated, so in stock

    def test_an_out_of_stock_twl_brand_quick_product_is_offered_and_flagged(self):
        for typed in ("Remnant Golden Fleece", "golden fleece"):
            result = decide(typed, [])
            self.assertEqual(result["decision"], "use", typed)
            self.assertEqual(result["choice"]["name"], REMNANT["title"])
            self.assertTrue(result["choice"]["out_of_stock"])
            self.assertIn("Out of stock", result["choice"]["warnings"][0])
            self.assertEqual(result["unavailable"], [])

    def test_a_non_twl_brand_out_of_stock_quick_product_is_reported_and_not_replaced(self):
        entry_products = quick(**{"Ardnahoe Bholsa": [OOS_BHOLSA]})
        result = decide("Ardnahoe Bholsa", [], entry_products)
        self.assertEqual(result["decision"], "none")
        self.assertIn("Ardnahoe Bholsa is out of stock.", result["unavailable"])
        self.assertNotIn("choice", result)

    def test_alternatives_are_offered_but_never_chosen_when_the_named_one_is_unavailable(self):
        result = decide("GlenAllachie 12", [GA12_PX], quick(**{"GlenAllachie 12": [OOS_GA12]}))
        self.assertEqual(result["decision"], "ask")
        self.assertEqual(names(result), [GA12_PX["title"]])
        self.assertIn("GlenAllachie 12 is out of stock.", result["unavailable"])
        self.assertIn("isn't available", result["guidance"])

    def test_a_quick_entry_that_points_at_nothing_says_so(self):
        result = decide("Arran 10", [], quick(**{"Arran 10": []}))
        self.assertEqual(result["decision"], "none")
        self.assertTrue(any("can't find it in Shopify" in line for line in result["unavailable"]))

    def test_older_out_of_stock_batches_do_not_muddy_the_pinned_choice(self):
        # Batches 6 and 7 are tagged TWL Brand and out of stock, so they are now offered as options for a
        # loose search, but the quick order entry names Batch 13 and that is what "GlenAllachie 10 Cask
        # Strength" gives.
        result = decide("GlenAllachie 10 Cask Strength", [GA10CS_B6, GA10CS_B7, GA10CS_B13])
        self.assertEqual((result["decision"], result["choice"]["name"]), ("use", GA10CS_B13["title"]))
        loose = decide("glenallachie cask strength", [GA10CS_B6, GA10CS_B7, GA10CS_B13])
        self.assertEqual(loose["decision"], "ask")
        self.assertEqual(names(loose)[0], GA10CS_B13["title"])                 # the in-stock, pinned one first
        self.assertTrue(all(o.get("out_of_stock") for o in loose["options"][1:]))


class RankingTests(unittest.TestCase):
    def test_samples_gift_packs_splits_and_cards_are_never_options(self):
        pool = [SAMPLE_ARRAN10, GIFT_PACK, GIFT_CARD, BOTTLE_SPLIT, NOT_IN_RANGE, OOS_NON_BRAND, TIER4_OOS]
        for typed in ("arran 10", "gift", "split", "bourbon", "sample arran", "ardnamurchan", "rum"):
            self.assertEqual(decide(typed, pool, {})["decision"], "none", typed)

    def test_a_lone_out_of_stock_twl_brand_product_is_offered_and_flagged(self):
        result = decide("arran barley", [BARLEY], {})
        self.assertEqual(result["decision"], "use")
        self.assertTrue(result["choice"]["out_of_stock"])

    def test_in_stock_options_come_before_out_of_stock_ones(self):
        result = decide("arran", [BARLEY, ARRAN_IB, ARRAN14], {})
        self.assertEqual(result["decision"], "ask")
        self.assertEqual(names(result), [ARRAN14["title"], ARRAN_IB["title"], BARLEY["title"]])
        self.assertTrue(result["options"][2]["out_of_stock"])
        self.assertNotIn("out_of_stock", result["options"][0])

    def test_being_in_stock_does_not_make_a_winner(self):
        # "Arran 14" could mean the out-of-stock core Arran 14 or the in-stock Palo Cortado. Ask, don't pick.
        core14 = product("Arran 14 Year Old Single Malt Scotch Whisky", "arran-14", ["brand_Arran", "TWL Brand"], 0, trade_core=True)
        result = decide("arran 14", [core14, ARRAN14], {})
        self.assertEqual(result["decision"], "ask")
        self.assertEqual(names(result), [ARRAN14["title"], core14["title"]])   # in stock listed first, both offered

    def test_a_pre_order_without_an_eta_says_so(self):
        no_eta = product("New Thing [PRE-ORDER]", "nt", ["TWL Brand", "pre-order"], 10)
        self.assertEqual(decide("new thing", [no_eta], {})["choice"]["warnings"], ["Pre-order product, no ETA set."])

    def test_an_out_of_stock_pre_order_carries_both_flags(self):
        both = product("Late Thing [PRE-ORDER]", "lt", ["TWL Brand", "pre-order"], 0, eta="2026-12-01")
        option = decide("late thing", [both], {})["choice"]
        self.assertTrue(option["out_of_stock"] and option["pre_order"])
        self.assertEqual(option["eta"], "1 Dec 2026")
        self.assertEqual(len(option["warnings"]), 2)

    def test_eta_dates_read_the_way_people_say_them(self):
        self.assertEqual(pick.format_eta("2026-10-16"), "16 Oct 2026")
        self.assertEqual(pick.format_eta("2026-01-05"), "5 Jan 2026")
        self.assertEqual(pick.format_eta("2026-10-16T00:00:00Z"), "16 Oct 2026")
        self.assertEqual(pick.format_eta("TBC"), "TBC")

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

    def test_product_ids_are_reduced_to_numbers(self):
        self.assertEqual(
            search.ids_query(["gid://shopify/Product/8436830666826", "123", "x; drop 9"]),
            "id:8436830666826 OR id:123 OR id:9",
        )

    def test_the_out_of_stock_search_is_limited_to_the_configured_tags(self):
        query = search.out_of_stock_query(["arran"], ["TWL Brand"])
        self.assertEqual(query, "title:*arran* AND status:active AND inventory_total:<=0 AND (tag:'TWL Brand')")
        two = search.out_of_stock_query(["arran"], ["A'B", "C"])
        self.assertEqual(two.count("tag:"), 2)
        self.assertNotIn("A'B", two)       # a quote can't break out of the tag filter
        with self.assertRaises(ShopifyError):
            search.out_of_stock_query(["arran"], [])

    def test_the_collection_is_found_by_exact_title_read_in_manual_order_and_cached(self):
        search.clear_cache()
        search._quick_collection.clear()
        self.addCleanup(search._quick_collection.clear)
        self.addCleanup(search.clear_cache)
        node = lambda pid, title: {"id": pid, "title": title, "handle": pid, "variants": {"nodes": []}}
        responses = {
            search.FIND_COLLECTION: {"collections": {"nodes": [{"id": "gid://c/1", "title": "Popular Trade Products"}, {"id": "gid://c/2", "title": "Popular Trade Products Old"}]}},
            search.COLLECTION_PRODUCTS: {"collection": {"title": "Popular Trade Products", "sortOrder": "MANUAL",
                                                        "products": {"nodes": [node("p2", "Second"), node("p1", "First")]}}},
        }
        calls = []

        def fake_graphql(query, variables=None):
            calls.append((query, variables))
            return responses[query]

        with mock.patch.object(search, "graphql", side_effect=fake_graphql), \
             mock.patch.object(search, "resolve_sources", return_value={"ourBrands": "x", "ibCollection": "x", "specialCollection": "x", "tradeCore": "x", "tradeIbs": "x", "tradeSpecial": "x"}):
            first = search.collection_products(CONFIG, "Popular Trade Products")
            search.collection_products(CONFIG, "Popular Trade Products")
        self.assertEqual([p["title"] for p in first], ["Second", "First"])                  # the collection's own order is kept
        self.assertEqual(sum(1 for q, _ in calls if q == search.FIND_COLLECTION), 1)        # the id is cached
        self.assertEqual(calls[1][1]["id"], "gid://c/1")                                    # exact title, not "... Old"
        self.assertEqual(calls[1][1]["first"], search.COLLECTION_MAX)

    def test_a_collection_that_is_not_sorted_manually_is_refused_because_its_order_means_nothing(self):
        search._quick_collection.clear()
        self.addCleanup(search._quick_collection.clear)
        responses = {
            search.FIND_COLLECTION: {"collections": {"nodes": [{"id": "gid://c/1", "title": "Popular Trade Products"}]}},
            search.COLLECTION_PRODUCTS: {"collection": {"title": "x", "sortOrder": "BEST_SELLING", "products": {"nodes": []}}},
        }
        with mock.patch.object(search, "graphql", side_effect=lambda q, v=None: responses[q]), \
             mock.patch.object(search, "resolve_sources", return_value={}):
            with self.assertRaises(ShopifyError) as caught:
                search.collection_products(CONFIG, "Popular Trade Products")
        self.assertIn("sorted manually", str(caught.exception))

    def test_a_missing_or_duplicated_collection_is_refused(self):
        for nodes in ([], [{"id": "a", "title": "Popular Trade Products"}, {"id": "b", "title": "Popular Trade Products"}]):
            search._quick_collection.clear()
            with mock.patch.object(search, "graphql", return_value={"collections": {"nodes": nodes}}):
                with self.assertRaises(ShopifyError):
                    search.collection_products(CONFIG, "Popular Trade Products")
        search._quick_collection.clear()

    def test_a_collection_that_vanishes_after_being_cached_is_forgotten(self):
        search._quick_collection["Popular Trade Products"] = ("gid://c/1", search.time.time())
        with mock.patch.object(search, "graphql", return_value={"collection": None}), \
             mock.patch.object(search, "resolve_sources", return_value={}):
            with self.assertRaises(ShopifyError):
                search.collection_products(CONFIG, "Popular Trade Products")
        self.assertNotIn("Popular Trade Products", search._quick_collection)

    def test_the_pre_order_eta_is_read_from_the_metafield(self):
        base = {"id": "i", "title": "t", "handle": "h", "variants": {"nodes": []}}
        self.assertEqual(search._product({**base, "preOrderEta": {"value": " 2026-10-16 "}})["pre_order_eta"], "2026-10-16")
        self.assertIsNone(search._product(base)["pre_order_eta"])
        self.assertIsNone(search._product({**base, "preOrderEta": None})["pre_order_eta"])
        self.assertIsNone(search._product({**base, "preOrderEta": {"value": ""}})["pre_order_eta"])

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

    def run_find(self, query, pool, by_handle=None, everything=None, oos_pool=None, include_inventory=False, members=None):
        """`members` are the products in the Popular Trade Products collection, in order. None means the
        collection can't be read, so the built-in list is used."""
        calls = []
        catalog = by_handle if by_handle is not None else BY_HANDLE

        def fake_collection(config, title):
            calls.append(f"collection:{title}")
            if members is None:
                raise ShopifyError(f"The collection '{title}' was not found.")
            return members

        def fake_search(config, shopify_query, limit=50):
            calls.append(shopify_query)
            if "title:*" not in shopify_query:      # a quick order entry, looked up by handle or by product id
                return [p for p in catalog.values()
                        if f"handle:{p['handle']}" in shopify_query or f"id:{p['id'].rsplit('/', 1)[-1]} " in shopify_query + " "]
            if "inventory_total:>0" in shopify_query:
                return pool
            if "inventory_total:<=0" in shopify_query:
                return oos_pool if oos_pool is not None else []
            return everything if everything is not None else []

        with mock.patch.object(search, "search", side_effect=fake_search), \
             mock.patch.object(search, "collection_products", side_effect=fake_collection):
            return pick.find_for_order(query, include_inventory), calls

    def test_when_the_collection_cannot_be_read_the_built_in_list_is_used_and_says_so(self):
        result, calls = self.run_find("Arran 10", [ARRAN10], members=None)
        self.assertEqual(result["decision"], "use")
        self.assertIn("collection:Popular Trade Products", calls)
        self.assertIn("built-in quick order list was used", result["note"])

    def test_arran_10_end_to_end(self):
        result, calls = self.run_find("Arran 10 Year Old", [ARRAN10, ARRAN_IB, BARLEY_IN_STOCK])
        self.assertEqual(result["decision"], "use")
        self.assertEqual(result["choice"]["name"], ARRAN10["title"])
        self.assertTrue(any(call == "handle:arran-10" for call in calls))                                  # the quick entry by handle
        self.assertTrue(any("title:*arran* AND title:*10*" in call and "inventory_total:>0" in call for call in calls))

    def test_an_unknown_product_says_nothing_matched_and_why(self):
        # In a TWL range but not tagged TWL Brand, so with no stock it is not offered, and the reason is given.
        oos = [product("Macallan 18 Year Old", "mac18", [], 0, trade_core=True)]
        result, calls = self.run_find("Macallan 18", [], everything=oos)
        self.assertEqual(result["decision"], "none")
        self.assertTrue(any("Matched but out of stock: Macallan 18 Year Old" in line for line in result["unavailable"]))

    def test_a_pinned_product_id_is_looked_up_by_id(self):
        result, calls = self.run_find("glenallachie 10 cs", [])
        self.assertEqual(result["decision"], "use")
        self.assertEqual(result["choice"]["name"], GA10CS_B13["title"])
        self.assertEqual(result["choice"]["eta"], "16 Oct 2026")
        self.assertIn("id:8436830666826", calls)

    def test_out_of_stock_twl_brand_products_are_searched_for_separately(self):
        result, calls = self.run_find("arran barley", [], oos_pool=[BARLEY])
        self.assertEqual(result["decision"], "use")
        self.assertTrue(result["choice"]["out_of_stock"])
        self.assertTrue(any("inventory_total:<=0" in c and "tag:'TWL Brand'" in c for c in calls))

    def test_the_two_searches_are_merged_without_duplicates(self):
        result, _ = self.run_find("arran", [ARRAN14, BARLEY], oos_pool=[BARLEY])
        self.assertEqual(len([o for o in result["options"] if o["name"] == BARLEY["title"]]), 1)

    def test_a_full_pool_is_flagged_as_possibly_incomplete(self):
        pool = [product(f"Widget {n}", f"w{n}", ["TWL Brand"], 5) for n in range(pick.POOL_SIZE)]
        result, _ = self.run_find("widget", pool)
        self.assertIn("some may not be shown", result["note"])

    def test_no_words_never_reaches_shopify(self):
        with mock.patch.object(search, "search") as searched:
            with self.assertRaises(ShopifyError):
                pick.find_for_order("year old")
        searched.assert_not_called()


# Members of the Popular Trade Products collection, as the collection returns them (the order is the priority).
COLLECTION = [ARRAN10, ARRAN_SHERRY, GA12, GA10CS_B13, REMNANT, INFINITE, BHOLSA]


class AbbreviationTests(unittest.TestCase):
    def test_every_code_you_gave_expands(self):
        expected = {
            "ar 10": ["arran", "10"], "ad 10": ["ardnamurchan", "10"], "ah bholsa": ["ardnahoe", "bholsa"],
            "ga 12": ["glenallachie", "12"], "ba 12": ["bunnahabhain", "12"], "ld 10": ["ledaig", "10"],
            "ds 12": ["deanston", "12"], "twj ledaig": ["whisky", "jury", "ledaig"],
        }
        for typed, tokens in expected.items():
            self.assertEqual(pick.tokens(typed), tokens, typed)

    def test_year_old_is_dropped_or_written_yo_and_cask_strength_is_cs(self):
        same = ["glenallachie 10 cask strength", "GlenAllachie 10 CS", "GlenAllachie 10yo CS", "glenallachie 10 year old cask strength",
                "GA 10 CS", "ga 10yo cs", "GA10CS"]
        for typed in same:
            self.assertEqual(pick.tokens(typed), ["glenallachie", "10", "cask", "strength"], typed)
        for typed in ("Arran 10", "Arran 10yo", "Arran 10 Year Old", "arran 10 yr old", "AR 10", "AR 10yo"):
            self.assertEqual(pick.tokens(typed), ["arran", "10"], typed)

    def test_dd_means_any_one_of_the_bottlers(self):
        for title in ("Decadent Drams 2014 Arran 10 Year Old Sherry Hogshead", "Decadent Drinks 2013 Arran 10 Cask", "Whiskyland 2015 Arran 10",
                      "Equinox & Solstice Arran 10 Year Old", "Old Islay Arran 10", "Old Orkney Arran 10"):
            self.assertTrue(pick.title_matches(pick.tokens("DD arran 10"), title), title)
        for title in ("Adelphi 2014 Arran 10", "Arran 10 Year Old Single Malt", "Old Pulteney Arran 10"):
            self.assertFalse(pick.title_matches(pick.tokens("DD arran 10"), title), title)

    def test_the_dd_search_offers_shopify_the_alternatives(self):
        groups = pick.load_names()["groups"]
        query = search.title_query(pick.tokens("dd arran 10"), in_stock=True, groups=groups)
        self.assertIn("title:*arran*", query)
        self.assertIn("(title:*decadent* AND title:*drams*)", query)
        self.assertIn("title:*whiskyland*", query)
        self.assertIn("(title:*old* AND title:*islay*)", query)
        self.assertIn(" OR ", query)
        self.assertTrue(query.endswith("status:active AND inventory_total:>0"))

    def test_a_group_code_never_reaches_shopify_as_a_literal_word(self):
        query = search.title_query(pick.tokens("dd arran"), groups=pick.load_names()["groups"])
        self.assertNotIn("title:*dd*", query)

    def test_codes_typed_inside_longer_words_are_left_alone(self):
        self.assertEqual(pick.tokens("gaelic"), ["gaelic"])
        self.assertEqual(pick.tokens("ardbeg"), ["ardbeg"])
        self.assertEqual(pick.tokens("ledaig"), ["ledaig"])

    def test_the_names_file_is_consistent(self):
        names = pick.load_names()
        for code in ("ar", "ad", "ah", "ga", "ba", "ld", "ds", "twj", "cs"):
            self.assertIn(code, names["abbreviations"])
        self.assertEqual(len(names["groups"]["dd"]), 6)
        self.assertFalse(set(names["abbreviations"]) & set(names["groups"]))
        for alias in names["aliases"]:
            self.assertTrue(alias["title_has"] and alias["say"])


class CollectionEntryTests(unittest.TestCase):
    """The quick order list from the Popular Trade Products collection."""

    def entries(self, members=COLLECTION):
        return pick.collection_entries(members)

    def decide(self, query, pool=(), members=COLLECTION):
        entries = self.entries(members)
        quick_products = {e["name"]: e["products"] for e in entries}
        return pick.decide(query, CONFIG, list(pool), quick_products, entries=entries)

    def test_short_names_come_from_the_titles(self):
        names = [e["name"] for e in self.entries()]
        self.assertEqual(names, [
            "Arran 10", "Arran Sherry", "GlenAllachie 12", "GlenAllachie 10 Cask Strength",
            "Remnant Golden Fleece", "Ardnahoe Infinite Loch", "Ardnahoe Bholsa"])

    def test_the_names_you_use_all_work(self):
        cases = {
            "Arran 10": ARRAN10, "Arran 10yo": ARRAN10, "AR 10": ARRAN10, "arran 10 year old": ARRAN10,
            "GlenAllachie 10 CS": GA10CS_B13, "GlenAllachie 10yo CS": GA10CS_B13, "GA 10 CS": GA10CS_B13,
            "GlenAllachie 10 Cask Strength": GA10CS_B13, "ga10cs": GA10CS_B13,
            "GlenAllachie 12": GA12, "GA 12": GA12, "GlenAllachie 12yo": GA12,
            "Arran Sherry": ARRAN_SHERRY, "AR Sherry": ARRAN_SHERRY,
            "Remnant Golden Fleece": REMNANT, "golden fleece": REMNANT,
            "Ardnahoe Infinite Loch": INFINITE, "AH Infinite Loch": INFINITE, "Ardnahoe Bholsa": BHOLSA, "AH Bholsa": BHOLSA,
        }
        for typed, expected in cases.items():
            result = self.decide(typed)
            self.assertEqual(result["decision"], "use", typed)
            self.assertEqual(result["choice"]["name"], expected["title"], typed)

    def test_a_new_batch_needs_no_edit_anywhere(self):
        batch_14 = product("GlenAllachie 10 Year Old Cask Strength Batch 14 Single Malt Scotch Whisky", "ga10cs-b14",
                           ["brand_GlenAllachie", "TWL Brand", "abv_59"], 60)
        swapped = [ARRAN10, ARRAN_SHERRY, GA12, batch_14, REMNANT, INFINITE, BHOLSA]      # staff swapped the product in the collection
        result = self.decide("GA 10 CS", members=swapped)
        self.assertEqual((result["decision"], result["choice"]["name"]), ("use", batch_14["title"]))

    def test_two_batches_in_the_collection_at_once_ask_which(self):
        result = self.decide("GlenAllachie 10 CS", members=COLLECTION + [GA10CS_B7])
        self.assertEqual(result["decision"], "ask")
        self.assertEqual(len(result["options"]), 2)

    def test_the_collections_order_is_the_priority_order(self):
        result = self.decide("arran", [ARRAN14])
        self.assertEqual(names(result)[:3], [ARRAN10["title"], ARRAN_SHERRY["title"], ARRAN14["title"]])
        reordered = [ARRAN_SHERRY, ARRAN10] + COLLECTION[2:]
        result = self.decide("arran", [ARRAN14], members=reordered)
        self.assertEqual(names(result)[:3], [ARRAN_SHERRY["title"], ARRAN10["title"], ARRAN14["title"]])

    def test_removing_a_product_from_the_collection_removes_its_priority(self):
        without = [p for p in COLLECTION if p is not ARRAN_SHERRY]
        result = self.decide("arran", [ARRAN14, ARRAN_SHERRY], members=without)
        self.assertEqual(names(result)[0], ARRAN10["title"])
        self.assertNotIn("Quick order list: Arran Sherry", next(o for o in result["options"] if o["name"] == ARRAN_SHERRY["title"])["why"])

    def test_a_product_added_to_the_collection_works_by_its_derived_name(self):
        newcomer = product("Ardnamurchan AD 10 Year Old Single Malt Scotch Whisky", "ard-ad10", ["brand_Ardnamurchan", "TWL Brand"], 30, our_brands=True)
        entries = pick.collection_entries(COLLECTION + [newcomer])
        self.assertEqual(entries[-1]["name"], "Ardnamurchan AD 10")
        quick_products = {e["name"]: e["products"] for e in entries}
        result = pick.decide("AD 10", CONFIG, [], quick_products, entries=entries)
        self.assertEqual((result["decision"], result["choice"]["name"]), ("use", newcomer["title"]))

    def test_bracketed_text_is_ignored_when_working_out_a_name(self):
        entry = self.entries([product("GlenAllachie 15 Year Old Single Malt Scotch Whisky [PRE-ORDER]", "g15", ["TWL Brand"])])[0]
        self.assertEqual(entry["name"], "GlenAllachie 15")
        self.assertIn(frozenset({"glenallachie", "15"}), entry["_aliases"])

    def test_two_members_with_the_same_short_name_stay_distinct(self):
        twin = product(ARRAN10["title"], "arran-10-twin", ["brand_Arran", "TWL Brand"], 5)
        names_ = [e["name"] for e in pick.collection_entries([ARRAN10, twin])]
        self.assertEqual(len(set(names_)), 2)

    def test_an_empty_collection_is_an_empty_list_not_the_fallback(self):
        with mock.patch.object(search, "collection_products", return_value=[]):
            entries, note = pick.quick_entries(CONFIG)
        self.assertEqual((entries, note), ([], None))

    def test_an_unreadable_collection_falls_back_with_a_note(self):
        with mock.patch.object(search, "collection_products", side_effect=ShopifyError("nope")):
            entries, note = pick.quick_entries(CONFIG)
        self.assertEqual(entries, CONFIG["quick_order"])
        self.assertIn("couldn't be read", note)

    def test_the_end_to_end_lookup_uses_the_collection_and_needs_no_product_lookups_for_it(self):
        result, calls = FindForOrderTests().run_find("GlenAllachie 10yo CS", [], members=COLLECTION)
        self.assertEqual((result["decision"], result["choice"]["name"]), ("use", GA10CS_B13["title"]))
        self.assertNotIn("note", result)
        self.assertFalse([c for c in calls if c.startswith("handle:") or c.startswith("id:")])

    def test_dd_finds_the_bottlers_products_end_to_end(self):
        decadent = product("Decadent Drams 2014 Arran 10 Year Old Sherry Hogshead Single Cask Single Malt Scotch Whisky", "dd-arran",
                           ["brand_Decadent Drams", "TWL IB", "brand_Arran"], 4, ib_collection=True)
        result, calls = FindForOrderTests().run_find("DD arran 10", [decadent], members=COLLECTION)
        self.assertEqual(result["decision"], "use")          # the only DD Arran 10, NOT the core Arran 10
        self.assertEqual(result["choice"]["name"], decadent["title"])
        self.assertTrue(any("(title:*decadent* AND title:*drams*)" in c for c in calls))
        second = product("Whiskyland 2015 Arran 10 Year Old Single Cask Single Malt Scotch Whisky", "wl-arran", ["brand_Whiskyland", "TWL IB", "brand_Arran"], 3, ib_collection=True)
        both, _ = FindForOrderTests().run_find("DD arran 10", [decadent, second], members=COLLECTION)
        self.assertEqual(both["decision"], "ask")
        self.assertEqual(len(both["options"]), 2)

    def test_extra_words_stop_a_quick_entry_being_an_exact_match(self):
        # "arran 10" is Arran 10, but adding words that the core product doesn't have means something else was meant.
        sherry_10 = product("Arran 10 Year Old Sherry Cask Finish Single Malt Scotch Whisky", "a10s", ["brand_Arran", "TWL Brand"], 5, trade_core=True)
        pool = [ARRAN10, sherry_10, ARRAN_SHERRY]
        result = self.decide("arran 10 sherry cask", pool)
        self.assertEqual(result["decision"], "use")
        self.assertEqual(result["choice"]["name"], sherry_10["title"])          # not Arran 10, not Arran Sherry
        self.assertNotEqual(self.decide("arran 10", pool)["choice"]["name"], sherry_10["title"])
        adelphi = self.decide("adelphi arran 10", [ARRAN_IB, ARRAN10])
        self.assertEqual(adelphi["choice"]["name"], ARRAN_IB["title"])          # never the core Arran 10


class ConfigTests(unittest.TestCase):
    def test_the_shipped_config_is_complete(self):
        self.assertEqual([e["name"] for e in CONFIG["quick_order"]], [
            "Arran 10", "Arran Sherry", "GlenAllachie 12", "GlenAllachie 10 Cask Strength",
            "Remnant Golden Fleece", "Ardnahoe Infinite Loch", "Ardnahoe Bholsa"])
        self.assertEqual(CONFIG["priority_brands"], ["Arran", "GlenAllachie", "Ardnahoe", "Ardnamurchan"])
        self.assertEqual(CONFIG["_oos_tags"], {"twl brand"})
        ga10 = next(e for e in CONFIG["quick_order"] if e["name"] == "GlenAllachie 10 Cask Strength")
        self.assertEqual(ga10["product_ids"], ["gid://shopify/Product/8436830666826"])
        for entry in CONFIG["quick_order"]:
            self.assertTrue(entry["_aliases"], entry["name"])
            self.assertTrue(entry.get("handles") or entry.get("product_ids") or entry.get("title_pattern"), entry["name"])
            # The entry's own name is always one of its aliases, so typing it exactly always works.
            self.assertIn(frozenset(pick.tokens(entry["name"])), entry["_aliases"], entry["name"])

    def test_aliases_do_not_collide_between_entries(self):
        seen = {}
        for entry in CONFIG["quick_order"]:
            for alias in set(entry["_aliases"]):      # abbreviations can make two of one entry's aliases identical
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

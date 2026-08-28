from decimal import Decimal

from app.optimizer import (
    Offer,
    OrderVariant,
    ShopProfile,
    filter_dominated_variants,
    optimize_orders,
    plan_scenarios,
)


def test_assigns_each_line_to_cheapest_shop_and_adds_shipping():
    shops = [
        ShopProfile(1, "A", Decimal("8.00"), Decimal("50.00"), None, 2),
        ShopProfile(2, "B", Decimal("10.00"), None, None, 4),
    ]
    offers = [
        Offer(11, 101, 1, Decimal("20.00"), 2, 2),
        Offer(12, 102, 1, Decimal("8.00"), 1, 2),
        Offer(13, 101, 2, Decimal("18.00"), 2, 4),
        Offer(14, 102, 2, Decimal("12.00"), 1, 4),
    ]

    variants = optimize_orders(offers, shops, tempo=0)

    best = variants[0]
    assert best.shop_ids == (1,)
    assert best.assignments == {101: 11, 102: 12}
    assert best.subtotals == {1: Decimal("48.00")}
    assert best.shipping == {1: Decimal("8.00")}
    assert best.total_chf == Decimal("56.00")
    assert best.max_liefertage == 2
    assert best.score == Decimal("56.00")


def test_free_shipping_threshold_is_inclusive():
    shops = [ShopProfile(1, "A", Decimal("9.00"), Decimal("50.00"), None, 3)]
    offers = [Offer(1, 1, 1, Decimal("25.00"), 2, None)]

    variant = optimize_orders(offers, shops, tempo=0)[0]

    assert variant.shipping == {1: Decimal("0.00")}
    assert variant.total_chf == Decimal("50.00")
    assert variant.max_liefertage == 3


def test_unknown_shipping_is_visible_but_ranked_after_a_known_total():
    shops = [
        ShopProfile(1, "Checkout", None, None, None, 3),
        ShopProfile(2, "Belegt", Decimal("5.00"), None, None, 3),
    ]
    offers = [
        Offer(1, 1, 1, Decimal("10.00"), 1, 3),
        Offer(2, 1, 2, Decimal("2000000.00"), 1, 3),
    ]
    variants = optimize_orders(offers, shops, tempo=0, limit=10)

    assert {variant.shop_ids for variant in variants} == {(1,), (2,)}
    assert variants[0].shop_ids == (2,)
    unknown = next(variant for variant in variants if variant.shop_ids == (1,))
    assert unknown.shipping == {1: None}
    assert unknown.total_chf == Decimal("10.00")
    assert unknown.contains_unknown_shipping is True
    scenarios = plan_scenarios(offers, shops, [1])
    assert scenarios["cheapest"].shop_ids == (2,)
    assert scenarios["fastest"].shop_ids == (2,)


def test_same_shop_set_prefers_reaching_free_shipping_over_an_unknown_total():
    shops = [ShopProfile(1, "Checkout", None, Decimal("20.00"), None, 3)]
    offers = [
        Offer(1, 1, 1, Decimal("10.00"), 1, 3),
        Offer(2, 1, 1, Decimal("25.00"), 1, 3),
    ]

    variants = optimize_orders(offers, shops, tempo=0, limit=10)

    assert len(variants) == 1
    assert variants[0].assignments == {1: 2}
    assert variants[0].shipping == {1: Decimal("0.00")}
    assert variants[0].contains_unknown_shipping is False


def test_rejects_shop_subset_below_minimum_order_value():
    shops = [
        ShopProfile(1, "Minimum", Decimal("0"), None, Decimal("50"), 2),
        ShopProfile(2, "Ohne Minimum", Decimal("0"), None, None, 3),
    ]
    offers = [
        Offer(1, 1, 1, Decimal("10"), 1, 2),
        Offer(2, 1, 2, Decimal("20"), 1, 3),
    ]

    variants = optimize_orders(offers, shops, tempo=0)

    assert [variant.shop_ids for variant in variants] == [(2,)]


def test_tempo_uses_fifteen_chf_per_max_delivery_day_and_changes_ranking():
    shops = [
        ShopProfile(1, "Billig", Decimal("0"), None, None, 10),
        ShopProfile(2, "Schnell", Decimal("0"), None, None, 1),
    ]
    offers = [
        Offer(1, 1, 1, Decimal("10"), 1, 10),
        Offer(2, 1, 2, Decimal("100"), 1, 1),
    ]

    cheap_first = optimize_orders(offers, shops, tempo=0)
    fast_first = optimize_orders(offers, shops, tempo=1)

    assert cheap_first[0].shop_ids == (1,)
    assert fast_first[0].shop_ids == (2,)
    assert fast_first[0].score == Decimal("115.00")
    assert cheap_first[0].score == Decimal("10.00")


def test_returns_at_most_three_variants_with_distinct_shop_sets():
    shops = [
        ShopProfile(shop_id, f"Shop {shop_id}", Decimal("0"), None, None, shop_id)
        for shop_id in range(1, 5)
    ]
    offers = [
        Offer(shop_id, 1, shop_id, Decimal(str(10 + shop_id)), 1, shop_id)
        for shop_id in range(1, 5)
    ]

    variants = optimize_orders(offers, shops, tempo=0)

    assert len(variants) == 3
    assert len({variant.shop_ids for variant in variants}) == 3


def test_builds_complete_plan_when_four_shops_are_required():
    shops = [
        ShopProfile(shop_id, f"Shop {shop_id}", Decimal("0"), None, None, 1)
        for shop_id in range(1, 5)
    ]
    offers = [
        Offer(shop_id, shop_id, shop_id, Decimal("10"), 1, 1)
        for shop_id in range(1, 5)
    ]

    variants = optimize_orders(offers, shops, tempo=0)

    assert variants[0].shop_ids == (1, 2, 3, 4)
    assert variants[0].missing_line_ids == ()
    assert variants[0].total_chf == Decimal("40.00")


def test_scenarios_use_shop_default_as_estimate_when_offer_has_no_days():
    shops = [ShopProfile(1, "A", Decimal("5"), None, None, 4)]
    offers = [Offer(1, 10, 1, Decimal("10"), 1, None)]

    scenarios = plan_scenarios(offers, shops, [10])

    assert scenarios["fastest"].max_liefertage == 4
    assert scenarios["fastest"].contains_estimates is True


def test_unsourced_unknown_shop_days_stay_unknown_and_rank_after_known_delivery():
    shops = [
        ShopProfile(1, "Unknown", Decimal("0"), None, None, None),
        ShopProfile(2, "Known", Decimal("0"), None, None, 3),
    ]
    offers = [
        Offer(1, 10, 1, Decimal("1"), 1, None),
        Offer(2, 10, 2, Decimal("2"), 1, None),
    ]

    scenarios = plan_scenarios(offers, shops, [10])

    assert scenarios["cheapest"].max_liefertage is None
    assert scenarios["cheapest"].contains_unknown_delivery is True
    assert scenarios["fastest"].assignments == {10: 2}
    assert scenarios["fastest"].max_liefertage == 3


def test_rejects_tempo_outside_unit_interval():
    shops = [ShopProfile(1, "A", Decimal("0"), None, None, 1)]
    offers = [Offer(1, 1, 1, Decimal("1"), 1, 1)]

    import pytest

    with pytest.raises(ValueError, match="tempo"):
        optimize_orders(offers, shops, tempo=1.01)


def scenario_fixture():
    shops = [
        ShopProfile(1, "Billig", Decimal("0"), None, None, 8),
        ShopProfile(2, "Schnell", Decimal("0"), None, None, 1),
    ]
    offers = [
        Offer(11, 101, 1, Decimal("10"), 1, None),
        Offer(12, 101, 2, Decimal("20"), 1, 1),
        Offer(21, 102, 1, Decimal("10"), 1, None),
        Offer(22, 102, 2, Decimal("20"), 1, 1),
    ]
    return offers, shops


def test_cheapest_scenario_minimizes_total():
    offers, shops = scenario_fixture()

    scenarios = plan_scenarios(offers, shops, required_line_ids=[101, 102])

    cheapest = scenarios["cheapest"]
    assert cheapest.assignments == {101: 11, 102: 21}
    assert cheapest.total_chf == Decimal("20.00")
    assert cheapest.contains_estimates is True


def test_fastest_scenario_minimizes_max_days_then_price():
    offers, shops = scenario_fixture()

    fastest = plan_scenarios(offers, shops, required_line_ids=[101, 102])["fastest"]

    assert fastest.assignments == {101: 12, 102: 22}
    assert fastest.max_liefertage == 1
    assert fastest.total_chf == Decimal("40.00")


def test_balanced_scenario_uses_tempo_half():
    offers, shops = scenario_fixture()

    balanced = plan_scenarios(offers, shops, required_line_ids=[101, 102])["balanced"]

    assert balanced.assignments == {101: 12, 102: 22}
    assert balanced.score == Decimal("47.500")


def test_one_shop_scenario_reports_missing_lines_when_no_shop_covers_all():
    shops = [
        ShopProfile(1, "Mehr Abdeckung", Decimal("0"), None, None, 2),
        ShopProfile(2, "Weniger Abdeckung", Decimal("0"), None, None, 1),
    ]
    offers = [
        Offer(11, 101, 1, Decimal("10"), 1, 2),
        Offer(21, 102, 1, Decimal("10"), 1, 2),
        Offer(31, 103, 2, Decimal("1"), 1, 1),
    ]

    one_shop = plan_scenarios(offers, shops, required_line_ids=[101, 102, 103])["one_shop"]

    assert one_shop.shop_ids == (1,)
    assert one_shop.assignments == {101: 11, 102: 21}
    assert one_shop.missing_line_ids == (103,)


def test_scenarios_apply_pin_and_exclude_overrides():
    offers, shops = scenario_fixture()

    scenarios = plan_scenarios(
        offers,
        shops,
        required_line_ids=[101, 102],
        pins={101: 12},
        excludes={21},
    )

    assert scenarios["cheapest"].assignments == {101: 12, 102: 22}
    assert scenarios["fastest"].assignments == {101: 12, 102: 22}


def test_pin_selects_product_not_shop_offer_when_product_key_matches():
    shops = [
        ShopProfile(1, "A", Decimal("0"), None, None, 2),
        ShopProfile(2, "B", Decimal("0"), None, None, 2),
    ]
    offers = [
        Offer(1, 101, 1, Decimal("20"), 1, 2, "same-product"),
        Offer(2, 101, 2, Decimal("10"), 1, 2, "same-product"),
        Offer(3, 101, 1, Decimal("5"), 1, 2, "other-product"),
    ]

    cheapest = plan_scenarios(
        offers, shops, required_line_ids=[101], pins={101: 1}
    )["cheapest"]

    assert cheapest.assignments == {101: 2}


def test_dominance_filter_removes_costlier_slower_and_unknown_complete_plans():
    def variant(total, days, offer_id, *, missing=()):
        return OrderVariant(
            shop_ids=(1,),
            assignments={101: offer_id},
            subtotals={1: Decimal(str(total))},
            shipping={1: Decimal("0")},
            total_chf=Decimal(str(total)),
            max_liefertage=days,
            score=Decimal(str(total)),
            missing_line_ids=missing,
        )

    best = variant(10, 2, 1)
    slower_and_costlier = variant(11, 3, 2)
    equally_priced_unknown = variant(10, None, 3)
    incomplete = variant(1, 1, 4, missing=(102,))

    assert filter_dominated_variants(
        [best, slower_and_costlier, equally_priced_unknown, incomplete]
    ) == [best, incomplete]


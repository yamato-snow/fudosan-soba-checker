"""賃料事例の保存・抽出（テンポラリ SQLite）。

取込は必ず `parse_rent_csv → upsert_rent_listings` の**本番経路**を通す
（ORM を直接 add すると、パースの取りこぼしがテストをすり抜ける）。
"""

from __future__ import annotations

from app.constants import RENT_KIND_CONDO, RENT_KIND_HOUSE
from app.services.districts import RingContext
from app.services.rent import EXPANSION_STAGES_RENT_CONDO
from app.services.rent_import import CSV_TEMPLATE_HEADER, parse_rent_csv
from app.services.rent_repo import RentTarget, fetch_rent_cases, list_rent_districts
from app.services.rent_storage import (
    count_rent_listings,
    list_rent_sources,
    purge_rent_source,
    upsert_rent_listings,
)

HOME_DISTRICT = "990010040"
NEIGHBOR_DISTRICT = "990010041"
OTHER_HOME_CITY_DISTRICT = "990010099"
OTHER_CITY_DISTRICT = "990020001"


def rent_row(
    *,
    district: str = HOME_DISTRICT,
    kind: str = RENT_KIND_CONDO,
    rent: str = "85000",
    admin: str = "5000",
    area: str = "62.5",
    year: str = "2005",
    s_year: str = "2026",
    s_month: str = "8",
) -> str:
    city = "サンプル市" if district.startswith("99001") else "となり町"
    return ",".join(
        [district, f"{city}地区{district[-2:]}", kind, rent, admin, area, "3LDK", year,
         "ＲＣ", "3", "8", "8", "無", s_year, s_month, "募集中"]
    )


def csv_text(*rows: str) -> str:
    return "\n".join([CSV_TEMPLATE_HEADER, *rows]) + "\n"


async def seed(session, *rows: str, source_ref: str = "t1") -> tuple[int, int]:
    parsed = parse_rent_csv(csv_text(*rows), source_ref=source_ref)
    assert parsed.errors == [], parsed.errors
    return await upsert_rent_listings(session, parsed.records)


def ctx(district: str = HOME_DISTRICT) -> RingContext:
    return RingContext(
        district_code=district,
        home_city="99001",
        target_cities=("99001", "99002"),
        neighbor_districts=(NEIGHBOR_DISTRICT,),
    )


def target(**kwargs) -> RentTarget:
    base = {
        "property_kind": RENT_KIND_CONDO,
        "district_code": HOME_DISTRICT,
        "exclusive_area": 62.5,
        "building_year": 2005,
    }
    return RentTarget(**{**base, **kwargs})


# --- 冪等・洗い替え -----------------------------------------------------


async def test_import_is_idempotent(session):
    inserted, skipped = await seed(session, rent_row(), rent_row(rent="90000"))
    assert (inserted, skipped) == (2, 0)

    again = await seed(session, rent_row(), rent_row(rent="90000"))
    assert again == (0, 2)
    assert await count_rent_listings(session) == 2


async def test_purge_rent_source_removes_only_that_label(session):
    await seed(session, rent_row(), source_ref="aug")
    await seed(session, rent_row(rent="90000"), source_ref="sep")
    assert await count_rent_listings(session) == 2

    removed = await purge_rent_source(session, "aug")
    assert removed == 1
    assert await count_rent_listings(session) == 1
    assert await list_rent_sources(session) == [("sep", 1)]

    # 消してから入れ直せる（洗い替えモードという第2の分岐を作らない運用）
    await seed(session, rent_row(rent="87000"), source_ref="aug")
    assert await count_rent_listings(session) == 2


async def test_same_content_in_different_source_ref_is_a_separate_case(session):
    await seed(session, rent_row(), source_ref="aug")
    inserted, _ = await seed(session, rent_row(), source_ref="sep")
    assert inserted == 1


# --- リング抽出 ---------------------------------------------------------


async def test_ring_expansion_reaches_wider_areas(session):
    await seed(
        session,
        rent_row(district=HOME_DISTRICT),
        rent_row(district=NEIGHBOR_DISTRICT, rent="86000"),
        rent_row(district=OTHER_HOME_CITY_DISTRICT, rent="87000"),
        rent_row(district=OTHER_CITY_DISTRICT, rent="88000"),
    )
    counts = [
        len(await fetch_rent_cases(session, ctx(), target(), stage))
        for stage in EXPANSION_STAGES_RENT_CONDO[:3]
    ]
    assert counts == [2, 3, 4]  # Ring0-1 → 基準市全域 → 対象市全域


async def test_property_kind_separates_the_population(session):
    await seed(
        session,
        rent_row(kind=RENT_KIND_CONDO),
        rent_row(kind=RENT_KIND_HOUSE, rent="120000", area="60"),
    )
    condo = await fetch_rent_cases(session, ctx(), target(), EXPANSION_STAGES_RENT_CONDO[0])
    assert [c.property_kind for c in condo] == [RENT_KIND_CONDO]


async def test_area_filter_uses_floor_tolerance(session):
    """賃料の面積は `floor_tolerance` の軸に載る（`area_tolerance` は土地面積＝賃料では使わない）。"""
    await seed(
        session,
        rent_row(area="62.5"),
        rent_row(area="75", rent="95000"),  # +20% → ±30% に入る
        rent_row(area="120", rent="150000"),  # +92% → ±50% でも入らない
    )
    narrow = await fetch_rent_cases(session, ctx(), target(), EXPANSION_STAGES_RENT_CONDO[0])
    assert len(narrow) == 2
    widest = await fetch_rent_cases(session, ctx(), target(), EXPANSION_STAGES_RENT_CONDO[-1])
    assert len(widest) == 2


async def test_building_year_filter_uses_building_year_not_survey_year(session):
    """`survey_year` は調査時点であって築年ではない（混同すると築年フィルタが壊れる）。"""
    await seed(
        session,
        rent_row(year="2005", s_year="2020"),
        rent_row(year="2018", rent="99000", s_year="2026"),  # 築年が ±5 の外
    )
    narrow = await fetch_rent_cases(session, ctx(), target(), EXPANSION_STAGES_RENT_CONDO[0])
    assert len(narrow) == 1
    relaxed = await fetch_rent_cases(session, ctx(), target(), EXPANSION_STAGES_RENT_CONDO[-1])
    assert len(relaxed) == 2  # 築年の条件を撤廃した最終段


async def test_missing_building_year_is_excluded_only_while_the_filter_is_on(session):
    await seed(session, rent_row(year=""), rent_row(year="", rent="90000", district=NEIGHBOR_DISTRICT))
    assert await fetch_rent_cases(session, ctx(), target(), EXPANSION_STAGES_RENT_CONDO[0]) == []
    assert len(await fetch_rent_cases(session, ctx(), target(), EXPANSION_STAGES_RENT_CONDO[-1])) == 2


async def test_target_without_building_year_keeps_all_cases(session):
    await seed(session, rent_row(year="1990"), rent_row(year="2020", rent="90000"))
    cases = await fetch_rent_cases(
        session, ctx(), target(building_year=None), EXPANSION_STAGES_RENT_CONDO[0]
    )
    assert len(cases) == 2


async def test_list_rent_districts_counts_by_district(session):
    await seed(session, rent_row(), rent_row(rent="90000"), rent_row(district=NEIGHBOR_DISTRICT))
    options = {d.code: d.count for d in await list_rent_districts(session)}
    assert options == {HOME_DISTRICT: 2, NEIGHBOR_DISTRICT: 1}


async def test_fetch_returns_rent_cases_with_unit_price(session):
    await seed(session, rent_row())
    (case,) = await fetch_rent_cases(session, ctx(), target(), EXPANSION_STAGES_RENT_CONDO[0])
    assert case.total_monthly == 90_000
    assert case.rent_unit_price == 90_000 / 62.5
    assert case.ring == EXPANSION_STAGES_RENT_CONDO[0].ring

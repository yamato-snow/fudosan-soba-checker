"""地区リング抽出＋取込 upsert の統合テスト（テンポラリ SQLite）。"""

from __future__ import annotations

from app.constants import TYPE_HOUSE, TYPE_LAND
from app.services.districts import (
    RING1_SOURCE_AUTO,
    RING1_SOURCE_MANUAL,
    RING1_SOURCE_NONE,
    RingContext,
    TargetProperty,
    fetch_cases,
    infer_neighbor_districts,
    list_districts,
    resolve_ring1,
)
from app.services.reinfolib import normalize
from app.services.storage import count_transactions, upsert_transactions
from app.services.valuation import EXPANSION_STAGES, EXPANSION_STAGES_HOUSE, Criteria

HOME_DISTRICT = "990010040"  # サンプル市・基準地区
NEIGHBOR_DISTRICT = "990010041"  # 近隣地区（Ring1）
OTHER_HOME_CITY_DISTRICT = "990010099"  # 同じサンプル市の別地区（Ring2）
OTHER_CITY_DISTRICT = "990020001"  # となり町（Ring3）
PRICEY_DISTRICT = "990010050"  # 同じサンプル市だが地価が離れている地区
THIN_DISTRICT = "990010060"  # 同じサンプル市・地価は近いが件数が足りない地区


def raw(
    *,
    district: str,
    city: str = "99001",
    land_type: str = TYPE_LAND,
    price: str = "20000000",
    area: str = "200",
    tfa: str = "",
    year: str = "",
    period: str = "2024年第4四半期",
) -> dict:
    return {
        "PriceCategory": "不動産取引価格情報",
        "Type": land_type,
        "MunicipalityCode": city,
        "Municipality": "サンプル市" if city == "99001" else "となり町",
        "DistrictName": f"地区{district[-2:]}",
        "DistrictCode": district,
        "TradePrice": price,
        "Area": area,
        "TotalFloorArea": tfa,
        "BuildingYear": year,
        "Period": period,
    }


async def seed(session, raws: list[dict]) -> None:
    records = [r for r in (normalize(x) for x in raws) if r is not None]
    await upsert_transactions(session, records)


def ctx() -> RingContext:
    return RingContext(
        district_code=HOME_DISTRICT,
        home_city="99001",
        target_cities=("99001", "99002"),
        neighbor_districts=(NEIGHBOR_DISTRICT,),
    )


async def test_upsert_is_idempotent(session):
    raws = [raw(district=HOME_DISTRICT, price=str(p)) for p in (10_000_000, 20_000_000)]
    records = [normalize(x) for x in raws]

    first_new, first_skipped = await upsert_transactions(session, records)
    second_new, second_skipped = await upsert_transactions(session, records)

    assert (first_new, first_skipped) == (2, 0)
    assert (second_new, second_skipped) == (0, 2)  # 再実行しても増えない
    assert await count_transactions(session) == 2


async def test_rings_widen_the_sample(session):
    await seed(
        session,
        [
            raw(district=HOME_DISTRICT, price="20000000"),
            raw(district=NEIGHBOR_DISTRICT, price="21000000"),
            raw(district=OTHER_HOME_CITY_DISTRICT, price="22000000"),
            raw(district=OTHER_CITY_DISTRICT, city="99002", price="23000000"),
        ],
    )
    target = TargetProperty(land_type=TYPE_LAND, district_code=HOME_DISTRICT, area=200)

    def crit(ring: int) -> Criteria:
        return Criteria(ring=ring, year_tolerance=5, area_tolerance=0.3)

    assert len(await fetch_cases(session, ctx(), target, crit(0))) == 1  # 同一地区
    assert len(await fetch_cases(session, ctx(), target, crit(1))) == 2  # ＋近隣地区
    assert len(await fetch_cases(session, ctx(), target, crit(2))) == 3  # ＋基準市全域
    assert len(await fetch_cases(session, ctx(), target, crit(3))) == 4  # ＋対象市全域


async def test_area_and_year_filters(session):
    await seed(
        session,
        [
            # 面積が近い戸建（築年も近い）
            raw(district=HOME_DISTRICT, land_type=TYPE_HOUSE, area="200", tfa="100", year="2000年"),
            # 面積が ±30% 外（±50% には入る）
            raw(district=HOME_DISTRICT, land_type=TYPE_HOUSE, area="280", tfa="100", year="2000年"),
            # 築年が離れすぎ（±5年外）
            raw(district=HOME_DISTRICT, land_type=TYPE_HOUSE, area="200", tfa="100", year="1980年"),
            # 種別違い（土地）は混ざらない
            raw(district=HOME_DISTRICT, land_type=TYPE_LAND, area="200"),
        ],
    )
    target = TargetProperty(
        land_type=TYPE_HOUSE,
        district_code=HOME_DISTRICT,
        area=200,
        total_floor_area=100,
        building_year=2001,
    )

    tight = await fetch_cases(session, ctx(), target, EXPANSION_STAGES_HOUSE[0])
    assert len(tight) == 1

    # 築年を撤廃すると築古が入る（土地面積条件はそのまま）
    loose_year = await fetch_cases(session, ctx(), target, EXPANSION_STAGES_HOUSE[5])
    assert len(loose_year) == 2

    # 土地面積を ±50% にすると更に増える
    loose_area = await fetch_cases(session, ctx(), target, EXPANSION_STAGES_HOUSE[6])
    assert len(loose_area) == 3


async def test_house_without_total_floor_area_is_excluded_from_cases(session):
    await seed(
        session,
        [
            raw(district=HOME_DISTRICT, land_type=TYPE_HOUSE, area="200", tfa="", year="2000年"),
        ],
    )
    target = TargetProperty(
        land_type=TYPE_HOUSE,
        district_code=HOME_DISTRICT,
        area=200,
        total_floor_area=100,
        building_year=2000,
    )
    assert await fetch_cases(session, ctx(), target, EXPANSION_STAGES[0]) == []


async def test_floor_filter_keeps_kokaya_out_of_the_sample(session):
    """軸ズレの回帰テスト: 延床105㎡のターゲットに延床40㎡（古家付き土地相当）を入れない。

    軸ズレ（主指標は延床単価なのに絞りは土地面積だけ）で㎡単価が桁違いに暴れていた事象。
    """
    await seed(
        session,
        [
            # 延床が近い（±30% = 73.5〜136.5㎡）
            raw(district=HOME_DISTRICT, land_type=TYPE_HOUSE, area="200", tfa="105", year="2000年"),
            raw(district=HOME_DISTRICT, land_type=TYPE_HOUSE, area="210", tfa="120", year="2000年"),
            # 古家付き土地相当（延床45㎡・㎡単価が跳ね上がる）
            raw(
                district=HOME_DISTRICT,
                land_type=TYPE_HOUSE,
                area="200",
                tfa="45",
                year="2000年",
                price="15000000",
            ),
            # 延床が大きすぎる（±50% でも入らない）
            raw(district=HOME_DISTRICT, land_type=TYPE_HOUSE, area="220", tfa="180", year="2000年"),
        ],
    )
    target = TargetProperty(
        land_type=TYPE_HOUSE,
        district_code=HOME_DISTRICT,
        area=200,
        total_floor_area=105,
        building_year=2000,
    )

    tight = await fetch_cases(session, ctx(), target, EXPANSION_STAGES_HOUSE[0])
    assert sorted(c.total_floor_area for c in tight) == [105, 120]

    # 延床を ±50%（最終下限）まで緩めても、45㎡ / 180㎡ は最後まで母集団に入らない
    widest = await fetch_cases(session, ctx(), target, EXPANSION_STAGES_HOUSE[-1])
    floors = sorted(c.total_floor_area for c in widest)
    assert 45 not in floors
    assert 180 not in floors


async def test_land_query_is_unaffected_by_floor_tolerance(session):
    """土地は現行どおり（延床条件なし）。"""
    await seed(session, [raw(district=HOME_DISTRICT, land_type=TYPE_LAND, area="200")])
    target = TargetProperty(land_type=TYPE_LAND, district_code=HOME_DISTRICT, area=200)

    assert len(await fetch_cases(session, ctx(), target, EXPANSION_STAGES[0])) == 1
    # 万一戸建用の条件を渡されても土地には延床が無いので落ちない
    assert len(await fetch_cases(session, ctx(), target, EXPANSION_STAGES_HOUSE[0])) == 1


async def _seed_for_ring1_inference(session) -> None:
    """同一市内に「地価が近い地区 / 離れた地区 / 件数が足りない地区」を作る。"""
    await seed(
        session,
        [
            # 対象地区: 土地㎡単価 100,000 前後（3件）
            *[
                raw(district=HOME_DISTRICT, price=str(p), area="200")
                for p in (19_800_000, 20_000_000, 20_200_000)
            ],
            # 地価が近い（110,000 = ±25% 以内）・3件 → Ring1 に入る
            *[
                raw(district=OTHER_HOME_CITY_DISTRICT, price=str(p), area="200")
                for p in (21_800_000, 22_000_000, 22_200_000)
            ],
            # 地価が離れている（200,000）→ 入らない
            *[
                raw(district=PRICEY_DISTRICT, price=str(p), area="200")
                for p in (39_000_000, 40_000_000, 41_000_000)
            ],
            # 地価は近いが2件しかない → 中央値が不安定なので入らない
            *[
                raw(district=THIN_DISTRICT, price=str(p), area="200")
                for p in (20_000_000, 20_100_000)
            ],
        ],
    )


async def test_infer_neighbor_districts_by_land_price(session):
    await _seed_for_ring1_inference(session)
    assert await infer_neighbor_districts(session, HOME_DISTRICT) == [OTHER_HOME_CITY_DISTRICT]


async def test_resolve_ring1_falls_back_to_inference_when_env_is_empty(session):
    await _seed_for_ring1_inference(session)
    bare = RingContext(
        district_code=HOME_DISTRICT,
        home_city="99001",
        target_cities=("99001", "99002"),
        neighbor_districts=(),
    )

    resolved, source = await resolve_ring1(session, bare)

    assert source == RING1_SOURCE_AUTO
    assert resolved.neighbor_districts == (OTHER_HOME_CITY_DISTRICT,)

    # Ring1 が実際に広がる（＝いきなり市全域へ飛ばない）
    target = TargetProperty(land_type=TYPE_LAND, district_code=HOME_DISTRICT, area=200)
    ring1 = Criteria(ring=1, year_tolerance=5, area_tolerance=0.3)
    assert len(await fetch_cases(session, bare, target, ring1)) == 3  # 推定前は同一地区のみ
    assert len(await fetch_cases(session, resolved, target, ring1)) == 6  # 推定後は近隣も入る


async def test_manual_neighbor_districts_always_win(session):
    await _seed_for_ring1_inference(session)

    resolved, source = await resolve_ring1(session, ctx())

    assert source == RING1_SOURCE_MANUAL
    assert resolved.neighbor_districts == (NEIGHBOR_DISTRICT,)  # 自動推定で上書きしない


async def test_ring1_source_is_none_when_data_is_too_thin(session):
    """対象地区の土地レコードが3件未満なら自動推定しない（Ring1=Ring0 のまま）。"""
    await seed(session, [raw(district=HOME_DISTRICT, price="20000000", area="200")])
    bare = RingContext(
        district_code=HOME_DISTRICT,
        home_city="99001",
        target_cities=("99001",),
        neighbor_districts=(),
    )

    resolved, source = await resolve_ring1(session, bare)

    assert source == RING1_SOURCE_NONE
    assert resolved.neighbor_districts == ()


async def test_list_districts_returns_counts(session):
    await seed(
        session,
        [
            raw(district=HOME_DISTRICT, price="20000000"),
            raw(district=HOME_DISTRICT, price="21000000"),
            raw(district=OTHER_CITY_DISTRICT, city="99002", price="22000000"),
        ],
    )
    options = await list_districts(session)
    by_code = {o.code: o for o in options}
    assert by_code[HOME_DISTRICT].count == 2
    assert by_code[OTHER_CITY_DISTRICT].municipality == "となり町"
    assert "件）" in by_code[HOME_DISTRICT].label

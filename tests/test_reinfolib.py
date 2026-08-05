"""reinfolib クライアントの単体テスト（ネットワークに出ずに MockTransport で検証）。"""

from __future__ import annotations

import httpx
import pytest

from app.config import Settings
from app.services.reinfolib import (
    ReinfolibClient,
    ReinfolibError,
    content_hash,
    normalize,
    parse_building_year,
    parse_period,
    quarters_window,
)

# Phase 0 で実際に取得したサンプル市 2024Q4 のレスポンス形をそのまま縮小したもの
SAMPLE = {
    "status": "OK",
    "data": [
        {
            "PriceCategory": "不動産取引価格情報",
            "Type": "宅地(土地)",
            "MunicipalityCode": "99001",
            "Prefecture": "静岡県",
            "Municipality": "サンプル市",
            "DistrictName": "地区40",
            "DistrictCode": "990010040",
            "TradePrice": "30000000",
            "Area": "300",
            "UnitPrice": "98000",
            "TotalFloorArea": "",
            "BuildingYear": "",
            "Period": "2024年第4四半期",
            "CityPlanning": "第１種住居地域",
        },
        {
            "PriceCategory": "成約価格情報",
            "Type": "宅地(土地と建物)",
            "MunicipalityCode": "99001",
            "Prefecture": "静岡県",
            "Municipality": "サンプル市",
            "DistrictName": "地区41",
            "DistrictCode": "990010041",
            "TradePrice": "18000000",
            "Area": "210",
            "UnitPrice": "",
            "TotalFloorArea": "145",
            "BuildingYear": "1993年",
            "Period": "2024年第4四半期",
        },
        {  # 区分マンション（A-1 で対象に追加）。実データどおり TotalFloorArea は空・Area=専有面積
            "PriceCategory": "成約価格情報",
            "Type": "中古マンション等",
            "MunicipalityCode": "99001",
            "Prefecture": "静岡県",
            "Municipality": "サンプル市",
            "DistrictName": "地区40",
            "DistrictCode": "990010040",
            "TradePrice": "8500000",
            "Area": "85",
            "UnitPrice": "",
            "TotalFloorArea": "",
            "BuildingYear": "1981年",
            "Structure": "ＲＣ",
            "FloorPlan": "３ＬＤＫ",
            "Period": "2024年第4四半期",
        },
        {  # 農地は落ちる
            "Type": "農地",
            "TradePrice": "700000",
            "Area": "700",
            "Period": "2024年第4四半期",
            "DistrictCode": "990010042",
        },
    ],
}


def _settings(**kw) -> Settings:
    base = dict(reinfolib_api_key="dummy", reinfolib_min_interval_sec=0.0, reinfolib_max_retries=3)
    base.update(kw)
    return Settings(**base)


def test_parse_building_year():
    assert parse_building_year("1993年") == 1993
    assert parse_building_year("") is None
    assert parse_building_year(None) is None
    assert parse_building_year("戦前") is None  # パース不能でも落ちない


def test_parse_period():
    assert parse_period("2024年第4四半期") == (2024, 4)
    with pytest.raises(ValueError):
        parse_period("")


def test_normalize_land_uses_land_area():
    record = normalize(SAMPLE["data"][0])
    assert record is not None
    assert record.land_type == "宅地(土地)"
    assert record.trade_price == 30_000_000
    assert record.unit_price == pytest.approx(100_000)  # 3000万 ÷ 300㎡（API の UnitPrice は使わない）
    assert record.land_unit_price == pytest.approx(100_000)
    assert record.building_year is None
    assert (record.period_year, record.period_quarter) == (2024, 4)


def test_normalize_house_uses_total_floor_area():
    record = normalize(SAMPLE["data"][1])
    assert record is not None
    assert record.unit_price == pytest.approx(18_000_000 / 145)  # 主指標＝延床単価
    assert record.land_unit_price == pytest.approx(18_000_000 / 210)  # 参考＝土地単価
    assert record.building_year == 1993
    assert record.price_category == "成約価格情報"


def test_normalize_house_without_total_floor_area_has_no_primary_unit_price():
    raw = dict(SAMPLE["data"][1], TotalFloorArea="")
    record = normalize(raw)
    assert record is not None
    assert record.unit_price is None  # 判定母集団からは除外される
    assert record.land_unit_price is not None


def test_normalize_condo_uses_exclusive_area():
    """区分は Area が専有面積なので、主指標は土地と同じ「総額÷Area」になる。

    実データ確認（docs/design.md）: TotalFloorArea は全件空・BuildingYear は充足。
    延床が空でも主指標が None にならないことがこのテストの本体。
    """
    record = normalize(SAMPLE["data"][2])
    assert record is not None
    assert record.land_type == "中古マンション等"
    assert record.total_floor_area is None
    assert record.unit_price == pytest.approx(100_000)  # 850万 ÷ 85㎡
    assert record.land_unit_price == pytest.approx(100_000)  # 区分では主指標と同値
    assert record.building_year == 1981
    assert record.structure == "ＲＣ"


def test_normalize_rejects_out_of_scope_types():
    assert normalize(SAMPLE["data"][3]) is None  # 農地
    assert normalize(dict(SAMPLE["data"][0], Type="林地")) is None


def test_content_hash_is_stable_and_discriminating():
    a = SAMPLE["data"][0]
    b = dict(a, TradePrice="31000000")
    assert content_hash(a) == content_hash(dict(a))
    assert content_hash(a) != content_hash(b)


async def test_fetch_quarter_filters_and_sends_api_key():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("Ocp-Apim-Subscription-Key")
        return httpx.Response(200, json=SAMPLE)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = ReinfolibClient(_settings(), client=http)
        records = await client.fetch_quarter("99001", 2024, 4)

    assert seen["key"] == "dummy"
    assert "XIT001" in seen["url"] and "city=99001" in seen["url"]
    assert "year=2024" in seen["url"] and "quarter=4" in seen["url"]
    # 対象3種別だけが残る（農地は落ちる）
    assert len(records) == 3
    assert {r.land_type for r in records} == {
        "宅地(土地)",
        "宅地(土地と建物)",
        "中古マンション等",
    }


async def test_fetch_quarter_retries_on_429_then_succeeds():
    calls = {"n": 0}
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="too many requests")
        return httpx.Response(200, json=SAMPLE)

    async def fake_sleep(sec: float) -> None:
        slept.append(sec)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = ReinfolibClient(_settings(), client=http, sleep=fake_sleep)
        records = await client.fetch_quarter("99001", 2024, 4)

    assert calls["n"] == 2
    assert slept and slept[0] == 1  # 指数バックオフ 1秒 → 2秒 …
    assert len(records) == 3


async def test_fetch_quarter_treats_404_as_empty():
    """未公開の四半期は 404「検索結果がありません」→ 空リスト（バッチを止めない）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "検索結果がありません。"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = ReinfolibClient(_settings(), client=http)
        assert await client.fetch_quarter("99001", 2026, 1) == []


async def test_fetch_quarter_raises_on_401_without_retry():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text="Access denied")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = ReinfolibClient(_settings(), client=http, sleep=lambda s: _noop())
        with pytest.raises(ReinfolibError):
            await client.fetch_quarter("99001", 2024, 4)

    assert calls["n"] == 1  # 認証エラーはリトライしない


async def _noop():
    return None


async def test_throttle_waits_between_requests():
    """1req/秒スロットルが効く（実時間は待たず、注入した時計で検証）。"""
    now = {"t": 0.0}
    slept: list[float] = []

    async def fake_sleep(sec: float) -> None:
        slept.append(sec)
        now["t"] += sec

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=SAMPLE)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = ReinfolibClient(
            _settings(reinfolib_min_interval_sec=1.0),
            client=http,
            sleep=fake_sleep,
            monotonic=lambda: now["t"],
        )
        await client.fetch_quarter("99001", 2024, 4)
        await client.fetch_quarter("99001", 2024, 3)

    assert slept == [1.0]  # 2回目の直前に1秒待つ


def test_missing_api_key_is_rejected_early():
    with pytest.raises(ReinfolibError):
        ReinfolibClient(_settings(reinfolib_api_key=""))


def test_quarters_window_covers_three_years():
    window = quarters_window(2025, 2, years=3)
    assert len(window) == 12
    assert window[0] == (2022, 3)
    assert window[-1] == (2025, 2)

"""エンドポイントのスモーク＋機能テスト（500 が出ないこと・判定結果が返ること）。"""

from __future__ import annotations

from app.constants import TYPE_HOUSE, TYPE_LAND
from app.db import get_sessionmaker
from app.services.reinfolib import normalize
from app.services.storage import upsert_transactions
from tests.test_districts import HOME_DISTRICT, NEIGHBOR_DISTRICT, raw


async def _seed(raws: list[dict]) -> None:
    async with get_sessionmaker()() as session:
        records = [r for r in (normalize(x) for x in raws) if r is not None]
        await upsert_transactions(session, records)


async def test_healthz(client):
    res = await client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


async def test_index_renders_without_data(client):
    res = await client.get("/")
    assert res.status_code == 200
    assert "取込バッチを実行" in res.text


async def test_transactions_page_and_json(client):
    await _seed(
        [
            raw(district=HOME_DISTRICT, price="20000000", area="200"),
            raw(district=NEIGHBOR_DISTRICT, price="24000000", area="200"),
        ]
    )

    page = await client.get("/transactions")
    assert page.status_code == 200
    assert "20,000,000" in page.text

    filtered = await client.get("/transactions", params={"district_code": HOME_DISTRICT})
    assert filtered.status_code == 200
    assert "24,000,000" not in filtered.text

    api = await client.get("/api/transactions")
    assert api.status_code == 200
    body = api.json()
    assert len(body) == 2
    assert body[0]["unit_price"] in (100_000.0, 120_000.0)
    assert body[0]["period"] == "2024年Q4"


async def test_valuate_htmx_returns_ratio_and_confidence(client):
    # 同一地区に 100,000〜140,000 円/㎡ の土地事例5件
    await _seed(
        [
            raw(district=HOME_DISTRICT, price=str(p), area="200")
            for p in (20_000_000, 22_000_000, 24_000_000, 26_000_000, 28_000_000)
        ]
    )

    res = await client.post(
        "/valuate",
        data={
            "land_type": TYPE_LAND,
            "district_code": HOME_DISTRICT,
            "listing_price": "20400000",  # 102,000円/㎡ → 中央値 120,000 比 −15%
            "area": "200",
        },
    )
    assert res.status_code == 200
    assert "-15.0%" in res.text
    assert "割安" in res.text
    assert "信頼度" in res.text and ">A（" in res.text.replace("<td>", ">")
    assert "5 件" in res.text


async def test_valuate_htmx_reports_unjudgeable(client):
    res = await client.post(
        "/valuate",
        data={
            "land_type": TYPE_LAND,
            "district_code": HOME_DISTRICT,
            "listing_price": "20000000",
            "area": "200",
        },
    )
    assert res.status_code == 200  # HTMX パーシャルとしては正常応答
    assert "判定不能" in res.text


async def test_valuate_htmx_validation_error(client):
    """中古戸建で延床未入力 → 500 ではなく 400 とメッセージ。"""
    res = await client.post(
        "/valuate",
        data={
            "land_type": TYPE_HOUSE,
            "district_code": HOME_DISTRICT,
            "listing_price": "30000000",
            "area": "200",
        },
    )
    assert res.status_code == 400
    assert "延床面積" in res.text


async def test_valuate_json_api(client):
    await _seed(
        [
            raw(district=HOME_DISTRICT, price=str(p), area="200")
            for p in (20_000_000, 22_000_000, 24_000_000, 26_000_000, 28_000_000)
        ]
    )
    res = await client.post(
        "/api/valuate",
        json={
            "land_type": TYPE_LAND,
            "district_code": HOME_DISTRICT,
            "listing_price": 20_400_000,
            "area": 200,
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["judgeable"] is True
    assert body["median_unit_price"] == 120_000
    assert round(body["ratio_percent"], 1) == -15.0
    assert body["band"] == "割安"
    assert body["confidence"] == "A"
    assert body["case_count"] == 5
    assert len(body["cases"]) == 5


async def test_valuate_json_expands_and_labels_confidence_b(client):
    """同一地区に事例が無く、対象市まで広げて判定 → 信頼度 B ＋ 緩和内容の明示。"""
    await _seed(
        [
            raw(district="990010099", price=str(p), area="200")
            for p in (20_000_000, 22_000_000, 24_000_000, 26_000_000, 28_000_000)
        ]
    )
    res = await client.post(
        "/api/valuate",
        json={
            "land_type": TYPE_LAND,
            "district_code": HOME_DISTRICT,
            "listing_price": 30_000_000,
            "area": 200,
        },
    )
    body = res.json()
    assert body["confidence"] == "B"
    assert body["ring_label"] == "基準市全域"
    assert body["relaxations"]
    assert body["band"] == "割高"


def _house(price: int, tfa: str, *, area: str = "200", district: str = HOME_DISTRICT) -> dict:
    return raw(
        district=district,
        land_type=TYPE_HOUSE,
        price=str(price),
        area=area,
        tfa=tfa,
        year="2000年",
    )


async def test_valuate_json_house_reports_trim_dispersion_and_ring1_source(client):
    """戸建: 延床で絞り、外れ値をトリムし、除外件数・Ring1 由来・従指標を返す。"""
    await _seed(
        [
            # 延床100〜110㎡・㎡単価 200,000〜250,000 の事例6件
            _house(20_000_000, "100"),
            _house(21_000_000, "100"),
            _house(22_000_000, "100"),
            _house(23_000_000, "100"),
            _house(24_000_000, "100"),
            _house(25_000_000, "100"),
            # 古家付き土地相当（延床45㎡）→ 延床フィルタで母集団に入らない
            _house(15_000_000, "45"),
        ]
    )
    res = await client.post(
        "/api/valuate",
        json={
            "land_type": TYPE_HOUSE,
            "district_code": HOME_DISTRICT,
            "listing_price": 22_500_000,
            "area": 200,
            "total_floor_area": 100,
            "building_year": 2000,
        },
    )

    assert res.status_code == 200
    body = res.json()
    assert body["judgeable"] is True
    assert body["case_count"] == 6  # 延床45㎡は入らない
    assert body["trimmed_count"] == 0
    assert body["dispersion_flag"] is False
    assert body["median_land_unit_price"] == 112_500  # 従指標（22.5M ÷ 200㎡ の中央値）
    assert body["ring1_source"] == "手動設定"  # テスト env は NEIGHBOR_DISTRICTS 設定済み
    assert all(c["total_floor_area"] == 100 for c in body["cases"])


async def test_valuate_htmx_shows_trim_and_ring1_source(client):
    await _seed(
        [
            _house(20_000_000, "100"),
            _house(21_000_000, "100"),
            _house(22_000_000, "100"),
            _house(23_000_000, "100"),
            _house(24_000_000, "100"),
            _house(25_000_000, "100"),
            # 極端に高い1件（㎡単価 900,000）→ IQR トリムで除外される
            _house(90_000_000, "100"),
        ]
    )
    res = await client.post(
        "/valuate",
        data={
            "land_type": TYPE_HOUSE,
            "district_code": HOME_DISTRICT,
            "listing_price": "22500000",
            "area": "200",
            "total_floor_area": "100",
            "building_year": "2000",
        },
    )

    assert res.status_code == 200
    assert "外れ値 1 件を除外" in res.text
    assert "近隣地区（Ring1）の由来" in res.text
    assert "手動設定" in res.text
    assert "参考: 土地面積単価" in res.text


async def test_openapi_and_all_routes_smoke(client):
    """全エンドポイントを一巡し 500 が出ないことを確認（fastapi-endpoint-tester 相当）。"""
    schema = await client.get("/openapi.json")
    assert schema.status_code == 200

    checks = [
        ("GET", "/healthz", {}),
        ("GET", "/", {}),
        ("GET", "/transactions", {}),
        ("GET", "/api/transactions", {}),
        (
            "POST",
            "/valuate",
            {
                "data": {
                    "land_type": TYPE_LAND,
                    "district_code": HOME_DISTRICT,
                    "listing_price": "20000000",
                    "area": "200",
                }
            },
        ),
        (
            "POST",
            "/api/valuate",
            {
                "json": {
                    "land_type": TYPE_LAND,
                    "district_code": HOME_DISTRICT,
                    "listing_price": 20_000_000,
                    "area": 200,
                }
            },
        ),
    ]
    for method, path, kwargs in checks:
        res = await client.request(method, path, **kwargs)
        assert res.status_code < 500, f"{method} {path} → {res.status_code}: {res.text[:200]}"

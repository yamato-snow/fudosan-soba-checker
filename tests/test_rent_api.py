"""賃料相場の画面（HTMX）。取込は必ず本番経路（POST /rent/import）を通す。

合成データは架空自治体コード 99001／地区コード 990010040 系のみ（匿名化の巻き戻し禁止）。
"""

from __future__ import annotations

import re

from app.constants import RENT_KIND_CONDO, RENT_KIND_HOUSE
from app.services.rent_import import CSV_TEMPLATE_HEADER

HOME_DISTRICT = "990010040"
NEIGHBOR_DISTRICT = "990010041"


def rent_row(
    *,
    district: str = HOME_DISTRICT,
    kind: str = RENT_KIND_CONDO,
    rent: int = 85_000,
    admin: int = 5_000,
    area: float = 62.5,
    year: int = 2005,
    s_year: int = 2026,
    s_month: int = 8,
) -> str:
    return ",".join(
        [
            district,
            f"サンプル市地区{district[-2:]}",
            kind,
            str(rent),
            str(admin),
            str(area),
            "3LDK",
            str(year),
            "ＲＣ",
            "3",
            "8",
            "8",
            "無",
            str(s_year),
            str(s_month),
            "募集中",
        ]
    )


def synthetic_csv(count: int = 30) -> str:
    """合成CSV。㎡単価が 1,280〜1,620 円/㎡ 前後に散る30件（同一地区・築年も近い）。"""
    rows = []
    for i in range(count):
        rows.append(
            rent_row(
                rent=80_000 + (i % 10) * 1_500,
                admin=4_000 + (i % 5) * 500,
                area=58.0 + (i % 6) * 2.5,
                year=2003 + (i % 5),
                s_month=1 + (i % 12),
            )
        )
    return "\n".join([CSV_TEMPLATE_HEADER, *rows]) + "\n"


async def import_csv(client, text: str, *, source_ref: str = "t1", purge: bool = False):
    data = {"source_ref": source_ref, "csv_text": text}
    if purge:
        data["purge_first"] = "true"
    return await client.post("/rent/import", data=data)


# --- 画面が開く ---------------------------------------------------------


async def test_rent_page_tells_you_to_import_when_empty(client):
    res = await client.get("/rent")
    assert res.status_code == 200
    assert "賃料事例がまだ1件もありません" in res.text
    assert "/rent/import" in res.text


async def test_rent_import_and_listings_pages_render(client):
    assert (await client.get("/rent/import")).status_code == 200
    listings = await client.get("/rent/listings")
    assert listings.status_code == 200
    assert "賃料事例がありません" in listings.text


# --- 取込 ---------------------------------------------------------------


async def test_import_is_idempotent_through_the_endpoint(client):
    first = await import_csv(client, synthetic_csv())
    assert first.status_code == 200
    assert "新規 <strong>30</strong> 件" in first.text

    second = await import_csv(client, synthetic_csv())
    assert "新規 <strong>0</strong> 件" in second.text
    assert "既存スキップ 30 件" in second.text
    assert "賃料事例は合計 30 件" in second.text


async def test_import_reports_bad_rows_but_keeps_the_good_ones(client):
    text = "\n".join(
        [
            CSV_TEMPLATE_HEADER,
            rent_row(),
            rent_row(kind="一棟", rent=70_000),
            rent_row(rent=95_000, area=70),
        ]
    )
    res = await import_csv(client, text)
    assert res.status_code == 200
    assert "新規 <strong>2</strong> 件" in res.text
    assert "エラー 1 件" in res.text
    assert "種別が不正です" in res.text


async def test_import_rejects_broken_header_with_400(client):
    res = await import_csv(client, "地区コード,種別\n990010040,区分\n")
    assert res.status_code == 400
    assert "必須列" in res.text


async def test_purge_then_reimport_replaces_the_label(client):
    await import_csv(client, synthetic_csv(), source_ref="aug")
    replaced = await import_csv(client, synthetic_csv(5), source_ref="aug", purge=True)
    assert "事前削除 30 件" in replaced.text
    assert "賃料事例は合計 5 件" in replaced.text


async def test_listings_page_shows_imported_rows(client):
    await import_csv(client, synthetic_csv(3))
    res = await client.get("/rent/listings", params={"property_kind": RENT_KIND_CONDO})
    assert res.status_code == 200
    assert "3 件を表示" in res.text
    assert "募集賃料ベース" in res.text


# --- 判定 ---------------------------------------------------------------


async def test_valuate_with_thirty_cases_returns_confidence_a(client):
    await import_csv(client, synthetic_csv())
    res = await client.post(
        "/rent/valuate",
        data={
            "property_kind": RENT_KIND_CONDO,
            "district_code": HOME_DISTRICT,
            "exclusive_area": "62.5",
            "building_year": "2005",
            "target_monthly": "",
        },
    )
    assert res.status_code == 200
    assert "想定月額賃料レンジ" in res.text
    assert re.search(r"<td>A（同一・近隣地区の初期条件で十分な事例数）</td>", res.text)
    # 想定賃料を入れていないのでバンドは出さない
    assert "相場中央値比" not in res.text
    # 鮮度は係数に溶かさず年レンジで見せる
    assert "事例の調査時点" in res.text
    assert "2026〜2026年" in res.text
    assert "募集賃料ベース" in res.text


async def test_valuate_with_target_rent_shows_band(client):
    await import_csv(client, synthetic_csv())
    res = await client.post(
        "/rent/valuate",
        data={
            "property_kind": RENT_KIND_CONDO,
            "district_code": HOME_DISTRICT,
            "exclusive_area": "62.5",
            "building_year": "2005",
            "target_monthly": "150000",
        },
    )
    assert res.status_code == 200
    assert "相場中央値比" in res.text
    assert "band-割高" in res.text


async def test_valuate_without_cases_offers_the_import_route(client):
    res = await client.post(
        "/rent/valuate",
        data={
            "property_kind": RENT_KIND_CONDO,
            "district_code": HOME_DISTRICT,
            "exclusive_area": "62.5",
            "building_year": "",
            "target_monthly": "",
        },
    )
    assert res.status_code == 200
    assert "判定不能" in res.text
    assert "あと 3 件登録すると判定できます" in res.text
    assert 'href="/rent/import"' in res.text


async def test_relaxation_wording_uses_exclusive_area_for_condo(client):
    """賃料の面積軸は「専有面積」と出る（`Criteria.floor_label`）。"""
    # 対象市の別地区に、面積が離れた事例だけを置く → 最終段まで緩和される
    rows = [
        rent_row(district=NEIGHBOR_DISTRICT, rent=110_000, area=88.0, year=1995 + i)
        for i in range(5)
    ]
    await import_csv(client, "\n".join([CSV_TEMPLATE_HEADER, *rows]) + "\n")

    res = await client.post(
        "/rent/valuate",
        data={
            "property_kind": RENT_KIND_CONDO,
            "district_code": HOME_DISTRICT,
            "exclusive_area": "62.5",
            "building_year": "2005",
            "target_monthly": "",
        },
    )
    assert res.status_code == 200
    assert "専有面積を±50%に緩和" in res.text
    assert "土地面積" not in res.text


async def test_house_kind_is_a_separate_population(client):
    await import_csv(client, synthetic_csv())
    res = await client.post(
        "/rent/valuate",
        data={
            "property_kind": RENT_KIND_HOUSE,
            "district_code": HOME_DISTRICT,
            "exclusive_area": "62.5",
            "building_year": "2005",
            "target_monthly": "",
        },
    )
    assert res.status_code == 200
    assert "判定不能" in res.text  # 区分30件は戸建の相場ではない


async def test_bad_kind_returns_400(client):
    res = await client.post(
        "/rent/valuate",
        data={
            "property_kind": "一棟",
            "district_code": HOME_DISTRICT,
            "exclusive_area": "62.5",
        },
    )
    assert res.status_code == 400


async def test_rent_routes_smoke(client):
    await import_csv(client, synthetic_csv(6))
    checks = [
        ("GET", "/rent", {}),
        ("GET", "/rent/import", {}),
        ("GET", "/rent/listings", {}),
        (
            "POST",
            "/rent/valuate",
            {
                "data": {
                    "property_kind": RENT_KIND_CONDO,
                    "district_code": HOME_DISTRICT,
                    "exclusive_area": "62.5",
                }
            },
        ),
    ]
    for method, path, kwargs in checks:
        res = await client.request(method, path, **kwargs)
        assert res.status_code < 500, f"{method} {path} → {res.status_code}: {res.text[:200]}"

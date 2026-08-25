"""収支シミュレーション画面（B-1: 税引前）。"""

from __future__ import annotations

from app.constants import TYPE_CONDO, TYPE_LAND
from app.db import get_sessionmaker
from app.services.reinfolib import normalize
from app.services.storage import upsert_transactions
from tests.test_districts import HOME_DISTRICT, raw
from tests.test_rent_api import import_csv, synthetic_csv

FORM = {
    "price": "15000000",
    "acquisition_costs": "1000000",
    "monthly_rent": "90000",
    "vacancy_rate_percent": "5",
    "monthly_admin_fee": "8000",
    "monthly_repair_reserve": "7000",
    "pm_fee_rate_percent": "5",
    "annual_property_tax": "80000",
    "loan_principal": "12000000",
    "loan_rate_percent": "2.0",
    "loan_years": "25",
    # 実フォームは未入力欄を空文字で送る
    "monthly_other_income": "",
    "rent_decline_rate_percent": "",
    "annual_insurance": "",
    "annual_other": "",
    "restoration_cost": "",
    "ad_cost": "",
    "turnover_years": "",
    "capex_annual": "",
    "years": "",
}


async def _seed_sales(raws: list[dict]) -> None:
    async with get_sessionmaker()() as session:
        records = [r for r in (normalize(x) for x in raws) if r is not None]
        await upsert_transactions(session, records)


async def test_cashflow_page_renders_without_any_query(client):
    res = await client.get("/cashflow")
    assert res.status_code == 200
    assert "収支を計算する" in res.text
    assert "割安判定の画面から遷移すると" in res.text


async def test_valuation_result_links_to_cashflow_with_the_inputs(client):
    await _seed_sales(
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
            "listing_price": "25000000",
            "area": "200",
        },
    )
    assert res.status_code == 200
    assert "この物件で収支シミュレーション" in res.text
    assert "price=25000000" in res.text
    assert "area=200" in res.text
    assert f"district_code={HOME_DISTRICT}" in res.text


async def test_cashflow_prefills_rent_from_the_rent_cases(client):
    await import_csv(client, synthetic_csv())
    res = await client.get(
        "/cashflow",
        params={
            "land_type": TYPE_CONDO,
            "district_code": HOME_DISTRICT,
            "area": "62.5",
            "building_year": "2005",
            "price": "15000000",
        },
    )
    assert res.status_code == 200
    assert "自動入力しました" in res.text
    # 賃料㎡単価の中央値 × 面積 が value に入っている（空欄のままではない）
    import re

    field = re.search(r'id="monthly_rent"[^>]*value="(\d+)"', res.text)
    assert field and int(field.group(1)) > 0


async def test_cashflow_says_why_it_did_not_prefill_when_cases_are_thin(client):
    res = await client.get(
        "/cashflow",
        params={
            "land_type": TYPE_CONDO,
            "district_code": HOME_DISTRICT,
            "area": "62.5",
        },
    )
    assert res.status_code == 200
    assert "件しかないため自動入力していません" in res.text


async def test_cashflow_does_not_prefill_for_land(client):
    res = await client.get("/cashflow", params={"land_type": TYPE_LAND, "area": "200"})
    assert res.status_code == 200
    assert "土地は賃料相場の対象外" in res.text


async def test_simulate_returns_metrics_and_thirty_year_table(client):
    res = await client.post("/cashflow/simulate", data=FORM)
    assert res.status_code == 200
    assert "表面利回り" in res.text
    assert "DSCR" in res.text
    assert "30年の明細を見る" in res.text
    assert 'class="scroll"' in res.text  # モバイル幅で横スクロールできる
    assert "<details>" in res.text
    assert "税引前" in res.text


async def test_simulate_shows_dash_for_dscr_on_a_cash_purchase(client):
    form = {**FORM, "loan_principal": "", "loan_rate_percent": "", "loan_years": ""}
    res = await client.post("/cashflow/simulate", data=form)
    assert res.status_code == 200
    assert "—" in res.text
    assert "∞" not in res.text


async def test_simulate_rejects_missing_price_with_400(client):
    res = await client.post("/cashflow/simulate", data={**FORM, "price": ""})
    assert res.status_code == 400
    assert "price" in res.text


async def test_simulate_rejects_zero_rent_with_400(client):
    res = await client.post("/cashflow/simulate", data={**FORM, "monthly_rent": "0"})
    assert res.status_code == 400


async def test_full_vacancy_shows_the_warning(client):
    res = await client.post("/cashflow/simulate", data={**FORM, "vacancy_rate_percent": "100"})
    assert res.status_code == 200
    assert "NOI がマイナス" in res.text


async def test_cashflow_routes_smoke(client):
    checks = [
        ("GET", "/cashflow", {}),
        ("POST", "/cashflow/simulate", {"data": FORM}),
    ]
    for method, path, kwargs in checks:
        res = await client.request(method, path, **kwargs)
        assert res.status_code < 500, f"{method} {path} → {res.status_code}: {res.text[:200]}"

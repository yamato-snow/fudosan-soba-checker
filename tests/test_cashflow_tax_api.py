"""税引後・出口（B-2）の画面。免責と警告の出し分けをここで押さえる。"""

from __future__ import annotations

from app.services.tax_tables import TAX_TABLE_VERSION
from tests.test_cashflow_api import FORM

TAX_FORM = {
    **FORM,
    "other_income": "5000000",
    "building_ratio": "0.4",
    "structure": "RC",
    "building_age": "20",
    "resident_tax_rate_percent": "",
    "useful_life_override": "",
    "exit_year": "",
    "exit_sale_price": "",
    "exit_sale_cost_rate_percent": "",
}

EXIT_FORM = {**TAX_FORM, "exit_year": "10", "exit_sale_price": "13000000"}


async def test_form_offers_the_tax_fields_without_a_default_building_ratio(client):
    res = await client.get("/cashflow")
    assert res.status_code == 200
    assert 'id="building_ratio"' in res.text
    assert "目安: 木造 0.5 / RC 0.4" in res.text  # placeholder に出すだけ
    assert 'id="building_ratio"' in res.text and 'name="building_ratio" type="number" min="0.01"' in res.text
    # value= が入っていない＝既定値で計算が走らない
    ratio_tag = res.text.split('id="building_ratio"')[1].split(">")[0]
    assert "value=" not in ratio_tag


async def test_after_tax_result_shows_tax_columns_and_dead_cross(client):
    res = await client.post("/cashflow/simulate", data=TAX_FORM)
    assert res.status_code == 200
    assert "減価償却" in res.text
    assert "不動産所得" in res.text
    assert "デッドクロス" in res.text
    assert "耐用年数（償却期間）" in res.text
    assert "31 年" in res.text  # RC 築20年 → (47−20)+20×0.2 = 31


async def test_after_tax_result_shows_the_disclaimers(client):
    res = await client.post("/cashflow/simulate", data=TAX_FORM)
    assert "税務判断は必ず税理士にご確認ください" in res.text
    assert TAX_TABLE_VERSION in res.text  # 版を併記する
    assert "期間中一定として計算しています" in res.text  # 運営費（常時）


async def test_building_ratio_sensitivity_is_three_points(client):
    res = await client.post("/cashflow/simulate", data=EXIT_FORM)
    assert res.status_code == 200
    assert "建物割合を ±0.10 動かしたときの総利益" in res.text
    assert "0.30" in res.text and "0.40（入力値）" in res.text and "0.50" in res.text


async def test_missing_building_ratio_is_rejected(client):
    res = await client.post("/cashflow/simulate", data={**TAX_FORM, "building_ratio": ""})
    assert res.status_code == 400
    assert "建物割合" in res.text


async def test_out_of_range_building_ratio_is_rejected(client):
    for value in ("0", "1", "1.2", "-0.1"):
        res = await client.post("/cashflow/simulate", data={**TAX_FORM, "building_ratio": value})
        assert res.status_code == 400, value


async def test_unknown_structure_is_rejected_rather_than_guessed(client):
    res = await client.post("/cashflow/simulate", data={**TAX_FORM, "structure": "ブロック造"})
    assert res.status_code == 400
    assert "耐用年数" in res.text


async def test_exit_shows_the_sale_breakdown_and_total_profit(client):
    res = await client.post("/cashflow/simulate", data=EXIT_FORM)
    assert res.status_code == 200
    assert "売却（10年目）" in res.text
    assert "長期譲渡 20.315%" in res.text
    assert "総利益" in res.text


async def test_short_term_exit_uses_the_short_term_rate(client):
    res = await client.post("/cashflow/simulate", data={**EXIT_FORM, "exit_year": "5"})
    assert res.status_code == 200
    assert "短期譲渡 39.63%" in res.text


async def test_land_interest_warning_appears_only_with_a_loan_and_a_loss_year(client):
    """無条件表示にすると「読まれない免責」になるので、条件が立ったときだけ出す。"""
    loss = await client.post(
        "/cashflow/simulate", data={**TAX_FORM, "monthly_rent": "40000", "vacancy_rate_percent": "20"}
    )
    assert "土地取得にかかる借入金利子" in loss.text
    assert "過大に見積もっています" in loss.text  # 方向を明示する

    cash = await client.post(
        "/cashflow/simulate",
        data={**TAX_FORM, "loan_principal": "", "loan_rate_percent": "", "loan_years": ""},
    )
    assert "土地取得にかかる借入金利子" not in cash.text


async def test_leaving_other_income_blank_falls_back_to_the_pretax_mode(client):
    res = await client.post("/cashflow/simulate", data={**TAX_FORM, "other_income": ""})
    assert res.status_code == 200
    assert "この結果は<strong>税引前</strong>です" in res.text
    assert "建物割合を ±0.10" not in res.text


async def test_tax_routes_smoke(client):
    for data in (TAX_FORM, EXIT_FORM):
        res = await client.post("/cashflow/simulate", data=data)
        assert res.status_code < 500, f"{res.status_code}: {res.text[:200]}"

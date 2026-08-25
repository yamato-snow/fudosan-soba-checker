"""収支シミュレーション（B-1: 税引前）の画面。

判定 → シミュレーションの受け渡しは**クエリ文字列**。`Valuation` に入力値のフィールドを
足さない（判定結果の器であって入力の器ではない）。URL がそのまま再現可能な入力になるので、
条件違いの比較がブラウザ履歴でできる＝セッションを導入しないで済む。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

# 賃料判定はルーター層の関数だが、prefill のために1回だけ呼ぶ（重複実装を作らない）
from app.api.routes_rent import assess_rent
from app.api.templates import templates
from app.config import Settings, get_settings
from app.constants import MIN_CASES_JUDGEABLE, RENT_KIND_BY_LAND_TYPE, TYPE_LABELS
from app.db import get_session
from app.schemas.cashflow import CashflowForm
from app.services.cashflow import simulate
from app.services.tax_tables import TAX_TABLE_VERSION

router = APIRouter()

# B-1 は税引前まで。税引後・出口（B-2）を有効にするときはここを True にする
# （plan のロールバック手順: ここを False に戻せば B-1 の状態で動き続ける）
WITH_TAX = False
WITH_EXIT = False

DISCLAIMER_ROUNDING = (
    "金額はすべて概算（円単位で四捨五入）です。実際の返済額・費用は契約条件で変わります。"
)
DISCLAIMER_OPERATING = (
    "管理費・修繕積立金・運営費は期間中一定として計算しています"
    "（実際は10〜15年で上昇する場合があります）。"
    "資本的支出は当年一括の経費として扱っています。"
)


async def _prefill_rent(
    session: AsyncSession,
    settings: Settings,
    *,
    land_type: str,
    district_code: str,
    area: float | None,
    building_year: int | None,
) -> tuple[int | None, str]:
    """賃料判定を1回走らせて想定月額賃料の初期値を作る。

    事例が足りなければ**空欄のまま理由を出す**（もっともらしい数字を勝手に埋めない）。
    """
    if not land_type:
        return None, "割安判定の画面から遷移すると、賃料事例から想定賃料を自動入力します"
    kind = RENT_KIND_BY_LAND_TYPE.get(land_type)
    if not kind:
        return None, "土地は賃料相場の対象外のため、想定賃料は自動入力していません"
    if not area or area <= 0 or not district_code:
        return None, "地区・面積が渡されていないため、想定賃料は自動入力していません"

    assessment = await assess_rent(
        session,
        settings,
        property_kind=kind,
        district_code=district_code,
        exclusive_area=area,
        building_year=building_year,
        target_monthly=None,
    )
    if not assessment.judgeable or assessment.median_monthly is None:
        return None, (
            f"賃料事例が {assessment.valuation.case_count} 件しかないため自動入力していません"
            f"（判定には最低 {MIN_CASES_JUDGEABLE} 件必要です）"
        )
    return assessment.median_monthly, (
        f"賃料㎡単価の中央値 × 面積 {area}㎡ から自動入力しました"
        f"（事例 {assessment.valuation.case_count} 件・信頼度 {assessment.valuation.confidence}）"
    )


@router.get("/cashflow", response_class=HTMLResponse)
async def cashflow_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    """入力フォーム。判定結果からのクエリ文字列で prefill する。"""
    query = dict(request.query_params)

    def number(key: str) -> float | None:
        raw = (query.get(key) or "").strip()
        try:
            return float(raw) if raw else None
        except ValueError:
            return None

    area = number("area")
    building_year = number("building_year")
    land_type = query.get("land_type", "")

    monthly_rent, rent_note = await _prefill_rent(
        session,
        settings,
        land_type=land_type,
        district_code=query.get("district_code", ""),
        area=area,
        building_year=int(building_year) if building_year else None,
    )

    return templates.TemplateResponse(
        request,
        "cashflow.html",
        {
            "price": number("price"),
            "area": area,
            "building_year": int(building_year) if building_year else None,
            "land_type": land_type,
            "land_type_label": TYPE_LABELS.get(land_type, ""),
            "district_code": query.get("district_code", ""),
            "monthly_rent": monthly_rent,
            "rent_note": rent_note,
            "with_tax": WITH_TAX,
            "tax_table_version": TAX_TABLE_VERSION,
        },
    )


@router.post("/cashflow/simulate", response_class=HTMLResponse)
async def cashflow_simulate(
    request: Request,
):
    """30項目のフォームを pydantic 1本で受ける（`Form(...)` を並べない）。"""
    payload = dict(await request.form())
    try:
        form = CashflowForm(**payload)
        result = simulate(form.to_input(with_tax=WITH_TAX, with_exit=WITH_EXIT))
    except ValidationError as exc:
        return templates.TemplateResponse(
            request, "_error.html", {"message": _readable(exc)}, status_code=400
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request, "_error.html", {"message": str(exc)}, status_code=400
        )

    return templates.TemplateResponse(
        request,
        "_cashflow_result.html",
        {
            "r": result,
            "with_tax": WITH_TAX,
            "tax_table_version": TAX_TABLE_VERSION,
            "disclaimer_rounding": DISCLAIMER_ROUNDING,
            "disclaimer_operating": DISCLAIMER_OPERATING,
        },
    )


def _readable(exc: ValidationError) -> str:
    """pydantic のエラーを日本語1行にまとめる（英語の JSON をそのまま出さない）。"""
    parts = []
    for error in exc.errors():
        field = ".".join(str(p) for p in error.get("loc", ())) or "入力"
        parts.append(f"{field}: {error.get('msg', '')}")
    return " ／ ".join(parts)

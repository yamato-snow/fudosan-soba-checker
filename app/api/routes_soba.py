"""HTMX 画面 + JSON API。"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.templates import templates
from app.config import Settings, get_settings
from app.constants import TARGET_TYPES, TYPE_HOUSE, TYPE_LABELS, TYPE_LAND
from app.db import get_session
from app.models.transaction import Transaction
from app.schemas.soba import CaseOut, TransactionOut, ValuationResponse
from app.services.districts import (
    RingContext,
    TargetProperty,
    fetch_cases,
    list_districts,
    resolve_ring1,
)
from app.services.valuation import (
    Valuation,
    expansion_driver,
    stages_for,
    unit_price_for_target,
)

router = APIRouter()


def _period(row: Transaction) -> str:
    return f"{row.period_year}年Q{row.period_quarter}"


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    """物件条件の入力フォーム。"""
    districts = await list_districts(session)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "districts": districts,
            "type_labels": TYPE_LABELS,
            "types": TARGET_TYPES,
            "home_district": settings.home_district,
            "type_land": TYPE_LAND,
            "type_house": TYPE_HOUSE,
        },
    )


@router.get("/transactions", response_class=HTMLResponse)
async def transactions_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=200, le=1000, ge=1),
    district_code: str = Query(default=""),
    land_type: str = Query(default=""),
):
    """Phase 1: 取込済みの相場データを一覧で見る。"""
    rows = await _query_transactions(session, limit, district_code, land_type)
    districts = await list_districts(session)
    return templates.TemplateResponse(
        request,
        "transactions.html",
        {
            "rows": rows,
            "districts": districts,
            "types": TARGET_TYPES,
            "type_labels": TYPE_LABELS,
            "selected_district": district_code,
            "selected_type": land_type,
            "period": _period,
        },
    )


@router.get("/api/transactions", response_model=list[TransactionOut])
async def transactions_json(
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=200, le=1000, ge=1),
    district_code: str = Query(default=""),
    land_type: str = Query(default=""),
) -> list[TransactionOut]:
    rows = await _query_transactions(session, limit, district_code, land_type)
    return [
        TransactionOut(
            id=r.id,
            price_category=r.price_category,
            land_type=r.land_type,
            municipality=r.municipality,
            district_name=r.district_name,
            district_code=r.district_code,
            trade_price=r.trade_price,
            area=r.area,
            total_floor_area=r.total_floor_area,
            building_year=r.building_year,
            unit_price=r.unit_price,
            period=_period(r),
        )
        for r in rows
    ]


async def _query_transactions(
    session: AsyncSession, limit: int, district_code: str, land_type: str
) -> list[Transaction]:
    stmt = select(Transaction)
    if district_code:
        stmt = stmt.where(Transaction.district_code == district_code)
    if land_type:
        stmt = stmt.where(Transaction.land_type == land_type)
    stmt = stmt.order_by(
        Transaction.period_year.desc(), Transaction.period_quarter.desc(), Transaction.id.desc()
    ).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def _valuate(
    session: AsyncSession,
    settings: Settings,
    *,
    land_type: str,
    district_code: str,
    listing_price: float,
    area: float,
    total_floor_area: float | None,
    building_year: int | None,
) -> Valuation:
    """入力 → 地区リングで事例抽出 → 判定。"""
    target_unit_price = unit_price_for_target(land_type, listing_price, area, total_floor_area)
    ctx = RingContext.from_settings(settings, district_code)
    # Ring1 は env が優先。未設定なら地価類似地区で自動推定する（未設定だと Ring1=Ring0 になり、
    # 地区が薄い実データでは大半がいきなり市全域へ飛んでしまうため）
    ctx, ring1_source = await resolve_ring1(session, ctx)
    target = TargetProperty(
        land_type=land_type,
        district_code=district_code,
        area=area,
        total_floor_area=total_floor_area,
        building_year=building_year,
    )

    # 段階拡張の判断は純粋ロジック（driver）が持ち、DB アクセスだけこちらで行う。
    # 必要な段階しか引かないので、初期条件で足りれば SELECT は1回で済む。
    driver = expansion_driver(target_unit_price, stages_for(land_type))
    criteria = next(driver)
    while True:
        cases = await fetch_cases(session, ctx, target, criteria)
        try:
            criteria = driver.send(cases)
        except StopIteration as stop:
            result: Valuation = stop.value
            result.ring1_source = ring1_source
            return result


@router.post("/valuate", response_class=HTMLResponse)
async def valuate_htmx(
    request: Request,
    land_type: str = Form(...),
    district_code: str = Form(...),
    listing_price: float = Form(...),
    area: float = Form(...),
    total_floor_area: float | None = Form(default=None),
    building_year: int | None = Form(default=None),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    """HTMX で結果パーシャルを返す。"""
    try:
        result = await _valuate(
            session,
            settings,
            land_type=land_type,
            district_code=district_code,
            listing_price=listing_price,
            area=area,
            total_floor_area=total_floor_area,
            building_year=building_year,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request, "_error.html", {"message": str(exc)}, status_code=400
        )

    return templates.TemplateResponse(
        request,
        "_result.html",
        {
            "v": result,
            "type_labels": TYPE_LABELS,
            # 判定 → シミュレーションの受け渡しはクエリ文字列（`Valuation` に入力値を足さない）。
            # 税パラメータは載せない（/cashflow 側のフォームで入力する）
            "cf_query": urlencode(
                {
                    "land_type": land_type,
                    "district_code": district_code,
                    "price": int(listing_price),
                    "area": area,
                    "building_year": building_year or "",
                }
            ),
        },
    )


@router.post("/api/valuate", response_model=ValuationResponse)
async def valuate_json(
    payload: dict,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ValuationResponse:
    from app.schemas.soba import ValuationRequest

    req = ValuationRequest(**payload)
    result = await _valuate(
        session,
        settings,
        land_type=req.land_type,
        district_code=req.district_code,
        listing_price=req.listing_price,
        area=req.area,
        total_floor_area=req.total_floor_area,
        building_year=req.building_year,
    )
    return ValuationResponse(
        judgeable=result.judgeable,
        target_unit_price=result.target_unit_price,
        median_unit_price=result.median_unit_price,
        ratio_percent=result.ratio_percent,
        band=result.band,
        case_count=result.case_count,
        ring_label=result.ring_label,
        confidence=result.confidence,
        confidence_note=result.confidence_note,
        relaxations=result.relaxations,
        message=result.message,
        median_land_unit_price=result.median_land_unit_price,
        trimmed_count=result.trimmed_count,
        dispersion_flag=result.dispersion_flag,
        ring1_source=result.ring1_source,
        cases=[
            CaseOut(
                district_name=c.district_name,
                land_type=c.land_type,
                trade_price=c.trade_price,
                area=c.area,
                total_floor_area=c.total_floor_area,
                building_year=c.building_year,
                unit_price=c.unit_price,
                period=f"{c.period_year}年Q{c.period_quarter}",
                price_category=c.price_category,
            )
            for c in result.cases
        ],
    )

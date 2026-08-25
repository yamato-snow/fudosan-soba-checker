"""賃料相場（A-2）の画面。売買側 `routes_soba.py` と同じ作法で書く。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.templates import templates
from app.config import Settings, get_settings
from app.constants import (
    RENT_KIND_LABELS,
    RENT_KINDS,
    RENT_NOTE_ASKING,
    RENT_STATUSES,
)
from app.db import get_session
from app.services.districts import DistrictOption, RingContext, list_districts, resolve_ring1
from app.services.rent import (
    RentAssessment,
    build_assessment,
    shortage_message,
    stages_for_rent,
    target_unit_price_for_rent,
)
from app.services.rent_import import (
    CSV_TEMPLATE_HEADER,
    ImportResult,
    RentImportError,
    decode_csv,
    parse_rent_csv,
)
from app.services.rent_repo import (
    RentTarget,
    fetch_rent_cases,
    list_rent_districts,
    query_rent_listings,
)
from app.services.rent_storage import (
    count_rent_listings,
    list_rent_sources,
    purge_rent_source,
    upsert_rent_listings,
)
from app.services.valuation import Valuation, expansion_driver

router = APIRouter()


async def _district_options(session: AsyncSession) -> list[DistrictOption]:
    """売買データと賃料事例の両方に出てくる地区（件数は**賃料事例の件数**）。

    賃料事例が0件でも地区を選べないと入口に立てないので、地区コードの体系は売買側から借りる。
    件数を賃料側にするのは「どこが薄いか」がその場で見えるようにするため。
    """
    rents = {d.code: d for d in await list_rent_districts(session)}
    merged: dict[str, DistrictOption] = {}
    for d in await list_districts(session):
        found = rents.get(d.code)
        merged[d.code] = DistrictOption(
            code=d.code,
            name=d.name,
            municipality=d.municipality,
            count=found.count if found else 0,
        )
    for code, d in rents.items():
        merged.setdefault(code, d)
    return sorted(merged.values(), key=lambda d: (d.municipality, d.name))


@router.get("/rent", response_class=HTMLResponse)
async def rent_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    """賃料相場チェックの入力フォーム。"""
    return templates.TemplateResponse(
        request,
        "rent.html",
        {
            "districts": await _district_options(session),
            "kinds": RENT_KINDS,
            "kind_labels": RENT_KIND_LABELS,
            "home_district": settings.home_district,
            "listing_count": await count_rent_listings(session),
            "note_asking": RENT_NOTE_ASKING,
        },
    )


async def assess_rent(
    session: AsyncSession,
    settings: Settings,
    *,
    property_kind: str,
    district_code: str,
    exclusive_area: float,
    building_year: int | None,
    target_monthly: float | None,
) -> RentAssessment:
    """入力 → 地区リングで賃料事例抽出 → 判定 → 月額に戻す。"""
    if property_kind not in RENT_KINDS:
        raise ValueError(f"種別は {' / '.join(RENT_KINDS)} のいずれかです")
    target_unit_price = target_unit_price_for_rent(exclusive_area, target_monthly)

    ctx = RingContext.from_settings(settings, district_code)
    # Ring1 の推定は売買（宅地の地価水準）で行う。賃料事例だけでは母集団が薄すぎて推定が立たない
    ctx, ring1_source = await resolve_ring1(session, ctx)
    target = RentTarget(
        property_kind=property_kind,
        district_code=district_code,
        exclusive_area=exclusive_area,
        building_year=building_year,
    )

    driver = expansion_driver(target_unit_price, stages_for_rent(property_kind))
    criteria = next(driver)
    while True:
        cases = await fetch_rent_cases(session, ctx, target, criteria)
        try:
            criteria = driver.send([c.to_valuation_case() for c in cases])
        except StopIteration as stop:
            result: Valuation = stop.value
            result.ring1_source = ring1_source
            return build_assessment(
                result,
                property_kind=property_kind,
                exclusive_area=exclusive_area,
                target_monthly=target_monthly,
            )


def _optional_number(raw: str, label: str) -> float | None:
    """空欄の任意入力を None にする。

    `<input type="number">` は未入力でも空文字を送るので、`float | None = Form(None)` だと
    「空欄のまま送ると 422」という使えない形になる。数値化はここで明示的にやる。
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"{label}は数値で入力してください: {text}") from exc


@router.post("/rent/valuate", response_class=HTMLResponse)
async def rent_valuate_htmx(
    request: Request,
    property_kind: str = Form(...),
    district_code: str = Form(...),
    exclusive_area: float = Form(...),
    building_year: str = Form(default=""),
    target_monthly: str = Form(default=""),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
):
    try:
        year = _optional_number(building_year, "築年")
        assessment = await assess_rent(
            session,
            settings,
            property_kind=property_kind,
            district_code=district_code,
            exclusive_area=exclusive_area,
            building_year=int(year) if year else None,
            target_monthly=_optional_number(target_monthly, "想定賃料"),
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request, "_error.html", {"message": str(exc)}, status_code=400
        )

    return templates.TemplateResponse(
        request,
        "_rent_result.html",
        {
            "a": assessment,
            "v": assessment.valuation,
            "kind_labels": RENT_KIND_LABELS,
            "note_asking": RENT_NOTE_ASKING,
            "shortage": shortage_message(assessment.shortfall),
        },
    )


@router.get("/rent/import", response_class=HTMLResponse)
async def rent_import_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """CSV 取込（textarea 貼り付け＋ファイル）。"""
    return templates.TemplateResponse(
        request,
        "rent_import.html",
        {
            "template_header": CSV_TEMPLATE_HEADER,
            "sources": await list_rent_sources(session),
            "kinds": RENT_KINDS,
            "statuses": RENT_STATUSES,
        },
    )


@router.post("/rent/import", response_class=HTMLResponse)
async def rent_import_submit(
    request: Request,
    source_ref: str = Form(...),
    csv_text: str = Form(default=""),
    purge_first: bool = Form(default=False),
    file: UploadFile | None = File(default=None),
    session: AsyncSession = Depends(get_session),
):
    """貼り付けテキスト or ファイルを取り込む。1行の書き損じで全体を捨てない（部分成功）。"""
    text = csv_text
    if file is not None and file.filename:
        text = decode_csv(await file.read())

    try:
        parsed: ImportResult = parse_rent_csv(text, source_ref=source_ref)
    except RentImportError as exc:
        return templates.TemplateResponse(
            request, "_error.html", {"message": str(exc)}, status_code=400
        )

    purged = await purge_rent_source(session, source_ref) if purge_first else 0
    inserted, skipped = await upsert_rent_listings(session, parsed.records)

    return templates.TemplateResponse(
        request,
        "_rent_import_result.html",
        {
            "inserted": inserted,
            "skipped": skipped,
            "purged": purged,
            "purge_first": purge_first,
            "errors": parsed.errors,
            "source_ref": source_ref,
            "total": await count_rent_listings(session),
        },
    )


@router.get("/rent/listings", response_class=HTMLResponse)
async def rent_listings_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=200, le=1000, ge=1),
    district_code: str = Query(default=""),
    property_kind: str = Query(default=""),
):
    """取込済みの賃料事例を一覧で見る（`transactions.html` と同型）。"""
    rows = await query_rent_listings(session, limit, district_code, property_kind)
    return templates.TemplateResponse(
        request,
        "rent_listings.html",
        {
            "rows": rows,
            "districts": await _district_options(session),
            "kinds": RENT_KINDS,
            "kind_labels": RENT_KIND_LABELS,
            "selected_district": district_code,
            "selected_kind": property_kind,
            "note_asking": RENT_NOTE_ASKING,
        },
    )

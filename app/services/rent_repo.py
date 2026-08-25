"""賃料事例の DB クエリ（`districts.py` の売買版と対になる薄い層）。

`build_case_query()` を一般化して両方を捌かせない: カラム対応表を挟むと、2本の素直な関数より
確実に読みにくくなる。**共有するのは地区リングの WHERE 条件（`_ring_filter`）だけ**で、
そこは Ring2 の OR ロジックを複製しないためにモデル引数化してある。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rent_listing import RentListing
from app.services.districts import DistrictOption, RingContext, _ring_filter
from app.services.rent import RentCase
from app.services.valuation import Criteria


@dataclass(frozen=True)
class RentTarget:
    """賃料を知りたい対象物件。"""

    property_kind: str
    district_code: str
    exclusive_area: float  # 区分=専有 / 戸建=延床
    building_year: int | None = None


def build_rent_case_query(ctx: RingContext, target: RentTarget, criteria: Criteria) -> Select:
    """条件に合う賃料事例を引くクエリ。"""
    stmt = select(RentListing).where(
        _ring_filter(ctx, criteria.ring, model=RentListing),
        RentListing.property_kind == target.property_kind,
        RentListing.rent_unit_price.is_not(None),
        RentListing.rent_unit_price > 0,
    )

    # 面積で絞る（区分=専有 / 戸建=延床）。㎡単価の分母と同じ軸なので必ず効かせる。
    # `criteria.area_tolerance`（＝土地面積の軸）は賃料では使わない
    if target.exclusive_area and target.exclusive_area > 0 and criteria.floor_tolerance is not None:
        low = target.exclusive_area * (1 - criteria.floor_tolerance)
        high = target.exclusive_area * (1 + criteria.floor_tolerance)
        stmt = stmt.where(RentListing.exclusive_area >= low, RentListing.exclusive_area <= high)

    # 築年（`survey_year` は調査時点であって築年ではない。ここで混同すると築年フィルタが壊れる）
    if target.building_year and criteria.year_tolerance is not None:
        low_year = target.building_year - criteria.year_tolerance
        high_year = target.building_year + criteria.year_tolerance
        stmt = stmt.where(
            RentListing.building_year.is_not(None),
            RentListing.building_year >= low_year,
            RentListing.building_year <= high_year,
        )

    return stmt.order_by(RentListing.survey_year.desc(), RentListing.survey_month.desc())


def to_rent_case(row: RentListing, ring: int) -> RentCase:
    return RentCase(
        rent_unit_price=float(row.rent_unit_price or 0),
        district_name=row.district_name,
        property_kind=row.property_kind,
        monthly_rent=row.monthly_rent,
        admin_fee=row.admin_fee,
        exclusive_area=row.exclusive_area,
        layout=row.layout,
        building_year=row.building_year,
        structure=row.structure,
        floor_no=row.floor_no,
        total_floors=row.total_floors,
        station_walk_min=row.station_walk_min,
        parking_included=row.parking_included,
        survey_year=row.survey_year,
        survey_month=row.survey_month,
        status=row.status,
        source_ref=row.source_ref,
        ring=ring,
    )


async def fetch_rent_cases(
    session: AsyncSession,
    ctx: RingContext,
    target: RentTarget,
    criteria: Criteria,
) -> list[RentCase]:
    result = await session.execute(build_rent_case_query(ctx, target, criteria))
    return [to_rent_case(row, criteria.ring) for row in result.scalars().all()]


async def list_rent_districts(session: AsyncSession) -> list[DistrictOption]:
    """賃料事例が存在する地区の一覧（件数つき）。"""
    stmt = (
        select(
            RentListing.district_code,
            RentListing.district_name,
            RentListing.municipality,
            func.count(RentListing.id),
        )
        .where(RentListing.district_code != "")
        .group_by(RentListing.district_code, RentListing.district_name, RentListing.municipality)
        .order_by(RentListing.municipality, RentListing.district_name)
    )
    result = await session.execute(stmt)
    return [
        DistrictOption(code=code, name=name, municipality=muni, count=count)
        for code, name, muni, count in result.all()
    ]


async def query_rent_listings(
    session: AsyncSession, limit: int, district_code: str, property_kind: str
) -> list[RentListing]:
    stmt = select(RentListing)
    if district_code:
        stmt = stmt.where(RentListing.district_code == district_code)
    if property_kind:
        stmt = stmt.where(RentListing.property_kind == property_kind)
    stmt = stmt.order_by(
        RentListing.survey_year.desc(), RentListing.survey_month.desc(), RentListing.id.desc()
    ).limit(limit)
    return list((await session.execute(stmt)).scalars().all())

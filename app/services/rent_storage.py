"""rent_listings テーブルへの保存（DB 依存をここに閉じ込める）。

`storage.py` の `upsert_transactions()` と**構造を完全に一致させる**。
片方だけ賢くすると「売買は冪等なのに賃料は二重に入る」という読みにくい壊れ方をする。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rent_listing import RentListing
from app.services.rent_import import RentRecord


async def upsert_rent_listings(
    session: AsyncSession, records: Sequence[RentRecord]
) -> tuple[int, int]:
    """内容ハッシュで冪等に投入する。戻り値は (新規件数, 既存スキップ件数)。

    同じ content_hash は同じ内容なので、衝突時は何もしない（＝再取込しても件数が増えない）。
    訂正して入れ直すときは `purge_rent_source()` で消してから取り込む（洗い替えモードは作らない）。
    """
    if not records:
        return 0, 0

    hashes = [r.content_hash for r in records]
    existing = set(
        (
            await session.execute(
                select(RentListing.content_hash).where(RentListing.content_hash.in_(hashes))
            )
        )
        .scalars()
        .all()
    )

    inserted = 0
    seen: set[str] = set()
    for record in records:
        if record.content_hash in existing or record.content_hash in seen:
            continue
        seen.add(record.content_hash)
        session.add(RentListing(**record.as_dict()))
        inserted += 1

    await session.commit()
    return inserted, len(records) - inserted


async def purge_rent_source(session: AsyncSession, source_ref: str) -> int:
    """取込ラベル単位で消す（訂正して入れ直す唯一の経路）。戻り値は削除件数。"""
    if not source_ref.strip():
        raise ValueError("取込ラベル（source_ref）を指定してください")
    result = await session.execute(
        delete(RentListing).where(RentListing.source_ref == source_ref.strip())
    )
    await session.commit()
    return int(result.rowcount or 0)


async def count_rent_listings(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count(RentListing.id)))).scalar_one())


async def list_rent_sources(session: AsyncSession) -> list[tuple[str, int]]:
    """取込ラベルごとの件数（消して入れ直すときに何があるか見えないと使えない）。"""
    stmt = (
        select(RentListing.source_ref, func.count(RentListing.id))
        .group_by(RentListing.source_ref)
        .order_by(RentListing.source_ref)
    )
    return [(ref, int(count)) for ref, count in (await session.execute(stmt)).all()]

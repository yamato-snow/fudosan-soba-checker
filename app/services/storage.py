"""transactions テーブルへの保存（DB 依存をここに閉じ込める）。"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.transaction import Transaction
from app.services.reinfolib import TransactionRecord


async def upsert_transactions(
    session: AsyncSession, records: Sequence[TransactionRecord]
) -> tuple[int, int]:
    """内容ハッシュで冪等に投入する。戻り値は (新規件数, 既存スキップ件数)。

    レコードは「その四半期の確定値」なので、同じ content_hash は同じ内容。
    よって衝突時は何もしない（＝再実行しても件数が増えない）。
    """
    if not records:
        return 0, 0

    hashes = [r.content_hash for r in records]
    existing = set(
        (
            await session.execute(
                select(Transaction.content_hash).where(Transaction.content_hash.in_(hashes))
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
        session.add(Transaction(**record.as_dict()))
        inserted += 1

    await session.commit()
    return inserted, len(records) - inserted


async def count_transactions(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count(Transaction.id)))).scalar_one())

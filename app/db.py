"""DB セッション（SQLAlchemy async）。

MVP は SQLite。将来 PostgreSQL に移す場合も `DATABASE_URL` の差し替えだけで済むよう、
クエリは services 層に閉じ込め、ここでは接続の面倒だけ見る。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, future=True)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def init_db() -> None:
    """テーブルを作る（MVP は Alembic を使わない。スキーマ変更は data/soba.db を消して再取込）。"""
    from app.models import transaction  # noqa: F401  テーブル定義の読み込み

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依存性。"""
    async with get_sessionmaker()() as session:
        yield session

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 本番の .env / data/soba.db を読ませない（テストは常にテンポラリ DB・ダミーキー）
os.environ["SOBA_ENV_FILE"] = str(PROJECT_ROOT / "tests" / "test.env")
os.environ.setdefault("REINFOLIB_API_KEY", "test-key")
os.environ.setdefault("TARGET_CITIES", "99001,99002")
os.environ.setdefault("NEIGHBOR_DISTRICTS", "990010040,990010041")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.config import get_settings  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _test_db(tmp_path_factory):
    """全テストでテンポラリ SQLite を使う。"""
    db_file = tmp_path_factory.mktemp("db") / "test.db"
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_file}"
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _fresh_session():
    """テーブルを作り直し（＝毎テスト空の状態にして）セッションを返す。"""
    from sqlalchemy import delete

    from app.db import get_sessionmaker, init_db
    from app.models.transaction import Transaction

    await init_db()
    session = get_sessionmaker()()
    await session.execute(delete(Transaction))
    await session.commit()
    return session


@pytest_asyncio.fixture
async def session():
    s = await _fresh_session()
    try:
        yield s
    finally:
        await s.close()


@pytest_asyncio.fixture
async def client():
    """ASGI 直結クライアント（lifespan は走らないので DB 初期化は明示的に行う）。"""
    from app.main import app

    s = await _fresh_session()
    await s.close()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c

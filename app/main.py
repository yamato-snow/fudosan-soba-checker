"""FastAPI エントリポイント。

    uvicorn app.main:app --reload
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes_rent import router as rent_router
from app.api.routes_soba import router
from app.db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="不動産相場チェッカー",
    description=(
        "国土交通省 不動産情報ライブラリ（reinfolib）の実取引データから、"
        "指定エリアの㎡単価相場に対する掲載価格の割安度を判定する自分専用ツール。"
    ),
    lifespan=lifespan,
)
app.include_router(router)
app.include_router(rent_router)

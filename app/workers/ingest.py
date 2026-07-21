"""reinfolib 取込バッチ（手動トリガの CLI）。

    python -m app.workers.ingest              # TARGET_CITIES × 直近3年の四半期
    python -m app.workers.ingest --years 1    # ウィンドウを狭める
    python -m app.workers.ingest --city 13101 # 対象市を絞る

差分取込はせず「毎回ウィンドウ全取得 → 内容ハッシュで upsert」。件数が高々数千件なので
これで十分単純かつ冪等（plan §設計確定事項 1）。reinfolib の更新周期（四半期）に合わせて再実行する。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys

from app.config import get_settings
from app.db import get_sessionmaker, init_db
from app.services.reinfolib import ReinfolibClient, ReinfolibError, quarters_window
from app.services.storage import count_transactions, upsert_transactions


def latest_published_quarter(today: dt.date) -> tuple[int, int]:
    """公開済みとみなせる最新四半期（reinfolib は数ヶ月遅れで公開されるため1つ前を採る）。"""
    quarter = (today.month - 1) // 3 + 1
    year = today.year
    for _ in range(2):  # 当四半期＋直前四半期は未公開のことが多い
        quarter -= 1
        if quarter == 0:
            quarter = 4
            year -= 1
    return year, quarter


async def run(years: int, cities: list[str], today: dt.date | None = None) -> int:
    settings = get_settings()
    await init_db()

    latest_year, latest_quarter = latest_published_quarter(today or dt.date.today())
    window = quarters_window(latest_year, latest_quarter, years)

    total_new = 0
    total_skipped = 0
    sessionmaker = get_sessionmaker()

    async with ReinfolibClient(settings) as client, sessionmaker() as session:
        for city in cities:
            for year, quarter in window:
                try:
                    records = await client.fetch_quarter(city, year, quarter)
                except ReinfolibError as exc:
                    print(f"  ! {city} {year}Q{quarter}: {exc}", file=sys.stderr)
                    continue
                new, skipped = await upsert_transactions(session, records)
                total_new += new
                total_skipped += skipped
                print(f"  {city} {year}Q{quarter}: 取得{len(records)}件 → 新規{new} / 既存{skipped}")

        total = await count_transactions(session)

    print(f"\n完了: 新規 {total_new}件 / 既存スキップ {total_skipped}件 / DB合計 {total}件")
    print(f"対象: {', '.join(cities)} × {window[0][0]}Q{window[0][1]}〜{window[-1][0]}Q{window[-1][1]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="reinfolib XIT001 取込バッチ")
    parser.add_argument("--years", type=int, default=settings.ingest_years, help="直近何年ぶんを取るか")
    parser.add_argument(
        "--city",
        action="append",
        dest="cities",
        help="市区町村コード5桁（複数指定可。既定は TARGET_CITIES）",
    )
    args = parser.parse_args(argv)

    cities = args.cities or settings.target_city_list
    if not cities:
        print("TARGET_CITIES が未設定です（.env に市区町村コード5桁を設定）", file=sys.stderr)
        return 1

    try:
        return asyncio.run(run(args.years, cities))
    except ReinfolibError as exc:
        print(f"取込に失敗しました: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

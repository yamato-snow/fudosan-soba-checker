#!/usr/bin/env python3
"""Phase A-1 検証: XIT001 の「中古マンション等」1四半期を取り、区分の実物を確認する。

実物確認3点（docs/plan/fudosan-soba-checker-toushi_plan.md A-1 step1）:
  (a) Area が専有面積か（戸建の土地面積とは意味が違う）
  (b) TotalFloorArea が空か（空なら延床フィルタを区分に適用してはいけない）
  (c) Structure の値域（RC/SRC 等。B-2 の耐用年数で使う語彙の下調べ）

ここが違うと `unit_price` の定義が全部ズレるので、コードを書く前に必ず流す。

使い方（API キー・対象市とも .env から読む。標準出力にキーは出さない）:
  .venv/bin/python scripts/phase_a1_probe.py [--city <市区町村コード5桁>] [--year 2024] [--quarter 4]

結果は out/phase_a1_condo_<city>_<year><quarter>.json に保存（out/ は .gitignore 済み）。
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402

TYPE_CONDO = "中古マンション等"


def main() -> int:
    settings = get_settings()
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default=settings.home_city, help="市区町村コード5桁")
    ap.add_argument("--year", type=int, default=2024)
    ap.add_argument("--quarter", type=int, default=4)
    args = ap.parse_args()

    if not settings.reinfolib_api_key:
        print("REINFOLIB_API_KEY が未設定です（.env に設定してください）", file=sys.stderr)
        return 1
    if not args.city:
        print("--city か TARGET_CITIES を指定してください", file=sys.stderr)
        return 1

    params = {"year": str(args.year), "quarter": str(args.quarter), "city": args.city}
    url = f"{settings.reinfolib_base_url.rstrip('/')}/XIT001?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url, headers={"Ocp-Apim-Subscription-Key": settings.reinfolib_api_key}
    )
    print(f"GET XIT001 {params}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read()
            # APIM 側が gzip で返すことがある（Content-Encoding を見て解く）
            if resp.headers.get("Content-Encoding") == "gzip" or body[:2] == b"\x1f\x8b":
                body = gzip.decompress(body)
            payload = json.loads(body)
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.reason}", file=sys.stderr)
        print(e.read().decode("utf-8", errors="replace")[:500], file=sys.stderr)
        return 1

    rows = payload.get("data") or []
    condos = [r for r in rows if (r.get("Type") or "").strip() == TYPE_CONDO]

    out_dir = Path(__file__).resolve().parent.parent / "out"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"phase_a1_condo_{args.city}_{args.year}{args.quarter}.json"
    out_path.write_text(json.dumps(condos, ensure_ascii=False, indent=2))
    print(f"保存: {out_path}")

    print(f"\n全 {len(rows)} 件中「{TYPE_CONDO}」 {len(condos)} 件")
    if not condos:
        print("0件でした。--year/--quarter を変えるか、別の市区町村コードで再実行してください")
        return 0

    print("\n(a) Area の分布（専有面積なら数十㎡台に集中。土地面積なら100㎡超が混じる）:")
    areas = sorted(float(r["Area"]) for r in condos if r.get("Area"))
    print(f"  n={len(areas)} min={areas[0]} median={areas[len(areas) // 2]} max={areas[-1]}")
    print(f"  値の実例: {areas[:10]}")

    print("\n(b) TotalFloorArea の充足率（空なら延床フィルタを区分に適用しない）:")
    filled = [r for r in condos if (r.get("TotalFloorArea") or "").strip()]
    print(f"  {len(filled)}/{len(condos)} 件に値あり")
    if filled:
        print(f"  値の実例: {[r.get('TotalFloorArea') for r in filled[:5]]}")

    print("\n(c) Structure の値域:")
    for value, count in Counter((r.get("Structure") or "(空)").strip() for r in condos).most_common():
        print(f"  {value}: {count}件")

    print("\n(参考) FloorPlan / BuildingYear の充足率:")
    for key in ("FloorPlan", "BuildingYear", "UnitPrice", "PricePerTsubo"):
        n = sum(1 for r in condos if (str(r.get(key) or "")).strip())
        print(f"  {key}: {n}/{len(condos)}")

    print(f"\n全 Type の内訳: {dict(Counter((r.get('Type') or '?').strip() for r in rows))}")
    print(f"\n1件目の全キー: {sorted(condos[0].keys())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Phase 0 検証: reinfolib XPT001 を1回叩いて「実物確認3点」を検証する。

実物確認3点（docs/plan/fudosan-soba-checker_plan.md）:
  (a) geometry の座標が何の代表点か（地区代表点と推定）
  (b) ㎡単価フィールドが戸建（宅地(土地建物)）にも入るか
  (c) レコードの一意IDの有無

使い方:
  REINFOLIB_API_KEY=<キー> HOME_LAT=35.xxxx HOME_LNG=139.xxxx \
      python3 scripts/phase0_probe.py [--from 20251] [--to 20254] [--zoom 13]

標準ライブラリのみで動作（venv不要）。結果は out/phase0_<z>_<x>_<y>.geojson に保存。
"""

import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_URL = "https://www.reinfolib.mlit.go.jp/ex-api/external/XPT001"


def latlng_to_tile(lat: float, lng: float, zoom: int) -> tuple[int, int]:
    """緯度経度 → XYZ タイル座標（Web メルカトル）"""
    n = 2**zoom
    x = int((lng + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="from_", default="20251", help="取引時期From (YYYYN)")
    ap.add_argument("--to", dest="to", default="20254", help="取引時期To (YYYYN)")
    ap.add_argument("--zoom", type=int, default=13, help="ズームレベル 11-15")
    args = ap.parse_args()

    api_key = os.environ.get("REINFOLIB_API_KEY")
    lat = os.environ.get("HOME_LAT")
    lng = os.environ.get("HOME_LNG")
    if not (api_key and lat and lng):
        print("環境変数 REINFOLIB_API_KEY / HOME_LAT / HOME_LNG を設定してください", file=sys.stderr)
        return 1

    x, y = latlng_to_tile(float(lat), float(lng), args.zoom)
    params = {
        "response_format": "geojson",
        "z": str(args.zoom),
        "x": str(x),
        "y": str(y),
        "from": args.from_,
        "to": args.to,
        # landTypeCode: 01=宅地(土地), 02=宅地(土地建物)。priceClassification 未指定=取引+成約の両方
        "landTypeCode": "01,02",
    }
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Ocp-Apim-Subscription-Key": api_key})

    print(f"GET {url}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        # 401/403=キー不正 or 未有効化、429=レート制限。本文にAPIM側の理由が入る
        print(f"HTTP {e.code}: {e.reason}", file=sys.stderr)
        print(e.read().decode("utf-8", errors="replace")[:500], file=sys.stderr)
        return 1
    data = json.loads(body)

    out_dir = Path(__file__).resolve().parent.parent / "out"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"phase0_{args.zoom}_{x}_{y}.geojson"
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"保存: {out_path}")

    features = data.get("features", [])
    print(f"\n件数: {len(features)}")
    if not features:
        print("0件でした。--from/--to の期間を広げるか、zoom を 11-12 に下げて再実行してください")
        return 0

    # --- 実物確認3点 ---
    sample = features[0]
    props = sample.get("properties", {})
    print("\n(a) geometry サンプル（座標の粒度を目視確認。同一地区で座標が同一なら地区代表点）:")
    coords = {}
    for f in features[:20]:
        d = f.get("properties", {}).get("district_name_ja", "?")
        coords.setdefault(d, set()).add(tuple(f.get("geometry", {}).get("coordinates", [])))
    for d, cs in list(coords.items())[:5]:
        print(f"  地区={d}: 座標バリエーション {len(cs)} 件 {list(cs)[:2]}")

    print("\n(b) 種別ごとの㎡単価フィールドの有無:")
    by_type = {}
    for f in features:
        p = f.get("properties", {})
        t = p.get("land_type_name_ja", "?")
        has_unit = bool(p.get("u_transaction_price_unit_price_square_meter_ja"))
        by_type.setdefault(t, [0, 0])
        by_type[t][0] += 1
        by_type[t][1] += 1 if has_unit else 0
    for t, (total, with_unit) in by_type.items():
        print(f"  {t}: {total}件中 ㎡単価あり {with_unit}件")

    print("\n(c) 一意IDらしきフィールド（id / *_id / code 系）:")
    id_like = [k for k in props if k == "id" or k.endswith("_id") or "code" in k]
    print(f"  {id_like or 'なし → 内容ハッシュ dedup で確定'}")

    print("\n全 properties キー:")
    print(f"  {sorted(props.keys())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""起動中のサーバに全エンドポイントを一巡させ、500 が出ないことを確認する。

    .venv/bin/python -m uvicorn app.main:app --port 8811 &
    .venv/bin/python scripts/smoke.py --base-url http://127.0.0.1:8811
"""

from __future__ import annotations

import argparse

import httpx

CHECKS = [
    ("GET", "/healthz", {}),
    ("GET", "/", {}),
    ("GET", "/transactions", {}),
    ("GET", "/api/transactions?limit=5", {}),
    ("GET", "/docs", {}),
    ("GET", "/openapi.json", {}),
    (
        "POST",
        "/valuate",
        {
            "data": {
                "land_type": "宅地(土地)",
                "district_code": "",
                "listing_price": "20000000",
                "area": "200",
            }
        },
    ),
    (
        "POST",
        "/api/valuate",
        {
            "json": {
                "land_type": "宅地(土地)",
                "district_code": "",
                "listing_price": 20000000,
                "area": 200,
            }
        },
    ),
    (
        # 区分（A-1）: 延床を渡さない。単価分岐が抜けていると 400/500 になる
        "POST",
        "/api/valuate",
        {
            "json": {
                "land_type": "中古マンション等",
                "district_code": "",
                "listing_price": 15000000,
                "area": 75,
                "building_year": 2000,
            }
        },
    ),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8811")
    ap.add_argument("--district", default="", help="実データの地区コードで叩き直す（未指定なら判定不能応答で 5xx だけ見る）")
    args = ap.parse_args()

    failures = 0
    with httpx.Client(base_url=args.base_url, timeout=30.0, follow_redirects=True) as client:
        for method, path, kwargs in CHECKS:
            if args.district and "district_code" in str(kwargs):
                for key in ("data", "json"):
                    if key in kwargs:
                        kwargs[key]["district_code"] = args.district
            try:
                res = client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                print(f"NG  {method:4} {path:28} 接続失敗: {exc}")
                failures += 1
                continue
            mark = "OK " if res.status_code < 500 else "NG "
            if res.status_code >= 500:
                failures += 1
            print(f"{mark} {method:4} {path:28} {res.status_code}")

    print("\n500 なし ✅" if not failures else f"\n{failures} 件が 5xx ❌")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

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
    # 賃料（A-2）
    ("GET", "/rent", {}),
    ("GET", "/rent/import", {}),
    ("GET", "/rent/listings", {}),
    (
        # 任意入力（築年・想定賃料）を空欄で送る＝実際にフォームから飛んでくる形
        "POST",
        "/rent/valuate",
        {
            "data": {
                "property_kind": "区分",
                "district_code": "",
                "exclusive_area": "62.5",
                "building_year": "",
                "target_monthly": "",
            }
        },
    ),
    (
        "POST",
        "/rent/import",
        {
            "data": {
                "source_ref": "smoke",
                "csv_text": "地区コード,種別,賃料,面積,調査年,調査月\n",
            }
        },
    ),
    # 収支シミュレーション（B-1）
    ("GET", "/cashflow", {}),
    ("GET", "/cashflow?land_type=中古マンション等&area=62.5&price=15000000", {}),
    (
        # 未入力欄を空文字で送る＝実際にフォームから飛んでくる形
        "POST",
        "/cashflow/simulate",
        {
            "data": {
                "price": "15000000",
                "acquisition_costs": "1000000",
                "monthly_rent": "90000",
                "vacancy_rate_percent": "5",
                "loan_principal": "12000000",
                "loan_rate_percent": "2.0",
                "loan_years": "25",
                "monthly_other_income": "",
                "annual_insurance": "",
                "capex_annual": "",
                "years": "",
            }
        },
    ),
    (
        # 現金購入（DSCR が None → 「—」表示。500 や ∞ にならないこと）
        "POST",
        "/cashflow/simulate",
        {"data": {"price": "15000000", "monthly_rent": "90000", "loan_principal": ""}},
    ),
    (
        # 税引後＋出口（B-2）。全角ＲＣの構造語彙も通す
        "POST",
        "/cashflow/simulate",
        {
            "data": {
                "price": "15000000",
                "acquisition_costs": "1000000",
                "monthly_rent": "90000",
                "vacancy_rate_percent": "5",
                "loan_principal": "12000000",
                "loan_rate_percent": "2.0",
                "loan_years": "25",
                "other_income": "5000000",
                "building_ratio": "0.4",
                "structure": "ＲＣ",
                "building_age": "20",
                "exit_year": "10",
                "exit_sale_price": "13000000",
                "useful_life_override": "",
                "resident_tax_rate_percent": "",
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

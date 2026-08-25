"""収支シミュレーションのフォーム入力（30項目近いので pydantic 1本で受ける）。

`Form(...)` を30個並べるとルーターが読めなくなるので、`dict(await request.form())` を
このモデルに通す（fastapi の下限は上げない）。`ValidationError` は既存の
`ValueError → _error.html + 400` と同じ場所で 400 に変換する＝規約を増やさない。

画面では率を**パーセント**で入力する（2.0 → 0.02）。変換はここだけで行う。
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from app.services.cashflow import (
    Acquisition,
    CapexPlan,
    CashflowInput,
    DepreciationInput,
    ExitPlan,
    Loan,
    OperatingCost,
    RentPlan,
    TaxProfile,
)


class CashflowForm(BaseModel):
    """フォームの生入力。空欄は「未入力」であって 0 ではない。"""

    # 取得
    price: int = Field(gt=0, description="物件価格（円）")
    acquisition_costs: int = Field(default=0, ge=0)
    # 建物割合は**既定値を持たない**。税引後モードでのみ必須（0 < r < 1）
    building_ratio: float | None = Field(default=None, gt=0, lt=1)

    # 借入（未入力なら現金購入）
    loan_principal: int | None = Field(default=None, ge=0)
    loan_rate_percent: float | None = Field(default=None, ge=0, le=20)
    loan_years: int | None = Field(default=None, ge=1, le=50)

    # 賃料
    monthly_rent: int = Field(gt=0, description="満室時の月額賃料（円）")
    monthly_other_income: int = Field(default=0, ge=0)
    vacancy_rate_percent: float = Field(default=0.0, ge=0, le=100)
    rent_decline_rate_percent: float = Field(default=0.0, ge=0, le=100)

    # 運営費
    monthly_admin_fee: int = Field(default=0, ge=0)
    monthly_repair_reserve: int = Field(default=0, ge=0)
    pm_fee_rate_percent: float = Field(default=0.0, ge=0, le=100)
    annual_property_tax: int = Field(default=0, ge=0)
    annual_insurance: int = Field(default=0, ge=0)
    annual_other: int = Field(default=0, ge=0)
    restoration_cost: int = Field(default=0, ge=0)
    ad_cost: int = Field(default=0, ge=0)
    turnover_years: float = Field(default=4.0, gt=0, le=50)

    capex_annual: int = Field(default=0, ge=0)
    years: int = Field(default=30, ge=1, le=50)

    # 税引後モード（未入力なら税引前モード）
    other_income: int | None = Field(default=None, ge=0)
    resident_tax_rate_percent: float = Field(default=10.0, ge=0, le=100)
    structure: str = ""
    building_age: int = Field(default=0, ge=0, le=200)
    useful_life_override: int | None = Field(default=None, ge=2, le=100)

    # 出口（未入力なら保有継続）
    exit_year: int | None = Field(default=None, ge=1, le=50)
    exit_sale_price: int | None = Field(default=None, gt=0)
    exit_sale_cost_rate_percent: float = Field(default=3.0, ge=0, le=100)

    # 表示用の付随情報（計算には使わない）
    area: float | None = Field(default=None, gt=0)
    district_code: str = ""
    land_type: str = ""

    @model_validator(mode="before")
    @classmethod
    def _drop_blanks(cls, values):
        """`<input type="number">` は未入力でも空文字を送る。空欄は「キーが無い」として扱う。"""
        if not isinstance(values, dict):
            return values
        return {k: v for k, v in values.items() if not (isinstance(v, str) and not v.strip())}

    def to_input(self, *, with_tax: bool = True, with_exit: bool = True) -> CashflowInput:
        """`CashflowInput` へ。`with_tax=False` / `with_exit=False` で段階的に無効化できる。"""
        loan = None
        if self.loan_principal and self.loan_principal > 0:
            loan = Loan(
                principal=self.loan_principal,
                annual_rate=(self.loan_rate_percent or 0.0) / 100,
                years=self.loan_years or self.years,
            )

        tax = None
        if with_tax and self.other_income is not None:
            tax = TaxProfile(
                other_income=self.other_income,
                resident_tax_rate=self.resident_tax_rate_percent / 100,
            )

        exit_plan = None
        if with_exit and self.exit_year and self.exit_sale_price:
            exit_plan = ExitPlan(
                year=self.exit_year,
                sale_price=self.exit_sale_price,
                sale_cost_rate=self.exit_sale_cost_rate_percent / 100,
            )

        return CashflowInput(
            acquisition=Acquisition(
                price=self.price,
                acquisition_costs=self.acquisition_costs,
                building_ratio=self.building_ratio,
            ),
            rent=RentPlan(
                monthly_rent=self.monthly_rent,
                monthly_other_income=self.monthly_other_income,
                vacancy_rate=self.vacancy_rate_percent / 100,
                rent_decline_rate=self.rent_decline_rate_percent / 100,
            ),
            operating=OperatingCost(
                monthly_admin_fee=self.monthly_admin_fee,
                monthly_repair_reserve=self.monthly_repair_reserve,
                pm_fee_rate=self.pm_fee_rate_percent / 100,
                annual_property_tax=self.annual_property_tax,
                annual_insurance=self.annual_insurance,
                annual_other=self.annual_other,
                restoration_cost=self.restoration_cost,
                ad_cost=self.ad_cost,
                turnover_years=self.turnover_years,
            ),
            capex=CapexPlan(annual_amount=self.capex_annual),
            depreciation=DepreciationInput(
                structure=self.structure,
                building_age=self.building_age,
                useful_life_override=self.useful_life_override,
            ),
            years=self.years,
            loan=loan,
            tax=tax,
            exit=exit_plan,
        )

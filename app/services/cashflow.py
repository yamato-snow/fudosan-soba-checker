"""収支シミュレーション（純粋関数。**DB・FastAPI・Jinja を一切 import しない**）。

投資判断の本題は「借りて・返して・回して、税引後に手元へ残るか」。ここはその計算だけを持つ。
入出力はすべて frozen dataclass で、`loan=None`（現金購入）/ `tax=None`（税引前モード）/
`exit=None`（保有継続）を**全部許す**。分岐がこの3つの None 判定だけで済むので、
税引前（B-1）と税引後（B-2）を同じ関数で扱える。

## 丸め誤差の規約

1. **公開する数値はすべて円単位 `int`**。内部は float で回し、載せる時点で `round()` する。
   `int()` 切り捨ては使わない（30年積み上げると系統的に下振れする）
2. 返済表は**月次 float で回して年次に集計**し、最終月で残高を元金に吸収して
   `loan_balance == 0` を保証する（実務の「端数は最終回」と一致）
3. 年次行では `payment = interest + principal` が**行として常に成立**するよう、
   利息と元金を丸めてから支払額を組み立てる。丸めで生じた元金合計のズレは最終年に吸収する
4. 恒等式はテストで assert する（`tests/test_cashflow.py`）

## Decimal を使わない理由

結果は**概算**であり、判断に効くのは百万円単位の差で、円未満の厳密さではない。
Decimal は依存と可読性のコストに見合わないため採用しない（design.md の
「意図的に作っていないもの」と同じ作法で、割り切りを明文化しておく）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

# --- 入力 ---------------------------------------------------------------


@dataclass(frozen=True)
class Acquisition:
    """取得。`building_ratio` は B-2（税引後）でのみ必須＝**既定値を持たせない**。

    総利益への感度が最も高い入力に既定値を置くと、ユーザーが触らないまま
    「もっともらしい数字」が出てしまう。入力画面では placeholder で目安を出すだけにする。
    """

    price: int  # 物件価格（円）
    acquisition_costs: int = 0  # 取得諸費用（仲介手数料・登記・不動産取得税 等）
    building_ratio: float | None = None  # 建物割合 0 < r < 1。税引後モードでは必須


@dataclass(frozen=True)
class Loan:
    principal: int
    annual_rate: float  # 0.02 = 年2.0%
    years: int


@dataclass(frozen=True)
class RentPlan:
    monthly_rent: int  # 満室時の月額賃料
    monthly_other_income: int = 0  # 駐車場・自販機など
    vacancy_rate: float = 0.0  # 空室率（0.1 = 10%）
    rent_decline_rate: float = 0.0  # 年あたりの賃料下落率


@dataclass(frozen=True)
class OperatingCost:
    """運営費。**期間中一定**として計算する（上昇率は入れない＝入力を増やしすぎない割り切り）。

    実際は10〜15年で上昇する場合があるので、この割り切りは結果画面の免責に必ず出す。
    """

    monthly_admin_fee: int = 0  # 管理費（区分）
    monthly_repair_reserve: int = 0  # 修繕積立金（区分）
    pm_fee_rate: float = 0.0  # PM 手数料。**実効収入に対する率**
    annual_property_tax: int = 0  # 固定資産税・都市計画税
    annual_insurance: int = 0
    annual_other: int = 0
    restoration_cost: int = 0  # 原状回復（入退去1回あたり）
    ad_cost: int = 0  # AD・広告料（1回あたり）
    turnover_years: float = 4.0  # 入退去の周期（年）


@dataclass(frozen=True)
class CapexPlan:
    """資本的支出。既定は**当年一括経費**（修繕費扱い）。追加償却は Phase C。"""

    annual_amount: int = 0
    treat_as_expense: bool = True


@dataclass(frozen=True)
class DepreciationInput:
    """減価償却の前提（B-2 で使う）。"""

    structure: str = ""  # 実データの表記のまま渡してよい（全角ＲＣ/ＳＲＣも解決する）
    building_age: int = 0  # 取得時点の築年数
    useful_life_override: int | None = None  # 手で耐用年数を指定したいとき


@dataclass(frozen=True)
class TaxProfile:
    """税引後モードの前提。`None` を渡すと税引前モードになる。"""

    other_income: int  # 他の課税所得（給与所得控除後・各種控除後の課税所得）
    resident_tax_rate: float = 0.10


@dataclass(frozen=True)
class ExitPlan:
    """出口。`None` を渡すと保有継続（売却しない）。"""

    year: int  # 保有年数（この年の末に売る）
    sale_price: int
    sale_cost_rate: float = 0.03  # 売却諸費用（仲介手数料等）の率


@dataclass(frozen=True)
class CashflowInput:
    acquisition: Acquisition
    rent: RentPlan
    operating: OperatingCost = OperatingCost()
    capex: CapexPlan = CapexPlan()
    depreciation: DepreciationInput = DepreciationInput()
    years: int = 30
    loan: Loan | None = None
    tax: TaxProfile | None = None
    exit: ExitPlan | None = None

    @property
    def equity(self) -> int:
        """自己資金＝(価格＋取得諸費用) − 借入。"""
        borrowed = self.loan.principal if self.loan else 0
        return self.acquisition.price + self.acquisition.acquisition_costs - borrowed


# --- 出力 ---------------------------------------------------------------


@dataclass(frozen=True)
class AmortizationRow:
    year: int
    payment: int
    interest: int
    principal: int
    loan_balance: int  # 年末残高


@dataclass(frozen=True)
class YearRow:
    year: int
    gross_income: int  # 満室年収
    effective_income: int  # 実効収入（空室控除後）
    operating_expenses: int
    noi: int
    loan_payment: int
    loan_interest: int
    loan_principal: int
    loan_balance: int
    capex: int
    btcf: int  # 税引前キャッシュフロー
    depreciation: int  # 減価償却費（税引前モードでは 0）
    real_estate_income: int  # 不動産所得（税引前モードでは 0）
    income_tax: int  # 所得税＋復興特別（税引前モードでは 0）
    resident_tax: int
    atcf: int  # 税引後キャッシュフロー（税引前モードでは btcf と同じ）
    dead_cross: bool  # 元金返済 > 減価償却


@dataclass(frozen=True)
class SingleYearMetrics:
    """初年度の指標（画面に式もそのまま出す）。"""

    gross_yield: float  # 表面利回り＝満室年収÷物件価格
    net_yield: float  # 実質利回り＝NOI÷(価格+取得諸費用)
    repayment_ratio: float | None  # 返済比率＝年間返済÷満室年収
    dscr: float | None  # DSCR＝NOI÷年間返済。借入なしは None（∞ではない）
    btcf: int
    atcf: int


@dataclass(frozen=True)
class ExitResult:
    year: int
    sale_price: int
    sale_costs: int
    loan_balance: int
    book_value: int  # 簿価＝取得費−累計償却
    capital_gain: int  # 譲渡所得
    transfer_tax: int
    is_long_term: bool
    net_sale_proceeds: int  # 売却手取り（税・諸費用・残債控除後）


@dataclass
class CashflowResult:
    years: list[YearRow]
    amortization: list[AmortizationRow]
    first_year: SingleYearMetrics
    equity: int
    total_atcf: int
    exit: ExitResult | None = None
    total_profit: int | None = None  # 累計ATCF＋売却手取り−自己資金
    dead_cross_year: int | None = None
    useful_life: int | None = None
    depreciation_years: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def has_loss_year(self) -> bool:
        """不動産所得がマイナスの年が1年以上あるか（土地利子の警告の出し分けに使う）。"""
        return any(r.real_estate_income < 0 for r in self.years)


# --- 借入 ---------------------------------------------------------------


def monthly_payment(principal: float, annual_rate: float, years: int) -> float:
    """元利均等返済の毎月返済額。`P*r/(1-(1+r)^-n)`。

    **金利0%の分岐を必ず持つ**（そのまま入れるとゼロ除算）。
    """
    if principal <= 0:
        return 0.0
    if years <= 0:
        raise ValueError("借入年数は1年以上で入力してください")
    n = years * 12
    if annual_rate == 0:
        return principal / n
    r = annual_rate / 12
    return principal * r / (1 - (1 + r) ** -n)


def amortization_schedule(loan: Loan) -> list[AmortizationRow]:
    """年次の返済表。月次で回して年次に集計する（年次で直接計算すると利息が合わない）。

    保証する恒等式:
      - `sum(principal) == loan.principal`
      - 各行で `payment == interest + principal`
      - `rows[-1].loan_balance == 0`
    """
    if loan.principal <= 0:
        return []
    if loan.annual_rate < 0:
        raise ValueError("金利に負の数は入れられません")

    payment = monthly_payment(loan.principal, loan.annual_rate, loan.years)
    monthly_rate = loan.annual_rate / 12
    balance = float(loan.principal)

    yearly: list[tuple[float, float]] = []  # (利息, 元金) の年次合計
    interest_acc = principal_acc = 0.0
    total_months = loan.years * 12

    for month in range(1, total_months + 1):
        interest_m = balance * monthly_rate
        principal_m = payment - interest_m
        if month == total_months or principal_m > balance:
            # 最終月（または端数で行き過ぎた月）は残高をそのまま元金に吸収する
            principal_m = balance
        balance -= principal_m
        interest_acc += interest_m
        principal_acc += principal_m
        if month % 12 == 0 or month == total_months:
            yearly.append((interest_acc, principal_acc))
            interest_acc = principal_acc = 0.0

    # 円に丸める。行の恒等式（payment == interest + principal）を先に守り、
    # 丸めで生じた元金合計のズレは最終年に吸収して sum(principal) == principal を守る
    interests = [round(i) for i, _p in yearly]
    principals = [round(p) for _i, p in yearly]
    drift = loan.principal - sum(principals)
    principals[-1] += drift

    rows: list[AmortizationRow] = []
    remaining = loan.principal
    for index, (interest, principal_amount) in enumerate(zip(interests, principals, strict=True)):
        remaining -= principal_amount
        rows.append(
            AmortizationRow(
                year=index + 1,
                payment=interest + principal_amount,
                interest=interest,
                principal=principal_amount,
                loan_balance=remaining,
            )
        )
    return rows


# --- 収入・運営費 -------------------------------------------------------


def gross_income(rent: RentPlan, year: int) -> float:
    """満室年収。賃料下落率は賃料本体にだけ効かせる（駐車場等は下落させない）。"""
    declined = rent.monthly_rent * (1 - rent.rent_decline_rate) ** (year - 1)
    return (declined + rent.monthly_other_income) * 12


def operating_expenses(cost: OperatingCost, effective_income: float) -> float:
    """年間運営費。PM 手数料は**実効収入**に対する率。

    原状回復・AD は `1/turnover_years` を毎年按分する（年ごとにガタつかせない＝
    「4年目だけ急に赤字」のような読みにくい表を作らない）。
    """
    turnover = cost.restoration_cost + cost.ad_cost
    per_year_turnover = turnover / cost.turnover_years if cost.turnover_years > 0 else 0.0
    return (
        (cost.monthly_admin_fee + cost.monthly_repair_reserve) * 12
        + effective_income * cost.pm_fee_rate
        + cost.annual_property_tax
        + cost.annual_insurance
        + cost.annual_other
        + per_year_turnover
    )


# --- 減価償却（B-2 で使う。B-1 では tax=None なので呼ばれない） ----------


def useful_life(structure: str | None, building_age: int, override: int | None = None) -> int:
    """中古の耐用年数（簡便法）。端数切捨て・**最低2年**。

      - 法定耐用年数を全部経過: 法定 × 0.2
      - 一部経過: (法定 − 経過) + 経過 × 0.2

    構造が解決できないときは木造として扱わず `ValueError`（勝手に有利/不利な既定を置かない）。
    """
    from app.services.tax_tables import statutory_useful_life

    if override is not None:
        if override < 2:
            raise ValueError("耐用年数は2年以上で入力してください")
        return override

    statutory = statutory_useful_life(structure)
    if statutory is None:
        raise ValueError(
            "構造から耐用年数を判定できません（木造 / 軽量鉄骨 / 重量鉄骨 / RC / SRC のいずれかを指定してください）"
        )
    age = max(0, building_age)
    if age >= statutory:
        life = statutory * 0.2
    else:
        life = (statutory - age) + age * 0.2
    return max(2, int(life))  # 簡便法は端数切捨て（ここだけ int() が正しい）


def depreciation_schedule(building_price: int, life: int, years: int) -> list[int]:
    """定額法。合計が **建物価格 − 1円**（備忘価額）になり、耐用年数超過年は 0。"""
    if building_price <= 0 or life <= 0:
        return [0] * years
    depreciable = building_price - 1
    annual = round(depreciable / life)
    schedule: list[int] = []
    remaining = depreciable
    for year in range(1, years + 1):
        if year > life or remaining <= 0:
            schedule.append(0)
            continue
        amount = min(annual, remaining) if year < life else remaining
        schedule.append(amount)
        remaining -= amount
    return schedule


# --- 所得税（B-2 で使う） -----------------------------------------------


def income_tax(taxable_income: float) -> int:
    """超過累進（速算控除方式）＋復興特別所得税（**所得税額に対して** 2.1%）。"""
    from app.services.tax_tables import INCOME_TAX_BRACKETS, SPECIAL_RECONSTRUCTION_RATE

    if taxable_income <= 0:
        return 0
    for upper, rate, deduction in INCOME_TAX_BRACKETS:
        if upper is None or taxable_income <= upper:
            base = taxable_income * rate - deduction
            break
    return round(max(0.0, base) * (1 + SPECIAL_RECONSTRUCTION_RATE))


def tax_delta(profile: TaxProfile, real_estate_income: float) -> tuple[int, int]:
    """不動産所得を足したことによる **税額の増分**（所得税, 住民税）。

    `tax(他所得 + 不動産所得) − tax(他所得)` の差分で出すので、赤字なら自然に負（＝還付方向）になる。
    損益通算を特別扱いの分岐で書かずに済む。
    """
    combined = profile.other_income + real_estate_income
    delta_income_tax = income_tax(combined) - income_tax(profile.other_income)
    delta_resident_tax = round(
        (max(0.0, combined) - max(0.0, float(profile.other_income))) * profile.resident_tax_rate
    )
    return delta_income_tax, delta_resident_tax


# --- 出口（B-2 で使う） -------------------------------------------------


def exit_result(
    plan: ExitPlan,
    *,
    acquisition_total: int,
    accumulated_depreciation: int,
    loan_balance: int,
) -> ExitResult:
    """売却。保有5年**超**＝長期20.315% / 5年**ちょうど**＝短期39.63%。売却損なら譲渡税0。"""
    from app.services.tax_tables import TRANSFER_TAX_LONG, TRANSFER_TAX_SHORT

    sale_costs = round(plan.sale_price * plan.sale_cost_rate)
    book_value = acquisition_total - accumulated_depreciation
    capital_gain = plan.sale_price - sale_costs - book_value
    is_long_term = plan.year > 5
    rate = TRANSFER_TAX_LONG if is_long_term else TRANSFER_TAX_SHORT
    transfer_tax = round(capital_gain * rate) if capital_gain > 0 else 0
    return ExitResult(
        year=plan.year,
        sale_price=plan.sale_price,
        sale_costs=sale_costs,
        loan_balance=loan_balance,
        book_value=book_value,
        capital_gain=capital_gain,
        transfer_tax=transfer_tax,
        is_long_term=is_long_term,
        net_sale_proceeds=plan.sale_price - sale_costs - transfer_tax - loan_balance,
    )


# --- 指標 ---------------------------------------------------------------


def single_year_metrics(row: YearRow, data: CashflowInput) -> SingleYearMetrics:
    price = data.acquisition.price
    total_cost = price + data.acquisition.acquisition_costs
    has_payment = row.loan_payment > 0
    return SingleYearMetrics(
        gross_yield=row.gross_income / price if price > 0 else 0.0,
        net_yield=row.noi / total_cost if total_cost > 0 else 0.0,
        repayment_ratio=(
            row.loan_payment / row.gross_income if has_payment and row.gross_income > 0 else None
        ),
        # 借入なしは None（∞ でも 0 でもない。画面では「—」と出す）
        dscr=(row.noi / row.loan_payment) if has_payment else None,
        btcf=row.btcf,
        atcf=row.atcf,
    )


# --- 束ねる -------------------------------------------------------------


def simulate(data: CashflowInput) -> CashflowResult:
    """30年の年次表を作る。分岐は `loan` / `tax` / `exit` が None かの3つだけ。"""
    if data.acquisition.price <= 0:
        raise ValueError("物件価格は正の数で入力してください")
    if data.rent.monthly_rent <= 0:
        raise ValueError("月額賃料は正の数で入力してください")
    if data.years <= 0:
        raise ValueError("シミュレーション期間は1年以上で入力してください")
    if not 0 <= data.rent.vacancy_rate <= 1:
        raise ValueError("空室率は 0〜100% で入力してください")

    warnings: list[str] = []
    amortization = amortization_schedule(data.loan) if data.loan else []
    by_year = {row.year: row for row in amortization}

    # 減価償却（税引後モードのみ）
    life: int | None = None
    depreciations = [0] * data.years
    if data.tax is not None:
        ratio = data.acquisition.building_ratio
        if ratio is None or not 0 < ratio < 1:
            raise ValueError("建物割合は 0 より大きく 1 未満で入力してください（既定値は持ちません）")
        life = useful_life(
            data.depreciation.structure,
            data.depreciation.building_age,
            data.depreciation.useful_life_override,
        )
        building_price = round(data.acquisition.price * ratio)
        depreciations = depreciation_schedule(building_price, life, data.years)

    rows: list[YearRow] = []
    accumulated_depreciation = 0
    for year in range(1, data.years + 1):
        gross = gross_income(data.rent, year)
        effective = gross * (1 - data.rent.vacancy_rate)
        opex = operating_expenses(data.operating, effective)
        noi = effective - opex

        loan_row = by_year.get(year)
        payment = loan_row.payment if loan_row else 0
        interest = loan_row.interest if loan_row else 0
        principal = loan_row.principal if loan_row else 0
        balance = loan_row.loan_balance if loan_row else 0  # 完済後・借入なしはどちらも0

        capex = data.capex.annual_amount
        btcf = noi - payment - capex

        depreciation = depreciations[year - 1]
        accumulated_depreciation += depreciation
        expensed_capex = capex if data.capex.treat_as_expense else 0
        real_estate_income = effective - (opex + interest + depreciation + expensed_capex)

        if data.tax is not None:
            delta_income_tax, delta_resident_tax = tax_delta(data.tax, real_estate_income)
        else:
            real_estate_income = 0.0
            delta_income_tax = delta_resident_tax = 0

        atcf = btcf - delta_income_tax - delta_resident_tax
        rows.append(
            YearRow(
                year=year,
                gross_income=round(gross),
                effective_income=round(effective),
                operating_expenses=round(opex),
                noi=round(noi),
                loan_payment=payment,
                loan_interest=interest,
                loan_principal=principal,
                loan_balance=balance,
                capex=capex,
                btcf=round(btcf),
                depreciation=depreciation,
                real_estate_income=round(real_estate_income),
                income_tax=delta_income_tax,
                resident_tax=delta_resident_tax,
                atcf=round(atcf),
                dead_cross=data.tax is not None and principal > depreciation,
            )
        )

    dead_cross_year = next((r.year for r in rows if r.dead_cross), None)

    # 出口
    exit_res: ExitResult | None = None
    total_profit: int | None = None
    if data.exit is not None:
        if not 1 <= data.exit.year <= data.years:
            raise ValueError(f"売却年は 1〜{data.years} 年の範囲で入力してください")
        held = rows[: data.exit.year]
        balance_at_exit = held[-1].loan_balance if held else 0
        exit_res = exit_result(
            data.exit,
            acquisition_total=data.acquisition.price + data.acquisition.acquisition_costs,
            accumulated_depreciation=sum(r.depreciation for r in held),
            loan_balance=balance_at_exit,
        )
        total_profit = sum(r.atcf for r in held) + exit_res.net_sale_proceeds - data.equity

    total_atcf = sum(r.atcf for r in rows)

    # 警告（読まれない免責にしないため、条件が立ったときだけ出す）
    if rows[0].noi < 0:
        warnings.append(
            "初年度の NOI がマイナスです（空室率・運営費の前提を確認してください）。"
            "この条件では保有するだけで現金が出ていきます。"
        )
    first = single_year_metrics(rows[0], data)
    if first.dscr is not None and first.dscr < 1.0:
        warnings.append(
            f"初年度の DSCR が {first.dscr:.2f} で 1.0 を下回っています"
            "（NOI で返済を賄えず、他の収入からの持ち出しになります）。"
        )
    if data.loan and data.loan.years > data.years:
        warnings.append(
            f"借入年数（{data.loan.years}年）がシミュレーション期間（{data.years}年）より長いため、"
            "期間終了時点で残債が残ります。"
        )

    return CashflowResult(
        years=rows,
        amortization=amortization,
        first_year=first,
        equity=data.equity,
        total_atcf=total_atcf,
        exit=exit_res,
        total_profit=total_profit,
        dead_cross_year=dead_cross_year,
        useful_life=life,
        depreciation_years=sum(1 for d in depreciations if d > 0),
        warnings=warnings,
    )


def sensitivity_by_building_ratio(
    data: CashflowInput, delta: float = 0.10
) -> list[tuple[float, int | None]]:
    """建物割合 ±delta（0〜1 にクランプ）の3点の総利益。

    「この数字は前提でこれだけ動く」を一目で見せるのが感度分析の本体価値。
    `simulate()` は純粋関数なので追加コストは呼び出し3回だけ。
    """
    ratio = data.acquisition.building_ratio
    if ratio is None:
        return []
    candidates = sorted({_clamp(ratio - delta), ratio, _clamp(ratio + delta)})
    results: list[tuple[float, int | None]] = []
    for candidate in candidates:
        variant = replace(
            data, acquisition=replace(data.acquisition, building_ratio=candidate)
        )
        results.append((candidate, simulate(variant).total_profit))
    return results


def _clamp(value: float, low: float = 0.01, high: float = 0.99) -> float:
    return round(min(high, max(low, value)), 4)

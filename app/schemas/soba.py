"""API の入出力スキーマ。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.constants import TARGET_TYPES


class ValuationRequest(BaseModel):
    """物件条件の入力。"""

    land_type: str = Field(description=f"種別（{ ' / '.join(TARGET_TYPES) }）")
    district_code: str = Field(description="地区コード9桁（プルダウンで選択）")
    listing_price: float = Field(gt=0, description="掲載価格（円）")
    area: float = Field(gt=0, description="土地面積（㎡）。中古マンションは専有面積（㎡）")
    total_floor_area: float | None = Field(
        default=None, description="延床面積（㎡・中古戸建は必須。中古マンションは不要）"
    )
    building_year: int | None = Field(
        default=None, description="築年（西暦・中古戸建／中古マンション）"
    )


class CaseOut(BaseModel):
    district_name: str
    land_type: str
    trade_price: int
    area: float
    total_floor_area: float | None
    building_year: int | None
    unit_price: float
    period: str
    price_category: str


class ValuationResponse(BaseModel):
    judgeable: bool
    target_unit_price: float
    median_unit_price: float | None
    ratio_percent: float | None
    band: str | None
    case_count: int  # トリム後の「実際に使った」件数
    ring_label: str
    confidence: str
    confidence_note: str
    relaxations: list[str]
    message: str = ""
    # 従指標: 土地面積あたりの単価の中央値（参考表示のみ・判定には使わない）
    median_land_unit_price: float | None = None
    trimmed_count: int = 0  # IQR トリムで除外した外れ値の件数
    dispersion_flag: bool = False  # ばらつき大（Q3/Q1 > 1.8）で信頼度を1段下げた
    ring1_source: str = ""  # Ring1 の由来（手動設定 / 自動推定 / なし）
    cases: list[CaseOut] = []


class TransactionOut(BaseModel):
    id: int
    price_category: str
    land_type: str
    municipality: str
    district_name: str
    district_code: str
    trade_price: int
    area: float
    total_floor_area: float | None
    building_year: int | None
    unit_price: float | None
    period: str

"""取引・成約データ（reinfolib XIT001 の正規化結果）。"""

from __future__ import annotations

from sqlalchemy import Float, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Transaction(Base):
    """1レコード = reinfolib の1取引（または成約）。

    一意IDが API 側に無い（Phase 0 で確認済）ため、内容ハッシュ `content_hash` を
    UNIQUE キーにして冪等 upsert する。
    """

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # dedup キー（(Period, DistrictCode, Type, PriceCategory, TradePrice, Area, BuildingYear) のハッシュ）
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    price_category: Mapped[str] = mapped_column(String(32), default="")  # 不動産取引価格情報 / 成約価格情報
    land_type: Mapped[str] = mapped_column(String(32))  # 宅地(土地) / 宅地(土地と建物)
    region: Mapped[str] = mapped_column(String(32), default="")

    municipality_code: Mapped[str] = mapped_column(String(5))
    municipality: Mapped[str] = mapped_column(String(64), default="")
    prefecture: Mapped[str] = mapped_column(String(32), default="")
    district_code: Mapped[str] = mapped_column(String(9), default="")
    district_name: Mapped[str] = mapped_column(String(64), default="")

    trade_price: Mapped[int] = mapped_column(Integer)  # 円（生値）
    area: Mapped[float] = mapped_column(Float)  # 土地面積 ㎡
    total_floor_area: Mapped[float | None] = mapped_column(Float, nullable=True)  # 延床 ㎡
    building_year: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 西暦

    # 主指標の㎡単価（土地=総額÷土地面積 / 戸建=総額÷延床）。自前計算（API値は使わない）
    unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 参考: 土地面積あたりの単価（戸建でも保持）
    land_unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    period_year: Mapped[int] = mapped_column(Integer)
    period_quarter: Mapped[int] = mapped_column(Integer)

    structure: Mapped[str] = mapped_column(String(64), default="")
    use: Mapped[str] = mapped_column(String(64), default="")
    city_planning: Mapped[str] = mapped_column(String(64), default="")

    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_transactions_content_hash"),
        Index("ix_transactions_district_code", "district_code"),
        Index("ix_transactions_municipality_code", "municipality_code"),
        Index("ix_transactions_land_type", "land_type"),
    )

    def __repr__(self) -> str:  # pragma: no cover - デバッグ用
        return (
            f"<Transaction {self.period_year}Q{self.period_quarter} "
            f"{self.district_name} {self.land_type} {self.trade_price}>"
        )

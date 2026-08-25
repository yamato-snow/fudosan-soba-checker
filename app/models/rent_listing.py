"""賃料事例（手入力／CSV 取込）。

国交省 API に賃貸データが無く、賃貸サイトのスクレイピングは各サイト規約で禁止されているため、
賃料の母集団は**自分で入れる**しかない。`Transaction` と同じ列名・同じ作法（内容ハッシュで冪等）に
揃えてあるので、地区リング（`_ring_filter`）と統計エンジン（`evaluate()`）をそのまま流用できる。

注意: 売買の `transactions` とは**別テーブル**。混ぜると「集計値と事例」ではなく
「売買価格と賃料」という単位の違うものが同じ中央値に載る。
"""

from __future__ import annotations

from sqlalchemy import Boolean, Float, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.constants import RENT_STATUS_OPEN
from app.db import Base


class RentListing(Base):
    """1レコード = 賃料事例1件（募集または成約）。"""

    __tablename__ = "rent_listings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # dedup キー（種別・地区・賃料・管理費・面積・間取り・築年・調査年月・source_ref のハッシュ）
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="")  # csv / manual
    source_ref: Mapped[str] = mapped_column(String(64), default="")  # 取込ラベル（消して入れ直す単位）

    # 地区（既存 Transaction と同じ体系＝リング機構をそのまま使える）
    municipality_code: Mapped[str] = mapped_column(String(5), default="")
    municipality: Mapped[str] = mapped_column(String(64), default="")
    district_code: Mapped[str] = mapped_column(String(9), default="")
    district_name: Mapped[str] = mapped_column(String(64), default="")

    property_kind: Mapped[str] = mapped_column(String(16))  # 戸建 / 区分

    monthly_rent: Mapped[int] = mapped_column(Integer)  # 円/月
    admin_fee: Mapped[int] = mapped_column(Integer, default=0)  # 管理費・共益費 円/月

    exclusive_area: Mapped[float] = mapped_column(Float)  # 区分=専有 / 戸建=延床 ㎡
    land_area: Mapped[float | None] = mapped_column(Float, nullable=True)  # 戸建の敷地（参考）
    layout: Mapped[str] = mapped_column(String(16), default="")  # 3LDK 等
    building_year: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 西暦
    structure: Mapped[str] = mapped_column(String(32), default="")
    floor_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_floors: Mapped[int | None] = mapped_column(Integer, nullable=True)
    station_walk_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parking_included: Mapped[bool] = mapped_column(Boolean, default=False)

    survey_year: Mapped[int] = mapped_column(Integer)  # 調査時点（築年ではない）
    survey_month: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default=RENT_STATUS_OPEN)

    # 主指標: (monthly_rent + admin_fee) ÷ exclusive_area 円/㎡/月。自前計算
    rent_unit_price: Mapped[float | None] = mapped_column(Float, nullable=True)

    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_rent_listings_content_hash"),
        Index("ix_rent_listings_district_code", "district_code"),
        Index("ix_rent_listings_municipality_code", "municipality_code"),
        Index("ix_rent_listings_property_kind", "property_kind"),
        Index("ix_rent_listings_source_ref", "source_ref"),
    )

    def __repr__(self) -> str:  # pragma: no cover - デバッグ用
        return (
            f"<RentListing {self.survey_year}/{self.survey_month} "
            f"{self.district_name} {self.property_kind} {self.monthly_rent}>"
        )

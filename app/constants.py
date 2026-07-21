"""ドメイン定数（種別・信頼度・バンド）。"""

from __future__ import annotations

# XIT001 の Type 値。本ツールの対象2種別（中古マンション等・農地等は対象外）
TYPE_LAND = "宅地(土地)"
TYPE_HOUSE = "宅地(土地と建物)"
TARGET_TYPES = (TYPE_LAND, TYPE_HOUSE)

TYPE_LABELS = {
    TYPE_LAND: "土地",
    TYPE_HOUSE: "中古戸建（土地と建物）",
}

# 判定に必要な最小事例数（これ未満なら抽出条件を段階拡張する）
MIN_CASES = 5
# これ未満は判定不能
MIN_CASES_JUDGEABLE = 3

RING_LABELS = {
    0: "同一地区",
    1: "近隣地区",
    2: "基準市全域",
    3: "対象市全域",
}

CONFIDENCE_A = "A"
CONFIDENCE_B = "B"
CONFIDENCE_C = "C"
CONFIDENCE_NONE = "判定不能"

CONFIDENCE_NOTES = {
    CONFIDENCE_A: "同一・近隣地区の初期条件で十分な事例数",
    CONFIDENCE_B: "抽出範囲を広げて事例数を確保",
    CONFIDENCE_C: "条件を緩めても事例が少ない（参考値）",
    CONFIDENCE_NONE: "事例が足りず判定できません",
}

BAND_CHEAP = "割安"
BAND_FAIR = "適正"
BAND_EXPENSIVE = "割高"

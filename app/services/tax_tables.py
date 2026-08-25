"""税・耐用年数のテーブル（**2026年時点**）。

税制は改正される。だから「いつ時点の税率で計算したか」が結果に写っていないと、
後から自分の判断を読み返したときに何を前提にしていたのか分からなくなる。
`TAX_TABLE_VERSION` を結果画面の免責に必ず併記すること。

外部設定ファイルにはしない（YAGNI）。自分専用のローカルツールで、改正時はここを書き換えれば足りる。
差し替えたい場合は `TaxProfile` 側で率を渡す。
"""

from __future__ import annotations

import unicodedata

TAX_TABLE_VERSION = "2026-01"

# --- 法定耐用年数（住宅用・国税庁の耐用年数表） ---
STRUCTURE_WOOD = "木造"
STRUCTURE_LIGHT_STEEL_3MM = "軽量鉄骨(骨格材3mm以下)"
STRUCTURE_LIGHT_STEEL_4MM = "軽量鉄骨(骨格材3mm超4mm以下)"
STRUCTURE_STEEL = "重量鉄骨"
STRUCTURE_RC = "RC"
STRUCTURE_SRC = "SRC"

USEFUL_LIFE_YEARS: dict[str, int] = {
    STRUCTURE_WOOD: 22,
    STRUCTURE_LIGHT_STEEL_3MM: 19,
    STRUCTURE_LIGHT_STEEL_4MM: 27,
    STRUCTURE_STEEL: 34,
    STRUCTURE_RC: 47,
    STRUCTURE_SRC: 47,
}

STRUCTURE_LABELS: dict[str, str] = {
    STRUCTURE_WOOD: "木造（22年）",
    STRUCTURE_LIGHT_STEEL_3MM: "軽量鉄骨・骨格材3mm以下（19年）",
    STRUCTURE_LIGHT_STEEL_4MM: "軽量鉄骨・骨格材3mm超4mm以下（27年）",
    STRUCTURE_STEEL: "重量鉄骨（34年）",
    STRUCTURE_RC: "RC（47年）",
    STRUCTURE_SRC: "SRC（47年）",
}

# 実データ（XIT001 の `Structure`）の語彙 → 上の区分。
# **全角の「ＲＣ」「ＳＲＣ」で入ってくる**ことを A-1 の裏取りで確認済み。
# NFKC で半角化してから引くので、全角・半角のどちらでも解決できる。
_STRUCTURE_ALIASES: dict[str, str] = {
    "木造": STRUCTURE_WOOD,
    "木": STRUCTURE_WOOD,
    "W": STRUCTURE_WOOD,
    "軽量鉄骨造": STRUCTURE_LIGHT_STEEL_4MM,
    "軽量鉄骨": STRUCTURE_LIGHT_STEEL_4MM,
    "軽量鉄骨(骨格材3mm以下)": STRUCTURE_LIGHT_STEEL_3MM,
    "軽量鉄骨(骨格材3mm超4mm以下)": STRUCTURE_LIGHT_STEEL_4MM,
    "鉄骨造": STRUCTURE_STEEL,
    "重量鉄骨": STRUCTURE_STEEL,
    "鉄骨": STRUCTURE_STEEL,
    "S": STRUCTURE_STEEL,
    "RC": STRUCTURE_RC,
    "鉄筋コンクリート造": STRUCTURE_RC,
    "鉄筋コンクリート": STRUCTURE_RC,
    "SRC": STRUCTURE_SRC,
    "鉄骨鉄筋コンクリート造": STRUCTURE_SRC,
    "鉄骨鉄筋コンクリート": STRUCTURE_SRC,
}


def normalize_structure(raw: str | None) -> str | None:
    """実データの構造表記 → 耐用年数表のキー。解決できなければ None。

    全角「ＲＣ」「ＳＲＣ」は NFKC で "RC" / "SRC" になる（A-1 で観測した実データの語彙）。
    """
    if not raw:
        return None
    text = unicodedata.normalize("NFKC", str(raw)).strip().replace(" ", "")
    if text in USEFUL_LIFE_YEARS:
        return text
    if text in _STRUCTURE_ALIASES:
        return _STRUCTURE_ALIASES[text]
    # "ＲＣ造" のような接尾辞つきも拾う（長いキーから順に見る）
    for alias in sorted(_STRUCTURE_ALIASES, key=len, reverse=True):
        if alias and alias in text:
            return _STRUCTURE_ALIASES[alias]
    return None


def statutory_useful_life(structure: str | None) -> int | None:
    """構造 → 法定耐用年数（住宅用）。解決できなければ None。"""
    key = normalize_structure(structure)
    return USEFUL_LIFE_YEARS.get(key) if key else None


# --- 所得税（超過累進・速算控除方式） ---
# (課税所得の上限, 税率, 控除額)。上限 None が最上段。
INCOME_TAX_BRACKETS: tuple[tuple[int | None, float, int], ...] = (
    (1_950_000, 0.05, 0),
    (3_300_000, 0.10, 97_500),
    (6_950_000, 0.20, 427_500),
    (9_000_000, 0.23, 636_000),
    (18_000_000, 0.33, 1_536_000),
    (40_000_000, 0.40, 2_796_000),
    (None, 0.45, 4_796_000),
)

# 復興特別所得税は**所得税額に対して** 2.1%（課税所得に対してではない）
SPECIAL_RECONSTRUCTION_RATE = 0.021

# 住民税（所得割）の標準税率
RESIDENT_TAX_RATE = 0.10

# 譲渡所得。保有5年**超**＝長期 / 5年**ちょうど**は短期
TRANSFER_TAX_LONG = 0.20315
TRANSFER_TAX_SHORT = 0.3963

__all__ = [
    "INCOME_TAX_BRACKETS",
    "RESIDENT_TAX_RATE",
    "SPECIAL_RECONSTRUCTION_RATE",
    "STRUCTURE_LABELS",
    "STRUCTURE_LIGHT_STEEL_3MM",
    "STRUCTURE_LIGHT_STEEL_4MM",
    "STRUCTURE_RC",
    "STRUCTURE_SRC",
    "STRUCTURE_STEEL",
    "STRUCTURE_WOOD",
    "TAX_TABLE_VERSION",
    "TRANSFER_TAX_LONG",
    "TRANSFER_TAX_SHORT",
    "USEFUL_LIFE_YEARS",
    "normalize_structure",
    "statutory_useful_life",
]

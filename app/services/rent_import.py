"""賃料事例 CSV のパース・正規化（純粋＝DB も FastAPI も触らない）。

現実の入力は「賃貸サイトを見ながら手で写した表」なので、次の汚れ方を前提にする:
  - Excel から出すと `cp932`、エディタから出すと `utf-8`（BOM 付きもある）
  - 全角数字（８．５万 ではなく ８５０００ の形で入る）・カンマ入り金額（85,000）
  - 単位が付く（85,000円 / 62.5㎡ / 徒歩8分）
  - 空行・`#` で始まるメモ行が混ざる

**部分成功**が原則。1行の書き損じで30行の入力をやり直させない（やり直させると母集団が育たない）。
不正行は行番号つきで返し、正常行はそのまま取り込む。
"""

from __future__ import annotations

import csv
import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass, field

from app.constants import (
    RENT_KINDS,
    RENT_SOURCE_CSV,
    RENT_STATUS_OPEN,
    RENT_STATUSES,
)

# ヘッダ名 → フィールド名。日本語（手で書く用）と英語（フィールド名そのまま）の両方を受ける
HEADER_ALIASES: dict[str, str] = {
    "市区町村コード": "municipality_code",
    "市区町村": "municipality",
    "地区コード": "district_code",
    "地区名": "district_name",
    "地区": "district_name",
    "種別": "property_kind",
    "賃料": "monthly_rent",
    "家賃": "monthly_rent",
    "管理費": "admin_fee",
    "共益費": "admin_fee",
    "面積": "exclusive_area",
    "専有面積": "exclusive_area",
    "延床面積": "exclusive_area",
    "土地面積": "land_area",
    "間取り": "layout",
    "築年": "building_year",
    "構造": "structure",
    "階": "floor_no",
    "所在階": "floor_no",
    "総階数": "total_floors",
    "駅徒歩": "station_walk_min",
    "徒歩": "station_walk_min",
    "駐車場込み": "parking_included",
    "駐車場": "parking_included",
    "調査年": "survey_year",
    "調査月": "survey_month",
    "状態": "status",
}

FIELD_NAMES = {
    "municipality_code",
    "municipality",
    "district_code",
    "district_name",
    "property_kind",
    "monthly_rent",
    "admin_fee",
    "exclusive_area",
    "land_area",
    "layout",
    "building_year",
    "structure",
    "floor_no",
    "total_floors",
    "station_walk_min",
    "parking_included",
    "survey_year",
    "survey_month",
    "status",
}

REQUIRED_FIELDS = (
    "district_code",
    "property_kind",
    "monthly_rent",
    "exclusive_area",
    "survey_year",
    "survey_month",
)

# エラー文面で「英語のフィールド名」ではなく書いた本人の言葉で返すための対応表
REQUIRED_LABELS = {
    "district_code": "地区コード",
    "property_kind": "種別",
    "monthly_rent": "賃料",
    "exclusive_area": "面積",
    "survey_year": "調査年",
    "survey_month": "調査月",
}

# 取込テンプレート（画面に出してそのままコピーできる形）
CSV_TEMPLATE_HEADER = (
    "地区コード,地区名,種別,賃料,管理費,面積,間取り,築年,構造,階,総階数,駅徒歩,駐車場込み,調査年,調査月,状態"
)

_TRUE_WORDS = {"1", "true", "yes", "y", "有", "あり", "込み", "込", "はい", "○", "o"}
_FALSE_WORDS = {"", "0", "false", "no", "n", "無", "なし", "別", "いいえ", "×", "x", "-"}

_DISTRICT_CODE_RE = re.compile(r"^\d{9}$")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass(frozen=True)
class RentRecord:
    """正規化済みの賃料事例1件。DB モデルとフィールド名を揃えてある。"""

    content_hash: str
    source: str
    source_ref: str
    municipality_code: str
    municipality: str
    district_code: str
    district_name: str
    property_kind: str
    monthly_rent: int
    admin_fee: int
    exclusive_area: float
    land_area: float | None
    layout: str
    building_year: int | None
    structure: str
    floor_no: int | None
    total_floors: int | None
    station_walk_min: int | None
    parking_included: bool
    survey_year: int
    survey_month: int
    status: str
    rent_unit_price: float | None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RowError:
    """取り込めなかった行。行番号は**元テキストの行番号**（コメント・空行を含めた数え）。"""

    line_no: int
    message: str
    raw: str = ""


@dataclass
class ImportResult:
    records: list[RentRecord] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return len(self.records)

    @property
    def error_count(self) -> int:
        return len(self.errors)


class RentImportError(ValueError):
    """ヘッダ不備など、行単位では受け止めきれない失敗。"""


def decode_csv(data: bytes) -> str:
    """`utf-8`（BOM 付き含む）→ `cp932` の順に試す。Excel 由来を落とさないため。"""
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    # 最後の砦: 化けても取り込みは進める（値の妥当性チェックは各行で効く）
    return data.decode("utf-8", errors="replace")


def normalize_text(raw: str | None) -> str:
    """全角→半角（NFKC）・前後空白除去。数値も文字列もまずこれを通す。"""
    if raw is None:
        return ""
    return unicodedata.normalize("NFKC", str(raw)).strip()


def to_number(raw: str | None) -> float | None:
    """"85,000円" / "８５０００" / "徒歩8分" → 数値。数字が無ければ None。"""
    text = normalize_text(raw).replace(",", "").replace(" ", "")
    if not text:
        return None
    m = _NUMBER_RE.search(text)
    return float(m.group(0)) if m else None


def to_bool(raw: str | None) -> bool:
    text = normalize_text(raw).lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    # 判断できない語は「込みではない」に倒す（賃料を過大評価しない方向）
    return False


def rent_unit_price(monthly_rent: float, admin_fee: float, exclusive_area: float) -> float:
    """主指標＝(賃料＋管理費) ÷ 面積 円/㎡/月。

    管理費を足す理由: 募集情報の「賃料/管理費」の切り分けは恣意的で、同じ実質額を
    「8.0万＋0.5万」とも「8.3万＋0.2万」とも書ける。切り分け方の違いを吸収する。
    """
    if exclusive_area <= 0:
        raise ValueError("面積は正の数である必要があります")
    return (monthly_rent + admin_fee) / exclusive_area


def content_hash(values: dict, source_ref: str) -> str:
    """一意 ID が無いので内容ハッシュで冪等にする（売買側 `reinfolib.content_hash` と同じ作法）。"""
    key = "|".join(
        str(values.get(k, "") or "")
        for k in (
            "property_kind",
            "district_code",
            "monthly_rent",
            "admin_fee",
            "exclusive_area",
            "layout",
            "building_year",
            "survey_year",
            "survey_month",
        )
    )
    return hashlib.sha256(f"{key}|{source_ref}".encode()).hexdigest()


def _map_header(cells: list[str]) -> list[str]:
    """ヘッダ行 → フィールド名の並び。未知の列は空文字（無視する）。"""
    mapped: list[str] = []
    for cell in cells:
        name = normalize_text(cell)
        if name in FIELD_NAMES:
            mapped.append(name)
        else:
            mapped.append(HEADER_ALIASES.get(name, ""))
    return mapped


def _split_line(line: str) -> list[str]:
    """1行だけを CSV として読む（行番号を保つため reader に流し込まない）。"""
    return next(csv.reader([line]), [])


def _significant_lines(text: str) -> list[tuple[int, str]]:
    """(元テキストの行番号, 行) のうち、空行と `#` コメント行を除いたもの。"""
    return [
        (no, line)
        for no, line in enumerate(text.splitlines(), start=1)
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _build_record(values: dict[str, str], source_ref: str, source: str) -> RentRecord:
    """1行ぶんの生値 → RentRecord。不正なら ValueError（呼び出し側が行番号を付ける）。"""
    kind = normalize_text(values.get("property_kind"))
    if kind not in RENT_KINDS:
        raise ValueError(f"種別が不正です（{' / '.join(RENT_KINDS)} のいずれか）: {kind or '空'}")

    district_code = normalize_text(values.get("district_code"))
    if not _DISTRICT_CODE_RE.match(district_code):
        raise ValueError(f"地区コードは9桁の数字で入力してください: {district_code or '空'}")

    monthly_rent = to_number(values.get("monthly_rent"))
    if not monthly_rent or monthly_rent <= 0:
        raise ValueError("賃料は正の数で入力してください")

    admin_fee = to_number(values.get("admin_fee")) or 0.0
    if admin_fee < 0:
        raise ValueError("管理費に負の数は入れられません")

    area = to_number(values.get("exclusive_area"))
    if not area or area <= 0:
        raise ValueError("面積（区分=専有／戸建=延床）は正の数で入力してください")

    survey_year = to_number(values.get("survey_year"))
    survey_month = to_number(values.get("survey_month"))
    if not survey_year or not 1900 <= survey_year <= 2100:
        raise ValueError("調査年は西暦で入力してください")
    if not survey_month or not 1 <= survey_month <= 12:
        raise ValueError("調査月は 1〜12 で入力してください")

    status = normalize_text(values.get("status")) or RENT_STATUS_OPEN
    if status not in RENT_STATUSES:
        raise ValueError(f"状態が不正です（{' / '.join(RENT_STATUSES)} のいずれか）: {status}")

    building_year = to_number(values.get("building_year"))  # 空は None のまま（築年条件が効かなくなるだけ）
    municipality_code = normalize_text(values.get("municipality_code")) or district_code[:5]

    normalized = {
        "property_kind": kind,
        "district_code": district_code,
        "monthly_rent": int(round(monthly_rent)),
        "admin_fee": int(round(admin_fee)),
        "exclusive_area": area,
        "layout": normalize_text(values.get("layout")),
        "building_year": int(building_year) if building_year else None,
        "survey_year": int(survey_year),
        "survey_month": int(survey_month),
    }

    land_area = to_number(values.get("land_area"))
    floor_no = to_number(values.get("floor_no"))
    total_floors = to_number(values.get("total_floors"))
    walk = to_number(values.get("station_walk_min"))

    return RentRecord(
        content_hash=content_hash(normalized, source_ref),
        source=source,
        source_ref=source_ref,
        municipality_code=municipality_code,
        municipality=normalize_text(values.get("municipality")),
        district_code=district_code,
        district_name=normalize_text(values.get("district_name")),
        property_kind=kind,
        monthly_rent=normalized["monthly_rent"],
        admin_fee=normalized["admin_fee"],
        exclusive_area=area,
        land_area=land_area if land_area and land_area > 0 else None,
        layout=normalized["layout"],
        building_year=normalized["building_year"],
        structure=normalize_text(values.get("structure")),
        floor_no=int(floor_no) if floor_no else None,
        total_floors=int(total_floors) if total_floors else None,
        station_walk_min=int(walk) if walk is not None and walk >= 0 else None,
        parking_included=to_bool(values.get("parking_included")),
        survey_year=normalized["survey_year"],
        survey_month=normalized["survey_month"],
        status=status,
        rent_unit_price=rent_unit_price(
            normalized["monthly_rent"], normalized["admin_fee"], area
        ),
    )


def parse_rent_csv(
    text: str, *, source_ref: str, source: str = RENT_SOURCE_CSV
) -> ImportResult:
    """CSV テキスト → (取り込める行, 行番号つきエラー)。1行の失敗で全体を捨てない。"""
    if not source_ref.strip():
        raise RentImportError("取込ラベル（source_ref）は必須です（消して入れ直す単位になります）")

    lines = _significant_lines(text)
    if not lines:
        raise RentImportError("データがありません（ヘッダ行と1行以上の明細が必要です）")

    _, header_line = lines[0]
    fields = _map_header(_split_line(header_line))
    missing = [f for f in REQUIRED_FIELDS if f not in fields]
    if missing:
        raise RentImportError(
            "ヘッダに必須列がありません: "
            + " / ".join(REQUIRED_LABELS[f] for f in missing)
            + f"（1行目: {header_line[:80]}）"
        )

    result = ImportResult()
    seen: set[str] = set()
    for line_no, line in lines[1:]:
        cells = _split_line(line)
        values = {
            name: cells[i] if i < len(cells) else ""
            for i, name in enumerate(fields)
            if name
        }
        try:
            record = _build_record(values, source_ref.strip(), source)
        except ValueError as exc:
            result.errors.append(RowError(line_no=line_no, message=str(exc), raw=line[:80]))
            continue
        if record.content_hash in seen:
            result.errors.append(
                RowError(line_no=line_no, message="同じ取込内で重複した事例です", raw=line[:80])
            )
            continue
        seen.add(record.content_hash)
        result.records.append(record)

    return result

"""賃料CSVのパース（純粋関数）。実際に入ってくる汚れ方を全部ここで受け止める。

匿名化の約束: 合成データは架空自治体コード 99001/99002・地区コード 990010040 系のみを使う。
実在の居住エリアを書かない（巻き戻し禁止）。
"""

from __future__ import annotations

import pytest

from app.constants import RENT_KIND_CONDO, RENT_KIND_HOUSE, RENT_STATUS_CLOSED, RENT_STATUS_OPEN
from app.services.rent_import import (
    CSV_TEMPLATE_HEADER,
    RentImportError,
    content_hash,
    decode_csv,
    normalize_text,
    parse_rent_csv,
    rent_unit_price,
    to_bool,
    to_number,
)

HEADER = CSV_TEMPLATE_HEADER
DISTRICT = "990010040"


def row(
    *,
    district: str = DISTRICT,
    kind: str = RENT_KIND_CONDO,
    rent: str = "85000",
    admin: str = "5000",
    area: str = "62.5",
    layout: str = "3LDK",
    year: str = "2005",
    structure: str = "ＲＣ",
    floor: str = "3",
    floors: str = "8",
    walk: str = "8",
    parking: str = "無",
    s_year: str = "2026",
    s_month: str = "8",
    status: str = RENT_STATUS_OPEN,
) -> str:
    return ",".join(
        [
            district,
            f"地区{district[-2:]}",
            kind,
            rent,
            admin,
            area,
            layout,
            year,
            structure,
            floor,
            floors,
            walk,
            parking,
            s_year,
            s_month,
            status,
        ]
    )


def csv_text(*rows: str, header: str = HEADER) -> str:
    return "\n".join([header, *rows]) + "\n"


# --- 主指標 -------------------------------------------------------------


def test_rent_unit_price_includes_admin_fee():
    # (85,000 + 5,000) / 62.5 = 1,440 円/㎡/月
    assert rent_unit_price(85_000, 5_000, 62.5) == pytest.approx(1440.0)


def test_rent_unit_price_rejects_zero_area():
    with pytest.raises(ValueError):
        rent_unit_price(85_000, 5_000, 0)


# --- 値の正規化 ---------------------------------------------------------


def test_to_number_handles_zenkaku_comma_and_units():
    assert to_number("８５０００") == 85_000
    assert to_number("85,000円") == 85_000
    assert to_number("62.5㎡") == pytest.approx(62.5)
    assert to_number("徒歩8分") == 8
    assert to_number("") is None
    assert to_number("なし") is None


def test_to_bool_reads_japanese_words():
    assert to_bool("有") is True
    assert to_bool("込み") is True
    assert to_bool("無") is False
    assert to_bool("") is False


def test_normalize_text_folds_zenkaku():
    assert normalize_text("　３ＬＤＫ ") == "3LDK"


def test_decode_csv_accepts_utf8_bom_and_cp932():
    text = csv_text(row())
    assert "地区コード" in decode_csv(text.encode("utf-8-sig"))
    assert "地区コード" in decode_csv(text.encode("cp932"))


# --- パース本体 ---------------------------------------------------------


def test_parse_reads_all_columns():
    result = parse_rent_csv(csv_text(row()), source_ref="t1")
    assert result.errors == []
    (record,) = result.records
    assert record.property_kind == RENT_KIND_CONDO
    assert record.district_code == DISTRICT
    assert record.municipality_code == "99001"  # 地区コードの先頭5桁から補う
    assert record.monthly_rent == 85_000
    assert record.admin_fee == 5_000
    assert record.exclusive_area == pytest.approx(62.5)
    assert record.layout == "3LDK"
    assert record.building_year == 2005
    assert record.floor_no == 3
    assert record.total_floors == 8
    assert record.station_walk_min == 8
    assert record.parking_included is False
    assert record.survey_year == 2026
    assert record.survey_month == 8
    assert record.status == RENT_STATUS_OPEN
    assert record.rent_unit_price == pytest.approx(1440.0)
    assert record.source_ref == "t1"


def test_parse_accepts_zenkaku_and_comma_amounts():
    """全角数字はそのまま、半角カンマ入り金額は Excel と同じく引用符つきで入ってくる。"""
    result = parse_rent_csv(csv_text(row(rent="８５０００", admin='"5,000"')), source_ref="t1")
    assert result.errors == []
    assert result.records[0].monthly_rent == 85_000
    assert result.records[0].admin_fee == 5_000
    # 全角カンマは区切り文字ではないので引用符なしでもそのまま読める
    zenkaku = parse_rent_csv(csv_text(row(rent="８５，０００")), source_ref="t1")
    assert zenkaku.errors == []
    assert zenkaku.records[0].monthly_rent == 85_000


def test_parse_skips_blank_and_comment_lines():
    text = "\n".join([HEADER, "# メモ: 8月に見た物件", "", row(), "   ", "# おわり"]) + "\n"
    result = parse_rent_csv(text, source_ref="t1")
    assert len(result.records) == 1
    assert result.errors == []


def test_parse_is_partial_success_with_line_numbers():
    """1行の書き損じで30行を捨てない。行番号は**元テキストの行番号**で返す。"""
    text = "\n".join(
        [
            HEADER,  # 1行目
            row(),  # 2行目: OK
            row(kind="一棟"),  # 3行目: 不正種別
            row(area="0", rent="70000"),  # 4行目: 面積0
            "# コメント",  # 5行目
            row(rent="90000", area="70"),  # 6行目: OK
            row(district="99001", rent="60000"),  # 7行目: 地区コードが9桁でない
        ]
    )
    result = parse_rent_csv(text, source_ref="t1")

    assert result.ok_count == 2
    assert [e.line_no for e in result.errors] == [3, 4, 7]
    assert "種別" in result.errors[0].message
    assert "面積" in result.errors[1].message
    assert "地区コード" in result.errors[2].message


def test_parse_allows_empty_building_year():
    result = parse_rent_csv(csv_text(row(year="")), source_ref="t1")
    assert result.errors == []
    assert result.records[0].building_year is None


def test_parse_defaults_status_and_accepts_closed():
    ok = parse_rent_csv(csv_text(row(status="")), source_ref="t1")
    assert ok.records[0].status == RENT_STATUS_OPEN
    closed = parse_rent_csv(csv_text(row(status=RENT_STATUS_CLOSED)), source_ref="t1")
    assert closed.records[0].status == RENT_STATUS_CLOSED
    bad = parse_rent_csv(csv_text(row(status="商談中")), source_ref="t1")
    assert bad.ok_count == 0 and "状態" in bad.errors[0].message


def test_parse_accepts_house_kind_and_english_headers():
    header = "district_code,property_kind,monthly_rent,exclusive_area,survey_year,survey_month"
    text = csv_text(f"{DISTRICT},{RENT_KIND_HOUSE},120000,95,2026,8", header=header)
    result = parse_rent_csv(text, source_ref="t1")
    assert result.errors == []
    assert result.records[0].property_kind == RENT_KIND_HOUSE
    assert result.records[0].admin_fee == 0


def test_parse_rejects_missing_required_header():
    header = "地区コード,種別,賃料,調査年,調査月"  # 面積が無い
    with pytest.raises(RentImportError) as exc:
        parse_rent_csv(csv_text(f"{DISTRICT},{RENT_KIND_CONDO},85000,2026,8", header=header), source_ref="t1")
    assert "面積" in str(exc.value)


def test_parse_rejects_empty_source_ref_and_empty_text():
    with pytest.raises(RentImportError):
        parse_rent_csv(csv_text(row()), source_ref="  ")
    with pytest.raises(RentImportError):
        parse_rent_csv("# コメントだけ\n", source_ref="t1")


def test_duplicate_rows_within_one_import_are_reported_not_doubled():
    result = parse_rent_csv(csv_text(row(), row()), source_ref="t1")
    assert result.ok_count == 1
    assert result.error_count == 1
    assert "重複" in result.errors[0].message


# --- 冪等キー -----------------------------------------------------------


def test_content_hash_is_stable_and_scoped_to_source_ref():
    values = {
        "property_kind": RENT_KIND_CONDO,
        "district_code": DISTRICT,
        "monthly_rent": 85_000,
        "admin_fee": 5_000,
        "exclusive_area": 62.5,
        "layout": "3LDK",
        "building_year": 2005,
        "survey_year": 2026,
        "survey_month": 8,
    }
    assert content_hash(values, "t1") == content_hash(values, "t1")
    assert content_hash(values, "t1") != content_hash(values, "t2")
    assert content_hash({**values, "monthly_rent": 86_000}, "t1") != content_hash(values, "t1")

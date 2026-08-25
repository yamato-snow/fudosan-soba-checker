"""構造の関所（人が気をつける代わりに機械に見張らせる約束事）。"""

from __future__ import annotations

from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"


def _python_sources() -> list[Path]:
    return sorted(p for p in APP_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def test_jinja_environment_is_defined_exactly_once():
    """ルーターが増えるたびにテンプレート環境をコピーすると、片方だけフィルタを足して壊れる。"""
    defining = [
        p.relative_to(APP_DIR).as_posix()
        for p in _python_sources()
        if "Jinja2Templates(" in p.read_text(encoding="utf-8")
    ]
    assert defining == ["api/templates.py"]

    filters = [
        p.relative_to(APP_DIR).as_posix()
        for p in _python_sources()
        if 'filters["yen"]' in p.read_text(encoding="utf-8")
    ]
    assert filters == ["api/templates.py"]


def test_valuation_is_free_of_infrastructure():
    """統計の芯（valuation.py）は DB も Web も知らない純粋ロジックのまま保つ。"""
    source = (APP_DIR / "services" / "valuation.py").read_text(encoding="utf-8")
    for forbidden in ("sqlalchemy", "fastapi", "jinja2", "app.models"):
        assert forbidden not in source


def test_rent_judgement_layer_does_not_touch_the_database():
    """`rent.py` は純粋。DB を触るのは `rent_repo.py` / `rent_storage.py` だけ。"""
    source = (APP_DIR / "services" / "rent.py").read_text(encoding="utf-8")
    for forbidden in ("sqlalchemy", "fastapi", "app.models"):
        assert forbidden not in source

    parser = (APP_DIR / "services" / "rent_import.py").read_text(encoding="utf-8")
    for forbidden in ("sqlalchemy", "fastapi", "app.models"):
        assert forbidden not in parser


def test_cashflow_is_a_pure_module():
    """`cashflow.py` は DB・FastAPI・Jinja を import しない（純粋関数だけの層）。"""
    source = (APP_DIR / "services" / "cashflow.py").read_text(encoding="utf-8")
    for forbidden in ("sqlalchemy", "fastapi", "jinja2", "Jinja2", "app.models", "app.db"):
        assert forbidden not in source, f"cashflow.py が {forbidden} を参照している"

    tables = (APP_DIR / "services" / "tax_tables.py").read_text(encoding="utf-8")
    for forbidden in ("sqlalchemy", "fastapi", "app.models", "app.db"):
        assert forbidden not in tables


def test_cashflow_documents_why_decimal_is_not_used():
    """割り切りは結果でなくコードに残す（design.md の「意図的に作っていないもの」と同じ作法）。"""
    source = (APP_DIR / "services" / "cashflow.py").read_text(encoding="utf-8")
    assert "Decimal" in source and "使わない理由" in source

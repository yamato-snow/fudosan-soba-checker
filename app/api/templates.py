"""Jinja2 テンプレート環境の唯一の定義。

ルーターが増えるたびに `Jinja2Templates(...)` と `yen` フィルタをコピーすると、
片方だけフィルタを足して「ある画面では動くがある画面では落ちる」壊れ方をする。
テンプレート環境はプロセスに1つでよいのでここに集約する。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["yen"] = lambda v: f"{int(v):,}" if v is not None else "-"

__all__ = ["TEMPLATES_DIR", "templates"]

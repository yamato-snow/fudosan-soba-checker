"""環境変数から読む設定（APIキー等はコードに書かない）。"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.environ.get("SOBA_ENV_FILE", str(PROJECT_ROOT / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # reinfolib 公式API
    reinfolib_api_key: str = ""
    reinfolib_base_url: str = "https://www.reinfolib.mlit.go.jp/ex-api/external"
    reinfolib_min_interval_sec: float = 1.0  # 1req/秒スロットル
    reinfolib_max_retries: int = 4

    # 取込対象: 市区町村コード5桁のカンマ区切り。先頭が基準市（Ring2 の基準）
    target_cities: str = ""
    # Ring1: 生活圏の地区コード（9桁 = 市5桁 + 地区4桁）のカンマ区切り
    neighbor_districts: str = ""
    # Ring0 の既定値（フォームの初期選択に使う。空でも可）
    home_district: str = ""
    # 取込ウィンドウ（直近何年ぶんの四半期を取るか）
    ingest_years: int = 3

    database_url: str = Field(default="")

    @field_validator("database_url")
    @classmethod
    def _default_db(cls, v: str) -> str:
        if v:
            return v
        return f"sqlite+aiosqlite:///{PROJECT_ROOT / 'data' / 'soba.db'}"

    @property
    def target_city_list(self) -> list[str]:
        return [c.strip() for c in self.target_cities.split(",") if c.strip()]

    @property
    def home_city(self) -> str:
        cities = self.target_city_list
        return cities[0] if cities else ""

    @property
    def neighbor_district_list(self) -> list[str]:
        return [d.strip() for d in self.neighbor_districts.split(",") if d.strip()]


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.database_url.startswith("sqlite+aiosqlite:///"):
        db_path = Path(settings.database_url.replace("sqlite+aiosqlite:///", ""))
        db_path.parent.mkdir(parents=True, exist_ok=True)
    return settings

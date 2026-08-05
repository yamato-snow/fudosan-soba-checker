"""不動産情報ライブラリ（国交省）公式API クライアント — XIT001 取引価格情報。

Phase 0（2026-07-21）の実物確認に基づく前提:
  - 数値は生値の文字列（TradePrice="10000", Area="130"）。パースが要るのは
    `BuildingYear`（"1993年"）と `Period`（"2024年第4四半期"）だけ
  - 種別フィルタの API パラメータは無い → クライアント側で Type を絞る
  - レコードの一意 ID は無い → 内容ハッシュで dedup
  - 1回の呼び出しで取引価格情報＋成約価格情報の両方が返る（PriceCategory で区別）
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import asdict, dataclass

import httpx

from app.config import Settings, get_settings
from app.constants import TARGET_TYPES, TYPE_CONDO, TYPE_HOUSE, TYPE_LAND

_PERIOD_RE = re.compile(r"(\d{4})年第(\d)四半期")
_YEAR_RE = re.compile(r"(\d{4})年")

# 一時的な失敗としてリトライする HTTP ステータス
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ReinfolibError(RuntimeError):
    """API 呼び出しが最終的に失敗したときに投げる。"""


@dataclass(frozen=True)
class TransactionRecord:
    """正規化済みの1取引。DB モデルとフィールド名を揃えてある。"""

    content_hash: str
    price_category: str
    land_type: str
    region: str
    municipality_code: str
    municipality: str
    prefecture: str
    district_code: str
    district_name: str
    trade_price: int
    area: float
    total_floor_area: float | None
    building_year: int | None
    unit_price: float | None
    land_unit_price: float | None
    period_year: int
    period_quarter: int
    structure: str
    use: str
    city_planning: str

    def as_dict(self) -> dict:
        return asdict(self)


def parse_building_year(raw: str | None) -> int | None:
    """"1993年" → 1993。"戦前"・空文字・和暦不明値は None（判定から除外せず築年条件だけ効かなくなる）。"""
    if not raw:
        return None
    m = _YEAR_RE.search(raw)
    return int(m.group(1)) if m else None


def parse_period(raw: str | None) -> tuple[int, int]:
    """"2024年第4四半期" → (2024, 4)。"""
    if not raw:
        raise ValueError("Period が空です")
    m = _PERIOD_RE.search(raw)
    if not m:
        raise ValueError(f"Period のパースに失敗: {raw!r}")
    return int(m.group(1)), int(m.group(2))


def _to_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    if not text:
        return None
    # "2000㎡以上" のような表記が混じるケースに備え、数字部分だけ取る
    m = re.search(r"\d+(?:\.\d+)?", text)
    if not m:
        return None
    return float(m.group(0))


def content_hash(raw: dict) -> str:
    """一意IDが無いため (Period, DistrictCode, Type, PriceCategory, TradePrice, Area, BuildingYear) でハッシュ。"""
    key = "|".join(
        str(raw.get(k, "") or "")
        for k in (
            "Period",
            "DistrictCode",
            "Type",
            "PriceCategory",
            "TradePrice",
            "Area",
            "BuildingYear",
        )
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def normalize(raw: dict) -> TransactionRecord | None:
    """XIT001 の生レコード → TransactionRecord。対象外・計算不能なものは None。"""
    land_type = (raw.get("Type") or "").strip()
    if land_type not in TARGET_TYPES:
        return None

    trade_price = _to_float(raw.get("TradePrice"))
    area = _to_float(raw.get("Area"))
    if not trade_price or not area:
        return None

    total_floor_area = _to_float(raw.get("TotalFloorArea"))
    land_unit_price = trade_price / area

    # 主指標: 土地=総額÷土地面積 / 区分=総額÷専有面積（Area が専有面積なので土地と同じ式）
    #        / 戸建=総額÷延床（延床が無い戸建は主指標を計算できない）
    if land_type in (TYPE_LAND, TYPE_CONDO):
        unit_price: float | None = land_unit_price
    elif total_floor_area:
        unit_price = trade_price / total_floor_area
    else:
        unit_price = None

    try:
        period_year, period_quarter = parse_period(raw.get("Period"))
    except ValueError:
        return None

    return TransactionRecord(
        content_hash=content_hash(raw),
        price_category=(raw.get("PriceCategory") or "").strip(),
        land_type=land_type,
        region=(raw.get("Region") or "").strip(),
        municipality_code=(raw.get("MunicipalityCode") or "").strip(),
        municipality=(raw.get("Municipality") or "").strip(),
        prefecture=(raw.get("Prefecture") or "").strip(),
        district_code=(raw.get("DistrictCode") or "").strip(),
        district_name=(raw.get("DistrictName") or "").strip(),
        trade_price=int(trade_price),
        area=area,
        total_floor_area=total_floor_area,
        building_year=parse_building_year(raw.get("BuildingYear")),
        unit_price=unit_price,
        land_unit_price=land_unit_price,
        period_year=period_year,
        period_quarter=period_quarter,
        structure=(raw.get("Structure") or "").strip(),
        use=(raw.get("Use") or "").strip(),
        city_planning=(raw.get("CityPlanning") or "").strip(),
    )


class ReinfolibClient:
    """XIT001 クライアント。1req/秒スロットル＋指数バックオフ付き。"""

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
        sleep=asyncio.sleep,
        monotonic=time.monotonic,
    ) -> None:
        self.settings = settings or get_settings()
        if not self.settings.reinfolib_api_key:
            raise ReinfolibError(
                "REINFOLIB_API_KEY が未設定です（.env に設定してください。コードには書かない）"
            )
        self._client = client
        self._owns_client = client is None
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request_at: float | None = None

    async def __aenter__(self) -> ReinfolibClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _throttle(self) -> None:
        interval = self.settings.reinfolib_min_interval_sec
        if interval <= 0:
            return
        now = self._monotonic()
        if self._last_request_at is not None:
            wait = interval - (now - self._last_request_at)
            if wait > 0:
                await self._sleep(wait)
        self._last_request_at = self._monotonic()

    async def _get(self, path: str, params: dict[str, str]) -> dict:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
            self._owns_client = True

        url = f"{self.settings.reinfolib_base_url.rstrip('/')}/{path}"
        headers = {
            "Ocp-Apim-Subscription-Key": self.settings.reinfolib_api_key,
            "Accept-Encoding": "gzip",
        }

        last_error: str = ""
        for attempt in range(self.settings.reinfolib_max_retries):
            await self._throttle()
            try:
                response = await self._client.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:  # ネットワーク断など
                last_error = f"通信エラー: {exc}"
            else:
                if response.status_code == 200:
                    return response.json()
                if response.status_code == 404:
                    # 未公開の四半期は 404「検索結果がありません」。データ無しとして扱う
                    return {"status": "OK", "data": []}
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                if response.status_code not in _RETRYABLE_STATUS:
                    raise ReinfolibError(f"{url} {params} → {last_error}")
            # 指数バックオフ（1, 2, 4, ... 秒）
            await self._sleep(2**attempt)

        raise ReinfolibError(f"{url} {params} → リトライ上限。最後のエラー: {last_error}")

    async def fetch_quarter(
        self,
        city: str,
        year: int,
        quarter: int,
        price_classification: str | None = None,
    ) -> list[TransactionRecord]:
        """市区町村コード×四半期の取引を取得し、対象2種別だけを正規化して返す。

        price_classification: None=取引価格＋成約価格の両方 / "01"=取引価格のみ / "02"=成約価格のみ
        """
        # XIT001 は city（市区町村コード5桁）指定で足りる（area=都道府県コードは使わない）
        params = {"year": str(year), "quarter": str(quarter), "city": city}
        if price_classification:
            params["priceClassification"] = price_classification

        payload = await self._get("XIT001", params)
        if payload.get("status") not in (None, "OK"):
            raise ReinfolibError(f"API が異常ステータスを返しました: {payload.get('status')}")

        records: list[TransactionRecord] = []
        seen: set[str] = set()
        for raw in payload.get("data") or []:
            record = normalize(raw)
            if record is None or record.content_hash in seen:
                continue
            seen.add(record.content_hash)
            records.append(record)
        return records


def quarters_window(latest_year: int, latest_quarter: int, years: int) -> list[tuple[int, int]]:
    """直近 `years` 年ぶんの (年, 四半期) を古い順に返す。"""
    result: list[tuple[int, int]] = []
    year, quarter = latest_year, latest_quarter
    for _ in range(years * 4):
        result.append((year, quarter))
        quarter -= 1
        if quarter == 0:
            quarter = 4
            year -= 1
    return sorted(result)


__all__ = [
    "ReinfolibClient",
    "ReinfolibError",
    "TransactionRecord",
    "content_hash",
    "normalize",
    "parse_building_year",
    "parse_period",
    "quarters_window",
    "TYPE_CONDO",
    "TYPE_HOUSE",
    "TYPE_LAND",
]

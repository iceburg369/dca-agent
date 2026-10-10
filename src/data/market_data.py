"""시세·재무·거시 데이터 수집.

- 과거 주가·지수·환율: FinanceDataReader (무료, 로그인 불필요)
- 현재가·PER·PBR·EPS·BPS : 한국투자증권 API (설정되어 있으면)
- 위 둘이 안 될 때      : data/ 폴더의 CSV (직접 넣거나 이전에 저장된 캐시)

받아 온 데이터는 data/prices/ 에 캐시해서 같은 날 다시 받지 않습니다.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from pathlib import Path

import pandas as pd

from ..config import DATA_DIR

log = logging.getLogger(__name__)
PRICE_DIR = DATA_DIR / "prices"
PRICE_DIR.mkdir(parents=True, exist_ok=True)
FUND_CSV = DATA_DIR / "fundamentals.csv"
RETRY_WAIT_SEC = 1.5


def _cache_path(symbol: str) -> Path:
    safe = symbol.replace("/", "_").replace(":", "_")
    return PRICE_DIR / f"{safe}.csv"


def _read_cache(symbol: str) -> pd.DataFrame | None:
    p = _cache_path(symbol)
    if not p.exists():
        return None
    df = pd.read_csv(p, index_col=0, parse_dates=True)
    return df if not df.empty else None


def price_history(symbol: str, start: str | None = None, refresh: bool = True) -> pd.DataFrame:
    """일봉. 반환 컬럼에 반드시 'Close' 포함. symbol 예: '005930', 'KS11', 'USD/KRW', 'FRED:DGS10'."""
    start = start or (dt.date.today() - dt.timedelta(days=365 * 6)).isoformat()
    cached = _read_cache(symbol)
    fresh_enough = cached is not None and cached.index.max().date() >= dt.date.today() - dt.timedelta(days=1)
    if cached is not None and (fresh_enough or not refresh):
        return cached.loc[cached.index >= pd.Timestamp(start)]
    try:
        import FinanceDataReader as fdr

        df = fdr.DataReader(symbol, start)
        if "Close" not in df.columns and len(df.columns) == 1:  # FRED 시리즈는 컬럼명이 시리즈명
            df = df.rename(columns={df.columns[0]: "Close"})
        df = df.dropna(subset=["Close"])
        if not df.empty:
            df.to_csv(_cache_path(symbol))
            return df
    except Exception as e:
        log.warning("%s 시세 다운로드 실패(%s) → 캐시 사용", symbol, type(e).__name__)
    if cached is not None:
        return cached.loc[cached.index >= pd.Timestamp(start)]
    raise RuntimeError(f"{symbol} 시세를 구할 수 없습니다 (인터넷 연결 또는 data/prices/{_cache_path(symbol).name} 확인)")


def _manual_fundamentals() -> dict[str, dict]:
    """data/fundamentals.csv : code,eps,bps,eps_growth  (직접 입력용 백업)"""
    if not FUND_CSV.exists():
        return {}
    df = pd.read_csv(FUND_CSV, dtype={"code": str})
    out = {}
    for _, r in df.iterrows():
        out[str(r["code"]).zfill(6)] = {k: (None if pd.isna(r.get(k)) else float(r.get(k))) for k in ("eps", "bps", "eps_growth")}
    return out


class QuoteProvider:
    """현재가 + 기본 재무 지표. KIS 가 있으면 KIS, 없으면 종가 + CSV."""

    def __init__(self, kis=None):
        self.kis = kis
        self.manual = _manual_fundamentals()

    def get(self, code: str) -> dict:
        q: dict = {}
        if self.kis is not None:
            try:
                q = self.kis.quote(code)
            except Exception as e:
                log.warning("KIS 현재가 실패 %s: %s", code, e)
        if self.kis is not None and q.get("eps_growth") is None and hasattr(self.kis, "growth"):
            for attempt in (1, 2):  # 모의투자 API 는 초당 호출 제한이 낮아 한 번 쉬고 재시도
                try:
                    q.update(self.kis.growth(code))
                    break
                except Exception as e:
                    if attempt == 2:
                        log.warning("KIS 이익 성장률 조회 실패 %s: %s", code, str(e)[:120])
                    else:
                        time.sleep(RETRY_WAIT_SEC)
        if not q.get("price"):
            hist = price_history(code)
            q["price"] = float(hist["Close"].iloc[-1])
            last_year = hist.loc[hist.index >= hist.index.max() - pd.Timedelta(days=365), "Close"]
            q.setdefault("w52_high", float(last_year.max()))
            q.setdefault("w52_low", float(last_year.min()))
        m = self.manual.get(code, {})
        for k in ("eps", "bps", "eps_growth"):
            if q.get(k) in (None, 0) and m.get(k) is not None:
                q[k] = m[k]
        if q.get("eps") and q.get("price"):
            q["per"] = q["price"] / q["eps"] if q["eps"] > 0 else None
        if q.get("bps") and q.get("price"):
            q["pbr"] = q["price"] / q["bps"] if q["bps"] > 0 else None
        return q


MACRO_SYMBOLS = {
    "kospi": "KS11",
    "usdkrw": "USD/KRW",
    "us10y": "FRED:DGS10",   # 미국 10년 국채금리
    "vix": "FRED:VIXCLS",    # 미국 변동성 지수
}


def macro_series(lookback_days: int = 400) -> dict[str, pd.Series]:
    start = (dt.date.today() - dt.timedelta(days=lookback_days)).isoformat()
    out = {}
    for key, sym in MACRO_SYMBOLS.items():
        try:
            out[key] = price_history(sym, start)["Close"].astype(float)
        except Exception as e:
            log.warning("거시 지표 %s 없음: %s", key, e)
    return out

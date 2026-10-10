"""뉴스 헤드라인과 DART 공시 수집.

- 뉴스: Google 뉴스 RSS (feedparser, API 키 불필요). 제목·날짜·링크만 가져옵니다.
- 공시: 금융감독원 DART (OpenDartReader, 무료 API 키 필요: opendart.fss.or.kr)
"""
from __future__ import annotations

import datetime as dt
import logging
import time
import urllib.parse
from functools import lru_cache

from ..config import env

log = logging.getLogger(__name__)

# 공시 제목에 이 단어가 있으면 '위험 공시'로 표시 (분석 agent 가 감점·검토)
RISK_DISCLOSURE_WORDS = [
    "유상증자", "전환사채", "신주인수권부사채", "감자", "횡령", "배임", "소송", "불성실공시",
    "관리종목", "상장폐지", "거래정지", "감사의견", "영업정지", "회생", "최대주주변경",
]
POSITIVE_DISCLOSURE_WORDS = ["자기주식취득", "자기주식소각", "현금배당", "단일판매", "공급계약", "무상증자"]


def news_headlines(query: str, days: int = 14, limit: int = 15) -> list[dict]:
    try:
        import feedparser
    except ImportError:
        return []
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"{query} when:{days}d", "hl": "ko", "gl": "KR", "ceid": "KR:ko"}
    )
    try:
        feed = feedparser.parse(url)
    except Exception as e:
        log.warning("뉴스 수집 실패 %s: %s", query, e)
        return []
    out = []
    for e in feed.entries[:limit]:
        published = ""
        if getattr(e, "published_parsed", None):
            published = time.strftime("%Y-%m-%d", e.published_parsed)
        out.append({"title": e.get("title", ""), "date": published, "link": e.get("link", "")})
    return out


@lru_cache(maxsize=1)
def _dart():
    key = env("DART_API_KEY")
    if not key:
        return None
    try:
        try:  # opendartreader 0.3+ (파이썬 3.13 이상)
            from opendartreader import OpenDartReader
        except ImportError:  # 0.2.x (파이썬 3.12 이하) — 패키지 이름 자체가 클래스
            import OpenDartReader  # type: ignore

        return OpenDartReader(key)
    except Exception as e:
        log.warning("DART 연결 실패: %s", e)
        return None


def disclosures(code: str, days: int = 60) -> list[dict]:
    dart = _dart()
    if dart is None:
        return []
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    try:
        df = dart.list(code, start=start.isoformat(), end=end.isoformat())
    except Exception as e:
        log.warning("공시 조회 실패 %s: %s", code, e)
        return []
    if df is None or df.empty:
        return []
    out = []
    for _, r in df.head(30).iterrows():
        title = str(r.get("report_nm", ""))
        tag = "neutral"
        if any(w in title for w in RISK_DISCLOSURE_WORDS):
            tag = "risk"
        elif any(w in title for w in POSITIVE_DISCLOSURE_WORDS):
            tag = "positive"
        out.append({"title": title, "date": str(r.get("rcept_dt", "")), "tag": tag})
    return out


def dart_available() -> bool:
    return _dart() is not None

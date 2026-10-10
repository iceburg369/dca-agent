"""통합(오케스트레이터) — 분석 agent 와 실행 agent 를 하나의 흐름으로 묶는다.

  ┌──────────────┐   analysis_latest.json   ┌──────────────┐
  │ 분석 Agent    │ ───────────────────────▶ │              │
  │ 거시→테마→   │                          │  사람 승인    │ approved.yaml
  │ 이슈→적정가  │                          │ (매수 후보 중 │──────────┐
  └──────────────┘                          │  직접 선택)   │          ▼
                                            └──────────────┘   ┌──────────────┐
                                                               │ 실행 Agent    │
                                                               │ DCA 분할매수  │──▶ 브로커
                                                               │ 분할매도      │   (가상/모의/실전)
                                                               └──────────────┘

두 agent 는 파일(계약)로만 대화합니다. 그래서 각각 따로 실행·수정·테스트할 수 있습니다.
"""
from __future__ import annotations

import datetime as dt
import logging

from .analysis.agent import AnalysisAgent, load_latest
from .config import REPORT_DIR, load_approved, load_strategy
from .data import market_data as md
from .execution.agent import ExecutionAgent
from .execution.broker import make_broker

log = logging.getLogger(__name__)


def build_quote_provider(cfg: dict) -> md.QuoteProvider:
    """한국투자 API 가 설정돼 있으면 현재가·PER·EPS 를 KIS 에서, 아니면 종가+CSV."""
    kis = None
    broker = cfg["execution"]["broker"]
    try:
        from .data.kis_client import KISClient

        kis = KISClient("real" if broker == "kis_real" else "demo")
    except Exception as e:
        log.info("한국투자 시세 미사용 (%s) → 무료 데이터로 진행", str(e)[:80])
    return md.QuoteProvider(kis)


from .config import STATE_DIR

EQUITY_FILE = STATE_DIR / "equity_history.csv"
LATE_NOTICE_FILE = STATE_DIR / "late_notice.txt"
HOLIDAY_FILE = STATE_DIR / "holiday_checked.txt"   # 오늘 휴장일로 확인됐으면 날짜 기록 (휴장일 조회는 하루 1회 권장)
TRADE_START, TRADE_END = "0905", "1515"   # 이 시간 안에 시작된 예약 실행만 매매 (장 마감 15:30 전 여유)


def done_today(today: str) -> bool:
    """오늘 이미 매매를 실행했는지 (가상 장부 평가금액 기록에 오늘 날짜가 있으면)."""
    if HOLIDAY_FILE.exists() and HOLIDAY_FILE.read_text(encoding="utf-8").strip() == today:
        return True
    if not EQUITY_FILE.exists():
        return False
    return any(line.startswith(today + ",") for line in EQUITY_FILE.read_text(encoding="utf-8").splitlines())


def daily_guard(now: dt.datetime, done_today: bool, notified: bool) -> str:
    """GitHub 예약 실행이 몇 시간씩 늦어지는 문제 대응: 장중에 처음 시작된 실행 한 번만 매매."""
    if now.weekday() >= 5:
        return "skip_weekend"
    if done_today:
        return "skip_done"
    hm = now.strftime("%H%M")
    if hm < TRADE_START:
        return "skip_early"
    if hm <= TRADE_END:
        return "run"
    return "skip_late" if notified else "notify_late"


def guarded_daily(**kw) -> dict | None:
    """예약 실행용: 장중 1회만 run_daily. 아니면 건너뛰고 이유를 반환."""
    now = dt.datetime.now()
    today = now.date().isoformat()
    notified = LATE_NOTICE_FILE.exists() and LATE_NOTICE_FILE.read_text(encoding="utf-8").strip() == today
    g = daily_guard(now, done_today(today), notified)
    if g == "run":
        return run_daily(**kw)
    if g == "notify_late":
        from .notify import send

        send(f"⏰ {today} GitHub 예약 실행이 장 시간 안에 시작되지 못해 오늘 자동매매를 건너뛰었습니다. "
             "(Actions → DCA 자동매매 → Run workflow 로 내일 장중에 직접 실행할 수도 있어요)")
        LATE_NOTICE_FILE.write_text(today, encoding="utf-8")
    log.info("예약 실행 건너뜀: %s (%s)", g, now.strftime("%H:%M"))
    return {"skipped": g}


def market_open_today(kis, today: str) -> bool:
    """오늘 장이 열리는지 (한국투자 국내휴장일조회). 확인할 수 없으면 평소대로 진행(True)."""
    if kis is None:
        return True
    try:
        r = kis.is_market_open(today)
    except Exception as e:
        log.warning("휴장일 확인 실패 → 평소대로 진행: %s", str(e)[:120])
        return True
    return True if r is None else bool(r)


def run_analysis(use_llm: bool = True, codes=None, qp=None) -> dict:
    cfg = load_strategy()
    agent = AnalysisAgent(use_llm=use_llm, quote_provider=qp or build_quote_provider(cfg))
    return agent.run(codes)


def run_daily(force_analysis: bool = False, use_llm: bool = True, dry_run: bool = False) -> dict:
    cfg = load_strategy()
    qp = build_quote_provider(cfg)
    interval = cfg["execution"].get("analysis_interval_days", 7)

    analysis = load_latest()
    age = None if analysis is None else (dt.date.today() - dt.date.fromisoformat(analysis["generated_at"][:10])).days
    if force_analysis or analysis is None or age >= interval:
        log.info("분석 agent 실행 (마지막 분석: %s일 전)", age)
        analysis = run_analysis(use_llm=use_llm, qp=qp)
    else:
        log.info("최근 분석(%d일 전) 재사용 — 다시 하려면 --force-analysis", age)

    approved = {str(a["code"]).zfill(6) for a in load_approved()}
    new_candidates = [s for s in analysis["stocks"] if s.get("verdict") == "buy_candidate" and s["code"] not in approved]
    broken = [s for s in analysis["stocks"] if s["code"] in approved and not s.get("thesis_ok", True)]

    today = dt.date.today().isoformat()
    if not market_open_today(qp.kis, today):
        from .notify import send

        HOLIDAY_FILE.write_text(today, encoding="utf-8")
        msg = f"🛌 {today} 휴장일 — 오늘은 매매하지 않습니다 (한국투자 국내휴장일조회 기준)"
        log.info(msg)
        send(msg)
        return {"analysis": analysis, "execution": None, "summary_path": None,
                "new_candidates": new_candidates, "market_closed": True}

    exe = ExecutionAgent(broker=make_broker(cfg), quote_provider=qp, cfg=cfg).run(analysis, dry_run=dry_run)
    L = [f"# 일일 요약 ({today})", "",
         f"- 거시: {analysis['macro']['score']}점 / {analysis['macro']['regime']}",
         f"- 총 평가금액: {exe['equity']:,.0f}원 (시드 대비 {exe['equity'] / exe['initial_cash'] - 1:+.2%})",
         f"- 오늘 체결: {len(exe['executed'])}건", ""]
    if new_candidates:
        L += ["## 🟢 승인 대기 중인 새 매수 후보", ""]
        L += [f"- {s['name']}({s['code']}) 점수 {s['total']}, 현재가 {s['price']:,.0f} / 적정가 {s['fair_value']:,} → `python main.py approve {s['code']}`" for s in new_candidates]
        L.append("")
    if broken:
        L += ["## 🔴 투자논리 점검 필요", ""] + [f"- {s['name']}: {' / '.join(s.get('risks', []))}" for s in broken] + [""]
    if exe["alerts"]:
        L += ["## 알림", ""] + [f"- {a}" for a in exe["alerts"]] + [""]
    L += [f"상세: {analysis.get('report_path', 'reports/analysis_*.md')}, {exe['report_path']}"]
    path = REPORT_DIR / f"daily_{today}.md"
    path.write_text("\n".join(L), encoding="utf-8")

    from .notify import format_daily, send

    send(format_daily(exe, analysis, new_candidates))
    return {"analysis": analysis, "execution": exe, "summary_path": str(path), "new_candidates": new_candidates}

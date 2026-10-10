"""실행 Agent — 분석 결과 + 사람의 승인 목록을 받아 DCA 매수 / 분할 매도를 실행한다.

  입력 : state/analysis_latest.json (분석 agent 결과), config/approved.yaml (사람 승인)
  처리 : rules.decide() 로 주문 결정 → broker 로 주문 → state/positions.json 갱신
  출력 : reports/execution_YYYY-MM-DD.md, state/equity_history.csv

이 agent 는 종목을 고르지 않습니다. 승인되지 않은 종목은 사지 않습니다.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import logging

from ..config import REPORT_DIR, STATE_DIR, load_approved, load_strategy
from ..data import market_data as md
from . import rules
from .broker import make_broker

log = logging.getLogger(__name__)
POS_FILE = STATE_DIR / "positions.json"
EQUITY_FILE = STATE_DIR / "equity_history.csv"
REALIZED_FILE = STATE_DIR / "realized_pnl.csv"   # 매도할 때마다 실현 손익 한 줄씩
REALIZED_FIELDS = ["date", "code", "name", "qty", "sell_price", "avg_price", "fee", "pnl"]
MAX_ANALYSIS_AGE_DAYS = 7


class ExecutionAgent:
    def __init__(self, broker=None, quote_provider=None, cfg=None):
        self.cfg = cfg or load_strategy()
        self.broker = broker or make_broker(self.cfg)
        self.quotes = quote_provider or md.QuoteProvider()
        self.pstates = json.loads(POS_FILE.read_text(encoding="utf-8")) if POS_FILE.exists() else {}

    def _save(self):
        POS_FILE.write_text(json.dumps(self.pstates, ensure_ascii=False, indent=2), encoding="utf-8")

    def run(self, analysis: dict, dry_run: bool = False, today: str | None = None) -> dict:
        today = today or dt.date.today().isoformat()
        alerts, executed, skipped = [], [], []

        age = (dt.date.fromisoformat(today) - dt.date.fromisoformat(analysis["generated_at"][:10])).days
        if age > MAX_ANALYSIS_AGE_DAYS:
            alerts.append(f"🟠 분석 결과가 {age}일 지났습니다 — 매수는 중단하고 매도 규칙만 점검합니다. `python main.py analyze` 를 먼저 실행하세요.")
        stale = age > MAX_ANALYSIS_AGE_DAYS

        by_code = {s["code"]: s for s in analysis["stocks"] if not s.get("error")}
        approved = {str(a["code"]).zfill(6): a for a in load_approved()} if self.cfg["execution"]["require_approval"] else {c: {} for c in by_code}
        holdings = self.broker.holdings()
        targets = sorted(set(approved) | set(holdings))

        # 현재가 & 평가금액
        prices = {}
        for code in targets:
            try:
                prices[code] = self.quotes.get(code)["price"]
            except Exception as e:
                alerts.append(f"⚪ {code}: 현재가 조회 실패({e}) — 건너뜀")
        cash = self.broker.cash()
        equity = cash + sum(h["qty"] * prices.get(c, h["avg_price"]) for c, h in holdings.items())
        ctx = {"date": today, "cash": cash, "equity": equity, "regime": analysis["macro"]["regime"]}

        for code in targets:
            if code not in prices:
                continue
            a = by_code.get(code, {})
            ap = approved.get(code, {})
            stock = {
                "code": code, "name": a.get("name") or ap.get("name") or code, "price": prices[code],
                "fair_value": ap.get("fair_value_override") or a.get("fair_value"),
                "thesis_ok": a.get("thesis_ok", True),
                "approved": code in approved and not stale,
            }
            if code in approved and not a:
                alerts.append(f"⚪ {code}: 승인됐지만 분석 대상(themes.yaml)에 없음 — 적정가 없이는 매수하지 않습니다")
            ps = self.pstates.setdefault(code, rules.new_pstate())
            n_pos = len([h for h in self.broker.holdings().values() if h["qty"] > 0])
            orders, al = rules.decide(self.cfg, ctx, stock, holdings.get(code), ps, n_pos)
            alerts += al
            for o in orders:
                if dry_run:
                    skipped.append({**o, "status": "DRY-RUN (주문 안 함)"})
                    ps.pop("_pending_levels", None)
                    continue
                avg_before = self.broker.holdings().get(code, {}).get("avg_price", 0)
                fill = self.broker.order(o["side"], code, o["qty"], o["price"], when=today, reason=o["reason"])
                if fill.ok:
                    qty_after = self.broker.holdings().get(code, {}).get("qty", 0)
                    rules.apply_fill(ps, o, fill.price, today, qty_after)
                    row = {**o, "fill_price": round(fill.price), "status": fill.message, "name": stock["name"],
                           "basis": analysis_basis(a, stock.get("fair_value"))}
                    if o["side"] == "sell":
                        row["realized_pnl"] = self._record_realized(today, code, stock["name"], o["qty"], fill, avg_before)
                    executed.append(row)
                    ctx["cash"] = self.broker.cash()
                else:
                    ps.pop("_pending_levels", None)
                    skipped.append({**o, "status": fill.message})
        if not dry_run:
            self._save()

        holdings = self.broker.holdings()
        cash = self.broker.cash()
        equity = cash + sum(h["qty"] * prices.get(c, h["avg_price"]) for c, h in holdings.items())
        if not dry_run:
            self._log_equity(today, cash, equity)
        names = {s["code"]: s["name"] for s in analysis["stocks"]}
        names.update({str(a["code"]).zfill(6): a.get("name", a["code"]) for a in approved.values() if a})
        positions = []
        for c, h in sorted(holdings.items()):
            p = prices.get(c, h["avg_price"])
            positions.append({"code": c, "name": names.get(c, c), "qty": h["qty"], "avg_price": h["avg_price"],
                              "price": p, "value": p * h["qty"], "pnl": (p - h["avg_price"]) * h["qty"],
                              "ret": p / h["avg_price"] - 1 if h["avg_price"] else 0.0})
        realized = self._read_realized()
        result = {"date": today, "broker": getattr(self.broker, "name", "?"), "dry_run": dry_run,
                  "executed": executed, "skipped": skipped, "alerts": alerts,
                  "cash": cash, "equity": equity, "holdings": holdings, "prices": prices,
                  "initial_cash": self.cfg["account"]["initial_cash"],
                  "positions": positions, "unrealized": sum(p["pnl"] for p in positions),
                  "realized_today": sum(float(r["pnl"]) for r in realized if r["date"] == today),
                  "realized_total": sum(float(r["pnl"]) for r in realized)}
        path = REPORT_DIR / f"execution_{today}.md"
        path.write_text(render_markdown(result, analysis), encoding="utf-8")
        result["report_path"] = str(path)
        return result

    def _record_realized(self, day, code, name, qty, fill, avg_price) -> float:
        """실현 손익 = (매도가 - 평균 매입가) × 수량 - 매도 수수료·세금.
        가상 장부는 실제 비용을, 한국투자 주문은 설정의 수수료·세율로 추정."""
        c = self.cfg["costs"]
        fee = fill.fee or fill.price * qty * (c["commission_pct"] + c["sell_tax_pct"])
        pnl = (fill.price - avg_price) * qty - fee
        new = not REALIZED_FILE.exists()
        with open(REALIZED_FILE, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=REALIZED_FIELDS)
            if new:
                w.writeheader()
            w.writerow({"date": day, "code": code, "name": name, "qty": qty, "sell_price": round(fill.price, 2),
                        "avg_price": round(avg_price, 2), "fee": round(fee), "pnl": round(pnl, 2)})
        return pnl

    @staticmethod
    def _read_realized() -> list[dict]:
        if not REALIZED_FILE.exists():
            return []
        with open(REALIZED_FILE, encoding="utf-8") as f:
            return list(csv.DictReader(f))

    @staticmethod
    def _log_equity(day, cash, equity):
        rows = []
        if EQUITY_FILE.exists():
            with open(EQUITY_FILE, encoding="utf-8") as f:
                rows = [r for r in csv.DictReader(f) if r["date"] != day]
        rows.append({"date": day, "cash": round(cash), "equity": round(equity)})
        with open(EQUITY_FILE, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["date", "cash", "equity"])
            w.writeheader()
            w.writerows(rows)


def analysis_basis(a: dict, fair_value=None) -> str:
    """주문 알림에 붙일 '왜 이 종목인가' 한 줄: 테마 · 종합점수 · 적정가와 근거 · 뉴스 요약."""
    if not a:
        return ""
    parts = []
    if a.get("theme_name"):
        parts.append(a["theme_name"])
    if a.get("total") is not None:
        parts.append(f"종합 {a['total']}점")
    fv = fair_value or a.get("fair_value")
    if fv:
        notes = a.get("fair_notes") or []
        parts.append(f"적정가 {fv:,.0f}원" + (f" ({notes[0]})" if notes else ""))
    if a.get("summary"):
        parts.append(a["summary"])
    return " · ".join(parts)


def render_markdown(r: dict, analysis: dict) -> str:
    names = {s["code"]: s["name"] for s in analysis["stocks"]}
    ret = r["equity"] / r["initial_cash"] - 1
    L = [f"# 실행 보고서 ({r['date']}) — {r['broker']}{' [DRY-RUN]' if r['dry_run'] else ''}", "",
         f"- 총 평가금액: **{r['equity']:,.0f}원** (시드 대비 {ret:+.2%})",
         f"- 현금: {r['cash']:,.0f}원 ({r['cash'] / r['equity']:.0%})" if r["equity"] else "", ""]
    L += ["## 오늘의 주문", ""]
    rows = r["executed"] + r["skipped"]
    if not rows:
        L.append("- 조건에 맞는 주문 없음 (장기 DCA 에서는 '아무것도 안 하는 날'이 대부분입니다)")
    for o in rows:
        side = "매수" if o["side"] == "buy" else "매도"
        L.append(f"- {side} {names.get(o['code'], o['code'])} {o['qty']}주 @ {o.get('fill_price', o['price']):,.0f} — {o['reason']} [{o['status']}]")
    L += ["", "## 보유 종목", "", "| 종목 | 수량 | 평단 | 현재가 | 수익률 |", "|---|---|---|---|---|"]
    for c, h in r["holdings"].items():
        p = r["prices"].get(c, h["avg_price"])
        L.append(f"| {names.get(c, c)} | {h['qty']} | {h['avg_price']:,.0f} | {p:,.0f} | {p / h['avg_price'] - 1:+.1%} |")
    if r["alerts"]:
        L += ["", "## 확인이 필요한 알림", ""] + [f"- {a}" for a in r["alerts"]]
    return "\n".join(L)

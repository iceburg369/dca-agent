"""DCA 분할매수 · 분할매도 규칙 (순수 함수).

실전 실행 agent 와 백테스트가 '같은 함수'를 쓰므로, 백테스트에서 확인한 규칙이
그대로 모의투자/실전에서 동작합니다.

입력
  cfg        : strategy.yaml
  ctx        : {"date", "cash", "equity", "regime"}
  stock      : {"code","name","price","fair_value","thesis_ok","approved"}
  holding    : {"qty","avg_price"} 또는 None
  pstate     : 종목별 전략 기록 {"tranches","last_buy_price","last_buy_date","peak_qty","sold_levels","invested"}
출력
  (orders, alerts)  orders = [{"side","code","qty","price","reason"}]
"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np


def new_pstate() -> dict:
    return {"tranches": 0, "last_buy_price": None, "last_buy_date": None, "peak_qty": 0, "sold_levels": [], "invested": 0.0}


def trading_days_between(a: str | None, b: str) -> int:
    if not a:
        return 10**6
    return int(np.busday_count(dt.date.fromisoformat(a[:10]), dt.date.fromisoformat(b[:10])))


def decide(cfg: dict, ctx: dict, stock: dict, holding: dict | None, pstate: dict, n_positions: int) -> tuple[list[dict], list[str]]:
    b, s, a = cfg["buy"], cfg["sell"], cfg["account"]
    code, name, price, fair = stock["code"], stock.get("name", stock["code"]), stock["price"], stock.get("fair_value")
    orders, alerts = [], []
    qty_held = holding["qty"] if holding else 0
    tag = f"{name}({code})"

    # ---------------- 매도 판단 (보유 중일 때 먼저) ----------------
    if qty_held > 0:
        avg = holding["avg_price"]
        gain = price / avg - 1 if avg else 0
        if not stock.get("thesis_ok", True):
            if s.get("thesis_broken_action") == "sell_all":
                orders.append({"side": "sell", "code": code, "qty": qty_held, "price": price, "reason": "투자논리 훼손 → 전량 매도"})
                return orders, alerts
            alerts.append(f"🔴 {tag}: 분석 agent 가 투자논리 훼손으로 판단 — 보유 {qty_held}주, 직접 검토 필요 (추가 매수 중단)")
        peak = max(pstate.get("peak_qty", 0), qty_held)
        sell_qty, reasons = 0, []
        for i, lv in enumerate(s["take_profit_ladder"]):
            if i in pstate["sold_levels"] or gain < lv["gain_pct"]:
                continue
            q = max(1, math.floor(peak * lv["sell_pct"]))
            sell_qty += q
            pstate.setdefault("_pending_levels", []).append(i)
            reasons.append(f"수익 {gain:+.1%} ≥ {lv['gain_pct']:.0%}")
        all_ladders_done = len(set(pstate["sold_levels"]) | set(pstate.get("_pending_levels", []))) >= len(s["take_profit_ladder"])
        if fair and all_ladders_done and price >= fair * s["overvalued_ratio_to_fair"]:
            sell_qty = qty_held
            reasons.append(f"적정가 {fair:,.0f}의 {price / fair:.0%} — 고평가 구간 잔량 매도")
        sell_qty = min(sell_qty, qty_held)
        if sell_qty > 0:
            orders.append({"side": "sell", "code": code, "qty": sell_qty, "price": price, "reason": "분할매도: " + "; ".join(reasons)})
            return orders, alerts  # 같은 날 사고팔지 않음

    # ---------------- 매수 판단 ----------------
    if not stock.get("approved", False):
        return orders, alerts
    if not stock.get("thesis_ok", True):
        return orders, alerts
    if not fair:
        alerts.append(f"⚪ {tag}: 적정가가 없어 매수하지 않음 (fair_value_override 로 직접 지정 가능)")
        return orders, alerts
    if price <= fair * b["anomaly_ratio_to_fair"]:
        alerts.append(f"🟠 {tag}: 적정가의 {price / fair:.0%} 까지 하락 — 가치 함정 여부 확인 전까지 매수 보류")
        return orders, alerts
    if qty_held == 0 and n_positions >= a["max_positions"]:
        return orders, alerts
    if pstate["tranches"] >= b["max_tranches"]:
        return orders, alerts

    base = b.get("base_dca", {}) or {}
    base_on = bool(base.get("enabled"))
    # 이번 사이클에 이미 분할매도를 했다면 정기 매수는 멈춤 (팔고 바로 되사는 수수료·세금 낭비 방지)
    base_ok = base_on and not pstate.get("sold_levels") and price <= fair * base.get("max_ratio_to_fair", 1.10)
    since = trading_days_between(pstate.get("last_buy_date"), ctx["date"])

    if pstate["tranches"] == 0 or qty_held == 0:
        if price <= fair * b["entry_ratio_to_fair"]:
            reason = f"첫 매수: 현재가 {price:,.0f} ≤ 적정가 {fair:,.0f}×{b['entry_ratio_to_fair']}"
        elif base_ok:
            reason = f"정기 분할매수 시작: 적정가 {fair:,.0f}의 {price / fair:.0%} (상한 {base.get('max_ratio_to_fair', 1.10):.0%})"
        else:
            return orders, alerts
    else:
        last = pstate.get("last_buy_price") or holding["avg_price"]
        if price <= last * (1 - b["step_drop_pct"]) and since >= b["cooldown_days"]:
            reason = f"추가 매수 {pstate['tranches'] + 1}차: 직전 매수가 대비 {price / last - 1:+.1%}"
        elif base_ok and since >= base.get("interval_days", 20):
            reason = f"정기 분할매수 {pstate['tranches'] + 1}차: {since}거래일 경과, 적정가의 {price / fair:.0%}"
        else:
            return orders, alerts

    budget = ctx["equity"] * a["per_stock_budget_pct"]
    mult = {"risk_off": b["risk_off_size_mult"], "risk_on": b["risk_on_size_mult"]}.get(ctx.get("regime"), 1.0)
    amount = budget / b["max_tranches"] * mult
    amount = min(amount, budget - pstate.get("invested", 0.0))
    spendable = ctx["cash"] - ctx["equity"] * a["min_cash_pct"]
    amount = min(amount, spendable)
    qty = int(amount // price)
    if qty <= 0:
        if spendable < price:
            alerts.append(f"⚪ {tag}: 매수 신호지만 최소 현금 비중 유지로 보류")
        return orders, alerts
    if mult != 1.0:
        reason += f" (거시 {ctx.get('regime')} → 금액 ×{mult})"
    orders.append({"side": "buy", "code": code, "qty": qty, "price": price, "reason": reason})
    return orders, alerts


def apply_fill(pstate: dict, order: dict, fill_price: float, date: str, qty_after: int) -> dict:
    """체결 후 종목 전략 기록 갱신."""
    if order["side"] == "buy":
        pstate["tranches"] += 1
        pstate["last_buy_price"] = fill_price
        pstate["last_buy_date"] = date
        pstate["invested"] = pstate.get("invested", 0.0) + fill_price * order["qty"]
        pstate["peak_qty"] = max(pstate.get("peak_qty", 0), qty_after)
    else:
        pstate["sold_levels"] = sorted(set(pstate["sold_levels"]) | set(pstate.get("_pending_levels", [])))
        if qty_after == 0:  # 전량 매도 → 다음 사이클을 위해 초기화
            pstate.clear()
            pstate.update(new_pstate())
        else:
            ratio = qty_after / (qty_after + order["qty"])
            pstate["invested"] = pstate.get("invested", 0.0) * ratio
    pstate.pop("_pending_levels", None)
    return pstate

"""브로커(주문 창구) 3종 — 실행 agent 는 이 인터페이스만 사용합니다.

  PaperBroker  : 내 PC 의 가상 장부 (state/paper_account.json). 계좌 없이 바로 시작.
  KISBroker    : 한국투자증권 모의투자(demo) 또는 실전(real). 공식 API 사용.

실전(real)은 이중 안전장치가 있어야만 켜집니다:
  - .env 에 LIVE_TRADING=yes
  - 실행 시 터미널에서 '실전주문' 직접 입력
"""
from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import dataclass

from ..config import STATE_DIR, env


def tick_size(price: float) -> int:
    """KRX 호가 단위 (유가·코스닥 공통, 2023년 개편 기준)."""
    for limit, tick in ((2000, 1), (5000, 5), (20000, 10), (50000, 50), (200000, 100), (500000, 500)):
        if price < limit:
            return tick
    return 1000


def round_price(price: float, side: str) -> int:
    """매수는 내림, 매도는 올림으로 호가 단위에 맞춤 (불리하게 체결되지 않도록)."""
    t = tick_size(price)
    return int(math.floor(price / t) * t) if side == "buy" else int(math.ceil(price / t) * t)


@dataclass
class Fill:
    ok: bool
    side: str
    code: str
    qty: int
    price: float
    amount: float
    fee: float
    message: str = ""


class PaperBroker:
    name = "paper"

    def __init__(self, costs: dict, initial_cash: float, path=None):
        self.costs = costs
        self.path = path or STATE_DIR / "paper_account.json"
        if self.path.exists():
            self.acct = json.loads(self.path.read_text(encoding="utf-8"))
            extra = float(initial_cash) - float(self.acct.get("initial_cash", initial_cash))
            if extra > 0:   # strategy.yaml 에서 시드를 올리면 차액을 한 번만 입금 (보유 종목·기록은 그대로)
                self.acct["cash"] += extra
                self.acct["initial_cash"] = float(initial_cash)
                self.acct.setdefault("deposits", []).append(
                    {"date": dt.date.today().isoformat(), "amount": extra, "note": "시드 증액"})
                self._save()
        else:
            self.acct = {"cash": float(initial_cash), "initial_cash": float(initial_cash), "holdings": {}, "trades": []}
            self._save()

    def _save(self):
        self.path.write_text(json.dumps(self.acct, ensure_ascii=False, indent=2), encoding="utf-8")

    def holdings(self) -> dict[str, dict]:
        return {k: dict(v) for k, v in self.acct["holdings"].items() if v["qty"] > 0}

    def cash(self) -> float:
        return float(self.acct["cash"])

    def order(self, side: str, code: str, qty: int, price: float, when: str | None = None, reason: str = "") -> Fill:
        if qty <= 0:
            return Fill(False, side, code, 0, price, 0, 0, "수량 0")
        slip = self.costs.get("slippage_pct", 0)
        px = price * (1 + slip) if side == "buy" else price * (1 - slip)
        amount = px * qty
        fee = amount * self.costs.get("commission_pct", 0)
        if side == "sell":
            fee += amount * self.costs.get("sell_tax_pct", 0)
        h = self.acct["holdings"].setdefault(code, {"qty": 0, "avg_price": 0.0})
        if side == "buy":
            if amount + fee > self.acct["cash"] + 1e-6:
                return Fill(False, side, code, qty, px, amount, fee, "현금 부족")
            new_qty = h["qty"] + qty
            h["avg_price"] = (h["avg_price"] * h["qty"] + amount + fee) / new_qty  # 수수료 포함 평단
            h["qty"] = new_qty
            self.acct["cash"] -= amount + fee
        else:
            if qty > h["qty"]:
                return Fill(False, side, code, qty, px, amount, fee, "보유 수량 부족")
            h["qty"] -= qty
            self.acct["cash"] += amount - fee
            if h["qty"] == 0:
                h["avg_price"] = 0.0
        self.acct["trades"].append({
            "time": when or dt.datetime.now().isoformat(timespec="seconds"),
            "side": side, "code": code, "qty": qty, "price": round(px, 2),
            "amount": round(amount), "fee": round(fee), "reason": reason,
        })
        self._save()
        return Fill(True, side, code, qty, px, amount, fee, "가상 체결")


class KISBroker:
    """한국투자증권 계좌. 주문은 '접수'이며 체결은 장중 시장 상황에 따릅니다."""

    def __init__(self, mode: str = "demo"):
        from ..data.kis_client import KISClient

        if mode == "real":
            if env("LIVE_TRADING") != "yes":
                raise PermissionError("실전 주문은 .env 에 LIVE_TRADING=yes 가 있어야 합니다.")
            typed = input("⚠️ 실전 계좌로 주문합니다. 계속하려면 '실전주문' 을 입력하세요: ").strip()
            if typed != "실전주문":
                raise PermissionError("실전 주문을 취소했습니다.")
        self.name = f"kis_{mode}"
        self.client = KISClient(mode)
        self._cache = None

    def _refresh(self):
        holds, cash = self.client.balance()
        self._cache = ({h["code"]: {"qty": h["qty"], "avg_price": h["avg_price"]} for h in holds}, cash)

    def holdings(self):
        if self._cache is None:
            self._refresh()
        return self._cache[0]

    def cash(self):
        if self._cache is None:
            self._refresh()
        return self._cache[1]

    def order(self, side, code, qty, price, when=None, reason=""):
        px = round_price(price, side)
        r = self.client.order(side, code, qty, px)
        self._cache = None
        return Fill(r["ok"], side, code, qty, px, px * qty, 0, "주문 접수" if r["ok"] else "주문 실패")


def make_broker(cfg: dict):
    kind = cfg["execution"]["broker"]
    if kind == "paper":
        return PaperBroker(cfg["costs"], cfg["account"]["initial_cash"])
    if kind == "kis_demo":
        return KISBroker("demo")
    if kind == "kis_real":
        return KISBroker("real")
    raise ValueError(f"알 수 없는 broker: {kind}")

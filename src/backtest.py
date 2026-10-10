"""DCA 규칙 백테스트 — '과거에 이 규칙대로 했다면?'

실행 agent 와 똑같은 rules.decide() 를 하루씩 돌립니다.

적정가는 과거 시점에 알 수 있었던 정보만 쓰도록 '직전 3년 종가 중앙값 × fair_mult'
로 대신합니다 (미래 정보 사용 금지). 실제 운용에서는 분석 agent 의 EPS/BPS 기반
적정가를 쓰므로, 백테스트는 '매수·매도 규칙의 성격'을 비교하는 용도입니다.

비교 대상
  - 전략     : 이 프로젝트의 DCA 규칙
  - 일시매수 : 첫날 전 종목 균등 매수 후 보유
  - 정기적립 : 매월 첫 거래일 같은 금액씩 균등 매수 (단순 적립식)
"""
from __future__ import annotations

import copy
import tempfile
from pathlib import Path

import pandas as pd

from .config import REPORT_DIR
from .execution import rules
from .execution.broker import PaperBroker


def _mdd(s: pd.Series) -> float:
    return float((s / s.cummax() - 1).min())


def _cagr(s: pd.Series) -> float:
    years = (s.index[-1] - s.index[0]).days / 365.25
    return float((s.iloc[-1] / s.iloc[0]) ** (1 / years) - 1) if years > 0 and s.iloc[0] > 0 else 0.0


def run_backtest(cfg: dict, closes: dict[str, pd.Series], names: dict[str, str] | None = None,
                 start: str | None = None, fair_mult: float = 1.0, window: int = 750, min_window: int = 250) -> dict:
    cfg = copy.deepcopy(cfg)
    names = names or {}
    px = pd.DataFrame(closes).sort_index().ffill()
    fair = px.rolling(window, min_periods=min_window).median() * fair_mult
    if start:
        px, fair = px.loc[start:], fair.loc[start:]
    px = px.loc[fair.dropna(how="all").index]
    fair = fair.loc[px.index]
    if px.empty:
        raise ValueError("백테스트 기간이 너무 짧습니다 (적정가 계산에 최소 1년 데이터 필요)")

    init = float(cfg["account"]["initial_cash"])
    tmp = Path(tempfile.mkdtemp()) / "bt_account.json"
    broker = PaperBroker(cfg["costs"], init, path=tmp)
    pstates = {c: rules.new_pstate() for c in px.columns}
    equity, trades = [], []

    for day, row in px.iterrows():
        d = day.date().isoformat()
        holdings = broker.holdings()
        eq = broker.cash() + sum(h["qty"] * row[c] for c, h in holdings.items())
        ctx = {"date": d, "cash": broker.cash(), "equity": eq, "regime": "neutral"}
        for c in px.columns:
            p = row[c]
            if pd.isna(p):
                continue
            stock = {"code": c, "name": names.get(c, c), "price": float(p),
                     "fair_value": None if pd.isna(fair.at[day, c]) else float(fair.at[day, c]),
                     "thesis_ok": True, "approved": True}
            n_pos = len(broker.holdings())
            orders, _ = rules.decide(cfg, ctx, stock, broker.holdings().get(c), pstates[c], n_pos)
            for o in orders:
                f = broker.order(o["side"], c, o["qty"], o["price"], when=d, reason=o["reason"])
                if f.ok:
                    rules.apply_fill(pstates[c], o, f.price, d, broker.holdings().get(c, {}).get("qty", 0))
                    trades.append({"date": d, "code": c, "name": names.get(c, c), **{k: o[k] for k in ("side", "qty", "reason")}, "price": round(f.price)})
                    ctx["cash"] = broker.cash()
                else:
                    pstates[c].pop("_pending_levels", None)
        holdings = broker.holdings()
        equity.append((day, broker.cash() + sum(h["qty"] * row[c] for c, h in holdings.items()), broker.cash()))

    eq = pd.DataFrame(equity, columns=["date", "equity", "cash"]).set_index("date")

    # 비교 1: 일시매수 (수수료·세금 동일 적용)
    c_cfg = cfg["costs"]
    buy_cost = 1 + c_cfg["commission_pct"] + c_cfg["slippage_pct"]
    sell_cost = 1 - c_cfg["commission_pct"] - c_cfg["sell_tax_pct"] - c_cfg["slippage_pct"]
    first = px.bfill().iloc[0]
    lump = (px.ffill().bfill() / first).mean(axis=1) * init / buy_cost
    lump.iloc[-1] *= sell_cost

    # 비교 2: 매월 정기 적립
    months = px.groupby(px.index.to_period("M")).head(1).index
    per_month = init / len(months)
    units = pd.Series(0.0, index=px.columns)
    cash_left, dca_vals = init, []
    filled = px.ffill().bfill()
    for day, row in filled.iterrows():
        if day in months and cash_left > 1:
            amt = min(per_month, cash_left)
            units += (amt / len(px.columns)) / (row * buy_cost)
            cash_left -= amt
        dca_vals.append(cash_left + float((units * row).sum()))
    dca = pd.Series(dca_vals, index=px.index)
    dca.iloc[-1] = cash_left + (dca.iloc[-1] - cash_left) * sell_cost

    def stats(s):
        return {"final": round(float(s.iloc[-1])), "return": round(float(s.iloc[-1] / init - 1), 4),
                "cagr": round(_cagr(s), 4), "mdd": round(_mdd(s), 4)}

    res = {
        "period": f"{px.index[0].date()} ~ {px.index[-1].date()}",
        "codes": list(px.columns),
        "strategy": {**stats(eq["equity"]), "trades": len(trades),
                     "avg_cash_pct": round(float((eq["cash"] / eq["equity"]).mean()), 3)},
        "lump_sum": stats(lump), "monthly_dca": stats(dca),
        "trades": trades, "equity": eq, "lump": lump, "dca": dca,
    }
    return res


def save_report(res: dict, names: dict[str, str]) -> Path:
    stamp = pd.Timestamp.now().strftime("%Y-%m-%d")
    png = REPORT_DIR / f"backtest_{stamp}.png"
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(10, 5))
        ax.plot(res["equity"].index, res["equity"]["equity"], label="DCA strategy", linewidth=2)
        ax.plot(res["lump"].index, res["lump"], label="Lump-sum buy & hold", alpha=0.7)
        ax.plot(res["dca"].index, res["dca"], label="Monthly fixed DCA", alpha=0.7)
        ax.set_title("Backtest: equity curve (KRW)")
        ax.grid(alpha=0.3)
        ax.legend()
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v / 1e6:,.0f}M"))
        fig.tight_layout()
        fig.savefig(png, dpi=120)
        plt.close(fig)
    except Exception:
        png = None

    def row(label, s):
        return f"| {label} | {s['final']:,} | {s['return']:+.1%} | {s['cagr']:+.1%} | {s['mdd']:.1%} |"

    L = [f"# 백테스트 결과 ({res['period']})", "",
         "대상: " + ", ".join(f"{names.get(c, c)}({c})" for c in res["codes"]), "",
         "| 방식 | 최종금액 | 수익률 | 연환산 | 최대낙폭 |", "|---|---|---|---|---|",
         row("**이 전략 (DCA+분할매도)**", res["strategy"]), row("일시매수 후 보유", res["lump_sum"]),
         row("매월 정기적립", res["monthly_dca"]), "",
         f"- 전략 매매 횟수: {res['strategy']['trades']}회, 평균 현금 비중: {res['strategy']['avg_cash_pct']:.0%}",
         "- 적정가는 '직전 3년 주가 중앙값'으로 대신했습니다 (미래 정보 없이). 실제 운용 적정가와 다릅니다.",
         "- 과거 성과가 미래 수익을 보장하지 않습니다. 생존 편향(지금 살아남은 종목만 고름)도 있습니다.", ""]
    if png:
        L += [f"![equity]({png.name})", ""]
    L += ["## 최근 매매 30건", "", "| 날짜 | 종목 | 구분 | 수량 | 가격 | 이유 |", "|---|---|---|---|---|---|"]
    for t in res["trades"][-30:]:
        L.append(f"| {t['date']} | {t['name']} | {'매수' if t['side'] == 'buy' else '매도'} | {t['qty']} | {t['price']:,} | {t['reason']} |")
    path = REPORT_DIR / f"backtest_{stamp}.md"
    path.write_text("\n".join(L), encoding="utf-8")
    pd.DataFrame(res["trades"]).to_csv(REPORT_DIR / f"backtest_trades_{stamp}.csv", index=False, encoding="utf-8-sig")
    return path

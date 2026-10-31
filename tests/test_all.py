"""인터넷·API 키 없이 돌아가는 테스트.  실행: python -m pytest -q"""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

from src.analysis import scoring as sc
from src.config import load_strategy
from src.execution import rules
from src.execution.broker import PaperBroker, round_price, tick_size


@pytest.fixture
def cfg():
    """규칙 테스트용: 계좌 숫자는 테스트를 만든 기준(1,000만원·종목당 18%·최대 5종목)으로 고정."""
    c = load_strategy()
    c["account"].update(initial_cash=10_000_000, per_stock_budget_pct=0.18, max_positions=5)
    return c


def ctx(cash=10_000_000, equity=10_000_000, regime="neutral", date="2026-10-05"):
    return {"date": date, "cash": cash, "equity": equity, "regime": regime}


def stock(price, fair=100_000, approved=True, thesis_ok=True):
    return {"code": "005930", "name": "테스트", "price": price, "fair_value": fair, "approved": approved, "thesis_ok": thesis_ok}


# ---------------------------------------------------------------- 매수 규칙
def test_no_buy_above_entry(cfg):
    # 정기 분할매수를 끈 상태: 적정가의 95% 위에서는 사지 않음
    cfg["buy"]["base_dca"]["enabled"] = False
    o, _ = rules.decide(cfg, ctx(), stock(97_000), None, rules.new_pstate(), 0)
    assert o == []


def test_base_dca_buys_near_fair(cfg):
    # 정기 분할매수 켬: 적정가의 110% 이하면 첫 매수, 이후 20거래일마다 1회분
    cfg["buy"]["base_dca"].update(enabled=True, interval_days=20, max_ratio_to_fair=1.10)
    o, _ = rules.decide(cfg, ctx(), stock(105_000), None, rules.new_pstate(), 0)
    assert o and "정기 분할매수" in o[0]["reason"]
    # 적정가의 110% 초과면 정기 매수도 안 함
    assert rules.decide(cfg, ctx(), stock(115_000), None, rules.new_pstate(), 0)[0] == []
    ps = rules.new_pstate()
    ps.update(tranches=1, last_buy_price=105_000, last_buy_date="2026-10-01", invested=315_000, peak_qty=3)
    h = {"qty": 3, "avg_price": 105_000}
    assert rules.decide(cfg, ctx(date="2026-10-15"), stock(104_000), h, ps, 1)[0] == []  # 10거래일
    o, _ = rules.decide(cfg, ctx(date="2026-10-30"), stock(104_000), h, ps, 1)  # 21거래일
    assert o and "정기 분할매수 2차" in o[0]["reason"]


def test_first_buy_below_entry(cfg):
    o, _ = rules.decide(cfg, ctx(), stock(94_000), None, rules.new_pstate(), 0)
    assert o and o[0]["side"] == "buy"
    # 1회 금액 = 1000만 × 18% / 5 = 36만 → 3주
    assert o[0]["qty"] == 360_000 // 94_000


def test_not_approved_never_buys(cfg):
    o, _ = rules.decide(cfg, ctx(), stock(50_000 + 10_000, approved=False), None, rules.new_pstate(), 0)
    assert o == []


def test_step_buy_and_cooldown(cfg):
    ps = rules.new_pstate()
    ps.update(tranches=1, last_buy_price=94_000, last_buy_date="2026-10-01", invested=376_000, peak_qty=4)
    h = {"qty": 4, "avg_price": 94_000}
    # -8% 미만 하락 → 매수 없음
    assert rules.decide(cfg, ctx(), stock(88_000), h, ps, 1)[0] == []
    # -8% 이상 하락이지만 쿨다운(5거래일) 미충족
    assert rules.decide(cfg, ctx(date="2026-10-05"), stock(86_000), h, ps, 1)[0] == []
    # 쿨다운 충족
    o, _ = rules.decide(cfg, ctx(date="2026-10-12"), stock(86_000), h, ps, 1)
    assert o and o[0]["side"] == "buy" and "2차" in o[0]["reason"]


def test_max_positions(cfg):
    o, _ = rules.decide(cfg, ctx(), stock(90_000), None, rules.new_pstate(), cfg["account"]["max_positions"])
    assert o == []


def test_min_cash_respected(cfg):
    o, al = rules.decide(cfg, ctx(cash=1_000_000, equity=10_000_000), stock(90_000), None, rules.new_pstate(), 0)
    assert o == [] and any("현금" in a for a in al)


def test_anomaly_holds(cfg):
    o, al = rules.decide(cfg, ctx(), stock(45_000), None, rules.new_pstate(), 0)
    assert o == [] and any("가치 함정" in a for a in al)


def test_risk_off_halves_size(cfg):
    o1, _ = rules.decide(cfg, ctx(), stock(9_000, fair=10_000), None, rules.new_pstate(), 0)
    o2, _ = rules.decide(cfg, ctx(regime="risk_off"), stock(9_000, fair=10_000), None, rules.new_pstate(), 0)
    assert o2[0]["qty"] == pytest.approx(o1[0]["qty"] / 2, abs=1)


# ---------------------------------------------------------------- 매도 규칙
def test_take_profit_ladder(cfg):
    cfg["sell"]["take_profit_ladder"] = [{"gain_pct": g, "sell_pct": 0.25} for g in (0.30, 0.60, 1.00)]
    # +29% 에서는 팔지 않음
    ps0 = rules.new_pstate(); ps0.update(tranches=3, peak_qty=100, last_buy_price=9_500, last_buy_date="2026-09-01")
    o0, _ = rules.decide(cfg, ctx(), stock(12_900, fair=20_000), {"qty": 100, "avg_price": 10_000}, ps0, 1)
    assert not any(o["side"] == "sell" for o in o0)
    ps = rules.new_pstate()
    ps.update(tranches=3, peak_qty=100, invested=1_000_000, last_buy_price=9_500, last_buy_date="2026-09-01")
    h = {"qty": 100, "avg_price": 10_000}
    o, _ = rules.decide(cfg, ctx(), stock(13_100, fair=20_000), h, ps, 1)
    assert o[0]["side"] == "sell" and o[0]["qty"] == 25
    rules.apply_fill(ps, o[0], 13_100, "2026-10-05", 75)
    assert ps["sold_levels"] == [0]
    # 같은 구간에서는 다시 팔지 않음
    o, _ = rules.decide(cfg, ctx(), stock(13_200, fair=20_000), {"qty": 75, "avg_price": 10_000}, ps, 1)
    assert o == []


def test_thesis_broken_alert_only(cfg):
    o, al = rules.decide(cfg, ctx(), stock(80_000, thesis_ok=False), {"qty": 10, "avg_price": 90_000}, rules.new_pstate(), 1)
    assert o == [] and any("투자논리" in a for a in al)


# ---------------------------------------------------------------- 가격·브로커
def test_tick_rounding():
    assert tick_size(1_500) == 1 and tick_size(75_000) == 100 and tick_size(600_000) == 1000
    assert round_price(75_050, "buy") == 75_000 and round_price(75_050, "sell") == 75_100


def test_paper_broker_costs(cfg, tmp_path):
    b = PaperBroker(cfg["costs"], 1_000_000, path=tmp_path / "acct.json")
    assert b.order("buy", "005930", 10, 50_000).ok
    assert b.holdings()["005930"]["qty"] == 10
    assert not b.order("buy", "005930", 100, 50_000).ok  # 현금 부족
    assert b.order("sell", "005930", 10, 50_000).ok
    assert b.cash() < 1_000_000  # 수수료·세금만큼 감소


# ---------------------------------------------------------------- 분석 점수
def test_fair_value_blend():
    r = sc.fair_value({"eps": 5_000, "bps": 50_000}, target_per=12, target_pbr=1.5)
    assert r["fair_value"] == round((60_000 * 0.6 + 75_000 * 0.4) / 1.0)
    assert r["confidence"] == "high"


def test_fair_value_loss_making_uses_pbr():
    r = sc.fair_value({"eps": -100, "bps": 10_000}, target_per=30, target_pbr=3)
    assert r["fair_value"] == 30_000 and r["confidence"] == "medium"


def test_fair_value_uses_forward_eps_for_growth():
    # 예상 EPS = 5,000 × (1+50%) = 7,500 → × PER 20 = 150,000 (PER 100% 반영)
    r = sc.fair_value({"eps": 5_000, "bps": 50_000, "eps_growth": 0.5}, target_per=20, target_pbr=2, per_weight=1.0)
    assert r["fair_value"] == 150_000
    assert any("예상 EPS" in n for n in r["notes"])


def test_fair_value_growth_is_capped():
    # +300% 성장은 +100% 로 제한 (호황 정점 이익을 그대로 믿지 않음)
    r = sc.fair_value({"eps": 5_000, "eps_growth": 3.0}, target_per=10, target_pbr=1, per_weight=1.0, max_growth=1.0)
    assert r["fair_value"] == 100_000
    # 이익 감소는 -50% 까지
    r = sc.fair_value({"eps": 5_000, "eps_growth": -0.9}, target_per=10, target_pbr=1, per_weight=1.0, min_growth=-0.5)
    assert r["fair_value"] == 25_000


def test_fair_value_per_weight_blend():
    # PER 80% : PBR 20%
    r = sc.fair_value({"eps": 5_000, "bps": 50_000}, target_per=20, target_pbr=2, per_weight=0.8)
    assert r["fair_value"] == round(100_000 * 0.8 + 100_000 * 0.2)
    r = sc.fair_value({"eps": 1_000, "bps": 100_000}, target_per=10, target_pbr=1, per_weight=0.3)
    assert r["fair_value"] == round(10_000 * 0.3 + 100_000 * 0.7)


def test_risk_disclosure_lowers_issue():
    s, flags = sc.issue_score_rule([{"title": "유상증자결정", "date": "20260901", "tag": "risk"}])
    assert s < 50 and flags


def _series(n=1500, seed=0, drift=0.0004, vol=0.02, start=50_000):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=dt.date(2026, 10, 2), periods=n)
    return pd.Series(start * np.exp(np.cumsum(rng.normal(drift, vol, n))), index=idx)


def test_macro_score_runs():
    r = sc.macro_score({"kospi": _series(), "usdkrw": _series(start=1350, vol=0.004), "us10y": _series(start=4.5, vol=0.01)})
    assert 0 <= r["score"] <= 100 and r["regime"] in ("risk_on", "neutral", "risk_off")


# ---------------------------------------------------------------- 백테스트
def test_backtest_runs(cfg):
    from src.backtest import run_backtest

    closes = {"000001": _series(seed=1), "000002": _series(seed=2, drift=0.0006)}
    r = run_backtest(cfg, closes, {"000001": "A", "000002": "B"})
    assert r["strategy"]["trades"] > 0
    assert set(r) >= {"strategy", "lump_sum", "monthly_dca"}


# ---------------------------------------------------------------- 두 agent 통합 (오프라인)
def test_end_to_end_offline(cfg, tmp_path, monkeypatch):
    import src.analysis.agent as aa
    import src.execution.agent as ea

    monkeypatch.setattr(aa, "ANALYSIS_FILE", tmp_path / "analysis.json")
    monkeypatch.setattr(aa, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(ea, "POS_FILE", tmp_path / "pos.json")
    monkeypatch.setattr(ea, "EQUITY_FILE", tmp_path / "eq.csv")
    monkeypatch.setattr(ea, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(aa, "universe", lambda: [{"code": "005930", "name": "삼성전자", "theme": "ai_semiconductor"}])

    hist = _series(seed=3, start=70_000)

    class FakeQuotes:
        def get(self, code):  # 적정가 = EPS 9,000 × 목표PER × 비중 + BPS 60,000 × 목표PBR × (1-비중)
            return {"price": 120_000, "eps": 9_000, "bps": 60_000}  # 적정가의 약 80%

    agent = aa.AnalysisAgent(
        use_llm=False, quote_provider=FakeQuotes(),
        macro_loader=lambda: {"kospi": _series(seed=4, start=3000)},
        history_loader=lambda code, *a, **k: pd.DataFrame({"Close": hist}),
        news_loader=lambda *a, **k: [], disclosure_loader=lambda *a, **k: [],
    )
    report = agent.run()
    s = report["stocks"][0]
    from src.config import load_themes
    v = load_themes()["ai_semiconductor"]["valuation"]
    w = v.get("per_weight", 0.6)
    expected = round(9_000 * v["target_per"] * w + 60_000 * v["target_pbr"] * (1 - w))
    assert s["fair_value"] == expected and s["verdict"] in ("buy_candidate", "watch")

    monkeypatch.setattr(ea, "load_approved", lambda: [{"code": "005930", "name": "삼성전자"}])
    broker = PaperBroker(cfg["costs"], cfg["account"]["initial_cash"], path=tmp_path / "acct.json")
    exe = ea.ExecutionAgent(broker=broker, quote_provider=FakeQuotes(), cfg=cfg).run(report, today=report["generated_at"][:10])
    assert exe["executed"] and exe["executed"][0]["side"] == "buy"
    assert broker.holdings()["005930"]["qty"] > 0


def test_analysis_flags_value_trap_like_executor(cfg, tmp_path, monkeypatch):
    """적정가의 50% 이하로 폭락한 종목은 분석 agent 도 '매수 후보'가 아니라 '관찰'로 판정해야 한다
    (실행 agent 의 가치 함정 보류 규칙과 일치)."""
    import src.analysis.agent as aa

    monkeypatch.setattr(aa, "ANALYSIS_FILE", tmp_path / "analysis.json")
    monkeypatch.setattr(aa, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(aa, "universe", lambda: [{"code": "005930", "name": "테스트", "theme": "ai_semiconductor"}])

    class CheapQuotes:  # 적정가 108,000 인데 현재가 40,000 (37%)
        def get(self, code):
            return {"price": 40_000, "eps": 9_000, "bps": 60_000}

    hist = _series(seed=3, start=40_000)
    agent = aa.AnalysisAgent(
        use_llm=False, quote_provider=CheapQuotes(),
        macro_loader=lambda: {"kospi": _series(seed=4, start=3000)},
        history_loader=lambda code, *a, **k: pd.DataFrame({"Close": hist}),
        news_loader=lambda *a, **k: [], disclosure_loader=lambda *a, **k: [],
    )
    s = agent.run()["stocks"][0]
    assert s["verdict"] == "watch"
    assert any("가치 함정" in f for f in s["flags"])


# ---------------------------------------------------------------- 텔레그램 알림
def _exe_result():
    return {
        "date": "2026-10-05", "broker": "paper", "dry_run": False,
        "equity": 10_050_000, "cash": 9_000_000, "initial_cash": 10_000_000,
        "executed": [
            {"side": "buy", "code": "005930", "name": "삼성전자", "qty": 5, "price": 70_000, "fill_price": 70_070,
             "reason": "첫 매수: 현재가 70,000 ≤ 적정가 108,000×0.95", "status": "가상 체결"},
            {"side": "sell", "code": "000660", "name": "SK하이닉스", "qty": 2, "price": 300_000, "fill_price": 299_700,
             "reason": "분할매도: 수익 +31.0% ≥ 30%", "status": "가상 체결"},
        ],
        "skipped": [], "alerts": ["🟠 테스트 알림"],
    }


def test_telegram_message_has_trade_details():
    from src.notify import format_daily

    msg = format_daily(_exe_result(), new_candidates=[{"name": "LS ELECTRIC", "code": "010120", "total": 66, "price": 200_000, "fair_value": 230_000}])
    assert "🔵 매수 삼성전자(005930)" in msg
    assert "5주 × 70,070원 = 350,350원" in msg
    assert "사유: 첫 매수" in msg
    assert "🔴 매도 SK하이닉스(000660)" in msg and "사유: 분할매도" in msg
    assert "테스트 알림" in msg and "LS ELECTRIC" in msg
    assert "+0.50%" in msg


def test_telegram_send_splits_and_never_crashes(monkeypatch):
    from src import notify

    sent = []
    long_text = "\n".join(f"줄 {i} " + "가" * 100 for i in range(100))
    assert notify.send(long_text, sender=sent.append)
    assert len(sent) > 1 and all(len(p) <= 4096 for p in sent)

    def boom(_):
        raise RuntimeError("network down")
    assert notify.send("hi", sender=boom) is False  # 실패해도 예외로 멈추지 않음

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert notify.send("hi") is False  # 설정 없으면 조용히 생략


# ---------------------------------------------------------------- 이익 성장률 (KIS 재무비율)
def test_quote_provider_merges_kis_growth(monkeypatch):
    from src.data import market_data as md

    class FakeKIS:
        def quote(self, code):
            return {"price": 100_000, "eps": 5_000, "bps": 40_000}

        def growth(self, code):
            return {"eps_growth": 0.42, "growth_basis": "2026.06 분기 순이익 증가율"}

    monkeypatch.setattr(md, "_manual_fundamentals", lambda: {})
    q = md.QuoteProvider(FakeKIS()).get("005930")
    assert q["eps_growth"] == 0.42 and q["eps"] == 5_000


def test_quote_provider_survives_growth_failure(monkeypatch):
    from src.data import market_data as md

    class BrokenGrowthKIS:
        def quote(self, code):
            return {"price": 100_000, "eps": 5_000, "bps": 40_000}

        def growth(self, code):
            raise RuntimeError("모의투자 미지원")

    monkeypatch.setattr(md, "_manual_fundamentals", lambda: {})
    q = md.QuoteProvider(BrokenGrowthKIS()).get("005930")
    assert q["price"] == 100_000 and q.get("eps_growth") is None


def test_parse_growth_rows_picks_latest():
    from src.data.kis_client import parse_growth_rows

    rows = [  # 한국투자 재무비율 API 형태 (값은 % 문자열)
        {"stac_yymm": "202512", "ntin_inrt": "30.5", "bsop_prfi_inrt": "25.0"},
        {"stac_yymm": "202606", "ntin_inrt": "120.0", "bsop_prfi_inrt": "80.0"},
        {"stac_yymm": "202603", "ntin_inrt": "", "bsop_prfi_inrt": "60.0"},
    ]
    g = parse_growth_rows(rows)
    assert g["eps_growth"] == pytest.approx(1.20) and "2026.06" in g["growth_basis"]
    # 순이익 증가율이 비어 있으면 영업이익 증가율 사용
    g = parse_growth_rows([{"stac_yymm": "202603", "ntin_inrt": "", "bsop_prfi_inrt": "60.0"}])
    assert g["eps_growth"] == pytest.approx(0.60) and "영업이익" in g["growth_basis"]
    assert parse_growth_rows([]) == {}


def test_quote_provider_retries_growth_once(monkeypatch):
    from src.data import market_data as md

    calls = {"n": 0}

    class FlakyKIS:  # 모의투자 API 호출 제한으로 첫 시도만 실패하는 경우
        def quote(self, code):
            return {"price": 100_000, "eps": 5_000, "bps": 40_000}

        def growth(self, code):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("조회 결과 없음")
            return {"eps_growth": 0.3, "growth_basis": "x"}

    monkeypatch.setattr(md, "_manual_fundamentals", lambda: {})
    monkeypatch.setattr(md, "RETRY_WAIT_SEC", 0)
    q = md.QuoteProvider(FlakyKIS()).get("005930")
    assert q["eps_growth"] == 0.3 and calls["n"] == 2


# ---------------------------------------------------------------- 잔고·수익률·실현손익
def test_realized_pnl_recorded_on_sell(cfg, tmp_path, monkeypatch):
    import src.execution.agent as ea

    monkeypatch.setattr(ea, "POS_FILE", tmp_path / "pos.json")
    monkeypatch.setattr(ea, "EQUITY_FILE", tmp_path / "eq.csv")
    monkeypatch.setattr(ea, "REALIZED_FILE", tmp_path / "realized.csv")
    monkeypatch.setattr(ea, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(ea, "load_approved", lambda: [{"code": "005930", "name": "삼성전자"}])
    cfg["sell"]["take_profit_ladder"] = [{"gain_pct": g, "sell_pct": 0.25} for g in (0.30, 0.60, 1.00)]

    broker = PaperBroker(cfg["costs"], 10_000_000, path=tmp_path / "acct.json")
    broker.order("buy", "005930", 100, 10_000)
    avg = broker.holdings()["005930"]["avg_price"]

    class Q:
        def get(self, code):
            return {"price": 13_100}

    analysis = {"generated_at": "2026-10-05T10:00:00", "macro": {"regime": "neutral"},
                "stocks": [{"code": "005930", "name": "삼성전자", "fair_value": 20_000, "thesis_ok": True}]}
    r = ea.ExecutionAgent(broker=broker, quote_provider=Q(), cfg=cfg).run(analysis, today="2026-10-05")

    sell = [o for o in r["executed"] if o["side"] == "sell"][0]
    assert sell["qty"] == 25
    fill_px = 13_100 * (1 - cfg["costs"]["slippage_pct"])
    fee = fill_px * 25 * (cfg["costs"]["commission_pct"] + cfg["costs"]["sell_tax_pct"])
    expected = (fill_px - avg) * 25 - fee
    assert r["realized_today"] == pytest.approx(expected, abs=2)
    assert r["realized_total"] == pytest.approx(expected, abs=2)
    assert sell["realized_pnl"] == pytest.approx(expected, abs=2)
    pos = r["positions"][0]
    assert pos["qty"] == 75 and pos["ret"] == pytest.approx(13_100 / avg - 1, abs=1e-6)
    assert r["unrealized"] == pytest.approx((13_100 - avg) * 75, abs=2)

    # 다음 날: 오늘 실현손익은 0, 누적은 유지
    r2 = ea.ExecutionAgent(broker=broker, quote_provider=Q(), cfg=cfg).run(analysis, today="2026-10-06")
    assert r2["realized_today"] == 0 and r2["realized_total"] == pytest.approx(expected, abs=2)


def test_telegram_shows_balance_and_realized():
    from src.notify import format_daily

    r = _exe_result()
    r.update(positions=[{"code": "005930", "name": "삼성전자", "qty": 5, "avg_price": 70_070, "price": 72_000,
                         "value": 360_000, "pnl": 9_650, "ret": 0.0275}],
             unrealized=9_650, realized_today=12_345, realized_total=45_678)
    msg = format_daily(r)
    assert "💼 보유 종목" in msg
    assert "삼성전자 5주" in msg and "+2.8%" in msg and "+9,650원" in msg
    assert "실현 손익" in msg and "+12,345원" in msg and "+45,678원" in msg


# ---------------------------------------------------------------- 휴장일 (한국투자 국내휴장일조회)
def test_parse_holiday_rows():
    from src.data.kis_client import parse_holiday_rows

    rows = [{"bass_dt": "20261005", "opnd_yn": "Y"}, {"bass_dt": "20261009", "opnd_yn": "N"}]
    assert parse_holiday_rows(rows, "2026-10-05") is True
    assert parse_holiday_rows(rows, "2026-10-09") is False   # 한글날
    assert parse_holiday_rows(rows, "2026-10-12") is None    # 응답에 없으면 모름


def test_market_open_check_falls_back_safely():
    from src.orchestrator import market_open_today

    class Open:
        def is_market_open(self, d): return True
    class Closed:
        def is_market_open(self, d): return False
    class Broken:
        def is_market_open(self, d): raise RuntimeError("모의투자 미지원")

    assert market_open_today(Open(), "2026-10-05") is True
    assert market_open_today(Closed(), "2026-10-09") is False
    assert market_open_today(Broken(), "2026-10-05") is True   # 확인 실패 → 평소대로 진행
    assert market_open_today(None, "2026-10-05") is True       # KIS 없음 → 평소대로


def test_order_message_includes_analysis_basis(cfg, tmp_path, monkeypatch):
    import src.execution.agent as ea
    from src.notify import format_daily

    monkeypatch.setattr(ea, "POS_FILE", tmp_path / "pos.json")
    monkeypatch.setattr(ea, "EQUITY_FILE", tmp_path / "eq.csv")
    monkeypatch.setattr(ea, "REALIZED_FILE", tmp_path / "realized.csv")
    monkeypatch.setattr(ea, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(ea, "load_approved", lambda: [{"code": "042660", "name": "한화오션"}])

    class Q:
        def get(self, code):
            return {"price": 78_700}

    analysis = {"generated_at": "2026-10-05T10:00:00", "macro": {"regime": "neutral"},
                "stocks": [{"code": "042660", "name": "한화오션", "fair_value": 114_546, "thesis_ok": True,
                            "theme_name": "조선", "total": 70.4, "verdict": "buy_candidate",
                            "fair_notes": ["2026.06 분기 순이익 증가율 +227%", "× PER 18 (비중 70%)"],
                            "summary": "수주 잔고 증가"}]}
    broker = PaperBroker(cfg["costs"], 10_000_000, path=tmp_path / "acct.json")
    r = ea.ExecutionAgent(broker=broker, quote_provider=Q(), cfg=cfg).run(analysis, today="2026-10-05")
    o = r["executed"][0]
    assert "조선" in o["basis"] and "70.4" in o["basis"] and "114,546" in o["basis"] and "순이익 증가율" in o["basis"]
    msg = format_daily(r, analysis)
    assert "근거:" in msg and "수주 잔고 증가" in msg


# ---------------------------------------------------------------- 보유 종목 가격 알림 (watch)
WATCH = {"code": "900001", "name": "가상종목", "avg_price": 100_000, "alerts": [
    {"id": "tp1", "above": 110_000, "msg": "1차 익절 — 1/3 매도 검토"},
    {"id": "tp2", "above": 112_000, "msg": "2차 익절 — 1/3 매도 검토"},
    {"id": "tp3", "above": 115_000, "msg": "목표주가 — 1/3 매도 검토"},
    {"id": "sl", "below": 101_000, "msg": "보호선 이탈 — 나머지 절반 정리 검토"},
]}


def test_watch_fires_each_alert_once():
    from src.watch import check

    fired = set()
    assert check(WATCH, 109_000, fired) == []
    hit = check(WATCH, 112_500, fired)                 # 한 번에 두 단계 돌파
    assert [a["id"] for a in hit] == ["tp1", "tp2"]
    fired |= {a["id"] for a in hit}
    assert check(WATCH, 113_000, fired) == []          # 이미 보낸 알림은 반복하지 않음
    assert [a["id"] for a in check(WATCH, 115_000, fired)] == ["tp3"]
    assert [a["id"] for a in check(WATCH, 100_500, set())] == ["sl"]


def test_watch_message_has_price_return_and_sound():
    from src.watch import format_alert

    msg = format_alert(WATCH, 112_500, [WATCH["alerts"][0], WATCH["alerts"][1]], when="10:12", market="KRX")
    assert "🚨" in msg and "가상종목" in msg and "112,500원" in msg
    assert "+12.5%" in msg                              # 평단 100,000 대비
    assert "1차 익절" in msg and "2차 익절" in msg and "주문은 하지 않았" in msg


def test_send_with_sound_flag():
    from src import notify

    got = []
    notify.send("x", sender=lambda m, **kw: got.append(kw))
    notify.send("y", sender=lambda m, **kw: got.append(kw), silent=False)
    assert got[-1].get("silent") is False


def test_watch_run_persists_and_retries_on_send_failure(tmp_path, monkeypatch):
    import yaml
    import src.watch as wt

    monkeypatch.setattr(wt, "WATCH_FILE", tmp_path / "watch.yaml")
    monkeypatch.setattr(wt, "FIRED_FILE", tmp_path / "fired.json")
    monkeypatch.setattr(wt, "LAST_FILE", tmp_path / "last.json")
    monkeypatch.delenv("WATCH_CONFIG", raising=False)
    (tmp_path / "watch.yaml").write_text(yaml.safe_dump({"watches": [WATCH]}, allow_unicode=True), encoding="utf-8")

    price = {"v": 110_500}
    get_price = lambda code: (price["v"], "KRX")
    ok_send = lambda msg, silent=None: True
    bad_send = lambda msg, silent=None: False

    assert wt.run(get_price, bad_send) == []            # 전송 실패 → 기록 안 함
    assert len(wt.run(get_price, ok_send)) == 1          # 다음 실행에서 다시 시도해 성공
    assert wt.run(get_price, ok_send) == []              # 같은 알림 반복 안 함
    price["v"] = 115_500
    msgs = wt.run(get_price, ok_send)
    assert len(msgs) == 1 and "2차 익절" in msgs[0] and "목표주가" in msgs[0]

    def broken(code):
        raise RuntimeError("API 오류")
    assert wt.run(broken, ok_send) == []                 # 시세 실패해도 멈추지 않음


# ---------------------------------------------------------------- LLM 선택: Claude / Amazon Nova(Bedrock)
def test_llm_provider_auto_selection(monkeypatch):
    from src.analysis.llm import pick_provider

    for k in ("LLM_PROVIDER", "ANTHROPIC_API_KEY", "AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID"):
        monkeypatch.delenv(k, raising=False)
    assert pick_provider() is None
    monkeypatch.delenv("NOVA_API_KEY", raising=False)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "ABSKexample")
    assert pick_provider() == "nova"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    assert pick_provider() == "anthropic"           # 둘 다 있으면 Claude 우선
    monkeypatch.setenv("LLM_PROVIDER", "nova")
    assert pick_provider() == "nova"                # 명시하면 그대로


def test_nova_converse_request_and_json_parse(monkeypatch):
    import boto3
    from botocore.stub import Stubber
    from src.analysis.llm import LLM

    monkeypatch.setenv("LLM_PROVIDER", "nova")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "test-token")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.delenv("NOVA_MODEL_ID", raising=False)
    client = boto3.client("bedrock-runtime", region_name="us-east-1")
    with Stubber(client) as stub:
        stub.add_response(
            "converse",
            {"output": {"message": {"role": "assistant", "content": [
                {"text": '```json\n{"issue_score": 1, "thesis_ok": true, "summary": "수주 증가"}\n```'}]}},
             "stopReason": "end_turn", "usage": {"inputTokens": 10, "outputTokens": 20, "totalTokens": 30},
             "metrics": {"latencyMs": 100}},
            expected_params={
                "modelId": "global.amazon.nova-2-lite-v1:0",
                "system": [{"text": __import__("src.analysis.llm", fromlist=["SYSTEM_BASE"]).SYSTEM_BASE}],
                "messages": [{"role": "user", "content": [{"text": "평가해줘"}]}],
                "inferenceConfig": {"maxTokens": 800, "temperature": 0.2},
            },
        )
        llm = LLM("ignored-claude-model", bedrock_client=client)
        assert llm.available and llm.provider == "nova"
        assert llm.ask_json("평가해줘") == {"issue_score": 1, "thesis_ok": True, "summary": "수주 증가"}
        assert llm.calls == 1


def test_nova_failure_returns_none(monkeypatch):
    import boto3
    from botocore.stub import Stubber
    from src.analysis.llm import LLM

    monkeypatch.setenv("LLM_PROVIDER", "nova")
    client = boto3.client("bedrock-runtime", region_name="us-east-1")
    with Stubber(client) as stub:
        stub.add_client_error("converse", service_error_code="AccessDeniedException", http_status_code=403)
        llm = LLM("x", bedrock_client=client)
        assert llm.ask_json("평가해줘") is None    # 실패해도 분석은 규칙 기반으로 계속


# ---------------------------------------------------------------- Amazon Nova API (nova.amazon.com, OpenAI 호환)
class _FakeResp:
    def __init__(self, status, data):
        self.status_code, self._d = status, data
        self.ok = 200 <= status < 300
        self.text = str(data)

    def json(self):
        return self._d


def test_provider_detects_nova_api_key(monkeypatch):
    from src.analysis.llm import pick_provider

    for k in ("LLM_PROVIDER", "ANTHROPIC_API_KEY", "AWS_ACCESS_KEY_ID", "NOVA_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "ABSKQmVkcm9ja0FQSUtleS1hYmM=")
    assert pick_provider() == "nova"                 # 진짜 Bedrock 키
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-api-key-YmVkcm9jaw==")
    assert pick_provider() == "nova"
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "a1b2c3-some-nova-dev-key")
    assert pick_provider() == "nova_api"             # Bedrock 형식이 아니면 Nova API 키로 간주
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK")
    monkeypatch.setenv("NOVA_API_KEY", "k")
    assert pick_provider() == "nova_api"


def test_nova_api_chat_completions(monkeypatch):
    from src.analysis.llm import LLM

    for k in ("LLM_PROVIDER", "ANTHROPIC_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_BEARER_TOKEN_BEDROCK", "NOVA_MODEL", "NOVA_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NOVA_API_KEY", "secret-key")
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers, body=json)
        return _FakeResp(200, {"choices": [{"message": {"role": "assistant",
                                                         "content": '{"adjust": 3, "summary": "수요 지속"}'}}]})

    llm = LLM("ignored", http_post=fake_post)
    assert llm.provider == "nova_api" and llm.available
    assert llm.ask_json("테마 평가") == {"adjust": 3, "summary": "수요 지속"}
    assert seen["url"] == "https://api.nova.amazon.com/v1/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer secret-key"
    assert seen["body"]["model"] == "nova-2-lite-v1"
    assert seen["body"]["messages"][0]["role"] == "system" and seen["body"]["messages"][1]["content"] == "테마 평가"
    assert "secret-key" not in llm.label


def test_nova_api_error_returns_none(monkeypatch):
    from src.analysis.llm import LLM

    monkeypatch.setenv("LLM_PROVIDER", "nova_api")
    monkeypatch.setenv("NOVA_API_KEY", "k")
    llm = LLM("x", http_post=lambda *a, **k: _FakeResp(401, {"error": "invalid key"}))
    assert llm.ask_json("p") is None


def test_nova_api_full_url_and_custom_auth_header(monkeypatch):
    from src.analysis.llm import LLM

    for k in ("LLM_PROVIDER", "ANTHROPIC_API_KEY", "AWS_BEARER_TOKEN_BEDROCK", "NOVA_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NOVA_API_KEY", "k123")
    monkeypatch.setenv("NOVA_BASE_URL", "https://gw.example.com/llm/v1/chat/completions/")
    monkeypatch.setenv("NOVA_AUTH_HEADER", "x-api-key")
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers)
        return _FakeResp(200, {"choices": [{"message": {"content": '{"ok": true}'}}]})

    llm = LLM("x", http_post=fake_post)
    assert llm.ask_json("p") == {"ok": True}
    assert seen["url"] == "https://gw.example.com/llm/v1/chat/completions"   # 경로 중복 없이
    assert seen["headers"]["x-api-key"] == "k123" and "Authorization" not in seen["headers"]


def test_nova_api_unreachable_stops_after_first_failure_and_hides_host(monkeypatch, caplog):
    import logging
    import requests
    from src.analysis.llm import LLM

    for k in ("LLM_PROVIDER", "ANTHROPIC_API_KEY", "AWS_BEARER_TOKEN_BEDROCK"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NOVA_API_KEY", "k")
    monkeypatch.setenv("NOVA_BASE_URL", "https://internal-gw.example.co.kr/llm/v1")
    calls = {"n": 0}

    def unreachable(url, **kw):
        calls["n"] += 1
        raise requests.exceptions.ConnectTimeout(
            "HTTPSConnectionPool(host='internal-gw.example.co.kr', port=443): Max retries exceeded")

    llm = LLM("x", http_post=unreachable)
    with caplog.at_level(logging.WARNING):
        assert llm.ask_json("a") is None
        assert llm.ask_json("b") is None
        assert llm.ask_json("c") is None
    assert calls["n"] == 1 and not llm.available          # 첫 실패 후 이번 실행에선 AI 끔
    assert "internal-gw.example.co.kr" not in caplog.text  # 주소가 로그에 남지 않음


# ---------------------------------------------------------------- 예약 실행 시간 보호 (장중 1회만)
def test_daily_guard_decisions():
    import datetime as dt
    from src.orchestrator import daily_guard

    d = lambda hm: dt.datetime(2026, 10, 8, int(hm[:2]), int(hm[2:]))
    assert daily_guard(d("0830"), done_today=False, notified=False) == "skip_early"
    assert daily_guard(d("1000"), done_today=False, notified=False) == "run"
    assert daily_guard(d("1510"), done_today=False, notified=False) == "run"
    assert daily_guard(d("1100"), done_today=True, notified=False) == "skip_done"     # 오늘 이미 실행
    assert daily_guard(d("2100"), done_today=False, notified=False) == "notify_late"  # 장중에 못 돌았음 → 알림
    assert daily_guard(d("2130"), done_today=False, notified=True) == "skip_late"     # 알림은 한 번만
    assert daily_guard(d("2100"), done_today=True, notified=False) == "skip_done"
    sat = dt.datetime(2026, 10, 10, 11, 0)
    assert daily_guard(sat, done_today=False, notified=False) == "skip_weekend"


def test_done_today_reads_equity_history(tmp_path, monkeypatch):
    import src.orchestrator as orc

    f = tmp_path / "eq.csv"
    monkeypatch.setattr(orc, "EQUITY_FILE", f)
    assert orc.done_today("2026-10-08") is False
    f.write_text("date,cash,equity\n2026-10-07,1,1\n", encoding="utf-8")
    assert orc.done_today("2026-10-08") is False
    f.write_text("date,cash,equity\n2026-10-07,1,1\n2026-10-08,1,1\n", encoding="utf-8")
    assert orc.done_today("2026-10-08") is True


def test_watch_records_last_price(tmp_path, monkeypatch):
    import datetime as dt
    import json
    import yaml
    import src.watch as wt

    monkeypatch.setattr(wt, "WATCH_FILE", tmp_path / "watch.yaml")
    monkeypatch.setattr(wt, "FIRED_FILE", tmp_path / "fired.json")
    monkeypatch.setattr(wt, "LAST_FILE", tmp_path / "last.json")
    monkeypatch.delenv("WATCH_CONFIG", raising=False)
    (tmp_path / "watch.yaml").write_text(yaml.safe_dump({"watches": [WATCH]}, allow_unicode=True), encoding="utf-8")
    wt.run(lambda c: (109_000, "통합(KRX+NXT)"), lambda m, silent=None: True, now=dt.datetime(2026, 10, 8, 10, 30))
    last = json.loads((tmp_path / "last.json").read_text(encoding="utf-8"))
    assert last["900001"] == {"price": 109000, "market": "통합(KRX+NXT)", "time": "2026-10-08T10:30"}



def test_watch_config_from_secret_overrides_file(tmp_path, monkeypatch):
    """개인 보유 종목·평단·알림 가격은 GitHub Secret(WATCH_CONFIG)에 두고, 저장소 파일은 예시만."""
    import yaml
    import src.watch as wt

    monkeypatch.setattr(wt, "WATCH_FILE", tmp_path / "watch.yaml")
    (tmp_path / "watch.yaml").write_text("watches: []\n", encoding="utf-8")
    monkeypatch.delenv("WATCH_CONFIG", raising=False)
    assert wt.load_watches() == []
    monkeypatch.setenv("WATCH_CONFIG", yaml.safe_dump({"watches": [WATCH]}, allow_unicode=True))
    ws = wt.load_watches()
    assert len(ws) == 1 and ws[0]["code"] == "900001"
    monkeypatch.setenv("WATCH_CONFIG", "watches: [this is: not: valid")       # 잘못 붙여넣어도 멈추지 않음
    assert wt.load_watches() == []


def test_watch_config_tolerates_common_paste_mistakes(monkeypatch, tmp_path):
    """Secret 에 붙여넣을 때 흔한 실수(```yaml 표시 포함, watches: 줄 빠짐, 숫자에 쉼표)에도 동작."""
    import src.watch as wt

    monkeypatch.setattr(wt, "WATCH_FILE", tmp_path / "none.yaml")
    body = ('  - code: "900001"\n    name: 가상종목\n    avg_price: 100,000\n'
            '    alerts:\n      - {id: tp2, above: 112000, msg: "2차"}\n      - {id: sl, below: "101,000", msg: "보호선"}\n')
    for text in ("```yaml\nwatches:\n" + body + "```\n",        # 코드 블록 표시까지 복사
                 body.replace("\n  ", "\n").lstrip(),           # watches: 줄 없이 목록만
                 "watches:\n" + body):
        monkeypatch.setenv("WATCH_CONFIG", text)
        ws = wt.load_watches()
        assert len(ws) == 1 and ws[0]["code"] == "900001", text
        assert ws[0]["avg_price"] == 100000
        assert [a.get("above", a.get("below")) for a in ws[0]["alerts"]] == [112000, 101000]
    monkeypatch.setenv("WATCH_CONFIG", "그냥 글자")                  # 엉뚱한 값이어도 멈추지 않음
    assert wt.load_watches() == []


def test_watch_config_accepts_smart_quotes_and_fullwidth(monkeypatch):
    """휴대폰 붙여넣기에서 생기는 둥근 따옴표(“ ”)·전각 문자·줄바꿈 없는 한 줄에도 동작."""
    from src.watch import parse_watch_config

    t = ('{watches: [{code: “900001”, name: “가상종목”, avg_price: 100000, alerts: '
         '[{id: tp2, above: 112000, msg: “2차 익절 — 1/3 매도 검토”}, {id: sl, below: 101000, msg: ‘보호선’}]}]}')
    ws = parse_watch_config(t)
    assert len(ws) == 1 and ws[0]["code"] == "900001" and len(ws[0]["alerts"]) == 2
    ws = parse_watch_config(t.replace("{", "｛").replace("}", "｝").replace(":", "："))
    assert len(ws) == 1 and len(ws[0]["alerts"]) == 2
    ws = parse_watch_config("﻿" + t.replace(" ", " "))           # 보이지 않는 특수 공백
    assert len(ws) == 1


def test_watch_config_diagnosis_reveals_no_values():
    from src.watch import diagnose_watch_config

    d = diagnose_watch_config('{watches: [{code: "900001", name: 가상종목, avg_price: 100000')
    assert "900001" not in d and "가상종목" not in d and "100000" not in d
    assert "글자" in d


def test_auto_approval_buys_only_analysis_candidates(cfg, tmp_path, monkeypatch):
    """require_approval: false 면 분석 agent 가 '매수 후보'로 판정한 종목만 사람 승인 없이 매매."""
    import src.execution.agent as ea

    for name, f in (("POS_FILE", "pos.json"), ("EQUITY_FILE", "eq.csv"), ("REALIZED_FILE", "r.csv")):
        monkeypatch.setattr(ea, name, tmp_path / f)
    monkeypatch.setattr(ea, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(ea, "load_approved", lambda: [])
    cfg["execution"]["require_approval"] = False

    class Q:
        def get(self, code):
            return {"price": 15_000}

    stocks = [{"code": "900001", "name": "후보", "fair_value": 20_000, "thesis_ok": True, "verdict": "buy_candidate"},
              {"code": "900002", "name": "관찰", "fair_value": 20_000, "thesis_ok": True, "verdict": "watch"}]
    analysis = {"generated_at": "2026-10-05T10:00:00", "macro": {"regime": "neutral"}, "stocks": stocks}
    broker = PaperBroker(cfg["costs"], 10_000_000, path=tmp_path / "acct.json")
    r = ea.ExecutionAgent(broker=broker, quote_provider=Q(), cfg=cfg).run(analysis, today="2026-10-05")
    bought = {o["code"] for o in r["executed"] if o["side"] == "buy"}
    assert bought == {"900001"}                         # 점수 미달('관찰') 종목은 사지 않음

    cfg["execution"]["require_approval"] = True          # 승인 모드면 승인 목록이 비었을 때 아무것도 안 삼
    broker2 = PaperBroker(cfg["costs"], 10_000_000, path=tmp_path / "acct2.json")
    r2 = ea.ExecutionAgent(broker=broker2, quote_provider=Q(), cfg=cfg).run(analysis, today="2026-10-05")
    assert not [o for o in r2["executed"] if o["side"] == "buy"]


# ---------------------------------------------------------------- 내 보유 종목: 자동 기준가 + 매일 리포트
MY = {"code": "900003", "name": "가상전자", "avg_price": 200_000, "qty": 4}


def test_watch_auto_levels_when_no_alerts(cfg):
    """alerts 를 안 적으면 전략 숫자(−8% 추가 매수, +30/60/100% 분할 매도)로 기준가를 자동 생성."""
    from src.watch import plan_levels, with_auto_alerts

    p = plan_levels(MY, cfg)
    assert [round(x) for x in p["buy"][:2]] == [184_000, 169_280]
    assert [round(x["price"]) for x in p["sell"]] == [260_000, 320_000, 400_000]
    assert [x["qty"] for x in p["sell"]] == [1, 1, 1]
    assert [x["qty"] for x in plan_levels({**MY, "qty": 10}, cfg)["sell"]] == [3, 3, 3]   # 남은 1주는 계속 보유
    w = with_auto_alerts(MY, cfg)
    ids = {a["id"]: a for a in w["alerts"]}
    assert ids["buy1"]["below"] == 184_000 and ids["tp30"]["above"] == 260_000
    custom = with_auto_alerts({**MY, "alerts": [{"id": "x", "above": 1}]}, cfg)
    assert [a["id"] for a in custom["alerts"]] == ["x"]           # 직접 적은 알림은 그대로


def test_daily_holdings_report(cfg):
    from src.watch import holdings_report

    msg = holdings_report([MY], lambda c: (190_000, "KRX"), cfg, fair_values={"900003": 250_000},
                          now=__import__("datetime").datetime(2026, 10, 20, 15, 40))
    assert "가상전자" in msg and "190,000" in msg and "-5.0%" in msg
    assert "184,000" in msg and "260,000" in msg                  # 다음 추가 매수가·1차 매도가
    assert "적정가 250,000" in msg and "76%" in msg
    assert "주문은 하지 않" in msg
    msg2 = holdings_report([MY], lambda c: (183_000, "KRX"), cfg, now=__import__("datetime").datetime(2026, 10, 20))
    assert "추가 매수 구간" in msg2
    msg3 = holdings_report([MY], lambda c: (265_000, "KRX"), cfg, now=__import__("datetime").datetime(2026, 10, 20))
    assert "분할 매도 구간" in msg3


def test_expensive_share_buys_one_instead_of_silently_skipping(cfg):
    """1회분 금액(예: 18만원)보다 1주 가격이 비싸도, 종목당 한도 안이면 1주는 산다. 한도를 넘으면 이유를 알린다."""
    from src.execution import rules

    ctx = {"date": "2026-10-23", "cash": 10_000_000, "equity": 10_000_000, "regime": "risk_off"}
    stock = {"code": "900004", "name": "비싼주", "price": 1_295_000, "fair_value": 1_760_000, "thesis_ok": True, "approved": True}
    orders, alerts = rules.decide(cfg, ctx, stock, None, rules.new_pstate(), 0)
    assert len(orders) == 1 and orders[0]["qty"] == 1 and "1주" in orders[0]["reason"]

    too_big = {**stock, "price": 2_500_000, "fair_value": 3_000_000}      # 종목당 한도(180만) 초과
    orders, alerts = rules.decide(cfg, ctx, too_big, None, rules.new_pstate(), 0)
    assert orders == [] and any("한도" in a for a in alerts)


# ---------------------------------------------------------------- 시드 증액 · 종목 확대
def test_paper_seed_increase_adds_cash_once(cfg, tmp_path):
    """strategy.yaml 의 initial_cash 를 올리면 기존 가상 계좌에 차액을 한 번만 입금 (보유 종목·기록 유지)."""
    path = tmp_path / "acct.json"
    b = PaperBroker(cfg["costs"], 10_000_000, path=path)
    b.order("buy", "005930", 10, 100_000)
    cash_before = b.cash()
    b2 = PaperBroker(cfg["costs"], 30_000_000, path=path)
    assert b2.cash() == pytest.approx(cash_before + 20_000_000)
    assert b2.holdings()["005930"]["qty"] == 10 and b2.acct["initial_cash"] == 30_000_000
    assert b2.acct["deposits"][-1]["amount"] == 20_000_000
    b3 = PaperBroker(cfg["costs"], 30_000_000, path=path)      # 다시 열어도 또 입금되지 않음
    assert b3.cash() == pytest.approx(b2.cash())


def test_new_theme_stocks_trigger_reanalysis():
    from src.orchestrator import missing_from_analysis

    themes = {"t": {"candidates": [{"code": "000001"}, {"code": "000002"}]}}
    assert missing_from_analysis({"stocks": [{"code": "000001"}]}, themes) == {"000002"}
    assert missing_from_analysis({"stocks": [{"code": "000001"}, {"code": "000002"}]}, themes) == set()


def test_universe_expanded_and_codes_valid():
    from src.config import load_strategy, load_themes

    codes = [c["code"] for t in load_themes().values() for c in t["candidates"]]
    assert len(codes) >= 30 and len(codes) == len(set(codes))
    assert all(len(c) == 6 and c.isdigit() for c in codes)
    a = load_strategy()["account"]
    assert a["initial_cash"] == 30_000_000 and a["per_stock_budget_pct"] == 0.25 and a["max_positions"] == 6

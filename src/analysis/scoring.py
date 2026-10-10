"""분석 4단계(거시 → 테마 → 이슈 → 적정가)의 규칙 기반 점수 계산.

모든 함수는 순수 계산이라 인터넷 없이 테스트할 수 있습니다.
점수는 모두 0~100, 50 이 중립입니다.
"""
from __future__ import annotations

import math

import pandas as pd


def clamp(x: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, x))


def _chg(s: pd.Series, days: int) -> float | None:
    s = s.dropna()
    if len(s) < 2:
        return None
    base = s.loc[s.index >= pd.Timestamp(s.index.max()) - pd.Timedelta(days=days)]
    if base.empty or base.iloc[0] == 0:
        return None
    return float(s.iloc[-1] / base.iloc[0] - 1)


# ---------------------------------------------------------------- 1. 거시
def macro_score(series: dict[str, pd.Series]) -> dict:
    """국내외 상황 점수. 반환: score, regime(risk_on/neutral/risk_off), signals[]"""
    score, signals = 50.0, []

    k = series.get("kospi")
    if k is not None and len(k) > 200:
        ma200 = k.rolling(200).mean().iloc[-1]
        above = k.iloc[-1] > ma200
        score += 10 if above else -10
        signals.append(f"코스피 {'200일선 위' if above else '200일선 아래'} ({k.iloc[-1]:,.0f} / {ma200:,.0f})")
        dd = k.iloc[-1] / k.tail(250).max() - 1
        if dd < -0.20:
            score -= 10
            signals.append(f"코스피 고점 대비 {dd:.0%} — 약세장 (분할매수에는 기회일 수 있음)")
        elif dd < -0.10:
            score -= 4
            signals.append(f"코스피 고점 대비 {dd:.0%} 조정")

    fx = series.get("usdkrw")
    c = _chg(fx, 60) if fx is not None else None
    if c is not None:
        if c > 0.03:
            score -= 8
            signals.append(f"원/달러 60일 {c:+.1%} 상승 — 원화 약세, 외국인 수급 부담")
        elif c < -0.03:
            score += 5
            signals.append(f"원/달러 60일 {c:+.1%} — 원화 강세")
        else:
            signals.append(f"원/달러 60일 {c:+.1%} 안정")

    r = series.get("us10y")
    if r is not None and len(r.dropna()) > 60:
        r = r.dropna()
        diff = float(r.iloc[-1] - r.loc[r.index >= pd.Timestamp(r.index.max()) - pd.Timedelta(days=60)].iloc[0])
        if diff > 0.30:
            score -= 10
            signals.append(f"미국 10년 금리 60일 {diff:+.2f}%p 급등 ({r.iloc[-1]:.2f}%) — 성장주 할인율 부담")
        elif diff < -0.30:
            score += 8
            signals.append(f"미국 10년 금리 60일 {diff:+.2f}%p 하락 — 성장주 우호")
        else:
            signals.append(f"미국 10년 금리 {r.iloc[-1]:.2f}% (60일 {diff:+.2f}%p)")

    v = series.get("vix")
    if v is not None and len(v.dropna()):
        lv = float(v.dropna().iloc[-1])
        if lv >= 30:
            score -= 10
            signals.append(f"VIX {lv:.1f} — 공포 구간")
        elif lv <= 16:
            score += 4
            signals.append(f"VIX {lv:.1f} — 안정")

    score = clamp(score)
    regime = "risk_on" if score >= 60 else "risk_off" if score <= 40 else "neutral"
    return {"score": round(score, 1), "regime": regime, "signals": signals}


# ---------------------------------------------------------------- 2. 테마
def theme_momentum(stock_closes: list[pd.Series], kospi: pd.Series | None, days: int = 90) -> float | None:
    """테마 종목들의 평균 수익률 - 코스피 수익률 (상대 강도)."""
    rets = [c for c in (_chg(s, days) for s in stock_closes) if c is not None]
    if not rets:
        return None
    avg = sum(rets) / len(rets)
    base = _chg(kospi, days) if kospi is not None else 0.0
    return avg - (base or 0.0)


def theme_score(base_score: float, rel_strength: float | None, llm_adj: float | None = None) -> float:
    s = float(base_score)
    if rel_strength is not None:
        s += clamp(rel_strength * 50, -10, 10)  # 코스피 대비 +20%p 면 +10
    if llm_adj is not None:
        s += clamp(llm_adj, -15, 15)
    return round(clamp(s), 1)


# ---------------------------------------------------------------- 3. 이슈
def issue_score_rule(disclosures: list[dict]) -> tuple[float, list[str]]:
    s, flags = 50.0, []
    for d in disclosures:
        if d.get("tag") == "risk":
            s -= 15
            flags.append(f"위험 공시: {d['title']} ({d['date']})")
        elif d.get("tag") == "positive":
            s += 4
    return clamp(s), flags


def issue_score(rule: float, llm: dict | None) -> float:
    if not llm:
        return round(rule, 1)
    # LLM 의 issue_score(-2~+2)를 0~100 으로 바꿔 규칙 점수와 평균
    try:
        l = 50 + float(llm.get("issue_score", 0)) * 20
    except (TypeError, ValueError):
        return round(rule, 1)
    return round(clamp((rule + clamp(l)) / 2), 1)


# ---------------------------------------------------------------- 4. 적정가
def fair_value(q: dict, target_per: float, target_pbr: float, hist_close: pd.Series | None = None,
               per_weight: float = 0.6, max_growth: float = 1.0, min_growth: float = -0.5) -> dict:
    """적정가 = (예상 EPS × 목표PER) × per_weight + (BPS × 목표PBR) × (1 - per_weight)

    - 예상 EPS = 최근 EPS × (1 + 이익 성장률). 성장률이 없으면 최근 EPS 그대로.
      성장주는 지난 이익으로 보면 늘 비싸 보이므로, '내년 이익' 기준으로 평가한다.
      단, 호황 정점의 성장률을 그대로 믿지 않도록 max_growth / min_growth 로 제한.
    - per_weight: 테마마다 PER 과 PBR 을 얼마나 볼지 (성장주는 PER 위주, 금융은 PBR 위주)
    - 재무 지표가 전혀 없으면 3년 가격 중앙값을 '임시 기준'으로 쓰고 신뢰도 low
    """
    eps, bps, g = q.get("eps"), q.get("bps"), q.get("eps_growth")
    parts, notes = [], []
    if eps and eps > 0:
        e = eps
        if g is not None:
            gc = max(min_growth, min(max_growth, g))
            e = eps * (1 + gc)
            capped = " (제한 적용)" if gc != g else ""
            notes.append(f"예상 EPS {e:,.0f} = 최근 {eps:,.0f} × (1{gc:+.0%}){capped}")
        parts.append((e * target_per, per_weight))
        notes.append(f"× PER {target_per:g} (비중 {per_weight:.0%})")
    elif eps is not None and eps <= 0:
        notes.append("적자(EPS≤0) → PER 평가 제외")
    if bps and bps > 0 and per_weight < 1:
        parts.append((bps * target_pbr, 1 - per_weight))
        notes.append(f"BPS {bps:,.0f} × PBR {target_pbr:g} (비중 {1 - per_weight:.0%})")

    if parts:
        w = sum(p[1] for p in parts)
        fv = sum(p[0] * p[1] for p in parts) / w
        conf = "high" if len(parts) == 2 else "medium"
    elif hist_close is not None and len(hist_close) > 100:
        fv = float(hist_close.tail(750).median())
        conf = "low"
        notes.append("재무지표 없음 → 3년 주가 중앙값을 임시 기준으로 사용")
    else:
        return {"fair_value": None, "confidence": "none", "notes": ["적정가 계산 불가"]}
    return {"fair_value": round(fv), "confidence": conf, "notes": notes}


def valuation_score(price: float, fv: float | None, confidence: str) -> float:
    if not fv or not price:
        return 40.0
    upside = fv / price - 1  # +30% 저평가면 95점
    s = 50 + upside * 150
    if confidence == "low":
        s = 50 + (s - 50) * 0.5  # 근거 약하면 점수 폭을 절반으로
    return round(clamp(s), 1)


def total_score(scores: dict, weights: dict) -> float:
    tot = sum(scores[k] * weights[k] for k in weights if scores.get(k) is not None)
    wsum = sum(weights[k] for k in weights if scores.get(k) is not None)
    return round(tot / wsum, 1) if wsum else 0.0


def is_finite(x) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x)

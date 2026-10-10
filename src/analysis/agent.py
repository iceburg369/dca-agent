"""분석 Agent — "무엇을, 얼마에 사면 되는가"를 판단한다.

순서 (사용자가 정한 순서 그대로):
  1) 국제·국내 거시 상황   → macro.score / regime
  2) 테마 강도 (현재·차기·미래) → theme score
  3) 기업 이슈 (뉴스·공시)   → issue score, 투자논리 유지 여부(thesis_ok)
  4) 적정 주가              → fair_value, valuation score
  → 종합 점수 + 판정(buy_candidate / watch / avoid)

결과물:
  state/analysis_latest.json  (실행 agent 가 읽는 계약 파일)
  reports/analysis_YYYY-MM-DD.md (사람이 읽는 보고서)

이 agent 는 주문을 절대 내지 않습니다.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from ..config import REPORT_DIR, STATE_DIR, load_strategy, load_themes, universe
from ..data import market_data as md
from ..data import news
from . import scoring as sc
from .llm import LLM

log = logging.getLogger(__name__)
ANALYSIS_FILE = STATE_DIR / "analysis_latest.json"


class AnalysisAgent:
    def __init__(self, use_llm: bool = True, quote_provider: md.QuoteProvider | None = None,
                 macro_loader=md.macro_series, history_loader=md.price_history,
                 news_loader=news.news_headlines, disclosure_loader=news.disclosures):
        self.cfg = load_strategy()
        self.themes = load_themes()
        self.llm = LLM(self.cfg["analysis"]["llm_model"], enabled=use_llm)
        self.quotes = quote_provider or md.QuoteProvider()
        self._macro_loader = macro_loader
        self._hist = history_loader
        self._news = news_loader
        self._disc = disclosure_loader

    # ------------------------------------------------------------ 1. 거시
    def analyze_macro(self) -> dict:
        series = self._macro_loader()
        res = sc.macro_score(series)
        if self.llm.available:
            heads = []
            for q in ("미국 금리 연준", "원달러 환율", "코스피 외국인"):
                heads += [h["title"] for h in self._news(q, days=7, limit=6)]
            j = self.llm.ask_json(
                "다음은 최근 7일 국내외 시장 헤드라인과 지표 신호입니다.\n"
                f"지표: {res['signals']}\n헤드라인:\n- " + "\n- ".join(heads[:18]) +
                "\n\n한국 장기 성장주 투자자 관점에서 거시 환경을 평가하세요. "
                'JSON: {"adjust": -10~10 정수(지표 점수 보정), "summary": "3문장 이내", '
                '"key_risks": ["최대 3개"]}'
            )
            if j:
                res["score"] = round(sc.clamp(res["score"] + float(j.get("adjust", 0) or 0)), 1)
                res["regime"] = "risk_on" if res["score"] >= 60 else "risk_off" if res["score"] <= 40 else "neutral"
                res["llm_summary"] = j.get("summary", "")
                res["key_risks"] = j.get("key_risks", [])
        return res

    # ------------------------------------------------------------ 2. 테마
    def analyze_themes(self, kospi) -> dict:
        out = {}
        for key, t in self.themes.items():
            closes = []
            for c in t.get("candidates", []):
                try:
                    closes.append(self._hist(str(c["code"]).zfill(6))["Close"])
                except Exception:
                    pass
            rel = sc.theme_momentum(closes, kospi)
            llm_adj, summary, heads = None, "", []
            if self.llm.available:
                for kw in t.get("keywords", [])[:3]:
                    heads += [h["title"] for h in self._news(kw, days=self.cfg["analysis"]["news_lookback_days"], limit=5)]
                j = self.llm.ask_json(
                    f"테마: {t['name']} (단계: {t['stage']})\n최근 헤드라인:\n- " + "\n- ".join(heads[:15]) +
                    "\n\n이 테마의 향후 1~3년 성장 지속성을 평가하세요. "
                    'JSON: {"adjust": -15~15 정수, "summary": "2문장", "outlook": "강화|유지|약화"}'
                )
                if j:
                    llm_adj = float(j.get("adjust", 0) or 0)
                    summary = f"[{j.get('outlook', '')}] {j.get('summary', '')}"
            out[key] = {
                "name": t["name"], "stage": t["stage"],
                "score": sc.theme_score(t.get("base_score", 50), rel, llm_adj),
                "rel_strength_90d": None if rel is None else round(rel, 3),
                "summary": summary,
            }
        return out

    # ------------------------------------------------------------ 3+4. 종목
    def analyze_stock(self, s: dict, macro: dict, theme: dict) -> dict:
        code, tcfg = s["code"], self.themes[s["theme"]]
        res = {"code": code, "name": s["name"], "theme": s["theme"], "theme_name": tcfg["name"]}
        try:
            q = self.quotes.get(code)
        except Exception as e:
            res.update({"error": f"시세 없음: {e}", "verdict": "skip"})
            return res
        try:
            hist = self._hist(code)["Close"]
        except Exception:
            hist = None
        price = q["price"]

        # 3. 이슈
        disc = self._disc(code, self.cfg["analysis"]["disclosure_lookback_days"])
        rule_issue, flags = sc.issue_score_rule(disc)
        llm_issue = None
        if self.llm.available:
            heads = self._news(s["name"], days=self.cfg["analysis"]["news_lookback_days"], limit=12)
            llm_issue = self.llm.ask_json(
                f"종목: {s['name']}({code}), 테마: {tcfg['name']}\n"
                "최근 뉴스 헤드라인:\n- " + "\n- ".join(f"{h['date']} {h['title']}" for h in heads) +
                "\n최근 공시 제목:\n- " + "\n- ".join(f"{d['date']} {d['title']}" for d in disc[:15]) +
                "\n\n장기(1~3년) 투자 관점에서 이 기업의 이슈를 평가하세요. 단기 주가 등락 뉴스는 무시하세요. "
                'JSON: {"issue_score": -2~2 정수(실적·경쟁력 영향), "thesis_ok": true/false(장기 성장 논리 유지 여부), '
                '"risks": ["최대 3개"], "catalysts": ["최대 3개"], "summary": "2문장"}'
            )
        issue = sc.issue_score(rule_issue, llm_issue)
        thesis_ok = True if not llm_issue else bool(llm_issue.get("thesis_ok", True))
        if any("상장폐지" in f or "횡령" in f or "감사의견" in f for f in flags):
            thesis_ok = False

        # 4. 적정가
        v = tcfg.get("valuation", {})
        gcfg = self.cfg["analysis"].get("growth", {})
        fv = sc.fair_value(q, v.get("target_per", 15), v.get("target_pbr", 1.5), hist,
                           per_weight=v.get("per_weight", 0.6),
                           max_growth=gcfg.get("max_growth", 1.0), min_growth=gcfg.get("min_growth", -0.5))
        if q.get("growth_basis"):
            fv["notes"].insert(0, q["growth_basis"])
        val = sc.valuation_score(price, fv["fair_value"], fv["confidence"])

        scores = {"macro": macro["score"], "theme": theme["score"], "issue": issue, "valuation": val}
        total = sc.total_score(scores, self.cfg["analysis"]["weights"])
        entry = round(fv["fair_value"] * self.cfg["buy"]["entry_ratio_to_fair"]) if fv["fair_value"] else None

        # 적정가 대비 지나친 폭락은 '싸다'가 아니라 '점검 필요' (실행 agent 의 매수 보류 규칙과 같은 기준)
        anomaly = bool(fv["fair_value"]) and price <= fv["fair_value"] * self.cfg["buy"]["anomaly_ratio_to_fair"]
        if anomaly:
            flags.append(f"적정가의 {price / fv['fair_value']:.0%} 수준 — 가치 함정 또는 재무 데이터 오류 점검 필요")

        if not thesis_ok or issue < 30:
            verdict = "avoid"
        elif anomaly:
            verdict = "watch"
        elif total >= self.cfg["analysis"]["min_score_to_buy"] and fv["confidence"] in ("high", "medium"):
            verdict = "buy_candidate"
        else:
            verdict = "watch"

        res.update({
            "price": price, "per": q.get("per"), "pbr": q.get("pbr"),
            "fair_value": fv["fair_value"], "fair_confidence": fv["confidence"], "fair_notes": fv["notes"],
            "entry_price": entry,
            "upside_pct": round(fv["fair_value"] / price - 1, 3) if fv["fair_value"] else None,
            "scores": scores, "total": total, "verdict": verdict, "thesis_ok": thesis_ok,
            "flags": flags,
            "risks": (llm_issue or {}).get("risks", []),
            "catalysts": (llm_issue or {}).get("catalysts", []),
            "summary": (llm_issue or {}).get("summary", ""),
        })
        return res

    # ------------------------------------------------------------ 실행
    def run(self, codes: list[str] | None = None) -> dict:
        uni = universe()
        if codes:
            uni = [u for u in uni if u["code"] in codes]
        log.info("분석 시작: %d 종목", len(uni))
        macro_series = self._macro_loader()
        self._macro_loader = lambda: macro_series  # 한 번만 받기
        macro = self.analyze_macro()
        themes = self.analyze_themes(macro_series.get("kospi"))
        stocks = [self.analyze_stock(s, macro, themes[s["theme"]]) for s in uni]
        stocks.sort(key=lambda r: r.get("total", -1), reverse=True)
        report = {
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "llm_used": self.llm.calls > 0, "llm_calls": self.llm.calls, "llm_label": self.llm.label,
            "dart_used": news.dart_available(),
            "macro": macro, "themes": themes, "stocks": stocks,
        }
        ANALYSIS_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        md_path = REPORT_DIR / f"analysis_{dt.date.today().isoformat()}.md"
        md_path.write_text(render_markdown(report), encoding="utf-8")
        report["report_path"] = str(md_path)
        return report


def load_latest() -> dict | None:
    if not ANALYSIS_FILE.exists():
        return None
    return json.loads(ANALYSIS_FILE.read_text(encoding="utf-8"))


VERDICT_KO = {"buy_candidate": "매수 후보", "watch": "관찰", "avoid": "회피", "skip": "데이터 없음"}
REGIME_KO = {"risk_on": "위험선호", "neutral": "중립", "risk_off": "위험회피"}
STAGE_KO = {"current": "현재 주도", "next": "차기 주도 후보", "future": "중장기 성장"}


def render_markdown(r: dict) -> str:
    m = r["macro"]
    L = [f"# 분석 보고서 ({r['generated_at'][:10]})", "",
         f"- AI 분석: {r.get('llm_label', '사용') if r['llm_used'] else '미사용(규칙 기반)'} {r.get('llm_calls', 0)}회 / DART 공시: {'사용' if r['dart_used'] else '미사용'}",
         "", f"## 1. 거시 상황 — {m['score']}점 ({REGIME_KO[m['regime']]})", ""]
    L += [f"- {s}" for s in m["signals"]]
    if m.get("llm_summary"):
        L += ["", f"> {m['llm_summary']}"]
    L += ["", "## 2. 테마", "", "| 테마 | 단계 | 점수 | 90일 상대강도 | 코멘트 |", "|---|---|---|---|---|"]
    for t in sorted(r["themes"].values(), key=lambda x: -x["score"]):
        rs = "-" if t["rel_strength_90d"] is None else f"{t['rel_strength_90d']:+.1%}"
        L.append(f"| {t['name']} | {STAGE_KO.get(t['stage'], t['stage'])} | {t['score']} | {rs} | {t['summary']} |")
    L += ["", "## 3~4. 종목 (이슈 + 적정가)", "",
          "| 종목 | 현재가 | 적정가(신뢰도) | 첫 매수가 | 여력 | 종합 | 판정 |", "|---|---|---|---|---|---|---|"]
    for s in r["stocks"]:
        if s.get("error"):
            L.append(f"| {s['name']} | - | - | - | - | - | {s['error']} |")
            continue
        fv = f"{s['fair_value']:,}({s['fair_confidence']})" if s["fair_value"] else "-"
        up = f"{s['upside_pct']:+.0%}" if s["upside_pct"] is not None else "-"
        ep = f"{s['entry_price']:,}" if s["entry_price"] else "-"
        L.append(f"| {s['name']}({s['code']}) | {s['price']:,.0f} | {fv} | {ep} | {up} | {s['total']} | {VERDICT_KO[s['verdict']]} |")
    L += ["", "### 종목별 메모", ""]
    for s in r["stocks"]:
        if s.get("error"):
            continue
        sc_ = s["scores"]
        L.append(f"**{s['name']}** — 거시 {sc_['macro']:.0f} · 테마 {sc_['theme']:.0f} · 이슈 {sc_['issue']:.0f} · 밸류 {sc_['valuation']:.0f}"
                 f" / 적정가 근거: {', '.join(s['fair_notes'])}")
        for k, label in (("summary", "요약"), ("risks", "리스크"), ("catalysts", "기회"), ("flags", "공시 경고")):
            v = s.get(k)
            if v:
                L.append(f"- {label}: {v if isinstance(v, str) else ' / '.join(v)}")
        L.append("")
    L += ["---", "다음 단계: 마음에 드는 '매수 후보'를 검토한 뒤 `python main.py approve <종목코드>` 로 승인하세요.",
          "이 보고서는 투자 권유가 아니며, 숫자는 설정(config/)과 데이터에 따라 달라집니다."]
    return "\n".join(L)

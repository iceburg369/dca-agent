"""보유 종목 가격 알림 — 정해 둔 가격에 닿으면 텔레그램으로 알림음과 함께 알린다.

  GitHub Secret WATCH_CONFIG (또는 config/watch.yaml) 에 종목과 알림 가격을 적어 두면,
  자동 실행기(.github/workflows/runner.yml)가 평일 2분마다 현재가를 확인한다.
  ⚠️ 주문은 하지 않는다. 알림만 보낸다 (실제 매도는 사용자가 직접).

  각 알림은 한 번만 보낸다 (state/watch_fired.json 에 기록).
  다시 받고 싶으면 설정에서 알림 id 를 바꾸거나 reset: true 로 저장.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

import yaml

from .config import CONFIG_DIR, STATE_DIR

log = logging.getLogger(__name__)
WATCH_FILE = CONFIG_DIR / "watch.yaml"
FIRED_FILE = STATE_DIR / "watch_fired.json"
LAST_FILE = STATE_DIR / "watch_last.json"    # 마지막으로 확인한 가격


def _num(v):
    """'100,000' / '100000원' 처럼 적어도 숫자로."""
    if isinstance(v, str):
        t = v.replace(",", "").replace("원", "").strip()
        try:
            return float(t) if "." in t else int(t)
        except ValueError:
            return v
    return v


_FIX = str.maketrans({"“": '"', "”": '"', "„": '"', "‘": "'", "’": "'", "\u00a0": " ", "\u3000": " ",
                      "\ufeff": "", "\u200b": "", "\t": "  "})


def _normalize(text: str) -> str:
    """휴대폰·메신저 붙여넣기에서 바뀌는 문자(둥근 따옴표, 전각 기호, 특수 공백)를 원래대로."""
    import unicodedata

    out = []
    for ch in (text or "").translate(_FIX):
        if "\uff01" <= ch <= "\uff5e" and ch in "｛｝［］：，＂＇－　":
            ch = unicodedata.normalize("NFKC", ch)
        out.append(ch)
    return "".join(out)


def diagnose_watch_config(text: str) -> str:
    """설정을 못 읽을 때 원인 힌트. 종목·가격 같은 값은 절대 넣지 않고 모양만 설명."""
    raw = text or ""
    t = _normalize(raw)
    parts = [f"글자 {len(raw)}자·{raw.count(chr(10)) + 1}줄"]
    if any(c in raw for c in "“”‘’"):
        parts.append("둥근 따옴표 있음(자동 변환함)")
    if "watches" not in t:
        parts.append("'watches' 단어 없음")
    if t.count("{") != t.count("}") or t.count("[") != t.count("]"):
        parts.append(f"괄호 짝 안 맞음 {{ {t.count('{')}/{t.count('}')} }}, [ {t.count('[')}/{t.count(']')} ]")
    try:
        data = yaml.safe_load("\n".join(ln for ln in t.splitlines() if not ln.strip().startswith("```")))
        parts.append(f"읽은 모양: {type(data).__name__}")
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" ({mark.line + 1}줄 {mark.column + 1}번째 글자 근처)" if mark else ""
        parts.append(f"형식 오류: {getattr(e, 'problem', '') or '알 수 없음'}{where}")
    return " · ".join(parts)


def parse_watch_config(text: str) -> list[dict]:
    """알림 설정 글(YAML)을 읽어 종목 목록으로. 붙여넣기 실수(```yaml 표시, watches: 줄 빠짐,
    숫자 쉼표)는 너그럽게 받아 주고, 읽을 수 없으면 경고만 남기고 빈 목록."""
    lines = [ln for ln in _normalize(text).splitlines() if not ln.strip().startswith("```")]
    try:
        data = yaml.safe_load("\n".join(lines))
    except yaml.YAMLError as e:
        log.warning("WATCH_CONFIG 형식 오류 (줄 바꿈·띄어쓰기 확인): %s", str(e).splitlines()[0][:100])
        return []
    if isinstance(data, dict) and "watches" in data:
        items = data.get("watches")
    elif isinstance(data, dict) and "code" in data:
        items = [data]                       # 종목 하나만 적은 경우
    elif isinstance(data, list):
        items = data                         # watches: 줄 없이 목록만 적은 경우
    else:
        if data:
            log.warning("WATCH_CONFIG 형식 오류: 맨 윗줄은 'watches:' 로 시작해야 합니다")
        return []
    out = []
    for w in items or []:
        if not isinstance(w, dict) or not w.get("enabled", True) or not w.get("code"):
            continue
        w = dict(w)
        w["code"] = str(w["code"]).zfill(6)
        if "avg_price" in w:
            w["avg_price"] = _num(w["avg_price"])
        alerts = []
        for a in w.get("alerts") or []:
            if isinstance(a, dict) and a.get("id") and ("above" in a or "below" in a):
                a = dict(a)
                for k in ("above", "below"):
                    if k in a:
                        a[k] = _num(a[k])
                alerts.append(a)
        w["alerts"] = alerts
        out.append(w)
    return out


def load_watches() -> list[dict]:
    """알림 설정. GitHub Secret WATCH_CONFIG(YAML)가 있으면 그것을, 없으면 config/watch.yaml 을 사용.
    개인 보유 종목·평단·알림 가격은 공개 저장소 파일이 아니라 Secret 에 둡니다."""
    import os

    text = os.getenv("WATCH_CONFIG")
    if text and text.strip():
        return parse_watch_config(text)
    if WATCH_FILE.exists():
        return parse_watch_config(WATCH_FILE.read_text(encoding="utf-8"))
    return []


def check(watch: dict, price: float, fired: set[str]) -> list[dict]:
    """아직 보내지 않은 알림 중 조건을 만족한 것 (설정 순서대로)."""
    hits = []
    for a in watch.get("alerts", []):
        if a["id"] in fired:
            continue
        if ("above" in a and price >= a["above"]) or ("below" in a and price <= a["below"]):
            hits.append(a)
    return hits


def format_alert(watch: dict, price: float, hits: list[dict], when: str, market: str = "") -> str:
    avg = watch.get("avg_price")
    ret = f" (평단 대비 {price / avg - 1:+.1%})" if avg else ""
    L = [f"🚨 {watch['name']}({watch['code']}) 가격 알림 {when}{' · ' + market if market else ''}",
         f"현재가 {price:,.0f}원{ret}", ""]
    for a in hits:
        cond = f"≥ {a['above']:,.0f}" if "above" in a else f"≤ {a['below']:,.0f}"
        L.append(f"▶ {a.get('msg', a['id'])}  [{cond}원]")
    L += ["", "※ 자동 주문은 하지 않았습니다. 직접 판단해서 지정가로 주문하세요."]
    return "\n".join(L)


def _load_fired() -> dict:
    if FIRED_FILE.exists():
        return json.loads(FIRED_FILE.read_text(encoding="utf-8"))
    return {}


def run(get_price, send, now: dt.datetime | None = None) -> list[str]:
    """get_price(code) -> (price, market).  반환: 보낸 메시지 목록."""
    now = now or dt.datetime.now()
    fired_all = _load_fired()
    last = json.loads(LAST_FILE.read_text(encoding="utf-8")) if LAST_FILE.exists() else {}
    sent = []
    from .config import load_strategy

    cfg = load_strategy()
    for w in load_watches():
        w = with_auto_alerts(w, cfg)
        code = str(w["code"]).zfill(6)
        if w.get("reset"):
            fired_all.pop(code, None)
        fired = set(fired_all.get(code, []))
        try:
            price, market = get_price(code)
        except Exception as e:
            log.warning("%s 현재가 조회 실패: %s", code, str(e)[:120])
            continue
        log.info("%s %s 현재가 %s (%s)", w["name"], code, f"{price:,.0f}", market)
        last[code] = {"price": round(price), "market": market, "time": now.isoformat(timespec="minutes")}
        hits = check(w, price, fired)
        if not hits:
            continue
        msg = format_alert({**w, "code": code}, price, hits, now.strftime("%m/%d %H:%M"), market)
        if send(msg, silent=False):
            fired_all[code] = sorted(fired | {a["id"] for a in hits})
            sent.append(msg)
    FIRED_FILE.write_text(json.dumps(fired_all, ensure_ascii=False, indent=2), encoding="utf-8")
    LAST_FILE.write_text(json.dumps(last, ensure_ascii=False, indent=2), encoding="utf-8")
    return sent


# ---------------------------------------------------------------- 내 보유 종목: 자동 기준가 + 매일 리포트
def plan_levels(w: dict, cfg: dict) -> dict:
    """평단 기준 추가 매수가(−8%씩)와 분할 매도가(+30/60/100%). 자동매매와 같은 전략 숫자(strategy.yaml) 사용.
    ※ 예측이 아니라 '규칙상 기준가' 입니다."""
    avg = float(w.get("avg_price") or 0)
    if avg <= 0:
        return {"buy": [], "sell": []}
    b, sl = cfg["buy"], cfg["sell"]
    step = b["step_drop_pct"]
    buys = [avg * (1 - step) ** k for k in range(1, max(int(b.get("max_tranches", 5)) - 1, 1) + 1)]
    qty = int(w.get("qty") or 0)
    sells = []
    for lv in sl["take_profit_ladder"]:
        q = max(1, round(qty * lv["sell_pct"])) if qty else None
        sells.append({"price": avg * (1 + lv["gain_pct"]), "gain": lv["gain_pct"], "qty": q})
    return {"buy": buys, "sell": sells}


def with_auto_alerts(w: dict, cfg: dict) -> dict:
    """alerts 를 비워 두면 기준가로 알림을 자동 생성 (추가 매수 2단계 + 분할 매도 3단계)."""
    if w.get("alerts"):
        return w
    p = plan_levels(w, cfg)
    alerts = [{"id": f"buy{i}", "below": round(x), "msg": f"추가 매수 {i}차 기준가 (평단 대비 {x / w['avg_price'] - 1:+.0%})"}
              for i, x in enumerate(p["buy"][:2], 1)]
    for x in p["sell"]:
        q = f" {x['qty']}주" if x["qty"] else " 보유분 1/4"
        alerts.append({"id": f"tp{round(x['gain'] * 100)}", "above": round(x["price"]),
                       "msg": f"분할 매도 기준가 (+{x['gain']:.0%}) —{q} 매도 검토"})
    return {**w, "alerts": alerts}


def holdings_report(watches: list[dict], get_price, cfg: dict, fair_values: dict | None = None,
                    now: dt.datetime | None = None) -> str:
    """매일 장 마감 후 보내는 '내 보유 종목' 리포트: 수익률, 다음 추가 매수가, 분할 매도가, 적정가 대비 위치."""
    now = now or dt.datetime.now()
    fair_values = fair_values or {}
    L = [f"📊 내 보유 종목 리포트 {now:%m/%d}"]
    for w in watches:
        code = str(w["code"]).zfill(6)
        try:
            price, market = get_price(code)
        except Exception as e:
            L += ["", f"{w.get('name', code)}: 현재가 조회 실패 ({str(e)[:40]})"]
            continue
        avg, qty = float(w.get("avg_price") or 0), int(w.get("qty") or 0)
        p = plan_levels(w, cfg)
        L += ["", f"▶ {w.get('name', code)}({code})" + (f" {qty}주" if qty else "") + (f" · 평단 {avg:,.0f}" if avg else "")]
        line = f"현재가 {price:,.0f}원 ({market})"
        if avg:
            line += f" · {price / avg - 1:+.1%}"
            if qty:
                line += f" · 평가손익 {(price - avg) * qty:+,.0f}원"
        L.append(line)
        if p["buy"]:
            nxt = [x for x in p["buy"] if x < price]
            L.append("⬇️ 추가 매수 기준가: " + ", ".join(f"{x:,.0f}({x / avg - 1:+.0%})" for x in p["buy"][:3])
                     + (f" — 다음 기준가까지 {nxt[0] / price - 1:+.1%}" if nxt else ""))
        if p["sell"]:
            L.append("⬆️ 분할 매도 기준가: " + ", ".join(
                f"{x['price']:,.0f}(+{x['gain']:.0%}{', ' + str(x['qty']) + '주' if x['qty'] else ''})" for x in p["sell"]))
        fair = fair_values.get(code)
        if fair:
            L.append(f"📐 분석 적정가 {fair:,.0f}원 · 현재가는 적정가의 {price / fair:.0%}")
        # 오늘의 판단 (규칙 기반)
        if p["sell"] and price >= p["sell"][0]["price"]:
            done = [x for x in p["sell"] if price >= x["price"]]
            view = f"🔴 분할 매도 구간 — +{done[-1]['gain']:.0%} 기준가 도달, 매도 검토"
        elif p["buy"] and price <= p["buy"][0]:
            view = "🔵 추가 매수 구간 — 기준가 아래, 분할 추가 매수 검토"
        elif fair and price >= fair * cfg["sell"]["overvalued_ratio_to_fair"]:
            view = "🟠 적정가 대비 고평가 — 일부 매도 검토"
        elif fair and price <= fair * cfg["buy"]["entry_ratio_to_fair"]:
            view = "🟢 적정가보다 낮음 — 보유 유지, 기준가 오면 추가 매수"
        else:
            view = "⚪ 보유 유지 — 기준가 도달 전까지 대기"
        L.append("판단: " + view)
    L += ["", "※ 예측이 아니라 자동매매와 같은 규칙으로 계산한 기준가입니다. 주문은 하지 않았습니다."]
    return "\n".join(L)

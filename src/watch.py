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


def parse_watch_config(text: str) -> list[dict]:
    """알림 설정 글(YAML)을 읽어 종목 목록으로. 붙여넣기 실수(```yaml 표시, watches: 줄 빠짐,
    숫자 쉼표)는 너그럽게 받아 주고, 읽을 수 없으면 경고만 남기고 빈 목록."""
    lines = [ln for ln in (text or "").splitlines() if not ln.strip().startswith("```")]
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
    for w in load_watches():
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

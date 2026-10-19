"""텔레그램 알림 — 매수·매도 내역(종목·수량·금액·사유)과 확인이 필요한 알림을 보낸다.

설정 (.env 또는 GitHub Secrets):
  TELEGRAM_BOT_TOKEN : @BotFather 에게 받은 봇 토큰
  TELEGRAM_CHAT_ID   : 내 채팅 ID (README '텔레그램 연결' 참고)

토큰이 없으면 아무것도 보내지 않고 조용히 넘어갑니다.
보내기에 실패해도 매매 프로그램은 멈추지 않습니다 (로그만 남김).
"""
from __future__ import annotations

import logging

from .config import env

log = logging.getLogger(__name__)
MAX_LEN = 3800  # 텔레그램 한 메시지 최대 4096자 — 여유 있게 나눔


def format_daily(exe: dict, analysis: dict | None = None, new_candidates: list | None = None,
                 candidates_title: str = "🟢 승인 대기 매수 후보") -> str:
    names = {s["code"]: s["name"] for s in (analysis or {}).get("stocks", [])}
    nm = lambda code, o=None: (o or {}).get("name") or names.get(code, code)
    ret = exe["equity"] / exe["initial_cash"] - 1 if exe.get("initial_cash") else 0
    head = "🧪 [미리보기 — 실제 주문 아님]\n" if exe.get("dry_run") else ""
    L = [f"{head}📈 DCA 자동매매 {exe['date']} ({exe['broker']})",
         f"평가금액 {exe['equity']:,.0f}원 ({ret:+.2%}) · 현금 {exe['cash']:,.0f}원"]

    trades = exe.get("executed", []) + exe.get("skipped", [])
    if trades:
        L.append("")
        for o in trades:
            side = "🔵 매수" if o["side"] == "buy" else "🔴 매도"
            px = o.get("fill_price", o["price"])
            amount = px * o["qty"]
            status = "" if o in exe.get("executed", []) and o.get("status") in ("가상 체결", "주문 접수") else f" [{o.get('status', '')}]"
            L.append(f"{side} {nm(o['code'], o)}({o['code']}){status}")
            L.append(f"  {o['qty']:,}주 × {px:,.0f}원 = {amount:,.0f}원")
            L.append(f"  사유: {o['reason']}")
            if o.get("basis"):
                L.append(f"  근거: {o['basis']}")
            if o.get("realized_pnl") is not None:
                L.append(f"  실현 손익 {o['realized_pnl']:+,.0f}원")
    else:
        L.append("오늘 주문 없음")

    positions = exe.get("positions") or []
    if positions:
        L += ["", "💼 보유 종목"]
        for p in positions:
            L.append(f"- {p['name']} {p['qty']:,}주 · 평단 {p['avg_price']:,.0f} → {p['price']:,.0f} "
                     f"({p['ret']:+.1%}, {p['pnl']:+,.0f}원)")
        L.append(f"평가 손익 합계 {exe.get('unrealized', 0):+,.0f}원")
    if "realized_total" in exe:
        L.append(f"💰 실현 손익: 오늘 {exe.get('realized_today', 0):+,.0f}원 · 누적 {exe['realized_total']:+,.0f}원")

    if exe.get("alerts"):
        L += ["", "⚠️ 확인 필요"] + [f"- {a}" for a in exe["alerts"]]
    if new_candidates:
        L += ["", candidates_title]
        L += [f"- {s['name']}({s['code']}) 점수 {s['total']} · 현재가 {s['price']:,.0f} / 적정가 {s['fair_value']:,}" for s in new_candidates]
    return "\n".join(L)


def _chunks(text: str) -> list[str]:
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > MAX_LEN and cur:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        out.append(cur)
    return out


def send(text: str, sender=None, silent: bool | None = None) -> bool:
    """sender 는 테스트용 주입. 기본은 텔레그램 Bot API.
    silent=True 면 소리 없이, 기본(None/False)은 휴대폰 알림음과 함께 전송."""
    token, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if sender is None:
        if not token or not chat:
            log.info("텔레그램 설정 없음 → 알림 생략")
            return False
        import requests

        def sender(msg, silent=False):
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat, "text": msg, "disable_web_page_preview": True,
                                    "disable_notification": bool(silent)}, timeout=15)
            if not r.ok:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    ok = True
    for part in _chunks(text):
        try:
            if silent is None:
                sender(part)
            else:
                sender(part, silent=silent)
        except Exception as e:  # 토큰이 로그에 찍히지 않도록 메시지만
            log.warning("텔레그램 전송 실패: %s", str(e).replace(token or "@@", "***"))
            ok = False
    return ok

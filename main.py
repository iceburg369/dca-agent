"""장기 DCA 자동매매 — 명령어 모음

  python main.py analyze             # 분석 agent: 거시→테마→이슈→적정가 보고서
  python main.py approve 005930      # 매수 후보를 직접 승인
  python main.py unapprove 005930    # 승인 취소
  python main.py execute [--dry-run] # 실행 agent: 승인 종목 DCA 매수 / 분할 매도
  python main.py daily               # 통합: (주 1회) 분석 → 실행 → 일일 요약
  python main.py backtest            # 과거 데이터로 규칙 검증
  python main.py status              # 현재 가상 계좌 상태
  python main.py notify-test         # 텔레그램 연결 확인 메시지 보내기
  python main.py check               # AI(Claude/Nova)·DART 연결 점검
  python main.py watch               # 보유 종목 가격 알림 확인 (config/watch.yaml)
"""
from __future__ import annotations

import argparse
import logging
import sys

from src.config import load_approved, load_strategy, save_approved, universe


def cmd_analyze(a):
    from src.orchestrator import run_analysis

    r = run_analysis(use_llm=not a.no_llm, codes=a.codes or None)
    print(f"\n거시: {r['macro']['score']}점 ({r['macro']['regime']}) / AI: {r.get('llm_label', '-')} {r['llm_calls']}회")
    print(f"{'종목':<14}{'현재가':>10}{'적정가':>10}{'점수':>7}  판정")
    for s in r["stocks"]:
        if s.get("error"):
            print(f"{s['name']:<14}  {s['error']}")
            continue
        fv = f"{s['fair_value']:,}" if s["fair_value"] else "-"
        print(f"{s['name']:<14}{s['price']:>10,.0f}{fv:>10}{s['total']:>7}  {s['verdict']}")
    print(f"\n보고서: {r['report_path']}")


def cmd_approve(a):
    items = load_approved()
    names = {u["code"]: u["name"] for u in universe()}
    for code in a.codes:
        code = code.zfill(6)
        if any(str(i["code"]).zfill(6) == code for i in items):
            print(f"{code} 는 이미 승인됨")
            continue
        if code not in names:
            print(f"⚠️ {code} 는 themes.yaml 후보에 없습니다. 먼저 테마 후보로 추가해야 적정가가 계산됩니다.")
        items.append({"code": code, "name": names.get(code, code), "note": a.note or "", "fair_value_override": a.fair})
        print(f"✅ 승인: {names.get(code, code)}({code})")
    save_approved(items)


def cmd_unapprove(a):
    codes = {c.zfill(6) for c in a.codes}
    items = [i for i in load_approved() if str(i["code"]).zfill(6) not in codes]
    save_approved(items)
    print("승인 해제:", ", ".join(codes), "(보유 중이면 매도 규칙은 계속 적용됩니다)")


def cmd_execute(a):
    from src.analysis.agent import load_latest
    from src.execution.agent import ExecutionAgent
    from src.orchestrator import build_quote_provider

    analysis = load_latest()
    if not analysis:
        sys.exit("먼저 `python main.py analyze` 를 실행하세요.")
    cfg = load_strategy()
    r = ExecutionAgent(quote_provider=build_quote_provider(cfg), cfg=cfg).run(analysis, dry_run=a.dry_run)
    _print_exec(r)
    from src.notify import format_daily, send

    send(format_daily(r, analysis))


def _print_exec(r):
    print(f"\n[{r['broker']}] 총 평가 {r['equity']:,.0f}원 / 현금 {r['cash']:,.0f}원")
    for o in r["executed"] + r["skipped"]:
        print(f"  {'매수' if o['side'] == 'buy' else '매도'} {o['code']} {o['qty']}주 @ {o['price']:,.0f} — {o['reason']} [{o['status']}]")
    for al in r["alerts"]:
        print("  " + al)
    print(f"보고서: {r['report_path']}")


def cmd_daily(a):
    from src.orchestrator import run_daily

    if a.guard:
        from src.orchestrator import guarded_daily

        r = guarded_daily(force_analysis=a.force_analysis, use_llm=not a.no_llm, dry_run=a.dry_run)
        if r.get("skipped"):
            print(f"예약 실행 건너뜀: {r['skipped']}")
            return
    else:
        r = run_daily(force_analysis=a.force_analysis, use_llm=not a.no_llm, dry_run=a.dry_run)
    if r.get("market_closed"):
        print("오늘은 휴장일 — 매매하지 않음")
        return
    _print_exec(r["execution"])
    for s in r["new_candidates"]:
        print(f"  🟢 승인 대기: {s['name']}({s['code']}) 점수 {s['total']}")
    print(f"일일 요약: {r['summary_path']}")


def cmd_backtest(a):
    from src.backtest import run_backtest, save_report
    from src.data.market_data import price_history

    cfg = load_strategy()
    names = {u["code"]: u["name"] for u in universe()}
    codes = [c.zfill(6) for c in (a.codes or [str(i["code"]) for i in load_approved()])]
    if not codes:
        sys.exit("백테스트할 종목을 지정하세요: python main.py backtest 005930 000660 ...")
    closes = {c: price_history(c, a.start_data)["Close"] for c in codes}
    res = run_backtest(cfg, closes, names, start=a.start, fair_mult=a.fair_mult)
    path = save_report(res, names)
    print(f"\n기간 {res['period']}")
    for k, label in (("strategy", "이 전략"), ("lump_sum", "일시매수"), ("monthly_dca", "정기적립")):
        s = res[k]
        print(f"  {label:<6} 수익률 {s['return']:+.1%}  연 {s['cagr']:+.1%}  최대낙폭 {s['mdd']:.1%}")
    print(f"보고서: {path}")


def cmd_status(a):
    from src.execution.broker import make_broker

    b = make_broker(load_strategy())
    print(f"[{b.name}] 현금 {b.cash():,.0f}원")
    for c, h in b.holdings().items():
        print(f"  {c} {h['qty']}주 평단 {h['avg_price']:,.0f}")


def cmd_watch(a):
    """config/watch.yaml 의 가격 알림 확인 (주문 없음, 텔레그램 알림만)."""
    from src import watch
    from src.notify import send

    kis = None
    try:
        from src.data.kis_client import KISClient

        kis = KISClient("demo")
    except Exception as e:
        print(f"한국투자 연결 안 됨 → 최근 종가로 확인 ({str(e)[:80]})")

    state = {"kis": kis, "retried": False}

    def get_price(code):
        import time

        if state["kis"] is not None:
            try:
                return state["kis"].live_price(code)
            except Exception as e:
                print(f"한국투자 시세 실패: {str(e)[:80]}")
            if not state["retried"]:  # 접속 토큰 발급은 1분에 1회 제한 → 61초 쉬고 다시 연결해 한 번 더
                state["retried"] = True
                time.sleep(61)
                try:
                    from src.data.kis_client import KISClient

                    state["kis"] = KISClient("demo")
                    return state["kis"].live_price(code)
                except Exception as e:
                    print(f"한국투자 재시도 실패: {str(e)[:80]}")
        from src.data.market_data import price_history  # 마지막 수단: 공개 시세(지연)

        return float(price_history(code)["Close"].iloc[-1]), "공개 시세(지연 가능)"

    if a.test:  # 실제 기록은 건드리지 않고, 현재가로 예시 알림 1건 전송 (알림음 확인용)
        for w in watch.load_watches():
            code = str(w["code"]).zfill(6)
            price, market = get_price(code)
            sample = [{"id": "test", "above": 0, "msg": "🔔 알림음 테스트입니다 (실제 매도 신호 아님)"}]
            ok = send(watch.format_alert({**w, "code": code}, price, sample, "테스트", market), silent=False)
            print(("전송 성공: " if ok else "전송 실패: ") + f"{w['name']} 현재가 {price:,.0f} ({market})")
        return
    sent = watch.run(get_price, send)
    print(f"알림 {len(sent)}건 전송" if sent else "조건에 맞는 알림 없음")
    for m in sent:
        print(m)


def cmd_check(a):
    """연결 점검: AI(Claude/Nova) 1회 호출 + DART 공시 1회 조회. 결과만 출력 (키 값은 출력 안 함)."""
    from src.analysis.llm import LLM, redact
    from src.data import news

    import os
    import re

    def key_shape(name):  # 키 값은 절대 출력하지 않고 '형태'만 알려줌
        raw = os.getenv(name)
        if not raw:
            return None
        k = raw.strip()
        kinds = [
            (k.startswith(("ABSK", "bedrock-api-key-")), "AWS Bedrock API 키 형태"),
            (bool(re.fullmatch(r"(AKIA|ASIA)[A-Z0-9]{16}", k)), "AWS IAM 액세스 키 ID 형태 (비밀 키와 짝으로 써야 함)"),
            (bool(re.fullmatch(r"[A-Za-z0-9/+]{40}", k)), "AWS IAM 비밀 액세스 키 형태로 보임"),
            (k.startswith("sk-ant-"), "Anthropic(Claude) 키 형태"),
            (k.startswith("sk-"), "OpenAI 형식 키"),
        ]
        kind = next((label for ok, label in kinds if ok), "알 수 없는 형식")
        ws = " · 앞뒤 공백/줄바꿈 있었음(자동 제거)" if raw != k else ""
        inner = " · 중간에 공백 포함!" if re.search(r"\s", k) else ""
        return f"{kind}, 길이 {len(k)}자{ws}{inner}"

    for n in ("AWS_BEARER_TOKEN_BEDROCK", "NOVA_API_KEY", "ANTHROPIC_API_KEY"):
        s = key_shape(n)
        if s:
            print(f"AI: {n} → {s}")

    cfg = load_strategy()
    llm = LLM(cfg["analysis"]["llm_model"])
    if not llm.available:
        print("AI: ❌ 사용 가능한 키 없음 (규칙 기반으로 동작)")
    else:
        r = llm.ask_json('연결 확인입니다. JSON 으로만 답하세요: {"ok": true, "model": "<당신의 모델 이름>"}', max_tokens=100)
        print(f"AI: {'✅ 응답 성공' if r else '❌ 응답 실패'} — {llm.label} → {r}")
        if llm.provider == "nova_api":
            from urllib.parse import urlsplit

            u = urlsplit(llm._nova_url)
            print(f"AI: 접속 주소 → {redact(u.scheme + '://' + u.netloc)}{u.path} · 인증 헤더 {llm._auth_header}")
        if not r and llm.provider == "nova_api":
            import requests
            from src.analysis.llm import SYSTEM_BASE, nova_auth_headers

            body = {"model": llm.model, "max_tokens": 50,
                    "messages": [{"role": "system", "content": SYSTEM_BASE}, {"role": "user", "content": '{"ok": true} 만 출력'}]}
            for h in ("Authorization", "x-api-key", "api-key"):  # 인증 방식 자동 시험
                try:
                    rr = requests.post(llm._nova_url, headers=nova_auth_headers(h, llm._nova_key), json=body, timeout=30)
                    print(f"AI: 인증 방식 '{h}' 시험 → HTTP {rr.status_code} {rr.text[:150].replace(llm._nova_key, '***')}")
                    if rr.ok:
                        print(f"AI: ✅ '{h}' 방식이 됩니다 → GitHub Variables 에 NOVA_AUTH_HEADER={h}")
                        break
                except Exception as e:
                    print(f"AI: 인증 방식 '{h}' 시험 실패 {redact(str(e))[:100]}")
                    from src.analysis.llm import _is_connection_error

                    if _is_connection_error(e):
                        print("AI: ❌ 주소에 접속 자체가 안 됩니다 (사내망 전용 주소일 수 있음)")
                        break
            try:  # 모델 이름이 틀렸을 수 있으니 사용 가능한 모델 목록 표시
                import requests

                m = requests.get(llm._nova_url.replace("/chat/completions", "/models"),
                                 headers={"Authorization": f"Bearer {llm._nova_key}"}, timeout=20)
                ids = [x.get("id") for x in (m.json().get("data") or [])] if m.ok else []
                print(f"AI: 사용 가능한 모델 목록 → {ids or ('HTTP ' + str(m.status_code))}")
            except Exception as e:
                print(f"AI: 모델 목록 조회 실패 {redact(str(e))[:80]}")
    if not news.dart_available():
        print("DART: ❌ DART_API_KEY 없음 또는 연결 실패")
    else:
        d = news.disclosures("005930", days=30)
        print(f"DART: {'✅ 공시 조회 성공' if d else '⚠️ 연결됐지만 결과 없음'} — 삼성전자 최근 30일 공시 {len(d)}건"
              + (f", 예: {d[0]['date']} {d[0]['title']}" if d else ""))


def cmd_notify_test(a):
    from src.notify import send

    ok = send("✅ DCA 자동매매 텔레그램 연결 성공")
    print("텔레그램 전송 성공" if ok else "텔레그램 전송 실패 — TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 확인")
    if not ok:
        sys.exit(1)


def main():
    p = argparse.ArgumentParser(description="장기 성장·가치주 DCA 자동매매 (분석 agent + 실행 agent)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("analyze"); s.add_argument("codes", nargs="*"); s.add_argument("--no-llm", action="store_true"); s.set_defaults(f=cmd_analyze)
    s = sub.add_parser("approve"); s.add_argument("codes", nargs="+"); s.add_argument("--note"); s.add_argument("--fair", type=float, help="적정가 직접 지정"); s.set_defaults(f=cmd_approve)
    s = sub.add_parser("unapprove"); s.add_argument("codes", nargs="+"); s.set_defaults(f=cmd_unapprove)
    s = sub.add_parser("execute"); s.add_argument("--dry-run", action="store_true"); s.set_defaults(f=cmd_execute)
    s = sub.add_parser("daily"); s.add_argument("--force-analysis", action="store_true"); s.add_argument("--guard", action="store_true", help="예약 실행용: 장중 1회만"); s.add_argument("--no-llm", action="store_true"); s.add_argument("--dry-run", action="store_true"); s.set_defaults(f=cmd_daily)
    s = sub.add_parser("backtest"); s.add_argument("codes", nargs="*"); s.add_argument("--start", help="성과 측정 시작일 YYYY-MM-DD")
    s.add_argument("--start-data", default="2016-01-01", help="데이터 시작일 (적정가 계산용 3년 포함)"); s.add_argument("--fair-mult", type=float, default=1.0); s.set_defaults(f=cmd_backtest)
    s = sub.add_parser("status"); s.set_defaults(f=cmd_status)
    s = sub.add_parser("notify-test"); s.set_defaults(f=cmd_notify_test)
    s = sub.add_parser("check"); s.set_defaults(f=cmd_check)
    s = sub.add_parser("watch"); s.add_argument("--test", action="store_true", help="알림음 확인용 예시 알림 보내기"); s.set_defaults(f=cmd_watch)

    a = p.parse_args()
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    a.f(a)


if __name__ == "__main__":
    main()

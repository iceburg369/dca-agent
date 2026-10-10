# CLAUDE.md — 이 프로젝트에서 Claude Code 가 지킬 규칙

## 프로젝트
국내 성장·가치주 장기 DCA 자동매매. 분석 agent + 실행 agent + 통합 오케스트레이터.
전략과 구조는 **DESIGN.md** 가 기준입니다. 작업 전 반드시 읽으세요. 테마 근거는 THEMES_2026Q4.md.

## 사용자
- 프로그래밍 비전공자. 설명은 한국어로, 쉬운 말로. 코드를 바꾸면 '무엇이 왜 바뀌는지' 한두 줄로 알려줄 것
- 단기 매매 기능(분봉, 스캘핑, 기술적 지표 단타)은 요청이 없으면 추가하지 않음

## 절대 규칙 (안전)
1. **실전 주문 코드를 실행하지 않는다.** `BROKER=kis_real` 이나 `LIVE_TRADING=yes` 로 바꾸는 것은 사용자가 직접 한다
2. API 키·계좌번호를 코드, 로그, 커밋, 채팅에 쓰지 않는다. `.env`, `~/KIS/config/kis_devlp.yaml` 만 사용
3. 증권사 통신은 `src/data/kis_client.py` 를 통해 **한국투자증권 공식 샘플 코드**만 호출한다. 직접 HTTP 주문 코드를 새로 만들지 않는다
4. 새 라이브러리는 PyPI 의 널리 쓰이는 패키지만. 추가 전에 사용자에게 이유를 말하고 requirements.txt 에 기록
5. 매수·매도 규칙은 `src/execution/rules.py` 한 곳에만. 실행 agent 와 백테스트가 같은 함수를 쓰게 유지
6. 분석 agent 는 주문하지 않고, 실행 agent 는 종목을 고르지 않는다 (역할 분리 유지)

## 작업 방식
- 전략 숫자 변경 → `config/strategy.yaml` 수정 → `python -m pytest -q` → `python main.py backtest` 결과를 이전과 비교해 보고
- 기능 추가 → 테스트를 `tests/test_all.py` 에 먼저 추가 (인터넷 없이 돌아가게 가짜 데이터 사용)
- 한 번에 크게 바꾸지 말고 단계별로. 각 단계 끝에 테스트

## 자주 쓰는 명령
```
python -m pytest -q                 # 규칙 테스트
python main.py analyze              # 분석 (Claude API 사용, --no-llm 으로 끄기)
python main.py approve 005930       # 승인
python main.py execute --dry-run    # 주문 없이 확인
python main.py daily                # 통합 실행
python main.py backtest 005930 000660
python main.py notify-test          # 텔레그램 연결 확인
```

## GitHub 자동 실행
- `.github/workflows/runner.yml`('자동 실행기') — 최대 330분 돌고 끝나기 전에 `gh workflow run runner.yml` 로 스스로 다음 실행을 시작(이어달리기). 예비 cron 3시간마다. 평일 10:00~15:15 에 오늘 매매 기록이 없으면 `dca.yml -f mode=scheduled` 시작, 08~20시 2분마다 `main.py watch`. `vars.RUNNER_ENABLED=false` 로 정지
- `.github/workflows/dca.yml` — 예약 없음. scheduled 모드는 `daily --guard`(장중 첫 실행 1회만 매매). 장부는 `trading-data` 브랜치에 저장 (main 에 state/·reports/ 커밋 금지)
- 저장소는 공개. 개인 보유 정보(종목·평단·알림가)는 Secret `WATCH_CONFIG` 에만 두고, 저장소 파일·테스트·로그·커밋에 쓰지 않는다. 알림 기록은 `watch-state` 브랜치에 openssl 로 암호화(STATE_KEY)
- Vercel 은 사용하지 않는다
- 워크플로에서 `kis_real` 은 막혀 있다. 이 차단을 풀지 않는다
- 키는 GitHub Secrets 로만. 워크플로 로그에 키가 찍히는 코드를 넣지 않는다

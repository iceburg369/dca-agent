# 장기 DCA 자동매매 (분석 agent + 실행 agent)

국내 성장·가치주를 **거시 → 테마 → 이슈 → 적정가** 순서로 분석하고, 사람이 승인한 종목만
**떨어질 때 나눠 사고(DCA), 오르면 나눠 파는** 프로그램입니다. Claude Code 로 계속 고쳐 나가도록 만들었습니다.

```
분석 agent ──(analysis_latest.json)──▶ 사람 승인(approved.yaml) ──▶ 실행 agent ──▶ 가상 장부 / 한국투자 모의투자
```

## 기반으로 쓴 검증된 오픈소스

| 용도 | 라이브러리 | 비고 |
|---|---|---|
| 주문·잔고·시세·재무·휴장일 | [한국투자증권 공식 open-trading-api](https://github.com/koreainvestment/open-trading-api) | 증권사가 직접 관리. 주문 코드를 새로 짜지 않고 공식 함수만 호출: `inquire_price`(현재가·PER·EPS·BPS), `finance_financial_ratio`(이익 증가율), `chk_holiday`(휴장일), `inquire_balance`(잔고), `order_cash`(주문) |
| 과거 주가·지수·환율·미국 금리 | [FinanceDataReader](https://github.com/FinanceData/FinanceDataReader) | 무료, 로그인 불필요 |
| 공시 | [OpenDartReader](https://github.com/FinanceData/OpenDartReader) | 금감원 DART, 무료 키 |
| 뉴스·공시 해석 | [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python) | Claude API |
| 뉴스·공시 해석 (대안) | [boto3 (AWS 공식 SDK)](https://github.com/boto/boto3) | Amazon Nova 2 Lite — Bedrock Converse API |

## 설치 (처음 한 번)

```bash
# 1) 이 폴더에서 가상환경 + 패키지
python -m venv .venv
source .venv/bin/activate          # 윈도우: .venv\Scripts\activate
pip install -r requirements.txt

# 2) 키 설정 파일
cp .env.example .env               # 윈도우: copy .env.example .env
#    .env 를 열어 ANTHROPIC_API_KEY, DART_API_KEY 입력 (없어도 규칙 기반으로 동작)

# 3) 테스트 (인터넷 없이 규칙 검증)
python -m pytest -q
```

## 사용 순서

```bash
python main.py analyze                   # ① 분석 보고서 → reports/analysis_날짜.md
python main.py backtest 005930 000660    # ② 관심 종목으로 과거 검증
python main.py approve 005930            # ③ 직접 검토 후 승인
python main.py execute --dry-run         # ④ 주문 미리보기
python main.py daily                     # ⑤ 매일 1번: (주1회 분석) + 실행 + 요약
python main.py status                    # 가상 계좌 확인
python main.py notify-test               # 텔레그램 연결 확인
```

처음엔 `BROKER=paper` (내 PC 가상 장부, 시드 1,000만원)로 시작합니다. 장부 초기화는 `state/` 폴더를 지우면 됩니다.

## 한국투자증권 모의투자 연결 (2단계)

1. 한국투자증권 계좌 개설 → 홈페이지 [트레이딩]-[모의투자]에서 모의투자 신청
2. [KIS Developers](https://apiportal.koreainvestment.com) 에서 Open API 신청 → 모의투자 앱키·시크릿 발급
3. 공식 코드 받기: `git clone https://github.com/koreainvestment/open-trading-api`
4. 공식 README 대로 `~/KIS/config/kis_devlp.yaml` 에 앱키·모의계좌번호 입력
5. `.env` 에 `KIS_OPEN_API_PATH=<받은 폴더>/examples_user`, `BROKER=kis_demo`
6. `python main.py execute --dry-run` → 이상 없으면 장 시간에 `python main.py daily`

KIS 를 연결하면 현재가 API 의 EPS·BPS 로 적정가를 계산합니다. 연결 전에는 `data/fundamentals.csv`
(`code,eps,bps,eps_growth`) 에 직접 숫자를 넣으면 적정가가 계산됩니다. 둘 다 없으면 3년 주가 중앙값을 임시 기준으로 씁니다(신뢰도 low → 매수 후보로 판정하지 않음).

## Claude Code 로 쓰기

이 폴더에서 `claude` 를 실행하면 CLAUDE.md·DESIGN.md 를 읽고 시작합니다.

- `/daily` — 하루 루틴 (분석 요약 → 승인 확인 → 실행)
- `/tune 분할매도를 +30/60/100% 로` — 규칙 하나 바꾸고 백테스트 전후 비교
- "analyst 에이전트로 이번 주 테마 정리해줘", "executor 에이전트로 수익률 점검해줘"

## GitHub 에서 자동 실행 (PC 를 켜 둘 필요 없음)

GitHub 무료 예약 실행은 몇 시간씩 늦게 시작하는 일이 잦습니다(10/8: 09:37 예약 → 18:25 실행).
그래서 예약에 기대지 않고 **`자동 실행기`(`.github/workflows/runner.yml`)가 계속 이어 달리며** 정해진 시각에 일을 시킵니다.

| 시간 (평일, 한국시간) | 하는 일 |
|---|---|
| 08:00~20:00 | 내 보유 종목 가격을 2분마다 확인 → 알림 조건이면 텔레그램(알림음) |
| 10:00 이후 장중 | `DCA 자동매매` 를 하루 한 번 시작 (이미 했거나 휴장일이면 건너뜀) |

- 한 번 실행은 최대 5시간 30분이고, 끝나기 직전에 **스스로 다음 실행을 시작**합니다. 끊겨도 3시간마다 예비 예약이 다시 살립니다.
- **저장소가 공개(Public)일 때만 돌아갑니다.** 공개 저장소는 사용 시간 제한 없이 무료이고, 비공개면 월 2,000분을 금방 다 쓰기 때문에 자동으로 쉬게 해 두었습니다.
- 멈추기: Settings → Secrets and variables → Actions → **Variables** 탭 → `RUNNER_ENABLED` = `false`. 다시 켜려면 `true` 로 바꾸고 Actions → `자동 실행기` → **Run workflow**.
- 처음 한 번은 Actions → `자동 실행기` → **Run workflow** 로 직접 시작해 주세요.

`DCA 자동매매` 자체:
(분석은 주 1회, 실행은 매일 → 결과를 텔레그램으로 전송)

- 가상 계좌 장부와 보고서는 `trading-data` 브랜치에 날마다 저장됩니다 (`main` 에는 코드만)
- 저장소 **Actions** 탭 → `DCA 자동매매` → 각 실행을 누르면 그날 요약과 분석 보고서가 보입니다
- 수동 실행: Actions → `DCA 자동매매` → **Run workflow** → 방식 선택
  (`daily` 평소 / `dry-run` 주문 없이 미리보기 / `force-analysis` 분석 새로 / `notify-test` 텔레그램 확인 / `check` AI·DART 연결 점검)
- 안전장치: GitHub 자동 실행에서는 실전 주문(`kis_real`)이 막혀 있습니다

### 키 등록 (Settings → Secrets and variables → Actions → New repository secret)

| 이름 | 필수 | 내용 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | 알림 받으려면 | 텔레그램 봇 토큰 |
| `TELEGRAM_CHAT_ID` | 알림 받으려면 | 내 채팅 ID |
| `ANTHROPIC_API_KEY` | 선택 | 없으면 뉴스 해석 없이 규칙 기반 분석 |
| `AWS_BEARER_TOKEN_BEDROCK` (또는 `AWS_ACCESS_KEY_ID`+`AWS_SECRET_ACCESS_KEY`) | 선택 | Claude 대신 Amazon Nova 2 Lite 사용. 리전은 Variables 의 `AWS_REGION` (기본 us-east-1) |
| `DART_API_KEY` | 선택 | 없으면 공시 점검 생략 |
| `KIS_PAPER_APP`, `KIS_PAPER_SEC`, `KIS_PAPER_ACCOUNT`, `KIS_HTS_ID` | 모의투자 연결 시 | 한국투자 모의투자 앱키·시크릿·계좌 앞 8자리·HTS ID |

**KIS 4개 Secret 은 사실상 필수입니다.** 넣기만 해도 가상 장부(paper)는 그대로 쓰면서, 적정가 계산에 필요한
PER·EPS·BPS 를 한국투자 API 에서 가져옵니다. 없으면 적정가가 '3년 주가 중앙값'으로만 계산돼 모든 종목이 '관찰'로 나옵니다.
주문까지 한국투자 모의투자 계좌로 보내려면 같은 화면 **Variables** 탭에 `BROKER` = `kis_demo` 를 추가하세요.
(한국투자 API 가 해외 서버(GitHub)에서의 접속을 허용하는지는 첫 실행 로그로 확인이 필요합니다)

## 내 보유 종목 가격 알림 (공개 저장소에서도 비공개로)

내 보유 종목·평단·알림 가격은 **파일에 쓰지 않고 Secret `WATCH_CONFIG`** 에 넣습니다. (공개 저장소라도 Secret 은 나만 볼 수 있습니다)
Settings → Secrets and variables → Actions → New repository secret → 이름 `WATCH_CONFIG`, 값 예시:

```yaml
watches:
  - code: "000000"        # 종목코드 6자리
    name: 종목이름
    avg_price: 100000     # 내 평단
    alerts:
      - {id: tp1, above: 110000, msg: "1차 매도 구간"}
      - {id: sl,  below:  95000, msg: "손절선"}
```

- 알림은 `id` 마다 한 번만 옵니다. 실행 기록(로그)에는 종목·가격이 `***` 로 가려지고 횟수만 남습니다.
- 어떤 알림을 이미 보냈는지는 `watch-state` 브랜치에 **암호화**해서 저장합니다. 열쇠는 Secret `STATE_KEY`(선택, 없으면 텔레그램 토큰을 열쇠로 씀).
- 알림 가격을 바꾸려면 Secret 을 수정하고, 다시 울리게 하려면 `id` 를 새 이름으로 바꾸세요.

## 텔레그램 연결

1. 텔레그램에서 **@BotFather** 검색 → `/newbot` → 봇 이름 입력 → **토큰**(`123456:ABC...`) 받기
2. 만든 봇과의 대화창을 열고 아무 메시지나 보내기 (예: `시작`)
3. 브라우저에서 `https://api.telegram.org/bot<토큰>/getUpdates` 접속 → `"chat":{"id":숫자` 의 **숫자**가 채팅 ID
4. GitHub Secrets 에 `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` 등록 (PC 에서 쓸 땐 `.env` 에)
5. Actions → Run workflow → `notify-test` 로 연결 확인

알림 예시:
```
📈 DCA 자동매매 2026-10-06 (paper)
평가금액 10,050,000원 (+0.50%) · 현금 9,000,000원

🔵 매수 삼성전자(005930)
  5주 × 70,070원 = 350,350원
  사유: 첫 매수: 현재가 70,000 ≤ 적정가 108,000×0.95
🔴 매도 SK하이닉스(000660)
  2주 × 299,700원 = 599,400원
  사유: 분할매도: 수익 +31.0% ≥ 30%

🟢 승인 대기 매수 후보
- LS ELECTRIC(010120) 점수 66 · 현재가 200,000 / 적정가 230,000
```

### 승인은 어떻게? (GitHub 에서)
저장소에서 `config/approved.yaml` 을 열고 연필(✏️) 버튼 → 종목 추가 → **Commit changes**. 다음 실행부터 반영됩니다.
```yaml
approved:
  - code: "005930"
    name: 삼성전자
    note: 직접 검토 완료
```

## 내 PC 에서 매일 자동 실행 (GitHub 대신 쓰고 싶을 때)

맥/리눅스 `crontab -e`:
```
50 14 * * 1-5 cd /경로/dca-agent && .venv/bin/python main.py daily >> reports/cron.log 2>&1
```
윈도우는 '작업 스케줄러'에 같은 명령을 등록하세요.

## 폴더

```
config/strategy.yaml   매수·매도 숫자 (여기만 바꿔도 전략이 바뀜)
config/themes.yaml     테마·후보 종목·목표 PER/PBR
config/approved.yaml   승인 종목
src/analysis/          분석 agent (scoring.py = 점수 규칙, llm.py = Claude)
src/execution/         실행 agent (rules.py = DCA 규칙, broker.py = 가상/KIS)
src/orchestrator.py    통합
src/backtest.py        백테스트
state/ reports/        장부·보고서 (자동 생성, git 제외)
```

## 주의

투자 판단과 손익 책임은 사용자에게 있습니다. 이 프로그램과 보고서는 투자 권유가 아닙니다.
실전 전환은 모의투자로 몇 달 검증한 뒤 소액으로만 하세요.

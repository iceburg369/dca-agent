---
description: 전략 숫자 하나를 바꿔 보고 백테스트로 전후 비교
argument-hint: 바꾸고 싶은 내용 (예: 분할매도를 +30/60/100% 로)
---

사용자 요청: $ARGUMENTS

1. 현재 config/strategy.yaml 로 `python main.py backtest` 를 돌려 기준 결과를 기록한다 (승인 종목이 없으면 사용자에게 종목을 묻는다)
2. 요청대로 strategy.yaml 을 수정한다 (한 번에 한 가지만)
3. `python -m pytest -q` 통과 확인
4. 같은 종목·기간으로 다시 백테스트
5. 수익률·연환산·최대낙폭·매매횟수·현금비중을 전/후 표로 비교하고, 유지할지 되돌릴지 사용자에게 묻는다
6. 유지하면 DESIGN.md 의 해당 규칙 설명도 고친다

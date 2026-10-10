"""한국투자증권 공식 샘플 코드(open-trading-api) 얇은 래퍼.

이 프로젝트는 증권사 통신 코드를 새로 짜지 않고, 한국투자증권이 GitHub 에 공개한
공식 코드(examples_user/kis_auth.py, domestic_stock_functions.py)를 그대로 불러 씁니다.

준비:
  1) git clone https://github.com/koreainvestment/open-trading-api
  2) .env 에 KIS_OPEN_API_PATH=<위 폴더>/examples_user
  3) ~/KIS/config/kis_devlp.yaml 에 앱키·계좌 입력 (공식 README 방법 그대로)
"""
from __future__ import annotations

import sys
import threading
from functools import lru_cache

from ..config import env

_lock = threading.Lock()


class KISUnavailable(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _modules():
    path = env("KIS_OPEN_API_PATH")
    if not path:
        raise KISUnavailable("KIS_OPEN_API_PATH 가 .env 에 없습니다 (README 의 '한국투자증권 연결' 참고).")
    for p in (path, f"{path}/domestic_stock"):
        if p not in sys.path:
            sys.path.append(p)
    try:
        import kis_auth as ka  # type: ignore
        import domestic_stock_functions as dsf  # type: ignore
    except Exception as e:  # 설정 파일 누락 등
        raise KISUnavailable(f"한국투자증권 공식 모듈을 불러오지 못했습니다: {e}") from e
    return ka, dsf


class KISClient:
    """mode: 'demo'(모의투자) | 'real'(실전)."""

    def __init__(self, mode: str = "demo"):
        if mode not in ("demo", "real"):
            raise ValueError("mode 는 demo 또는 real")
        self.mode = mode
        self.ka, self.dsf = _modules()
        with _lock:
            self.ka.auth(svr="vps" if mode == "demo" else "prod")
        self.trenv = self.ka.getTREnv()

    # ---------------- 시세 ----------------
    def live_price(self, code: str) -> tuple[float, str]:
        """알림용 현재가. 통합(KRX+NXT) 시세를 먼저 시도하고, 안 되면 KRX 시세."""
        for mkt, label in (("UN", "통합(KRX+NXT)"), ("J", "KRX")):
            try:
                df = self.dsf.inquire_price(env_dv=self.mode, fid_cond_mrkt_div_code=mkt, fid_input_iscd=code)
                if df is not None and not df.empty:
                    p = float(df.iloc[0].get("stck_prpr") or 0)
                    if p > 0:
                        return p, label
            except Exception:
                continue
        raise RuntimeError(f"{code} 현재가 조회 실패")

    def quote(self, code: str) -> dict:
        df = self.dsf.inquire_price(env_dv=self.mode, fid_cond_mrkt_div_code="J", fid_input_iscd=code)
        if df is None or df.empty:
            raise RuntimeError(f"{code} 현재가 조회 실패")
        r = df.iloc[0]

        def f(key):
            try:
                v = float(r.get(key, "") or "nan")
                return v if v == v else None  # NaN 제거
            except ValueError:
                return None

        return {
            "price": f("stck_prpr"),
            "per": f("per"),
            "pbr": f("pbr"),
            "eps": f("eps"),
            "bps": f("bps"),
            "w52_high": f("w52_hgpr"),
            "w52_low": f("w52_lwpr"),
            "sector": r.get("bstp_kor_isnm"),
        }

    # ---------------- 재무: 이익 성장률 ----------------
    def growth(self, code: str) -> dict:
        """한국투자 재무비율 API(공식 finance_financial_ratio)의 최근 분기 순이익 증가율."""
        df = self.dsf.finance_financial_ratio(fid_div_cls_code="1", fid_cond_mrkt_div_code="J", fid_input_iscd=code)
        if df is None or df.empty:
            raise RuntimeError(f"{code} 재무비율 조회 결과 없음")
        return parse_growth_rows(df.to_dict("records"))

    # ---------------- 휴장일 ----------------
    def is_market_open(self, day: str) -> bool | None:
        """공식 chk_holiday(국내휴장일조회). 공식 안내대로 하루 1회만 호출할 것.
        개장일여부(opnd_yn)=Y 면 주문 가능한 날."""
        df = self.dsf.chk_holiday(bass_dt=day.replace("-", ""))
        if df is None or df.empty:
            return None
        return parse_holiday_rows(df.to_dict("records"), day)

    # ---------------- 계좌 ----------------
    def balance(self) -> tuple[list[dict], float]:
        holdings_df, summary_df = self.dsf.inquire_balance(
            env_dv=self.mode,
            cano=self.trenv.my_acct,
            acnt_prdt_cd=self.trenv.my_prod,
            afhr_flpr_yn="N",
            inqr_dvsn="02",
            unpr_dvsn="01",
            fund_sttl_icld_yn="N",
            fncg_amt_auto_rdpt_yn="N",
            prcs_dvsn="00",
        )
        holdings = []
        if holdings_df is not None and not holdings_df.empty:
            for _, r in holdings_df.iterrows():
                qty = int(float(r.get("hldg_qty", 0) or 0))
                if qty > 0:
                    holdings.append(
                        {"code": r["pdno"], "qty": qty, "avg_price": float(r.get("pchs_avg_pric", 0) or 0)}
                    )
        cash = 0.0
        if summary_df is not None and not summary_df.empty:
            # 가수도정산금액(D+2 예수금)을 주문 가능 현금으로 사용
            cash = float(summary_df.iloc[0].get("prvs_rcdl_excc_amt", 0) or 0)
        return holdings, cash

    # ---------------- 주문 ----------------
    def order(self, side: str, code: str, qty: int, price: int) -> dict:
        """지정가 주문. side: buy | sell."""
        df = self.dsf.order_cash(
            env_dv=self.mode,
            ord_dv=side,
            cano=self.trenv.my_acct,
            acnt_prdt_cd=self.trenv.my_prod,
            pdno=code,
            ord_dvsn="00",  # 00: 지정가
            ord_qty=str(int(qty)),
            ord_unpr=str(int(price)),
            excg_id_dvsn_cd="KRX",
            sll_type="01" if side == "sell" else "",
        )
        ok = df is not None and not df.empty
        return {"ok": ok, "raw": df.to_dict("records") if ok else None}


def parse_growth_rows(rows: list[dict]) -> dict:
    """재무비율 응답에서 가장 최근 결산월의 순이익 증가율(없으면 영업이익 증가율)을 소수로 반환.

    예) ntin_inrt "120.0" → eps_growth 1.20
    """
    def num(v):
        try:
            x = float(str(v).replace(",", ""))
            return x if x == x else None
        except (TypeError, ValueError):
            return None

    for r in sorted(rows, key=lambda r: str(r.get("stac_yymm", "")), reverse=True):
        for key, label in (("ntin_inrt", "순이익"), ("bsop_prfi_inrt", "영업이익")):
            v = num(r.get(key))
            if v is not None:
                ym = str(r.get("stac_yymm", ""))
                return {"eps_growth": v / 100, "growth_basis": f"{ym[:4]}.{ym[4:6]} 분기 {label} 증가율 {v:+.0f}%"}
    return {}


def parse_holiday_rows(rows: list[dict], day: str) -> bool | None:
    """국내휴장일조회 응답에서 해당 날짜의 개장일여부(opnd_yn). 없으면 None."""
    key = day.replace("-", "")
    for r in rows:
        if str(r.get("bass_dt", "")) == key:
            return str(r.get("opnd_yn", "")).upper() == "Y"
    return None

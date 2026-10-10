"""분석 agent 의 AI 호출 — Claude(Anthropic) 또는 Amazon Nova 2 Lite(AWS Bedrock).

어떤 AI 를 쓸지는 키를 보고 자동으로 고릅니다 (LLM_PROVIDER 로 직접 지정 가능).
  - ANTHROPIC_API_KEY 가 있으면 Claude
  - 없고 AWS Bedrock 인증(AWS_BEARER_TOKEN_BEDROCK 또는 AWS_ACCESS_KEY_ID)이 있으면 Nova 2 Lite
  - 둘 다 없거나 --no-llm 이면 None → 분석 agent 는 규칙 기반 점수만으로 계속 동작
Nova 는 AWS 공식 SDK(boto3)의 Bedrock Converse API 로 호출합니다.
"""
from __future__ import annotations

import json
import logging
import re

from ..config import env

log = logging.getLogger(__name__)
NOVA_DEFAULT_MODEL = "global.amazon.nova-2-lite-v1:0"   # Bedrock: 전 세계 리전 자동 배분(Global inference profile)
NOVA_API_DEFAULT_MODEL = "nova-2-lite-v1"                # nova.amazon.com 개발자 API
NOVA_API_DEFAULT_URL = "https://api.nova.amazon.com/v1"
BEDROCK_KEY_PREFIXES = ("ABSK", "bedrock-api-key-")      # AWS Bedrock API 키는 이 글자로 시작


def nova_api_key() -> str | None:
    """Nova API 키: NOVA_API_KEY, 또는 AWS_BEARER_TOKEN_BEDROCK 에 Bedrock 형식이 아닌 키가 들어 있으면 그 값."""
    if env("NOVA_API_KEY"):
        return env("NOVA_API_KEY")
    tok = env("AWS_BEARER_TOKEN_BEDROCK")
    if tok and not tok.startswith(BEDROCK_KEY_PREFIXES):
        return tok
    return None


def redact(msg: str) -> str:
    """로그에 사내·개인 엔드포인트 주소가 남지 않도록 호스트 이름을 가린다."""
    from urllib.parse import urlsplit

    for name in ("NOVA_BASE_URL", "BEDROCK_ENDPOINT_URL"):
        u = env(name)
        if u:
            host = urlsplit(u if "://" in u else "https://" + u).hostname
            if host:
                msg = msg.replace(host, "***")
    return msg


def nova_auth_headers(header: str, key: str) -> dict:
    """인증 헤더. 기본은 'Authorization: Bearer <키>', 게이트웨이에 따라 x-api-key / api-key 등."""
    h = {"Content-Type": "application/json"}
    if header.lower() == "authorization":
        h["Authorization"] = f"Bearer {key}"
    else:
        h[header] = key
    return h


def pick_provider() -> str | None:
    p = (env("LLM_PROVIDER") or "").strip().lower()
    if p in ("anthropic", "claude"):
        return "anthropic"
    if p in ("nova_api", "nova-api"):
        return "nova_api"
    if p in ("nova", "bedrock"):
        return "nova"
    if p == "none":
        return None
    if env("ANTHROPIC_API_KEY"):
        return "anthropic"
    if nova_api_key():
        return "nova_api"
    if env("AWS_BEARER_TOKEN_BEDROCK") or env("AWS_ACCESS_KEY_ID"):
        return "nova"
    return None


SYSTEM_BASE = (
    "당신은 한국 주식의 장기 투자(성장주·가치주, 분할매수)를 돕는 신중한 애널리스트입니다. "
    "주어진 자료(헤드라인, 공시 제목, 숫자)만 근거로 판단하고, 자료에 없는 사실은 지어내지 마세요. "
    "단기 등락보다 기업의 장기 경쟁력과 실적에 영향을 주는 이슈를 중시합니다. "
    "반드시 요청한 JSON 한 개만 출력하고 다른 글은 쓰지 마세요."
)


class LLM:
    def __init__(self, model: str, enabled: bool = True, bedrock_client=None, http_post=None):
        self.model = model
        self.client = None
        self.provider = None
        self.calls = 0
        if not enabled:
            return
        provider = "nova" if bedrock_client is not None else pick_provider()
        if provider == "nova_api":
            # nova.amazon.com 개발자 API — OpenAI 호환 chat/completions, requests 로 호출
            key = nova_api_key()
            if not key:
                log.warning("NOVA_API_KEY 없음 → 규칙 기반 분석만 사용")
                return
            if http_post is None:
                import requests

                http_post = requests.post
            self.model = env("NOVA_MODEL") or NOVA_API_DEFAULT_MODEL
            other = env("BEDROCK_ENDPOINT_URL")  # 앞 안내대로 이 이름에 넣었을 수도 있음
            if other and "bedrock-runtime" in other:
                other = None
            base = (env("NOVA_BASE_URL") or other or NOVA_API_DEFAULT_URL).rstrip("/")
            self._nova_url = base if base.endswith("/chat/completions") else base + "/chat/completions"
            self._auth_header = env("NOVA_AUTH_HEADER") or "Authorization"
            self._nova_key = key
            self.client = http_post
            self.provider = "nova_api"
            return
        if provider is None:
            log.info("AI 키 없음 → 규칙 기반 분석만 사용")
            return
        if provider == "anthropic":
            try:
                import anthropic

                self.client = anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY"))
                self.provider = "anthropic"
            except ImportError:
                log.warning("anthropic 패키지가 없습니다: pip install anthropic")
            return
        # Amazon Nova (Bedrock)
        import os

        for k in ("AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            if os.environ.get(k):  # boto3 는 환경변수를 직접 읽으므로 공백·줄바꿈 제거
                os.environ[k] = os.environ[k].strip()
        self.model = env("NOVA_MODEL_ID") or NOVA_DEFAULT_MODEL
        try:
            if bedrock_client is None:
                import boto3

                kw = {"region_name": env("AWS_REGION") or "us-east-1"}
                if env("BEDROCK_ENDPOINT_URL"):  # VPC 엔드포인트 등 별도 주소를 받은 경우
                    kw["endpoint_url"] = env("BEDROCK_ENDPOINT_URL")
                bedrock_client = boto3.client("bedrock-runtime", **kw)
            self.client = bedrock_client
            self.provider = "nova"
        except ImportError:
            log.warning("boto3 패키지가 없습니다: pip install boto3")
        except Exception as e:
            log.warning("Bedrock 연결 실패: %s", str(e)[:160])

    @property
    def available(self) -> bool:
        return self.client is not None

    @property
    def label(self) -> str:
        return {"anthropic": f"Claude ({self.model})", "nova": f"Amazon Nova·Bedrock ({self.model})",
                "nova_api": f"Amazon Nova API ({self.model})"}.get(self.provider, "미사용")

    def ask_json(self, prompt: str, max_tokens: int = 800) -> dict | None:
        if not self.available:
            return None
        try:
            if self.provider == "nova_api":
                r = self.client(self._nova_url,
                                headers=nova_auth_headers(self._auth_header, self._nova_key),
                                json={"model": self.model, "max_tokens": max_tokens, "temperature": 0.2,
                                      "messages": [{"role": "system", "content": SYSTEM_BASE},
                                                   {"role": "user", "content": prompt}]},
                                timeout=(10, 60))  # 접속 10초, 응답 60초
                if not r.ok:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                text = r.json()["choices"][0]["message"]["content"] or ""
            elif self.provider == "nova":
                resp = self.client.converse(
                    modelId=self.model,
                    system=[{"text": SYSTEM_BASE}],
                    messages=[{"role": "user", "content": [{"text": prompt}]}],
                    inferenceConfig={"maxTokens": max_tokens, "temperature": 0.2},
                )
                text = "".join(c.get("text", "") for c in resp["output"]["message"]["content"])
            else:
                msg = self.client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    system=SYSTEM_BASE,
                    messages=[{"role": "user", "content": prompt}],
                )
                text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            self.calls += 1
            return _parse_json(text)
        except Exception as e:
            log.warning("%s 호출 실패: %s", self.label, redact(str(e))[:200])
            if _is_connection_error(e):  # 접속 자체가 안 되면 이번 실행에서는 더 시도하지 않음
                log.warning("AI 서버에 접속할 수 없어 이번 실행은 규칙 기반으로만 분석합니다")
                self.client = None
            return None


def _is_connection_error(e: Exception) -> bool:
    try:
        import requests

        if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
            return True
    except ImportError:
        pass
    try:
        from botocore.exceptions import ConnectTimeoutError, EndpointConnectionError

        return isinstance(e, (EndpointConnectionError, ConnectTimeoutError))
    except ImportError:
        return False


def _parse_json(text: str) -> dict | None:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None

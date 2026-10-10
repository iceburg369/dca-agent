"""설정 파일과 환경 변수(.env)를 읽어 오는 모듈."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
STATE_DIR = ROOT / "state"
REPORT_DIR = ROOT / "reports"
DATA_DIR = ROOT / "data"

for d in (STATE_DIR, REPORT_DIR, DATA_DIR):
    d.mkdir(exist_ok=True)

try:  # .env 가 있으면 읽고, 없어도 동작
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover
    pass


def _load(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_strategy() -> dict:
    cfg = _load("strategy.yaml")
    model = os.getenv("CLAUDE_MODEL")
    if model:
        cfg["analysis"]["llm_model"] = model
    broker = os.getenv("BROKER")
    if broker:
        cfg["execution"]["broker"] = broker
    return cfg


def load_themes() -> dict:
    return _load("themes.yaml").get("themes", {})


def load_approved() -> list[dict]:
    return _load("approved.yaml").get("approved") or []


def save_approved(items: list[dict]) -> None:
    path = CONFIG_DIR / "approved.yaml"
    header = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("#"):
                header.append(line)
            else:
                break
    body = yaml.safe_dump({"approved": items}, allow_unicode=True, sort_keys=False)
    path.write_text("\n".join(header) + "\n" + body, encoding="utf-8")


def universe() -> list[dict]:
    """themes.yaml 의 후보 종목을 [{code, name, theme_key}] 로 펼친다 (중복 제거)."""
    seen, out = set(), []
    for key, t in load_themes().items():
        for c in t.get("candidates", []):
            code = str(c["code"]).zfill(6)
            if code in seen:
                continue
            seen.add(code)
            out.append({"code": code, "name": c.get("name", code), "theme": key})
    return out


def env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name, default)
    if isinstance(v, str):
        v = v.strip()  # 키를 붙여넣을 때 섞인 앞뒤 공백·줄바꿈 제거
    return v if v not in ("", None) else default

"""서버 전용 설정. 키는 환경변수 또는 저장소 루트의 .env 에서만 읽고, 브라우저로 보내지 않는다.

    GEMINI_API_KEY     Gemini Developer API 키
    GEMINI_MODEL       기본 gemini-3.8-flash
    OPENAI_API_KEY     OpenAI 테스트 경로(--provider openai) 키
    OPENAI_PROJECT     입력·출력 공유가 꺼진 것을 확인한 OpenAI 프로젝트 ID. 없으면 OpenAI 를 부르지 않는다
    OPENAI_MODEL       기본 gpt-6-luna. GPT-6 세대 미만은 거부한다
    HUB_PROJECT_KEY    제주데이터허브 프로젝트 키 (API 583 을 프로젝트에 담으면 발급)
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "gemini-3.8-flash"


def _read_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def get(name: str, default: str | None = None) -> str | None:
    """환경변수가 먼저, 없으면 .env."""
    value = os.environ.get(name)
    if value:
        return value
    return _read_dotenv(ROOT / ".env").get(name) or default


def gemini_model() -> str:
    return get("GEMINI_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL

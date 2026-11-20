"""실제 Gemini 로 한 차례 돌려 본다. 사용: python -m tools.agent_smoke "문장"

키(GEMINI_API_KEY)는 환경변수나 .env 에서만 읽고 출력하지 않는다.
"""

import sys

from app import config
from app.agent import Agent
from app.engine import Engine

text = sys.argv[1] if len(sys.argv) > 1 else "제주시 시내에서 동네 주민 상대로 반찬가게 하려고요"
print("key set:", bool(config.get("GEMINI_API_KEY")), "model:", config.gemini_model())
r = Agent(Engine()).turn(None, text)
print("agent:", r["agent"])
print("candidates:", len(r["candidates"]))
print("reply:", r.get("agentReply"))
print("message:", r["message"])

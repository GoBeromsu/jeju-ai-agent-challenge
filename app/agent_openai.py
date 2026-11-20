"""기본 상담 경로: 공식 SDK(openai) + Responses API.

- 모델이 질문과 도구를 결정한다. 집계 수치와 지도 자료는 로컬 도구가 반환한다.
- 실제 호출 조건: OPENAI_API_KEY. OPENAI_PROJECT 는 선택 설정이다.
  store=False 로 보낸다. 실패하면 규칙 경로(engine.turn)로 이어 가고 그 사실을 응답에 남긴다.
- 기록: logs/openai_calls.jsonl 에 모델명·호출 횟수·토큰 수·도구 이름만 남긴다. 키와 사용자 문장 원문은 남기지 않는다.
- 상한: 한 차례에 모델 호출 MAX_CALLS 번, 호출마다 출력 MAX_OUTPUT_TOKENS 토큰.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import config
from .advisor import Consultation, TOOL_SPECS
from .agent import FALLBACK_NOTE, LOCAL_NOTE, MAX_MODEL_STEPS, SYSTEM, _conditions_for_model, safe_for_model
from .context_boundary import GENERAL, local_turn
from .engine import Engine, Profile

LOG = config.ROOT / "logs" / "openai_calls.jsonl"
DEFAULT_MODEL = "gpt-6-luna"  # 사용자가 정한 테스트 모델 (GPT-6 Luna)
MIN_GENERATION = 6  # GPT-6 세대 미만은 비교·자동 대체에도 쓰지 않는다
MAX_CALLS = MAX_MODEL_STEPS
MAX_OUTPUT_TOKENS = 1200


def _schema() -> list[dict]:
    return [
        {"type": "function", "name": name, "description": description, "parameters": schema, "strict": False}
        for name, (description, schema) in TOOL_SPECS.items()
    ]


def check_model(model: str) -> str:
    """gpt-<세대>-… 형식이고 세대가 MIN_GENERATION 이상이어야 한다."""
    m = re.fullmatch(r"gpt-(\d+)(?:\.\d+)?-[a-z0-9-]+", model)
    if not m or int(m.group(1)) < MIN_GENERATION or "." in model.split("-")[1]:
        raise ValueError(f"OpenAI 테스트 모델은 GPT-{MIN_GENERATION} 세대 이상만 써요: {model}")
    return model


class OpenAIAgent:
    """Agent(Gemini) 와 같은 turn() 계약. 응답의 agent 칸에 실제 모델/규칙 경로를 밝힌다."""

    provider = "openai"

    def __init__(self, engine: Engine, client=None, model: str | None = None, log_path: Path = LOG, enabled: bool = True) -> None:
        self.engine = engine
        self.enabled = enabled
        self.model = check_model(model or config.get("OPENAI_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL)
        self.log_path = log_path
        self._client = client
        self.last_error: str | None = None

    def client(self):
        if self._client is None:
            key, project = config.get("OPENAI_API_KEY"), config.get("OPENAI_PROJECT")
            if not key:
                raise RuntimeError("OPENAI_API_KEY 없음")
            from openai import OpenAI

            self._client = OpenAI(api_key=key, project=project, timeout=60.0, max_retries=1)
        return self._client

    @property
    def configured(self) -> bool:
        if not self.enabled:
            return False
        if self._client is not None:
            return True
        if not config.get("OPENAI_API_KEY"):
            return False
        try:
            import openai  # noqa: F401
        except ImportError:
            return False
        return True

    # ---------- 기록 (원문 없음) ----------
    def _log(self, record: dict) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": round(time.time(), 3), "provider": "openai", **record}, ensure_ascii=False) + "\n")

    def _create(self, input_items: list, meter: dict):
        if meter["modelCalls"] >= MAX_CALLS:
            raise RuntimeError(f"모델 호출 상한 {MAX_CALLS}회 초과")
        meter["modelCalls"] += 1
        resp = self.client().responses.create(
            model=self.model,
            instructions=SYSTEM,
            input=input_items,
            tools=_schema(),
            parallel_tool_calls=False,
            reasoning={"effort": "low"},
            max_output_tokens=MAX_OUTPUT_TOKENS,
            store=False,
            include=["reasoning.encrypted_content"],  # store=False 에서 추론 항목을 다음 호출로 넘기려면 필요
        )
        usage = getattr(resp, "usage", None)
        meter["inputTokens"] += int(getattr(usage, "input_tokens", 0) or 0)
        meter["outputTokens"] += int(getattr(usage, "output_tokens", 0) or 0)
        return resp

    # ---------- 한 차례 ----------
    def turn(self, profile_dict: dict | None, text: str, *, selection: dict | None = None,
             context_mode: str = GENERAL) -> dict:
        if context_mode != GENERAL:
            out = local_turn(self.engine, profile_dict, text, selection=selection)
            out["agent"].update(provider="openai", model=self.model, path="local",
                                reason="제한 문맥 로컬 처리", modelCalls=0, inputTokens=0, outputTokens=0)
            return out
        meter = {"modelCalls": 0, "inputTokens": 0, "outputTokens": 0}
        base = {"provider": "openai", "model": self.model, "maxCalls": MAX_CALLS, "maxOutputTokens": MAX_OUTPUT_TOKENS}
        if not self.configured:
            out = self.engine.turn(profile_dict, text)
            out["message"] = f"{FALLBACK_NOTE} {out['message']}"
            out["agent"].update(base, path="rules", reason="OpenAI 키 또는 SDK 미설정" if self.enabled else "모델 비활성화", **meter)
            self._log({"model": self.model, "path": "rules", "reason": "not configured", **meter})
            return out
        if not safe_for_model(text):
            out = self.engine.turn(profile_dict, text)
            out["message"] = f"{LOCAL_NOTE} {out['message']}"
            out["agent"].update(base, path="rules", reason="바깥 전송 제한", **meter)
            return out
        try:
            out = self._turn(profile_dict, text, meter, selection)
            out["agent"] = {**base, **out["agent"], **meter}
            self._log({"model": self.model, "path": "model", "tools": [c["name"] for c in out["agent"]["calls"]], "inputChars": len(text), **meter})
            return out
        except Exception as e:  # 모델·네트워크·형식 오류는 모두 규칙 경로로
            self.last_error = type(e).__name__
            out = self.engine.turn(profile_dict, text)
            out["message"] = f"{FALLBACK_NOTE} {out['message']}"
            out["agent"].update(base, path="rules", reason="모델 호출 실패", error=self.last_error, **meter)
            self._log({"model": self.model, "path": "rules", "reason": "model error", "error": self.last_error, **meter})
            return out

    def _turn(self, profile_dict: dict | None, text: str, meter: dict, selection: dict | None) -> dict:
        turn = Consultation(self.engine, Profile.from_dict(profile_dict), text, selection)
        items = [{"role": item["role"], "content": item["text"]}
                 for item in turn.profile.history if safe_for_model(item["text"])]
        items.append({"role": "user", "content": json.dumps({
            "현재조건": _conditions_for_model(self.engine, turn.profile, selection), "사용자": text,
        }, ensure_ascii=False)})
        reply = ""
        for _ in range(MAX_CALLS):
            resp = self._create(items, meter)
            calls = [item for item in resp.output if item.type == "function_call"]
            if not calls:
                reply = (resp.output_text or "").strip()
                if not reply:
                    raise RuntimeError("모델이 도구도 답도 내지 않음")
                break
            outputs = []
            for call in calls:
                result = turn.call(call.name, json.loads(call.arguments or "{}"))
                if turn.question:
                    break
                outputs.append({"type": "function_call_output", "call_id": call.call_id,
                                "output": json.dumps(result, ensure_ascii=False)})
            if turn.question:
                break
            items = [*items, *resp.output, *outputs]
        out = turn.finish(reply)
        out["agent"]["path"] = "model"
        return out

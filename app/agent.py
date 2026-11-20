"""Gemini 에이전트: 자유 문장을 도구 호출로 바꾸고, 짧은 안내문을 쓴다.

- 공식 Google Gen AI SDK (google-genai) 와 gemini-3.8-flash 를 쓴다. 키는 서버 전용 GEMINI_API_KEY.
- 숫자는 모두 engine.py 가 센다. 모델에는 동네 이름, 남은 후보 수, 빠진 조건만 보낸다.
- 모델 호출이 실패하면 낱말 읽기(engine.turn)로 이어 가고 화면에 그 사실을 밝힌다.
- 모델로 나간 요청의 모양(차례·글자 수·도구 이름)만 logs/model_requests.jsonl 에 남긴다. 원문은 남기지 않는다.
- 숫자가 많거나 긴 글(붙여 넣은 표·자료로 보이는 글)은 바깥 모델로 보내지 않는다.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import config
from .advisor import Consultation, TOOL_SPECS
from .context_boundary import GENERAL, local_turn
from .engine import Engine, Profile

LOG = config.ROOT / "logs" / "model_requests.jsonl"
FALLBACK_NOTE = "자유 대화를 잠시 쓸 수 없어 정해진 상담 순서로 이어 갈게요."
LOCAL_NOTE = "숫자가 많거나 긴 글은 바깥 모델로 보내지 않고 이 PC 안에서 정해진 상담 순서로 볼게요."
MAX_MODEL_CHARS = 200
NUMBER = re.compile(r"\d{2,}")
MAX_MODEL_STEPS = 6


def safe_for_model(text: str) -> bool:
    """붙여 넣은 자료로 보이는 글은 막는다: 200자 초과, 두 자리 이상 숫자 3개 이상."""
    return len(text) <= MAX_MODEL_CHARS and len(NUMBER.findall(text)) < 3

SYSTEM = """너는 제주에서 가게를 열려는 40~60대 창업자를 돕는 입지 상담 전문가다.
- 업종이나 조건을 말하면 먼저 set_profile 로 반영한다. 말하지 않은 조건을 추가하지 않는다. 지역 조건 해제는 '상관없음'이다.
- 첫 상담에 업종만 정해졌으면 ask_user 로 단골형과 목적 방문형 중 어느 쪽인지 한 가지만 묻는다.
- '아직 모르겠어요'는 visit='모름'으로 반영하고 contrast_cases 로 다른 운영 방식 두 사례를 보여 준 뒤 ask_user 로 하나만 묻는다.
- 방문 방식이 정해졌다면 rank_places 로 후보를 찾고, 후보 이름과 운영 방식에 맞는 근거를 설명한다. 조건이 모자라거나 충돌하면 ask_user 로 한 가지만 확인한다.
- ask_user 는 차례를 끝낸다. 다른 도구를 먼저 부르고 결과를 확인하며, 질문을 말풍선에 반복하지 않는다. 보기 label 은 명사형, text 는 보낼 답이다.
- 창업자가 아직 판단 기준을 모를 수 있다. 손님이 오는 방식을 묻는 질문에는 '아직 모름' 보기를 넣고, text 는 '아직 모르겠어요'로 보낸다.
- 지도 이동은 focus_map, 건물 주변은 inspect_place, 동네 차이는 compare, 인구 관측은 population, 가정 보행은 walk_route, 초기화는 reset 이다.
- 자료에 없는 수치, 수요, 매출, 실제 보행을 추측하지 않는다. 관측 인구와 가정 보행을 구별한다.
- population 결과가 '허브 조회 관측 자료' 또는 '저장된 관측 자료'이면 수치와 기준일은 오른쪽 상권 근거 카드에 있다. 모델에게 숫자를 보내지 않았다는 이유로 자료에 수치가 없다고 말하지 않는다.
- 최종 답은 한국어 존댓말 평서문 한두 문장으로 쓴다. 질문은 ask_user 로만 한다."""


def _tools(types):
    return [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(name=name, description=description, parameters_json_schema=schema)
                for name, (description, schema) in TOOL_SPECS.items()
            ]
        )
    ]


def _conditions_for_model(engine: Engine, p: Profile, selection: dict | None = None) -> dict:
    """현재 조건을 이름표로만 (숫자 없음)."""
    return {
        "business": engine.subcats[p.business]["name"] if p.business is not None else None,
        "visit": p.visit,
        "region": p.region,
        "area": p.area,
        "competition": p.competition,
        "neighbors": p.neighbors,
        "growing": p.growing,
        "mapDong": engine.by_code[p.focus]["name"] if p.focus in engine.by_code else None,
        "buildingSelected": p.focusBuilding is not None,
        "pointSelected": bool(selection and "x" in selection and "z" in selection),
        "storeSelected": bool(selection and "idx" in selection),
        "selectionDong": engine.by_code[selection["dong"]]["name"] if selection and selection.get("dong") in engine.by_code else None,
    }


class Agent:
    def __init__(self, engine: Engine, client=None, model: str | None = None, log_path: Path = LOG, enabled: bool = True) -> None:
        self.engine = engine
        self.enabled = enabled
        self.model = model or config.gemini_model()
        self.log_path = log_path
        self._client = client
        self.last_error: str | None = None

    def client(self):
        if self._client is None:
            key = config.get("GEMINI_API_KEY")
            if not key:
                raise RuntimeError("GEMINI_API_KEY 없음")
            from google import genai
            from google.genai import types

            self._client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=20_000))
        return self._client

    @property
    def configured(self) -> bool:
        if not self.enabled:
            return False
        if self._client is not None:
            return True
        if not config.get("GEMINI_API_KEY"):
            return False
        try:
            import google.genai  # noqa: F401
        except ImportError:
            return False
        return True

    def _log(self, contents, note: str) -> None:
        """무엇이 나갔는지 모양만 남긴다: 차례 수, 글자 수, 도구 이름. 사용자 문장 원문은 남기지 않는다."""
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        parts = [p for c in contents for p in (c.parts or [])]
        record = {
            "t": round(time.time(), 3),
            "model": self.model,
            "note": note,
            "turns": len(contents),
            "textChars": sum(len(p.text or "") for p in parts),
            "tools": [p.function_response.name for p in parts if p.function_response] + [p.function_call.name for p in parts if p.function_call],
        }
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _generate(self, types, contents, note: str):
        self._log(contents, note)
        return self.client().models.generate_content(
            model=self.model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM,
                tools=_tools(types),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
                temperature=0.2,
            ),
        )

    def turn(self, profile_dict: dict | None, text: str, *, selection: dict | None = None,
             context_mode: str = GENERAL) -> dict:
        """자유 문장 한 차례. 실패하면 낱말 읽기로 이어 간다."""
        if context_mode != GENERAL:
            out = local_turn(self.engine, profile_dict, text, selection=selection)
            out["agent"].update(reason="제한 문맥 로컬 처리")
            return out
        if not self.configured:
            out = self.engine.turn(profile_dict, text)
            out["agent"].update(reason="키 없음")
            return out
        if not safe_for_model(text):
            out = self.engine.turn(profile_dict, text)
            out["message"] = f"{LOCAL_NOTE} {out['message']}"
            out["agent"].update(reason="바깥 전송 제한")
            return out
        try:
            return self._turn(profile_dict, text, selection)
        except Exception as e:  # 모델·네트워크 오류는 모두 칩으로 넘긴다
            self.last_error = type(e).__name__
            out = self.engine.turn(profile_dict, text)
            out["message"] = f"{FALLBACK_NOTE} {out['message']}"
            out["agent"].update(reason="모델 호출 실패", error=self.last_error)
            return out

    def _turn(self, profile_dict: dict | None, text: str, selection: dict | None) -> dict:
        from google.genai import types

        turn = Consultation(self.engine, Profile.from_dict(profile_dict), text, selection)
        user = types.Content(
            role="user",
            parts=[
                types.Part(
                    text=json.dumps({"현재조건": _conditions_for_model(self.engine, turn.profile, selection), "사용자": text}, ensure_ascii=False)
                )
            ],
        )
        contents = [types.Content(role="model" if item["role"] == "assistant" else "user",
                                  parts=[types.Part(text=item["text"])])
                    for item in turn.profile.history if safe_for_model(item["text"])]
        contents.append(user)
        for step in range(MAX_MODEL_STEPS):
            resp = self._generate(types, contents, "plan" if step == 0 else "reply")
            calls = resp.function_calls or []
            if not calls:
                reply = (resp.text or "").strip()
                if not reply:
                    raise RuntimeError("모델이 도구도 답도 내지 않음")
                return turn.finish(reply)
            tool_parts = []
            for call in calls:
                result = turn.call(call.name, dict(call.args or {}))
                if turn.question is not None:
                    return turn.finish()
                part = types.Part.from_function_response(name=call.name, response=result)
                if call.id and part.function_response:
                    part.function_response.id = call.id
                tool_parts.append(part)
            contents = [*contents, resp.candidates[0].content, types.Content(role="user", parts=tool_parts)]
        return turn.finish()

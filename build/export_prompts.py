"""제출용 프롬프트 문서를 코드에서 바로 뽑는다(문서와 코드가 어긋나지 않게).

    python3 build/export_prompts.py   # docs/프롬프트.md
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import agent, agent_openai  # noqa: E402
from app.advisor import BASIS, NEXT_QUESTION, VISIT_QUESTION  # noqa: E402


def main() -> None:
    gem_tools = [
        {"name": "set_conditions", "args": "business, visit(목적|지나가다|모름), region(제주시|서귀포시|상관없음), area(동|읍면|상관없음), competition, neighbors, growing"},
        {"name": "show_dong", "args": "name"},
        {"name": "reset", "args": "-"},
    ]
    lines = [
        "# 프롬프트와 도구 정의",
        "",
        "이 문서는 `python3 build/export_prompts.py` 로 코드에서 그대로 뽑았습니다. 숫자·지도·후보 고르기는 모델이 아니라 계산 도구(app/engine.py, app/advisor.py)가 합니다.",
        "",
        "## 1. 운영 경로: Gemini (app/agent.py)",
        f"- 모델: `{agent.config.gemini_model()}` (환경변수 GEMINI_MODEL 로 바꿈), Google Gen AI SDK `google-genai`",
        "- 모델로 보내는 것: 첫 요청은 사용자 문장과 현재 조건 이름표(업종 이름·손님 방식·지역·동/읍면·경쟁·이웃·성장 여부). 도구를 부르면 답 요청에 도구 결과(남은 후보 수·후보 동네 이름·빠진 이유·보여 준 동네 이름)가 더해진다. 점포 수·인구 같은 숫자는 보내지 않는다. 200자 초과이거나 두 자리 이상 숫자가 3개 이상인 글은 보내지 않는다.",
        f"- 모델이 실패하면: \"{agent.FALLBACK_NOTE}\" 를 앞에 붙이고 같은 상담 순서(규칙 경로)로 이어 간다.",
        "",
        "### 시스템 지시",
        "```text",
        agent.SYSTEM,
        "```",
        "",
        "### 도구",
        "| 도구 | 인자 |",
        "|---|---|",
        *[f"| `{t['name']}` | {t['args']} |" for t in gem_tools],
        "",
        "## 2. 테스트 경로: OpenAI (app/agent_openai.py, 운영 기본값 아님)",
        f"- 모델: `{agent_openai.DEFAULT_MODEL}` (GPT-{agent_openai.MIN_GENERATION} 세대 미만 거부), 공식 SDK `openai` + Responses API, store=False",
        f"- 한 차례 상한: 모델 호출 {agent_openai.MAX_CALLS}회, 호출마다 출력 {agent_openai.MAX_OUTPUT_TOKENS} 토큰",
        "- 기록: 모델명·호출 횟수·토큰 수·도구 이름만. 키와 사용자 문장 원문은 남기지 않는다.",
        "",
        "### 시스템 지시",
        "```text",
        agent_openai.INSTRUCTIONS,
        "```",
        "",
        "### 도구 (JSON 스키마)",
        "```json",
        json.dumps(agent_openai._schema(), ensure_ascii=False, indent=2),
        "```",
        "",
        "## 3. 상담 순서 (모델 없이도 같은 흐름, app/advisor.py)",
        "",
        f"첫 질문: **{VISIT_QUESTION['text']}**",
        "",
        f"> {VISIT_QUESTION['why']}",
        "",
        "보기: " + " / ".join(o["label"] for o in VISIT_QUESTION["options"]),
        "",
        "| 답 | 비교 기준 | 설명 |",
        "|---|---|---|",
        *[f"| {k} | {v['title']} | {v['explain']} |" for k, v in BASIS.items()],
        "",
        "| 답 | 다음 질문 | 이유 |",
        "|---|---|---|",
        *[f"| {k} | {v['text']} | {v['why']} |" for k, v in NEXT_QUESTION.items()],
        "",
    ]
    out = ROOT / "docs" / "프롬프트.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(out.relative_to(ROOT))


if __name__ == "__main__":
    main()

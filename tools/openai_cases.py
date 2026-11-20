"""OpenAI 테스트 경로의 합성 입력과 판정. 모델은 gpt-6-luna 하나. 비교 모델은 GPT-6 세대 이상을 따로 정할 때만 쓴다.

    python -m tools.openai_cases            # 호출 없이 입력·판정 기준만 출력
    python -m tools.openai_cases --live     # OPENAI_API_KEY + OPENAI_PROJECT(공유 없는 프로젝트) 있을 때만 실제 호출

입력은 모두 지어낸 문장이다(고객 입력 아님). 실제 호출 여부·횟수·토큰은 응답의 agent 칸과 logs/openai_calls.jsonl 에 남는다.
"""

from __future__ import annotations

import argparse
import json

from app.agent_openai import DEFAULT_MODEL, OpenAIAgent
from app.engine import Engine

# (이름, 이전 차례 문장들, 이번 문장, 판정: 도구 이름 집합 또는 조건 기대값)
CASES = [
    {"id": "복합 조건", "before": [], "text": "서귀포 읍면에서 관광객 상대로 작은 카페 하고 싶어요",
     "expect": {"tools": {"set_conditions"}, "profile": {"region": "서귀포시", "area": "읍면", "visit": "목적"}, "business": "카페", "competition": False}},
    {"id": "이전 조건 수정", "before": ["제주시 동 지역에서 반찬가게 하려고요"], "text": "아 제주시 말고 서귀포로 바꿀게요",
     "expect": {"tools": {"set_conditions"}, "profile": {"region": "서귀포시", "area": "동"}}},
    {"id": "조건 풀기", "before": ["서귀포 읍면에서 카페 할래요"], "text": "읍면 아니어도 괜찮아요",
     "expect": {"tools": {"set_conditions"}, "profile": {"area": None, "region": "서귀포시"}}},
    {"id": "무리한 조건", "before": [], "text": "제주시 노형동 근처인데 서귀포시 읍면에 카페 내고 싶어요",
     "expect": {"tools": {"ask_clarify"}}},
    {"id": "부족한 정보", "before": [], "text": "가게 하나 차리고 싶은데 어디가 좋아요?",
     "expect": {"tools": {"ask_clarify"}}},
    {"id": "말하지 않은 조건 금지", "before": [], "text": "제주에 작은 카페를 열고 싶어요",
     "expect": {"tools": {"set_conditions"}, "business": "카페", "competition": False, "neighbors": False, "growing": False}},
]


def judge(engine: Engine, case: dict, out: dict) -> list[str]:
    """놓친 항목 목록. 비면 통과."""
    miss = []
    exp = case["expect"]
    tools = {c["name"] for c in out.get("agent", {}).get("calls", [])}
    if out.get("agent", {}).get("path") != "model":
        return ["모델 경로 아님: " + str(out.get("agent", {}).get("reason"))]
    if not exp["tools"] <= tools:
        miss.append(f"도구 {sorted(exp['tools'])} 기대, 실제 {sorted(tools)}")
    p = out["profile"]
    for k, v in exp.get("profile", {}).items():
        if p.get(k) != v:
            miss.append(f"{k}={p.get(k)!r}, 기대 {v!r}")
    if "business" in exp:
        got = engine.subcats[p["business"]]["name"] if p.get("business") is not None else None
        if got != exp["business"]:
            miss.append(f"업종 {got!r}, 기대 {exp['business']!r}")
    for k in ("competition", "neighbors", "growing"):
        if k in exp and p.get(k) != exp[k]:
            miss.append(f"{k}={p.get(k)} (말하지 않은 조건)")
    return miss


def run_case(agent: OpenAIAgent, case: dict) -> dict:
    profile = None
    for t in case["before"]:
        profile = agent.engine.turn(profile, t)["profile"]  # 이전 차례는 규칙 경로로 만든다(호출 절약)
    return agent.turn(profile, case["text"])


def compare(engine: Engine, primary: OpenAIAgent, secondary: OpenAIAgent | None) -> list[dict]:
    rows = []
    for case in CASES:
        out = run_case(primary, case)
        row = {"id": case["id"], "primary": primary.model, "primaryMiss": judge(engine, case, out), "primaryAgent": out["agent"]}
        if row["primaryMiss"] and secondary is not None:
            out2 = run_case(secondary, case)
            row.update({"secondary": secondary.model, "secondaryMiss": judge(engine, case, out2), "secondaryAgent": out2["agent"]})
        rows.append(row)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="실제 OpenAI 호출 (공유 없는 프로젝트 확인 후)")
    ap.add_argument("--compare-model", help="놓친 입력만 다시 돌릴 비교 모델 (GPT-6 세대 이상만, 기본 없음)")
    args = ap.parse_args()
    engine = Engine()
    if not args.live:
        for c in CASES:
            print(json.dumps({"id": c["id"], "text": c["text"], "expect": {k: sorted(v) if isinstance(v, set) else v for k, v in c["expect"].items()}}, ensure_ascii=False))
        return
    luna = OpenAIAgent(engine, model=DEFAULT_MODEL)
    if not luna.configured:
        raise SystemExit("OPENAI_API_KEY 와 OPENAI_PROJECT 가 없어 실제 호출하지 않았어요.")
    second = OpenAIAgent(engine, model=args.compare_model) if args.compare_model else None
    for row in compare(engine, luna, second):
        print(json.dumps(row, ensure_ascii=False))


if __name__ == "__main__":
    main()

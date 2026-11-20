"""OpenAI 테스트 경로: 모의 Responses 응답으로 도구 루프, 규칙 대체, 상한, 기록을 검사한다. 실제 호출 없음."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen
from pathlib import Path
from types import SimpleNamespace

from app.agent_openai import MAX_CALLS, MAX_OUTPUT_TOKENS, OpenAIAgent
from app.engine import Engine
from app.hub import HubClient, Population
from app.place import Place
from app.server import make_server
from tools.openai_cases import CASES, compare, judge, run_case

SECRET = "sk-test-SHOULD-NEVER-BE-LOGGED"


def call(name: str, args: dict, i: int = 0):
    return SimpleNamespace(type="function_call", name=name, arguments=json.dumps(args, ensure_ascii=False), call_id=f"call_{i}")


def resp(*calls, text: str = "", out_tokens: int = 30):
    return SimpleNamespace(output=list(calls), output_text=text, usage=SimpleNamespace(input_tokens=200, output_tokens=out_tokens))


class FakeResponses:
    def __init__(self, script):
        self.script = list(script)
        self.requests: list[dict] = []

    def create(self, **kw):
        self.requests.append(kw)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, script):
        self.responses = FakeResponses(script)


class OpenAIPathTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = Engine()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "openai_calls.jsonl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def agent(self, script, model: str = "gpt-6-luna") -> OpenAIAgent:
        return OpenAIAgent(self.engine, client=FakeClient(script), model=model, log_path=self.log)

    def test_pasted_numbers_never_reach_openai(self) -> None:
        a = self.agent([])
        out = a.turn(None, "카페 매출 1200 1500 1800 2100 만원 자료예요")
        self.assertEqual(a._client.responses.requests, [])
        self.assertEqual(out["agent"]["reason"], "바깥 전송 제한")
        self.assertEqual(out["agent"]["modelCalls"], 0)

    def test_compound_conditions_use_engine_and_record_limits(self) -> None:
        a = self.agent([
            resp(call("set_conditions", {"business": "카페", "region": "서귀포시", "area": "읍면", "visit": "목적"})),
            resp(call("rank_places", {}, 1)),
            resp(text="서귀포 읍면에서 찾아오는 손님 기준으로 골랐어요.", out_tokens=40),
        ])
        out = a.turn(None, CASES[0]["text"])
        self.assertEqual(judge(self.engine, CASES[0], out), [])
        ag = out["agent"]
        self.assertEqual((ag["path"], ag["used"], ag["model"]), ("model", True, "gpt-6-luna"))
        self.assertEqual((ag["calls"][0]["name"], ag["maxCalls"], ag["maxOutputTokens"]), ("set_conditions", MAX_CALLS, MAX_OUTPUT_TOKENS))
        self.assertEqual(len(a._client.responses.requests), 3)
        for req in a._client.responses.requests:
            self.assertIs(req["store"], False)
            self.assertEqual(req["max_output_tokens"], MAX_OUTPUT_TOKENS)
            self.assertTrue({"set_profile", "rank_places"} <= {t["name"] for t in req["tools"]})
        # 숫자와 후보는 엔진이 정한다: 같은 조건의 규칙 경로와 후보가 같다
        rules = self.engine.turn(None, "서귀포 읍면 카페 일부러 찾아와 머무는 손님")
        self.assertEqual(out["candidates"], rules["candidates"])
        self.assertEqual(out["agentReply"], "서귀포 읍면에서 찾아오는 손님 기준으로 골랐어요.")
        # 두 번째 호출에 도구 결과가 같은 call_id 로 돌아간다
        second = a._client.responses.requests[1]["input"]
        self.assertTrue(any(isinstance(x, dict) and x.get("type") == "function_call_output" and x["call_id"] == "call_0" for x in second))
        final = a._client.responses.requests[2]["input"]
        self.assertEqual([x["call_id"] for x in final if isinstance(x, dict) and x.get("type") == "function_call_output"], ["call_0", "call_1"])

    def test_edit_previous_condition(self) -> None:
        case = CASES[1]
        a = self.agent([resp(call("set_conditions", {"region": "서귀포시"})), resp(text="서귀포 동 지역으로 바꿨어요.")])
        out = run_case(a, case)
        self.assertEqual(judge(self.engine, case, out), [])
        self.assertEqual(self.engine.subcats[out["profile"]["business"]]["name"], "반찬/식료품 소매업")  # 앞서 말한 업종 유지

    def test_conflict_asks_one_clarifying_question_without_second_call(self) -> None:
        case = CASES[3]
        a = self.agent([resp(call("ask_clarify", {"question": "가게는 제주시와 서귀포시 중 어디에 내실 생각이세요?", "reason": "충돌", "options": ["제주시", "서귀포시"]}))])
        out = run_case(a, case)
        self.assertEqual(judge(self.engine, case, out), [])
        self.assertEqual(out["message"], "가게는 제주시와 서귀포시 중 어디에 내실 생각이세요?")
        self.assertEqual([c["label"] for c in out["chips"]], ["제주시", "서귀포시", "처음부터"])
        self.assertEqual(out["clarify"]["reason"], "충돌")
        self.assertEqual(out["agent"]["calls"][0]["name"], "ask_clarify")
        self.assertEqual(len(a._client.responses.requests), 1)

    def test_model_failure_falls_back_to_rules_and_says_so(self) -> None:
        a = self.agent([RuntimeError("503")])
        out = a.turn(None, "제주에 작은 카페를 열고 싶어요")
        ag = out["agent"]
        self.assertEqual((ag["path"], ag["used"], ag["reason"], ag["modelCalls"]), ("rules", False, "모델 호출 실패", 1))
        self.assertTrue(out["message"].startswith("자유 대화를 잠시 쓸 수 없어"))
        self.assertEqual(out["advice"]["stage"], "question")  # 규칙 경로도 같은 상담

    def test_question_tool_rejects_missing_model_choices(self) -> None:
        from app.advisor import Consultation
        from app.engine import Profile

        consultation = Consultation(self.engine, Profile(), "제주에 카페를 열고 싶어요")
        result = consultation.call("ask_user", {})
        self.assertIsNone(consultation.question)
        self.assertIn("찾지 못했습니다", result["status"])

    def test_not_configured_makes_no_call(self) -> None:
        a = OpenAIAgent(self.engine, log_path=self.log, enabled=False)
        out = a.turn(None, "제주에 작은 카페를 열고 싶어요")
        self.assertEqual((out["agent"]["path"], out["agent"]["modelCalls"]), ("rules", 0))

    def test_call_cap_is_enforced(self) -> None:
        a = self.agent([resp(call("rank_places", {}, i)) for i in range(6)])
        out = a.turn(None, "제주 후보를 살펴보고 싶어요")
        self.assertTrue(out["agent"]["used"])
        self.assertEqual((out["agent"]["modelCalls"], len(out["agent"]["steps"])), (6, 6))
        self.assertEqual(out["question"], None)

    def test_undecided_turn_returns_contrasting_cases_then_stops_at_one_question(self) -> None:
        a = self.agent([
            resp(call("set_profile", {"business": "카페", "visit": "모름"})),
            resp(call("contrast_cases", {}, 1)),
            resp(call("ask_user", {"question": "어느 운영 방식이 가게와 더 닮았나요?", "options": [
                {"label": "동네 손님", "text": "매일 들르는 단골 손님"},
                {"label": "여행 손님", "text": "일부러 찾아와 머무는 손님"},
            ]}, 2), call("reset", {}, 3)),
        ])
        out = a.turn(None, "카페를 열 건데 손님은 아직 모르겠어요")
        self.assertEqual([s["tool"] for s in out["agent"]["steps"]], ["set_profile", "contrast_cases", "ask_user"])
        cases = out["advice"]["candidates"]
        self.assertEqual([c["letter"] for c in cases], ["A", "B"])
        self.assertNotEqual(cases[0]["code"], cases[1]["code"])
        self.assertNotEqual(cases[0]["reason"], cases[1]["reason"])
        self.assertEqual([o["text"] for o in out["question"]["options"]],
                         ["매일 들르는 단골 손님", "일부러 찾아와 머무는 손님"])
        self.assertEqual([o["label"] for o in out["question"]["options"]], ["동네 손님", "여행 손님"])
        self.assertEqual(len(a._client.responses.requests), 3)

    def test_map_navigation_preserves_recent_chat_for_the_next_chip(self) -> None:
        client = FakeClient([
            resp(call("set_profile", {"business": "카페"})),
            resp(call("ask_user", {"question": "어떤 손님이 올까요?", "options": [
                {"label": "동네 손님", "text": "매일 들르는 단골 손님"},
                {"label": "여행 손님", "text": "일부러 찾아와 머무는 손님"},
            ]})),
            resp(call("set_profile", {"visit": "지나가다"})),
            resp(text="단골 손님을 기준으로 살펴보겠습니다."),
        ])
        server = make_server(port=0, agent_client=client, population=Population(client=HubClient(project_key="")))
        server.RequestHandlerClass.func.agent.log_path = self.log
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/api/turn"

        def post(**body):
            request = Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with urlopen(request) as response:
                return json.load(response)

        opening = "제주에 작은 카페를 열고 싶어요"
        first = post(text=opening)
        self.assertTrue(first["agent"]["used"])
        self.assertEqual(first["agent"]["provider"], "openai")
        self.assertEqual(first["question"]["options"][0]["text"], "제주시 쪽을 생각하고 있어요.")
        building = "50130320:new:0"
        focused = post(profile=first["profile"], mapFocus={"target": building, "level": "building"})
        self.assertEqual((focused["focus"], focused["scene"]["fly"], focused["focusLevel"], focused["focusTarget"]),
                         ("50130320", "50130320", "building", building))
        self.assertFalse(focused["agent"]["used"])
        self.assertEqual(focused["notebook"]["question"], first["notebook"]["question"])
        self.assertEqual(focused["profile"]["pendingQuestion"], "region")

        second = post(profile=focused["profile"], text=first["question"]["options"][0]["text"], chip=True)
        self.assertTrue(second["agent"]["used"])
        self.assertEqual(second["profile"]["visit"], "지나가다")
        self.assertEqual(second["profile"]["region"], "제주시")
        self.assertEqual(second["focusTarget"], building)
        self.assertIn({"role": "user", "content": opening}, client.responses.requests[2]["input"])
        island = post(profile=second["profile"], mapFocus={"target": "", "level": "island"})
        self.assertEqual((island["focus"], island["focusLevel"], island["focusTarget"]), (None, "island", None))
        self.assertEqual(island["profile"]["history"], second["profile"]["history"])

    def test_map_and_site_tools_return_local_evidence_without_sending_geometry_to_model(self) -> None:
        self.engine.place = Place(self.engine)
        code, store = "50130320", 53823
        x, z = self.engine.place.xz[store]
        a = self.agent([
            resp(call("set_profile", {"business": "카페"}, 0)),
            resp(call("focus_map", {"target": f"{code}:store:{store}", "level": "building"}, 1)),
            resp(call("inspect_place", {}, 2)),
            resp(call("walk_route", {}, 3)),
            resp(call("compare", {"a": "표선면", "b": "연동"}, 4)),
            resp(text="점포 주변과 가정 보행 경로를 따로 확인했습니다."),
        ])
        out = a.turn(None, "표선면의 점포와 보행 경로를 살펴보고 싶어요", selection={"dong": code, "idx": store, "x": x, "z": z})
        self.assertEqual(out["focusTarget"], f"{code}:store:{store}")
        self.assertEqual(out["store"]["idx"], store)
        self.assertEqual(out["nearby"]["siteRadius"], out["store"]["siteRadius"])
        self.assertEqual(out["nearby"]["siteRadius"]["center"], {"x": x, "z": z})
        self.assertTrue(out["nearby"]["items"][0]["why"])
        self.assertEqual((out["walk"]["kind"], out["walk"]["supported"]), ("assumed", True))
        self.assertEqual(out["agent"]["steps"][3]["kind"], "assumed")
        differences = out["comparison"]["differences"]
        self.assertEqual({row["field"] for row in differences},
                         {"total", "areaKm2", "anchorTotal", "business.count", "business.share", "bizTrend.growth"})
        self.assertTrue(all(row["a"] != row["b"] for row in differences))
        sent = a._client.responses.requests[-1]["input"]
        body = next(json.loads(item["output"]) for item in sent if isinstance(item, dict) and item.get("call_id") == "call_3")
        self.assertEqual(set(body), {"동네", "kind", "status"})
        inspect_body = next(json.loads(item["output"]) for item in sent if isinstance(item, dict) and item.get("call_id") == "call_2")
        self.assertEqual(set(inspect_body), {"동네", "관계"})
        compare_body = next(json.loads(item["output"]) for item in sent if isinstance(item, dict) and item.get("call_id") == "call_4")
        self.assertTrue(all(set(item) == {"지표", "높은쪽"} for item in compare_body["differences"]))

    def test_log_has_counts_but_no_key_or_raw_input(self) -> None:
        a = self.agent([resp(call("set_conditions", {"business": "카페"})), resp(text="네.", out_tokens=12)])
        raw = "합성-원문-표식 제주에 작은 카페를 열고 싶어요"
        a.turn(None, raw)
        a2 = self.agent([RuntimeError(f"401 bad key {SECRET}")])
        a2.turn(None, raw)
        logged = self.log.read_text()
        self.assertNotIn("합성-원문-표식", logged)
        self.assertNotIn(SECRET, logged)
        rows = [json.loads(x) for x in logged.splitlines()]
        self.assertEqual(rows[0]["path"], "model")
        self.assertEqual((rows[0]["modelCalls"], rows[0]["outputTokens"]), (2, 42))
        self.assertEqual((rows[1]["path"], rows[1]["error"]), ("rules", "RuntimeError"))

    def test_older_generations_are_refused(self) -> None:
        from app.agent_openai import DEFAULT_MODEL, check_model

        self.assertEqual(DEFAULT_MODEL, "gpt-6-luna")
        for bad in ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5-luna", "gpt-4o"):
            with self.assertRaises(ValueError, msg=bad):
                OpenAIAgent(self.engine, client=FakeClient([]), model=bad, log_path=self.log)
        self.assertEqual(check_model("gpt-7-luna"), "gpt-7-luna")

    def test_second_model_only_for_misses(self) -> None:
        # 기본 모델이 마지막 사례에서 말하지 않은 경쟁 조건을 넣어 놓친다 → 그 사례만 비교 모델(GPT-6 세대 이상)로
        luna_script, second_script = [], []
        replies = {
            "복합 조건": [call("set_conditions", {"business": "카페", "region": "서귀포시", "area": "읍면", "visit": "목적"})],
            "이전 조건 수정": [call("set_conditions", {"region": "서귀포시"})],
            "조건 풀기": [call("set_conditions", {"area": "상관없음"})],
            "무리한 조건": [call("ask_clarify", {"question": "어느 쪽에 내실 생각이세요?", "reason": "충돌",
                                                  "options": ["제주시", "서귀포시"]})],
            "부족한 정보": [call("ask_clarify", {"question": "어떤 가게를 생각하세요?", "reason": "부족",
                                                 "options": ["카페", "반찬가게"]})],
        }
        for case in CASES:
            if case["id"] in replies:
                calls = replies[case["id"]]
                luna_script.append(resp(*calls))
                if calls[0].name != "ask_clarify":
                    luna_script.append(resp(text="네."))
        luna_script += [resp(call("set_conditions", {"business": "카페", "competition": True})), resp(text="네.")]
        second_script += [resp(call("set_conditions", {"business": "카페"})), resp(text="네.")]
        luna, second = self.agent(luna_script), self.agent(second_script, model="gpt-6-compare")
        rows = compare(self.engine, luna, second)
        missed = [r for r in rows if r["primaryMiss"]]
        self.assertEqual([r["id"] for r in missed], ["말하지 않은 조건 금지"])
        self.assertEqual(missed[0]["secondaryMiss"], [])
        self.assertTrue(all("secondary" not in r for r in rows if not r["primaryMiss"]))
        self.assertEqual(len(second._client.responses.requests), 2)


if __name__ == "__main__":
    unittest.main()

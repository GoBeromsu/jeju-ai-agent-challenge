"""v1 수용 테스트: 모델을 켠 경우(가짜 Gemini 응답)와 끈 경우, 허브 유동인구 실시간·스냅샷·없음.

실제 Gemini 호출은 tools/agent_smoke.py 로 따로 확인한다 (키와 모델 가용성이 필요).
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import Request, urlopen

from app.agent import safe_for_model
from app.engine import Engine
from app.hub import HubClient, HubError, Population, aggregate
from app.server import make_server

FIXTURE = Path(__file__).parent / "fixtures" / "pop583_yeondong_page1.json"

try:
    from google.genai import types

    HAVE_GENAI = True
except ImportError:  # pragma: no cover
    HAVE_GENAI = False


def model_turn(*calls: tuple[str, dict], text: str | None = None):
    parts = [types.Part(function_call=types.FunctionCall(name=n, args=a)) for n, a in calls]
    if text:
        parts.append(types.Part(text=text))
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=parts))]
    )


class FakeModels:
    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.requests: list = []

    def generate_content(self, model, contents, config):
        self.requests.append(contents)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, script: list) -> None:
        self.models = FakeModels(script)


class FakeHub(HubClient):
    """허브 응답 모양(fixture)을 그대로 돌려주는 가짜. 성별 인자로 행을 나눈다."""

    def __init__(self, fail: bool = False) -> None:
        super().__init__(project_key="test-key")
        self.fail = fail
        self.fixture = json.loads(FIXTURE.read_text())

    def _get(self, params: dict) -> dict:
        self.calls += 1
        if self.fail:
            raise HubError("허브 호출 실패: 503")
        rows = [r for r in self.fixture["data"] if r["gender"] == params.get("gender", r["gender"])]
        rows = [{**r, "emd": params.get("emd", r["emd"]), "baseDate": params["startDate"]} for r in rows]
        return {"totCnt": len(rows), "hasMore": False, "data": rows}


@unittest.skipUnless(HAVE_GENAI, "google-genai 가 없으면 건너뛴다")
class AgentTest(unittest.TestCase):
    def make(self, script: list, hub: HubClient | None = None):
        from app.agent import Agent

        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "model.jsonl"
        engine = Engine(population=Population(client=hub or HubClient(project_key=""), snapshot_path=Path("/nonexistent")))
        self.agent = Agent(engine, client=FakeClient(script), model="gemini-3.8-flash", log_path=self.log)
        return engine, self.agent

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def assert_log_has_no_store_numbers(self) -> None:
        """모델로 나간 요청에는 동네 이름·남은 후보 수·빠진 조건·조건 이름표만 있어야 한다."""
        allowed_keys = {"candidateNames", "candidateCount", "excludedReasons", "조건", "보여준동네", "candidates", "stage"}
        log = self.log.read_text(encoding="utf-8")
        for line in log.splitlines():
            self.assertLessEqual(set(json.loads(line)), {"t", "model", "note", "turns", "textChars", "tools"})
        self.assertNotIn("사용자", log)  # 로그에는 원문이 없다
        for contents in self.agent.client().models.requests:
            for content in (c.model_dump(mode="json", exclude_none=True) for c in contents):
                for part in content.get("parts", []):
                    fr = part.get("function_response")
                    if fr:
                        resp = fr["response"]
                        self.assertLessEqual(set(resp), allowed_keys)
                        if "candidateCount" in resp:
                            self.assertIsInstance(resp["candidateCount"], int)
                        stripped = {k: v for k, v in resp.items() if k != "candidateCount"}
                        digits = re.findall(r"\d+", json.dumps(stripped, ensure_ascii=False))
                        # 숫자는 '이도2동' 같은 동네 이름 안에서만 나온다
                        self.assertTrue(all(len(d) == 1 for d in digits), digits)
                    if "text" in part and content.get("role") == "user":
                        if part["text"].startswith('{"현재조건"'):
                            payload = json.loads(part["text"])
                            self.assertEqual(set(payload), {"현재조건", "사용자"})
                        else:
                            self.assertTrue(safe_for_model(part["text"]))

    def test_p1_with_llm(self) -> None:
        engine, agent = self.make(
            [
                model_turn(("set_conditions", {"business": "반찬가게", "region": "제주시", "area": "동", "visit": "지나가다"})),
                model_turn(("rank_places", {})),
                model_turn(text="제주시 동 지역에서 단골 기준으로 골랐어요."),
                model_turn(("set_conditions", {"competition": True, "neighbors": True})),
                model_turn(("rank_places", {})),
                model_turn(text="조건에 맞는 동네가 남았어요."),
                model_turn(("show_dong", {"name": "이도2동"})),
                model_turn(text="이도2동으로 갈게요."),
            ]
        )
        r = agent.turn(None, "제주시 시내에서 동네 주민 상대로 반찬가게 하려고요")
        self.assertTrue(r["agent"]["used"])
        self.assertEqual(engine.subcats[r["profile"]["business"]]["name"], "반찬/식료품 소매업")
        self.assertEqual(r["candidates"], [c["code"] for c in r["advice"]["candidates"]])
        self.assertEqual(len(r["candidates"]), 3)  # 단골 기준 후보 세 곳
        r = agent.turn(r["profile"], "같은 가게 적고 마트나 병원 가까운 데로")
        no_llm = engine.turn(None, "제주시 동 지역 반찬가게 지나가다 들르는 손님 경쟁 적은 곳 마트 병원 이웃")
        self.assertEqual(r["candidates"], no_llm["candidates"])  # 모델 경로와 칩 경로가 같은 계산
        r = agent.turn(r["profile"], "이도2동 보여 주세요")
        self.assertEqual(r["card"]["name"], "이도2동")
        same = engine.turn(no_llm["profile"], "", r["card"]["code"])["card"]
        for k in ("total", "anchorTotal", "business", "anchors"):
            self.assertEqual(r["card"][k], same[k], k)
        self.assert_log_has_no_store_numbers()

    def test_p2_with_llm(self) -> None:
        engine, agent = self.make(
            [
                model_turn(("set_conditions", {"business": "카페", "region": "서귀포시", "area": "읍면", "visit": "목적", "competition": True})),
                model_turn(text="서귀포 읍면은 모두 카페가 많아요."),
                model_turn(("set_conditions", {"area": "상관없음"})),
                model_turn(("rank_places", {})),
                model_turn(text="서귀포 동 지역까지 넓혔어요."),
            ]
        )
        r = agent.turn(None, "은퇴하고 서귀포 시골에서만 카페를 하고 싶어요. 경쟁 적은 곳만 보여 주세요.")
        self.assertEqual(r["candidates"], [])
        self.assertIn("같은 업종 비중이 섬 평균보다 높음", r["excludedReasons"])
        r = agent.turn(r["profile"], "그럼 시내도 괜찮아요")
        self.assertEqual(len(r["candidates"]), 3)  # 서귀포 6곳 중 찾아오는 손님 기준 세 곳
        self.assertTrue(all(engine.by_code[c]["sgg"].startswith("서귀포") for c in r["candidates"]))
        self.assert_log_has_no_store_numbers()

    def test_llm_failure_falls_back_to_chips(self) -> None:
        engine, agent = self.make([RuntimeError("503 UNAVAILABLE")])
        r = agent.turn(None, "제주시 동 지역에서 반찬가게")
        self.assertFalse(r["agent"]["used"])
        self.assertIn("정해진 상담 순서로 이어 갈게요", r["message"])
        self.assertEqual(len(r["candidates"]), 19)
        self.assertTrue(r["chips"])

    def test_llm_failure_keeps_the_same_consultation(self) -> None:
        engine, agent = self.make([RuntimeError("504 DEADLINE_EXCEEDED")])
        r = agent.turn(None, "제주에 작은 카페를 열고 싶어요")
        self.assertFalse(r["agent"]["used"])
        self.assertEqual(r["advice"]["stage"], "question")
        self.assertFalse(r["profile"]["competition"])
        self.assertIn("손님이 주로 어떻게 올 것 같으세요?", r["message"])
        self.assertNotIn("포화", r["message"])
        self.assertEqual([c["label"] for c in r["chips"]], ["매일 들르는 단골", "일부러 찾아와 머무는 손님", "아직 모름", "처음부터"])

    def test_population_never_reaches_model(self) -> None:
        engine, agent = self.make(
            [model_turn(("focus_map", {"target": "연동", "level": "dong"})),
             model_turn(("population", {"dong": "연동"})),
             model_turn(text="연동의 관측 자료입니다.")],
            hub=FakeHub(),
        )
        r = agent.turn(None, "연동 보여줘")
        self.assertEqual(r["card"]["population"]["source"], "live")
        self.assertEqual(r["card"]["population"]["kind"], "observed")
        self.assertNotIn("population", r)
        self.assertIn("상권 근거 카드", str(self.agent.client().models.requests))
        self.assertNotIn(str(r["card"]["population"]["resd"]), str(self.agent.client().models.requests))
        logged = self.log.read_text()
        self.assertNotIn(str(r["card"]["population"]["resd"]), logged)
        self.assertNotIn("resdPop", logged)


class PopulationTest(unittest.TestCase):
    def test_aggregate_from_real_response_shape(self) -> None:
        rows = json.loads(FIXTURE.read_text())["data"]
        agg = aggregate(rows)
        self.assertEqual(agg["resd"], sum(r["resdPop"] for r in rows))
        total = agg["resd"] + agg["work"] + agg["visit"]
        self.assertAlmostEqual(agg["residentShare"], round(100 * agg["resd"] / total, 1))

    def test_live_then_snapshot_then_none(self) -> None:
        live = Population(client=FakeHub(), snapshot_path=Path("/nonexistent"))
        engine = Engine(population=live)
        r = engine.turn(None, "연동 보기")
        pop = r["card"]["population"]
        self.assertEqual((pop["dataset"], pop["scope"], pop["source"]), (583, "dong", "live"))
        self.assertRegex(pop["date"], r"^\d{8}$")
        rows = json.loads(FIXTURE.read_text())["data"]
        work = sum(row["workPop"] for row in rows)
        denominator = sum(row["resdPop"] + row["workPop"] + row["visitPop"] for row in rows)
        self.assertEqual(pop["denominator"], denominator)
        self.assertEqual(pop["workShare"], round(100 * work / denominator, 1))
        self.assertEqual(pop["midAgeCount"], sum(row["resdPop"] + row["workPop"] + row["visitPop"]
                                                for row in rows if row["ageGroup"] in ("40대", "50대", "60대")))
        self.assertTrue(next(s for s in r["slots"] if s["key"] == "who")["filled"])

        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / "snap.json"
            snap.write_text(json.dumps({"date": "20260831", "dongs": {"연동": aggregate(FakeHub().fixture["data"])}}))
            fallback = Population(client=FakeHub(fail=True), snapshot_path=snap)
            card = Engine(population=fallback).turn(None, "연동 보기")["card"]
            self.assertEqual(card["population"]["source"], "snapshot")
            self.assertEqual(card["population"]["date"], "20260831")

        none = Engine(population=Population(client=HubClient(project_key=""), snapshot_path=Path("/nonexistent")))
        r = none.turn(None, "연동 보기")
        self.assertIsNone(r["card"]["population"])
        self.assertFalse(next(s for s in r["slots"] if s["key"] == "who")["filled"])

    def test_biz_trend_on_card(self) -> None:
        e = Engine()
        card = e.turn(None, "이도2동 보기")["card"]
        self.assertEqual(card["bizTrend"]["active2018"], 8410)
        self.assertEqual(card["bizTrend"]["active2023"], 9082)
        self.assertEqual(card["bizTrend"]["closed2019"], 1104)
        self.assertEqual((card["bizTrend"]["dataset"], card["bizTrend"]["startYear"],
                          card["bizTrend"]["endYear"], card["bizTrend"]["change"]), (830, 2018, 2023, 672))

    def test_direct_dong_selection_shares_hub_lookup_across_rerenders(self) -> None:
        code = Engine().find_dong_code("연동")
        for fail in (False, True):
            with self.subTest(fail=fail):
                hub = FakeHub(fail=fail)
                population = Population(client=hub, snapshot_path=Path("/nonexistent"))
                with make_server(port=0, population=population, use_llm=False) as server:
                    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
                    thread.start()
                    url = f"http://127.0.0.1:{server.server_port}/api/turn"

                    def select_dong() -> dict:
                        req = Request(url, data=json.dumps({"text": "", "dong": code}).encode())
                        with urlopen(req, timeout=5) as response:
                            return json.load(response)

                    try:
                        with ThreadPoolExecutor(max_workers=4) as pool:
                            results = [job.result() for job in [pool.submit(select_dong) for _ in range(4)]]
                    finally:
                        server.shutdown()
                        thread.join(timeout=5)

                self.assertEqual(hub.calls, 1 if fail else 3)
                for result in results:
                    self.assertFalse(result["agent"]["used"])
                    self.assertNotIn("population", result)
                    pop = result["card"]["population"]
                    if fail:
                        self.assertIsNone(pop)
                    else:
                        self.assertEqual((pop["dataset"], pop["source"], result["card"]["code"]), (583, "live", code))


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAVE_GENAI, "google-genai 가 없으면 건너뛴다")
class OutboundGuardTest(unittest.TestCase):
    def test_pasted_numbers_stay_on_this_pc(self):
        from app.agent import Agent

        with tempfile.TemporaryDirectory() as tmp:
            engine = Engine(population=Population(client=HubClient(project_key=""), snapshot_path=Path("/nonexistent")))
            agent = Agent(engine, client=FakeClient([]), model="gemini-3.8-flash", log_path=Path(tmp) / "m.jsonl")
            out = agent.turn(None, "카페 매출 1200 1500 1800 2100 만원 자료예요")
            self.assertEqual(agent.client().models.requests, [])
            self.assertEqual(out["agent"]["reason"], "바깥 전송 제한")
            self.assertFalse((Path(tmp) / "m.jsonl").exists())

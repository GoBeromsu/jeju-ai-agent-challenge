"""Server-side context isolation tests; all model and exporter calls are fakes."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen

from app.advisor import Consultation
from app.agent import Agent
from app.agent_openai import OpenAIAgent
from app.context_boundary import CENTER_ACK
from app.engine import Engine, Profile
from app.hub import Population
from app.server import make_server

LOCAL_CANARY = "LOCAL-ONLY-CENTER-CANARY-72f1"
HUB_CANARY = "HUB-POPULATION-MUST-NOT-BECOME-CENTER-CANARY-84d0"
HISTORY_CANARY = "RESTRICTED-HISTORY-CANARY-92a4"
USER_CANARY = "RESTRICTED-USER-CANARY-53c6"
SELECTION_CANARY = "SELECTION-CANARY-18b3"
ERROR_CANARY = "RESTRICTED-SDK-ERROR-CANARY-451a"


def function_call(name: str, args: dict):
    return SimpleNamespace(type="function_call", name=name, arguments=json.dumps(args), call_id="call_1")


def response(*items, text: str = "", fail: Exception | None = None):
    if fail is not None:
        return fail
    return SimpleNamespace(
        output=list(items),
        output_text=text,
        usage=SimpleNamespace(input_tokens=25, output_tokens=8),
    )


class CanaryExporter:
    """Fake trace/export sink records spans, payloads, errors, and flushes."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.flushes = 0

    def record(self, payload: object) -> None:
        self.events.append(json.dumps(payload, ensure_ascii=False, default=str))

    def flush(self) -> None:
        self.flushes += 1


class FakeResponses:
    def __init__(self, script: list, exporter: CanaryExporter) -> None:
        self.script = list(script)
        self.exporter = exporter
        self.requests: list[dict] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        self.exporter.record({"span": "model.request", "payload": kwargs})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            self.exporter.record({"span": "model.retry", "payload": kwargs})
            self.exporter.record({"span": "model.error", "error": str(item)})
            self.exporter.flush()
            raise item
        return item


class FakeClient:
    def __init__(self, script: list, exporter: CanaryExporter | None = None) -> None:
        self.exporter = exporter or CanaryExporter()
        self.responses = FakeResponses(script, self.exporter)


class FakeGeminiModels:
    def __init__(self, exporter: CanaryExporter) -> None:
        self.exporter = exporter
        self.requests: list[dict] = []

    def generate_content(self, **kwargs):
        self.requests.append(kwargs)
        self.exporter.record({"span": "gemini.request", "payload": kwargs})
        raise AssertionError("restricted context called Gemini")


class FakeHub:
    enabled = True

    def __init__(self) -> None:
        self.calls = 0

    def dong(self, name: str):
        self.calls += 1
        raise AssertionError("restricted center lookup attempted live Hub access")


class ContextBoundaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "openai.jsonl"
        self.exporter = CanaryExporter()
        self.client = FakeClient(
            [
                response(function_call("set_profile", {"business": "카페"})),
                response(text="자연어 의미를 반영했습니다."),
            ],
            self.exporter,
        )
        self.hub = FakeHub()
        self.population = Population(client=self.hub, snapshot_path=Path(self.tmp.name) / "missing.json")
        self.population.snapshot = {
            "date": "20261119",
            "dongs": {
                "연동": {
                    "resd": 2,
                    "work": 3,
                    "visit": 5,
                    "rows": 1,
                    "byAge": {"40대": 10},
                    "canary": HUB_CANARY,
                }
            },
        }
        self.server = make_server(
            port=0,
            agent_client=self.client,
            population=self.population,
            provider="openai",
            use_llm=True,
        )
        self.boundary = self.server.RequestHandlerClass.func.context_boundary
        self.lookup_modes: list[list[str]] = []

        def center_lookup(_query):
            self.lookup_modes.append(list(self.boundary._contexts.values()))
            return {"status": "found", "rows": [{"count": 7, "canary": LOCAL_CANARY}]}

        self.boundary.center_lookup = center_lookup
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/turn"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def post(self, body: dict) -> dict:
        request = Request(
            self.url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=5) as result:
            return json.load(result)

    def test_restricted_lookup_is_local_and_general_reentry_is_fresh(self) -> None:
        self.client.responses.script = [RuntimeError(ERROR_CANARY)]
        local = self.post(
            {
                "contextMode": "restricted",
                "resetContext": True,
                "centerQuery": {"target": "연동", "kind": "synthetic"},
                "text": f"연동 인구 자료를 확인해 주세요. {USER_CANARY}",
                "profile": {"history": [{"role": "user", "text": HISTORY_CANARY}]},
                "selection": {"type": "store", "dong": "bad", "store": 0, "marker": SELECTION_CANARY},
            }
        )
        restricted_id = local["contextId"]
        self.assertEqual(local["contextMode"], "restricted")
        self.assertTrue(local["contextReset"])
        self.assertEqual(local["centerResult"]["rows"][0]["canary"], LOCAL_CANARY)
        self.assertIsNone(local["card"]["population"])
        self.assertNotIn(HUB_CANARY, json.dumps(local, ensure_ascii=False))
        self.assertTrue(self.lookup_modes)
        self.assertIn("restricted", self.lookup_modes[0])
        self.assertEqual(self.client.responses.requests, [])
        self.assertEqual(self.exporter.events, [])
        self.assertEqual(self.exporter.flushes, 0)
        self.assertEqual(self.hub.calls, 0)

        still_local = self.post(
            {
                "contextId": restricted_id,
                "contextMode": "general",
                "text": "새 일반 상담으로 넘어가고 싶어요.",
                "profile": {"history": [{"role": "user", "text": HISTORY_CANARY}]},
            }
        )
        self.assertEqual((still_local["contextId"], still_local["contextMode"]), (restricted_id, "restricted"))
        self.assertEqual(still_local["centerResult"]["rows"][0]["canary"], LOCAL_CANARY)
        self.assertEqual(len(self.client.responses.requests), 0)
        self.assertEqual(self.exporter.events, [])
        self.assertEqual(self.exporter.flushes, 0)

        reset = self.post(
            {
                "contextId": restricted_id,
                "contextMode": "general",
                "resetContext": True,
                "text": "",
                "profile": {"history": [{"role": "user", "text": HISTORY_CANARY}]},
                "selection": {"type": "store", "dong": "bad", "store": 0, "marker": SELECTION_CANARY},
                "providerThread": HISTORY_CANARY,
            }
        )
        self.assertEqual(reset["contextMode"], "general")
        self.assertTrue(reset["contextReset"])
        self.assertNotEqual(reset["contextId"], restricted_id)
        self.assertIsNone(reset["profile"]["business"])
        self.assertEqual(reset["profile"]["history"], [])

        self.client.responses.script = [
            response(function_call("set_profile", {"business": "카페"})),
            response(text="자연어 의미를 반영했습니다."),
        ]
        general = self.post(
            {
                "contextId": reset["contextId"],
                "contextMode": "general",
                "profile": reset["profile"],
                "text": "로스팅한 원두로 에스프레소를 내리고 싶어요.",
            }
        )
        self.assertEqual(general["contextMode"], "general")
        self.assertFalse(general["contextReset"])
        self.assertEqual(general["profile"]["business"], Engine().find_business("카페"))
        self.assertEqual(general["agent"]["path"], "model")
        self.assertEqual(general["agent"]["modelCalls"], 2)
        self.assertEqual(len(self.client.responses.requests), 2)
        self.assertGreater(len(self.exporter.events), 0)
        captured = json.dumps(self.client.responses.requests, ensure_ascii=False, default=vars)
        exported = "\n".join(self.exporter.events)
        self.assertIn("로스팅한 원두로 에스프레소를 내리고 싶어요.", captured)
        self.assertNotIn(HISTORY_CANARY, captured)
        self.assertNotIn(USER_CANARY, captured)
        self.assertNotIn(SELECTION_CANARY, captured)
        self.assertNotIn(restricted_id, captured)
        self.assertNotIn(HISTORY_CANARY, exported)
        self.assertNotIn(USER_CANARY, exported)
        self.assertNotIn(SELECTION_CANARY, exported)
        self.assertNotIn(ERROR_CANARY, exported)
        self.assertNotIn(restricted_id, exported)
        self.assertNotIn(LOCAL_CANARY, exported)
        self.assertEqual(self.hub.calls, 0)

    def test_source_marker_and_mixed_input_fail_closed(self) -> None:
        out = self.post(
            {
                "contextMode": "general",
                "text": "카페 조건을 해석하고 센터 자료도 함께 확인해 주세요.",
                "centerQuery": {"query": "합성 센터 조회"},
            }
        )
        self.assertEqual(out["contextMode"], "restricted")
        self.assertEqual(self.client.responses.requests, [])
        self.assertEqual(self.exporter.events, [])
        self.assertEqual(out["centerResult"]["rows"][0]["canary"], LOCAL_CANARY)

    def test_missing_center_data_is_not_replaced_with_hub_snapshot(self) -> None:
        self.boundary.center_lookup = lambda _query: None
        out = self.post(
            {
                "contextMode": "general",
                "centerQuery": {"target": "연동"},
                "text": "연동 센터 자료를 확인해 주세요.",
            }
        )

        self.assertEqual(out["contextMode"], "restricted")
        self.assertEqual(out["centerResult"], {"status": "자료 없음"})
        self.assertIsNone(out["card"]["population"])
        self.assertNotIn(HUB_CANARY, json.dumps(out, ensure_ascii=False))
        self.assertEqual(self.hub.calls, 0)
        self.assertEqual(self.client.responses.requests, [])
        self.assertEqual(self.exporter.events, [])

    def test_center_tool_ack_is_constant_for_data_no_data_and_error(self) -> None:
        engine = Engine(population=self.population)
        present = Consultation(engine, Profile(), "")
        engine.center_lookup = lambda _query: {"canary": LOCAL_CANARY}
        present_result = present.call("center_lookup", {"query": {"synthetic": True}})

        absent = Consultation(engine, Profile(), "")
        engine.center_lookup = lambda _query: None
        absent_result = absent.call("center_lookup", {"query": {"synthetic": True}})

        failed = Consultation(engine, Profile(), "")
        engine.center_lookup = lambda _query: (_ for _ in ()).throw(RuntimeError(LOCAL_CANARY))
        failed_result = failed.call("center_lookup", {"query": {"synthetic": True}})

        self.assertEqual(present_result, CENTER_ACK)
        self.assertEqual(absent_result, CENTER_ACK)
        self.assertEqual(failed_result, CENTER_ACK)
        self.assertEqual(present.context["centerResult"], {"canary": LOCAL_CANARY})
        self.assertEqual(absent.context["centerResult"], {"status": "자료 없음"})
        self.assertEqual(failed.context["centerResult"], {"status": "자료 없음"})
        self.assertNotIn(LOCAL_CANARY, json.dumps([present_result, absent_result, failed_result]))

    def test_direct_agent_restricted_mode_does_not_log_or_call_client(self) -> None:
        engine = Engine(population=self.population)
        gemini_models = FakeGeminiModels(self.exporter)
        gemini_client = SimpleNamespace(models=gemini_models)
        gemini = Agent(engine, client=gemini_client, model="gemini-test", log_path=Path(self.tmp.name) / "gemini.jsonl")
        openai = OpenAIAgent(engine, client=self.client, log_path=self.log)

        gemini.turn(None, "일반 응답과 제한 조회를 함께 해 주세요.", context_mode="restricted")
        openai.turn(None, "연동 인구", context_mode="restricted")

        self.assertEqual(self.client.responses.requests, [])
        self.assertEqual(gemini_models.requests, [])
        self.assertFalse(gemini.log_path.exists())
        self.assertFalse(self.log.exists())
        self.assertEqual(self.exporter.events, [])
        self.assertEqual(self.exporter.flushes, 0)


if __name__ == "__main__":
    unittest.main()

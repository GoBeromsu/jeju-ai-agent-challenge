"""Public Vercel adapter: deployment-configured model opt-in, rules fallback, no center/Hub.

All model calls are fakes; nothing here reaches the network or a real key.
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock
from urllib.request import Request, urlopen

import api.index as public_api
from api.index import PublicContextBoundary, PublicModelAgent, build_agent
from app.engine import Engine
from app.hub import HubClient, Population
from app.place import Place


def function_call(name: str, args: dict, i: int = 0):
    return SimpleNamespace(type="function_call", name=name, arguments=json.dumps(args, ensure_ascii=False), call_id=f"call_{i}")


def response(*calls, text: str = ""):
    return SimpleNamespace(output=list(calls), output_text=text, usage=SimpleNamespace(input_tokens=20, output_tokens=7))


class FakeResponses:
    def __init__(self, script: list, now: list[float] | None = None, step: float = 0.0) -> None:
        self.script = list(script)
        self.requests: list[dict] = []
        self.now = now
        self.step = step

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if self.now is not None:
            self.now[0] += self.step
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, script: list, now: list[float] | None = None, step: float = 0.0) -> None:
        self.responses = FakeResponses(script, now, step)


class Factory:
    def __init__(self, client) -> None:
        self.client = client
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, api_key: str, project: str | None):
        self.calls.append((api_key, project))
        return self.client


def engine() -> Engine:
    e = Engine(population=Population(client=HubClient(project_key="")))
    e.population.snapshot = {}
    e.place = Place(e)
    return e


class PublicBuildTest(unittest.TestCase):
    def test_no_deployment_key_means_rules_only_and_never_reads_dotenv(self) -> None:
        e = engine()
        with mock.patch("app.config.get", side_effect=AssertionError("local .env must not configure the public model")):
            agent = build_agent(e, {})
        self.assertFalse(agent.enabled)
        self.assertFalse(agent.configured)
        self.assertIsNone(agent.last_error)
        out = agent.turn(None, "제주에 작은 카페를 열고 싶어요")
        self.assertEqual((out["agent"]["path"], out["agent"]["modelCalls"]), ("rules", 0))
        self.assertEqual(out["agent"]["reason"], "모델 비활성화")

    def test_deployment_key_enables_model_but_bad_model_or_missing_sdk_stays_on_rules(self) -> None:
        e = engine()
        client = FakeClient([response(text="네.")])
        env = {"OPENAI_API_KEY": "sk-deploy", "OPENAI_MODEL": "gpt-6-luna", "OPENAI_PROJECT": "proj_1"}
        factory = Factory(client)
        agent = build_agent(e, env, client_factory=factory)
        self.assertIsInstance(agent, PublicModelAgent)
        self.assertTrue(agent.configured)
        self.assertEqual(factory.calls, [("sk-deploy", "proj_1")])

        factory = Factory(client)
        old_model = build_agent(e, {**env, "OPENAI_MODEL": "gpt-5.6-luna"}, client_factory=factory)
        self.assertFalse(old_model.enabled)
        self.assertEqual((old_model.last_error, factory.calls), ("OPENAI_MODEL", []))

        factory = Factory(client)
        blank_key = build_agent(e, {"OPENAI_API_KEY": "  "}, client_factory=factory)
        self.assertFalse(blank_key.enabled)
        self.assertEqual((blank_key.last_error, factory.calls), (None, []))

        def broken(_key, _project):
            raise ModuleNotFoundError("openai")

        missing_sdk = build_agent(e, env, client_factory=broken)
        self.assertFalse(missing_sdk.enabled)
        self.assertEqual(missing_sdk.last_error, "ModuleNotFoundError")

    def test_env_key_opts_in_and_dotenv_is_never_consulted(self) -> None:
        factory = Factory(FakeClient([response(text="네.")]))
        agent = build_agent(engine(), {"OPENAI_API_KEY": "sk-env"}, client_factory=factory)
        self.assertTrue(agent.enabled)
        self.assertEqual(factory.calls, [("sk-env", None)])
        # Same empty environment, but a local .env would answer `config.get`: it must not be read.
        with mock.patch("app.config.get", side_effect=AssertionError(".env must not configure the public model")):
            rules = build_agent(engine(), {}, client_factory=factory)
        self.assertFalse(rules.enabled)
        self.assertEqual(len(factory.calls), 1)

    def test_handler_status_reports_rules_and_model_configuration(self) -> None:
        # The handler wires the same build_agent(); no SDK call happens here.
        e = engine()
        for env, configured, reason in (({}, False, None), ({"OPENAI_API_KEY": "sk-deploy"}, True, None)):
            agent = build_agent(e, env, client_factory=Factory(FakeClient([response(text="네.")])))
            self.assertEqual((agent.provider, agent.configured, agent.model, agent.last_error),
                             ("openai", configured, "gpt-6-luna", reason))
        self.assertFalse(public_api.handler.offline)
        self.assertEqual(e.population.status(), {"live": False, "snapshotDate": None})


class PublicModelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine()

    def agent(self, script, now=None, step=0.0, clock=None):
        client = FakeClient(script, now, step)
        factory = Factory(client)
        env = {"OPENAI_API_KEY": "sk-deploy"}
        return build_agent(self.engine, env, client_factory=factory, clock=clock or time.monotonic), client

    def test_general_context_uses_model_and_reports_it(self) -> None:
        a, client = self.agent([
            response(function_call("set_profile", {"business": "카페"})),
            response(text="로스팅한 원두로 에스프레소를 내리는 카페로 볼게요."),
        ])
        out = a.turn(None, "로스팅한 원두로 에스프레소를 내리고 싶어요")
        self.assertEqual(out["agent"]["path"], "model")
        self.assertEqual((out["agent"]["provider"], out["agent"]["model"]), ("openai", "gpt-6-luna"))
        self.assertEqual(out["agent"]["modelCalls"], 2)
        self.assertEqual(out["agentReply"], "로스팅한 원두로 에스프레소를 내리는 카페로 볼게요.")
        self.assertEqual([c["name"] for c in out["agent"]["calls"]], ["set_profile"])
        for request in client.responses.requests:
            self.assertIs(request["store"], False)
            self.assertLessEqual(request["timeout"], public_api.REQUEST_TIMEOUT)

    def test_restricted_context_and_pasted_data_never_call_the_model(self) -> None:
        a, client = self.agent([response(text="네.")])
        local = a.turn(None, "연동 센터 자료를 확인해 주세요", context_mode="restricted")
        self.assertEqual((local["agent"]["path"], local["agent"]["modelCalls"]), ("local", 0))
        pasted = a.turn(None, "매출 1200 1500 1800 2100 만원 자료예요")
        self.assertEqual((pasted["agent"]["reason"], pasted["agent"]["modelCalls"]), ("바깥 전송 제한", 0))
        self.assertEqual(client.responses.requests, [])

    def test_turn_budget_bounds_calls_and_falls_back_to_rules(self) -> None:
        now = [0.0]
        a, client = self.agent(
            [response(function_call("set_profile", {"business": "카페"}, i)) for i in range(6)],
            now=now, step=5.0, clock=lambda: now[0],
        )
        out = a.turn(None, "제주에 작은 카페를 열고 싶어요")
        # Four calls fit the 20s budget; each is asked to finish inside what is left.
        self.assertEqual(len(client.responses.requests), 4)
        self.assertEqual([r["timeout"] for r in client.responses.requests], [12, 12, 10, 5])
        self.assertEqual((out["agent"]["path"], out["agent"]["reason"]), ("rules", "모델 호출 실패"))
        self.assertEqual((out["agent"]["error"], out["agent"]["modelCalls"]), ("TimeoutError", 4))
        self.assertTrue(out["message"].startswith("자유 대화를 잠시 쓸 수 없어"))

    def test_exhausted_budget_refuses_the_first_call(self) -> None:
        readings = iter([0.0, 10**9])
        a, client = self.agent([response(text="네.")], clock=lambda: next(readings))
        out = a.turn(None, "제주에 작은 카페를 열고 싶어요")
        self.assertEqual(client.responses.requests, [])
        self.assertEqual((out["agent"]["path"], out["agent"]["modelCalls"]), ("rules", 0))
        self.assertEqual(out["agent"]["error"], "TimeoutError")


class PublicHandlerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = engine()
        self.boundary = PublicContextBoundary()
        self.engine.center_lookup = self.boundary.lookup_center
        self.client = FakeClient([
            response(function_call("set_profile", {"business": "카페"})),
            response(text="카페 상담으로 이어 갈게요."),
        ])
        env = {"OPENAI_API_KEY": "sk-deploy"}
        self.agent = build_agent(self.engine, env, client_factory=Factory(self.client))
        bound = type("BoundPublic", (public_api.handler,), {
            "engine": self.engine, "agent": self.agent, "context_boundary": self.boundary,
        })
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), bound)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def get_json(self, path: str) -> dict:
        with urlopen(self.base + path, timeout=10) as result:
            return json.load(result)

    def post(self, body: dict) -> dict:
        request = Request(self.base + "/api/turn", data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=10) as result:
            return json.load(result)

    def test_status_and_general_turn_through_public_handler(self) -> None:
        status = self.get_json("/api/status")
        self.assertTrue(status["agent"]["configured"])
        self.assertEqual(status["agent"]["provider"], "openai")
        self.assertEqual(status["population"], {"live": False, "snapshotDate": None})

        out = self.post({"contextMode": "general", "resetContext": True, "text": "로스팅한 원두로 에스프레소를 내리고 싶어요"})
        self.assertTrue(out["contextId"].startswith("pub-"))
        self.assertEqual(out["contextMode"], "general")
        self.assertEqual(out["agent"]["path"], "model")
        self.assertEqual(self.engine.subcats[out["profile"]["business"]]["name"], "카페")
        self.assertEqual(len(self.client.responses.requests), 2)
        self.assertEqual(self.engine.population.client.calls, 0)
        self.assertNotIn("sk-deploy", json.dumps(out, ensure_ascii=False))

    def test_restricted_center_turn_is_local_and_never_touches_model_or_hub(self) -> None:
        out = self.post({
            "contextMode": "restricted", "resetContext": True,
            "centerQuery": {"text": "연동"}, "text": "연동 센터 자료를 확인해 주세요",
        })
        self.assertTrue(out["contextId"].startswith("res-"))
        self.assertEqual(out["contextMode"], "restricted")
        self.assertEqual(out["agent"]["used"], False)
        self.assertEqual(out["agent"]["calls"], [])
        self.assertEqual(out["centerResult"], {"status": "자료 없음"})
        self.assertEqual(self.client.responses.requests, [])
        self.assertEqual(self.engine.population.client.calls, 0)

    def test_map_key_is_only_for_public_ids(self) -> None:
        key = self.get_json("/api/map-config?contextId=pub-" + "a" * 20)
        self.assertEqual(set(key), {"javascriptKey"})
        restricted = self.get_json("/api/map-config?contextId=res-" + "a" * 20)
        self.assertIsNone(restricted["javascriptKey"])


if __name__ == "__main__":
    unittest.main()

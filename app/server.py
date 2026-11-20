"""로컬 서버: web/ 정적 파일과 /api 를 한 포트로 내준다. 표준 라이브러리만 쓴다."""

from __future__ import annotations

import argparse
import json
import re
import threading
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import config
from .agent import Agent
from .advisor import Consultation
from .agent_openai import OpenAIAgent
from .consultation import finalize_turn
from .context_boundary import GENERAL, RESTRICTED, ContextBoundary, local_turn, requires_local_context
from .engine import Engine, Profile
from .hub import HubClient, Population
from .place import Place

WEB = Path(__file__).resolve().parent.parent / "web"
MAX_BODY = 64 * 1024


class Handler(SimpleHTTPRequestHandler):
    engine: Engine
    agent: Agent | OpenAIAgent
    context_boundary: ContextBoundary
    offline: bool = False
    # Windows 레지스트리의 확장자 연결을 믿지 않는다 (.js 가 text/plain 이면 ES 모듈이 막힌다)
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript; charset=utf-8",
        ".mjs": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".json": "application/json",
        ".woff2": "font/woff2",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".gz": "application/octet-stream",
        ".bin": "application/octet-stream",
        ".txt": "text/plain; charset=utf-8",
    }

    def guess_type(self, path) -> str:
        ext = Path(str(path)).suffix.lower()
        return self.extensions_map.get(ext) or super().guess_type(path)

    def log_message(self, fmt: str, *args) -> None:  # 콘솔을 조용히
        pass

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def translate_path(self, path: str) -> str:
        route = urlsplit(path).path
        dong = re.fullmatch(r"/dong/(\d{8})", route)
        if route == "/" or (dong and dong.group(1) in self.engine.by_code):
            return super().translate_path("/studio.html")
        return super().translate_path(path)

    def context_is_public_general(self, context_id: str) -> bool:
        """이 ID가 브라우저 공개 설정(카카오 JS 키)을 받을 수 있는지 본다.

        조회만 한다. 문맥을 만들거나 알 수 없는 ID를 승격하지 않는다.
        배포판처럼 문맥을 프로세스에 두지 않는 곳은 이 훅을 덮어쓴다.
        """
        with self.context_boundary._lock:
            return self.context_boundary._contexts.get(context_id) == GENERAL

    def _json(self, status: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/api/map-config":
            javascript_key = None
            try:
                query = parse_qs(url.query, keep_blank_values=True, max_num_fields=1)
                context_ids = query.get("contextId", [])
                if set(query) == {"contextId"} and len(context_ids) == 1:
                    context_id = context_ids[0]
                    if context_id and len(context_id) <= 128 and not self.offline:
                        if self.context_is_public_general(context_id):
                            value = config.get("KAKAO_MAP_JS_KEY")
                            javascript_key = value.strip() if value and value.strip() else None
            except ValueError:
                pass
            self._json(200, {"javascriptKey": javascript_key})
            return
        if url.path == "/api/site-radius":
            try:
                query = parse_qs(url.query, keep_blank_values=True, max_num_fields=3)
                if not {"x", "z"} <= query.keys() <= {"x", "z", "business"} or any(len(v) != 1 for v in query.values()):
                    raise ValueError("site-radius query")
                point = (float(query["x"][0]), float(query["z"][0]))
                business = int(query["business"][0]) if "business" in query else None
                if business is not None and not 0 <= business < len(self.engine.subcats):
                    raise ValueError("business")
                stats = self.engine.place.site_radius(Profile(business=business), point)
            except ValueError:
                self._json(400, {"error": "유효한 x, z 및 업종 값이 필요합니다"})
                return
            self._json(200, {"siteRadius": stats})
            return
        if self.path == "/api/population":
            # 저장해 둔 스냅샷만 준다 (실시간은 동네를 열 때 카드로)
            snap = self.engine.population.snapshot if self.engine.population else {}
            self._json(200, {"date": snap.get("date"), "dongs": snap.get("dongs", {})})
            return
        if self.path == "/api/dongs":
            self._json(200, {"dongs": self.engine.place.dong_list(), "storeDate": self.engine.meta["storeDate"]})
            return
        if self.path == "/api/status":
            self._json(
                200,
                {
                    "agent": {"provider": getattr(self.agent, "provider", "gemini"), "configured": self.agent.configured, "model": self.agent.model, "lastError": self.agent.last_error},
                    "population": self.engine.population.status(),
                },
            )
            return
        super().do_GET()

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            self._json(413, {"error": "요청이 너무 큽니다"})
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, RecursionError):  # 깨진 JSON·UTF-8, 너무 깊은 중첩, 너무 긴 정수
            self._json(400, {"error": "JSON이 아닙니다"})
            return
        if not isinstance(body, dict) or not isinstance(body.get("text", ""), str):
            self._json(400, {"error": "요청 모양이 맞지 않습니다"})
            return
        reset = body.get("resetContext", False)
        if type(reset) is not bool:
            self._json(400, {"error": "요청 모양이 맞지 않습니다"})
            return
        if not reset:
            if (
                not isinstance(body.get("profile", {}) if body.get("profile") is not None else {}, dict)
                or not isinstance(body.get("selection", {}) if body.get("selection") is not None else {}, dict)
            ):
                self._json(400, {"error": "요청 모양이 맞지 않습니다"})
                return
            business = (body.get("profile") or {}).get("business")
            if self.path != "/api/turn" and business is not None and (type(business) is not int or not 0 <= business < len(self.engine.subcats)):
                self._json(400, {"error": "없는 업종입니다"})
                return
        try:
            self._post(body)
        except (KeyError, ValueError, TypeError, IndexError, OverflowError, AttributeError):
            self._json(400, {"error": "요청 값이 맞지 않습니다"})

    def _post(self, body: dict) -> None:
        place = self.engine.place
        if self.path == "/api/store":
            try:
                out = place.store(Profile.from_dict(body.get("profile")), body["dong"], int(body["idx"]))
            except (KeyError, ValueError, TypeError):
                self._json(400, {"error": "이 동네에 없는 점포입니다"})
                return
            self._json(200, out)
            return
        if self.path == "/api/walk":
            from .walk import plan

            dest = body.get("dest")
            try:
                out = plan(self.engine.walk, place, body["dong"], int(body["store"]), None if dest is None else int(dest), body.get("stops"))
            except (KeyError, ValueError, TypeError):
                self._json(400, {"error": "dong, store 값이 필요합니다"})
                return
            self._json(200, out)
            return
        if self.path == "/api/nearby":
            try:
                out = place.nearby(Profile.from_dict(body.get("profile")), body["dong"], float(body["x"]), float(body["z"]))
            except (KeyError, ValueError, TypeError):
                self._json(400, {"error": "dong, x, z 값이 필요합니다"})
                return
            self._json(200, out)
            return
        if self.path == "/api/turn":
            text = body.get("text", "").strip()
            reset = body.get("resetContext", False)
            mode = body.get("contextMode", GENERAL)
            context = self.context_boundary.enter(
                body.get("contextId"), mode, reset=reset, force_local=requires_local_context(body)
            )
            profile = Profile() if context.reset or (context.fresh and context.mode == RESTRICTED) else Profile.from_dict(body.get("profile"))
            map_focus = None if context.reset else body.get("mapFocus")
            if map_focus is None and not context.reset and body.get("dong"):
                map_focus = {"level": "dong", "target": body["dong"]}
            selection = {} if context.reset else dict(body.get("selection") or {})

            if context.mode == RESTRICTED:
                center_query = body.get("centerQuery")
                if center_query is None or center_query is False:
                    center_query = body.get("restrictedLookup")
                if center_query is None and body.get("dataSource") in ("center", "restricted-center"):
                    center_query = {"source": body["dataSource"], "text": text}
                if center_query is None:
                    center_query = {"text": text}
                out = local_turn(
                    self.engine, vars(profile), text, selection=selection, map_focus=map_focus,
                    dong=None if context.reset else body.get("dong"), center_query=center_query,
                )
            else:
                business = profile.business
                if business is not None and not 0 <= business < len(self.engine.subcats):
                    raise ValueError("없는 업종입니다")
                if map_focus is not None:
                    if not isinstance(map_focus, dict):
                        raise TypeError("mapFocus")
                    Consultation(self.engine, profile, "").focus_map(map_focus.get("target", ""), map_focus["level"])
                if text:
                    code = selection.get("dong") or profile.focus
                    if selection.get("type") == "store":
                        idx = selection.get("store")
                        if not code or type(idx) is not int or idx not in self.engine.place.details(code):
                            raise ValueError("이 동네 점포가 아닙니다.")
                        x, z = self.engine.place.xz[idx]
                        selection.update(dong=code, idx=idx, x=x, z=z)
                    out = self.agent.turn(vars(profile), text, selection=selection, context_mode=context.mode)
                elif map_focus is not None:
                    out = self.engine.respond(profile, [], "", model_controlled=True)
                    out["agent"].update(reason="지도 이동")
                else:
                    out = self.engine.turn(vars(profile))

            out = finalize_turn(out, text, out.get("profile", vars(profile)))
            out["contextId"] = context.context_id
            out["contextMode"] = context.mode
            out["contextReset"] = context.reset
            self._json(200, out)
        else:
            self._json(404, {"error": "없는 주소"})


def make_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    agent_client=None,
    population=None,
    use_llm: bool = True,
    provider: str = "openai",
    offline: bool = False,
) -> ThreadingHTTPServer:
    """offline 이면 모델도 허브도 부르지 않는다 (키가 있어도 바깥으로 나가는 호출 없음)."""
    if offline:  # 넘겨받은 자료원보다 우선한다
        use_llm = False
        population = Population(client=HubClient(project_key=""))
    engine = Engine(population=population or Population())
    engine.place = Place(engine)
    if provider == "openai":
        agent = OpenAIAgent(engine, client=agent_client, enabled=use_llm)
    else:
        agent = Agent(engine, client=agent_client, enabled=use_llm)
    # 서버마다 따로 묶어, 한 프로세스에 서버가 둘이어도 설정이 섞이지 않게 한다
    context_boundary = ContextBoundary()
    engine.center_lookup = context_boundary.lookup_center
    bound = type("BoundHandler", (Handler,), {"engine": engine, "agent": agent, "context_boundary": context_boundary, "offline": offline})
    handler = partial(bound, directory=str(WEB))
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="제주 동네 답사 에이전트")
    parser.add_argument("--host", default="127.0.0.1", help="도커 안에서는 0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-llm", action="store_true", help="모델 없이 낱말 읽기와 칩으로만 동작")
    parser.add_argument("--offline", action="store_true", help="모델·허브 모두 부르지 않음 (바깥 호출 없음)")
    parser.add_argument("--provider", choices=["openai", "gemini"], default="openai", help="기본 상담 모델: openai")
    args = parser.parse_args()
    server = make_server(host=args.host, port=args.port, use_llm=not args.no_llm, provider=args.provider, offline=args.offline)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"제주 동네 답사 에이전트가 켜졌습니다: {url}")
    print("끄려면 이 창을 닫으세요.")
    if not args.no_browser:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

import unittest

from app.engine import Engine
from app.place import Place


class PlaceGuardTest(unittest.TestCase):
    def test_unknown_dong_code_never_becomes_a_file_path(self):
        place = Place(Engine())
        for code in ["../../etc/passwd", "C:/Windows/win.ini", r"\\server\share\x"]:
            with self.assertRaises(KeyError):
                place.details(code)
        self.assertEqual(len(place.details("50130320")), 1326)


class RequestShapeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import json
        import threading
        import urllib.request

        from app.hub import HubClient, Population
        from app.server import make_server

        cls.json, cls.req = json, urllib.request
        cls.server = make_server(port=0, population=Population(client=HubClient(project_key="")), use_llm=False)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/api/turn"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def post(self, body) -> tuple[int, dict]:
        r = self.req.Request(self.url, data=self.json.dumps(body).encode(), method="POST")
        try:
            with self.req.urlopen(r) as resp:
                return resp.status, self.json.load(resp)
        except self.req.HTTPError as e:
            with e:
                return e.code, self.json.load(e)

    def test_wrong_shapes_are_400_and_nulls_are_fine(self):
        for bad in ([], "", 0, False):
            self.assertEqual(self.post({"text": "카페", "profile": bad})[0], 400, bad)
            self.assertEqual(self.post({"text": "여기 왜?", "selection": bad})[0], 400, bad)
        self.assertEqual(self.post({"text": "카페", "profile": None, "selection": None})[0], 200)

    def test_long_paste_and_many_turns_keep_working(self):
        code, out = self.post({"text": "카페", "chip": True})
        self.assertEqual(code, 200)
        profile = out["profile"]
        code, out = self.post({"text": "가" * 2001, "profile": profile})
        self.assertEqual(code, 200)
        for _ in range(205):
            code, out = self.post({"text": "아직 모르겠어요", "profile": out["profile"]})
            self.assertEqual(code, 200)

    def test_decoder_limits_and_foreign_store_are_400(self):
        for raw in (b'{"text": "' + b"[" * 30000 + b'"}', b"[" * 30000, b'{"n": ' + b"9" * 5000 + b"}"):
            r = self.req.Request(self.url, data=raw[:60000], method="POST")
            try:
                with self.req.urlopen(r) as resp:
                    resp.read()
                    code = resp.status
            except self.req.HTTPError as e:
                e.read()
                code = e.code
            self.assertIn(code, (200, 400), raw[:20])
        sel = {"type": "store", "dong": "50130320", "store": 1}  # 1번 점포는 표선면이 아니다
        self.assertEqual(self.post({"text": "여기 왜 표시됐어요?", "selection": sel})[0], 400)
        sel["store"] = 53823
        self.assertEqual(self.post({"text": "여기 왜 표시됐어요?", "selection": sel})[0], 200)

    def test_scripts_are_served_as_javascript(self):
        base = self.url.rsplit("/api/", 1)[0]
        for path, kind in (("/main.js", "text/javascript"), ("/vendor/three.module.js", "text/javascript"), ("/style.css", "text/css")):
            with self.req.urlopen(base + path) as resp:
                resp.read()
                self.assertTrue(resp.headers["Content-Type"].startswith(kind), path)

    def test_site_radius_matches_documented_sites_with_shipped_stores(self):
        base = self.url.rsplit("/api/", 1)[0] + "/api/site-radius"
        for x, z, expected in (
            (-22216.63912592888, -9128.24, [(18, 58), (19, 64), (28, 132)]),
            (11061.841238433644, -18033.84, [(12, 130), (22, 217), (33, 327)]),
        ):
            with self.req.urlopen(f"{base}?x={x}&z={z}&business=2") as resp:
                site = self.json.load(resp)["siteRadius"]
            self.assertEqual((site["dataset"], site["date"], site["scope"]), (793, "2025-09-18", "site"))
            self.assertEqual([(r["numerator"], r["denominator"]) for r in site["radii"]], expected)

    def test_site_radius_distinguishes_no_business_from_zero_denominator(self):
        base = self.url.rsplit("/api/", 1)[0] + "/api/site-radius?x=0&z=100000"
        with self.req.urlopen(base) as resp:
            self.assertIsNone(self.json.load(resp)["siteRadius"])
        with self.req.urlopen(base + "&business=2") as resp:
            rows = self.json.load(resp)["siteRadius"]["radii"]
        self.assertEqual([(r["numerator"], r["denominator"], r["share"]) for r in rows], [(0, 0, None)] * 3)

    def test_site_radius_rejects_invalid_query(self):
        base = self.url.rsplit("/api/", 1)[0] + "/api/site-radius"
        for query in ("x=0", "x=nan&z=0", "x=0&z=inf", "x=0&z=0&business=-1", "x=0&x=1&z=0"):
            with self.subTest(query=query), self.assertRaises(self.req.HTTPError) as error:
                self.req.urlopen(f"{base}?{query}")
            with error.exception as response:
                self.assertEqual(response.code, 400)

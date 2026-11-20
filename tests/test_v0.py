"""v0 수용 테스트: P1·P2 시나리오를 칩만으로 끝까지 밟고, 카드 숫자를 원본 CSV와 맞춘다.

    python -m unittest discover -s tests
"""

from __future__ import annotations

import csv
import json
import threading
import unittest
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

from app.consultation import finalize_turn
from app.engine import ANCHOR_PATTERN, Engine, Profile
from app.server import make_server

ROOT = Path(__file__).resolve().parent.parent
RAW_STORE = ROOT / "data" / "raw" / "hub" / "store_793.csv"


def chip(result: dict, label_part: str) -> str:
    for c in result["chips"]:
        if label_part in c["label"]:
            return c["text"]
    raise AssertionError(f"칩 '{label_part}' 없음: {[c['label'] for c in result['chips']]}")


class ScenarioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.e = Engine()

    def run_p1(self) -> dict:
        e = self.e
        r = e.turn(None, "제주시 동 지역에서 반찬가게를 하고 싶어요")
        self.assertEqual(e.subcats[r["profile"]["business"]]["name"], "반찬/식료품 소매업")
        self.assertEqual(r["advice"]["stage"], "question")
        r = e.turn(r["profile"], chip(r, "매일 들르는 단골"))
        self.assertEqual(r["advice"]["stage"], "candidates")
        self.assertTrue(2 <= len(r["candidates"]) <= 3)
        first = r["candidateNames"][0]
        return e.turn(r["profile"], chip(r, f"A {first}"))

    def test_p1_reaches_card(self) -> None:
        r = self.run_p1()
        self.assertIn("card", r)
        self.assertEqual(r["scene"]["fly"], r["profile"]["focus"])
        self.assertEqual(r["card"]["excludedBy"], [])

    def test_first_sentence_asks_one_life_question_without_competition(self) -> None:
        r = self.e.turn(None, "제주에 작은 카페를 열고 싶어요")
        self.assertFalse(r["profile"]["competition"])
        self.assertEqual(r["advice"]["stage"], "question")
        self.assertEqual([c["label"] for c in r["chips"]][:3], ["매일 들르는 단골", "일부러 찾아와 머무는 손님", "아직 모름"])
        self.assertNotIn("포화", r["message"])
        self.assertNotIn("43", r["message"])

    def test_answers_change_metrics_question_and_candidates(self) -> None:
        e = self.e
        p = e.turn(None, "제주에 작은 카페를 열고 싶어요")["profile"]
        a = e.turn(p, "매일 들르는 단골 손님")
        b = e.turn(p, "일부러 찾아와 머무는 손님")
        u = e.turn(p, "아직 모르겠어요")
        self.assertNotEqual(a["advice"]["metricKeys"], b["advice"]["metricKeys"])
        self.assertNotEqual(a["advice"]["question"]["id"], b["advice"]["question"]["id"])
        self.assertNotEqual(a["candidates"], b["candidates"])
        self.assertEqual(u["advice"]["stage"], "compare")
        self.assertEqual(len(u["candidates"]), 2)
        self.assertEqual(u["candidates"][0], a["candidates"][0])
        self.assertEqual(u["candidates"][1], b["candidates"][0])
        for r in (a, b, u):
            self.assertFalse(r["profile"]["competition"])
            text = json.dumps(r["advice"], ensure_ascii=False)
            for word in ("유동", "임대료", "매출"):
                self.assertNotIn(word, text)

    def test_letter_in_chat_opens_same_candidate(self) -> None:
        e = self.e
        p = e.turn(None, "제주에 작은 카페를 열고 싶어요")["profile"]
        r = e.turn(p, "일부러 찾아와 머무는 손님")
        b = e.turn(r["profile"], "B 후보 보기")
        self.assertEqual(b["profile"]["focus"], r["advice"]["candidates"][1]["code"])

    def test_p2_location_and_competition_preferences_use_shared_rank(self) -> None:
        e = self.e
        text = "서귀포 읍면에서 카페를 하려고 해요. 경쟁 적은 곳이 더 좋아요."
        raw = e.turn(None, text)
        r = finalize_turn(raw, text, raw["profile"])
        self.assertEqual(e.subcats[r["profile"]["business"]]["name"], "카페")
        self.assertEqual(r["profile"]["decisionPreference"], "subShare")
        self.assertEqual(r["profile"]["requiredConditions"], [])
        expected = e.rank_consultation(Profile.from_dict(r["profile"]), "subShare")
        self.assertEqual(r["candidates"], [row["id"] for row in expected])
        self.assertEqual(len(r["candidates"]), 3)
        self.assertEqual(r["alternatives"], [])

        first = r["candidateNames"][0]
        raw = e.turn(r["profile"], f"{first} 보기")
        r = finalize_turn(raw, f"{first} 보기", raw["profile"])
        self.assertEqual(r["card"]["name"], first)
        self.assertEqual(r["card"]["excludedBy"], [])

    def test_model_summary_has_no_numbers_of_stores(self) -> None:
        r = self.run_p1()
        summary = self.e.summary_for_model(r)
        self.assertEqual(set(summary), {"candidateNames", "candidateCount", "excludedReasons"})
        dumped = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn(str(r["card"]["total"]), dumped)

    def test_reset(self) -> None:
        r = self.run_p1()
        r = self.e.turn(r["profile"], "처음부터")
        self.assertIsNone(r["profile"]["business"])
        self.assertEqual(len(r["candidates"]), 43)


@unittest.skipUnless(RAW_STORE.exists(), "원본 상가 CSV가 없으면 건너뛴다")
class CardMatchesRawCsvTest(unittest.TestCase):
    """카드 숫자는 원본 CSV를 따로 센 값과 같아야 한다."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.e = Engine()
        with RAW_STORE.open(encoding="cp949", newline="") as f:
            rows = list(csv.DictReader(f))
        cls.total = Counter(r["행정동코드"] for r in rows)
        cls.by_sub = Counter((r["행정동코드"], r["상권업종소분류명"]) for r in rows)
        cls.anchors = Counter(r["행정동코드"] for r in rows if ANCHOR_PATTERN.search(r["상권업종소분류명"]))

    def test_every_dong_every_card(self) -> None:
        for biz_text, biz_name in (("반찬가게", "반찬/식료품 소매업"), ("카페", "카페"), ("미용실", "미용실")):
            r = self.e.turn(None, biz_text)
            for d in self.e.dongs:
                card = self.e.card(Profile.from_dict(r["profile"]), d["code"])
                self.assertEqual(card["total"], self.total[d["code"]], d["name"])
                self.assertEqual(card["business"]["count"], self.by_sub[(d["code"], biz_name)], (d["name"], biz_name))
                self.assertEqual(card["anchorTotal"], self.anchors[d["code"]], d["name"])


class ServerTest(unittest.TestCase):
    def test_turn_endpoint_and_static(self) -> None:
        server = make_server(port=0, offline=True)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/turn",
                data=json.dumps({"text": "반찬가게"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            body = json.loads(urllib.request.urlopen(req).read())
            self.assertEqual(body["notebook"]["state"], "question")
            self.assertTrue(body["notebook"]["question"])
            self.assertEqual(body["candidates"], [item["id"] for item in body["notebook"]["results"]])
            self.assertLessEqual(len(body["candidates"]), 3)
            root = f"http://127.0.0.1:{port}/"
            with urllib.request.urlopen(root) as response:
                self.assertEqual(response.geturl(), root)
                html = response.read()
            with urllib.request.urlopen(root + "studio.html?dong=50130320") as direct:
                self.assertEqual(html, direct.read())
            with urllib.request.urlopen(root + "dong/50130320") as direct:
                self.assertEqual(html, direct.read())
            with self.assertRaises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(root + "dong/00000000")
            self.assertEqual(missing.exception.code, 404)
            self.assertIn(b"studio-client.js", html)
            meta = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/data/meta.json").read())
            self.assertEqual(meta["storeCount"], 57865)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()

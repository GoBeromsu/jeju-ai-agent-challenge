"""가정 보행 미리보기: 실제 정류장 → 가정한 목적지 경로와 '가게 옆을 지나는가' 판정."""

from __future__ import annotations

import unittest

from app.engine import Engine
from app.place import Place
from app.walk import NEAR_STORE_M, Walk, plan

PYOSEON = "50130320"


class WalkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = Engine()
        cls.place = Place(cls.engine)
        cls.walk = Walk()
        det = cls.place.details(PYOSEON)
        cls.store = next(i for i, r in det.items() if r[1] == "육장갈비")
        cls.hanaro = next(i for i, r in det.items() if r[1] == "표선농협하나로마트")

    def test_default_destination_is_assumed_hanaro_and_routes_are_real_stops(self) -> None:
        p = plan(self.walk, self.place, PYOSEON, self.store)
        self.assertTrue(p["supported"])
        self.assertEqual(p["dest"]["idx"], self.hanaro)
        self.assertTrue(p["dest"]["assumed"])
        self.assertIn("관측이 아니고", p["assumption"])
        g = self.walk.graphs[PYOSEON]
        for r in p["routes"]:
            self.assertIn(r["stop"]["ref"], g.by_ref)  # OSM 에 있는 정류소 번호
            self.assertTrue(r["ok"])
            self.assertGreater(r["lengthM"], 0)
            self.assertEqual(r["path"][0], [round(c, 1) for c in g.nodes[g.snap(r["stop"]["x"], r["stop"]["z"])[0]]])

    def test_store_verdict_is_withheld_when_store_point_is_off_road(self) -> None:
        p = plan(self.walk, self.place, PYOSEON, self.store)
        for r in p["routes"]:
            st = r["store"]
            if st["snapM"] > NEAR_STORE_M:
                self.assertIsNone(st["passes"])
            else:
                self.assertEqual(st["passes"], st["distanceM"] <= NEAR_STORE_M)

    def test_changing_destination_and_stop_changes_route(self) -> None:
        a = plan(self.walk, self.place, PYOSEON, self.store)
        other = a["destOptions"][1]["idx"]
        b = plan(self.walk, self.place, PYOSEON, self.store, dest=other)
        self.assertEqual(b["dest"]["idx"], other)
        self.assertNotEqual(a["routes"][0]["path"], b["routes"][0]["path"])
        ref = a["stopOptions"][-1]["ref"]
        c = plan(self.walk, self.place, PYOSEON, self.store, stop_refs=[ref])
        self.assertEqual([r["stop"]["ref"] for r in c["routes"]], [ref])

    def test_other_dong_is_reported_unsupported(self) -> None:
        code = next(d["code"] for d in self.engine.dongs if d["code"] != PYOSEON)
        idx = next(iter(self.place.details(code)))
        p = plan(self.walk, self.place, code, idx)
        self.assertFalse(p["supported"])
        self.assertIn("표선면만", p["why"])


if __name__ == "__main__":
    unittest.main()

"""동네·점포 단위 답: 동네 목록, 동네 첫 화면, 점포 하나, 건물 주변 점포, '여기/이 가게' 질문.

숫자와 이름은 모두 상가(상권) 정보에서 바로 센다. 건물과 점포는 서로 다른 자료(OSM 건물 윤곽,
상가 좌표)라서 어느 건물에 어느 가게가 들어 있는지는 확인하지 못했다. 그래서 건물을 누르면
'주변 점포'라고 밝히고 보여 준다.
"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path
from typing import TypedDict

from .engine import DATA, Engine, Profile

STORE_RECORD = "<ffHHBB"
SITE_RADII_M = (300, 500, 1000)
EARTH_RADIUS_M = 6_371_000
RADIUS_LIMITS = tuple(math.sin(r / (2 * EARTH_RADIUS_M)) ** 2 for r in SITE_RADII_M)
NEARBY_M = 35.0  # 건물을 눌렀을 때 주변 점포로 보는 거리
DIRECTIONS = ["동쪽", "북동쪽", "북쪽", "북서쪽", "서쪽", "남서쪽", "남쪽", "남동쪽"]
WHY_WORDS = ("왜", "이유", "무슨 뜻", "뭐예요", "뭔가요", "어떤 의미")
NEAR_WORDS = ("근처", "주변", "옆", "가까운", "어디", "몇 곳", "몇 개", "얼마나")


class RadiusCount(TypedDict):
    radiusM: int
    numerator: int
    denominator: int
    share: float | None


class SiteBusiness(TypedDict):
    id: int
    name: str
    majorCategory: str


class SiteRadius(TypedDict):
    dataset: int
    source: str
    date: str
    scope: str
    distance: str
    center: dict[str, float]
    business: SiteBusiness
    radii: list[RadiusCount]


def josa(word: str, pair: tuple[str, str]) -> str:
    """받침에 따라 은/는, 이/가, 을/를 을 고른다."""
    ch = word[-1]
    if not ("가" <= ch <= "힣"):
        return word + pair[1]
    return word + (pair[0] if (ord(ch) - 0xAC00) % 28 else pair[1])


class Place:
    def __init__(self, engine: Engine, data_dir: Path = DATA) -> None:
        self.e = engine
        self.data_dir = data_dir
        buf = (data_dir / "stores.bin").read_bytes()
        self.xz: list[tuple[float, float]] = []
        self._sites: list[list[tuple[float, float, float, int]]] = [[] for _ in self.e.meta["categories"]]
        lon0, lat0 = self.e.meta["origin"]
        self._origin = (math.radians(lat0), math.radians(lon0))
        self._lat_per_m = math.radians(1 / 111_320)
        self._lon_per_m = self._lat_per_m / math.cos(self._origin[0])
        for x, z, _, sub, major, _ in struct.iter_unpack(STORE_RECORD, buf):
            self.xz.append((x, z))
            lat, lon = self._lat_lon((x, z))
            self._sites[major].append((lat, lon, math.cos(lat), sub))
        self._detail: dict[str, dict[int, list]] = {}

    def _lat_lon(self, point: tuple[float, float]) -> tuple[float, float]:
        """Invert the store build projection (x east, z south) into radians."""
        x, z = point
        return self._origin[0] - z * self._lat_per_m, self._origin[1] + x * self._lon_per_m

    def site_radius(self, profile: Profile, point: tuple[float, float]) -> SiteRadius | None:
        """Count a great-circle radius without clipping to an administrative boundary."""
        lat, lon = self._lat_lon(point)
        if not (-math.pi / 2 <= lat <= math.pi / 2 and -math.pi <= lon <= math.pi):
            raise ValueError("유효한 지도 좌표가 아닙니다")
        sub = profile.business
        if sub is None:
            return None
        category = self.e.subcats[sub]
        major = self.e.meta["categories"].index(category["cat"])
        matches = [0] * len(SITE_RADII_M)
        totals = [0] * len(SITE_RADII_M)
        cos_lat = math.cos(lat)
        for slat, slon, store_cos_lat, store_sub in self._sites[major]:
            half_arc = math.sin((slat - lat) / 2) ** 2 + cos_lat * store_cos_lat * math.sin((slon - lon) / 2) ** 2
            for i, limit in enumerate(RADIUS_LIMITS):
                if half_arc <= limit:
                    totals[i] += 1
                    matches[i] += store_sub == sub
        return {
            "dataset": 793,
            "source": "제주데이터허브 상가(상권) 정보",
            "date": self.e.meta["storeDate"],
            "scope": "site",
            "distance": "straight-line",
            "center": {"x": point[0], "z": point[1]},
            "business": {"id": sub, "name": category["name"], "majorCategory": category["cat"]},
            "radii": [
                {"radiusM": radius, "numerator": n, "denominator": total, "share": 100 * n / total if total else None}
                for radius, n, total in zip(SITE_RADII_M, matches, totals)
            ],
        }

    def building_dong(self, target: str) -> str | None:
        """정적 지도 윤곽 인덱스 또는 등록 점포 위치를 동네로 해석한다."""
        code, _, ref = target.partition(":")
        layer, _, index = ref.partition(":")
        if code not in self.e.by_code or not index.isascii() or not index.isdecimal() or len(index) > 8:
            return None
        idx = int(index)
        if index != str(idx):
            return None
        if layer == "store":
            return code if idx in self.details(code) else None
        if code == "50130320" and layer in ("new", "old"):
            footprints = self.data_dir.parent / "studio-footprints.json"
            if footprints.is_file() and idx < len(json.loads(footprints.read_text(encoding="utf-8"))[layer if layer == "old" else "features"]):
                return code
        return None
    # ---------- 동네 ----------
    def direction(self, dong: dict) -> str:
        """본섬을 동서 40km·남북 19km 타원으로 보고 점포가 몰린 곳의 방향을 말한다 (원점 126.55E 33.38N)."""
        x, z = dong["storeCenter"]
        nx, nz = x / 40000, -z / 19000  # 북쪽이 +
        if math.hypot(nx, nz) < 0.3:
            return "섬 가운데"
        if nz > 1.6:
            return "섬 북쪽 바다 (추자도)"
        k = round(math.atan2(nz, nx) / (math.pi / 4)) % 8
        return "섬 " + DIRECTIONS[k]

    def dong_list(self) -> list[dict]:
        out = []
        for d in self.e.dongs:
            out.append(
                {
                    "code": d["code"],
                    "name": d["name"],
                    "sgg": d["sgg"],
                    "kind": "읍·면" if self.e.is_eup_myeon(d) else "동",
                    "direction": self.direction(d),
                    "total": d["total"],
                }
            )
        out.sort(key=lambda x: (x["sgg"] != "제주시", x["kind"] != "동", x["name"]))
        return out

    def overview_message(self, profile: Profile, code: str) -> str:
        d = self.e.by_code[code]
        date = self.e.meta["storeDate"]
        # 숫자는 카드에만 둔다. 말풍선은 무엇을 보고 다음에 무엇을 할지만 말한다.
        particle = josa(d["name"], ("을", "를"))[len(d["name"]) :]
        head = f"{d['name']}({d['sgg']}, {self.direction(d)}){particle} 열었어요."
        if profile.business is None:
            top = sorted(d["sub"].items(), key=lambda kv: -kv[1])[:3]
            tops = ", ".join(f"{self.e.subcats[int(k)]['name']} {v}곳" for k, v in top)
            return (
                f"{head} 점포 수는 카드에 있어요({date} 기준). 많은 업종은 {tops}이에요. "
                "어떤 가게를 생각하고 계세요? 아래에서 고르시거나, 지도를 확대해 점포를 눌러 보셔도 돼요."
            )
        name = self.e.subcats[profile.business]["name"]
        return (
            f"{head} 점포 수와 {name} 비중은 카드에 적었어요({date} 기준). "
            f"지도 위 '점포 색' 안내에서 '내 업종' 색으로 칠한 점이 {name} 가게예요. 확대해서 눌러 보세요."
        )

    # ---------- 점포 ----------
    def details(self, code: str) -> dict[int, list]:
        if code not in self._detail:
            if code not in self.e.by_code:  # 파일 이름으로 쓰기 전에 알려진 동네 코드만 허용
                raise KeyError(code)
            rows = json.loads((self.data_dir / "stores" / f"{code}.json").read_text(encoding="utf-8"))
            self._detail[code] = {r[0]: r for r in rows}
        return self._detail[code]

    def relation(self, profile: Profile, sub: int) -> str:
        if profile.business is not None and sub == profile.business:
            return "same"
        if sub in self.e.anchor_subs:
            return "anchor"
        if profile.business is not None and self.e.subcats[sub]["mid"] == self.e.subcats[profile.business]["mid"]:
            return "similar"
        return "other"

    def store(self, profile: Profile, code: str, idx: int, *, include_site_radius: bool = True) -> dict:
        d = self.e.by_code[code]
        rec = self.details(code).get(idx)
        if rec is None:
            raise KeyError(idx)
        _, name, sub, addr = rec
        sc = self.e.subcats[sub]
        rel = self.relation(profile, sub)
        mine = self.e.subcats[profile.business]["name"] if profile.business is not None else None
        same_n = self.e.sub_count(d, sub)
        why: list[str] = []
        checks: list[str] = []
        if rel == "same":
            why.append(f"내 업종({mine})과 같은 가게예요. 손님을 나눠 갖는 경쟁 가게로 봐요.")
            why.append(f"{d['name']}에는 {mine} 가게가 {same_n}곳 있어요(점포 {d['total']:,}곳 중).")
            checks += ["점심·저녁 시간에 손님이 얼마나 드는지 보세요.", "가격과 대표 메뉴를 적어 와서 내 가게와 비교해 보세요."]
        elif rel == "anchor":
            why.append(f"{sc['name']}은(는) 사람을 일부러 오게 하는 가게예요. 근처에 있으면 오가는 손님이 생겨요.")
            if mine:
                why.append(f"{mine}처럼 지나가다 들르는 손님이 중요한 가게라면 가까울수록 좋아요.")
            checks += ["이 가게 앞을 사람이 어느 방향으로 걷는지 보세요.", "주차장과 버스 정류장이 어디 있는지 보세요."]
        elif rel == "similar":
            why.append(f"내 업종({mine})과 같은 '{sc['mid']}' 묶음이에요. 손님 일부가 겹칠 수 있어요.")
            checks.append("손님층(나이, 혼자/가족)이 내 가게와 겹치는지 보세요.")
        else:
            if mine:
                why.append(f"내 업종({mine})과 직접 관련은 적은 '{sc['cat']}' 가게예요. 이 거리의 분위기를 보여 줘요.")
            else:
                why.append(f"'{sc['cat']}' 업종 가게예요. 업종을 고르시면 내 가게와의 관계를 알려 드려요.")
            checks.append("이 거리에 비어 있는 가게(임대 문구)가 있는지 보세요.")
        checks.append(f"자료는 {self.e.meta['storeDate']} 기준이에요. 지금도 영업하는지 확인하세요.")
        return {
            "idx": idx,
            "dong": code,
            "dongName": d["name"],
            "name": name,
            "sub": sc["name"],
            "mid": sc["mid"],
            "cat": sc["cat"],
            "address": addr,
            "relation": rel,
            "why": why,
            "checks": checks,
            "sameInDong": same_n,
            "dongTotal": d["total"],
            "source": f"제주데이터허브 상가(상권) 정보, {self.e.meta['storeDate']} 기준",
            "buildingNote": "어느 건물에 들어 있는지는 확인하지 못했어요. 점포 좌표에 표시했어요.",
            **({"siteRadius": self.site_radius(profile, self.xz[idx])} if include_site_radius else {}),
        }

    def nearby(self, profile: Profile, code: str, x: float, z: float) -> dict:
        rows = []
        for idx, rec in self.details(code).items():
            sx, sz = self.xz[idx]
            dist = math.hypot(sx - x, sz - z)
            if dist <= NEARBY_M:
                rows.append((dist, idx, rec))
        rows.sort()
        items = [
            {"idx": idx, "name": rec[1], "sub": self.e.subcats[rec[2]]["name"], "relation": self.relation(profile, rec[2]), "m": round(dist)}
            for dist, idx, rec in rows[:30]
        ]
        return {
            "dong": code,
            "dongName": self.e.by_code[code]["name"],
            "radius": NEARBY_M,
            "count": len(rows),
            "items": items,
            "note": f"이 건물에서 {NEARBY_M:g}m 안에 있는 주변 점포예요. 이 건물에 입점했는지는 확인하지 못했어요.",
            "siteRadius": self.site_radius(profile, (x, z)),
        }

    def inspect(self, profile: Profile, code: str, x: float, z: float) -> dict:
        """주변 점포에 업종 관계와 답사 이유를 덧붙인다."""
        nearby = self.nearby(profile, code, x, z)
        items = []
        for item in nearby["items"]:
            store = self.store(profile, code, item["idx"], include_site_radius=False)
            items.append({**item, "why": store["why"], "checks": store["checks"]})
        return {**nearby, "items": items}

    # ---------- '여기 / 이 가게' 질문 ----------
    def ask(self, profile: Profile, text: str, selection: dict | None, focus: str | None) -> dict | None:
        """선택한 점포나 동네에 대한 질문이면 답과 지도에 강조할 점포를 돌려준다. 아니면 None."""
        code = (selection or {}).get("dong") or focus
        if not code:
            return None
        d = self.e.by_code[code]
        sel_store = selection.get("store") if selection and selection.get("type") == "store" else None
        if sel_store is not None and (type(sel_store) is not int or sel_store not in self.details(code)):
            raise ValueError("이 동네 점포가 아닙니다")
        asked_sub = self.e.find_business(text)
        anchors_q = any(w in text for w in ("마트", "병원", "약국", "학원", "이웃"))
        is_why = any(w in text for w in WHY_WORDS)
        is_near = any(w in text for w in NEAR_WORDS)

        if sel_store is not None and is_why and asked_sub is None and not anchors_q:
            s = self.store(profile, code, int(sel_store))
            rel_label = {"same": "같은 업종", "anchor": "손님을 끌어오는 이웃", "similar": "비슷한 업종", "other": "다른 업종"}[s["relation"]]
            highlight, label = self._highlight_for(profile, code, s["relation"], int(sel_store))
            return {
                "answer": (
                    f"{s['name']}({josa(s['sub'], ('은', '는')).replace(s['sub'], s['sub'] + ')', 1)} 상가 정보에 있는 실제 점포라 표시했어요. "
                    if profile.business is None
                    else f"{s['name']}({josa(s['sub'], ('은', '는')).replace(s['sub'], s['sub'] + ')', 1)} {rel_label}이라서 표시했어요. "
                )
                + " ".join(s["why"])
                + f" 지도에 {label}을 함께 표시했어요.",
                "highlight": highlight,
                "highlightLabel": label,
                "store": s,
            }
        if asked_sub is not None and profile.business is None and sel_store is None and not is_near:
            return None  # 업종을 정하는 말이다 (예: '카페')
        if asked_sub is not None and (is_near or is_why or "가게" in text or len(text) < 20):
            name = self.e.subcats[asked_sub]["name"]
            idxs = [i for i, r in self.details(code).items() if r[2] == asked_sub]
            idxs = self._near_selection(idxs, sel_store)
            n = self.e.sub_count(d, asked_sub)
            where = "이 가게 가까운 순으로 " if sel_store is not None else ""
            return {
                "answer": f"{d['name']}에 {name} 가게는 {n}곳이에요(점포 {d['total']:,}곳 중). {where}지도에 {len(idxs)}곳을 표시했어요.",
                "highlight": idxs,
                "highlightLabel": f"{name} {n}곳",
            }
        if anchors_q:
            idxs = [i for i, r in self.details(code).items() if r[2] in self.e.anchor_subs]
            idxs = self._near_selection(idxs, sel_store)
            return {
                "answer": f"{d['name']}에 마트·병원·약국·학원은 {len(idxs)}곳이에요. 사람을 일부러 오게 하는 가게들이라 지도에 표시했어요.",
                "highlight": idxs,
                "highlightLabel": f"마트·병원·약국·학원 {len(idxs)}곳",
            }
        if sel_store is not None and (is_why or is_near or "여기" in text or "이 가게" in text):
            s = self.store(profile, code, int(sel_store))
            return {"answer": " ".join(s["why"]) + " 현장에서는: " + " ".join(s["checks"]), "highlight": [int(sel_store)], "highlightLabel": s["name"], "store": s}
        return None

    def _highlight_for(self, profile: Profile, code: str, relation: str, idx: int) -> tuple[list[int], str]:
        det = self.details(code)
        sub = det[idx][2]
        if relation == "anchor":
            idxs = [i for i, r in det.items() if r[2] in self.e.anchor_subs]
            return self._near_selection(idxs, idx), f"마트·병원·약국·학원 {len(idxs)}곳"
        idxs = [i for i, r in det.items() if r[2] == sub]
        return self._near_selection(idxs, idx), f"{self.e.subcats[sub]['name']} {len(idxs)}곳"

    def _near_selection(self, idxs: list[int], sel: int | None) -> list[int]:
        if sel is None:
            return idxs
        sx, sz = self.xz[int(sel)]
        return sorted(idxs, key=lambda i: math.hypot(self.xz[i][0] - sx, self.xz[i][1] - sz))

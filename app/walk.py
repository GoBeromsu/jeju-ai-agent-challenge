"""가정 보행 미리보기: 실제 정류장 → 가정한 목적지까지 OSM 길 위 최단 경로, 그 길이 고른 가게 옆을 지나는가.

- 관측 유동이 아니다. 'OSM 에 그려진 길로 가장 짧게 걸으면'이라는 가정이다.
- 출입구·보도 폭·신호·계단 편의는 확인하지 않는다. 그래서 '가게 앞'이라고 단정하지 않고 표시점과의 거리만 말한다.
- 걸음 속도 분당 67m(시속 4km)도 가정이다.
"""

from __future__ import annotations

import heapq
import json
import math
from pathlib import Path

from . import config

WALK_DIR = config.ROOT / "web" / "data" / "walk"
WALK_M_PER_MIN = 67
NEAR_STORE_M = 15  # 이 안이면 '표시점 옆을 지난다'


class WalkGraph:
    def __init__(self, path: Path) -> None:
        d = json.loads(path.read_text(encoding="utf-8"))
        self.meta = {"source": d["source"], "osmBase": d["osmBase"]}
        self.nodes: list[tuple[float, float]] = [tuple(n) for n in d["nodes"]]
        self.adj: list[list[tuple[int, float, str]]] = [[] for _ in self.nodes]
        for a, b, w, kind in d["edges"]:
            self.adj[a].append((b, w, kind))
            self.adj[b].append((a, w, kind))
        self.crossings = set(d["crossings"])
        self.stops = d["stops"]
        self.by_ref = {s["ref"]: s for s in self.stops}

    def snap(self, x: float, z: float) -> tuple[int, float]:
        best, bd = -1, math.inf
        for i, (nx, nz) in enumerate(self.nodes):
            if not self.adj[i]:
                continue
            dd = (nx - x) ** 2 + (nz - z) ** 2
            if dd < bd:
                best, bd = i, dd
        return best, math.sqrt(bd)

    def shortest(self, a: int, b: int) -> tuple[float, list[int]] | None:
        dist = {a: 0.0}
        prev: dict[int, int] = {}
        pq = [(0.0, a)]
        while pq:
            d, u = heapq.heappop(pq)
            if u == b:
                break
            if d > dist.get(u, math.inf):
                continue
            for v, w, _ in self.adj[u]:
                nd = d + w
                if nd < dist.get(v, math.inf):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        if b not in dist:
            return None
        path = [b]
        while path[-1] != a:
            path.append(prev[path[-1]])
        return dist[b], path[::-1]

    def _kinds(self, path: list[int]) -> dict[str, float]:
        out: dict[str, float] = {}
        for u, v in zip(path, path[1:]):
            w, kind = min((w, k) for t, w, k in self.adj[u] if t == v)
            out[kind] = round(out.get(kind, 0) + w, 1)
        return out

    @staticmethod
    def _dist_to_path(px: float, pz: float, pts: list[tuple[float, float]]) -> float:
        if len(pts) == 1:
            return math.hypot(px - pts[0][0], pz - pts[0][1])
        best = math.inf
        for (ax, az), (bx, bz) in zip(pts, pts[1:]):
            dx, dz = bx - ax, bz - az
            L = dx * dx + dz * dz
            t = 0.0 if L == 0 else max(0.0, min(1.0, ((px - ax) * dx + (pz - az) * dz) / L))
            best = min(best, math.hypot(px - (ax + t * dx), pz - (az + t * dz)))
        return best

    def route(self, stop_ref: str, dest: tuple[float, float], store: tuple[float, float] | None) -> dict:
        s = self.by_ref[stop_ref]
        a, snap_a = self.snap(s["x"], s["z"])
        b, snap_b = self.snap(*dest)
        r = self.shortest(a, b)
        if r is None:
            return {"stop": s, "ok": False, "why": "정류장과 목적지가 OSM 길로 이어져 있지 않아요."}
        length, path = r
        pts = [self.nodes[i] for i in path]
        out = {
            "stop": {k: s[k] for k in ("name", "ref", "x", "z")},
            "ok": True,
            "path": [[round(x, 1), round(z, 1)] for x, z in pts],
            "lengthM": round(length),
            "minutes": max(1, round(length / WALK_M_PER_MIN)),
            "crossings": sum(1 for i in path if i in self.crossings),
            "kinds": self._kinds(path),
            "snapM": {"stop": round(snap_a), "dest": round(snap_b)},
        }
        if store is not None:
            near = self._dist_to_path(store[0], store[1], pts)
            c, snap_c = self.snap(*store)
            via1, via2 = self.shortest(a, c), self.shortest(c, b)
            # 가게 표시점이 길에서 멀면(주소 좌표 등) 지나가는지 판단하지 않는다
            out["store"] = {
                "distanceM": round(near),
                "passes": None if snap_c > NEAR_STORE_M else near <= NEAR_STORE_M,
                "detourM": round(via1[0] + via2[0] - length) if via1 and via2 else None,
                "snapM": round(snap_c),
            }
        return out

    def stops_near(self, x: float, z: float, k: int = 2) -> list[dict]:
        """가까운 정류장 이름 k 개, 이름마다 모든 방향(ref)."""
        by_name: dict[str, list[dict]] = {}
        for s in self.stops:
            by_name.setdefault(s["name"], []).append(s)
        ranked = sorted(by_name.items(), key=lambda kv: min(math.hypot(s["x"] - x, s["z"] - z) for s in kv[1]))
        return [s for _, group in ranked[:k] for s in group]


class Walk:
    def __init__(self, walk_dir: Path = WALK_DIR) -> None:
        self.graphs = {p.stem: WalkGraph(p) for p in walk_dir.glob("*.json")} if walk_dir.exists() else {}

    def supported(self, code: str) -> bool:
        return code in self.graphs


MARKET_SUB_NAMES = ("슈퍼마켓",)


def plan(walk: Walk, place, code: str, store: int, dest: int | None = None, stop_refs: list[str] | None = None) -> dict:
    """정류장(양방향) → 목적지 경로를 정류장마다 하나씩 비교한다. 목적지를 안 주면 가게에서 가장 가까운 슈퍼마켓."""
    if not walk.supported(code):
        return {"kind": "assumed", "supported": False, "why": "이 동네는 보행 길 자료를 아직 받지 않았어요. 지금은 표선면만 돼요."}
    g = walk.graphs[code]
    det = place.details(code)
    if store not in det or (dest is not None and dest not in det):
        raise KeyError("이 동네 점포가 아닙니다")
    sx, sz = place.xz[store]
    markets = sorted(
        (i for i, r in det.items() if place.e.subcats[r[2]]["name"] in MARKET_SUB_NAMES and i != store),
        key=lambda i: math.hypot(place.xz[i][0] - sx, place.xz[i][1] - sz),
    )
    dest_options = markets[:3]
    hanaro = [i for i in markets if "하나로마트" in det[i][1]]
    if hanaro:
        dest_options = hanaro[:1] + [i for i in dest_options if i != hanaro[0]][:2]
    if dest is None:
        dest = dest_options[0]
    dx, dz = place.xz[dest]
    stops = [g.by_ref[r] for r in stop_refs if r in g.by_ref] if stop_refs else g.stops_near(sx, sz)
    routes = [g.route(s["ref"], (dx, dz), (sx, sz)) for s in stops]
    seen: dict[tuple, str] = {}
    for r in routes:
        if r.get("ok"):
            key = tuple(map(tuple, r["path"]))
            if key in seen:
                r["sameAsRef"] = seen[key]  # 길 건너 정류장인데 같은 길 지점에 붙었다: 건너는지 확인 못함
            else:
                seen[key] = r["stop"]["ref"]
    return {
        "kind": "assumed",
        "supported": True,
        "assumption": "가정 보행: OSM 에 그려진 길로 가장 짧게 걷는다고 본 경로예요. 실제로 사람이 이 길로 다닌다는 관측이 아니고, 출입구·보도·신호는 확인하지 못했어요.",
        "speed": f"걸음 분당 {WALK_M_PER_MIN}m(시속 4km) 가정",
        "store": {"idx": store, "name": det[store][1], "x": sx, "z": sz},
        "dest": {"idx": dest, "name": det[dest][1], "x": dx, "z": dz, "assumed": True},
        "destOptions": [{"idx": i, "name": det[i][1]} for i in dest_options],
        "stopOptions": [{"name": s["name"], "ref": s["ref"]} for s in g.stops_near(sx, sz, 4)],
        "routes": routes,
        "source": g.meta,
    }

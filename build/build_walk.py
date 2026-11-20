"""표선면 보행 그래프: OSM 길(보도·골목·차도), 횡단보도, 버스 정류장을 앱 좌표로 바꾼다.

    python3 build/build_walk.py     # data/raw/ext/osm_pyoseon_walk.json → web/data/walk/50130320.json

원자료: OpenStreetMap (ODbL 1.0, © OpenStreetMap contributors), Overpass API 로 받은 표선 주변 상자
(33.31~33.345N, 126.80~126.86E). 받은 시각은 파일 안 osm3s.timestamp_osm_base.
- 길은 OSM 에 그려진 선만 쓴다. 출입구·보도 폭·신호는 확인하지 않는다(가정 보행).
- 정류장은 OSM bus_stop 노드의 이름과 ref(정류소 번호). 같은 이름의 두 ref 는 길 건너 양방향이다.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "raw" / "ext" / "osm_pyoseon_walk.json"
OUT = ROOT / "web" / "data" / "walk" / "50130320.json"
LON0, LAT0 = 126.55, 33.38  # build_data.py 와 같은 원점
M_LAT = 111_320.0
M_LON = 111_320.0 * math.cos(math.radians(LAT0))
# 걸을 수 없는 길(자동차 전용)은 뺀다. 이 상자에는 motorway·trunk 가 없다.
NOT_WALKABLE = {"motorway", "motorway_link", "trunk", "trunk_link", "construction", "proposed"}
KIND = {"footway": "보도", "pedestrian": "보행자 길", "path": "오솔길", "steps": "계단", "cycleway": "자전거길"}


def xz(lat: float, lon: float) -> tuple[float, float]:
    return round((lon - LON0) * M_LON, 1), round(-(lat - LAT0) * M_LAT, 1)


def main() -> None:
    raw = json.loads(SRC.read_text(encoding="utf-8"))
    node_index: dict[int, int] = {}
    nodes: list[tuple[float, float]] = []
    edges: list[list] = []
    crossings_osm: set[int] = set()
    stops = []
    for e in raw["elements"]:
        t = e.get("tags", {})
        if e["type"] == "node":
            if t.get("highway") == "crossing" or t.get("footway") == "crossing":
                crossings_osm.add(e["id"])
            if t.get("highway") == "bus_stop" and t.get("name"):
                x, z = xz(e["lat"], e["lon"])
                stops.append({"name": t["name"], "ref": t.get("ref") or str(e["id"]), "x": x, "z": z, "osm": e["id"]})
    for e in raw["elements"]:
        t = e.get("tags", {})
        if e["type"] != "way" or t.get("highway") in NOT_WALKABLE or t.get("highway") is None:
            continue
        kind = KIND.get(t["highway"], "차도 옆" if t["highway"] in ("primary", "secondary", "tertiary") else "골목·마을길")
        if t.get("footway") == "crossing":
            kind = "횡단보도"
        ids, geom = e["nodes"], e["geometry"]
        prev = None
        for nid, g in zip(ids, geom):
            if nid not in node_index:
                node_index[nid] = len(nodes)
                nodes.append(xz(g["lat"], g["lon"]))
            cur = node_index[nid]
            if prev is not None and prev != cur:
                (ax, az), (bx, bz) = nodes[prev], nodes[cur]
                edges.append([prev, cur, round(math.hypot(bx - ax, bz - az), 1), kind])
            prev = cur
    crossings = sorted(node_index[n] for n in crossings_osm if n in node_index)
    stops.sort(key=lambda s: (s["name"], s["ref"]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "dong": "50130320",
                "source": "OpenStreetMap (ODbL 1.0, © OpenStreetMap contributors) via Overpass API",
                "osmBase": raw.get("osm3s", {}).get("timestamp_osm_base"),
                "nodes": nodes,
                "edges": edges,
                "crossings": crossings,
                "stops": stops,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    print(f"{OUT.relative_to(ROOT)}: 노드 {len(nodes)}, 길 {len(edges)}, 횡단보도 {len(crossings)}, 정류장 {len(stops)}")


if __name__ == "__main__":
    main()

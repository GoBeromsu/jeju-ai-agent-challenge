"""OpenStreetMap 건물 윤곽을 방향 있는 상자로 굽는다 (서울 3D 아틀라스 build_buildings.py 방식, MIT).

OpenFreeMap z14 벡터 타일의 building 층 → 가장 긴 변 방향으로 회전한 상자.
높이는 OSM render_height (없으면 6m 추정). 국토부 GIS 건물통합정보(층수)를 받으면 그쪽으로 바꾼다.

출력 web/data/buildings.bin.gz : 레코드마다 x,z (Uint16 양자화), w,d (Uint8, 0.5m 단위), angle (Uint8), height (Uint8, m)
사용: uv run -q --with mapbox-vector-tile --with numpy python build/build_buildings.py
"""

from __future__ import annotations

import concurrent.futures
import gzip
import json
import math
import sys
import urllib.request
from pathlib import Path

import mapbox_vector_tile
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "raw" / "ext" / "openfreemap_z14"
OUT = ROOT / "web" / "data"
sys.path.insert(0, str(ROOT / "build"))
from build_data import project  # noqa: E402
from build_terrain import X0, X1, Z0, Z1  # noqa: E402

Z = 14
UA = "jeju-ai-agent-challenge build"
# 본섬 + 우도·가파도·마라도, 추자도
BOXES = [(126.13, 33.10, 126.99, 33.58), (126.25, 33.90, 126.40, 34.00)]


def tileset_url() -> str:
    req = urllib.request.Request("https://tiles.openfreemap.org/planet", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["tiles"][0]


def tilexy(lon, lat):
    n = 2**Z
    return (lon + 180) / 360 * n, (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n


def tile_lonlat(tx, ty, px, py, extent):
    n = 2**Z
    a = tx + px / extent
    b = ty + (extent - py) / extent  # mapbox_vector_tile 는 y 를 아래→위로 돌려준다
    lon = a / n * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * b / n))))
    return lon, lat


def fetch(template, tx, ty):
    path = CACHE / f"{tx}-{ty}.pbf"
    if path.exists():
        return path.read_bytes()
    url = template.replace("{z}", str(Z)).replace("{x}", str(tx)).replace("{y}", str(ty))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            break
        except Exception:
            if attempt == 2:
                raise
    path.write_bytes(data)
    return data


def boxes_from_tile(raw, tx, ty):
    out = []
    tile = mapbox_vector_tile.decode(raw)
    layer = tile.get("building")
    if not layer:
        return out
    extent = layer.get("extent", 4096)
    for f in layer["features"]:
        g = f["geometry"]
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"] if g["type"] == "MultiPolygon" else []
        h = f["properties"].get("render_height") or 6
        for poly in polys:
            ring = poly[0]
            if len(ring) < 4:
                continue
            pts = [project(*tile_lonlat(tx, ty, px, py, extent)) for px, py in ring[:-1]]
            edges = list(zip(pts, pts[1:] + pts[:1]))
            (ax, az), (bx, bz) = max(edges, key=lambda e: (e[1][0] - e[0][0]) ** 2 + (e[1][1] - e[0][1]) ** 2)
            a = math.atan2(bz - az, bx - ax)
            c, s = math.cos(a), math.sin(a)
            u = [x * c + z * s for x, z in pts]
            v = [-x * s + z * c for x, z in pts]
            uc, vc = (max(u) + min(u)) / 2, (max(v) + min(v)) / 2
            w, d = max(u) - min(u), max(v) - min(v)
            if w < 2 or d < 2 or w > 127 or d > 127:
                continue
            x, z = uc * c - vc * s, uc * s + vc * c
            if not (X0 <= x <= X1 and Z0 <= z <= Z1):
                continue
            out.append((x, z, w, d, a % math.pi, min(max(h, 3), 255)))
    return out


def main() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    template = tileset_url()
    tiles = []
    for lon0, lat0, lon1, lat1 in BOXES:
        x0, y0 = map(int, tilexy(lon0, lat1))
        x1, y1 = map(int, tilexy(lon1, lat0))
        tiles += [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
    seen = set()
    recs = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        futs = {pool.submit(fetch, template, x, y): (x, y) for x, y in tiles}
        for i, fut in enumerate(concurrent.futures.as_completed(futs)):
            tx, ty = futs[fut]
            for b in boxes_from_tile(fut.result(), tx, ty):
                key = (round(b[0]), round(b[1]), round(b[2]), round(b[3]))
                if key in seen:  # 타일 경계에 걸친 건물은 두 타일에 다 들어 있다
                    continue
                seen.add(key)
                recs.append(b)
            if i % 200 == 0:
                print(f"tiles {i}/{len(tiles)} buildings {len(recs)}", flush=True)
    arr = np.zeros(len(recs), dtype=[("x", "<u2"), ("z", "<u2"), ("w", "u1"), ("d", "u1"), ("a", "u1"), ("h", "u1")])
    r = np.array(recs, dtype=np.float64)
    arr["x"] = np.clip(np.round((r[:, 0] - X0) / (X1 - X0) * 65535), 0, 65535)
    arr["z"] = np.clip(np.round((r[:, 1] - Z0) / (Z1 - Z0) * 65535), 0, 65535)
    arr["w"] = np.clip(np.round(r[:, 2] * 2), 1, 255)
    arr["d"] = np.clip(np.round(r[:, 3] * 2), 1, 255)
    arr["a"] = np.round(r[:, 4] / math.pi * 255) % 256
    arr["h"] = np.clip(np.round(r[:, 5]), 3, 255)
    (OUT / "buildings.bin.gz").write_bytes(gzip.compress(arr.tobytes(), 9))
    info = json.loads((OUT / "terrain.json").read_text(encoding="utf-8"))
    info["buildings"] = {
        "count": len(recs),
        "tileset": template,
        "source": "© OpenStreetMap contributors (ODbL), OpenFreeMap / OpenMapTiles 벡터 타일",
        "heightMax": int(arr["h"].max()) if len(recs) else 0,
    }
    (OUT / "terrain.json").write_text(json.dumps(info, ensure_ascii=False, indent=1))
    print(json.dumps({"tiles": len(tiles), "buildings": len(recs), "MB": round((OUT / "buildings.bin.gz").stat().st_size / 1e6, 2)}))


if __name__ == "__main__":
    main()

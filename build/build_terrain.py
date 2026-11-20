"""지형·도로·나무를 미리 굽는다 (실행 중 계산 없음).

입력
- data/raw/ext/copernicus_N33_E126.tif : Copernicus GLO-30 DEM (30m), build/fetch_data.py 로 받는다
- data/raw/ext/osm_roads_jeju.json    : OpenStreetMap 도로 (Overpass, ODbL, osm base 2026-07-24). fetch_data.py 가 받지 않는 시점 자료(README 참고)
- web/data/dongs.json, web/data/stores.bin : build_data.py 결과

출력 (web/data)
- terrain.bin.gz : 높이 격자 Uint16 (m, 120m 간격) + 동네 번호 격자는 텍스처로
- terrain_color.jpg : 음영과 땅색을 구운 색 텍스처 (40m)
- terrain_dong.png : 동네 번호 텍스처 (0=바다, 1..43)
- roads.bin.gz : 도로 폴리라인 (등급, 점 수, 첫 점 Uint16 + 차이 Int16)
- trees.bin.gz : 나무 위치 Uint16 x,z + 크기 Uint8
- terrain.json : 격자 범위와 크기

사용: uv run -q --with numpy --with scipy --with pillow --with tifffile --with imagecodecs python build/build_terrain.py
"""

from __future__ import annotations

import gzip
import json
import math
import struct
import sys
from pathlib import Path

import numpy as np
import tifffile
from PIL import Image, ImageDraw
from scipy import ndimage

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "ext"
OUT = ROOT / "web" / "data"
sys.path.insert(0, str(ROOT / "build"))
from build_data import LAT0, LON0, M_PER_DEG_LAT, M_PER_DEG_LON, project  # noqa: E402

X0, X1 = -38400.0, 39600.0
Z0, Z1 = -70800.0, 30000.0
GRID = 120.0  # 높이 격자 간격 (m)
TEX = 40.0  # 색 텍스처 간격 (m)
ROAD_CLASSES = {
    "motorway": 0, "trunk": 0, "motorway_link": 0, "trunk_link": 0,
    "primary": 1, "primary_link": 1,
    "secondary": 2, "secondary_link": 2,
    "tertiary": 3, "tertiary_link": 3,
    "unclassified": 4, "residential": 4, "living_street": 4,
}


def load_dem():
    t = tifffile.TiffFile(RAW / "copernicus_N33_E126.tif")
    page = t.pages[0]
    dem = page.asarray().astype(np.float32)
    tie = page.tags["ModelTiepointTag"].value
    scale = page.tags["ModelPixelScaleTag"].value
    return dem, tie[3], tie[4], scale[0], scale[1]


def sample_dem(dem, lon0, lat0, sx, sy, xs, zs):
    lon = xs / M_PER_DEG_LON + LON0
    lat = LAT0 - zs / M_PER_DEG_LAT
    col = (lon - lon0) / sx - 0.5
    row = (lat0 - lat) / sy - 0.5
    return ndimage.map_coordinates(dem, [row, col], order=1, mode="nearest")


def grid(step):
    w = int(round((X1 - X0) / step)) + 1
    h = int(round((Z1 - Z0) / step)) + 1
    xs = X0 + np.arange(w) * step
    zs = Z0 + np.arange(h) * step
    return w, h, np.meshgrid(xs, zs)


def rasterize_dongs(dongs, step):
    w, h, _ = grid(step)
    img = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(img)
    for i, d in enumerate(dongs):
        for ring in d["rings"]:
            pts = [((x - X0) / step, (z - Z0) / step) for x, z in ring]
            if len(pts) >= 3:
                draw.polygon(pts, fill=i + 1)
    return np.array(img)


def store_density(step, shape):
    buf = (OUT / "stores.bin").read_bytes()
    n = len(buf) // 14
    arr = np.frombuffer(buf, dtype=np.dtype([("x", "<f4"), ("z", "<f4"), ("stack", "<u2"), ("sub", "<u2"), ("cat", "u1"), ("dong", "u1")]), count=n)
    h, w = shape
    cx = np.clip(((arr["x"] - X0) / step).astype(int), 0, w - 1)
    cz = np.clip(((arr["z"] - Z0) / step).astype(int), 0, h - 1)
    dens = np.zeros(shape, np.float32)
    np.add.at(dens, (cz, cx), 1)
    return ndimage.gaussian_filter(dens, 200 / step)


def hex_rgb(h):
    return np.array([int(h[i : i + 2], 16) for i in (1, 3, 5)], np.float32)


def build_color(dem_args, ids, density):
    w, h, (xs, zs) = grid(TEX)
    elev = sample_dem(*dem_args, xs.ravel(), zs.ravel()).reshape(h, w)
    land = ids > 0
    elev = np.where(land, np.maximum(elev, 0), 0)
    # 음영: 북서쪽 빛, 높이 1.6배로 과장해 계산
    gz, gx = np.gradient(elev * 1.6, TEX)
    nx, ny, nz = -gx, np.ones_like(gx), -gz
    norm = np.sqrt(nx * nx + ny * ny + nz * nz)
    light = np.array([-0.5, 0.75, -0.45])
    light /= np.linalg.norm(light)
    lam = (nx * light[0] + ny * light[1] + nz * light[2]) / norm
    shade = 0.62 + 0.5 * np.clip(lam, 0, 1)

    field = hex_rgb("#dfe4c4")  # 해안 밭
    meadow = hex_rgb("#b9cf98")  # 중산간 초지
    forest = hex_rgb("#6f9a63")  # 숲
    alpine = hex_rgb("#a7a58a")  # 한라산 정상부
    town = hex_rgb("#efebe0")  # 시가지
    sea = hex_rgb("#bcd6db")

    def band(e, a, b):
        return np.clip((e - a) / (b - a), 0, 1)[..., None]

    col = field + (meadow - field) * band(elev, 60, 260)
    col = col + (forest - col) * band(elev, 320, 650)
    col = col + (alpine - col) * band(elev, 1500, 1800)
    # 나무 얼룩: 숲 띠에서 결 무늬
    rng = np.random.default_rng(7)
    noise = ndimage.gaussian_filter(rng.standard_normal((h, w)).astype(np.float32), 1.2)
    forestness = (band(elev, 250, 600)[..., 0] * (1 - band(elev, 1500, 1750)[..., 0]))
    col = col * (1 - 0.18 * np.clip(noise * 3, -1, 1)[..., None] * forestness[..., None])
    urban = np.clip(density / 6.0, 0, 1)[..., None]
    col = col + (town - col) * urban * 0.9
    col = col * shade[..., None]
    # 해안선 모래띠
    coast = land & ~ndimage.binary_erosion(land, iterations=2)
    col[coast] = col[coast] * 0.5 + hex_rgb("#efe6c8") * 0.5
    col[~land] = sea
    img = Image.fromarray(np.clip(col, 0, 255).astype(np.uint8), "RGB")
    img.save(OUT / "terrain_color.jpg", quality=82, optimize=True)
    return elev, forestness, urban[..., 0]


def build_heights(dem_args, ids_grid):
    w, h, (xs, zs) = grid(GRID)
    elev = sample_dem(*dem_args, xs.ravel(), zs.ravel()).reshape(h, w)
    land = ids_grid > 0
    # 바다 쪽 한 칸까지는 해안 높이를 0 으로 이어 경계가 끊기지 않게
    elev = np.where(land, np.maximum(elev, 1), 0)
    q = np.clip(np.round(elev), 0, 65535).astype("<u2")
    return w, h, q


def simplify(points, tol):
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        ax, az = points[a]
        bx, bz = points[b]
        dx, dz = bx - ax, bz - az
        L = math.hypot(dx, dz) or 1e-9
        best, idx = -1.0, -1
        for i in range(a + 1, b):
            px, pz = points[i]
            d = abs(dz * (px - ax) - dx * (pz - az)) / L
            if d > best:
                best, idx = d, i
        if best > tol:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p for p, k in zip(points, keep) if k]


def quant(x, z):
    return (
        int(round((x - X0) / (X1 - X0) * 65535)),
        int(round((z - Z0) / (Z1 - Z0) * 65535)),
    )


def build_roads():
    data = json.loads((RAW / "osm_roads_jeju.json").read_text(encoding="utf-8"))
    out = bytearray()
    count = points = 0
    per_class = [0] * 5
    for el in data["elements"]:
        cls = ROAD_CLASSES.get(el["tags"].get("highway"))
        geom = el.get("geometry")
        if cls is None or not geom:
            continue
        pts = [project(g["lon"], g["lat"]) for g in geom]
        pts = [p for p in pts if X0 <= p[0] <= X1 and Z0 <= p[1] <= Z1]
        pts = simplify(pts, 4.0 if cls >= 3 else 6.0)
        if len(pts) < 2:
            continue
        q = [quant(x, z) for x, z in pts]
        deltas = []
        for (ax, az), (bx, bz) in zip(q, q[1:]):
            dx, dz = bx - ax, bz - az
            if not (-32768 <= dx <= 32767 and -32768 <= dz <= 32767):
                break
            deltas.append((dx, dz))
        n = len(deltas) + 1
        out += struct.pack("<BHHH", cls, n, *q[0])
        for dx, dz in deltas:
            out += struct.pack("<hh", dx, dz)
        count += 1
        points += n
        per_class[cls] += 1
    (OUT / "roads.bin.gz").write_bytes(gzip.compress(bytes(out), 9))
    return {"polylines": count, "points": points, "perClass": per_class, "osmTimestamp": data["osm3s"]["timestamp_osm_base"]}


def build_trees(forestness, urban, ids):
    """숲 띠와 시가지 밖 들판에 나무를 흩뿌린다. 실제 수목 자료가 아니라 땅색과 같은 근거(고도·점포 밀도)로 그린 풍경."""
    rng = np.random.default_rng(11)
    h, w = forestness.shape
    land = ids > 0
    p = np.where(land, 0.05 + 0.55 * forestness, 0) * (1 - np.clip(urban * 1.5, 0, 1))
    pick = rng.random((h, w)) < p * 0.5
    zz, xx = np.nonzero(pick)
    x = X0 + (xx + rng.random(len(xx))) * TEX
    z = Z0 + (zz + rng.random(len(zz))) * TEX
    size = np.clip(rng.normal(1.0, 0.25, len(xx)) * (1 + forestness[zz, xx]), 0.5, 2.2)
    qx = np.clip(np.round((x - X0) / (X1 - X0) * 65535), 0, 65535).astype("<u2")
    qz = np.clip(np.round((z - Z0) / (Z1 - Z0) * 65535), 0, 65535).astype("<u2")
    qs = np.clip(np.round(size * 100), 0, 255).astype("u1")
    rec = np.zeros(len(qx), dtype=[("x", "<u2"), ("z", "<u2"), ("s", "u1")])
    rec["x"], rec["z"], rec["s"] = qx, qz, qs
    (OUT / "trees.bin.gz").write_bytes(gzip.compress(rec.tobytes(), 9))
    return len(rec)


def main() -> None:
    dongs = json.loads((OUT / "dongs.json").read_text(encoding="utf-8"))
    dem_args = load_dem()
    ids_tex = rasterize_dongs(dongs, TEX)
    Image.fromarray(ids_tex, "L").save(OUT / "terrain_dong.png", optimize=True)
    density = store_density(TEX, ids_tex.shape)
    _, forestness, urban = build_color(dem_args, ids_tex, density)
    ids_grid = rasterize_dongs(dongs, GRID)
    w, h, heights = build_heights(dem_args, ids_grid)
    (OUT / "terrain.bin.gz").write_bytes(gzip.compress(heights.tobytes(), 9))
    roads = build_roads()
    trees = build_trees(forestness, urban, ids_tex)
    tex_w, tex_h, _ = grid(TEX)
    info = {
        "bounds": [X0, Z0, X1, Z1],
        "grid": {"w": w, "h": h, "step": GRID},
        "texture": {"w": tex_w, "h": tex_h, "step": TEX},
        "maxHeight": int(heights.max()),
        "roads": roads,
        "trees": trees,
        "sources": {
            "dem": "Copernicus GLO-30 DEM (© DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018, provided under COPERNICUS by the European Union and ESA)",
            "roads": "© OpenStreetMap contributors (ODbL)",
            "trees": "고도와 점포 밀도로 흩뿌린 풍경 (실제 수목 위치 아님)",
        },
    }
    (OUT / "terrain.json").write_text(json.dumps(info, ensure_ascii=False, indent=1))
    sizes = {p.name: round(p.stat().st_size / 1e6, 2) for p in OUT.glob("*") if p.name.startswith(("terrain", "roads", "trees"))}
    print(json.dumps({"grid": [w, h], "maxHeight": info["maxHeight"], "roads": roads, "trees": trees, "MB": sizes}, ensure_ascii=False))


if __name__ == "__main__":
    main()

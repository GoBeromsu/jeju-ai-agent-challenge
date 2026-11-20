"""Bake raw hub/public data into compact files the web viewer loads.

Inputs (data/raw, not committed):
  hub/store_793.csv                      허브 상가(상권) 정보 (cp949)
  ext/hangjeongdong_ver20261001.geojson  vuski/admdongkor 행정동 경계

Outputs (web/data):
  dongs.json    43 동네: 코드, 이름, 중심, 단순화한 외곽선(로컬 미터), 대분류·소분류 점포 수
  subcats.json  소분류 목록 (이름, 중분류, 대분류, 점포 수)
  stores.bin    점포 하나당 STORE_RECORD 한 줄
  meta.json     업종 목록, 투영 기준점, 데이터 기준일
"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OUT = ROOT / "web" / "data"

# 제주 중심 근처를 원점으로 둔 등거리 투영 (미터). 섬 하나 범위라 오차가 작다.
LON0, LAT0 = 126.55, 33.38
M_PER_DEG_LAT = 111_320.0
M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(LAT0))

# x, z (float32), 쌓인 순서 (uint16), 소분류 (uint16), 대분류 (uint8), 동네 번호 (uint8)
STORE_RECORD = "<ffHHBB"

CATEGORIES = [
    "음식",
    "소매",
    "숙박",
    "수리·개인",
    "과학·기술",
    "시설관리·임대",
    "교육",
    "예술·스포츠",
    "부동산",
    "보건의료",
]


def project(lon: float, lat: float) -> tuple[float, float]:
    """경위도 → 로컬 평면 (x 동쪽, z 남쪽)."""
    return (lon - LON0) * M_PER_DEG_LON, -(lat - LAT0) * M_PER_DEG_LAT


def simplify(ring: list[list[float]], tol: float) -> list[list[float]]:
    """Douglas-Peucker 단순화."""
    if len(ring) < 4:
        return ring

    def dp(pts: list[list[float]]) -> list[list[float]]:
        (x1, y1), (x2, y2) = pts[0], pts[-1]
        dx, dy = x2 - x1, y2 - y1
        norm = math.hypot(dx, dy) or 1e-9
        best, idx = 0.0, 0
        for i in range(1, len(pts) - 1):
            px, py = pts[i]
            d = abs(dy * px - dx * py + x2 * y1 - y2 * x1) / norm
            if d > best:
                best, idx = d, i
        if best <= tol:
            return [pts[0], pts[-1]]
        return dp(pts[: idx + 1])[:-1] + dp(pts[idx:])

    # 닫힌 고리는 시작점과 끝점이 같아 직선 기준이 없으므로 가장 먼 점에서 둘로 나눈다.
    x0, y0 = ring[0]
    far = max(range(len(ring)), key=lambda i: (ring[i][0] - x0) ** 2 + (ring[i][1] - y0) ** 2)
    return dp(ring[: far + 1])[:-1] + dp(ring[far:])


def load_dongs() -> list[dict]:
    gj = json.loads((RAW / "ext" / "hangjeongdong_ver20261001.geojson").read_text(encoding="utf-8"))
    dongs = []
    for f in gj["features"]:
        p = f["properties"]
        if not p.get("sidonm", "").startswith("제주"):
            continue
        geom = f["geometry"]
        polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
        rings = []
        for poly in polys:
            outer = [list(project(lon, lat)) for lon, lat in poly[0]]
            s = simplify(outer, 25.0)
            if len(s) >= 4:
                rings.append([[round(x, 1), round(z, 1)] for x, z in s])
        code8 = p["adm_cd2"][:8]
        name = p["adm_nm"].split()[-1]
        dongs.append({"code": code8, "name": name, "sgg": p["sggnm"], "rings": rings})
    dongs.sort(key=lambda d: d["code"])
    return dongs


def load_birth_death() -> dict[str, dict]:
    """허브 읍면동별 사업체 생멸 통계 (KOSIS). 코드가 없어 동 이름으로 잇는다. '*'(5 이하)는 None."""
    bd = pd.read_csv(RAW / "hub" / "birth_death_830.csv", encoding="utf-8-sig", dtype=str)
    bd = bd[bd["읍면동"] != "소계"]
    out: dict[str, dict] = {}
    for (name, kind, sub), g in bd.groupby(["읍면동", "사업체구분", "생멸구분"]):
        series = {}
        for year, n in zip(g["기준연도"], g["사업체수"]):
            series[year] = None if n.strip() == "*" else int(n)
        out.setdefault(name, {})[f"{kind}/{sub}"] = series
    return out


def ring_centroid(ring: list[list[float]]) -> tuple[float, float, float]:
    """다각형 넓이와 무게중심 (넓이는 부호 없는 값)."""
    a = cx = cz = 0.0
    for (x1, z1), (x2, z2) in zip(ring, ring[1:] + ring[:1]):
        c = x1 * z2 - x2 * z1
        a += c
        cx += (x1 + x2) * c
        cz += (z1 + z2) * c
    a *= 0.5
    if abs(a) < 1e-6:
        return 0.0, ring[0][0], ring[0][1]
    return abs(a), cx / (6 * a), cz / (6 * a)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    dongs = load_dongs()
    code_to_idx = {d["code"]: i for i, d in enumerate(dongs)}

    df = pd.read_csv(RAW / "hub" / "store_793.csv", encoding="cp949", dtype={"행정동코드": str})
    df = df[df["상권업종대분류명"].isin(CATEGORIES)].copy()
    df["cat"] = df["상권업종대분류명"].map({c: i for i, c in enumerate(CATEGORIES)})
    df["dong"] = df["행정동코드"].map(code_to_idx)
    missing = int(df["dong"].isna().sum())
    df = df.dropna(subset=["dong"])
    df["dong"] = df["dong"].astype(int)

    # 소분류 목록: 점포 수가 많은 순서로 번호를 매긴다
    sub_counts = df.groupby(["상권업종소분류명", "상권업종중분류명", "상권업종대분류명"]).size()
    sub_counts = sub_counts.sort_values(ascending=False)
    subcats = [
        {"name": name, "mid": mid, "cat": cat, "n": int(n)}
        for (name, mid, cat), n in sub_counts.items()
    ]
    sub_idx = {s["name"]: i for i, s in enumerate(subcats)}
    df["sub"] = df["상권업종소분류명"].map(sub_idx).astype(int)

    # 같은 좌표의 점포를 위로 쌓는다: 쌓인 높이 = 그 자리 점포 수
    df = df.sort_values(["경도", "위도", "cat", "sub"])
    df["stack"] = df.groupby(["경도", "위도"]).cumcount()

    buf = bytearray()
    cols = ["경도", "위도", "stack", "sub", "cat", "dong"]
    for lon, lat, stack, sub, cat, dong in df[cols].itertuples(index=False):
        x, z = project(lon, lat)
        buf += struct.pack(STORE_RECORD, x, z, min(int(stack), 65535), int(sub), int(cat), int(dong))
    (OUT / "stores.bin").write_bytes(bytes(buf))

    # 동네를 열 때만 받는 점포 상세: [stores.bin 순번, 상호, 소분류 번호, 주소]
    detail_dir = OUT / "stores"
    detail_dir.mkdir(exist_ok=True)
    df = df.reset_index(drop=True)
    addr = df["도로명주소"].fillna(df["지번주소"]).fillna("").str.replace("제주특별자치도 ", "", regex=False)
    for d_idx, d in enumerate(dongs):
        rows = df.index[df["dong"] == d_idx]
        recs = [[int(i), str(df.at[i, "상호명"]), int(df.at[i, "sub"]), addr[i]] for i in rows]
        (detail_dir / f"{d['code']}.json").write_text(json.dumps(recs, ensure_ascii=False, separators=(",", ":")))

    birth_death = load_birth_death()
    for d_idx, d in enumerate(dongs):
        bd = birth_death[d["name"]]
        d["bizStats"] = {
            "active": bd["활동사업체/소계"],
            "opened": bd["신생사업체/신규"],
            "closed": bd["소멸사업체/폐업"],
        }
        part = df[df["dong"] == d_idx]
        d["total"] = int(len(part))
        d["byCat"] = [int((part["cat"] == i).sum()) for i in range(len(CATEGORIES))]
        counts = part["sub"].value_counts()
        d["sub"] = {str(int(k)): int(v) for k, v in sorted(counts.items())}
        # 가장 넓은 고리의 무게중심을 동네 중심으로 둔다 (추자도·우도 같은 섬 고리를 피한다)
        _, cx, cz = max(ring_centroid(r) for r in d["rings"])
        d["center"] = [round(cx, 1), round(cz, 1)]
        # 점포가 몰린 곳: 카메라가 날아가 내려다볼 지점
        xs, zs = zip(*(project(lon, lat) for lon, lat in part[["경도", "위도"]].itertuples(index=False)))
        d["storeCenter"] = [round(float(pd.Series(xs).median()), 1), round(float(pd.Series(zs).median()), 1)]
        d["area_km2"] = round(sum(ring_centroid(r)[0] for r in d["rings"]) / 1e6, 2)

    (OUT / "dongs.json").write_text(json.dumps(dongs, ensure_ascii=False, separators=(",", ":")))
    (OUT / "subcats.json").write_text(json.dumps(subcats, ensure_ascii=False, separators=(",", ":")))
    meta = {
        "categories": CATEGORIES,
        "origin": [LON0, LAT0],
        "storeRecord": STORE_RECORD,
        "storeRecordBytes": struct.calcsize(STORE_RECORD),
        "storeCount": int(len(df)),
        "storeDate": "2025-09-18",
        "bizStatsSource": "제주데이터허브 읍면동별 사업체 생멸 통계 (원 출처 KOSIS), 활동 2018~2023, 신규 2019~2023, 폐업 2018~2019",
        "droppedNoDong": missing,
    }
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    print(f"dongs={len(dongs)} subcats={len(subcats)} stores={len(df)} dropped={missing} bin={len(buf)/1e6:.2f}MB")


if __name__ == "__main__":
    main()

"""원본 자료를 공식 제공처에서 다시 받는다. 표준 라이브러리만 쓴다.

    python build/fetch_data.py        # data/raw 를 채운다
    python build/fetch_data.py --check  # 받은 파일의 sha256 이 기록과 같은지 본다

허브 파일은 로그인 없이 받는 공개 다운로드 주소다 (/api/file?id=&fileId=).
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

RAW = Path(__file__).resolve().parent.parent / "data" / "raw"

SOURCES = [
    {
        "path": "hub/store_793.csv",
        "title": "제주데이터허브 제주특별자치도 상가(상권) 정보 (data/793, 2025-09-18)",
        "url": "https://jejudatahub.net/api/file?id=5789&fileId=4692",
        "sha256": "508e53bef3a490a15f1af690283c01b9d257c952204466fd74f23c26b30edcf2",
    },
    {
        "path": "hub/birth_death_830.csv",
        "title": "제주데이터허브 읍면동별 사업체 생멸 통계 (data/830, 원 출처 KOSIS)",
        "url": "https://jejudatahub.net/api/file?id=5754&fileId=4654",
        "sha256": "13bb99848dbf353354ffb7c35fb76c5b61aaf3da67bd454c6691e5d60b20e1eb",
    },
    {
        "path": "ext/hangjeongdong_ver20261001.geojson",
        "title": "vuski/admdongkor 행정동 경계 ver20261001 (CC BY 4.0)",
        "url": "https://raw.githubusercontent.com/vuski/admdongkor/master/ver20261001/HangJeongDong_ver20261001.geojson",
        "sha256": "843ece3a0b3429742cd4a1b699cc13987a88b5e5401f382e7fdaaa194ea5e605",
    },
    {
        "path": "ext/copernicus_N33_E126.tif",
        "title": "Copernicus GLO-30 DEM N33 E126 (ESA, 무료 재배포 허용)",
        "url": "https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_N33_00_E126_00_DEM/Copernicus_DSM_COG_10_N33_00_E126_00_DEM.tif",
        "sha256": "61cdc972f8e3a8c05da9fd52ed965f86eb6e21579f583a69da0a566211059221",
    },
]
# OSM 도로(Overpass)와 건물(OpenFreeMap 타일)은 시점마다 바뀌어 해시를 고정하지 않는다.
# 도로는 data/raw/ext/osm_roads_jeju.json (osm base 2026-07-24), 건물은 build/build_buildings.py 가 직접 받는다.


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    bad = 0
    for s in SOURCES:
        dest = RAW / s["path"]
        if not args.check and (args.force or not dest.exists()):
            dest.parent.mkdir(parents=True, exist_ok=True)
            print(f"받는 중: {s['title']}")
            with urllib.request.urlopen(s["url"], timeout=120) as r:
                dest.write_bytes(r.read())
        if not dest.exists():
            print(f"없음: {dest}")
            bad += 1
            continue
        digest = sha256(dest)
        ok = s["sha256"] is None or digest == s["sha256"]
        bad += 0 if ok else 1
        print(f"{'OK ' if ok else '다름'} {s['path']} sha256={digest}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

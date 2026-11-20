"""허브 유동인구(data/583) 스냅샷을 굽는다. 실행 중 허브가 안 될 때 카드가 이 값을 쓴다.

사용: HUB_PROJECT_KEY=... python -m build.fetch_population [--date YYYYMMDD]
하루 호출 한도 1000회 중 43동 × 성별 2 = 86회(+최신 날짜 찾기)를 쓴다.
"""

import argparse
import json

from app.engine import Engine
from app.hub import SNAPSHOT, HubClient


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    args = ap.parse_args()
    client = HubClient()
    if not client.enabled:
        raise SystemExit("HUB_PROJECT_KEY 가 없습니다 (.env 또는 환경변수)")
    names = [d["name"] for d in Engine().dongs]
    day = args.date or client.latest_date()
    dongs = client.island(names, day)
    empty = [n for n, v in dongs.items() if not v["rows"]]
    SNAPSHOT.write_text(json.dumps({"date": day, "source": "제주데이터허브 읍면동 단위 내국인 유동인구 API", "dongs": dongs}, ensure_ascii=False))
    print(f"date={day} dongs={len(dongs)} empty={empty} calls={client.calls} -> {SNAPSHOT}")


if __name__ == "__main__":
    main()

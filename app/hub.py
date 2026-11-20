"""제주데이터허브 읍면동 단위 내국인 유동인구 API (허브 data/583).

실행 중에 허브를 직접 부른다. 실패하거나 키가 없으면 빌드 때 구운 스냅샷을 쓴다.
응답 한 번에 최대 10행이 오므로(허브 미리보기에서 확인) 동네·성별마다 따로 부른다 (연령대 8개).
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

from . import config

API_BASE = "https://open.jejudatahub.net/api/proxy/Daaa1t3at3tt8a8DD3t55538t35Dab1t/"
SNAPSHOT = config.ROOT / "web" / "data" / "population_snapshot.json"
AGE_ORDER = ["10세미만", "10대", "20대", "30대", "40대", "50대", "60대", "70대이상"]
GENDERS = ["남", "여"]


class HubError(Exception):
    pass


def aggregate(rows: list[dict]) -> dict:
    """한 동네 하루치 행들 → 거주·근무·방문 합과 연령대별 합."""
    resd = sum(int(r["resdPop"]) for r in rows)
    work = sum(int(r["workPop"]) for r in rows)
    visit = sum(int(r["visitPop"]) for r in rows)
    by_age: dict[str, int] = {}
    for r in rows:
        total = int(r["resdPop"]) + int(r["workPop"]) + int(r["visitPop"])
        by_age[r["ageGroup"]] = by_age.get(r["ageGroup"], 0) + total
    all_ = resd + work + visit
    mid = sum(by_age.get(a, 0) for a in ("40대", "50대", "60대"))
    return {
        "resd": resd,
        "work": work,
        "visit": visit,
        "residentShare": round(100 * resd / all_, 1) if all_ else None,
        "visitShare": round(100 * visit / all_, 1) if all_ else None,
        "midAgeShare": round(100 * mid / all_, 1) if all_ else None,
        "byAge": {a: by_age[a] for a in AGE_ORDER if a in by_age},
        "rows": len(rows),
    }


class HubClient:
    def __init__(self, project_key: str | None = None, timeout: float = 8.0) -> None:
        self.project_key = project_key if project_key is not None else config.get("HUB_PROJECT_KEY")
        self.timeout = timeout
        self._latest: tuple[float, str] | None = None
        self._cache: dict[tuple[str, str], dict] = {}
        self._lock = threading.Lock()
        self.calls = 0

    @property
    def enabled(self) -> bool:
        return bool(self.project_key)

    def _get(self, params: dict) -> dict:
        if not self.project_key:
            raise HubError("허브 키가 없습니다")
        url = API_BASE + self.project_key + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with self._lock:
            self.calls += 1
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read().decode("utf-8")
        except Exception as e:  # 네트워크·HTTP 오류는 모두 스냅샷으로 넘긴다
            raise HubError(f"허브 호출 실패: {e}") from e
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise HubError(f"허브 응답이 JSON이 아님: {body[:80]}") from e

    def rows(self, day: str, emd: str) -> list[dict]:
        out: list[dict] = []
        for g in GENDERS:
            d = self._get({"startDate": day, "endDate": day, "emd": emd, "gender": g})
            out.extend(d.get("data", []))
        return out

    def latest_date(self, probe_emd: str = "연동") -> str:
        """데이터가 들어 있는 마지막 날짜. 하루 한 번만 찾는다."""
        if self._latest and time.time() - self._latest[0] < 6 * 3600:
            return self._latest[1]
        day = date.today() - timedelta(days=1)
        for _ in range(14):  # 월말을 거슬러 올라간다 (최대 14개월)
            key = day.strftime("%Y%m%d")
            d = self._get({"startDate": key, "endDate": key, "emd": probe_emd, "gender": "남"})
            if d.get("totCnt", 0) > 0:
                self._latest = (time.time(), key)
                return key
            day = day.replace(day=1) - timedelta(days=1)
        raise HubError("최근 14개월 안에 자료가 없습니다")

    def dong(self, emd: str, day: str | None = None) -> dict:
        day = day or self.latest_date()
        key = (day, emd)
        if key not in self._cache:
            agg = aggregate(self.rows(day, emd))
            agg["date"] = day
            self._cache[key] = agg
        return self._cache[key]

    def island(self, names: list[str], day: str | None = None) -> dict[str, dict]:
        day = day or self.latest_date()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda n: self.dong(n, day), names))
        return dict(zip(names, results))


class Population:
    """허브 실시간 호출 → 실패하면 스냅샷 → 둘 다 없으면 None."""

    def __init__(self, client: HubClient | None = None, snapshot_path: Path = SNAPSHOT) -> None:
        self.client = client or HubClient()
        self.snapshot: dict = json.loads(snapshot_path.read_text(encoding="utf-8")) if snapshot_path.exists() else {}

    def for_dong(self, name: str) -> dict | None:
        if self.client.enabled:
            try:
                return {**self.client.dong(name), "source": "live", "kind": "observed"}
            except HubError as e:
                err = str(e)
            else:  # pragma: no cover
                err = ""
        else:
            err = "허브 키 없음"
        snap = self.snapshot.get("dongs", {}).get(name)
        if snap:
            return {**snap, "date": self.snapshot["date"], "source": "snapshot", "liveError": err, "kind": "observed"}
        return None

    def status(self) -> dict:
        return {
            "live": self.client.enabled,
            "snapshotDate": self.snapshot.get("date"),
        }

"""목 프로필 계산 엔진. LLM 없이 같은 입력에 같은 결과를 낸다.

화면의 모든 숫자는 이 모듈이 계산한다. 에이전트(LLM)는 이 모듈의 도구를 고르고
결과 요약(동네 이름, 남은 후보 수, 빠진 조건)만 받는다.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import median
from threading import Lock
from time import monotonic

DATA = Path(__file__).resolve().parent.parent / "web" / "data"

# 손님을 끌어오는 이웃: 장 보러, 진료받으러, 아이 데리러 사람이 일부러 오는 가게
ANCHOR_PATTERN = re.compile(r"슈퍼마켓|의원|(?<!동물)병원|약국|입시·교과학원")

# 소분류 이름에 그대로 나오지 않는 말
ALIASES = {
    "반찬": "반찬/식료품 소매업",
    "커피": "카페",
    "밥집": "백반/한정식",
    "한식당": "백반/한정식",
    "식당": "백반/한정식",
    "빵집": "빵/도넛",
    "베이커리": "빵/도넛",
    "분식": "김밥/만두/분식",
    "김밥": "김밥/만두/분식",
    "고깃집": "돼지고기 구이/찜",
    "술집": "요리 주점",
    "호프": "생맥주 전문",
    "국숫집": "국수/칼국수",
    "횟집": "횟집",
    "꽃집": "꽃집",
}
GENERIC_TOKENS = {"전문", "기타", "일반", "그", "외", "운영업", "소매업", "판매업", "서비스업", "업", "및", "음식점", "가게"}

REGION_WORDS = {"제주시": "제주시", "서귀포": "서귀포시"}
AREA_WORDS = {"동 지역": "동", "시내": "동", "도심": "동", "읍면": "읍면", "읍·면": "읍면", "시골": "읍면", "한적": "읍면"}
VISIT_WORDS = {
    "아직 모르": "모름",
    "모르겠": "모름",
    "아직 모름": "모름",
    "매일 들르": "지나가다",
    "머무": "목적",
    "지나가다": "지나가다",
    "동네 손님": "지나가다",
    "주민": "지나가다",
    "단골": "지나가다",
    "일부러": "목적",
    "찾아오": "목적",
    "관광객": "목적",
    "여행객": "목적",
}
COMPETITION_WORDS = ("경쟁", "덜 붐", "적은 곳", "적은 동네", "포화", "겹치지")
NEIGHBOR_WORDS = ("마트", "병원", "학원", "약국", "사람 많", "이웃")
GROWING_WORDS = ("늘고", "늘어나", "성장", "뜨는")
RESET_WORDS = ("처음부터", "다시 시작", "초기화")
RELAX_REGION_WORDS = ("지역 상관없", "어디든", "섬 전체")
RELAX_AREA_WORDS = (
    "동·읍면 상관없", "읍면도 괜찮", "시내도 괜찮", "읍면 아니어도", "읍면이 아니어도",
    "읍·면 아니어도", "읍·면이 아니어도", "읍면 상관없", "읍·면 상관없",
)
LETTER_PATTERN = re.compile(r"(?:^|\s)([ABC])(?:\s|$|후보|를|로|에|가|는|\b)")

SATURATION_RATIO = 1.5  # 섬 전체 비중의 1.5배를 넘으면 포화로 본다

# 두 동네를 비교할 때 쓰는 근거 이름과, 비교를 요청하는 말
BASIS_LABELS = {"anchors": "생활시설 등록점포 수", "subShare": "동종 업종 등록점포 비중"}
COMPARE_WORDS = ("비교", "차이", "왜", "아닌", "아니", "제외", "다른 곳", "다른곳")


PROFILE_RULES = {
    "business": lambda v: type(v) is int and v >= 0,
    "visit": lambda v: v in ("목적", "지나가다", "모름"),
    "region": lambda v: v in ("제주시", "서귀포시"),
    "area": lambda v: v in ("동", "읍면"),
    "competition": lambda v: type(v) is bool,
    "neighbors": lambda v: type(v) is bool,
    "growing": lambda v: type(v) is bool,
    "focus": lambda v: isinstance(v, str) and len(v) <= 16,
    "focusBuilding": lambda v: isinstance(v, str) and len(v) <= 40,
    "final": lambda v: isinstance(v, str) and len(v) <= 16,
    "settled": lambda v: type(v) is bool,
    "requiredConditions": lambda v: isinstance(v, list) and all(
        item in ("region", "area", "competition", "neighbors", "growing") for item in v
    ),
    "regionResolved": lambda v: type(v) is bool,
    "decisionPreference": lambda v: v in ("anchors", "subShare", "undecided"),
    "preferenceAsked": lambda v: type(v) is bool,
    "pendingQuestion": lambda v: v in ("business", "region", "preference"),
}


@dataclass
class Profile:
    """목 프로필 네 칸과 조건. 브라우저와 서버 사이를 그대로 오간다."""

    business: int | None = None  # 소분류 번호
    visit: str | None = None  # 목적 | 지나가다
    region: str | None = None  # 제주시 | 서귀포시
    area: str | None = None  # 동 | 읍면
    competition: bool = False  # 경쟁 적은 곳만
    neighbors: bool = False  # 이웃 가게 많은 곳만
    growing: bool = False  # 사업체가 섬 평균보다 빨리 늘어난 곳만
    focus: str | None = None  # 보고 있는 동네 코드 (다시 묻지 않는다)
    focusBuilding: str | None = None  # 지도 자료의 건물 대상 ID
    final: str | None = None  # 최종 관심 후보
    settled: bool = False  # 더 묻지 않아도 될 만큼 답했다
    requiredConditions: list[str] = field(default_factory=list)
    regionResolved: bool = False
    decisionPreference: str | None = None  # anchors | subShare | undecided
    preferenceAsked: bool = False
    pendingQuestion: str | None = None
    history: list[dict[str, str]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Profile":
        """브라우저가 보낸 값을 그대로 믿지 않는다: 모양이 틀리면 ValueError."""
        if d is None:
            d = {}
        if not isinstance(d, dict):
            raise ValueError("profile")
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d and d[k] is not None}
        for k, v in known.items():
            if k == "history":
                if not isinstance(v, list) or len(v) > 8 or not all(
                    isinstance(m, dict) and set(m) == {"role", "text"} and m["role"] in ("user", "assistant")
                    and isinstance(m["text"], str) and len(m["text"]) <= 200 for m in v
                ):
                    raise ValueError("profile.history")
                continue
            ok = PROFILE_RULES[k](v)
            if not ok:
                raise ValueError(f"profile.{k}")
        return cls(**known)


class Engine:
    def __init__(self, data_dir: Path = DATA, population=None) -> None:
        self.population = population
        self._population_lock = Lock()
        self._population_fallbacks: dict[str, tuple[float, dict | None]] = {}
        self.place = None  # app.place.Place, 서버가 붙인다
        from .walk import Walk

        self.walk = Walk()
        from .advisor import Advisor

        self.advisor = Advisor(self)
        self.dongs: list[dict] = json.loads((data_dir / "dongs.json").read_text(encoding="utf-8"))
        self.subcats: list[dict] = json.loads((data_dir / "subcats.json").read_text(encoding="utf-8"))
        self.meta: dict = json.loads((data_dir / "meta.json").read_text(encoding="utf-8"))
        self.by_code = {d["code"]: d for d in self.dongs}
        self.anchor_subs = [i for i, s in enumerate(self.subcats) if ANCHOR_PATTERN.search(s["name"])]
        self.island_total = sum(d["total"] for d in self.dongs)
        self._sub_tokens = [self._tokens(s["name"]) for s in self.subcats]
        anchors = [self.anchor_count(d) for d in self.dongs]
        self.anchor_median = median(anchors)
        first = sum(d["bizStats"]["active"]["2018"] for d in self.dongs)
        last = sum(d["bizStats"]["active"]["2023"] for d in self.dongs)
        self.island_growth = round(100 * (last - first) / first, 1)

    def growth(self, dong: dict) -> float:
        a = dong["bizStats"]["active"]
        return round(100 * (a["2023"] - a["2018"]) / a["2018"], 1)

    # ---------- 기본 집계 ----------
    @staticmethod
    def _tokens(name: str) -> list[str]:
        parts = re.split(r"[/·\s,]+", name)
        return [p for p in parts if len(p) >= 2 and p not in GENERIC_TOKENS]

    def sub_count(self, dong: dict, sub: int) -> int:
        return dong["sub"].get(str(sub), 0)

    def anchor_count(self, dong: dict) -> int:
        return sum(self.sub_count(dong, s) for s in self.anchor_subs)

    def island_share(self, sub: int) -> float:
        return self.subcats[sub]["n"] / self.island_total

    def is_eup_myeon(self, dong: dict) -> bool:
        return dong["name"].endswith(("읍", "면"))

    # ---------- 문장 읽기 (LLM 없이) ----------
    def find_business(self, text: str) -> int | None:
        for word, name in ALIASES.items():
            if word in text:
                for i, s in enumerate(self.subcats):
                    if s["name"] == name:
                        return i
        best: tuple[int, int, int] | None = None  # (토큰 길이, 점포 수, -번호)
        for i, toks in enumerate(self._sub_tokens):
            hit = max((len(t) for t in toks if t in text), default=0)
            if hit:
                key = (hit, self.subcats[i]["n"], -i)
                if best is None or key > best:
                    best = key
        return -best[2] if best else None

    def find_dong(self, text: str) -> dict | None:
        for d in sorted(self.dongs, key=lambda d: -len(d["name"])):
            if d["name"] in text:
                return d
        return None

    def read_sentence(self, profile: Profile, text: str) -> list[str]:
        """문장에서 조건을 읽어 프로필에 넣고, 바뀐 칸 이름을 돌려준다."""
        changed: list[str] = []
        if "업종 바꾸" in text or "다른 업종" in text:
            profile.business = None
            return ["업종"]
        if any(w in text for w in RESET_WORDS):
            profile.__init__()  # type: ignore[misc]
            return ["처음부터"]
        if "후보로 돌아가" in text or "후보 목록" in text:
            profile.focus = None
            return ["후보로"]
        if "이 후보로 정하" in text and profile.focus:
            profile.final = profile.focus
            return ["최종 후보"]
        if any(w in text for w in RELAX_REGION_WORDS):
            profile.region = None
            profile.settled = True
            changed.append("지역 풀기")
        if any(w in text for w in RELAX_AREA_WORDS):
            profile.area = None
            profile.settled = True
            changed.append("동·읍면 풀기")
        sub = self.find_business(text)
        if sub is not None and sub != profile.business:
            profile.business = sub
            changed.append("업종")
        for w, v in VISIT_WORDS.items():
            if w in text:
                profile.visit = v
                changed.append("손님이 오는 방식")
                break
        for w, v in REGION_WORDS.items():
            if w in text and "지역 풀기" not in changed:
                profile.region = v
                changed.append("지역")
                break
        for w, v in AREA_WORDS.items():
            if w in text and "동·읍면 풀기" not in changed:
                profile.area = v
                changed.append("동·읍면")
                break
        if any(w in text for w in COMPETITION_WORDS):
            profile.competition = True
            changed.append("경쟁")
        if any(w in text for w in NEIGHBOR_WORDS):
            profile.neighbors = True
            changed.append("이웃 가게")
        if any(w in text for w in GROWING_WORDS):
            profile.growing = True
            changed.append("가게가 느는 동네")
        return changed

    # ---------- 판정 ----------
    def judge(self, profile: Profile, dong: dict) -> list[str]:
        """이 동네가 후보에서 빠지는 조건 목록. 비어 있으면 후보다."""
        out: list[str] = []
        if profile.region and not dong["sgg"].startswith(profile.region):
            out.append(f"{profile.region}가 아님")
        if profile.area == "동" and self.is_eup_myeon(dong):
            out.append("읍면 지역")
        if profile.area == "읍면" and not self.is_eup_myeon(dong):
            out.append("동 지역")
        if profile.competition and profile.business is not None:
            share = self.share(dong, profile.business)
            if share > self.island_share(profile.business):
                out.append("같은 업종 비중이 섬 평균보다 높음")
        if profile.neighbors and self.anchor_count(dong) < self.anchor_median:
            out.append("손님을 끌어오는 이웃 가게가 적음")
        if profile.growing and self.growth(dong) < self.island_growth:
            out.append("사업체가 섬 평균보다 덜 늘어남")
        return out

    def share(self, dong: dict, sub: int) -> float:
        return self.sub_count(dong, sub) / dong["total"] if dong["total"] else 0.0

    def saturated(self, dong: dict, sub: int) -> bool:
        return self.share(dong, sub) > SATURATION_RATIO * self.island_share(sub)

    def score(self, profile: Profile, dong: dict) -> float:
        """후보 순서. 경쟁 비중이 낮고 이웃 가게가 많을수록 앞."""
        s = self.anchor_count(dong) / max(self.anchor_median, 1)
        if profile.business is not None:
            s -= self.share(dong, profile.business) / self.island_share(profile.business)
        return round(s, 6)

    def evaluate(self, profile: Profile) -> dict:
        candidates, excluded = [], []
        for d in self.dongs:
            why = self.judge(profile, d)
            if why:
                excluded.append({"code": d["code"], "name": d["name"], "why": why})
            else:
                candidates.append(d)
        candidates.sort(key=lambda d: (-self.score(profile, d), d["code"]))
        reasons: dict[str, int] = {}
        for e in excluded:
            for w in e["why"]:
                reasons[w] = reasons.get(w, 0) + 1
        saturated = []
        if profile.business is not None:
            saturated = [d["name"] for d in candidates if self.saturated(d, profile.business)]
        alternatives = []
        if not candidates:
            for key, label, text in (
                ("region", "지역 조건 해제", "지역 상관없이"),
                ("area", "동·읍면 조건 해제", "동·읍면 상관없이"),
            ):
                if getattr(profile, key):
                    relaxed = Profile.from_dict({**asdict(profile), key: None})
                    names = [d["name"] for d in self.dongs if not self.judge(relaxed, d)]
                    if names:
                        relaxed_sorted = sorted(
                            (d for d in self.dongs if d["name"] in names),
                            key=lambda d: (-self.score(relaxed, d), d["code"]),
                        )
                        alternatives.append(
                            {"label": label, "text": text, "count": len(names), "names": [d["name"] for d in relaxed_sorted]}
                        )
        return {
            "candidates": [d["code"] for d in candidates],
            "candidateNames": [d["name"] for d in candidates],
            "excluded": excluded,
            "excludedReasons": reasons,
            "saturated": saturated,
            "alternatives": alternatives,
        }

    def advisor_metrics(self, dong: dict, sub: int) -> dict:
        """Return the existing card metrics for Advisor to present without recalculating them."""
        food = dong["byCat"][0]
        same = self.sub_count(dong, sub)
        is_food = self.subcats[sub]["cat"] == "음식"
        return {
            "total": dong["total"],
            "anchors": self.anchor_count(dong),
            "density": round(dong["total"] / dong["area_km2"]) if dong["area_km2"] else 0,
            "lodging": dong["byCat"][2],
            "cafes": same,
            "food": food,
            "cafeInFood": round(100 * same / food, 1) if is_food and food else None,
            "growth": self.growth(dong),
        }

    def island_cafe_in_food(self, sub: int) -> float | None:
        if self.subcats[sub]["cat"] != "음식":
            return None
        food = sum(d["byCat"][0] for d in self.dongs)
        return round(100 * self.subcats[sub]["n"] / food, 1) if food else None

    def normalize_consultation_profile(self, profile: Profile, text: str) -> Profile:
        """Use the same local phrase reader for SDK and no-SDK turns, then normalize decision state."""
        p = Profile.from_dict(asdict(profile))
        text = (text or "").strip()
        if not text:
            if p.region:
                p.regionResolved = True
            return p

        p.pendingQuestion = None
        previous_region = p.region
        self.read_sentence(p, text)
        if p.business is not None and p.business >= len(self.subcats):
            p.business = None

        region_choices = [value for word, value in REGION_WORDS.items() if word in text]
        if len(set(region_choices)) > 1 and any(word in text for word in ("중 어디", "어디가", "어디로")):
            p.region = previous_region
        elif region_choices:
            p.region = max(
                ((text.rfind(word), value) for word, value in REGION_WORDS.items() if word in text),
                key=lambda match: match[0],
            )[1]
            p.regionResolved = True
        if any(word in text for word in RELAX_REGION_WORDS):
            p.region = None
            p.regionResolved = True
        elif any(word in text for word in ("아직 모르", "아직 모름")) and p.region is None:
            p.regionResolved = True
        elif p.region or self.find_dong(text):
            p.regionResolved = True

        if any(word in text for word in (
            "생활시설", "생활 이웃", "마트", "병원", "약국", "학원", "생활권", "장을 보", "진료"
        )):
            p.decisionPreference = "anchors"
        elif any(word in text for word in (
            "동종", "같은 업종", "비중 낮", "경쟁", "포화", "덜 붐", "가게가 적"
        )):
            p.decisionPreference = "subShare"
        elif p.preferenceAsked and any(word in text for word in ("아직 모르", "아직 모름", "상관없", "모르겠")):
            p.decisionPreference = "undecided"
        elif p.preferenceAsked and text.upper() in ("A", "A요", "1번", "첫 번째"):
            p.decisionPreference = "anchors"
        elif p.preferenceAsked and text.upper() in ("B", "B요", "2번", "두 번째"):
            p.decisionPreference = "subShare"

        required = set(p.requiredConditions)
        topics = {
            "region": ("제주시", "서귀포"),
            "area": ("동 지역", "동지역", "시내", "읍면", "읍·면", "시골"),
            "competition": ("경쟁", "동종 가게", "같은 업종", "같은 가게"),
            "neighbors": ("생활시설", "마트", "병원", "약국", "학원", "이웃"),
            "growing": ("사업체", "가게", "성장", "늘어"),
        }
        for key, words in topics.items():
            if any(word in text for word in words):
                topic_pattern = r"(?:" + "|".join(re.escape(word) for word in words) + r")"
                only_pattern = re.compile(
                    topic_pattern + r"[^,，.。!?]{0,12}(?:만|뿐)"
                )
                explicit_pattern = re.compile(
                    r"(?:반드시|꼭|무조건)[^,，.。!?]{0,24}" + topic_pattern
                    + r"|" + topic_pattern + r"[^,，.。!?]{0,16}(?:필수|한정|이어야|해야)"
                )
                topic_is_required = bool(only_pattern.search(text) or explicit_pattern.search(text))
                if topic_is_required:
                    required.add(key)
                else:
                    required.discard(key)
        if any(word in text for word in RELAX_REGION_WORDS):
            required.discard("region")
        if any(word in text for word in RELAX_AREA_WORDS):
            required.discard("area")
        p.requiredConditions = [key for key in ("region", "area", "competition", "neighbors", "growing") if key in required]
        return p

    def _consultation_filter_profile(self, profile: Profile) -> Profile:
        p = Profile.from_dict(asdict(profile))
        required = set(p.requiredConditions)
        if "region" not in required:
            p.region = None
        if "area" not in required:
            p.area = None
        if "competition" not in required:
            p.competition = False
        if "neighbors" not in required:
            p.neighbors = False
        if "growing" not in required:
            p.growing = False
        return p

    def rank_consultation(self, profile: Profile, basis: str, limit: int | None = 3) -> list[dict]:
        """Rank the shared required-condition pool by one evidence measure and stable dong ID."""
        if profile.business is None:
            return []
        if basis not in ("anchors", "subShare"):
            raise ValueError("unknown consultation basis")
        if limit is not None and limit < 0:
            raise ValueError("limit")

        filtered = self._consultation_filter_profile(profile)
        rows = []
        for dong in self.dongs:
            if self.judge(filtered, dong):
                continue
            counts = dong.get("sub")
            same = None
            if isinstance(counts, dict):
                value = counts.get(str(profile.business), 0)
                if type(value) is int and value >= 0:
                    same = value
            total = dong.get("total")
            denominator = total if type(total) is int and total >= 0 else None
            share = same / denominator if same is not None and denominator else None
            anchors = self.anchor_count(dong) if isinstance(counts, dict) else None
            metric = anchors if basis == "anchors" else share
            rows.append({
                "id": dong["code"],
                "code": dong["code"],
                "name": dong["name"],
                "basis": basis,
                "metric": metric,
                "anchors": anchors,
                "sameCount": same,
                "denominator": denominator,
                "share": share,
            })

        if basis == "anchors":
            rows.sort(key=lambda row: (row["metric"] is None, -(row["metric"] or 0), row["id"]))
        else:
            rows.sort(key=lambda row: (row["metric"] is None, row["metric"] if row["metric"] is not None else 0, row["id"]))
        for row in rows:
            row["tied"] = row["metric"] is not None and sum(other["metric"] == row["metric"] for other in rows) > 1
        return rows if limit is None else rows[:limit]

    def named_dongs(self, text: str) -> list[dict]:
        """문장에 이름이 나온 동네를 말한 순서대로 돌려준다."""
        hits = [(text.find(dong["name"]), dong["code"]) for dong in self.dongs if dong["name"] in text]
        return [self.by_code[code] for _, code in sorted(hits)]

    def _comparison_basis(self, dong: dict, basis: str, business: int) -> dict:
        """순위와 같은 계산으로 근거 값·같은 업종 수·분모를 읽는다."""
        counts = dong.get("sub")
        same = None
        if isinstance(counts, dict):
            raw = counts.get(str(business), 0)
            same = raw if type(raw) is int and raw >= 0 else None
        total = dong.get("total")
        denominator = total if type(total) is int and total >= 0 else None
        share = same / denominator if same is not None and denominator else None
        anchors = self.anchor_count(dong) if isinstance(counts, dict) else None
        return {
            "anchors": anchors,
            "sameCount": same,
            "denominator": denominator,
            "share": share,
            "metric": anchors if basis == "anchors" else share,
        }

    def _comparison_evidence(self, dong: dict, business: int, *, include_store: bool = True) -> str:
        """793 상가와 830 사업체 활동에서 실제로 있는 값만 적는다. 없는 값은 0으로 채우지 않는다."""
        counts = dong.get("sub")
        total = dong.get("total")
        parts: list[str] = []
        if include_store and isinstance(counts, dict):
            raw = counts.get(str(business), 0)
            same = raw if type(raw) is int and raw >= 0 else None
            if same is not None and type(total) is int and total >= 0:
                name = self.subcats[business]["name"]
                share = same / total if total else None
                percent = f" ({share * 100:.1f}%)" if share is not None else " (분모가 0곳이라 비중을 계산하지 못함)"
                parts.append(f"793 상가 기준 {name} 등록점포 {same}곳 / 전체 등록점포 {total}곳{percent}입니다.")
            elif same is None:
                parts.append("793 상가 기준 같은 업종 등록점포 수가 자료에 없습니다.")
            elif total is None:
                parts.append(f"793 상가 기준 같은 업종 {same}곳이며 전체 등록점포 분모가 자료에 없습니다.")
        stats = dong.get("bizStats")
        active = stats.get("active") if isinstance(stats, dict) else None
        if isinstance(active, dict) and type(active.get("2018")) is int and type(active.get("2023")) is int:
            first, last = active["2018"], active["2023"]
            change = last - first
            sign = "+" if change >= 0 else ""
            growth = f"{change / first * 100:+.1f}%" if first else "계산 불가"
            parts.append(
                f"830 사업체 활동은 2018년 {first:,}곳에서 2023년 {last:,}곳으로 {sign}{change:,}곳 변했습니다. "
                f"증감률은 {dong['name']} {growth}, 제주 전체 {self.island_growth:+.1f}%입니다."
            )
        return " ".join(parts)

    def _comparison_detail(self, dong: dict, basis: str, business: int, value: float | None) -> str:
        """같은 근거값과 분모를 실제 숫자로만 적는다. 없는 값은 0으로 읽히게 쓰지 않는다."""
        basis_values = self._comparison_basis(dong, basis, business)
        if basis == "anchors":
            if value is None:
                return "생활시설 등록점포 수가 자료에 없어 순위를 매기지 못했습니다."
            return f"생활시설 등록점포 {value}곳입니다."
        count, denominator = basis_values["sameCount"], basis_values["denominator"]
        if value is None:
            if denominator is None:
                return "전체 등록점포 분모가 확인되지 않아 같은 업종 비중을 계산하지 못했습니다."
            if denominator == 0:
                return "전체 등록점포가 0곳이라 같은 업종 비중을 계산하지 못했습니다."
            return f"같은 업종 등록점포 수가 확인되지 않아 비중을 계산하지 못했습니다. 전체 등록점포는 {denominator}곳입니다."
        return f"793 상가 기준 같은 업종 {count}곳 / 전체 등록점포 {denominator}곳 ({value * 100:.1f}%)입니다."

    @staticmethod
    def _comparison_display(basis: str, value: float | None) -> str:
        if value is None:
            return "값 없음"
        return f"{value}곳" if basis == "anchors" else f"{value * 100:.1f}%"

    def consultation_comparison(
        self, profile: Profile, basis: str, named: list[dict],
    ) -> dict | None:
        """같은 필수 조건 풀과 같은 근거로 물은 동네와 기준 후보를 나란히 놓는다."""
        if profile.business is None or not named:
            return None
        filtered = self._consultation_filter_profile(profile)
        ranked = self.rank_consultation(filtered, basis, limit=None)
        rank_by_id = {row["id"]: index + 1 for index, row in enumerate(ranked)}
        row_by_id = {row["id"]: row for row in ranked}
        pool = list(ranked)
        excluded = {
            dong["code"]: self.judge(filtered, dong) for dong in self.dongs
            if self.judge(filtered, dong)
        }

        # 물은 동네를 먼저 두고, 하나만 물었으면 같은 풀의 기준 1순위를 옆에 둔다.
        codes: list[str] = []
        for dong in named[:2]:
            if dong["code"] not in codes:
                codes.append(dong["code"])
        for row in pool:
            if len(codes) >= 2:
                break
            if row["id"] not in codes:
                codes.append(row["id"])

        rows: list[dict] = []
        for code in codes:
            dong = self.by_code[code]
            why = excluded.get(code, [])
            metric = row_by_id[code]["metric"] if code in row_by_id else None
            value = None if why else metric
            # 비중 기준이면 근거 문장에 이미 793 수치가 있으므로, 제외된 행에만 793을 추가로 적는다.
            evidence = self._comparison_evidence(
                dong, profile.business, include_store=basis == "anchors" or bool(why)
            )
            if why:
                status = "excluded"
                detail = f"필수 조건({' · '.join(why)})에서 제외되었습니다. {evidence}"
            elif metric is None:
                status = "missing"
                detail = self._comparison_detail(dong, basis, profile.business, None) + (f" {evidence}" if evidence else "")
            elif rank_by_id[code] <= 3:
                status = "eligible"
                detail = (
                    self._comparison_detail(dong, basis, profile.business, value)
                    + f" 후보 {len(pool)}곳 중 {rank_by_id[code]}번째여서 화면의 상위 3곳에 포함됩니다. {evidence}"
                )
            else:
                status = "outsideTop3"
                detail = (
                    self._comparison_detail(dong, basis, profile.business, value)
                    + f" 후보 {len(pool)}곳 중 {rank_by_id[code]}번째여서 화면의 상위 3곳에는 들지 않았습니다. {evidence}"
                )
            rows.append({
                "id": code,
                "name": dong["name"],
                "value": value,
                "detail": detail.strip(),
                "status": status,
            })

        return {
            "basisLabel": BASIS_LABELS[basis],
            "source": "제주데이터허브 상가(상권) 정보 (data/793), 읍면동별 사업체 생멸 통계 (data/830·KOSIS)",
            "date": self.meta["storeDate"],
            "rows": rows,
            "summary": self._comparison_summary(
                basis, rows, total=len(self.dongs), excluded=excluded, pool=pool,
                ranks=rank_by_id, top=[row["name"] for row in pool[:3]],
            ),
        }

    def _comparison_summary(
        self, basis: str, rows: list[dict], *, total: int, excluded: dict[str, list[str]],
        pool: list[dict], ranks: dict[str, int], top: list[str],
    ) -> str:
        """같은 풀에서 왜 그 동네인지만 말한다: 조건 제외, 상위 3곳 밖, 근거 없음."""
        label = BASIS_LABELS[basis]
        missing = sum(1 for row in pool if row["metric"] is None)
        observed = len(pool) - missing
        outside = max(observed - len(top), 0)
        reasons: dict[str, int] = {}
        for why in excluded.values():
            for reason in why:
                reasons[reason] = reasons.get(reason, 0) + 1
        parts = [
            f"비교 모집단은 자료에 등록된 행정동 전체 {total}곳이고, 필수 조건에서 {len(excluded)}곳이 빠져 "
            f"같은 후보 풀 {len(pool)}곳이 남았습니다. 근거는 {label}입니다."
        ]
        if reasons:
            parts.append(
                "조건 제외 이유는 " + ", ".join(f"{reason} {count}곳" for reason, count in sorted(reasons.items())) + "입니다."
            )
        else:
            parts.append("필수 조건으로 빠진 곳은 없습니다.")
        if not pool:
            parts.append(
                "필수 조건을 적용한 뒤 후보가 0곳이라 순위와 상위 3곳을 만들지 않았습니다."
            )
        elif top:
            parts.append(
                f"후보 {len(pool)}곳 중 근거 값을 확인한 {observed}곳만 순위를 매겼고, "
                f"화면에는 상위 {len(top)}곳({', '.join(top)})만 표시했습니다. "
                f"상위 3곳 밖 후보는 {outside}곳, 근거 값이 확인되지 않은 후보는 {missing}곳입니다."
            )
        else:
            parts.append(
                f"후보 {len(pool)}곳 모두 근거 값을 확인하지 못해 순위를 매기지 않았습니다."
            )
        for row in rows:
            name = row["name"]
            if row["status"] == "excluded":
                parts.append(f"{name}: 필수 조건({' · '.join(excluded[row['id']])})에서 제외되었습니다.")
            elif row["status"] == "missing":
                parts.append(f"{name}: 후보이지만 {label} 값을 확인하지 못했습니다.")
            else:
                position = "상위 3곳에 포함됩니다" if row["status"] == "eligible" else "상위 3곳 밖입니다"
                parts.append(
                    f"{name}: 후보 {len(pool)}곳 중 {ranks[row['id']]}번째({label} "
                    f"{self._comparison_display(basis, row['value'])})로 {position}."
                )
        parts.append(
            f"쓴 값은 {self.meta['storeDate']} 상가 자료 기준이고, 순서는 같은 값의 크기와 행정동 코드 순서일 뿐 "
            "어느 동네가 더 낫다는 뜻이 아닙니다."
        )
        return " ".join(parts)

    def consultation_decision(self, profile: Profile, text: str) -> dict:
        """Choose at most one local question or return the locally ordered evidence cards."""
        p = self.normalize_consultation_profile(profile, text)
        filtered = self._consultation_filter_profile(p)
        excluded = [
            {"code": dong["code"], "name": dong["name"], "why": why}
            for dong in self.dongs if (why := self.judge(filtered, dong))
        ]
        excluded_reasons: dict[str, int] = {}
        for item in excluded:
            for reason in item["why"]:
                excluded_reasons[reason] = excluded_reasons.get(reason, 0) + 1
        eligible = [dong for dong in self.dongs if not self.judge(filtered, dong)]
        saturated = [
            dong["name"] for dong in eligible
            if p.business is not None and self.saturated(dong, p.business)
        ]
        choices: list[dict] = []
        results: list[dict] = []
        question = ""
        state = "result"
        basis = "anchors"
        display_rows: list[dict] = []

        request_result = any(word in text for word in ("결과", "후보", "추천", "순위"))
        greeting = text.strip().rstrip(".!?~ ") in ("안녕", "안녕하세요", "안녕하십니까", "반가워요")
        if p.business is None and greeting:
            summary = "안녕하세요. 제주에서 가게 자리를 함께 살펴봅니다. 눈여겨본 동네나 만나고 싶은 손님부터 편하게 말씀해 주세요."
        elif p.business is None:
            state = "question"
            question, choices = self.advisor.consultation_question("business")
            p.pendingQuestion = "business"
            summary = "생각하시는 업종을 알려 주시면 실제 등록 점포 자료를 기준으로 살펴봅니다."
        else:
            all_anchor = self.rank_consultation(p, "anchors")
            same_share = self.rank_consultation(p, "subShare")
            if not request_result and (not p.regionResolved or (not text and p.pendingQuestion == "region")):
                state = "question"
                question, choices = self.advisor.consultation_question("region")
                p.regionResolved = True
                p.pendingQuestion = "region"
                selected = same_share if p.decisionPreference == "subShare" else all_anchor
                basis = "subShare" if p.decisionPreference == "subShare" else "anchors"
                results = [self._consultation_result(row, basis) for row in selected]
                display_rows = selected
                if p.decisionPreference in ("anchors", "subShare"):
                    summary = (
                        "제주 전체 후보를 선택하신 기준 순서로 표시했습니다. 지역을 정하지 않으시면 제주 전체를 유지합니다."
                    )
                else:
                    summary = "제주 전체 자료에서 생활시설 등록점포 수 기준으로 최대 세 곳을 비교용으로 표시했습니다. 지역을 정하지 않으시면 제주 전체를 유지합니다."
            else:
                anchor_ids = [row["id"] for row in all_anchor]
                share_ids = [row["id"] for row in same_share]
                differs = anchor_ids != share_ids
                anchor_by_id = {row["id"]: row for row in all_anchor}
                share_by_id = {row["id"]: row for row in same_share}
                compared_ids = set(anchor_by_id) | set(share_by_id)
                anchor_values = {anchor_by_id[code]["anchors"] for code in compared_ids
                                 if code in anchor_by_id and anchor_by_id[code]["anchors"] is not None}
                share_values = {share_by_id[code]["share"] for code in compared_ids
                                if code in share_by_id and share_by_id[code]["share"] is not None}
                explainable = differs and (len(anchor_values) > 1 or len(share_values) > 1)
                chosen = p.decisionPreference
                pending_preference = not text and p.pendingQuestion == "preference"
                if pending_preference or (chosen is None and explainable and not p.preferenceAsked and not request_result):
                    state = "question"
                    question, choices = self.advisor.consultation_question("preference")
                    p.preferenceAsked = True
                    p.pendingQuestion = "preference"
                    results = [self._consultation_result(row, "anchors") for row in all_anchor]
                    display_rows = all_anchor
                    first_a = all_anchor[0] if all_anchor else None
                    first_b = same_share[0] if same_share else None
                    if first_a and first_b:
                        summary = (
                            f"생활시설 수 기준으로는 {first_a['name']}, 동종 업종 비중 기준으로는 {first_b['name']}이(가) 먼저 나옵니다. "
                            "현재 표시는 비교용 순서이며, 선택한 선호를 뜻하지 않습니다."
                        )
                    else:
                        summary = "두 기준의 후보 순서가 다릅니다. 생활시설 수 순서는 비교용 표시이며, 선택한 선호를 뜻하지 않습니다."
                    basis = "anchors"
                else:
                    if chosen == "subShare":
                        basis, selected = "subShare", same_share
                        display_rows = selected
                        summary = "동종 업종 등록점포 비중이 낮은 순서로 표시했습니다."
                    else:
                        selected = all_anchor
                        display_rows = selected
                        if chosen == "anchors":
                            summary = "생활시설 등록점포 수 순서로 표시했습니다."
                        elif anchor_ids == share_ids:
                            summary = "두 기준의 상위 후보 순서가 같아 선택 질문을 생략했습니다. 생활시설 수 순서는 비교용 표시이며, 선호를 확정한 뜻은 아닙니다."
                        elif request_result or chosen == "undecided" or p.preferenceAsked:
                            summary = "생활시설 수 순서로 비교 결과를 표시했습니다. 이는 비교용 표시 순서이며, 선택한 선호를 뜻하지 않습니다."
                        else:
                            summary = "비교할 근거가 부족해 선호를 묻지 않았습니다. 생활시설 수 순서는 비교용 표시이며, 선택한 선호를 뜻하지 않습니다."
                    results = [self._consultation_result(row, basis) for row in selected]

                if not all_anchor and not same_share:
                    summary = "필수 조건을 적용한 뒤 후보가 없습니다. " + (
                        "제외 이유: " + ", ".join(
                            f"{reason} {count}곳" for reason, count in sorted(excluded_reasons.items())
                        )
                        if excluded_reasons else "해당 지역의 등록 점포 근거가 없습니다."
                    )

        ids = [item["id"] for item in results]
        tied = any(row.get("tied") for row in display_rows)
        if tied:
            summary += " 같은 근거값은 동률로 보고 행정동 코드 순으로 고정했으며, 코드 순은 우열을 뜻하지 않습니다."
        # 이름을 대고 이유를 물었을 때만, 같은 풀·같은 근거로 답한다. 순위와 선호는 그대로 둔다.
        # 결과 턴에서만 내보낸다: 질문 턴은 기준이나 지역을 고르는 중이라 비교할 근거가 아직 없다.
        comparison = None
        if state == "result" and p.business is not None:
            named = self.named_dongs(text)
            asked = any(word in text for word in COMPARE_WORDS)
            if named and (len(named) >= 2 or asked):
                comparison = self.consultation_comparison(p, basis, named)
                if comparison:
                    summary = f"{summary} {comparison['summary']}"
        decision = {
            "state": state,
            "question": question,
            "choices": choices,
            "results": results[:3],
            "summary": summary,
            "basis": basis,
            "profile": p,
            "candidates": ids,
            "candidateNames": [item["name"] for item in results],
            "excluded": excluded,
            "excludedReasons": excluded_reasons,
            "saturated": saturated,
            "alternatives": [],
        }
        if comparison:
            decision["comparison"] = comparison
        return decision

    def _consultation_result(self, row: dict, basis: str) -> dict:
        if basis == "anchors":
            reason = (
                f"생활시설 등록점포 {row['anchors']}곳입니다."
                if row["anchors"] is not None else "생활시설 등록점포 수를 확인하지 못했습니다."
            )
            denominator = "해당 없음"
        else:
            count, denominator_value, share = row["sameCount"], row["denominator"], row["share"]
            if share is None:
                if denominator_value is None:
                    reason = "전체 등록점포 분모가 확인되지 않아 같은 업종 비중을 계산하지 못했습니다."
                elif denominator_value == 0:
                    reason = "전체 등록점포 분모가 0곳이라 같은 업종 비중을 계산하지 못했습니다."
                else:
                    reason = "같은 업종 등록점포 수가 확인되지 않아 비중을 계산하지 못했습니다."
            else:
                reason = f"같은 업종 {count}곳 / 전체 등록점포 {denominator_value}곳 ({share * 100:.1f}%)입니다."
            denominator = denominator_value
        return {
            "id": row["id"],
            "name": row["name"],
            "reason": reason,
            "caution": "등록 점포 분포는 매출이나 실제 손님 수요를 뜻하지 않으므로 현장에서 확인해야 합니다.",
            "date": self.meta["storeDate"],
            "source": "제주데이터허브 상가(상권) 정보 (data/793)",
            "denominator": denominator,
        }

    # ---------- 목 프로필 네 칸 ----------
    def slots(self, profile: Profile) -> list[dict]:
        biz = self.subcats[profile.business]["name"] if profile.business is not None else None
        visit_text = {"목적": "일부러 찾아와 머문다", "지나가다": "매일 들르는 단골", "모름": "아직 모름 (두 예 비교)"}.get(profile.visit or "")
        return [
            {
                "key": "visit",
                "title": "손님이 어떻게 오나",
                "filled": profile.visit is not None,
                "value": visit_text or "아직 몰라요",
                "source": "창업자의 답",
            },
            self._who_slot(),
            {
                "key": "competition",
                "title": "경쟁 조건",
                "filled": profile.competition,
                "value": f"{biz} 비중이 섬 평균 이하인 곳만" if biz and profile.competition else "정하지 않음",
                "source": "허브 상가(상권) 정보 2025-09-18",
            },
            {
                "key": "neighbors",
                "title": "끌어오는 이웃",
                "filled": profile.neighbors,
                "value": "마트·병원·약국·학원이 많은 곳" if profile.neighbors else "아직 조건 없음",
                "source": "허브 상가(상권) 정보 2025-09-18",
            },
        ]

    def _who_slot(self) -> dict:
        status = self.population.status() if self.population else {"live": False, "snapshotDate": None}
        if status["live"]:
            value, filled = "동네를 고르면 허브에서 최신 자료를 받아요", True
        elif status["snapshotDate"]:
            value, filled = f"저장해 둔 {status['snapshotDate']} 자료로 봐요", True
        else:
            value, filled = "허브 연결 전이라 답사로 확인해요", False
        return {
            "key": "who",
            "title": "누가 오나",
            "filled": filled,
            "value": value,
            "source": "허브 읍면동 내국인 유동인구 (거주·근무·방문)",
        }

    def chips(self, profile: Profile, result: dict | None = None) -> list[dict]:
        out: list[dict] = []
        for alt in (result or {}).get("alternatives", []):
            out.append({"label": f"{alt['label']} ({alt['count']}곳)", "text": alt["text"]})
        if profile.business is None:
            for label in ("반찬가게", "카페", "분식집", "빵집", "미용실", "편의점"):
                out.append({"label": label, "text": label})
            return out
        if profile.focus:
            # 동네를 고른 뒤에는 그 동네 안에서 이어 간다 (동네를 다시 묻지 않는다)
            biz = self.subcats[profile.business]["name"]
            out.append({"label": f"{biz} 가게 위치", "text": f"{biz} 가게 어디 있어요?", "ask": True})
            out.append({"label": "마트·병원·약국 위치", "text": "근처 마트 병원 약국", "ask": True})
            if profile.visit is None:
                out.append({"label": "매일 들르는 단골", "text": "매일 들르는 단골 손님"})
                out.append({"label": "일부러 찾아와 머무는 손님", "text": "일부러 찾아와 머무는 손님"})
            else:
                out.append({"label": "후보 목록", "text": "후보 목록으로 돌아가기"})
            out.append({"label": "업종 바꾸기", "text": "업종 바꾸기"})
            return out
        if profile.visit is None:
            out.append({"label": "지나가는 손님", "text": "지나가다 들르는 동네 손님"})
            out.append({"label": "찾아오는 손님", "text": "일부러 찾아오는 손님"})
        if profile.region is None:
            out.append({"label": "제주시", "text": "제주시"})
            out.append({"label": "서귀포시", "text": "서귀포"})
        if profile.area is None:
            out.append({"label": "동 지역", "text": "동 지역"})
            out.append({"label": "읍·면", "text": "읍면"})
        if not profile.competition:
            out.append({"label": "경쟁 적은 동네", "text": "경쟁 적은 곳"})
        if not profile.neighbors and profile.visit != "목적":
            out.append({"label": "마트·병원 인접", "text": "마트 병원 이웃"})
        if not profile.growing:
            out.append({"label": "사업체 증가 동네", "text": "가게가 늘고 있는 동네"})
        names = (result or {}).get("candidateNames", [])
        if 0 < len(names) <= 10:
            for name in names[:3]:
                out.append({"label": name, "text": f"{name} 보기", "dong": True})
        out.append({"label": "초기화", "text": "처음부터"})
        return out

    # ---------- 답사 카드 ----------
    def population_for(self, code: str) -> dict | None:
        """Attach 583 provenance and denominators to the existing Hub aggregate."""
        if self.population is None:
            return None
        # A threaded map rerender must not repeat the Hub's date probe or dong lookup.
        # The existing Hub client caches live results; briefly retain fallbacks too.
        with self._population_lock:
            cached = self._population_fallbacks.get(code)
            if cached and monotonic() < cached[0]:
                pop = cached[1]
            else:
                pop = self.population.for_dong(self.by_code[code]["name"])
                if pop is None or pop["source"] != "live" or pop.get("rows") == 0:
                    self._population_fallbacks[code] = (monotonic() + 60, pop)
                else:
                    self._population_fallbacks.pop(code, None)
        if pop is None or pop.get("rows") == 0:
            return None
        total = pop["resd"] + pop["work"] + pop["visit"]
        mid_age = sum(pop["byAge"].get(age, 0) for age in ("40대", "50대", "60대"))
        return {
            **{key: value for key, value in pop.items() if key != "liveError"},
            "dataset": 583,
            "scope": "dong",
            "denominator": total,
            "workShare": round(100 * pop["work"] / total, 1) if total else None,
            "midAgeCount": mid_age,
        }

    def card(self, profile: Profile, code: str, *, include_population: bool = True,
             population_by_code: dict[str, dict | None] | None = None) -> dict:
        d = self.by_code[code]
        anchors = {
            self.subcats[s]["name"]: self.sub_count(d, s) for s in self.anchor_subs if self.sub_count(d, s)
        }
        top_subs = sorted(d["sub"].items(), key=lambda kv: -kv[1])[:5]
        card: dict = {
            "code": code,
            "name": d["name"],
            "sgg": d["sgg"],
            "total": d["total"],
            "areaKm2": d["area_km2"],
            "anchors": anchors,
            "anchorTotal": sum(anchors.values()),
            "anchorMedian": self.anchor_median,
            "topSubs": [{"name": self.subcats[int(k)]["name"], "n": v} for k, v in top_subs],
            "excludedBy": self.judge(self._consultation_filter_profile(profile), d),
            "storeDate": self.meta["storeDate"],
        }
        reasons: list[str] = []
        checks: list[str] = []
        if profile.business is not None:
            sub = profile.business
            n = self.sub_count(d, sub)
            card["business"] = {
                "name": self.subcats[sub]["name"],
                "count": n,
                "share": round(100 * self.share(d, sub), 1),
                "islandShare": round(100 * self.island_share(sub), 1),
                "saturated": self.saturated(d, sub),
            }
            b = card["business"]
            if b["share"] <= b["islandShare"]:
                reasons.append(
                    f"점포 {d['total']:,}곳 중 {b['name']} 점포는 {n}곳({b['share']}%)으로, 섬 평균 {b['islandShare']}%보다 낮아요."
                )
            if b["saturated"]:
                checks.append(f"{b['name']} 비중이 섬 평균의 1.5배가 넘어요. 같은 거리에 몇 곳이 몰려 있는지 걸어서 세어 보세요.")
        if card["anchorTotal"] >= self.anchor_median:
            reasons.append(f"마트·병원·약국·학원이 {card['anchorTotal']}곳으로 동네 중간값({self.anchor_median:g}곳) 이상이에요.")
        if profile.visit == "지나가다":
            checks.append("퇴근길·등하굣길이 가게 앞을 지나는지 저녁에 직접 걸어 보세요.")
        if profile.visit == "목적":
            checks.append("차를 세울 곳이 있는지, 지도 앱에서 찾기 쉬운 자리인지 확인하세요.")
        checks.append("누가 오가는지는 아직 데이터로 채우지 못했어요. 평일과 주말 낮에 한 번씩 서 있어 보세요.")
        checks.append(f"점포 수는 {self.meta['storeDate']} 기준이에요. 지금 비어 있는 가게가 있는지 확인하세요.")
        a = d["bizStats"]["active"]
        growth = self.growth(d)
        card["bizTrend"] = {
            "dataset": 830,
            "scope": "dong",
            "startYear": 2018,
            "endYear": 2023,
            "change": a["2023"] - a["2018"],
            "active2018": a["2018"],
            "active2023": a["2023"],
            "growth": growth,
            "islandGrowth": self.island_growth,
            "opened2023": d["bizStats"]["opened"].get("2023"),
            "closed2019": d["bizStats"]["closed"].get("2019"),
            "closeRate2019": round(100 * d["bizStats"]["closed"]["2019"] / a["2019"], 1)
            if d["bizStats"]["closed"].get("2019") and a.get("2019")
            else None,
            "source": self.meta["bizStatsSource"],
        }
        if growth >= self.island_growth:
            reasons.append(
                f"사업체가 2018년 {a['2018']:,}곳에서 2023년 {a['2023']:,}곳으로 {growth}% 늘었어요(섬 평균 {self.island_growth}%)."
            )
        pop = None
        if include_population:
            pop = population_by_code[code] if population_by_code is not None and code in population_by_code else self.population_for(code)
        card["population"] = pop
        if pop:
            # 데이터로 채운 칸이므로 일반 문구를 빼고 구성에 맞는 질문으로 바꾼다
            checks = [c for c in checks if not c.startswith("누가 오가는지")]
            if profile.visit == "지나가다" and pop["visitShare"] is not None and pop["visitShare"] > 40:
                checks.insert(0, f"오가는 사람 중 방문객이 {pop['visitShare']}%예요. 주민 장사라면 주택가 쪽 골목을 따로 걸어 보세요.")
            elif profile.visit == "목적" and pop["residentShare"] is not None and pop["residentShare"] > 60:
                checks.insert(0, f"오가는 사람 중 주민이 {pop['residentShare']}%예요. 일부러 찾아올 손님이 있는지 주말에 확인하세요.")
        card["reasons"] = reasons
        card["fieldChecks"] = checks
        return card

    def compare_cards(self, profile: Profile, a: str, b: str) -> dict:
        """두 답사 카드의 서로 다른 비교 지표만 돌려준다."""
        left, right = (self.card(profile, code, include_population=False) for code in (a, b))
        fields = {
            "total": "전체 점포 수",
            "areaKm2": "동네 면적",
            "anchorTotal": "생활 편의시설 수",
            "business.count": "같은 업종 점포 수",
            "business.share": "같은 업종 비중",
            "bizTrend.growth": "사업체 증가율",
        }
        differences = []
        for field, label in fields.items():
            parent, _, child = field.partition(".")
            lv = left.get(parent, {}).get(child) if child else left[parent]
            rv = right.get(parent, {}).get(child) if child else right[parent]
            if lv != rv:
                differences.append({"field": field, "label": label, "a": lv, "b": rv})
        return {"a": left["name"], "b": right["name"], "differences": differences}

    # ---------- 한 차례 ----------
    def apply_conditions(self, profile: Profile, args: dict) -> list[str]:
        """에이전트 도구 set_profile 의 결정적 구현. 바뀐 칸 이름을 돌려준다."""
        changed: list[str] = []
        if args.get("business"):
            sub = self.find_business(str(args["business"]))
            if sub is not None and sub != profile.business:
                profile.business = sub
                changed.append("업종")
        if args.get("visit") in ("목적", "지나가다", "모름"):
            profile.visit = args["visit"]
            changed.append("손님이 오는 방식")
        if args.get("region") in ("제주시", "서귀포시"):
            profile.region = args["region"]
            profile.regionResolved = True
            changed.append("지역")
        elif args.get("region") == "상관없음" and profile.region:
            profile.region = None
            profile.requiredConditions = [key for key in profile.requiredConditions if key != "region"]
            changed.append("지역 풀기")
        if args.get("area") in ("동", "읍면"):
            profile.area = args["area"]
            changed.append("동·읍면")
        elif args.get("area") == "상관없음" and profile.area:
            profile.area = None
            profile.requiredConditions = [key for key in profile.requiredConditions if key != "area"]
            changed.append("동·읍면 풀기")
        for key, label in (("competition", "경쟁"), ("neighbors", "이웃 가게"), ("growing", "가게가 느는 동네")):
            if isinstance(args.get(key), bool) and args[key] != getattr(profile, key):
                setattr(profile, key, args[key])
                if not args[key]:
                    profile.requiredConditions = [item for item in profile.requiredConditions if item != key]
                changed.append(label)
        if args.get("decisionPreference") in ("anchors", "subShare", "undecided"):
            profile.decisionPreference = args["decisionPreference"]
        return changed

    def find_dong_code(self, name: str) -> str | None:
        hit = self.find_dong(name or "")
        return hit["code"] if hit else None

    def respond(self, profile: Profile, changed: list[str], text: str = "", focus: str | None = None, *,
                model_controlled: bool = False, population_by_code: dict[str, dict | None] | None = None) -> dict:
        result = self.evaluate(profile)
        if focus is None:
            focus = profile.focus
        if not model_controlled and focus is None and profile.business is None and changed and result["candidates"] and len(result["candidates"]) <= 5:
            focus = result["candidates"][0]
        if focus != profile.focus or not focus:
            profile.focusBuilding = None
        profile.focus = focus
        message = self.message(profile, result, changed, text, focus)
        if focus and self.place and set(changed) <= {"업종", "동네"} and not (text and not changed and not focus):
            message = self.place.overview_message(profile, focus)
        chips = self.chips(profile, result)
        advice = None
        if profile.business is not None and not model_controlled:
            advice = self.advisor.advise(Profile.from_dict({**asdict(profile), "focus": None}))
            if not focus and (advice["stage"] == "question" or advice.get("candidates")):
                # 업종을 정한 뒤 동네를 고르기 전: 전문가 상담 한 걸음 (질문 하나 또는 후보 2~3곳)
                if not (text and not changed):
                    message = self.advisor.message(profile, advice)
                chips = self.advisor.chips(profile, advice)
                if advice.get("candidates"):
                    result = {**result, "candidates": [c["code"] for c in advice["candidates"]],
                              "candidateNames": [c["name"] for c in advice["candidates"]]}
        out = {
            "agentReply": "",
            "agent": {"used": False, "steps": []},
            "question": None,
            "focus": focus,
            "focusLevel": "building" if profile.focusBuilding else "dong" if focus else "island",
            "focusTarget": profile.focusBuilding or focus,
            "profile": asdict(profile),
            "businessName": self.subcats[profile.business]["name"] if profile.business is not None else None,
            "slots": self.slots(profile),
            "chips": chips,
            "message": message,
            **result,
            "advice": advice,
            "scene": {
                "highlight": result["candidates"],
                "business": profile.business,
                "fly": focus,
            },
        }
        if focus:
            out["card"] = self.card(profile, focus, population_by_code=population_by_code)
        return out

    def letter_focus(self, profile: Profile, text: str) -> str | None:
        """'A 후보 보기'처럼 후보 글자로 고르면 그 동네 코드."""
        m = LETTER_PATTERN.search(text or "")
        if not m or profile.business is None:
            return None
        adv = self.advisor.advise(Profile.from_dict({**asdict(profile), "focus": None}))
        for c in adv.get("candidates", []):
            if c["letter"] == m.group(1):
                return c["code"]
        return None

    def turn(self, profile_dict: dict | None, text: str = "", dong: str | None = None) -> dict:
        """LLM 없이 한 차례: 문장을 낱말로 읽어 조건을 바꾼다."""
        profile = Profile.from_dict(profile_dict)
        changed: list[str] = []
        if text:
            changed = self.read_sentence(profile, text)
            profile = self.normalize_consultation_profile(profile, text)
        focus: str | None = dong or self.letter_focus(profile, text)
        if focus:
            profile.focusBuilding = None
            return self.respond(profile, ["동네"], text, focus)
        if text and not focus:
            focus = self.find_dong_code(text)
        return self.respond(profile, changed, text, focus)

    def summary_for_model(self, result: dict) -> dict:
        """LLM에 보내도 되는 것만 남긴다: 동네 이름, 남은 후보 수, 빠진 조건."""
        return {
            "candidateNames": result["candidateNames"][:10],
            "candidateCount": len(result["candidates"]),
            "excludedReasons": sorted(result["excludedReasons"]),
        }

    def message(self, profile: Profile, result: dict, changed: list[str], text: str, focus: str | None) -> str:
        if changed == ["처음부터"]:
            return "처음부터 다시 볼게요. 어떤 가게를 하고 싶으세요?"
        if text and not changed and not focus:
            return "말씀을 조건으로 바꾸지 못했어요. 아래 버튼으로 골라 주세요."
        if profile.business is None:
            return "어떤 가게를 하고 싶으세요? 아래에서 고르거나 한 문장으로 적어 주세요."
        biz = self.subcats[profile.business]["name"]
        n = len(result["candidates"])
        parts = [f"{biz} 기준으로 후보 동네가 {n}곳 남았어요."]
        if result["saturated"]:
            parts.append(f"이 중 {len(result['saturated'])}곳은 같은 가게가 이미 많아요(포화).")
        if n:
            parts.append("앞쪽 후보: " + ", ".join(result["candidateNames"][:3]) + ".")
        else:
            if result["excludedReasons"].get("같은 업종 비중이 섬 평균보다 높음"):
                share = round(100 * self.island_share(profile.business), 1)
                parts.append(f"고른 지역은 모두 {biz} 비중이 섬 평균({share}%)보다 높아요. 이미 포화예요.")
            for alt in result["alternatives"]:
                parts.append(f"{alt['label']} {alt['count']}곳이 남아요: " + ", ".join(alt["names"][:3]) + ".")
            if not result["alternatives"]:
                parts.append("조건 하나를 풀어 보세요.")
        return " ".join(parts)

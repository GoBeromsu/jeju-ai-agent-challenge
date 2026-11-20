"""전문가처럼 생활 장면을 하나씩 묻고, 답에 따라 비교 기준과 후보를 바꾼다.

- 첫 문장은 업종만 정한다. 경쟁이 적은 곳 같은 조건을 스스로 붙이지 않는다.
- 질문은 한 번에 하나. 답이 어떤 지표를 바꾸는지 함께 말한다. '아직 모름'이면 두 운영 예를 나란히 보여 준다.
- 지표는 모두 상가(상권) 정보와 사업체 생멸 통계에서 센다. 유동인구·임대료·매출은 셈하지 않는다.
"""

from __future__ import annotations

import math
from typing import Literal

from .engine import Engine, Profile
from .place import josa
from .walk import plan
from .context_boundary import CENTER_ACK

VISIT_QUESTION = {
    "id": "visit",
    "text": "손님이 주로 어떻게 올 것 같으세요?",
    "why": "답에 따라 보는 지표가 달라져요. 매일 들르는 단골이면 마트·병원 같은 생활 이웃과 점포가 촘촘한 정도를, 일부러 찾아와 머무는 손님이면 숙박 업소와 같은 업종({name}) 가게가 모인 정도를 먼저 봐요.",
    "options": [
        {"label": "매일 들르는 단골", "text": "매일 들르는 단골 손님"},
        {"label": "일부러 찾아와 머무는 손님", "text": "일부러 찾아와 머무는 손님"},
        {"label": "아직 모름", "text": "아직 모르겠어요"},
    ],
}

BASIS = {
    "지나가다": {
        "title": "매일 들르는 단골 기준",
        "reason": "장보기나 병원 방문처럼 일상 동선에 들르기 좋은지, 생활 편의시설과 점포가 모인 정도를 기준으로 골랐습니다.",
        "explain": "단골은 장 보러, 병원 가는 길에 들러요. 그래서 마트·병원·약국·학원(생활 이웃) 수와 점포가 1㎢에 몇 곳인지(촘촘함)를 먼저 봤어요. 같은 업종({name}) 비중은 순위를 정하는 데 조금만 쓰고 주의점으로 따로 적었어요.",
        "metrics": ["anchors", "density", "cafeInFood"],
    },
    "목적": {
        "title": "일부러 찾아와 머무는 손님 기준",
        "reason": "나들이 중 일부러 찾을 만한 상권인지, 숙박 업소와 같은 업종이 모인 정도를 기준으로 골랐습니다.",
        "explain": "찾아오는 손님은 여행·나들이 중에 와요. 그래서 숙박 업소 수와 같은 업종({name}) 가게가 모여 있는 정도를 먼저 봤어요. 같은 업종이 모인 곳은 경쟁도 많으니 같은 업종 비중을 주의점으로 적었어요.",
        "metrics": ["lodging", "cafes", "cafeInFood"],
    },
}
METRIC_LABEL = {
    "anchors": "생활 이웃 (마트·병원·약국·학원)",
    "density": "점포 촘촘함 (1㎢당)",
    "cafeInFood": "음식점 중 같은 업종 비중",
    "lodging": "숙박 업소",
    "cafes": "같은 업종 가게",
}

NEXT_QUESTION = {
    "지나가다": {
        "id": "area",
        "text": "가게 자리로 시내(동 지역)와 읍·면 중 어디가 생활권에 가까우세요?",
        "why": "단골 장사는 사는 곳과 가까워야 오래 다닐 수 있어요. 답하시면 후보를 그 지역 안에서 다시 골라요.",
        "options": [
            {"label": "시내(동 지역)", "text": "동 지역"},
            {"label": "읍·면", "text": "읍면"},
            {"label": "상관없어요", "text": "동·읍면 상관없이"},
        ],
    },
    "목적": {
        "id": "region",
        "text": "제주시 쪽과 서귀포시 쪽 중 다니시기 편한 곳이 있나요?",
        "why": "찾아오는 손님 장사는 섬 어디든 될 수 있어서, 사장님이 매일 오가기 편한 쪽으로 좁히는 게 좋아요.",
        "options": [
            {"label": "제주시 쪽", "text": "제주시"},
            {"label": "서귀포시 쪽", "text": "서귀포"},
            {"label": "상관없어요", "text": "지역 상관없이"},
        ],
    },
    "모름": {
        "id": "pick-style",
        "text": "두 예 중 어느 쪽이 생각하시는 가게와 더 닮았나요?",
        "why": "닮은 쪽을 고르시면 그 기준으로 후보를 세 곳까지 골라 드려요. 계속 모르셔도 두 예를 지도에서 비교하실 수 있어요.",
        "options": [
            {"label": "A처럼 단골 장사", "text": "매일 들르는 단골 손님"},
            {"label": "B처럼 찾아오는 손님", "text": "일부러 찾아와 머무는 손님"},
        ],
    },
}


class Advisor:
    def __init__(self, engine: Engine) -> None:
        self.e = engine

    def consultation_question(self, stage: str) -> tuple[str, list[dict]]:
        """Supply adaptive question copy and choices; ordering and candidates remain in Engine."""
        questions = {
            "business": (
                "어떤 업종을 생각하고 계세요?",
                [
                    {"label": label, "text": label}
                    for label in ("카페", "반찬가게", "분식집", "빵집", "미용실", "편의점")
                ],
            ),
            "region": (
                "가게를 열 지역으로는 어디를 생각하고 계세요?",
                [
                    {"label": "제주시", "text": "제주시 쪽을 생각하고 있어요."},
                    {"label": "서귀포시", "text": "서귀포시 쪽을 생각하고 있어요."},
                    {"label": "아직 모름", "text": "아직 잘 모르겠어요."},
                ],
            ),
            "preference": (
                "가게 주변에서 어느 쪽이 더 중요하신가요?",
                [
                    {"label": "생활시설 주변", "text": "생활시설 주변을 더 중요하게 봐 주세요."},
                    {"label": "동종 가게 비중 낮은 곳", "text": "동종 가게 비중이 낮은 곳을 더 중요하게 봐 주세요."},
                    {"label": "아직 모름", "text": "아직 잘 모르겠어요."},
                ],
            ),
        }
        try:
            return questions[stage]
        except KeyError as error:
            raise ValueError("unknown consultation question") from error

    # ---------- 지표 ----------
    def metrics(self, d: dict, sub: int) -> dict:
        return self.e.advisor_metrics(d, sub)

    def island_cafe_in_food(self, sub: int) -> float | None:
        return self.e.island_cafe_in_food(sub)

    def ranking(self, profile: Profile, visit: str) -> list[dict]:
        """Adapt the legacy visitor labels to Engine's one authoritative evidence ranking."""
        basis = "anchors" if visit == "지나가다" else "subShare"
        rows = self.e.rank_consultation(profile, basis, limit=None)
        return [
            {"code": row["id"], "name": row["name"], "score": row["metric"],
             "m": self.metrics(self.e.by_code[row["id"]], profile.business)}
            for row in rows
        ]

    # ---------- 후보 카드 ----------
    def candidate(self, letter: str, row: dict, profile: Profile, visit: str, style: str | None = None) -> dict:
        d = self.e.by_code[row["code"]]
        m = row["m"]
        sub = profile.business
        name = self.e.subcats[sub]["name"]
        island = self.island_cafe_in_food(sub)
        why, cautions = [], []
        if visit == "지나가다":
            why.append(f"생활 이웃 {m['anchors']}곳 (43개 동네 중간값 {self.e.anchor_median:g}곳)")
            why.append(f"점포 {m['total']:,}곳, 1㎢당 {m['density']:,}곳")
        else:
            why.append(f"숙박 업소 {m['lodging']:,}곳 (점포 {m['total']:,}곳 중)")
            why.append(f"{name} {m['cafes']}곳이 모여 있음")
        if m["cafeInFood"] is not None and island is not None:
            line = f"음식점 {m['food']:,}곳 중 {name} {m['cafes']}곳 ({m['cafeInFood']}%, 섬 평균 {island}%)"
            if m["cafeInFood"] > island:
                cautions.append(f"{line}. 같은 업종이 많은 편이라 무엇으로 다르게 할지 정해 두세요.")
            else:
                why.append(line)
        if visit == "지나가다":
            cautions.append("집과 생활권이 가까운지, 매일 오가기 편한지 직접 다녀 보세요.")
        else:
            cautions.append("교통 자료는 아직 없어요. 손님이 차로 온다고 가정하면 주차할 곳과 지도 앱에서 찾기 쉬운지 확인하세요.")
            cautions.append("평일 낮과 주말에 한 번씩 가 보세요. 손님 수는 자료로 확인하지 못했어요.")
        return {
            "letter": letter,
            "code": d["code"],
            "name": d["name"],
            "sgg": d["sgg"],
            "kind": "읍·면" if self.e.is_eup_myeon(d) else "동",
            "style": style or BASIS[visit]["title"],
            "reason": BASIS[visit]["reason"],
            "metrics": m,
            "why": why,
            "cautions": cautions,
            "storeDate": self.e.meta["storeDate"],
        }

    def advise(self, profile: Profile) -> dict:
        """지금 프로필에 맞는 다음 한 걸음: 질문 하나 또는 후보 묶음."""
        if profile.business is None:
            return {"stage": "business"}
        if profile.visit is None:
            name = self.e.subcats[profile.business]["name"]
            return {"stage": "question", "question": {**VISIT_QUESTION, "why": VISIT_QUESTION["why"].format(name=name)}}
        name = self.e.subcats[profile.business]["name"]
        if profile.visit == "모름":
            a = self.ranking(profile, "지나가다")
            destination = self.ranking(profile, "목적")
            # 한 동네만 남아도 두 운영 방식을 비교할 수 있다.
            b = [r for r in destination if not a or r["code"] != a[0]["code"]] or destination[:1]
            cands = []
            if a:
                cands.append(self.candidate("A", a[0], profile, "지나가다", "운영 예 A: 매일 들르는 단골"))
            if b:
                cands.append(self.candidate("B", b[0], profile, "목적", "운영 예 B: 찾아와 머무는 손님"))
            return {
                "stage": "compare",
                "basisTitle": "두 운영 예 비교",
                "explain": f"아직 정하지 않으셔도 돼요. 같은 {name}라도 단골 장사면 생활 이웃이 많은 곳(A), 찾아오는 손님 장사면 숙박과 {name}가 모인 곳(B)을 먼저 봐요. 지도에서 두 곳을 비교해 보세요.",
                "candidates": cands,
                "alternatives": [],
                "question": NEXT_QUESTION["모름"],
            }
        rows = self.ranking(profile, profile.visit)
        basis = BASIS[profile.visit]
        cands = [self.candidate(L, r, profile, profile.visit) for L, r in zip("ABC", rows[:3])]
        settled = profile.area is not None or profile.region is not None or profile.settled
        return {
            "stage": "candidates",
            "basisTitle": basis["title"],
            "explain": basis["explain"].format(name=name),
            "metricKeys": basis["metrics"],
            "metricLabels": {k: METRIC_LABEL[k] for k in basis["metrics"]},
            "candidates": cands,
            "alternatives": [r["name"] for r in rows[3:6]],
            "question": None if settled else NEXT_QUESTION[profile.visit],
        }

    def chips(self, profile: Profile, adv: dict) -> list[dict]:
        out: list[dict] = []
        q = adv.get("question")
        if q:
            out += [{"label": o["label"], "text": o["text"]} for o in q["options"]]
        for c in adv.get("candidates", []):
            out.append({"label": f"{c['letter']} {c['name']}", "text": f"{c['letter']} 후보 보기", "dong": True, "code": c["code"]})
        if adv["stage"] in ("candidates", "compare"):
            if profile.visit != "지나가다":
                out.append({"label": "단골 기준으로 보기", "text": "매일 들르는 단골 손님"})
            if profile.visit != "목적":
                out.append({"label": "찾아오는 손님 기준으로 보기", "text": "일부러 찾아와 머무는 손님"})
        out.append({"label": "처음부터", "text": "처음부터"})
        return out

    def message(self, profile: Profile, adv: dict) -> str:
        if adv["stage"] == "business":
            return "어떤 가게를 생각하고 계세요? 한 문장으로 적어 주세요. 예: 제주에 작은 카페를 열고 싶어요."
        name = self.e.subcats[profile.business]["name"]
        if adv["stage"] == "question":
            q = adv["question"]
            return f"{josa(name, ('을', '를'))} 생각하고 계시군요. {q['text']} {q['why']}"
        cands = adv["candidates"]
        if not cands:
            return "지금 조건에 맞는 동네가 없어요. 조건 하나를 풀어 보세요."
        names = ", ".join(f"{c['letter']} {c['name']}" for c in cands)
        # 기준 설명과 숫자는 카드에 있으니 말풍선에서는 되풀이하지 않는다
        text = f"{adv['basisTitle']}에 맞춰 골랐어요. 먼저 볼 곳은 {josa(names, ('이에요', '예요'))}. 이유와 주의점은 카드에 있어요."
        if adv.get("alternatives"):
            text += f" 다음 후보는 {josa(', '.join(adv['alternatives']), ('이에요', '예요'))}."
        text += " 지도의 글자나 아래 카드를 누르면 그 동네로 들어가요."
        return text


def _schema(properties: dict, *required: str) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_NUM = {"type": "number"}
_INT = {"type": "integer"}
TOOL_SPECS = {
    "ask_user": ("판단에 필요한 질문 하나와 사용자 상황에 맞는 보기 2~3개를 직접 만든다. 고정 보기를 생략해서 요청하지 않는다.",
                 _schema({"question": _STR, "options": {"type": "array", "minItems": 2, "maxItems": 3,
                                                         "items": _schema({"label": _STR, "text": _STR}, "label", "text")}},
                         "question", "options")),
    "set_profile": ("사용자가 말한 조건과 선호만 반영한다. 업종은 사용자의 표현 그대로 넣는다.",
                    _schema({"business": _STR, "visit": {**_STR, "enum": ["목적", "지나가다", "모름"]},
                             "region": {**_STR, "enum": ["제주시", "서귀포시", "상관없음"]},
                             "area": {**_STR, "enum": ["동", "읍면", "상관없음"]},
                             "competition": _BOOL, "neighbors": _BOOL, "growing": _BOOL,
                             "decisionPreference": {**_STR, "enum": ["anchors", "subShare", "undecided"]}})),
    "rank_places": ("정해진 방문 방식에 맞는 후보와 추천 근거를 조회한다.", _schema({})),
    "contrast_cases": ("단골형과 목적 방문형의 운영 예를 하나씩 조회한다.", _schema({})),
    "compare": ("두 동네(이름·코드·후보 글자)의 다른 지표만 비교한다.", _schema({"a": _STR, "b": _STR}, "a", "b")),
    "focus_map": ("제주 전체, 동네(이름·코드·후보 글자), 건물(지도 대상 ID 또는 '선택 건물')로 이동한다.",
                  _schema({"target": _STR, "level": {**_STR, "enum": ["island", "dong", "building"]}}, "target", "level")),
    "inspect_place": ("지정한 동네/현재 동네의 세계 좌표 x,z 주변 점포를 검색한다. 좌표를 생략하면 선택한 지점을 쓴다.",
                      _schema({"code": _STR, "x": _NUM, "z": _NUM})),
    "population": ("동네의 관측 유동인구를 조회한다. 이름·코드·후보 글자를 받는다.", _schema({"dong": _STR}, "dong")),
    "walk_route": ("등록 점포 ID(store)를 기준으로 정류장부터 목적지까지의 가정 보행을 본다. 선택한 점포면 code와 store를 생략할 수 있다.",
                   _schema({"code": _STR, "store": _INT, "dest": _INT, "stop": _STR})),
    "reset": ("상담과 지도 조건을 초기화한다.", _schema({})),
}

TOOL_LABELS = dict(zip(TOOL_SPECS, (
    "결정 질문", "상담 조건 반영", "후보 순위 조회", "운영 사례 비교", "동네 차이 비교",
    "지도 초점 이동", "주변 점포 조사", "관측 인구 조회", "가정 보행 경로", "상담 초기화",
), strict=True))


class ToolInputError(ValueError):
    """도구 요청에 존재하지 않는 대상 또는 누락된 값이 있다."""


class Consultation:
    """한 차례의 도구 상태를 보관하며 화면용 자료와 모델용 요약을 분리한다."""

    def __init__(self, engine: Engine, profile: Profile, text: str, selection: dict | None = None) -> None:
        self.e = engine
        self.profile = profile
        self.text = text
        self.selection = selection or {}
        self.changed: list[str] = []
        self.steps: list[dict] = []
        self.calls: list[dict] = []
        self.question: dict | None = None
        self.clarify: dict | None = None
        self.context: dict[str, dict | None] = {}
        self.population_code: str | None = None
        self.population_by_code: dict[str, dict | None] = {}

    def dong(self, target: str) -> str | None:
        target = target.strip()
        if target in ("현재 동네", "선택 동네", "여기"):
            return self.selection.get("dong") or self.profile.focus
        if not target:
            return None
        return (target if target in self.e.by_code else
                self.e.letter_focus(self.profile, target) or self.e.find_dong_code(target))

    def focus_map(self, target: str, level: Literal["island", "dong", "building"]) -> str:
        p = self.profile
        match level:
            case "island":
                code, building = None, None
            case "dong":
                code, building = self.dong(target), None
                if not code:
                    raise ToolInputError("해당 동네가 없습니다.")
            case "building":
                building = p.focusBuilding if target == "선택 건물" else target
                code = self.e.place.building_dong(building) if self.e.place and building else None
                if not code:
                    raise ToolInputError("해당 건물 대상이 없습니다.")
            case _:
                raise ToolInputError("지도 범위를 확인하지 못했습니다.")
        p.focus, p.focusBuilding = code, building
        return self.e.by_code[code]["name"] if code else "제주 전체"

    def call(self, name: str, args: dict) -> dict:
        # 이전 SDK 스크립트가 재생하는 도구 이름도 받되 새 정의에는 넣지 않는다.
        aliases = {"set_conditions": "set_profile", "show_dong": "focus_map", "ask_clarify": "ask_user"}
        tool = aliases.get(name, name)
        self.calls.append({"name": name, "args": args})
        kind = {"population": "observed", "walk_route": "assumed"}.get(tool, "computed")
        if name == "show_dong":
            inputs = {"target": args.get("name", ""), "level": "dong"}
        else:
            inputs = args
        try:
            summary, result = self._dispatch(tool, inputs)
        except (ToolInputError, KeyError, TypeError, ValueError, OverflowError):
            summary = "입력값에 해당하는 자료를 찾지 못했습니다."
            result = {"status": summary}
        self.steps.append({"tool": tool, "label": TOOL_LABELS.get(tool, "도구 조회"), "args": inputs,
                           "summary": summary, "kind": kind})
        return result

    def _dispatch(self, tool: str, args: dict) -> tuple[str, dict]:
        e, p = self.e, self.profile
        match tool:
            case "reset":
                self.profile = Profile()
                self.context.clear()
                self.population_code = None
                self.population_by_code.clear()
                self.changed = ["처음부터"]
                return "상담 조건을 초기화했습니다.", {"status": "초기화"}
            case "set_profile":
                changed = e.apply_conditions(p, args)
                self.profile = e.normalize_consultation_profile(p, self.text)
                p = self.profile
                if changed:
                    self.context.pop("advice", None)
                self.changed.extend(changed)
                result = e.summary_for_model(e.evaluate(e._consultation_filter_profile(p)))
                result["조건"] = {"business": e.subcats[p.business]["name"] if p.business is not None else None,
                                "visit": p.visit, "region": p.region, "area": p.area}
                return "상담 조건을 확인하고 후보를 찾았습니다.", result
            case "ask_user":
                return self._ask(args)
            case "rank_places" | "contrast_cases":
                visit = "모름" if tool == "contrast_cases" else p.visit
                advice = e.advisor.advise(Profile.from_dict({**vars(p), "visit": visit}))
                self.context["advice"] = advice
                rows = advice.get("candidates", [])
                candidates = [{k: row[k] for k in ("letter", "name", "style", "reason")} for row in rows]
                return ("운영 방식에 맞는 후보와 추천 근거를 확인했습니다." if rows else "현재 조건으로 추천할 후보를 찾지 못했습니다.",
                        {"candidateCount": len(rows), "candidates": candidates, "stage": advice["stage"]})
            case "compare":
                a, b = self.dong(args["a"]), self.dong(args["b"])
                if not a or not b:
                    raise ToolInputError
                difference = e.compare_cards(p, a, b)
                self.context["comparison"] = difference
                directions = [{"지표": row["label"], "높은쪽": difference["a"] if row["a"] > row["b"] else difference["b"]}
                              for row in difference["differences"]]
                return "두 동네에서 지표가 다른 부분만 비교했습니다.", {"a": difference["a"], "b": difference["b"], "differences": directions}
            case "focus_map":
                name = self.focus_map(args["target"], args["level"])
                return f"{name} 지도 초점을 맞췄습니다.", {"보여준동네": name}
            case "population":
                code = self.dong(args["dong"])
                if not code:
                    raise ToolInputError
                name = e.by_code[code]["name"]
                if code not in self.population_by_code:
                    self.population_by_code[code] = e.population_for(code)
                pop = self.population_by_code[code]
                self.population_code = code
                status = {"live": "허브 조회 관측 자료", "snapshot": "저장된 관측 자료"}.get(pop["source"] if pop else "", "자료 없음")
                return f"{name}의 인구 자료 상태: {status}.", {
                    "동네": name, "kind": "observed", "source": status,
                    "화면": "오른쪽 상권 근거 카드에 관측 집계값과 기준일을 표시합니다." if pop else "표시할 관측값이 없습니다.",
                }
            case "center_lookup":
                try:
                    lookup = getattr(e, "center_lookup", None)
                    result = lookup(args.get("query")) if callable(lookup) else None
                except Exception:
                    result = None
                self.context["centerResult"] = result if result is not None else {"status": "자료 없음"}
                return "제한 자료 조회를 처리했습니다.", CENTER_ACK
            case "inspect_place" | "walk_route":
                return self._location_tool(tool, args)
            case _:
                raise ToolInputError

    def _location_tool(self, tool: str, args: dict) -> tuple[str, dict]:
        e = self.e
        if not e.place:
            raise ToolInputError
        selected_dong = self.selection.get("dong") or self.profile.focus
        code = self.dong(args["code"]) if args.get("code") else selected_dong
        if not code:
            raise ToolInputError
        same_selection = code == selected_dong
        name = e.by_code[code]["name"]
        if tool == "inspect_place":
            point = [float(args.get(axis, self.selection.get(axis) if same_selection else None)) for axis in ("x", "z")]
            if not all(map(math.isfinite, point)):
                raise ToolInputError
            inspected = e.place.inspect(self.profile, code, *point)
            self.context["nearby"] = inspected
            idx = self.selection.get("idx") if same_selection else None
            if idx is not None:
                store = e.place.store(self.profile, code, idx)
                self.context["store"] = store
            meanings = {"same": "같은 업종이라 경쟁과 차별화를 살필 대상입니다.",
                        "similar": "비슷한 업종이라 손님층이 겹칠 수 있습니다.",
                        "anchor": "일상 방문 목적지라 단골의 동선을 살필 대상입니다.",
                        "other": "직접 경쟁보다 거리의 분위기를 보여 주는 곳입니다."}
            reasons = sorted({meanings[item["relation"]] for item in inspected["items"]})
            return "주변 점포와 계획 업종의 관계를 확인했습니다.", {"동네": name, "관계": reasons}
        store = args.get("store", self.selection.get("idx") if same_selection else None)
        if type(store) is not int:
            raise ToolInputError
        stop = args.get("stop")
        walk = plan(e.walk, e.place, code, store, args.get("dest"), [stop] if stop else None)
        self.context["walk"] = walk
        status = "관측이 아닌 가정 보행 경로입니다." if walk["supported"] else "이 동네는 가정 보행 자료가 없습니다."
        return status, {"동네": name, "kind": "assumed", "status": status}

    def _ask(self, args: dict) -> tuple[str, dict]:
        options = []
        for value in args.get("options") or []:
            if len(options) == 3:
                break
            label, text = (value, value) if isinstance(value, str) else (value.get("label"), value.get("text"))
            if isinstance(label, str) and isinstance(text, str) and label.strip() and text.strip():
                options.append({"label": label.strip()[:80], "text": text.strip()[:120]})
        question = (args.get("question") or "").strip()
        if not question or len(options) < 2:
            raise ToolInputError("질문과 보기 2개 이상을 직접 만들어야 합니다.")
        self.question = {"text": question[:200], "options": options}
        if "reason" in args:
            self.clarify = {"question": self.question["text"], "reason": args["reason"],
                            "options": [o["label"] for o in self.question["options"]]}
        return "결정 질문 하나를 보냈습니다.", {"status": "사용자 답변 대기"}

    def finish(self, reply: str = "") -> dict:
        self.profile = self.e.normalize_consultation_profile(self.profile, self.text)
        out = self.e.respond(self.profile, self.changed, "", model_controlled=True, population_by_code=self.population_by_code)
        out.update(self.context)
        if self.population_code and self.population_code != self.profile.focus:
            out["population"] = self.population_by_code[self.population_code]
        nearby = self.context.get("nearby")
        if nearby:
            out["highlightStores"] = [item["idx"] for item in nearby["items"]]
            out["highlightLabel"] = "주변 점포"
            out["keepCamera"] = True
        advice = self.context.get("advice")
        if advice and advice.get("candidates"):
            out["candidates"] = [c["code"] for c in advice["candidates"]]
            out["candidateNames"] = [c["name"] for c in advice["candidates"]]
            out["scene"]["highlight"] = out["candidates"]
            out["chips"] = self.e.advisor.chips(self.profile, {**advice, "question": None})
        if advice:
            advisor_question = advice.get("question")
            out["advice"] = {**advice, "question": (
                {**(advisor_question or {}), **self.question} if self.question else None)}
        elif self.question:
            default = self.e.advisor.advise(self.profile).get("question")
            out["advice"] = {"stage": "question", "question": {"why": "", **(default or {}), **self.question}}
        if self.question:
            out["question"] = self.question
            out["message"] = self.question["text"]
            if self.clarify:
                out["clarify"] = self.clarify
            if self.question["options"]:
                remaining = [] if self.clarify else [c for c in out["chips"] if c.get("dong")]
                out["chips"] = [*self.question["options"], *remaining,
                                {"label": "처음부터" if self.clarify else "초기화", "text": "처음부터"}]
        elif reply:
            out["message"] = reply
        # The local decision fixes the question and the ranking; this turn only reports what the model wrote.
        out["agentReply"] = reply
        out["agent"] = {"used": True, "calls": self.calls, "steps": self.steps}
        assistant = " ".join(part for part in (reply, self.question["text"] if self.question else "") if part)
        # An empty turn must not become an empty assistant message in the next model request.
        self.profile.history = [
            *self.profile.history,
            {"role": "user", "text": self.text},
            *([{"role": "assistant", "text": assistant[:200]}] if assistant else []),
        ][-8:]
        out["profile"]["history"] = self.profile.history
        return out

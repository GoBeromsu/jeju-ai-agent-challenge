"""Shared local decision finalization for SDK and no-SDK turns."""

from __future__ import annotations

import re
from dataclasses import asdict
from functools import lru_cache

from .engine import Engine, Profile

MAX_REPLY_CHARS = 300
_NUMBER = re.compile(r"\d")
_QUESTION_MARK = re.compile(r"[?？]")
# 자료에 없는 수치·수요를 주장하는 표현은 모델 문장으로 인정하지 않는다.
_UNSAFE_CLAIMS = ("매출", "수요", "수익", "유동인구", "유동 인구", "임대료", "보장", "확실")
# 우열을 단정하는 표현은 질문에도 답에도 쓰지 않는다.
_UNSAFE_SUPERIOR = ("최고", "최적", "최우수", "1위", "우수", "우세", "압도")
# 결과 턴에서 비교·추천을 단정하는 표현도 엔진 몫이다.
_UNSAFE_ORDER = ("반드시", "무조건", "가장 좋", "더 좋", "더 낫", "유리한")
_RANK_WORDS = ("먼저", "우선", "1순위", "첫 번째")


@lru_cache(maxsize=1)
def _decision_engine() -> Engine:
    """The bundled 793 snapshot is fixed; this engine performs local-only calculations."""
    return Engine()


@lru_cache(maxsize=1)
def _dong_names() -> frozenset[str]:
    return frozenset(dong["name"] for dong in _decision_engine().dongs)


def _model_path(response: dict) -> bool:
    """Provider prose is eligible only on a general model turn; rules and restricted turns never are."""
    agent = response.get("agent")
    if not isinstance(agent, dict) or agent.get("used") is not True:
        return False
    if agent.get("path") not in (None, "model"):
        return False
    return "제한" not in str(agent.get("reason") or "")


def _clean(value) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _grounded(text: str, allowed: set[str]) -> bool:
    """Every dong named in provider prose must already be in the local evidence."""
    return {name for name in _dong_names() if name in text} <= allowed


def _unbacked_number(text: str) -> bool:
    """Dong names contain digits, so only numbers outside them count as a claim."""
    for name in _dong_names():
        text = text.replace(name, "")
    return bool(_NUMBER.search(text))


def _claims_unsafe(text: str, words: tuple[str, ...]) -> bool:
    """A weekday like 수요일 is not a demand claim, so it must not count as one."""
    return any(word in text.replace("수요일", "") for word in words)


def _region_claims(text: str) -> set[str]:
    claims = set()
    if "서귀포" in text:
        claims.add("서귀포시")
    if "제주시" in text:
        claims.add("제주시")
    return claims


def _grounded_regions(decision: dict) -> set[str]:
    """A region is grounded when the filter applied it or every shown candidate sits in it."""
    regions: set[str] = set()
    profile = decision["profile"]
    if "region" in profile.requiredConditions and profile.region:
        regions.add(profile.region)
    engine = _decision_engine()
    shown = {engine.by_code[item["id"]]["sgg"] for item in decision["results"] if item["id"] in engine.by_code}
    return regions | (shown if len(shown) == 1 else set())


def _sentences(text: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"(?<=[.!?])\s*", text.strip()) if part.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def _model_reply(response: dict, decision: dict) -> str:
    """Keep the provider's own sentence only where local evidence already fixed the answer."""
    if decision["state"] != "result" or not _model_path(response):
        return ""
    # The contract allows 한두 문장; a longer answer starts claiming more than the cards show.
    sentences = _sentences(_clean(response.get("agentReply")))
    text = " ".join(sentences)
    if not text or len(sentences) > 2 or len(text) > MAX_REPLY_CHARS:
        return ""
    if _unbacked_number(text) or _QUESTION_MARK.search(text):
        return ""
    if any(word in text for word in _UNSAFE_ORDER) or _claims_unsafe(text, _UNSAFE_CLAIMS + _UNSAFE_SUPERIOR):
        return ""
    allowed = set(decision["candidateNames"])
    engine = _decision_engine()
    # A dong the local turn already opened (card/focus) is evidence too; a new one is not.
    for code in (decision["profile"].focus, response.get("focus")):
        if isinstance(code, str) and code in engine.by_code:
            allowed.add(engine.by_code[code]["name"])
    if not _grounded(text, allowed):
        return ""
    # A region the local evidence does not support cannot be reported as the basis of this ranking.
    if _region_claims(text) - _grounded_regions(decision):
        return ""
    # "먼저/우선" is a rank claim: the first dong named must be the local first candidate.
    if decision["results"] and any(word in text for word in _RANK_WORDS):
        named = sorted((text.find(dong["name"]), dong["name"])
                       for dong in _decision_engine().dongs if dong["name"] in text)
        if named and named[0][1] != decision["results"][0]["name"]:
            return ""
    return text


def _model_question(response: dict, decision: dict) -> str:
    """Adopt provider wording only for the question the local engine already chose."""
    if decision["state"] != "question" or not _model_path(response):
        return ""
    question = response.get("question")
    if not isinstance(question, dict):
        return ""
    text = _clean(question.get("text"))
    options = question.get("options")
    if not text or len(text) > 200 or not isinstance(options, list):
        return ""
    # A different answer set is a different question: the local copy and its choices win.
    # Only the answer texts (what next turn sends) identify the question; labels are display-only.
    offered = [_clean(option.get("text")) for option in options if isinstance(option, dict)]
    if len(offered) != len(decision["choices"]) or set(offered) != {choice["text"] for choice in decision["choices"]}:
        return ""
    if _unbacked_number(text) or _claims_unsafe(text, _UNSAFE_CLAIMS + _UNSAFE_SUPERIOR):
        return ""
    return text if _grounded(text, set(decision["candidateNames"])) else ""


def _flat(text: str) -> str:
    return re.sub(r"[\s.!?,·:;\"'()\[\]]", "", text)


def _summary_with_reply(summary: str, reply: str) -> str:
    """One provider sentence may follow the local evidence, never replace or echo it."""
    sentences = _sentences(reply)
    if not sentences or _echoes(summary, sentences):
        return summary
    return f"{summary} {sentences[0]}" if summary else sentences[0]


def _keep_reply(summary: str, reply: str) -> str:
    """A reply that partly repeats the local summary must not print twice next to it."""
    return "" if not reply or _echoes(summary, _sentences(reply)) else reply


def _echoes(summary: str, sentences: list[str]) -> bool:
    left = _flat(summary)
    return bool(left) and any(
        right and (right in left or left in right) for right in map(_flat, sentences)
    )


def finalize_turn(response: dict, text: str, profile: Profile | dict | None) -> dict:
    """Apply the same deterministic consultation decision after any interpretation path."""
    if not isinstance(response, dict):
        raise TypeError("response")

    raw_profile = response.get("profile")
    if isinstance(raw_profile, dict):
        current = Profile.from_dict(raw_profile)
    elif isinstance(profile, Profile):
        current = Profile.from_dict(asdict(profile))
    else:
        current = Profile.from_dict(profile)

    engine = _decision_engine()
    decision = engine.consultation_decision(current, text)
    current = decision["profile"]
    # One question and one ranking: the engine fixes both, provider wording may only rephrase them.
    question = _model_question(response, decision) or decision["question"]
    reply = _model_reply(response, decision)
    # The card summary carries one provider sentence; message stays the local guidance it was.
    summary = _summary_with_reply(decision["summary"], reply)
    reply = _keep_reply(decision["summary"], reply)
    message = f"{question} {decision['summary']}" if question else decision["summary"]
    notebook = {
        "state": decision["state"],
        "question": question,
        "choices": decision["choices"],
        "results": decision["results"][:3],
        "summary": summary,
    }
    if decision.get("comparison") is not None:
        notebook["comparison"] = decision["comparison"]
    if text:
        history = list(current.history)
        user_entry = {"role": "user", "text": text[:200]}
        assistant_entry = {
            "role": "assistant",
            "text": " ".join(part for part in (question, summary) if part)[:200],
        }
        if len(history) >= 2 and history[-2] == user_entry and history[-1]["role"] == "assistant":
            history[-1] = assistant_entry
        elif history and history[-1] == user_entry:
            history.append(assistant_entry)
        else:
            history.extend((user_entry, assistant_entry))
        current.history = history[-8:]

    out = dict(response)
    out["profile"] = asdict(current)
    out["notebook"] = notebook
    out["candidates"] = decision["candidates"]
    out["candidateNames"] = decision["candidateNames"]
    out["excluded"] = decision["excluded"]
    out["excludedReasons"] = decision["excludedReasons"]
    out["saturated"] = decision["saturated"]
    out["alternatives"] = decision["alternatives"]
    out["question"] = (
        {"text": question, "options": decision["choices"]}
        if decision["state"] == "question" else None
    )
    out["message"] = message
    # The provider may keep its own sentence, but never a ranking, a number, or question authority.
    out["agentReply"] = reply
    out["chips"] = list(decision["choices"])
    if decision["state"] == "result":
        out["chips"].extend(
            {"label": item["name"], "text": f"{item['name']} 보기", "dong": True, "code": item["id"]}
            for item in notebook["results"]
        )
    out["chips"].append({"label": "초기화", "text": "처음부터"})

    advice = dict(out.get("advice") or {})
    advice.update(
        stage="question" if decision["state"] == "question" else "candidates",
        explain=summary,
        question={"text": question, "options": decision["choices"]}
        if decision["state"] == "question" else None,
        candidates=[
            {
                "letter": chr(ord("A") + index),
                "code": item["id"],
                "name": item["name"],
                "reason": item["reason"],
                "style": decision["basis"],
                "why": [item["reason"]],
                "cautions": [item["caution"]],
                "storeDate": item["date"],
            }
            for index, item in enumerate(notebook["results"])
        ],
    )
    out["advice"] = advice
    if "clarify" in out:
        out["clarify"] = None

    scene = dict(out.get("scene") or {})
    scene["highlight"] = decision["candidates"]
    scene["business"] = current.business
    scene["fly"] = out.get("focus")
    out["scene"] = scene
    return out

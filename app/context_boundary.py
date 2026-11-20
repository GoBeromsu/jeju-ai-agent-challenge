"""Server-owned session mode and local-only restricted turn execution."""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass

from .engine import Profile

GENERAL = "general"
RESTRICTED = "restricted"
CENTER_ACK = {"status": "완료"}


@dataclass(frozen=True)
class ContextDecision:
    context_id: str
    mode: str
    reset: bool = False
    fresh: bool = False


class ContextBoundary:
    """Keep a context restricted until an explicit reset starts a new context."""

    def __init__(self) -> None:
        self._contexts: dict[str, str] = {}
        self._default_context_id: str | None = None
        self._lock = threading.Lock()
        self.center_lookup = lambda _query: None

    def lookup_center(self, query):
        return self.center_lookup(query)

    def enter(
        self,
        context_id: str | None,
        requested_mode: str,
        *,
        reset: bool = False,
        force_local: bool = False,
    ) -> ContextDecision:
        if requested_mode not in (GENERAL, RESTRICTED):
            raise ValueError("contextMode")
        if type(reset) is not bool:
            raise ValueError("resetContext")
        if context_id is not None and (not isinstance(context_id, str) or not context_id or len(context_id) > 128):
            raise ValueError("contextId")

        with self._lock:
            if reset:
                previous = context_id or self._default_context_id
                if previous in self._contexts:
                    self._contexts[previous] = "retired"
                mode = RESTRICTED if force_local or requested_mode == RESTRICTED else GENERAL
                fresh_id = secrets.token_urlsafe(24)
                self._contexts[fresh_id] = mode
                if previous == self._default_context_id or self._default_context_id is None:
                    self._default_context_id = fresh_id
                return ContextDecision(fresh_id, mode, reset=True, fresh=True)

            if context_id is None:
                if self._default_context_id is not None:
                    current = self._contexts.get(self._default_context_id)
                    if current == "retired":
                        fresh_id = secrets.token_urlsafe(24)
                        self._contexts[fresh_id] = RESTRICTED
                        self._default_context_id = fresh_id
                        return ContextDecision(fresh_id, RESTRICTED, fresh=True)
                    mode = RESTRICTED if force_local or requested_mode == RESTRICTED or current == RESTRICTED else GENERAL
                    self._contexts[self._default_context_id] = mode
                    return ContextDecision(self._default_context_id, mode, fresh=mode != current)
                mode = RESTRICTED if force_local or requested_mode == RESTRICTED else GENERAL
                fresh_id = secrets.token_urlsafe(24)
                self._contexts[fresh_id] = mode
                self._default_context_id = fresh_id
                return ContextDecision(fresh_id, mode, fresh=True)

            current = self._contexts.get(context_id)
            if current != GENERAL and current != RESTRICTED:
                # An unknown or retired ID is not a new general authorization.
                fresh_id = secrets.token_urlsafe(24)
                self._contexts[fresh_id] = RESTRICTED
                self._default_context_id = fresh_id
                return ContextDecision(fresh_id, RESTRICTED, fresh=True)

            mode = RESTRICTED if force_local or requested_mode == RESTRICTED or current == RESTRICTED else GENERAL
            self._contexts[context_id] = mode
            return ContextDecision(context_id, mode, fresh=mode != current)


def requires_local_context(body: dict) -> bool:
    """Recognize source-specific restricted operations before any lookup or model call."""
    if body.get("contextMode") == RESTRICTED:
        return True
    if any(key in body and body[key] is not None and body[key] is not False for key in ("centerQuery", "restrictedLookup")):
        return True
    return body.get("dataSource") in ("center", "restricted-center")


def _local_center_turn(engine, profile: Profile, text: str, selection: dict, query, changed: list[str]) -> dict:
    from .advisor import Consultation

    turn = Consultation(engine, profile, text, selection)
    turn.changed = changed
    if profile.focus:
        turn.population_by_code[profile.focus] = None
    turn.call("center_lookup", {"query": query})
    out = turn.finish()
    out["agent"] = {"used": False, "calls": [], "steps": []}
    return out


def local_turn(
    engine,
    profile_dict: dict | None,
    text: str,
    *,
    selection: dict | None = None,
    map_focus: dict | None = None,
    dong: str | None = None,
    center_query=None,
) -> dict:
    """Run a restricted turn using deterministic rules and bundled data only."""
    from .advisor import Consultation

    profile = Profile.from_dict(profile_dict)
    selection = dict(selection or {})
    if map_focus is None and dong:
        map_focus = {"level": "dong", "target": dong}
    if map_focus is not None:
        Consultation(engine, profile, text, selection).focus_map(map_focus.get("target", ""), map_focus["level"])

    changed = engine.read_sentence(profile, text) if text else []
    selected_code = selection.get("dong") or profile.focus
    selected_place = None
    if selection.get("type") == "store":
        idx = selection.get("store")
        if not selected_code or type(idx) is not int or idx not in engine.place.details(selected_code):
            raise ValueError("이 동네 점포가 아닙니다.")
        x, z = engine.place.xz[idx]
        selection.update(dong=selected_code, idx=idx, x=x, z=z)
        selected_place = selected_code

    focus = None if map_focus is not None else engine.letter_focus(profile, text)
    if focus is None and map_focus is None:
        focus = engine.find_dong_code(text)
    if selected_place:
        focus = selected_place
    if focus:
        profile.focus = focus
        profile.focusBuilding = None

    if center_query is not None:
        return _local_center_turn(engine, profile, text, selection, center_query, changed)

    # Restricted context never falls back to the separate Hub 583 population source.
    population_by_code = {profile.focus: None} if profile.focus else {}
    return engine.respond(profile, changed, text, focus, population_by_code=population_by_code)

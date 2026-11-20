"""Public entrypoint for the Vercel Python runtime.

Vercel runs several workers, and a request may land on a different one than the
request that minted a context ID. The public boundary therefore keeps no
session memory: an ID carries its own mode, and `enter()` is a pure function of
the request, so every worker answers identically.

- `pub-…` is the only ID accepted as a general consultation.
- `res-…` stays restricted on every worker (fail closed, never promoted).
- Unknown, retired, or malformed IDs fail closed to a fresh restricted context.

The `pub-` prefix is guessable and carries no secret. That is deliberate: it
grants nothing but the public Kakao JS key the browser loads anyway, and the
restricted side stays closed because only `pub-` ever reads as general.

The public deployment holds no restricted center data and no Hub client, so a
restricted hint is served by local rules only and never leaves the process. A
restricted *request* never reaches the model; the only restricted material this
deployment could hold (center rows, Hub population) does not exist here, so a
client that replays its own transcript carries nothing it did not already have.

Free-form answers are rules-only by default. A deployment opts in to the real
OpenAI model by configuring one environment variable in the Vercel project
settings: `OPENAI_API_KEY`. This module reads the process environment only and
never a local `.env`, so a key left on a laptop cannot be uploaded by accident
and the checked-in `.env.example` cannot enable paid traffic. `OPENAI_MODEL`
and `OPENAI_PROJECT` are optional environment overrides.

Even when enabled, the model serves unrestricted general context only:
restricted hints, `res-…` IDs, and pasted-data payloads take the local rules
path, and every model turn is capped by `MAX_CALLS` / `MAX_OUTPUT_TOKENS`
(app.agent_openai) plus the wall-clock budget below. With no key this is the
same rules-only deployment as before; a key that is present but unusable (model
older than the pinned generation, SDK missing) also stays on the rules path and
reports the reason through `/api/status` → `agent.lastError`.
"""

from __future__ import annotations

import os
import secrets
import threading
import time

from app.agent_openai import DEFAULT_MODEL, OpenAIAgent, check_model
from app.context_boundary import GENERAL, RESTRICTED, ContextBoundary, ContextDecision
from app.engine import Engine
from app.hub import HubClient, Population
from app.place import Place
from app.server import WEB, Handler

PUBLIC_PREFIX = "pub-"
RESTRICTED_PREFIX = "res-"
_TOKEN_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")

REQUEST_TIMEOUT = 12.0  # seconds per model call; the SDK client also runs with max_retries=0
# One /api/turn gets 20s of model work. The Vercel project must allow a function
# duration above this (Hobby/Pro defaults are far higher); a shorter limit simply
# terminates the request, and the next turn starts from the same rules data.
TURN_BUDGET = 20.0


def _mint(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(18)


def _is_minted(context_id: object, prefix: str) -> bool:
    """Format check only: no worker memory, no shared secret, no promotion."""
    if not isinstance(context_id, str) or not context_id.startswith(prefix):
        return False
    token = context_id[len(prefix):]
    return 16 <= len(token) <= 48 and set(token) <= _TOKEN_CHARS


class PublicContextBoundary(ContextBoundary):
    """Worker-independent boundary for the public deployment.

    The inherited in-memory `_contexts` is never read or written here. Mode is
    derived from the requested mode and the ID's own prefix, so a session that
    started on another worker keeps its general profile instead of collapsing to
    a restricted context.
    """

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

        # Any restricted hint (mode, center query, restricted source) is local-only.
        if force_local or requested_mode == RESTRICTED:
            if not reset and _is_minted(context_id, RESTRICTED_PREFIX):
                return ContextDecision(context_id, RESTRICTED)
            return ContextDecision(_mint(RESTRICTED_PREFIX), RESTRICTED, reset=reset, fresh=True)

        if reset:
            return ContextDecision(_mint(PUBLIC_PREFIX), GENERAL, reset=True, fresh=True)

        if _is_minted(context_id, PUBLIC_PREFIX):
            return ContextDecision(context_id, GENERAL)

        # An unknown, retired, restricted, or malformed ID is not a new general authorization.
        return ContextDecision(_mint(RESTRICTED_PREFIX), RESTRICTED, fresh=True)

    @staticmethod
    def is_public_general(context_id: object) -> bool:
        return _is_minted(context_id, PUBLIC_PREFIX)


class _BoundedResponses:
    """Clamps each model call to the time left in the current turn.

    The budget itself is checked by `PublicModelAgent._create` before a call is
    metered; this wrapper only keeps one call from outliving what remains. The
    per-request `timeout` is an SDK request option, not a body field.
    """

    def __init__(self, responses, agent: "PublicModelAgent") -> None:
        self._responses = responses
        self._agent = agent

    def create(self, **kwargs):
        remaining = self._agent.deadline - self._agent.clock()
        kwargs["timeout"] = min(REQUEST_TIMEOUT, max(0.1, remaining))
        return self._responses.create(**kwargs)


class _BoundedClient:
    def __init__(self, client, agent: "PublicModelAgent") -> None:
        self.responses = _BoundedResponses(client.responses, agent)


class _PublicAgent(OpenAIAgent):
    """No file logging: the public runtime has no durable disk and nothing to append to."""

    def _log(self, record: dict) -> None:
        pass


class PublicModelAgent(_PublicAgent):
    """The regular OpenAI agent with a per-turn deadline; credentials stay env-only.

    The turn budget is per thread so one request cannot extend or shorten the
    deadline of another sharing a warm worker.
    """

    def __init__(self, engine: Engine, *, client, model: str, clock=time.monotonic) -> None:
        super().__init__(engine, client=None, model=model, enabled=True)
        self.clock = clock
        self._budget = threading.local()
        self._client = _BoundedClient(client, self)

    @property
    def deadline(self) -> float:
        return getattr(self._budget, "deadline", 0.0)

    def _create(self, input_items: list, meter: dict):
        # Checked before the call is metered, so `modelCalls` never counts a request that was not sent.
        if self.deadline - self.clock() <= 0:
            raise TimeoutError("public model turn budget exhausted")
        return super()._create(input_items, meter)

    def turn(self, profile_dict: dict | None, text: str, *, selection: dict | None = None,
             context_mode: str = GENERAL) -> dict:
        self._budget.deadline = self.clock() + TURN_BUDGET
        try:
            return super().turn(profile_dict, text, selection=selection, context_mode=context_mode)
        finally:
            self._budget.deadline = 0.0


def _openai_client(api_key: str, project: str | None):
    from openai import OpenAI

    return OpenAI(api_key=api_key, project=project, timeout=REQUEST_TIMEOUT, max_retries=0)


def _rules_only(engine: Engine, reason: str | None = None, model: str = DEFAULT_MODEL) -> OpenAIAgent:
    agent = _PublicAgent(engine, model=model, enabled=False)
    agent.last_error = reason
    return agent


def build_agent(engine: Engine, environ=None, *, client_factory=_openai_client,
                clock=time.monotonic) -> OpenAIAgent:
    """Rules-only unless the deployment configured an API key in its environment.

    `environ` defaults to the process environment; `.env` files are deliberately
    not consulted here. Any configuration problem leaves the deployment on the
    rules path and reports a short reason through `/api/status` (`lastError`).
    """
    env = os.environ if environ is None else environ
    key = (env.get("OPENAI_API_KEY") or "").strip()
    model = (env.get("OPENAI_MODEL") or "").strip() or DEFAULT_MODEL
    if not key:
        return _rules_only(engine)

    try:
        check_model(model)
    except ValueError:
        return _rules_only(engine, "OPENAI_MODEL")

    try:
        client = client_factory(key, (env.get("OPENAI_PROJECT") or "").strip() or None)
    except Exception as e:  # SDK missing or client misconfigured: stay on rules
        return _rules_only(engine, type(e).__name__, model)

    return PublicModelAgent(engine, client=client, model=model, clock=clock)


public_population = Population(client=HubClient(project_key=""))
public_population.snapshot = {}  # no Hub data is bundled with the public rules/AI deployment


class handler(Handler):
    context_boundary = PublicContextBoundary()
    engine = Engine(population=public_population)
    engine.place = Place(engine)
    engine.center_lookup = context_boundary.lookup_center  # resolves to None: no center source here
    agent = build_agent(engine)
    offline = False  # Kakao JS configuration is public; Hub is disabled and the model needs an env key.

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB, **kwargs)

    def context_is_public_general(self, context_id: str) -> bool:
        # No worker memory: the ID's own prefix decides, so a map key is never granted
        # to a restricted or unknown ID on any worker.
        return PublicContextBoundary.is_public_general(context_id)

    def list_directory(self, path):
        self.send_error(404)
        return None

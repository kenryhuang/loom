"""Call-before-send admission with durable reservations, including unknown replies."""

import inspect
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import is_dataclass, replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from loom.core import err, make_loom_error
from loom.service.contracts import ServiceError


class BudgetLedger:
    def __init__(self, database, *, clock=None):
        self.database = str(database)
        self.clock = clock or (lambda: datetime.now(UTC))

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def reserve(self, identifier, binding, tokens, category):
        with self.transaction() as db:
            prior = db.execute("SELECT * FROM agent_budget_calls WHERE id=?", (identifier,)).fetchone()
            if prior:
                if (
                    prior["agent_id"] != binding["agent_id"]
                    or prior["reserved"] != tokens
                    or prior["category"] != category
                    or prior["goal_id"] != binding.get("goal_id")
                    or prior["work_package_id"] != binding.get("work_package_id")
                ):
                    raise ServiceError("Budget call identity conflict", 409)
                return dict(prior)
            row = db.execute("SELECT body FROM agents WHERE id=?", (binding["agent_id"],)).fetchone()
            if not row:
                raise ServiceError("Agent no longer available", 409)
            agent = json.loads(row[0])
            if agent["state"] != "enabled":
                raise ServiceError("Agent is paused; new calls are deferred", 409)
            policy = agent["role"]["budget"]
            window = self.clock().astimezone(ZoneInfo(agent["role"]["timezone"])).date().isoformat()
            clauses = [("daily", "agent_id=? AND window=?", (agent["id"], window), f"window={window}, timezone={agent['role']['timezone']}")]
            if binding.get("goal_id"):
                clauses.append(("goal", "agent_id=? AND goal_id=?", (agent["id"], binding["goal_id"]), f"goal_id={binding['goal_id']}"))
            if binding.get("work_package_id") and category != "learning":
                clauses.append(
                    ("work", "agent_id=? AND work_package_id=? AND category<>?", (agent["id"], binding["work_package_id"], "learning"),
                     f"work_package_id={binding['work_package_id']}")
                )
            if category == "learning":
                clauses.append(("learning", "agent_id=? AND window=? AND category=?", (agent["id"], window, category),
                                f"window={window}, timezone={agent['role']['timezone']}"))
            failures = []
            for scope, where, args, context in clauses:
                maximum = policy[f"{scope}_tokens"]
                actual, reserved = db.execute(
                    f"SELECT COALESCE(SUM(actual),0), COALESCE(SUM(CASE WHEN actual IS NULL THEN reserved ELSE 0 END),0) "
                    f"FROM agent_budget_calls WHERE {where}", args,
                ).fetchone()
                used = actual + reserved
                if used + tokens > maximum:
                    remedy = ("or wait for the next daily window" if scope in {"daily", "learning"}
                              else "(cumulative budget; does not reset daily)")
                    failures.append(
                        f"Agent {scope} token budget exhausted: limit={maximum}, used={used} "
                        f"(actual={actual}, reserved_or_unknown={reserved}), requested={tokens}, "
                        f"projected={used + tokens}, remaining={max(0, maximum - used)}, exceeded_by={used + tokens - maximum} tokens; "
                        f"{context}, category={category}; increase {scope}_tokens {remedy}"
                    )
            calls = db.execute("SELECT COUNT(*) FROM agent_budget_calls WHERE agent_id=? AND window=?", (agent["id"], window)).fetchone()[0]
            if calls >= policy["max_calls"]:
                failures.append(
                    f"Agent daily call budget exhausted: limit={policy['max_calls']}, used={calls}, requested=1, "
                    f"projected={calls + 1}, remaining=0, exceeded_by={calls + 1 - policy['max_calls']} calls; "
                    f"window={window}, timezone={agent['role']['timezone']}, category={category}; increase max_calls or wait for the next daily window"
                )
            if failures:
                raise ServiceError(" | ".join(failures), 409, code="AGENT_BUDGET")
            db.execute(
                "INSERT INTO agent_budget_calls VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    agent["id"],
                    binding.get("goal_id"),
                    binding.get("work_package_id"),
                    window,
                    category,
                    tokens,
                    None,
                    "reserved",
                    self.clock().isoformat(),
                ),
            )
        return {"id": identifier, "reserved": tokens, "state": "reserved"}

    def next_window(self, aid):
        with self.transaction() as db:
            agent = json.loads(db.execute("SELECT body FROM agents WHERE id=?", (aid,)).fetchone()[0])
        local = self.clock().astimezone(ZoneInfo(agent["role"]["timezone"]))
        return (local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).astimezone(UTC).isoformat()

    def settle(self, identifier, actual):
        with self.transaction() as db:
            row = db.execute("SELECT state,actual FROM agent_budget_calls WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise ServiceError("Unknown budget reservation", 409)
            if row["state"] == "settled":
                if row["actual"] != actual:
                    raise ServiceError("Budget settlement conflict", 409)
                return
            if actual is not None and (type(actual) is not int or actual < 0):
                raise ServiceError("Invalid usage")
            db.execute("UPDATE agent_budget_calls SET actual=?,state=? WHERE id=?", (actual, "settled" if actual is not None else "unknown", identifier))

    def summary(self, aid):
        with self.transaction() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT category,window,COUNT(*) AS calls,SUM(COALESCE(actual,reserved)) AS tokens,"
                    "SUM(actual IS NULL) AS unknown_calls FROM agent_budget_calls WHERE agent_id=? GROUP BY category,window",
                    (aid,),
                )
            ]


class BudgetProvider:
    """Wrap every physical provider request, including streaming and verification."""

    def __init__(self, provider, ledger, binding, *, category="execution", output_tokens=None):
        output_tokens = output_tokens or binding.get("output_tokens", 8192)
        if is_dataclass(provider) and hasattr(provider, "max_completion_tokens"):
            provider = replace(provider, max_completion_tokens=min(getattr(provider, "max_completion_tokens", None) or output_tokens, output_tokens))
        self.provider, self.ledger, self.binding = provider, ledger, binding
        self.category, self.output_tokens = category, output_tokens
        self.model = provider.model
        for name in ("verification_provider", "visual_verification_provider"):
            child = getattr(provider, name, None)
            if child is not None:
                setattr(self, name, BudgetProvider(child, ledger, binding, category="acceptance", output_tokens=output_tokens))

    @property
    def wrapped_provider(self):
        return self.provider

    def with_provider(self, provider):
        return BudgetProvider(provider, self.ledger, self.binding, category=self.category, output_tokens=self.output_tokens)

    def __getattr__(self, name):
        if name == "stream_chat":
            if hasattr(self.provider, name):
                return self._stream_chat
            raise AttributeError(name)
        return getattr(self.provider, name)

    def _reserve(self, messages, tools):
        # UTF-8 byte upper bound retains multimodal prompt metadata; actual usage settles it.
        size = len(str(messages).encode()) + len(str(tools or []).encode()) + self.output_tokens
        identifier = "call_" + uuid.uuid4().hex
        self.ledger.reserve(identifier, self.binding, size, self.category)
        return identifier

    def _settle(self, identifier, response):
        usage = getattr(response, "usage", None)
        tokens = getattr(usage, "total_tokens", None)
        self.ledger.settle(identifier, tokens if tokens and tokens > 0 else None)

    async def chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        try:
            identifier = self._reserve(messages, tools)
        except ServiceError as exc:
            return err(make_loom_error("AGENT_BUDGET", str(exc), retryable=True))
        kwargs = dict(tools=tools, cancellation=cancellation)
        if tool_choice is not None and "tool_choice" in inspect.signature(self.provider.chat).parameters:
            kwargs["tool_choice"] = tool_choice
        try:
            response = await self.provider.chat(messages, **kwargs)
            self._settle(identifier, response.value if response.ok else None)
            return response
        except BaseException:
            self.ledger.settle(identifier, None)
            raise

    async def _stream_chat(self, messages, tools=None, cancellation=None, tool_choice=None):
        identifier = self._reserve(messages, tools)
        settled = False
        kwargs = dict(tools=tools, cancellation=cancellation)
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        try:
            async for event in self.provider.stream_chat(messages, **kwargs):
                if event.kind == "completed":
                    self._settle(identifier, event.response)
                    settled = True
                yield event
        finally:
            if not settled:
                self.ledger.settle(identifier, None)

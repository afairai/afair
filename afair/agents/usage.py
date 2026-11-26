"""Durable operational provider ledger and atomic UTC-day spending reservations.

Separate encrypted SQLite avoids holding the substrate write lock across network I/O.
Unknown-price calls fail closed only when a dollar budget has been configured.
"""

from __future__ import annotations

import json
import math
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog

from afair.substrate.db import _open_connection, get_vault_key

if TYPE_CHECKING:
    from collections.abc import Iterator

    from afair.settings import Settings


class BackgroundBudgetExceeded(Exception):
    """Defer work without inventing success. ``reason`` is public-safe taxonomy."""

    def __init__(self, message: str, *, reason: str = "unspecified") -> None:
        super().__init__(message)
        self.reason = (
            reason
            if reason
            in {
                "unspecified",
                "pricing_unavailable",
                "ledger_unavailable",
                "unpriced_attempts",
                "daily_budget_exhausted",
            }
            else "unspecified"
        )


_SCOPE: ContextVar[tuple[Settings, str] | None] = ContextVar("background_usage", default=None)


@contextmanager
def background_usage(settings: Settings, worker: str) -> Iterator[None]:
    """Attribute nested judge, retry and escalation calls to their owning worker."""
    token = _SCOPE.set(_SCOPE.get() or (settings, worker))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def _key(settings: Settings) -> bytes | None:
    return (
        settings.vault_key.get_secret_value().encode("utf-8")
        if settings.vault_key
        else get_vault_key()
    )


def _connection(settings: Settings) -> Any:
    settings.vault_dir.mkdir(parents=True, exist_ok=True)
    conn = _open_connection(settings.vault_dir / "usage.db", vault_key=_key(settings))
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("""CREATE TABLE IF NOT EXISTS provider_usage (
        id TEXT PRIMARY KEY, day TEXT NOT NULL, started_at TEXT NOT NULL,
        worker TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
        reserved_usd REAL NOT NULL, cost_usd REAL, prompt_tokens INTEGER,
        completion_tokens INTEGER, cost_source TEXT NOT NULL,
        operation TEXT NOT NULL DEFAULT 'completion', cache_read_tokens INTEGER,
        cache_write_tokens INTEGER)""")
    # Older ledgers acquired these columns after release. Recheck under
    # the write lock so concurrent first calls cannot race an ALTER TABLE.
    additions = {
        "operation": "TEXT NOT NULL DEFAULT 'completion'",
        "cache_read_tokens": "INTEGER",
        "cache_write_tokens": "INTEGER",
    }
    if not additions.keys() <= {
        row[1] for row in conn.execute("PRAGMA table_info(provider_usage)")
    }:
        try:
            conn.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(provider_usage)")}
            for name, definition in additions.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE provider_usage ADD COLUMN {name} {definition}")
            conn.commit()
        except BaseException:
            conn.close()
            raise
    conn.execute("CREATE INDEX IF NOT EXISTS provider_usage_day ON provider_usage(day)")
    conn.commit()
    return conn


def _get(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _price(model: str, prompt: float, output: int, settings: Settings) -> float | None:
    import litellm

    override = settings.background_model_prices.get(model)
    if override is not None:
        return (prompt * override[0] + output * override[1]) / 1_000_000
    try:
        incoming, outgoing = litellm.cost_per_token(
            model=model, prompt_tokens=prompt, completion_tokens=output
        )
        price = float(incoming + outgoing)
        return price if math.isfinite(price) and price >= 0 else None
    except Exception:
        return None


def provider_completion(*, _operation: str = "completion", **kwargs: Any) -> Any:
    """One accounted attempt. Disable SDK retries so every attempt has its own row.

    The reservation bounds text input by UTF-8 bytes plus tool schema overhead,
    and doubles input price to cover one-hour cache creation. Unknown multimodal
    input sizes or unknown model prices defer under an enabled cap.
    """
    import litellm

    request = getattr(litellm, _operation)
    scope = _SCOPE.get()
    if scope is None:
        return request(**kwargs)
    settings, worker = scope
    model = str(kwargs["model"])
    serialized = json.dumps(kwargs.get("messages", []), ensure_ascii=False)
    tools = json.dumps(kwargs.get("tools", []), ensure_ascii=False)
    prompt_bound = len((serialized + tools).encode("utf-8")) + 1024
    max_output = int(kwargs.get("max_tokens", 2000))
    cache_factor = 2 if "cache_control" in serialized + tools else 1
    reserve = (
        _price(model, prompt_bound * cache_factor, max_output, settings)
        if _operation == "completion"
        else None
    )
    # Compressed image bytes and URLs cannot bound visual tokenization.
    # Reserve the model's entire documented input context instead, so vision
    # remains usable without fetching URLs in the accounting layer.
    unknown_input = False
    if '"image_url"' in serialized:
        try:
            context_limit = (
                1_000_000
                if model in {"anthropic/claude-haiku-5-5", "anthropic/claude-sonnet-5-5"}
                else int(litellm.get_model_info(model)["max_input_tokens"] or 0)
            )
            if context_limit <= 0:
                raise ValueError("missing input context limit")
            reserve = _price(model, context_limit * cache_factor, max_output, settings)
        except Exception:
            unknown_input = True
    cap = settings.background_daily_budget_usd if _operation == "completion" else None
    if cap is not None and (reserve is None or unknown_input):
        raise BackgroundBudgetExceeded(
            f"no safe cost bound for {model}", reason="pricing_unavailable"
        )
    now = datetime.now(UTC)
    ident = uuid4().hex
    kwargs["num_retries"] = 0
    kwargs["max_retries"] = 0

    def ledger_unavailable(exc: Exception, message: str) -> Any:
        # Remote embeddings predate accounting. Self-hosters with no configured
        # budget retain availability. A configured spending guard always defers.
        if _operation == "embedding" and settings.background_daily_budget_usd is None:
            structlog.get_logger(__name__).warning(
                "usage.embedding_unaccounted", failure_class=type(exc).__name__
            )
            return request(**kwargs)
        raise BackgroundBudgetExceeded(message, reason="ledger_unavailable") from exc

    try:
        conn = _connection(settings)
    except Exception as exc:
        return ledger_unavailable(exc, "provider usage ledger unavailable")
    try:
        try:
            _reserve(conn, settings, cap, reserve, now, ident, worker, model, _operation)
        except BackgroundBudgetExceeded:
            raise
        except Exception as exc:
            return ledger_unavailable(exc, "provider usage reservation unavailable")
        try:
            response = request(**kwargs)
        except BaseException as exc:
            # Only definitive rejected requests are known to be unbilled.
            status = _get(exc, "status_code")
            unbilled = status in (400, 401, 403, 404, 429)
            with suppress(Exception):
                conn.execute(
                    "UPDATE provider_usage SET status='failed',cost_usd=?,cost_source=? WHERE id=?",
                    (
                        0.0 if unbilled else None,
                        "unbilled_rejection"
                        if unbilled
                        else ("reservation" if reserve is not None else "unknown"),
                        ident,
                    ),
                )
                conn.commit()
            raise
        # A settlement failure must not discard a paid result or trigger a
        # duplicate retry. The already committed reservation remains charged.
        try:
            _settle(conn, response, model, settings, reserve, ident, _operation)
        except Exception as exc:
            with suppress(Exception):
                conn.rollback()
            structlog.get_logger(__name__).warning(
                "usage.settlement_failed", error_type=type(exc).__name__
            )
        return response
    finally:
        with suppress(Exception):
            conn.close()


def _reserve(
    conn: Any,
    settings: Settings,
    cap: float | None,
    reserve: float | None,
    now: datetime,
    ident: str,
    worker: str,
    model: str,
    operation: str,
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    unknown = conn.execute(
        "SELECT COUNT(*) FROM provider_usage WHERE day=? AND cost_usd IS NULL "
        "AND cost_source='unknown' AND operation='completion'",
        (now.date().isoformat(),),
    ).fetchone()[0]
    if cap is not None and unknown:
        conn.rollback()
        raise BackgroundBudgetExceeded(
            "daily ledger contains unpriced provider attempts", reason="unpriced_attempts"
        )
    spent = conn.execute(
        "SELECT COALESCE(SUM(COALESCE(cost_usd,reserved_usd)),0) "
        "FROM provider_usage WHERE day=? AND operation='completion'",
        (now.date().isoformat(),),
    ).fetchone()[0]
    if cap is not None and spent + (reserve or 0) > cap:
        conn.rollback()
        raise BackgroundBudgetExceeded(
            "daily background provider budget exhausted", reason="daily_budget_exhausted"
        )
    conn.execute(
        "INSERT INTO provider_usage (id,day,started_at,worker,model,status,reserved_usd,cost_usd,prompt_tokens,completion_tokens,cost_source,operation) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            ident,
            now.date().isoformat(),
            now.isoformat(),
            worker,
            model,
            "reserved",
            reserve or 0,
            None,
            None,
            None,
            "reservation" if reserve is not None else "unknown",
            operation,
        ),
    )
    conn.commit()


def _settle(
    conn: Any,
    response: Any,
    model: str,
    settings: Settings,
    reserve: float | None,
    ident: str,
    operation: str,
) -> None:
    import litellm

    usage = _get(response, "usage")
    incoming = _get(usage, "prompt_tokens")
    outgoing = _get(usage, "completion_tokens", 0 if operation == "embedding" else None)
    cache_read = int(_get(usage, "cache_read_input_tokens", 0) or 0)
    cache_write = int(_get(usage, "cache_creation_input_tokens", 0) or 0)
    details = _get(usage, "prompt_tokens_details")
    cache_read = max(cache_read, int(_get(details, "cached_tokens", 0) or 0))
    cache_write = max(cache_write, int(_get(details, "cache_creation_tokens", 0) or 0))
    cache_ttls = _get(details, "cache_creation_token_details")
    cache_5m = int(_get(cache_ttls, "ephemeral_5m_input_tokens", 0) or 0)
    cache_1h = int(_get(cache_ttls, "ephemeral_1h_input_tokens", 0) or 0)
    weighted_cache_write = (
        cache_5m * 1.25 + cache_1h * 2 + max(0, cache_write - cache_5m - cache_1h) * 2
    )
    cost: float | None = None
    source = "reservation"
    try:
        if (
            model == "anthropic/claude-haiku-5-5"
            and settings.background_model_prices.get(model) == (0.50, 2.50)
            and incoming is not None
            and outgoing is not None
        ):
            rate_in, rate_out = (0.10, 0.50) if int(incoming) <= 100_000 else (0.50, 2.50)
            # Cache-write TTL is not normalized by all providers; use the
            # conservative one-hour rate when the TTL is unknown.
            cost = (
                max(0, int(incoming) - cache_read - cache_write) * rate_in
                + cache_read * rate_in * 0.1
                + weighted_cache_write * rate_in
                + int(outgoing) * rate_out
            ) / 1_000_000
            source = "haiku_5_5_estimate"
        elif (
            model == "anthropic/claude-sonnet-5-5"
            and settings.background_model_prices.get(model) == (2.0, 10.0)
            and incoming is not None
            and outgoing is not None
        ):
            # Sonnet 5.5 cache reads are $0.10/MTok (0.05x input), not 0.1x.
            cost = (
                max(0, int(incoming) - cache_read - cache_write) * 2.0
                + cache_read * 0.10
                + weighted_cache_write * 2.0
                + int(outgoing) * 10.0
            ) / 1_000_000
            source = "sonnet_5_5_estimate"
        elif (
            model in settings.background_model_prices
            and incoming is not None
            and outgoing is not None
        ):
            cost = _price(
                model,
                max(0, int(incoming) - cache_read - cache_write)
                + cache_read * 0.1
                + weighted_cache_write,
                int(outgoing),
                settings,
            )
            source = "configured_estimate"
        else:
            cost = float(
                litellm.completion_cost(
                    completion_response=response, model=model, call_type=operation
                )
            )
            source = "litellm_estimate"
        if cost is None or not math.isfinite(cost) or cost < 0:
            cost = None
    except Exception:
        cost = None
    conn.execute(
        "UPDATE provider_usage SET status='completed',cost_usd=?,prompt_tokens=?,"
        "completion_tokens=?,cost_source=?,cache_read_tokens=?,cache_write_tokens=? WHERE id=?",
        (
            cost,
            incoming,
            outgoing,
            source if cost is not None else ("reservation" if reserve is not None else "unknown"),
            cache_read,
            cache_write,
            ident,
        ),
    )
    conn.commit()


def usage_summary(settings: Settings, day: str | None = None) -> dict[str, Any]:
    """Operational daily totals; unknown cost is explicit, never presented as free."""
    day = day or datetime.now(UTC).date().isoformat()
    path = settings.vault_dir / "usage.db"
    if not path.exists():
        return {"day": day, "budget_usd": settings.background_daily_budget_usd, "workers": []}
    conn = _open_connection(path, vault_key=_key(settings), read_only=True)
    conn.execute("PRAGMA query_only = ON")
    try:
        available = {row[1] for row in conn.execute("PRAGMA table_info(provider_usage)")}
        operation = "operation" if "operation" in available else "'completion'"
        cache_read = "cache_read_tokens" if "cache_read_tokens" in available else "NULL"
        cache_write = "cache_write_tokens" if "cache_write_tokens" in available else "NULL"
        rows = conn.execute(
            f"SELECT worker,model,{operation} AS operation,COUNT(*) AS attempts,"
            "SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed_attempts,"
            "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed_attempts,"
            f"SUM(COALESCE({cache_read},0)) AS cache_read_tokens,"
            f"SUM(COALESCE({cache_write},0)) AS cache_write_tokens,"
            "SUM(COALESCE(cost_usd,0)) AS settled_estimated_usd,"
            "SUM(CASE WHEN cost_usd IS NULL THEN reserved_usd ELSE 0 END) AS retained_reservations_usd,"
            "SUM(COALESCE(prompt_tokens,0)) AS prompt_tokens,"
            "SUM(COALESCE(completion_tokens,0)) AS completion_tokens,"
            "SUM(COALESCE(cost_usd,reserved_usd)) AS accounted_usd,"
            "SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) AS unsettled_attempts,"
            "SUM(CASE WHEN cost_usd IS NULL AND cost_source='unknown' THEN 1 ELSE 0 END) AS unpriced_attempts "
            "FROM provider_usage WHERE day=? GROUP BY 1,2,3 ORDER BY 1,2,3",
            (day,),
        ).fetchall()
        return {
            "day": day,
            "budget_usd": settings.background_daily_budget_usd,
            "workers": [dict(row) for row in rows],
        }
    finally:
        conn.close()

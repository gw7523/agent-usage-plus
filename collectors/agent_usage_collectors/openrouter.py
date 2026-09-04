"""OpenRouter usage collector for the Agent Usage Plus panel.

Two independent sources, merged into one record:

1. Balance — the authenticated key's own budget from ``GET /api/v1/auth/key``
   (per-key spending limit), with a fallback to the account prepaid-credit
   ledger from ``GET /api/v1/credits`` for keys without a configured limit.
2. Local token history and cost estimate — a read-only walk of pi session
   transcripts (``~/.pi/agent/sessions``) that ran on the ``openrouter``
   provider. The ``cost`` block is priced from OpenRouter's own public model
   catalogue (fetched once a day, cached) rather than the bundled
   ``logic/cost.js`` catalogue: OpenRouter routes to hundreds of third-party
   models whose prices that static catalogue has no way to know. Messages
   whose ``api`` starts with ``openai-codex`` stay owned by the Codex
   collector so no tokens are ever counted twice. The catalogue is only
   fetched once a key is present — an unconfigured collector makes no
   network call at all.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .common import auth_missing, base_record, classify_failure, find_key, get_json, print_record, request_json

AUTH_ENDPOINT = "https://openrouter.ai/api/v1/auth/key"
CREDITS_ENDPOINT = "https://openrouter.ai/api/v1/credits"
MODELS_ENDPOINT = "https://openrouter.ai/api/v1/models"
AUTH_HELP = "Set OPENROUTER_API_KEY, or add openrouter.apiKey to ~/.config/omarchy/agent-usage-plus/collectors.json (chmod 600), then run agent-usage-plus-collectors update."

HISTORY_DAYS = 7
SESSION_ROOTS = ("~/.pi/agent/sessions", "~/.omp/agent/sessions")
EXCLUDED_API_PREFIX = "openai-codex"  # owned by the Codex collector

PRICING_CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "agent-usage-plus"
PRICING_TTL_SECONDS = 86400

Rate = tuple[float, float, float, float]  # (input, output, cacheRead, cacheWrite) USD per token


def local_day(value: Any) -> str:
    """Normalize a pi entry timestamp (ISO string or epoch millis) to a local date."""
    if value is None:
        return datetime.now().strftime("%Y-%m-%d")
    if isinstance(value, (int, float)):
        if value > 10_000_000_000:
            value = value / 1000
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d")
    text = str(value)
    try:
        if text.endswith("Z"):
            dt = datetime.fromisoformat(text[:-1] + "+00:00")
        else:
            dt = datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def number(value: Any) -> float | None:
    """Coerce to a non-negative float; anything malformed reads as absent."""
    try:
        result = float(value)
        return result if result >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def token_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def model_name(raw: Any) -> str:
    value = str(raw or "openrouter").strip()
    return value or "openrouter"


def empty_bucket() -> dict[str, int]:
    return {"inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}


def add_to_bucket(bucket: dict[str, int], input_tokens: int, output_tokens: int, cache_read: int, cache_write: int) -> None:
    bucket["inputTokens"] += input_tokens
    bucket["outputTokens"] += output_tokens
    bucket["cacheReadInputTokens"] += cache_read
    bucket["cacheCreationInputTokens"] += cache_write


def bucket_tokens(bucket: dict[str, int]) -> int:
    return (
        bucket["inputTokens"] + bucket["outputTokens"]
        + bucket["cacheReadInputTokens"] + bucket["cacheCreationInputTokens"]
    )


def bucket_usd(bucket: dict[str, int], rate: Rate) -> float:
    prompt_rate, completion_rate, cache_read_rate, cache_write_rate = rate
    return (
        bucket["inputTokens"] * prompt_rate
        + bucket["outputTokens"] * completion_rate
        + bucket["cacheReadInputTokens"] * cache_read_rate
        + bucket["cacheCreationInputTokens"] * cache_write_rate
    )


# ---- Local transcript scan -------------------------------------------------

def scan_pi_sessions() -> tuple[dict[str, Any], dict[str, dict[str, dict[str, int]]]]:
    """Aggregate openrouter-provider pi sessions into the standard local-stats
    fields, plus a trailing-week (date -> model -> TokenBucket) matrix used to
    price the cost block separately. No pricing happens here: this scan never
    touches the network, so it runs even without a configured key."""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    recent_dates = [(now - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(HISTORY_DAYS - 1, -1, -1)]
    recent = {day: {"date": day, "messageCount": 0} for day in recent_dates}
    daily_model_tokens: dict[str, dict[str, dict[str, int]]] = {}
    today_tokens_by_model: dict[str, dict[str, int]] = {}
    model_usage: dict[str, dict[str, int]] = {}
    today_sessions: set[str] = set()
    active_days: set[str] = set()
    seen: set[str] = set()

    today_prompts = 0
    today_total_tokens = 0
    total_prompts = 0
    total_sessions: set[str] = set()

    home = Path.home()
    for raw_root in SESSION_ROOTS:
        root = Path(str(raw_root).replace("~", str(home)))
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.jsonl")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                line = line.strip()
                if '"provider":"openrouter"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "message":
                    continue
                message = entry.get("message") or {}
                if message.get("role") != "assistant":
                    continue
                if str(message.get("provider") or "") != "openrouter":
                    continue
                if str(message.get("api") or "").startswith(EXCLUDED_API_PREFIX):
                    continue
                message_key = str(path) + ":" + str(entry.get("id") or "")
                if message_key in seen:
                    continue
                seen.add(message_key)

                usage = message.get("usage") or {}
                if not usage:
                    continue
                input_tokens = token_count(usage.get("input"))
                output_tokens = token_count(usage.get("output"))
                cache_read = token_count(usage.get("cacheRead"))
                cache_write = token_count(usage.get("cacheWrite"))
                total = token_count(usage.get("totalTokens"))
                if total and not (input_tokens or output_tokens or cache_read or cache_write):
                    input_tokens = total
                if not (input_tokens or output_tokens or cache_read or cache_write):
                    continue

                model = model_name(message.get("model"))
                total_tokens = input_tokens + output_tokens + cache_read + cache_write
                day = local_day(entry.get("timestamp") or message.get("timestamp"))

                bucket = model_usage.setdefault(model, empty_bucket())
                add_to_bucket(bucket, input_tokens, output_tokens, cache_read, cache_write)

                if day in recent:
                    recent[day]["messageCount"] += total_tokens
                    day_models = daily_model_tokens.setdefault(day, {})
                    day_bucket = day_models.setdefault(model, empty_bucket())
                    add_to_bucket(day_bucket, input_tokens, output_tokens, cache_read, cache_write)

                if day == today:
                    today_prompts += 1
                    today_sessions.add(message_key)
                    today_total_tokens += total_tokens
                    today_bucket = today_tokens_by_model.setdefault(model, empty_bucket())
                    add_to_bucket(today_bucket, input_tokens, output_tokens, cache_read, cache_write)

                total_prompts += 1
                total_sessions.add(message_key)
                active_days.add(day)

    stats = {
        "todayPrompts": today_prompts,
        "todaySessions": len(today_sessions),
        "todayTotalTokens": today_total_tokens,
        "todayTokensByModel": today_tokens_by_model,
        "recentDays": [recent[day] for day in recent_dates],
        "totalPrompts": total_prompts,
        "totalSessions": len(total_sessions),
        "activeDays": len(active_days),
        "activeDates": sorted(active_days),
        "modelUsage": model_usage,
    }
    return stats, daily_model_tokens


def load_model_rates() -> tuple[dict[str, Rate], str]:
    """Model id -> (input, output, cacheRead, cacheWrite) USD per token, from
    OpenRouter's public model catalogue, cached for a day. Models the
    catalogue prices without a separate cache field fall back to the input
    rate (or zero for free variants); anything malformed is skipped rather
    than poisoning the whole table."""
    cache = PRICING_CACHE_DIR / "openrouter-pricing.json"
    try:
        if cache.is_file() and time.time() - cache.stat().st_mtime < PRICING_TTL_SECONDS:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            rates_block = cached.get("rates") if isinstance(cached, dict) else None
            if isinstance(rates_block, dict) and rates_block:
                return (
                    {k: tuple(float(x) for x in v) for k, v in rates_block.items()},
                    str(cached.get("version") or "unknown"),
                )
    except Exception:
        pass

    rates: dict[str, Rate] = {}
    version = "openrouter-models-unknown"
    try:
        payload = request_json(MODELS_ENDPOINT)
        for model in payload.get("data") or []:
            pricing = model.get("pricing") or {}
            if not isinstance(pricing, dict):
                continue
            model_id = str(model.get("id") or "")
            if not model_id:
                continue
            try:
                prompt_rate = float(pricing.get("prompt") or 0)
                completion_rate = float(pricing.get("completion") or 0)
            except (TypeError, ValueError):
                continue
            raw_read = pricing.get("input_cache_read")
            raw_write = pricing.get("input_cache_write")
            try:
                cache_read_rate = float(raw_read) if raw_read not in (None, "") else prompt_rate
            except (TypeError, ValueError):
                cache_read_rate = prompt_rate
            try:
                cache_write_rate = float(raw_write) if raw_write not in (None, "") else prompt_rate
            except (TypeError, ValueError):
                cache_write_rate = prompt_rate
            rates[model_id] = (prompt_rate, completion_rate, cache_read_rate, cache_write_rate)
        version = "openrouter-models-" + datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"version": version, "rates": rates}), encoding="utf-8")
    except Exception:
        pass
    return rates, version


def rate_for(rates: dict[str, Rate], model: str) -> Rate | None:
    return rates.get(model) or rates.get(model.split(":")[0])


def build_cost(
    daily_model_tokens: dict[str, dict[str, dict[str, int]]],
    rates: dict[str, Rate],
    pricing_version: str,
) -> dict[str, Any] | None:
    """Contract-shaped cost estimate over the same trailing window as
    ``recentDays``: byModel/byDay priced at published rates. A model missing
    from the catalogue is never invented as $0 — it is named in
    ``unknownModels`` and its tokens excluded from the subtotal."""
    week_usage: dict[str, dict[str, int]] = {}
    unknown: set[str] = set()
    by_day: list[dict[str, Any]] = []
    active_days = 0

    for date in sorted(daily_model_tokens):
        day_models = daily_model_tokens[date]
        day_usd = 0.0
        day_has_tokens = False
        for model, bucket in day_models.items():
            tokens = bucket_tokens(bucket)
            if tokens == 0:
                continue
            day_has_tokens = True
            week_bucket = week_usage.setdefault(model, empty_bucket())
            add_to_bucket(
                week_bucket, bucket["inputTokens"], bucket["outputTokens"],
                bucket["cacheReadInputTokens"], bucket["cacheCreationInputTokens"],
            )
            rate = rate_for(rates, model)
            if rate is None:
                unknown.add(model)
                continue
            day_usd += bucket_usd(bucket, rate)
        if day_has_tokens:
            active_days += 1
        if day_usd > 0:
            by_day.append({"date": date, "usd": round(day_usd, 2)})

    by_model: list[dict[str, Any]] = []
    total_usd = 0.0
    priced_tokens = 0
    unpriced_tokens = 0
    for model, bucket in week_usage.items():
        tokens = bucket_tokens(bucket)
        rate = rate_for(rates, model)
        if rate is None:
            unpriced_tokens += tokens
            continue
        usd = bucket_usd(bucket, rate)
        total_usd += usd
        priced_tokens += tokens
        by_model.append({"model": model, "usd": round(usd, 2), "tokens": tokens})

    if priced_tokens == 0 and unknown:
        return None  # every used model is unpriced: no fabricated $0

    by_model.sort(key=lambda entry: -entry["usd"])
    return {
        "estimateUsd": round(total_usd, 2),
        "period": f"{HISTORY_DAYS}d",
        "pricingVersion": pricing_version,
        "byModel": by_model,
        "byDay": by_day,
        "incomplete": bool(unknown),
        "unknownModels": sorted(unknown)[:20],
        "pricedTokens": priced_tokens,
        "unpricedTokens": unpriced_tokens,
        "activeDays": active_days,
    }


# ---- Balance ---------------------------------------------------------------

def key_budget_balance(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Per-key spending-limit balance from the auth/key probe, or None when
    the key has no configured limit (a normal account configuration)."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    limit = number(data.get("limit"))
    remaining = number(data.get("limit_remaining"))
    usage = number(data.get("usage"))
    if limit is None or remaining is None:
        return None
    balance: dict[str, Any] = {"remaining": remaining, "funded": limit, "currency": "USD"}
    if usage is not None:
        balance["spent"] = usage
    return balance


def record_from_payload(payload: dict[str, Any], record: dict[str, Any] | None = None) -> dict[str, Any]:
    """Map a ``GET /api/v1/auth/key`` response onto the panel record. Pure and
    payload-only, so tests can exercise it without a network call; ``collect``
    passes in the record it already has (local stats included) rather than
    starting a fresh one."""
    if record is None:
        record = base_record("openrouter", "OpenRouter", "OpenRouter API key")
    balance = key_budget_balance(payload)
    if balance is not None:
        record["balance"] = balance
        reset = (payload.get("data") or {}).get("limit_reset")
        if isinstance(reset, str) and reset in {"daily", "weekly", "monthly"}:
            record["tierLabel"] = f"OpenRouter API key · {reset} budget"
    else:
        # A current-key probe can succeed even if that key has no spending
        # limit. This is a normal account configuration, not a panel error.
        record["tierLabel"] = "OpenRouter API key · no key budget"
    record["ready"] = True
    return record


def credits_balance(key: str) -> dict[str, Any]:
    """Account prepaid-credit ledger from GET /api/v1/credits."""
    payload = get_json(CREDITS_ENDPOINT, key)
    data = payload.get("data") or {}
    funded = number(data.get("total_credits"))
    spent = number(data.get("total_usage"))
    if funded is None:
        raise ValueError("credits endpoint returned no total_credits")
    return {
        "remaining": round(max(0.0, funded - spent), 6),
        "funded": funded,
        "spent": spent,
        "currency": "USD",
    }


# ---- Record ----------------------------------------------------------------

def collect() -> dict[str, Any]:
    record = base_record("openrouter", "OpenRouter", "OpenRouter API key")

    stats, daily_model_tokens = scan_pi_sessions()
    record.update(stats)
    record["hasLocalStats"] = True
    record["hasPromptStats"] = True

    key = find_key("OPENROUTER_API_KEY", "openrouter")
    if not key:
        # No credentials yet: local transcript stats still show, but the
        # catalogue that prices them is never fetched, and the record stays
        # genuinely not-ready until a key is configured.
        return auth_missing(record, AUTH_HELP)

    rates, pricing_version = load_model_rates()
    cost = build_cost(daily_model_tokens, rates, pricing_version)
    if cost is not None:
        record["cost"] = cost

    try:
        payload = get_json(AUTH_ENDPOINT, key)
    except Exception as exc:  # converted to a display record, never leaked
        return classify_failure(record, "OpenRouter", exc, AUTH_HELP)

    record_from_payload(payload, record)
    if "balance" not in record:
        # The key has no configured spending limit; fall back to the
        # account's prepaid-credit ledger so prepaid accounts still get a
        # balance card. If that is unavailable too, "no key budget" (already
        # set by record_from_payload) is still a normal account
        # configuration, not a panel error.
        try:
            record["balance"] = credits_balance(key)
            record["tierLabel"] = "Prepaid"
        except Exception:
            pass

    return record


if __name__ == "__main__":
    print_record(collect())

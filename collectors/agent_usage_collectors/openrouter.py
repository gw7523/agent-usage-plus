"""OpenRouter usage collector for the Agent Usage Plus panel.

Two independent sources, merged into one record:

1. Balance — the authenticated key's own budget from ``GET /api/v1/auth/key``
   (per-key spending limit), with a fallback to the account prepaid-credit
   ledger from ``GET /api/v1/credits`` for keys without a configured limit.
2. Local token history and cost estimates — a read-only walk of pi session
   transcripts (``~/.pi/agent/sessions``) that ran on the ``openrouter``
   provider, priced against OpenRouter's public model catalogue. Messages
   whose ``api`` starts with ``openai-codex`` stay owned by the Codex
   collector so no tokens are ever counted twice.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .common import auth_missing, base_record, classify_failure, find_key, get_json, print_record

AUTH_ENDPOINT = "https://openrouter.ai/api/v1/auth/key"
CREDITS_ENDPOINT = "https://openrouter.ai/api/v1/credits"
MODELS_ENDPOINT = "https://openrouter.ai/api/v1/models"
AUTH_HELP = "Set OPENROUTER_API_KEY, or add openrouter.apiKey to ~/.config/omarchy/agent-usage-plus/collectors.json (chmod 600), then run agent-usage-plus-collectors update."

HISTORY_DAYS = 7
SESSION_ROOTS = ("~/.pi/agent/sessions", "~/.omp/agent/sessions")
EXCLUDED_API_PREFIX = "openai-codex"  # owned by the Codex collector

PRICING_CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "agent-usage-plus"
PRICING_TTL_SECONDS = 86400


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


# ---- Local transcript scan -------------------------------------------------

def scan_pi_sessions(rates: dict[str, tuple[float, float, float, float]]) -> dict[str, Any]:
    """Aggregate openrouter-provider pi sessions into token and cost stats."""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    recent_dates = [(now - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(HISTORY_DAYS - 1, -1, -1)]
    recent = {day: {"date": day, "messageCount": 0, "cost": 0.0} for day in recent_dates}
    today_tokens_by_model: dict[str, dict[str, int]] = {}
    today_costs_by_model: dict[str, float] = {}
    unknown_models: set[str] = set()
    priced_tokens = 0
    unpriced_tokens = 0
    model_usage: dict[str, dict[str, int]] = {}
    week_costs_by_model: dict[str, float] = {}
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
                # The catalogue is the price authority: an exact id match is
                # priced (even at a published $0); a model the catalogue
                # doesn't list is unpriced, never folded into a $0 subtotal.
                # The ":"-variant base id is tried so e.g. "model:free"
                # entries price against their base listing.
                catalogue_entry = rates.get(model) or rates.get(model.split(":")[0])
                priced = catalogue_entry is not None
                prompt_rate, completion_rate, cache_read_rate, cache_write_rate = catalogue_entry or (0.0, 0.0, 0.0, 0.0)
                cost = (
                    input_tokens * prompt_rate
                    + output_tokens * completion_rate
                    + cache_read * cache_read_rate
                    + cache_write * cache_write_rate
                )
                total_tokens = input_tokens + output_tokens + cache_read + cache_write
                if not priced:
                    unknown_models.add(model)
                    unpriced_tokens += total_tokens
                else:
                    priced_tokens += total_tokens
                day = local_day(entry.get("timestamp") or message.get("timestamp"))
                bucket = model_usage.setdefault(model, {
                    "inputTokens": 0,
                    "outputTokens": 0,
                    "cacheReadInputTokens": 0,
                    "cacheCreationInputTokens": 0,
                })
                bucket["inputTokens"] += input_tokens
                bucket["outputTokens"] += output_tokens
                bucket["cacheReadInputTokens"] += cache_read
                bucket["cacheCreationInputTokens"] += cache_write

                if day in recent:
                    recent[day]["messageCount"] += total_tokens
                    recent[day]["cost"] = round(recent[day].get("cost", 0.0) + cost, 6)
                    week_costs_by_model[model] = round(week_costs_by_model.get(model, 0.0) + cost, 6)

                if day == today:
                    today_prompts += 1
                    today_sessions.add(message_key)
                    today_total_tokens += total_tokens
                    today_bucket = today_tokens_by_model.setdefault(model, {
                        "inputTokens": 0, "outputTokens": 0,
                        "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0,
                    })
                    today_bucket["inputTokens"] += input_tokens
                    today_bucket["outputTokens"] += output_tokens
                    today_bucket["cacheReadInputTokens"] += cache_read
                    today_bucket["cacheCreationInputTokens"] += cache_write

                total_prompts += 1
                total_sessions.add(message_key)
                active_days.add(day)
                week_costs_by_model[model] = round(week_costs_by_model.get(model, 0.0) + cost, 6)

    return {
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
        "weekCostsByModel": week_costs_by_model,
        "pricedTokens": priced_tokens,
        "unpricedTokens": unpriced_tokens,
        "unknownModels": sorted(unknown_models)[:20],
    }


def load_model_rates() -> tuple[dict[str, tuple[float, float, float, float]], str]:
    """Model id -> (input, output, cacheRead, cacheWrite) USD per million
    tokens, from OpenRouter's public model catalogue, cached for a day.
    Models the catalogue prices without a separate cache field fall back to
    the input rate (or zero for free variants); anything malformed is
    skipped rather than poisoning the whole table."""
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

    rates: dict[str, tuple[float, float, float, float]] = {}
    version = "openrouter-models-unknown"
    try:
        request = urllib.request.Request(MODELS_ENDPOINT, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
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


def credits_balance(key: str) -> dict[str, Any]:
    """Account prepaid-credit ledger from GET /api/v1/credits."""
    request = urllib.request.Request(
        CREDITS_ENDPOINT,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
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


def fetch_balance(key: str) -> dict[str, Any] | None:
    """Prefer the key's own budget; fall back to the account credit ledger
    for prepaid accounts without a key limit. None means no balance is
    knowable for this key."""
    try:
        request = urllib.request.Request(
            AUTH_ENDPOINT,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        balance = key_budget_balance(payload)
        if balance is not None:
            balance["estimated"] = False
            return balance
    except Exception:
        pass

    try:
        return credits_balance(key)
    except Exception:
        return None


# ---- Record ----------------------------------------------------------------

def collect() -> dict[str, Any]:
    record = base_record("openrouter", "OpenRouter", "OpenRouter API key")

    rates, pricing_version = load_model_rates()
    stats = scan_pi_sessions(rates)
    record.update(stats)
    record["hasLocalStats"] = True
    record["hasPromptStats"] = True
    record["ready"] = True

    # Contract-shaped cost estimate: pi-session tokens priced at published
    # rates. byModel covers the trailing week (the same window as
    # recentDays); byDay lines up one-to-one with it. A model missing from
    # the catalogue is unpriced, never invented as $0.
    week_costs = stats.get("weekCostsByModel") or {}
    model_usage = stats.get("modelUsage") or {}
    if week_costs and stats.get("pricedTokens", 0) > 0:
        # Priced tokens exist: publish the estimate. Models the catalogue
        # doesn't list are excluded (named in unknownModels), and the block
        # is marked partial rather than folding them into a $0 subtotal.
        by_model = []
        for model, cost in sorted(week_costs.items(), key=lambda kv: -kv[1]):
            bucket = model_usage.get(model) or {}
            tokens = sum(token_count(bucket.get(key)) for key in (
                "inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"
            ))
            by_model.append({"model": model, "usd": round(cost, 2), "tokens": tokens})
        by_day = [
            {"date": day.get("date") if isinstance(day, dict) else str(day),
             "usd": round(float(day.get("cost") or 0.0), 2)}
            for day in stats.get("recentDays", [])
        ]
        record["cost"] = {
            "estimateUsd": round(sum(entry["usd"] for entry in by_model), 2),
            "period": "7d",
            "byModel": by_model,
            "byDay": by_day,
            "pricedTokens": stats.get("pricedTokens"),
            "unpricedTokens": stats.get("unpricedTokens"),
            "pricingVersion": pricing_version,
            "incomplete": bool(stats.get("unknownModels")),
            "unknownModels": sorted(stats.get("unknownModels", []))[:20],
        }

    key = find_key("OPENROUTER_API_KEY", "openrouter")
    if not key:
        record["tierLabel"] = "OpenRouter API key · no key budget"
        return record

    balance = fetch_balance(key)
    if balance is not None:
        record["balance"] = balance
        record["tierLabel"] = "Prepaid"
    else:
        record["usageStatusText"] = "OpenRouter balance unavailable"
        record["authHelpText"] = AUTH_HELP

    return record


if __name__ == "__main__":
    print_record(collect())

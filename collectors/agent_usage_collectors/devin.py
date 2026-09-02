"""Devin CLI local usage and subscription quota collector.

The credential location and login flow are documented by Devin at
https://docs.devin.ai/cli/enterprise/devin-auth. The quota request mirrors
the read-only ``GetUserStatus`` RPC used by the official Devin CLI; that RPC
is not a public API contract, so unexpected response shapes fail closed.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError
from urllib.parse import urlparse

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by the fallback parser test
    tomllib = None  # type: ignore[assignment]

from .common import (
    auth_missing,
    classify_failure,
    endpoint_problem,
    now_iso,
    print_record,
    request_json,
)


AGENT_ID = "devin"
AGENT_NAME = "Devin"
DEFAULT_API_SERVER = "https://server.codeium.com"
QUOTA_PATH = "/exa.seat_management_pb.SeatManagementService/GetUserStatus"
CLI_COMPAT_VERSION = "1.108.2"
AUTH_HELP = "Run `devin auth login`, then refresh Agent Usage Plus."
MAX_CREDENTIAL_BYTES = 65_536
MAX_MODEL_IDS = 100

_EMPTY_TOKEN_BUCKET = {
    "inputTokens": 0,
    "outputTokens": 0,
    "cacheReadInputTokens": 0,
    "cacheCreationInputTokens": 0,
}
_FALLBACK_TOML_ENTRY = re.compile(
    r"^\s*(windsurf_api_key|api_server_url)\s*=\s*(\"(?:[^\"\\]|\\.)*\"|'[^']*')\s*(?:#.*)?$"
)


class CredentialFileError(ValueError):
    """A safe-to-display credentials-file error with no credential content."""


def devin_paths() -> tuple[Path, Path]:
    data_home = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    devin_home = Path(os.environ.get("DEVIN_HOME") or (data_home / "devin" / "cli"))
    credentials = Path(
        os.environ.get("DEVIN_CREDENTIALS_FILE") or (data_home / "devin" / "credentials.toml")
    )
    return credentials, devin_home / "sessions.db"


def empty_stats(today: date | None = None) -> dict[str, Any]:
    current = today or date.today()
    days = [(current - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)]
    return {
        "todayPrompts": 0,
        "todaySessions": 0,
        "todayTotalTokens": 0,
        "todayTokensByModel": {},
        "recentDays": [{"date": day, "messageCount": 0} for day in days],
        "modelUsage": {},
        "totalPrompts": 0,
        "totalSessions": 0,
        "activeDays": 0,
        "activeDates": [],
    }


def token_count(value: Any) -> int:
    try:
        parsed = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not math.isfinite(parsed):
        return 0
    return max(0, round(parsed))


def local_day(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            timestamp = float(value)
            while timestamp > 10_000_000_000:
                timestamp /= 1000
            return datetime.fromtimestamp(timestamp).astimezone().date().isoformat()
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.date().isoformat()
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def add_usage(
    stats: dict[str, Any],
    *,
    session_id: str,
    day: str | None,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_write: int,
    sessions: set[str],
    today_sessions: set[str],
    active_dates: set[str],
) -> None:
    total = input_tokens + output_tokens + cache_read + cache_write
    if total <= 0 or day is None:
        return

    session_id = session_id[:512]
    model = model.strip()[:160] or "devin"
    if model not in stats["modelUsage"] and len(stats["modelUsage"]) >= MAX_MODEL_IDS - 1:
        model = "other"
    sessions.add(session_id)
    active_dates.add(day)
    stats["totalPrompts"] += 1
    bucket = stats["modelUsage"].setdefault(model, dict(_EMPTY_TOKEN_BUCKET))
    bucket["inputTokens"] += input_tokens
    bucket["outputTokens"] += output_tokens
    bucket["cacheReadInputTokens"] += cache_read
    bucket["cacheCreationInputTokens"] += cache_write

    for recent in stats["recentDays"]:
        if recent["date"] == day:
            recent["messageCount"] += total
            break

    if day != date.today().isoformat():
        return
    stats["todayPrompts"] += 1
    stats["todayTotalTokens"] += total
    today_sessions.add(session_id)
    today_bucket = stats["todayTokensByModel"].setdefault(model, dict(_EMPTY_TOKEN_BUCKET))
    today_bucket["inputTokens"] += input_tokens
    today_bucket["outputTokens"] += output_tokens
    today_bucket["cacheReadInputTokens"] += cache_read
    today_bucket["cacheCreationInputTokens"] += cache_write


def finalize_stats(
    stats: dict[str, Any],
    sessions: set[str],
    today_sessions: set[str],
    active_dates: set[str],
) -> dict[str, Any]:
    stats["todaySessions"] = len(today_sessions)
    stats["totalSessions"] = len(sessions)
    stats["activeDays"] = len(active_dates)
    stats["activeDates"] = sorted(active_dates)[-400:]
    return stats


def stats_from_rows(rows: Iterable[tuple[Any, ...]]) -> dict[str, Any]:
    stats = empty_stats()
    sessions: set[str] = set()
    today_sessions: set[str] = set()
    active_dates: set[str] = set()

    for row in rows:
        (
            session_id,
            session_model,
            generation_model,
            created_at,
            input_tokens,
            prompt_tokens,
            output_tokens,
            completion_tokens,
            cache_read_tokens,
            cached_tokens,
            cache_creation_tokens,
        ) = row
        add_usage(
            stats,
            session_id=str(session_id),
            day=local_day(created_at),
            model=str(generation_model or session_model or "devin"),
            input_tokens=token_count(input_tokens) or token_count(prompt_tokens),
            output_tokens=token_count(output_tokens) or token_count(completion_tokens),
            cache_read=token_count(cache_read_tokens) or token_count(cached_tokens),
            cache_write=token_count(cache_creation_tokens),
            sessions=sessions,
            today_sessions=today_sessions,
            active_dates=active_dates,
        )
    return finalize_stats(stats, sessions, today_sessions, active_dates)


def collect_database(db_path: Path) -> tuple[dict[str, Any], bool, str]:
    if not db_path.is_file():
        return empty_stats(), False, ""
    try:
        connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
        try:
            connection.execute("PRAGMA query_only = ON")
            rows = connection.execute(
                """
                SELECT
                    m.session_id,
                    s.model,
                    json_extract(m.chat_message, '$.metadata.generation_model'),
                    COALESCE(
                        json_extract(m.chat_message, '$.metadata.created_at'),
                        m.created_at
                    ),
                    json_extract(m.chat_message, '$.metadata.metrics.input_tokens'),
                    json_extract(m.chat_message, '$.metadata.metrics.prompt_tokens'),
                    json_extract(m.chat_message, '$.metadata.metrics.output_tokens'),
                    json_extract(m.chat_message, '$.metadata.metrics.completion_tokens'),
                    json_extract(m.chat_message, '$.metadata.metrics.cache_read_tokens'),
                    json_extract(m.chat_message, '$.metadata.metrics.cached_tokens'),
                    json_extract(m.chat_message, '$.metadata.metrics.cache_creation_tokens')
                FROM message_nodes AS m
                LEFT JOIN sessions AS s ON s.id = m.session_id
                WHERE json_valid(m.chat_message)
                  AND json_extract(m.chat_message, '$.role') = 'assistant'
                """
            )
            return stats_from_rows(rows), True, ""
        finally:
            connection.close()
    except sqlite3.Error:
        return empty_stats(), False, "Devin local database unavailable"


def collect_local_stats() -> tuple[dict[str, Any], bool, str]:
    _, db_path = devin_paths()
    return collect_database(db_path)


def optional_number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def reset_iso(value: Any) -> str:
    seconds = optional_number(value)
    if seconds is None:
        return ""
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        )
    except (OSError, OverflowError, ValueError):
        return ""


def parse_plan_status(payload: dict[str, Any]) -> dict[str, Any]:
    user_status = payload.get("userStatus")
    if not isinstance(user_status, dict):
        raise ValueError("Devin quota response has no user status")
    plan_status = user_status.get("planStatus")
    if not isinstance(plan_status, dict):
        raise ValueError("Devin quota response has no plan status")
    plan_info = plan_status.get("planInfo") if isinstance(plan_status.get("planInfo"), dict) else {}

    tier = str(plan_info.get("planName") or "").strip()[:60]
    hide_daily = plan_info.get("hideDailyQuota") is True
    daily_remaining = optional_number(plan_status.get("dailyQuotaRemainingPercent"))
    weekly_remaining = optional_number(plan_status.get("weeklyQuotaRemainingPercent"))
    limits: list[dict[str, Any]] = []

    if not hide_daily and daily_remaining is not None:
        limits.append(
            {
                "label": "Daily",
                "title": "Daily",
                "percent": 1.0 - min(100.0, max(0.0, daily_remaining)) / 100.0,
                "resetsAt": reset_iso(plan_status.get("dailyQuotaResetAtUnix")),
            }
        )
    if weekly_remaining is not None:
        limits.append(
            {
                "label": "Weekly (7-day)",
                "title": "Weekly",
                "percent": 1.0 - min(100.0, max(0.0, weekly_remaining)) / 100.0,
                "resetsAt": reset_iso(plan_status.get("weeklyQuotaResetAtUnix")),
            }
        )
    elif hide_daily and daily_remaining is not None:
        limits.append(
            {
                "label": "Weekly (7-day)",
                "title": "Weekly",
                "percent": 1.0 - min(100.0, max(0.0, daily_remaining)) / 100.0,
                "resetsAt": reset_iso(plan_status.get("weeklyQuotaResetAtUnix")),
            }
        )

    balance = None
    overage_micros = optional_number(plan_status.get("overageBalanceMicros"))
    if overage_micros is not None:
        balance = {
            "remaining": max(0.0, overage_micros) / 1_000_000.0,
            "currency": "USD",
            "estimated": False,
        }
    if not limits and balance is None:
        raise ValueError("Devin returned no quota data")
    return {"limits": limits, "balance": balance, "tier": tier}


def _fallback_toml(text: str) -> dict[str, Any]:
    data: dict[str, str] = {}
    for line in text.splitlines():
        match = _FALLBACK_TOML_ENTRY.match(line)
        if not match:
            continue
        key, raw = match.groups()
        if raw.startswith('"'):
            try:
                data[key] = json.loads(raw)
            except (TypeError, ValueError):
                break
        else:
            data[key] = raw[1:-1]
    return data


def validate_api_server(value: Any) -> str:
    raw = str(value or DEFAULT_API_SERVER).strip().rstrip("/")
    if len(raw) > 2_048:
        raise CredentialFileError("Devin credentials contain an invalid API server URL.")
    parsed = urlparse(raw)
    try:
        valid_port = parsed.port is None or 0 < parsed.port <= 65_535
    except ValueError:
        valid_port = False
    if not valid_port or (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise CredentialFileError("Devin credentials contain an invalid API server URL.")
    return raw


def read_credentials() -> tuple[str, str] | None:
    credentials_path, _ = devin_paths()
    try:
        if credentials_path.stat().st_size > MAX_CREDENTIAL_BYTES:
            raise CredentialFileError("Devin credentials file is unexpectedly large.")
        raw = credentials_path.read_bytes()
        if len(raw) > MAX_CREDENTIAL_BYTES:
            raise CredentialFileError("Devin credentials file is unexpectedly large.")
        text = raw.decode("utf-8")
        data = tomllib.loads(text) if tomllib is not None else _fallback_toml(text)
    except FileNotFoundError:
        return None
    except CredentialFileError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise CredentialFileError("Devin credentials file could not be parsed.") from exc
    if not isinstance(data, dict):
        raise CredentialFileError("Devin credentials file could not be parsed.")
    token = data.get("windsurf_api_key")
    if not isinstance(token, str) or not token.strip():
        return None
    return token.strip(), validate_api_server(data.get("api_server_url"))


def collect_quota(token: str, api_server: str) -> dict[str, Any]:
    payload = request_json(
        f"{api_server}{QUOTA_PATH}",
        headers={"Connect-Protocol-Version": "1"},
        body={
            "metadata": {
                "apiKey": token,
                "ideName": "devin",
                "ideVersion": CLI_COMPAT_VERSION,
                "extensionName": "devin",
                "extensionVersion": CLI_COMPAT_VERSION,
                "locale": "en",
            }
        },
        timeout_seconds=15,
    )
    return parse_plan_status(payload)


def base_record() -> dict[str, Any]:
    return {
        "id": AGENT_ID,
        "name": AGENT_NAME,
        "schemaVersion": 1,
        "updatedAt": now_iso(),
        "hasLocalStats": False,
        "hasPromptStats": False,
        "tierLabel": "CLI",
        "limits": [],
    }


def collect() -> dict[str, Any]:
    record = base_record()
    stats, local_ready, local_status = collect_local_stats()
    record.update(stats)
    record["hasLocalStats"] = local_ready
    record["hasPromptStats"] = local_ready
    record["scope"] = "device" if local_ready else "account"
    record["ready"] = local_ready and stats["totalPrompts"] > 0
    if local_status:
        record["usageStatusText"] = local_status
        record["authHelpText"] = (
            "Close Devin and try the next refresh. Subscription limits can still show."
        )

    try:
        credentials = read_credentials()
    except CredentialFileError as exc:
        return endpoint_problem(record, "Devin credentials unavailable", str(exc))
    if credentials is None:
        return auth_missing(record, AUTH_HELP, status="Waiting for Devin sign-in")

    token, api_server = credentials
    try:
        quota = collect_quota(token, api_server)
    except HTTPError as exc:
        if exc.code in (401, 403):
            return endpoint_problem(
                record,
                "Devin sign-in expired",
                "Run `devin auth login` to restore subscription limits.",
            )
        return classify_failure(record, "Devin", exc, AUTH_HELP)
    except Exception as exc:  # noqa: BLE001 - mapped to documented visible states
        return classify_failure(record, "Devin", exc, AUTH_HELP)

    record["limits"] = quota["limits"]
    if quota["balance"] is not None:
        record["balance"] = quota["balance"]
    if quota["tier"]:
        record["tierLabel"] = quota["tier"]
    record["ready"] = True
    return record


if __name__ == "__main__":
    print_record(collect())

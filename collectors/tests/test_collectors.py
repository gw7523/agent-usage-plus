from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from agent_usage_collectors import cursor, deepseek, gemini, kimi, opencode_go, openrouter, xai
from agent_usage_collectors.common import MAX_RESPONSE_BYTES, base_record, classify_failure, endpoint_problem, request_json
from agent_usage_collectors.deepseek import record_from_payload as deepseek_record
from agent_usage_collectors.cursor import record_from_payload as cursor_record
from agent_usage_collectors.gemini import record_from_payload as gemini_record
from agent_usage_collectors.kimi import record_from_payload as kimi_record
from agent_usage_collectors.opencode_go import collect as collect_opencode_go
from agent_usage_collectors.opencode_go import limit_window as opencode_go_limit_window
from agent_usage_collectors.opencode_go import stats_from_rows as opencode_go_stats_from_rows
from agent_usage_collectors.openrouter import record_from_payload as openrouter_record
from agent_usage_collectors.transcript_cost import decorate, normalise_today_buckets
from agent_usage_collectors.xai import record_from_payload as xai_record
from agent_usage_collectors.xai import team_id_from_validation
from agent_usage_collectors.zai import collect as collect_zai
from agent_usage_collectors.zai import record_from_payload as zai_record


class CollectorParsingTests(unittest.TestCase):
    def test_request_json_bounds_provider_response_reads(self) -> None:
        class Response:
            def __init__(self, body: bytes) -> None:
                self.body = body
                self.read_limit: int | None = None

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self, limit: int = -1) -> bytes:
                self.read_limit = limit
                return self.body

        response = Response(b"{}")
        with patch("agent_usage_collectors.common.urlopen", return_value=response):
            self.assertEqual(request_json("https://provider.example/usage"), {})
        self.assertEqual(response.read_limit, MAX_RESPONSE_BYTES + 1)

        oversized = Response(b"{" + b"x" * MAX_RESPONSE_BYTES + b"}")
        with patch("agent_usage_collectors.common.urlopen", return_value=oversized):
            with self.assertRaisesRegex(ValueError, "too large"):
                request_json("https://provider.example/usage")

    def test_every_companion_collector_reports_missing_auth_without_network(self) -> None:
        with patch.object(openrouter, "find_key", return_value=None), patch.object(deepseek, "find_key", return_value=None), patch.object(kimi, "find_key", return_value=None), patch.object(xai, "find_key", return_value=None), patch.object(gemini, "read_access_token", return_value=None), patch.object(cursor, "read_token", return_value=None), patch("agent_usage_collectors.zai.find_any_key", return_value=None):
            records = [
                openrouter.collect(),
                deepseek.collect(),
                kimi.collect(),
                xai.collect(),
                gemini.collect(),
                cursor.collect(),
                collect_zai(),
            ]
        self.assertTrue(all(record.get("ready") is False for record in records))
        self.assertEqual(records[0]["usageStatusText"], "Waiting for API key")
        self.assertEqual(records[3]["usageStatusText"], "Waiting for API key")
        self.assertEqual(records[4]["usageStatusText"], "Waiting for Gemini sign-in")
        self.assertEqual(records[5]["usageStatusText"], "Waiting for Cursor sign-in")
        self.assertEqual(records[6]["usageStatusText"], "Waiting for Z.AI API key")

    def test_openrouter_budget_maps_to_balance(self) -> None:
        record = openrouter_record({"data": {"limit": 25, "limit_remaining": 17.5, "usage": 7.5, "limit_reset": "monthly"}})
        self.assertTrue(record["ready"])
        self.assertEqual(record["balance"], {"remaining": 17.5, "funded": 25.0, "spent": 7.5, "currency": "USD"})
        self.assertIn("monthly", record["tierLabel"])

    def test_openrouter_without_key_limit_is_not_an_error(self) -> None:
        record = openrouter_record({"data": {"usage": 4.25, "limit": None}})
        self.assertTrue(record["ready"])
        self.assertNotIn("balance", record)
        self.assertNotIn("usageStatusText", record)

    def test_deepseek_prefers_usd_ledger(self) -> None:
        record = deepseek_record({"is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": "100"}, {"currency": "USD", "total_balance": "3.20"}]})
        self.assertEqual(record["balance"], {"remaining": 3.2, "currency": "USD"})
        self.assertTrue(record["ready"])

    def test_xai_prepaid_credit_is_converted_from_signed_cents(self) -> None:
        record = xai_record({"total": {"val": "-1234"}})
        self.assertEqual(record["balance"], {"remaining": 12.34, "currency": "USD"})
        self.assertTrue(record["ready"])

    def test_xai_finds_team_from_legacy_or_team_scope_validation(self) -> None:
        self.assertEqual(team_id_from_validation({"teamId": "legacy-team"}, None), "legacy-team")
        self.assertEqual(team_id_from_validation({"scope": "SCOPE_TEAM", "scopeId": "scoped-team"}, None), "scoped-team")
        self.assertIsNone(team_id_from_validation({"scope": "SCOPE_ORGANIZATION", "scopeId": "org"}, None))

    @patch("agent_usage_collectors.zai.find_any_key", return_value=None)
    def test_zai_missing_key_is_a_clear_state(self, _key: object) -> None:
        record = collect_zai()
        self.assertEqual(record["usageStatusText"], "Waiting for Z.AI API key")
        self.assertNotIn("balance", record)

    def test_zai_maps_coding_plan_windows(self) -> None:
        record = zai_record({
            "success": True,
            "code": 200,
            "data": {
                "planName": "GLM Pro",
                "limits": [
                    {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 20, "remaining": 800, "usage": 1000, "nextResetTime": 1787529600000},
                    {"type": "TOKENS_LIMIT", "unit": 6, "number": 1, "percentage": 40},
                    {"type": "TIME_LIMIT", "unit": 5, "number": 1, "percentage": 5},
                ],
            },
        })
        self.assertTrue(record["ready"])
        self.assertEqual(record["tierLabel"], "GLM Pro")
        self.assertEqual([limit["title"] for limit in record["limits"]], ["5-hour", "MCP", "Weekly"])
        self.assertEqual(record["limits"][0]["percent"], 0.2)
        self.assertEqual(record["limits"][0]["resetsAt"], "2026-08-24T00:00:00Z")

    @patch("agent_usage_collectors.zai.setting", return_value=None)
    @patch("agent_usage_collectors.zai.request_json")
    @patch("agent_usage_collectors.zai.find_any_key", return_value="zai-test-key")
    def test_zai_collects_personal_quota(self, _key: object, get_json: object, _setting: object) -> None:
        get_json.return_value = {"success": True, "code": 200, "data": {"limits": [{"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 12}]}}
        record = collect_zai()
        self.assertTrue(record["ready"])
        self.assertEqual(get_json.call_args.args[0], "https://api.z.ai/api/monitor/usage/quota/limit")

    @patch.dict("os.environ", {"Z_AI_QUOTA_ENDPOINT": "https://evil.z.ai/api/monitor/usage/quota/limit"})
    @patch("agent_usage_collectors.zai.request_json")
    @patch("agent_usage_collectors.zai.find_any_key", return_value="zai-test-key")
    def test_zai_rejects_unlisted_quota_override_without_sending_key(self, _key: object, get_json: object) -> None:
        record = collect_zai()
        self.assertEqual(record["usageStatusText"], "Z.AI quota endpoint is invalid")
        get_json.assert_not_called()

    @patch("agent_usage_collectors.zai.setting", side_effect=lambda env, config: {"Z_AI_USAGE_SCOPE": "team", "Z_AI_ORGANIZATION": "org-1", "Z_AI_PROJECT": "project-1"}.get(env))
    @patch("agent_usage_collectors.zai.request_json")
    @patch("agent_usage_collectors.zai.find_any_key", return_value="zai-test-key")
    def test_zai_team_quota_adds_scope_headers_and_query(self, _key: object, get_json: object, _setting: object) -> None:
        get_json.return_value = {"success": True, "code": 200, "data": {"limits": [{"type": "TOKENS_LIMIT", "unit": 6, "number": 1, "percentage": 3}]}}
        record = collect_zai()
        self.assertTrue(record["ready"])
        self.assertIn("type=2", get_json.call_args.args[0])
        self.assertEqual(get_json.call_args.kwargs["headers"]["Bigmodel-Organization"], "org-1")
        self.assertEqual(get_json.call_args.kwargs["headers"]["Bigmodel-Project"], "project-1")

    @patch("agent_usage_collectors.zai.setting", side_effect=lambda env, config: "team" if env == "Z_AI_USAGE_SCOPE" else None)
    @patch("agent_usage_collectors.zai.find_any_key", return_value="zai-test-key")
    def test_zai_team_scope_requires_selectors(self, _key: object, _setting: object) -> None:
        record = collect_zai()
        self.assertEqual(record["usageStatusText"], "Z.AI team details required")
        self.assertIn("organization", record["authHelpText"])

    @patch("agent_usage_collectors.xai.get_json")
    @patch("agent_usage_collectors.xai.find_setting", return_value=None)
    @patch("agent_usage_collectors.xai.find_key", return_value="management-secret")
    def test_xai_collects_validated_team_prepaid_credit(self, _key: object, _team: object, get_json: object) -> None:
        get_json.side_effect = [
            {"scope": "SCOPE_TEAM", "scopeId": "team-1"},
            {"total": {"val": "-500"}},
        ]
        from agent_usage_collectors.xai import collect as collect_xai
        record = collect_xai()
        self.assertEqual(record["balance"], {"remaining": 5.0, "currency": "USD"})
        self.assertEqual(get_json.call_count, 2)

    @patch("agent_usage_collectors.xai.get_json")
    @patch("agent_usage_collectors.xai.find_key", return_value="management-secret")
    def test_xai_rejected_management_key_is_explicit(self, _key: object, get_json: object) -> None:
        get_json.side_effect = HTTPError("https://x", 403, "no", {}, None)
        from agent_usage_collectors.xai import collect as collect_xai
        record = collect_xai()
        self.assertEqual(record["usageStatusText"], "Management key rejected")

    def test_kimi_maps_weekly_and_rolling_windows(self) -> None:
        record = kimi_record({
            "user": {"membership": {"level": "LEVEL_INTERMEDIATE"}},
            "usage": {"limit": "100", "remaining": "74", "resetTime": "2026-08-25T17:32:50Z"},
            "limits": [{
                "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                "detail": {"limit": 100, "used": 15, "resetTime": "2026-08-23T12:32:50Z"},
            }],
        })
        self.assertEqual(record["tierLabel"], "Intermediate")
        self.assertEqual(record["limits"][0]["title"], "Session")
        self.assertEqual(record["limits"][0]["percent"], 0.15)
        self.assertEqual(record["limits"][1]["percent"], 0.26)

    def test_gemini_maps_current_cli_bucket_shape(self) -> None:
        record = gemini_record(
            {"currentTier": {"name": "Google AI Pro"}},
            {"buckets": [
                {"modelId": "gemini-3-pro", "remainingFraction": 0.4, "resetTime": "2026-08-24T00:00:00Z"},
                {"modelId": "gemini-3-flash", "remainingFraction": 0.9},
            ]},
        )
        self.assertEqual(record["tierLabel"], "Google AI Pro")
        self.assertEqual(record["limits"][0]["title"], "Pro")
        self.assertEqual(record["limits"][0]["percent"], 0.6)
        self.assertEqual(record["limits"][1]["title"], "Flash")

    def test_cursor_maps_dashboard_subscription_pools(self) -> None:
        record = cursor_record({
            "membershipType": "ultra",
            "billingCycleEnd": "2026-09-01T00:00:00Z",
            "isUnlimited": False,
            "individualUsage": {"plan": {"autoPercentUsed": 98.1, "apiPercentUsed": 100, "totalPercentUsed": 98.5}},
        })
        self.assertEqual(record["tierLabel"], "Ultra")
        self.assertEqual([limit["title"] for limit in record["limits"]], ["Cursor Models", "Other Models", "Included total"])
        self.assertEqual(record["limits"][0]["percent"], 0.981)

    def test_cursor_unlimited_is_a_real_ready_state_without_fake_meter(self) -> None:
        record = cursor_record({"membershipType": "business", "billingCycleEnd": "2026-09-01T00:00:00Z", "isUnlimited": True})
        self.assertTrue(record["ready"])
        self.assertEqual(record["limits"], [])
        self.assertIn("unlimited", record["tierLabel"])

    def test_auth_and_transport_states_have_correct_retry_behavior(self) -> None:
        error = HTTPError("https://x", 401, "no", {}, None)
        rejected = classify_failure(base_record("x", "X", "X"), "X", error, "Fix auth")
        error.close()
        network = classify_failure(base_record("x", "X", "X"), "X", URLError("offline"), "Fix auth")
        self.assertEqual(rejected["usageStatusText"], "API key rejected")
        self.assertNotIn("retryAdvised", rejected)
        self.assertTrue(network["retryAdvised"])

    def test_endpoint_problem_carries_forward_last_known_good_reading(self) -> None:
        with tempfile.TemporaryDirectory() as state_home:
            usage_dir = Path(state_home) / "omarchy" / "agents" / "usage"
            usage_dir.mkdir(parents=True)
            (usage_dir / "x.json").write_text(json.dumps({
                "id": "x",
                "updatedAt": "2026-08-24T00:00:00Z",
                "limits": [{"label": "Session", "percent": 42}],
            }), encoding="utf-8")
            with patch.dict("os.environ", {"XDG_STATE_HOME": state_home}):
                error = HTTPError("https://x", 429, "rate limited", {}, None)
                record = classify_failure(base_record("x", "X", "X"), "X", error, "Fix auth")
                error.close()
        self.assertEqual(record["limits"], [{"label": "Session", "percent": 42}])
        self.assertEqual(record["updatedAt"], "2026-08-24T00:00:00Z")
        self.assertEqual(record["usageStatusText"], "X usage unavailable")

    def test_endpoint_problem_without_a_prior_reading_stays_empty(self) -> None:
        with tempfile.TemporaryDirectory() as state_home:
            with patch.dict("os.environ", {"XDG_STATE_HOME": state_home}):
                record = endpoint_problem(base_record("x", "X", "X"), "X down", "help")
        self.assertEqual(record["limits"], [])
        self.assertNotIn("balance", record)

    def test_transcript_cost_decorator_uses_complete_known_model_pricing(self) -> None:
        record = decorate({
            "id": "claude",
            "activeDays": 4,
            "modelUsage": {
                "claude-sonnet-5": {
                    "inputTokens": 1_000_000,
                    "outputTokens": 1_000_000,
                    "cacheReadInputTokens": 1_000_000,
                    "cacheCreationInputTokens": 1_000_000,
                },
            },
        }, "claude", "Local transcript history")
        self.assertEqual(record["cost"]["estimateUsd"], 14.7)
        self.assertEqual(record["cost"]["activeDays"], 4)

    def test_transcript_cost_decorator_labels_a_partial_unknown_model_total(self) -> None:
        record = decorate({
            "id": "codex",
            "modelUsage": {
                "gpt-5.6-sol": {"inputTokens": 1},
                "unpriced-model": {"outputTokens": 1},
            },
        }, "codex", "Local transcript history")
        self.assertTrue(record["cost"]["incomplete"])
        self.assertEqual(record["cost"]["unknownModels"], ["unpriced-model"])

    def test_transcript_cost_decorator_upgrades_old_daily_scalar_totals(self) -> None:
        record = {"todayTokensByModel": {"model": 42}}
        normalise_today_buckets(record)
        self.assertEqual(record["todayTokensByModel"]["model"], {
            "inputTokens": 42,
            "outputTokens": 0,
            "cacheReadInputTokens": 0,
            "cacheCreationInputTokens": 0,
        })

    def test_transcript_cost_decorator_keeps_the_base_record_on_bad_legacy_buckets(self) -> None:
        record = {"todayTokensByModel": {
            "scalar": float("inf"),
            "partial": {"inputTokens": "not-a-number", "outputTokens": 42},
        }}
        normalise_today_buckets(record)
        self.assertEqual(record["todayTokensByModel"], {
            "scalar": {
                "inputTokens": 0,
                "outputTokens": 0,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
            },
            "partial": {
                "inputTokens": 0,
                "outputTokens": 42,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 0,
            },
        })


class OpenCodeGoCollectorTests(unittest.TestCase):
    def test_stats_from_rows_aggregates_today_and_all_time(self) -> None:
        today_ms = int(_utc_midnight_ms(days_ago=0))
        yesterday_ms = int(_utc_midnight_ms(days_ago=1))
        rows = [
            ("session-1", today_ms, "claude-sonnet-5", 100, 50, 10, 0),
            ("session-1", today_ms, "claude-sonnet-5", 20, 5, 0, 0),
            ("session-2", yesterday_ms, "gpt-5", 200, 100, 0, 0),
        ]
        stats = opencode_go_stats_from_rows(rows)
        self.assertEqual(stats["todayPrompts"], 2)
        self.assertEqual(stats["todaySessions"], 1)
        self.assertEqual(stats["todayTotalTokens"], 185)
        self.assertEqual(stats["totalPrompts"], 3)
        self.assertEqual(stats["totalSessions"], 2)
        self.assertEqual(stats["activeDays"], 2)
        self.assertEqual(
            stats["modelUsage"]["claude-sonnet-5"],
            {"inputTokens": 120, "outputTokens": 55, "cacheReadInputTokens": 10, "cacheCreationInputTokens": 0},
        )
        self.assertEqual(len(stats["recentDays"]), 7)
        self.assertEqual(stats["recentDays"][-1]["messageCount"], 185)

    def test_stats_from_rows_empty_is_a_valid_zero_state(self) -> None:
        stats = opencode_go_stats_from_rows([])
        self.assertEqual(stats["totalPrompts"], 0)
        self.assertEqual(stats["recentDays"], [])

    def test_limit_window_scales_percent_from_0_100_to_0_1(self) -> None:
        window = opencode_go_limit_window({"rolling": {"percent": 42, "resetsAt": "2026-08-23T18:00:00+00:00"}}, "rolling", "Session")
        self.assertEqual(window["percent"], 0.42)
        self.assertEqual(window["title"], "Session")

    def test_limit_window_rejects_missing_percent(self) -> None:
        with self.assertRaisesRegex(ValueError, "percent is missing"):
            opencode_go_limit_window({"rolling": {}}, "rolling", "Session")

    def test_collect_reports_missing_auth_but_keeps_local_stats(self) -> None:
        stats = {
            "todayPrompts": 3, "todaySessions": 1, "todayTotalTokens": 900, "todayTokensByModel": {},
            "recentDays": [], "modelUsage": {}, "totalPrompts": 3, "totalSessions": 1,
            "activeDays": 1, "activeDates": [],
        }
        with tempfile.TemporaryDirectory() as empty_state_dir:
            with patch.object(opencode_go, "read_key", return_value=None), patch.object(opencode_go, "collect_local_stats", return_value=stats), patch("agent_usage_collectors.common.usage_dir", return_value=Path(empty_state_dir)):
                record = collect_opencode_go()
        self.assertFalse(record["ready"])
        self.assertEqual(record["usageStatusText"], "Waiting for auth")
        self.assertEqual(record["todayTotalTokens"], 900)
        self.assertEqual(record["limits"], [])

    def test_collect_reports_sign_in_expired_without_dropping_local_stats(self) -> None:
        stats = {
            "todayPrompts": 3, "todaySessions": 1, "todayTotalTokens": 900, "todayTokensByModel": {},
            "recentDays": [], "modelUsage": {}, "totalPrompts": 3, "totalSessions": 1,
            "activeDays": 1, "activeDates": [],
        }
        error = HTTPError("https://opencode.ai/zen/go/v1/usage", 401, "unauthorized", None, None)
        with tempfile.TemporaryDirectory() as empty_state_dir:
            with patch.object(opencode_go, "read_key", return_value="stale-key"), patch.object(opencode_go, "collect_local_stats", return_value=stats), patch.object(opencode_go, "request_json", side_effect=error), patch("agent_usage_collectors.common.usage_dir", return_value=Path(empty_state_dir)):
                record = collect_opencode_go()
        self.assertEqual(record["usageStatusText"], "OpenCode Go sign-in expired")
        self.assertEqual(record["todayTotalTokens"], 900)

    def test_collect_with_working_key_reports_all_three_windows(self) -> None:
        stats = opencode_go_stats_from_rows([])
        payload = {
            "usage": {
                "rolling": {"percent": 10, "resetsAt": "2026-08-23T18:00:00+00:00"},
                "weekly": {"percent": 20, "resetsAt": "2026-08-27T00:00:00+00:00"},
                "monthly": {"percent": 30, "resetsAt": "2026-09-01T00:00:00+00:00"},
            }
        }
        with patch.object(opencode_go, "read_key", return_value="live-key"), patch.object(opencode_go, "collect_local_stats", return_value=stats), patch.object(opencode_go, "request_json", return_value=payload):
            record = collect_opencode_go()
        self.assertTrue(record["ready"])
        self.assertEqual([limit["title"] for limit in record["limits"]], ["Session", "Weekly", "Monthly"])
        self.assertEqual(record["limits"][1]["percent"], 0.2)


def _utc_midnight_ms(days_ago: int) -> float:
    from datetime import datetime, timedelta, timezone

    midnight = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
    return midnight.timestamp() * 1000


if __name__ == "__main__":
    unittest.main()

"""Local tests for the Odoo-free LLM usage DTO helpers."""

from datetime import date, datetime, timezone
import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "llm_usage", ROOT / "src" / "services" / "llm_usage.py"
)
llm_usage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(llm_usage)


class ParseRangeTests(unittest.TestCase):
    def test_today_uses_taipei_calendar_date(self):
        now = datetime(2026, 10, 19, 17, 0, tzinfo=timezone.utc)
        self.assertEqual(
            llm_usage.parse_range("today", now=now),
            (date(2026, 10, 20), date(2026, 10, 20)),
        )

    def test_previous_month_and_month_to_date(self):
        now = datetime(2026, 3, 2, 1, tzinfo=llm_usage.TAIPEI)
        self.assertEqual(
            llm_usage.parse_range("last_month", now=now),
            (date(2026, 2, 1), date(2026, 2, 28)),
        )
        self.assertEqual(
            llm_usage.parse_range("month", now=now),
            (date(2026, 3, 1), date(2026, 3, 2)),
        )

    def test_custom_31_days_allowed_but_32_rejected(self):
        now = datetime(2026, 10, 20, 12, tzinfo=llm_usage.TAIPEI)
        self.assertEqual(
            llm_usage.parse_range("custom", "2026-09-20", "2026-10-20", now),
            (date(2026, 9, 20), date(2026, 10, 20)),
        )
        with self.assertRaises(llm_usage.UsageInvalid):
            llm_usage.parse_range("custom", "2026-09-19", "2026-10-20", now)

    def test_rejects_future_too_old_malformed_and_non_string_dates(self):
        now = datetime(2026, 10, 20, 12, tzinfo=llm_usage.TAIPEI)
        for start, end in (
            ("2026-10-21", "2026-10-21"),
            ("2026-04-23", "2026-04-23"),
            ("2026-1-01", "2026-01-02"),
            ("2026-02-30", "2026-03-01"),
            (date(2026, 10, 1), "2026-10-02"),
            ("2026-10-02", "2026-10-01"),
        ):
            with self.subTest(start=start, end=end):
                with self.assertRaises(llm_usage.UsageInvalid):
                    llm_usage.parse_range("custom", start, end, now)
        with self.assertRaises(llm_usage.UsageInvalid):
            llm_usage.parse_range("today", now=datetime(2026, 1, 1))

    def test_day_bounds_are_taipei_midnight_inclusive(self):
        start, end = llm_usage.day_bounds(date(2026, 10, 20))
        self.assertEqual(start, 1792425600)
        self.assertEqual(end, start + 86399)


class FlowAndSummaryTests(unittest.TestCase):
    def setUp(self):
        self.day = date(2026, 10, 20)
        self.local_keys = {
            41: {"id": 7, "name": "Visible key", "kind": "paid"},
        }

    def test_flow_maps_local_key_and_does_not_leak_foreign_fields(self):
        rows = llm_usage.normalize_flow([{
            "token_id": 41,
            "use_group": "secret-group",
            "model_name": "vendor/model-v1",
            "count": 3,
            "quota": 2,
            "token_used": 12,
            "channel_name": "private channel",
            "raw_secret": "must not appear",
        }], self.local_keys, self.day)
        self.assertEqual(rows, [{
            "day": "2026-10-20",
            "key_id": 7,
            "key_name": "Visible key",
            "key_kind": "paid",
            "model": "vendor/model-v1",
            "requests": 3,
            "quota": 2,
            "tokens": 12,
        }])

    def test_deleted_key_retains_totals_without_foreign_identity(self):
        row = llm_usage.normalize_flow([{
            "token_id": 987654,
            "use_group": "sensitive",
            "model_name": "model",
            "count": 4,
            "quota": 5,
            "token_used": 6,
            "token_name": "private upstream name",
        }], self.local_keys, self.day)[0]
        self.assertEqual((row["key_id"], row["key_name"], row["key_kind"]), (None, None, None))
        self.assertEqual((row["requests"], row["quota"], row["tokens"]), (4, 5, 6))
        self.assertNotIn("token_id", row)
        self.assertNotIn("use_group", row)

    def test_omitted_flow_token_id_means_unknown_key_without_losing_totals(self):
        row = llm_usage.normalize_flow([{
            "use_group": "default", "model_name": "model", "count": 2,
            "quota": 3, "token_used": 4,
        }], self.local_keys, self.day)[0]
        self.assertEqual((row["key_id"], row["key_name"], row["key_kind"]),
                         (None, None, None))
        self.assertEqual((row["requests"], row["quota"], row["tokens"]), (2, 3, 4))

    def test_bad_flow_rows_fail_whole_response_including_bools_and_negatives(self):
        valid = {"token_id": 0, "use_group": "default", "model_name": "model", "count": 1,
                 "quota": 1, "token_used": 1}
        for changes in ({"count": True}, {"quota": -1}, {"token_used": "2"},
                        {"token_id": False}, {"model_name": "bad model"}):
            with self.subTest(changes=changes):
                with self.assertRaises(llm_usage.UsageInvalid):
                    llm_usage.normalize_flow([valid, {**valid, **changes}], {}, self.day)
        with self.assertRaises(llm_usage.UsageInvalid):
            llm_usage.normalize_flow([valid] * 10001, {}, self.day)

    def test_summary_sums_tiny_quotas_before_conversion_and_keeps_io_honest(self):
        summary = llm_usage.summarize(
            [{"quota": 1, "requests": 1, "tokens": 2},
             {"quota": 1, "requests": 2, "tokens": 3}],
            balance_quota=10,
            recent_rows=[{"quota": 7, "requests": 0, "tokens": 0}],
            free_today_count=4,
        )
        self.assertEqual(summary["quota"], 2)
        self.assertEqual(summary["spend_points"], 2 / 15625)
        self.assertEqual(summary["requests"], 3)
        self.assertEqual(summary["tokens"], 5)
        self.assertIsNone(summary["input_tokens"])
        self.assertIsNone(summary["output_tokens"])
        self.assertEqual(summary["days_left"], 10)
        self.assertEqual(summary["free_today_count"], 4)

    def test_summary_rejects_negative_and_boolean_numbers(self):
        for rows, balance in (([{"quota": True, "requests": 0, "tokens": 0}], 0),
                              ([{"quota": -1, "requests": 0, "tokens": 0}], 0),
                              ([], True)):
            with self.subTest(rows=rows, balance=balance):
                with self.assertRaises(llm_usage.UsageInvalid):
                    llm_usage.summarize(rows, balance_quota=balance)

    def test_negative_balance_is_clamped_before_days_left_estimate(self):
        summary = llm_usage.summarize(
            [], balance_quota=-9,
            recent_rows=[{"quota": 7, "requests": 1, "tokens": 1}],
        )
        self.assertEqual(summary["days_left"], 0)

    def test_flow_with_missing_local_label_keeps_id_and_returns_null_label(self):
        row = llm_usage.normalize_flow([{
            "token_id": 41, "use_group": "paid", "model_name": "model-a",
            "count": 1, "quota": 5, "token_used": 8,
        }], {41: {"id": 7, "name": None, "kind": "paid"}}, self.day)[0]
        self.assertEqual((row["key_id"], row["key_name"], row["key_kind"]),
                         (7, None, "paid"))


class DailySeriesTests(unittest.TestCase):
    def test_spend_recomputes_from_integer_quota_sum(self):
        day = date(2026, 10, 1)
        rows = [
            {"day": day.isoformat(), "key_id": 7, "key_name": "Key",
             "model": "model-a", "requests": 1, "quota": amount, "tokens": 1}
            for amount in (3, 7)
        ]
        chart = llm_usage.daily_series(rows, day, day, group_by="key", metric="spend")
        self.assertEqual(chart["series"][0]["quota"], [10])
        self.assertEqual(chart["series"][0]["values"], [10 / 15625])

    def test_key_aggregation_uses_provided_day_and_zero_fills_missing_dates(self):
        start, end = date(2026, 10, 1), date(2026, 10, 3)
        flow = llm_usage.normalize_flow([
            {"token_id": 1, "use_group": "default", "model_name": "model-a", "count": 1,
             "quota": 2, "token_used": 8},
            {"token_id": 2, "use_group": "paid", "model_name": "model-b", "count": 3,
             "quota": 5, "token_used": 9},
        ], {
            1: {"id": 10, "name": "Key A", "kind": "paid"},
            2: {"id": 10, "name": "Key A", "kind": "paid"},
        }, date(2026, 10, 1))
        chart = llm_usage.daily_series(flow, start, end, group_by="key", metric="requests")
        self.assertEqual(chart["dates"], ["2026-10-01", "2026-10-02", "2026-10-03"])
        self.assertEqual(chart["series"], [{
            "id": "10", "label": "Key A", "values": [4, 0, 0], "quota": [7, 0, 0]
        }])
        unknown = [{**flow[0], "key_id": None, "key_name": None}]
        chart_unknown = llm_usage.daily_series(unknown, start, end, group_by="key")
        self.assertEqual(chart_unknown["series"][0]["id"], "unknown")
        self.assertIsNone(chart_unknown["series"][0]["label"])


class LogDtoTests(unittest.TestCase):
    @staticmethod
    def log_row(**overrides):
        row = {
            "type": 5,
            "created_at": 1792472400,
            "token_name": "upstream-token",
            "model_name": "vendor/model-v1",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "quota": 0,
            "use_time": 2,
            "request_id": "req_123.abc",
            "other": '{"status_code":429,"error_type":"rate_limit_error",'
                     '"message":"private error body", "cache_tokens":3,'
                     '"admin_info":{"user":"private"}}',
            "content": "private prompt",
            "channel": 44,
            "channel_name": "private channel",
            "admin_info": "private admin",
        }
        row.update(overrides)
        return row

    def test_logs_convert_taipei_time_and_allowlist_error_fields(self):
        dto = llm_usage.normalize_logs({"items": [self.log_row()], "total": 1}, {
            "upstream-token": {"id": 12, "name": "Local display"},
        })
        item = dto["items"][0]
        self.assertEqual(item["time_tpe"], "2026-10-20T13:00:00+08:00")
        self.assertEqual(item["key_id"], 12)
        self.assertEqual(item["key_name"], "Local display")
        self.assertEqual(item["cache_tokens"], 3)
        self.assertEqual(item["duration_ms"], 2000)
        self.assertEqual(item["error_code"], "rate_limit")
        self.assertEqual(item["status"], "error")
        self.assertEqual(set(item), {
            "time_tpe", "key_id", "key_name", "model", "input_tokens",
            "output_tokens", "cache_tokens", "duration_ms", "quota",
            "spend_points", "request_id", "status", "error_code",
        })
        self.assertNotIn("content", item)
        self.assertNotIn("channel", item)
        self.assertNotIn("private error body", repr(item))
        self.assertNotIn("private admin", repr(item))

    def test_unknown_key_and_unsafe_request_id_are_not_exposed(self):
        dto = llm_usage.normalize_logs({"items": [self.log_row(
            token_name="deleted-upstream-token", request_id="bad/id",
            other='{"error_code":"attacker-controlled-raw-text"}',
        )], "total": 1}, {})
        item = dto["items"][0]
        self.assertIsNone(item["key_id"])
        self.assertIsNone(item["key_name"])
        self.assertIsNone(item["request_id"])
        self.assertEqual(item["error_code"], "upstream_error")

    def test_error_logs_allow_empty_model_and_token_name_but_success_model_stays_strict(self):
        error = self.log_row(type=5, model_name="", token_name="")
        dto = llm_usage.normalize_logs({"items": [error], "total": 1}, {})
        item = dto["items"][0]
        self.assertIsNone(item["model"])
        self.assertIsNone(item["key_id"])
        self.assertIsNone(item["key_name"])

        missing_model = self.log_row(type=5, token_name="")
        del missing_model["model_name"]
        missing_dto = llm_usage.normalize_logs({"items": [missing_model], "total": 1}, {})
        self.assertIsNone(missing_dto["items"][0]["model"])

        success = self.log_row(type=2, model_name="", token_name="")
        with self.assertRaises(llm_usage.UsageInvalid):
            llm_usage.normalize_logs({"items": [success], "total": 1}, {})

    def test_rejects_other_log_types_bad_numbers_and_oversized_page(self):
        for changes in ({"type": 1}, {"prompt_tokens": True}, {"quota": -1},
                        {"use_time": "2"}):
            with self.subTest(changes=changes):
                with self.assertRaises(llm_usage.UsageInvalid):
                    llm_usage.normalize_logs({"items": [self.log_row(**changes)], "total": 1}, {})
        with self.assertRaises(llm_usage.UsageInvalid):
            llm_usage.normalize_logs({"items": [self.log_row()] * 51, "total": 51}, {})

    def test_oversized_other_json_is_ignored_without_logging_or_leaking(self):
        huge_private = "secret-value" * 2000
        dto = llm_usage.normalize_logs({"items": [self.log_row(
            other='{"message":"' + huge_private + '"}'
        )], "total": 1}, {})
        self.assertEqual(dto["items"][0]["error_code"], "upstream_error")
        self.assertNotIn(huge_private, repr(dto))


if __name__ == "__main__":
    unittest.main()

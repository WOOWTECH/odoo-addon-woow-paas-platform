#!/usr/bin/env python3
"""board-scan.py 的 stdlib-only 自測。"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Sequence

SCRIPT = Path(__file__).with_name("board-scan.py")
SPEC = importlib.util.spec_from_file_location("board_scan", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"無法載入 {SCRIPT}")
board_scan = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = board_scan
SPEC.loader.exec_module(board_scan)


def completed(
    payload: object = None,
    *,
    returncode: int = 0,
    stdout: bytes | None = None,
    stderr: bytes = b"",
) -> subprocess.CompletedProcess[bytes]:
    if stdout is None:
        stdout = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def issue(
    index: int,
    *,
    labels: Sequence[str] = (),
    title: str | None = None,
    pull_request: object = None,
) -> dict[str, object]:
    return {
        "number": index,
        "state": "open",
        "labels": [{"name": label} for label in labels],
        "title": title or f"Issue {index}",
        "updated_at": "2026-09-15T05:58:00Z",
        "pull_request": pull_request,
    }


def metadata(repo: str, count: int) -> dict[str, object]:
    return {"full_name": repo, "open_issues_count": count}


def page_endpoint(repo: str, page: int) -> str:
    return f"repos/{repo}/issues?state=open&type=all&page={page}&limit=50"


class FakeRunner:
    def __init__(
        self, responses: dict[str, subprocess.CompletedProcess[bytes]]
    ) -> None:
        self.responses = responses
        self.commands: list[list[str]] = []

    def __call__(
        self, command: Sequence[str]
    ) -> subprocess.CompletedProcess[bytes]:
        command_list = list(command)
        self.commands.append(command_list)
        endpoint = command_list[-1]
        if endpoint not in self.responses:
            raise AssertionError(f"未設定 mock response：{endpoint}")
        return self.responses[endpoint]


class BoardScanTest(unittest.TestCase):
    def test_multi_page_scan_skips_pull_requests(self) -> None:
        repo = "owner/repo"
        runner = FakeRunner(
            {
                page_endpoint(repo, 1): completed(
                    [
                        issue(3, labels=["agent-status:in-progress"]),
                        issue(99, pull_request={"url": "https://example.invalid/pr/99"}),
                    ]
                ),
                page_endpoint(repo, 2): completed([issue(2)]),
                page_endpoint(repo, 3): completed([]),
                f"repos/{repo}": completed(metadata(repo, 2)),
            }
        )

        result = board_scan.scan_repository(board_scan.TeaApi("login", runner), repo)

        self.assertEqual([item.index for item in result.issues], [3, 2])
        self.assertEqual([item.index for item in result.matches], [3])
        self.assertEqual(result.api_open_issues_count, 2)

    def test_empty_first_page_is_valid(self) -> None:
        repo = "owner/empty"
        runner = FakeRunner(
            {
                page_endpoint(repo, 1): completed([]),
                f"repos/{repo}": completed(metadata(repo, 0)),
            }
        )

        result = board_scan.scan_repository(board_scan.TeaApi("login", runner), repo)

        self.assertEqual(result.issues, ())
        self.assertEqual(result.matches, ())

    def test_pull_request_only_page_does_not_end_scan(self) -> None:
        repo = "owner/with-pr"
        runner = FakeRunner(
            {
                page_endpoint(repo, 1): completed(
                    [issue(10, pull_request={"url": "https://example.invalid/pr/10"})]
                ),
                page_endpoint(repo, 2): completed(
                    [issue(9, labels=["needs-info"])]
                ),
                page_endpoint(repo, 3): completed([]),
                f"repos/{repo}": completed(metadata(repo, 1)),
            }
        )

        result = board_scan.scan_repository(board_scan.TeaApi("login", runner), repo)

        self.assertEqual([item.index for item in result.issues], [9])
        self.assertEqual([item.index for item in result.matches], [9])

    def test_invalid_slug_is_rejected_before_api_call(self) -> None:
        runner = FakeRunner({})

        with self.assertRaisesRegex(board_scan.ScanError, "repo slug 格式錯誤"):
            board_scan.scan_repository(
                board_scan.TeaApi("login", runner), "missing-owner"
            )

        self.assertEqual(runner.commands, [])

    def test_api_nonzero_exit_fails_loud(self) -> None:
        repo = "owner/missing"
        runner = FakeRunner(
            {
                page_endpoint(repo, 1): completed(
                    returncode=1, stdout=b"", stderr=b"404 Not Found\n"
                )
            }
        )

        with self.assertRaisesRegex(board_scan.ScanError, r"rc=1.*404 Not Found"):
            board_scan.scan_repository(board_scan.TeaApi("login", runner), repo)

    def test_api_error_json_fails_loud(self) -> None:
        repo = "owner/missing"
        runner = FakeRunner(
            {
                page_endpoint(repo, 1): completed(
                    {"message": "not found", "url": "https://example.invalid/swagger"}
                )
            }
        )

        with self.assertRaisesRegex(board_scan.ScanError, "Gitea API error：not found"):
            board_scan.scan_repository(board_scan.TeaApi("login", runner), repo)

    def test_invalid_json_fails_loud(self) -> None:
        repo = "owner/bad-json"
        runner = FakeRunner(
            {page_endpoint(repo, 1): completed(stdout=b"{not json")}
        )

        with self.assertRaisesRegex(board_scan.ScanError, "不是合法 JSON"):
            board_scan.scan_repository(board_scan.TeaApi("login", runner), repo)

    def test_invalid_utf8_fails_loud(self) -> None:
        repo = "owner/bad-utf8"
        runner = FakeRunner({page_endpoint(repo, 1): completed(stdout=b"\xff")})

        with self.assertRaisesRegex(board_scan.ScanError, "不是合法 UTF-8"):
            board_scan.scan_repository(board_scan.TeaApi("login", runner), repo)

    def test_invalid_issue_field_fails_loud(self) -> None:
        repo = "owner/bad-field"
        malformed = issue(1)
        malformed["labels"] = "agent-status:in-progress"
        runner = FakeRunner({page_endpoint(repo, 1): completed([malformed])})

        with self.assertRaisesRegex(board_scan.ScanError, "labels 必須是 JSON array"):
            board_scan.scan_repository(board_scan.TeaApi("login", runner), repo)

    def test_label_filter_uses_exact_prefixes(self) -> None:
        repo = "owner/labels"
        runner = FakeRunner(
            {
                page_endpoint(repo, 1): completed(
                    [
                        issue(4, labels=["agent-status:blocked", "priority:high"]),
                        issue(3, labels=["needs-decision"]),
                        issue(2, labels=["agent-status", "needs"]),
                        issue(1, labels=["priority:low"]),
                    ]
                ),
                page_endpoint(repo, 2): completed([]),
                f"repos/{repo}": completed(metadata(repo, 4)),
            }
        )

        result = board_scan.scan_repository(board_scan.TeaApi("login", runner), repo)

        self.assertEqual([item.index for item in result.matches], [4, 3])

    def test_multiple_repositories_print_summaries_at_end(self) -> None:
        first = "owner/first"
        second = "owner/second"
        runner = FakeRunner(
            {
                page_endpoint(first, 1): completed(
                    [issue(1, labels=["needs-info"], title="第一張")]
                ),
                page_endpoint(first, 2): completed([]),
                f"repos/{first}": completed(metadata(first, 1)),
                page_endpoint(second, 1): completed([]),
                f"repos/{second}": completed(metadata(second, 0)),
            }
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        returncode = board_scan.main(
            [first, second], runner=runner, stdout=stdout, stderr=stderr
        )

        self.assertEqual(returncode, 0)
        self.assertEqual(stderr.getvalue(), "")
        lines = stdout.getvalue().splitlines()
        self.assertEqual(lines[0].split()[0], "MATCH")
        self.assertTrue(lines[1].startswith(f"SUMMARY repo={first} open=1"))
        self.assertTrue(lines[2].startswith(f"SUMMARY repo={second} open=0"))

    def test_open_count_mismatch_fails_loud(self) -> None:
        repo = "owner/mismatch"
        runner = FakeRunner(
            {
                page_endpoint(repo, 1): completed([issue(1)]),
                page_endpoint(repo, 2): completed([]),
                f"repos/{repo}": completed(metadata(repo, 2)),
            }
        )

        with self.assertRaisesRegex(board_scan.ScanError, "數量不一致"):
            board_scan.scan_repository(board_scan.TeaApi("login", runner), repo)

    def test_duplicate_index_across_pages_fails_loud(self) -> None:
        repo = "owner/duplicate"
        runner = FakeRunner(
            {
                page_endpoint(repo, 1): completed([issue(1)]),
                page_endpoint(repo, 2): completed([issue(1)]),
            }
        )

        with self.assertRaisesRegex(board_scan.ScanError, "在分頁間重複"):
            board_scan.scan_repository(board_scan.TeaApi("login", runner), repo)

    def test_bad_metadata_field_fails_loud(self) -> None:
        repo = "owner/bad-metadata"
        runner = FakeRunner(
            {
                page_endpoint(repo, 1): completed([]),
                f"repos/{repo}": completed(
                    {"full_name": repo, "open_issues_count": "0"}
                ),
            }
        )

        with self.assertRaisesRegex(board_scan.ScanError, "必須是非負整數"):
            board_scan.scan_repository(board_scan.TeaApi("login", runner), repo)

    def test_api_command_has_login_and_custom_user_agent(self) -> None:
        repo = "owner/headers"
        runner = FakeRunner(
            {
                page_endpoint(repo, 1): completed([]),
                f"repos/{repo}": completed(metadata(repo, 0)),
            }
        )

        board_scan.scan_repository(board_scan.TeaApi("named-login", runner), repo)

        first_command = runner.commands[0]
        self.assertEqual(first_command[:2], ["tea", "api"])
        self.assertIn("User-Agent: woow-issue-leader-board-scan/1.0", first_command)
        self.assertIn("named-login", first_command)
        self.assertNotIn("curl", first_command)


if __name__ == "__main__":
    unittest.main(verbosity=2)

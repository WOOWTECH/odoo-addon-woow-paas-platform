#!/usr/bin/env python3
"""以 Gitea API 完整掃描 open issue，供看板重建與 stale 掃描使用。"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Sequence, TextIO

DEFAULT_LOGIN = "git-prod.woowtech.io"
PAGE_SIZE = 50
USER_AGENT = "woow-issue-leader-board-scan/1.0"
REPO_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[bytes]]


class ScanError(RuntimeError):
    """掃描無法證明完整時的 fail-loud 錯誤。"""


@dataclass(frozen=True)
class Issue:
    repo: str
    index: int
    labels: tuple[str, ...]
    title: str
    updated_at: str


@dataclass(frozen=True)
class RepoScan:
    repo: str
    issues: tuple[Issue, ...]
    matches: tuple[Issue, ...]
    api_open_issues_count: int


def _run_command(command: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        list(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )


class TeaApi:
    """只透過 tea 管理的登入憑證呼叫 Gitea API。"""

    def __init__(self, login: str, runner: Runner | None = None) -> None:
        self.login = login
        self.runner = runner or _run_command

    def get(self, endpoint: str, context: str) -> object:
        command = [
            "tea",
            "api",
            "-H",
            f"User-Agent: {USER_AGENT}",
            "--login",
            self.login,
            endpoint,
        ]
        try:
            result = self.runner(command)
        except subprocess.TimeoutExpired as exc:
            raise ScanError(f"{context}：tea api 逾時") from exc
        except OSError as exc:
            raise ScanError(f"{context}：無法執行 tea api：{exc}") from exc

        if result.returncode != 0:
            stderr = _decode_diagnostic(result.stderr).strip()
            detail = f"；stderr={stderr}" if stderr else "；stderr=<空>"
            raise ScanError(
                f"{context}：tea api 失敗（rc={result.returncode}）{detail}"
            )

        try:
            text = result.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ScanError(
                f"{context}：tea api stdout 不是合法 UTF-8（byte {exc.start}）"
            ) from exc

        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ScanError(
                f"{context}：tea api stdout 不是合法 JSON（line {exc.lineno}, column {exc.colno}）"
            ) from exc


def _decode_diagnostic(value: bytes) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _validate_repo_slug(repo: str) -> None:
    if not REPO_SLUG.fullmatch(repo):
        raise ScanError(f"repo slug 格式錯誤：{repo!r}（預期 owner/repo）")


def _positive_index(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ScanError(f"{context}：number 必須是正整數")
    return value


def _parse_issue(repo: str, item: object, page: int, position: int) -> Issue | None:
    context = f"repo={repo} page={page} item={position}"
    if not isinstance(item, dict):
        raise ScanError(f"{context}：issue 項目必須是 JSON object")

    index = _positive_index(item.get("number"), context)
    if item.get("state") != "open":
        raise ScanError(f"{context} index={index}：state 不是 open")

    if "pull_request" not in item:
        raise ScanError(f"{context} index={index}：缺少 pull_request 欄位")
    pull_request = item["pull_request"]
    if pull_request is not None:
        if not isinstance(pull_request, dict):
            raise ScanError(f"{context} index={index}：pull_request 欄位格式錯誤")
        return None

    title = item.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ScanError(f"{context} index={index}：title 必須是非空字串")

    updated_at = item.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at.strip():
        raise ScanError(f"{context} index={index}：updated_at 必須是非空字串")

    raw_labels = item.get("labels")
    if not isinstance(raw_labels, list):
        raise ScanError(f"{context} index={index}：labels 必須是 JSON array")

    labels: list[str] = []
    for label_position, label in enumerate(raw_labels, start=1):
        if not isinstance(label, dict):
            raise ScanError(
                f"{context} index={index} label={label_position}：label 必須是 JSON object"
            )
        name = label.get("name")
        if not isinstance(name, str) or not name:
            raise ScanError(
                f"{context} index={index} label={label_position}：name 必須是非空字串"
            )
        labels.append(name)

    return Issue(
        repo=repo,
        index=index,
        labels=tuple(labels),
        title=title,
        updated_at=updated_at,
    )


def _has_board_label(issue: Issue) -> bool:
    return any(
        label.startswith("agent-status:") or label.startswith("needs-")
        for label in issue.labels
    )


def _read_open_count(api: TeaApi, repo: str) -> int:
    context = f"repo={repo} metadata"
    data = api.get(f"repos/{repo}", context)
    if not isinstance(data, dict):
        raise ScanError(f"{context}：repository metadata 必須是 JSON object")

    full_name = data.get("full_name")
    if not isinstance(full_name, str) or full_name.casefold() != repo.casefold():
        raise ScanError(f"{context}：full_name 與輸入 repo 不一致")

    count = data.get("open_issues_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ScanError(f"{context}：open_issues_count 必須是非負整數")
    return count


def scan_repository(api: TeaApi, repo: str) -> RepoScan:
    """逐頁掃描 repo；只有 API 回傳合法空 array 才算終點。"""

    _validate_repo_slug(repo)
    page = 1
    issues: list[Issue] = []
    seen_indexes: set[int] = set()

    while True:
        context = f"repo={repo} page={page}"
        endpoint = (
            f"repos/{repo}/issues?state=open&type=all&page={page}&limit={PAGE_SIZE}"
        )
        data = api.get(endpoint, context)
        if not isinstance(data, list):
            if isinstance(data, dict) and isinstance(data.get("message"), str):
                raise ScanError(f"{context}：Gitea API error：{data['message']}")
            raise ScanError(f"{context}：issues response 必須是 JSON array")
        if len(data) > PAGE_SIZE:
            raise ScanError(
                f"{context}：回傳 {len(data)} 筆，超過 limit={PAGE_SIZE}"
            )
        if not data:
            break

        for position, item in enumerate(data, start=1):
            if not isinstance(item, dict):
                raise ScanError(
                    f"{context} item={position}：issue 項目必須是 JSON object"
                )
            index = _positive_index(
                item.get("number"), f"{context} item={position}"
            )
            if index in seen_indexes:
                raise ScanError(f"{context}：index={index} 在分頁間重複")
            seen_indexes.add(index)

            issue = _parse_issue(repo, item, page, position)
            if issue is not None:
                issues.append(issue)
        page += 1

    api_count = _read_open_count(api, repo)
    if len(issues) != api_count:
        raise ScanError(
            f"repo={repo}：掃描 open issue={len(issues)}，"
            f"API open_issues_count={api_count}，數量不一致"
        )

    matches = tuple(issue for issue in issues if _has_board_label(issue))
    return RepoScan(
        repo=repo,
        issues=tuple(issues),
        matches=matches,
        api_open_issues_count=api_count,
    )


def _format_match(issue: Issue) -> str:
    labels = json.dumps(list(issue.labels), ensure_ascii=False, separators=(",", ":"))
    updated_at = json.dumps(issue.updated_at, ensure_ascii=False)
    title = json.dumps(issue.title, ensure_ascii=False)
    return (
        f"MATCH repo={issue.repo} index={issue.index} labels={labels} "
        f"updated_at={updated_at} title={title}"
    )


def _format_summary(scan: RepoScan) -> str:
    return (
        f"SUMMARY repo={scan.repo} open={len(scan.issues)} "
        f"api_open_issues_count={scan.api_open_issues_count} "
        f"matched={len(scan.matches)}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "逐頁掃描 Gitea open issue，列出 agent-status:* / needs-*，"
            "並核對 repository open_issues_count。"
        )
    )
    parser.add_argument(
        "--login",
        default=DEFAULT_LOGIN,
        help=f"tea login 名稱（預設：{DEFAULT_LOGIN}）",
    )
    parser.add_argument("repos", nargs="+", metavar="OWNER/REPO")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Runner | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    output = stdout or sys.stdout
    error_output = stderr or sys.stderr
    api = TeaApi(args.login, runner=runner)

    try:
        scans = [scan_repository(api, repo) for repo in args.repos]
    except ScanError as exc:
        print(f"ERROR {exc}", file=error_output)
        return 1

    for scan in scans:
        for issue in scan.matches:
            print(_format_match(issue), file=output)
    for scan in scans:
        print(_format_summary(scan), file=output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

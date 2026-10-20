"""#1190／商用驗收 X-BK-006-1、DATA-ODOO-005-2、AI-LLM-009-1：唯讀 backup-status。

備份失敗以前只有叢集通用 KubeJobFailed 寄 ops 信箱，平台服務頁與 API 都沒有任何備份
欄位；LiteLLM 空間不足只在 sidecar log 印 WARNING。這裡鎖定 operator 端的事實彙整：
woow-odoo 讀 backup Job＋terminationMessage，litellm 讀 db-agent 狀態列。
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.services import backup_status as bs
from src.services.helm import KubectlException

NS = "paas-ws-50"
REL = "svc-239"


def _job(name, *, started, kind="daily", complete=None, failed=None, failed_reason="BackoffLimitExceeded",
         active=0):
    status = {"startTime": started}
    conditions = []
    if complete:
        status["completionTime"] = complete
        conditions.append({"type": "Complete", "status": "True", "lastTransitionTime": complete})
    if failed:
        conditions.append({"type": "Failed", "status": "True", "lastTransitionTime": failed,
                           "reason": failed_reason})
    if active:
        status["active"] = active
    status["conditions"] = conditions
    return {
        "metadata": {"name": name, "creationTimestamp": started},
        "spec": {"template": {"spec": {"containers": [
            {"name": "backup", "env": [{"name": "MODE", "value": "backup"},
                                       {"name": "BACKUP_KIND", "value": kind}]}]}}},
        "status": status,
    }


def _pod(job, *, created, message=None, reason="Error", state="state"):
    terminated = {"exitCode": 1 if reason != "Completed" else 0, "reason": reason}
    if message is not None:
        terminated["message"] = message if isinstance(message, str) else json.dumps(message)
    return {
        "metadata": {"name": f"{job}-{created[-3:-1]}", "creationTimestamp": created,
                     "labels": {"batch.kubernetes.io/job-name": job, "job-name": job}},
        "status": {"phase": "Failed" if reason != "Completed" else "Succeeded",
                   "containerStatuses": [{"name": "backup", state: {"terminated": terminated}}]},
    }


def _index(*timestamps):
    return {"schema_version": 1, "backups": [
        {"backup_id": f"daily/{ts}", "kind": "daily", "timestamp": ts} for ts in timestamps]}


CRONJOB = {"metadata": {"name": f"{REL}-backup"},
           "spec": {"schedule": "8 2 * * *", "suspend": False},
           "status": {"lastScheduleTime": "2026-10-17T18:08:00Z"}}


class TestSummarizeOdoo:
    def test_latest_success_reads_index_timestamp_not_completion_time(self):
        jobs = [_job("b-1", started="2026-10-16T18:08:00Z", complete="2026-10-16T18:12:30Z")]
        pods = [_pod("b-1", created="2026-10-16T18:08:01Z", reason="Completed",
                     message=_index("2026-10-16T18:08:05Z", "2026-10-15T18:08:04Z"))]
        out = bs.summarize_odoo([CRONJOB], jobs, pods)
        assert out["supported"] is True and out["source"] == "odoo-cronjob"
        assert out["last_status"] == "succeeded"
        assert out["last_success_at"] == "2026-10-16T18:08:05Z"
        assert out["last_success_source"] == "index"
        assert out["last_failure_at"] is None and out["last_failure_reason"] is None
        assert out["consecutive_failures"] == 0
        assert out["enabled"] is True and out["schedule"] == "8 2 * * *"

    def test_no_space_failure_after_success_is_reported_with_reason(self):
        """X-BK-006-1 的實測情境：備份卷滿 → BackoffLimitExceeded，chart 0.2.14 寫 reason。"""
        jobs = [
            _job("b-1", started="2026-10-15T18:08:00Z", complete="2026-10-15T18:12:00Z"),
            _job("b-2", started="2026-10-16T18:08:00Z", failed="2026-10-16T18:09:40Z"),
            _job("b-3", started="2026-10-17T18:08:00Z", failed="2026-10-17T18:09:40Z"),
        ]
        failure = {"schema_version": 1, "status": "failed", "mode": "backup", "kind": "daily",
                   "reason": "no_space", "stage": "dump", "exit_code": 1,
                   "failed_at": "2026-10-17T18:09:30Z",
                   "message": "pg_dump failed; see the preceding pg_dump error"}
        pods = [
            _pod("b-1", created="2026-10-15T18:08:01Z", reason="Completed",
                 message=_index("2026-10-15T18:08:03Z")),
            _pod("b-2", created="2026-10-16T18:09:01Z", message=failure),
            # 同一個 Job 的三次重試：取最新一顆的 message
            _pod("b-3", created="2026-10-17T18:08:01Z", message={**failure, "reason": "dump_failed"}),
            _pod("b-3", created="2026-10-17T18:09:20Z", message=failure),
        ]
        out = bs.summarize_odoo([CRONJOB], jobs, pods)
        assert out["last_status"] == "failed"
        assert out["last_failure_reason"] == "no_space"
        assert out["last_failure_at"] == "2026-10-17T18:09:40Z"
        assert out["last_failure_message"] == "pg_dump failed; see the preceding pg_dump error"
        assert out["last_success_at"] == "2026-10-15T18:08:03Z"
        assert out["consecutive_failures"] == 2
        assert [r["status"] for r in out["recent"]] == ["failed", "failed", "succeeded"]

    @pytest.mark.parametrize("pod_reason,job_reason,expected", [
        ("Error", "BackoffLimitExceeded", "job_failed"),
        ("OOMKilled", "BackoffLimitExceeded", "oom_killed"),
        ("Error", "DeadlineExceeded", "deadline_exceeded"),
    ])
    def test_old_chart_failure_without_message_falls_back_to_k8s_reason(self, pod_reason, job_reason, expected):
        jobs = [_job("b-1", started="2026-10-17T18:08:00Z", failed="2026-10-17T19:08:00Z",
                     failed_reason=job_reason)]
        pods = [_pod("b-1", created="2026-10-17T18:08:01Z", reason=pod_reason)]
        out = bs.summarize_odoo([CRONJOB], jobs, pods)
        assert out["last_status"] == "failed"
        assert out["last_failure_reason"] == expected
        assert out["last_failure_message"] is None

    def test_partial_upgrade_skip_is_not_a_success(self):
        jobs = [
            _job("b-1", started="2026-10-15T18:08:00Z", complete="2026-10-15T18:12:00Z"),
            _job("b-2", started="2026-10-16T18:08:00Z", complete="2026-10-16T18:08:10Z"),
        ]
        pods = [
            _pod("b-1", created="2026-10-15T18:08:01Z", reason="Completed",
                 message=_index("2026-10-15T18:08:03Z")),
            _pod("b-2", created="2026-10-16T18:08:01Z", reason="Completed",
                 message={"skipped_reason": "partial_upgrade"}),
        ]
        out = bs.summarize_odoo([CRONJOB], jobs, pods)
        assert out["last_status"] == "skipped"
        assert out["last_success_at"] == "2026-10-15T18:08:03Z"
        assert out["consecutive_failures"] == 0

    def test_running_job(self):
        jobs = [_job("b-1", started="2026-10-17T18:08:00Z", active=1)]
        out = bs.summarize_odoo([CRONJOB], jobs, [])
        assert out["running"] is True and out["last_status"] == "running"

    def test_jobs_gc_falls_back_to_cronjob_last_successful_time(self):
        cronjob = {**CRONJOB, "status": {**CRONJOB["status"], "lastSuccessfulTime": "2026-10-12T18:12:00Z"}}
        out = bs.summarize_odoo([cronjob], [], [])
        assert out["last_status"] == "none"
        assert out["last_success_at"] == "2026-10-12T18:12:00Z"
        assert out["last_success_source"] == "cronjob_status"

    def test_suspended_cronjob_reports_disabled(self):
        cronjob = {**CRONJOB, "spec": {**CRONJOB["spec"], "suspend": True}}
        assert bs.summarize_odoo([cronjob], [], [])["enabled"] is False

    def test_reason_codes_are_sanitized(self):
        jobs = [_job("b-1", started="2026-10-17T18:08:00Z", failed="2026-10-17T18:09:00Z")]
        pods = [_pod("b-1", created="2026-10-17T18:08:01Z",
                     message={"status": "failed", "reason": "<script>alert(1)</script>",
                              "message": "x" * 1000})]
        out = bs.summarize_odoo([CRONJOB], jobs, pods)
        assert out["last_failure_reason"] == "unknown"
        assert len(out["last_failure_message"]) == 300


class TestSummarizeDbAgent:
    def test_parse_only_known_keys(self):
        stdout = ("backup_last_ok_at\t2026-10-17T19:00:05Z\n"
                  "litellm_salt_key\tsecret-must-not-leak\n"
                  "backup_last_error\t\n")
        assert bs.parse_db_agent_settings(stdout) == {
            "backup_last_ok_at": "2026-10-17T19:00:05Z", "backup_last_error": ""}

    def test_ok(self):
        out = bs.summarize_db_agent({"backup_last_ok_at": "2026-10-17T19:00:05Z",
                                     "backup_last_ok_file": "backups/daily-x.dump",
                                     "backup_last_error": ""}, enabled=True)
        assert out["source"] == "litellm-db-agent"
        assert out["last_status"] == "succeeded"
        assert out["last_success_at"] == "2026-10-17T19:00:05Z"
        assert out["last_failure_reason"] is None and out["consecutive_failures"] == 0

    def test_no_space_newer_than_ok_is_failed(self):
        """AI-LLM-009-1：空間不足略過 daily dump。"""
        out = bs.summarize_db_agent({
            "backup_last_ok_at": "2026-10-15T19:00:05Z",
            "backup_last_error": "no_space", "backup_last_error_kind": "daily",
            "backup_last_error_detail": "~1024 KiB needed, 1184 KiB free, 1228 KiB reserved for the database",
            "backup_last_error_at": "2026-10-17T19:00:01Z"}, enabled=True)
        assert out["last_status"] == "failed"
        assert out["last_failure_reason"] == "no_space"
        assert out["last_failure_at"] == "2026-10-17T19:00:01Z"
        assert "KiB free" in out["last_failure_message"]
        assert out["last_success_at"] == "2026-10-15T19:00:05Z"

    def test_old_chart_without_keys_is_unknown_not_failed(self):
        out = bs.summarize_db_agent({}, enabled=True)
        assert out["last_status"] == "unknown" and out["status_recorded"] is False


def _k8s(items, exec_stdout=None, exec_error=None):
    k8s = MagicMock()

    def get_items(kinds, namespace, selector):
        return items.get((kinds, selector), [])
    k8s.get_items.side_effect = get_items
    if exec_error:
        k8s.exec_in_pod.side_effect = exec_error
    else:
        k8s.exec_in_pod.return_value = SimpleNamespace(stdout=exec_stdout or "")
    return k8s


ODOO_SEL = bs.ODOO_BACKUP_SELECTOR.format(name=REL)
DB_SEL = bs.DB_AGENT_POD_SELECTOR.format(name=REL)


def _db_pod(phase="Running", enabled="true"):
    return {"metadata": {"name": f"{REL}-postgresql-0"},
            "spec": {"containers": [{"name": "postgresql"},
                                    {"name": "db-agent", "env": [
                                        {"name": "WOOW_BACKUP_ENABLED", "value": enabled}]}]},
            "status": {"phase": phase}}


class TestReadBackupStatus:
    def test_odoo_selector_only_counts_backup_task(self):
        k8s = _k8s({("cronjobs", ODOO_SEL): [CRONJOB]})
        out = bs.read_backup_status(k8s, NS, REL)
        assert out["source"] == "odoo-cronjob"
        selectors = {call.args[2] for call in k8s.get_items.call_args_list}
        assert selectors == {ODOO_SEL}
        assert "woow.paas/task=backup" in ODOO_SEL  # housekeeping／restore 不算
        assert out["observed_at"].endswith("Z")
        k8s.exec_in_pod.assert_not_called()

    def test_litellm_reads_settings_in_db_agent_container(self):
        k8s = _k8s({("pods", DB_SEL): [_db_pod()]},
                   exec_stdout="backup_last_error\tno_space\nbackup_last_error_at\t2026-10-17T19:00:01Z\n")
        out = bs.read_backup_status(k8s, NS, REL)
        assert out["source"] == "litellm-db-agent" and out["last_status"] == "failed"
        args = k8s.exec_in_pod.call_args
        assert args.args[:3] == (NS, f"{REL}-postgresql-0", "db-agent")
        command = args.args[3]
        assert command[:2] == ["sh", "-c"] and command[3].startswith("SELECT key, value FROM woow_platform.settings")
        assert "litellm_salt_key" not in command[3]

    def test_litellm_backup_disabled(self):
        k8s = _k8s({("pods", DB_SEL): [_db_pod(enabled="false")]}, exec_stdout="")
        assert bs.read_backup_status(k8s, NS, REL)["enabled"] is False

    def test_litellm_settings_unreadable_is_unrecorded(self):
        k8s = _k8s({("pods", DB_SEL): [_db_pod()]},
                   exec_error=KubectlException("exec failed", "kubectl exec", 'relation "woow_platform.settings" does not exist'))
        out = bs.read_backup_status(k8s, NS, REL)
        assert out["status_recorded"] is False and out["unavailable_reason"] == "status_unreadable"
        assert out["last_status"] == "unknown"

    def test_litellm_database_not_running(self):
        k8s = _k8s({("pods", DB_SEL): [_db_pod(phase="Pending")]})
        out = bs.read_backup_status(k8s, NS, REL)
        assert out["unavailable_reason"] == "database_not_running"
        k8s.exec_in_pod.assert_not_called()

    def test_other_chart_unsupported(self):
        k8s = _k8s({("pods", DB_SEL): [{"metadata": {"name": "n8n-postgresql-0"},
                                         "spec": {"containers": [{"name": "postgresql"}]},
                                         "status": {"phase": "Running"}}]})
        out = bs.read_backup_status(k8s, NS, REL)
        assert out["supported"] is False
        k8s.exec_in_pod.assert_not_called()


@pytest.fixture
def client():
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


class TestBackupStatusEndpoint:
    URL = f"/api/releases/{NS}/{REL}/backup-status"

    @patch("src.api.releases.k8s_service")
    def test_ok(self, mock_k8s, client):
        mock_k8s.get_items.side_effect = lambda kinds, ns, sel: [CRONJOB] if kinds == "cronjobs" else []
        resp = client.get(self.URL)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["supported"] is True and body["source"] == "odoo-cronjob"
        assert body["last_status"] == "none"

    @patch("src.api.releases.k8s_service")
    def test_kubectl_failure_is_502(self, mock_k8s, client):
        mock_k8s.get_items.side_effect = KubectlException("boom", "kubectl get", "forbidden")
        assert client.get(self.URL).status_code == 502

    def test_foreign_namespace_403(self, client):
        assert client.get(f"/api/releases/kube-system/{REL}/backup-status").status_code == 403

    def test_requires_api_key(self):
        with patch("src.main.settings") as mock_settings:
            mock_settings.api_key = "test-api-key"
            mock_settings.namespace_prefix = "paas-ws-"
            resp = TestClient(app).get(self.URL)
        assert resp.status_code in (401, 403)

"""#1552：helm 寫入類作業的併發上限＋不下載 OpenAPI schema。

正式站 2026-10-07：三台服務在約 10 秒內各送一次「修改配置」，operator 同時跑三個 helm
（每個約 200MiB，大半是解析整份 /openapi/v2）⇒ 512Mi 容器 OOMKilled、請求全斷。沙盒量測與
重現見 PR 說明；這裡用假的 helm 子行程（sleep）驗證：

* 同時進行的 helm install／upgrade／rollback／uninstall 不超過 ``HELM_MAX_CONCURRENT_OPERATIONS``，
  超過的排隊、最後全部完成；
* 同一個請求的「預檢 dry-run＋真正 upgrade」只佔一個名額（可重入、不重複排隊）；
* 排隊逾時回 503 ``error_code=operator_busy``，而且一個 helm 都沒有跑；
* install 排隊時不卡住 event loop（/health 照常回應）；
* install／upgrade 帶 ``--disable-openapi-validation``（可用設定關掉）。
"""
import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.config import Settings, settings
from src.main import app
from src.services.helm import HelmService
from src.services.helm_limiter import (
    OPERATOR_BUSY_ERROR_CODE,
    HelmOperationBusy,
    helm_operation_slot,
    reset_helm_operation_limiter,
)
from src.services.upgrade_preflight import describe_helm_failure

NS = "paas-ws-test1552"


@pytest.fixture
def limiter_settings():
    """改併發上限／排隊上限，結束後還原並重建 semaphore。"""
    saved = (settings.helm_max_concurrent_operations, settings.helm_queue_timeout_seconds,
             settings.helm_disable_openapi_validation)

    def apply(limit=2, queue_timeout=30, disable_validation=True):
        settings.helm_max_concurrent_operations = limit
        settings.helm_queue_timeout_seconds = queue_timeout
        settings.helm_disable_openapi_validation = disable_validation
        reset_helm_operation_limiter()

    yield apply
    (settings.helm_max_concurrent_operations, settings.helm_queue_timeout_seconds,
     settings.helm_disable_openapi_validation) = saved
    reset_helm_operation_limiter()


class FakeHelm:
    """取代 ``HelmService._run_command``：install／upgrade 睡一下並記錄同時在跑幾個。"""

    def __init__(self, seconds=0.3):
        self.seconds = seconds
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0
        self.calls = []

    def __call__(self, service, args, input_data=None, capture_output=True, timeout=None):
        verb = args[0]
        with self.lock:
            self.calls.append(list(args))
        if verb in ("install", "upgrade", "rollback", "uninstall"):
            with self.lock:
                self.running += 1
                self.peak = max(self.peak, self.running)
            try:
                time.sleep(self.seconds)
            finally:
                with self.lock:
                    self.running -= 1
        if verb in ("install", "upgrade"):
            name = args[1]
            out = json.dumps({
                "name": name, "namespace": NS, "manifest": "",
                "version": 2, "info": {"status": "deployed", "last_deployed": "x"},
                "chart": {"metadata": {"name": "c", "version": "0.1.0", "appVersion": "1"}},
                "config": {},
            })
        else:
            out = ""
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")


@pytest.fixture
def fake_helm():
    fake = FakeHelm()

    def run(self, args, input_data=None, capture_output=True, timeout=None):
        return fake(self, args, input_data, capture_output, timeout)

    with patch.object(HelmService, "_run_command", run):
        yield fake


@pytest.fixture
def client():
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


def _patch_body(i):
    return {"chart": "oci://example.invalid/c", "values": {"marker": i},
            "reset_values": True, "reuse_values": False}


# --------------------------------------------------------------------------- settings


def test_settings_defaults_and_env(monkeypatch):
    s = Settings()
    assert s.helm_disable_openapi_validation is True
    assert s.helm_max_concurrent_operations == 3
    assert s.helm_queue_timeout_seconds == 45
    monkeypatch.setenv("HELM_MAX_CONCURRENT_OPERATIONS", "5")
    monkeypatch.setenv("HELM_QUEUE_TIMEOUT_SECONDS", "20")
    monkeypatch.setenv("HELM_DISABLE_OPENAPI_VALIDATION", "false")
    s = Settings()
    assert (s.helm_max_concurrent_operations, s.helm_queue_timeout_seconds,
            s.helm_disable_openapi_validation) == (5, 20, False)


@pytest.mark.parametrize("env,value", [
    ("HELM_MAX_CONCURRENT_OPERATIONS", "0"),
    ("HELM_QUEUE_TIMEOUT_SECONDS", "0"),
])
def test_settings_reject_non_positive(monkeypatch, env, value):
    monkeypatch.setenv(env, value)
    with pytest.raises(Exception):
        Settings()


# --------------------------------------------------------------------------- helm flags


@pytest.mark.parametrize("disable,expected", [(True, True), (False, False)])
def test_install_upgrade_skip_openapi_download(limiter_settings, fake_helm, disable, expected):
    limiter_settings(disable_validation=disable)
    svc = HelmService()
    svc.install(NS, "r1", "oci://example.invalid/c")
    svc.upgrade(NS, "r1", "oci://example.invalid/c", dry_run=True)
    svc.upgrade(NS, "r1", "oci://example.invalid/c")
    verbs = [c for c in fake_helm.calls if c[0] in ("install", "upgrade")]
    assert len(verbs) == 3
    for call in verbs:
        assert ("--disable-openapi-validation" in call) is expected


def test_rollback_and_uninstall_take_a_slot(limiter_settings, fake_helm):
    limiter_settings(limit=1, queue_timeout=1)
    svc = HelmService()
    with helm_operation_slot("held-by-test"):
        holder_done = threading.Event()
        errors = []

        def other():
            try:
                svc.rollback(NS, "r1", 1)
            except HelmOperationBusy as e:
                errors.append(e)
            holder_done.set()

        t = threading.Thread(target=other)
        t.start()
        holder_done.wait(5)
        t.join()
    assert errors and errors[0].detail()["error_code"] == OPERATOR_BUSY_ERROR_CODE
    assert not [c for c in fake_helm.calls if c[0] == "rollback"]
    # 名額空出來之後兩個都能做。
    svc.rollback(NS, "r1", 1)
    svc.uninstall(NS, "r1")
    assert [c[0] for c in fake_helm.calls] == ["rollback", "uninstall"]


# --------------------------------------------------------------------------- limiter


def test_slot_bounds_concurrency_and_everyone_finishes(limiter_settings):
    limiter_settings(limit=2, queue_timeout=30)
    lock = threading.Lock()
    state = {"now": 0, "peak": 0, "done": 0}

    def work(_):
        with helm_operation_slot("upgrade"):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.15)
            with lock:
                state["now"] -= 1
                state["done"] += 1

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(work, range(8)))
    assert state["peak"] == 2
    assert state["done"] == 8


def test_slot_is_reentrant_within_a_thread(limiter_settings):
    limiter_settings(limit=1, queue_timeout=1)
    with helm_operation_slot("upgrade"):
        with helm_operation_slot("upgrade --dry-run"):
            with helm_operation_slot("upgrade"):
                pass
    # 出來之後名額有還回去。
    with helm_operation_slot("upgrade"):
        pass


def test_queue_timeout_raises_busy_with_structured_detail(limiter_settings):
    limiter_settings(limit=1, queue_timeout=1)
    caught = []
    with helm_operation_slot("upgrade"):
        def other():
            try:
                with helm_operation_slot("install"):
                    pass
            except HelmOperationBusy as e:
                caught.append(e)

        t = threading.Thread(target=other)
        started = time.monotonic()
        t.start()
        t.join(5)
        assert time.monotonic() - started >= 0.9
    assert len(caught) == 1
    detail = caught[0].detail()
    assert detail["error_code"] == OPERATOR_BUSY_ERROR_CODE
    assert detail["retryable"] is True
    assert "沒有套用" in detail["message"]


# --------------------------------------------------------------------------- endpoints


def test_concurrent_upgrades_are_queued_not_parallel(limiter_settings, fake_helm, client):
    """6 個併發 PATCH（平台 reconfigure 的形狀）：helm 同時最多 2 個、6 個全部 200。"""
    limiter_settings(limit=2, queue_timeout=30)

    def send(i):
        return client.patch(f"/api/releases/{NS}/svc-{i}", json=_patch_body(i))

    with ThreadPoolExecutor(max_workers=6) as ex:
        responses = list(ex.map(send, range(6)))
    assert [r.status_code for r in responses] == [200] * 6
    assert fake_helm.peak == 2  # 真的併發了，但沒超過上限
    upgrades = [c for c in fake_helm.calls if c[0] == "upgrade"]
    # 每個請求一次預檢 dry-run＋一次真正 upgrade。
    assert len(upgrades) == 12
    assert sum("--dry-run" in c for c in upgrades) == 6


def test_preflight_and_upgrade_share_one_slot(limiter_settings, fake_helm, client):
    """名額只有 1 個時，一個請求的 dry-run＋upgrade 不會自己卡死自己。"""
    limiter_settings(limit=1, queue_timeout=2)
    r = client.patch(f"/api/releases/{NS}/svc-1", json=_patch_body(1))
    assert r.status_code == 200
    assert [("--dry-run" in c) for c in fake_helm.calls if c[0] == "upgrade"] == [True, False]


def test_upgrade_busy_returns_503_and_runs_nothing(limiter_settings, fake_helm, client):
    limiter_settings(limit=1, queue_timeout=1)
    with helm_operation_slot("held-by-test"):
        result = {}
        t = threading.Thread(target=lambda: result.setdefault(
            "r", client.patch(f"/api/releases/{NS}/svc-1", json=_patch_body(1))))
        t.start()
        t.join(10)
    r = result["r"]
    assert r.status_code == 503
    assert r.headers.get("retry-after") == "15"
    body = r.json()
    assert body["detail"]["error_code"] == OPERATOR_BUSY_ERROR_CODE
    assert body["error_type"] == "HelmOperationBusy"
    assert not [c for c in fake_helm.calls if c[0] == "upgrade"]


def test_queued_install_does_not_block_event_loop(limiter_settings, fake_helm, client):
    """install 在排隊時，/health 仍在 1 秒內回應（舊版 install 直接跑在 event loop 上）。"""
    limiter_settings(limit=1, queue_timeout=10)
    install_result = {}
    with patch("src.main.HelmService") as health_helm:
        health_helm.return_value.get_version.return_value = "v3.13.3"
        with helm_operation_slot("held-by-test"):
            t = threading.Thread(target=lambda: install_result.setdefault("r", client.post(
                "/api/releases",
                json={"namespace": NS, "name": "svc-new", "chart": "oci://example.invalid/c"})))
            t.start()
            time.sleep(0.3)  # 讓 install 進到排隊
            started = time.monotonic()
            health = client.get("/health")
            assert health.status_code == 200
            assert time.monotonic() - started < 1.0
            assert t.is_alive(), "install should still be waiting for the slot"
        t.join(10)
    assert install_result["r"].status_code == 201
    assert [c[0] for c in fake_helm.calls if c[0] == "install"] == ["install"]


def test_uninstall_busy_returns_503(limiter_settings, fake_helm, client):
    limiter_settings(limit=1, queue_timeout=1)
    with helm_operation_slot("held-by-test"):
        result = {}
        t = threading.Thread(target=lambda: result.setdefault(
            "r", client.delete(f"/api/releases/{NS}/svc-1")))
        t.start()
        t.join(10)
    assert result["r"].status_code == 503
    assert not [c for c in fake_helm.calls if c[0] == "uninstall"]


# --------------------------------------------------------------------------- messages


def test_interrupted_operation_has_chinese_hint():
    stderr = "Error: UPGRADE FAILED: another operation (install/upgrade/rollback) is in progress"
    msg = describe_helm_failure(stderr)
    assert "被中斷" in msg and "回復版本" in msg


def test_reset_config_waits_only_its_own_short_budget(limiter_settings, fake_helm, client):
    """reset 的排隊上限是 RESET_QUEUE_TIMEOUT_SECONDS（算進 #461 階梯），不是一般的 45s。"""
    from src.api import releases as rel

    limiter_settings(limit=1, queue_timeout=600)
    with patch.object(rel, "RESET_QUEUE_TIMEOUT_SECONDS", 1):
        with helm_operation_slot("held-by-test"):
            result = {}
            started = time.monotonic()
            t = threading.Thread(target=lambda: result.setdefault("r", client.post(
                f"/api/releases/{NS}/svc-1/reset-config",
                json={"chart": "/nonexistent-local-chart", "admin_password_carrier": "helm_secret",
                      "sensitive_values": {"config": {"sensitive": {"admin_password": "x" * 16}}}})))
            t.start()
            t.join(20)
            elapsed = time.monotonic() - started
    assert result["r"].status_code == 503
    assert result["r"].json()["detail"]["error_code"] == OPERATOR_BUSY_ERROR_CODE
    assert elapsed < 10
    assert not fake_helm.calls

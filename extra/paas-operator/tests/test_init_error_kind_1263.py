"""#1263：init 失敗分類（error_step／error_kind）與 kubectl timeout 的可判別屬性。

分類依「遠端副作用是否可能已發生」（#1257 Q11）：readiness 探測唯讀 ⇒ not_executed；
收到明確錯誤回應 ⇒ failed；n8n 已拿到 api_key 但 Secret 沒寫成 ⇒ partial；其餘
（timeout、exec 失敗、輸出不可解析、非預期例外）⇒ unknown。
"""
import subprocess
from unittest.mock import patch

import pytest

from src.services.helm import KubectlException, KubernetesService


@patch("subprocess.run")
def test_kubectl_timeout_is_distinguishable_without_parsing_message(mock_run):
    mock_run.side_effect = subprocess.TimeoutExpired(cmd=["kubectl", "exec"], timeout=5)

    with pytest.raises(KubectlException) as excinfo:
        KubernetesService()._run_command(["exec", "p"], timeout=5)

    assert excinfo.value.timed_out is True


@patch("subprocess.run")
def test_kubectl_non_zero_exit_is_not_a_timeout(mock_run):
    mock_run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")

    with pytest.raises(KubectlException) as excinfo:
        KubernetesService()._run_command(["get", "pods"])

    assert excinfo.value.timed_out is False


@patch("subprocess.run")
def test_exec_in_pod_rewrap_keeps_timeout_flag(mock_run, monkeypatch):
    """exec_in_pod 會重包例外；重包不可把 timeout 資訊丟掉。"""
    monkeypatch.setattr("src.services.helm.validate_namespace", lambda ns: None)
    mock_run.side_effect = subprocess.TimeoutExpired(cmd=["kubectl", "exec"], timeout=5)

    with pytest.raises(KubectlException) as excinfo:
        KubernetesService().exec_in_pod("paas-ws-x", "p", "c", ["true"], timeout=5)

    assert excinfo.value.timed_out is True


def test_kubectl_exception_legacy_constructor_defaults_to_not_timed_out():
    """既有呼叫端（含測試）以三個位置參數建構，不可因新屬性而壞。"""
    assert KubectlException("m", "cmd", "err").timed_out is False

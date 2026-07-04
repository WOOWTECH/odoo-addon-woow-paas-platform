"""L1: PAAS_ENV operator config field (deploy-env primitive for ns env label)."""
import pytest
from pydantic import ValidationError

from src.config import Settings, _env_hint_from_prefix, assert_env_guard
from src.services.helm import validate_label_value


def test_paas_env_defaults_empty(monkeypatch):
    """未設 PAAS_ENV → config load 得空字串（config 層不 crash）。

    啟動層的 F6 fail-fast 另由 lifespan 的 assert_env_guard 負責（見下方 guard 測試）。
    """
    monkeypatch.delenv("PAAS_ENV", raising=False)
    assert Settings(_env_file=None).paas_env == ""


def test_paas_env_reads_env_var(monkeypatch):
    """注入鏈：PAAS_ENV env var → settings.paas_env（驗鏈非 mock 物件）。"""
    monkeypatch.setenv("PAAS_ENV", "prod")
    assert Settings(_env_file=None).paas_env == "prod"
    monkeypatch.setenv("PAAS_ENV", "stg")
    assert Settings(_env_file=None).paas_env == "stg"


@pytest.mark.parametrize("bad", ["staging", "production", "Prod", "PROD", "paas-ws-stg-", "stg "])
def test_paas_env_rejects_invalid_value_fail_closed(monkeypatch, bad):
    """非白名單值（含大寫變體 / prefix 字面）→ config load 即 ValidationError（fail-closed）。

    寧可 operator 啟動 fail-loud，也不要靜默貼錯 env label。
    """
    monkeypatch.setenv("PAAS_ENV", bad)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_paas_env_whitelist_values_are_legal_k8s_label_values():
    """L1↔L2 契約：白名單每個值都是合法 k8s label value（供 ns env label 直接用）。

    防「白名單值帶尾 dash 之類」→ L2 atomic 拒整批（operator #208 類）。
    """
    for v in ("prod", "stg"):
        validate_label_value("paas.woowtech.io/env", v)  # 不得 raise


# --- F6 (#279) startup env guard ---------------------------------------------
# 防跨環境洩漏：workspace int id 非叢集唯一 + prod/stg 共用 woow-k3s → 少了 / 貼錯
# paas.woowtech.io/env 的 #279 label 會讓 stg agent 觸達同號 prod CloudService。


@pytest.mark.parametrize("prefix,expected", [
    ("paas-ws-", "prod"),          # prod 部署實際 prefix
    ("paas-ws-stg-", "stg"),       # stg 部署實際 prefix
    ("paas-ws-test-op-", "dev"),   # integration-test prefix
    ("paas-ws-dev-foo-", "dev"),
    ("something-else-", None),     # 無法判斷 → None（correctness 不誤擋）
])
def test_env_hint_from_prefix(prefix, expected):
    """NAMESPACE_PREFIX 反推的獨立 env hint（僅供 guard 交叉核對、非 stamp 來源）。"""
    assert _env_hint_from_prefix(prefix) == expected


def test_env_guard_real_prod_ok():
    """真 prod（PAAS_ENV=prod + paas-ws-）通過。"""
    assert_env_guard("prod", "paas-ws-", allow_unset=False)  # 不得 raise


def test_env_guard_real_stg_ok():
    """真 stg（PAAS_ENV=stg + paas-ws-stg-）通過。"""
    assert_env_guard("stg", "paas-ws-stg-", allow_unset=False)  # 不得 raise


def test_env_guard_empty_env_no_optout_raises():
    """空 PAAS_ENV + 無 opt-out → fail fast（core F6，擋 cross-env fail-open）。"""
    with pytest.raises(RuntimeError, match="PAAS_ENV is empty"):
        assert_env_guard("", "paas-ws-", allow_unset=False)


def test_env_guard_empty_env_optout_dev_ok():
    """空 PAAS_ENV + opt-out + 非真-env prefix（local/dev）→ 放行。"""
    assert_env_guard("", "paas-ws-", allow_unset=True)          # bare（local dev）
    assert_env_guard("", "paas-ws-test-op-", allow_unset=True)  # integration test


def test_env_guard_empty_env_optout_real_prefix_raises():
    """空 PAAS_ENV + opt-out 但 prefix 具真-env 名（stg）→ 拒絕（雙 misconfig 不放行）。"""
    with pytest.raises(RuntimeError, match="names a real environment"):
        assert_env_guard("", "paas-ws-stg-", allow_unset=True)


def test_env_guard_swap_prod_env_on_stg_prefix_raises():
    """swap A：PAAS_ENV=prod 但 paas-ws-stg- → env/prefix 矛盾 → fail fast。"""
    with pytest.raises(RuntimeError, match="contradicts NAMESPACE_PREFIX"):
        assert_env_guard("prod", "paas-ws-stg-", allow_unset=False)


def test_env_guard_swap_stg_env_on_bare_prefix_raises():
    """swap B：PAAS_ENV=stg 但 bare paas-ws-（prod 慣例）→ 矛盾 → fail fast。"""
    with pytest.raises(RuntimeError, match="contradicts NAMESPACE_PREFIX"):
        assert_env_guard("stg", "paas-ws-", allow_unset=False)


def test_env_guard_dev_prefix_with_env_set_ok():
    """dev-shaped prefix（test/dev）+ 任一 PAAS_ENV → hint=dev、correctness 略過（不誤擋 dev 實驗）。"""
    assert_env_guard("stg", "paas-ws-test-op-", allow_unset=False)   # 不得 raise
    assert_env_guard("prod", "paas-ws-dev-foo-", allow_unset=False)  # 不得 raise


def test_allow_unset_paas_env_field_default_false(monkeypatch):
    """allow_unset_paas_env 預設 False（正式部署不得預設繞過 guard）。"""
    monkeypatch.delenv("ALLOW_UNSET_PAAS_ENV", raising=False)
    assert Settings(_env_file=None).allow_unset_paas_env is False

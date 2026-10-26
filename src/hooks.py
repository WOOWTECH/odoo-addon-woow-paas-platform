import logging
import os

_logger = logging.getLogger(__name__)

# pip package name -> import name (only for non-trivial mappings)
_IMPORT_NAME_OVERRIDES = {
    'pyyaml': 'yaml',
    'pillow': 'PIL',
    'scikit-learn': 'sklearn',
    'python-dateutil': 'dateutil',
    'pycryptodomex': 'Cryptodome',
}


def _parse_requirements(filepath=None):
    """
    Parse requirements.txt and return a list of (import_name, pip_spec) tuples.

    Skips comments and blank lines. Converts pip package names to Python
    import names (e.g. 'langchain-openai>=0.3.0' -> ('langchain_openai', 'langchain-openai>=0.3.0')).
    """
    if filepath is None:
        filepath = os.path.join(os.path.dirname(__file__), 'requirements.txt')

    if not os.path.isfile(filepath):
        _logger.warning("requirements.txt not found at %s, skipping dependency check", filepath)
        return []

    result = []
    with open(filepath, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue

            # Extract bare package name (before any version specifier)
            pkg_name = line
            for sep in ('>=', '<=', '==', '!=', '~=', '<', '>'):
                pkg_name = pkg_name.split(sep)[0]
            pkg_name = pkg_name.strip()

            # Determine import name
            pkg_lower = pkg_name.lower()
            if pkg_lower in _IMPORT_NAME_OVERRIDES:
                import_name = _IMPORT_NAME_OVERRIDES[pkg_lower]
            else:
                import_name = pkg_name.replace('-', '_')

            result.append((import_name, line))

    return result


def _check_python_dependencies():
    """
    Check and auto-install missing Python dependencies from requirements.txt.
    """
    import subprocess
    import sys

    requirements = _parse_requirements()
    if not requirements:
        return

    _logger.info("Checking Python dependencies from requirements.txt (%d packages)", len(requirements))

    missing_packages = []
    for import_name, pip_spec in requirements:
        try:
            __import__(import_name)
            _logger.info("  %s is installed", import_name)
        except ImportError:
            _logger.warning("  Missing: %s (%s)", import_name, pip_spec)
            missing_packages.append(pip_spec)

    if not missing_packages:
        _logger.info("All Python dependencies are satisfied")
        return

    _logger.info("Auto-installing %d missing packages...", len(missing_packages))

    for pip_spec in missing_packages:
        try:
            _logger.info("Installing %s...", pip_spec)
            subprocess.check_call([
                sys.executable, '-m', 'pip', 'install',
                '--break-system-packages', pip_spec
            ])
            _logger.info("  Installed %s", pip_spec)
        except subprocess.CalledProcessError:
            # Fallback: try without --break-system-packages
            try:
                subprocess.check_call([
                    sys.executable, '-m', 'pip', 'install', pip_spec
                ])
                _logger.info("  Installed %s (fallback)", pip_spec)
            except subprocess.CalledProcessError:
                raise ImportError(
                    f"Failed to install {pip_spec}. "
                    f"Please install manually: pip install --break-system-packages {pip_spec}"
                )

    # Verify all packages are importable
    for import_name, pip_spec in requirements:
        try:
            __import__(import_name)
        except ImportError:
            raise ImportError(
                f"Package {import_name} ({pip_spec}) was installed but cannot be imported. "
                "Please restart Odoo."
            )


def pre_init_hook(env):
    """
    Pre-init hook: auto-install Python dependencies from requirements.txt
    before module installation.
    """
    _check_python_dependencies()


_CONFIG_PARAMETERS = [
    'woow_paas_platform.custom_domains_enabled',
    'woow_paas_platform.shc_durable_provision_prototype_enabled',
    'woow_paas_platform.shc_durable_provision_reconcile_enabled',
    'woow_paas_platform.shc_durable_provision_retention_days',
    'woow_paas_platform.operator_url',
    'woow_paas_platform.operator_api_key',
    'woow_paas_platform.paas_domain',
    'woow_paas_platform.default_ai_config_id',
    'woow_paas_platform.default_ai_assistant_id',
    'woow_paas_platform.default_agent_template_id',
    'woow_paas_platform.default_enable_agent_runtime',
    'woow_paas_platform.namespace_prefix',
    'woow_paas_platform.deploy_env',
    'woow_paas_platform.grace_period_days',
    'woow_paas_platform.cleanup_after_months',
    'woow_paas_platform.storage_class_name',
    'woow_paas_platform.registry_secret_name',
    'woow_paas_platform.apiserver_cidrs',
    'woow_paas_platform.topup_payment_enabled',
    'woow_paas_platform.portal_project_settings_editable',
    'woow_paas_platform.show_projects',
    'woow_paas_platform.max_consecutive_payment_failures',
    # #778 配套 3：計費靜默告警的門檻天數（'0' 停用；缺值走模組內建預設）
    'woow_paas_platform.billing_silence_alert_days',
    # #864 PR-C：manual 發票老化告警的門檻天數（正整數才生效；'0'／負值／非法值／
    # 缺值一律 fail-safe 回 models/account_move_invoice_status.py 的內建 7 天。
    # ⛔ 刻意沒有 disable 開關——與上面 #778 那條的 '0' 停用語意**不同**）
    'woow_paas_platform.manual_invoice_aging_days',
    # #864 PR-E：double-charge 冷卻閘窗口（小時）。正整數才生效；'0'／負值／
    # 非法值／缺值一律 fail-safe 回 models/sh_subscription_fix.py 的內建 24
    # 小時（Eugene 裁決 2026-09-01，comment 11215）。⛔ 刻意沒有 disable 開關
    # ——同 manual_invoice_aging_days 的理由：這是防重複扣款的安全機制。
    'woow_paas_platform.double_charge_cooldown_hours',
    # #864 PR-F：QueryTrade auto-recredit kill-switch 與 independent-cron
    # heartbeat。兩者是常數＋get_param 寫法，必須在卸載時顯式清理。
    'woow_paas_platform.auto_recredit_enabled',
    'woow_paas_platform.auto_recredit_heartbeat_at',
    'woow_paas_platform.sa_default_service_url',
    'woow_paas_platform.agent_idle_reap_seconds',
    'woow_paas_platform.agent_gc_seconds',
    # #489：轉移狀態的 age timeout 門檻（缺值時走 cloud_service.py 的內建預設）
    'woow_paas_platform.deploy_timeout_minutes',
    'woow_paas_platform.vm_deploy_timeout_minutes',
    # #648 (b)：Public URL 的 DNS 落地閘（缺值時走 cloud_service.py 的內建預設）。
    # `dns_gate_enabled` 是 prod kill switch——新守線誤判時要有開關能立刻停損。
    # ⚠ 這兩個 key 走「模組常數 ＋ get_param」而不是 res.config.settings 的
    # `config_parameter=` 欄位（同上面 #489 兩條的寫法），而 repo-lint 的
    # `check_config_parameter_uninstall` **只掃 `config_parameter=` 字面 kwarg**
    # ⇒ 漏登記時那條 gate 不會紅。補在
    # `test_public_url_dns_gate_648.py::TestNewConfigParametersAreRegisteredForUninstall`。
    'woow_paas_platform.dns_grace_seconds',
    'woow_paas_platform.dns_gate_enabled',
    # #1647：升級前 compute 配額預檢的 prod kill switch（未設＝開；cloud_service.py 的
    # `_COMPUTE_QUOTA_PRECHECK_PARAM`）。「模組常數＋get_param()」寫法，lint 掃不到 ⇒ 補在
    # `test_compute_quota_preflight_1647.py::TestComputeQuotaPrecheckSwitch1647`。
    'woow_paas_platform.compute_quota_precheck_enabled',
    # #1257 Q37：post-deploy init 的 unknown／partial 額度與等待時限（「模組常數＋
    # get_param()」寫法，lint 掃不到 ⇒ 補在
    # `test_init_execution_control_1257.py::TestInitConfigParametersRegisteredForUninstall`）。
    'woow_paas_platform.init_unknown_limit',
    'woow_paas_platform.init_wait_timeout_minutes',
    # #1416：部署逾時後背景自癒的重查時間窗（小時；「模組常數＋get_param()」寫法，
    # lint 掃不到 ⇒ 補在 `test_deploy_timeout_recovery_1416.py::
    # TestRecoveryConfigParameterRegisteredForUninstall`）。
    'woow_paas_platform.timeout_recovery_window_hours',
    # #501：限量商品的供給閘門（缺值時走 cloud_app_template.py 的內建預設）。
    # key 由 slug 推導，新增限量商品時這裡要跟著補兩個
    # （守線＝repo-lint 的 config-parameter-uninstall）。
    'woow_paas_platform.global_instance_cap.haos',
    'woow_paas_platform.sales_paused.haos',
    # #1415：叢集儲存額度告警的「最大可放單顆 volume」門檻（GiB；'0' 停用該項；缺值走
    # platform_monitor.py 的內建 80）。「模組常數＋get_param()」寫法 ⇒ 補在
    # `test_cluster_storage_capacity_1415.py::TestClusterStorageParamRegisteredForUninstall`。
    'woow_paas_platform.cluster_storage_alert_min_volume_gib',
    # #1453：「說明與支援」網址（缺值走 controllers/paas.py 的 DEFAULT_HELP_URL）。
    # 「模組常數＋get_param()」寫法 ⇒ 補在
    # `test_platform_ux_r8.py::TestHelpUrl.test_registered_for_uninstall`。
    'woow_paas_platform.help_url',
    # #1540：已刪除服務磁碟保留天數的平台端估計（缺值走 models/data_retrieval.py 的內建 90）。
    # 「模組常數＋get_param()」寫法 ⇒ 補在
    # `test_data_retrieval_1540.py::TestDataRetrievalConfigParameter`。
    'woow_paas_platform.data_retention_days',
    # NB: the agent namespace secret moved to the AGENT_RUNTIME_NAMESPACE_SECRET
    # env (F-PC-3 fail-closed) and is no longer an ir.config_parameter.
    # #778：上游 sh_subscription_wallet 的 SSOT 開關——平台 pin '0'（opt-out）。
    # key 屬上游 namespace 但由平台設值；平台卸載時清掉＝上游回復自身預設
    # （未設視同啟用 SSOT）。
    'sh_subscription_wallet.billing_ssot',
    # ECPay 站內付 2.0 V4 上線（2026-10-15）：儲值發票開票模式、手動儲值是否帶
    # OrderResultURL 的 kill switch；以及 #1745 新增但當時漏登記的自動儲值下限與
    # migration 240 的完成標記。「模組常數＋get_param()」寫法 ⇒ 補在
    # `test_ecpg_v4_golive.py::TestEcpgV4ParamsRegisteredForUninstall`。
    'woow_paas_platform.topup_invoice_issue_mode',
    'woow_paas_platform.ecpg_topup_order_result_url_enabled',
    'woow_paas_platform.auto_topup_min_amount',
    'woow_paas_platform.migration_18_0_1_0_240_topup_plan_50_retired',
    # LLM-01b：AI 模型分頁（DESIGN §2.11）。全部是「模組常數＋get_param()」寫法、不 seed
    # 預設值，lint 掃不到 ⇒ 補在 `test_llm_tracer.py::TestLlmConfigRegistered`
    # （它比對的是 models/llm_common.py 的 LLM_CONFIG_PARAMETERS）。
    'woow_paas_platform.show_llm',
    'woow_paas_platform.llm_orders_enabled',
    'woow_paas_platform.llm_pilot_partner_ids',
    'woow_paas_platform.llm_pricing_profile',
    'woow_paas_platform.newapi_proxy_url',
    'woow_paas_platform.newapi_username_prefix',
    'woow_paas_platform.llm_synthetic_ip_block',
    'woow_paas_platform.llm_canary_partner_id',
    # LLM-02b：告警出口（N13）。
    'woow_paas_platform.llm_alert_email',
    'woow_paas_platform.llm_alert_webhook_url',
    # LLM-02：給使用者的 API base URL（DESIGN §2.11，只收白名單值）。
    'woow_paas_platform.llm_public_base_url',
    # LLM-08：unknown credit verifier 的受信任 new-api service-admin id。
    'woow_paas_platform.llm_service_admin_id',
]


def _ensure_default_agent_template(env):
    """Point the platform-default agent template at the seeded system template
    on first install, WITHOUT clobbering an admin's existing choice.

    Idempotent: only sets the config_parameter when it is currently empty, so
    re-running -u (post_init fires on every update) never overrides a value the
    admin picked in Settings. The seeded template is tenant-neutral (public,
    published, unbound, no user-scope skills/mcp) so broadcasting it as the
    cluster-wide default is safe.
    """
    ICP = env['ir.config_parameter'].sudo()
    key = 'woow_paas_platform.default_agent_template_id'
    if ICP.get_param(key):
        return  # admin already chose (or a prior install set it) — don't clobber
    template = env.ref(
        'woow_paas_platform.agent_template_default', raise_if_not_found=False,
    )
    if template:
        ICP.set_param(key, str(template.id))
        _logger.info(
            "Auto-pointed platform default agent template -> %s (id=%s)",
            template.name, template.id,
        )


def _ensure_ha_client_sa_scopes(env):
    """Grant the HA public client the Security Access scopes (fresh-install
    path). oauth_clients.xml is noupdate=1 so the seed XML scope change does
    not reach existing databases; the idempotent merge lives on the
    oauth_client model and is also wired into data/oauth_scope_updates.xml so
    the upgrade (-u) path is covered too (G-H3: missing client scopes break
    HA token refresh)."""
    env['woow_paas_platform.oauth_client']._ensure_ha_smart_home_sa_scopes()


def _ensure_ha_client_redirect_mode(env):
    """Migrate the HA public client to host-flexible ha_callback redirect
    matching (fresh-install path). Upgrade path is covered by
    data/oauth_redirect_mode_update.xml. oauth_clients.xml is noupdate=1 so the
    seed change to redirect_uri_match_mode never reaches existing DBs; the
    idempotent healer lives on the model. See seed-noupdate-sticky pitfall."""
    env['woow_paas_platform.oauth_client']._ensure_ha_smart_home_redirect_mode()


def post_init_hook(env):
    """Auto-point the platform default agent template, ensure HA SA-client
    scopes / redirect mode / device grant, then ensure the subscription
    journal is configured on every company.

    The subscription-journal auto-config lives on
    ``res.company._ensure_subscription_journal`` (idempotent) and is wired into
    both this install path AND every ``-u`` via
    ``data/subscription_journal_config.xml`` — post_init alone is install-only,
    which would leave ``sh_journal_id`` permanently NULL on an environment
    whose sale journal lands after this module installs (silent auto-invoice
    failure)."""
    _ensure_default_agent_template(env)
    _ensure_ha_client_sa_scopes(env)
    _ensure_ha_client_redirect_mode(env)
    env['woow_paas_platform.oauth_client']._ensure_ha_smart_home_device_grant()
    env['woow_paas_platform.oauth_client']._ensure_ha_app_provision_scope()
    env['res.company']._ensure_subscription_journal()
    # #778：SSOT 對撞防護（fresh-install 路徑；-u 路徑由
    # data/billing_ssot_guard.xml 的 <function> 覆蓋）。平台在 module graph
    # 拓撲序上晚於 sh_subscription_wallet，此處恆能把 wallet 的
    # post_init_hook 關掉的 base auto-invoice cron 開回、並 pin 上游
    # billing_ssot param 為 opt-out。
    Subscription = env['sh.subscription.subscription']
    Subscription._ensure_billing_ssot_optout()
    Subscription._ensure_base_auto_invoice_cron_active()


def uninstall_hook(env):
    """Remove ir.config_parameter records created by this module."""
    env['ir.config_parameter'].search([
        ('key', 'in', _CONFIG_PARAMETERS),
    ]).unlink()

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
    'woow_paas_platform.topup_payment_enabled',
    'woow_paas_platform.portal_project_settings_editable',
    'woow_paas_platform.show_projects',
    'woow_paas_platform.max_consecutive_payment_failures',
    'woow_paas_platform.sa_default_service_url',
    'woow_paas_platform.agent_idle_reap_seconds',
    'woow_paas_platform.agent_gc_seconds',
    # NB: the agent namespace secret moved to the AGENT_RUNTIME_NAMESPACE_SECRET
    # env (F-PC-3 fail-closed) and is no longer an ir.config_parameter.
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
    env['res.company']._ensure_subscription_journal()


def uninstall_hook(env):
    """Remove ir.config_parameter records created by this module."""
    env['ir.config_parameter'].search([
        ('key', 'in', _CONFIG_PARAMETERS),
    ]).unlink()

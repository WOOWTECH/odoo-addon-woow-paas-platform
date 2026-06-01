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
    'woow_paas_platform.grace_period_days',
    'woow_paas_platform.cleanup_after_months',
    'woow_paas_platform.storage_class_name',
    'woow_paas_platform.registry_secret_name',
    'woow_paas_platform.topup_payment_enabled',
    'woow_paas_platform.max_consecutive_payment_failures',
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


def post_init_hook(env):
    """Auto-point the platform default agent template, then ensure the
    subscription journal is configured on the main company."""
    _ensure_default_agent_template(env)
    company = env.ref('base.main_company', raise_if_not_found=False)
    if not company:
        return
    if company.sh_journal_id:
        _logger.info("Subscription journal already configured: %s", company.sh_journal_id.name)
        return
    sale_journal = env['account.journal'].search([
        ('company_id', '=', company.id),
        ('type', '=', 'sale'),
    ], limit=1)
    if sale_journal:
        company.sh_journal_id = sale_journal
        _logger.info("Auto-configured subscription journal: %s (id=%s)", sale_journal.name, sale_journal.id)
    else:
        _logger.warning("No sales journal found for company %s — subscription invoices will fail", company.name)


def uninstall_hook(env):
    """Remove ir.config_parameter records created by this module."""
    env['ir.config_parameter'].search([
        ('key', 'in', _CONFIG_PARAMETERS),
    ]).unlink()

import logging
import os

_logger = logging.getLogger(__name__)

# pip package name -> import name (only for non-trivial mappings)
_IMPORT_NAME_OVERRIDES = {
    'pyyaml': 'yaml',
    'pillow': 'PIL',
    'scikit-learn': 'sklearn',
    'python-dateutil': 'dateutil',
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

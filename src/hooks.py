import logging

_logger = logging.getLogger(__name__)


def _check_python_dependencies():
    """
    檢查必要的 Python 依賴是否已安裝
    如果缺少依賴，將自動安裝
    """
    import subprocess
    import sys

    _logger.info("Checking Python dependencies for LangChain integration")

    required_packages = {
        'langchain_openai': 'langchain-openai>=0.3.0',
        'langchain_core': 'langchain-core>=0.3.0',
        'langchain_mcp_adapters': 'langchain-mcp-adapters>=0.1.0',
        'langgraph': 'langgraph>=0.2.0',
    }

    missing_packages = []

    for package, pip_spec in required_packages.items():
        try:
            __import__(package)
            _logger.info(f"✓ {package} is installed")
        except ImportError:
            _logger.warning(f"✗ Missing required package: {package}")
            missing_packages.append(pip_spec)

    if missing_packages:
        _logger.info("=" * 60)
        _logger.info("Auto-installing missing Python packages...")
        _logger.info("=" * 60)

        for pip_spec in missing_packages:
            try:
                _logger.info(f"Installing {pip_spec}...")
                # Use --break-system-packages for Debian/Ubuntu externally-managed Python
                subprocess.check_call([
                    sys.executable, '-m', 'pip', 'install',
                    '--break-system-packages', pip_spec
                ])
                _logger.info(f"✓ Successfully installed {pip_spec}")
            except subprocess.CalledProcessError:
                # Fallback: try without --break-system-packages
                try:
                    subprocess.check_call([
                        sys.executable, '-m', 'pip', 'install', pip_spec
                    ])
                    _logger.info(f"✓ Successfully installed {pip_spec} (fallback)")
                except subprocess.CalledProcessError:
                    raise ImportError(
                        f"Failed to install {pip_spec}. "
                        f"Please install manually: pip install --break-system-packages {pip_spec}"
                    )

        # 重新檢查安裝是否成功
        for package in required_packages.keys():
            try:
                __import__(package)
                _logger.info(f"✓ {package} is now available")
            except ImportError:
                raise ImportError(
                    f"Package {package} was installed but cannot be imported. "
                    "Please restart Odoo."
                )


def pre_init_hook(env):
    """
    在模組安裝前執行，檢查必要的 Python 依賴
    如果依賴缺失，將自動安裝
    """
    _check_python_dependencies()

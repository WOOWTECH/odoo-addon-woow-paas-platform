"""Entry point: ``python -m newapi_proxy``.

Settings are validated before the server starts; a bad value exits with code
78 (EX_CONFIG) and a message that names the variable, never the value.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import sys
import time

import uvicorn

from . import __version__, logs
from .app import create_app
from .settings import SettingsError, load_settings


def main() -> int:
    try:
        settings, warnings = load_settings(os.environ, int(time.time()))
    except SettingsError as err:
        # str(err) is "<VAR>: <problem>" and never carries the value.
        sys.stderr.write(f'{{"level":"error","event":"config_invalid","detail":"{err}"}}\n')
        return 78
    logs.configure(settings.log_level, settings.secrets)
    for code in warnings:
        logs.event("config_warning", logging.WARNING, code=code)
    logs.event(
        "startup",
        version=__version__,
        web_kids=sorted(settings.web_keys),
        cron_kids=sorted(settings.cron_keys),
        ap_token=settings.ap_token is not None,
        op_token=settings.op_token is not None,
        username_regex=settings.username_re.pattern,
        tenant_ip_range=f"{ipaddress.IPv4Address(settings.tenant_ip_first)}-"
        f"{ipaddress.IPv4Address(settings.tenant_ip_last)}",
        add_quota_hourly_cap=settings.add_quota_hourly_cap,
        inflight=dict(settings.inflight),
        break_glass_until=settings.break_glass_until,
    )
    app = create_app(settings)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.port,
        http="h11",
        loop="asyncio",
        lifespan="on",
        log_config=None,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        limit_concurrency=512,
        timeout_keep_alive=5,
        timeout_graceful_shutdown=30,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

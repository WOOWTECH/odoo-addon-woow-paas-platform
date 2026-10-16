"""Entry point: ``python -m newapi_proxy``.

Settings are validated before the server starts; a bad value exits with code
78 (EX_CONFIG) and a message that names the variable, never the value.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import sys
import time

from . import __version__, logs
from .app import AdminApp, create_app
from .server import build_servers, serve
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
        replay_store=f"{settings.valkey_host}:{settings.valkey_port}" if settings.valkey_host else "local",
        port=settings.port,
        admin_port=settings.admin_port,
        max_connections=settings.max_connections,
    )
    app = create_app(settings)
    main_server, admin_server = build_servers(settings, app, AdminApp(app.proxy))
    asyncio.run(serve(main_server, admin_server))
    return 0


if __name__ == "__main__":
    sys.exit(main())

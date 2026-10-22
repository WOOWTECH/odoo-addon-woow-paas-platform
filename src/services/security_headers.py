"""#1457：平台頁面的安全標頭（HSTS、防點擊劫持、nosniff）。

完整的安全標頭只套在本 addon 自己的 HTML 入口：`/woow`（standalone app）、`/oauth2/*`
（授權同意頁、token 端點）與 `/device`（裝置授權頁）。商用驗收另要求公開平台主機
`aiot.woowtech.io` 的每個回應都帶 HSTS；這個單一標頭依 Host 限定，避免改到同一套
Odoo 服務的官方網站與 ERP。

HSTS 刻意**不帶** includeSubDomains／preload：那是 Cloudflare zone 層的決定（見 #1457
的建議清單）——在這裡加會把 aiot.woowtech.io 底下所有子網域一起鎖死，而平台不知道
那些子網域是不是都走 HTTPS。

本模組不 import ORM／controller：`models/ir_http.py` 與測試都直接用它，不會繞出循環 import。
"""

SECURITY_HEADER_PATH_PREFIXES = ('/woow', '/oauth2/', '/device')
AIOT_PUBLIC_HOST = 'aiot.woowtech.io'
HSTS_HEADER = 'Strict-Transport-Security'

SECURITY_HEADERS = {
    # 180 天。Cloudflare zone 層開 HSTS 後，邊緣的設定會取代這一條。
    HSTS_HEADER: 'max-age=15552000',
    'X-Frame-Options': 'SAMEORIGIN',
    'Content-Security-Policy': "frame-ancestors 'self'",
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'strict-origin-when-cross-origin',
}


def wants_security_headers(path):
    """`/woow`、`/device` 比對整段路徑（`/woowfoo` 不算）；`/oauth2/` 比對前綴。"""
    path = path or ''
    for prefix in SECURITY_HEADER_PATH_PREFIXES:
        if prefix.endswith('/'):
            if path.startswith(prefix):
                return True
        elif path == prefix or path.startswith(prefix + '/'):
            return True
    return False


def wants_platform_hsts(host):
    """只讓公開平台主機（可帶連接埠）在所有路徑取得 HSTS。"""
    host = str(host or '').strip().lower()
    hostname, separator, port = host.rpartition(':')
    if separator and port.isdigit():
        host = hostname
    return host.rstrip('.') == AIOT_PUBLIC_HOST


def apply_hsts_header(headers):
    """補上 HSTS；若下游已給更嚴格的值，保留原值。"""
    if HSTS_HEADER not in headers:
        headers[HSTS_HEADER] = SECURITY_HEADERS[HSTS_HEADER]


def apply_security_headers(headers):
    """補上 SECURITY_HEADERS；已經有同名標頭（例：Odoo 自己設的 CSP）就不覆蓋。"""
    for name, value in SECURITY_HEADERS.items():
        if name not in headers:
            headers[name] = value

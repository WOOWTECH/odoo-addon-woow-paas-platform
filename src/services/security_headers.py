"""#1457：平台頁面的安全標頭（HSTS、防點擊劫持、nosniff）。

只套在本 addon 自己的 HTML 入口：`/woow`（standalone app）、`/oauth2/*`（授權同意頁、
token 端點）與 `/device`（裝置授權頁）。同一套 Odoo 也服務官方網站與 ERP，全站預設
不在本 addon 改（CLAUDE.md「與上游 ERP 解耦」）。

HSTS 刻意**不帶** includeSubDomains／preload：那是 Cloudflare zone 層的決定（見 #1457
的建議清單）——在這裡加會把 aiot.woowtech.io 底下所有子網域一起鎖死，而平台不知道
那些子網域是不是都走 HTTPS。

本模組不 import ORM／controller：`models/ir_http.py` 與測試都直接用它，不會繞出循環 import。
"""

SECURITY_HEADER_PATH_PREFIXES = ('/woow', '/oauth2/', '/device')

SECURITY_HEADERS = {
    # 180 天。Cloudflare zone 層開 HSTS 後，邊緣的設定會取代這一條。
    'Strict-Transport-Security': 'max-age=15552000',
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


def apply_security_headers(headers):
    """補上 SECURITY_HEADERS；已經有同名標頭（例：Odoo 自己設的 CSP）就不覆蓋。"""
    for name, value in SECURITY_HEADERS.items():
        if name not in headers:
            headers[name] = value

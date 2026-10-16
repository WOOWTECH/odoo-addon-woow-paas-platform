#!/usr/bin/env python3
"""LLM-01b 的 stg 驗收（AC2–AC9）：每一格都是一行可以重跑的指令（DESIGN §8.4；票 LLM-01b）。

用法::

    python3 .sandbox/llm-acceptance.py --slug aimodels <子命令> [參數]

``--context`` 預設 ``woow-k3s``。碰到的範圍：``dev-sandbox-<slug>``（odoo shell／psql／port-forward）、
``new-api-stg``（唯讀 SQL、代理 log）、``paas-stg-system``（只讀 ConfigMap 登記表）。不碰 prod。

**秘密**：本工具不印任何秘密值。測試使用者的密碼只存在 ``--state-dir``（預設
``$XDG_RUNTIME_DIR`` 或 ``~/.cache`` 底下、0700 目錄、0600 檔），經 stdin 進 odoo shell；session
cookie 只在記憶體；``keys/create`` 回的 ``sk-…`` 一律遮成 ``sk-…(長度)``；log／DB 掃描只印筆數。
代理的簽章金鑰只在 Odoo pod 自己的 env 裡（``signed_request.py`` 讀 env，不經 argv）。

子命令與驗收對照（CT／GA 是 new-api 的限流桶：CT＝每個合成 IP 20 次／20 分鐘）：

====================  ======  =====================================================================
子命令                驗收    內容
====================  ======  =====================================================================
env-names             AC2     Odoo pod 裡三個 LLM env 的**名字**（不印值）
config                —       DB 裡的 LLM 設定（代理、前綴、block、開關、pilot）與 prefix 登記
user-create LOGIN     —       建 portal 測試使用者（密碼 0600 存 state 目錄）
api LOGIN EP [JSON]   AC3/5   以該使用者登入 ``/woow`` 的 session，呼叫 ``/api/llm/<EP>``
account LOGIN         AC3/4   該使用者的 AI 帳戶在 Odoo DB 的狀態與合成 IP（不含密文）
ap-search USERNAME    AC3     Odoo pod 內經代理以 AP 查 new-api 使用者（AP 的 GA 1 格、0 CT）
newapi USERNAME       AC3/4/8 new-api-stg DB 唯讀：使用者、PAT（名稱、last_used_ip）、session ip（0 CT）
cron-status|-pause|-resume   ``ir_cron_llm_provision`` 的 active（Odoo 測試被硬殺後要 resume）
run-cron [LOGIN]      AC6     odoo shell 跑一次開戶 cron；``requests`` 外呼計數、incident 數
crash-drill LOGIN p2|p7 AC4   odoo shell 跑 cron，在 P2（ap_search_user）／P7（建 UP）回應之後丟
                              BaseException 模擬行程死掉；之後 ``expire-lease`` ＋ ``run-cron``
expire-lease LOGIN    AC4     SQL 讓該帳戶的 lease 到期（等同行程死掉 5 分鐘後）
ac7 [LOGIN]           AC7     odoo shell 裡把簽章金鑰 env 改成尾端 \\n／中間夾 \\n：cron 是否搶戶、
                              keys/create 是否 llm_unavailable、log 是否含值
ac9                   AC9     odoo shell 寫 ``newapi_proxy_url=http://evil.example`` ⇒ 應被拒絕
grep-secrets          AC7     Odoo pod log、代理 log、Odoo DB dump 的 ``sk-``／``nap_`` 筆數
====================  ======  =====================================================================

AC4 的 CT 用量（每個帳戶自己的合成 IP 一個桶，所以 P2／P7 各用一個新使用者）：正常開戶 3（P4、
P6、P7）；P2 當掉後重跑 3；P7 當掉後重跑再 3（P4、P6、P7；P5 數 PAT 只吃 GA）⇒ P7 演練共 6 格；
驗證「孤兒 PAT ≤ 1」用 ``newapi``（DB 唯讀、0 CT），**不**用密碼登入列 PAT。``keys/create`` 1 格
（reveal）。全部在 20 分鐘 20 格之內；GA 另計（AP 每戶約 2、租戶每戶約 5）。

⚠ ``crash-drill`` 模擬的是「例外從 client 呼叫穿出」：P8 在 ``finally`` 裡照樣會撤 session；真正的
SIGKILL 連 P8 都不會跑（多一個 30 天後過期的 session，清理在 LLM-03）。演練前先 ``cron-pause``，
不然 live server 的 cron 會跟 odoo shell 搶同一戶；演練完 ``cron-resume``。
"""
from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
SIGNED_REQUEST = HERE.parent / 'extra' / 'newapi-proxy' / 'scripts' / 'signed_request.py'
PROXY = 'http://newapi-proxy.new-api-stg.svc.cluster.local:8080'
CRON_XMLID = ('woow_paas_platform', 'ir_cron_llm_provision')
ACCOUNT_TABLE = 'woow_paas_platform_llm_account'
NEWAPI_NS = 'new-api-stg'
LOGIN_RE = re.compile(r'[a-z][a-z0-9_.-]{2,40}')
USERNAME_RE = re.compile(r'w[a-z]{1,3}[0-9]{10}')
SECRET_PATTERNS = {'sk-': re.compile(r'sk-[A-Za-z0-9]{20,}'), 'nap_': re.compile(r'nap_[A-Za-z0-9]{40,}')}


def fail(message):
    print('ERROR: ' + message, file=sys.stderr)
    sys.exit(2)


def mask(value):
    """把回應裡任何像金鑰的東西遮掉（遞迴）。"""
    if isinstance(value, dict):
        return {k: mask(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask(v) for v in value]
    if isinstance(value, str):
        if value.startswith('sk-') and len(value) > 12:
            return 'sk-…(%d)' % len(value)
        for pattern in SECRET_PATTERNS.values():
            value = pattern.sub('<masked>', value)
    return value


class Sandbox:
    def __init__(self, args):
        self.context = args.context
        self.slug = args.slug
        self.ns = 'dev-sandbox-%s' % args.slug
        self.state_dir = Path(args.state_dir or os.environ.get('XDG_RUNTIME_DIR')
                              or Path.home() / '.cache') / ('llm-acceptance-%s' % args.slug)
        self._odoo = self._pg = self._nginx = self._db = None

    # ---- kubectl ------------------------------------------------------- #
    def kubectl(self, *args, input=None, check=True, ns=None):
        cmd = ['kubectl', '--context', self.context]
        if ns is not False:
            cmd += ['-n', ns or self.ns]
        proc = subprocess.run(cmd + list(args), input=input, capture_output=True, text=True)
        if check and proc.returncode != 0:
            fail('kubectl %s failed: %s' % (' '.join(args[:3]), proc.stderr.strip()[:300]))
        return proc

    def _one(self, kind, component):
        out = self.kubectl('get', kind, '-l', 'app.kubernetes.io/instance=%s,'
                           'app.kubernetes.io/component=%s' % (self.slug, component),
                           '-o', 'jsonpath={.items[*].metadata.name}').stdout.split()
        if len(out) != 1:
            fail('expected one %s/%s in %s, got %s' % (kind, component, self.ns, out))
        return out[0]

    @property
    def odoo(self):
        self._odoo = self._odoo or self._one('deploy', 'odoo')
        return self._odoo

    @property
    def pg(self):
        self._pg = self._pg or self._one('statefulset', 'postgres')
        return self._pg

    @property
    def nginx(self):
        self._nginx = self._nginx or self._one('svc', 'nginx')
        return self._nginx

    @property
    def db(self):
        if not self._db:
            # 只讀 chart Secret 的 postgres-database 這一格（DB 名稱，不是秘密）。
            names = self.kubectl('get', 'secret', '-l', 'app.kubernetes.io/instance=%s' % self.slug,
                                 '-o', 'jsonpath={range .items[*]}{.data.postgres-database}'
                                 '{"\\n"}{end}').stdout.split()
            names = [base64.b64decode(n).decode() for n in names if n]
            if len(names) != 1:
                fail('cannot resolve the sandbox DB name')
            self._db = names[0]
        return self._db

    def psql(self, sql):
        out = self.kubectl('exec', '-i', 'statefulset/%s' % self.pg, '--', 'psql', '-U', 'odoo',
                           '-d', self.db, '-At', '-F', '\t', '-v', 'ON_ERROR_STOP=1', input=sql).stdout
        return [line.split('\t') for line in out.splitlines() if line]

    def odoo_shell(self, script, env=None):
        """在 Odoo pod 裡跑 odoo shell；``script`` 經 stdin（可以帶秘密），``env`` 只放非秘密值。"""
        assigns = ['%s=%s' % (k, v) for k, v in (env or {}).items()]
        proc = self.kubectl('exec', '-i', 'deploy/%s' % self.odoo, '--', 'env', *assigns, 'odoo',
                            'shell', '-c', '/etc/odoo/odoo.conf', '-d', self.db, '--no-http',
                            '--log-level=warn', input=script, check=False)
        results = [line[len('RESULT '):] for line in proc.stdout.splitlines()
                   if line.startswith('RESULT ')]
        if proc.returncode != 0 or not results:
            fail('odoo shell failed (rc=%s): %s' % (proc.returncode, mask(proc.stderr)[-800:]))
        return json.loads(results[-1])

    # ---- state ---------------------------------------------------------- #
    def _cred_path(self, login):
        if not LOGIN_RE.fullmatch(login):
            fail('login must match %s' % LOGIN_RE.pattern)
        return self.state_dir / ('%s.json' % login)

    def save_credentials(self, login, password):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        path = self._cred_path(login)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump({'login': login, 'password': password}, handle)

    def credentials(self, login):
        path = self._cred_path(login)
        if not path.exists():
            fail('no saved credentials for %s; run user-create first' % login)
        return json.loads(path.read_text())


# --------------------------------------------------------------------------- #
#  odoo shell scripts（經 stdin；結果一律印成一行 RESULT <json>）                  #
# --------------------------------------------------------------------------- #
SHELL_USER_CREATE = '''
import json
login, password = {login!r}, {password!r}
Users = env['res.users'].sudo().with_context(no_reset_password=True, mail_create_nosubscribe=True)
user = Users.search([('login', '=', login)], limit=1)
if user:
    user.write({{'password': password}})
else:
    user = Users.create({{'name': 'LLM acceptance ' + login, 'login': login, 'password': password,
                         'email': login + '@llm-acceptance.invalid',
                         'groups_id': [(6, 0, [env.ref('base.group_portal').id])]}})
env.cr.commit()
print('RESULT ' + json.dumps({{'user_id': user.id, 'partner_id': user.partner_id.id}}))
'''

SHELL_RUN_CRON = '''
import json, os
from unittest.mock import patch
import requests
Account = env['woow_paas_platform.llm_account']
cls = type(Account)
only = [int(i) for i in os.environ.get('ONLY_ACCOUNT', '').split(',') if i]
real_claim = cls._llm_claim_for_provision
calls = []
real_request = requests.sessions.Session.request
def counting(self, method, url, *a, **k):
    calls.append(method)
    return real_request(self, method, url, *a, **k)
def scoped(records, account_ids=None):
    return real_claim(records, account_ids=only or None)
before = env['woow.billing.incident'].sudo().search_count([])
with patch.object(requests.sessions.Session, 'request', counting), \\
        patch.object(cls, '_llm_claim_for_provision', scoped):
    Account._cron_llm_provision()
env.cr.commit()
env.cr.execute("SELECT state, count(*) FROM {table} GROUP BY state")
print('RESULT ' + json.dumps({{
    'outbound_requests': len(calls),
    'incidents_added': env['woow.billing.incident'].sudo().search_count([]) - before,
    'accounts_by_state': dict(env.cr.fetchall())}}))
'''

SHELL_CRASH_DRILL = '''
import json, os
from unittest.mock import patch
stage = os.environ['DRILL_STAGE']
account_id = int(os.environ['DRILL_ACCOUNT_ID'])
target = {{'p2': 'ap_search_user', 'p7': 'user_create_access_token'}}[stage]
Account = env['woow_paas_platform.llm_account']
cls = type(Account)
real_client = cls._llm_client
real_claim = cls._llm_claim_for_provision

class SimulatedCrash(BaseException):
    pass

def crashing(records, role):
    client = real_client(records, role)
    class Wrapper:
        def __getattr__(self, name):
            attr = getattr(client, name)
            if name != target:
                return attr
            def call(*a, **k):
                result = attr(*a, **k)
                raise SimulatedCrash('%s returned %s' % (name, result.kind))
            return call
    return Wrapper()

def scoped(records, account_ids=None):
    return real_claim(records, account_ids=[account_id])

out = {{'stage': stage}}
with patch.object(cls, '_llm_client', crashing), patch.object(cls, '_llm_claim_for_provision', scoped):
    try:
        Account._cron_llm_provision()
        out['crashed'] = False
    except SimulatedCrash as exc:
        out['crashed'] = True
        out['crash_after'] = str(exc)
env.cr.rollback()
env.cr.execute("SELECT state, newapi_user_id, COALESCE(up_token_enc, '') <> '', "
               "provision_lease_token IS NOT NULL, provision_attempts, last_error_code "
               "FROM {table} WHERE id = %s", (account_id,))
row = env.cr.fetchone()
out['row'] = dict(zip(('state', 'newapi_user_id', 'has_up', 'leased', 'attempts', 'last_error_code'), row))
print('RESULT ' + json.dumps(out, default=str))
'''

SHELL_AC7 = '''
import io, json, logging, os, uuid
from unittest.mock import patch
from odoo.addons.woow_paas_platform.models.llm_common import LlmError
from odoo.addons.woow_paas_platform.services import newapi_client as nac
Account = env['woow_paas_platform.llm_account']
cls = type(Account)
buffer = io.StringIO()
handler = logging.StreamHandler(buffer)
root = logging.getLogger('odoo.addons.woow_paas_platform')
root.addHandler(handler)
root.setLevel(logging.DEBUG)
claims = []
def counting_claim(records, account_ids=None):
    claims.append(1)
    return None
out = {{}}
values = []
with patch.object(cls, '_llm_claim_for_provision', counting_claim):
    for label, name in (('cron', nac.ENV_AUTH_CRON), ('web', nac.ENV_AUTH_WEB)):
        good = os.environ.get(name, '')
        kid, _sep, secret = good.partition(':')
        half = len(secret) // 2
        values += [secret[:half], secret[half:]]
        trailing = good + '\\n'
        embedded = '%s:%s\\n%s' % (kid, secret[:half], secret[half:])
        if label == 'cron':
            for case, value in (('trailing_newline', trailing), ('embedded_newline', embedded)):
                del claims[:]
                with patch.dict(os.environ, {{name: value}}):
                    Account._cron_llm_provision()
                out['cron_%s_claimed' % case] = len(claims)
        else:
            login = os.environ.get('AC7_LOGIN')
            if login:
                user = env['res.users'].sudo().search([('login', '=', login)], limit=1)
                Key = env['woow_paas_platform.llm_api_key']
                before = Key.sudo().search_count([])
                with patch.dict(os.environ, {{name: embedded}}):
                    try:
                        Key._llm_create_free_trial_key(user.partner_id, str(uuid.uuid4()), user, '')
                        out['web_embedded_newline_keys_create'] = 'success'
                    except LlmError as exc:
                        out['web_embedded_newline_keys_create'] = exc.code
                out['web_mirror_rows_added'] = Key.sudo().search_count([]) - before
env.cr.rollback()
log = buffer.getvalue()
out['log_mentions_reason_code'] = 'signing_key_invalid' in log
out['log_contains_key_value'] = any(v and v in log for v in values)
print('RESULT ' + json.dumps(out))
'''

SHELL_AC9 = '''
import json
from odoo.exceptions import ValidationError
icp = env['ir.config_parameter'].sudo()
out = {{}}
for value in ('http://evil.example', 'https://newapi.woowtech.io',
              'http://newapi-proxy.new-api-stg.svc.cluster.local:8080'):
    try:
        with env.cr.savepoint():
            icp.set_param('woow_paas_platform.newapi_proxy_url', value)
        out[value] = 'accepted'
    except ValidationError:
        out[value] = 'rejected'
env.cr.rollback()
print('RESULT ' + json.dumps(out))
'''


# --------------------------------------------------------------------------- #
#  子命令                                                                       #
# --------------------------------------------------------------------------- #
def cmd_env_names(sbx, args):
    names = sbx.kubectl('exec', 'deploy/%s' % sbx.odoo, '--', 'sh', '-c',
                        'env | cut -d= -f1').stdout.split()
    wanted = ('WOOW_NEWAPI_PROXY_AUTH_WEB', 'WOOW_NEWAPI_PROXY_AUTH_CRON', 'WOOW_LLM_SECRET_KEYS')
    result = {name: name in names for name in wanted}
    print(json.dumps(result))
    return 0 if all(result.values()) else 1


def cmd_config(sbx, args):
    rows = sbx.psql("SELECT key, value FROM ir_config_parameter WHERE key IN ("
                    "'woow_paas_platform.newapi_proxy_url', 'woow_paas_platform.newapi_username_prefix', "
                    "'woow_paas_platform.llm_synthetic_ip_block', 'woow_paas_platform.show_llm', "
                    "'woow_paas_platform.llm_pilot_partner_ids', 'woow_paas_platform.llm_canary_partner_id') "
                    "ORDER BY key")
    config = dict(rows)
    prefix = config.get('woow_paas_platform.newapi_username_prefix')
    registry = None
    if prefix:
        proc = sbx.kubectl('get', 'configmap', 'woow-llm-sandbox-prefix-%s' % prefix,
                           '-o', 'jsonpath={.data.slug} {.data.namespace}', ns='paas-stg-system',
                           check=False)
        registry = proc.stdout.strip() or None
    print(json.dumps({'config': config, 'prefix_registered_to': registry}, ensure_ascii=False))
    return 0


def cmd_user_create(sbx, args):
    password = secrets.token_urlsafe(24)
    result = sbx.odoo_shell(SHELL_USER_CREATE.format(login=args.login, password=password))
    sbx.save_credentials(args.login, password)
    print(json.dumps(dict(result, login=args.login, credentials=str(sbx._cred_path(args.login)))))
    return 0


class PortForward:
    def __init__(self, sbx):
        self.sbx = sbx

    def __enter__(self):
        cmd = ['kubectl', '--context', self.sbx.context, '-n', self.sbx.ns, 'port-forward',
               'svc/%s' % self.sbx.nginx, ':80']
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        line = self.proc.stdout.readline()
        match = re.search(r'127\.0\.0\.1:(\d+)', line)
        if not match:
            self.proc.kill()
            fail('port-forward did not start: %s' % line.strip())
        self.base = 'http://127.0.0.1:%s' % match.group(1)
        return self

    def __exit__(self, *exc):
        self.proc.terminate()
        self.proc.wait(timeout=10)


def _jsonrpc(opener, url, params, timeout=60):
    data = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params}).encode()
    request = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def cmd_api(sbx, args):
    creds = sbx.credentials(args.login)
    params = json.loads(args.json) if args.json else {}
    if args.endpoint == 'keys/create':
        params.setdefault('client_request_id', str(uuid.uuid4()))
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with PortForward(sbx) as forward:
        login = _jsonrpc(opener, forward.base + '/web/session/authenticate',
                         {'db': sbx.db, 'login': creds['login'], 'password': creds['password']})
        if 'error' in login:
            fail('login failed: %s' % login['error'].get('message'))
        started = time.monotonic()
        body = _jsonrpc(opener, forward.base + '/api/llm/' + args.endpoint, params)
        elapsed = time.monotonic() - started
    print(json.dumps({'elapsed_ms': int(elapsed * 1000), 'response': mask(body)},
                     ensure_ascii=False))
    return 0


def _account_row(sbx, login):
    if not LOGIN_RE.fullmatch(login):
        fail('bad login')
    rows = sbx.psql(
        "SELECT a.id, a.state, a.newapi_username, a.newapi_user_id, a.provision_attempts, "
        "a.last_error_code, a.provision_lease_until, a.up_state, "
        "COALESCE(a.newapi_password_enc, '') <> '', COALESCE(a.up_token_enc, '') <> '' "
        "FROM %s a JOIN res_users u ON u.partner_id = a.partner_id "
        "WHERE u.login = '%s' AND a.state <> 'closed'" % (ACCOUNT_TABLE, login))
    if not rows:
        return None
    keys = ('id', 'state', 'newapi_username', 'newapi_user_id', 'attempts', 'last_error_code',
            'lease_until', 'up_state', 'has_password', 'has_up')
    return dict(zip(keys, rows[0]))


def _tenant_ip(account_id, block):
    import ipaddress
    return str(ipaddress.IPv4Address('198.18.0.0') + 256 + block * 4096 + account_id)


def cmd_account(sbx, args):
    row = _account_row(sbx, args.login)
    if row is None:
        print(json.dumps({'account': None}))
        return 1
    block = dict(sbx.psql("SELECT key, value FROM ir_config_parameter WHERE key = "
                          "'woow_paas_platform.llm_synthetic_ip_block'")).get(
        'woow_paas_platform.llm_synthetic_ip_block', '0')
    row['tenant_ip'] = _tenant_ip(int(row['id']), int(block))
    print(json.dumps(row))
    return 0


def cmd_ap_search(sbx, args):
    if not USERNAME_RE.fullmatch(args.username):
        fail('username must match %s' % USERNAME_RE.pattern)
    url = '%s/api/user/search?keyword=%s&p=1&page_size=10' % (PROXY, args.username)
    proc = sbx.kubectl('exec', '-i', 'deploy/%s' % sbx.odoo, '--', 'python3', '-',
                       '--identity', 'ap', 'GET', url, input=SIGNED_REQUEST.read_text(),
                       check=False)
    try:
        line = json.loads(proc.stdout.strip().splitlines()[-1])
        body = json.loads(line['body'])
    except (ValueError, IndexError, KeyError):
        fail('unexpected output from signed_request.py: %s' % mask(proc.stdout + proc.stderr)[-400:])
    data = body.get('data') or {}
    items = data.get('items') if isinstance(data, dict) else data
    summary = [{k: item.get(k) for k in ('id', 'username', 'role', 'group', 'quota', 'status',
                                          'DeletedAt')} for item in items or []]
    print(json.dumps({'status': line['status'], 'proxy_error': line.get('proxy_error'),
                      'items': summary}))
    exact = [s for s in summary if s['username'] == args.username]
    return 0 if line['status'] == 200 and len(exact) == 1 else 1


def _newapi_sql(sbx, sql):
    proc = sbx.kubectl('exec', 'newapi-postgres-0', '--', 'sh', '-c',
                       'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F "\t" -c "$1"', '_', sql,
                       ns=NEWAPI_NS)
    return [line.split('\t') for line in proc.stdout.splitlines() if line]


def cmd_newapi(sbx, args):
    if not USERNAME_RE.fullmatch(args.username):
        fail('username must match %s' % USERNAME_RE.pattern)
    users = _newapi_sql(sbx, "SELECT id, username, role, \"group\", quota, status, "
                             "deleted_at IS NOT NULL FROM users WHERE username = '%s'" % args.username)
    out = {'users': [dict(zip(('id', 'username', 'role', 'group', 'quota', 'status', 'deleted'), u))
                     for u in users]}
    if len(users) == 1:
        uid = int(users[0][0])
        out['pats'] = [dict(zip(('id', 'name', 'last_used_ip', 'expires_at'), r)) for r in _newapi_sql(
            sbx, 'SELECT id, name, last_used_ip, expires_at FROM user_access_tokens '
                 'WHERE user_id = %d ORDER BY id' % uid)]
        out['sessions'] = [dict(zip(('ip', 'status', 'login_method'), r)) for r in _newapi_sql(
            sbx, 'SELECT ip, status, login_method FROM user_sessions WHERE user_id = %d '
                 'ORDER BY created_at' % uid)]
        out['tokens'] = [dict(zip(('id', 'name', 'group', 'status', 'remain_quota'), r)) for r in
                         _newapi_sql(sbx, 'SELECT id, name, "group", status, remain_quota FROM tokens '
                                          'WHERE user_id = %d ORDER BY id' % uid)]
        out['woow_paas_up_pats'] = len([p for p in out['pats'] if p['name'].startswith('woow-paas-up-')])
    print(json.dumps(out))
    return 0 if len(users) == 1 else 1


def _cron_id(sbx):
    rows = sbx.psql("SELECT res_id FROM ir_model_data WHERE module = '%s' AND name = '%s'" % CRON_XMLID)
    return int(rows[0][0])


def cmd_cron(sbx, args):
    cron_id = _cron_id(sbx)
    if args.command == 'cron-pause':
        sbx.psql("SET lock_timeout = '75s'; UPDATE ir_cron SET active = false WHERE id = %d" % cron_id)
    elif args.command == 'cron-resume':
        sbx.psql('UPDATE ir_cron SET active = true WHERE id = %d' % cron_id)
    rows = sbx.psql('SELECT active, nextcall FROM ir_cron WHERE id = %d' % cron_id)
    print(json.dumps({'cron_id': cron_id, 'active': rows[0][0] == 't', 'nextcall': rows[0][1]}))
    return 0


def cmd_run_cron(sbx, args):
    env = {}
    if args.login:
        row = _account_row(sbx, args.login) or fail('no account for %s' % args.login)
        env['ONLY_ACCOUNT'] = row['id']
    print(json.dumps(sbx.odoo_shell(SHELL_RUN_CRON.format(table=ACCOUNT_TABLE), env)))
    return 0


def cmd_crash_drill(sbx, args):
    cron_id = _cron_id(sbx)
    if sbx.psql('SELECT active FROM ir_cron WHERE id = %d' % cron_id)[0][0] == 't':
        fail('run cron-pause first (otherwise the live server races the drill for the same account)')
    row = _account_row(sbx, args.login) or fail('no account for %s' % args.login)
    if row['state'] != 'provisioning':
        fail('account %s is %s, not provisioning' % (row['id'], row['state']))
    result = sbx.odoo_shell(SHELL_CRASH_DRILL.format(table=ACCOUNT_TABLE),
                            {'DRILL_STAGE': args.stage, 'DRILL_ACCOUNT_ID': row['id']})
    print(json.dumps(result))
    return 0 if result.get('crashed') else 1


def cmd_expire_lease(sbx, args):
    row = _account_row(sbx, args.login) or fail('no account for %s' % args.login)
    sbx.psql("UPDATE %s SET provision_lease_until = (now() at time zone 'UTC') - interval '1 minute' "
             "WHERE id = %d" % (ACCOUNT_TABLE, int(row['id'])))
    print(json.dumps({'account_id': row['id'], 'lease': 'expired'}))
    return 0


def cmd_ac7(sbx, args):
    env = {'AC7_LOGIN': args.login} if args.login else {}
    result = sbx.odoo_shell(SHELL_AC7, env)
    print(json.dumps(result))
    ok = (result.get('cron_trailing_newline_claimed') == 1
          and result.get('cron_embedded_newline_claimed') == 0
          and not result.get('log_contains_key_value')
          and (not args.login or (result.get('web_embedded_newline_keys_create') == 'llm_unavailable'
                                  and result.get('web_mirror_rows_added') == 0)))
    return 0 if ok else 1


def cmd_ac9(sbx, args):
    result = sbx.odoo_shell(SHELL_AC9)
    print(json.dumps(result))
    return 0 if result.get('http://evil.example') == 'rejected' else 1


def _count_stream(cmd, label, counts):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                            errors='replace')
    for line in proc.stdout:
        for name, pattern in SECRET_PATTERNS.items():
            hits = len(pattern.findall(line))
            if hits:
                counts.setdefault(label, {}).setdefault(name, 0)
                counts[label][name] += hits
    proc.wait()
    counts.setdefault(label, {})['_exit'] = proc.returncode


def cmd_grep_secrets(sbx, args):
    counts = {}
    base = ['kubectl', '--context', sbx.context]
    _count_stream(base + ['-n', sbx.ns, 'logs', 'deploy/%s' % sbx.odoo, '--all-containers',
                          '--since=%s' % args.since], 'odoo_pod_log', counts)
    for pod in sbx.kubectl('get', 'pod', '-l', 'app.kubernetes.io/component=proxy', '-o',
                           'jsonpath={.items[*].metadata.name}', ns=NEWAPI_NS).stdout.split():
        _count_stream(base + ['-n', NEWAPI_NS, 'logs', pod, '--since=%s' % args.since],
                      'proxy_log:%s' % pod, counts)
    _count_stream(base + ['-n', sbx.ns, 'exec', 'statefulset/%s' % sbx.pg, '--', 'pg_dump',
                          '-U', 'odoo', '--data-only', sbx.db], 'odoo_db_dump', counts)
    print(json.dumps(counts))
    leaked = any(v for c in counts.values() for k, v in c.items() if k != '_exit')
    return 1 if leaked else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--slug', required=True)
    parser.add_argument('--context', default='woow-k3s')
    parser.add_argument('--state-dir')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('env-names')
    sub.add_parser('config')
    p = sub.add_parser('user-create')
    p.add_argument('login')
    p = sub.add_parser('api')
    p.add_argument('login')
    p.add_argument('endpoint', choices=['state', 'account/activate', 'keys/list', 'keys/create',
                                        'usage/recent'])
    p.add_argument('json', nargs='?')
    p = sub.add_parser('account')
    p.add_argument('login')
    p = sub.add_parser('ap-search')
    p.add_argument('username')
    p = sub.add_parser('newapi')
    p.add_argument('username')
    for name in ('cron-status', 'cron-pause', 'cron-resume'):
        sub.add_parser(name)
    p = sub.add_parser('run-cron')
    p.add_argument('login', nargs='?')
    p = sub.add_parser('crash-drill')
    p.add_argument('login')
    p.add_argument('stage', choices=['p2', 'p7'])
    p = sub.add_parser('expire-lease')
    p.add_argument('login')
    p = sub.add_parser('ac7')
    p.add_argument('login', nargs='?')
    sub.add_parser('ac9')
    p = sub.add_parser('grep-secrets')
    p.add_argument('--since', default='24h')
    args = parser.parse_args(argv)
    sbx = Sandbox(args)
    handler = {
        'env-names': cmd_env_names, 'config': cmd_config, 'user-create': cmd_user_create,
        'api': cmd_api, 'account': cmd_account, 'ap-search': cmd_ap_search, 'newapi': cmd_newapi,
        'cron-status': cmd_cron, 'cron-pause': cmd_cron, 'cron-resume': cmd_cron,
        'run-cron': cmd_run_cron, 'crash-drill': cmd_crash_drill, 'expire-lease': cmd_expire_lease,
        'ac7': cmd_ac7, 'ac9': cmd_ac9, 'grep-secrets': cmd_grep_secrets,
    }[args.command]
    return handler(sbx, args)


if __name__ == '__main__':
    sys.exit(main())

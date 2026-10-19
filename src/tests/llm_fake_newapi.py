"""測試用的 client 假實作：與 ``services/newapi_client.NewApiClient`` **同介面**，回 ``Result``。

**mock 的邊界是 client，不是代理後面**：``llm_account._llm_client`` 回的是這裡的物件，所以
簽章、標頭驗證、query 序列化、§3.4 的回應分類、deadline 截斷在 Odoo 測試裡**都沒有執行**。
那些由純 Python 的 ``tests/test_llm_services.py`` 驗（含拿代理的 ``routetable.py`` 逐一比對
每個 client 方法的路由、身分、query、``web`` 旗標的契約測試），stg 實測再驗一次。
Odoo 測試真的跑的是 model、cron、獨立 cursor 與 commit，以及：

* **WEB／CRON 金鑰的選擇**：測試的 ``_llm_client`` adapter 先跑真的
  ``newapi_client.get_newapi_client``／``signing_key_for_role``（讀該 role 的 env、驗格式），
  再回 :meth:`FakeNewApi.client` 的角色視圖；WEB 視圖叫非 :data:`newapi_client.WEB_METHODS`
  的方法會跟真 client 一樣在送出前丟 ``NewApiInputError``，``role_calls`` 記每次呼叫的 role。

這裡照 rc.44 的行為（〔PROV〕§3、§4）模擬開戶與金鑰需要的部分：

* ``POST /api/user/`` 不回 id、同名回 ``success:false`` 沒有 ``message_key``（unique
  violation 原文）；``GET /api/user/search`` 以 username 子字串比對。
* login 回 JWT＋session；verify 回一次性 proof（綁 scopes／expires_at）；建 PAT 要 proof；
  PAT 只回一次明文。
* ``POST /api/token/`` 不回 id；``GET /api/token/`` 依 id desc；reveal 回 48 字元裸金鑰
  （代理的 120 秒限制也照做）。

另外提供：
* ``calls``：每次呼叫的 ``(method, client_ip)``——「零外呼」與「用的是合成 IP」的斷言。
* ``role_calls``：經角色視圖的每次呼叫 ``(method, role)``——web 路徑用 WEB、cron 用 CRON。
* ``fail_next``：指定方法下一次**照常執行**、但回給定的 ``Result``（例如「建好了但回應遺失」
  的 ``Unknown``、列表失敗的 ``NotExecuted``）。
* ``fail_before``：指定方法下一次**不執行**、只回給定的 ``Result``（例如「沒建成、結果未知」）。
* ``crash_before``／``crash_after``：在指定方法之前／之後丟例外（一次）——模擬 pod 在
  開戶途中被殺。丟 :class:`SimulatedCrash`（``BaseException``）才不會被 cron 的
  catch-all 吃掉，等同行程直接死掉。
"""
import secrets
import string
import time

from ..services import newapi_client as nac

_ALNUM = string.ascii_letters + string.digits
_TOKEN_BODY_FIELDS = (
    'name', 'expired_time', 'remain_quota', 'unlimited_quota', 'model_limits_enabled',
    'model_limits', 'allow_ips', 'group', 'cross_group_retry', 'allow_user_quota_query',
)
#: ``NewApiClient`` 的公開方法（角色視圖只管這些；``pats_of`` 等測試 helper 照常穿透）。
CLIENT_METHODS = frozenset(
    name for name in dir(nac.NewApiClient)
    if not name.startswith('_') and callable(getattr(nac.NewApiClient, name)))


class SimulatedCrash(BaseException):
    """模擬行程被殺：不是 ``Exception``，cron 的 catch-all 攔不到。"""


def _jwt():
    return 'eyJ%s.%s.%s' % (secrets.token_urlsafe(12).replace('=', ''),
                            secrets.token_urlsafe(24).replace('=', ''),
                            secrets.token_urlsafe(16).replace('=', ''))


def _rand(n):
    return ''.join(secrets.choice(_ALNUM) for _ in range(n))


class _RoleClient:
    """同一份假狀態的一個角色視圖（像真的：一個 client＝一組 WEB 或 CRON 金鑰）。"""

    def __init__(self, fake, role):
        self._fake = fake
        self.role = role

    def __getattr__(self, name):
        attribute = getattr(self._fake, name)
        if name not in CLIENT_METHODS:
            return attribute
        fake, role = self._fake, self.role

        def call(*args, **kwargs):
            fake.role_calls.append((name, role))
            if role == nac.ROLE_WEB and name not in nac.WEB_METHODS:
                # 與 NewApiClient._request 相同：WEB 金鑰叫 cron 路由，送出前就擋。
                raise nac.NewApiInputError('web_key_scope')
            dropped = fake.fail_before.pop(name, None)
            if dropped is not None:
                # 請求沒有到 new-api（或到了但沒有任何效果），只回這個結果。
                fake.calls.append((name, args[0] if name.startswith('user_') and args else ''))
                return dropped
            return attribute(*args, **kwargs)
        return call

    def __repr__(self):
        return '<FakeNewApi role=%s>' % self.role


class FakeNewApi:
    """一個 new-api（＋代理）的記憶體版；經 :meth:`client` 取得角色視圖。"""

    def __init__(self, role=nac.ROLE_CRON, clock=time.time):
        self.role = role
        self.clock = clock
        self.calls = []
        self.role_calls = []
        self.fail_next = {}
        self.fail_before = {}
        self.crash_before = {}
        self.crash_after = {}
        self.users = {}
        self.sessions = {}
        self.proofs = {}
        self.pats = {}
        self.tokens = {}
        self.logs = []
        self.quota_additions = []
        # The configured verifier trust anchor. Tests may deliberately change
        # it to exercise a mismatched operation log.
        self.service_admin_id = 7
        # Optional barriers for committed worker tests. A hook may block or
        # raise SimulatedCrash before/after the irreversible in-memory write.
        self.before_quota_add = None
        self.after_quota_add = None
        self._next = {'user': 100, 'pat': 500, 'token': 900, 'log': 2000}
        #: 測試可以改：新使用者的初始額度（P9 斷言 0）。
        self.quota_for_new_user = 0

    # ---- helpers ------------------------------------------------------- #
    def client(self, role):
        """``role``（``nac.ROLE_WEB``／``ROLE_CRON``）的 client 視圖。"""
        return _RoleClient(self, role)

    def _id(self, kind):
        self._next[kind] += 1
        return self._next[kind]

    def _enter(self, name, client_ip=''):
        self.calls.append((name, client_ip))
        exc = self.crash_before.pop(name, None)
        if exc:
            raise exc()

    def _leave(self, name, result):
        exc = self.crash_after.pop(name, None)
        if exc:
            raise exc()
        forced = self.fail_next.pop(name, None)
        return forced if forced is not None else result

    @staticmethod
    def _ok(data=None):
        return nac.Ok(data=data, message='', status=200, request_id=_rand(16))

    @staticmethod
    def _code(code, status=401):
        return nac.Definite(code=code, status=status)

    def _user_by_bearer(self, bearer):
        if bearer in self.pats:
            pat = self.pats[bearer]
            if pat['expires_at'] and pat['expires_at'] < self.clock():
                return None, self._code('ACCESS_TOKEN_EXPIRED')
            return self.users.get(pat['user_id']), None
        for session in self.sessions.values():
            if session['jwt'] == bearer and session['active']:
                return self.users.get(session['user_id']), None
        return None, self._code('AUTH_UNAUTHORIZED')

    def pats_of(self, user_id):
        return [p for p in self.pats.values() if p['user_id'] == user_id]

    def user_id_for_up(self, up):
        pat = self.pats.get(up)
        return pat and pat['user_id']

    # ---- anon / ap ----------------------------------------------------- #
    def status(self, **kw):
        self._enter('status')
        return self._leave('status', self._ok({'quota_per_unit': 500000}))

    def ap_create_user(self, username, password, display_name, **kw):
        self._enter('ap_create_user')
        if any(u['username'] == username for u in self.users.values()):
            result = nac.Unknown('no_message_key', status=200)  # unique violation, write call
        else:
            uid = self._id('user')
            self.users[uid] = {'id': uid, 'username': username, 'password': password,
                               'display_name': display_name, 'role': 1, 'status': 1,
                               'group': 'default', 'quota': self.quota_for_new_user,
                               'used_quota': 0, 'request_count': 0, 'DeletedAt': None}
            result = self._ok()
        return self._leave('ap_create_user', result)

    def ap_search_user(self, username, **kw):
        self._enter('ap_search_user')
        items = [dict(u, password=None) for u in self.users.values() if username in u['username']]
        return self._leave('ap_search_user', self._ok({'items': items, 'total': len(items)}))

    def ap_health(self, prefix, **kw):
        """circuit 的健康檢查（LLM-02）：搜尋永遠不存在的 ``<prefix>0000000000``。"""
        self._enter('ap_health')
        return self._leave('ap_health', self._ok({'items': [], 'total': 0}))

    def ap_pricing(self, **kw):
        """``GET /api/pricing``: preserve public envelope metadata (LLM-06)."""
        self._enter('ap_pricing')
        return self._leave('ap_pricing', self._ok({
            'data': [{'model_name': 'qwen3-4b-cluster1', 'enable_groups': ['free'], 'model_ratio': 0}],
            'group_ratio': {'default': 1.3, 'free': 0}, 'vendors': [],
        }))

    def ap_get_user(self, user_id, **kw):
        self._enter('ap_get_user')
        user = self.users.get(user_id)
        if not user:
            return self._leave('ap_get_user', nac.Definite(reason='no_message_key', status=200))
        return self._leave('ap_get_user', self._ok(dict(user, password=None)))

    def ap_add_quota(self, user_id, value, **kw):
        self._enter('ap_add_quota')
        if self.before_quota_add:
            self.before_quota_add(user_id, value)
        user = self.users.get(user_id)
        if not user:
            return self._leave('ap_add_quota', nac.Definite(
                message_key='User does not exist', status=200))
        if type(value) is not int or value <= 0:
            return self._leave('ap_add_quota', nac.Definite(
                message_key='Invalid parameters', status=200))
        before = user['quota']
        user['quota'] += value
        result = self._ok({'quota': user['quota']})
        self.quota_additions.append((user_id, value, result.request_id))
        # Mirror controller/user_quota.go::manageUserQuota and
        # model/log.go::RecordLogWithAdminInfo. In particular, Log.quota is a
        # display/usage field and the authoritative operation amount is
        # other.op.params.requested_quota.
        self.logs.append({
            'id': self._id('log'), 'type': 1, 'user_id': user_id,
            'username': user['username'], 'created_at': int(self.clock()),
            'request_id': result.request_id, 'quota': 0,
            'other': {
                'admin_info': {'admin_id': self.service_admin_id},
                'op': {'action': 'user.quota_add', 'params': {
                    'target_user_id': user_id, 'target_username': user['username'],
                    'mode': 'add', 'requested_quota': value,
                    'from': before, 'to': user['quota'],
                }},
            },
        })
        if self.after_quota_add:
            self.after_quota_add(user_id, value)
        return self._leave('ap_add_quota', result)

    def ap_log(self, query, **kw):
        """``GET /api/log/`` admin page, including deterministic id tie-breaks."""
        self._enter('ap_log')
        page = query.get('p')
        page_size = query.get('page_size')
        if type(page) is not int or type(page_size) is not int or not (1 <= page and 1 <= page_size <= 100):
            return self._leave('ap_log', nac.Definite(reason='no_message_key', status=200))
        rows = list(self.logs)
        log_type = query.get('type')
        if type(log_type) is int and log_type:
            rows = [row for row in rows if row.get('type') == log_type]
        username = query.get('username')
        if username:
            rows = [row for row in rows if row.get('username') == username]
        start = query.get('start_timestamp', 0)
        end = query.get('end_timestamp', 0)
        if type(start) is not int or type(end) is not int or start < 0 or end < start:
            return self._leave('ap_log', nac.Definite(reason='no_message_key', status=200))
        rows = [row for row in rows if row.get('created_at', 0) >= start and
                (not end or row.get('created_at', 0) <= end)]
        request_id = query.get('request_id')
        if request_id:
            rows = [row for row in rows if row.get('request_id') == request_id]
        rows.sort(key=lambda row: (row.get('created_at', 0), row.get('id', 0)), reverse=True)
        total = len(rows)
        start_idx = (page - 1) * page_size
        items = [dict(row) for row in rows[start_idx:start_idx + page_size]]
        return self._leave('ap_log', self._ok({
            'page': page, 'page_size': page_size, 'total': total, 'items': items,
        }))

    def ap_list_users(self, page, page_size=100, **kw):
        """``GET /api/user/?sort_by=id&sort_order=asc``：全部使用者（含別的環境與軟刪除的）。"""
        self._enter('ap_list_users')
        ordered = sorted(self.users.values(), key=lambda u: u['id'])
        chunk = ordered[(page - 1) * page_size:page * page_size]
        items = [dict(u, password=None) for u in chunk]
        return self._leave('ap_list_users', self._ok({
            'page': page, 'page_size': page_size, 'total': len(ordered), 'items': items}))

    def add_user(self, username, quota=0, used_quota=0, request_count=0, deleted=False):
        """直接放一個使用者（測試用：別的環境、管理員、軟刪除的）；回 id。"""
        uid = self._id('user')
        self.users[uid] = {'id': uid, 'username': username, 'password': 'x' * 40,
                           'display_name': 'other', 'role': 1, 'status': 1, 'group': 'default',
                           'quota': quota, 'used_quota': used_quota,
                           'request_count': request_count,
                           'DeletedAt': '2026-10-01T00:00:00Z' if deleted else None}
        return uid

    # ---- user: session / PAT ------------------------------------------- #
    def user_login(self, client_ip, username, password, **kw):
        self._enter('user_login', client_ip)
        user = next((u for u in self.users.values() if u['username'] == username), None)
        if not user or user['password'] != password or user['status'] != 1:
            return self._leave('user_login', nac.Definite(message_key=nac.MSG_LOGIN_FAILED,
                                                          status=200))
        sid = _rand(24)
        jwt = _jwt()
        self.sessions[sid] = {'user_id': user['id'], 'jwt': jwt, 'active': True, 'ip': client_ip}
        return self._leave('user_login', self._ok({
            'access_token': jwt, 'session': {'sid': sid}, 'user': {'id': user['id']}}))

    def _session_user(self, jwt):
        for session in self.sessions.values():
            if session['jwt'] == jwt and session['active']:
                return self.users[session['user_id']]
        return None

    def user_list_access_tokens(self, client_ip, jwt, **kw):
        self._enter('user_list_access_tokens', client_ip)
        user = self._session_user(jwt)
        if not user:
            return self._leave('user_list_access_tokens', self._code('AUTH_SESSION_REQUIRED', 403))
        items = [{'id': p['id'], 'name': p['name']} for p in self.pats_of(user['id'])]
        return self._leave('user_list_access_tokens', self._ok({'items': items}))

    def user_verify_generate(self, client_ip, jwt, password, expires_at, **kw):
        self._enter('user_verify_generate', client_ip)
        user = self._session_user(jwt)
        if not user:
            return self._leave('user_verify_generate', self._code('AUTH_UNAUTHORIZED'))
        if user['password'] != password:
            return self._leave('user_verify_generate',
                               self._code('SECURITY_VERIFICATION_FAILED', 200))
        proof = _jwt()
        self.proofs[proof] = {'user_id': user['id'], 'expires_at': expires_at, 'used': False}
        return self._leave('user_verify_generate', self._ok({'proof_token': proof}))

    def user_create_access_token(self, client_ip, jwt, proof, name, expires_at, **kw):
        self._enter('user_create_access_token', client_ip)
        user = self._session_user(jwt)
        record = self.proofs.get(proof)
        if not user or not record or record['used'] or record['user_id'] != user['id']:
            return self._leave('user_create_access_token', self._code('SECURITY_PROOF_INVALID', 403))
        if record['expires_at'] != expires_at:
            return self._leave('user_create_access_token',
                               self._code('SECURITY_PROOF_CONTEXT_MISMATCH', 403))
        record['used'] = True
        if len(self.pats_of(user['id'])) >= 20:
            return self._leave('user_create_access_token', self._code('ACCESS_TOKEN_LIMIT', 400))
        token = 'nap_' + _rand(43)
        pid = self._id('pat')
        self.pats[token] = {'id': pid, 'user_id': user['id'], 'name': name,
                            'expires_at': expires_at, 'last_used_ip': client_ip}
        return self._leave('user_create_access_token', self._ok({
            'token': token, 'item': {'id': pid, 'name': name, 'token_hint': token[-4:]}}))

    def user_delete_session(self, client_ip, jwt, sid, **kw):
        self._enter('user_delete_session', client_ip)
        session = self.sessions.get(sid)
        if not session or session['jwt'] != jwt:
            return self._leave('user_delete_session', self._code('AUTH_SESSION_NOT_FOUND', 404))
        session['active'] = False
        return self._leave('user_delete_session', self._ok({'revoked_sid': sid, 'current': True}))

    # ---- user: UP ------------------------------------------------------- #
    def user_self(self, client_ip, up, **kw):
        self._enter('user_self', client_ip)
        user, error = self._user_by_bearer(up)
        if error:
            return self._leave('user_self', error)
        self.pats[up]['last_used_ip'] = client_ip
        return self._leave('user_self', self._ok({
            'id': user['id'], 'username': user['username'], 'group': user['group'],
            'quota': user['quota'], 'used_quota': user['used_quota'],
            'request_count': user['request_count'], 'status': user['status']}))

    def user_create_token(self, client_ip, up, body, **kw):
        self._enter('user_create_token', client_ip)
        user, error = self._user_by_bearer(up)
        if error:
            return self._leave('user_create_token', error)
        tid = self._id('token')
        self.tokens[tid] = dict(body, id=tid, user_id=user['id'], key=_rand(48),
                                created_time=int(self.clock()), status=1, used_quota=0)
        return self._leave('user_create_token', self._ok())

    def user_list_tokens(self, client_ip, up, page=1, page_size=10, **kw):
        self._enter('user_list_tokens', client_ip)
        user, error = self._user_by_bearer(up)
        if error:
            return self._leave('user_list_tokens', error)
        mine = sorted((t for t in self.tokens.values() if t['user_id'] == user['id']),
                      key=lambda t: -t['id'])
        items = [dict(t, key=t['key'][:4] + '*' * 10 + t['key'][-4:])
                 for t in mine[(page - 1) * page_size:page * page_size]]
        return self._leave('user_list_tokens', self._ok({'items': items, 'total': len(mine)}))

    def user_get_token(self, client_ip, up, token_id, **kw):
        self._enter('user_get_token', client_ip)
        user, error = self._user_by_bearer(up)
        if error:
            return self._leave('user_get_token', error)
        token = self.tokens.get(token_id)
        if not token or token['user_id'] != user['id']:
            return self._leave('user_get_token', nac.Definite(reason='no_message_key', status=200))
        return self._leave('user_get_token', self._ok(dict(token, key=token['key'][:4] + '**')))

    def user_reveal_token(self, client_ip, up, token_id, **kw):
        self._enter('user_reveal_token', client_ip)
        user, error = self._user_by_bearer(up)
        if error:
            return self._leave('user_reveal_token', error)
        token = self.tokens.get(token_id)
        if not token or token['user_id'] != user['id']:
            return self._leave('user_reveal_token',
                               nac.Definite(proxy_error='denied', reason='reveal_not_found',
                                            status=403))
        if token['created_time'] < self.clock() - 120:
            return self._leave('user_reveal_token',
                               nac.Definite(proxy_error='denied', reason='reveal_window',
                                            status=403))
        return self._leave('user_reveal_token', self._ok({'key': token['key']}))

    def user_update_token(self, client_ip, up, body, **kw):
        self._enter('user_update_token', client_ip)
        user, error = self._user_by_bearer(up)
        if error:
            return self._leave('user_update_token', error)
        if not isinstance(body, dict) or set(body) != set(_TOKEN_BODY_FIELDS) | {'id'} \
                or type(body.get('id')) is not int:
            return self._leave('user_update_token',
                               nac.Definite(reason='token_fields', status=400))
        token = self.tokens.get(body['id'])
        if not token or token['user_id'] != user['id']:
            return self._leave('user_update_token',
                               nac.Definite(reason='no_message_key', status=200))
        token.update({field: body[field] for field in _TOKEN_BODY_FIELDS})
        return self._leave('user_update_token', self._ok())

    def user_set_token_status(self, client_ip, up, token_id, enabled, **kw):
        if type(enabled) is not bool:
            raise nac.NewApiInputError('token_status') from None
        self._enter('user_set_token_status', client_ip)
        user, error = self._user_by_bearer(up)
        if error:
            return self._leave('user_set_token_status', error)
        token = self.tokens.get(token_id) if type(token_id) is int else None
        if not token or token['user_id'] != user['id']:
            return self._leave('user_set_token_status',
                               nac.Definite(reason='no_message_key', status=200))
        token['status'] = 1 if enabled else 2
        return self._leave('user_set_token_status', self._ok())

    def user_delete_token(self, client_ip, up, token_id, **kw):
        self._enter('user_delete_token', client_ip)
        user, error = self._user_by_bearer(up)
        if error:
            return self._leave('user_delete_token', error)
        token = self.tokens.get(token_id) if type(token_id) is int else None
        if not token or token['user_id'] != user['id']:
            return self._leave('user_delete_token',
                               nac.Definite(reason='no_message_key', status=200))
        self.tokens.pop(token_id)
        return self._leave('user_delete_token', self._ok())

    def user_log_self(self, client_ip, up, query, **kw):
        self._enter('user_log_self', client_ip)
        user, error = self._user_by_bearer(up)
        if error:
            return self._leave('user_log_self', error)
        rows = [r for r in self.logs if r['user_id'] == user['id']
                and r['created_at'] >= int(query.get('start_timestamp') or 0)
                and (not query.get('group') or r.get('group') == query['group'])]
        rows.sort(key=lambda r: -r['created_at'])
        size = int(query.get('page_size') or 10)
        return self._leave('user_log_self', self._ok({'items': rows[:size], 'total': len(rows)}))

    def add_log(self, user_id, token_name, model='qwen3-4b-cluster1', group='free'):
        """模擬一次 relay 呼叫留下的 type=2 log（含要被 DTO 剝掉的 channel）。"""
        self.logs.append({
            'id': self._id('log'), 'user_id': user_id, 'token_name': token_name, 'model_name': model, 'group': group,
            'created_at': int(self.clock()), 'prompt_tokens': 12, 'completion_tokens': 34,
            'use_time': 2, 'request_id': _rand(20), 'channel': 7, 'quota': 0, 'type': 2,
        })

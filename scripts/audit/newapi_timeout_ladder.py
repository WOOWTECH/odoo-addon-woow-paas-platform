"""LLM-01b／01c（DESIGN §1.2、§3.5，〔REV〕63）：前端 → Odoo → newapi-proxy 的逾時階梯。

前端 > 平台 > 代理 > new-api：每一層的期限都要比外面那一層早到，分類才會由內層給出（代理回
``connect``／``timeout``），而不是外層先逾時、變成說不清楚的 Unknown。這裡守的是 Odoo 端的具名
常數（``src/services/newapi_client.py``）與它們對代理、對端點總預算、對 lease 的大小關係：

1. 代理接受的 ``X-Woow-Read-Timeout`` 範圍（``extra/newapi-proxy/newapi_proxy/app.py`` 的
   ``_READ_TIMEOUT_RE``）必須是 1–N 連續整數，而 client 夾的上限 ``PROXY_READ_TIMEOUT_MAX`` == N。
2. 每一個 ``TIMEOUT_* = (connect, read)``：送出去的標頭值（read − ``PROXY_READ_TIMEOUT_MARGIN``，
   夾在 1–N）落在代理範圍內，而且**嚴格小於** read（代理的總期限比 Odoo 的 read 早到）；
   ``MIN_CALL_SECONDS`` ≥ 餘裕＋1（deadline 截斷後的 read 也成立）。
3. 建金鑰：``KEY_CREATE_BUDGET_SECONDS`` ≥ 一次完整呼叫（connect＋read）、> ``MIN_CALL_SECONDS``；
   ``llm_api_key._STALE_CREATING_SECONDS`` > 預算＋一次完整呼叫（不會把還在跑的請求判成殭屍）。
4. 開戶：``PROVISION_TICK_SECONDS`` ≥ 一次完整呼叫；``llm_common.PROVISION_LEASE_MINUTES`` 的 lease
   > tick＋一次完整呼叫＋``SESSION_REVOKE_GRACE_SECONDS``（lease 一定比最壞的一個 tick 長）。
   其他 LLM cron（LLM-02b）：``CRON_TICK_SECONDS`` ≥ 一次完整的 cron 讀取（``TIMEOUT_CRON_READ``），
   而且 ≤ 開戶 tick（N28：開戶是唯一允許到 60 秒的 tick）。
5. **結構**：``NewApiClient`` 每個帶 ``timeout=`` 預設值的公開方法，預設值都必須是某個 ``TIMEOUT_*``
   常數（不能寫字面值），而且「方法 → 常數」的對照必須等於 ``baselines/newapi-timeout-ladder.json``
   ——新增方法或改它的逾時類別要人重新對 DESIGN §3.5 的表，再手改那份 json（``--update-baseline``
   **不會**重產它）。``_request`` 算標頭時必須用 ``PROXY_READ_TIMEOUT_MAX``／``_MARGIN`` 兩個名字。

6. **前端那一半**（LLM-01c）：``src/static/src/paas/services/llm_service.js`` 的
   ``KEY_CREATE_TIMEOUT_MS`` > （``KEY_CREATE_BUDGET_SECONDS``＋一次完整呼叫）× 1000——前端要比後端的
   建金鑰總預算晚放棄，否則前端先逾時、後端還在建，使用者看不到明文；``rpc.js`` 的預設
   ``REQUEST_TIMEOUT_MS`` > 互動讀／寫各一次完整呼叫。結構：``llm_service.js`` 裡每個 ``timeoutMs:``
   都只能是 ``KEY_CREATE_TIMEOUT_MS``（不寫字面值），而且每個 ``/api/llm/keys/create`` 呼叫都一定帶它。

⚠ 不守的格（具名盲區）：互動寫入的端點總預算
15 秒（LLM-07）還沒有常數，第 6 點暫以「一次完整呼叫」比；呼叫端明確覆寫 ``timeout=`` 的地方（例如 P9 用
``TIMEOUT_PROVISION``）只要是 ``TIMEOUT_*`` 就過，不驗用對了哪一個；代理到 new-api 的實際上游呼叫
由代理自己的 deadline 邏輯負責（``upstream.py``，代理的單元測試守）。
解析失敗、找不到常數一律當違規（否定性判準：探針壞掉時「零違規」與「全部合規」輸出相同）。
"""
import ast
import json
import re
from pathlib import Path

CLIENT = 'src/services/newapi_client.py'
COMMON = 'src/models/llm_common.py'
KEYS = 'src/models/llm_api_key.py'
PROXY_APP = 'extra/newapi-proxy/newapi_proxy/app.py'
BASELINE = 'scripts/audit/baselines/newapi-timeout-ladder.json'
FRONTEND_SERVICE = 'src/static/src/paas/services/llm_service.js'
FRONTEND_RPC = 'src/static/src/paas/services/rpc.js'
SOURCES = (CLIENT, COMMON, KEYS, PROXY_APP, BASELINE, FRONTEND_SERVICE, FRONTEND_RPC)
PREFIX = 'newapi-timeout-ladder：'
CLIENT_CLASS = 'NewApiClient'


def _tree(root, rel):
    return ast.parse((Path(root) / rel).read_text(encoding='utf-8'), filename=rel)


def _module_constants(tree):
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            try:
                out[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                continue
    return out


def _int(constants, name, rel):
    value = constants.get(name)
    if type(value) is not int:
        raise ValueError('%s 缺少整數常數 %s' % (rel, name))
    return value


def _proxy_range(tree):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == '_READ_TIMEOUT_RE' for t in node.targets):
            call = node.value
            if isinstance(call, ast.Call) and call.args and isinstance(call.args[0], ast.Constant) \
                    and isinstance(call.args[0].value, str):
                pattern = re.compile(call.args[0].value)
                return sorted(n for n in range(0, 121) if pattern.fullmatch(str(n)))
    raise ValueError('%s 找不到 _READ_TIMEOUT_RE = re.compile("…")' % PROXY_APP)


def _client_class(tree):
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == CLIENT_CLASS:
            return node
    raise ValueError('%s 找不到 class %s' % (CLIENT, CLIENT_CLASS))


def method_timeouts(tree):
    """``{方法名: 預設 timeout 的常數名}``（只看帶 ``timeout`` keyword-only 預設值的公開方法）。"""
    mapping = {}
    for node in _client_class(tree).body:
        if not isinstance(node, ast.FunctionDef) or node.name.startswith('_'):
            continue
        names = [a.arg for a in node.args.kwonlyargs]
        if 'timeout' not in names:
            continue
        default = node.args.kw_defaults[names.index('timeout')]
        mapping[node.name] = default.id if isinstance(default, ast.Name) else ast.dump(default)
    if not mapping:
        raise ValueError('%s 的 %s 沒有任何帶 timeout 預設值的方法' % (CLIENT, CLIENT_CLASS))
    return mapping


def _header_uses_named_clamp(tree):
    for node in _client_class(tree).body:
        if isinstance(node, ast.FunctionDef) and node.name == '_request':
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            return {'PROXY_READ_TIMEOUT_MAX', 'PROXY_READ_TIMEOUT_MARGIN'} <= names
    raise ValueError('%s 找不到 %s._request' % (CLIENT, CLIENT_CLASS))


def _js_int(root, rel, name):
    """``[export] const NAME = <整數>;``（行首）。找不到就丟 ValueError（fail loud）。"""
    text = (Path(root) / rel).read_text(encoding='utf-8')
    match = re.search(r'^(?:export\s+)?const\s+%s\s*=\s*(\d+)\s*;' % re.escape(name), text, re.M)
    if not match:
        raise ValueError('%s 找不到整數常數 %s' % (rel, name))
    return int(match.group(1)), text


def check(root):
    """回傳違規清單；缺檔、解析失敗、結構漂移皆不得回空清單。"""
    violations = []

    def fail(message):
        violations.append(PREFIX + message)

    try:
        client_tree = _tree(root, CLIENT)
        client = _module_constants(client_tree)
        common = _module_constants(_tree(root, COMMON))
        keys = _module_constants(_tree(root, KEYS))
        accepted = _proxy_range(_tree(root, PROXY_APP))
        baseline = json.loads((Path(root) / BASELINE).read_text(encoding='utf-8'))
        timeouts = {name: value for name, value in client.items() if name.startswith('TIMEOUT_')}
        if not timeouts:
            raise ValueError('%s 找不到任何 TIMEOUT_* 常數' % CLIENT)
        margin = _int(client, 'PROXY_READ_TIMEOUT_MARGIN', CLIENT)
        clamp = _int(client, 'PROXY_READ_TIMEOUT_MAX', CLIENT)
        min_call = _int(client, 'MIN_CALL_SECONDS', CLIENT)
        budget = _int(client, 'KEY_CREATE_BUDGET_SECONDS', CLIENT)
        tick = _int(client, 'PROVISION_TICK_SECONDS', CLIENT)
        cron_tick = _int(client, 'CRON_TICK_SECONDS', CLIENT)
        grace = _int(client, 'SESSION_REVOKE_GRACE_SECONDS', CLIENT)
        lease = _int(common, 'PROVISION_LEASE_MINUTES', COMMON) * 60
        stale = _int(keys, '_STALE_CREATING_SECONDS', KEYS)
        mapping = method_timeouts(client_tree)
        named_clamp = _header_uses_named_clamp(client_tree)
        front_key_ms, front_source = _js_int(root, FRONTEND_SERVICE, 'KEY_CREATE_TIMEOUT_MS')
        rpc_default_ms, _ = _js_int(root, FRONTEND_RPC, 'REQUEST_TIMEOUT_MS')
    except Exception as exc:  # noqa: BLE001 - fail loud, never "zero violations"
        return [PREFIX + '無法載入或解析（%s：%s）；見 DESIGN §3.5' % (type(exc).__name__, exc)]

    # 1. 代理範圍與 client 的夾值一致
    if not accepted or accepted != list(range(1, accepted[-1] + 1)):
        fail('代理 _READ_TIMEOUT_RE 接受的不是 1–N 的連續整數（%s）' % accepted[:5])
        return violations
    if clamp != accepted[-1]:
        fail('client PROXY_READ_TIMEOUT_MAX=%s 必須等於代理接受的上限 %s（%s）'
             % (clamp, accepted[-1], PROXY_APP))
    if margin < 1:
        fail('PROXY_READ_TIMEOUT_MARGIN=%s 必須 ≥ 1：代理的總期限要比 Odoo 的 read 早到' % margin)
    if not named_clamp:
        fail('%s._request 算 X-Woow-Read-Timeout 時必須用 PROXY_READ_TIMEOUT_MAX／'
             'PROXY_READ_TIMEOUT_MARGIN（不要寫回字面值）' % CLIENT_CLASS)

    # 2. 每個 TIMEOUT_* 的標頭值
    for name, value in sorted(timeouts.items()):
        if not (isinstance(value, tuple) and len(value) == 2
                and all(type(v) is int and v >= 1 for v in value)):
            fail('%s 必須是正整數的 (connect, read)，收到 %r' % (name, value))
            continue
        connect, read = value
        header = max(1, min(clamp, read - margin))
        if header not in accepted:
            fail('%s：送出的 X-Woow-Read-Timeout=%s 代理不收（%s–%s）'
                 % (name, header, accepted[0], accepted[-1]))
        if not header < read:
            fail('%s：X-Woow-Read-Timeout=%s 必須 < Odoo 的 read=%s（否則 Odoo 先逾時、分類變 Unknown）'
                 % (name, header, read))
    if min_call < margin + 1:
        fail('MIN_CALL_SECONDS=%s 必須 ≥ 餘裕＋1=%s（deadline 截斷後的 read 也要比標頭長）'
             % (min_call, margin + 1))

    # 3. 建金鑰
    key_create = timeouts.get('TIMEOUT_KEY_CREATE')
    if not isinstance(key_create, tuple):
        fail('%s 缺少 TIMEOUT_KEY_CREATE' % CLIENT)
    else:
        one = sum(key_create)
        if budget < one:
            fail('KEY_CREATE_BUDGET_SECONDS=%s 必須 ≥ 一次完整呼叫 %s（TIMEOUT_KEY_CREATE）' % (budget, one))
        if budget <= min_call:
            fail('KEY_CREATE_BUDGET_SECONDS=%s 必須 > MIN_CALL_SECONDS=%s' % (budget, min_call))
        if stale <= budget + one:
            fail('llm_api_key._STALE_CREATING_SECONDS=%s 必須 > 建金鑰預算＋一次呼叫=%s'
                 '（否則還在跑的請求會被當成殭屍標 failed_reveal）' % (stale, budget + one))

    # 4. 開戶
    provision = timeouts.get('TIMEOUT_PROVISION')
    if not isinstance(provision, tuple):
        fail('%s 缺少 TIMEOUT_PROVISION' % CLIENT)
    else:
        one = sum(provision)
        if tick < one:
            fail('PROVISION_TICK_SECONDS=%s 必須 ≥ 一次完整呼叫 %s（TIMEOUT_PROVISION）' % (tick, one))
        if lease <= tick + one + grace:
            fail('lease（PROVISION_LEASE_MINUTES）=%ss 必須 > tick＋一次呼叫＋P8 寬限=%ss'
                 '（否則還在跑的 tick 會被另一個 worker 接手）' % (lease, tick + one + grace))

    # 4b. 其他 LLM cron 的 tick（N28）
    cron_read = timeouts.get('TIMEOUT_CRON_READ')
    if not isinstance(cron_read, tuple):
        fail('%s 缺少 TIMEOUT_CRON_READ' % CLIENT)
    else:
        one = sum(cron_read)
        if cron_tick < one:
            fail('CRON_TICK_SECONDS=%s 必須 ≥ 一次完整的 cron 讀取 %s（TIMEOUT_CRON_READ）'
                 % (cron_tick, one))
        if cron_tick > tick:
            fail('CRON_TICK_SECONDS=%s 不得大於開戶 tick PROVISION_TICK_SECONDS=%s（N28）'
                 % (cron_tick, tick))

    # 5. 結構：方法 → 逾時類別
    literal = sorted(m for m, const in mapping.items() if not const.startswith('TIMEOUT_'))
    if literal:
        fail('這些 client 方法的 timeout 預設值不是 TIMEOUT_* 常數：%s' % ', '.join(literal))
    unknown = sorted(m for m, const in mapping.items()
                     if const.startswith('TIMEOUT_') and const not in timeouts)
    if unknown:
        fail('這些方法用了不存在的 TIMEOUT_*：%s' % ', '.join(unknown))
    expected = baseline.get('methods') if isinstance(baseline, dict) else None
    if mapping != expected:
        changed = sorted(k for k in set(mapping) | set(expected or {})
                         if mapping.get(k) != (expected or {}).get(k))
        fail('client 方法的逾時類別與 %s 不符（%s）：對照 DESIGN §3.5 重新核對後手改那份 json'
             % (BASELINE, ', '.join(changed)))

    # 6. 前端那一半（LLM-01c）
    if isinstance(key_create, tuple):
        floor_ms = (budget + sum(key_create)) * 1000
        if front_key_ms <= floor_ms:
            fail('%s KEY_CREATE_TIMEOUT_MS=%s 必須 > （KEY_CREATE_BUDGET_SECONDS＋一次完整呼叫）×1000=%s'
                 '（前端先放棄的話，後端建好的金鑰明文就沒人收）' % (FRONTEND_SERVICE, front_key_ms, floor_ms))
    for name in ('TIMEOUT_INTERACTIVE_READ', 'TIMEOUT_INTERACTIVE_WRITE'):
        value = timeouts.get(name)
        if not isinstance(value, tuple):
            fail('%s 缺少 %s' % (CLIENT, name))
            continue
        if rpc_default_ms <= sum(value) * 1000:
            fail('%s REQUEST_TIMEOUT_MS=%s 必須 > %s 一次完整呼叫 %s ms'
                 % (FRONTEND_RPC, rpc_default_ms, name, sum(value) * 1000))
    used = re.findall(r'\btimeoutMs:\s*([A-Za-z_0-9.]+)', front_source)
    literal_front = sorted(set(u for u in used if u != 'KEY_CREATE_TIMEOUT_MS'))
    if literal_front:
        fail('%s 的 timeoutMs 只能用具名常數 KEY_CREATE_TIMEOUT_MS，收到：%s'
             % (FRONTEND_SERVICE, ', '.join(literal_front)))
    create_calls = re.findall(r'\bcall\("/api/llm/keys/create"([^;]*?)\);',
                              front_source, re.S)
    if not create_calls or any(not re.search(r'\btimeoutMs:\s*KEY_CREATE_TIMEOUT_MS\b', body)
                               for body in create_calls):
        fail('%s 建金鑰（"/api/llm/keys/create"）的呼叫必須帶 timeoutMs: KEY_CREATE_TIMEOUT_MS'
             '（否則用 rpc.js 的預設 30 秒，比 DESIGN §3.5 的 40 秒短）' % FRONTEND_SERVICE)

    return violations


if __name__ == '__main__':
    import sys
    problems = check(Path(__file__).resolve().parents[2])
    print('\n'.join(problems) or 'ok')
    sys.exit(1 if problems else 0)

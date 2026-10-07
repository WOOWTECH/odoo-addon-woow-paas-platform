"""#1257 Q31：核對 init_n8n 的同步 budget envelope 與 client connect/read 接線。

比照 ``init_odoo_budget.py``（#1152）：只接受已人工核對的呼叫／迴圈結構，結構
指紋不同即 fail-loud，不能直接重產 baseline 消紅。指紋只正規化算式要用的數字
（kubectl／node request 的預設 timeout、readiness exec timeout、restart 的次數與
間隔），其值仍從實際 AST 取出；其他數值從 module 宣告讀，不硬寫 531。

envelope 同樣不含 threadpool 排隊、process 建立／收尾、DNS／網路，也不保證容器內
exec 被取消；requests read 是 inactivity timeout。lease（Q32）另以
``connect + read + 餘裕`` 從同一組常數推導，見 ``src/models/cloud_service_init_attempt.py``。
"""
import ast
import copy
import hashlib
import importlib.util
import json
from pathlib import Path


CLIENT = 'src/services/paas_operator.py'
OPERATOR = 'extra/paas-operator/src/services/n8n_init.py'
KUBECTL = 'extra/paas-operator/src/services/helm.py'
API = 'extra/paas-operator/src/api/init.py'
BASELINE = 'scripts/audit/baselines/init-n8n-budget-structure.json'
SOURCES = (CLIENT, OPERATOR, KUBECTL, API)

# 每個名字都是算式的前提：步驟順序、呼叫次數、迴圈與 sleep 條件、timeout forwarding。
STRUCTURES = {
    CLIENT: ('PaaSOperatorClient.init_n8n', 'PaaSOperatorClient._request'),
    OPERATOR: (
        'N8nInitService.__init__', 'N8nInitService._node_request',
        'N8nInitService.initialize', 'N8nInitService._get_n8n_pod',
        'N8nInitService._wait_for_ready', 'N8nInitService._is_owner_setup_done',
        'N8nInitService._setup_owner', 'N8nInitService._login',
        'N8nInitService._get_api_key_scopes', 'N8nInitService._create_api_key',
        'N8nInitService._find_secret', 'N8nInitService._find_deployment',
        'N8nInitService._delete_api_keys_labelled', 'N8nInitService._ensure_api_key',
    ),
    KUBECTL: (
        'KubernetesService._run_command', 'KubernetesService.get_pod_name',
        'KubernetesService.exec_in_pod', 'KubernetesService.patch_secret',
        'KubernetesService.get_deployments',
        'KubernetesService.rollout_restart_deployment',
    ),
    API: ('init_n8n',),
}

# 每步各打一次 node request（沒帶 timeout ⇒ 用 ``_node_request`` 的預設值）。
# ``_create_api_key`` 內部先呼叫 ``_get_api_key_scopes``，兩者分開計。
# #1534：``_is_owner_setup_done`` 之後分兩條互斥的路——owner 尚未建立走 FRESH_STEPS（建
# owner、登入、建 key＋prune），已存在走 ``_ensure_api_key``（一次 exec，不登入）。envelope
# 取兩條路的較大者；互斥由 ``_owner_branches`` 從 AST 確認。
OWNER_CHECK = ('N8nInitService._is_owner_setup_done',)
FRESH_STEPS = (
    'N8nInitService._setup_owner',
    'N8nInitService._login', 'N8nInitService._get_api_key_scopes',
    'N8nInitService._create_api_key',
)
NODE_STEPS = OWNER_CHECK + FRESH_STEPS

_ODOO = None


def _odoo():
    """共用 ``init_odoo_budget`` 的 AST helper（同一套解析規則，不複製一份）。"""
    global _ODOO
    if _ODOO is None:
        spec = importlib.util.spec_from_file_location(
            'woow_init_odoo_budget_helpers', Path(__file__).with_name('init_odoo_budget.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _ODOO = module
    return _ODOO


def _restart_loop(initialize):
    """回傳 (For 節點, range 次數 literal, sleep 秒數 literal, sleep 條件上界 literal)。"""
    h = _odoo()
    loop = h.one((n for n in ast.walk(initialize) if isinstance(n, ast.For)
                  and h.calls(n, 'rollout_restart_deployment')), 'restart loop')
    it = loop.iter
    if not (isinstance(it, ast.Call) and isinstance(it.func, ast.Name) and it.func.id == 'range'
            and len(it.args) == 1):
        raise ValueError('restart loop 不再是 range(N)')
    sleep = h.one(h.calls(loop, 'sleep'), 'restart sleep')
    guard = h.one((n for n in ast.walk(loop) if isinstance(n, ast.If) and h.calls(n, 'sleep')),
                  'restart sleep condition')
    if not (isinstance(guard.test, ast.Compare) and len(guard.test.comparators) == 1
            and isinstance(guard.test.ops[0], ast.Lt)):
        raise ValueError('restart sleep 條件不是可解析的 attempt < K')
    return loop, it.args[0], sleep.args[0], guard.test.comparators[0]


def _owner_branches(initialize):
    """#1534：``initialize`` 裡 ``if self._is_owner_setup_done(...)`` 的兩個分支必須互斥。

    body（owner 已存在）只走 ``_ensure_api_key``、不登入不建 owner；orelse 走建 owner／
    登入／建 key、不碰 ``_ensure_api_key``。成立時 envelope 才能取兩條路的較大者。
    """
    h = _odoo()
    branch = h.one((n for n in ast.walk(initialize) if isinstance(n, ast.If)
                    and h.calls(n.test, '_is_owner_setup_done')), 'owner branch')
    existing = ast.Module(body=branch.body, type_ignores=[])
    fresh = ast.Module(body=branch.orelse, type_ignores=[])
    if len(h.calls(existing, '_ensure_api_key')) != 1 or any(
            h.calls(existing, n) for n in ('_setup_owner', '_login', '_create_api_key')):
        raise ValueError('owner 已存在的分支必須恰好呼叫一次 _ensure_api_key，且不登入、不建 owner／key')
    if h.calls(fresh, '_ensure_api_key') or any(
            len(h.calls(fresh, n)) != 1 for n in ('_setup_owner', '_login', '_create_api_key')):
        raise ValueError('owner 尚未建立的分支必須各呼叫一次 _setup_owner／_login／_create_api_key')
    outside = [c for c in h.calls(initialize, '_ensure_api_key') + h.calls(initialize, '_login')
               if not any(c is n for n in ast.walk(branch))]
    if outside:
        raise ValueError('_ensure_api_key／_login 不可出現在 owner 分支之外')


def _ensure_seconds(op):
    """``_ensure_api_key`` 的最壞耗時：恰好一次 exec_in_pod，timeout 必須是模組常數。"""
    h = _odoo()
    fn = h.definition(op, 'N8nInitService._ensure_api_key')
    call = h.one(h.calls(fn, 'exec_in_pod'), 'ensure exec')
    if h.calls(fn, '_node_request'):
        raise ValueError('_ensure_api_key 不可另打 node request')
    timeout = h.keyword(call, 'timeout')
    if not isinstance(timeout, ast.Name):
        raise ValueError('ensure exec 的 timeout 必須是模組常數')
    return h.constant(op, timeout.id)


def structure_fingerprints(trees):
    h = _odoo()
    result = {}
    for path, names in STRUCTURES.items():
        for name in names:
            node = copy.deepcopy(h.definition(trees[path], name))
            if path == KUBECTL and name == 'KubernetesService._run_command':
                timeout = h.timeout_default(node)
                h.positive_int(timeout, 'kubectl default timeout')
                timeout.value = '<kubectl default timeout>'
            if path == OPERATOR and name == 'N8nInitService._node_request':
                timeout = h.timeout_default(node)
                h.positive_int(timeout, 'node request default timeout')
                timeout.value = '<node request default timeout>'
            if path == OPERATOR and name == 'N8nInitService._wait_for_ready':
                timeout = h.keyword(h.one(h.calls(node, '_node_request'), name), 'timeout')
                h.positive_int(timeout, 'readiness request timeout')
                timeout.value = '<readiness request timeout>'
            if path == OPERATOR and name == 'N8nInitService.initialize':
                _, count, seconds, _ = _restart_loop(node)
                h.positive_int(count, 'restart 次數')
                h.positive_int(seconds, 'restart 間隔')
                count.value = '<restart attempts>'
                seconds.value = '<restart delay>'
            data = json.dumps(h.shape(node), ensure_ascii=True, separators=(',', ':'))
            result[f'{path}:{name}'] = hashlib.sha256(data.encode()).hexdigest()
    return result


def _node_seconds(h, func, default):
    total = 0
    for call in h.calls(func, '_node_request'):
        explicit = [k.value for k in call.keywords if k.arg == 'timeout']
        total += h.positive_int(explicit[0], 'node request timeout') if explicit else default
    return total


def check(root):
    """回傳違規清單；缺來源、解析失敗、結構漂移皆不得回空清單。"""
    prefix = 'init-n8n-budget-ladder：'
    try:
        h = _odoo()
        trees = {path: ast.parse((Path(root) / path).read_text(), filename=path) for path in SOURCES}
        client = trees[CLIENT]
        connect = h.constant(client, 'INIT_N8N_CONNECT_TIMEOUT')
        read = h.constant(client, 'INIT_N8N_READ_TIMEOUT')
        init = h.definition(client, 'PaaSOperatorClient.init_n8n')
        wire = h.keyword(h.one(h.calls(init, '_request'), 'client init_n8n _request'), 'timeout')
        if not isinstance(wire, ast.Tuple) or [ast.dump(n) for n in wire.elts] != [
            ast.dump(ast.Name(id='INIT_N8N_CONNECT_TIMEOUT', ctx=ast.Load())),
            ast.dump(ast.Name(id='INIT_N8N_READ_TIMEOUT', ctx=ast.Load())),
        ]:
            raise ValueError('client timeout wiring 必須是專屬 (CONNECT, READ) tuple，不可綁回 HELM 或錯接')
        if connect != 120:
            raise ValueError('init connect 必須維持原有效 120s，不可跟著 read 延長')
        fingerprints = structure_fingerprints(trees)
        baseline = json.loads((Path(root) / BASELINE).read_text())
        if fingerprints != baseline:
            changed = sorted(k for k in fingerprints.keys() | baseline.keys()
                             if fingerprints.get(k) != baseline.get(k))
            raise ValueError('結構變動，budget 算式失去鑑別力；重新核對而非盲目刷新指紋：' + ', '.join(changed))

        # 上面的結構鎖確保：每個步驟的呼叫次數固定、沒有額外迴圈，下列乘法才成立。
        op = trees[OPERATOR]
        kube = trees[KUBECTL]
        kubectl = h.positive_int(h.timeout_default(h.definition(
            kube, 'KubernetesService._run_command')), 'kubectl default timeout')
        node = h.positive_int(h.timeout_default(h.definition(
            op, 'N8nInitService._node_request')), 'node request default timeout')

        def kubectl_calls(path_tree, name):
            return len(h.calls(h.definition(path_tree, name), '_run_command'))

        discover = h.definition(op, 'N8nInitService._get_n8n_pod')
        selectors = h.one((n.value for n in discover.body if isinstance(n, ast.Assign)
                           and any(isinstance(t, ast.Name) and t.id == 'selectors' for t in n.targets)),
                          'selectors')
        if not isinstance(selectors, ast.List):
            raise ValueError('selectors 不再是可計數的 list')
        discovery = kubectl * len(selectors.elts) * len(h.calls(discover, 'get_pod_name')) \
            * kubectl_calls(kube, 'KubernetesService.get_pod_name')

        wait = h.definition(op, 'N8nInitService._wait_for_ready')
        ready_exec = h.positive_int(h.keyword(h.one(h.calls(wait, '_node_request'), 'ready request'),
                                              'timeout'), 'ready request')
        retries = h.constant(op, 'MAX_READY_RETRIES')
        delay = h.constant(op, 'READY_RETRY_DELAY')
        sleep_condition = h.one((n for n in ast.walk(wait) if isinstance(n, ast.If)
                                 and h.calls(n, 'sleep')), 'sleep condition')
        condition = sleep_condition.test
        if not isinstance(condition, ast.Compare) or not isinstance(condition.comparators[0], ast.BinOp):
            raise ValueError('sleep 條件不是可解析的比較／減法')
        omitted = h.positive_int(condition.comparators[0].right, '最後省略的 sleep')
        readiness = retries * ready_exec + (retries - omitted) * delay

        _owner_branches(h.definition(op, 'N8nInitService.initialize'))
        owner_check = sum(_node_seconds(h, h.definition(op, name), node) for name in OWNER_CHECK)
        fresh = sum(_node_seconds(h, h.definition(op, name), node) for name in FRESH_STEPS)
        existing = _ensure_seconds(op)

        # #1471：建 key 前先列出並刪掉同名舊 key。列出 1 次（迴圈外）＋迴圈內最多
        # _MAX_KEY_DELETES 次刪除；兩個請求都必須帶明確 timeout，迴圈必須切在常數上限。
        prune_fn = h.definition(op, 'N8nInitService._delete_api_keys_labelled')
        prune_loop = h.one((n for n in ast.walk(prune_fn) if isinstance(n, ast.For)), 'prune loop')
        in_loop = {id(c) for c in h.calls(prune_loop, '_node_request')}
        prune_calls = h.calls(prune_fn, '_node_request')
        if len(prune_calls) != 2 or len(in_loop) != 1:
            raise ValueError('prune 必須是迴圈外列出 1 次＋迴圈內刪除 1 個呼叫')
        cap = h.constant(op, '_MAX_KEY_DELETES')
        if not (isinstance(prune_loop.iter, ast.Subscript) and isinstance(prune_loop.iter.slice, ast.Slice)
                and isinstance(prune_loop.iter.slice.upper, ast.Name)
                and prune_loop.iter.slice.upper.id == '_MAX_KEY_DELETES'):
            raise ValueError('prune 迴圈必須切在 [:_MAX_KEY_DELETES]')
        per = []
        for call in prune_calls:
            explicit = [k.value for k in call.keywords if k.arg == 'timeout']
            if not explicit:
                raise ValueError('prune 的 node request 必須帶明確 timeout')
            value = explicit[0]
            if isinstance(value, ast.Name):
                value = h.constant(op, value.id)
            else:
                value = h.positive_int(value, 'prune timeout')
            per.append((id(call) in in_loop, value))
        prune = sum(v * (cap if looped else 1) for looped, v in per)

        secret = kubectl * kubectl_calls(op, 'N8nInitService._find_secret')
        patch = kubectl * kubectl_calls(kube, 'KubernetesService.patch_secret')
        deployment = kubectl * kubectl_calls(kube, 'KubernetesService.get_deployments')

        _, count, seconds, bound = _restart_loop(h.definition(op, 'N8nInitService.initialize'))
        attempts = h.positive_int(count, 'restart 次數')
        restart = attempts * kubectl * kubectl_calls(kube, 'KubernetesService.rollout_restart_deployment') \
            + min(attempts, h.positive_int(bound, 'restart sleep 上界')) * h.positive_int(seconds, 'restart 間隔')

        # prune 只在 owner 尚未建立那條路（_create_api_key 內）。
        branch = max(fresh + prune, existing)
        envelope = discovery + readiness + owner_check + branch + secret + patch + deployment + restart
        if read <= envelope:
            return [prefix + f'client read={read}s 必須 > 宣告子步 envelope={envelope}s '
                    f'（discovery={discovery}，readiness={readiness}，owner check={owner_check}，'
                    f'max(新建 owner {fresh}+prune {prune}，既有 owner exec {existing})={branch}，'
                    f'secret+patch+deployment={secret + patch + deployment}，restart={restart}）；'
                    '不是 server 硬上限']
        return []
    except (OSError, ValueError, SyntaxError, KeyError, AttributeError, TypeError, IndexError) as exc:
        return [prefix + f'無法驗證：{exc}（#1257 Q31；不可 fallback 成固定 531）']

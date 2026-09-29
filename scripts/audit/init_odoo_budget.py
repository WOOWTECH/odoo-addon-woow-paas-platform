"""#1152：核對 init_odoo 的同步 budget envelope 與 client connect/read 接線。

不是一般 Python cost analyzer：只接受已人工核對的呼叫／迴圈結構，結構指紋不同
即 fail-loud，不能直接重產 baseline 消紅。改結構時須重新核對本檔算式與 mutation。
指紋不鎖註解、docstring、type annotation；只有 kubectl default／readiness exec
的數字被正規化，其值仍從實際 AST 取出。其他數值從 module 宣告讀，不硬寫 805。

envelope 不含 threadpool 排隊、process 建立／收尾、DNS／網路，也不保證容器內
exec 被取消；requests read 是 inactivity timeout。外層缺口見 #1257／#1159。
"""
import ast
import copy
import hashlib
import json
from pathlib import Path


CLIENT = 'src/services/paas_operator.py'
OPERATOR = 'extra/paas-operator/src/services/odoo_init.py'
KUBECTL = 'extra/paas-operator/src/services/helm.py'
CONTRACT = 'extra/paas-operator/src/services/odoo_chart_contract.py'
API = 'extra/paas-operator/src/api/init.py'
BASELINE = 'scripts/audit/baselines/init-odoo-budget-structure.json'
SOURCES = (CLIENT, OPERATOR, KUBECTL, CONTRACT, API)

# 每個名字都是算式的前提：順序、呼叫次數、fallback 條件、timeout forwarding。
STRUCTURES = {
    CLIENT: ('PaaSOperatorClient.init_odoo', 'PaaSOperatorClient._request'),
    OPERATOR: (
        'OdooInitService.__init__', 'OdooInitService.initialize',
        'OdooInitService._get_odoo_pod', 'OdooInitService._wait_for_ready',
        'OdooInitService._update_module_list',
    ),
    KUBECTL: (
        'KubernetesService._run_command', 'KubernetesService.get_pod_name',
        'KubernetesService.get_pod', 'KubernetesService.exec_in_pod',
    ),
    CONTRACT: ('odoo_pod_selectors', 'validate_fallback_pod'),
    API: ('init_odoo',),
}


def one(nodes, label):
    nodes = list(nodes)
    if len(nodes) != 1:
        raise ValueError(f'{label} 預期唯一節點，實得 {len(nodes)}；須重新核對結構')
    return nodes[0]


def definition(tree, name):
    for part in name.split('.'):
        tree = one((n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))
                    and n.name == part), name)
    return tree


def positive_int(node, label):
    if not isinstance(node, ast.Constant) or type(node.value) is not int or node.value <= 0:
        raise ValueError(f'{label} 不是可解析的正整數 literal')
    return node.value


def constant(tree, name):
    assignment = one((n for n in tree.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)), name)
    if len(assignment.targets) != 1:
        raise ValueError(f'{name} 不可共用 assignment')
    return positive_int(assignment.value, name)


def calls(node, name):
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == name]


def keyword(call, name):
    return one((k for k in call.keywords if k.arg == name), name).value


def timeout_default(node):
    defaults = dict(zip((a.arg for a in node.args.args[-len(node.args.defaults):]), node.args.defaults))
    return defaults['timeout']


def shape(node):
    """跨 Python 3.11／3.12 穩定的 AST 結構；排除不影響執行的文字／型別。"""
    if isinstance(node, list):
        return [shape(n) for n in node]
    if not isinstance(node, ast.AST):
        return node
    fields = []
    for key, value in ast.iter_fields(node):
        if key in ('type_comment', 'type_params', 'annotation', 'returns'):
            continue
        if key == 'body' and isinstance(node, ast.FunctionDef) and ast.get_docstring(node):
            value = value[1:]
        fields.append((key, shape(value)))
    return [type(node).__name__, fields]


def structure_fingerprints(trees):
    result = {}
    for path, names in STRUCTURES.items():
        for name in names:
            node = copy.deepcopy(definition(trees[path], name))
            if path == KUBECTL and name == 'KubernetesService._run_command':
                timeout = timeout_default(node)
                positive_int(timeout, 'kubectl default timeout')
                assert isinstance(timeout, ast.Constant)
                timeout.value = '<kubectl default timeout>'
            if path == OPERATOR and name == 'OdooInitService._wait_for_ready':
                timeout = keyword(one(calls(node, 'exec_in_pod'), name), 'timeout')
                positive_int(timeout, 'readiness exec timeout')
                assert isinstance(timeout, ast.Constant)
                timeout.value = '<readiness exec timeout>'
            data = json.dumps(shape(node), ensure_ascii=True, separators=(',', ':'))
            result[f'{path}:{name}'] = hashlib.sha256(data.encode()).hexdigest()
    return result


def check(root):
    """回傳違規清單；缺來源、解析失敗、結構漂移皆不得回空清單。"""
    prefix = 'init-odoo-budget-ladder：'
    try:
        trees = {path: ast.parse((Path(root) / path).read_text(), filename=path) for path in SOURCES}
        client = trees[CLIENT]
        connect = constant(client, 'INIT_ODOO_CONNECT_TIMEOUT')
        read = constant(client, 'INIT_ODOO_READ_TIMEOUT')
        init = definition(client, 'PaaSOperatorClient.init_odoo')
        wire = keyword(one(calls(init, '_request'), 'client init_odoo _request'), 'timeout')
        if not isinstance(wire, ast.Tuple) or [ast.dump(n) for n in wire.elts] != [
            ast.dump(ast.Name(id='INIT_ODOO_CONNECT_TIMEOUT', ctx=ast.Load())),
            ast.dump(ast.Name(id='INIT_ODOO_READ_TIMEOUT', ctx=ast.Load())),
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

        # 上面的結構鎖確保：selector 各查一次、index=1 才多讀一次 metadata；沒有額外迴圈。
        selector = definition(trees[CONTRACT], 'odoo_pod_selectors')
        selectors = one((n for n in selector.body if isinstance(n, ast.Return)), 'selector return').value
        if not isinstance(selectors, ast.Tuple):
            raise ValueError('selectors 不再是可計數的 tuple')
        selector_count = len(selectors.elts)
        discover = definition(trees[OPERATOR], 'OdooInitService._get_odoo_pod')
        kubectl_seconds = positive_int(timeout_default(definition(
            trees[KUBECTL], 'KubernetesService._run_command')), 'kubectl default timeout')
        discovery = kubectl_seconds * (selector_count * len(calls(discover, 'get_pod_name'))
                                       + len(calls(discover, 'get_pod')))
        wait = definition(trees[OPERATOR], 'OdooInitService._wait_for_ready')
        exec_seconds = positive_int(keyword(one(calls(wait, 'exec_in_pod'), 'ready exec'), 'timeout'), 'ready exec')
        retries = constant(trees[OPERATOR], 'MAX_READY_RETRIES')
        delay = constant(trees[OPERATOR], 'READY_RETRY_DELAY')
        # 結構鎖保證 range(MAX_READY_RETRIES)，且只有 attempt < MAX_READY_RETRIES - 1 時 sleep。
        sleep_condition = one((n for n in ast.walk(wait) if isinstance(n, ast.If)
                               and calls(n, 'sleep')), 'sleep condition')
        condition = sleep_condition.test
        if not isinstance(condition, ast.Compare) or not isinstance(condition.comparators[0], ast.BinOp):
            raise ValueError('sleep 條件不是可解析的比較／減法')
        omitted_sleeps = positive_int(condition.comparators[0].right, '最後省略的 sleep')
        update = constant(trees[OPERATOR], 'MODULE_UPDATE_TIMEOUT')
        envelope = discovery + retries * exec_seconds + (retries - omitted_sleeps) * delay + update
        if read <= envelope:
            return [prefix + f'client read={read}s 必須 > 宣告子步 envelope={envelope}s '
                    f'（discovery={discovery}，exec={retries}×{exec_seconds}，'
                    f'sleep={retries - omitted_sleeps}×{delay}，update={update}）；不是 server 硬上限']
        return []
    except (OSError, ValueError, SyntaxError, KeyError, AttributeError, TypeError) as exc:
        return [prefix + f'無法驗證：{exc}（#1152；不可 fallback 成固定 805）']

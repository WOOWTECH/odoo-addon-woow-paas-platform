#!/usr/bin/env python3
"""#524 ③：workspace-scoped model 必須有「對 Internal User 生效」的 record rule。

## 這道 gate 在問什麼

不是「你有沒有寫 rule」，而是——

    **凡是宣告了 `workspace_id`、而且 ACL 對 `base.group_user` 開著的 model，
    是否都存在至少一條「對 Internal User 實際生效」的 active `ir.rule`。**

#524 就是這條判準的反例：`smart_home` 有 `workspace_id`、ACL 給 `base.group_user`
`1,1,1,0`，而 `ir_rules.xml` 對它命中 **0**。實測（真 HTTP、只掛 `base.group_user`
的兩個租戶帳號）三軸皆成立：跨租戶讀得到（含 `tunnel_id` 明文）、改得動、
還能繞過下單流程自建。

## 為什麼需要機械化

同一個形態要手工套用多次、分屬不同單、不同時間點、可能不同人：
`security_access` 已拆、`cloud_service`（#676／#519）已拆、`smart_home`（#524）這次拆。
本 repo 對手工鏡射有前科（#415：兩套平行邏輯已漏鏡射兩次）。三個 model 共用同一個
`subscription_lifecycle_mixin`，本就是同一族——下一個新 model 忘了、或某次拆分漏一條，
應該由 CI 紅燈告知，而不是等下一次安全調查。

## 兩個關鍵的「不要做錯」

### 1. 欄位比對必須精確，不能子字串

`agent_session` 宣告的是 **`primary_workspace_id`**。`grep 'workspace_id = fields'`
會**子字串命中**它 ⇒ 把一個沒有 workspace 歸屬語意的 model 拉進來誤判。
本 gate 走 `ast`，比對的是**賦值目標的完整識別字**（`node.targets[0].id == 'workspace_id'`），
`primary_workspace_id` 不會命中。

> ⚠ 與 issue 留言原文「用 **registry** 欄位名精確比對」的偏差，寫在這裡不藏：
> registry 要跑起來的 Odoo ＋ DB，這道 gate 要能在 CI 無 DB 的情況下跑，故改用
> AST。**精確性的要求被滿足了**（完整識別字比對、且會沿著 repo 內的 `_inherit`
> 鏈把 mixin 的欄位併進來）；沒被滿足的是「執行期才長出來的欄位」——見下方盲區②。

### 2. 「至少有一條 ir.rule」是**假綠**判準

Odoo core `ir_rule.py::_get_rules` 的 SQL 只選 **global rule（無 groups）**
或**使用者所屬 group 的 rule**。所以一條只綁 `base.group_system` 的 rule
對 `base.group_user` **完全不生效**——model 上「有 rule」，租戶卻不受任何列過濾。
本 gate 因此要求那條 rule 必須是 global，或它的 groups 含 `base.group_user`；
另外 `active="False"` 的、以及四個 `perm_*` 全 False 的（不參與任何方向）都不算數。

## 🔴 已知盲區（**刻意同時寫在本 docstring 與 gate 的實際輸出裡**）

理由：**通過的檢查會壓抑繼續找的動機**。只在文件裡聲明邊界等於沒聲明——看到綠燈
的人不會去翻文件。所以每一次執行（不論紅綠）都會把下面這幾條印出來。

1. **只涵蓋宣告了 `workspace_id` 的 model。** 沒有該欄位者不在範圍，例如
   `woow_paas_platform.change_plan_wizard`（`TransientModel`，只有
   `cloud_service_id` / `smart_home_id` / `*_plan_id`，ACL 是
   `base.group_user,1,1,1,0` 且無 rule）。它**不會**被本 gate 抓到。
2. **只看靜態宣告。** 執行期才加上的欄位（`_inherit` 到本 repo 之外的 model 再由
   該 model 提供 `workspace_id`、或 `_fields` 動態注入）看不到。
3. **只看本 repo 的 ACL CSV 與 ir.rule XML。** 由相依模組授予的 ACL、或由相依模組
   宣告的 ir.rule 不在視野內。為了讓這條盲區不會靜默擴大，gate 會 fail-loud：
   若 `src/security/` 之外還出現第二個 `model="ir.rule"` 來源檔，直接 exit 2。
4. **方向覆蓋只是 advisory。** 主判準是 model 層級（「有沒有任何一條生效的 rule」，
   即 issue #524 留言 4640 的原文判準）。逐方向覆蓋（ACL 開了 unlink 但沒有任何
   適用 rule 帶 `perm_unlink`）另列一段**不阻擋**——今天 `project.project` 就落在
   那裡，那是 PR #705 覆核 F4 已追蹤的既有缺口，不屬 #524。用 `--strict` 可把
   advisory 一併升為阻擋（等那條修好後即可在 CI 打開）。

## 用法

    python3 scripts/audit/workspace_scoped_rule_gate.py [--repo-root PATH] [--strict]

exit 0 = 通過；1 = 有違規；2 = gate 自身的前提被打破（見盲區③）。
"""
from __future__ import annotations

import argparse
import ast
import csv
import os
import re
import sys
import xml.etree.ElementTree as ET

#: 觸發本 gate 的欄位名（**完整識別字**比對，不是子字串）。
WORKSPACE_FIELD = 'workspace_id'

#: 「Internal User」那個群組的 xmlid。
INTERNAL_USER_GROUP = 'base.group_user'

PERMS = ('perm_read', 'perm_write', 'perm_create', 'perm_unlink')
ACL_PERM_COLUMNS = ('perm_read', 'perm_write', 'perm_create', 'perm_unlink')

_REF_RE = re.compile(r"ref\(\s*['\"]([^'\"]+)['\"]\s*\)")


# --------------------------------------------------------------------------- #
#  model / 欄位（AST，精確識別字）                                             #
# --------------------------------------------------------------------------- #
def _is_fields_call(node):
    """`fields.Char(...)` / `fields.Many2one(...)` 這種呼叫。"""
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == 'fields')


def _string_or_list(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.List, ast.Tuple)):
        return [e.value for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []


def collect_models(py_files):
    """回傳 ``{model_name: {'fields': set, 'inherit': [...], 'source': path}}``。

    同一個 model 名可能出現在多個檔案（`_inherit` 擴充），欄位取聯集。
    """
    models = {}
    for path in py_files:
        try:
            with open(path, encoding='utf-8') as fh:
                tree = ast.parse(fh.read(), filename=path)
        except SyntaxError:
            continue
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            name = None
            inherit = []
            declared = set()
            for stmt in cls.body:
                if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
                    continue
                target = stmt.targets[0]
                if not isinstance(target, ast.Name):
                    continue
                if target.id == '_name':
                    got = _string_or_list(stmt.value)
                    name = got[0] if got else None
                elif target.id == '_inherit':
                    inherit = _string_or_list(stmt.value)
                elif _is_fields_call(stmt.value):
                    # 🔴 完整識別字：`primary_workspace_id` != `workspace_id`
                    declared.add(target.id)
            if name is None and len(inherit) == 1:
                name = inherit[0]          # 純擴充既有 model
            if name is None:
                continue
            entry = models.setdefault(
                name, {'fields': set(), 'inherit': [], 'sources': []})
            entry['fields'] |= declared
            entry['inherit'] += [i for i in inherit if i != name]
            entry['sources'].append(os.path.relpath(path))
    return models


def resolve_inherited_fields(models):
    """把 repo 內 `_inherit` 鏈上的欄位併進來（mixin 提供 `workspace_id` 的情況）。"""
    resolved = {}

    def walk(name, seen):
        if name in resolved:
            return resolved[name]
        if name in seen or name not in models:
            return set()
        seen.add(name)
        acc = set(models[name]['fields'])
        for parent in models[name]['inherit']:
            acc |= walk(parent, seen)
        resolved[name] = acc
        return acc

    for name in models:
        walk(name, set())
    return resolved


# --------------------------------------------------------------------------- #
#  ACL / ir.rule                                                               #
# --------------------------------------------------------------------------- #
def model_xmlid_suffix(model_name):
    """Odoo 慣例：`ir.model` 的 xmlid 是 ``model_<模型名把 . 換成 _>``。"""
    return 'model_' + model_name.replace('.', '_')


def build_xmlid_index(model_names):
    """``{'model_project_project': 'project.project', ...}``（去掉模組前綴後比對）。"""
    return {model_xmlid_suffix(n): n for n in model_names}


def parse_acl(csv_path, xmlid_index):
    """回傳 ``{model_name: set(已授予的方向)}``（只看 `base.group_user` 的列）。

    另回傳無法解析成 model 的 xmlid 列，讓它們**列出來**而不是被靜默丟掉。
    """
    granted = {}
    unresolved = []
    with open(csv_path, encoding='utf-8') as fh:
        for row in csv.DictReader(fh):
            if (row.get('group_id:id') or '').strip() != INTERNAL_USER_GROUP:
                continue
            raw = (row.get('model_id:id') or '').strip()
            key = raw.split('.')[-1]
            model = xmlid_index.get(key)
            if not model:
                unresolved.append(raw)
                continue
            perms = {p for p in ACL_PERM_COLUMNS
                     if (row.get(p) or '0').strip() in ('1', 'True', 'true')}
            granted.setdefault(model, set())
            granted[model] |= perms
    return granted, unresolved


def parse_rules(xml_path, xmlid_index):
    """回傳 ``[{model, xmlid, groups, perms, active}]``（只收 `ir.rule` record）。"""
    rules = []
    root = ET.parse(xml_path).getroot()
    for rec in root.iter('record'):
        if rec.get('model') != 'ir.rule':
            continue
        entry = {'xmlid': rec.get('id'), 'model': None, 'groups': [],
                 'perms': set(), 'active': True, 'has_groups_field': False}
        for field in rec.findall('field'):
            fname = field.get('name')
            if fname == 'model_id':
                key = (field.get('ref') or '').split('.')[-1]
                entry['model'] = xmlid_index.get(key)
            elif fname == 'groups':
                entry['has_groups_field'] = True
                entry['groups'] = _REF_RE.findall(field.get('eval') or '')
            elif fname == 'active':
                entry['active'] = (field.get('eval') or '').strip() != 'False'
            elif fname in PERMS:
                if (field.get('eval') or '').strip() == 'True':
                    entry['perms'].add(fname)
        # perm_* 未宣告時 Odoo 的預設是 True；本 repo 一律顯式寫四個，
        # 但別假設——沒有任何 perm 欄位出現時，照框架預設補齊。
        declared_perms = {f.get('name') for f in rec.findall('field')}
        if not (declared_perms & set(PERMS)):
            entry['perms'] = set(PERMS)
        rules.append(entry)
    return rules


def rule_applies_to_internal_user(rule):
    """global rule（無 groups 欄位）或 groups 含 `base.group_user` 才對租戶生效。

    🔴 這正是「至少有一條 ir.rule」那個假綠判準要被取代的地方：只綁
    `base.group_system` 的 rule 在這裡回 False。
    """
    if not rule['active']:
        return False
    if not rule['perms']:
        return False
    if not rule['has_groups_field']:
        return True                      # global rule：對所有人 AND
    return INTERNAL_USER_GROUP in rule['groups']


# --------------------------------------------------------------------------- #
#  主檢查                                                                      #
# --------------------------------------------------------------------------- #
BLIND_SPOTS = (
    "只涵蓋**宣告了 `workspace_id`** 的 model。沒有該欄位者不在範圍——"
    "例如 `woow_paas_platform.change_plan_wizard`（TransientModel、ACL 對 "
    "base.group_user 是 1,1,1,0、無 rule）。本 gate 綠燈**不代表它安全**。",
    "只看**靜態宣告**（AST）。執行期才長出來的 `workspace_id` 看不到。",
    "只看**本 repo** 的 `ir.model.access.csv` 與 `ir_rules.xml`。"
    "相依模組授予的 ACL／宣告的 rule 不在視野內。",
    "**逐方向覆蓋只是 advisory**（`--strict` 可升為阻擋）。主判準是 model 層級。",
)


def run(repo_root, strict=False, out=sys.stdout):
    src = os.path.join(repo_root, 'src')
    security_dir = os.path.join(src, 'security')
    acl_csv = os.path.join(security_dir, 'ir.model.access.csv')
    rules_xml = os.path.join(security_dir, 'ir_rules.xml')

    # 盲區③ 的 fail-loud：確認本 repo 的 ir.rule 真的只有一個來源。
    other_sources = []
    for dirpath, _dirnames, filenames in os.walk(src):
        for fn in filenames:
            if not fn.endswith('.xml'):
                continue
            full = os.path.join(dirpath, fn)
            if os.path.abspath(full) == os.path.abspath(rules_xml):
                continue
            try:
                with open(full, encoding='utf-8') as fh:
                    if 'model="ir.rule"' in fh.read():
                        other_sources.append(os.path.relpath(full, repo_root))
            except OSError:
                continue
    if other_sources:
        print("GATE 前提被打破：`ir.rule` 出現在 %s 以外的檔案 —— %s\n"
              "本 gate 只解析單一來源，多來源會讓它靜默漏檢。請先更新 gate。"
              % (os.path.relpath(rules_xml, repo_root), ', '.join(other_sources)),
              file=out)
        return 2

    py_files = []
    for sub in ('models', 'wizard', 'controllers', 'services'):
        base = os.path.join(src, sub)
        for dirpath, _dirnames, filenames in os.walk(base):
            py_files += [os.path.join(dirpath, f) for f in filenames
                         if f.endswith('.py')]

    models = collect_models(py_files)
    fields_by_model = resolve_inherited_fields(models)
    xmlid_index = build_xmlid_index(models.keys())
    acl_granted, acl_unresolved = parse_acl(acl_csv, xmlid_index)
    rules = parse_rules(rules_xml, xmlid_index)

    workspace_models = sorted(
        m for m, flds in fields_by_model.items() if WORKSPACE_FIELD in flds)

    in_scope = [m for m in workspace_models if acl_granted.get(m)]
    no_acl = [m for m in workspace_models if not acl_granted.get(m)]

    violations = []
    direction_gaps = []
    for model in in_scope:
        applicable = [r for r in rules
                      if r['model'] == model and rule_applies_to_internal_user(r)]
        if not applicable:
            any_rule = [r for r in rules if r['model'] == model]
            violations.append((model, any_rule))
            continue
        covered = set().union(*[r['perms'] for r in applicable])
        for perm in sorted(acl_granted[model]):
            if perm not in covered:
                direction_gaps.append((model, perm))

    # ------------------------------------------------------------------ #
    #  輸出                                                              #
    # ------------------------------------------------------------------ #
    print("workspace-scoped record rule gate（#524）", file=out)
    print("判準：宣告 `workspace_id` × ACL 開 `base.group_user` × "
          "存在對 Internal User 生效的 active ir.rule", file=out)
    print("", file=out)
    print("涵蓋範圍內的 model（%d）：%s"
          % (len(in_scope), ', '.join(in_scope) or '（無）'), file=out)
    if no_acl:
        print("有 workspace_id 但本 repo 的 ACL 未對 base.group_user 開放（%d，"
              "不檢查、列出以免靜默漏掉）：%s"
              % (len(no_acl), ', '.join(sorted(no_acl))), file=out)
    if acl_unresolved:
        print("ACL 中無法對應到本 repo model 的列（%d，列出以免靜默漏掉）：%s"
              % (len(acl_unresolved), ', '.join(sorted(set(acl_unresolved)))),
              file=out)
    print("", file=out)
    print("⚠ 本 gate 的已知盲區（綠燈不等於這幾類也安全）：", file=out)
    for i, spot in enumerate(BLIND_SPOTS, 1):
        print("  %d. %s" % (i, spot), file=out)
    print("", file=out)

    if direction_gaps:
        print("advisory —— ACL 開了某個方向，但沒有任何適用 rule 帶該 perm "
              "（Odoo 對「沒有適用 rule 的方向」是**不做列過濾**）：", file=out)
        for model, perm in direction_gaps:
            print("  • %s：ACL 給了 %s，適用 rule 皆無該 perm" % (model, perm),
                  file=out)
        print("", file=out)

    if violations:
        print("❌ 違規（%d）：" % len(violations), file=out)
        for model, any_rule in violations:
            if any_rule:
                detail = ("該 model 有 %d 條 ir.rule，但**沒有一條對 Internal "
                          "User 生效**（只綁 base.group_system／已停用／四個 "
                          "perm 全 False）：%s"
                          % (len(any_rule),
                             ', '.join(r['xmlid'] for r in any_rule)))
            else:
                detail = "該 model 在 ir_rules.xml 裡**一條 rule 都沒有**"
            print("  • %s —— %s" % (model, detail), file=out)
            print("    ⇒ 任何已登入的 Internal User 對這個 model 沒有任何列過濾"
                  "（跨租戶讀寫）。修法：在 src/security/ir_rules.xml 加一組"
                  "讀／寫分離的 rule，domain 用 "
                  "`('workspace_id.access_ids', 'any', [...])` 把 user_id 與 "
                  "role 綁在同一列。", file=out)
        return 1

    if strict and direction_gaps:
        print("❌ --strict：上面的 advisory 視為違規。", file=out)
        return 1

    print("✅ 通過：範圍內每個 model 都有至少一條對 Internal User 生效的 rule。",
          file=out)
    print("   （再讀一次上面的盲區——通過的檢查會壓抑繼續找的動機。）", file=out)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument(
        '--repo-root',
        default=os.path.abspath(
            os.path.join(os.path.dirname(__file__), '..', '..')))
    parser.add_argument('--strict', action='store_true',
                        help='把 advisory 的逐方向缺口也視為違規')
    args = parser.parse_args(argv)
    return run(args.repo_root, strict=args.strict)


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""repo-lint：把「假綠家族」pitfall 轉成機械 gate 的靜態檢查工具。

十四個 check（每條都對應一個已翻車的 pitfall，訊息內附文件指針）：

1. controller-sudo   src/controllers/*.py 的 .sudo( 呼叫點數量對 baseline 白名單；
                     新增 sudo() 必須有意識地更新 baseline（附 justification）。
                     依據 docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md
2. owl-template      src/static/src/**/*.xml 的 OWL template 語法陷阱：
                     component（大寫開頭 tag）上用 t-att / t-att-* / t-attf-*、
                     t-on-* 帶不存在的按鍵 modifier（只支援 .stop/.prevent/.self/.capture）。
                     依據 docs/tracking/pitfalls/owl-template-crash-class-syntax.md
3. data-noupdate     src/data/*.xml 新增 noupdate="1" 的檔案要對 baseline；
                     noupdate 是 sticky 的，fresh install 綠 ≠ 升級路徑綠。
                     依據 docs/tracking/pitfalls/seed-noupdate-sticky-template-links.md
4. reuse-values      src/**/*.py 出現 reuse_values=True 即 fail；helm --reuse-values 讓
                     chart 升版新增預設永不生效（既存 release 升級靜默漏，#411 Gap 2）。
                     依據 docs/decisions/patterns/helm-reset-values-unified-pipeline.md
5. seed-topology     純靜態 parse src/data/subscription_{products,template_links}*.xml，
                     算六個拓撲數對 baseline。改 seed 當下就被擋、附「持有計數的位置」
                     清單，取代「三週後 CI 才紅、紅在無關的 test 名下」（#433）。
                     依據 docs/decisions/patterns/seed-topology-invariants.md
6. test-env-restore  src/tests/**/*.py 出現 addCleanup(os.environ.pop, ...) 即 fail；
                     那是無條件刪除而非還原原值，會洗掉外部注入的 env，害字母序在後
                     的測試 fail-closed raise（單獨跑綠／全套跑紅，#436）。
                     正解 patch.dict(os.environ, {...})（start/stop 或 context manager）。
7. capability-gate   前端能力 gate 的雙向守線（#460 S2）：
                     (a) src/static/src/**/*.{js,xml} 不得用 slug 做集合成員判斷
                         （X.includes(t.slug) / SLUGS.indexOf(x)）——那是自建第二判準；
                     (b) Reset 密碼按鈕的 t-if 鏈必須追得到 has_admin_gui，
                         不論 getter 叫什麼名字（鎖「唯一的對形態」，擋任何第三判準）。
                     依據 docs/decisions/patterns/capability-gate-single-source-field.md
8. conceal-instances src/**/*.py（除 src/tests/）不得用 `template_id.is_hidden` 判**既有
                     service** 的可見性——實例層判準是 template_id.conceal_instances；
                     is_hidden 只剩曝光層語意。照抄舊 gate ＝「暫時下架一個商品」會把租戶
                     手上 state=running 的服務一起變成 Service not found（#471／#464）。
                     依據 docs/decisions/patterns/hidden-template-presentation-chain.md
9. reset-budget-ladder  reset 密碼的三層 timeout 預算必須嚴格外大於內、且算式自洽
                     （前端 JS 90s > 平台 client 75s > operator hard cap 65s，後者
                     ＝2×helm --timeout ＋ CLI 開銷 ＋ exec ＋ cleanup ＋ slack）。
                     #461／F-449-3 的根因就是三個預算彼此不知道對方；`--atomic` 失敗
                     還會吃第二次 --timeout（rollback wait），少算就在 rollback 途中
                     kill helm ⇒ release 卡 pending-rollback（真機實測）。
                     依據 docs/decisions/patterns/reset-async-budget-ladder.md
10. transition-anchor-utc  src/**（除 src/tests/）寫 deployed_at / last_upgraded_at
                     不得用 `datetime.now()`（本機時區）——Odoo Datetime 契約是 UTC
                     naive，非 UTC 伺服器上 age timeout 會靜默失效或誤殺，而容器時區
                     是 UTC 所以**測不出來**，只能靠 gate（#489）。
12. mixin-subscription-sudo  src/models/subscription_lifecycle_mixin.py 內不得裸讀
                     `self.subscription_id`——那是對 sh.subscription.subscription 的
                     relation traversal，而 workspace 擁有權不給 Odoo 群組，典型租戶
                     會在 env cache 落空時撞 AccessError（SH delete/provision、
                     CS update/delete 全中，#29）。統一走 `_sub()`（sudo）。
                     依據 docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md

11. assertlogs-numeric-level  src/tests/** 的 assertLogs/assertNoLogs 不得用字串
                     `'INFO'` 當 level——Odoo 的 addLevelName(RUNBOT=25, "INFO") 覆寫
                     了名稱→數值表，字串會解析成 25 ⇒ `_logger.info()`(20) 全被擋掉：
                     assertLogs 恆紅（且紅得像被測 code 掉了 log）、assertNoLogs 恆綠。
                     改傳數值 logging.INFO（#492）。

13. config-parameter-uninstall  res_config_settings.py 每個 `config_parameter=` 的 key
                     必須登記進 src/hooks.py 的 `_CONFIG_PARAMETERS`。Odoo 卸載 addon
                     **不會**清 ir.config_parameter，漏登記＝卸載後留下孤兒設定（重裝
                     時被讀到、值卻是上一輪的）。CLAUDE.md 一直明載這條義務卻零機械
                     保護——而「少寫一行」正是 review 最抓不到的形態（#501）。
                     只查單向（宣告了沒登記）；反向不擋，清單裡有歷史遺留與非 settings
                     來源的 key。
14. duplicate-class-methods  同一個 class body 內重複定義同名 method 即 fail。Python
                     靜默採用後者、前者整段失效（無錯誤、無警告、無 log），在 Odoo model
                     上等於「新加的 override 把既有守衛整個關掉」。#501 PR 實例：新增的
                     `write()` 覆蓋掉 #279 擋 workspace 重指派的租戶隔離守衛，針對性測試
                     涵蓋不到、只有全套會紅。測試檔同樣涵蓋（重複 test method ＝前一個
                     永不執行的無聲假綠）。

用法：
  scripts/audit/repo-lint.py --all                # 全 repo 掃描（CI / merge 前手動）
  scripts/audit/repo-lint.py --update-baseline    # 接受現況為新 baseline
  scripts/audit/repo-lint.py --hook               # PostToolUse hook 模式（stdin 吃 hook JSON，只掃該檔）

hook 模式輸出 {"decision":"block","reason":...} 讓 model 立即自我修正；CLI 模式違規 exit 1。
Fail-open：hook 模式下任何內部錯誤 → exit 0，絕不擋開發。
"""
import ast
import glob
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASELINE_DIR = os.path.join(SCRIPT_DIR, "baselines")
SUDO_BASELINE = os.path.join(BASELINE_DIR, "controller-sudo.txt")
NOUPDATE_BASELINE = os.path.join(BASELINE_DIR, "data-noupdate.txt")
SEED_TOPOLOGY_BASELINE = os.path.join(BASELINE_DIR, "seed-topology.txt")

SUDO_DOC = "docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md"
OWL_DOC = "docs/tracking/pitfalls/owl-template-crash-class-syntax.md"
NOUPDATE_DOC = "docs/tracking/pitfalls/seed-noupdate-sticky-template-links.md"
REUSE_VALUES_DOC = "docs/decisions/patterns/helm-reset-values-unified-pipeline.md"
SEED_TOPOLOGY_DOC = "docs/decisions/patterns/seed-topology-invariants.md"
CAPABILITY_GATE_DOC = "docs/decisions/patterns/capability-gate-single-source-field.md"
HIDDEN_CHAIN_DOC = "docs/decisions/patterns/hidden-template-presentation-chain.md"
RESET_LADDER_DOC = "docs/decisions/patterns/reset-async-budget-ladder.md"
ASSERTLOGS_LEVEL_DOC = (
    "docs/tracking/pitfalls/assertlogs-string-level-shadowed-by-odoo.md")

# #461 reset 預算階梯：三層各自的常數位置（跨 repo 內兩條發版線，故一定要跨檔算）。
RESET_LADDER_FRONTEND = "src/static/src/paas/services/cloud_service.js"
RESET_LADDER_PLATFORM = "src/services/paas_operator.py"
RESET_LADDER_OPERATOR = "extra/paas-operator/src/api/releases.py"
RESET_LADDER_HELM = "extra/paas-operator/src/services/helm.py"
RESET_LADDER_FILES = (
    RESET_LADDER_FRONTEND, RESET_LADDER_PLATFORM,
    RESET_LADDER_OPERATOR, RESET_LADDER_HELM,
)

# #471：既有實例的隱藏判準（實例層）vs 只擋上架曝光的舊旗標（曝光層）。
CONCEAL_FIELD = "conceal_instances"
CONCEAL_WRONG_FIELD = "is_hidden"

# #29：mixin 內部的 subscription traversal 唯一入口是 `_sub()`（sudo）。
MIXIN_SUDO_FILE = "src/models/subscription_lifecycle_mixin.py"

OWL_ALLOWED_MODIFIERS = {"stop", "prevent", "self", "capture", "synthetic"}

# 前端能力 gate 的唯一判準欄位（後端 payload 直接鏡射 template 欄位，#460 S1）。
ADMIN_GUI_FIELD = "has_admin_gui"
# 「哪顆按鈕」的 XML 端錨點：handler 名比 t-if 的 getter 名穩定（getter 可以更名，
# 但按鈕綁的 action 更名就是改功能本身）。
ADMIN_GUI_BUTTON_HANDLER = "resetAdminPassword"
# t-if 表達式裡不算 component member 的字（OWL 運算子 / literal / 內建 namespace）。
OWL_EXPR_NON_MEMBERS = {
    "and", "or", "not", "in", "true", "false", "null", "undefined",
    "props", "state", "this", "new", "typeof", "env",
}

# seed 拓撲數字一變，這些位置就要一起被檢視。刻意記「檔案＋具名位置」而非行號
# ——行號會隨無關的編輯腐化，符號名不會。
SEED_TOPOLOGY_DEPENDENTS = (
    "src/tests/test_plan_group_keys_368.py：_seeded_link_refs() 對 links.xml 做集合"
    "等值（拓撲成長不必改），但若這次是「刻意隱藏已掛 template 的 base」，"
    "必須顯式加進同檔的 _INTENTIONALLY_HIDDEN 並在 PR 說明理由。",
    "src/tests/test_subscription_tax_clear_366.py："
    "TestSeedProductCorpusIntact._MIN_SEEDED_PRODUCTS（backstop 下界，seed 成長時調高）。",
    "src/tests/test_plan_variants_generated.py：GENERATED 檔，不要手改——"
    "要改請改 scripts/seed/generate_plan_variants.py 的 _TESTS_TEMPLATE 後重跑 generator。",
    "scripts/seed/test_generate_plan_variants.py："
    "TestClassify.test_counts_per_family 的 family 分佈（每 family 幾個變體）。",
    "docs/plans/2026-Q3/2026-07-19-cloud-services-220-manifest.md："
    "變體的單一事實源，改價／增減變體從這裡開始。",
)

_CYCLE_SUFFIX_RE = re.compile(r"_(monthly|yearly)$")


def repo_root():
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.path.dirname(os.path.dirname(SCRIPT_DIR))


def read_baseline_pairs(path):
    """讀 `value<TAB>relpath` 形式的 baseline，回 dict。"""
    result = {}
    if not os.path.exists(path):
        return result
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t", 1)
            if len(parts) == 2:
                result[parts[1]] = parts[0]
    return result


# ---------------------------------------------------------------- checks

def check_controller_sudo(root, only_file=None):
    """回傳 violations list；baseline 是每檔 .sudo( 次數上限。"""
    baseline = {k: int(v) for k, v in read_baseline_pairs(SUDO_BASELINE).items()}
    violations = []
    files = [only_file] if only_file else sorted(
        glob.glob(os.path.join(root, "src/controllers/*.py")))
    for fp in files:
        if not os.path.exists(fp):
            continue
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        with open(fp, encoding="utf-8", errors="replace") as f:
            count = len(re.findall(r"\.sudo\(", f.read()))
        allowed = baseline.get(rel, 0)
        if count > allowed:
            violations.append(
                "%s：.sudo( 呼叫點 %d 個 > baseline %d 個。controller layer 的 sudo() 會繞過 ir.rule，"
                "是 B1-class data leak 的主要來源（見 %s）。若這個新 sudo() 有正當理由"
                "（如 platform public-read），請補 HttpCase cross-user negative test，"
                "並跑 scripts/audit/repo-lint.py --update-baseline 把 justification 寫進 commit message。"
                % (rel, count, allowed, SUDO_DOC))
    return violations


def current_sudo_counts(root):
    counts = {}
    for fp in sorted(glob.glob(os.path.join(root, "src/controllers/*.py"))):
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        with open(fp, encoding="utf-8", errors="replace") as f:
            counts[rel] = len(re.findall(r"\.sudo\(", f.read()))
    return counts


def check_owl_template(root, only_file=None):
    violations = []
    files = [only_file] if only_file else sorted(
        glob.glob(os.path.join(root, "src/static/src/**/*.xml"), recursive=True))
    for fp in files:
        if not os.path.exists(fp):
            continue
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        try:
            tree = ET.parse(fp)
        except ET.ParseError as exc:
            violations.append("%s：XML parse error（%s）——連 well-formed 都沒過，Odoo 載入會炸。" % (rel, exc))
            continue
        for elem in tree.iter():
            tag = elem.tag
            if not isinstance(tag, str) or "}" in tag:
                continue
            is_component = tag[:1].isupper()
            for attr in elem.attrib:
                if is_component and (attr == "t-att" or attr.startswith("t-att-") or attr.startswith("t-attf-")):
                    violations.append(
                        "%s：component <%s> 上用了 %s。OWL component 的 prop 直接吃 expression"
                        "（寫 name=\"expr\" 不加 t-att-），t-att- 只給 HTML element 用；"
                        "這會在 runtime 摧毀整個 OWL root → 全站白屏（三層測試都攔不到，見 %s）。"
                        % (rel, tag, attr, OWL_DOC))
                if attr.startswith("t-on-") and "." in attr:
                    mods = attr.split(".")[1:]
                    bad = [m for m in mods if m not in OWL_ALLOWED_MODIFIERS]
                    if bad:
                        violations.append(
                            "%s：<%s> 的 %s 用了 OWL 不支援的 modifier %s（OWL≠Vue，只支援 "
                            ".stop/.prevent/.self/.capture；按鍵判斷一律在 handler 內讀 ev.key，見 %s）。"
                            % (rel, tag, attr, "/".join(bad), OWL_DOC))
    return violations


def check_data_noupdate(root, only_file=None):
    baseline = set(read_baseline_pairs(NOUPDATE_BASELINE))
    violations = []
    files = [only_file] if only_file else sorted(
        glob.glob(os.path.join(root, "src/data/*.xml")))
    for fp in files:
        if not os.path.exists(fp):
            continue
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        with open(fp, encoding="utf-8", errors="replace") as f:
            has_noupdate = re.search(r"<data[^>]*\bnoupdate=[\"']1[\"']", f.read()) is not None
        if has_noupdate and rel not in baseline:
            violations.append(
                "%s：新增了 noupdate=\"1\" 的 data 檔。noupdate flag 會寫進 ir.model.data 變 sticky，"
                "其他 data 檔對同一 record 的 -u 更新會被靜默 skip（fresh install 綠 ≠ 升級路徑綠，見 %s）。"
                "若確定要 noupdate（如 cron / admin-curated seed），跑 --update-baseline 收錄。"
                % (rel, NOUPDATE_DOC))
    return violations


def current_noupdate_files(root):
    files = []
    for fp in sorted(glob.glob(os.path.join(root, "src/data/*.xml"))):
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        with open(fp, encoding="utf-8", errors="replace") as f:
            if re.search(r"<data[^>]*\bnoupdate=[\"']1[\"']", f.read()):
                files.append(rel)
    return files


def _reuse_values_true_lines(src):
    """回傳出現 reuse_values=True 的行號 list（AST 判定，避開字串/註解/docstring）。

    兩種形態都抓：
      1. 呼叫點 keyword arg：``foo(..., reuse_values=True)``
      2. 函式 signature default：``def f(..., reuse_values=True)``（client default 亦屬修復對象）

    用 AST 而非 regex——test 的 assert 訊息字串 '不得再傳 reuse_values=True' 這種
    描述文字會被 regex 誤報，AST 只認真正的 keyword/arg 節點。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []  # 不 parse 的檔略過（fail-open，對齊本工具哲學）
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if (kw.arg == "reuse_values"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value is True):
                    lines.append(kw.value.lineno)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            pairs = list(zip(a.args[len(a.args) - len(a.defaults):], a.defaults))
            pairs += list(zip(a.kwonlyargs, a.kw_defaults))
            for arg, default in pairs:
                if (default is not None and arg.arg == "reuse_values"
                        and isinstance(default, ast.Constant) and default.value is True):
                    lines.append(arg.lineno)
    return sorted(set(lines))


def check_reuse_values(root, only_file=None):
    """平台 code（src/**/*.py）出現 reuse_values=True 即 fail。

    #411：helm --reuse-values 讓 render 用舊 release 的 coalesced values 當 base，
    chart 升版新增的預設值永不生效（既存 release 升級靜默漏 chart 新預設，Gap 2
    bug class）。平台既存 release 升級一律走全量 values 組裝 ＋ reset_values=True。
    依據 docs/decisions/patterns/helm-reset-values-unified-pipeline.md
    """
    violations = []
    files = [only_file] if only_file else sorted(
        glob.glob(os.path.join(root, "src/**/*.py"), recursive=True))
    for fp in files:
        if not os.path.exists(fp):
            continue
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        with open(fp, encoding="utf-8", errors="replace") as f:
            src = f.read()
        for line_no in _reuse_values_true_lines(src):
            violations.append(
                "%s:%d：出現 reuse_values=True。#411：helm --reuse-values 讓 chart 升版新增"
                "預設永不生效（既存 release 升級靜默漏 chart 新預設，Gap 2 bug class）。平台既存"
                " release 升級一律走全量 values 組裝 ＋ reset_values=True（見 %s）。"
                % (rel, line_no, REUSE_VALUES_DOC))
    return violations


def _fold_int_constants(src, seed=None):
    """回傳 module 層 ``NAME = <int 算式>`` 的 {name: value}（小型常數折疊）。

    只認 int 字面值、以及由已解析常數／字面值組成的 ``+`` / ``*`` 算式——夠用來讀
    `2 * RESET_HELM_TIMEOUT_SECONDS + HELM_CLI_OVERHEAD_SECONDS` 這種預算算式，
    又不需要 import 模組（operator 的套件不在平台的 sys.path 上，import 不可行）。
    解不開的 assignment 直接略過（缺鍵由呼叫端 fail-loud，不靜默當 0）。

    ``seed`` 供跨檔的名字：算式可以引用**別的模組 import 進來的**常數
    （releases.py 的 subprocess 預算就引用 helm.py 的 HELM_CLI_OVERHEAD_SECONDS），
    那個名字在本檔沒有 assignment，必須由呼叫端先解好餵進來。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}
    resolved = dict(seed or {})

    def value_of(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, int) \
                and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.Name):
            return resolved.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mult)):
            left, right = value_of(node.left), value_of(node.right)
            if left is None or right is None:
                return None
            return left + right if isinstance(node.op, ast.Add) else left * right
        return None

    for node in tree.body:  # module 層即可：預算常數不該藏在 function 裡
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        val = value_of(node.value)
        if val is not None:
            resolved[target.id] = val
    return resolved


def _read_repo_file(root, rel):
    path = os.path.join(root, rel)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def check_reset_budget_ladder(root):
    """#461：reset 的三層 timeout 預算必須嚴格外大於內，且算式自洽。

    為什麼要一條 gate 而不是註解：#461（F-449-3）的根因就是「前端 30s / 平台 120s /
    operator ≈560s 三個預算**彼此不知道對方**」——每一層各自都看起來合理，合起來
    卻保證冷 image cache 時必逾時。散文攔不住這件事，它已經被攔失敗過一次了。

    兩層算式各自釘：
      (a) subprocess == 2 * helm --timeout + CLI 開銷
          `--timeout` 是 per-operation，`--atomic` 失敗時 helm 會再跑一次帶 wait 的
          rollback ⇒ 只給 n+slack 會在 rollback 進行中 SIGKILL helm ⇒ release 卡
          pending-rollback、Secret 停在沒人持有的新密碼（真機實測，見 doc）。
      (b) hard cap == subprocess + exec + cleanup + kubectl slack
    然後跨層：前端 > 平台 > operator hard cap。

    依據 docs/decisions/patterns/reset-async-budget-ladder.md
    """
    violations = []

    def fail(msg):
        violations.append("%s（見 %s）" % (msg, RESET_LADDER_DOC))

    js = _read_repo_file(root, RESET_LADDER_FRONTEND)
    operator_src = _read_repo_file(root, RESET_LADDER_OPERATOR)
    platform_src = _read_repo_file(root, RESET_LADDER_PLATFORM)
    helm_src = _read_repo_file(root, RESET_LADDER_HELM)
    if js is None or operator_src is None or platform_src is None or helm_src is None:
        # 檔案被搬走／改名：不能靜默放行，否則階梯守衛整條失效。
        fail("預算階梯的來源檔缺失（預期 %s）" % ", ".join(RESET_LADDER_FILES))
        return violations

    front = re.search(
        r"^const\s+RESET_REQUEST_TIMEOUT_MS\s*=\s*(\d+)\s*;", js, re.MULTILINE)
    helm = _fold_int_constants(helm_src)
    # operator 的 subprocess 算式引用 helm.py import 進來的開銷常數，故先解 helm.py
    # 再把它當 seed 餵進去（否則那條算式解不開，會被誤報成「缺常數」）。
    op = _fold_int_constants(operator_src, seed={
        k: v for k, v in helm.items() if k == "HELM_CLI_OVERHEAD_SECONDS"
    })
    plat = _fold_int_constants(platform_src)

    needed_op = (
        "RESET_HELM_TIMEOUT_SECONDS", "RESET_HELM_SUBPROCESS_TIMEOUT_SECONDS",
        "RESET_EXEC_TIMEOUT_SECONDS", "RESET_CLEANUP_EXEC_TIMEOUT_SECONDS",
        "RESET_KUBECTL_SLACK_SECONDS", "RESET_HARD_CAP_SECONDS",
    )
    missing = [k for k in needed_op if k not in op]
    if missing:
        fail("%s 缺少預算常數 %s" % (RESET_LADDER_OPERATOR, ", ".join(missing)))
    if "HELM_CLI_OVERHEAD_SECONDS" not in helm:
        fail("%s 缺少 HELM_CLI_OVERHEAD_SECONDS" % RESET_LADDER_HELM)
    if "RESET_CONFIG_TIMEOUT" not in plat:
        fail("%s 缺少 RESET_CONFIG_TIMEOUT" % RESET_LADDER_PLATFORM)
    if front is None:
        fail("%s 缺少 RESET_REQUEST_TIMEOUT_MS" % RESET_LADDER_FRONTEND)
    if violations:
        return violations

    n = op["RESET_HELM_TIMEOUT_SECONDS"]
    overhead = helm["HELM_CLI_OVERHEAD_SECONDS"]
    subprocess_budget = op["RESET_HELM_SUBPROCESS_TIMEOUT_SECONDS"]
    hard_cap = op["RESET_HARD_CAP_SECONDS"]
    platform_budget = plat["RESET_CONFIG_TIMEOUT"]
    frontend_seconds = int(front.group(1)) / 1000

    if subprocess_budget != 2 * n + overhead:
        fail(
            "RESET_HELM_SUBPROCESS_TIMEOUT_SECONDS=%s 不等於 2×%s（helm --timeout）"
            "＋%s（CLI 開銷）=%s。`--atomic` 失敗要吃兩次 --timeout（upgrade wait ＋"
            "自動 rollback wait），少算會在 rollback 進行中 kill helm ⇒ release 卡"
            " pending-rollback、Secret 停在沒人持有的新密碼"
            % (subprocess_budget, n, overhead, 2 * n + overhead))

    parts_sum = (
        subprocess_budget + op["RESET_EXEC_TIMEOUT_SECONDS"]
        + op["RESET_CLEANUP_EXEC_TIMEOUT_SECONDS"] + op["RESET_KUBECTL_SLACK_SECONDS"]
    )
    if hard_cap != parts_sum:
        fail("RESET_HARD_CAP_SECONDS=%s 不等於各子步之和 %s（改了子步沒同步硬上限）"
             % (hard_cap, parts_sum))

    if not platform_budget > hard_cap:
        fail(
            "平台 RESET_CONFIG_TIMEOUT=%ss 必須 > operator RESET_HARD_CAP_SECONDS=%ss"
            "；否則平台會在 operator 還沒答完就斷線 ⇒ 密碼不落庫、租戶得重按（#461 原症狀）"
            % (platform_budget, hard_cap))
    if not frontend_seconds > platform_budget:
        fail(
            "前端 RESET_REQUEST_TIMEOUT_MS=%ss 必須 > 平台 RESET_CONFIG_TIMEOUT=%ss"
            "；否則瀏覽器先 abort ⇒ 後端修好了租戶也看不到成功（#459 實測成功案例"
            "也要 49.9s／76.7s）" % (frontend_seconds, platform_budget))
    return violations


def _is_os_environ(node):
    """node 是 ``os.environ`` 或 ``environ``（from os import environ）嗎？"""
    if isinstance(node, ast.Attribute):
        return node.attr == "environ" and isinstance(node.value, ast.Name) \
            and node.value.id == "os"
    return isinstance(node, ast.Name) and node.id == "environ"


def _env_unconditional_cleanup_lines(src):
    """回傳「把 os.environ 的刪除方法直接當 cleanup callable 傳」的行號。

    只認**直接參照**（``addCleanup(os.environ.pop, KEY, None)``）。repo 既有的
    ``_set_env`` / ``_env_guard`` 是 lambda / 內層函式先存 prev 再條件還原 ——
    語意正確，不在此列，故誤報空間趨近零。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []  # 語法壞掉自有 Odoo 載入會炸，這裡不重複報
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in ("addCleanup", "addClassCleanup"):
            continue
        first = node.args[0]
        if (isinstance(first, ast.Attribute)
                and first.attr in ("pop", "__delitem__", "clear")
                and _is_os_environ(first.value)):
            bad.append(node.lineno)
    return sorted(bad)


def check_test_env_restore(root, only_file=None):
    """src/tests/**/*.py 不得用 ``addCleanup(os.environ.pop, ...)`` 還原 env。

    #436：這個形狀是**無條件刪除**，不是「還原原值」。測試跑完把**外部注入**的
    env（K8s Secret 注入的 AGENT_RUNTIME_NAMESPACE_SECRET）整個刪掉，字母序在後的
    測試讀不到就 fail-closed raise —— 單獨跑全綠、只有全套跑才紅，而且紅在無辜的
    test 名下（典型假綠家族：綠不代表對，是順序剛好）。
    """
    violations = []
    files = [only_file] if only_file else sorted(
        glob.glob(os.path.join(root, "src/tests/**/*.py"), recursive=True))
    for fp in files:
        if not os.path.exists(fp):
            continue
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        with open(fp, encoding="utf-8", errors="replace") as f:
            src = f.read()
        for line_no in _env_unconditional_cleanup_lines(src):
            violations.append(
                "%s:%d：addCleanup(os.environ.pop, ...) 是**無條件刪除**而非還原原值，"
                "會把外部注入的 env 洗掉，害字母序在後的測試 fail-closed raise（#436，"
                "單獨跑綠／全套跑紅、且紅在無辜的 test 名下）。改用 patch.dict —— "
                "原本沒有就刪、原本有就還原：\n"
                "          patcher = patch.dict(os.environ, {KEY: VALUE})\n"
                "          patcher.start()\n"
                "          self.addCleanup(patcher.stop)\n"
                "        （test method 內可直接用 with patch.dict(...) context manager；"
                "需要條件式還原的多變數情境見 test_agent_pod_phase_d_hardening.py 的 "
                "_env_guard。）" % (rel, line_no))
    return violations


# #492：odoo/netsvc.py:338 有 `logging.addLevelName(logging.RUNBOT, "INFO")`
# 且 `logging.RUNBOT = 25`。addLevelName 會**雙向**寫表，於是 stdlib 的
# `logging._nameToLevel['INFO']` 被由 20 覆寫成 25。而 unittest 的
# `_log.py:37` 正是 `self.level = logging._nameToLevel.get(level, level)`
# ⇒ `level='INFO'` 在 Odoo 進程裡實際是 setLevel(25)，把所有 `_logger.info()`
# （20）擋在門外。（不傳 level 反而安全：`_log.py:39` 用的是數值 logging.INFO。）
# 只列**確定被 Odoo 覆寫**的名字：其餘 level 名的字串形式仍與數值等價，不必為了
# 一致性去改動無辜的既有測試（本 repo 現存的 'DEBUG' / 'WARNING' 用法沒有問題）。
ASSERTLOGS_SHADOWED_LEVELS = {"INFO": (25, 20)}


def _assertlogs_string_level_hits(src):
    """回 [(lineno, level 名)]：assertLogs/assertNoLogs 用了被覆寫的字串 level。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else None)
        if name not in ("assertLogs", "assertNoLogs"):
            continue
        level = next((kw.value for kw in node.keywords if kw.arg == "level"), None)
        if level is None and len(node.args) >= 2:
            level = node.args[1]        # assertLogs(logger, level) 的 positional 形式
        if (isinstance(level, ast.Constant) and isinstance(level.value, str)
                and level.value.upper() in ASSERTLOGS_SHADOWED_LEVELS):
            hits.append((level.lineno, level.value.upper()))
    return hits


def check_assertlogs_numeric_level(root, only_file=None):
    """src/tests/**：``assertLogs`` 的 level 不得用字串 ``'INFO'``（#492）。

    這條守的是**恆紅／恆綠的自我否定斷言**——不是被測行為，而是斷言本身失效：

    * ``assertLogs(logger, level='INFO')`` 在 Odoo 進程裡等於 setLevel(**25**)
      （見 ASSERTLOGS_SHADOWED_LEVELS 上方的成因），於是任何 `_logger.info()`
      都捕捉不到 → 測試恆紅（`no logs of level INFO or higher triggered`），
      **且紅得像是被測 code 掉了 log**，實際上生產行為完好；
    * 反向同樣危險：`assertNoLogs(..., level='INFO')` 會漏放 20–24 級的 log，
      恆綠、零鑑別力。

    #492 就是前者：#461 加的 `test_chart_version_drift_is_logged` 從**引入當下
    就紅**（在 59e17eb2 上實測同一訊息），一路帶到 develop 破掉 0F/0E 基線。
    改用數值 ``logging.INFO`` 即可——不經過那張被覆寫的名稱表。
    """
    violations = []
    files = [only_file] if only_file else sorted(
        glob.glob(os.path.join(root, "src/tests/**/*.py"), recursive=True))
    for fp in files:
        if not os.path.exists(fp):
            continue
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        with open(fp, encoding="utf-8", errors="replace") as f:
            src = f.read()
        for line_no, level_name in _assertlogs_string_level_hits(src):
            shadow, real = ASSERTLOGS_SHADOWED_LEVELS[level_name]
            violations.append(
                "%s:%d：assertLogs/assertNoLogs 的 level 用了字串 '%s' —— Odoo 的 "
                "odoo/netsvc.py `logging.addLevelName(logging.RUNBOT, \"INFO\")`"
                "（RUNBOT=%d）覆寫了 stdlib 的 `logging._nameToLevel`，而 unittest 的 "
                "_log.py:37 正是查那張表 ⇒ 這個字串被解析成 %d 而非 %d："
                "`_logger.%s()` 全部落在門檻外，assertLogs 恆紅（訊息長得像被測 code "
                "掉了 log，實際上生產行為好好的）、assertNoLogs 恆綠。"
                "改傳**數值**：level=logging.%s。依據 %s"
                % (rel, line_no, level_name, shadow, shadow, real,
                   level_name.lower(), level_name, ASSERTLOGS_LEVEL_DOC))
    return violations


def _field_value(field):
    """讀 <field> 的值：eval 屬性優先，否則 text。"""
    if field.get("eval") is not None:
        return field.get("eval").strip()
    return (field.text or "").strip()


def _record_fields(record):
    return {f.get("name"): _field_value(f) for f in record.findall("field")}


def _is_true(value):
    return value in ("True", "1")


def seed_topology_counts(root):
    """純靜態 parse seed XML，回六個拓撲數（絕不碰 DB）。

    base / 變體的區分用語意（ac_max_access_seats > 0 ＝席數變體）而非檔名，
    這樣把變體寫進錯的檔也會被算出來。
    """
    data_dir = os.path.join(root, "src/data")
    plan_files = sorted(glob.glob(os.path.join(data_dir, "subscription_products*.xml")))
    link_files = sorted(glob.glob(os.path.join(data_dir, "subscription_template_links*.xml")))

    base_plans, variant_plans, sub_products = 0, 0, 0
    hidden_plan_xmlids = set()
    for fp in plan_files:
        for rec in ET.parse(fp).getroot().findall(".//record"):
            model, xmlid = rec.get("model"), rec.get("id")
            fields = _record_fields(rec)
            if model == "sh.subscription.plan":
                seats = fields.get("ac_max_access_seats") or "0"
                if seats.isdigit() and int(seats) > 0:
                    variant_plans += 1
                else:
                    base_plans += 1
                if _is_true(fields.get("storefront_hidden")):
                    hidden_plan_xmlids.add(xmlid)
            elif model == "product.product" and _is_true(fields.get("sh_product_subscribe")):
                sub_products += 1

    # links.xml（不含 *_variants）＝ base 掛 template 的權威來源：<data noupdate="0">
    # ＋全部 (6, 0, [...]) 全量 reset ⇒ DB 鏡射檔案。變體用 (4,) append，不入 base 拓撲。
    refs = set()
    for fp in link_files:
        if fp.endswith("_variants.xml"):
            continue
        for rec in ET.parse(fp).getroot().findall(".//record"):
            for field in rec.findall("field"):
                if field.get("name") == "subscription_plan_ids":
                    refs |= set(re.findall(r"ref\('([^']+)'\)", field.get("eval") or ""))

    visible = refs - hidden_plan_xmlids
    return {
        "base_plans": base_plans,
        "variant_plans": variant_plans,
        "subscription_products": sub_products,
        "linked_bases": len(refs),
        "visible_linked_bases": len(visible),
        "tiers": len({_CYCLE_SUFFIX_RE.sub("", ref) for ref in refs}),
    }


def check_seed_topology(root, only_file=None):
    """seed 拓撲六數對 baseline；一變就 fail，並附「該一起檢視的位置」清單。

    #433：成因不是「有人寫了硬編值」，而是「改 seed 時 bump 了 3 個持有計數的
    檔案、漏掉 2 個，因為沒有共用清單」。這條 gate 把「三週後 CI 才紅、且紅在
    無關的 test 名下」提前成「改 seed 當下就被擋、附帶待檢清單」。
    """
    baseline = read_baseline_pairs(SEED_TOPOLOGY_BASELINE)
    if not baseline:
        return []  # 尚未建立 baseline → 不擋（fail-open，對齊本工具哲學）
    try:
        counts = seed_topology_counts(root)
    except (ET.ParseError, OSError):
        return []  # seed XML 壞掉自有 data-noupdate / Odoo 載入會炸，這裡不重複報
    drifted = [(key, baseline.get(key), value)
               for key, value in sorted(counts.items())
               if baseline.get(key) != str(value)]
    if not drifted:
        return []
    detail = "、".join(
        "%s %s→%d" % (key, old if old is not None else "(未收錄)", new)
        for key, old, new in drifted)
    return [
        "seed 拓撲已變動（%s）。這代表 src/data/subscription_*.xml 的 seed 拓撲被改了；"
        "請逐一檢視下列持有相關計數／集合斷言的位置，確認都已同步，再跑 "
        "scripts/audit/repo-lint.py --update-baseline 收錄新拓撲（見 %s）：\n"
        % (detail, SEED_TOPOLOGY_DOC)
        + "\n".join("      - " + dep for dep in SEED_TOPOLOGY_DEPENDENTS)
    ]


def _strip_js_noise(src):
    """把 JS 的註解與字串字面值換成等長空白（行號／offset 全部保留）。

    形態 regex 只該看真正的程式碼：ServiceDetailPage 的 getter docstring 本身就在
    講「原本這裡是硬編碼的三筆 slug 白名單」，不剝除就會自我誤報。
    regex literal（``/["']/g``）有機會被誤當字串起點而多剝一段——那只會少報不會
    誤報，對齊本工具 fail-open 的哲學。
    """
    out = list(src)
    i, n, state = 0, len(src), None
    while i < n:
        ch = src[i]
        if state is None:
            if ch == "/" and i + 1 < n and src[i + 1] in "/*":
                state = "line" if src[i + 1] == "/" else "block"
                out[i] = out[i + 1] = " "
                i += 2
                continue
            if ch in "'\"`":
                state, out[i] = ch, " "
                i += 1
                continue
            i += 1
            continue
        if state == "line":
            if ch == "\n":
                state = None
            else:
                out[i] = " "
            i += 1
            continue
        if state == "block":
            if ch == "*" and i + 1 < n and src[i + 1] == "/":
                out[i] = out[i + 1] = " "
                state = None
                i += 2
                continue
            if ch != "\n":
                out[i] = " "
            i += 1
            continue
        # 字串字面值內
        if ch == "\\":
            out[i] = " "
            if i + 1 < n and src[i + 1] != "\n":
                out[i + 1] = " "
            i += 2
            continue
        if ch == state:
            state, out[i] = None, " "
        elif ch != "\n":
            out[i] = " "
        i += 1
    return "".join(out)


def _strip_xml_comments(src):
    """XML 註解換等長空白（同理：註解裡引述壞形態不該被當違規）。"""
    return re.sub(r"<!--.*?-->",
                  lambda m: re.sub(r"[^\n]", " ", m.group(0)), src, flags=re.DOTALL)


def _balanced_close(text, open_idx):
    """回 text[open_idx] 的括號（``(`` 或 ``{``）對應的閉合 index；找不到回 None。"""
    open_ch = text[open_idx]
    close_ch = {"(": ")", "{": "}"}[open_ch]
    depth = 0
    for j in range(open_idx, len(text)):
        if text[j] == open_ch:
            depth += 1
        elif text[j] == close_ch:
            depth -= 1
            if depth == 0:
                return j
    return None


_MEMBERSHIP_CALL_RE = re.compile(r"\.\s*(?:includes|indexOf)\s*\(")
_SLUG_WORD_RE = re.compile(r"\bslug\b")
_SLUG_RECEIVER_RE = re.compile(
    r"\b[\w$]*slugs?\s*\.\s*(?:includes|indexOf)\s*\(", re.IGNORECASE)


def _slug_membership_hits(text):
    """回 [(行號, 片段)]：以 slug 做「集合成員判斷」的兩種形態。

    1. **判準是 slug**：``X.includes(t.slug)`` / ``X.indexOf(slug) !== -1``
       ——不管集合是陣列 literal 還是抽出去的常數，都抓得到。
    2. **集合叫 slug**：``MCP_SLUGS.includes(x)`` / ``slug.includes('mcp-')``
       ——擋「先把 slug 存成區域變數再比」與 prefix 猜能力這兩種繞法。

    刻意**不**抓 ``filter`` / ``find`` / ``map``：那是列表操作（marketplace 篩選、
    排序）不是布林判準，抓了會誤殺合法用途。單值 identity 比較
    （``template.slug === 'odoo'``，如 isOdooService）同樣不在形態內。
    """
    hits = {}
    for m in _MEMBERSHIP_CALL_RE.finditer(text):
        open_idx = m.end() - 1
        close_idx = _balanced_close(text, open_idx)
        arg = text[open_idx + 1:close_idx] if close_idx else text[open_idx + 1:open_idx + 120]
        if _SLUG_WORD_RE.search(arg):
            hits[m.start()] = text[m.start():(close_idx + 1) if close_idx else m.end()]
    for m in _SLUG_RECEIVER_RE.finditer(text):
        hits.setdefault(m.start(), m.group(0))
    result = []
    for pos, snippet in sorted(hits.items()):
        flat = " ".join(snippet.split())
        result.append((text[:pos].count("\n") + 1,
                       flat[:70] + "…" if len(flat) > 70 else flat))
    return result


def check_slug_capability_gate(root, only_file=None):
    """前端（src/static/src/**/*.{js,xml}）不得用 slug 集合成員判斷決定能力。

    #460：Reset 密碼按鈕的顯示判準是硬編碼三筆 slug 白名單，後端授權判準卻是
    ``template.has_admin_gui``（#357 C01）——兩端必然漂移：8 個 has_admin_gui=True
    的模板有 5 個後端放行、前端按不到按鈕，**修復對租戶不可達等於沒修**。能力一律
    由後端欄位表達（payload 鏡射 model 欄位），前端不維護自己的名單。
    依據 docs/decisions/patterns/capability-gate-single-source-field.md
    """
    violations = []
    if only_file:
        files = [only_file]
    else:
        files = sorted(
            glob.glob(os.path.join(root, "src/static/src/**/*.js"), recursive=True)
            + glob.glob(os.path.join(root, "src/static/src/**/*.xml"), recursive=True))
    for fp in files:
        if not os.path.exists(fp):
            continue
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        with open(fp, encoding="utf-8", errors="replace") as f:
            src = f.read()
        text = _strip_xml_comments(src) if fp.endswith(".xml") else _strip_js_noise(src)
        for line_no, snippet in _slug_membership_hits(text):
            violations.append(
                "%s:%d：用 slug 做集合成員判斷（%s）。前端能力 gate 不得自建 slug 名單"
                "——能力由後端欄位表達（service payload 的 template.%s，#460 S1）。slug "
                "名單是第二判準，與後端授權判準（#357 C01）必然漂移：8 個 %s=True 的模板"
                "曾有 5 個後端放行、前端按不到，修復對租戶不可達。缺欄位就補後端 payload，"
                "不要在前端補名單。（單值 identity 比較如 template.slug === 'odoo' 不在本"
                "規則內；若這裡確實不是能力判準，請在 PR 說明並調整本規則 scope，見 %s）"
                % (rel, line_no, snippet, ADMIN_GUI_FIELD, ADMIN_GUI_FIELD,
                   CAPABILITY_GATE_DOC))
    return violations


def _js_member_bodies(stripped, name):
    """回 class member ``get name() {...}`` / ``name() {...}`` 的 body 列表。"""
    bodies = []
    for m in re.finditer(r"(?:get\s+)?\b%s\s*\(\s*\)\s*\{" % re.escape(name), stripped):
        open_idx = m.end() - 1
        close_idx = _balanced_close(stripped, open_idx)
        bodies.append(stripped[open_idx:close_idx + 1] if close_idx else stripped[open_idx:])
    return bodies


def _gate_js_sources(xml_path):
    """XML 對應的 component JS（同名優先，否則同目錄全部）——已剝註解／字串。"""
    same = os.path.splitext(xml_path)[0] + ".js"
    paths = [same] if os.path.exists(same) else sorted(
        glob.glob(os.path.join(os.path.dirname(xml_path), "*.js")))
    sources = []
    for p in paths:
        with open(p, encoding="utf-8", errors="replace") as f:
            sources.append(_strip_js_noise(f.read()))
    return sources


def _gate_reaches_admin_gui(exprs, js_sources):
    """t-if 表達式（含祖先鏈）是否直接或經 getter 追到 has_admin_gui。"""
    for expr in exprs:
        if ADMIN_GUI_FIELD in expr:
            return True
        for name in set(re.findall(r"[A-Za-z_$][\w$]*", expr)) - OWL_EXPR_NON_MEMBERS:
            for src in js_sources:
                if any(ADMIN_GUI_FIELD in body for body in _js_member_bodies(src, name)):
                    return True
    return False


def check_admin_gui_gate_anchor(root):
    """Reset 密碼按鈕的顯示 gate 必須（直接或經 getter）追得到 has_admin_gui。

    check_slug_capability_gate 擋的是「已知的壞形態」；這條反過來鎖「唯一的對形態」
    ——不論 getter 叫什麼名字、判準寫成哪種樣子，只要按鈕的 t-if 鏈追不到
    has_admin_gui 就 fail。第三個判準（category / mcp_enabled / 任何新名單）因此在
    review 之前就被機器攔下（#460 S2）。

    Fail-open：找不到按鈕（功能下架 / handler 更名）不擋——形態守線由上一條負責。
    """
    violations = []
    handler_re = re.compile(r"\b%s\b" % re.escape(ADMIN_GUI_BUTTON_HANDLER))
    for fp in sorted(glob.glob(os.path.join(root, "src/static/src/**/*.xml"), recursive=True)):
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        try:
            tree = ET.parse(fp)
        except ET.ParseError:
            continue  # XML 壞掉由 check_owl_template 報，這裡不重複
        buttons = [elem for elem in tree.iter()
                   if any(handler_re.search(v or "") for v in elem.attrib.values())]
        if not buttons:
            continue
        with open(fp, encoding="utf-8", errors="replace") as f:
            line_no = next((i for i, line in enumerate(f, 1)
                            if handler_re.search(line)), 1)
        parents = {child: parent for parent in tree.iter() for child in parent}
        js_sources = _gate_js_sources(fp)
        for elem in buttons:
            exprs, node = [], elem
            while node is not None:  # 自身 → 祖先鏈（gate 可能掛在外層 wrapper）
                exprs += [node.get(attr) for attr in ("t-if", "t-elif") if node.get(attr)]
                node = parents.get(node)
            if _gate_reaches_admin_gui(exprs, js_sources):
                continue
            violations.append(
                "%s:%d：Reset 密碼按鈕（%s）的顯示 gate 追不到 %s（t-if 鏈：%s）。按鈕的 "
                "t-if（含祖先）必須直接引用該欄位，或引用一個 body 讀該欄位的 getter"
                "——那是與後端 reset 授權判準（controllers/paas.py 的 "
                "service.template_id.%s，#357 C01）唯一收斂的判準。前端一旦長出第二判準"
                "（slug 名單、category、mcp_enabled…），就會回到 #460：後端放行、租戶"
                "按不到，修復不可達（見 %s）。"
                % (rel, line_no, ADMIN_GUI_BUTTON_HANDLER, ADMIN_GUI_FIELD,
                   "／".join(exprs) or "(無 t-if)", ADMIN_GUI_FIELD, CAPABILITY_GATE_DOC))
    return violations


def _template_is_hidden_via_service_lines(src):
    """回傳 ``<x>.template_id.is_hidden`` 屬性鏈的行號 list（AST 判定）。

    只認「經由 ``template_id`` 取到 template 再讀 ``is_hidden``」這個形態——那必然是
    在判**某個既有 service 的**可見性。曝光層的 ``template.is_hidden``（直接持有
    template 變數，如 marketplace domain 與 template by-id）不匹配，因此 B 組四處
    不會被誤報。

    用 AST 而非 regex：註解與 docstring 裡本來就會出現 `template_id.is_hidden`
    這串字（本次拆分的說明文字就是），regex 會全部誤報。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []  # 不 parse 的檔略過（fail-open，對齊本工具哲學）
    lines = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and node.attr == CONCEAL_WRONG_FIELD
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "template_id"):
            lines.append(node.lineno)
    return sorted(set(lines))


def check_conceal_instances_gate(root, only_file=None):
    """既有 service 的可見性判準不得是 ``template_id.is_hidden``（#471）。

    #471：原本一個 ``is_hidden`` 同時承載「不上架」與「既有實例整條鏈消失」，於是
    任何「暫時下架」的一般商品都會把租戶手上正在跑的服務一起從前台抹掉——詳情頁
    打不開、rollback／change-plan／MCP 設定全回 Service not found（#464 實測
    hermes-agent）。拆成 ``is_hidden``（曝光層）＋ ``conceal_instances``（實例層）後，
    唯一的退化路徑就是**有人新增一個既有服務的操作端點、照抄舊 gate**。這條 gate
    把那次照抄從「三個月後租戶回報服務不見了」提前成「寫下去當下就被擋」。

    依據 docs/decisions/patterns/hidden-template-presentation-chain.md
    """
    violations = []
    files = [only_file] if only_file else sorted(
        glob.glob(os.path.join(root, "src/**/*.py"), recursive=True))
    for fp in files:
        if not os.path.exists(fp):
            continue
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        if rel.startswith("src/tests/"):
            continue  # 測試會刻意提及舊判準（紅燈對照、回歸描述）
        with open(fp, encoding="utf-8", errors="replace") as f:
            src = f.read()
        for line_no in _template_is_hidden_via_service_lines(src):
            violations.append(
                "%s:%d：用 `template_id.%s` 判既有 service 的可見性。實例層判準是 "
                "`template_id.%s`——`%s` 只剩**曝光層**語意（marketplace 不列／template "
                "詳情與 plans 不可讀／generic create 拒收，那四處直接持有 template 變數、"
                "不經 template_id）。照抄舊 gate 的後果是「暫時下架一個商品」把租戶手上 "
                "state=running 的服務一起變成 Service not found（#471／#464）。"
                "曝光層 gate 請改成直接讀 template 的形態，見 %s"
                % (rel, line_no, CONCEAL_WRONG_FIELD, CONCEAL_FIELD,
                   CONCEAL_WRONG_FIELD, HIDDEN_CHAIN_DOC))
    return violations


def check_transition_anchor_utc(root, only_file=None):
    """轉移狀態的時間錨不得用 ``datetime.now()`` 寫入（#489）。

    ``deployed_at`` / ``last_upgraded_at`` 從 #489 起是 age timeout 的**判定用**
    錨，不再只是前台顯示。Odoo 的 Datetime 欄位契約是 **UTC naive**，而 Python
    的 ``datetime.now()`` 回的是**本機時區** naive——兩者在非 UTC 的伺服器上不
    同基準：

      * UTC+8 → 錨比 ``fields.Datetime.now()`` 大 8 小時 → age 恆為負 →
        **逾時保護靜默失效**；
      * UTC-x → age 虛增 → **誤殺**（而誤殺不可自癒：poll 只撈轉移狀態，
        寫進 error 後沒有任何路徑會把它 poll 回 running）。

    測試環境的容器時區是 UTC，所以這個缺陷**測不出來**——只能靠 gate。
    （合併前 `controllers/paas.py` 的 reconfigure 路徑正是這個寫法。）
    """
    anchor_fields = {"deployed_at", "last_upgraded_at"}
    violations = []
    files = [only_file] if only_file else sorted(
        glob.glob(os.path.join(root, "src/**/*.py"), recursive=True))
    for fp in files:
        if not os.path.exists(fp):
            continue
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        if rel.startswith("src/tests/"):
            continue
        with open(fp, encoding="utf-8", errors="replace") as f:
            src = f.read()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if not (isinstance(key, ast.Constant) and key.value in anchor_fields):
                    continue
                if not (isinstance(value, ast.Call)
                        and isinstance(value.func, ast.Attribute)
                        and value.func.attr == "now"):
                    continue
                # 合格：fields.Datetime.now()。不合格：datetime.now() /
                # datetime.datetime.now()（本機時區）。
                owner = value.func.value
                if isinstance(owner, ast.Attribute) and owner.attr == "Datetime":
                    continue
                violations.append(
                    "%s:%d：`%s` 用 datetime.now() 寫入。#489 起這是 age timeout 的判定"
                    "用錨，必須與 fields.Datetime.now() 同基準（Odoo Datetime 欄位是 UTC "
                    "naive；datetime.now() 是本機時區）。非 UTC 伺服器上這會讓逾時保護"
                    "靜默失效或誤殺，而測試容器是 UTC → 測不出來。"
                    % (rel, getattr(value, "lineno", key.lineno), key.value))
    return violations


def check_mixin_subscription_sudo(root, only_file=None):
    """``subscription_lifecycle_mixin`` 內不得裸讀 ``self.subscription_id``（#29）。

    ``subscription_id`` 指向 ``sh.subscription.subscription``，其 read ACL 只發給
    ``group_manager_sh_subscription`` / ``group_user_sh_subscription`` /
    ``base.group_portal``。workspace 擁有權是**平台側**概念、不連帶給 Odoo 群組，
    所以 mixin 內部一裸讀，典型租戶（只有 ``base.group_user``）就在 env cache 落空
    時撞 AccessError——SH delete / provision、CS update / delete 回
    "You are not allowed to access sh.subscription.subscription"。

    這條缺陷**測不出來也看不出來**的原因是 cache：admin 端的任何前置讀取都會餵飽
    transaction 級 cache，讓後續 user-scoped traversal 不再查 ACL（#27 當年就是被
    這個假象騙過，把稽核界線畫在 controller）。production 沒有這個假象——每個 HTTP
    request 都從空 cache 開始。

    修法是把 traversal 收斂到唯一入口 ``_sub()``（回 ``self.sudo()
    .subscription_id``）。這條 gate 守的就是「唯一入口」這個不變式：新增方法時照抄
    舊寫法會在編輯當下被擋，而不是三個月後由租戶回報。
    """
    violations = []
    target = os.path.join(root, MIXIN_SUDO_FILE)
    if only_file and os.path.abspath(only_file) != os.path.abspath(target):
        return violations
    if not os.path.exists(target):
        return violations
    with open(target, encoding="utf-8", errors="replace") as f:
        src = f.read()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return violations  # fail-open，對齊本工具哲學
    for node in ast.walk(tree):
        # 只認 `self.subscription_id`（Name self）。`self.sudo().subscription_id`
        # 的 value 是 Call，不匹配；docstring / 註解裡的同名字串也不匹配（AST）。
        if (isinstance(node, ast.Attribute)
                and node.attr == "subscription_id"
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"):
            violations.append(
                "%s:%d：裸讀 `self.subscription_id`。mixin 內部的 subscription "
                "traversal 一律走 `self._sub()`（sudo）——workspace 擁有者拿不到 "
                "sh_subscription 的 Odoo 群組，裸讀會讓典型租戶在 cache 落空時撞 "
                "AccessError（#29；controller 端同 class 缺陷見 #27／PR #28）。"
                "sudo 在這裡不是權限放大：controller 已先做完 workspace 歸屬 + "
                "role 兩道閘。"
                % (MIXIN_SUDO_FILE, node.lineno))
    return violations


CONFIG_PARAM_SETTINGS_FILE = "src/models/res_config_settings.py"
CONFIG_PARAM_HOOKS_FILE = "src/hooks.py"
CONFIG_PARAM_DOC = "CLAUDE.md「使用 config_parameter 時的卸載清理」"


def _declared_config_parameters(path):
    """`res.config.settings` 欄位上宣告的 config_parameter 字面（AST，避開註解／字串）。"""
    with open(path, encoding="utf-8", errors="replace") as f:
        tree = ast.parse(f.read())
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "config_parameter" and isinstance(kw.value, ast.Constant) \
                    and isinstance(kw.value.value, str):
                found.setdefault(kw.value.value, node.lineno)
    return found


def _uninstall_cleanup_parameters(path):
    """`hooks.py` 的 `_CONFIG_PARAMETERS` list 字面（AST）。"""
    with open(path, encoding="utf-8", errors="replace") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "_CONFIG_PARAMETERS" not in names:
            continue
        if isinstance(node.value, (ast.List, ast.Tuple, ast.Set)):
            return {e.value for e in node.value.elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return None


def check_config_parameter_uninstall(root, only_file=None):
    """`config_parameter` 欄位的 key 必須進 `hooks.py` 的 `_CONFIG_PARAMETERS`。

    Odoo 卸載 addon **不會**清 `ir.config_parameter`，所以每個 `config_parameter=`
    都要在 uninstall hook 的清單裡登記一筆，否則卸載後留下孤兒設定（重裝時被讀到、
    值卻是上一輪的）。CLAUDE.md 明載這條義務，但在 #501 之前**沒有任何機械保護**
    ——漏了只能靠 review 抓，而 review 抓不到「少寫一行」。

    只查一個方向（宣告了卻沒登記）。反向（登記了卻沒有對應欄位）刻意不擋：清單裡
    本來就有歷史遺留與非 settings 來源的 key，擋它會逼人刪掉仍需清理的項目。

    ⚠ **覆蓋邊界**：兩側都只認**字面量**。`config_parameter=` 若寫成運算式
    （字串串接、變數、f-string），或 `_CONFIG_PARAMETERS` 用推導式產生，本檢查會
    **靜默略過**、不報 violation —— 綠燈不代表沒有漏登記的 key。目前 repo 內兩側
    都是字面量（17 個宣告 / 一個 list literal），維持這個形態本檢查才有效力；要改成
    動態產生時，請連同本檢查一起重寫，不要以為它還在守。
    """
    settings_path = os.path.join(root, CONFIG_PARAM_SETTINGS_FILE)
    hooks_path = os.path.join(root, CONFIG_PARAM_HOOKS_FILE)
    if not (os.path.exists(settings_path) and os.path.exists(hooks_path)):
        return []
    # 不變式跨兩個檔，動到任一個都整條重算，不做 only_file 切片。
    if only_file and os.path.relpath(
            os.path.abspath(only_file), os.path.abspath(root)
    ).replace(os.sep, "/") not in (CONFIG_PARAM_SETTINGS_FILE, CONFIG_PARAM_HOOKS_FILE):
        return []
    declared = _declared_config_parameters(settings_path)
    registered = _uninstall_cleanup_parameters(hooks_path)
    if registered is None:
        return ["%s：找不到 `_CONFIG_PARAMETERS` 的 list 字面，卸載清理的守線失效。"
                % CONFIG_PARAM_HOOKS_FILE]
    violations = []
    for key, lineno in sorted(declared.items(), key=lambda kv: kv[1]):
        if key not in registered:
            violations.append(
                "%s:%d：config_parameter %r 沒有登記進 %s 的 `_CONFIG_PARAMETERS`。"
                "Odoo 卸載 addon 不會清 ir.config_parameter，漏登記＝卸載後留下孤兒"
                "設定（重裝時被讀到、值卻是上一輪的）。把這個 key 加進該清單即可"
                "（見 %s）。"
                % (CONFIG_PARAM_SETTINGS_FILE, lineno, key,
                   CONFIG_PARAM_HOOKS_FILE, CONFIG_PARAM_DOC))
    return violations


DUP_METHOD_DOC = "docs/decisions/patterns/README.md"


def _accessor_or_overload(node):
    """`@x.setter/getter/deleter` 與 `@overload` 是**刻意**的同名成對定義，豁免。"""
    for dec in node.decorator_list:
        if isinstance(dec, ast.Attribute) and dec.attr in (
                'setter', 'getter', 'deleter'):
            return True
        name = dec.id if isinstance(dec, ast.Name) else (
            dec.attr if isinstance(dec, ast.Attribute) else None)
        if name == 'overload':
            return True
    return False


def _duplicate_methods(class_node):
    """回傳 [(method_name, first_lineno, dup_lineno)]——只看 class body 的直接子節點。"""
    seen, dups = {}, []
    for child in class_node.body:
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if _accessor_or_overload(child):
            continue
        if child.name in seen:
            dups.append((child.name, seen[child.name], child.lineno))
        seen[child.name] = child.lineno
    return dups


def check_duplicate_class_methods(root, only_file=None):
    """同一個 class body 內不得重複定義同名 method。

    Python **靜默接受**重複定義，後者覆蓋前者——沒有錯誤、沒有警告、沒有 log。
    在 Odoo model 上這等於「新加的 override 把既有的 override 整個關掉」，而既有
    的那個往往是別人辛苦補的守衛。

    實例（本檢查的來源，#501 PR #517）：為了擋「先建成 deleting 再 write 回來」
    的上限繞道，新增了第二個 `CloudService.write()`，靜默覆蓋掉 #279 Path B 那個
    「不可變更已部署服務的 workspace」的租戶隔離守衛。針對性測試涵蓋不到那條
    路徑，**只有全套會紅**（`TestReachSyncTriggers.test_cs_workspace_reassign_on_live_ns_rejected`
    → `UserError not raised`），而全套很貴、又容易被不乾淨的基準掩蓋成「本來就紅」。

    測試檔同樣涵蓋：重複的 test method 會讓前一個**永遠不執行**，是無聲的假綠。

    ⚠ **覆蓋邊界**（同 config-parameter-uninstall 的標準，寫明才不會被誤信）：
    - 只比對**同一個 class body 的直接子節點**。跨 class 不比（那是正常的繼承
      覆蓋）；巢狀 class 各自獨立比對。
    - `if` / `try` / `with` 區塊**內**的 `def` **不納入**比對——條件式定義是合法
      形態（依版本／可選相依定義不同實作）。代價：「兩個分支各定義一次且都會執行」
      這種真的會覆蓋的寫法抓不到。目前 repo 內沒有這種用法；若日後出現，這條
      gate 對它是靜默的。
    - `@property` 的 `setter/getter/deleter` 與 `typing.overload` 豁免（刻意同名）。
    - 只掃 `src/**/*.py`。
    """
    violations = []
    files = [only_file] if only_file else sorted(
        glob.glob(os.path.join(root, "src/**/*.py"), recursive=True))
    for fp in files:
        if not os.path.exists(fp) or not fp.endswith(".py"):
            continue
        rel = os.path.relpath(fp, root).replace(os.sep, "/")
        if not rel.startswith("src/"):
            continue
        with open(fp, encoding="utf-8", errors="replace") as f:
            try:
                tree = ast.parse(f.read())
            except SyntaxError:
                continue  # 語法錯誤由 Python 自己報，這裡不重複
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for name, first, dup in _duplicate_methods(node):
                violations.append(
                    "%s:%d：class `%s` 重複定義 `%s()`（前一個在 :%d）。Python 靜默"
                    "採用後者、前者整段失效——沒有錯誤也沒有警告，而前者往往是既有的"
                    "守衛（#501 實例：新增的 write() 覆蓋掉 #279 擋 workspace 重指派的"
                    "租戶隔離守衛，只有全套會紅）。要擴充既有 method 就**併進那一個**，"
                    "不要新增第二個定義。（見 %s）"
                    % (rel, dup, node.name, name, first, DUP_METHOD_DOC))
    return violations


# ---------------------------------------------------------------- modes

def run_all(root):
    violations = (check_controller_sudo(root)
                  + check_owl_template(root)
                  + check_data_noupdate(root)
                  + check_reuse_values(root)
                  + check_seed_topology(root)
                  + check_test_env_restore(root)
                  + check_slug_capability_gate(root)
                  + check_admin_gui_gate_anchor(root)
                  + check_conceal_instances_gate(root)
                  + check_reset_budget_ladder(root)
                  + check_transition_anchor_utc(root)
                  + check_assertlogs_numeric_level(root)
                  + check_mixin_subscription_sudo(root)
                  + check_config_parameter_uninstall(root)
                  + check_duplicate_class_methods(root))
    if violations:
        print("repo-lint：%d 個違規" % len(violations))
        for v in violations:
            print("  ✗ " + v)
        return 1
    print("repo-lint：全部通過（controller-sudo / owl-template / data-noupdate / "
          "reuse-values / seed-topology / test-env-restore / capability-gate / "
          "conceal-instances / reset-budget-ladder / transition-anchor-utc / "
          "assertlogs-numeric-level / mixin-subscription-sudo / "
          "config-parameter-uninstall / duplicate-class-methods）")
    return 0


def update_baseline(root):
    os.makedirs(BASELINE_DIR, exist_ok=True)
    counts = current_sudo_counts(root)
    with open(SUDO_BASELINE, "w", encoding="utf-8") as f:
        f.write("# controller .sudo( 呼叫點數量上限（repo-lint.py --update-baseline 產生）\n")
        f.write("# 調升前必讀 %s\n" % SUDO_DOC)
        for rel, count in sorted(counts.items()):
            f.write("%d\t%s\n" % (count, rel))
    files = current_noupdate_files(root)
    with open(NOUPDATE_BASELINE, "w", encoding="utf-8") as f:
        f.write("# 允許 noupdate=\"1\" 的 data 檔（repo-lint.py --update-baseline 產生）\n")
        f.write("# 新增前必讀 %s\n" % NOUPDATE_DOC)
        for rel in files:
            f.write("1\t%s\n" % rel)
    topology = seed_topology_counts(root)
    with open(SEED_TOPOLOGY_BASELINE, "w", encoding="utf-8") as f:
        f.write("# seed 拓撲計數（repo-lint.py --update-baseline 產生）\n")
        f.write("# 純靜態 parse src/data/subscription_{products,template_links}*.xml，"
                "與 DB 無關。\n")
        f.write("# 這裡的數字變動時，check_seed_topology 會列出所有該一起檢視的位置。"
                "調整前必讀 %s\n" % SEED_TOPOLOGY_DOC)
        for key, value in sorted(topology.items()):
            f.write("%d\t%s\n" % (value, key))
    print("baseline 已更新：%s（%d 檔）、%s（%d 檔）、%s（%d 個計數）"
          % (os.path.relpath(SUDO_BASELINE), len(counts),
             os.path.relpath(NOUPDATE_BASELINE), len(files),
             os.path.relpath(SEED_TOPOLOGY_BASELINE), len(topology)))
    return 0


def run_hook():
    try:
        payload = json.load(sys.stdin)
        file_path = (payload.get("tool_input") or {}).get("file_path") or ""
        if not file_path:
            return 0
        root = repo_root()
        rel = os.path.relpath(os.path.abspath(file_path), os.path.abspath(root)).replace(os.sep, "/")
        if rel.startswith(".."):
            return 0
        abs_path = os.path.abspath(file_path)
        # #461 預算階梯是**跨檔**不變式（前端 JS ／ 平台 client ／ operator 常數各在
        # 一處），所以只要動到其中任一檔就整條重算，不做 only_file 切片。
        ladder = (check_reset_budget_ladder(root)
                  if rel in RESET_LADDER_FILES else [])
        if re.match(r"^src/controllers/.*\.py$", rel):
            violations = check_controller_sudo(root, only_file=abs_path)
            violations += check_reuse_values(root, only_file=abs_path)
            violations += check_conceal_instances_gate(root, only_file=abs_path)
            violations += check_transition_anchor_utc(root, only_file=abs_path)
            violations += check_duplicate_class_methods(root, only_file=abs_path)
        elif re.match(r"^src/.*\.py$", rel):
            violations = check_reuse_values(root, only_file=abs_path)
            violations += check_conceal_instances_gate(root, only_file=abs_path)
            violations += check_transition_anchor_utc(root, only_file=abs_path)
            violations += check_mixin_subscription_sudo(root, only_file=abs_path)
            violations += check_config_parameter_uninstall(root, only_file=abs_path)
            violations += check_duplicate_class_methods(root, only_file=abs_path)
            if rel.startswith("src/tests/"):
                violations += check_test_env_restore(root, only_file=abs_path)
                violations += check_assertlogs_numeric_level(root, only_file=abs_path)
        elif re.match(r"^src/static/src/.*\.xml$", rel):
            violations = check_owl_template(root, only_file=abs_path)
            violations += check_slug_capability_gate(root, only_file=abs_path)
            violations += check_admin_gui_gate_anchor(root)
        elif re.match(r"^src/static/src/.*\.js$", rel):
            # anchor 走全域：改 JS（getter 被刪／改名／改判準）壞掉的是 XML 那顆按鈕。
            violations = check_slug_capability_gate(root, only_file=abs_path)
            violations += check_admin_gui_gate_anchor(root)
        elif re.match(r"^src/data/.*\.xml$", rel):
            violations = check_data_noupdate(root, only_file=abs_path)
            if re.match(r"^src/data/subscription_(products|template_links).*\.xml$", rel):
                violations += check_seed_topology(root)
        elif ladder:
            # extra/paas-operator/** 不在上面任何分支，但它持有階梯的內層常數。
            violations = []
        else:
            return 0
        violations += ladder
        if violations:
            print(json.dumps({
                "decision": "block",
                "reason": "[repo-lint] 剛才的編輯觸發假綠 gate，請立即修正：\n"
                          + "\n".join("✗ " + v for v in violations),
            }))
        return 0
    except Exception:
        return 0  # fail-open：hook 模式絕不擋開發


def main(argv):
    if "--hook" in argv:
        return run_hook()
    root = repo_root()
    if "--update-baseline" in argv:
        return update_baseline(root)
    return run_all(root)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

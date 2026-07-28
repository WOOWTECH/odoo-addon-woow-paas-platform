#!/usr/bin/env python3
"""repo-lint：把「假綠家族」pitfall 轉成機械 gate 的靜態檢查工具。

六個 check（每條都對應一個已翻車的 pitfall，訊息內附文件指針）：

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

OWL_ALLOWED_MODIFIERS = {"stop", "prevent", "self", "capture", "synthetic"}

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


# ---------------------------------------------------------------- modes

def run_all(root):
    violations = (check_controller_sudo(root)
                  + check_owl_template(root)
                  + check_data_noupdate(root)
                  + check_reuse_values(root)
                  + check_seed_topology(root)
                  + check_test_env_restore(root))
    if violations:
        print("repo-lint：%d 個違規" % len(violations))
        for v in violations:
            print("  ✗ " + v)
        return 1
    print("repo-lint：全部通過（controller-sudo / owl-template / data-noupdate / "
          "reuse-values / seed-topology / test-env-restore）")
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
        if re.match(r"^src/controllers/.*\.py$", rel):
            violations = check_controller_sudo(root, only_file=abs_path)
            violations += check_reuse_values(root, only_file=abs_path)
        elif re.match(r"^src/.*\.py$", rel):
            violations = check_reuse_values(root, only_file=abs_path)
            if rel.startswith("src/tests/"):
                violations += check_test_env_restore(root, only_file=abs_path)
        elif re.match(r"^src/static/src/.*\.xml$", rel):
            violations = check_owl_template(root, only_file=abs_path)
        elif re.match(r"^src/data/.*\.xml$", rel):
            violations = check_data_noupdate(root, only_file=abs_path)
            if re.match(r"^src/data/subscription_(products|template_links).*\.xml$", rel):
                violations += check_seed_topology(root)
        else:
            return 0
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

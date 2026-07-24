#!/usr/bin/env python3
"""repo-lint：把「假綠家族」pitfall 轉成機械 gate 的靜態檢查工具。

四個 check（每條都對應一個已翻車的 pitfall，訊息內附文件指針）：

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

SUDO_DOC = "docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md"
OWL_DOC = "docs/tracking/pitfalls/owl-template-crash-class-syntax.md"
NOUPDATE_DOC = "docs/tracking/pitfalls/seed-noupdate-sticky-template-links.md"
REUSE_VALUES_DOC = "docs/decisions/patterns/helm-reset-values-unified-pipeline.md"

OWL_ALLOWED_MODIFIERS = {"stop", "prevent", "self", "capture", "synthetic"}


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


# ---------------------------------------------------------------- modes

def run_all(root):
    violations = (check_controller_sudo(root)
                  + check_owl_template(root)
                  + check_data_noupdate(root)
                  + check_reuse_values(root))
    if violations:
        print("repo-lint：%d 個違規" % len(violations))
        for v in violations:
            print("  ✗ " + v)
        return 1
    print("repo-lint：全部通過（controller-sudo / owl-template / data-noupdate / reuse-values）")
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
    print("baseline 已更新：%s（%d 檔）、%s（%d 檔）"
          % (os.path.relpath(SUDO_BASELINE), len(counts),
             os.path.relpath(NOUPDATE_BASELINE), len(files)))
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
        elif re.match(r"^src/static/src/.*\.xml$", rel):
            violations = check_owl_template(root, only_file=abs_path)
        elif re.match(r"^src/data/.*\.xml$", rel):
            violations = check_data_noupdate(root, only_file=abs_path)
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

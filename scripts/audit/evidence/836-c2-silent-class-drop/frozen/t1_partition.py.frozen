"""任務一 step2：共用 facts + node-identity map + 集合分割（S 由減法得出）。"""
import ast, importlib.util, os, pathlib, subprocess, sys

ROOT = pathlib.Path("/Users/eugene/Documents/woow/AREA-odoo/woow-addons/wt-836")
SUITE = "src/tests"

spec = importlib.util.spec_from_file_location(
    "_audit", ROOT / "scripts/audit/test_collection_audit.py")
audit = importlib.util.module_from_spec(spec)
sys.modules["_audit"] = audit
spec.loader.exec_module(audit)
lint = audit.repo_lint()

# ── 共用同一顆 facts（leader 指出的陷阱：兩次 parse 出的 node identity 不相等） ──
facts_shared = lint._test_tree_facts(str(ROOT))
_calls = []
def _fake_tree_facts(root):
    _calls.append(root)
    assert root == str(ROOT), "root 不一致：%r" % root
    return facts_shared
lint._test_tree_facts = _fake_tree_facts

# ── node identity map（key = id(node)，呼叫點多寡與歸屬無關） ──
suite = lint.TEST_SUITE_DIR_REL
id2ident, ident2node = {}, {}
for rel in sorted(facts_shared["classes"]):
    if not rel.startswith(suite + "/") or rel.endswith("/__init__.py"):
        continue
    mod = audit._tests_module_path(rel, suite)
    for name, node in sorted(facts_shared["classes"][rel].items()):
        ident = "%s.%s" % (mod, name)
        id2ident[id(node)] = ident
        ident2node[ident] = (rel, name, node)

# ── U：我自己的 plain-ast walker（獨立於 facts） ──
U = set()
for dirpath, dirnames, filenames in os.walk(ROOT / SUITE):
    dirnames[:] = [d for d in dirnames if d != "__pycache__"]
    for fn in sorted(filenames):
        if not fn.endswith(".py") or fn == "__init__.py":
            continue
        fp = os.path.join(dirpath, fn)
        rel = os.path.relpath(fp, ROOT).replace(os.sep, "/")
        tree = ast.parse(open(fp, encoding="utf-8").read())
        mod = rel[len(SUITE) + 1:-3].replace("/", ".")
        U |= {"%s.%s" % (mod, n.name) for n in tree.body
              if isinstance(n, ast.ClassDef)}

print("G2  node-map 命中數 :", len(id2ident), " |U|(my walker):", len(U))
print("G2  map ident 集 == U:", set(id2ident.values()) == U)
assert len(id2ident) == len(U) == len(ident2node), "key 機制壞了，不是資料有事"

# ── G1：證明 scan_expected 內部拿到的就是我手上這顆 node ──
seen_ids = []
_orig_expected_methods = audit._expected_methods
def _spy(lint_, facts_, rel_, name_, node_):
    seen_ids.append(id(node_))
    assert facts_ is facts_shared, "facts 不是同一顆！"
    return _orig_expected_methods(lint_, facts_, rel_, name_, node_)
audit._expected_methods = _spy

expected, excluded = audit.scan_expected(ROOT)
audit._expected_methods = _orig_expected_methods

print("G1  _expected_methods 被呼叫次數:", len(seen_ids),
      " 全部 id 都在我的 map 裡:", all(i in id2ident for i in seen_ids))
print("    _test_tree_facts 被呼叫次數:", len(_calls))

# ── E：committed baseline 檔 ──
raw = subprocess.run(["git", "-C", str(ROOT), "show",
                      "HEAD:scripts/audit/baselines/test-collection.txt"],
                     capture_output=True, text=True, check=True).stdout
E = {}
for line in raw.splitlines():
    s = line.strip()
    if not s or s.startswith("#"):
        continue
    c, _, i = s.partition("\t")
    E[i.strip()] = int(c.strip())

X = {i for i, _ in excluded}
print()
print("P4  |X| excluded    :", len(X), " (list len:", len(excluded), ")")
print("P3  E.keys == expected.keys :", set(E) == set(expected),
      " |expected|:", len(expected))
print("P3b E 的數值也逐項相同      :", E == expected)
print("P5  E ⊆ U:", set(E) <= U, " X ⊆ U:", X <= U, " E ∩ X = ∅:", not (set(E) & X))

S = U - set(E) - X
print("P6  |S| = |U−E−X|   :", len(S))

# ── P7：S 由減法得出，再「預測」它按性質切成 56/22 ──
S56, S22, weird = set(), set(), []
for ident in sorted(S):
    rel, name, node = ident2node[ident]
    kind = lint._test_class_kind(facts_shared, rel, name)
    if kind == lint._TEST_KIND_TAGGED:
        tags = lint._test_effective_tags(facts_shared, rel, name)
        (S56 if "standard" in tags else weird).append(ident) if isinstance(S56, list) else (
            S56.add(ident) if "standard" in tags else weird.append(ident))
    else:
        S22.add(ident)
print("P7  tagged+standard (= `if n:` 那格) :", len(S56))
print("P7  非 tagged        (= own 為空那格) :", len(S22))
print("P7  既非上述兩者（不該存在）          :", len(weird), weird[:5])

import json
out = pathlib.Path("/private/tmp/claude-501/-Users-eugene-Documents-woow-AREA-odoo-woow-addons-wt-836/256a4fa3-cb38-463e-ac53-c019dbe9584e/scratchpad/836")
(out / "S56.txt").write_text("\n".join(sorted(S56)) + "\n")
(out / "S22.txt").write_text("\n".join(sorted(S22)) + "\n")
(out / "X6.txt").write_text("\n".join(sorted(X)) + "\n")
print("\n寫出 S56.txt / S22.txt / X6.txt")

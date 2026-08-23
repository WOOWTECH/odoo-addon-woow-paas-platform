"""任務一 step3：P9 / P10 —— 用 S56 / S22 去驅動真的 C2，對預測。"""
import ast, contextlib, importlib.util, io, os, pathlib, sys

ROOT = pathlib.Path("/Users/eugene/Documents/woow/AREA-odoo/woow-addons/wt-836")
OUT = pathlib.Path("/private/tmp/claude-501/-Users-eugene-Documents-woow-AREA-odoo-woow-addons-wt-836/256a4fa3-cb38-463e-ac53-c019dbe9584e/scratchpad/836")

spec = importlib.util.spec_from_file_location(
    "_audit", ROOT / "scripts/audit/test_collection_audit.py")
audit = importlib.util.module_from_spec(spec)
sys.modules["_audit"] = audit
spec.loader.exec_module(audit)
lint = audit.repo_lint()

facts_shared = lint._test_tree_facts(str(ROOT))
lint._test_tree_facts = lambda root: facts_shared

suite = lint.TEST_SUITE_DIR_REL
ident2node, id2ident = {}, {}
for rel in sorted(facts_shared["classes"]):
    if not rel.startswith(suite + "/") or rel.endswith("/__init__.py"):
        continue
    mod = audit._tests_module_path(rel, suite)
    for name, node in sorted(facts_shared["classes"][rel].items()):
        ident = "%s.%s" % (mod, name)
        ident2node[ident] = node
        id2ident[id(node)] = ident

S56 = set((OUT / "S56.txt").read_text().split())
S22 = set((OUT / "S22.txt").read_text().split())
ID56 = {id(ident2node[i]) for i in S56}
ID22 = {id(ident2node[i]) for i in S22}
assert len(ID56) == 56 and len(ID22) == 22, (len(ID56), len(ID22))

def run_c2():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = audit.check_baseline(ROOT, audit.BASELINE)
    return rc, buf.getvalue()

def report(tag, rc, out):
    plus = [l[4:].strip() for l in out.splitlines() if l.startswith("  + ")]
    minus = [l for l in out.splitlines() if l.startswith("  - ")]
    bang = [l for l in out.splitlines() if l.startswith("  ! ")]
    note = [l for l in out.splitlines() if l.startswith("C2 註記")]
    tilde = [l for l in out.splitlines() if l.startswith("  ~ ")]
    print(f"[{tag}] rc={rc}")
    print(f"[{tag}] '  + ' 行 = {len(plus)}   '  - ' 行 = {len(minus)}   '  ! ' 行 = {len(bang)}")
    print(f"[{tag}] 註記 header: {note[0] if note else '(無)'}   '  ~ ' 行 = {len(tilde)}")
    return set(plus)

# ── P9：只換 _expected_methods（全 repo 唯一呼叫點，:298） ──
orig_em = audit._expected_methods
def fake_em(lint_, facts_, rel_, name_, node_):
    if id(node_) in ID56:
        return {"__injected_test_method__"}
    return orig_em(lint_, facts_, rel_, name_, node_)
audit._expected_methods = fake_em
rc9, out9 = run_c2()
audit._expected_methods = orig_em
plus9 = report("P9", rc9, out9)
print("[P9] '+' ident 集 == S56 逐字相同:", plus9 == S56)
print("[P9] 差異:", sorted(plus9 ^ S56)[:5])
(OUT / "p9_output.txt").write_text(out9)

print()
# ── P10：只換 scan_expected 內那一格（frame 判呼叫點，避開另外 5 個呼叫點） ──
orig_own = lint._test_own_test_methods
SE_CODE = audit.scan_expected.__code__
hits = []
def fake_own(node):
    f = sys._getframe(1)
    if f.f_code is SE_CODE and id(node) in ID22:
        hits.append((id(node), f.f_lineno))
        return ["__injected_test_method__"]
    return orig_own(node)
lint._test_own_test_methods = fake_own
rc10, out10 = run_c2()
lint._test_own_test_methods = orig_own
plus10 = report("P10", rc10, out10)
print("[P10] 假回傳命中次數:", len(hits), " 命中的呼叫點行號:", sorted({l for _, l in hits}))
(OUT / "p10_output.txt").write_text(out10)

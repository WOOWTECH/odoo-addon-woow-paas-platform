"""任務一 step1：我自己的 plain-ast 枚舉（不碰 _test_tree_facts / repo-lint）。"""
import ast, os, subprocess, sys

ROOT = "/Users/eugene/Documents/woow/AREA-odoo/woow-addons/wt-836"
SUITE = "src/tests"

# ---- U：全集 ----
U = set()
files = 0
for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, SUITE)):
    dirnames[:] = [d for d in dirnames if d != "__pycache__"]
    for fn in sorted(filenames):
        if not fn.endswith(".py") or fn == "__init__.py":
            continue
        fp = os.path.join(dirpath, fn)
        rel = os.path.relpath(fp, ROOT).replace(os.sep, "/")
        files += 1
        tree = ast.parse(open(fp, encoding="utf-8").read())
        mod = rel[len(SUITE) + 1:-3].replace("/", ".")
        for n in tree.body:
            if isinstance(n, ast.ClassDef):
                U.add("%s.%s" % (mod, n.name))

# ---- E：committed baseline 檔（git show HEAD:） ----
raw = subprocess.run(
    ["git", "-C", ROOT, "show", "HEAD:scripts/audit/baselines/test-collection.txt"],
    capture_output=True, text=True, check=True).stdout
E = {}
data_lines = 0
for line in raw.splitlines():
    s = line.strip()
    if not s or s.startswith("#"):
        continue
    data_lines += 1
    cnt, _, ident = s.partition("\t")
    E[ident.strip()] = int(cnt.strip())

print("files scanned      :", files)
print("P1  |U|            :", len(U))
print("P2  baseline 資料行 :", data_lines, " |E| keys:", len(E))
print("     重複 key       :", data_lines - len(E))

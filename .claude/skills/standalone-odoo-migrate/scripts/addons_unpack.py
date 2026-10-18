#!/usr/bin/env python3
"""addons_unpack.py — 解開來源站導出的 addon tar，逐模組對盤點檔驗內容 hash（standalone-odoo-migrate，#1604）。

用法（由 source/export-addons.sh 呼叫；也可單獨重驗）：
  addons_unpack.py --inventory inventory-<ns>-….json --out <dir> [--modules a,b] <addon 根目錄>=<tar> [...]

輸入：
  --inventory  source/inventory.sh 的輸出；`inspect.addons.modules` 內有 digest 的就是非 core 模組
  <根目錄>=<tar>  在來源容器內 `tar -cf - -C <根目錄> <模組…>` 的輸出；同一個根目錄可以有多顆
                 （根目錄本身是 symlink 的模組各自一顆，#1844），同一個模組只能出現在其中一顆
輸出：
  <dir>/addons/<模組>/…        原始碼（不含 .git／__pycache__／*.pyc，與內容 hash 的範圍相同）
  <dir>/addons-export.json     每個模組的來源目錄、版本、sha256、檔數、bytes、git 出處

tar 來自客戶的容器，一律當不可信資料：只收一般檔、目錄與 symlink；絕對路徑、`..`、裝置檔、hardlink 都拒收。
symlink 等全部檔案寫完才建立，而且不允許任何成員的路徑經過 symlink ⇒ 不會有東西被寫到目的地以外。指向模組外
的 symlink 照樣保留（內容 hash 計入的是連結目標字串），在報告的 symlinks_outside 列出，匯入 paas-odoo-ci 前要人處理。
內容 hash 用 odoo_inspect.dir_digest（與盤點同一支函式）——hash 相等＝本機拿到的原始碼
與盤點當下容器內的一致；kubectl exec 串流被截斷、或盤點後原始碼被改，都會在這裡被擋下。
exit：0 成功、2 輸入錯誤、3 tar 損毀或內容不合規、4 內容 hash 與盤點不一致
"""
import argparse
import json
import os
import posixpath
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from odoo_inspect import dir_digest  # noqa: E402


class UnpackError(Exception):
    """tar 損毀或內容不合規（exit 3）。"""


def wanted_modules(inventory, only=None):
    """盤點檔內的非 core 模組（有 digest 的）；only 給了就只留那些，缺的名字 raise ValueError。"""
    modules = {name: meta for name, meta in inventory["inspect"]["addons"]["modules"].items() if meta.get("digest")}
    if only:
        missing = sorted(set(only) - set(modules))
        if missing:
            raise ValueError("not a non-core module in the inventory: %s" % ", ".join(missing))
        modules = {name: modules[name] for name in only}
    return modules


def unpack(tar_path, base_dir, expected, dest):
    """把一顆 tar 解到 dest；最上層只能是 expected（這個 addon 根目錄該有的模組名）。

    回傳 (解出的模組名集合, 指向模組外的 symlink 清單 [(成員路徑, 目標)])。
    """
    seen, links, paths = set(), [], []
    # tarfile 把非開頭處的截斷 header 當成正常結尾 ⇒ 先自己確認結尾：完整的 tar 以兩個全零的 512 bytes 區塊收尾
    size = Path(tar_path).stat().st_size
    with open(tar_path, "rb") as fh:
        fh.seek(max(size - 1024, 0))
        tail = fh.read()
    if size < 1024 or size % 512 or tail.strip(b"\0"):
        raise UnpackError("%s: no end-of-archive marker (%d bytes; truncated stream?)" % (tar_path, size))
    try:
        with tarfile.open(tar_path, mode="r:") as tar:
            for member in tar:
                parts = Path(member.name).parts
                if not parts or parts[0] in ("/", "..") or ".." in parts or member.name.startswith("/"):
                    raise UnpackError("%s: unsafe path %r" % (tar_path, member.name))
                if parts[0] not in expected:
                    raise UnpackError("%s: unexpected top-level entry %r (not a module of %s)" % (
                        tar_path, parts[0], base_dir))
                if not (member.isreg() or member.isdir() or member.issym()):
                    raise UnpackError("%s: %r is not a regular file, directory or symlink" % (tar_path, member.name))
                name = posixpath.normpath(member.name)
                paths.append(name)
                if member.issym():
                    links.append((name, member.linkname))
                else:
                    try:
                        tar.extract(member, dest, filter="data")
                    except tarfile.FilterError as exc:
                        raise UnpackError("%s: %s" % (tar_path, exc)) from exc
                seen.add(parts[0])
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise UnpackError("%s: %s (truncated stream?)" % (tar_path, exc)) from exc
    outside = []
    link_paths = {name for name, _target in links}
    for name in paths:
        if any(name.startswith(link + "/") for link in link_paths):
            raise UnpackError("%s: %r is beneath a symlink" % (tar_path, name))
    for name, target in links:
        full = Path(dest) / name
        if full.exists() or full.is_symlink():
            raise UnpackError("%s: symlink %r collides with another entry" % (tar_path, name))
        full.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, full)
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
        module = name.split("/", 1)[0]
        if target.startswith("/") or not (resolved + "/").startswith(module + "/"):
            outside.append((name, target))
    return seen, outside


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--modules", help="只取這些模組（逗號分隔）；預設盤點檔內所有非 core 模組")
    parser.add_argument("tars", nargs="+", metavar="<addon 根目錄>=<tar>")
    args = parser.parse_args(argv)
    try:
        inventory = json.loads(Path(args.inventory).read_text())
        only = [m.strip() for m in args.modules.split(",") if m.strip()] if args.modules else None
        modules = wanted_modules(inventory, only)
        tars = {}
        for item in args.tars:
            base_dir, tar_path = item.split("=", 1)
            tars.setdefault(base_dir, []).append(tar_path)
    except (OSError, ValueError, KeyError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    out = Path(args.out)
    dest = out / "addons"
    if dest.exists() and any(dest.iterdir()):
        print("ERROR: %s is not empty (unpack into a new directory)" % dest, file=sys.stderr)
        return 2
    by_dir = {}
    for name, meta in modules.items():
        by_dir.setdefault(meta["dir"], set()).add(name)
    if set(by_dir) != set(tars):
        print("ERROR: tar set %s does not match addon dirs %s" % (sorted(tars), sorted(by_dir)), file=sys.stderr)
        return 2
    dest.mkdir(parents=True, exist_ok=True)
    outside = {}
    try:
        for base_dir, names in sorted(by_dir.items()):
            seen_all = set()
            for tar_path in tars[base_dir]:
                seen, links = unpack(tar_path, base_dir, names, dest)
                if seen & seen_all:
                    raise UnpackError("%s: modules %s already came from another tar of %s" % (
                        tar_path, ", ".join(sorted(seen & seen_all)), base_dir))
                seen_all |= seen
                for name, target in links:
                    outside.setdefault(name.split("/", 1)[0], []).append({"path": name, "target": target})
            if seen_all != names:
                raise UnpackError("%s: missing modules %s" % (
                    ", ".join(tars[base_dir]), ", ".join(sorted(names - seen_all))))
    except UnpackError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 3
    report, mismatched = {}, []
    for name, meta in sorted(modules.items()):
        got = dir_digest(dest / name)
        if got["sha256"] != meta["digest"]["sha256"]:
            mismatched.append("%s: inventory %s (%d files) != exported %s (%d files)" % (
                name, meta["digest"]["sha256"][:12], meta["digest"]["files"], got["sha256"][:12], got["files"]))
        report[name] = {"dir": meta["dir"], "version": meta.get("version"), "sha256": got["sha256"],
                        "files": got["files"], "bytes": got["bytes"], "git": meta.get("git"),
                        "symlinks_outside": outside.get(name, []),
                        "root_symlink": meta.get("root_symlink")}
    if mismatched:
        print("ERROR: content hash differs from the inventory (stream truncated, or source changed since the "
              "inventory — rerun inventory.sh):\n  " + "\n  ".join(mismatched), file=sys.stderr)
        return 4
    (out / "addons-export.json").write_text(json.dumps({
        "schema_version": 1, "kind": "standalone-odoo-addons-export",
        "generated_at": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "source": inventory.get("source"), "inventory_generated_at": inventory.get("generated_at"),
        "modules": report,
    }, ensure_ascii=False, indent=2) + "\n")
    print("addons export verified: %d modules, %d files, %d bytes -> %s" % (
        len(report), sum(m["files"] for m in report.values()), sum(m["bytes"] for m in report.values()), dest),
        file=sys.stderr)
    for name, items in sorted(outside.items()):
        print("  note: %s has %d symlink(s) pointing outside the module (e.g. %s -> %s)" % (
            name, len(items), items[0]["path"], items[0]["target"]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

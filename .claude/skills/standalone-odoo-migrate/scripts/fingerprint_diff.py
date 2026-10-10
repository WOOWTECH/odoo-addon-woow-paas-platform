#!/usr/bin/env python3
"""fingerprint_diff.py — 比對來源與目標的資料指紋（standalone-odoo-migrate，#1604）。

用法：
  fingerprint_diff.py --source-db src.fp --target-db dst.fp [--source-fs src.fs --target-fs dst.fs]
                      [--stage restore|upgrade] [--json out.json]

輸入：
  *.fp  fingerprint.sql 的輸出（`table|名稱|筆數|hash`、`seq|…`、`ext|…`、`meta|…`）
  *.fs  filestore 逐檔清單：在 filestore/<db> 目錄內跑
        `find . -type f -printf '%P\\0' | sort -z | xargs -0 -r sha256sum`

兩個階段的判準不同：
  restore  還原後、Odoo 啟動前：每張表、每個 sequence、每個檔都必須一致（逐位元）。extension 版本可不同（另列）。
  upgrade  升級閘跑完 -u all 之後：模組 metadata 表（ir_*、res_groups* 等）可以變，列為「預期內」；
           其餘表有任何變動都列「要人工確認」。filestore 只允許多出 /web/assets 產物，不允許少檔或內容改變。
exit：0 通過、1 不通過、2 輸入錯誤。
"""
import argparse
import json
import re
import sys
from pathlib import Path

# -u all 會重寫的模組 metadata（Odoo 的 XML data 與 registry 自動同步的表）
METADATA_PATTERNS = [re.compile(p) for p in (
    r"^ir_", r"^res_groups", r"^rule_group_rel$", r"^res_lang$", r"^res_country", r"^res_currency$",
    r"^report_paperformat$", r"^mail_template", r"^base_", r"_group_rel$", r"_groups_rel$",
)]


def is_metadata(table):
    return any(p.search(table) for p in METADATA_PATTERNS)


def parse_fp(path):
    tables, seqs, exts, meta = {}, {}, {}, {}
    for line in Path(path).read_text().splitlines():
        parts = line.split("|")
        if parts[0] == "table" and len(parts) == 4:
            tables[parts[1]] = (int(parts[2]), parts[3])
        elif parts[0] == "seq" and len(parts) == 3:
            seqs[parts[1]] = parts[2]
        elif parts[0] == "ext" and len(parts) == 3:
            exts[parts[1]] = parts[2]
        elif parts[0] == "meta" and len(parts) == 3:
            meta[parts[1]] = parts[2]
    if not tables:
        raise ValueError("%s: no table lines (not fingerprint.sql output?)" % path)
    return {"tables": tables, "seqs": seqs, "exts": exts, "meta": meta}


def parse_fs(path):
    files = {}
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        digest, rel = line.split(None, 1)
        files[rel.strip().lstrip("*")] = digest
    return files


def diff_db(src, dst, stage):
    problems, expected = [], []
    for table in sorted(set(src["tables"]) | set(dst["tables"])):
        a, b = src["tables"].get(table), dst["tables"].get(table)
        if a == b:
            continue
        if a is None or b is None:
            desc = "%s：只在%s" % (table, "目標" if a is None else "來源")
        else:
            desc = "%s：筆數 %d → %d%s" % (table, a[0], b[0], "，內容不同" if a[0] == b[0] else "")
        (expected if stage == "upgrade" and is_metadata(table) else problems).append(desc)
    for seq in sorted(set(src["seqs"]) | set(dst["seqs"])):
        a, b = src["seqs"].get(seq), dst["seqs"].get(seq)
        if a == b:
            continue
        seq_table = seq[:-len("_id_seq")] if seq.endswith("_id_seq") else seq
        desc = "sequence %s：%s → %s" % (seq, a, b)
        (expected if stage == "upgrade" and is_metadata(seq_table) else problems).append(desc)
    ext_changes = ["%s：%s → %s" % (e, src["exts"].get(e), dst["exts"].get(e))
                   for e in sorted(set(src["exts"]) | set(dst["exts"])) if src["exts"].get(e) != dst["exts"].get(e)]
    return problems, expected, ext_changes


def diff_fs(src, dst, stage):
    problems, expected = [], []
    missing = sorted(set(src) - set(dst))
    changed = sorted(f for f in set(src) & set(dst) if src[f] != dst[f])
    extra = sorted(set(dst) - set(src))
    if missing:
        problems.append("目標少了 %d 個檔（例：%s）" % (len(missing), ", ".join(missing[:5])))
    if changed:
        problems.append("%d 個檔內容不同（例：%s）" % (len(changed), ", ".join(changed[:5])))
    if extra:
        # filestore 是內容定址（<hash 前兩碼>/<sha1>），-u all 重產 asset bundle 只會多檔、不會改檔
        (expected if stage == "upgrade" else problems).append("目標多了 %d 個檔" % len(extra))
    return problems, expected, {"source_files": len(src), "target_files": len(dst)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--target-db", required=True)
    parser.add_argument("--source-fs")
    parser.add_argument("--target-fs")
    parser.add_argument("--stage", choices=("restore", "upgrade"), default="restore")
    parser.add_argument("--json")
    args = parser.parse_args(argv)
    if bool(args.source_fs) != bool(args.target_fs):
        print("ERROR: --source-fs and --target-fs go together", file=sys.stderr)
        return 2
    try:
        src, dst = parse_fp(args.source_db), parse_fp(args.target_db)
        problems, expected, ext_changes = diff_db(src, dst, args.stage)
        fs_stats = None
        if args.source_fs:
            fs_problems, fs_expected, fs_stats = diff_fs(parse_fs(args.source_fs), parse_fs(args.target_fs), args.stage)
            problems += fs_problems
            expected += fs_expected
    except (OSError, ValueError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    ok = not problems
    report = {"stage": args.stage, "ok": ok, "tables": len(src["tables"]), "problems": problems,
              "expected_changes": expected, "extension_changes": ext_changes, "filestore": fs_stats}
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print("資料指紋比對（%s 階段）：%s，%d 張表%s" % (
        args.stage, "通過" if ok else "不通過", len(src["tables"]),
        "，filestore %d／%d 檔" % (fs_stats["source_files"], fs_stats["target_files"]) if fs_stats else ""))
    for title, items in (("不一致", problems), ("預期內的變動", expected), ("extension 版本變動", ext_changes)):
        if items:
            print("\n%s：" % title)
            print("\n".join("- " + i for i in items))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

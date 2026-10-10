#!/usr/bin/env python3
"""fingerprint_diff.py — 比對來源與目標的資料指紋（standalone-odoo-migrate，#1604）。

用法：
  fingerprint_diff.py --source-db src.fp --target-db dst.fp [--source-fs src.fs --target-fs dst.fs]
                      [--source-business baseline.bfp --target-business upgraded.bfp]
                      [--stage restore|upgrade] [--json out.json]

輸入：
  *.fp  fingerprint.sql 的輸出（`table|名稱|筆數|hash`、`seq|…`、`ext|…`、`meta|…`）
  *.fs  filestore 逐檔清單：在 filestore/<db> 目錄內跑
        `find . -type f -printf '%P\\0' | sort -z | xargs -0 -r sha256sum`
  *.bfp fingerprint_business.sql 的輸出（客戶資料列指紋；只在 upgrade 階段用）

兩個階段的判準不同：
  restore  還原後、Odoo 啟動前：每張表、每個 sequence、每個檔都必須一致（逐位元）。extension 版本可不同（另列）。
  upgrade  升級閘跑完 -u all 之後。有給 *.bfp 時以客戶資料列為準：基準當時就存在的客戶資料列（不含模組可更新的
           資料列、write_date／write_uid）必須完全相同；模組資料列被重寫、Odoo 啟動後新增的列、registry 表的變動
           列為「預期內」。沒給 *.bfp 時退回整表比對：只有模組 metadata 表（ir_*、res_groups* 等）可以變。
           sequence 只能前進。filestore 只允許多出檔案，不允許少檔或內容改變。
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


# 客戶資料列比對時仍會變的表：registry 自己維護（非模組資料列也會被重建）或 Odoo 一啟動就會改寫既有列
REGISTRY_PATTERNS = [re.compile(p) for p in (
    r"^ir_model", r"^ir_module_", r"^ir_cron", r"^ir_act_window_view$", r"^ir_logging$", r"^ir_profile$",
    r"^bus_", r"^res_users_log$", r"^res_groups", r"^rule_group_rel$", r"^module_country$", r"^base_",
    r"_group_rel$", r"_groups_rel$",
)]


def is_metadata(table):
    return any(p.search(table) for p in METADATA_PATTERNS)


def is_registry(table):
    return any(p.search(table) for p in REGISTRY_PATTERNS)


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


def parse_bfp(path):
    out = {"tables": {}, "maxid": {}, "added": {}, "missing": []}
    for line in Path(path).read_text().splitlines():
        parts = line.split("|")
        if parts[0] == "btable" and len(parts) == 4:
            out["tables"][parts[1]] = (int(parts[2]), parts[3])
        elif parts[0] == "bmax" and len(parts) == 3:
            out["maxid"][parts[1]] = int(parts[2])
        elif parts[0] == "badded" and len(parts) == 3:
            out["added"][parts[1]] = int(parts[2])
        elif parts[0] == "bmissing" and len(parts) == 3:
            out["missing"].append((parts[1], parts[2]))
    if not out["tables"]:
        raise ValueError("%s: no btable lines (not fingerprint_business.sql output?)" % path)
    return out


def diff_business(base, cur):
    """客戶資料列比對。回傳 (problems, expected, 客戶資料列不同的表)。"""
    problems, expected, differing = [], [], set()
    for table, col in cur["missing"]:
        problems.append("%s：欄 %s 在升級後不見了" % (table, col))
    for table in sorted(base["tables"]):
        a, b = base["tables"][table], cur["tables"].get(table)
        if a == b:
            continue
        differing.add(table)
        if b is None:
            problems.append("%s：升級後整張表不見了（客戶資料 %d 列）" % (table, a[0]))
        elif is_registry(table):
            expected.append("%s：registry／執行期表，%d → %d 列" % (table, a[0], b[0]))
        elif table not in base["maxid"] and b[0] > a[0]:
            expected.append("%s：%d → %d 列（沒有 id 欄，無法確認舊列是否原封不動）" % (table, a[0], b[0]))
        else:
            problems.append("%s：客戶資料列有變動（%d → %d 列%s）" % (
                table, a[0], b[0], "，內容不同" if a[0] == b[0] else ""))
    for table in sorted(cur["added"]):
        if not is_registry(table):
            expected.append("%s：升級後新增 %d 列" % (table, cur["added"][table]))
    return problems, expected, differing


def parse_fs(path):
    files = {}
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        digest, rel = line.split(None, 1)
        files[rel.strip().lstrip("*")] = digest
    return files


def seq_advanced(a, b):
    """sequence 沒有倒退（null＝還沒取過號，視為 0）。"""
    try:
        return int(b if b not in (None, "null") else 0) >= int(a if a not in (None, "null") else 0)
    except ValueError:
        return False


def diff_db(src, dst, stage, business=False):
    """business=True：客戶資料列另由 diff_business 判定，整表差異只剩「模組資料列被重寫」，全部列為預期內。"""
    problems, expected = [], []
    upgrade = stage == "upgrade"
    for table in sorted(set(src["tables"]) | set(dst["tables"])):
        a, b = src["tables"].get(table), dst["tables"].get(table)
        if a == b:
            continue
        if a is None:
            desc = "%s：只在目標（升級新增的表）" % table if upgrade and business else "%s：只在目標" % table
            (expected if upgrade and (business or is_metadata(table)) else problems).append(desc)
        elif b is None:
            if not (upgrade and business):  # business 模式由 diff_business 報「整張表不見了」
                (expected if upgrade and is_metadata(table) else problems).append("%s：只在來源" % table)
        else:
            desc = "%s：筆數 %d → %d%s" % (table, a[0], b[0], "，內容不同" if a[0] == b[0] else "")
            (expected if upgrade and (business or is_metadata(table)) else problems).append(desc)
    for seq in sorted(set(src["seqs"]) | set(dst["seqs"])):
        a, b = src["seqs"].get(seq), dst["seqs"].get(seq)
        if a == b:
            continue
        seq_table = seq[:-len("_id_seq")] if seq.endswith("_id_seq") else seq
        desc = "sequence %s：%s → %s" % (seq, a, b)
        if upgrade and business:
            ok = b is None and is_registry(seq_table) or b is not None and (a is None or seq_advanced(a, b))
            (expected if ok else problems).append(desc if ok else desc + "（倒退或消失）")
        else:
            (expected if upgrade and is_metadata(seq_table) else problems).append(desc)
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
    parser.add_argument("--source-business", help="baseline.bfp（還原後、Odoo 啟動前）")
    parser.add_argument("--target-business", help="upgraded.bfp（升級閘跑完後）")
    parser.add_argument("--stage", choices=("restore", "upgrade"), default="restore")
    parser.add_argument("--json")
    args = parser.parse_args(argv)
    if bool(args.source_fs) != bool(args.target_fs):
        print("ERROR: --source-fs and --target-fs go together", file=sys.stderr)
        return 2
    if bool(args.source_business) != bool(args.target_business):
        print("ERROR: --source-business and --target-business go together", file=sys.stderr)
        return 2
    if args.source_business and args.stage != "upgrade":
        print("ERROR: --source-business only applies to --stage upgrade", file=sys.stderr)
        return 2
    try:
        src, dst = parse_fp(args.source_db), parse_fp(args.target_db)
        business = bool(args.source_business)
        problems, expected, ext_changes = diff_db(src, dst, args.stage, business)
        if business:
            b_problems, b_expected, differing = diff_business(
                parse_bfp(args.source_business), parse_bfp(args.target_business))
            # 整表有差、客戶資料列沒差的表不逐張列（幾十張）；只留張數
            rewritten = [e for e in expected if not e.startswith("sequence ") and e.split("：", 1)[0] not in differing]
            expected = [e for e in expected if e.startswith("sequence ") or e.split("：", 1)[0] in differing]
            expected = b_expected + ["%d 張表只有模組資料列或 write_date 被重寫" % len(rewritten)] * bool(rewritten) + [
                e for e in expected if e.startswith("sequence ")]
            problems = b_problems + problems
        fs_stats = None
        if args.source_fs:
            fs_problems, fs_expected, fs_stats = diff_fs(parse_fs(args.source_fs), parse_fs(args.target_fs), args.stage)
            problems += fs_problems
            expected += fs_expected
    except (OSError, ValueError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    ok = not problems
    report = {"stage": args.stage, "ok": ok, "tables": len(src["tables"]),
              "mode": "business" if args.source_business else "full", "problems": problems,
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

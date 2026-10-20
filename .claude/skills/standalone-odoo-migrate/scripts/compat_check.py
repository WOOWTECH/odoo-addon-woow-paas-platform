#!/usr/bin/env python3
"""compat_check.py — 來源站盤點 × 目標 image（× paas-odoo-ci）相容性比對（standalone-odoo-migrate，#1604）。

用法：
  compat_check.py --inventory inventory-<ns>.json --target image-inventory-<digest>.json \\
                  [--ci-repo <paas-odoo-ci checkout>] [--accept-diverged a,b] [--json report.json]

輸入：
  --inventory  source/inventory.sh 的輸出（WMO 跑）
  --target     target/image-inventory.sh 的輸出（目標 cloud-odoo image）
  --ci-repo    paas-odoo-ci 的 checkout（讀 addons/、addons-cloud/ 與 config/cloud-addons.list）；給了才能分辨
               「只要加進清單」與「要匯入原始碼」

每個 DB 內已安裝的模組歸成一類：
  OK            目標 image 已有、版本 ≥ DB；非 core 的內容 hash 與來源相同
  SELECT        目標 image 的 /all-addons 已有 ⇒ 服務要勾選（cloud_odoo_addon）才會進 addons_path
  NEED_LIST     paas-odoo-ci addons/ 已有原始碼、不在 cloud-addons.list ⇒ 加進清單、重 build image
  NEED_IMPORT   只有來源站有原始碼 ⇒ 匯入 paas-odoo-ci addons/＋加進清單
  DIVERGED      目標（或 paas-odoo-ci）有同名模組但程式碼與來源不同 ⇒ 要人決定用哪一份
                裁決「用目標／paas-odoo-ci 那份」之後以 --accept-diverged 列出，該模組改照一般規則歸類
                （OK／SELECT／NEED_LIST），報告另列「已裁決的差異」；版本低於 DB 的仍然阻斷
  BLOCK_DOWNGRADE  可用的程式碼版本低於 DB 內版本（Odoo 不支援降版）
  BLOCK_MISSING    DB 已安裝、但來源與目標都找不到原始碼

stdout 印 Markdown 報告。exit：0＝全 OK、可進彩排；1＝有待辦（NEED_*／SELECT／DIVERGED／注意事項）；
3＝有阻斷（BLOCK_*）；2＝輸入錯誤。
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from odoo_inspect import scan_addons, version_key  # noqa: E402

INSTALLED_STATES = {"installed", "to upgrade", "to remove"}
PENDING_STATES = {"to install", "to upgrade", "to remove"}
# 目標 PG major（woow-odoo chart 的 paas-postgres image）；manual restore 允許的 extension
TARGET_PG_MAJOR = 17
ALLOWED_EXTENSIONS = {"plpgsql", "pg_trgm", "vector", "unaccent"}
BLOCK = ("BLOCK_DOWNGRADE", "BLOCK_MISSING")
ACTION = ("NEED_IMPORT", "NEED_LIST", "DIVERGED", "SELECT")
# woow-odoo chart 的 addons-copy：這些目錄裡的模組要服務勾選（addons.selected）才進 addons_path
SELECT_DIRS = frozenset({"/all-addons"})


def norm_dist(name):
    """PyPI 名稱正規化（PEP 503）。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def load_ci(ci_repo):
    if not ci_repo:
        return None
    root = Path(ci_repo)
    listed = set()
    list_file = root / "config" / "cloud-addons.list"
    if list_file.exists():
        for line in list_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                listed.add(line)
    # addons/＝平台 image 也帶；addons-cloud/＝只進 cloud-odoo（客戶站搬進來的 addon 放這裡）
    scanned = scan_addons([str(root / "addons"), str(root / "addons-cloud")], digest_filter=lambda _d: True)
    return {"modules": scanned["modules"], "listed": listed}


def classify(inv, tgt, ci, select_dirs=SELECT_DIRS, accepted=frozenset()):
    db_modules = inv["db"]["modules"]
    src = inv["addons"]["modules"]
    tmods = tgt["addons"]["modules"]
    rows = []
    for name in sorted(db_modules):
        info = db_modules[name]
        if info["state"] not in INSTALLED_STATES:
            continue
        dv = info.get("version")
        s = src.get(name)
        s_custom = bool(s and s.get("digest"))
        row = {"module": name, "state": info["state"], "db_version": dv,
               "source_dir": s and s["dir"], "source_version": s and s["version"]}
        t = tmods.get(name)
        c = ci and ci["modules"].get(name)

        def too_old(version):
            return bool(dv and version and version_key(version) < version_key(dv))

        if t:
            row.update(target_dir=t["dir"], target_version=t["version"])
            if too_old(t["version"]):
                row["status"] = "BLOCK_DOWNGRADE"
            elif (s_custom and (not t.get("digest") or t["digest"]["sha256"] != s["digest"]["sha256"])
                  and name not in accepted):
                row["status"] = "DIVERGED"
                row["note"] = ("來源以客製副本覆蓋 core 模組" if not t.get("digest")
                               else "目標 image 內的程式碼與來源不同")
            elif t["dir"] in select_dirs:
                row["status"] = "SELECT"
            else:
                row["status"] = "OK"
        elif c:
            row.update(ci_version=c["version"], ci_listed=name in ci["listed"])
            if too_old(c["version"]):
                row["status"] = "BLOCK_DOWNGRADE"
                row["note"] = "paas-odoo-ci 的版本低於 DB"
            elif s_custom and c["digest"]["sha256"] != s["digest"]["sha256"] and name not in accepted:
                row["status"] = "DIVERGED"
                row["note"] = "paas-odoo-ci 的程式碼與來源不同"
            else:
                row["status"] = "NEED_LIST"
                if row["ci_listed"]:
                    row["note"] = "已在 cloud-addons.list，目標 image 還沒包含（換新 image）"
        elif s_custom:
            row["status"] = "BLOCK_DOWNGRADE" if too_old(s["version"]) else "NEED_IMPORT"
            if row["status"] == "BLOCK_DOWNGRADE":
                row["note"] = "來源站的程式碼版本低於 DB（manifest 未同步？）"
        else:
            row["status"] = "BLOCK_MISSING"
            row["note"] = ("來源 core 有、目標 core 沒有" if s else "來源與目標都找不到原始碼")
        other = t or c
        if (name in accepted and s_custom and other and row["status"] not in BLOCK
                and (other.get("digest") or {}).get("sha256") != s["digest"]["sha256"]):
            row["accepted_divergence"] = True
            row["note"] = "已裁決：用%s那份（與來源站的程式碼不同）" % ("目標 image " if t else " paas-odoo-ci ")
        rows.append(row)
    return rows


def python_deps(rows, inv, ci):
    """需要進 image 的模組所宣告的 external_dependencies.python。"""
    deps = {}
    for row in rows:
        if row["status"] not in ("NEED_IMPORT", "NEED_LIST", "DIVERGED", "SELECT"):
            continue
        manifest = inv["addons"]["modules"].get(row["module"])
        if row["status"] == "NEED_LIST" and ci:
            manifest = ci["modules"].get(row["module"]) or manifest
        for dep in ((manifest or {}).get("external_dependencies") or {}).get("python", []):
            deps.setdefault(dep, []).append(row["module"])
    return deps


def pip_gaps(deps, inv, tgt):
    """回傳 (模組宣告但目標缺的依賴, 來源容器有但目標 image 沒有的套件)。

    第二項比的是兩邊**實際安裝**的 distribution，不解析 pip 指令字串：standalone 站常在 initContainer
    跑 pip，那些套件主容器看不到；只有 inspect 量到的清單才是真的。
    """
    tdists = {norm_dist(n) for n in tgt["pip"]["distributions"]}
    timports = set(tgt["pip"].get("import_map", {}))
    simports = inv["pip"].get("import_map", {})
    sdists = {norm_dist(n): n for n in inv["pip"]["distributions"]}
    missing = []
    for dep, modules in sorted(deps.items()):
        req = re.split(r"[<>=!~;\[ ]", dep, 1)[0]
        if norm_dist(req) in tdists or req in timports:
            continue
        missing.append({"requirement": dep, "modules": modules,
                        "source_dists": simports.get(req) or ([sdists[norm_dist(req)]] if norm_dist(req) in sdists else [])})
    source_only = sorted(n for n in inv["pip"]["distributions"] if norm_dist(n) not in tdists)
    return missing, source_only


def cluster_internal(host):
    """主機名是不是只在來源叢集內解析得到／連得到（K8s service 名稱或私有位址）。"""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", host):
        a, b = (int(x) for x in host.split(".")[:2])
        return a in (10, 127) or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168) or (a == 100 and 64 <= b <= 127)
    return "." not in host or host.endswith((".svc", ".cluster.local", ".local", ".internal"))


def notes(inv, tgt, k8s):
    out, blocks = [], []
    db = inv["db"]
    if inv.get("core_date") and tgt.get("core_date") and \
            version_key(tgt["core_date"]) < version_key(inv["core_date"]):
        blocks.append("目標 image core %s 比來源 %s 舊：升級閘方向檢查會 exit 78" % (tgt["core_date"], inv["core_date"]))
    major = int(str(db["server_version"]).split(".")[0])
    if major > TARGET_PG_MAJOR:
        blocks.append("來源 PG %s 比目標 PG %d 新：pg_restore 不支援" % (db["server_version"], TARGET_PG_MAJOR))
    unknown = sorted(set(db["extensions"]) - ALLOWED_EXTENSIONS)
    if unknown:
        blocks.append("不支援的 extension：%s（restore 只建 %s）" % (", ".join(unknown), ", ".join(sorted(ALLOWED_EXTENSIONS))))
    if db.get("extra_schemas"):
        out.append("public 以外的 schema：%s（restore 要確認一併帶到）" % ", ".join(db["extra_schemas"]))
    if db.get("datlocprovider") not in (None, "c"):
        out.append("datlocprovider=%s（不是 libc），restore 建庫要另外處理" % db["datlocprovider"])
    if db.get("vector_columns"):
        out.append("有 vector 欄位 %d 個：restore 必須預建 vector extension" % len(db["vector_columns"]))
    pending = {k: v for k, v in db["module_states"].items() if k in PENDING_STATES}
    if pending:
        out.append("來源有未完成的模組操作 %s：凍結前要先處理掉" % pending)
    fs = inv["filestore"]
    if fs.get("missing_referenced"):
        out.append("來源 filestore 已缺 %d 個被引用的檔（搬遷前就缺，搬完一樣缺）" % fs["missing_referenced"])
    for sh in inv["addons"]["shadowed"]:
        out.append("模組 %s 在 %s 的副本被 %s 蓋掉（以生效者為準）" % (sh["module"], sh["dir"], sh["shadowed_by"]))
    for err in inv["addons"]["errors"]:
        out.append("manifest 讀不到：%s（%s）" % (err["module"], err["error"]))
    if "woow_paas_platform.upgrade_done" in db.get("params_present", []):
        out.append("來源 DB 已有 upgrade_done marker：預寫前要確認來源")
    if "database.is_neutralized" in db.get("params_present", []):
        blocks.append("來源 DB 帶中和標記 database.is_neutralized：不是正式資料")
    conf = inv.get("conf", {})
    if conf.get("smtp_server") and (conf.get("smtp_port") == "465" or conf.get("smtp_ssl") == "True"):
        out.append("conf 層 SMTP（port %s／ssl %s）：tenant chart 不支援 465，改 DB ir.mail_server"
                   % (conf.get("smtp_port"), conf.get("smtp_ssl")))
    plan_keys = [k for k in ("workers", "limit_memory_soft", "limit_memory_hard") if conf.get(k)]
    if plan_keys:
        out.append("來源 conf %s：目標的 workers 與記憶體上限由購買的方案決定，不能逐台覆寫 ⇒ 依這組數字選方案"
                   % "、".join("%s=%s" % (k, conf[k]) for k in plan_keys))
    admin_keys = [(k, v) for k, v in (("limit_time_cpu", "conf.limitTimeCpu"), ("limit_time_real", "conf.limitTimeReal"),
                                      ("max_cron_threads", "conf.maxCronThreads")) if conf.get(k)]
    if conf.get("server_wide_modules") and conf["server_wide_modules"].replace(" ", "") != "base,web":
        admin_keys.append(("server_wide_modules", "conf.serverWideModules"))
    if admin_keys:
        out.append("來源 conf %s：chart 預設不同，要一致就由平台管理員寫進服務的 helm_values（%s）後 Redeploy"
                   % ("、".join("%s=%s" % (k, conf[k]) for k, _v in admin_keys), "、".join(v for _k, v in admin_keys)))
    internal = sorted({"%s:%s（%s）" % (m["host"], m["port"], m["kind"]) for m in db.get("mail_hosts", [])
                       if m.get("active") and cluster_internal(m.get("host"))})
    if internal:
        out.append("啟用中的寄收信主機是叢集內位址：%s。租戶 ns 的 egress 只放行同 ns、DNS 與對外網路，搬過去之後連不到 ⇒ "
                   "叢集共用的 mailcow 用 target/shared-mail.sh open 接上（先取得使用者同意）；其他主機要先與該服務的負責人談"
                   % "、".join(internal))
    base_url = (db.get("params") or {}).get("web.base.url")
    if base_url:
        out.append("對外網址 %s：切換時要把這個 hostname 改指到新服務（SKILL.md 步驟 9「對外網址」）" % base_url)
    if db.get("pos_sessions_open") or db.get("pos_orders_draft"):
        out.append("POS：開啟中的 session %s、draft 訂單 %s（凍結時必須為 0）"
                   % (db.get("pos_sessions_open"), db.get("pos_orders_draft")))
    idle = db.get("pos_sessions_idle_opening") or []
    if idle:
        out.append("POS：%d 個 session 停在開帳步驟、沒開始過也沒有訂單（%s），不擋凍結；照原狀搬，店家在新站接著開帳"
                   % (len(idle), "、".join(str(s["name"]) for s in idle)))
    for wl in (k8s or {}).get("workloads", []):
        for item in wl.get("pip_install_commands", []):
            out.append("%s 的%s %s 執行時 pip：%s%s" % (
                wl["name"], " initContainer" if item["init"] else "主容器", item["container"], "；".join(item["commands"]),
                "（initContainer 裝的套件主容器看不到，以實際安裝清單為準）" if item["init"] else ""))
        if wl["role"] == "other":
            out.append("同 ns 其他工作負載 %s（%s）：確認去向（tunnel／MCP／forwarder）"
                       % (wl["name"], ", ".join(c["image"] for c in wl["containers"])))
    return out, blocks


def render(report):
    lines = ["# 相容性報告：%s → %s" % (report["source"], report["target_image"]), "",
             "結論：**%s**" % report["verdict"], ""]
    counts = report["counts"]
    lines += ["模組（DB 已安裝）：" + "、".join("%s %d" % kv for kv in sorted(counts.items())), ""]

    def table(title, rows, cols):
        if not rows:
            return
        lines.extend(["## " + title, "", "| " + " | ".join(c for c, _ in cols) + " |",
                      "|" + "---|" * len(cols)])
        for r in rows:
            lines.append("| " + " | ".join(str(r.get(k) if r.get(k) is not None else "") for _, k in cols) + " |")
        lines.append("")

    by = {}
    for r in report["modules"]:
        by.setdefault(r["status"], []).append(r)
    if report["blocks"]:
        lines += ["## 阻斷（環境）", ""] + ["- " + b for b in report["blocks"]] + [""]
    table("阻斷（模組）", by.get("BLOCK_DOWNGRADE", []) + by.get("BLOCK_MISSING", []),
          [("模組", "module"), ("狀態", "status"), ("DB 版本", "db_version"), ("可用版本", "target_version"),
           ("說明", "note")])
    table("要匯入 paas-odoo-ci 的 addon（NEED_IMPORT）", by.get("NEED_IMPORT", []),
          [("模組", "module"), ("DB 版本", "db_version"), ("來源目錄", "source_dir"), ("來源版本", "source_version")])
    table("paas-odoo-ci 已有、要加進 cloud-addons.list（NEED_LIST）", by.get("NEED_LIST", []),
          [("模組", "module"), ("DB 版本", "db_version"), ("ci 版本", "ci_version"), ("說明", "note")])
    table("程式碼不一致，要人決定（DIVERGED）", by.get("DIVERGED", []),
          [("模組", "module"), ("DB 版本", "db_version"), ("來源版本", "source_version"),
           ("目標／ci 版本", "target_version"), ("說明", "note")])
    table("目標 image 已有、服務要勾選（SELECT）", by.get("SELECT", []),
          [("模組", "module"), ("DB 版本", "db_version"), ("目標版本", "target_version")])
    table("已裁決的差異（--accept-diverged）", [r for r in report["modules"] if r.get("accepted_divergence")],
          [("模組", "module"), ("歸類", "status"), ("DB 版本", "db_version"), ("來源版本", "source_version"),
           ("說明", "note")])
    if report["pip_missing"]:
        lines += ["## 目標 image 缺的 python 依賴", ""]
        lines += ["- `%s`（%s；來源套件：%s）" % (m["requirement"], ", ".join(m["modules"]),
                                           ", ".join(m["source_dists"]) or "?") for m in report["pip_missing"]]
        lines.append("")
    if report["pip_source_only"]:
        lines += ["## 來源容器有、目標 image 沒有的 python 套件", "",
                  "- " + ", ".join("`%s`" % p for p in report["pip_source_only"]), "",
                  "（多半是來源執行時 pip install 的；確認仍有模組在 import，再加進 paas-odoo-ci 的 requirements）", ""]
    if report["notes"]:
        lines += ["## 注意事項", ""] + ["- " + n for n in report["notes"]] + [""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--ci-repo")
    parser.add_argument("--accept-diverged", default="",
                        help="已裁決改用目標／paas-odoo-ci 版本的模組（逗號分隔）")
    parser.add_argument("--json")
    args = parser.parse_args(argv)
    try:
        doc = json.loads(Path(args.inventory).read_text())
        tgt = json.loads(Path(args.target).read_text())
    except (OSError, ValueError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    inv = doc.get("inspect", doc)
    if inv.get("mode") != "source" or tgt.get("mode") != "image":
        print("ERROR: --inventory must be source/inventory.sh output, --target image-inventory.sh output",
              file=sys.stderr)
        return 2
    ci = load_ci(args.ci_repo)
    accepted = frozenset(m.strip() for m in args.accept_diverged.split(",") if m.strip())
    rows = classify(inv, tgt, ci, accepted=accepted)
    deps = python_deps(rows, inv, ci)
    pip_missing, source_only = pip_gaps(deps, inv, tgt)
    extra_notes, env_blocks = notes(inv, tgt, doc.get("k8s"))
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    if env_blocks or any(r["status"] in BLOCK for r in rows):
        verdict, code = "BLOCKED", 3
    elif any(r["status"] in ACTION for r in rows) or pip_missing:
        verdict, code = "ACTIONS", 1
    else:
        verdict, code = "READY", 0
    unused = sorted(accepted - {r["module"] for r in rows if r.get("accepted_divergence")})
    if unused:
        extra_notes.insert(0, "--accept-diverged 列了沒有差異（或不存在）的模組：%s" % ", ".join(unused))
    if ci is None and any(r["status"] == "NEED_IMPORT" for r in rows):
        extra_notes.insert(0, "沒給 --ci-repo：NEED_IMPORT 裡可能有 paas-odoo-ci 已經有的 addon")
    report = {
        "schema_version": 1, "kind": "standalone-odoo-compat",
        "source": "%s/%s" % (doc.get("source", {}).get("namespace", "?"), inv["db"]["name"]),
        "target_image": tgt.get("image") or tgt.get("image_digest") or "?",
        "verdict": verdict, "counts": counts, "modules": rows,
        "pip_missing": pip_missing, "pip_source_only": source_only,
        "notes": extra_notes, "blocks": env_blocks,
        "core_date": {"source": inv.get("core_date"), "target": tgt.get("core_date")},
    }
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    print(render(report))
    return code


if __name__ == "__main__":
    sys.exit(main())

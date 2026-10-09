#!/usr/bin/env python3
"""odoo_inspect.py — 在 Odoo 容器內做唯讀盤點（standalone-odoo-migrate skill，#1604）。

兩種模式；腳本由 stdin 送進 pod：``kubectl exec -i … -- python3 - <mode> [args]``。

  source [--db NAME] [--conf PATH]
      來源站：odoo.conf（只輸出白名單鍵）、addon 目錄（版本＋內容 hash）、pip、DB 統計、filestore。
      DB 走 READ ONLY 交易，不寫任何東西。
  image [--dirs a,b,…] [--digest sha256:…]
      目標 image：core 與 image 內 addon 目錄的模組版本、非 core 模組的內容 hash、pip。不連 DB。
  fingerprint --sql-b64 <base64 of fingerprint.sql> [--db NAME] [--conf PATH]
      來源站資料指紋：以 psql 跑 fingerprint.sql（READ ONLY），filestore 逐檔 sha256。DB 密碼只在 pod 內。
  export --sql-b64 … --url <http://receiver…/<set>> [--db NAME] [--conf PATH] [--image-digest sha256:…]
      搬遷匯出（在來源 helper pod 內）：同一個 REPEATABLE READ snapshot 內採 DB 指紋＋pg_dump --snapshot，
      filestore 逐檔 sha256＋tar 串流，全部 HTTP PUT 到 receiver（token 取 env UPLOAD_TOKEN），最後送 SHA256SUMS。

輸出：stdout 上 MARK_START／MARK_END 之間的一行 JSON（Odoo 或 shell 的雜訊可能混進 stdout）。
敏感值：密碼、token、SMTP 帳號、email、使用者 login 一律不輸出；DB 密碼只在 pod 內使用。

本機也可以 import：compat_check.py 用 dir_digest()／adapt_version()／version_key()
算 paas-odoo-ci checkout 內的 addon，hash 演算法因此只有這一份。
"""
import argparse
import ast
import base64
import http.client
import io
import hashlib
import json
import os
import subprocess
import sys
import tarfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

MARK_START = "---ODOO-INSPECT-JSON-START---"
MARK_END = "---ODOO-INSPECT-JSON-END---"
SERIES = "18.0"
CORE_ADDONS = "/usr/lib/python3/dist-packages/odoo/addons"
# image 模式預設掃描的 addon 目錄（woow-odoo chart：/all-addons＝cloud-odoo 候選、
# /opt/woow/addons＝platform image 內建；/mnt/extra-addons 為官方 image 慣例）
IMAGE_DEFAULT_DIRS = ("/all-addons", "/opt/woow/addons", "/mnt/extra-addons")
# 內容 hash 不計入的路徑片段與副檔名（編譯產物、版控中繼資料）
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache"}
SKIP_SUFFIXES = (".pyc", ".pyo")
SKIP_FILES = {".DS_Store"}
# odoo.conf 可以原值輸出的鍵；其餘鍵只輸出「有設定」
CONF_SAFE_KEYS = {
    "addons_path", "data_dir", "db_name", "db_host", "db_port", "dbfilter", "db_maxconn",
    "db_maxconn_gevent", "db_sslmode", "db_template", "list_db", "proxy_mode", "workers",
    "max_cron_threads", "limit_memory_hard", "limit_memory_soft", "limit_request",
    "limit_time_cpu", "limit_time_real", "limit_time_real_cron", "server_wide_modules",
    "without_demo", "log_level", "http_port", "gevent_port", "longpolling_port",
    "smtp_server", "smtp_port", "smtp_ssl", "unaccent", "x_sendfile",
}


# ---------------------------------------------------------------- 共用（本機也會 import）

def adapt_version(version, series=SERIES):
    """與 Odoo 18 ``odoo.modules.module.adapt_version`` 同式：沒有系列前綴就補上。"""
    version = str(version or "1.0")
    if version == series or not version.startswith(series + "."):
        version = "%s.%s" % (series, version)
    return version


def version_key(version):
    """模組版本比較鍵：逐段轉 int；非數字段以 -1 代替並保留原字串（讓比較穩定、不 raise）。"""
    parts = []
    for part in str(version).split("."):
        parts.append((int(part), "") if part.isdigit() else (-1, part))
    return tuple(parts)


def dir_digest(path):
    """目錄內容 hash：依相對路徑排序，逐檔 sha256，再對「路徑\\0檔案 hash\\n」整體取 sha256。

    跳過 .git／__pycache__／*.pyc 等，symlink 以「symlink:<目標>」計。兩端（來源 pod、
    本機 paas-odoo-ci checkout、目標 image）用同一支函式，hash 相等＝內容一致。
    """
    root = Path(path)
    entries = []
    total_bytes = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if name in SKIP_FILES or name.endswith(SKIP_SUFFIXES):
                continue
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            if full.is_symlink():
                digest = hashlib.sha256(("symlink:" + os.readlink(full)).encode()).hexdigest()
            else:
                h = hashlib.sha256()
                with open(full, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                digest = h.hexdigest()
                total_bytes += full.stat().st_size
            entries.append("%s\0%s\n" % (rel, digest))
    overall = hashlib.sha256("".join(sorted(entries)).encode()).hexdigest()
    return {"sha256": overall, "files": len(entries), "bytes": total_bytes}


def read_manifest(module_dir):
    path = Path(module_dir) / "__manifest__.py"
    try:
        data = ast.literal_eval(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — 盤點要列出壞掉的 manifest，不中斷
        return None, "%s: %s" % (type(exc).__name__, exc)
    if not isinstance(data, dict):
        return None, "manifest is not a dict"
    return data, None


def scan_addons(dirs, digest_filter=None):
    """依 addons_path 順序掃描模組；同名模組先出現者生效（Odoo 規則），其餘列為 shadowed。

    digest_filter(dir) 為 True 的目錄才算內容 hash（core 不算，太大也沒必要）。
    """
    effective, shadowed, errors = {}, [], []
    for index, base in enumerate(dirs):
        base_path = Path(base)
        if not base_path.is_dir():
            continue
        for manifest in sorted(base_path.glob("*/__manifest__.py")):
            module_dir = manifest.parent
            name = module_dir.name
            data, err = read_manifest(module_dir)
            if err:
                errors.append({"module": name, "dir": str(base_path), "error": err})
                continue
            entry = {
                "dir": str(base_path),
                "path_index": index,
                "version": adapt_version(data.get("version")),
                "depends": list(data.get("depends") or []),
                "external_dependencies": data.get("external_dependencies") or {},
                "author": data.get("author"),
                "license": data.get("license"),
                "installable": data.get("installable", True),
                "auto_install": bool(data.get("auto_install", False)),
            }
            if digest_filter and digest_filter(str(base_path)):
                entry["digest"] = dir_digest(module_dir)
                entry["git"] = git_info(module_dir, base_path)
            if name in effective:
                shadowed.append({"module": name, "dir": str(base_path),
                                 "shadowed_by": effective[name]["dir"]})
            else:
                effective[name] = entry
    return {"modules": effective, "shadowed": shadowed, "errors": errors}


def git_info(module_dir, stop_at):
    """找模組所在的 git 工作目錄（往上到 addon 根目錄為止），回傳 HEAD；remote 去掉帳密。"""
    current = Path(module_dir)
    stop = Path(stop_at).parent
    while True:
        git_dir = current / ".git"
        if git_dir.exists():
            return {"root": str(current), "head": _git_head(git_dir), "remote": _git_remote(git_dir)}
        if current == stop or current.parent == current:
            return None
        current = current.parent


def _git_head(git_dir):
    try:
        if git_dir.is_file():  # worktree／submodule：gitdir: <path>
            target = git_dir.read_text().split(":", 1)[1].strip()
            git_dir = (git_dir.parent / target).resolve()
        head = (git_dir / "HEAD").read_text().strip()
        if not head.startswith("ref:"):
            return head
        ref = head.split(" ", 1)[1]
        ref_file = git_dir / ref
        if ref_file.exists():
            return ref_file.read_text().strip()
        packed = git_dir / "packed-refs"
        if packed.exists():
            for line in packed.read_text().splitlines():
                if line.endswith(" " + ref):
                    return line.split(" ", 1)[0]
    except Exception:  # noqa: BLE001
        return None
    return None


def _git_remote(git_dir):
    try:
        if git_dir.is_file():
            return None
        url = None
        for line in (git_dir / "config").read_text().splitlines():
            line = line.strip()
            if line.startswith("url ="):
                url = line.split("=", 1)[1].strip()
                break
        if url and "@" in url and "://" in url:  # 去掉 https://user:token@host 的帳密
            scheme, rest = url.split("://", 1)
            url = scheme + "://" + rest.split("@", 1)[1]
        return url
    except Exception:  # noqa: BLE001
        return None


def pip_inventory():
    from importlib import metadata
    dists = {}
    for dist in metadata.distributions():
        name = dist.metadata.get("Name")
        if name:
            dists[name] = dist.version
    import_map = {}
    try:
        for top, names in metadata.packages_distributions().items():
            import_map[top] = sorted(set(names))
    except Exception:  # noqa: BLE001 — Python < 3.10
        pass
    return {"python": sys.version.split()[0], "distributions": dict(sorted(dists.items())),
            "import_map": dict(sorted(import_map.items()))}


def core_date():
    """官方 odoo:18.0 lineage 的 core 日期（與 woow-odoo chart lib.sh core_date 同式）。"""
    try:
        raw = subprocess.run(["dpkg-query", "-W", "-f=${Version}", "odoo"], check=True,
                             capture_output=True, text=True).stdout.strip()
    except Exception:  # noqa: BLE001
        return None
    v = raw.split(":", 1)[-1]
    for sep in "-+~":
        v = v.split(sep, 1)[0]
    return ".".join(v.split(".")[:3])


def emit(payload):
    print(MARK_START)
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    print(MARK_END)


# ---------------------------------------------------------------- source 模式

def find_conf(explicit):
    if explicit:
        return explicit
    try:  # 主程序（PID 1）的 --config／-c
        args = Path("/proc/1/cmdline").read_bytes().split(b"\0")
        args = [a.decode(errors="replace") for a in args if a]
        for i, arg in enumerate(args):
            if arg.startswith("--config="):
                return arg.split("=", 1)[1]
            if arg in ("-c", "--config") and i + 1 < len(args):
                return args[i + 1]
    except Exception:  # noqa: BLE001
        pass
    return os.environ.get("ODOO_RC") or "/etc/odoo/odoo.conf"


def read_conf(path):
    import configparser
    parser = configparser.RawConfigParser()
    parser.read(path)
    return dict(parser.items("options")) if parser.has_section("options") else {}


def redact_conf(conf):
    out = {}
    for key, value in sorted(conf.items()):
        out[key] = value if key in CONF_SAFE_KEYS else ("<set>" if value not in ("", "False") else value)
    return out


def _conf_value(conf, key):
    value = conf.get(key)
    return None if value in (None, "", "False") else value


def db_params(conf):
    """連線參數：官方 image 慣例的 env（HOST／PORT／USER／PASSWORD）優先，odoo.conf 次之。

    官方 entrypoint 以命令列參數蓋過 conf；harumi 類站則在啟動時用 env 改寫一份 /tmp/odoo.conf，
    ConfigMap 裡的 db_password 只是佔位。helper pod 讀的是 ConfigMap 原檔 ⇒ env 必須優先。
    """
    return {
        "host": os.environ.get("HOST") or _conf_value(conf, "db_host") or "db",
        "port": int(os.environ.get("PORT") or _conf_value(conf, "db_port") or 5432),
        "user": os.environ.get("USER") or _conf_value(conf, "db_user") or "odoo",
        "password": os.environ.get("PASSWORD") or _conf_value(conf, "db_password") or "",
    }


def _rows(cur, sql, args=None):
    cur.execute(sql, args)
    return cur.fetchall()


def _table_exists(cur, name):
    cur.execute("SELECT to_regclass(%s)", (name,))
    return cur.fetchone()[0] is not None


def pick_db(params, explicit, conf):
    if explicit:
        return explicit
    name = _conf_value(conf, "db_name")
    if name and "," not in name:
        return name
    import psycopg2
    conn = psycopg2.connect(dbname="postgres", **params)
    try:
        with conn.cursor() as cur:
            names = [r[0] for r in _rows(cur, "SELECT datname FROM pg_database WHERE NOT datistemplate "
                                              "AND datname <> 'postgres' ORDER BY 1")]
    finally:
        conn.close()
    if len(names) != 1:
        raise SystemExit("cannot pick DB automatically (candidates: %s); pass --db" % ", ".join(names))
    return names[0]


def db_stats(params, dbname):
    import psycopg2
    conn = psycopg2.connect(dbname=dbname, **params)
    conn.set_session(readonly=True)
    out = {"name": dbname}
    try:
        with conn.cursor() as cur:
            out["server_version"] = _rows(cur, "SHOW server_version")[0][0]
            row = _rows(cur, "SELECT pg_database_size(current_database()), pg_encoding_to_char(encoding), "
                             "datcollate, datctype FROM pg_database WHERE datname = current_database()")[0]
            out.update(size_bytes=row[0], encoding=row[1], datcollate=row[2], datctype=row[3])
            try:
                out["datlocprovider"] = _rows(cur, "SELECT datlocprovider FROM pg_database "
                                                   "WHERE datname = current_database()")[0][0]
            except psycopg2.Error:  # PG < 15
                conn.rollback()
                out["datlocprovider"] = None
            out["extensions"] = dict(_rows(cur, "SELECT extname, extversion FROM pg_extension ORDER BY 1"))
            out["vector_columns"] = ["%s.%s" % r for r in _rows(
                cur, "SELECT table_name, column_name FROM information_schema.columns "
                     "WHERE udt_name = 'vector' AND table_schema = 'public' ORDER BY 1, 2")]
            out["extra_schemas"] = [r[0] for r in _rows(
                cur, "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg\\_%%' "
                     "AND nspname NOT IN ('information_schema', 'public') ORDER BY 1")]
            out["module_states"] = dict(_rows(cur, "SELECT state, count(*) FROM ir_module_module GROUP BY 1"))
            out["modules"] = {
                name: {"state": state, "version": adapt_version(ver) if ver else None, "author": author}
                for name, state, ver, author in _rows(
                    cur, "SELECT name, state, latest_version, author FROM ir_module_module "
                         "WHERE state NOT IN ('uninstalled', 'uninstallable') ORDER BY name")
            }
            row = _rows(cur, "SELECT count(*) FILTER (WHERE share IS NOT TRUE AND active), "
                             "count(*) FILTER (WHERE share AND active) FROM res_users")[0]
            out["users"] = {"internal_active": row[0], "portal_active": row[1]}
            rows = _rows(cur, "SELECT login = 'admin' FROM res_users WHERE id = 2")
            out["admin_login_is_default"] = rows[0][0] if rows else None
            row = _rows(cur, "SELECT count(*), coalesce(sum(file_size), 0), count(DISTINCT store_fname) "
                             "FROM ir_attachment WHERE store_fname IS NOT NULL")[0]
            out["attachments"] = {"rows": row[0], "bytes": int(row[1]), "distinct_files": row[2]}
            out["params"] = dict(_rows(cur, "SELECT key, value FROM ir_config_parameter WHERE key IN "
                                            "('web.base.url', 'web.base.url.freeze', 'report.url') ORDER BY 1"))
            out["params_present"] = [r[0] for r in _rows(
                cur, "SELECT key FROM ir_config_parameter WHERE key IN ('database.uuid', 'database.secret', "
                     "'database.create_date', 'woow_paas_platform.upgrade_done', 'database.is_neutralized') "
                     "ORDER BY 1")]
            row = _rows(cur, "SELECT count(*) FILTER (WHERE active), count(*) FROM ir_mail_server")[0]
            out["mail_servers"] = {"active": row[0], "total": row[1]}
            out["crons_active"] = _rows(cur, "SELECT count(*) FROM ir_cron WHERE active")[0][0]
            if _table_exists(cur, "payment_provider"):
                out["payment_providers_enabled"] = [
                    "%s:%s" % r for r in _rows(cur, "SELECT code, state FROM payment_provider "
                                                    "WHERE state <> 'disabled' ORDER BY 1")]
            if _table_exists(cur, "pos_session"):
                out["pos_sessions_open"] = _rows(cur, "SELECT count(*) FROM pos_session "
                                                      "WHERE state <> 'closed'")[0][0]
            if _table_exists(cur, "pos_order"):
                out["pos_orders_draft"] = _rows(cur, "SELECT count(*) FROM pos_order "
                                                     "WHERE state = 'draft'")[0][0]
            out["tables"] = _rows(cur, "SELECT count(*) FROM pg_class c JOIN pg_namespace n "
                                       "ON n.oid = c.relnamespace WHERE c.relkind = 'r' "
                                       "AND n.nspname = 'public'")[0][0]
            out["largest_tables"] = [
                {"table": r[0], "bytes": r[1]} for r in _rows(
                    cur, "SELECT c.relname, pg_total_relation_size(c.oid) FROM pg_class c "
                         "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind = 'r' "
                         "AND n.nspname = 'public' ORDER BY 2 DESC LIMIT 15")]
            store_fnames = {r[0] for r in _rows(cur, "SELECT DISTINCT store_fname FROM ir_attachment "
                                                     "WHERE store_fname IS NOT NULL")}
    finally:
        conn.close()
    return out, store_fnames


def filestore_stats(path, store_fnames):
    root = Path(path)
    if not root.is_dir():
        return {"path": str(root), "exists": False}
    files, total = set(), 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = Path(dirpath) / name
            files.add(full.relative_to(root).as_posix())
            total += full.stat().st_size
    missing = sorted(store_fnames - files)
    return {"path": str(root), "exists": True, "files": len(files), "bytes": total,
            "missing_referenced": len(missing), "missing_sample": missing[:20],
            "unreferenced": len(files - store_fnames)}


def run_source(args):
    conf_path = find_conf(args.conf)
    conf = read_conf(conf_path)
    data_dir = _conf_value(conf, "data_dir") or "/var/lib/odoo"
    addons_path = [p.strip() for p in (conf.get("addons_path") or "").split(",") if p.strip()]
    data_addons = str(Path(data_dir) / "addons" / SERIES)
    # Odoo 的實際掃描順序：core（base addons）＋conf addons_path＋data_dir/addons/<series>
    dirs = [CORE_ADDONS] + [p for p in addons_path if p != CORE_ADDONS] + [data_addons]
    addons = scan_addons(dirs, digest_filter=lambda d: not d.startswith(CORE_ADDONS))
    params = db_params(conf)
    dbname = pick_db(params, args.db, conf)
    db, store_fnames = db_stats(params, dbname)
    emit({
        "mode": "source",
        "conf_path": conf_path,
        "conf": redact_conf(conf),
        "data_dir": data_dir,
        "addons_dirs": dirs,
        "core_date": core_date(),
        "addons": addons,
        "pip": pip_inventory(),
        "db": db,
        "filestore": filestore_stats(Path(data_dir) / "filestore" / dbname, store_fnames),
    })


# ---------------------------------------------------------------- fingerprint 模式

def filestore_listing(root):
    """`sha256  相對路徑` 逐行（與 `sha256sum` 輸出同格式，依路徑排序）。"""
    root = Path(root)
    lines = []
    if root.is_dir():
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in filenames:
                full = Path(dirpath) / name
                h = hashlib.sha256()
                with open(full, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                lines.append((full.relative_to(root).as_posix(), h.hexdigest()))
    return "".join("%s  %s\n" % (digest, rel) for rel, digest in sorted(lines))


def run_fingerprint(args):
    conf = read_conf(find_conf(args.conf))
    params = db_params(conf)
    dbname = pick_db(params, args.db, conf)
    sql = base64.b64decode(args.sql_b64).decode("utf-8")
    env = dict(os.environ, PGPASSWORD=params["password"], LC_ALL="C.UTF-8")
    res = subprocess.run(["psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-h", params["host"],
                          "-p", str(params["port"]), "-U", params["user"], "-d", dbname, "-f", "-"],
                         input=sql, capture_output=True, text=True, env=env)
    if res.returncode != 0:
        raise SystemExit("psql failed: %s" % res.stderr.strip())
    data_dir = _conf_value(conf, "data_dir") or "/var/lib/odoo"
    emit({"mode": "fingerprint", "db_name": dbname, "db": res.stdout,
          "fs": filestore_listing(Path(data_dir) / "filestore" / dbname)})


# ---------------------------------------------------------------- export 模式

def fingerprint_lines(cur, sql):
    """在既有交易內執行 fingerprint.sql，輸出與 `psql -X -q -At -f fingerprint.sql` 相同的行。

    只認得 fingerprint.sql 用到的語法：SET、單行結尾分號的 SELECT、`\\gexec`；BEGIN／COMMIT 交給呼叫端（export
    要讓指紋與 pg_dump 共用同一個 snapshot）。SQL 只有一份（fingerprint.sql），目標端以 psql 跑同一個檔。
    """
    out, buf = [], []
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        if stripped == "\\gexec":
            cur.execute("\n".join(buf))
            for (query,) in cur.fetchall():
                cur.execute(query)
                out.extend(str(r[0]) for r in cur.fetchall())
            buf = []
            continue
        buf.append(line)
        if stripped.endswith(";"):
            stmt = "\n".join(buf)
            buf = []
            head = stmt.strip().rstrip(";").split(None, 1)[0].upper()
            if head in ("BEGIN", "COMMIT", "ROLLBACK"):
                continue
            cur.execute(stmt)
            if head == "SELECT":
                out.extend(str(r[0]) for r in cur.fetchall())
    return "".join(line + "\n" for line in out)


class ChunkedPut:
    """HTTP PUT（Transfer-Encoding: chunked），邊送邊算 sha256；close() 回傳 (bytes, sha256, 回應)。"""

    def __init__(self, url, name, token):
        parsed = urllib.parse.urlsplit(url.rstrip("/") + "/" + name)
        self.conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=600)
        self.conn.putrequest("PUT", parsed.path)
        self.conn.putheader("Transfer-Encoding", "chunked")
        self.conn.putheader("X-Upload-Token", token)
        self.conn.endheaders()
        self.digest, self.total = hashlib.sha256(), 0

    def write(self, data):
        if data:
            self.conn.send(b"%x\r\n" % len(data) + bytes(data) + b"\r\n")
            self.digest.update(data)
            self.total += len(data)
        return len(data)

    def close(self):
        self.conn.send(b"0\r\n\r\n")
        resp = self.conn.getresponse()
        body = resp.read().decode().strip()
        if resp.status != 201:
            raise SystemExit("upload failed: HTTP %s %s" % (resp.status, body))
        local = "%d %s" % (self.total, self.digest.hexdigest())
        if body != local:
            raise SystemExit("receiver checksum mismatch: local %s vs receiver %s" % (local, body))
        return self.total, self.digest.hexdigest()


def put_bytes(url, name, token, data):
    up = ChunkedPut(url, name, token)
    up.write(data)
    return up.close()


def run_export(args):
    import psycopg2
    token = os.environ.get("UPLOAD_TOKEN") or ""
    if not token:
        raise SystemExit("UPLOAD_TOKEN env is required")
    conf = read_conf(find_conf(args.conf))
    params = db_params(conf)
    dbname = pick_db(params, args.db, conf)
    data_dir = _conf_value(conf, "data_dir") or "/var/lib/odoo"
    fs_root = Path(data_dir) / "filestore" / dbname
    sums = {}

    conn = psycopg2.connect(dbname=dbname, **params)
    conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
    with conn.cursor() as cur:
        cur.execute("SELECT pg_export_snapshot()")
        snapshot = cur.fetchone()[0]
        fp = fingerprint_lines(cur, base64.b64decode(args.sql_b64).decode("utf-8"))
        cur.execute("SELECT pg_encoding_to_char(encoding), datcollate, datctype FROM pg_database "
                    "WHERE datname = current_database()")
        encoding, collate, ctype = cur.fetchone()
        try:
            cur.execute("SELECT datlocprovider FROM pg_database WHERE datname = current_database()")
            provider = cur.fetchone()[0]
        except psycopg2.Error:
            raise SystemExit("export needs PostgreSQL >= 15 (datlocprovider)")
        cur.execute("SELECT extname, extversion FROM pg_extension ORDER BY 1")
        extensions = dict(cur.fetchall())
        cur.execute("SELECT current_setting('server_version'), pg_database_size(current_database())")
        server_version, db_size = cur.fetchone()
        # pg_dump 在 snapshot 仍有效時（本交易未結束）以 --snapshot 讀同一份資料
        env = dict(os.environ, PGPASSWORD=params["password"], LC_ALL="C.UTF-8")
        dump = subprocess.Popen(["pg_dump", "-Fc", "--snapshot", snapshot, "-h", params["host"],
                                 "-p", str(params["port"]), "-U", params["user"], "-d", dbname],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        up = ChunkedPut(args.url, "db.dump", token)
        for chunk in iter(lambda: dump.stdout.read(1 << 20), b""):
            up.write(chunk)
        err = dump.stderr.read().decode()
        if dump.wait() != 0:
            raise SystemExit("pg_dump failed: %s" % err.strip())
        sums["db.dump"] = up.close()
    conn.rollback()
    conn.close()
    pg_dump_version = subprocess.run(["pg_dump", "--version"], capture_output=True, text=True,
                                     env=dict(os.environ, LC_ALL="C.UTF-8")).stdout.strip()

    fs_listing = filestore_listing(fs_root)
    up = ChunkedPut(args.url, "filestore.tar", token)
    with tarfile.open(fileobj=up, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        for line in fs_listing.splitlines():
            rel = line.split("  ", 1)[1]
            tar.add(str(fs_root / rel), arcname=rel, recursive=False)
    sums["filestore.tar"] = up.close()

    manifest = {
        "schema_version": 1, "kind": "standalone-odoo-export",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "db_name": dbname, "encoding": encoding, "datcollate": collate, "datctype": ctype,
        "datlocprovider": provider, "extensions": extensions, "pg_version": server_version,
        "db_size_bytes": db_size, "pg_dump_version": pg_dump_version,
        "filestore_files": len(fs_listing.splitlines()),
        "source_image_digest": args.image_digest, "source_core_date": core_date(),
    }
    sums["source.fp"] = put_bytes(args.url, "source.fp", token, fp.encode())
    sums["source.fs"] = put_bytes(args.url, "source.fs", token, fs_listing.encode())
    sums["manifest.json"] = put_bytes(args.url, "manifest.json", token,
                                      json.dumps(manifest, indent=2, sort_keys=True).encode())
    sha_text = "".join("%s  %s\n" % (sha, name) for name, (_n, sha) in sorted(sums.items()))
    put_bytes(args.url, "SHA256SUMS", token, sha_text.encode())
    emit({"mode": "export", "manifest": manifest,
          "uploaded": {name: {"bytes": n, "sha256": sha} for name, (n, sha) in sums.items()},
          "tables": sum(1 for line in fp.splitlines() if line.startswith("table|"))})


# ---------------------------------------------------------------- image 模式

def run_image(args):
    extra = [d.strip() for d in args.dirs.split(",") if d.strip()] if args.dirs else list(IMAGE_DEFAULT_DIRS)
    dirs = [CORE_ADDONS] + extra
    emit({
        "mode": "image",
        "image_digest": args.digest,
        "addons_dirs": dirs,
        "core_date": core_date(),
        "addons": scan_addons(dirs, digest_filter=lambda d: not d.startswith(CORE_ADDONS)),
        "pip": pip_inventory(),
    })


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    src = sub.add_parser("source")
    src.add_argument("--db")
    src.add_argument("--conf")
    img = sub.add_parser("image")
    img.add_argument("--dirs")
    img.add_argument("--digest")
    fp = sub.add_parser("fingerprint")
    fp.add_argument("--sql-b64", required=True)
    fp.add_argument("--db")
    fp.add_argument("--conf")
    ex = sub.add_parser("export")
    ex.add_argument("--sql-b64", required=True)
    ex.add_argument("--url", required=True)
    ex.add_argument("--db")
    ex.add_argument("--conf")
    ex.add_argument("--image-digest")
    args = parser.parse_args(argv)
    {"source": run_source, "image": run_image, "fingerprint": run_fingerprint, "export": run_export}[args.mode](args)


if __name__ == "__main__":
    main()

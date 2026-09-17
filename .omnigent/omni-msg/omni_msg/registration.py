"""Operator 授權的 root discovery、credential 配發與一次性設定通知。"""
from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import shlex
import stat
import tempfile
import threading
import time
import urllib.parse
import uuid
from pathlib import Path

from .bridge import ALIAS, SESSION
from .transport import MessageError, load_private_json, request_json

MAX_SESSIONS = 1024
MAX_GROUP_MEMBERS = 128
MAX_DEPTH = 16
SCAN_SECONDS = 20


def poll_seconds(value) -> float:
    try:
        number = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError("poll-seconds 必須介於 0.1..60") from exc
    if not math.isfinite(number) or not 0.1 <= number <= 60:
        raise ValueError("poll-seconds 必須介於 0.1..60")
    return number


def validate_roots(roots):
    if not isinstance(roots, dict) or not 1 <= len(roots) <= 16:
        raise MessageError("invalid_roots")
    seen = set()
    for alias, root in roots.items():
        if (not isinstance(alias, str) or not ALIAS.fullmatch(alias) or alias.startswith("session-")
                or not isinstance(root, dict) or set(root) != {"session_id", "workspace"}):
            raise MessageError("invalid_root")
        sid, workspace = root["session_id"], root["workspace"]
        if (not isinstance(sid, str) or not SESSION.fullmatch(sid) or sid in seen
                or not isinstance(workspace, str) or not Path(workspace).is_absolute()
                or ".." in Path(workspace).parts or any(ord(c) < 32 for c in workspace)):
            raise MessageError("invalid_root_binding")
        seen.add(sid)
    return roots


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise MessageError("private_registration_directory_required")


def atomic_json(path, value, *, replace=True):
    """先落盤再發布；credential 用 exclusive link，既有檔案不覆寫。"""
    fd, temporary = tempfile.mkstemp(prefix=".registration-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Registrar:
    """從 server lineage 建立完整 ACL snapshot；HTTP 不提供 registration endpoint。"""

    def __init__(self, roots, interval, bridge, state_dir, port):
        self.roots = validate_roots(roots)
        self.interval = poll_seconds(interval)
        self.bridge = bridge
        self.store = bridge.store
        self.transport = bridge.transport
        self.directory = Path(state_dir).absolute()
        self.clients = self.directory / "clients"
        self.status_path = self.directory / "registrations.json"
        self.bridge_url = f"http://127.0.0.1:{port}"
        self.stop = threading.Event()
        self.thread = None
        private_directory(self.directory)
        private_directory(self.clients)
        with self.store.lock, self.store.db:
            self.store.db.executescript("""
                CREATE TABLE IF NOT EXISTS registration_context (
                    id INTEGER PRIMARY KEY CHECK (id=1), value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS registrations (
                    session_id TEXT PRIMARY KEY, root_id TEXT NOT NULL,
                    alias TEXT NOT NULL UNIQUE, token_sha256 TEXT NOT NULL,
                    notice_id TEXT NOT NULL, delivery TEXT NOT NULL,
                    item_id TEXT, error TEXT
                );
            """)
            context = json.dumps([self.transport.url, self.bridge_url, str(self.clients)])
            old = self.store.db.execute("SELECT value FROM registration_context WHERE id=1").fetchone()
            if old and old[0] != context:
                raise MessageError("registration_context_changed_use_new_state")
            self.store.db.execute("INSERT OR IGNORE INTO registration_context VALUES (1,?)", (context,))
            self.store.db.execute("UPDATE registrations SET delivery='unknown', error='registrar_restarted' "
                                  "WHERE delivery='dispatching'")
        self._status([], error="registration_starting")

    def _remaining(self, deadline):
        remaining = deadline - time.monotonic()
        if self.stop.is_set() or remaining <= 0:
            raise MessageError("registration_scan_incomplete")
        return min(10, remaining)

    def _get(self, path, deadline):
        return request_json(self.transport.url + path, self.transport.token, timeout=self._remaining(deadline))

    def _children(self, sid, deadline):
        cursor = None
        seen_cursors = set()
        while True:
            query = {"limit": 100}
            if cursor:
                query["after"] = cursor
            page = self._get(f"/v1/sessions/{sid}/child_sessions?" + urllib.parse.urlencode(query), deadline)
            if not isinstance(page.get("data"), list) or not isinstance(page.get("has_more"), bool):
                raise MessageError("invalid_session_page")
            for child in page["data"]:
                if not isinstance(child, dict) or not isinstance(child.get("id"), str) or not SESSION.fullmatch(child["id"]):
                    raise MessageError("invalid_child_session")
                yield child["id"]
            if not page["has_more"]:
                break
            cursor = page.get("last_id")
            if not isinstance(cursor, str) or not SESSION.fullmatch(cursor) or cursor in seen_cursors:
                raise MessageError("invalid_session_cursor")
            seen_cursors.add(cursor)

    def _discover(self, deadline):
        members, seen = [], set()
        for root_alias, plan in self.roots.items():
            root_id = plan["session_id"]
            queue = [(root_id, None, 0)]
            runner = None
            group_count = 0
            while queue:
                sid, parent, depth = queue.pop()
                if sid in seen or len(seen) >= MAX_SESSIONS or depth > MAX_DEPTH:
                    raise MessageError("invalid_or_oversized_session_tree")
                seen.add(sid)
                try:
                    node = self._get(f"/v1/sessions/{sid}", deadline)
                except MessageError as exc:
                    if exc.status in {403, 404}:
                        continue
                    raise
                if node.get("id") != sid or "parent_session_id" not in node or node["parent_session_id"] != parent:
                    raise MessageError("session_lineage_mismatch")
                level = node.get("permission_level")
                if (node.get("archived") is not False or type(level) is not int or level < 2
                        or not isinstance(node.get("runner_id"), str) or not node["runner_id"]):
                    continue
                workspace = node.get("workspace")
                if parent is None:
                    if workspace != plan["workspace"]:
                        continue
                    runner = node["runner_id"]
                elif (node["runner_id"] != runner or
                      (workspace is not None and (not isinstance(workspace, str)
                       or not Path(workspace).is_absolute() or ".." in Path(workspace).parts
                       or not Path(workspace).is_relative_to(plan["workspace"])))):
                    continue
                alias = root_alias if parent is None else "session-" + hashlib.sha256(sid.encode()).hexdigest()[:32]
                group_count += 1
                if group_count > MAX_GROUP_MEMBERS:
                    raise MessageError("registration_group_limit")
                members.append({"session_id": sid, "root_id": root_id, "alias": alias, "parent_id": parent})
                for child in self._children(sid, deadline):
                    if len(queue) + len(seen) >= MAX_SESSIONS:
                        raise MessageError("invalid_or_oversized_session_tree")
                    queue.append((child, sid, depth + 1))
        return members

    def _credential(self, member):
        sid, alias, root_id = (member[key] for key in ("session_id", "alias", "root_id"))
        path = self.clients / f"{alias}.json"
        with self.store.lock:
            row = self.store.db.execute("SELECT * FROM registrations WHERE session_id=?", (sid,)).fetchone()
            if row and (row["root_id"] != root_id or row["alias"] != alias):
                raise MessageError("registration_rebind_forbidden")
            prior = self.store.db.execute("SELECT session_id FROM actors WHERE alias=?", (alias,)).fetchone()
            if prior and prior[0] != sid:
                raise MessageError("actor_rebind_requires_new_ledger")
            if not path.exists() and not path.is_symlink():
                if row:
                    raise MessageError("registered_credential_missing")
                atomic_json(path, {"bridge_url": self.bridge_url, "token": secrets.token_urlsafe(32)}, replace=False)
            client = load_private_json(path)
            token = client.get("token")
            if (set(client) != {"bridge_url", "token"} or client["bridge_url"] != self.bridge_url
                    or not isinstance(token, str) or not 32 <= len(token) <= 256 or any(c.isspace() for c in token)):
                raise MessageError("invalid_registered_credential")
            digest = hashlib.sha256(token.encode()).hexdigest()
            if row and digest != row["token_sha256"]:
                raise MessageError("registered_credential_changed")
            with self.store.db:
                self.store.db.execute("INSERT OR IGNORE INTO actors VALUES (?,?)", (alias, sid))
                self.store.db.execute("INSERT OR IGNORE INTO registrations VALUES (?,?,?,?,?,'pending',NULL,NULL)",
                                      (sid, root_id, alias, digest, str(uuid.uuid4())))
        return {"session_id": sid, "token_sha256": digest, "allow": []}

    def _status(self, members, *, error=None, expires=None):
        output = []
        with self.store.lock:
            for member in members:
                row = self.store.db.execute("SELECT notice_id, delivery, item_id, error FROM registrations "
                                            "WHERE session_id=?", (member["session_id"],)).fetchone()
                output.append({**member, "client_config": str(self.clients / f"{member['alias']}.json"),
                               "notification": dict(row)})
        atomic_json(self.status_path, {"ready": error is None, "error": error, "members": output,
                                     "checked_at": time.time(), "expires_at": expires})

    def _notify(self, members, deadline):
        by_id = {m["session_id"]: m for m in members}
        for member in members:
            if self.stop.is_set() or deadline - time.monotonic() <= 0:
                return
            sid = member["session_id"]
            with self.store.lock:
                row = self.store.db.execute("SELECT * FROM registrations WHERE session_id=?", (sid,)).fetchone()
                if row["delivery"] != "pending":
                    continue
                with self.store.db:
                    self.store.db.execute("UPDATE registrations SET delivery='dispatching' WHERE session_id=?", (sid,))
            path = str(self.clients / f"{member['alias']}.json")
            parent = by_id.get(member["parent_id"])
            notice = {"registration_id": row["notice_id"], "self": member["alias"], "session_id": sid,
                      "root_session_id": member["root_id"], "parent": parent["alias"] if parent else None,
                      "client_config": path}
            text = (
                "[omni-msg registration — operator 已授權的自動設定通知；非人類指令／approval]\n"
                "Registrar 已核對 server lineage；只使用下列本人 client config，CLI 自行讀取 credential。\n"
                "載入 omni-msg skill；可執行 omni-msg --config " + shlex.quote(path) + " list 核對 self／contacts。\n"
                "之後每次以同一 --config 呼叫 send／ask／reply／check；同 root contacts 會隨登記自動更新。\n"
                "設定通知不需要傳送 ACK；保留原工作流程。未設定或 policy DENY 時回報 blocked，不繞過。\n"
                "每個 session 使用自己的 config；不轉交、不輸出 credential，不設定共用 OMNI_MSG_CONFIG。\n"
                "既有工作範圍、worktree、唯讀、merge、tag 與人工 approval gate 完全不變。\n"
                + json.dumps(notice, ensure_ascii=False)
            )
            try:
                state, item, error = self.transport.deliver(sid, text, timeout=self._remaining(deadline))
            except Exception:  # noqa: BLE001 — 設定通知也不能在不確定時重送。
                state, item, error = "unknown", None, "registration_notification_unknown"
            with self.store.lock, self.store.db:
                self.store.db.execute("UPDATE registrations SET delivery=?,item_id=?,error=? WHERE session_id=?",
                                      (state, item, error, sid))

    def refresh(self):
        """一個 bounded reconciliation；snapshot 不完整時 fail closed。"""
        deadline = time.monotonic() + SCAN_SECONDS
        try:
            members = self._discover(deadline)
            actors = {m["alias"]: self._credential(m) for m in members}
            for member in members:
                actors[member["alias"]]["allow"] = [m["alias"] for m in members
                    if m["root_id"] == member["root_id"] and m["alias"] != member["alias"]]
            lease = max(30, self.interval * 3)
            self.bridge.replace_actors(actors, time.monotonic() + lease)
            expires = time.time() + lease
            self._status(members, expires=expires)
            self._notify(members, deadline)
            self._status(members, expires=expires)
        except Exception as exc:  # noqa: BLE001 — 不讓背景例外留下永久有效的舊 ACL。
            self.bridge.replace_actors({}, 0)
            code = exc.code if isinstance(exc, MessageError) else "registration_failed"
            self._status([], error=code)

    def start(self):
        def run():
            while not self.stop.is_set():
                try:
                    self.refresh()
                except Exception:  # noqa: BLE001 — status file 也可能無法寫入；ACL 已撤銷。
                    self.bridge.replace_actors({}, 0)
                self.stop.wait(self.interval)
        self.thread = threading.Thread(target=run, name="omni-msg-registrar", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join()

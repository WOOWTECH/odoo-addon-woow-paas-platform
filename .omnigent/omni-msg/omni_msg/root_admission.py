"""受限的 root admission；bundle 名稱不是授權，內容指紋才是信任依據之一。"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import tarfile
import urllib.parse
import zlib
from pathlib import Path, PurePosixPath

from .bridge import SESSION
from .transport import MAX_METADATA_BYTES, MessageError

MAX_BUNDLE_BYTES = 8 * 1024 * 1024
MAX_UNPACKED_BYTES = 16 * 1024 * 1024
MAX_BUNDLE_FILES = 2048
FINGERPRINT_PREFIX = "omni-msg-bundle-v1:"
MAX_CANDIDATES = 256


def bundle_fingerprint(raw: bytes) -> str:
    """只在記憶體讀取有限 tar；涵蓋有效檔案路徑、內容與 executable bits。"""
    if not isinstance(raw, bytes) or len(raw) > MAX_BUNDLE_BYTES:
        raise MessageError("bundle_too_large")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
            unpacked = compressed.read(MAX_UNPACKED_BYTES + 1)
        if len(unpacked) > MAX_UNPACKED_BYTES:
            raise MessageError("bundle_too_large")
        entries, seen, total = [], set(), 0
        with tarfile.open(fileobj=io.BytesIO(unpacked), mode="r:") as archive:
            for member in archive:
                if set(member.pax_headers) - {"path", "size", "mtime", "atime", "ctime", "uid", "gid", "uname", "gname", "comment", "charset"}:
                    raise MessageError("unsupported_bundle_metadata")
                name = PurePosixPath(member.name)
                if (name.is_absolute() or ".." in name.parts or "\\" in member.name
                        or any(ord(c) < 32 for c in member.name) or len(member.name) > 4096
                        or member.mode & 0o7000):
                    raise MessageError("unsafe_bundle_entry")
                normalized = str(name)
                if normalized in seen or len(seen) >= MAX_BUNDLE_FILES:
                    raise MessageError("duplicate_or_oversized_bundle")
                seen.add(normalized)
                if member.isdir():
                    continue
                if member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE} or member.sparse:
                    raise MessageError("unsupported_bundle_entry")
                total += member.size
                if member.size < 0 or total > MAX_UNPACKED_BYTES:
                    raise MessageError("bundle_too_large")
                if normalized == ".":
                    raise MessageError("unsafe_bundle_entry")
                with archive.extractfile(member) as stream:
                    body = stream.read(member.size + 1)
                if len(body) != member.size:
                    raise MessageError("invalid_bundle")
                entries.append([normalized, member.mode & 0o111, hashlib.sha256(body).hexdigest()])
        if "config.yaml" not in {entry[0] for entry in entries}:
            raise MessageError("bundle_config_required")
        canonical = json.dumps(sorted(entries), ensure_ascii=True, separators=(",", ":")).encode()
        return FINGERPRINT_PREFIX + hashlib.sha256(b"omni-msg-bundle-v1\0" + canonical).hexdigest()
    except (EOFError, OSError, ValueError, UnicodeError, tarfile.TarError, zlib.error) as exc:
        raise MessageError("invalid_bundle") from exc


def validate_rules(rules):
    if not isinstance(rules, list) or not 1 <= len(rules) <= 16:
        raise MessageError("invalid_root_admission_rules")
    fields = {"agent_name", "owner", "host_id", "workspace", "bundle_fingerprint"}
    seen = set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != fields:
            raise MessageError("invalid_root_admission_rule")
        if any(not isinstance(v, str) or not v or v != v.strip() or any(ord(c) < 32 for c in v)
               for v in rule.values()):
            raise MessageError("invalid_root_admission_rule")
        if (len(rule["agent_name"]) > 256 or len(rule["owner"]) > 256
                or not SESSION.fullmatch(rule["host_id"])
                or not Path(rule["workspace"]).is_absolute() or ".." in Path(rule["workspace"]).parts
                or not re.fullmatch(FINGERPRINT_PREFIX + r"[a-f0-9]{64}", rule["bundle_fingerprint"])):
            raise MessageError("invalid_root_admission_rule")
        key = tuple(rule[k] for k in sorted(fields))
        if key in seen:
            raise MessageError("duplicate_root_admission_rule")
        seen.add(key)
    return rules


class RootAdmission:
    """以已認證的 server metadata 與 bundle 內容判斷 root，不配發 credential。"""

    def __init__(self, rules, get_json, get_bundle):
        self.rules = validate_rules(rules)
        self.get_json = get_json
        self.get_bundle = get_bundle
        self.status = {"scanned": 0, "matched": [], "rejected": []}

    def _candidates(self, deadline):
        seen = set()
        for name in sorted({rule["agent_name"] for rule in self.rules}):
            cursor, cursors = None, set()
            while True:
                query = {"kind": "default", "agent_name": name, "limit": 10, "include_archived": "false"}
                if cursor:
                    query["after"] = cursor
                page = self.get_json("/v1/sessions?" + urllib.parse.urlencode(query), deadline)
                if not isinstance(page.get("data"), list) or not isinstance(page.get("has_more"), bool):
                    raise MessageError("invalid_root_session_page")
                for row in page["data"]:
                    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not SESSION.fullmatch(row["id"]):
                        raise MessageError("invalid_root_session_page")
                    if row["id"] in seen or len(seen) >= MAX_CANDIDATES:
                        raise MessageError("duplicate_or_oversized_root_scan")
                    seen.add(row["id"])
                    yield row
                if not page["has_more"]:
                    break
                cursor = page.get("last_id")
                if not isinstance(cursor, str) or not SESSION.fullmatch(cursor) or cursor in cursors:
                    raise MessageError("invalid_root_session_cursor")
                cursors.add(cursor)

    @staticmethod
    def _scope_reason(row, rule):
        # permission_level 是 viewer 權限，不能替代實際 owner（admin 也可能看到 4）。
        for key in ["agent_name", "owner", "host_id", "workspace"]:
            if row.get(key) != rule[key]:
                return key + "_mismatch"
        return None

    def discover(self, deadline, *, explicit_ids=()):
        self.status = {"scanned": 0, "matched": [], "rejected": []}
        matches, fingerprints = {}, {}
        for row in self._candidates(deadline):
            self.status["scanned"] += 1
            sid = row["id"]
            if sid in explicit_ids:
                continue
            rejection = self._match(row, deadline, fingerprints, matches)
            if rejection:
                if isinstance(rejection, str):
                    rejection = {"reason": rejection}
                self.status["rejected"].append({"session_id": sid, **rejection})
        self.status["matched"] = sorted(matches)
        return matches

    @staticmethod
    def _phase_rejection(reason, phase, error):
        return {
            "reason": reason,
            "phase": phase,
            "status": error.status,
            "limit": error.limit,
            "read_bytes": error.read_bytes,
        }

    def _match(self, row, deadline, fingerprints, matches):
        sid = row["id"]
        scoped = [rule for rule in self.rules if self._scope_reason(row, rule) is None]
        if not scoped:
            return self._scope_reason(row, self.rules[0])
        if row.get("parent_session_id") is not None:
            return "not_top_level"
        try:
            node = self.get_json(
                f"/v1/sessions/{sid}?include_items=false", deadline,
            )
        except MessageError as exc:
            if exc.code == "registration_scan_incomplete":
                raise
            reason = "root_access_unavailable" if exc.status in {403, 404} else "session_metadata_unverifiable"
            return self._phase_rejection(reason, "session_metadata", exc)
        if node.get("id") != sid:
            return self._phase_rejection(
                "session_metadata_unverifiable", "session_metadata",
                MessageError(
                    "root_identity_mismatch", 502,
                    limit=MAX_METADATA_BYTES, read_bytes=None,
                ),
            )
        if "parent_session_id" not in node or node["parent_session_id"] is not None:
            return "not_top_level"
        if "owner" in node and node["owner"] != row["owner"]:
            return "owner_changed"
        if node.get("agent_id") != row.get("agent_id"):
            return "agent_identity_changed"
        if (not isinstance(node.get("agent_id"), str)
                or not SESSION.fullmatch(node["agent_id"])
                or not isinstance(node.get("agent_name"), str)):
            return self._phase_rejection(
                "session_metadata_unverifiable", "session_metadata",
                MessageError(
                    "invalid_session_metadata", 502,
                    limit=MAX_METADATA_BYTES, read_bytes=None,
                ),
            )
        current = {**node, "owner": row["owner"]}
        scoped = [rule for rule in scoped if self._scope_reason(current, rule) is None]
        if not scoped:
            return "root_scope_changed"
        if node.get("archived") is not False:
            return "archived"
        level = node.get("permission_level")
        if type(level) is not int or level < 2:
            return "edit_permission_required"
        if not isinstance(node.get("runner_id"), str) or not node["runner_id"]:
            return "runner_binding_required"
        try:
            agent = self.get_json(f"/v1/sessions/{sid}/agent", deadline)
        except MessageError as exc:
            if exc.code == "registration_scan_incomplete":
                raise
            return self._phase_rejection(
                "agent_metadata_unverifiable", "agent_metadata", exc,
            )
        if (not isinstance(agent.get("id"), str) or not SESSION.fullmatch(agent["id"])
                or agent["id"] != node["agent_id"] or agent.get("name") != node["agent_name"]
                or type(agent.get("version")) is not int or agent["version"] < 1):
            return self._phase_rejection(
                "agent_metadata_unverifiable", "agent_metadata",
                MessageError(
                    "invalid_agent_metadata", 502,
                    limit=MAX_METADATA_BYTES, read_bytes=None,
                ),
            )
        key = (agent["id"], agent["version"])
        if key not in fingerprints:
            try:
                raw = self.get_bundle(f"/v1/sessions/{sid}/agent/contents", deadline)
            except MessageError as exc:
                if exc.code == "registration_scan_incomplete":
                    raise
                return self._phase_rejection(
                    "bundle_unverifiable", "bundle", exc,
                )
            try:
                fingerprints[key] = bundle_fingerprint(raw)
            except MessageError as exc:
                error = MessageError(
                    exc.code, 502,
                    limit=MAX_BUNDLE_BYTES, read_bytes=len(raw),
                )
                return self._phase_rejection(
                    "bundle_unverifiable", "bundle", error,
                )
        if fingerprints[key] not in {r["bundle_fingerprint"] for r in scoped}:
            return "bundle_fingerprint_mismatch"
        matches[sid] = {k: node[k] for k in ["workspace", "host_id", "agent_id", "runner_id"]}
        return None

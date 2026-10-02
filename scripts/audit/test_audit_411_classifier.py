#!/usr/bin/env python3
"""#411 audit 純邏輯測試（fixture，不需 odoo 環境、不連叢集）。

覆蓋 audit_411_reset_values.py 的：
- 比對引擎：_flatten / _is_injector_path / _is_config_sensitive / classify_diff /
  compute_platform_values
- 報告落點守線：default_report_path / read_report_env / guard_report_target

跑法：
    python3 scripts/audit/test_audit_411_classifier.py
    # 或 python3 -m pytest scripts/audit/test_audit_411_classifier.py
"""
import ast
import importlib.util
import pathlib
import tempfile
import unittest

# 從同目錄檔案載入受測模組（檔名底線合法，但用 importlib 免依賴 sys.path）。
_spec = importlib.util.spec_from_file_location(
    "audit_411", pathlib.Path(__file__).with_name("audit_411_reset_values.py")
)
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)


class TestFlatten(unittest.TestCase):
    def test_nested_to_tuple_paths(self):
        self.assertEqual(
            audit._flatten({"a": {"b": 1}, "c": 2}),
            {("a", "b"): 1, ("c",): 2},
        )

    def test_list_is_leaf(self):
        # list 整體當 leaf（apiServerCidrs 這類整份比對）
        self.assertEqual(audit._flatten({"x": [1, 2]}), {("x",): [1, 2]})

    def test_dotted_key_not_split(self):
        # 鍵名含點不 dot-join 假陽性（tuple path 保護）
        self.assertEqual(
            audit._flatten({"nodeSelector": {"kubernetes.io/arch": "amd64"}}),
            {("nodeSelector", "kubernetes.io/arch"): "amd64"},
        )


class TestInjectorAndSensitivePaths(unittest.TestCase):
    def test_injector_paths(self):
        for p in [
            ("persistence", "storageClass"),
            ("postgresql", "primary", "persistence", "storageClass"),
            ("global", "imagePullSecrets"),
            ("imagePullSecrets",),
            ("config", "allowedOrigins"),
            ("networkPolicy", "apiServerCidrs"),
            ("codeServer", "publicUrl"),
            ("server", "baseUrl"),
            ("nextcloud", "publicUrl"),
            ("config", "dashboardPublicUrl"),
            ("omnigentHost", "serverUrl"),
        ]:
            self.assertTrue(audit._is_injector_path(p), p)

    def test_partial_or_deeper_paths_are_not_injector(self):
        # 整條路徑比對：同前綴但不同葉子的租戶鍵不得被誤判成自癒
        for p in [("omnigentHost", "hostName"), ("omnigentHost",),
                  ("omnigentHost", "serverUrl", "x"), ("config", "allowedOriginsX")]:
            self.assertFalse(audit._is_injector_path(p), p)

    def test_non_injector_paths(self):
        for p in [("image", "tag"), ("config", "nonSensitive", "x"), ()]:
            self.assertFalse(audit._is_injector_path(p), p)

    def test_config_sensitive(self):
        self.assertTrue(audit._is_config_sensitive(("config", "sensitive", "admin_password")))
        self.assertFalse(audit._is_config_sensitive(("config", "nonSensitive", "x")))


_CLOUD_SERVICE_PY = (
    pathlib.Path(__file__).resolve().parents[2] / "src" / "models" / "cloud_service.py"
)


def _tuple_of_str(node):
    if isinstance(node, ast.Tuple) and node.elts and all(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts):
        return tuple(e.value for e in node.elts)
    return None


def _paths_in_dict(node):
    """{slug: [(k1, k2), ...]} 字面量裡的所有 tuple path。"""
    out = set()
    if not isinstance(node, ast.Dict):
        return out
    for value in node.values:
        if isinstance(value, ast.List):
            for elt in value.elts:
                path = _tuple_of_str(elt)
                if path:
                    out.add(path)
    return out


def platform_injector_paths(source):
    """以 AST 讀 cloud_service.py：module 層 ``_*_VALUE_PATHS`` 表，加上每個
    ``_inject_*`` 函式裡的 ``path_map`` 字面量。回 {(table name, path), ...}。"""
    tree = ast.parse(source)
    found = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id.endswith("_VALUE_PATHS"):
            for path in _paths_in_dict(node.value):
                found.add((node.targets[0].id, path))
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name.startswith("_inject_"):
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == "path_map"
                        for t in node.targets):
                    for path in _paths_in_dict(node.value):
                        found.add((fn.name, path))
    return found


class TestInjectorPathsLockstep(unittest.TestCase):
    """audit 的 injector 清單是 cloud_service.py 的複本：那邊多一條 injector path、
    這邊沒登記 ⇒ 紅（否則該鍵的 stored／computed 差異會被誤分成 backfill／裁決）。"""

    def test_every_platform_injector_path_is_registered(self):
        found = platform_injector_paths(_CLOUD_SERVICE_PY.read_text(encoding="utf-8"))
        # floor：解析器若壞掉（找不到任何表）這支測試就變成空跑，所以釘住幾條已知的
        paths = {path for _table, path in found}
        for must in [("omnigentHost", "serverUrl"), ("config", "dashboardPublicUrl"),
                     ("config", "allowedOrigins"), ("persistence", "storageClass")]:
            self.assertIn(must, paths)
        missing = sorted(
            "%s: %s" % (table, ".".join(path))
            for table, path in found if not audit._is_injector_path(path)
        )
        self.assertEqual(missing, [], "injector path 沒登記到 audit_411 _INJECTOR_VALUE_PATHS")

    def test_lockstep_check_detects_an_unregistered_path(self):
        # 陽性對照：同一個抽取器對「多一條沒登記的 injector path」必須抓得到
        source = (
            "_NEW_THING_VALUE_PATHS = {'x': [('brandNew', 'url')]}\n"
            "def _inject_other(values):\n"
            "    path_map = {'y': [('another', 'path')]}\n"
        )
        found = platform_injector_paths(source)
        self.assertEqual(
            {path for _t, path in found}, {("brandNew", "url"), ("another", "path")})
        self.assertTrue(
            all(not audit._is_injector_path(path) for _t, path in found))


class TestClassifyDiff(unittest.TestCase):
    def test_pending_when_computed_none(self):
        # 未提供 computed（dump 未涵蓋）→ None（標 pending）
        self.assertIsNone(audit.classify_diff({"a": 1}, None))

    def test_match_not_listed(self):
        d = audit.classify_diff({"a": 1, "b": {"c": 2}}, {"a": 1, "b": {"c": 2}})
        self.assertEqual(d["counts"], {"a": 0, "b": 0, "c": 0, "info": 0})
        self.assertEqual(d["items"], [])

    def test_a_injector_value_diff(self):
        # injector 同鍵不同值 → a（reset 後管線重算，自癒）
        d = audit.classify_diff(
            {"persistence": {"storageClass": "old"}},
            {"persistence": {"storageClass": "longhorn-delete"}},
        )
        self.assertEqual(d["counts"]["a"], 1)
        self.assertEqual(d["items"][0]["category"], "a")

    def test_a_injector_stored_only(self):
        # injector 鍵 stored 有 computed 無 → a（仍自癒）
        d = audit.classify_diff({"global": {"imagePullSecrets": ["x"]}}, {})
        self.assertEqual(d["counts"], {"a": 1, "b": 0, "c": 0, "info": 0})

    def test_a_omnigent_url_left_behind_after_omnigent_deleted(self):
        # Omnigent 被刪：stored release 還有舊網址、computed 沒有 → a，不得叫人 backfill
        d = audit.classify_diff(
            {"omnigentHost": {"serverUrl": "http://svc-x-omnigent.ns.svc.cluster.local:8000",
                              "enabled": True}},
            {"omnigentHost": {"enabled": True}},
        )
        self.assertEqual(d["counts"], {"a": 1, "b": 0, "c": 0, "info": 0})
        self.assertEqual(d["items"][0]["path"], ["omnigentHost", "serverUrl"])

    def test_a_config_sensitive_stored_only(self):
        # config.sensitive stored-only → a（sensitive_values 另帶，不 backfill）；值遮罩
        d = audit.classify_diff(
            {"config": {"sensitive": {"admin_password": "must-not-leak"}}}, {}
        )
        it = d["items"][0]
        self.assertEqual(it["category"], "a")
        self.assertEqual(it["stored"], "***")

    def test_b_backfill_stored_only_non_injector(self):
        # stored 有、computed 無、非 injector → b（backfill）
        d = audit.classify_diff({"customFoo": "bar"}, {})
        self.assertEqual(d["counts"]["b"], 1)
        it = d["items"][0]
        self.assertEqual(it["category"], "b")
        self.assertEqual(it["stored"], "bar")
        self.assertIsNone(it["computed"])

    def test_c_image_tag_pin(self):
        # 同鍵不同值、非 injector → c（image tag pin 型，reset 後被覆蓋）
        d = audit.classify_diff(
            {"image": {"tag": "1.0.0-pinned"}}, {"image": {"tag": "2.0.0"}}
        )
        self.assertEqual(d["counts"]["c"], 1)
        it = d["items"][0]
        self.assertEqual(it["category"], "c")
        self.assertEqual(it["stored"], "1.0.0-pinned")
        self.assertEqual(it["computed"], "2.0.0")

    def test_info_computed_only(self):
        # computed 有、stored 無 → info（reset 後自然補上，預期）
        d = audit.classify_diff({}, {"newChartDefault": "x"})
        self.assertEqual(d["counts"]["info"], 1)
        self.assertEqual(d["items"][0]["category"], "info")

    def test_secret_by_keyname_heuristic_masked(self):
        # 非 config.sensitive 但鍵名命中 heuristic（currentKey）→ 值遮罩，分類照常（b）
        d = audit.classify_diff({"auth": {"currentKey": "deadbeef" * 8}}, {})
        it = d["items"][0]
        self.assertEqual(it["category"], "b")
        self.assertEqual(it["stored"], "***")

    def test_mixed_release(self):
        # 綜合：injector 差異(a) + backfill(b) + tag pin(c) + 新預設(info) + match(不列)
        stored = {
            "persistence": {"storageClass": "old"},        # a
            "customTenantKnob": "keep",                     # b
            "image": {"tag": "pinned"},                     # c
            "replicaCount": 1,                              # match
        }
        computed = {
            "persistence": {"storageClass": "longhorn-delete"},  # a
            "image": {"tag": "fresh"},                            # c
            "replicaCount": 1,                                    # match
            "newDefault": True,                                   # info
        }
        d = audit.classify_diff(stored, computed)
        self.assertEqual(d["counts"], {"a": 1, "b": 1, "c": 1, "info": 1})


class TestComputePlatformValues(unittest.TestCase):
    def test_lookup_hit_and_miss(self):
        lookup = {("ns1", "rel1"): {"a": 1}}
        self.assertEqual(audit.compute_platform_values(lookup, "ns1", "rel1"), {"a": 1})
        self.assertIsNone(audit.compute_platform_values(lookup, "ns1", "relX"))
        self.assertIsNone(audit.compute_platform_values({}, "ns", "rel"))


class TestRenderReportIntegration(unittest.TestCase):
    """render_report 端到端：diff 結構整合、遮罩、鐵律段不被分類段吃掉。"""

    def _meta(self):
        return {
            "context": "woow-k3s", "env": "stg",
            "generated_at": "2026-01-01 00:00:00 +0800",
            "helm_version": "v4.2.3", "raw_dir": "/tmp/x",
            "ns_count": 1, "diff_status": "test",
        }

    def test_render_with_diff_lists_backfill_and_masks_secret(self):
        stored = {"customKnob": "keep", "config": {"sensitive": {"admin_password": "SEKRET"}}}
        diff = audit.classify_diff(stored, {})
        record = {
            "env": "stg", "ns": "paas-ws-stg-x", "release": "svc-1",
            "chart": "n8n-2.0.1", "status": "deployed", "revision": "2",
            "stored_key_count": 2, "masked_stored": audit.sanitize(stored),
            "classification": "a:1 b:1 c:0 info:0", "diff": diff,
        }
        out = audit.render_report([record], self._meta())
        self.assertIn("### (b) backfill 清單", out)
        self.assertIn("customKnob", out)              # backfill 項出現
        self.assertNotIn("SEKRET", out)               # 明文 secret 不外洩
        self.assertIn("鐵律與 raw dump 落點", out)     # 鐵律段仍在（未被提前 return 吃掉）

    def test_render_without_diff_shows_pending(self):
        record = {
            "env": "stg", "ns": "ns", "release": "r", "chart": "c",
            "status": "deployed", "revision": "1", "stored_key_count": 0,
            "masked_stored": {}, "classification": "pending", "diff": None,
        }
        out = audit.render_report([record], self._meta())
        self.assertIn("diff 標 pending", out)
        self.assertIn("鐵律與 raw dump 落點", out)


class TestReportTarget(unittest.TestCase):
    """報告落點守線（本腳本會**無條件覆寫** --report 指到的檔案）。

    舊版 `--report` 預設值直接指向 repo 內已 commit 的報告，對空集合環境（prod 零 CS）
    跑一次就把完整報告靜默蓋成空骨架、exit 0 無警告。修法＝預設落 tmp（repo 外）、
    檔名帶 env、既有報告 env 標記不符時 fail-loud。
    """

    def _meta(self, env):
        return {
            "context": "woow-k3s", "env": env,
            "generated_at": "2026-01-01 00:00:00 +0800",
            "helm_version": "v4.2.3", "raw_dir": "/tmp/x",
            "ns_count": 0, "diff_status": "test",
        }

    def _existing_report(self, tmpdir, env):
        """在 tmpdir 產一份「該 env 的既有報告」（走真 render_report，格式與實跑一致）。"""
        path = pathlib.Path(tmpdir) / "existing.md"
        path.write_text(audit.render_report([], self._meta(env)), encoding="utf-8")
        return path

    # --- 預設落點 ---
    def test_default_report_outside_repo(self):
        # 預設落點不得在 repo 內（否則不給 --report 就會覆寫已 commit 的報告）
        repo_root = pathlib.Path(__file__).resolve().parents[2]
        for env in ("prod", "stg", "both"):
            path = audit.default_report_path(env)
            self.assertFalse(
                str(path).startswith(str(repo_root)), f"env={env} 預設落點在 repo 內：{path}"
            )

    def test_default_report_name_carries_env(self):
        # 檔名帶 env → prod / stg / both 三份預設報告天然不互踩
        names = {env: audit.default_report_path(env).name for env in ("prod", "stg", "both")}
        self.assertEqual(len(set(names.values())), 3, names)
        for env, name in names.items():
            self.assertIn(env, name)
            self.assertTrue(name.endswith(".md"), name)

    # --- env 標記解析 ---
    def test_read_report_env_roundtrip(self):
        # render_report 寫的 header marker 必須被 read_report_env 讀回（格式漂移即紅）
        self.assertEqual(audit.read_report_env(audit.render_report([], self._meta("stg"))), "stg")
        self.assertEqual(audit.read_report_env(audit.render_report([], self._meta("both"))), "both")

    def test_read_report_env_none_when_absent(self):
        self.assertIsNone(audit.read_report_env("# 手寫文件\n\n沒有 env 標記。\n"))

    # --- 覆寫守線 ---
    def test_guard_blocks_env_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._existing_report(tmp, "stg")
            with self.assertRaises(SystemExit) as ctx:
                audit.guard_report_target(path, "prod", force=False)
            self.assertIn("--force", str(ctx.exception))
            # 守線只擋、不動檔（fail 前後內容不變）
            self.assertEqual(audit.read_report_env(path.read_text(encoding="utf-8")), "stg")

    def test_guard_allows_same_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._existing_report(tmp, "stg")
            audit.guard_report_target(path, "stg", force=False)  # 不 raise

    def test_guard_allows_with_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._existing_report(tmp, "stg")
            audit.guard_report_target(path, "prod", force=True)  # 顯式覆寫放行

    def test_guard_allows_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit.guard_report_target(pathlib.Path(tmp) / "nope.md", "prod", force=False)

    def test_guard_allows_when_no_marker(self):
        # 抓不到 marker（手寫檔／格式外）→ 放行不誤擋，只在有明確衝突時 fail-loud
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "hand-written.md"
            path.write_text("# 手寫報告\n", encoding="utf-8")
            audit.guard_report_target(path, "prod", force=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)

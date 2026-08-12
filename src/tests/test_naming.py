"""Tests for naming utility module."""
import unittest

from ..services.naming import (
    generate_ws_hash,
    generate_resource_hash,
    make_namespace,
    make_service_subdomain,
    make_smarthome_subdomain,
    make_security_access_subdomain,
    reconstruct_mcp_subdomain,
    parse_paas_name,
    classify_env_hint,
)

# #677：`make_*` 系列吃的是 `workspace.ws_hash`（伺服器產生的 8 hex），**不是**
# `slug`。本檔原本傳的是 slug 字面值（`'my-ws'` 之類），那在修法後會被
# `naming.py::_require_ws_hash` 直接 raise —— 那正是它的用意（漏改的呼叫點會炸，
# 不會安靜產出 `paas-ws-my-ws`）。
WS_HASH = 'a1b2c3d4'


class TestNaming(unittest.TestCase):
    """Test cases for naming utility functions."""

    def test_generate_ws_hash_deterministic(self):
        """Test that the same slug always produces the same hash."""
        h1 = generate_ws_hash('my-workspace')
        h2 = generate_ws_hash('my-workspace')
        self.assertEqual(h1, h2)

    def test_generate_ws_hash_length(self):
        """Test that ws_hash is exactly 8 hex chars."""
        h = generate_ws_hash('test-slug')
        self.assertEqual(len(h), 8)
        # Should be valid hex
        int(h, 16)

    def test_generate_ws_hash_different_slugs(self):
        """Test that different slugs produce different hashes."""
        h1 = generate_ws_hash('workspace-a')
        h2 = generate_ws_hash('workspace-b')
        self.assertNotEqual(h1, h2)

    def test_generate_resource_hash_deterministic(self):
        """Test that same inputs produce same hash."""
        h1 = generate_resource_hash('ref-123', 'my-service')
        h2 = generate_resource_hash('ref-123', 'my-service')
        self.assertEqual(h1, h2)

    def test_generate_resource_hash_length(self):
        """Test that resource hash is exactly 8 hex chars."""
        h = generate_resource_hash('ref-abc', 'svc-name')
        self.assertEqual(len(h), 8)
        int(h, 16)

    def test_generate_resource_hash_salted(self):
        """Test that different reference_ids produce different hashes."""
        h1 = generate_resource_hash('ref-1', 'same-name')
        h2 = generate_resource_hash('ref-2', 'same-name')
        self.assertNotEqual(h1, h2)

    def test_make_namespace_format(self):
        """Test namespace follows paas-ws-{hash} format."""
        ns = make_namespace(WS_HASH)
        self.assertTrue(ns.startswith('paas-ws-'))
        self.assertEqual(len(ns), 16)  # 'paas-ws-' (8) + hash (8)

    def test_make_namespace_k8s_valid(self):
        """Test namespace meets K8s naming constraints (max 63 chars, lowercase).

        #677 之後長度是**結構性**有界的（前綴 ＋ 固定 8 hex），不再取決於輸入長度
        ——原本這條餵一個超長 slug 來證明「長輸入也不會超標」，那個風險已經不存在。
        改為連同前綴一起驗：`namespace_prefix` 是 config_parameter（stg 用
        `paas-ws-stg-`），前綴才是今天唯一還能把名字撐長的東西。
        """
        import re
        for prefix in ('paas-ws-', 'paas-ws-stg-'):
            ns = make_namespace(WS_HASH, prefix=prefix)
            self.assertLessEqual(len(ns), 63, prefix)
            self.assertEqual(ns, ns.lower(), prefix)
            # K8s names must be DNS-compatible
            self.assertTrue(re.match(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$', ns), prefix)

    def test_make_namespace_rejects_a_slug(self):
        """#677 絆線：maker 收到非 8-hex（例如有人又傳 slug 進來）必須 raise。

        沒有它，漏改的呼叫點會產出 `paas-ws-my-workspace` —— 合法 DNS label、
        正確前綴、部署得起來，只是指向一個不屬於任何 workspace 的 namespace。
        """
        for bad in ('my-workspace', 'ws-137425', '', None, 'A1B2C3D4', 'a1b2c3d'):
            with self.assertRaises(ValueError):
                make_namespace(bad)

    def test_make_service_subdomain_format(self):
        """Test service subdomain follows paas-cs-{ws_hash}-{svc_hash} format."""
        sd = make_service_subdomain(WS_HASH, 'ref-123', 'my-svc')
        self.assertTrue(sd.startswith('paas-cs-'))
        parts = sd.split('-')
        # paas-cs-{8char}-{8char}
        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[0], 'paas')
        self.assertEqual(parts[1], 'cs')
        self.assertEqual(len(parts[2]), 8)
        self.assertEqual(len(parts[3]), 8)

    def test_make_smarthome_subdomain_format(self):
        """Test smarthome subdomain follows paas-sm-{ws_hash}-{sm_hash} format."""
        sd = make_smarthome_subdomain(WS_HASH, 'ref-456', 'my-home')
        self.assertTrue(sd.startswith('paas-sm-'))
        parts = sd.split('-')
        self.assertEqual(len(parts), 4)
        self.assertEqual(parts[0], 'paas')
        self.assertEqual(parts[1], 'sm')
        self.assertEqual(len(parts[2]), 8)
        self.assertEqual(len(parts[3]), 8)

    def test_make_smarthome_subdomain_k8s_valid(self):
        """Test smarthome subdomain meets K8s naming constraints."""
        sd = make_smarthome_subdomain(WS_HASH, 'ref-id', 'home-name')
        self.assertLessEqual(len(sd), 63)
        self.assertEqual(sd, sd.lower())

    def test_different_resource_types_different_prefixes(self):
        """Test that service and smarthome subdomains have different prefixes."""
        svc = make_service_subdomain(WS_HASH, 'ref', 'name')
        sm = make_smarthome_subdomain(WS_HASH, 'ref', 'name')
        self.assertTrue(svc.startswith('paas-cs-'))
        self.assertTrue(sm.startswith('paas-sm-'))
        self.assertNotEqual(svc, sm)


class TestSecurityAccessNaming(unittest.TestCase):
    """make_security_access_subdomain: paas-sa-{ws_hash}-{res_hash}."""

    def test_sa_subdomain_format(self):
        """Test SA subdomain follows paas-sa-{ws_hash}-{res_hash} format."""
        result = make_security_access_subdomain(WS_HASH, 'ref-123', 'My SA')
        self.assertEqual(
            result,
            f"paas-sa-{WS_HASH}-{generate_resource_hash('ref-123', 'My SA')}",
        )
        self.assertTrue(result.startswith('paas-sa-'))
        # prefix + 8 hex + hyphen + 8 hex
        self.assertEqual(len(result), len('paas-sa-') + 8 + 1 + 8)

    def test_sa_subdomain_deterministic(self):
        """Test that same inputs always produce the same subdomain."""
        a = make_security_access_subdomain(WS_HASH, 'r1', 'n1')
        b = make_security_access_subdomain(WS_HASH, 'r1', 'n1')
        self.assertEqual(a, b)

    def test_sa_prefix_distinct_from_sm_cs(self):
        """Test SA prefix differs from smarthome and cloud service (namespace isolation)."""
        sa = make_security_access_subdomain(WS_HASH, 'r', 'n')
        sm = make_smarthome_subdomain(WS_HASH, 'r', 'n')
        cs = make_service_subdomain(WS_HASH, 'r', 'n')
        # Same hash, different prefix -> three resource types stay isolated.
        self.assertNotEqual(sa, sm)
        self.assertNotEqual(sa, cs)

    def test_sa_subdomain_k8s_valid(self):
        """Test SA subdomain meets K8s/DNS naming constraints."""
        sd = make_security_access_subdomain(WS_HASH, 'ref-id', 'sa-name')
        self.assertLessEqual(len(sd), 63)
        self.assertEqual(sd, sd.lower())
        import re
        self.assertTrue(re.match(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$', sd))


class TestNamingEnvToken(unittest.TestCase):
    """L3: env token 注入 make_*_subdomain。

    決策 DP1：格式 ``paas-{env}-{type}-``（stg=paas-stg-sa-、prod=paas-sa- 空 token）。
    決策 DP2：CS/SM 是否在 model 呼叫端傳 env 由控制器決定；naming helper 一律支援
    env 參數。prod/空 token 必須與舊行為 byte-identical（既有硬編斷言不假紅）。
    斷言用存在性/差異性，不硬編完整快照（phase-versioned-test-assertion）。
    """

    def test_sa_subdomain_prod_equals_no_env(self):
        """env='prod' 與不傳 env byte-identical（prod 維持現狀、零漂移）。"""
        no_env = make_security_access_subdomain(WS_HASH, 'ref-123', 'My SA')
        prod = make_security_access_subdomain(WS_HASH, 'ref-123', 'My SA', env='prod')
        self.assertEqual(prod, no_env)
        self.assertTrue(prod.startswith('paas-sa-'))
        self.assertNotIn('stg', prod)

    def test_sa_subdomain_default_has_no_env_token(self):
        """預設（不傳 env）不含環境 token。"""
        r = make_security_access_subdomain(WS_HASH, 'ref-123', 'My SA')
        self.assertTrue(r.startswith('paas-sa-'))
        self.assertNotIn('stg', r)

    def test_sa_subdomain_stg_carries_env_token(self):
        """env='stg' 帶 token、仍 paas- 起頭、與 prod 版相異（存在性+差異性）。

        placement（env 在 type 前）由 test_env_token_format_is_paas_env_type 專守，
        此處只守「有 token 且與 prod 不同」，避免兩條鎖同一 intent。
        """
        prod = make_security_access_subdomain(WS_HASH, 'ref-123', 'My SA', env='prod')
        stg = make_security_access_subdomain(WS_HASH, 'ref-123', 'My SA', env='stg')
        self.assertIn('stg', stg)
        self.assertTrue(stg.startswith('paas-'))
        self.assertNotEqual(stg, prod)

    def test_env_token_format_is_paas_env_type(self):
        """鎖 DP1 決策：env token 排在 type 之前（相對位置語意，不耦合確切分隔符）。

        用 index 比較而非硬編 'paas-stg-sa-'：未來若合法改分隔符仍守住「env 在
        type 前」的 intent，不會因快照漂移誤紅（phase-versioned-test-assertion）。
        """
        stg = make_security_access_subdomain(WS_HASH, 'ref-123', 'My SA', env='stg')
        self.assertTrue(stg.startswith('paas-'))
        self.assertLess(stg.index('stg-'), stg.index('sa-'))

    def test_env_token_non_whitelist_value_stays_dns_safe(self):
        """非白名單值（raw set_param 繞過 Selection）也須 slugify 成 DNS-safe。

        deploy_env 是 Selection 白名單，但 set_param 可寫任意值；naming helper 絕
        不能產出非法 DNS/k8s label（含空白 / 底線 / 非 ASCII / 尾 dash），否則
        tunnel name / DNS comment 在 CF 端炸。
        """
        import re
        for bad in ('My Env_2!', 'staging zone', 'prod_v2', 'XX__yy', '!!!'):
            r = make_security_access_subdomain(WS_HASH, 'ref', 'name', env=bad)
            self.assertEqual(r, r.lower(), bad)
            self.assertLessEqual(len(r), 63, bad)
            self.assertRegex(r, r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?$', bad)

    def test_cs_subdomain_stg_carries_env_token(self):
        """make_service_subdomain 支援 env 且 stg 帶 token。"""
        cs = make_service_subdomain(WS_HASH, 'ref-123', 'my-svc', env='stg')
        self.assertIn('stg', cs)
        self.assertTrue(cs.startswith('paas-stg-cs-'))

    def test_sm_subdomain_stg_carries_env_token(self):
        """make_smarthome_subdomain 支援 env 且 stg 帶 token。"""
        sm = make_smarthome_subdomain(WS_HASH, 'ref-456', 'my-home', env='stg')
        self.assertIn('stg', sm)
        self.assertTrue(sm.startswith('paas-stg-sm-'))

    def test_env_token_dns_safe_all_makers_all_envs(self):
        """所有 maker × {空,prod,stg} 皆 DNS/k8s-safe（lower、<=63、regex）。"""
        import re
        makers = (
            make_service_subdomain,
            make_smarthome_subdomain,
            make_security_access_subdomain,
        )
        for env in ('', 'prod', 'stg'):
            for maker in makers:
                r = maker(WS_HASH, 'reference-id-long', 'resource', env=env)
                self.assertEqual(r, r.lower(), (maker.__name__, env))
                self.assertLessEqual(len(r), 63, (maker.__name__, env))
                self.assertRegex(r, r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?$')

    def test_env_token_normalised_case_and_whitespace(self):
        """admin 誤填 'STG' / ' stg ' 須正規化，不產非法 name。"""
        canonical = make_security_access_subdomain(WS_HASH, 'ref', 'name', env='stg')
        self.assertEqual(
            make_security_access_subdomain(WS_HASH, 'ref', 'name', env='STG'), canonical)
        self.assertEqual(
            make_security_access_subdomain(WS_HASH, 'ref', 'name', env=' stg '), canonical)


class TestReconstructMcpSubdomain(unittest.TestCase):
    """L4 reconciler：reconstruct_mcp_subdomain 必須與 operator deploy/teardown
    的 MCP subdomain byte-identical（漂移 → 活躍 MCP route 被誤判成孤兒）。

    operator 端公式（cross-repo，無共用 import，靠 golden cases 鎖）：
      cloudflare.py generate_subdomain(ns, release) = f"{release}-{ns.replace('paas-ws-','')}"
      releases.py 對 MCP route 再接 "-mcp"
    → 完整 MCP subdomain = f"{release}-{ns_suffix}-mcp"。
    """

    def test_matches_operator_generate_subdomain_plus_mcp(self):
        """golden：複刻 operator generate_subdomain + '-mcp'（鎖確切字面，防跨 repo 漂移）。"""
        self.assertEqual(
            reconstruct_mcp_subdomain('paas-ws-115223fa', 'myapp'),
            'myapp-115223fa-mcp',
        )
        self.assertEqual(
            reconstruct_mcp_subdomain('paas-ws-abc12345', 'n8n-xyz'),
            'n8n-xyz-abc12345-mcp',
        )

    def test_namespace_without_paas_ws_prefix_is_no_op_like_operator(self):
        """operator 用 str.replace（前綴不在則 no-op）→ reconstruct 須一致（不額外剝）。"""
        self.assertEqual(
            reconstruct_mcp_subdomain('custom-ns', 'app'),
            'app-custom-ns-mcp',
        )

    def test_ns_suffix_is_namespace_minus_paas_ws(self):
        """鎖 intent：ns_suffix = namespace 去掉 'paas-ws-' 前綴（與 operator 同源）。"""
        result = reconstruct_mcp_subdomain('paas-ws-deadbeef', 'rel')
        self.assertEqual(result, 'rel-deadbeef-mcp')
        self.assertNotIn('paas-ws-', result)

    def test_none_namespace_defensive(self):
        """None namespace 不炸（collector 只對有值的 CS 呼叫，但函式自身防呆）。"""
        self.assertEqual(reconstruct_mcp_subdomain(None, 'app'), 'app--mcp')


class TestParsePaasName(unittest.TestCase):
    """L4 reconciler S3：parse_paas_name 解析 (env_token, kind)，literal (stg) 白名單。

    鏡像 _env_token 對 {prod,stg} 白名單輸出：prod/legacy 無 token byte-identical → (None,kind)；
    stg → ('stg',kind)；非白名單 token（foo）/ 非 paas 名 → (None,None)（不 over-accept、不靜默漏）。
    """

    def test_prod_legacy_no_token_returns_none_kind(self):
        self.assertEqual(parse_paas_name('paas-sa-5c25b8a1'), (None, 'sa'))
        self.assertEqual(parse_paas_name('paas-sm-115223fa'), (None, 'sm'))
        self.assertEqual(parse_paas_name('paas-cs-aabbccdd-11223344'), (None, 'cs'))

    def test_stg_token_parsed(self):
        self.assertEqual(parse_paas_name('paas-stg-sa-5c25b8a1'), ('stg', 'sa'))
        self.assertEqual(parse_paas_name('paas-stg-sm-115223fa'), ('stg', 'sm'))

    def test_non_whitelist_token_not_over_accepted(self):
        """非白名單 token 'foo' 不可被當 explicit env（回 (None,None) 而非 ('foo','sa')）。"""
        self.assertEqual(parse_paas_name('paas-foo-sa-xxx'), (None, None))

    def test_multi_label_token_not_matched(self):
        """_env_token 對多 label（'foo bar'→paas-foo-bar-sa-）→ 不 match → unknown（不靜默誤判）。"""
        self.assertEqual(parse_paas_name('paas-foo-bar-sa-xxx'), (None, None))

    def test_non_paas_names_return_none_none(self):
        for name in ('woowtech.io', 'promo2026', 'paas-stg-xx-yy', ''):
            self.assertEqual(parse_paas_name(name), (None, None), name)


class TestClassifyEnvHint(unittest.TestCase):
    """L4 reconciler S3：classify_env_hint(name, pinned_env) -> (env_hint, is_cross_env)。"""

    def test_same_env_not_cross(self):
        self.assertEqual(classify_env_hint('paas-stg-sm-1', 'stg'), ('stg', False))

    def test_explicit_token_other_env_is_cross(self):
        """prod-pinned 看到 stg 資源 → cross-env（單向：只抓得到 stg 被 prod 看到）。"""
        self.assertEqual(classify_env_hint('paas-stg-sm-1', 'prod'), ('stg', True))

    def test_no_token_is_unknown_not_cross(self):
        """prod 空 token 與 legacy 不可分 → unknown、非 cross（不瞎猜，prod 確認靠 DP-DIFF）。"""
        self.assertEqual(classify_env_hint('paas-sm-1', 'stg'), ('unknown', False))
        self.assertEqual(classify_env_hint('paas-sm-1', 'prod'), ('unknown', False))

    def test_non_paas_name_is_unknown(self):
        self.assertEqual(classify_env_hint('woowtech.io', 'stg'), ('unknown', False))

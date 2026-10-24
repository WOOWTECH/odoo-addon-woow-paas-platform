"""Seed assertions for the mcp-server chart family of cloud service templates.

家族成員（9 個，判準＝``helm_chart_name`` 以 ``/mcp-server`` 結尾）：
hermes（pilot）／odoo／odoo-manage／n8n／k8s／vk／opendesign／litellm（2026-10-05 以隱藏範本進 seed，10-07 公開上架）／
nextcloud（以隱藏範本進 seed，沙盒實測後上架）。

## 為什麼是兩層 mixin（#686）

原本只有一層 ``_McpServerSeedAssertions``，而它**是以 hermes 為反例寫的**——
``test_template_overrides_chart_default_mcp_command`` 斷言
``command != 'hermes-mcp-server'``，語意就是「別的 app 不可沿用 hermes 預設」。
於是 pilot 自己**結構上加不進來**：不是被忘了，是這個 mixin 的形狀讓它加不進去。
後果是 ``has_admin_gui`` 這條家族不變式從來沒守到 hermes，seed 也真的漏了
（#686：admin GUI 掛公開根 URL、密碼落到 mcp-admin-core 內建的字面 ``admin``）。

修法是**拆層**而不是硬塞：

* ``_McpServerFamilyAssertions``——家族不變式，**所有成員全部繼承**。
* ``_McpServerSeedAssertions``——僅適用於「非 hermes」的那一條（必須顯式覆寫
  chart 的 hermes 形預設 command）。

啟動命令在**兩層都有斷言、但語意不同**：家族層守「宣告的契約與 seed 一致」
（hermes 宣告 ``EXPECT_MCP_COMMAND = None``＝刻意吃 chart 預設），衍生層再多守
「非 hermes 必須顯式覆寫且不得沿用 hermes 預設」。

## 為什麼還要 ``TestMcpServerFamilyCoverage``

#686 真正的教訓不是「漏了 hermes 這一格」，而是「漏格不會有人發現」——新上架一筆
mcp-server chart 的 template 只要忘了寫測試子類，家族不變式對它就等於不存在，而
測試輸出與全數覆蓋時**一模一樣**。所以覆蓋率本身要被斷言，判準取自 seed 的結構
（chart 名），不是硬編清單。
"""
import json

from odoo.tests.common import TransactionCase

# 家族判準：chart 座標的尾段。與 test_admin_password_carrier_449.py 的
# MCP_SERVER_CHART_SUFFIX 同源（該檔用它決定 admin_password 載體），刻意不共用
# 常數——兩邊各自宣告才會在 chart 改名時各自紅，而不是一起靜默失效。
MCP_SERVER_CHART_SUFFIX = '/mcp-server'

# chart mcp-server（0.1.5～0.1.8 皆同）的 config.nonSensitive 預設是 hermes 形的
# （values.yaml: config.nonSensitive.mcp_server.command）。hermes 靠它拿到啟動
# 命令；其餘 app 必須覆寫，否則子行程從不 spawn、MCP endpoint 靜默 502 而
# /healthz 與 GUI 看起來都正常（GAP-O1）。
CHART_DEFAULT_MCP_COMMAND = 'hermes-mcp-server'


def _assert_storage_resources_aligned(tc, tmpl):
    """#268 lockstep guard: the seeded helm_default_values must equal the
    advertised marketplace spec, so the PVC size and container limits the
    tenant actually gets match what the marketplace card promised.

    Historic drift (#268): persistence.size was 2Gi while min_storage_gb
    advertised 5, and the container memory limit fell back to the chart
    default 512Mi while min_ram_gb advertised 1.0. Assumes whole-Gi specs
    (all mcp-* templates are 5G storage / 1G RAM / 1 vCPU)."""
    values = json.loads(tmpl.helm_default_values)
    tc.assertEqual(
        values['persistence']['size'], '%dGi' % int(tmpl.min_storage_gb),
        'persistence.size must match advertised min_storage_gb')
    limits = values['resources']['limits']
    tc.assertEqual(
        limits['memory'], '%dGi' % int(tmpl.min_ram_gb),
        'resources.limits.memory must match advertised min_ram_gb')
    tc.assertEqual(
        limits['cpu'], str(int(tmpl.min_vcpu)),
        'resources.limits.cpu must match advertised min_vcpu')


class _McpServerFamilyAssertions:
    """mcp-server chart 家族的**家族不變式**——所有成員全部必須繼承。

    Subclasses bind app-specific xmlids/values. This mixin is intentionally
    NOT a TransactionCase, so Odoo's test runner would skip it and only run
    the concrete subclasses below — except that without
    ``allow_inherited_tests_method = True`` the runner also walks past every
    ``test_*`` method defined HERE (mixin is not in the subclass'
    ``__dict__``), so the subclasses silently contributed 0 tests (false-green,
    #356). The flag propagates via the MRO to every concrete subclass.
    """

    # #356: collect inherited test_* methods from this mixin via the subclasses.
    allow_inherited_tests_method = True

    SLUG = None
    TMPL = None
    PLAN_M = None
    PLAN_Y = None
    PROD_M = None
    IMAGE_REPO_SUFFIX = None  # e.g. 'odoo-mcp-admin'
    APP = None  # helm values `app:` selector, e.g. 'odoo'
    EXPECT_ENV_PREFIX = ''  # odoo/n8n/k8s/vk/opendesign 空；hermes 用 'HERMES_'、litellm 用 'LITELLM_MCP_'、nextcloud 用 'NEXTCLOUD_MCP_'
    # seed 覆寫的 config.nonSensitive.mcp_server.command。**None＝刻意不覆寫、
    # 吃 chart 預設**（只有 pilot hermes 是這個形態）。
    EXPECT_MCP_COMMAND = None
    # 上架與否；目前 9 個成員都是 True（is_hidden 是曝光層，與本欄正交：odoo-manage 已下架仍 True），欄位保留給未來待上架的成員。
    EXPECT_ACTIVE = True
    # 月繳／年繳定價。hermes 是 1500／15000（#330 定價重定），其餘齊一 3000／24000。
    EXPECT_PLAN_PRICE_M = 3000
    EXPECT_PLAN_PRICE_Y = 24000

    def _values(self):
        return json.loads(self.env.ref(self.TMPL).helm_default_values)

    @staticmethod
    def _seeded_mcp_command(values):
        """seed 有沒有覆寫 ``mcp_server.command``；沒有回 None（＝吃 chart 預設）。"""
        return (
            values.get('config', {}).get('nonSensitive', {})
            .get('mcp_server', {}).get('command')
        )

    def test_template_seeded(self):
        tmpl = self.env.ref(self.TMPL)
        self.assertEqual(tmpl.slug, self.SLUG)
        self.assertEqual(tmpl.category, 'mcp')
        self.assertEqual(tmpl.is_active, self.EXPECT_ACTIVE)
        # External port is unified 8080 across the whole family (design D7).
        self.assertEqual(tmpl.default_port, 8080)
        self.assertTrue(tmpl.ingress_enabled)

    def test_template_is_not_sidecar_backed(self):
        """MCP capability is built into the body, NOT a sidecar."""
        tmpl = self.env.ref(self.TMPL)
        self.assertFalse(tmpl.mcp_enabled)
        self.assertFalse(tmpl.mcp_sidecar_image)

    def test_template_no_post_deploy_init(self):
        """config seed handled by chart initContainer; no post-deploy action."""
        tmpl = self.env.ref(self.TMPL)
        self.assertEqual(tmpl.post_deploy_init_type, 'none')

    def test_template_uses_mcp_server_chart(self):
        tmpl = self.env.ref(self.TMPL)
        self.assertTrue(tmpl.helm_chart_name.startswith('oci://'))
        # endswith（而非 assertIn）：與 TestMcpServerFamilyCoverage 及 #449 載體
        # 守線同一個判準，family 成員資格才不會兩處各認各的。
        self.assertTrue(
            tmpl.helm_chart_name.endswith(MCP_SERVER_CHART_SUFFIX),
            '%s 的 chart 座標 %r 不以 %s 結尾——家族成員資格的判準就是它，'
            '不符合的話 TestMcpServerFamilyCoverage 也看不到這一筆' % (
                self.SLUG, tmpl.helm_chart_name, MCP_SERVER_CHART_SUFFIX))

    def test_template_helm_values_use_camelcase_envprefix(self):
        """camelCase guard (design 階段1 守線): the values key must be
        ``envPrefix``, not ``env_prefix`` — helm silently ignores unknown
        keys and falls back to the chart default, which mis-configures
        odoo/n8n (empty envPrefix) without any error."""
        values = self._values()
        self.assertIn('envPrefix', values)
        self.assertNotIn('env_prefix', values)
        # Assert the VALUE, not just the key: the most likely copy-paste error
        # is pasting hermes' envPrefix 'HERMES_' (or app 'hermes') and forgetting
        # to clear it — the image-repo check below cannot catch that drift.
        self.assertEqual(values['envPrefix'], self.EXPECT_ENV_PREFIX)
        self.assertEqual(values['app'], self.APP)
        self.assertIn(self.IMAGE_REPO_SUFFIX, values['image']['repository'])
        # External service port stays 8080 regardless of internal app port.
        self.assertEqual(values['service']['port'], 8080)

    def test_template_launch_command_matches_declared_contract(self):
        """seed 的啟動命令必須等於子類宣告的契約（``EXPECT_MCP_COMMAND``）。

        兩個方向都守：宣告了值卻沒 seed（或 seed 錯值）＝子行程 spawn 不起來；
        宣告 None（＝刻意吃 chart 的 hermes 形預設，只有 pilot 適用）卻在 seed
        裡偷偷覆寫，也會紅——要改吃自己的命令就得同時改宣告，是一次刻意的動作，
        不會是複製貼上滑過去的。

        衍生層 ``_McpServerSeedAssertions`` 另有更強的一條（非 hermes **必須**
        顯式覆寫且不得等於 chart 預設）；這裡是所有成員都成立的那一半。
        """
        command = self._seeded_mcp_command(self._values())
        self.assertEqual(
            command, self.EXPECT_MCP_COMMAND,
            '%s：seed 的 config.nonSensitive.mcp_server.command 是 %r，'
            '但測試類宣告的契約是 %r（None＝刻意吃 chart 預設 %r）' % (
                self.SLUG, command, self.EXPECT_MCP_COMMAND,
                CHART_DEFAULT_MCP_COMMAND))

    def test_template_requires_platform_admin_password(self):
        """#357 C01+C02 / #686：admin_password 供給契約的 seed 側守線。

        C01（has_admin_gui）：家族成員的 admin GUI 內建於 image，需平台供給
        admin_password。閘門必須是 has_admin_gui——不能用 mcp_enabled，後者是「外掛
        MCP sidecar」語意、對 body-內建的家族成員恆 False（見
        test_template_is_not_sidecar_backed），拿它當閘門 create() 會過濾成空集合、
        secret 從不建立、租戶拿到一組永不生效的密碼（#357 根因）。

        沒有這個欄位的後果不是「密碼比較弱」而是**沒有密碼**：平台從不鑄造，
        mcp-admin-core 落到內建 fallback ``admin_password = "admin"``，admin GUI
        又掛在服務公開根 URL 上（#686 在 stg 實測 ``POST /api/auth/login
        {"password":"admin"}`` → 200）。

        C02（requiredFields）：chart mcp-server 0.1.5 的 config.requiredFields 預設
        8 條，覆寫語意是 REPLACE 而非 merge（deployment.yaml 用 join "," 餵
        initContainer 的 SEED_REQUIRED_PATHS）。平台只供給 admin_password 一條，
        故必須覆寫成單條——connection.*（租戶自行在 GUI 填）與 mcp_auth_token
        （不由平台供給）若留在 requiredFields，initContainer 會因缺值讓**每個新
        租戶的 pod 永遠卡 Init**。
        """
        tmpl = self.env.ref(self.TMPL)
        self.assertTrue(
            tmpl.has_admin_gui,
            'admin GUI 內建 → 必須 has_admin_gui=True 才會觸發平台供給 '
            'admin_password；否則落到 mcp-admin-core 的字面 "admin"（#686）')
        config = self._values()['config']
        self.assertTrue(config['requireFields'])
        self.assertEqual(config['sensitive'], {})
        self.assertEqual(
            config['requiredFields'], ['admin_password'],
            'requiredFields 覆寫＝REPLACE：只列本波真正供給的路徑，'
            '多列一條就讓 pod 卡 Init')

    def test_template_storage_and_resources_match_advertised_spec(self):
        """#268: PVC size + container limits must equal the advertised spec
        (min_storage_gb / min_ram_gb / min_vcpu) — guards the 2Gi-vs-5G and
        512Mi-vs-1G drift this issue fixed."""
        _assert_storage_resources_aligned(self, self.env.ref(self.TMPL))

    def test_plans_seeded_at_expected_pricing(self):
        monthly = self.env.ref(self.PLAN_M)
        yearly = self.env.ref(self.PLAN_Y)
        self.assertEqual(monthly.sh_plan_price, self.EXPECT_PLAN_PRICE_M)
        self.assertEqual(monthly.sh_unit, 'month')
        self.assertEqual(yearly.sh_plan_price, self.EXPECT_PLAN_PRICE_Y)
        self.assertEqual(yearly.sh_unit, 'year')

    def test_products_link_plans(self):
        prod_m = self.env.ref(self.PROD_M)
        self.assertEqual(prod_m.type, 'service')
        self.assertTrue(prod_m.sh_product_subscribe)
        self.assertEqual(prod_m.sh_subscription_plan_id, self.env.ref(self.PLAN_M))

    def test_template_linked_to_plans(self):
        tmpl = self.env.ref(self.TMPL)
        linked = tmpl.subscription_plan_ids
        self.assertIn(self.env.ref(self.PLAN_M), linked)
        self.assertIn(self.env.ref(self.PLAN_Y), linked)

    def test_plan_bound_only_to_its_own_template(self):
        """Replication footgun guard: the mcp-* plan must not be cross-linked
        to a different cloud_app_template (e.g. the regular cloud_app_odoo /
        cloud_app_n8n cloud service), which would happen if a copy-paste
        reused the wrong plan xmlid. Covers BOTH monthly and yearly — the
        yearly plan is just as likely to be mis-bound and the model
        @api.constrains does not catch same-(cloud)-model cross-binding."""
        Template = self.env['woow_paas_platform.cloud_app_template']
        own = self.env.ref(self.TMPL)
        for plan_ref in (self.PLAN_M, self.PLAN_Y):
            plan = self.env.ref(plan_ref)
            others = Template.search(
                [('subscription_plan_ids', 'in', plan.ids),
                 ('id', '!=', own.id)])
            self.assertFalse(others, "%s cross-linked to %s" % (plan_ref, others))

    def test_plans_not_shared_with_other_service_types(self):
        """The cross-service-type constraint must not be tripped by the seed
        (both monthly and yearly)."""
        for plan_ref in (self.PLAN_M, self.PLAN_Y):
            plan = self.env.ref(plan_ref)
            sh = self.env['woow_paas_platform.smart_home_template'].search(
                [('subscription_plan_ids', 'in', plan.ids)])
            sa = self.env['woow_paas_platform.security_access_template'].search(
                [('subscription_plan_ids', 'in', plan.ids)])
            self.assertFalse(sh, "%s leaked to smart_home" % plan_ref)
            self.assertFalse(sa, "%s leaked to security_access" % plan_ref)


class _McpServerSeedAssertions(_McpServerFamilyAssertions):
    """家族不變式 **＋ 僅適用於「非 hermes」的那一條**。

    這一層放的是語意上就排除 pilot 的斷言：chart 的 config 預設是 hermes 形的，
    所以「必須覆寫掉 hermes 預設」對 hermes 本身不成立。hermes 走
    ``_McpServerFamilyAssertions``，不繼承本類。
    """

    def test_template_overrides_chart_default_mcp_command(self):
        """GAP-O1/N1 guard: the shared chart's DEFAULT config is hermes-shaped
        (config.nonSensitive.mcp_server.command='hermes-mcp-server'). 非 pilot 的
        成員 MUST override that command in helm_default_values, otherwise the chart
        seeds them with the hermes launch command, the wrong child process
        is spawned (or none), and the MCP endpoint silently 502s while /healthz
        and the GUI still look fine. Asserts the per-app command is set and is
        NOT the stale hermes default — the highest-value runtime footgun in the
        whole replication (caught at odoo layer-2, not by unit tests before)."""
        command = self._seeded_mcp_command(self._values())
        self.assertIsNotNone(
            command,
            '%s 必須在 seed 顯式覆寫 mcp_server.command；留空＝沿用 chart 的 '
            'hermes 形預設，子行程 spawn 不起來而 /healthz 與 GUI 仍正常' % self.SLUG)
        self.assertEqual(command, self.EXPECT_MCP_COMMAND)
        self.assertNotEqual(command, CHART_DEFAULT_MCP_COMMAND)


class TestMcpServerTemplateSeed(_McpServerFamilyAssertions, TransactionCase):
    """mcp-hermes（pilot）：家族不變式全繼承。

    #686 之前這個類別是手寫、直接繼承 TransactionCase，比 mixin 更早存在，於是
    ``has_admin_gui`` / ``config.requireFields`` 兩條家族不變式從來沒守到它——
    seed 漏的那一格與守線漏的那一格是同一格。

    ``EXPECT_MCP_COMMAND = None``：hermes 刻意**不**覆寫 chart 的
    ``config.nonSensitive``，靠 chart 預設拿到 ``hermes-mcp-server`` ＋ args ＋
    內部 port 8000。#686 補 ``config.requireFields`` / ``requiredFields`` 時已用
    ``helm template``（chart 0.1.5、helm 3.13.3）實測：補上 ``config`` 區塊後
    render 出來的 ``config.base.json`` 與補之前**逐位元組相同**（helm 對 map 是
    深合併，不是整個 config 物件取代）。
    """

    SLUG = 'mcp-hermes'
    TMPL = 'woow_paas_platform.cloud_app_mcp_hermes'
    PLAN_M = 'woow_paas_platform.plan_mcp_hermes_monthly'
    PLAN_Y = 'woow_paas_platform.plan_mcp_hermes_yearly'
    PROD_M = 'woow_paas_platform.product_mcp_hermes_monthly'
    IMAGE_REPO_SUFFIX = 'hermes-mcp-admin'
    APP = 'hermes'
    EXPECT_ENV_PREFIX = 'HERMES_'
    EXPECT_MCP_COMMAND = None  # 刻意吃 chart 預設，見類別 docstring
    # #330 定價重定：Hermes T1＝1500／15000（沿用 xmlid、原地改 T1）
    EXPECT_PLAN_PRICE_M = 1500
    EXPECT_PLAN_PRICE_Y = 15000


class TestMcpOdooTemplateSeed(_McpServerSeedAssertions, TransactionCase):
    """mcp-odoo template: tenant-neutral, sidecar-free, odoo-level pricing."""

    SLUG = 'mcp-odoo'
    TMPL = 'woow_paas_platform.cloud_app_mcp_odoo'
    PLAN_M = 'woow_paas_platform.plan_mcp_odoo_monthly'
    PLAN_Y = 'woow_paas_platform.plan_mcp_odoo_yearly'
    PROD_M = 'woow_paas_platform.product_mcp_odoo_monthly'
    IMAGE_REPO_SUFFIX = 'odoo-mcp-admin'
    APP = 'odoo'
    # #70 的 LaunchPolicy 改用 woow-odoo-mcp；launcher 安裝讀取錯誤、大小限制、
    # isError 與唯讀並行修補後，仍呼叫 odoo-mcp 1.1.0 的原生 streamable-http。
    # 這不是 #708 已淘汰的 mcp_sse_wrapper；command 必須與新 image 的 policy 同步。
    EXPECT_MCP_COMMAND = 'woow-odoo-mcp'
    # 與上面那條 launch spec 綁成 lockstep 的常數，見
    # test_image_repin_must_recheck_launch_spec。
    # 2026-10-03 重釘 e310dfe0（#20–#27）：已覆核 apps/odoo/Dockerfile 與 uv.lock 的
    # woow-odoo-mcp-server／odoo-mcp 依賴自 7b5944c4 起未變，console script ``odoo-mcp``
    # 與 streamable-http args 照舊成立。
    # 2026-10-06 重釘 d5032a72（woow-mcp-server #34，#1500 看門狗）：已覆核 e310dfe0..d5032a72 之間
    # apps/odoo 與 uv.lock 的 odoo-mcp（1.1.0）／mcp（1.28.1）皆未變，只有 mcp-admin-core process.py
    # 改動，console script ``odoo-mcp`` 與 streamable-http args 照舊成立。
    # 2026-10-07 重釘 cfc2a2b（woow-mcp-server #37，#1500 跟進：狀態 API 補看門狗欄位）：已覆核
    # d5032a72..cfc2a2b 只動 mcp-admin-core routers/settings.py 與其測試，apps/odoo、uv.lock 未變，
    # console script ``odoo-mcp`` 與 streamable-http args 照舊成立。
    # 2026-10-08 重釘 ff32a7c1（woow-mcp-server #45，#1530：子行程 command／args 改由 app 的 LaunchPolicy
    # 固定、env 白名單、非 root）：已覆核 apps/odoo/odoo_mcp_admin/main.py 的 LaunchPolicy＝``odoo-mcp``＋
    # ``--transport streamable-http --host 127.0.0.1 --port {port}``，與 seed 的 command／args 逐字一致；
    # cfc2a2b..ff32a7c1 之間 uv.lock 的 odoo-mcp 依賴未變（只多了 nextcloud 用的 psycopg）。
    # 2026-10-10 重釘 1ec1bb0b（woow-mcp-server #48，#1614：共用層 log 遮蔽 URL 憑證；main CI run 17227
    # job 18849 的 push digest）：已覆核 ff32a7c1..1ec1bb0b 之間 apps/odoo 沒有任何變動（Dockerfile、
    # pyproject、odoo_mcp_admin/main.py 都同），LaunchPolicy 仍是 ``odoo-mcp``＋``--transport streamable-http
    # --host 127.0.0.1 --port {port}``；uv.lock 只差 nextcloud 的套件，odoo-mcp（1.1.0）／mcp（1.28.1）未變。
    # 前一個 pin：ff32a7c11f57f32838417fb3aa3a4d4593210818@sha256:503d3121cd94cd74087f0ad839521c36207696433e3bd6d3cd47abdbe4cd4181。
    # 2026-10-12 重釘 1d16391d（#49／#50，main CI18319 job20067）：
    # 實查發布image之console entrypoint odoo_mcp.__main__:main、odoo-mcp1.1.0／mcp1.28.1、
    # CLI原生streamable-http與LaunchPolicy完整args；1ec1bb0b..1d16391d的apps/odoo、uv.lock未變。
    # 實際imageID／CLI／source SHA證據見docs/verification/2026-10-12-mcp-published-pins.json。
    # 2026-10-13 補覆核 0b2cff1f（woow-mcp-server #51，Odoo 寫入閘門；main CI run 18449 job 20226 的 push
    # digest）：seed 已由 #1672 於 2026-10-12 重釘，當時沒有同步本常數，#1687 補上。原始碼：1d16391d..0b2cff1f
    # 只動 apps/odoo 的 main.py、seed_defaults.py（新檔）、frontend appConfig.jsx 與一支 core 測試；Dockerfile、
    # pyproject、uv.lock、mcp_admin_core 原始碼皆未變。LaunchPolicy 的差異只在 env_keys／editable_env_keys／
    # derived_env（放行 ODOO_MCP_ENABLE_WRITES），command 與 args 兩行沒動。發布 image：離線 probe pod 實際
    # 拉到的 imageID 即 seed 釘的 digest；console script ``odoo-mcp``＝odoo_mcp.__main__:main、odoo-mcp 1.1.0／
    # mcp 1.28.1、``--help`` 列 transport {sse,stdio,streamable-http}；core 的 resolve_launch 得 ``odoo-mcp``＋
    # ``--transport streamable-http --host 127.0.0.1 --port 8000``，照該 argv 實際起子行程後 127.0.0.1:8000 的
    # ``/mcp`` initialize 回 200＋mcp-session-id。證據見
    # docs/verification/2026-10-13-mcp-odoo-0b2cff1f-launch-contract.json。
    # 前一個 pin：1d16391d1a62fdda2b8de6bd16491c22330b127f@sha256:39b1e6e933b93f51d1c8a09e23b3764d45c898314fa47c33b6876b4573cba0e6。
    # 下一輪 e178c4ce（#52）：相對0b2，Dockerfile／uv.lock／LaunchPolicy／patches均未變。
    # 真imageID45b88bf4之 CLI args、MCP initialize／預設tools/list41／離線tools/call 已驗；
    # QA240 direct-write 模式的MCP38／管理目錄41差三支明示隱藏的審批工具，亦已另驗。
    # 此常數與seed一起改釘；229／230既有tag和正式部署不受這個候選分支影響。
    # 2026-10-18 重釘 #70（5526e865）：逐字覆核 LaunchPolicy、pyproject console script
    # 與 launcher；args 不變，command 同步改為 woow-odoo-mcp。odoo-mcp 仍 1.1.0，
    # mcp SDK 已升 1.30.0；五支真 image 的 imports／CLI help／ASGI 限定 smoke 共 111 項通過。
    # 未啟動 lifespan／MCP daemon、未呼叫 MCP 工具；既有 PVC 升級與 ORM 仍未驗。
    # 來源／實機窄證據：docs/verification/2026-10-18-mcp-5526e865-source-contract.json
    # 與同目錄的 2026-10-18-mcp-5526e865-limited-image-smoke.json。
    # 2026-10-23 重釘 #72（32a5263c）：相對 #70，兩 app 的 Dockerfile、pyproject、
    # main.py、launcher.py 與 uv.lock 均未變；主分支 CI26543 的 core、八支 image
    # build／healthz／push 全通過，registry HEAD 獨立確認 digest。正式 PVC 升級另驗。
    # 2026-10-24：#71 merge 5fa1ff0a 包含 #72；八支 image registry digest 已逐一驗證。
    # LaunchPolicy 維持 woow-odoo-mcp／streamable-http／127.0.0.1／port8000。
    EXPECT_IMAGE_TAG = '6b91398b8377a119fe9ef3f99bad49fd6443ad42@sha256:165b3f73399a2113626d6983c32960dfee3406cadda36884cf0dffeaf3e07572'

    def test_mcp_server_launch_spec_matches_image_entrypoint(self):
        """#708：只釘 command 不夠，args 整組也要釘。

        實際炸掉的是 args——重釘後 ``command`` 仍是完全合法的 ``python``，家族層
        「有覆寫 chart 預設」那條也照樣綠；死在 args 裡的
        ``-m mcp_admin_core.mcp_sse_wrapper mcp_server_odoo.server:server``
        指向新 image 裡不存在的模組，子行程 ModuleNotFoundError 立刻死，proxy 對
        ``/private_TOKEN/mcp`` 回 502，而 ``/healthz`` 與 admin GUI 全綠。
        """
        mcp = self._values()['config']['nonSensitive']['mcp_server']
        self.assertNotIn(
            'mcp_admin_core.mcp_sse_wrapper', mcp['args'],
            '現行 image 原生講 streamable-http，不得回退成 wrapper 包——'
            '它沒有 mcp_server_odoo 模組，wrapper 會 ModuleNotFoundError')
        self.assertEqual(
            mcp['args'],
            ['--transport', 'streamable-http', '--host', '127.0.0.1',
             '--port', '8000'])
        self.assertEqual(mcp['port'], 8000)
        # proxy 連的是 mcp_server.port，子行程綁的是 args 裡的 port：兩者一旦
        # 不一致，症狀與「子行程沒起來」一模一樣（502），但原因完全不同。
        self.assertEqual(
            mcp['args'][mcp['args'].index('--port') + 1], str(mcp['port']),
            'args 的 port 必須等於 proxy 讀的 mcp_server.port')

    def test_image_repin_must_recheck_launch_spec(self):
        """#708 lockstep：image 一被重釘這條就紅，逼下一個人覆核 launch spec。

        這正是 2026-07-14 重釘（commit 6fb93163）漏掉的那一步——那次 diff 只動
        了版號與 image.tag 兩行，同一個 JSON 裡的 command/args 留在舊 image 的
        寫法，測試全綠，此後每一台新開的實例 /mcp 都是 502。

        本測**不**（也不可能）驗 image 裡裝了什麼；它只保證「換 image」與「覆核
        啟動指令」不能再各自獨立發生。只掛在 odoo 這一格是刻意的：#708 的範疇是
        這一張 template，家族其餘成員要不要一起上 lockstep 另議。
        """
        self.assertEqual(
            self._values()['image']['tag'], self.EXPECT_IMAGE_TAG,
            'image 被重釘了：請先實查新 image 的 MCP 入口（有沒有 console '
            'script？原生支援哪些 transport？模組名是什麼？），據以確認或修正本 '
            'class 的 EXPECT_MCP_COMMAND 與 launch args，最後才把 '
            'EXPECT_IMAGE_TAG 更新成新 tag。判別規則見 '
            'docs/reference/features/mcp-template-launch-contract.md 與 #708。')


class TestMcpN8nTemplateSeed(_McpServerSeedAssertions, TransactionCase):
    """mcp-n8n template: tenant-neutral, sidecar-free, odoo-level pricing.

    Active (launcher sha 2ef1dc16 + chart 0.1.4, whose secret.yaml
    self-generates the mandatory n8n AUTH_TOKEN — no operator per-instance
    Secret needed); validated layer-3 on stg. EXPECT_ACTIVE defaults to True.
    """

    SLUG = 'mcp-n8n'
    TMPL = 'woow_paas_platform.cloud_app_mcp_n8n'
    PLAN_M = 'woow_paas_platform.plan_mcp_n8n_monthly'
    PLAN_Y = 'woow_paas_platform.plan_mcp_n8n_yearly'
    PROD_M = 'woow_paas_platform.product_mcp_n8n_monthly'
    IMAGE_REPO_SUFFIX = 'n8n-mcp-admin'
    APP = 'n8n'
    EXPECT_MCP_COMMAND = 'n8n-mcp-http'


class TestMcpOdooManageTemplateSeed(_McpServerSeedAssertions, TransactionCase):
    """mcp-odoo-manage template（#338 範疇①）: tenant-neutral, sidecar-free, odoo-level pricing.

    Active（image 真接線: odoo-manage-mcp-admin 雙釘 digest, woow-mcp-server #14 打包
    ivnvxd/mcp-server-odoo 0.7.1）. 底層 server 用 FastMCP、原生 StreamableHTTP，故直跑
    console script `mcp-server-odoo`——ivnvxd 無 module-level server 物件，wrapper spec
    對它不存在. EXPECT_ACTIVE defaults to True.

    這裡原本補了一句「NOT 標準 odoo 的 mcp_sse_wrapper 包」，那是**首建 image 時期**的
    對比（標準版底層 odoo-mcp-server 0.1.3 stdio-only、家族只有它走 wrapper）。標準版
    自 #708 起改成直跑 console script（見 TestMcpOdooTemplateSeed），兩張已是同一種
    形狀，該對比不再成立。
    """

    SLUG = 'mcp-odoo-manage'
    TMPL = 'woow_paas_platform.cloud_app_mcp_odoo_manage'
    PLAN_M = 'woow_paas_platform.plan_mcp_odoo_manage_monthly'
    PLAN_Y = 'woow_paas_platform.plan_mcp_odoo_manage_yearly'
    PROD_M = 'woow_paas_platform.product_mcp_odoo_manage_monthly'
    IMAGE_REPO_SUFFIX = 'odoo-manage-mcp-admin'
    APP = 'odoo-manage'
    EXPECT_MCP_COMMAND = 'mcp-server-odoo'


class TestMcpK8sTemplateSeed(_McpServerSeedAssertions, TransactionCase):
    """mcp-k8s template（#375）：kubernetes-mcp-server，Go binary 原生 http。

    治理三 toggle／tools.disabled 的真傳導語意由 test_mcp_3type_seed_375.py 守；
    這裡守的是家族不變式那一層（#686 之前 k8s 沒有繼承任何一條）。
    """

    SLUG = 'mcp-k8s'
    TMPL = 'woow_paas_platform.cloud_app_mcp_k8s'
    PLAN_M = 'woow_paas_platform.plan_mcp_k8s_monthly'
    PLAN_Y = 'woow_paas_platform.plan_mcp_k8s_yearly'
    PROD_M = 'woow_paas_platform.product_mcp_k8s_monthly'
    IMAGE_REPO_SUFFIX = 'k8s-mcp-admin'
    APP = 'k8s'
    EXPECT_MCP_COMMAND = 'k8s-mcp-http'


class TestMcpVkTemplateSeed(_McpServerSeedAssertions, TransactionCase):
    """mcp-vk template（#375）：vibe-kanban-mcp，經 supergateway 橋 stdio。"""

    SLUG = 'mcp-vk'
    TMPL = 'woow_paas_platform.cloud_app_mcp_vk'
    PLAN_M = 'woow_paas_platform.plan_mcp_vk_monthly'
    PLAN_Y = 'woow_paas_platform.plan_mcp_vk_yearly'
    PROD_M = 'woow_paas_platform.product_mcp_vk_monthly'
    IMAGE_REPO_SUFFIX = 'vk-mcp-admin'
    APP = 'vk'
    EXPECT_MCP_COMMAND = 'supergateway'


class TestMcpOpenDesignTemplateSeed(_McpServerSeedAssertions, TransactionCase):
    """mcp-opendesign template（#375）：vendored od_mcp_server，supergateway 橋。

    ⚠ 與 ``cloud_app_opendesign``（Open Design 完整應用，chart open-design、
    basic-auth 由 nginx auth-proxy 閘）是兩筆不同的 template，勿互抄。
    """

    SLUG = 'mcp-opendesign'
    TMPL = 'woow_paas_platform.cloud_app_mcp_opendesign'
    PLAN_M = 'woow_paas_platform.plan_mcp_opendesign_monthly'
    PLAN_Y = 'woow_paas_platform.plan_mcp_opendesign_yearly'
    PROD_M = 'woow_paas_platform.product_mcp_opendesign_monthly'
    IMAGE_REPO_SUFFIX = 'opendesign-mcp-admin'
    APP = 'opendesign'
    EXPECT_MCP_COMMAND = 'supergateway'


class TestMcpLitellmTemplateSeed(_McpServerSeedAssertions, TransactionCase):
    """mcp-litellm template（PLAN §10）：LiteLLM 管理 MCP，launcher 直跑 Streamable HTTP。

    唯一 envPrefix 非空的非 pilot 成員（``LITELLM_MCP_``：connection 鍵 base_url／master_key 經前綴
    注入子行程）。出廠唯讀、同工作區網址注入、上架即雙釘等本 app 專屬守線在 test_mcp_litellm_seed.py；
    這裡只守家族不變式。``is_hidden`` 不影響 ``is_active``（曝光層與上架旗標正交；2026-10-07 公開上架）。
    """

    SLUG = 'mcp-litellm'
    TMPL = 'woow_paas_platform.cloud_app_mcp_litellm'
    PLAN_M = 'woow_paas_platform.plan_mcp_litellm_monthly'
    PLAN_Y = 'woow_paas_platform.plan_mcp_litellm_yearly'
    PROD_M = 'woow_paas_platform.product_mcp_litellm_monthly'
    IMAGE_REPO_SUFFIX = 'litellm-mcp-admin'
    APP = 'litellm'
    EXPECT_ENV_PREFIX = 'LITELLM_MCP_'
    EXPECT_MCP_COMMAND = 'litellm-mcp-server'


class TestMcpNextcloudTemplateSeed(_McpServerSeedAssertions, TransactionCase):
    """mcp-nextcloud template：以一個 Nextcloud 帳號（app password）工作的 MCP，launcher 直跑 Streamable HTTP。

    envPrefix 非空（``NEXTCLOUD_MCP_``：connection 鍵 base_url／username／app_password 經前綴注入子行程）。
    唯一釘 chart 0.1.7（#1530 非 root）的成員——image 以 uid／gid 1000 執行。出廠唯讀（由 app 自己做）、
    同工作區 Nextcloud 公開網址注入、隱藏到沙盒實測後等本 app 專屬守線在 test_mcp_nextcloud_seed.py；
    這裡只守家族不變式。``is_hidden=True`` 不影響 ``is_active``（曝光層與上架旗標正交）。
    """

    SLUG = 'mcp-nextcloud'
    TMPL = 'woow_paas_platform.cloud_app_mcp_nextcloud'
    PLAN_M = 'woow_paas_platform.plan_mcp_nextcloud_monthly'
    PLAN_Y = 'woow_paas_platform.plan_mcp_nextcloud_yearly'
    PROD_M = 'woow_paas_platform.product_mcp_nextcloud_monthly'
    IMAGE_REPO_SUFFIX = 'nextcloud-mcp-admin'
    APP = 'nextcloud'
    EXPECT_ENV_PREFIX = 'NEXTCLOUD_MCP_'
    EXPECT_MCP_COMMAND = 'nextcloud-mcp-server'


def _all_subclasses(cls):
    """遞迴收集子類（兩層 mixin ⇒ 具體類不一定是直接子類）。"""
    found = set()
    for sub in cls.__subclasses__():
        found.add(sub)
        found |= _all_subclasses(sub)
    return found


class TestMcpServerFamilyCoverage(TransactionCase):
    """覆蓋率本身是斷言：每一筆 mcp-server chart 的 seed 都要有測試類綁著。

    #686 的教訓不是「漏了 hermes」，是「漏格不會有人發現」——hermes 的
    ``has_admin_gui`` 從未被斷言，而測試輸出與全數覆蓋時一模一樣。原 issue 甚至
    把覆蓋率判成 3/7（誤：k8s／vk／opendesign 的 ``has_admin_gui`` 其實由
    test_mcp_3type_seed_375.py 守著，只是**家族其餘不變式**沒守）——連盤點都會出錯，
    正說明這件事不能靠人工清點。

    判準取自 seed 的結構（chart 座標），不是硬編清單：新上架一筆 mcp-server chart
    的 template 卻沒寫測試子類就會紅，而單純改動既有 template 不會。
    """

    def _family_templates(self):
        """seed 裡所有走 mcp-server chart 的 template；**保證非空**。

        非空是這條全稱斷言的取樣層陽性對照：chart 座標改名／seed 未載入時，
        search 會回空集合、迴圈整個跳過、測試照樣綠——而假綠的輸出與真綠一模一樣
        （對齊 test_admin_password_carrier_449.py 的同型守法）。
        """
        templates = self.env['woow_paas_platform.cloud_app_template'].with_context(
            active_test=False,
        ).search([])
        family = templates.filtered(
            lambda t: (t.helm_chart_name or '').endswith(MCP_SERVER_CHART_SUFFIX))
        self.assertTrue(
            family,
            '走 mcp-server chart 的 template 一筆都沒有。這不代表「全部都有測試」，'
            '而是探針讀不到東西（chart 座標改名／seed 未載入）——此時本條覆蓋率'
            '守線等於不存在。')
        return family

    def test_every_mcp_server_chart_template_has_a_seed_test_class(self):
        covered = {
            cls.TMPL for cls in _all_subclasses(_McpServerFamilyAssertions)
            if getattr(cls, 'TMPL', None)
        }
        missing = []
        for template in self._family_templates():
            xmlid = template.get_external_id().get(template.id)
            if xmlid not in covered:
                missing.append('%s（%s）' % (xmlid or '(無 xmlid)', template.slug))
        # 輸出「是哪一個」而不是「差幾個」：差幾個沒辦法直接動手。
        self.assertFalse(
            missing,
            '下列 mcp-server chart template 沒有任何 _McpServerFamilyAssertions '
            '子類綁著它，家族不變式（has_admin_gui／requiredFields／envPrefix…）'
            '對它等於不存在：%s。請在 test_mcp_server_template_seed.py 補一個具體'
            '測試類（非 pilot 走 _McpServerSeedAssertions，pilot 走 '
            '_McpServerFamilyAssertions）。' % '、'.join(missing))

    def test_every_seed_test_class_points_at_a_real_family_template(self):
        """反向：測試類宣告的 TMPL 必須真的是一筆 mcp-server chart 的 seed。

        沒有這一條的話，把上一條的紅燈「修掉」的最省事作法是隨便掛一個 TMPL
        字串——覆蓋率數字對了、斷言卻跑在別的（甚至不存在的）record 上。
        """
        family_xmlids = {
            t.get_external_id().get(t.id) for t in self._family_templates()
        }
        for cls in _all_subclasses(_McpServerFamilyAssertions):
            tmpl = getattr(cls, 'TMPL', None)
            if not tmpl:
                continue
            self.assertIn(
                tmpl, family_xmlids,
                '%s.TMPL=%r 不是走 mcp-server chart 的 seed template' % (
                    cls.__name__, tmpl))

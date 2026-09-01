"""TDD red phase tests — cloud_service ir.rule workspace isolation (AC #7-8).

Plan: docs/refactor/plan-cloud-service-project-decouple.md (D8=A)

Current state (red expectation):
- ``src/security/ir.model.access.csv:7`` grants base.group_user RWCU on
  ``woow_paas_platform.cloud_service``.
- ``src/security/ir_rules.xml`` has *no* record rule for cloud_service.
- AC #7 violates: any internal user can read every workspace's services
  via ORM (controller filter is the only barrier; ORM is wide-open).
- AC #8 must remain green after D8=A is applied — the controller endpoint
  is the regression line.
"""
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase
from odoo.exceptions import AccessError
from ..models.cloud_service import _internal_provision


class TestCloudServiceWorkspaceRule(TransactionCase):
    """AC #7 — cross-workspace user must not read/write/unlink other ws services."""

    def setUp(self):
        super().setUp()
        self.Service = self.env['woow_paas_platform.cloud_service']
        self.Template = self.env['woow_paas_platform.cloud_app_template']
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.Access = self.env['woow_paas_platform.workspace_access']
        User = self.env['res.users']

        group_user = self.env.ref('base.group_user')

        self.alice = User.create({
            'login': 'cs_acl_alice',
            'name': 'Alice CS-ACL',
            'email': 'alice-cs-acl@test.example',
            'groups_id': [(6, 0, [group_user.id])],
        })
        self.bob = User.create({
            'login': 'cs_acl_bob',
            'name': 'Bob CS-ACL',
            'email': 'bob-cs-acl@test.example',
            'groups_id': [(6, 0, [group_user.id])],
        })

        # Workspace W1 owned by Alice; W2 owned by Bob.
        self.w1 = self.Workspace.create({
            'name': 'W1 CS-ACL', 'owner_id': self.alice.id,
        })
        self.w2 = self.Workspace.create({
            'name': 'W2 CS-ACL', 'owner_id': self.bob.id,
        })

        self.template = self.Template.sudo().create({
            'name': 'Tmpl CS-ACL',
            'slug': 'tmpl-cs-acl',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'tmpl-cs-acl',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })

        self.svc_w1 = _internal_provision(self.Service.sudo()).create({
            'name': 'Svc in W1',
            'workspace_id': self.w1.id,
            'template_id': self.template.id,
        })
        self.svc_w2 = _internal_provision(self.Service.sudo()).create({
            'name': 'Svc in W2',
            'workspace_id': self.w2.id,
            'template_id': self.template.id,
        })

    # ---------------- AC #7 read ---------------- #
    def test_ac7_bob_cannot_read_w1_service_via_orm(self):
        """AC #7 read: search must not return cross-workspace services."""
        visible = self.Service.with_user(self.bob).search([
            ('id', '=', self.svc_w1.id),
        ])
        self.assertFalse(
            visible,
            "AC #7 violated: Bob (W2 only) saw a W1 cloud_service. "
            "Need ir.rule `cloud_service_rule_workspace_member`.",
        )

    def test_ac7_bob_browse_w1_read_raises_access_error(self):
        """AC #7 read: explicit browse().read() must raise AccessError."""
        svc_as_bob = self.Service.with_user(self.bob).browse(self.svc_w1.id)
        with self.assertRaises(AccessError):
            svc_as_bob.read(['name'])

    # ---------------- AC #7 write ---------------- #
    def test_ac7_bob_cannot_write_w1_service_via_orm(self):
        """AC #7 write: write on cross-workspace service must raise."""
        svc_as_bob = self.Service.with_user(self.bob).browse(self.svc_w1.id)
        with self.assertRaises(AccessError):
            svc_as_bob.write({'name': 'Bob hijacked W1'})

    # ---------------- AC #7 unlink ---------------- #
    def test_ac7_bob_cannot_unlink_w1_service_via_orm(self):
        """AC #7 unlink: unlink on cross-workspace service must raise."""
        svc_as_bob = self.Service.with_user(self.bob).browse(self.svc_w1.id)
        with self.assertRaises(AccessError):
            svc_as_bob.unlink()

    # ---------------- AC #7 own-workspace positive ---------------- #
    def test_ac7_bob_can_read_own_w2_service(self):
        """AC #7 positive: Bob must still read W2 services (sanity)."""
        visible = self.Service.with_user(self.bob).search([
            ('id', '=', self.svc_w2.id),
        ])
        self.assertEqual(
            visible, self.svc_w2,
            "Sanity: workspace member must keep read access to own ws",
        )


class TestCloudServiceControllerRegression(TransactionCase):
    """AC #8 — /api/workspaces/<id>/services list/get unchanged for members.

    Calls the controller method directly (not via HTTP) so the test is
    deterministic and exercises the same path as
    ``test_cloud_api.TestCloudAPI``.  After D8=A adds an ir.rule, the
    controller currently uses ``check_user_access`` + ORM read; the rule
    must let members keep reading their own ws services.
    """

    def setUp(self):
        from odoo.addons.woow_paas_platform.controllers.paas import PaasController
        super().setUp()
        self.controller = PaasController()

        self.Service = self.env['woow_paas_platform.cloud_service']
        self.Template = self.env['woow_paas_platform.cloud_app_template']
        self.Workspace = self.env['woow_paas_platform.workspace']
        User = self.env['res.users']

        group_user = self.env.ref('base.group_user')

        self.member = User.create({
            'login': 'cs_ctrl_member',
            'name': 'Ctrl Member',
            'email': 'cs-ctrl-member@test.example',
            'groups_id': [(6, 0, [group_user.id])],
        })
        self.outsider = User.create({
            'login': 'cs_ctrl_outsider',
            'name': 'Ctrl Outsider',
            'email': 'cs-ctrl-outsider@test.example',
            'groups_id': [(6, 0, [group_user.id])],
        })

        self.ws = self.Workspace.create({
            'name': 'WS CS-Ctrl Regression',
            'owner_id': self.member.id,
        })

        self.template = self.Template.sudo().create({
            'name': 'Tmpl CS-Ctrl',
            'slug': 'tmpl-cs-ctrl',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'tmpl-cs-ctrl',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.svc = _internal_provision(self.Service.sudo()).create({
            'name': 'Svc CS-Ctrl',
            'workspace_id': self.ws.id,
            'template_id': self.template.id,
        })

    def _patch_paas_request(self, env):
        return patch(
            'odoo.addons.woow_paas_platform.controllers.paas.request',
            new=MagicMock(env=env),
        )

    def test_ac8_list_services_endpoint_unchanged_for_member(self):
        """AC #8: controller list still returns own ws services for member."""
        with self._patch_paas_request(self.env(user=self.member)):
            result = self.controller.api_workspace_services(
                workspace_id=self.ws.id, action='list',
            )
        self.assertTrue(result.get('success'),
            f"List endpoint regression: {result.get('error')}")
        service_ids = [s['id'] for s in result.get('data', [])]
        self.assertIn(
            self.svc.id, service_ids,
            "AC #8 regression: workspace member must keep seeing own ws "
            "services via the controller after D8=A",
        )

    def test_ac8_list_services_denied_for_outsider(self):
        """AC #8 regression: outsider still denied by controller."""
        with self._patch_paas_request(self.env(user=self.outsider)):
            result = self.controller.api_workspace_services(
                workspace_id=self.ws.id, action='list',
            )
        self.assertFalse(
            result.get('success'),
            "Regression: outsider must still be rejected by controller "
            "(workspace.check_user_access barrier predates D8=A)",
        )


class TestCloudServiceAdminBypass(TransactionCase):
    """#994 — `base.group_system` 必須看得到／管得動所有 workspace 的 service。

    RED 前提（補 `cloud_service_rule_admin_full` 之前）：
    `cloud_service` 上只有兩條 ir.rule，都綁 `base.group_user`、都要求呼叫者是
    `workspace_id.access_ids` 的成員。`base.group_system` 透過 implied_ids 內含
    `base.group_user`（見 `src/security/ir_rules.xml` 對 McpServer 那段註解），
    所以管理員同樣受這兩條約束；本 model 又沒有 global rule，而 Odoo 把同一使用者
    適用的 group rule 全部 OR 起來（`ir_rule.py` `_compute_domain`）⇒ 少了永真腿
    就沒有任何一腿為真 ⇒ 後台 Services 清單（`action_cloud_service`）恆空，
    維運無法處置卡在 `error` / `in_progress` 的租戶服務。

    ⚠ 本 class 的最後一支是**反向對照**，它在補規則前後都應該綠。它的價值在於
    「永真腿寫太寬」時會紅——例如把 `groups` 誤綁成 `base.group_user`。改動本規則
    時若只看前三支轉綠就收工，那個誤綁不會被任何斷言抓到。
    """

    def setUp(self):
        super().setUp()
        self.Service = self.env['woow_paas_platform.cloud_service']
        self.Template = self.env['woow_paas_platform.cloud_app_template']
        self.Workspace = self.env['woow_paas_platform.workspace']
        User = self.env['res.users']

        group_user = self.env.ref('base.group_user')
        group_system = self.env.ref('base.group_system')

        # workspace 擁有者——admin 與 outsider 都刻意不是這個 workspace 的成員。
        self.owner = User.create({
            'login': 'cs_bypass_owner',
            'name': 'Owner CS-Bypass',
            'email': 'owner-cs-bypass@test.example',
            'groups_id': [(6, 0, [group_user.id])],
        })
        # 兩個群組必須在 create 當下一起給：事後 write 會留下 stale env cache，
        # `with_user(...)` 的 ACL 檢查讀不到新群組（見 test_mcp_acl_isolation
        # 的 `_create_admin_user` 註解）。
        self.admin = User.create({
            'login': 'cs_bypass_admin',
            'name': 'Admin CS-Bypass',
            'email': 'admin-cs-bypass@test.example',
            'groups_id': [(6, 0, [group_user.id, group_system.id])],
        })
        # 反向對照用：只有 base.group_user、同樣不是成員。
        self.outsider = User.create({
            'login': 'cs_bypass_outsider',
            'name': 'Outsider CS-Bypass',
            'email': 'outsider-cs-bypass@test.example',
            'groups_id': [(6, 0, [group_user.id])],
        })

        self.ws = self.Workspace.create({
            'name': 'WS CS-Bypass',
            'owner_id': self.owner.id,
        })

        self.template = self.Template.sudo().create({
            'name': 'Tmpl CS-Bypass',
            'slug': 'tmpl-cs-bypass',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'tmpl-cs-bypass',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })

        self.svc = _internal_provision(self.Service.sudo()).create({
            'name': 'Svc owned by someone else',
            'workspace_id': self.ws.id,
            'template_id': self.template.id,
        })

    def test_admin_reads_service_of_workspace_he_is_not_member_of(self):
        """#994 read：後台 Services 清單的那一步——非成員 admin 必須搜得到。"""
        visible = self.Service.with_user(self.admin).search([
            ('id', '=', self.svc.id),
        ])
        self.assertEqual(
            visible, self.svc,
            "#994: base.group_system 搜不到非自己 workspace 的 cloud_service ⇒ "
            "後台 action_cloud_service 清單恆空。需要 "
            "`cloud_service_rule_admin_full` 永真腿。",
        )

    def test_admin_writes_service_of_workspace_he_is_not_member_of(self):
        """#994 write：處置異常服務要改得動欄位（例如清 error_message）。"""
        svc_as_admin = self.Service.with_user(self.admin).browse(self.svc.id)
        svc_as_admin.write({'error_message': 'cleared by ops'})
        self.assertEqual(
            self.svc.error_message, 'cleared by ops',
            "#994: base.group_system 改不動非自己 workspace 的 cloud_service",
        )

    def test_admin_unlinks_service_of_workspace_he_is_not_member_of(self):
        """#994 unlink：孤兒 / 異常 service 要刪得掉。"""
        svc_id = self.svc.id
        self.Service.with_user(self.admin).browse(svc_id).unlink()
        self.assertFalse(
            self.Service.sudo().browse(svc_id).exists(),
            "#994: base.group_system 刪不掉非自己 workspace 的 cloud_service",
        )

    def test_admin_reads_workspace_id_of_foreign_service(self):
        """#994 待驗前提：list/form view 會渲染 `workspace_id` 這個 many2one。

        `workspace` model 自己**沒有**永真腿（照 ir_rules.xml 的判準：它沒有
        backend view/action）。若只補 `cloud_service` 一邊就讓這裡炸掉，本單射程
        必須擴到 `workspace`——屆時理由不是「它有自己的 view」而是「被
        cloud_service 的 view 間接需要」，註解要寫清楚以免下一個讀者以為判準變了。
        """
        svc_as_admin = self.Service.with_user(self.admin).browse(self.svc.id)
        rows = svc_as_admin.read(['workspace_id'])
        self.assertEqual(
            rows[0]['workspace_id'][0], self.ws.id,
            "#994: admin 讀得到 cloud_service 卻讀不到它的 workspace_id ⇒ "
            "後台 list/form 的 workspace 欄位會壞掉，射程要擴到 workspace model",
        )

    def test_admin_sees_foreign_service_through_the_backend_list_call(self):
        """#994 端到端：走 list view 真正呼叫的 `web_search_read`，不是 ORM read。

        `read()` 那支證明 ORM 層通了，但後台 list 走的是
        `web_search_read(domain, specification, ...)`（`addons/web/models/models.py`），
        它內部是 `search_fetch` ＋ `web_read`——與 `read()` 不同的取值路徑。
        本單的症狀是「清單恆空」，所以驗收要打在產生那個清單的那個呼叫上，
        不能只驗一個形狀相近的代理。specification 帶 `workspace_id` 是因為
        view 有這一欄，而 `workspace` model 自己沒有永真腿。
        """
        result = self.Service.with_user(self.admin).web_search_read(
            domain=[('id', '=', self.svc.id)],
            specification={
                'name': {},
                'state': {},
                'workspace_id': {'fields': {'display_name': {}}},
            },
        )
        self.assertEqual(
            result['length'], 1,
            "#994: 後台 Services 清單（web_search_read）對 base.group_system 仍是空的",
        )
        self.assertEqual(
            result['records'][0]['workspace_id']['display_name'], self.ws.name,
            "#994: 清單出得來但 workspace 欄位取不到 display_name ⇒ "
            "射程要擴到 workspace model",
        )

    def test_plain_internal_user_still_cannot_read_foreign_service(self):
        """#994 反向對照：永真腿只放行 group_system，不得順手放行所有內部使用者。

        本支在補規則前後都應綠。它抓的是「新規則的 `groups` 誤綁成
        `base.group_user`」——那種寫法會讓上面三支照樣轉綠，只有這一支會紅。
        """
        visible = self.Service.with_user(self.outsider).search([
            ('id', '=', self.svc.id),
        ])
        self.assertFalse(
            visible,
            "#994 迴歸：只有 base.group_user、非 workspace 成員的使用者仍不得看到 "
            "其他 workspace 的 cloud_service——永真腿寫太寬了",
        )

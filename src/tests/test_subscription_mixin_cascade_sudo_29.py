"""#29 回歸：``subscription_lifecycle_mixin`` 內部 subscription traversal 的 cascade sudo。

## 缺陷

mixin 的三個狀態判準方法直接讀 ``self.subscription_id`` 再取 ``sub.state`` /
``sub.sh_end_date``——那是對 ``sh.subscription.subscription`` 的 **relation
traversal**，會走 ``ir.model.access``。而該 model 的讀權限只發給
``group_manager_sh_subscription`` / ``group_user_sh_subscription`` /
``base.group_portal``（見 sh_subscription/security/ir.model.access.csv:7-13），
**workspace 擁有者並不會自動拿到**：workspace ownership 是平台側概念，跟 Odoo 群組
無關。於是典型租戶（只有 ``base.group_user`` 的內部使用者）打 SH ``action=delete`` /
``action=provision``、CS ``action=update`` / ``action=delete`` 時，過得了 #24 的
[OWNER, ADMIN] role check，卻在 ``_check_operation_allowed`` 內部撞 AccessError，
endpoint 回 ``{success: false, error: "You are not allowed to access
sh.subscription.subscription"}``。

PR #28（#27）只修了 **controller 端**的 traversal（Q2=B 刻意把稽核界線畫在
controller）；當時「CS 在 stg 可用」的反證後來被 R1 review 推翻——那是 **env cache
命中的假象**，不是權限上的健全性。本檔即 #29 的 S2 段：把界線推進 mixin 內部。

## 為什麼 sudo 不是權限放大

mixin 是 SH / CS / SA 三個 controller 共用的**內部 helper**，呼叫它之前
controller 已經做完 workspace 歸屬 + role 兩道閘（誰能驅動這些方法早就被決定了）。
這裡的 sudo 只是讓「已獲授權的人」讀得到自己那筆訂閱的狀態，與 ``paas.py`` /
``smart_home.py`` 既有的 cascade-sudo 同一個 pattern。

## 測試形狀（三件套，缺一都會假綠）

1. **鑑別力驗證**：``setUp`` 先斷言 fixture 使用者「真的」讀不到
   ``sh.subscription.subscription``（``_assert_subscription_read_denied``）。若哪天
   ACL 放寬、或 sandbox DB 的 ``default_user_template`` 又把管理群組塞回一般使用者，
   這條會**大聲失敗**，而不是讓整組回歸測試靜靜地失去意義（PR #28 R1 review 抓到的
   正是這種無聲 no-op）。
2. **cache 失效**：Odoo 的 ``env.cache`` 是 transaction 級且**跨 env 共用**。fixture
   以 admin 建資料時已經把訂閱讀進 cache，不 ``invalidate_all()`` 的話 user-scoped
   traversal 會吃 cache 而不重查 ACL——缺陷正是在 cache 落空時才現形（production 每
   個 HTTP request 都從空 cache 開始）。
3. **語意未被 sudo 洗掉**：sudo 之後訂閱閘仍必須擋得住過期／取消的訂閱。若有人用
   「吞掉例外」來修這個 bug，``test_*_blocks_*`` 會紅。這類斷言有兩個假綠陷阱，
   都由 ``_assert_blocked_by_subscription_state`` 統一處理：``AccessError`` 是
   ``UserError`` 的子類別（所以要排除）；而 ``assertRaises`` 本身會餵飽 cache 讓
   AccessError 根本冒不出來（所以改手寫 try/except，理由見該 helper 的 docstring）。
"""
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from odoo.addons.woow_paas_platform.controllers.smart_home import (
    SmartHomeController,
)


def _patch_sh_request(env):
    """把 smart_home controller 的 ``request`` 換成只帶 env 的 MagicMock。"""
    return patch(
        'odoo.addons.woow_paas_platform.controllers.smart_home.request',
        new=MagicMock(env=env),
    )


def _patch_paas_request(env):
    """把 paas controller 的 ``request`` 換成只帶 env 的 MagicMock。"""
    return patch(
        'odoo.addons.woow_paas_platform.controllers.paas.request',
        new=MagicMock(env=env),
    )


class _SubscriptionMixinSudoFixture:
    """共用 fixture：一個「只有 base.group_user」的 workspace 擁有者 + 一筆訂閱。

    刻意不繼承 ``TransactionCase``——否則 Odoo 的 test loader 會把這個抽象基底
    也收進去跑（``_make_entity`` 未實作 → 整組 ERROR）。
    """

    #: 子類別覆寫，用來讓 login / 名稱在同一次 test run 內不撞號。
    _label = 'entity'

    # ------------------------------------------------------------------ #
    # fixture helpers
    # ------------------------------------------------------------------ #

    def _make_plain_owner(self):
        """建一個**只有** ``base.group_user`` 的內部使用者。

        顯式重設 ``groups_id``：從 stg clone 出來的 sandbox DB 帶有
        ``res.config.settings.default_user_template``，會在 ``create`` 當下自動塞
        4 個管理群組 + Invoicing。不重設的話這個 fixture 其實是 admin-equivalent，
        整組回歸測試會靜靜地失去意義（PR #28 的 R1 reviewer 用 revert cycle 抓到
        過一次：修復前測試照樣綠）。
        """
        user = self.env['res.users'].create({
            'name': 'Mixin Sudo Owner (%s)' % self._label,
            'login': 'mixin_sudo_owner_%s' % self._label,
            'email': 'mixin_sudo_owner_%s@example.test' % self._label,
        })
        user.groups_id = [(6, 0, [self.env.ref('base.group_user').id])]
        return user

    def _assert_manager_group_absent(self, user):
        """釘住 fixture 的語意：這個人不在 sh_subscription 的管理群組裡。"""
        manager_group = self.env.ref(
            'sh_subscription.group_manager_sh_subscription',
            raise_if_not_found=False,
        )
        if manager_group:
            self.assertNotIn(
                manager_group, user.groups_id,
                "fixture 失去鑑別力：使用者被塞進 group_manager_sh_subscription，"
                "traversal 本來就不會被擋，這組回歸測試等於恆綠。",
            )

    def _assert_subscription_read_denied(self, user, subscription):
        """鑑別力驗證：這個使用者對訂閱 model **真的**沒有讀權限。

        直接 ``read()`` 而不是走欄位屬性，確保吃到的是 ``ir.model.access`` 判定
        而不是 cache。這裡用 ``assertRaises`` 是安全的：Odoo 只在**預期例外是
        AccessError** 時才會在 savepoint flush 之後補 ``cr.clear()``
        （odoo/tests/common.py:477-482）。
        """
        self.env.invalidate_all()
        with self.assertRaises(AccessError):
            subscription.with_user(user).read(['state'])
        self.env.invalidate_all()

    def _seed_subscription(self, entity, state='in_progress', **field_vals):
        """在 ``entity`` 上掛一筆指定狀態的訂閱（比照既有 lifecycle 測試的做法）。

        不走 ``_create_and_confirm_subscription``：那條路要 journal 設定並會真的
        開發票，對一個純 ACL 回歸來說是無關的耦合。
        """
        Plan = self.env['sh.subscription.plan']
        plan = Plan.create({
            'name': 'Mixin Sudo Plan (%s)' % self._label,
            'sh_plan_price': 100.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
        })
        product_tmpl = self.env['product.template'].create({
            'name': 'Mixin Sudo Product (%s)' % self._label,
            'type': 'service',
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': plan.id,
            'list_price': plan.sh_plan_price,
        })
        subscription = self.env['sh.subscription.subscription'].create({
            'sh_partner_id': self.owner.partner_id.id,
            'product_id': product_tmpl.product_variant_id.id,
            'sh_subscription_plan_id': plan.id,
        })
        # 直接寫 state：測的是 ACL，不是 sh_subscription 的狀態機。
        vals = dict(field_vals)
        vals['state'] = state
        subscription.write(vals)
        entity.write({'subscription_id': subscription.id})
        return subscription

    def _entity_as_owner(self):
        """以「非 manager 的 workspace 擁有者」身分重新取得 entity。

        每次都先倒 cache：admin 端的 fixture 讀取會把訂閱欄位餵進 transaction 級
        cache，不倒的話 user-scoped traversal 命中 cache、根本不會重查 ACL——缺陷
        就被遮掉了。
        """
        self.env.invalidate_all()
        return self.entity.with_user(self.owner)

    def _switch_subscription(self, state, **field_vals):
        """改掉既有訂閱的狀態（grace / past-grace 情境用）。"""
        vals = dict(field_vals)
        vals['state'] = state
        self.subscription.write(vals)

    def _assert_blocked_by_subscription_state(self, entity, expected_phrase):
        """斷言「被擋下」而且**擋的原因是訂閱狀態、不是 ACL**。

        刻意用手寫 try/except 而不是 ``assertRaises``：Odoo 的
        ``BaseCase._assertRaises``（odoo/tests/common.py:472）會先開一個
        ``cr.savepoint()``，而 savepoint 會 flush——flush 把大量記錄餵回 cache，
        後續讀取命中 cache 就**不會**再查 ACL。Odoo 自己只在「預期例外是
        AccessError」時補一發 ``cr.clear()``；預期 ``UserError`` 時不清，於是
        ``assertRaises(UserError)`` 會把本來該冒出來的 AccessError 蓋掉（實測：
        同一個情境用 assertRaises 得到 UserError('suspended')、改手寫 try/except
        得到 AccessError）。這正是 #29 issue 描述的「cache-hit illusion」在測試
        框架裡的翻版——用 assertRaises 寫這條，修復前也會綠。

        另外 ``AccessError`` 是 ``UserError`` 的子類別，所以「有擋到」的斷言一定
        要再排除 AccessError，否則 ACL 例外會冒充成閘生效。
        """
        raised = None
        try:
            entity._check_operation_allowed(operation='write')
        except Exception as exc:  # noqa: BLE001 — 型別本身是斷言對象
            raised = exc
        self.assertIsNotNone(
            raised, "訂閱閘沒擋住——sudo 修法不得把狀態判準變成 no-op。")
        self.assertNotIsInstance(
            raised, AccessError,
            "擋下來的原因是 ACL 而不是訂閱狀態——那是 #29 的缺陷本身，不是閘生效。",
        )
        self.assertIsInstance(raised, UserError)
        self.assertIn(expected_phrase, str(raised).lower())

    # ------------------------------------------------------------------ #
    # setUp
    # ------------------------------------------------------------------ #

    def setUp(self):
        super().setUp()
        self.owner = self._make_plain_owner()
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Mixin Sudo WS (%s)' % self._label,
            'owner_id': self.owner.id,
        })
        self.entity = self._make_entity()
        self.subscription = self._seed_subscription(self.entity)

        # fixture 的三道 sanity（任一條沒過，底下整組就沒有回歸價值）
        self.assertEqual(self.subscription.state, 'in_progress')
        self._assert_manager_group_absent(self.owner)
        self._assert_subscription_read_denied(self.owner, self.subscription)

    def _make_entity(self):
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # _is_subscription_active
    # ------------------------------------------------------------------ #

    def test_is_subscription_active_no_access_error(self):
        """非 manager 的 workspace 擁有者呼叫 ``_is_subscription_active`` 不得炸。

        修復前：``sub.state`` 觸發 ``sh.subscription.subscription`` 的 read ACL →
        AccessError。修復後：traversal 走 sudo，回 True。
        """
        entity = self._entity_as_owner()
        self.assertTrue(entity._is_subscription_active())

    def test_is_subscription_active_false_for_expired(self):
        """語意未被 sudo 洗掉：過期訂閱仍要判成「非 active」。"""
        self._switch_subscription('expire')
        entity = self._entity_as_owner()
        self.assertFalse(entity._is_subscription_active())

    # ------------------------------------------------------------------ #
    # _is_in_grace_period
    # ------------------------------------------------------------------ #

    def test_is_in_grace_period_no_access_error(self):
        """寬限期判斷讀 ``sub.sh_end_date``，同一條 traversal 同一個缺陷。"""
        self._switch_subscription(
            'expire', sh_end_date=date.today() - timedelta(days=3),
        )
        entity = self._entity_as_owner()
        self.assertTrue(entity._is_in_grace_period())

    # ------------------------------------------------------------------ #
    # _is_past_grace_period
    # ------------------------------------------------------------------ #

    def test_is_past_grace_period_no_access_error(self):
        """取消的訂閱：``sub.state`` 一樣是 traversal。"""
        self._switch_subscription('cancel')
        entity = self._entity_as_owner()
        self.assertTrue(entity._is_past_grace_period())

    # ------------------------------------------------------------------ #
    # _check_operation_allowed
    # ------------------------------------------------------------------ #

    def test_check_operation_allowed_active_does_not_raise(self):
        """訂閱有效 → 直接放行，不得因為 ACL 而炸。

        這是 SH delete / provision 與 CS update / delete 在 endpoint 上的實際
        入口，也是 #29 在 production 的症狀點。
        """
        entity = self._entity_as_owner()
        entity._check_operation_allowed(operation='write')

    def test_check_operation_allowed_still_blocks_past_grace(self):
        """sudo 不得把訂閱閘變成 no-op：超過寬限期仍要擋寫入。"""
        self._switch_subscription('cancel')
        self._assert_blocked_by_subscription_state(
            self._entity_as_owner(), 'suspended',
        )

    def test_check_operation_allowed_grace_blocks_write_allows_read(self):
        """寬限期內：read 放行、write 擋下（同樣要求擋人的原因不是 ACL）。"""
        self._switch_subscription(
            'expire', sh_end_date=date.today() - timedelta(days=3),
        )
        entity = self._entity_as_owner()
        entity._check_operation_allowed(operation='read')
        self._assert_blocked_by_subscription_state(entity, 'grace period')


class TestSubscriptionMixinSudoSmartHome(
    _SubscriptionMixinSudoFixture, TransactionCase,
):
    """SmartHome —— #29 的原始回報路徑（delete / provision）。"""

    # Odoo 的 test loader 預設只收 `cls.__dict__` 裡的 test method
    # （odoo/tests/loader.py:get_module_test_cases），共用測試體放在 mixin
    # 基底時**一個都不會被收**（跑起來是 "0 tests"，不是紅燈）。要顯式打開。
    allow_inherited_tests_method = True
    _label = 'sh'

    def _make_entity(self):
        return self.env['woow_paas_platform.smart_home'].create({
            'name': 'Mixin Sudo Home',
            'workspace_id': self.workspace.id,
        })


class TestSubscriptionMixinSudoCloudService(
    _SubscriptionMixinSudoFixture, TransactionCase,
):
    """CloudService —— 對稱端（update / delete 走同一個 mixin）。"""

    allow_inherited_tests_method = True
    _label = 'cs'

    def _make_entity(self):
        template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Mixin Sudo Tmpl',
            'slug': 'mixin-sudo-tmpl',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'mixin-sudo-tmpl',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        return self.env['woow_paas_platform.cloud_service'].create({
            'name': 'Mixin Sudo Service',
            'workspace_id': self.workspace.id,
            'template_id': template.id,
        })


class TestSubscriptionMixinSudoSecurityAccess(
    _SubscriptionMixinSudoFixture, TransactionCase,
):
    """SecurityAccess —— 第三個 mixin 消費者，同一條 traversal。"""

    allow_inherited_tests_method = True
    _label = 'sa'

    def _make_entity(self):
        return self.env['woow_paas_platform.security_access'].create({
            'name': 'Mixin Sudo Gateway',
            'workspace_id': self.workspace.id,
        })


class TestSmartHomeDetailEndpointMixinSudo(TransactionCase):
    """endpoint 級：SH ``action=delete`` / ``action=provision``。

    model 級測試證明 mixin 本身修好了；這裡證明**使用者看得到的那個量**也修好了
    ——endpoint 回 ``success: true`` 且真的往下走到業務 handler，而不是被
    ``except Exception`` 接住變成 ``{success: false, error: "You are not allowed
    to access sh.subscription.subscription"}``。

    只 mock 業務 handler（``_delete_smarthome`` / ``_provision_smarthome``），
    **不 mock** ``_check_operation_allowed``——它正是被測物。
    """

    def setUp(self):
        super().setUp()
        self.controller = SmartHomeController()
        self.owner = self.env['res.users'].create({
            'name': 'SH Endpoint Sudo Owner',
            'login': 'mixin_sudo_sh_endpoint_owner',
            'email': 'mixin_sudo_sh_endpoint_owner@example.test',
        })
        self.owner.groups_id = [(6, 0, [self.env.ref('base.group_user').id])]
        manager_group = self.env.ref(
            'sh_subscription.group_manager_sh_subscription',
            raise_if_not_found=False,
        )
        if manager_group:
            self.assertNotIn(manager_group, self.owner.groups_id)

        # Workspace.create 會自動補 owner 的 workspace_access(role='owner')。
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'SH Endpoint Sudo WS',
            'owner_id': self.owner.id,
        })
        self.smart_home = self.env['woow_paas_platform.smart_home'].create({
            'name': 'SH Endpoint Sudo Home',
            'workspace_id': self.workspace.id,
        })
        plan = self.env['sh.subscription.plan'].create({
            'name': 'SH Endpoint Sudo Plan',
            'sh_plan_price': 100.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
        })
        product_tmpl = self.env['product.template'].create({
            'name': 'SH Endpoint Sudo Product',
            'type': 'service',
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': plan.id,
            'list_price': plan.sh_plan_price,
        })
        self.subscription = self.env['sh.subscription.subscription'].create({
            'sh_partner_id': self.owner.partner_id.id,
            'product_id': product_tmpl.product_variant_id.id,
            'sh_subscription_plan_id': plan.id,
        })
        self.subscription.write({'state': 'in_progress'})
        self.smart_home.write({'subscription_id': self.subscription.id})

        # 鑑別力：這個 owner 真的讀不到訂閱 model。
        self.env.invalidate_all()
        with self.assertRaises(AccessError):
            self.subscription.with_user(self.owner).read(['state'])
        # 進 endpoint 前倒乾淨 cache——production 的每個 request 都是空 cache。
        self.env.invalidate_all()

    def test_delete_owner_without_manager_group_passes_subscription_gate(self):
        """修復前：``_check_operation_allowed`` 丟 AccessError，endpoint 回
        ``success: false``、``_delete_smarthome`` 從未被呼叫。"""
        with patch.object(
            SmartHomeController, '_delete_smarthome',
            return_value={'success': True, 'data': {'message': 'deleted'}},
        ) as mocked, _patch_sh_request(self.env(user=self.owner)):
            res = self.controller.api_smarthome_detail(
                workspace_id=self.workspace.id,
                smarthome_id=self.smart_home.id,
                action='delete',
            )
        self.assertTrue(res['success'], res.get('error'))
        mocked.assert_called_once()

    def test_provision_owner_without_manager_group_passes_subscription_gate(self):
        """provision 分支與 delete 共用同一道訂閱閘，缺陷與修復一致。"""
        with patch.object(
            SmartHomeController, '_provision_smarthome',
            return_value={'success': True, 'data': {}},
        ) as mocked, _patch_sh_request(self.env(user=self.owner)):
            res = self.controller.api_smarthome_detail(
                workspace_id=self.workspace.id,
                smarthome_id=self.smart_home.id,
                action='provision',
            )
        self.assertTrue(res['success'], res.get('error'))
        mocked.assert_called_once()

    def test_delete_still_blocked_when_subscription_cancelled(self):
        """訂閱閘仍要擋得住：取消後 delete 必須被拒，且**不是**因為 ACL。"""
        self.subscription.write({'state': 'cancel'})
        self.env.invalidate_all()
        with patch.object(
            SmartHomeController, '_delete_smarthome',
            return_value={'success': True, 'data': {}},
        ) as mocked, _patch_sh_request(self.env(user=self.owner)):
            res = self.controller.api_smarthome_detail(
                workspace_id=self.workspace.id,
                smarthome_id=self.smart_home.id,
                action='delete',
            )
        self.assertFalse(res['success'])
        self.assertNotIn('not allowed to access', res['error'].lower())
        self.assertIn('suspended', res['error'].lower())
        mocked.assert_not_called()


class TestCloudServiceEndpointMixinSudo(TransactionCase):
    """endpoint 級：CS ``action=update`` / ``action=delete``（對稱覆蓋）。"""

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.owner = self.env['res.users'].create({
            'name': 'CS Endpoint Sudo Owner',
            'login': 'mixin_sudo_cs_endpoint_owner',
            'email': 'mixin_sudo_cs_endpoint_owner@example.test',
        })
        self.owner.groups_id = [(6, 0, [self.env.ref('base.group_user').id])]
        manager_group = self.env.ref(
            'sh_subscription.group_manager_sh_subscription',
            raise_if_not_found=False,
        )
        if manager_group:
            self.assertNotIn(manager_group, self.owner.groups_id)

        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'CS Endpoint Sudo WS',
            'owner_id': self.owner.id,
        })
        template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'CS Endpoint Sudo Tmpl',
            'slug': 'cs-endpoint-sudo-tmpl',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'cs-endpoint-sudo-tmpl',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.service = self.env['woow_paas_platform.cloud_service'].create({
            'name': 'CS Endpoint Sudo Service',
            'workspace_id': self.workspace.id,
            'template_id': template.id,
        })
        plan = self.env['sh.subscription.plan'].create({
            'name': 'CS Endpoint Sudo Plan',
            'sh_plan_price': 100.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
        })
        product_tmpl = self.env['product.template'].create({
            'name': 'CS Endpoint Sudo Product',
            'type': 'service',
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': plan.id,
            'list_price': plan.sh_plan_price,
        })
        self.subscription = self.env['sh.subscription.subscription'].create({
            'sh_partner_id': self.owner.partner_id.id,
            'product_id': product_tmpl.product_variant_id.id,
            'sh_subscription_plan_id': plan.id,
        })
        self.subscription.write({'state': 'in_progress'})
        self.service.write({'subscription_id': self.subscription.id})

        self.env.invalidate_all()
        with self.assertRaises(AccessError):
            self.subscription.with_user(self.owner).read(['state'])
        self.env.invalidate_all()

    def test_update_owner_without_manager_group_passes_subscription_gate(self):
        with patch.object(
            PaasController, '_update_service',
            return_value={'success': True, 'data': {}},
        ) as mocked, _patch_paas_request(self.env(user=self.owner)):
            res = self.controller.api_workspace_service(
                workspace_id=self.workspace.id,
                service_id=self.service.id,
                action='update',
            )
        self.assertTrue(res['success'], res.get('error'))
        mocked.assert_called_once()

    def test_delete_owner_without_manager_group_passes_subscription_gate(self):
        with patch.object(
            PaasController, '_delete_service',
            return_value={'success': True, 'data': {}},
        ) as mocked, _patch_paas_request(self.env(user=self.owner)):
            res = self.controller.api_workspace_service(
                workspace_id=self.workspace.id,
                service_id=self.service.id,
                action='delete',
            )
        self.assertTrue(res['success'], res.get('error'))
        mocked.assert_called_once()

    def test_update_still_blocked_when_subscription_cancelled(self):
        """CS 端同樣要證明閘沒被 sudo 洗掉。"""
        self.subscription.write({'state': 'cancel'})
        self.env.invalidate_all()
        with patch.object(
            PaasController, '_update_service',
            return_value={'success': True, 'data': {}},
        ) as mocked, _patch_paas_request(self.env(user=self.owner)):
            res = self.controller.api_workspace_service(
                workspace_id=self.workspace.id,
                service_id=self.service.id,
                action='update',
            )
        self.assertFalse(res['success'])
        self.assertNotIn('not allowed to access', res['error'].lower())
        self.assertIn('suspended', res['error'].lower())
        mocked.assert_not_called()

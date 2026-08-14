"""#519 提案 B：`subscription_id` 的歸屬／狀態／唯一性。

提案 A 關掉的是「誰能建 cloud_service」，關不掉「已合法建立的服務事後把
`subscription_id` 改指向別人的活訂閱」。Odoo 18 寫 Many2one **不檢查 comodel 的
ACL／ir.rule**（`write()` 只 `check_access` 被寫入的 model 自己），而
`subscription_id` 沒有 `check_company`、沒有 `@api.constrains`——所以租戶可以把
**任意存在的訂閱 id**（含別人的）綁到自己的服務上，id 可暴力枚舉、不需要對那筆
訂閱有讀權限。實務攻擊：自己的訂閱被停權後改指向別人的活訂閱，逃過
`_cron_enforce_subscription_lifecycle`。

## 🔴 這是 transition guard，不是常駐不變式

訂閱到期／關閉之後**仍然合法地**綁在服務上——停權 cron 正是靠這個才做得到它的
工作。所以狀態這一格只在**綁定真的改變的那一次**檢查。`test_B5_*` 就是這條的
判別式：它讓一筆已經到期的訂閱留在原位再寫一次同值，必須**不**被擋。寫成常駐
不變式的實作會在那裡紅。

## 這一檔自己的假綠面，逐個配對照

* **一律 raise** → B2 的陽性對照（自己的、in_progress 的訂閱必須綁得上）。
* **歸因** → 斷言 `SubscriptionBindingError` 而非 `AccessError`；後者也是 ACL
  拒絕會丟的型別，分不出是被哪一層擋的。
* **沒 raise ≠ 沒寫進去** → 每組 deny 之後用 raw SQL 讀回落地值。
* **唯一性測試可能因為索引不存在而以別的方式失敗** → B4 之前先斷言索引真的在
  `pg_indexes` 裡。少了它，「索引沒建起來」與「索引擋住了」會長得很像。
* **第三方行為漂移** → B3b 直接釘住「新建的訂閱預設是 draft」。B3 的擋人能力整個
  建立在那個事實上（租戶的鑄幣口只鑄得出 draft，見 #725）；vendored addon 哪天改成
  自動 confirm，B3b 會先紅並指出原因，而不是讓 B3 靜默失去意義。
* **C4（`default_*` 注入）** → B6 同時測 context 載體與 `ir.default` 這個**持久化**
  載體。只讀 raw vals 的實作在 B6 會紅。
"""

from psycopg2 import IntegrityError

from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger

from odoo.addons.woow_paas_platform.models.cloud_service import (
    SubscriptionBindingError,
    _internal_provision,
)

_INDEX = 'woow_paas_cloud_service_subscription_uniq'


class _BindingFixture(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Service = self.env['woow_paas_platform.cloud_service']
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.Subscription = self.env['sh.subscription.subscription']
        Template = self.env['woow_paas_platform.cloud_app_template']
        User = self.env['res.users']
        group_user = self.env.ref('base.group_user')

        self.alice = User.create({
            'name': 'B519 Alice', 'login': 'b519_alice',
            'email': 'b519_alice@test.example',
            'groups_id': [(6, 0, [group_user.id])],
        })
        self.bob = User.create({
            'name': 'B519 Bob', 'login': 'b519_bob',
            'email': 'b519_bob@test.example',
            'groups_id': [(6, 0, [group_user.id])],
        })
        self.assertFalse(self.alice.has_group('base.group_system'),
                         '前提：alice 是租戶不是系統管理員')

        self.ws_a = self.Workspace.create({
            'name': 'B519 WS A', 'owner_id': self.alice.id})
        self.ws_b = self.Workspace.create({
            'name': 'B519 WS B', 'owner_id': self.bob.id})

        self.template = Template.create({
            'name': 'B519 Template', 'slug': 'b519',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'b519',
            # 釘版：未釘版會被供給閘擋下，fixture 死在抵達受測步驟之前。
            'helm_chart_version': '1.0.0',
        })

        self.sub_alice = self._sub(self.alice, 'in_progress')
        self.sub_bob = self._sub(self.bob, 'in_progress')
        self.svc_alice = self._service(self.ws_a, 'B519 svc alice')

    # ------------------------------------------------------------------ #
    #  Helpers                                                           #
    # ------------------------------------------------------------------ #

    def _sub(self, user, state):
        return self.Subscription.sudo().create({
            'sh_partner_id': user.partner_id.id,
            'state': state,
        })

    def _service(self, workspace, name, **extra):
        vals = {
            'name': name,
            'workspace_id': workspace.id,
            'template_id': self.template.id,
        }
        vals.update(extra)
        return _internal_provision(self.Service).create(vals)

    def _db_subscription_id(self, service):
        """直接讀 Postgres：「沒有 raise」與「沒有寫進去」是兩個宣稱。"""
        self.env.flush_all()
        self.env.cr.execute(
            'SELECT subscription_id FROM woow_paas_platform_cloud_service '
            'WHERE id = %s', (service.id,))
        return self.env.cr.fetchone()[0]


class TestSubscriptionBindingOwnership(_BindingFixture):

    def test_B1_cannot_point_at_someone_elses_live_subscription(self):
        before = self._db_subscription_id(self.svc_alice)
        with self.assertRaises(SubscriptionBindingError):
            self.svc_alice.write({'subscription_id': self.sub_bob.id})
        self.assertEqual(self._db_subscription_id(self.svc_alice), before,
                         'B1：指標不得被改動')

    def test_B1b_the_same_denial_holds_as_the_tenant_over_bare_orm(self):
        """以 alice 自己的身分走裸 ORM——這是實際攻擊路徑的形狀。"""
        svc = self.svc_alice.with_user(self.alice)
        with self.assertRaises(SubscriptionBindingError):
            svc.write({'subscription_id': self.sub_bob.id})
        self.assertFalse(self._db_subscription_id(self.svc_alice))

    def test_B2_positive_control_own_in_progress_subscription_binds(self):
        """陽性對照。**沒有這條，上面每一條 deny 都可能只是「守衛一律 raise」。**"""
        self.svc_alice.write({'subscription_id': self.sub_alice.id})
        self.assertEqual(self._db_subscription_id(self.svc_alice),
                         self.sub_alice.id)

    def test_B3_cannot_bind_a_draft_subscription(self):
        """租戶鑄得出訂閱，但只鑄得出 draft（第三方 `/renew-subscription`，#725）。

        只驗歸屬不驗狀態的實作會在這裡綠——那正是「無限鑄幣」打穿 B 的形態。
        """
        draft = self._sub(self.alice, 'draft')
        with self.assertRaises(SubscriptionBindingError):
            self.svc_alice.write({'subscription_id': draft.id})
        self.assertFalse(self._db_subscription_id(self.svc_alice))

    def test_B3b_third_party_pin_new_subscriptions_default_to_draft(self):
        """釘住 B3 賴以成立的第三方事實。

        `/renew-subscription`（`auth="user"` 的 sudo controller）讓任何登入使用者
        拿自己的訂閱當範本再鑄一筆。它擋得住的**唯一**理由是鑄出來的停在 draft，
        而租戶沒有任何可觸及的 confirm 路徑。vendored addon 改成自動 confirm 的
        話，這條會先紅並說明原因，而不是讓 B3 靜默退化成零鑑別力。
        """
        minted = self.Subscription.sudo().create({
            'sh_partner_id': self.alice.partner_id.id,
        })
        self.assertEqual(
            minted.state, 'draft',
            '第三方 sh_subscription 的預設 state 變了 ⇒ #519 提案 B 的狀態判準'
            '（以及 #725 的風險評估）要重新看過')

    def test_B_binding_error_is_an_access_error_subclass(self):
        self.assertTrue(issubclass(SubscriptionBindingError, AccessError))


class TestSubscriptionBindingNoOpAndUnbind(_BindingFixture):

    def test_B5_no_op_rewrite_is_not_blocked_even_after_expiry(self):
        """🔴 本檔最重要的一條：守衛是 transition guard，不是常駐不變式。

        訂閱到期之後仍然合法地綁著（停權 cron 靠這個工作）。前端整批送出未修改
        欄位是常態 ⇒ 「寫同一個值」不得被擋。把守衛寫成「每次 write 都驗當下綁的
        訂閱是不是 in_progress」的實作，會在這裡紅。
        """
        self.svc_alice.write({'subscription_id': self.sub_alice.id})
        self.sub_alice.sudo().write({'state': 'expire'})
        # 同值重寫 + 一併送出別的欄位（前端的實際形狀）
        self.svc_alice.write({
            'subscription_id': self.sub_alice.id,
            'name': 'B519 renamed',
        })
        self.assertEqual(self._db_subscription_id(self.svc_alice),
                         self.sub_alice.id)
        self.assertEqual(self.svc_alice.name, 'B519 renamed')

    def test_unbinding_is_refused(self):
        """清空指標＝讓服務永久脫離停權／結算。

        `_cron_enforce_subscription_lifecycle` 的 domain 是
        `subscription_id != False`，所以「解除綁定」不是回到未開通狀態，是**逃出
        生命週期管理**。平台自己沒有任何一條路徑會這樣寫。
        """
        self.svc_alice.write({'subscription_id': self.sub_alice.id})
        with self.assertRaises(SubscriptionBindingError):
            self.svc_alice.write({'subscription_id': False})
        self.assertEqual(self._db_subscription_id(self.svc_alice),
                         self.sub_alice.id)

    def test_unbinding_a_service_that_has_no_subscription_is_a_no_op(self):
        """尚未掛訂閱的服務寫 False 不算解除綁定——marketplace 下單流程在建立與
        掛訂閱之間就處在這個狀態，不能誤擋。"""
        self.svc_alice.write({'subscription_id': False})
        self.assertFalse(self._db_subscription_id(self.svc_alice))


class TestSubscriptionBindingUniqueness(_BindingFixture):

    def test_the_partial_unique_index_exists(self):
        """B4 的前提。少了這條，「索引根本沒建起來」與「索引擋住了」會長得很像。"""
        self.env.cr.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname = %s", (_INDEX,))
        row = self.env.cr.fetchone()
        self.assertTrue(row, '%s 不存在（CloudService.init() 沒建起來？）' % _INDEX)
        self.assertIn('subscription_id IS NOT NULL', row[0],
                      '必須是 partial index（NULL 不參與唯一性）')

    def test_B4_two_services_cannot_share_one_subscription(self):
        """並發下唯一的保證是 DB 索引——Odoo 的 cursor 是 REPEATABLE READ，
        Python 端 count-after-act 在同一個 tx 裡拿不到新 snapshot（#518）。"""
        self.svc_alice.write({'subscription_id': self.sub_alice.id})
        svc2 = self._service(self.ws_a, 'B519 svc alice 2')
        with self.assertRaises(IntegrityError), mute_logger('odoo.sql_db'):
            svc2.write({'subscription_id': self.sub_alice.id})
            self.env.flush_all()

    def test_null_subscription_ids_do_not_collide(self):
        """陽性對照：partial index 只約束非 NULL 值。兩筆未掛訂閱的服務必須共存
        ——marketplace 下單流程在掛訂閱之前正是這個狀態。"""
        self._service(self.ws_a, 'B519 unbound 1')
        self._service(self.ws_a, 'B519 unbound 2')
        self.env.flush_all()


class TestSubscriptionBindingAtCreate(_BindingFixture):

    def test_create_with_someone_elses_subscription_is_refused(self):
        with self.assertRaises(SubscriptionBindingError):
            self._service(self.ws_a, 'B519 create forged',
                          subscription_id=self.sub_bob.id)

    def test_create_positive_control(self):
        svc = self._service(self.ws_a, 'B519 create ok',
                            subscription_id=self.sub_alice.id)
        self.assertEqual(self._db_subscription_id(svc), self.sub_alice.id)

    def test_B6_context_default_injection_is_refused(self):
        """C4 判別式（context 載體）。只讀 raw vals 的實作在這裡綠。"""
        with self.assertRaises(SubscriptionBindingError):
            _internal_provision(self.Service).with_context(
                default_subscription_id=self.sub_bob.id
            ).create({
                'name': 'B519 ctx default',
                'workspace_id': self.ws_a.id,
                'template_id': self.template.id,
            })

    def test_B6b_ir_default_injection_is_refused(self):
        """C4 判別式（**持久化**載體）。

        `ir.default` 比 context 更難防：Odoo 18 base 的
        `access_ir_default_group_user` 給 `base.group_user` **1,1,1,1**，而
        `ir.default.set()` 是 `@api.model`、不查呼叫者對目標欄位的權限 ⇒ 租戶自己
        寫得進去，而且**寫一次就一直在**。這是「守衛不讀 vals／讀落地值」這個設計
        決定的直接理由。
        """
        self.env['ir.default'].set(
            'woow_paas_platform.cloud_service', 'subscription_id',
            self.sub_bob.id)
        with self.assertRaises(SubscriptionBindingError):
            self._service(self.ws_a, 'B519 ir default')


class TestSubscriptionBindingLegitFlows(_BindingFixture):

    def test_B7_create_and_confirm_subscription_still_works(self):
        """合法回歸：平台自己的開通流程走完之後服務有訂閱，且守衛沒有誤擋。

        這條同時是「判準的兩側在合法流程裡確實對得上」的證據——訂閱剛
        `sh_subscription_confirm()` 完（in_progress），`sh_partner_id` 就是
        `workspace_id.owner_id.partner_id`。
        """
        plan = self.env['sh.subscription.plan'].search([], limit=1)
        if not plan:
            self.skipTest('環境沒有 sh.subscription.plan seed，B7 無法在此驗')
        partner = self.ws_a.owner_id.partner_id
        self.svc_alice._create_and_confirm_subscription(partner, plan)
        sub = self.svc_alice.sudo().subscription_id
        self.assertTrue(sub, 'B7：開通流程走完必須掛上訂閱')
        self.assertEqual(sub.state, 'in_progress')
        self.assertEqual(sub.sh_partner_id, partner)

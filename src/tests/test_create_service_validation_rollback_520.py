# -*- coding: utf-8 -*-
"""#520：generic create 端點吞掉業務例外 → 被拒絕的服務仍留在 DB 並被 cron 部署。

`CloudService.create()` 的每帳號實例上限（`max_per_partner`，#336/#345）是靠
`raise ValidationError` 讓交易 rollback 來「撤回」已 INSERT 的那一列。但唯一的
使用者入口 `_create_service` 把例外整包吃掉、以 HTTP 200 正常返回 → 例外從未
傳播到框架層 → request 正常結束 → **transaction 照常 commit**。撤回從未發生：

    07-31  帳號已有 Hermes Agent id=60
    08-02  建 my-hermes → INSERT id=61 → count=2 > max_per_partner=1 → raise
           → controller 吞掉 → commit → id=61 留在 DB（畫面卻顯示失敗）
    08-02  cron Deploy Pending 撿到 pending 的 61 → helm install → Running

所以本檔的驗收面**不是回應字串**，而是側效果：資料庫實體與 helm install。
只斷言 (a) 會假綠——把泛用訊息換成原文訊息就能過，資料照樣殘留、照樣被部署。

三條斷言（issue 驗收要求）：

  (a) 回應帶得到真正的上限訊息（不是 'An error occurred...' 泛用字串）
  (b) DB 沒有留下該筆 CloudService
  (c) 沒有觸發 helm install

**陽性對照內建**（否定性斷言若探針本身失效，假綠與真綠的輸出完全一樣）：

  - (b) 的探針：同一組測試先經**同一條 HTTP 路徑**成功建立 svc1 並斷言查得到。
    若 HttpCase 的 transaction 隔離讓「request 內寫的列測試端本來就看不到」，
    這條會先紅——(b) 的空集合就不會是假綠。
  - (c) 的探針：同一次 cron 掃描中斷言 svc1 **有**被 install。若 mock 沒接上、
    cron 根本沒跑到，這條會紅——(c) 的「沒有第二個 install」就不會是假綠。

另補兩個例外分類面（issue 修復方向 1 與 2）：

  - INSERT 成功、**之後**才 raise 的業務例外（UserError）一樣要撤回：這條打的是
    「rollback 涵蓋整段 create，不只 create() 內部那一步」。
  - 未預期例外維持泛用訊息（不外洩內部細節），但**資料一樣不得殘留**。
"""
from unittest.mock import MagicMock, patch

from odoo.exceptions import AccessError
from odoo.tests.common import HttpCase, tagged

from odoo.addons.woow_paas_platform.models.subscription_lifecycle_mixin import (
    SubscriptionLifecycleMixin,
)
from .ns_quota_fixture import answer_quota_reads

_GENERIC_ERROR = 'An error occurred while creating the service.'
_OPERATOR_CLIENT = (
    'odoo.addons.woow_paas_platform.services.paas_operator.'
    'get_paas_operator_client'
)


@tagged('post_install', '-at_install')
class TestCreateServiceRejectionLeavesNoData(HttpCase):
    """被拒絕的 create 不得留下資料、不得被部署，且錯誤訊息要說得出原因。"""

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        self.admin_user = self.env.ref('base.user_admin')
        self.partner = self.admin_user.partner_id
        self.CloudService = self.env['woow_paas_platform.cloud_service']

        self.ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 520', 'owner_id': self.admin_user.id})

        # 訂閱建立需要 `company.sh_journal_id`（否則 `_create_and_confirm_
        # subscription` 直接 raise UserError）。它由 post_init_hook /
        # `<function>` 設定，**bare install 的 at_install 環境不保證已設好**——
        # 這裡自己補齊，讓本檔的成功路徑（＝(b)(c) 兩個陽性對照）不受安裝路徑
        # 差異左右。慣例鏡射 test_subscription_journal_config.setUpClass。
        company = self.env.company.sudo()
        if not self.env['account.journal'].search([
            ('company_id', '=', company.id), ('type', '=', 'sale'),
        ], limit=1):
            self.env['account.journal'].sudo().create({
                'name': 'Test Sales 520', 'code': 'TS520', 'type': 'sale',
                'company_id': company.id})
        self.env['res.company'].sudo()._ensure_subscription_journal()
        self.assertTrue(
            company.sh_journal_id,
            '本檔的陽性對照需要可用的訂閱 journal；沒有它「建立成功」根本走不完，'
            '(b)(c) 的否定性斷言就會退化成假綠')

        # 自建 plan + product：不綁任何 seed 商品的定價／上架狀態（#478/#494 教訓
        # ——把當下的商業事實寫死成契約，商品一改動整組測試集體變紅）。
        Plan = self.env['sh.subscription.plan']
        self.plan = Plan.create({
            'name': 'Plan 520', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 100, 'sh_no_of_billing_cycle': -1})
        self.env['product.template'].create({
            'name': 'Product 520', 'type': 'service', 'list_price': 100,
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': self.plan.id})
        # 刻意不建 product 的 plan：用來注入「INSERT 成功之後才 raise」的
        # UserError（_create_and_confirm_subscription 找不到訂閱商品）。
        self.plan_no_product = Plan.create({
            'name': 'Plan 520 no product', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 100, 'sh_no_of_billing_cycle': -1})

        Template = self.env['woow_paas_platform.cloud_app_template']
        # helm_* 三欄 required=True（NOT NULL），不給會撞 NotNullViolation。
        self.tpl_limited = Template.create({
            'name': 'Limited 520', 'slug': 'limited-520',
            'helm_repo_url': '', 'helm_chart_name': 'oci://example.invalid/limited-520',
            'helm_chart_version': '0.0.0',
            'is_active': True, 'is_hidden': False,
            'max_per_partner': 1,
            'subscription_plan_ids': [(6, 0, [self.plan.id])]})
        self.tpl_open = Template.create({
            'name': 'Open 520', 'slug': 'open-520',
            'helm_repo_url': '', 'helm_chart_name': 'oci://example.invalid/open-520',
            'helm_chart_version': '0.0.0',
            'is_active': True, 'is_hidden': False,
            'max_per_partner': 0,
            'subscription_plan_ids': [(6, 0, [self.plan_no_product.id])]})

    # ── HTTP 路徑（＝使用者唯一入口）────────────────────────────────────
    def _create(self, template, name, plan=None):
        """走 `/api/workspaces/<id>/services` 的 action='create'（前端的形狀）。"""
        plan = plan or self.plan
        body = (
            '{"jsonrpc":"2.0","method":"call","params":'
            '{"action":"create","template_id":%s,"name":"%s","plan_id":%s}}'
            % (template.id, name, plan.id)
        )
        resp = self.url_open(
            '/api/workspaces/%s/services' % self.ws.id,
            data=body, headers={'Content-Type': 'application/json'})
        parsed = resp.json()
        self.assertIn('result', parsed, 'resp: %s' % parsed)
        self.env.invalidate_all()
        return parsed['result']

    def _rows(self, template, name=None):
        """DB 實體探針：該 template 名下（可再限定 name）的 CloudService。"""
        domain = [('template_id', '=', template.id),
                  ('workspace_id', '=', self.ws.id)]
        if name is not None:
            domain.append(('name', '=', name))
        return self.CloudService.with_context(active_test=False).search(domain)

    # ================================================================
    #  (a) + (b)：上限訊息回得到，且被拒絕的那筆不得留在 DB
    # ================================================================
    def test_at_limit_create_returns_reason_and_leaves_no_row(self):
        first = self._create(self.tpl_limited, 'svc-520-ok')
        # 陽性對照①：同一條 HTTP 路徑成功建立的列，測試端查得到。
        # 這條先綠，下面 (b) 的空集合才有意義（否則「查不到」可能只是
        # HttpCase 的 transaction 根本看不見 request 寫入）。
        self.assertTrue(first.get('success'), '第一個服務應建立成功（回 %r）' % first)
        self.assertEqual(len(self._rows(self.tpl_limited, 'svc-520-ok')), 1,
                         '成功建立的服務必須查得到——探針有鑑別力的證明')

        rejected = self._create(self.tpl_limited, 'svc-520-rejected')

        # (a) 業務規則訊息要原文回給前端（AppConfigurationPage.js 顯示 result.error）
        self.assertFalse(rejected.get('success'), '超過上限應失敗（回 %r）' % rejected)
        self.assertNotEqual(
            rejected.get('error'), _GENERIC_ERROR,
            '業務規則例外不得被統一成泛用字串——使用者與第一線支援都看不到原因')
        self.assertIn(
            self.tpl_limited.name, rejected.get('error') or '',
            'ValidationError 原文（含 template 名稱與上限）應原樣回傳（回 %r）'
            % rejected)

        # (b) 核心：被拒絕的那筆不得留在 DB
        self.assertFalse(
            self._rows(self.tpl_limited, 'svc-520-rejected'),
            '被上限守衛拒絕的 CloudService 不得留在 DB（守衛靠 rollback 撤回）')
        self.assertEqual(
            len(self._rows(self.tpl_limited)), 1,
            'template 名下應僅存第一筆；殘留列會被 cron 撿去部署（#520 症狀）')

    # ================================================================
    #  (c)：被拒絕的建立不得觸發 helm install
    # ================================================================
    def test_rejected_create_is_never_deployed_by_cron(self):
        self.assertTrue(self._create(self.tpl_limited, 'svc-520-ok').get('success'))
        svc1 = self._rows(self.tpl_limited, 'svc-520-ok')
        self.assertEqual(len(svc1), 1)

        self._create(self.tpl_limited, 'svc-520-rejected')

        client = MagicMock()
        answer_quota_reads(client)
        client.install_release.return_value = {'revision': 1}
        with patch(_OPERATOR_CLIENT, return_value=client):
            # cron 一次處理一筆並 re-trigger；掃到沒有 pending 為止。
            for _ in range(5):
                if not self.CloudService.search_count([('state', '=', 'pending')]):
                    break
                self.CloudService._cron_process_pending_deploys()

        installed = {
            call.kwargs['release_name']
            for call in client.install_release.call_args_list
        }
        # 陽性對照②：合法的 svc1 確實被 install——證明 mock 接得上、cron 真的跑了。
        self.assertIn(
            svc1.helm_release_name, installed,
            '合法服務應被 cron 部署；此條不綠代表探針失效，下面的「沒有第二個」'
            '就只是假綠')
        # (c) 被拒絕的那筆從不存在 → 不該有第二個 release 被裝起來
        self.assertEqual(
            installed, {svc1.helm_release_name},
            '被拒絕的建立不得產生 helm install（#520 stg 實測：cron 撿到殘留的 '
            'pending 列 → helm install → Running，且無訂閱、無 admin 密碼）')

    # ================================================================
    #  INSERT 之後才 raise 的業務例外，一樣要整段撤回
    # ================================================================
    def test_business_error_after_insert_rolls_back(self):
        """create() 已成功 INSERT，之後 `_create_and_confirm_subscription`
        raise UserError（該 plan 沒有訂閱商品）。

        rollback 若只涵蓋 `create()` 內部那一步，這筆就會以「無訂閱的 pending 服務」
        殘留——正是 #520 在 stg 造出的那種孤兒（佔 K8s 資源、配發網址、不計費）。
        """
        result = self._create(
            self.tpl_open, 'svc-520-no-product', plan=self.plan_no_product)

        self.assertFalse(result.get('success'), '無訂閱商品應失敗（回 %r）' % result)
        self.assertNotEqual(result.get('error'), _GENERIC_ERROR,
                            'UserError 屬業務規則例外，訊息應原文回傳')
        # 綁「訊息指得出是哪個 plan」而非措辭本身：確認失敗真的來自注入的
        # 無商品 plan，而不是別的前置條件（例如 journal 未設）順手把測試染綠。
        self.assertIn(
            self.plan_no_product.name, result.get('error') or '',
            '應是注入的「無訂閱商品」那條路徑失敗（回 %r）' % result)
        self.assertFalse(
            self._rows(self.tpl_open, 'svc-520-no-product'),
            'create 之後才失敗的請求，已 INSERT 的那一列一樣不得留下')

    # ================================================================
    #  AccessError：繼承 UserError，但不得被當成「業務規則訊息」原文吐出
    # ================================================================
    def test_access_error_does_not_leak_internal_names(self):
        """`AccessError` 是 `UserError` 的子類——單寫 `except UserError` 會連它
        一起原文回前端，把 Odoo 內部 model／欄位技術名吐到租戶畫面上。

        權限錯誤不是「講給租戶聽」的業務規則，走泛用訊息（細節留 server log）。
        這條鎖住那個分支：刪掉它，訊息就會開始外洩。
        """
        internal = 'woow_paas_platform.cloud_service'
        with patch.object(
            SubscriptionLifecycleMixin, '_create_and_confirm_subscription',
            side_effect=AccessError('您無權存取 %s 記錄' % internal),
        ):
            result = self._create(self.tpl_open, 'svc-520-denied',
                                  plan=self.plan_no_product)

        self.assertFalse(result.get('success'), result)
        self.assertEqual(result.get('error'), _GENERIC_ERROR,
                         'AccessError 不得被當成業務規則訊息原文回傳')
        self.assertNotIn(internal, result.get('error') or '',
                         'Odoo 內部 model 名不得出現在租戶前台')
        self.assertFalse(
            self._rows(self.tpl_open, 'svc-520-denied'),
            'AccessError 一樣要撤回已 INSERT 的那一列')

    # ================================================================
    #  未預期例外：訊息維持泛用（不外洩），但資料一樣不得殘留
    # ================================================================
    def test_unexpected_error_stays_opaque_but_still_rolls_back(self):
        secret = 'psycopg2 connection string s3cr3t'
        with patch.object(
            SubscriptionLifecycleMixin, '_create_and_confirm_subscription',
            side_effect=RuntimeError(secret),
        ):
            result = self._create(self.tpl_open, 'svc-520-boom',
                                  plan=self.plan_no_product)

        self.assertFalse(result.get('success'), result)
        self.assertEqual(
            result.get('error'), _GENERIC_ERROR,
            '未預期例外維持泛用訊息，內部細節只留在 server log')
        self.assertNotIn(secret, result.get('error') or '',
                         '內部細節不得外洩到前端')
        self.assertFalse(
            self._rows(self.tpl_open, 'svc-520-boom'),
            '未預期例外同樣不得留下半成品服務')

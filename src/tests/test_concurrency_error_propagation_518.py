# -*- coding: utf-8 -*-
"""#518：並發類 DB 例外必須傳出 controller，不能被 `except` 鏈吃掉。

#520 已經把「被拒絕的建立不得留下資料」修好了（`request.env.cr.rollback()` ＋
三段 except），本檔**不重複那件事**——`test_create_service_validation_rollback_520.py`
是那條的權威守線。

本檔守的是 #520 沒有涵蓋的那一半：`psycopg2.errors.SerializationFailure` 的 MRO 是
`SerializationFailure → TransactionRollbackError → OperationalError → DatabaseError
→ Error → Exception`——**它是 `Exception` 子類**，會落進 #520 最後那個
`except Exception` 分支，被 rollback ＋ 泛用訊息收掉。而 Odoo `retrying()` 的攔截
（`except (IntegrityError, OperationalError)`）在 controller 的 except 鏈**外層**，
一旦被先捕獲就永遠不會以新 tx／新 snapshot 重試。

這對 #501 的全域實例上限是致命的：Odoo 的 cursor 是 **REPEATABLE READ**
（`odoo/sql_db.py` 的 `set_isolation_level(ISOLATION_LEVEL_REPEATABLE_READ)`），
「取鎖後重新計數」拿不到新 snapshot，上限只能靠「寫衝突列 → 並發第二者拿 40001
→ retrying 以新 snapshot 重試」成立。重試這一環被吃掉，上限在並發下就等於沒有。

鑑別力：修前 controller 回 `{'success': False, ...}`（HTTP 200、JSON-RPC `result`）；
修後例外傳到框架，回的是 JSON-RPC `error`。兩者可區分。

⚠️ **本檔刻意不斷言「Odoo 重試了幾次」**。手動建構的 psycopg2 例外 `pgcode` 是
`None`（且唯讀），而 `retrying()` 在重試分支會呼叫 `errorcodes.lookup(exc.pgcode)`
⇒ 合成例外會讓它在第一輪就 `KeyError`，根本驅動不到真正的重試路徑。用不忠實的
mock 去斷言框架行為只會得到一條誤導的守線。**重試與重試後的計數正確性由 PR 內的
外部真並發腳本證明**（同一堆疊、同一端點、cap=1、5 個同時下單：停用衝突列落 5 列
＝超賣 4 台，啟用後落 1 列）。
"""
import json

from odoo.tests.common import HttpCase, tagged

MOD = 'woow_paas_platform'


@tagged('post_install', '-at_install')
class TestConcurrencyErrorReachesRetrying(HttpCase):

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        admin = self.env.ref('base.user_admin')
        self.tpl = self.env['%s.cloud_app_template' % MOD].create({
            'name': 'Concurrency Fixture 518',
            'slug': 'concurrency-fixture-518',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.invalid/concurrency-518',
            'helm_chart_version': '',
        })
        self.plan = self.env['sh.subscription.plan'].create({
            'name': 'Concurrency 518',
            'sh_duration': 1, 'sh_unit': 'month', 'sh_plan_price': 100,
            'service_type': 'cloud_service',
            # NOT NULL 無 default——不給會撞 IntegrityError，測試就變成在測 schema。
            'sh_no_of_billing_cycle': -1,
        })
        self.tpl.subscription_plan_ids = [(6, 0, [self.plan.id])]
        self.ws = self.env['%s.workspace' % MOD].create({
            'name': 'WS 518', 'owner_id': admin.id})

    def test_serialization_failure_is_not_swallowed(self):
        from unittest.mock import patch
        from psycopg2.errors import SerializationFailure

        Model = type(self.env['%s.cloud_service' % MOD])

        def exploding_create(self, vals_list):
            raise SerializationFailure(
                'could not serialize access due to concurrent update')

        with patch.object(Model, 'create', exploding_create):
            resp = self.url_open(
                '/api/workspaces/%s/services' % self.ws.id,
                data=json.dumps({
                    'jsonrpc': '2.0', 'method': 'call', 'id': 518,
                    'params': {
                        'action': 'create', 'template_id': self.tpl.id,
                        'name': 'svc-518', 'plan_id': self.plan.id,
                    },
                }), headers={'Content-Type': 'application/json'})
        self.assertNotIn(
            'result', resp.json(),
            '並發例外被 controller 吞掉並轉成一般業務失敗 ⇒ retrying() 沒有機會'
            '以新 snapshot 重試，全域上限守衛在並發下形同虛設（#518）。回應：%s'
            % resp.text[:200])

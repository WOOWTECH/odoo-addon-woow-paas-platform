"""#864 PR-E — double-charge 冷卻閘的真 cron tick 探針。

`_double_charge_cooldown_active` ⛔ 不能用一般 ORM ``search``——Odoo cron 是
REPEATABLE READ，本 tick 的快照凍結在起跑那刻，而
``woow.billing.incident._log()`` 是用**獨立 cursor**在同一輪期間 commit 的，
一般 ORM search 在同一個 tick 內讀不到它。這一格從未被真的跑過，本檔沿
``test_billing_incident_real_rollback_864.py`` 的 BaseCase／真 registry
cursor 骨架，用真的 ``sh.subscription.subscription._sh_create_auto_invoice``
cron 入口跑：

1. **同一個 tick** 兩筆同 partner 同日到期訂閱：第一筆已扣/未入帳落 incident，
   第二筆呼叫 ``_send_payment_request`` 的次數必須是 0（總外呼＝1）——證明
   讀的是獨立 cursor，不是被本 tick 快照凍結的 ORM search。
2. **兩個 cron tick**：tick1 產生 incident，tick2（未 resolved）仍不重扣。
3. resolved／逾窗／不同 partner：動作側必須真的呼叫外部（正向對照，證明
   擋下的是重複而不是整個充值路徑被誤殺）。
4. partner 換 token、以及 incident 的 payment_token_ref 為 NULL：冷卻閘仍要
   擋下——證明判準是 partner-only，不吃 token 綁定。

執行：

    odoo -u woow_paas_platform --test-enable \\
      --test-tags double_charge_cooldown_864 --stop-after-init ...

本 tag 刻意 ``-standard``；PR-E 不改 CI workflow，須在 sandbox 明確選 tag。
"""
import logging
import uuid
from datetime import date, datetime, timedelta
from unittest.mock import patch

import odoo
from odoo import api, fields
from odoo.modules.registry import Registry
from odoo.tests.common import BaseCase, get_db_name, tagged

_logger = logging.getLogger(__name__)
_SUB_MODEL = 'sh.subscription.subscription'
_INCIDENT_MODEL = 'woow.billing.incident'


@tagged(
    'post_install', '-at_install', '-standard', 'double_charge_cooldown_864',
)
class TestDoubleChargeCooldown864(BaseCase):

    def setUp(self):
        super().setUp()
        self.registry = Registry(get_db_name())
        self.marker = 'b864cd%s' % uuid.uuid4().hex[:12]
        self.addCleanup(self._purge)
        self._patchers = []

        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            provider = env['payment.provider'].sudo().search(
                [('code', '=', 'ECPay')], limit=1,
            )
            if not provider:
                self.skipTest("ECPay provider not configured in this DB")
            provider.write({
                'enable_ecpg': True,
                'MerchantID': '3002607',
                'HashKey': 'pwFHCqoQZGmho4w6',
                'HashIV': 'EkRm7iFT261dpevs',
                'state': 'test',
            })
            self.provider_id = provider.id
            self.payment_method_id = (
                env.ref('payment.payment_method_card',
                         raise_if_not_found=False)
                or env.ref('payment.payment_method_unknown')
            ).id
            # 真 plan/product：讓 _generate_invoice_vals 產出有金額的發票，
            # 否則 amount=0 → _try_settle_invoice 的 wallet_balance>=0 分支
            # 直接判定「wallet 夠付」、根本不會走到 _attempt_auto_topup ⇒
            # `_send_payment_request` 永遠不會被呼叫（鏡像
            # test_wallet_cron_neutralized.py 的 `_WalletCronFixture` 設定）。
            plan = env['sh.subscription.plan'].create({
                'name': '864-e cooldown plan', 'sh_plan_price': 100.0,
                'sh_duration': 1, 'sh_unit': 'month',
                'sh_no_of_billing_cycle': -1, 'sh_trial': False,
            })
            product_tmpl = env['product.template'].sudo().create({
                'name': '864-e cooldown product', 'type': 'service',
                'sh_product_subscribe': True,
                'sh_subscription_plan_id': plan.id,
                'list_price': 100.0,
            })
            self.plan_id = plan.id
            self.product_id = product_tmpl.product_variant_id.id
            cr.commit()

    def tearDown(self):
        for patcher in self._patchers:
            patcher.stop()
        super().tearDown()

    def _purge(self):
        """ORM unlink in dependency order — cron ticks post/create invoices,
        topups and transactions for the marker partners, so a raw SQL
        ``DELETE FROM res_partner`` (without first clearing those referencing
        rows) hits ``account_move_partner_id_fkey`` / topup's
        ``ondelete='restrict'`` on ``payment_transaction_id``."""
        try:
            with self.registry.cursor() as cr:
                cr.execute(
                    "DELETE FROM woow_billing_incident WHERE external_ref "
                    "LIKE %s", ('%' + self.marker + '%',),
                )
                cr.commit()
                env = api.Environment(cr, odoo.SUPERUSER_ID, {})
                partners = env['res.partner'].sudo().search(
                    [('name', 'like', self.marker)])
                if not partners:
                    return
                env['sh.subscription.subscription'].sudo().search(
                    [('sh_partner_id', 'in', partners.ids)]).unlink()
                env['woow.wallet.topup'].sudo().search(
                    [('partner_id', 'in', partners.ids)]).unlink()
                env['payment.transaction'].sudo().search(
                    [('partner_id', 'in', partners.ids)]).unlink()
                # active_test=False：token-swap 情境會 deactivate 舊 token，
                # 預設 search 的 active_test 過濾會讓它從清單漏網，留下擋住
                # partner 刪除的孤兒列。
                env['payment.token'].sudo().with_context(
                    active_test=False).search(
                    [('partner_id', 'in', partners.ids)]).unlink()
                env['account.move'].sudo().search(
                    [('partner_id', 'in', partners.ids)]).unlink()
                partners.unlink()
                cr.commit()
        except Exception:
            _logger.exception(
                '#864 double-charge cooldown fixture cleanup failed '
                '(marker=%s)', self.marker,
            )

    def _make_partner_with_token(self, env, suffix, bindcard_suffix):
        partner = env['res.partner'].create({
            'name': '%s-%s' % (self.marker, suffix),
            'email': '%s-%s@example.test' % (self.marker, suffix),
        })
        token = env['payment.token'].sudo().create({
            'provider_id': self.provider_id,
            'partner_id': partner.id,
            'payment_method_id': self.payment_method_id,
            'payment_details': '**** **** **** ' + bindcard_suffix[-4:],
            'provider_ref': bindcard_suffix,
            'ecpg_bindcard_id': bindcard_suffix,
            'ecpg_member_id': str(partner.id),
        })
        return partner, token

    def _make_due_sub(self, env, partner, price=100.0):
        today = date.today()
        return env[_SUB_MODEL].sudo().create({
            'sh_partner_id': partner.id,
            'sh_subscription_plan_id': self.plan_id,
            'product_id': self.product_id,
            'sh_unit': 'month',
            'sh_recurrency': 1,
            'sh_plan_price': price,
            'sh_qty': 1,
            'sh_start_date': today - timedelta(days=1),
            'sh_end_date': today + timedelta(days=29),
            'sh_date_of_next_payment': today,
            'state': 'in_progress',
            'sh_trial': False,
        })

    def _patch_charged_not_credited(self):
        """Patch the ECPG boundary so every real charge attempt looks like a
        CHARGED_NOT_CREDITED (ECPay accepted, our bookkeeping then raises).
        Runs against the live registry class, shared across every cursor."""
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            TxModel = type(env['payment.transaction'])

        def _set_done_then_raise(tx_self):
            tx_self.state = 'done'
            raise RuntimeError('inner callback boom (integration)')

        patcher = patch.object(
            TxModel, '_send_payment_request', autospec=True,
            side_effect=_set_done_then_raise,
        )
        mock = patcher.start()
        self._patchers.append(patcher)
        return mock

    def _run_cron_tick(self):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            env[_SUB_MODEL]._sh_create_auto_invoice()
            cr.commit()

    def _incident_count_for_partner(self, partner_id):
        with self.registry.cursor() as cr:
            cr.execute(
                "SELECT COUNT(*) FROM woow_billing_incident WHERE "
                "partner_ref = %s AND kind = 'charged_not_credited'",
                (partner_id,),
            )
            return cr.fetchone()[0]

    def _incident_ref_for_partner(self, partner_id):
        with self.registry.cursor() as cr:
            cr.execute(
                "SELECT external_ref FROM woow_billing_incident WHERE "
                "partner_ref = %s AND kind = 'charged_not_credited' "
                "ORDER BY id DESC LIMIT 1",
                (partner_id,),
            )
            row = cr.fetchone()
            return row[0] if row else None

    # --- (1) 同一個 tick，同 partner 兩筆同日到期：總外呼 == 1 -------------- #

    def test_same_tick_two_subs_same_partner_charges_only_once(self):
        mock_charge = self._patch_charged_not_credited()
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            partner, _token = self._make_partner_with_token(
                env, 'same-tick', 'AB90010001')
            self._make_due_sub(env, partner, price=100.0)
            self._make_due_sub(env, partner, price=100.0)
            partner_id = partner.id
            cr.commit()

        self._run_cron_tick()

        self.assertEqual(
            mock_charge.call_count, 1,
            '同一個 tick 兩筆同 partner 同日到期：只准真的外呼一次——第二筆 '
            '必須讀到第一筆用獨立 cursor 剛 commit 的 incident 才擋得住，'
            '若冷卻閘誤用 ORM search（被本 tick RR 快照凍結），這裡會是 2',
        )
        self.assertEqual(self._incident_count_for_partner(partner_id), 1)

    # --- (2) 兩個 cron tick：tick1 產生 incident，tick2 仍不重扣 ------------ #

    def test_two_cron_ticks_unresolved_incident_still_blocks(self):
        mock_charge = self._patch_charged_not_credited()
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            partner, _token = self._make_partner_with_token(
                env, 'two-tick', 'AB90010002')
            self._make_due_sub(env, partner, price=100.0)
            partner_id = partner.id
            cr.commit()

        self._run_cron_tick()  # tick 1：產生 incident
        self.assertEqual(mock_charge.call_count, 1)
        self.assertEqual(self._incident_count_for_partner(partner_id), 1)

        self._run_cron_tick()  # tick 2：未 resolved，不得重扣

        self.assertEqual(
            mock_charge.call_count, 1,
            'tick2 的 incident 仍未 resolved，不得再對同 partner 真的外呼',
        )
        self.assertEqual(
            self._incident_count_for_partner(partner_id), 1,
            '同一筆 incident 只累加 occurrences，不應該多出第二筆',
        )

    # --- (3a) resolved 之後，動作側必須恢復真的外呼 ------------------------- #

    def test_resolved_incident_allows_charge_again(self):
        mock_charge = self._patch_charged_not_credited()
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            partner, _token = self._make_partner_with_token(
                env, 'resolved', 'AB90010003')
            self._make_due_sub(env, partner, price=100.0)
            partner_id = partner.id
            cr.commit()

        self._run_cron_tick()
        self.assertEqual(mock_charge.call_count, 1)
        ref = self._incident_ref_for_partner(partner_id)
        self.assertTrue(ref)

        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            resolved_id = env[_INCIDENT_MODEL]._resolve(
                ref, resolution_note='integration test: manually resolved',
                resolved_by_ref=odoo.SUPERUSER_ID,
            )
            cr.commit()
        self.assertIsInstance(resolved_id, int)

        self._run_cron_tick()

        self.assertEqual(
            mock_charge.call_count, 2,
            'incident 已 resolved：下一個 tick 必須真的再外呼一次，證明擋下 '
            '的是「未 resolved 的重複」而不是整條充值路徑被誤殺',
        )

    # --- (3b) 逾冷卻窗口，動作側必須恢復真的外呼 ----------------------------- #

    def test_expired_cooldown_window_allows_charge_again(self):
        mock_charge = self._patch_charged_not_credited()
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            partner, _token = self._make_partner_with_token(
                env, 'expired-window', 'AB90010004')
            self._make_due_sub(env, partner, price=100.0)
            partner_id = partner.id
            cr.commit()

        self._run_cron_tick()
        self.assertEqual(mock_charge.call_count, 1)

        # 把 incident 的 first/last_seen_at 一起撥回 25 小時前（預設窗口 24
        # 小時之外）——只改 last_seen_at 會讓它早於 first_seen_at，違反
        # woow_billing_incident_seen_at_order 這條 DB check constraint。
        stale = datetime.utcnow() - timedelta(hours=25)
        with self.registry.cursor() as cr:
            cr.execute(
                "UPDATE woow_billing_incident SET first_seen_at = %s, "
                "last_seen_at = %s "
                "WHERE partner_ref = %s AND kind = 'charged_not_credited'",
                (stale, stale, partner_id),
            )
            cr.commit()

        self._run_cron_tick()

        self.assertEqual(
            mock_charge.call_count, 2,
            '逾冷卻窗口（預設 24 小時）之後必須恢復真的外呼',
        )

    # --- (3c) 不同 partner 不互相污染 ---------------------------------------- #

    def test_different_partner_is_not_blocked_by_unrelated_incident(self):
        mock_charge = self._patch_charged_not_credited()
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            partner_a, _token_a = self._make_partner_with_token(
                env, 'partner-a', 'AB90010005')
            partner_b, _token_b = self._make_partner_with_token(
                env, 'partner-b', 'AB90010006')
            self._make_due_sub(env, partner_a, price=100.0)
            self._make_due_sub(env, partner_b, price=100.0)
            cr.commit()

        self._run_cron_tick()

        self.assertEqual(
            mock_charge.call_count, 2,
            '不同 partner 各自到期、各自第一次外呼，互不阻擋——冷卻閘判準是 '
            'partner-only，不得誤傷同批其他 partner',
        )

    # --- (4) partner 換 token／incident 的 payment_token_ref 為 NULL 仍要擋 --- #

    def test_cooldown_blocks_regardless_of_token_binding(self):
        """🔴 partner-only：incident 沒有 token_ref（NULL）或 partner 換了新
        token，冷卻閘都要擋——判準只看 partner_ref，不吃 token 綁定（防換卡
        繞過、防 token_ref NULL 造成的洞）。"""
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            partner, old_token = self._make_partner_with_token(
                env, 'token-swap', 'AB90010007')
            partner_id = partner.id
            # 直接寫一筆 payment_token_ref=NULL 的 incident（模擬手動儲值路徑
            # 那種尚未分類到具體 token 的情境），不透過真的 charge 流程。
            env[_INCIDENT_MODEL]._log({
                'external_ref': 'test:%s-manual-null-token' % self.marker,
                'kind': 'charged_not_credited',
                'partner_ref': partner_id,
                'payment_token_ref': None,
                'amount': 50.0,
                'credits': 50.0,
                'bonus': 0.0,
                'currency': 'TWD',
                'failure_code': 'wallet_credit_failed',
                'source_model': 'woow.wallet.topup',
                'source_ref': 1,
                'detail': 'integration test synthetic incident',
            })
            # partner 換了一張新 token（原 token 停用，換發新卡）。
            old_token.active = False
            new_token = env['payment.token'].sudo().create({
                'provider_id': self.provider_id,
                'partner_id': partner_id,
                'payment_method_id': self.payment_method_id,
                'payment_details': '**** **** **** 9999',
                'provider_ref': 'AB90019999',
                'ecpg_bindcard_id': 'AB90019999',
                'ecpg_member_id': str(partner_id),
            })
            self._make_due_sub(env, partner, price=100.0)
            cr.commit()

        mock_charge = self._patch_charged_not_credited()
        self._run_cron_tick()

        self.assertEqual(
            mock_charge.call_count, 0,
            'incident 的 payment_token_ref 是 NULL、partner 又換了新 token'
            '——冷卻閘仍要擋下，證明判準是 partner-only',
        )

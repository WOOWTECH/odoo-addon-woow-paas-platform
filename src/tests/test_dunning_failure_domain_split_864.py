"""#864 PR-E — `_on_payment_failure` 拆兩個獨立失敗域。

修法前：``_on_payment_failure`` 整段沒有 try/except，而它裡面
``_notify_low_balance``（``sh_subscription_fix.py:706-729``）連呼兩次
``formatLang`` 也沒有保護。任一步拋例外 ⇒ 外層 ``_process_subscription_
billing`` 的 savepoint 整個回捲 ⇒ 連這一輪已經確定的失敗計數（甚至更早的
charge 結果）都被連坐捲掉。

修法把 dunning bookkeeping 拆成兩個各自獨立、各自用 ``self.env.cr.
savepoint()`` 包住的失敗域：

1. 失敗計數（``woow_consecutive_payment_failures`` / ``woow_last_payment_
   failure_at``）
2. 通知／cancel（低餘額信 + 逾 threshold 的 cancel + 一次性通知）

本檔驗證：domain 2 炸掉不會牽連 domain 1（counter 仍持久化）；domain 2 自己
的寫入（cancel 狀態）在它自己炸掉時會被回捲（證明是真的 nested savepoint、
不只是「例外被吞掉」）；cron 不會被單一 subscription 的 dunning bug 卡死，
下一筆照常處理。
"""
from datetime import date, timedelta
from unittest.mock import patch

from odoo.tests.common import TransactionCase


class TestDunningFailureDomainSplit864(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Sub = cls.env['sh.subscription.subscription']
        cls.Partner = cls.env['res.partner']

    def _make_partner(self, name, balance=0.0):
        return self.Partner.create({
            'name': name,
            'email': f'{name.lower().replace(" ", "_")}@example.test',
            'wallet_balance': balance,
        })

    def _make_sub_record(self, partner, price=100.0):
        today = date.today()
        return self.Sub.sudo().create({
            'sh_partner_id': partner.id,
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

    def _set_threshold(self, value):
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.max_consecutive_payment_failures', str(value),
        )

    def _patch_settle(self, succeed: bool):
        return patch.object(
            type(self.Sub), '_try_settle_invoice',
            lambda self, subscription, invoice: succeed,
        )

    def _run_cron(self):
        self.Sub._sh_create_auto_invoice()

    # --- Domain 1 (counter) 必須撐過 domain 2（通知）的例外 ---------------- #

    def test_notify_failure_does_not_roll_back_failure_counter(self):
        """formatLang 等處拋錯的 domain 2 例外，不得回捲 domain 1 的失敗計數。"""
        partner = self._make_partner("DomainSplitA")
        sub = self._make_sub_record(partner)

        with self._patch_settle(succeed=False), \
                patch.object(type(self.Sub), '_notify_low_balance',
                             side_effect=RuntimeError('formatLang boom')), \
                self.assertLogs(
                    'odoo.addons.woow_paas_platform.models.sh_subscription_fix',
                    level='ERROR') as log_cm:
            self._run_cron()
        sub.invalidate_recordset()

        self.assertEqual(
            sub.woow_consecutive_payment_failures, 1,
            'domain 2（通知）炸掉不得牽連 domain 1（失敗計數）',
        )
        self.assertIsNotNone(sub.woow_last_payment_failure_at)
        self.assertTrue(
            any('dunning notify/cancel bookkeeping failed' in msg
                for msg in log_cm.output),
            'domain 2 的例外必須被記成 fail-soft warning，而不是靜默吞掉',
        )

    # --- Domain 2 自己的寫入在自己炸掉時要被回捲（證明是真 savepoint） ------ #

    def test_cancel_failure_rolls_back_cancel_state_but_not_counter(self):
        """cancel 分支炸掉：cancel 狀態本身要回捲，但失敗計數仍要保留。"""
        self._set_threshold(1)
        partner = self._make_partner("DomainSplitB")
        sub = self._make_sub_record(partner)

        with self._patch_settle(succeed=False), \
                patch.object(type(self.Sub), '_notify_subscription_cancelled',
                             side_effect=RuntimeError('cancel email boom')):
            self._run_cron()
        sub.invalidate_recordset()

        self.assertEqual(
            sub.woow_consecutive_payment_failures, 1,
            'domain 1（失敗計數）必須撐過 domain 2 的例外',
        )
        self.assertEqual(
            sub.state, 'in_progress',
            '🔴 cancel 狀態寫在跟通知同一個 domain 2 savepoint 內——通知炸掉時 '
            '必須連同 cancel 狀態一起回捲（證明真的是 nested savepoint，不是 '
            '單純吞例外）',
        )
        self.assertFalse(sub.woow_cancellation_notified)

    # --- cron 不因單一 subscription 的 dunning bug 卡死 --------------------- #

    def test_cron_continues_to_next_subscription_after_dunning_failure(self):
        """一筆 subscription 的 dunning bookkeeping 炸掉，不得拖累同批其他筆。"""
        partner_a = self._make_partner("DomainSplitC-A")
        partner_b = self._make_partner("DomainSplitC-B")
        sub_a = self._make_sub_record(partner_a)
        sub_b = self._make_sub_record(partner_b)

        original_notify = type(self.Sub)._notify_low_balance

        def _boom_only_for_a(self, partner, invoice):
            if partner.id == partner_a.id:
                raise RuntimeError('formatLang boom for A only')
            return original_notify(self, partner, invoice)

        with self._patch_settle(succeed=False), \
                patch.object(type(self.Sub), '_notify_low_balance',
                             _boom_only_for_a):
            self._run_cron()
        sub_a.invalidate_recordset()
        sub_b.invalidate_recordset()

        self.assertEqual(
            sub_a.woow_consecutive_payment_failures, 1,
            '出問題那筆自己的失敗計數仍要保留',
        )
        self.assertEqual(
            sub_b.woow_consecutive_payment_failures, 1,
            '同批另一筆必須正常處理，不因前一筆的 dunning bug 被拖累',
        )

    # --- 對照組：正常路徑（無例外）行為不變 -------------------------------- #

    def test_normal_path_unaffected_by_domain_split(self):
        """無例外時，拆分後的行為要跟拆分前完全一致（既有 T19a 的簡化重跑）。"""
        partner = self._make_partner("DomainSplitD")
        sub = self._make_sub_record(partner)
        with self._patch_settle(succeed=False):
            self._run_cron()
        sub.invalidate_recordset()
        self.assertEqual(sub.woow_consecutive_payment_failures, 1)
        self.assertEqual(sub.state, 'in_progress')

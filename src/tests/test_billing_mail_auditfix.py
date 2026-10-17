"""商用驗收 AUDITFIX：扣款／取消／付款成功通知信（三個缺陷一起測）。

## dunning-mail-english（扣款失敗、取消、付款成功的通知信對 zh_TW 租戶是英文）

* ``mail_template_low_balance``、``mail_template_subscription_cancelled`` 沒有 ``lang``，
  zh_TW.po 的 subject／body 譯文是空的 ⇒ 一律英文。
* 付款成功信是上游 ``sh_subscription.sh_subscription_customer_email``，沒有 ``lang``。

## payment-mail-wrong-date（付款成功信寫的到期日是過去日期）

上游那封寫「Your plan will expire on <sh_end_date>」；平台訂閱的 ``sh_end_date`` 是第一期
最後一天、續期不推進（#1574），付款成功信因此寫著過去的日期。

## dunning-lost-on-smtp-failure（SMTP 故障期間取消通知永久遺失）

``_on_payment_failure`` 在 ``_notify_subscription_cancelled`` 之後無條件蓋
``woow_cancellation_notified=True``；信是 ``force_send=False`` 排進佇列，SMTP 421 時停在
``exception``，Odoo 不會重試，取消後的訂閱也不再進扣款迴圈 ⇒ 永久遺失。
"""
import smtplib
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

_THRESHOLD = 6


class _BillingMailFixture(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env['res.lang']._activate_lang('zh_TW')
        cls.today = fields.Date.today()

    def _partner(self, name, lang):
        return self.env['res.partner'].create({
            'name': name,
            'email': '%s@example.test' % name.lower().replace(' ', '_'),
            'lang': lang,
            'wallet_balance': 0.0,
        })

    def _plan(self, never_expire=True):
        return self.env['sh.subscription.plan'].create({
            'name': 'Auditfix Mail Plan %s' % ('NE' if never_expire else 'fixed'),
            'sh_plan_price': 500.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
            'sh_never_expire': never_expire,
        })

    def _subscription(self, partner, never_expire=True):
        plan = self._plan(never_expire)
        product_tmpl = self.env['product.template'].create({
            'name': 'Auditfix Mail Product',
            'type': 'service',
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': plan.id,
            'list_price': plan.sh_plan_price,
        })
        sub = self.env['sh.subscription.subscription'].create({
            'sh_partner_id': partner.id,
            'product_id': product_tmpl.product_variant_id.id,
            'sh_subscription_plan_id': plan.id,
        })
        sub.write({
            'state': 'in_progress',
            'sh_plan_price': 500.0,
            'sh_start_date': self.today - timedelta(days=60),
            # 平台訂閱的 sh_end_date＝第一期最後一天，之後不推進（#1574）＝過去的日期。
            'sh_end_date': self.today - timedelta(days=31),
            'sh_date_of_next_payment': self.today + timedelta(days=30),
        })
        return sub

    def _invoice(self, partner):
        return self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'Auditfix mail fee', 'quantity': 1, 'price_unit': 500.0})],
        })


@tagged('post_install', '-at_install')
class TestBillingMailLanguageAuditfix(_BillingMailFixture):
    """dunning-mail-english：三種信依收件人語言渲染。"""

    def test_low_balance_mail_follows_the_tenant_language(self):
        Sub = self.env['sh.subscription.subscription']
        zh = self._partner('Low Bal ZH', 'zh_TW')
        en = self._partner('Low Bal EN', 'en_US')

        zh_mail = Sub._notify_low_balance(zh, self._invoice(zh))
        en_mail = Sub._notify_low_balance(en, self._invoice(en))

        self.assertEqual(zh_mail.subject, '需要處理：請為錢包儲值')
        self.assertIn('錢包餘額不足', zh_mail.body_html)
        self.assertNotIn('could not be paid', zh_mail.body_html)
        self.assertEqual(en_mail.subject, 'Action needed: Please top up your wallet')
        self.assertIn('could not be paid', en_mail.body_html)

    def test_cancellation_mail_follows_the_tenant_language(self):
        Sub = self.env['sh.subscription.subscription']
        zh = self._partner('Cancel ZH', 'zh_TW')
        sub = self._subscription(zh)

        mail = Sub._notify_subscription_cancelled(sub, self._invoice(zh))

        self.assertTrue(mail, '取消通知要回傳排進佇列的 mail.mail')
        self.assertEqual(mail.subject, '[Woow PaaS] 您的訂閱已取消')
        self.assertIn('自動取消', mail.body_html)
        self.assertNotIn('auto-cancelled', mail.body_html)

    def test_existing_templates_get_the_lang_backfilled_without_overwriting(self):
        """noupdate 模板在既有 DB 吃不到 XML 的 lang：每次 -u 由 healer 補空值。"""
        low = self.env.ref('woow_paas_platform.mail_template_low_balance')
        cancelled = self.env.ref('woow_paas_platform.mail_template_subscription_cancelled')
        low.write({'lang': False})
        cancelled.write({'lang': '{{ object.lang or "zh_TW" }}'})

        self.env['sh.subscription.subscription']._woow_ensure_billing_mail_lang()

        self.assertEqual(low.lang, '{{ object.lang }}')
        self.assertEqual(cancelled.lang, '{{ object.lang or "zh_TW" }}', '營運手改的值不覆蓋')

    def _status_mail(self, sub):
        return self.env['mail.mail'].search([
            ('model', '=', 'sh.subscription.subscription'), ('res_id', '=', sub.id),
        ], order='id desc', limit=1)

    def test_payment_mail_shows_next_charge_date_in_the_tenant_language(self):
        """payment-mail-wrong-date：付款成功信寫下次扣款日，不寫已經過去的 sh_end_date。"""
        zh = self._partner('Paid ZH', 'zh_TW')
        sub = self._subscription(zh)
        attachment = self.env['ir.attachment'].create({
            'name': 'Subscription Invoice', 'raw': b'%PDF-1.4',
            'res_model': 'sh.subscription.subscription', 'res_id': sub.id})
        lang_env = self.env(context=dict(self.env.context, lang='zh_TW'))
        next_charge = lang_env['ir.qweb.field.date'].value_to_html(
            sub.sh_date_of_next_payment, {})
        stale_end = lang_env['ir.qweb.field.date'].value_to_html(sub.sh_end_date, {})

        sub._sh_send_subscription_email(attachment)

        mail = self._status_mail(sub)
        self.assertTrue(mail, '平台訂閱要寄平台的狀態信')
        self.assertIn('您的訂閱已啟用', mail.subject)
        self.assertIn('下次扣款日', mail.body_html)
        self.assertIn(next_charge, mail.body_html)
        self.assertNotIn(stale_end, mail.body_html, '不得寫出已經過去的 sh_end_date')
        self.assertNotIn('expire', mail.body_html)
        self.assertIn(attachment, mail.attachment_ids)

    def test_non_platform_subscription_keeps_the_upstream_mail(self):
        """ERP／官網的固定期數方案不受影響（與 #1574 同一個判準）。"""
        en = self._partner('ERP EN', 'en_US')
        sub = self._subscription(en, never_expire=False)

        sub._sh_send_subscription_email(False)

        mail = self._status_mail(sub)
        self.assertTrue(mail)
        self.assertIn('Your Subscription (Ref', mail.subject)


@tagged('post_install', '-at_install')
class TestCancellationNoticeRetryAuditfix(_BillingMailFixture):
    """dunning-lost-on-smtp-failure：取消通知只有排進佇列才算已通知；投遞失敗由每日排程補送。"""

    def setUp(self):
        super().setUp()
        self.Sub = self.env['sh.subscription.subscription']
        self.partner = self._partner('Cancel Retry', 'zh_TW')
        self.sub = self._subscription(self.partner)
        self.sub.write({
            'sh_date_of_next_payment': self.today,
            'woow_consecutive_payment_failures': _THRESHOLD - 1,
        })
        self.invoice = self._invoice(self.partner)

    def _auto_cancel(self):
        self.Sub._on_payment_failure(self.sub, self.invoice, self.today, _THRESHOLD)
        self.assertEqual(self.sub.state, 'cancel')

    def _cancel_mails(self):
        template = self.env.ref('woow_paas_platform.mail_template_subscription_cancelled')
        return self.env['mail.mail'].search([
            ('mail_message_id.model', '=', 'res.partner'),
            ('mail_message_id.res_id', '=', self.partner.id),
            ('subject', '=', template.with_context(lang='zh_TW').subject),
        ])

    def _deliver_with_smtp_421(self, mail):
        """真的跑一次投遞，SMTP 回 421（服務暫時不可用）。"""
        IrMailServer = type(self.env['ir.mail_server'])
        with patch.object(IrMailServer, 'connect', return_value=MagicMock()), \
                patch.object(IrMailServer, 'send_email', side_effect=smtplib.SMTPResponseException(
                    421, b'4.3.2 Service not available, closing transmission channel')):
            mail.send(raise_exception=False)
        self.assertEqual(mail.state, 'exception', '前提：421 讓信停在 exception')

    def test_smtp_421_notice_is_resent_by_the_next_cron(self):
        self._auto_cancel()
        first = self.sub.woow_cancellation_mail_id
        self.assertTrue(first, '要記下取消通知的載體')
        self.assertFalse(first.auto_delete, '載體要留下來才分得出有沒有送到')
        self._deliver_with_smtp_421(first)

        self.Sub._retry_cancellation_notices()

        second = self.sub.woow_cancellation_mail_id
        self.assertTrue(second and second != first, '投遞失敗的取消通知要補送一封新的')
        self.assertEqual(second.state, 'outgoing')
        self.assertTrue(self.sub.woow_cancellation_notified)

    def test_queue_failure_is_not_marked_notified_and_is_retried(self):
        template_cls = type(self.env['mail.template'])
        with patch.object(template_cls, 'send_mail', side_effect=RuntimeError('queue down')):
            self._auto_cancel()
        self.assertFalse(self.sub.woow_cancellation_notified, '沒排進佇列不得標成已通知')

        self.Sub._retry_cancellation_notices()

        self.assertTrue(self.sub.woow_cancellation_notified)
        self.assertTrue(self.sub.woow_cancellation_mail_id)

    def test_delivered_or_cancelled_notice_is_not_resent(self):
        self._auto_cancel()
        mail = self.sub.woow_cancellation_mail_id
        for state in ('sent', 'cancel', 'outgoing'):
            with self.subTest(state=state):
                mail.write({'state': state})
                self.Sub._retry_cancellation_notices()
                self.assertEqual(self.sub.woow_cancellation_mail_id, mail)

    def test_manual_cancellation_and_old_failures_are_left_alone(self):
        # 人工／租戶取消：沒有 woow_auto_cancelled_at ⇒ 不寄「自動取消」信。
        self.sub.write({'state': 'cancel'})
        self.Sub._retry_cancellation_notices()
        self.assertFalse(self.sub.woow_cancellation_mail_id)
        # 超過補送視窗：不再追送。
        self.sub.write({'state': 'in_progress'})
        self._auto_cancel()
        mail = self.sub.woow_cancellation_mail_id
        mail.write({'state': 'exception'})
        self.sub.write({
            'woow_auto_cancelled_at': fields.Datetime.now() - timedelta(days=10)})
        self.Sub._retry_cancellation_notices()
        self.assertEqual(self.sub.woow_cancellation_mail_id, mail)

    def test_daily_cron_runs_the_retry(self):
        self._auto_cancel()
        first = self.sub.woow_cancellation_mail_id
        first.write({'state': 'exception'})

        self.Sub._sh_create_auto_invoice()

        self.assertNotEqual(self.sub.woow_cancellation_mail_id, first)

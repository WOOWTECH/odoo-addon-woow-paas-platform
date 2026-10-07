"""#1574：上游每日到期 cron 不得把 never-expire 方案的平台訂閱轉 expire。

上游 ``_sh_auto_expired_subscription`` 對所有 ``in_progress`` 且 ``sh_end_date == today``
的訂閱轉 ``expire``，不檢查 ``sh_never_expire``；平台所有方案都是 never-expire，卻會
寫 ``sh_end_date``（第一期最後一天）且續期不推進它 ⇒ 每張平台訂閱都在第一期的
``sh_end_date`` 當天被轉 expire、不再續扣、寬限期後停權（沙盒以平台正規建立路徑重現）。

修法：平台 override 跳過 never-expire 方案；其他訂閱照上游行為（含「已生效子訂閱 ⇒
renewed」）。``test_refund_window_next_payment.TestAutoExpireInteraction``（沒有方案的
訂閱）繼續釘住上游的 ``==`` 行為。
"""
from datetime import date, timedelta
from unittest.mock import patch

from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

from ..models.cloud_service import _internal_provision


def _today(day):
    return patch.object(fields.Date, 'today', staticmethod(lambda *args: day))


@tagged('post_install', '-at_install')
class TestAutoExpireSkipsNeverExpire1574(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Sub = self.env['sh.subscription.subscription'].sudo()
        self.partner = self.env['res.partner'].create({'name': 'AutoExpire 1574'})
        self.never = self.env['sh.subscription.plan'].sudo().create({
            'name': 'Plan 1574 never-expire', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 100.0, 'sh_no_of_billing_cycle': -1,
            'sh_never_expire': True, 'sh_trial': False})
        self.fixed = self.env['sh.subscription.plan'].sudo().create({
            'name': 'Plan 1574 fixed-term', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 100.0, 'sh_no_of_billing_cycle': 3,
            'sh_never_expire': False, 'sh_trial': False})
        patcher = patch.object(type(self.Sub), '_sh_send_subscription_email', return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _sub(self, plan, end):
        return self.Sub.create({
            'sh_partner_id': self.partner.id,
            'sh_subscription_plan_id': plan.id if plan else False,
            'sh_plan_price': 100.0, 'sh_unit': 'month', 'sh_recurrency': 1,
            'sh_start_date': end - relativedelta(months=1) + timedelta(days=1),
            'sh_end_date': end,
            'sh_date_of_next_payment': end + timedelta(days=1),
            'state': 'in_progress',
        })

    def _run(self):
        self.Sub._sh_auto_expired_subscription()
        self.Sub.invalidate_model()

    def test_never_expire_plan_is_not_expired_on_its_end_date(self):
        sub = self._sub(self.never, date.today())
        self._run()
        self.assertEqual(sub.state, 'in_progress')

    def test_fixed_term_plan_still_expires(self):
        sub = self._sub(self.fixed, date.today())
        self._run()
        self.assertEqual(sub.state, 'expire')

    def test_fixed_term_with_a_confirmed_child_is_renewed(self):
        sub = self._sub(self.fixed, date.today())
        child = self._sub(self.fixed, date.today() + relativedelta(months=1))
        child.write({'sh_subscription_id': sub.id})
        self._run()
        self.assertEqual(sub.state, 'renewed')
        self.assertEqual(child.state, 'in_progress')

    def test_other_days_are_untouched(self):
        sub = self._sub(self.fixed, date.today() + timedelta(days=1))
        self._run()
        self.assertEqual(sub.state, 'in_progress')

    def test_platform_created_subscription_survives_its_first_end_date(self):
        """正規建立路徑（_create_and_confirm_subscription）＋seed 的 never-expire 方案。"""
        admin = self.env.ref('base.user_admin')
        self.env['res.company'].sudo()._ensure_subscription_journal()
        workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 1574', 'owner_id': admin.id})
        template = self.env['woow_paas_platform.cloud_app_template'].sudo().create({
            'name': 'App 1574', 'slug': 'app-1574-auto-expire', 'helm_repo_url': '',
            'helm_chart_name': 'oci://example.invalid/app-1574',
            'helm_chart_version': '0.0.0'})
        service = _internal_provision(
            self.env['woow_paas_platform.cloud_service'].sudo()).create({
                'name': 'Svc 1574', 'workspace_id': workspace.id,
                'template_id': template.id})
        plan = self.env.ref('woow_paas_platform.plan_ha_paas_monthly')
        self.assertTrue(plan.sh_never_expire, '前提：seed 方案是 never-expire')
        sub = service._create_and_confirm_subscription(admin.partner_id, plan)
        self.assertEqual(sub.state, 'in_progress')
        self.assertTrue(sub.sh_end_date, '前提：平台會寫 sh_end_date（第一期最後一天）')
        with _today(sub.sh_end_date):
            self._run()
        self.assertEqual(sub.state, 'in_progress',
                         'never-expire 的平台訂閱不得在第一期最後一天被上游 cron 轉 expire')

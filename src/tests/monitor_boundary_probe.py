"""D05/D06 的 framework notification boundary probe；真 activity/mail/PG，不 fake monitor outcome。"""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from psycopg2 import OperationalError
from odoo import fields

from .committed_cron_scope import MONITOR, POLL_CRON


def prepare_system_recipients(case, env):
    """只建立 own system users，不修改既有 admin／company／cron 預設。"""
    users = env['res.users'].with_context(no_reset_password=True).create([
        {'name': f'1159 boundary recipient {suffix}', 'login': f'1159-boundary-{suffix}-{case.tag}',
         'email': f'1159-boundary-{suffix}-{case.tag}@example.invalid', 'notification_type': 'email',
         'groups_id': [(6, 0, [env.ref('base.group_system').id])]}
        for suffix in ('a', 'b')])
    recipients = env.ref('base.group_system').users.filtered(lambda user: user.active and not user.share)
    case.assertGreaterEqual(len(recipients), 2)
    case.assertNotEqual(recipients[0], users[-1], '排程者不能就是首位收件者')
    return env[MONITOR].with_user(users[-1]).sudo()


@contextmanager
def notification_fault(case, summary, fault='40001', before_fault=None):
    """首位真建立／排 queue 後，第二位拋 PG40001 或 RuntimeError；send 是外部 double。"""
    activity_model = type(case.env[MONITOR])
    mail_model = type(case.env['mail.mail'])
    original_schedule = activity_model.activity_schedule
    evidence = SimpleNamespace(calls=[], activities=[], mails=[], pgcodes=[])

    def schedule(records, *args, **kwargs):
        if records.id != case.scope.monitor_id or kwargs.get('summary') != summary:
            return original_schedule(records, *args, **kwargs)
        evidence.calls.append(kwargs['user_id'])
        if len(evidence.calls) == 2:
            if before_fault:
                before_fault(records)
            if fault == '40001':
                try:
                    records.env.cr.execute("""
                        DO $$ BEGIN
                            RAISE EXCEPTION '1159 monitor boundary conflict' USING ERRCODE = '40001';
                        END $$
                    """)
                except OperationalError as error:
                    evidence.pgcodes.append(error.pgcode)
                    raise
            raise RuntimeError('1159 second notification recipient failed')
        Mail = records.env['mail.mail'].sudo()
        domain = [('model', '=', MONITOR), ('res_id', '=', records.id)]
        before = set(Mail.search(domain).ids)
        activity = original_schedule(records, *args, **kwargs)
        records.env.flush_all()
        evidence.activities.extend(activity.ids)
        evidence.mails.extend(set(Mail.search(domain).ids) - before)
        return activity

    with patch.object(activity_model, 'activity_schedule', schedule), \
            patch.object(mail_model, 'send', autospec=True) as send, \
            patch.object(mail_model, 'send_after_commit', autospec=True) as send_after_commit:
        evidence.send = send
        evidence.send_after_commit = send_after_commit
        yield evidence


@contextmanager
def poll_health_time(case, age_minutes):
    """只在原 _cron_stale 執行期間 double 時間，不改共享 cron metadata／predicate。"""
    from datetime import timedelta
    model = type(case.env[MONITOR])
    original = model._cron_stale

    def timed(records, xmlid, minutes):
        if xmlid != POLL_CRON:
            return original(records, xmlid, minutes)
        cron = records.env.ref(xmlid).sudo()
        case.assertTrue(cron.active, '真 poll cron 必須 active，才能用時鐘對照 healthy/stale')
        anchor = cron.lastcall or cron.write_date
        case.assertTrue(anchor)
        with patch.object(fields.Datetime, 'now', return_value=anchor + timedelta(minutes=age_minutes)):
            return original(records, xmlid, minutes)

    with patch.object(model, '_cron_stale', timed):
        yield


def assert_notification_rollback(case, env, evidence):
    """回捲機關必須是真產品 savepoint；不使用 assertRaises 的代回捲。"""
    case.assertEqual(len(evidence.calls), 2, '必須先完成首位排程，再於第二位故障')
    case.assertTrue(evidence.activities, '故障前真建立 activity')
    case.assertTrue(evidence.mails, '故障前真建立 mail queue，不是純 mock')
    case.assertFalse(env['mail.activity'].sudo().browse(evidence.activities).exists())
    case.assertFalse(env['mail.mail'].sudo().browse(evidence.mails).exists())
    env.cr.execute('SELECT 1')
    case.assertEqual(env.cr.fetchone(), (1,), '產品 savepoint 復原 SQL aborted cursor')

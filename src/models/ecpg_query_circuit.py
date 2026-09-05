"""#864 PR-F durable、跨 worker 的 ECPG QueryTrade 403 circuit。

circuit 刻意與 ``woow.billing.incident`` 分離：HTTP 403 是 MerchantID-wide
availability/configuration 訊號，不是單一 top-up 的屬性。只有 private raw-SQL
helper 可 mutation；generic ORM write 會讓 caller 縮短 safety cooldown。
"""
import hashlib
import logging
from datetime import timedelta

from odoo import _, SUPERUSER_ID, api, fields, models
from odoo.exceptions import AccessError

from .billing_incident import _is_odoo_module_uninstall

_logger = logging.getLogger(__name__)


class ECPGQueryCircuit(models.Model):
    _name = 'woow_paas_platform.ecpg_query_circuit'
    _description = 'ECPG Credit Query Circuit'
    _rec_name = 'scope_key'

    scope_key = fields.Char(required=True, readonly=True, index=True)
    provider_ref = fields.Integer(readonly=True)
    blocked_until = fields.Datetime(readonly=True, index=True)
    consecutive_forbidden_count = fields.Integer(required=True, default=0, readonly=True)
    last_forbidden_at = fields.Datetime(readonly=True)
    last_success_at = fields.Datetime(readonly=True)
    last_alerted_at = fields.Datetime(readonly=True)

    _sql_constraints = [
        ('scope_key_uniq', 'UNIQUE(scope_key)', 'ECPG query circuit scope must be unique.'),
    ]
    _LOCK_TIMEOUT = '1s'

    @api.model
    def _scope_key(self, merchant_id):
        """回傳不可逆且可安全寫 log 的 MerchantID scope key。"""
        return hashlib.sha256(str(merchant_id).encode()).hexdigest()

    @api.model
    def _blocked_until(self, merchant_id):
        """用 fresh cursor 讀取；cron 的 RR snapshot 不可作為 circuit 真值。"""
        scope_key = self._scope_key(merchant_id)
        with self.env.registry.cursor() as cr:
            cr.execute('SET LOCAL lock_timeout = %s', (self._LOCK_TIMEOUT,))
            cr.execute(
                'SELECT blocked_until FROM woow_paas_platform_ecpg_query_circuit '
                'WHERE scope_key = %s', (scope_key,), log_exceptions=False)
            row = cr.fetchone()
            return row and row[0]

    @api.model
    def _record_forbidden(self, merchant_id, provider_ref, seconds=1800):
        """Durable 延長 shared cooldown；race writer 絕不可把期限縮短。"""
        now = fields.Datetime.now()
        blocked_until = now + timedelta(seconds=max(1800, int(seconds)))
        scope_key = self._scope_key(merchant_id)
        with self.env.registry.cursor() as cr:
            cr.execute('SET LOCAL lock_timeout = %s', (self._LOCK_TIMEOUT,))
            cr.execute(
                'INSERT INTO woow_paas_platform_ecpg_query_circuit '
                '(scope_key, provider_ref, blocked_until, consecutive_forbidden_count, '
                'last_forbidden_at, create_uid, write_uid, create_date, write_date) '
                'VALUES (%s, %s, %s, 1, %s, %s, %s, %s, %s) '
                'ON CONFLICT (scope_key) DO UPDATE SET '
                'blocked_until = GREATEST(woow_paas_platform_ecpg_query_circuit.blocked_until, EXCLUDED.blocked_until), '
                'provider_ref = EXCLUDED.provider_ref, '
                'consecutive_forbidden_count = woow_paas_platform_ecpg_query_circuit.consecutive_forbidden_count + 1, '
                'last_forbidden_at = EXCLUDED.last_forbidden_at, write_uid = EXCLUDED.write_uid, '
                'write_date = EXCLUDED.write_date '
                'RETURNING consecutive_forbidden_count, blocked_until',
                (scope_key, provider_ref, blocked_until, now, SUPERUSER_ID,
                 SUPERUSER_ID, now, now), log_exceptions=False)
            row = cr.fetchone()
            cr.commit()
        return row

    @api.model
    def _claim_repeat_forbidden_alert(self, merchant_id):
        """同一個 403 streak 只讓一個 worker 取得告警權。"""
        now = fields.Datetime.now()
        scope_key = self._scope_key(merchant_id)
        with self.env.registry.cursor() as cr:
            cr.execute('SET LOCAL lock_timeout = %s', (self._LOCK_TIMEOUT,))
            cr.execute(
                'UPDATE woow_paas_platform_ecpg_query_circuit '
                'SET last_alerted_at = %s, write_uid = %s, write_date = %s '
                'WHERE scope_key = %s AND consecutive_forbidden_count >= 2 '
                'AND last_alerted_at IS NULL RETURNING id',
                (now, SUPERUSER_ID, now, scope_key),
                log_exceptions=False,
            )
            claimed = cr.fetchone() is not None
            cr.commit()
            return claimed

    @api.model
    def _record_success(self, merchant_id):
        """只有完成 query 才關閉舊 403 streak；transport failure 不算成功。"""
        now = fields.Datetime.now()
        scope_key = self._scope_key(merchant_id)
        with self.env.registry.cursor() as cr:
            cr.execute('SET LOCAL lock_timeout = %s', (self._LOCK_TIMEOUT,))
            cr.execute(
                'UPDATE woow_paas_platform_ecpg_query_circuit '
                'SET blocked_until = NULL, consecutive_forbidden_count = 0, '
                'last_success_at = %s, last_alerted_at = NULL, '
                'write_uid = %s, write_date = %s WHERE scope_key = %s',
                (now, SUPERUSER_ID, now, scope_key),
                log_exceptions=False,
            )
            cr.commit()

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError(_(
            'ECPG query circuits may be created only by the internal writer.'
        ))

    def write(self, vals):
        raise AccessError(_(
            'ECPG query circuits may be updated only by the internal writer.'
        ))

    def unlink(self):
        if _is_odoo_module_uninstall(self.env):
            return super().unlink()
        raise AccessError(_(
            'ECPG query circuits may be deleted only during module uninstall.'
        ))

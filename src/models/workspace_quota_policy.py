"""#1679 Workspace quota 最低保障（floor）政策：管理員入口、寫入守衛與不降額同步。

- 目標 = 每項 max(既有公式, 最低保障)；未設定（NULL）沿用公式。計算本身在
  ``cloud_service._compute_ns_quota``（所有重算路徑共用同一個政策）。
- 唯一人工入口是 ``set_quota_minimums``：以 uid 判斷 ``base.group_system``（Portal
  capability 的 sudo 保留 portal uid，所以不會因此取得這個能力），再以模組私有 sentinel
  寫入。ORM create／write 對 floor 與同步控制欄位一律拒絕——連 system 管理員也只能走入口，
  確保驗證、版本、稽核與立即同步不會被繞過。
- 同步一律「不降額」（``services/ns_quota_policy``）；目標低於叢集的項目只標示待處理。
"""
import json
import logging

from psycopg2 import errors as pg_errors

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import mute_logger

from ..services.ns_quota_policy import (
    QuotaNotProvable, build_hard, merge_never_lower, parse_quantity,
)
from ..services import paas_operator as paas_operator_service
from ..services.paas_operator import PaaSOperatorError, PaaSOperatorTimeoutError
from ..services.workspace_quota import workspace_namespace

_logger = logging.getLogger(__name__)

#: floor 欄位 → ``set_quota_minimums`` 的參數名
FLOOR_FIELDS = {
    'quota_floor_cpu': 'cpu',
    'quota_floor_memory_gi': 'memory_gi',
    'quota_floor_storage_gi': 'storage_gi',
}
#: 只有同步流程能寫的控制欄位
SYNC_FIELDS = (
    'quota_target_version', 'quota_sync_state', 'quota_sync_reason',
    'quota_synced_version', 'quota_synced_at', 'quota_synced_hard', 'quota_pending_decrease',
)
PROTECTED_FIELDS = tuple(FLOOR_FIELDS) + SYNC_FIELDS

SYNC_STATES = [
    ('pending_first_deploy', 'Pending first deployment'),
    ('pending', 'Pending sync'),
    ('synced', 'Synced'),
    ('unconfirmed', 'Result unconfirmed'),
    ('blocked', 'Blocked by safety check'),
]
# 多個 namespace 時對外顯示「最需要處理」的那個狀態
_STATE_RANK = {'synced': 0, 'pending_first_deploy': 1, 'pending': 2, 'unconfirmed': 3, 'blocked': 4}

# 生命週期與開通路徑等 quota 鎖的上限。持鎖者都是短交易（管理員推送、其他生命週期推送、
# 開通的 quota 步驟到該交易 commit），等幾秒就能序列化；等不到才放棄，不無限期卡住刪除或開通。
LIFECYCLE_LOCK_TIMEOUT = '10s'
PROVISION_LOCK_TIMEOUT = '30s'


# ------------------------------------------------------------------------- #
#  寫入開閘：模組私有 sentinel（形狀同 workspace_access 的 #732）             #
# ------------------------------------------------------------------------- #
# 不能用 `env.su`／`_is_privileged_writer` 放行：Portal capability 回傳的 workspace 本身
# 就是 sudo，豁免 su 等於讓任何未來的 `sudo().write(vals)` 變成改 quota 的後門。也不能是
# 裸 truthy context key：call_kw 把呼叫端 context 整包換進 env，JSON 表達得出的值就偽造得出。
class _InternalQuotaPolicyWrite:
    """平台伺服器端寫入 quota 政策／同步欄位的 marker type。"""

    __slots__ = ()

    def __repr__(self):
        return '<internal workspace quota policy write>'


_INTERNAL_QUOTA_POLICY_WRITE = _InternalQuotaPolicyWrite()

#: 唯一的開閘 context key；判準是值的物件身分，見 `_is_internal_quota_policy_write`。
_QUOTA_POLICY_WRITE_CTX_KEY = 'woow_ws_quota_policy_write'


def _internal_quota_policy_write(records):
    """回傳唯一被允許寫 quota 政策／同步欄位的 context 底下的 ``records``。"""
    return records.with_context(
        **{_QUOTA_POLICY_WRITE_CTX_KEY: _INTERNAL_QUOTA_POLICY_WRITE})


def _is_internal_quota_policy_write(env):
    """判準：只比對 context 值的物件身分（`is`），不讀 su／user。"""
    return env.context.get(_QUOTA_POLICY_WRITE_CTX_KEY) is _INTERNAL_QUOTA_POLICY_WRITE


def _normalize_floor(label, value):
    """管理員輸入 → 儲存值：正整數照存；0／False／空值＝清除（沿用公式）。

    0 不是有效的零額度（零額度會讓工作區無法開任何服務），所以只代表「未設定」。
    負值、非整數、非有限值、字串（例如 ``'18Gi'``）一律拒絕。
    """
    if value is None or value is False or value == '':
        return 0
    if isinstance(value, bool):
        raise ValidationError(_('%(resource)s minimum must be a whole number.', resource=label))
    if isinstance(value, float):
        if value != value or value in (float('inf'), float('-inf')) or not value.is_integer():
            raise ValidationError(_('%(resource)s minimum must be a whole number.', resource=label))
        value = int(value)
    if not isinstance(value, int):
        raise ValidationError(_('%(resource)s minimum must be a whole number.', resource=label))
    if value < 0:
        raise ValidationError(_('%(resource)s minimum cannot be negative.', resource=label))
    return value


def _floor_text(value, unit):
    return '%s%s' % (value, unit) if value else '-'


class WorkspaceQuotaPolicy(models.Model):
    _inherit = 'woow_paas_platform.workspace'

    quota_floor_cpu = fields.Integer(
        string='CPU Minimum (cores)', groups='base.group_system', copy=False, readonly=True,
        help='Minimum CPU quota for this Workspace. The applied quota is the larger of the '
             'plan formula and this minimum. Empty means the formula applies.')
    quota_floor_memory_gi = fields.Integer(
        string='Memory Minimum (Gi)', groups='base.group_system', copy=False, readonly=True,
        help='Minimum memory quota for this Workspace, in Gi. Empty means the formula applies.')
    quota_floor_storage_gi = fields.Integer(
        string='Storage Minimum (Gi)', groups='base.group_system', copy=False, readonly=True,
        help='Minimum storage quota for this Workspace, in Gi. Empty means the formula applies.')

    quota_target_version = fields.Integer(
        string='Quota Policy Version', groups='base.group_system', copy=False, readonly=True,
        default=0)
    quota_sync_state = fields.Selection(
        SYNC_STATES, string='Quota Sync Status', groups='base.group_system', copy=False,
        readonly=True)
    quota_sync_reason = fields.Text(
        string='Quota Sync Detail', groups='base.group_system', copy=False, readonly=True)
    quota_synced_version = fields.Integer(
        string='Last Synced Policy Version', groups='base.group_system', copy=False,
        readonly=True)
    quota_synced_at = fields.Datetime(
        string='Last Successful Quota Sync', groups='base.group_system', copy=False,
        readonly=True)
    quota_synced_hard = fields.Text(
        string='Last Pushed Quota', groups='base.group_system', copy=False, readonly=True,
        help='The quota the platform last pushed successfully. It is not a live cluster '
             'observation.')
    quota_pending_decrease = fields.Text(
        string='Pending Quota Decrease', groups='base.group_system', copy=False, readonly=True,
        help='Resources whose target is below the current cluster quota. The platform keeps '
             'the higher cluster value until a safe decrease is available.')

    quota_formula_cpu = fields.Char(
        string='Formula CPU', compute='_compute_quota_policy_view', groups='base.group_system')
    quota_formula_memory = fields.Char(
        string='Formula Memory', compute='_compute_quota_policy_view', groups='base.group_system')
    quota_formula_storage = fields.Char(
        string='Formula Storage', compute='_compute_quota_policy_view', groups='base.group_system')
    quota_target_cpu = fields.Char(
        string='Target CPU', compute='_compute_quota_policy_view', groups='base.group_system')
    quota_target_memory = fields.Char(
        string='Target Memory', compute='_compute_quota_policy_view', groups='base.group_system')
    quota_target_storage = fields.Char(
        string='Target Storage', compute='_compute_quota_policy_view', groups='base.group_system')
    quota_floor_cpu_display = fields.Char(
        string='CPU Minimum', compute='_compute_quota_policy_view', groups='base.group_system')
    quota_floor_memory_display = fields.Char(
        string='Memory Minimum', compute='_compute_quota_policy_view', groups='base.group_system')
    quota_floor_storage_display = fields.Char(
        string='Storage Minimum', compute='_compute_quota_policy_view', groups='base.group_system')
    quota_audit_ids = fields.Many2many(
        'woow_paas_platform.workspace_quota_audit', string='Quota Change Log',
        compute='_compute_quota_policy_view', groups='base.group_system')

    # --------------------------------------------------------------------- #
    #  寫入守衛                                                              #
    # --------------------------------------------------------------------- #
    def _reject_quota_policy_write(self, values, *, defaults=False):
        if _is_internal_quota_policy_write(self.env):
            return
        touched = set(PROTECTED_FIELDS).intersection(values)
        if defaults:
            touched |= {name for name in PROTECTED_FIELDS
                        if 'default_' + name in self.env.context}
        if touched:
            raise AccessError(_(
                'Workspace quota minimums can only be changed through the administrator '
                'quota action.'))

    @api.model_create_multi
    def create(self, vals_list):
        for values in vals_list:
            self._reject_quota_policy_write(values, defaults=True)
        return super().create(vals_list)

    def write(self, values):
        self._reject_quota_policy_write(values)
        return super().write(values)

    # --------------------------------------------------------------------- #
    #  顯示用計算（公式、目標、異動紀錄）                                      #
    # --------------------------------------------------------------------- #
    @api.depends_context('uid')
    def _compute_quota_policy_view(self):
        Service = self.env['woow_paas_platform.cloud_service'].sudo()
        Audit = self.env['woow_paas_platform.workspace_quota_audit'].sudo()
        for workspace in self:
            not_set = _('Not set (formula applies)')
            workspace.quota_floor_cpu_display = (
                _('%(value)s cores', value=workspace.quota_floor_cpu)
                if workspace.quota_floor_cpu else not_set)
            workspace.quota_floor_memory_display = (
                '%sGi' % workspace.quota_floor_memory_gi if workspace.quota_floor_memory_gi
                else not_set)
            workspace.quota_floor_storage_display = (
                '%sGi' % workspace.quota_floor_storage_gi if workspace.quota_floor_storage_gi
                else not_set)
            if not workspace.id:
                workspace.update({name: False for name in (
                    'quota_formula_cpu', 'quota_formula_memory', 'quota_formula_storage',
                    'quota_target_cpu', 'quota_target_memory', 'quota_target_storage')})
                workspace.quota_audit_ids = False
                continue
            formula = Service._compute_ns_quota_formula(workspace)
            target = Service._compute_ns_quota(workspace)
            (workspace.quota_formula_cpu, workspace.quota_formula_memory,
             workspace.quota_formula_storage) = formula
            (workspace.quota_target_cpu, workspace.quota_target_memory,
             workspace.quota_target_storage) = target
            workspace.quota_audit_ids = Audit.search([('workspace_ref', '=', workspace.id)])

    # --------------------------------------------------------------------- #
    #  管理員入口                                                            #
    # --------------------------------------------------------------------- #
    def set_quota_minimums(self, cpu=None, memory_gi=None, storage_gi=None):
        """管理員設定 CPU（cores）、memory、storage（Gi）最低保障，保存後立即同步。

        參數為 ``None`` 表示該項不變更；``0``／``False`` 表示清除（沿用公式）。
        任何一項目標因此下降的變更本票一律拒絕（安全降額由後續票交付），不會保存。
        回傳同步結果摘要。
        """
        # uid 判準：Portal capability 的 sudo 保留 portal uid，不會因此通過。
        if not self.env.user.has_group('base.group_system'):
            raise AccessError(_('Only system administrators may change Workspace quota minimums.'))
        self.ensure_one()
        self.check_access('read')
        workspace = self.sudo()
        workspace._lock_quota_policy()
        workspace.invalidate_recordset(list(PROTECTED_FIELDS))

        requested = {'quota_floor_cpu': cpu, 'quota_floor_memory_gi': memory_gi,
                     'quota_floor_storage_gi': storage_gi}
        labels = {'quota_floor_cpu': _('CPU'), 'quota_floor_memory_gi': _('Memory'),
                  'quota_floor_storage_gi': _('Storage')}
        old = {name: workspace[name] or 0 for name in FLOOR_FIELDS}
        new = dict(old)
        for name, value in requested.items():
            if value is not None:
                new[name] = _normalize_floor(labels[name], value)
        if new == old:
            return workspace._quota_status_summary()

        Service = self.env['woow_paas_platform.cloud_service'].sudo()
        before = Service._compute_ns_quota(workspace, floors=old)
        after = Service._compute_ns_quota(workspace, floors=new)
        lowered = [label for label, b, a in zip((_('CPU'), _('Memory'), _('Storage')), before, after)
                   if parse_quantity(a) < parse_quantity(b)]
        if lowered:
            raise UserError(_(
                'This change would lower the quota target for %(resources)s. Lowering a '
                'Workspace quota is not supported yet; the minimum was not saved.',
                resources=', '.join(lowered)))

        version = workspace.quota_target_version + 1
        _internal_quota_policy_write(workspace).write(dict(
            new, quota_target_version=version, quota_sync_state='pending',
            quota_sync_reason=False))
        workspace._log_quota_minimum_change(old, new, version)
        return workspace._quota_sync('admin')

    def action_open_quota_minimum_wizard(self):
        if not self.env.user.has_group('base.group_system'):
            raise AccessError(_('Only system administrators may change Workspace quota minimums.'))
        self.ensure_one()
        self.check_access('read')
        workspace = self.sudo()
        return {
            'type': 'ir.actions.act_window', 'name': _('Set Quota Minimums'),
            'res_model': 'woow_paas_platform.workspace_quota_minimum_wizard',
            'view_mode': 'form', 'target': 'new',
            'context': {'default_workspace_id': self.id,
                        'default_cpu': workspace.quota_floor_cpu,
                        'default_memory_gi': workspace.quota_floor_memory_gi,
                        'default_storage_gi': workspace.quota_floor_storage_gi},
        }

    def _log_quota_minimum_change(self, old, new, version):
        """稽核：append-only ledger（結構化）＋ admin-only monitor chatter（無通知）。"""
        self.ensure_one()
        actor = self.env.user

        def _summary(values):
            return 'cpu=%s, memory=%s, storage=%s' % (
                _floor_text(values['quota_floor_cpu'], ''),
                _floor_text(values['quota_floor_memory_gi'], 'Gi'),
                _floor_text(values['quota_floor_storage_gi'], 'Gi'))

        _internal_quota_policy_write(self.env['woow_paas_platform.workspace_quota_audit']).sudo().create({
            'workspace_ref': self.id, 'ws_hash': self.ws_hash, 'workspace_name': self.name,
            'actor_uid': actor.id, 'actor_login': actor.login, 'target_version': version,
            'old_values': _summary(old), 'new_values': _summary(new),
        })
        monitor = self.env['woow_paas_platform.platform_monitor']._singleton()
        monitor._message_log(body=_(
            'Workspace %(name)s (id=%(id)s, %(ws_hash)s) quota minimums changed by %(login)s '
            '(uid %(uid)s), policy version %(version)s: %(old)s -> %(new)s',
            name=self.name, id=self.id, ws_hash=self.ws_hash, login=actor.login, uid=actor.id,
            version=version, old=_summary(old), new=_summary(new)))

    # --------------------------------------------------------------------- #
    #  同步（不降額）                                                        #
    # --------------------------------------------------------------------- #
    def _lock_quota_policy(self, timeout=None):
        """對 workspace 列取 ``FOR NO KEY UPDATE``，序列化同一工作區的 quota 推送。

        ``timeout=None``（管理員入口）：等到拿到為止。REPEATABLE READ 下，對方已 commit 的
        更新會讓本交易拿鎖時 40001，請求層會重播，重播後讀到新政策。

        ``timeout='10s'`` 等（生命週期、開通路徑）：最多等這麼久；逾時或 40001 回 ``False``，
        交易仍可用（savepoint 回捲）。呼叫端拿不到鎖就不推送、也不寫這一列——鎖外推送會和
        持鎖者的推送互相覆蓋（operator 沒有 compare-and-set），寫這一列則會在對方 commit 後
        40001、在已呼叫 operator 之後整筆回捲。
        """
        self.ensure_one()
        cr = self.env.cr
        query = f"SELECT id FROM {self._table} WHERE id = %s FOR NO KEY UPDATE"
        if timeout is None:
            cr.execute(query, (self.id,))
            return True
        try:
            # 同 core ir_cron 取鎖的做法：預期中的衝突不該在 sql_db 印成 ERROR「bad query」。
            with mute_logger('odoo.sql_db'), cr.savepoint(flush=False):
                cr.execute("SET LOCAL lock_timeout = %s", (timeout,))
                cr.execute(query, (self.id,))
                cr.execute("SET LOCAL lock_timeout = DEFAULT")
        except (pg_errors.SerializationFailure, pg_errors.LockNotAvailable) as exc:
            _logger.warning("[#1679] quota sync lock busy workspace=%s (%s); quota not pushed",
                            self.id, type(exc).__name__)
            return False
        return True

    def _quota_namespaces(self):
        """要同步的 namespace：工作區推導出的 namespace ＋ 存活服務實際住的 namespace。"""
        self.ensure_one()
        names = set()
        derived = workspace_namespace(self.env, self)
        if derived:
            names.add(derived)
        services = self.env['woow_paas_platform.cloud_service'].sudo().search([
            ('workspace_id', '=', self.id), ('state', '!=', 'deleting')])
        names.update(ns for ns in services.mapped('helm_namespace') if ns)
        return sorted(names)

    def _quota_sync(self, trigger, namespaces=None, *, lock_timeout=None):
        """把目前目標推到叢集（每項都不低於叢集現況），記錄結果並回傳摘要。

        ``lock_timeout``：生命週期路徑等鎖的上限（見 ``_lock_quota_policy``）；拿不到鎖回
        ``busy``，不推送、不寫。管理員入口已先拿鎖，不帶這個參數。
        """
        self.ensure_one()
        workspace = self.sudo()
        if lock_timeout and not workspace._lock_quota_policy(timeout=lock_timeout):
            return {'state': 'busy', 'trigger': trigger}
        version = workspace.quota_target_version
        target = self.env['woow_paas_platform.cloud_service'].sudo()._ns_quota_payload(workspace)
        client = paas_operator_service.get_paas_operator_client(self.env(context={}))
        results = {}
        for namespace in (namespaces or workspace._quota_namespaces()):
            results[namespace] = _sync_namespace(client, namespace, target)
        workspace._record_quota_sync(version, results)
        summary = workspace._quota_status_summary()
        summary['trigger'] = trigger
        return summary

    def _record_quota_sync(self, version, results):
        self.ensure_one()
        if not results:
            vals = {'quota_sync_state': 'pending_first_deploy',
                    'quota_sync_reason': 'no_namespace'}
        else:
            state = max((r['state'] for r in results.values()), key=_STATE_RANK.get)
            # 舊回應不可標記新設定成功：記錄時政策版本已前進就不算 synced。
            # 讀 DB 前先 flush，否則同交易內剛寫的新版本還在 ORM 快取裡。
            self.flush_recordset(['quota_target_version'])
            self.env.cr.execute(
                f"SELECT quota_target_version FROM {self._table} WHERE id = %s", (self.id,))
            current = self.env.cr.fetchone()[0] or 0
            superseded = current != version
            if superseded and state == 'synced':
                state = 'pending'
            reasons = ['%s: %s' % (ns, r['reason']) for ns, r in sorted(results.items())
                       if r.get('reason')]
            if superseded:
                reasons.append('superseded: policy version %s replaced %s' % (current, version))
            pending = ['%s %s: cluster %s, target %s' % (ns, key, live, wanted)
                       for ns, r in sorted(results.items())
                       for key, live, wanted in r.get('pending_decrease', ())]
            vals = {'quota_sync_state': state, 'quota_sync_reason': '\n'.join(reasons) or False,
                    'quota_pending_decrease': '\n'.join(pending) or False}
            if state == 'synced':
                vals.update({
                    'quota_synced_version': version,
                    'quota_synced_at': fields.Datetime.now(),
                    'quota_synced_hard': json.dumps(
                        {ns: r['hard'] for ns, r in sorted(results.items()) if r.get('hard')},
                        sort_keys=True),
                })
        _internal_quota_policy_write(self).write(vals)

    def _quota_create_namespace(self, client, namespace):
        """開通路徑（``create_namespace``）的同一個政策：必要時才降級成不送 quota。

        ``create_namespace`` 同時負責 namespace 基線（LimitRange、NetworkPolicy、PSA、
        pull secret），所以只要能證明不降額就一定要呼叫：

        - 叢集上沒有受管 quota（list 成功、確定沒有）⇒ 以目標建立，沒有既有值可降；
        - 有受管 quota ⇒ 以「每項 max(目標, 叢集)」呼叫；
        - 套用會降低某一項（例如 pvc 現況大於 10）⇒ 不呼叫（namespace 已存在、基線已在），
          記為 blocked，開通照常繼續；
        - 讀不到或資料不完整 ⇒ 回 ``(False, reason)``，由呼叫端讓開通失敗（不能證明不降額）；
        - 工作區正被其他交易同步、等不到鎖 ⇒ 回 ``(False, 'quota_sync_busy')``：不讀、不送、
          不寫，開通失敗待重試（鎖外推送可能蓋掉對方剛提高的值）。

        回傳 ``(ok, reason)``；``create_namespace`` 的 operator 錯誤原樣 raise，維持既有的
        開通失敗處理。
        """
        self.ensure_one()
        workspace = self.sudo()
        if not workspace._lock_quota_policy(timeout=PROVISION_LOCK_TIMEOUT):
            return False, 'quota_sync_busy'
        version = workspace.quota_target_version
        target = self.env['woow_paas_platform.cloud_service'].sudo()._ns_quota_payload(workspace)
        plan = _plan_namespace(client, namespace, target)
        if plan.get('not_found'):
            payload, plan = target, dict(plan, state='synced', reason=None,
                                         hard=build_hard(target), pending_decrease=())
        elif plan['state'] == 'synced':
            payload = plan['payload']
        elif plan['reason'].startswith('would_lower'):
            workspace._record_quota_sync(version, {namespace: plan})
            return True, plan['reason']
        else:
            workspace._record_quota_sync(version, {namespace: plan})
            return False, plan['reason']
        result = _push_result(plan, lambda: client.create_namespace(
            namespace=namespace, cpu_limit=payload['cpu_limit'],
            memory_limit=payload['memory_limit'], storage_limit=payload['storage_limit'],
            workspace_id=self.id or None,
            ephemeral_storage_request=payload.get('ephemeral_storage_request'),
            ephemeral_storage_limit=payload.get('ephemeral_storage_limit')))
        workspace._record_quota_sync(version, {namespace: result})
        if result.get('error') is not None:
            raise result['error']
        return True, None

    def _quota_status_summary(self):
        self.ensure_one()
        return {
            'state': self.quota_sync_state or False,
            'reason': self.quota_sync_reason or False,
            'pending_decrease': self.quota_pending_decrease or False,
            'version': self.quota_target_version,
            'synced_version': self.quota_synced_version,
        }


def _plan_namespace(client, namespace, target):
    """讀取→合併→逐項檢查；不推送。回傳 dict（不 raise）。

    ``push``：可以送出 ``payload``（不會降低任何一項）；``not_found``：叢集上沒有受管
    quota（list 成功、確定沒有這個物件）。其餘情況 ``state`` 已是最終結果。
    """
    if not client:
        return {'state': 'pending', 'reason': 'operator_not_configured'}
    try:
        report = client.get_namespace_quota(namespace, include_managed=True)
    except PaaSOperatorError as exc:
        unsupported = getattr(exc, 'status_code', None) in (404, 405)
        return {'state': 'blocked',
                'reason': 'operator_unsupported' if unsupported else 'quota_unreadable'}
    managed = report.get('managed_quota') if isinstance(report, dict) else None
    if not isinstance(managed, dict):
        return {'state': 'blocked', 'reason': 'operator_unsupported'}
    if managed.get('available') is not True:
        reason = managed.get('reason') or 'managed_quota_unavailable'
        if reason == 'managed_quota_not_found':
            if managed.get('namespace_exists') is False:
                # 確認 namespace 尚不存在：政策已保存，等首次開通套用；不建立空 namespace。
                return {'state': 'pending_first_deploy', 'reason': 'namespace_not_created',
                        'not_found': True}
            return {'state': 'blocked', 'reason': reason, 'not_found': True}
        return {'state': 'blocked', 'reason': reason}
    try:
        payload, lowering, pending = merge_never_lower(
            target, managed.get('spec_hard'), managed.get('hard'))
    except QuotaNotProvable as exc:
        return {'state': 'blocked', 'reason': 'quota_not_provable: %s' % exc}
    if lowering:
        return {'state': 'blocked', 'reason': 'would_lower: %s' % ', '.join(sorted(lowering)),
                'pending_decrease': pending}
    hard = build_hard(payload)
    spec_hard = managed.get('spec_hard') or {}
    unchanged = all(parse_quantity(spec_hard.get(key)) == parse_quantity(value)
                    for key, value in hard.items())
    return {'state': 'synced', 'hard': hard, 'payload': payload, 'pending_decrease': pending,
            'push': not unchanged}


def _push_result(plan, call):
    """執行推送（``call()``），把 operator 的結果轉成同步狀態。"""
    try:
        call()
    except PaaSOperatorTimeoutError as exc:
        # operator 端的 kubectl 可能已套用：結果未知，不是失敗也不是成功。
        return {'state': 'unconfirmed', 'reason': 'operator_timeout',
                'pending_decrease': plan.get('pending_decrease', ()), 'error': exc}
    except PaaSOperatorError as exc:
        return {'state': 'pending',
                'reason': 'operator_error: %s' % (exc.status_code or exc.message),
                'pending_decrease': plan.get('pending_decrease', ()), 'error': exc}
    return dict(plan, state='synced')


def _sync_namespace(client, namespace, target):
    """單一 namespace 的不降額同步（PUT）；回傳結果 dict（不 raise）。"""
    plan = _plan_namespace(client, namespace, target)
    if not plan.get('push'):
        return plan
    payload = plan['payload']
    return _push_result(plan, lambda: client.update_namespace_quota(
        namespace, payload['cpu_limit'], payload['memory_limit'], payload['storage_limit'],
        ephemeral_storage_request=payload.get('ephemeral_storage_request'),
        ephemeral_storage_limit=payload.get('ephemeral_storage_limit')))

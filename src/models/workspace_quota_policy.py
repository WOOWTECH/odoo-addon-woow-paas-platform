"""#1679 Workspace quota 最低保障（floor）政策：管理員入口、寫入守衛與不降額同步。

- 目標 = 每項 max(既有公式, 最低保障)；未設定（NULL）沿用公式。計算本身在
  ``cloud_service._compute_ns_quota``（所有重算路徑共用同一個政策）。
- 唯一人工入口是 ``set_quota_minimums``：以 uid 判斷 ``base.group_system``（Portal
  capability 的 sudo 保留 portal uid，所以不會因此取得這個能力），再以模組私有 sentinel
  寫入。ORM create／write 對 floor 與同步控制欄位一律拒絕——連 system 管理員也只能走入口，
  確保驗證、版本、稽核與立即同步不會被繞過。
- 同步一律「不降額」（``services/ns_quota_policy``）；目標低於叢集的項目只標示待處理。

#1680 同步可恢復且舊工作不覆蓋新設定：

- **提交序列化**：所有平台 quota 寫入（管理員入口、手動重試、背景重試、生命週期重算、開通
  建立 namespace）都先對工作區這一列取 ``FOR NO KEY UPDATE``（``_lock_quota_policy``），讀取
  叢集、推送與記錄結果都在鎖內。背景與生命週期路徑拿不到鎖不等、不寫，交給 cron 下一輪。
- **遠端版本防護**：每次推送帶新的 ``sync_token`` 與「鎖內 GET 讀到的 resourceVersion」
  （或 ``expect_absent``），operator 以條件寫入套用；物件之後被任何人改過就 409、不覆寫。
  所以舊請求晚到只可能落在它規劃時的那一版上，而那一版之後任何一次推送都會讓它落不下。
- **讀回對帳**：``quota_expected_tokens`` 記每個 namespace「遠端應該帶的 token」（確認成功的，
  或結果不明的在途 token）。只有遠端 token 與它相符才算已同步；不符（舊請求晚到、交易回捲後
  遠端留下沒提交的值）就以新 token 重推（圍籬），值仍取 max(目標, 叢集)，不降額。
- **有限重試**：見 ``RETRY_BACKOFF_MINUTES``；額度以目標指紋（namespace 集合＋payload）計，
  目標改變、管理員保存或手動重試才重置。永久阻擋（would_lower 等）不自動重試。

#1681 管理員安全降低或清除最低保障：

- 管理員的變更讓目標下降時，先讀每個 namespace 的受管 quota，以共用的 ``plan_safe_lower``
  依「完整新目標 vs 叢集 hard」判斷哪些項會降，逐項比對 5 項 used。讀不到、缺項、舊
  operator、低於任一 used、會降低 pvc 一律拒絕，**不保存**。
- 會降低叢集時要求風險確認：``preview_quota_minimums`` 回傳伺服器簽章的確認碼，綁定
  Workspace、操作者、政策版本、新的最低保障、完整目標與 namespace 集合，10 分鐘內有效。
  ``set_quota_minimums(..., confirmation=...)`` 在鎖內逐項比對並**再檢查一次**用量，通過才
  保存，並記下 ``quota_lower_authorization``（政策版本＋目標），之後每次重試都重讀 used 重檢。
- 確認只套用在管理員主動降低／清除；生命週期與背景同步不經過這裡。
"""
import base64
from datetime import timedelta
import hashlib
import json
import logging
import uuid

from psycopg2 import errors as pg_errors

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import consteq, mute_logger
from odoo.tools.misc import hmac as odoo_hmac

from ..services.ns_quota_policy import (
    QuotaNotProvable, build_hard, merge_never_lower, parse_quantity, plan_safe_lower,
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
    # #1680
    'quota_expected_tokens', 'quota_sync_unguarded', 'quota_attempt_fingerprint',
    'quota_synced_fingerprint', 'quota_retry_count', 'quota_next_retry_at',
    'quota_last_attempt_at', 'quota_verified_at',
    # #1682
    'quota_reconcile_pending',
    # #1681
    'quota_lower_authorization',
)
PROTECTED_FIELDS = tuple(FLOOR_FIELDS) + SYNC_FIELDS

SYNC_STATES = [
    ('pending_first_deploy', 'Pending first deployment'),
    ('pending', 'Pending sync'),
    ('synced', 'Synced'),
    ('unconfirmed', 'Result unconfirmed'),
    ('blocked', 'Blocked by safety check'),
    ('exhausted', 'Retries exhausted'),
]
# 多個 namespace 時對外顯示「最需要處理」的那個狀態（exhausted 只出現在工作區層級）
_STATE_RANK = {'synced': 0, 'pending_first_deploy': 1, 'pending': 2, 'unconfirmed': 3, 'blocked': 4}

#: #1680 自動重試：第 n 次背景重試排在前一次失敗後 ``RETRY_BACKOFF_MINUTES[n-1]`` 分鐘。
#: 共 5 次（1、5、15、60、240 分鐘，首次失敗後約 5 小時 21 分內），之後轉 ``exhausted``，
#: 只剩管理員手動重試或目標改變能再試。
RETRY_BACKOFF_MINUTES = (1, 5, 15, 60, 240)
MAX_AUTO_RETRIES = len(RETRY_BACKOFF_MINUTES)
#: 背景路徑拿不到工作區鎖（別的交易正在同步）時，cron 多久之後再看一次；不消耗重試額度。
BUSY_RETRY = timedelta(minutes=1)
#: 已同步的工作區多久讀回驗證一次（遠端 token 或值與平台記錄不符就圍籬重推）。
VERIFY_INTERVAL = timedelta(hours=6)
#: 這些觸發來源代表人的明確動作：重置重試額度。
_RESETTING_TRIGGERS = ('admin', 'manual')
_RECONCILE_CRON = 'woow_paas_platform.ir_cron_reconcile_workspace_quota'
#: #1682：這些觸發來源的同步不會自動降額（管理員只能降已確認的目標、開通一律不降）。它們留下
#: 下列資源的待處理降額時，交給背景（生命週期免確認的安全降額）儘快收回，不必等 6 小時讀回。
_NON_RECLAIMING_TRIGGERS = ('admin', 'provision')
_RECLAIMABLE_KEYS = ('requests.cpu', 'limits.cpu', 'requests.memory', 'limits.memory',
                     'requests.storage')
#: blocked 之中屬於暫時性、值得自動重試的原因（其餘 blocked 是永久阻擋，等人處理）。
_TRANSIENT_BLOCKED = ('quota_unreadable', 'managed_quota_status_pending')
#: 確定沒寫入、但值得自動重試的 4xx（其餘 4xx 是請求本身有問題，記為 blocked 等人處理）。
_TRANSIENT_HTTP = (401, 403, 408, 429)
#: #1681 降額確認碼的有效期間與 HMAC scope。
LOWER_CONFIRMATION_TTL = timedelta(minutes=10)
_LOWER_CONFIRMATION_SCOPE = 'woow_paas_platform.workspace_quota_lower_confirmation'


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


class _SubsetAttempt:
    """#1682：``_quota_attempt_fingerprint`` 回傳的 marker——這次只同步了工作區的子集。

    子集的結果不能代表整個工作區，所以不記指紋、不動重試額度（見 ``_record_quota_sync``）。
    布林值為假：沿用「指紋非空＝整個工作區的嘗試」的判斷（例如 #1681 清除降額授權）。
    """

    __slots__ = ()

    def __bool__(self):
        return False

    def __repr__(self):
        return '<workspace quota subset attempt>'


_SUBSET_ATTEMPT = _SubsetAttempt()


def _fingerprint(target, namespaces):
    """目標指紋：namespace 集合＋目標 payload。重試額度與「已同步是否仍是現在的目標」都以它計。"""
    raw = json.dumps({'namespaces': sorted(namespaces), 'target': target}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


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
    # #1680 同步可恢復
    quota_expected_tokens = fields.Text(
        string='Expected Remote Sync Tokens', groups='base.group_system', copy=False,
        readonly=True,
        help='Per namespace, the sync token the cluster quota should carry: the last confirmed '
             'push, or a push whose result is unknown. A different token on the cluster means '
             'another write landed there, so the next sync pushes again.')
    quota_sync_unguarded = fields.Boolean(
        string='Operator Lacks Version Protection', groups='base.group_system', copy=False,
        readonly=True,
        help='At the last sync the operator did not support versioned quota writes, so pushes '
             'are made without protection: an older request that finishes late could overwrite '
             'a newer value. Upgrade the operator.')
    quota_attempt_fingerprint = fields.Char(
        string='Last Attempted Target', groups='base.group_system', copy=False, readonly=True)
    quota_synced_fingerprint = fields.Char(
        string='Last Synced Target', groups='base.group_system', copy=False, readonly=True)
    quota_retry_count = fields.Integer(
        string='Automatic Retries Used', groups='base.group_system', copy=False, readonly=True,
        help='Automatic retries used for the current target (at most 5). Resets when the target '
             'changes, an administrator saves the minimums or retries manually, or a sync succeeds.')
    quota_next_retry_at = fields.Datetime(
        string='Next Automatic Retry', groups='base.group_system', copy=False, readonly=True)
    quota_last_attempt_at = fields.Datetime(
        string='Last Sync Attempt', groups='base.group_system', copy=False, readonly=True)
    quota_verified_at = fields.Datetime(
        string='Last Verified Against Cluster', groups='base.group_system', copy=False,
        readonly=True)
    # #1682：背景要儘快以全集重新評估一次——子集同步（生命週期／開通只同步自己的 namespace）
    # 之後，或管理員／開通的同步留下可自動收回的降額時。與目標指紋分開：指紋只負責「目標有沒有
    # 變」（決定重試額度要不要重置），這個旗標只負責「要不要排一次對帳」——兩件事共用空指紋會讓
    # 同目標事件重置額度（#1682 留言 22779）。
    quota_reconcile_pending = fields.Boolean(
        string='Full Quota Re-check Pending', groups='base.group_system', copy=False,
        readonly=True,
        help='The next background run re-checks every namespace of this Workspace: a service '
             'event synced only its own namespace, or a decrease is waiting to be reclaimed.')
    # #1681 管理員確認過的降額：綁定政策版本與目標指紋，只有目前目標仍等於它時同步才可降額。
    quota_lower_authorization = fields.Char(
        string='Confirmed Quota Decrease', groups='base.group_system', copy=False, readonly=True,
        help='Set when an administrator confirms a quota decrease. Syncs may lower the cluster '
             'quota to that exact target (after checking current usage again) while the policy '
             'version and target are unchanged.')
    quota_sync_status = fields.Selection(
        SYNC_STATES, string='Current Sync Status', compute='_compute_quota_policy_view',
        groups='base.group_system',
        help='The last sync result, except that a sync for an older target is shown as pending.')

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
                    'quota_target_cpu', 'quota_target_memory', 'quota_target_storage',
                    'quota_sync_status')})
                workspace.quota_audit_ids = False
                continue
            state = workspace.quota_sync_state
            if state == 'synced' and (
                    workspace.quota_reconcile_pending
                    or workspace.quota_synced_fingerprint != _fingerprint(
                        Service._ns_quota_payload(workspace), workspace._quota_namespaces())):
                # 已同步的是舊目標（服務集合或政策已變），或只同步了子集：還沒同步到現在的目標
                state = 'pending'
            workspace.quota_sync_status = state
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
    def _quota_minimum_change(self, cpu, memory_gi, storage_gi):
        """管理員入口共用的前置：授權、鎖、正規化。回傳 ``(workspace, old, new)``。"""
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
        return workspace, old, new

    def _quota_minimum_review(self, old, new):
        """新最低保障會不會降低叢集：回傳 ``(target, decreases, problems)``。

        只有政策目標下降（管理員主動降低／清除）才讀叢集；判斷以完整新目標對叢集 hard，
        所以先前留下的待處理降額也會一併列出、一併檢查。
        """
        self.ensure_one()
        Service = self.env['woow_paas_platform.cloud_service'].sudo()
        target = Service._ns_quota_payload(self)
        before = Service._compute_ns_quota(self, floors=old)
        after = Service._compute_ns_quota(self, floors=new)
        target.update(zip(('cpu_limit', 'memory_limit', 'storage_limit'), after))
        if not any(parse_quantity(a) < parse_quantity(b) for b, a in zip(before, after)):
            return target, [], []
        decreases, problems = _review_lower(
            paas_operator_service.get_paas_operator_client(self.env(context={})),
            self._quota_namespaces(), target)
        return target, decreases, problems

    def preview_quota_minimums(self, cpu=None, memory_gi=None, storage_gi=None):
        """#1681 管理員預覽：這次變更是否會降低叢集 quota、能不能安全降、要不要確認。

        不保存任何東西。回傳 ``state``：

        - ``no_change``／``ok``：不會降低叢集，可直接 ``set_quota_minimums``；
        - ``confirmation_required``：會降低叢集且目前用量允許，附 ``confirmation``（確認碼）、
          ``decreases`` 與 ``warning``；
        - ``rejected``：讀取失敗、缺項、舊 operator 或低於用量，``reasons`` 說明原因。
        """
        workspace, old, new = self._quota_minimum_change(cpu, memory_gi, storage_gi)
        if new == old:
            return {'state': 'no_change'}
        target, decreases, problems = workspace._quota_minimum_review(old, new)
        if problems:
            return {'state': 'rejected', 'reasons': problems, 'decreases': decreases}
        if not decreases:
            return {'state': 'ok', 'decreases': []}
        return {
            'state': 'confirmation_required',
            'confirmation': workspace._sign_lower_confirmation(new, target),
            'decreases': decreases,
            'warning': _lower_warning(),
            'expires_in_minutes': int(LOWER_CONFIRMATION_TTL.total_seconds() // 60),
        }

    def set_quota_minimums(self, cpu=None, memory_gi=None, storage_gi=None, confirmation=None):
        """管理員設定 CPU（cores）、memory、storage（Gi）最低保障，保存後立即同步。

        參數為 ``None`` 表示該項不變更；``0``／``False`` 表示清除（沿用公式）。
        #1681：會降低叢集 quota 的變更必須帶 ``preview_quota_minimums`` 給的確認碼；確認碼
        必須仍符合目前的 Workspace、操作者、政策版本、最低保障、目標與 namespace，且鎖內
        再檢查一次用量。任一項不符、讀取失敗、缺項或低於用量就拒絕，不保存。
        回傳同步結果摘要。
        """
        workspace, old, new = self._quota_minimum_change(cpu, memory_gi, storage_gi)
        if new == old:
            return workspace._quota_status_summary()
        target, decreases, problems = workspace._quota_minimum_review(old, new)
        if problems:
            raise UserError(_(
                'The quota cannot be lowered safely, so the minimums were not saved: '
                '%(reasons)s', reasons='; '.join(problems)))
        if decreases:
            if not confirmation:
                raise UserError(_(
                    'This change lowers the cluster quota and needs confirmation. Review it '
                    'first; the minimums were not saved.'))
            workspace._verify_lower_confirmation(confirmation, new, target)

        version = workspace.quota_target_version + 1
        vals = dict(new, quota_target_version=version, quota_sync_state='pending',
                    quota_sync_reason=False, quota_lower_authorization=(
                        workspace._quota_lower_authorization_key(version, target)
                        if decreases else False))
        _internal_quota_policy_write(workspace).write(vals)
        workspace._log_quota_minimum_change(old, new, version, decreases)
        return workspace._quota_sync('admin')

    # --------------------------------------------------------------------- #
    #  #1681 降額確認碼                                                      #
    # --------------------------------------------------------------------- #
    def _lower_confirmation_claims(self, new, target):
        """確認碼綁定的內容（不含到期時間）。"""
        self.ensure_one()
        return {'workspace': self.id, 'uid': self.env.uid, 'version': self.quota_target_version,
                'floors': {name: new[name] for name in sorted(FLOOR_FIELDS)},
                'target': {key: target.get(key) for key in sorted(target)},
                'namespaces': self._quota_namespaces()}

    def _sign_lower_confirmation(self, new, target):
        expires = int((fields.Datetime.now() + LOWER_CONFIRMATION_TTL).timestamp())
        body = json.dumps(dict(self._lower_confirmation_claims(new, target), exp=expires),
                          sort_keys=True, separators=(',', ':'))
        signature = odoo_hmac(self.env(su=True), _LOWER_CONFIRMATION_SCOPE, body)
        return '%s.%s' % (base64.urlsafe_b64encode(body.encode()).decode(), signature)

    def _verify_lower_confirmation(self, confirmation, new, target):
        """確認碼必須是伺服器簽的、未過期，而且內容與這一次完全相同。"""
        invalid = UserError(_(
            'The quota decrease confirmation is invalid, expired or for a different change. '
            'Review the change again; the minimums were not saved.'))
        try:
            encoded, signature = str(confirmation).rsplit('.', 1)
            body = base64.urlsafe_b64decode(encoded.encode()).decode()
            claims = json.loads(body)
        except (ValueError, TypeError, UnicodeDecodeError):
            raise invalid from None
        expected = odoo_hmac(self.env(su=True), _LOWER_CONFIRMATION_SCOPE, body)
        if not consteq(expected, signature) or not isinstance(claims, dict):
            raise invalid
        expires = claims.pop('exp', None)
        if not isinstance(expires, int) or expires < fields.Datetime.now().timestamp():
            raise invalid
        if claims != json.loads(json.dumps(self._lower_confirmation_claims(new, target))):
            raise invalid

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

    def action_retry_quota_sync(self):
        """#1680 管理員手動重試：重置自動重試額度，以最新政策與服務集合立即同步。

        與背景重試走同一個同步（同樣的鎖、讀取、安全降額與防護），所以不能用來繞過任何安全
        檢查——#1682 起與背景一樣會把已保存的目標安全降下來（重讀用量、5 項全部容得下才降），
        非降不可的項（例如 pvc）與用量不足的降額照樣不送。不改政策，所以不寫異動紀錄
        （ledger）；只在監控 chatter 留一行誰在何時手動重試。
        """
        if not self.env.user.has_group('base.group_system'):
            raise AccessError(_('Only system administrators may retry Workspace quota sync.'))
        self.ensure_one()
        self.check_access('read')
        workspace = self.sudo()
        workspace._lock_quota_policy()
        workspace.invalidate_recordset(list(PROTECTED_FIELDS))
        actor = self.env.user
        self.env['woow_paas_platform.platform_monitor']._singleton()._message_log(body=_(
            'Workspace %(name)s (id=%(id)s, %(ws_hash)s) quota sync retried manually by '
            '%(login)s (uid %(uid)s)', name=workspace.name, id=workspace.id,
            ws_hash=workspace.ws_hash, login=actor.login, uid=actor.id))
        # #1682：與背景重試同一套安全降額（重讀用量、全部安全才降），避免「背景會降、手動不降」。
        result = workspace._quota_sync('manual', allow_lower=True)
        state = result.get('state')
        if state == 'synced':
            message, kind = _('Quota synced with the cluster.'), 'success'
        else:
            message, kind = _('Quota not synced: %(reason)s',
                              reason=result.get('reason') or state or ''), 'warning'
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': _('Workspace quota'), 'message': message, 'type': kind,
                       'sticky': kind != 'success',
                       'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'}},
        }

    def _log_quota_minimum_change(self, old, new, version, decreases=()):
        """稽核：append-only ledger（結構化）＋ admin-only monitor chatter（無通知）。

        #1681：確認過的降額另在 chatter 列出每項叢集值、目標與確認當下的用量。
        """
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
        body = _(
            'Workspace %(name)s (id=%(id)s, %(ws_hash)s) quota minimums changed by %(login)s '
            '(uid %(uid)s), policy version %(version)s: %(old)s -> %(new)s',
            name=self.name, id=self.id, ws_hash=self.ws_hash, login=actor.login, uid=actor.id,
            version=version, old=_summary(old), new=_summary(new))
        if decreases:
            body += '\n' + _('Confirmed quota decrease (usage checked at confirmation): '
                             '%(items)s', items='; '.join(_decrease_text(d) for d in decreases))
        monitor._message_log(body=body)

    # --------------------------------------------------------------------- #
    #  同步（不降額）                                                        #
    # --------------------------------------------------------------------- #
    def _lock_quota_policy(self, nowait=False):
        """對 workspace 列取 ``FOR NO KEY UPDATE``，序列化同一工作區的 quota 推送。

        管理員入口（``nowait=False``）等到拿到為止。REPEATABLE READ 下，對方已 commit 的更新
        會讓本交易拿鎖時 40001，請求層會重播，重播後讀到新政策。

        生命週期與開通路徑（``nowait=True``）拿不到就回 ``False``，交易仍可用（savepoint
        回捲）。不等鎖：持鎖者最後一定會寫這一列，REPEATABLE READ 下等到對方 commit 只會換來
        40001，等待只是拖慢刪除與開通。呼叫端拿不到鎖就不推送、也不寫這一列——鎖外推送可能
        蓋掉持鎖者剛提高的值（舊 operator 不支援版本防護，沒有 compare-and-set），寫這一列
        則會在對方 commit 後 40001、在已呼叫 operator 之後整筆回捲。
        """
        self.ensure_one()
        cr = self.env.cr
        query = f"SELECT id FROM {self._table} WHERE id = %s FOR NO KEY UPDATE"
        if not nowait:
            cr.execute(query, (self.id,))
            return True
        try:
            # 同 core ir_cron 取鎖的做法：預期中的衝突不該在 sql_db 印成 ERROR「bad query」。
            with mute_logger('odoo.sql_db'), cr.savepoint(flush=False):
                cr.execute(query + ' NOWAIT', (self.id,))
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

    def _quota_expected_tokens(self):
        self.ensure_one()
        try:
            tokens = json.loads(self.quota_expected_tokens or '{}')
        except ValueError:
            return {}
        return tokens if isinstance(tokens, dict) else {}

    def _quota_attempt_fingerprint(self, target, own, attempted):
        """這次嘗試要記的目標指紋。

        生命週期與開通只同步自己那一個 namespace；工作區若不只這一個，子集的結果不能代表整個
        工作區（會把其他 namespace 的狀態蓋掉）。這時回 ``_SUBSET_ATTEMPT``：不記指紋、不動
        重試額度，改以 ``quota_reconcile_pending`` 標記，並立刻排背景同步重新評估整個工作區。

        #1682：以前回空指紋兼任「觸發對帳」與「重置額度」，同目標的生命週期事件就會讓已耗盡的
        工作區重新拿到 5 次自動重試；指紋也不能改記子集的值，否則會在子集與全集之間來回。
        """
        if set(attempted) == set(own):
            return _fingerprint(target, own)
        self._schedule_quota_reconcile()
        return _SUBSET_ATTEMPT

    def _schedule_quota_reconcile(self, at=None):
        """喚醒背景同步 cron（``ir.cron.trigger`` 是 DB 列，跟著呼叫端交易 commit／回捲）。"""
        cron = self.env.ref(_RECONCILE_CRON, raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger(at)

    def _quota_sync(self, trigger, namespaces=None, *, nowait=False, allow_lower=None):
        """把目前目標推到叢集，記錄結果並回傳摘要。

        ``trigger``：admin／manual（人的動作，重置重試額度）、retry（背景重試，消耗額度）、
        reconcile（背景發現目標已變）、verify（定期讀回）、lifecycle、provision。
        ``nowait=True``（背景與生命週期路徑）：拿不到鎖回 ``busy``，不推送、不寫這一列，
        改排 cron 一分鐘後再看（``BUSY_RETRY``）。管理員入口已先拿鎖，不帶這個參數。

        #1681／#1682 ``allow_lower``：``True`` 可降到目標（每次都重讀 used 重檢，見
        ``plan_safe_lower``）；``False`` 不降額；``None``（預設）只在管理員確認過、且目前政策
        版本與目標仍等於確認時的那一個才降（``quota_lower_authorization``）。這是伺服器端
        參數，底線方法無法經 RPC 呼叫，context 也影響不了它。
        """
        self.ensure_one()
        workspace = self.sudo()
        if nowait and not workspace._lock_quota_policy(nowait=True):
            workspace._schedule_quota_reconcile(fields.Datetime.now() + BUSY_RETRY)
            return {'state': 'busy', 'trigger': trigger}
        version = workspace.quota_target_version
        target = self.env['woow_paas_platform.cloud_service'].sudo()._ns_quota_payload(workspace)
        if allow_lower is None:
            allow_lower = workspace._quota_lower_authorized(target)
        own = workspace._quota_namespaces()
        attempted = sorted(set(namespaces)) if namespaces else own
        client = paas_operator_service.get_paas_operator_client(self.env(context={}))
        expected = workspace._quota_expected_tokens()
        results = {}
        for namespace in attempted:
            results[namespace] = _sync_namespace(
                client, namespace, target, expected.get(namespace), allow_lower=bool(allow_lower))
        fingerprint = workspace._quota_attempt_fingerprint(target, own, attempted)
        workspace._record_quota_sync(version, results, fingerprint, trigger)
        if (workspace.quota_lower_authorization and fingerprint
                and workspace.quota_sync_state == 'synced'):
            # 確認過的目標已整個工作區落地：授權用完即清，之後同一目標不再沿用這次確認。
            _internal_quota_policy_write(workspace).write({'quota_lower_authorization': False})
        summary = workspace._quota_status_summary()
        summary['trigger'] = trigger
        return summary

    @staticmethod
    def _quota_lower_authorization_key(version, target):
        """確認綁定的內容：政策版本＋完整目標 payload。"""
        return '%s:%s' % (version, _fingerprint(target, []))

    def _quota_lower_authorized(self, target):
        """目前政策版本與目標是否仍等於管理員確認過的降額。"""
        self.ensure_one()
        return bool(self.quota_lower_authorization) and (
            self.quota_lower_authorization == self._quota_lower_authorization_key(
                self.quota_target_version, target))

    def _record_quota_sync(self, version, results, fingerprint=None, trigger=None):
        """記錄一次同步嘗試。``fingerprint`` 為 ``_SUBSET_ATTEMPT`` 表示只同步了子集。"""
        self.ensure_one()
        now = fields.Datetime.now()
        previous_state = self.quota_sync_state
        previous_count = self.quota_retry_count or 0
        subset = fingerprint is _SUBSET_ATTEMPT
        if subset:
            # 子集不代表整個工作區：指紋維持上一次全集嘗試的值（重試額度只跟著全集目標走）。
            fingerprint = self.quota_attempt_fingerprint
        retryable = False
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
            unguarded = sorted(ns for ns, r in results.items() if r.get('guarded') is False)
            if unguarded:
                reasons.append(
                    'remote_guard_unavailable: %s (the operator does not support versioned '
                    'quota writes; pushes are made without protection against late requests)'
                    % ', '.join(unguarded))
            pending = ['%s %s: cluster %s, target %s' % (ns, key, live, wanted)
                       for ns, r in sorted(results.items())
                       for key, live, wanted in r.get('pending_decrease', ())]
            tokens = self._quota_expected_tokens()
            for ns, r in results.items():
                if r.get('expect_token'):
                    tokens[ns] = r['expect_token']
            vals = {'quota_sync_state': state, 'quota_sync_reason': '\n'.join(reasons) or False,
                    'quota_pending_decrease': '\n'.join(pending) or False,
                    'quota_sync_unguarded': bool(unguarded),
                    'quota_expected_tokens': json.dumps(tokens, sort_keys=True) if tokens else False}
            if state == 'synced' and subset:
                # 子集成功不等於整個工作區已同步：只補記這幾個 namespace 推了什麼。
                try:
                    pushed = json.loads(self.quota_synced_hard or '{}')
                except ValueError:
                    pushed = {}
                if not isinstance(pushed, dict):
                    pushed = {}
                pushed.update({ns: r['hard'] for ns, r in results.items() if r.get('hard')})
                vals['quota_synced_hard'] = json.dumps(pushed, sort_keys=True) if pushed else False
            elif state == 'synced':
                vals.update({
                    'quota_synced_version': version,
                    'quota_synced_at': now,
                    'quota_synced_hard': json.dumps(
                        {ns: r['hard'] for ns, r in sorted(results.items()) if r.get('hard')},
                        sort_keys=True),
                    'quota_synced_fingerprint': fingerprint or False,
                    'quota_verified_at': now,
                })
            retryable = superseded or any(r.get('retryable') for r in results.values()
                                          if r['state'] != 'synced')
        # 重試額度：以目標指紋計；人的動作（保存、手動重試）也重置。背景重試每次消耗一格。
        # 子集嘗試不重置、不消耗，成功也不歸零（其他 namespace 可能仍在失敗）。
        reset = fingerprint != self.quota_attempt_fingerprint or trigger in _RESETTING_TRIGGERS
        count = 0 if reset else previous_count + (1 if trigger == 'retry' else 0)
        next_at = False
        if vals['quota_sync_state'] == 'synced':
            if not subset:
                count = 0
        elif retryable:
            if count >= MAX_AUTO_RETRIES:
                vals['quota_sync_state'] = 'exhausted'
            else:
                next_at = now + timedelta(minutes=RETRY_BACKOFF_MINUTES[count])
        # 不會自動降額的同步（管理員只提高、開通）留下 CPU／memory／storage 的待處理降額：
        # 交給背景以生命週期的安全降額收回（每次仍重讀用量檢查）。
        reclaim = trigger in _NON_RECLAIMING_TRIGGERS and any(
            item[0] in _RECLAIMABLE_KEYS
            for r in (results or {}).values() for item in r.get('pending_decrease', ()))
        vals.update({'quota_attempt_fingerprint': fingerprint or False,
                     'quota_last_attempt_at': now, 'quota_retry_count': count,
                     'quota_next_retry_at': next_at,
                     'quota_reconcile_pending': subset or reclaim})
        _internal_quota_policy_write(self).write(vals)
        if next_at:
            self._schedule_quota_reconcile(next_at)
        if reclaim:
            self._schedule_quota_reconcile()
        # 只在這一次用掉最後一格時通知；同目標的子集事件或對帳讓狀態回到 exhausted 不算新的耗盡。
        if (vals['quota_sync_state'] == 'exhausted' and previous_state != 'exhausted'
                and previous_count < MAX_AUTO_RETRIES):
            self.env['woow_paas_platform.platform_monitor']._singleton()._message_log(body=_(
                'Workspace %(name)s (id=%(id)s, %(ws_hash)s) quota sync stopped after '
                '%(count)s automatic retries: %(reason)s. Retry it manually from the Workspace '
                'quota page.', name=self.name, id=self.id, ws_hash=self.ws_hash,
                count=MAX_AUTO_RETRIES, reason=vals.get('quota_sync_reason') or ''))

    # --------------------------------------------------------------------- #
    #  背景同步（ir.cron）                                                   #
    # --------------------------------------------------------------------- #
    @api.model
    def _cron_reconcile_workspace_quotas(self, limit=50):
        """背景重試／對帳入口：每個工作區各自開一筆交易（新 cursor），處理完就 commit。

        各自交易：快照在拿鎖前才建立（看得到別人剛 commit 的政策與服務集合），而且不會把
        前面處理過的工作區鎖到整批結束（那會讓開通拿不到鎖而失敗）。
        """
        # 上限只算「需要處理」的工作區：先切片再判斷，會讓 id 較大的工作區永遠輪不到。
        due = [workspace.id for workspace in self.sudo()._quota_reconcile_candidates()
               if workspace._quota_reconcile_trigger()]
        for workspace_id in due[:limit]:
            try:
                with self.env.registry.cursor() as cr:
                    env = api.Environment(cr, SUPERUSER_ID, {})
                    env[self._name].browse(workspace_id)._quota_reconcile_one()
            except Exception as exc:  # noqa: BLE001 —— 一個工作區失敗不影響其他工作區
                _logger.exception("[#1680] quota reconcile failed workspace=%s", workspace_id)
                self._record_quota_reconcile_error(workspace_id, exc)
        if len(due) > limit:
            self._schedule_quota_reconcile()

    @api.model
    def _record_quota_reconcile_error(self, workspace_id, exc):
        """逃出同步的例外（整筆交易已回捲）另開一筆交易記成可重試的失敗。

        不記的話，指紋、額度與下次重試時間都跟著回捲，下一輪又當成新目標重來——有限重試就被
        繞過了。這裡走同一個 ``_record_quota_sync``，所以同樣受 5 次上限約束。
        """
        try:
            with self.env.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                workspace = env[self._name].browse(workspace_id)
                if not workspace.exists() or not workspace._lock_quota_policy(nowait=True):
                    return
                target = env['woow_paas_platform.cloud_service'].sudo()._ns_quota_payload(workspace)
                own = workspace._quota_namespaces()
                failure = {'state': 'pending', 'retryable': True,
                           'reason': 'internal_error: %s' % type(exc).__name__}
                trigger = 'retry' if workspace._quota_reconcile_trigger() == 'retry' else 'reconcile'
                workspace._record_quota_sync(
                    workspace.quota_target_version, {ns: dict(failure) for ns in own} or
                    {'-': dict(failure)}, _fingerprint(target, own), trigger)
        except Exception:  # noqa: BLE001 —— 連記錄都失敗：只留 log，兜底 cron 仍會再看
            _logger.exception("[#1680] could not record quota reconcile error workspace=%s",
                              workspace_id)

    def _quota_reconcile_candidates(self):
        live = self.env['woow_paas_platform.cloud_service'].sudo().search([
            ('state', '!=', 'deleting'), ('workspace_id', '!=', False)]).mapped('workspace_id')
        return self.search([
            '|', ('id', 'in', live.ids),
            '|', ('quota_reconcile_pending', '=', True),
            ('quota_sync_state', 'in', ('pending', 'unconfirmed', 'blocked', 'exhausted')),
        ], order='id')

    def _quota_reconcile_trigger(self):
        """背景要不要同步這個工作區、以哪一種觸發：目標已變、重試到期、該讀回驗證；否則 ``None``。"""
        self.ensure_one()
        workspace = self.sudo()
        now = fields.Datetime.now()
        target = self.env['woow_paas_platform.cloud_service'].sudo()._ns_quota_payload(workspace)
        state = workspace.quota_sync_state
        if (workspace.quota_reconcile_pending
                or _fingerprint(target, workspace._quota_namespaces())
                != workspace.quota_attempt_fingerprint):
            return 'reconcile'
        if (state in ('pending', 'unconfirmed', 'blocked') and workspace.quota_next_retry_at
                and workspace.quota_next_retry_at <= now):
            return 'retry'
        if state == 'synced' and (not workspace.quota_verified_at
                                  or workspace.quota_verified_at <= now - VERIFY_INTERVAL):
            return 'verify'
        return None

    def _quota_reconcile_one(self):
        """單一工作區的背景判斷：目標已變、重試到期、或該讀回驗證才同步；其餘不動。"""
        self.ensure_one()
        workspace = self.sudo()
        if not workspace.exists():
            return {'state': 'skipped'}
        trigger = workspace._quota_reconcile_trigger()
        if not trigger:
            return {'state': 'skipped'}
        # #1682：背景是生命週期降額的後續（被用量擋下的降額要靠它收回），一律走安全降額。
        # 已保存的政策都是合法的（未確認的人工降額不會被保存，見 #1681），所以不必分辨降額來源。
        return workspace._quota_sync(trigger, nowait=True, allow_lower=True)

    def _quota_create_namespace(self, client, namespace):
        """開通路徑（``create_namespace``）的同一個政策：必要時才降級成不送 quota。

        ``create_namespace`` 同時負責 namespace 基線（LimitRange、NetworkPolicy、PSA、
        pull secret），所以只要能證明不降額就一定要呼叫：

        - 叢集上沒有受管 quota（list 成功、確定沒有）⇒ 以目標建立，沒有既有值可降；
        - 有受管 quota ⇒ 以「每項 max(目標, 叢集)」呼叫；
        - 套用會降低某一項（例如 pvc 現況大於 10）⇒ 不呼叫（namespace 已存在、基線已在），
          記為 blocked，開通照常繼續；
        - 讀不到或資料不完整 ⇒ 回 ``(False, reason)``，由呼叫端讓開通失敗（不能證明不降額）；
        - 工作區正被其他交易同步、拿不到鎖 ⇒ 回 ``(False, 'quota_sync_busy')``：不讀、不送、
          不寫，開通失敗待重試（鎖外推送可能蓋掉對方剛提高的值）。

        #1680：quota 寫入與其他路徑同一套防護——帶新 token 與讀到的 resourceVersion（沒有
        受管 quota 時是 ``expect_absent``）。前提不成立時 operator 照做基線、不覆寫 quota，
        這裡記為待同步並排背景重試，開通照常繼續。

        回傳 ``(ok, reason)``；``create_namespace`` 的 operator 錯誤原樣 raise，維持既有的
        開通失敗處理。
        """
        self.ensure_one()
        workspace = self.sudo()
        if not workspace._lock_quota_policy(nowait=True):
            return False, 'quota_sync_busy'
        version = workspace.quota_target_version
        target = self.env['woow_paas_platform.cloud_service'].sudo()._ns_quota_payload(workspace)
        fingerprint = workspace._quota_attempt_fingerprint(
            target, workspace._quota_namespaces(), [namespace])
        plan = _plan_namespace(client, namespace, target)
        token = uuid.uuid4().hex if plan.get('guarded') else None
        if plan.get('not_found'):
            payload, plan = target, dict(plan, state='synced', reason=None, retryable=False,
                                         hard=build_hard(target), pending_decrease=())
            guard = {'sync_token': token, 'expect_absent': True}
        elif plan['state'] == 'synced':
            payload = plan['payload']
            guard = {'sync_token': token, 'expected_resource_version': plan['resource_version']}
        elif plan['reason'].startswith('would_lower'):
            workspace._record_quota_sync(version, {namespace: plan}, fingerprint, 'provision')
            return True, plan['reason']
        else:
            workspace._record_quota_sync(version, {namespace: plan}, fingerprint, 'provision')
            return False, plan['reason']
        extra = {'quota_guard': guard} if token else {}
        result = _push_result(plan, lambda: client.create_namespace(
            namespace=namespace, cpu_limit=payload['cpu_limit'],
            memory_limit=payload['memory_limit'], storage_limit=payload['storage_limit'],
            workspace_id=self.id or None,
            ephemeral_storage_request=payload.get('ephemeral_storage_request'),
            ephemeral_storage_limit=payload.get('ephemeral_storage_limit'), **extra),
            lambda: _read_back(client, namespace, token, payload), token)
        workspace._record_quota_sync(version, {namespace: result}, fingerprint, 'provision')
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


def _lower_warning():
    return _('Current usage passing this check does not guarantee room for future pod '
             're-creation or rollouts. It is not a capacity reservation and is not atomic with '
             'Kubernetes admission: usage can grow between this check and the quota update.')


def _decrease_text(item):
    return '%(namespace)s %(key)s: %(cluster)s -> %(target)s (used %(used)s)' % item


def _review_lower(client, namespaces, target):
    """#1681 管理員降額前檢查：每個 namespace 讀受管 quota，以共用 ``plan_safe_lower`` 判斷。

    回傳 ``(decreases, problems)``：``decreases`` 是會降的項（dict：namespace、key、cluster、
    target、used）；``problems`` 非空就不能保存。確認 namespace 尚不存在的沒有可降的 quota。
    """
    decreases, problems = [], []
    if not client:
        return decreases, ['operator_not_configured']
    for namespace in namespaces:
        try:
            report = client.get_namespace_quota(namespace, include_managed=True)
        except PaaSOperatorError as exc:
            unsupported = getattr(exc, 'status_code', None) in (404, 405)
            problems.append('%s: %s' % (
                namespace, 'operator_unsupported' if unsupported else 'quota_unreadable'))
            continue
        managed = report.get('managed_quota') if isinstance(report, dict) else None
        if (isinstance(managed, dict) and managed.get('available') is not True
                and managed.get('reason') == 'managed_quota_not_found'
                and managed.get('namespace_exists') is False):
            continue
        plan = plan_safe_lower(target, managed)
        problems.extend('%s: %s %s' % (namespace, key, reason) if key != '*'
                        else '%s: %s' % (namespace, reason) for key, reason in plan['blocked'])
        if plan['lowering_unsafe']:
            problems.append('%s: would_lower: %s' % (
                namespace, ', '.join(sorted(plan['lowering_unsafe']))))
        decreases.extend({'namespace': namespace, 'key': key, 'cluster': cluster,
                          'target': wanted, 'used': used}
                         for key, cluster, wanted, used in plan['lowered'])
    return decreases, problems


def _plan_namespace(client, namespace, target, allow_lower=False):
    """讀取→合併→逐項檢查；不推送。回傳 dict（不 raise）。

    #1681／#1682 ``allow_lower``：以共用的 ``plan_safe_lower`` 判斷能不能降到目標（逐項檢查
    5 項 used、缺項／舊 operator fail-closed）。全部安全才降；任一項不安全就只送增額
    （同不降額合併），並在 ``unsafe_decrease`` 帶原因，由 ``_sync_namespace`` 記為可重試的
    blocked。不帶＝#1679／#1680 的不降額。

    ``push``：可以送出 ``payload``（不會降低任何一項）；``not_found``：叢集上沒有受管
    quota（list 成功、確定沒有這個物件）。其餘情況 ``state`` 已是最終結果。
    #1680：``retryable`` 標示暫時性失敗（值得自動重試）；``guarded`` 為 operator 是否支援
    版本防護（GET 有沒有 ``sync_token`` 鍵——舊 operator 會靜默忽略 PUT 的未知欄位，不能
    用 PUT 有沒有報錯判斷）；``remote_token``／``resource_version`` 是叢集目前的版本。
    """
    if not client:
        return {'state': 'pending', 'reason': 'operator_not_configured', 'retryable': True}
    try:
        report = client.get_namespace_quota(namespace, include_managed=True)
    except PaaSOperatorError as exc:
        unsupported = getattr(exc, 'status_code', None) in (404, 405)
        return {'state': 'blocked',
                'reason': 'operator_unsupported' if unsupported else 'quota_unreadable',
                'retryable': not unsupported}
    managed = report.get('managed_quota') if isinstance(report, dict) else None
    if not isinstance(managed, dict):
        return {'state': 'blocked', 'reason': 'operator_unsupported', 'retryable': False}
    guarded = 'sync_token' in managed
    if managed.get('available') is not True:
        reason = managed.get('reason') or 'managed_quota_unavailable'
        if reason == 'managed_quota_not_found':
            if managed.get('namespace_exists') is False:
                # 確認 namespace 尚不存在：政策已保存，等首次開通套用；不建立空 namespace。
                return {'state': 'pending_first_deploy', 'reason': 'namespace_not_created',
                        'not_found': True, 'guarded': guarded, 'retryable': False}
            return {'state': 'blocked', 'reason': reason, 'not_found': True, 'guarded': guarded,
                    'retryable': False}
        return {'state': 'blocked', 'reason': reason, 'retryable': reason in _TRANSIENT_BLOCKED}
    lowered, unsafe = [], []
    if allow_lower:
        safe = plan_safe_lower(target, managed)
        if safe['payload'] is None:
            return {'state': 'blocked', 'reason': safe['blocked'][0][1], 'retryable': False}
        payload, lowering, pending = (
            safe['payload'], safe['lowering_unsafe'], safe['pending_decrease'])
        lowered, unsafe = safe['lowered'], safe['blocked']
    else:
        try:
            payload, lowering, pending = merge_never_lower(
                target, managed.get('spec_hard'), managed.get('hard'))
        except QuotaNotProvable as exc:
            return {'state': 'blocked', 'reason': 'quota_not_provable: %s' % exc,
                    'retryable': False}
    if lowering:
        # 永久阻擋：叢集上的值（例如 pvc 12）高於 operator 一定會寫的值；不自動重試。
        return {'state': 'blocked', 'reason': 'would_lower: %s' % ', '.join(sorted(lowering)),
                'pending_decrease': pending, 'retryable': False}
    hard = build_hard(payload)
    spec_hard = managed.get('spec_hard') or {}
    unchanged = all(parse_quantity(spec_hard.get(key)) == parse_quantity(value)
                    for key, value in hard.items())
    return {'state': 'synced', 'hard': hard, 'payload': payload, 'pending_decrease': pending,
            'push': not unchanged, 'guarded': guarded, 'remote_token': managed.get('sync_token'),
            'resource_version': managed.get('resource_version'), 'lowered': lowered,
            'unsafe_decrease': '; '.join('%s %s' % item for item in unsafe) or None}


def _read_back(client, namespace, token, payload):
    """推送結果不明時讀回：``True`` 已落地、``False`` 未落地（仍可能在途）、``None`` 讀不到。

    有 token（新 operator）只認 token；舊 operator 只能比值（推送前值不同才會推，所以值相等
    就是落地了）。
    """
    try:
        report = client.get_namespace_quota(namespace, include_managed=True)
    except PaaSOperatorError:
        return None
    managed = report.get('managed_quota') if isinstance(report, dict) else None
    if not isinstance(managed, dict):
        return None
    if token:
        if 'sync_token' not in managed:
            return None
        return managed.get('sync_token') == token
    spec_hard = managed.get('spec_hard')
    if not isinstance(spec_hard, dict) or not spec_hard:
        return None
    return all(parse_quantity(spec_hard.get(key)) == parse_quantity(value)
               for key, value in build_hard(payload).items())


def _push_result(plan, call, readback=None, token=None):
    """執行推送（``call()``），把 operator 的結果轉成同步狀態。

    結果不明（逾時、5xx、斷線）先讀回：落地就算成功（回應遺失而已）；否則記下在途 token
    （``expect_token``）——遠端之後帶的若不是它，下一次同步必定以新 token 重推，讓還在途的
    舊請求因 resourceVersion 已變而再也落不下。409 ``quota_conflict`` 與其他 4xx 是確定沒寫。
    """
    base = {'pending_decrease': plan.get('pending_decrease', ()), 'guarded': plan.get('guarded')}
    try:
        response = call()
    except PaaSOperatorTimeoutError as exc:
        # operator 端的 kubectl 可能已套用：先讀回，讀不出來就是結果未知，不是失敗也不是成功。
        landed = readback() if readback else None
        if landed:
            return dict(plan, state='synced', expect_token=token, error=exc,
                        reason='operator_timeout (read back: applied)')
        return dict(base, state='unconfirmed', retryable=True, expect_token=token, error=exc,
                    reason='operator_timeout' + (' (read back failed)' if landed is None else ''))
    except PaaSOperatorError as exc:
        code = getattr(exc, 'status_code', None)
        if code == 409 and plan.get('guarded'):
            return dict(base, state='pending', reason='quota_conflict', retryable=True, error=exc)
        if code in _TRANSIENT_HTTP:
            # 確定沒寫（operator 在寫入前就拒絕），但重試可能就好（限流、逾時、憑證剛修好）。
            return dict(base, state='pending', reason='operator_error: %s' % code,
                        retryable=True, error=exc)
        if code is not None and 400 <= code < 500:
            # 請求本身被拒（形狀、路徑）：重試也不會好，要人處理。
            return dict(base, state='blocked', reason='operator_error: %s' % code,
                        retryable=False, error=exc)
        landed = readback() if readback else None
        detail = code or exc.message
        if landed:
            return dict(plan, state='synced', expect_token=token, error=exc,
                        reason='operator_error: %s (read back: applied)' % detail)
        return dict(base, state='pending', reason='operator_error: %s' % detail,
                    retryable=True, expect_token=token, error=exc)
    if isinstance(response, dict) and response.get('quota') == 'conflict':
        # 建立路徑：基線已做、quota 沒覆寫（前提不成立）。
        return dict(base, state='pending', reason='quota_conflict', retryable=True)
    return dict(plan, state='synced', expect_token=token)


def _sync_namespace(client, namespace, target, expected_token=None, allow_lower=False):
    """單一 namespace 的同步（PUT）；回傳結果 dict（不 raise）。

    #1680：值不變但遠端 token 不是平台預期的那一個（別的寫入落在那裡）也要重推一次（圍籬）。
    #1681／#1682 ``allow_lower``：見 ``_plan_namespace``。降額被用量擋下時增額照送，結果記為
    ``blocked``／``unsafe_decrease``（可重試，叢集保留原額度）。
    """
    plan = _plan_namespace(client, namespace, target, allow_lower=allow_lower)
    if plan['state'] != 'synced':
        return plan
    guarded = plan['guarded']
    if guarded and plan['remote_token'] != expected_token:
        plan = dict(plan, push=True)
    if not plan.get('push'):
        return _unsafe_decrease_result(plan)
    payload = plan['payload']
    token = uuid.uuid4().hex if guarded else None
    extra = {'quota_guard': {'sync_token': token,
                             'expected_resource_version': plan['resource_version']}} if guarded else {}
    return _unsafe_decrease_result(_push_result(plan, lambda: client.update_namespace_quota(
        namespace, payload['cpu_limit'], payload['memory_limit'], payload['storage_limit'],
        ephemeral_storage_request=payload.get('ephemeral_storage_request'),
        ephemeral_storage_limit=payload.get('ephemeral_storage_limit'), **extra),
        lambda: _read_back(client, namespace, token, payload), token))


def _unsafe_decrease_result(result):
    """推送（或不需推送）成功，但想降的項被用量擋下：目前目標沒有達成，記為可重試的 blocked。

    增額與圍籬 token 已落地（``expect_token`` 保留），叢集保留原額度；每次重試都重讀 used 重判。
    """
    if result.get('state') != 'synced' or not result.get('unsafe_decrease'):
        return result
    return dict(result, state='blocked', retryable=True,
                reason='unsafe_decrease: %s' % result['unsafe_decrease'])

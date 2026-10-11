"""#1647：工作區頁顯示 CPU／記憶體／磁碟配額的上限與已佔用量。

2026-10-10 正式站事故：一台服務的 CPU limit 被調到超過工作區 namespace 的 ``limits.cpu``
上限，新 pod 被 ResourceQuota 拒絕、停了近六小時；操作的人事前不知道上限是多少，前台
也看不到。operator 新增 ``GET /api/namespaces/{name}/quota``（唯讀），本檔釘住平台端：

* **換算**（``services/workspace_quota.build_view``）：CPU／記憶體看 ``limits.*``、磁碟看
  ``requests.storage``；bytes → GB 以 1024³；等級與百分比只由 hard／used 算（不採用
  operator 的 ``ratio``）；垃圾輸入不 raise。
* **端點**（``POST /api/workspaces/<id>/quota``）：工作區任何成員都能看（含 Portal 帳號——
  真實租戶的帳號類型）；非成員拿到與服務清單同一句錯誤，而且 operator **一次都不會被
  呼叫**；送給 operator 的 namespace 是伺服器由 ``ws_hash`` 推導的；沒有服務、只有隱藏
  實例、舊 namespace、operator 未設定、operator 太舊、operator 失敗各有自己的 reason；
  端點不寫任何 record。
* **client**：``get_namespace_quota`` 的路徑與逾時；三層逾時維持階梯（前端 ＞ 平台 ＞
  ``DEFAULT_TIMEOUT`` 以下）。
* **前端**（靜態讀檔）：面板有掛上工作區頁、等級讀後端欄位、文案都有 zh_TW。
"""
import os
import re
from unittest.mock import MagicMock, call, patch

from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, tagged
from odoo.tools.translate import PoFileReader

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from odoo.addons.woow_paas_platform.models.workspace_access import (
    ROLE_ADMIN, ROLE_GUEST, ROLE_USER,
)
from ..models.cloud_service import _internal_provision
from ..services import workspace_quota
from ..services.naming import make_namespace
from ..services.paas_operator import (
    DEFAULT_TIMEOUT,
    NAMESPACE_QUOTA_TIMEOUT,
    PaaSOperatorAPIError,
    PaaSOperatorClient,
    PaaSOperatorConnectionError,
    PaaSOperatorTimeoutError,
)

# 兩個 seam 都打在 controller 自己的 namespace：`request` 與 `get_paas_operator_client`
# 在 controllers/paas.py 都是檔頭 import，端點查名字的地方就是那裡
# （docs/decisions/patterns/unittest-mock-dual-namespace-patch.md）。
_REQUEST_SEAM = 'odoo.addons.woow_paas_platform.controllers.paas.request'
_CLIENT_SEAM = 'odoo.addons.woow_paas_platform.controllers.paas.get_paas_operator_client'
_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PAAS = os.path.join(_SRC, 'static', 'src', 'paas')

NOT_FOUND = 'Workspace not found or access denied'
PREFIX_PARAM = 'woow_paas_platform.namespace_prefix'
GI = 1024 ** 3
# ``_call`` 的「用預設 client」哨兵。用 ``is`` 比對，不對 mock 做 ``==``——MagicMock 會把
# ``__eq__``／``__bool__`` 也記進 ``mock_calls``，汙染「operator 完全沒被碰」的斷言。
_DEFAULT_CLIENT = object()


def _cell(hard, used, hard_value, used_value, unit, ratio=None):
    return {'hard': hard, 'used': used, 'hard_value': hard_value, 'used_value': used_value,
            'unit': unit, 'ratio': ratio, 'quota': 'paas-ws-test-quota'}


def _report(**resources):
    """operator ``GET /api/namespaces/{ns}/quota`` 的成功回應（形狀同 #1647 的 contract）。

    預設值刻意讓 requests.* 與 limits.* 的 used 不同（#1639 起的常態），才分辨得出畫面
    取的是哪一組。關鍵字參數以 ``__`` 代替資源鍵裡的 ``.``；給 ``None`` 表示拿掉那一格。
    """
    cells = {
        'requests.cpu': _cell('12', '2500m', 12.0, 2.5, 'cores', 0.2083),
        'limits.cpu': _cell('12', '9500m', 12.0, 9.5, 'cores', 0.7917),
        'requests.memory': _cell('24Gi', '6Gi', 24 * GI, 6 * GI, 'bytes', 0.25),
        'limits.memory': _cell('24Gi', '18Gi', 24 * GI, 18 * GI, 'bytes', 0.75),
        'requests.storage': _cell('100Gi', '60Gi', 100 * GI, 60 * GI, 'bytes', 0.6),
        'persistentvolumeclaims': _cell('10', '4', 10, 4, 'count', 0.4),
    }
    for key, value in resources.items():
        resource = key.replace('__', '.')
        if value is None:
            cells.pop(resource, None)
        else:
            cells[resource] = value
    return {'namespace': 'paas-ws-test', 'available': True, 'reason': None,
            'quotas': ['paas-ws-test-quota'], 'resources': cells}


def _cpu(hard, used, ratio=None):
    return _cell(str(hard), str(used), hard, used, 'cores', ratio)


def _by_key(view):
    return {cell['key']: cell for cell in view['resources']}


# ---------------------------------------------------------------------------
# 換算（純函式）
# ---------------------------------------------------------------------------


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaView1647(TransactionCase):

    def test_shape_and_order(self):
        view = workspace_quota.build_view(_report())
        self.assertEqual(set(view), {'available', 'reason', 'level', 'hint', 'resources'})
        self.assertIs(view['available'], True)
        self.assertEqual(view['reason'], '')
        self.assertEqual(view['hint'], '')
        self.assertEqual([c['key'] for c in view['resources']], ['cpu', 'memory', 'storage'])
        for cell in view['resources']:
            self.assertEqual(
                set(cell), {'key', 'unit', 'used', 'hard', 'remaining', 'percent', 'level'})

    def test_cpu_and_memory_read_limits_not_requests(self):
        cells = _by_key(workspace_quota.build_view(_report()))
        self.assertEqual(
            cells['cpu'],
            {'key': 'cpu', 'unit': 'vcpu', 'used': 9.5, 'hard': 12.0, 'remaining': 2.5,
             'percent': 79, 'level': 'ok'})
        self.assertEqual(
            cells['memory'],
            {'key': 'memory', 'unit': 'gb', 'used': 18.0, 'hard': 24.0, 'remaining': 6.0,
             'percent': 75, 'level': 'ok'})

    def test_limits_missing_does_not_fall_back_to_requests(self):
        """沒有 limits.cpu 就沒有 CPU 那一格——不拿 requests.cpu 頂替（那會顯示「還很空」）。"""
        view = workspace_quota.build_view(_report(limits__cpu=None))
        self.assertEqual([c['key'] for c in view['resources']], ['memory', 'storage'])

    def test_storage_reads_requests_storage_in_gib(self):
        cells = _by_key(workspace_quota.build_view(_report()))
        self.assertEqual(
            cells['storage'],
            {'key': 'storage', 'unit': 'gb', 'used': 60.0, 'hard': 100.0, 'remaining': 40.0,
             'percent': 60, 'level': 'ok'})

    def test_bytes_to_gb_uses_1024_cubed(self):
        report = _report(limits__memory=_cell('1500Mi', '512Mi', 1500 * 1024 ** 2,
                                              512 * 1024 ** 2, 'bytes'))
        memory = _by_key(workspace_quota.build_view(report))['memory']
        self.assertEqual((memory['hard'], memory['used'], memory['remaining']),
                         (1.46, 0.5, 0.96))

    def test_remaining_is_never_negative_and_percent_exceeds_100(self):
        """上限被調降後 used 可以大於 hard：剩餘顯示 0、比例照實超過 100、等級 full。"""
        cpu = _by_key(workspace_quota.build_view(_report(limits__cpu=_cpu(12.0, 16.0))))['cpu']
        self.assertEqual(cpu['remaining'], 0)
        self.assertEqual(cpu['percent'], 133)
        self.assertEqual(cpu['level'], 'full')

    def test_percent_is_floored(self):
        cpu = _by_key(workspace_quota.build_view(_report(limits__cpu=_cpu(1000, 899))))['cpu']
        self.assertEqual(cpu['percent'], 89, '89.9% 無條件捨去成 89')

    def test_percent_is_not_dragged_down_by_float_error(self):
        """29／100 要顯示 29：0.29 * 100 在浮點是 28.999999999999996，不可捨成 28。"""
        for hard, used in ((100, 29), (1.0, 0.29), (100.0, 29.0)):
            cpu = _by_key(workspace_quota.build_view(
                _report(limits__cpu=_cpu(hard, used))))['cpu']
            self.assertEqual(cpu['percent'], 29, (hard, used))

    def test_level_thresholds(self):
        self.assertEqual(workspace_quota.QUOTA_WARN_PERCENT, 90)
        for used, level in ((0, 'ok'), (89, 'ok'), (89.9, 'ok'), (90, 'warning'),
                            (99.9, 'warning'), (100, 'full'), (150, 'full')):
            view = workspace_quota.build_view(_report(limits__cpu=_cpu(100, used)))
            self.assertEqual(_by_key(view)['cpu']['level'], level, used)

    def test_level_thresholds_are_exact_on_fractional_cores(self):
        """剛好用到 90% 就是 warning：不受浮點誤差影響，而且與同一格顯示的百分比一致。

        這七組上限用浮點相乘去比門檻會判錯——``18.9 * 100`` 是 1889.9999999999998，比不過
        ``21 * 90``，於是那一格顯示 90% 卻不轉琥珀、也沒有告警句。21／42／84 是
        ``_compute_ns_quota`` 算得出來的上限（7／14／28 vCPU × 3）。
        """
        for hard, used in ((21, 18.9), (37, 33.3), (42, 37.8), (74, 66.6), (79, 71.1),
                           (84, 75.6), (89, 80.1)):
            # operator 給的 cores 是 float（``float(round(q, 3))``）；int 上限也一併驗。
            for hard_value in (hard, float(hard)):
                cpu = _by_key(workspace_quota.build_view(
                    _report(limits__cpu=_cpu(hard_value, used))))['cpu']
                self.assertEqual((cpu['percent'], cpu['level']), (90, 'warning'),
                                 (hard_value, used))
        # 反向對照：差一個 millicore 就還沒到門檻；到 100% 才是 full。
        for hard, used, percent, level in ((21.0, 18.899, 89, 'ok'),
                                           (21.0, 18.901, 90, 'warning'),
                                           (21.0, 20.999, 99, 'warning'),
                                           (21.0, 21.0, 100, 'full'),
                                           (21.0, 21.001, 100, 'full')):
            cpu = _by_key(workspace_quota.build_view(
                _report(limits__cpu=_cpu(hard, used))))['cpu']
            self.assertEqual((cpu['percent'], cpu['level']), (percent, level), (hard, used))

    def test_level_always_agrees_with_the_displayed_percent(self):
        """等級由顯示的百分比決定：1–128 核的每個上限，在 90%／100% 門檻的兩側逐一核對。

        used 以 millicore 為步距（operator 的精度）；門檻上的那一點與差一個 millicore 的
        那一點都要落在對的那一邊。
        """
        warn = workspace_quota.QUOTA_WARN_PERCENT
        checked = 0
        for hard in range(1, 129):
            for milli, percent, level in (
                    (hard * warn * 10 - 1, warn - 1, 'ok'),
                    (hard * warn * 10, warn, 'warning'),
                    (hard * 1000 - 1, 99, 'warning'),
                    (hard * 1000, 100, 'full')):
                cpu = _by_key(workspace_quota.build_view(
                    _report(limits__cpu=_cpu(float(hard), milli / 1000))))['cpu']
                self.assertEqual((cpu['percent'], cpu['level']), (percent, level),
                                 (hard, milli))
                checked += 1
        self.assertEqual(checked, 128 * 4)

    def test_absurd_ratio_is_skipped_not_fatal(self):
        """used 與 hard 差了三百多個數量級：不是合理的配額數字，略過該格、不 raise。"""
        for hard, used in ((5e-324, 1e308), (1e-300, 10 ** 300), (1, 10 ** 307)):
            view = workspace_quota.build_view(_report(limits__cpu=_cpu(hard, used)))
            self.assertIs(view['available'], True, (hard, used))
            self.assertEqual([c['key'] for c in view['resources']], ['memory', 'storage'],
                             (hard, used))

    def test_level_thresholds_on_byte_values(self):
        """bytes 是整數：門檻是「恰好達到」才算，差 1 byte 都不提早跳級。"""
        hard = 20 * GI
        for used, level in ((17 * GI, 'ok'), (18 * GI - 1, 'ok'), (18 * GI, 'warning'),
                            (20 * GI - 1, 'warning'), (20 * GI, 'full'), (21 * GI, 'full')):
            report = _report(limits__memory=_cell('20Gi', 'x', hard, used, 'bytes'))
            self.assertEqual(
                _by_key(workspace_quota.build_view(report))['memory']['level'], level, used)

    def test_operator_ratio_is_ignored(self):
        """等級只看 hard／used：operator 的 ratio 是四捨五入過的顯示值，錯了也不影響。"""
        wrong_high = workspace_quota.build_view(
            _report(limits__cpu=_cpu(100, 10, ratio=1.5)))
        self.assertEqual(_by_key(wrong_high)['cpu']['level'], 'ok')
        self.assertEqual(_by_key(wrong_high)['cpu']['percent'], 10)
        wrong_low = workspace_quota.build_view(
            _report(limits__cpu=_cpu(100, 100, ratio=0.1)))
        self.assertEqual(_by_key(wrong_low)['cpu']['level'], 'full')
        rounded_up = workspace_quota.build_view(
            _report(limits__cpu=_cpu(100000, 89996, ratio=0.9)))
        self.assertEqual(_by_key(rounded_up)['cpu']['level'], 'ok',
                         '89.996% 被 operator 四捨五入成 0.9，不得因此跳成 warning')

    def test_overall_level_is_the_worst_cell(self):
        self.assertEqual(workspace_quota.build_view(_report())['level'], 'ok')
        warning = _report(limits__memory=_cell('24Gi', '23Gi', 24 * GI, 23 * GI, 'bytes'))
        self.assertEqual(workspace_quota.build_view(warning)['level'], 'warning')
        full = _report(
            limits__memory=_cell('24Gi', '23Gi', 24 * GI, 23 * GI, 'bytes'),
            requests__storage=_cell('100Gi', '100Gi', 100 * GI, 100 * GI, 'bytes'))
        self.assertEqual(workspace_quota.build_view(full)['level'], 'full')

    def test_storage_hint_only_when_the_disk_cell_is_tight(self):
        """磁碟那一格是 warning／full 才帶 ``hint``——前台據此換掉「刪除不需要的服務」那句。

        CPU／記憶體吃緊而磁碟還有空間時不帶：那種情況刪服務的建議照舊。
        """
        def storage(used_gi):
            return _cell('100Gi', '%dGi' % used_gi, 100 * GI, used_gi * GI, 'bytes')

        memory_warning = _cell('24Gi', '23Gi', 24 * GI, 23 * GI, 'bytes')
        cases = (
            # (報表, 整體等級, hint)
            (_report(), 'ok', ''),
            (_report(requests__storage=storage(89)), 'ok', ''),
            (_report(requests__storage=storage(90)), 'warning', 'storage'),
            (_report(requests__storage=storage(100)), 'full', 'storage'),
            (_report(requests__storage=storage(130)), 'full', 'storage'),
            # 其他格吃緊、磁碟還有空間：不帶。
            (_report(limits__cpu=_cpu(12.0, 12.0)), 'full', ''),
            (_report(limits__cpu=_cpu(12.0, 11.0)), 'warning', ''),
            (_report(limits__memory=memory_warning), 'warning', ''),
            # 磁碟吃緊就帶，不論最嚴重的是哪一格。
            (_report(limits__cpu=_cpu(12.0, 12.0), requests__storage=storage(95)),
             'full', 'storage'),
            (_report(limits__memory=memory_warning, requests__storage=storage(100)),
             'full', 'storage'),
            # 報表裡沒有磁碟那一格：沒有東西可提示。
            (_report(limits__cpu=_cpu(12.0, 12.0), requests__storage=None), 'full', ''),
        )
        self.assertEqual(workspace_quota.HINT_STORAGE, 'storage')
        for report, level, hint in cases:
            view = workspace_quota.build_view(report)
            self.assertEqual((view['level'], view['hint']), (level, hint),
                             report['resources'].get('requests.storage'))

    def test_storage_hint_after_deleting_services_with_retained_data(self):
        """review display-B2 的情境：三台各 50GB 的服務刪掉兩台。

        上限只按存活的服務算（50 × 3＝150Gi，``_compute_ns_quota``），兩顆保留中的 PVC
        仍計入 used（#317）⇒ 150／150 GB、已用完。這時叫租戶「刪掉不需要的服務」沒有用，
        payload 要帶 hint 讓前台換一句。
        """
        report = _report(
            requests__storage=_cell('150Gi', '150Gi', 150 * GI, 150 * GI, 'bytes'))
        view = workspace_quota.build_view(report)
        storage = _by_key(view)['storage']
        self.assertEqual((storage['used'], storage['hard'], storage['percent'],
                          storage['level']), (150.0, 150.0, 100, 'full'))
        self.assertEqual((view['level'], view['hint']), ('full', 'storage'))

    def test_missing_cells_are_skipped(self):
        view = workspace_quota.build_view(_report(limits__memory=None))
        self.assertIs(view['available'], True)
        self.assertEqual([c['key'] for c in view['resources']], ['cpu', 'storage'])

    def test_no_usable_cell_is_quota_not_found(self):
        only_other_keys = _report(limits__cpu=None, limits__memory=None,
                                  requests__storage=None)
        self.assertEqual(workspace_quota.build_view(only_other_keys),
                         workspace_quota.unavailable('quota_not_found'))
        empty = dict(_report(), resources={})
        self.assertEqual(workspace_quota.build_view(empty),
                         workspace_quota.unavailable('quota_not_found'))

    def test_unavailable_shape(self):
        self.assertEqual(
            workspace_quota.unavailable('disabled'),
            {'available': False, 'reason': 'disabled', 'level': 'ok', 'hint': '',
             'resources': []})

    def test_operator_unavailable_reasons_are_mapped(self):
        def unavailable_report(reason):
            return {'namespace': 'paas-ws-test', 'available': False, 'reason': reason,
                    'quotas': [], 'resources': {}}

        for operator_reason, reason in (
                ('quota_not_found', 'quota_not_found'),
                ('quota_unreadable', 'unreadable'),
                ('something_new', 'error'),
                (None, 'error'),
                (['quota_not_found'], 'error')):
            self.assertEqual(
                workspace_quota.build_view(unavailable_report(operator_reason)),
                workspace_quota.unavailable(reason), operator_reason)

    def test_available_must_be_literally_true(self):
        """``available`` 缺席或不是 True 都不算拿到（即使 resources 看起來有東西）。"""
        for available in (None, 'true', 1, False):
            report = dict(_report(), available=available)
            self.assertIs(workspace_quota.build_view(report)['available'], False, available)
        missing = _report()
        del missing['available']
        self.assertIs(workspace_quota.build_view(missing)['available'], False)

    def test_garbage_input_never_raises(self):
        for report in (None, [], 'oops', 42, True):
            self.assertEqual(workspace_quota.build_view(report),
                             workspace_quota.unavailable('error'), report)
        for resources in (None, [], 'oops', 42):
            self.assertEqual(
                workspace_quota.build_view(dict(_report(), resources=resources)),
                workspace_quota.unavailable('error'), resources)

    def test_bad_cells_are_skipped_not_fatal(self):
        bad_cells = (
            'not-a-dict',
            None,
            {},
            _cell('12', '1', '12', 1.0, 'cores'),       # hard 是字串
            _cell('12', '1', 12.0, '1', 'cores'),       # used 是字串
            _cell('12', '1', True, 1.0, 'cores'),       # bool 不是數字
            _cell('12', '1', 12.0, False, 'cores'),
            _cell('0', '0', 0, 0, 'cores'),             # hard 為 0：沒有比例可言
            _cell('-1', '0', -1.0, 0.0, 'cores'),
            _cell('12', '-1', 12.0, -1.0, 'cores'),
            _cell('12', '1', float('nan'), 1.0, 'cores'),
            _cell('12', '1', 12.0, float('inf'), 'cores'),
            _cell('12', '1', 10 ** 400, 1, 'cores'),    # 大到轉不成 float
            _cell('12', '1', 1, 10 ** 400, 'cores'),
        )
        for bad in bad_cells:
            view = workspace_quota.build_view(_report(limits__cpu=bad))
            self.assertIs(view['available'], True, bad)
            self.assertEqual([c['key'] for c in view['resources']], ['memory', 'storage'], bad)

    def test_all_cells_bad_is_quota_not_found(self):
        report = _report(limits__cpu=_cell('0', '0', 0, 0, 'cores'),
                         limits__memory='x', requests__storage=None)
        self.assertEqual(workspace_quota.build_view(report),
                         workspace_quota.unavailable('quota_not_found'))

    def test_workspace_namespace_follows_ws_hash_and_prefix(self):
        workspace = self.env['woow_paas_platform.workspace'].create({'name': 'ns 1647'})
        params = self.env['ir.config_parameter'].sudo()
        params.set_param(PREFIX_PARAM, 'paas-ws-')
        self.assertEqual(workspace_quota.workspace_namespace(self.env, workspace),
                         'paas-ws-' + workspace.ws_hash)
        params.set_param(PREFIX_PARAM, 'paas-ws-stg-')
        self.assertEqual(workspace_quota.workspace_namespace(self.env, workspace),
                         'paas-ws-stg-' + workspace.ws_hash)
        params.set_param(PREFIX_PARAM, '')
        self.assertEqual(workspace_quota.workspace_namespace(self.env, workspace),
                         'paas-ws-' + workspace.ws_hash, '空值退回預設前綴')

    def test_workspace_namespace_is_none_without_a_valid_ws_hash(self):
        for ws_hash in (False, '', 'not-hex!', 'ABCDEF12'):
            self.assertIsNone(
                workspace_quota.workspace_namespace(self.env, MagicMock(ws_hash=ws_hash)),
                ws_hash)


# ---------------------------------------------------------------------------
# 端點
# ---------------------------------------------------------------------------


class _Fixture(TransactionCase):

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.Service = self.env['woow_paas_platform.cloud_service']
        self.params = self.env['ir.config_parameter'].sudo()
        self.params.set_param(PREFIX_PARAM, 'paas-ws-')
        self.owner = self._user('quota-1647-owner')
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': '1647 Workspace', 'owner_id': self.owner.id,
        })
        Template = self.env['woow_paas_platform.cloud_app_template']
        self.template = Template.create({
            'name': 'Quota 1647',
            'slug': 'quota-1647',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/quota-1647',
            'helm_chart_version': '1.0.0',
            'post_deploy_init_type': 'none',
        })
        self.admin = self._member('quota-1647-admin', ROLE_ADMIN)
        self.user = self._member('quota-1647-user', ROLE_USER)
        self.guest = self._member('quota-1647-guest', ROLE_GUEST)
        self.outsider = self._user('quota-1647-outsider')

    @property
    def namespace(self):
        prefix = self.params.get_param(PREFIX_PARAM, 'paas-ws-')
        return make_namespace(self.workspace.ws_hash, prefix=prefix)

    def _user(self, login):
        return self.env['res.users'].create({
            'name': login, 'login': login,
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })

    def _member(self, login, role):
        user = self._user(login)
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': user.id, 'role': role,
        })
        return user

    def _portal_user(self, login, role=None):
        """Portal 帳號（真實租戶的帳號類型，#1528）；給 ``role`` 就加入本工作區。"""
        user = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': login, 'login': login, 'email': '%s@example.invalid' % login,
            'groups_id': [(6, 0, [self.env.ref('base.group_portal').id])],
        })
        # 鑑別力前提：帳號類型真的如名字所說（capability 以 ``share`` 分流）。
        self.assertTrue(user.share)
        self.assertFalse(user.has_group('base.group_user'))
        if role:
            self.env['woow_paas_platform.workspace_access'].create({
                'workspace_id': self.workspace.id, 'user_id': user.id, 'role': role,
            })
        return user

    def _service(self, name='quota-1647', template=None, **extra):
        vals = {
            'name': name,
            'workspace_id': self.workspace.id,
            'template_id': (template or self.template).id,
            'state': 'running',
            'helm_namespace': self.namespace,
            'helm_release_name': 'svc-' + name,
        }
        vals.update(extra)
        return _internal_provision(self.Service).create(vals)

    def _client(self, report=None):
        client = MagicMock()
        client.get_namespace_quota.return_value = _report() if report is None else report
        return client

    def _failing(self, error):
        client = MagicMock()
        client.get_namespace_quota.side_effect = error
        return client

    def _call(self, user, client=_DEFAULT_CLIENT, workspace_id=None):
        """以 ``user`` 的身分打端點；回 (結果, client mock, 取得 client 的 factory mock)。

        ``client=None`` 表示平台沒有設定 operator（factory 回 None）。
        """
        if client is _DEFAULT_CLIENT:
            client = self._client()
        with patch(_CLIENT_SEAM, return_value=client) as factory, \
                patch(_REQUEST_SEAM, new=MagicMock(env=self.env(user=user))):
            result = self.controller.api_workspace_quota(workspace_id or self.workspace.id)
        return result, client, factory


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaEndpoint1647(_Fixture):

    # ── 誰看得到 ──

    def test_every_member_role_can_read(self):
        self._service()
        for user in (self.owner, self.admin, self.user, self.guest):
            result, client, _factory = self._call(user)
            self.assertIs(result['success'], True, user.login)
            self.assertIs(result['data']['available'], True, user.login)
            client.get_namespace_quota.assert_called_once_with(self.namespace)

    def test_portal_members_can_read(self):
        """review display-B7：真實租戶都是 Portal 帳號，Portal 成員一樣看得到配額。

        Portal 對 cloud_service 沒有 ACL。端點查「這個工作區有沒有租戶看得到的服務」用的
        是 capability 回傳的 workspace 的 env（Portal 時是 sudo 過的）；若有人把它改成
        ``request.env[...]``，Portal 成員會直接 AccessError、面板變成「暫時無法取得」，
        而全是 internal 帳號的測試不會紅——這一條就是守那個。
        """
        self._service()
        for role in (ROLE_USER, ROLE_GUEST):
            portal = self._portal_user('quota-1647-portal-%s' % role, role)
            # 鑑別力前提：以 Portal 自己的權限查不了 cloud_service。
            with self.assertRaises(AccessError):
                self.Service.with_user(portal).search_count(
                    [('workspace_id', '=', self.workspace.id)])
            result, client, factory = self._call(portal)
            self.assertIs(result['success'], True, role)
            self.assertIs(result['data']['available'], True, role)
            self.assertEqual([c['key'] for c in result['data']['resources']],
                             ['cpu', 'memory', 'storage'], role)
            self.assertEqual(client.method_calls,
                             [call.get_namespace_quota(self.namespace)], role)
            factory.assert_called_once()

    def test_portal_member_of_an_empty_workspace_is_no_namespace(self):
        """Portal 成員走「沒有服務」那條路徑也不會撞 ACL，而且不碰 operator。"""
        portal = self._portal_user('quota-1647-portal-empty', ROLE_USER)
        result, client, factory = self._call(portal)
        self.assertEqual(
            result, {'success': True, 'data': workspace_quota.unavailable('no_namespace')})
        factory.assert_not_called()
        self.assertEqual(client.mock_calls, [])

    def test_portal_non_member_gets_nothing_and_operator_is_never_called(self):
        self._service()
        portal = self._portal_user('quota-1647-portal-outsider')
        result, client, factory = self._call(portal)
        self.assertEqual(result, {'success': False, 'error': NOT_FOUND})
        factory.assert_not_called()
        self.assertEqual(client.mock_calls, [])

    def test_non_member_gets_nothing_and_operator_is_never_called(self):
        self._service()
        result, client, factory = self._call(self.outsider)
        self.assertEqual(result, {'success': False, 'error': NOT_FOUND})
        factory.assert_not_called()
        self.assertEqual(client.mock_calls, [])

    def test_unknown_workspace_is_the_same_error(self):
        self._service()
        missing_id = self.env['woow_paas_platform.workspace'].search(
            [], order='id desc', limit=1).id + 1000
        result, client, factory = self._call(self.owner, workspace_id=missing_id)
        self.assertEqual(result, {'success': False, 'error': NOT_FOUND})
        factory.assert_not_called()
        self.assertEqual(client.mock_calls, [])

    def test_other_workspace_member_cannot_read_this_one(self):
        """別的工作區的 owner 拿這個工作區的 id 來問：一樣是 not found、不碰 operator。"""
        self._service()
        other_owner = self._user('quota-1647-other-owner')
        self.env['woow_paas_platform.workspace'].create({
            'name': '1647 Other', 'owner_id': other_owner.id})
        result, client, factory = self._call(other_owner)
        self.assertEqual(result, {'success': False, 'error': NOT_FOUND})
        factory.assert_not_called()
        self.assertEqual(client.mock_calls, [])

    # ── 成功路徑 ──

    def test_success_payload_matches_the_contract(self):
        self._service()
        report = _report(limits__cpu=_cpu(12.0, 11.0, ratio=0.9167))
        result, _client, _factory = self._call(self.user, client=self._client(report))
        self.assertEqual(result, {'success': True, 'data': {
            'available': True, 'reason': '', 'level': 'warning', 'hint': '', 'resources': [
                {'key': 'cpu', 'unit': 'vcpu', 'used': 11.0, 'hard': 12.0,
                 'remaining': 1.0, 'percent': 91, 'level': 'warning'},
                {'key': 'memory', 'unit': 'gb', 'used': 18.0, 'hard': 24.0,
                 'remaining': 6.0, 'percent': 75, 'level': 'ok'},
                {'key': 'storage', 'unit': 'gb', 'used': 60.0, 'hard': 100.0,
                 'remaining': 40.0, 'percent': 60, 'level': 'ok'},
            ]}})

    def test_storage_hint_reaches_the_endpoint_payload(self):
        self._service()
        report = _report(
            requests__storage=_cell('100Gi', '100Gi', 100 * GI, 100 * GI, 'bytes'))
        result, _client, _factory = self._call(self.user, client=self._client(report))
        self.assertEqual((result['data']['level'], result['data']['hint']),
                         ('full', 'storage'))

    # ⚠ 下面三條用 ``method_calls`` 而不是 ``mock_calls``：端點有一句 ``if not client``，
    # MagicMock 會把那次真值判斷記成 ``call.__bool__()`` 放進 ``mock_calls``；
    # ``method_calls`` 只記對 operator 的方法呼叫，正是這裡要釘的東西。

    def test_operator_is_called_exactly_once_with_the_derived_namespace(self):
        self._service()
        result, client, factory = self._call(self.user)
        self.assertIs(result['success'], True)
        self.assertEqual(self.namespace, 'paas-ws-' + self.workspace.ws_hash)
        self.assertEqual(client.method_calls, [call.get_namespace_quota(self.namespace)])
        factory.assert_called_once()

    def test_namespace_follows_the_configured_prefix(self):
        self.params.set_param(PREFIX_PARAM, 'paas-ws-stg-')
        self._service()
        expected = 'paas-ws-stg-' + self.workspace.ws_hash
        self.assertEqual(self.namespace, expected)
        _result, client, _factory = self._call(self.user)
        self.assertEqual(client.method_calls, [call.get_namespace_quota(expected)])

    def test_caller_cannot_choose_the_namespace(self):
        """端點沒有 namespace 參數：多帶的 kwargs 不影響送給 operator 的值。"""
        self._service()
        client = self._client()
        with patch(_CLIENT_SEAM, return_value=client), \
                patch(_REQUEST_SEAM, new=MagicMock(env=self.env(user=self.user))):
            result = self.controller.api_workspace_quota(
                self.workspace.id, namespace='paas-ws-deadbeef', name='kube-system')
        self.assertIs(result['success'], True)
        self.assertEqual(client.method_calls, [call.get_namespace_quota(self.namespace)])

    def test_endpoint_does_not_touch_the_services(self):
        """純唯讀：成功與失敗兩條路徑之後，服務的 state／write_date／error_message 都沒變。

        同一個測試交易裡每次 ORM 寫入的 write_date 都是同一個時間戳，直接比前後值沒有
        鑑別力 ⇒ 先用 SQL 把 write_date 撥回過去，端點若寫過這一列它就會被改成現在。
        另外直接盯 ``write``，一次都不准被呼叫。
        """
        service = self._service()
        self.env.flush_all()
        self.env.cr.execute(
            'UPDATE woow_paas_platform_cloud_service SET write_date = %s WHERE id = %s',
            ('2020-01-01 00:00:00', service.id))
        service.invalidate_recordset()
        before = (service.state, service.write_date, service.error_message)
        self.assertEqual(str(before[1]), '2020-01-01 00:00:00', '前提：write_date 已撥回')

        with patch.object(type(self.Service), 'write', autospec=True,
                          side_effect=AssertionError('quota 端點不得寫 cloud_service')) as write:
            ok, _client, _factory = self._call(self.owner)
            failed, _client, _factory = self._call(
                self.owner, client=self._failing(PaaSOperatorConnectionError('down')))
        self.assertIs(ok['data']['available'], True, '前提：成功路徑真的走到 operator')
        self.assertEqual(failed['data']['reason'], 'error', '前提：失敗路徑真的走到 operator')
        write.assert_not_called()

        self.env.flush_all()
        service.invalidate_recordset()
        self.assertEqual((service.state, service.write_date, service.error_message), before)

    # ── 沒有東西可顯示：不問 operator ──

    def _assert_no_namespace(self):
        result, client, factory = self._call(self.owner)
        self.assertEqual(
            result, {'success': True, 'data': workspace_quota.unavailable('no_namespace')})
        factory.assert_not_called()
        self.assertEqual(client.mock_calls, [])

    def test_workspace_without_services_is_no_namespace(self):
        self._assert_no_namespace()

    def test_workspace_with_only_concealed_services_is_no_namespace(self):
        concealed = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Concealed 1647',
            'slug': 'concealed-1647',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/concealed-1647',
            'helm_chart_version': '1.0.0',
            'post_deploy_init_type': 'none',
            'is_hidden': True,
            'conceal_instances': True,
        })
        self._service(name='concealed-1647', template=concealed)
        self._assert_no_namespace()

    def test_hidden_only_template_still_counts(self):
        """只是下架（is_hidden、沒有 conceal_instances）的範本，既有服務照常算。"""
        hidden_only = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Hidden Only 1647',
            'slug': 'hidden-only-1647',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/hidden-only-1647',
            'helm_chart_version': '1.0.0',
            'post_deploy_init_type': 'none',
            'is_hidden': True,
        })
        self._service(name='hidden-only-1647', template=hidden_only)
        result, client, _factory = self._call(self.owner)
        self.assertIs(result['data']['available'], True)
        client.get_namespace_quota.assert_called_once_with(self.namespace)

    def test_service_in_a_different_namespace_is_no_namespace(self):
        """服務住在舊 namespace（與現在的推導值不同）：不拿推導值去問別的 namespace。"""
        self._service(helm_namespace='paas-ws-0ldf00d1')
        self._assert_no_namespace()

    def test_service_of_another_workspace_does_not_count(self):
        other = self.env['woow_paas_platform.workspace'].create({'name': '1647 Elsewhere'})
        self._service(workspace_id=other.id)
        self._assert_no_namespace()

    # ── operator 未設定／太舊／失敗 ──

    def _assert_reason(self, client, reason):
        result, client, _factory = self._call(self.owner, client=client)
        self.assertEqual(
            result, {'success': True, 'data': workspace_quota.unavailable(reason)})
        return client

    def test_operator_not_configured_is_disabled(self):
        self._service()
        self._assert_reason(None, 'disabled')

    def test_connection_and_timeout_errors_are_error(self):
        self._service()
        for error in (PaaSOperatorConnectionError('Failed to connect', detail='refused'),
                      PaaSOperatorTimeoutError('Request timed out', detail='read timeout')):
            client = self._assert_reason(self._failing(error), 'error')
            client.get_namespace_quota.assert_called_once_with(self.namespace)

    def test_old_operator_405_and_404_are_operator_outdated(self):
        """比這個端點舊的 operator 只替同一路徑註冊 PUT ⇒ GET 回 405；更舊的回 404。"""
        self._service()
        for status_code, detail in ((405, 'Method Not Allowed'), (404, 'Not Found')):
            error = PaaSOperatorAPIError(
                'API error: %s' % detail, status_code=status_code, detail=detail)
            self._assert_reason(self._failing(error), 'operator_outdated')

    def test_other_api_errors_are_error(self):
        self._service()
        for status_code, detail in ((500, 'Internal Server Error'),
                                    (400, "Namespace must start with 'paas-ws-'"),
                                    (401, 'Invalid or missing API key'),
                                    (503, 'Operator API key not configured')):
            error = PaaSOperatorAPIError(
                'API error: %s' % detail, status_code=status_code, detail=detail)
            self._assert_reason(self._failing(error), 'error')

    def test_operator_unavailable_reports_pass_through_build_view(self):
        self._service()
        for operator_reason, reason in (('quota_not_found', 'quota_not_found'),
                                        ('quota_unreadable', 'unreadable')):
            report = {'namespace': self.namespace, 'available': False,
                      'reason': operator_reason, 'quotas': [], 'resources': {}}
            self._assert_reason(self._client(report), reason)

    def test_malformed_operator_response_is_error_not_a_crash(self):
        self._service()
        for report in ([], 'oops', {'available': True, 'resources': None}):
            self._assert_reason(self._client(report), 'error')

    def test_unexpected_exception_is_not_swallowed(self):
        """只接 PaaSOperatorError：程式錯誤要照常冒出來，不可被包成「暫時無法取得」。"""
        self._service()
        with self.assertRaises(ZeroDivisionError):
            self._call(self.owner, client=self._failing(ZeroDivisionError('bug')))

    # ── 既有端點不受影響 ──

    def test_service_list_endpoint_still_does_not_call_the_operator(self):
        """#1159 Q1：服務清單維持純讀 DB——配額是另開的端點，不是塞進 list。"""
        service = self._service()
        client = self._client()
        with patch(_CLIENT_SEAM, return_value=client) as factory, \
                patch(_REQUEST_SEAM, new=MagicMock(env=self.env(user=self.admin))):
            listed = self.controller.api_workspace_services(self.workspace.id, action='list')
        self.assertTrue(listed.get('success'), listed)
        self.assertEqual([item['id'] for item in listed['data']], [service.id],
                         '前提：清單真的回了這台服務')
        self.assertNotIn('quota', listed)
        for item in listed['data']:
            self.assertNotIn('quota', item)
        factory.assert_not_called()
        self.assertEqual(client.mock_calls, [])


# ---------------------------------------------------------------------------
# operator client 與逾時階梯
# ---------------------------------------------------------------------------


def _read(*parts):
    with open(os.path.join(_PAAS, *parts), encoding='utf-8') as handle:
        return handle.read()


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaClient1647(TransactionCase):

    def test_get_namespace_quota_request(self):
        client = PaaSOperatorClient('http://operator.invalid', 'key')
        payload = {'namespace': 'paas-ws-abcd1234', 'available': False,
                   'reason': 'quota_not_found', 'quotas': [], 'resources': {}}
        with patch.object(PaaSOperatorClient, '_request', return_value=payload) as request:
            result = client.get_namespace_quota('paas-ws-abcd1234')
        self.assertEqual(result, payload)
        request.assert_called_once_with(
            'GET', '/api/namespaces/paas-ws-abcd1234/quota', timeout=NAMESPACE_QUOTA_TIMEOUT)

    def test_client_does_not_swallow_errors(self):
        client = PaaSOperatorClient('http://operator.invalid', 'key')
        error = PaaSOperatorAPIError('API error', status_code=405, detail='Method Not Allowed')
        with patch.object(PaaSOperatorClient, '_request', side_effect=error):
            with self.assertRaises(PaaSOperatorAPIError):
                client.get_namespace_quota('paas-ws-abcd1234')

    def test_timeout_ladder(self):
        """前端 ＞ 平台（connect＋read）；平台刻意比 DEFAULT_TIMEOUT 短。"""
        self.assertEqual(len(NAMESPACE_QUOTA_TIMEOUT), 2, '(connect, read)')
        js = _read('services', 'workspace_quota.js')
        match = re.search(r'const\s+QUOTA_REQUEST_TIMEOUT_MS\s*=\s*([0-9_]+)\s*;', js)
        self.assertIsNotNone(match, 'services/workspace_quota.js 找不到 QUOTA_REQUEST_TIMEOUT_MS')
        frontend_ms = int(match.group(1).replace('_', ''))
        self.assertLess(sum(NAMESPACE_QUOTA_TIMEOUT) * 1000, frontend_ms)
        self.assertLess(sum(NAMESPACE_QUOTA_TIMEOUT), DEFAULT_TIMEOUT)
        # 那個常數真的有被用在這支請求上（不是宣告了沒接線）。
        self.assertRegex(
            js, r'jsonRpc\(\s*`/api/workspaces/\$\{workspaceId\}/quota`\s*,\s*\{\}\s*,\s*'
                r'QUOTA_REQUEST_TIMEOUT_MS\s*\)')


# ---------------------------------------------------------------------------
# 前端契約（靜態讀檔）
# ---------------------------------------------------------------------------


_T_STRING_RE = re.compile(r'_t\("((?:[^"\\]|\\.)*)"')
# JS 的註解（整行 ``//`` 與 ``/* */`` 區塊）：靜態斷言只看程式碼，不讓說明文字裡提到的
# 識別字算數。只認「整行」的 ``//``，不會誤傷字串裡的 ``//``。
_JS_COMMENT_RE = re.compile(r'/\*.*?\*/|^[ \t]*//[^\n]*$', re.S | re.M)


def _js_code(source):
    return _JS_COMMENT_RE.sub('', source)


# OverviewTab 的 workspaceQuotaHint：getter 本體只有一句單行 _t()。
_OVERVIEW_HINT_RE = re.compile(
    r'get workspaceQuotaHint\(\)\s*\{\s*return _t\("((?:[^"\\]|\\.)*)"\);')


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaFrontend1647(TransactionCase):

    def test_panel_is_mounted_on_the_workspace_page(self):
        xml = _read('pages', 'workspace', 'WorkspaceDetailPage.xml')
        js = _read('pages', 'workspace', 'WorkspaceDetailPage.js')
        self.assertIn('<WorkspaceQuotaPanel', xml)
        self.assertRegex(
            xml, r'<WorkspaceQuotaPanel\s+workspaceId="props\.workspaceId"\s+'
                 r'reloadKey="quotaReloadKey"\s*/>')
        components = re.search(r'static components = \{([^}]*)\}', js)
        self.assertIsNotNone(components)
        self.assertIn('WorkspaceQuotaPanel', components.group(1))
        self.assertIn('get quotaReloadKey()', js)

    def test_panel_reads_backend_level_and_has_no_threshold_of_its_own(self):
        js = _read('components', 'workspace-quota', 'WorkspaceQuotaPanel.js')
        self.assertIn('quota?.level', js)
        self.assertIn('resource.level', js)
        self.assertIn('resource.percent', js)
        # 門檻只在後端（workspace_quota.QUOTA_WARN_PERCENT）：前端不得自己再判一次。
        threshold = r'(?<![\w.])%d(?![\w.])' % workspace_quota.QUOTA_WARN_PERCENT
        fraction = r'(?<![\w.])0?\.9\b'
        comparison = r'\b(used|hard|percent|ratio)\s*[<>]=?'
        self.assertNotRegex(js, threshold)
        self.assertNotRegex(js, fraction)
        self.assertNotRegex(js, comparison)
        # 陽性對照：同一組 regex 對會違規的寫法確實會命中。
        self.assertRegex('if (percent >= 90) {', threshold)
        self.assertRegex('ratio > 0.9', fraction)
        self.assertRegex('if (resource.used >= resource.hard) {', comparison)

    def test_disk_alert_explains_retained_data_instead_of_deleting_services(self):
        """磁碟吃緊時的告警句：讀後端的 ``hint``，講保留中的資料，不叫人刪服務。"""
        js = _read('components', 'workspace-quota', 'WorkspaceQuotaPanel.js')
        # 判準在後端：前端只拿 payload 的 hint 與後端同一個字面值比。
        self.assertIn('quota?.hint === "%s"' % workspace_quota.HINT_STORAGE, js)
        strings = _T_STRING_RE.findall(js)
        alerts = [s for s in strings if s.startswith('This workspace ')]
        self.assertEqual(len(alerts), 4, '快用完／已用完 × 一般／磁碟吃緊：%s' % alerts)
        retained = [s for s in alerts if 'retention period' in s]
        generic = [s for s in alerts if 'retention period' not in s]
        self.assertEqual((len(retained), len(generic)), (2, 2), alerts)
        for sentence in retained:
            self.assertIn('disk quota', sentence)
            self.assertNotIn('Delete a service', sentence, '刪服務釋不出磁碟配額')
        for sentence in generic:
            self.assertIn('Delete a service', sentence, '陽性對照：一般版仍是原本那句')
        # 說明句也要交代磁碟含保留中的資料，否則數字對不上畫面上的服務。
        intro = [s for s in strings if s.startswith('All services in this workspace share')]
        self.assertEqual(len(intro), 1)
        self.assertIn('retention period', intro[0])

    def test_overview_hint_does_not_promise_usage_on_the_workspace_page(self):
        """review display-B6：配額面板在好幾種情況下整塊不畫，服務頁的說明句不能叫人去那裡看。"""
        overview = _read('pages', 'service', 'tabs', 'OverviewTab.js')
        hint = _OVERVIEW_HINT_RE.search(overview)
        self.assertIsNotNone(hint, 'OverviewTab.js 找不到 workspaceQuotaHint 的單行 _t()')
        sentence = hint.group(1)
        self.assertIn('share one overall CPU, memory and disk quota', sentence)
        self.assertNotIn('workspace page', sentence)
        self.assertNotIn('current usage', sentence)

    def test_follow_up_reload_is_one_shot_not_polling(self):
        """review display-B1：讀到資料後只補讀**一次**——不是輪詢。

        刪除服務後回到工作區頁，pod 還在結束中、k8s 的用量還沒回補，而之後沒有任何東西
        會觸發重讀 ⇒ 面板讀到資料後排一次延遲補讀。這裡釘住它的形狀：延遲來自 service
        檔的常數、整個面板只有一個計時器、補讀本身不再排下一次、新的讀取與卸載都會取消
        還沒觸發的那一次。行為本身（假計時器逐步觸發）另以 OWL＋jsdom 在本機驗過。
        """
        service = _js_code(_read('services', 'workspace_quota.js'))
        delay = re.search(
            r'export const QUOTA_FOLLOW_UP_DELAY_MS\s*=\s*([0-9_]+)\s*;', service)
        self.assertIsNotNone(
            delay, 'services/workspace_quota.js 找不到 QUOTA_FOLLOW_UP_DELAY_MS')
        self.assertEqual(int(delay.group(1).replace('_', '')), 30000)

        code = _js_code(_read('components', 'workspace-quota', 'WorkspaceQuotaPanel.js'))
        self.assertRegex(
            code, r'import \{[^}]*\bQUOTA_FOLLOW_UP_DELAY_MS\b[^}]*\} '
                  r'from "\.\./\.\./services/workspace_quota";')
        # 不輪詢：沒有 setInterval；整個檔只有一個 setTimeout，延遲就是那個常數。
        self.assertNotIn('setInterval', code)
        self.assertEqual(code.count('setTimeout('), 1)
        timer = re.search(
            r'this\._followUpTimer = setTimeout\(\(\) => \{(.*?)\},\s*'
            r'QUOTA_FOLLOW_UP_DELAY_MS\);', code, re.S)
        self.assertIsNotNone(timer, '延遲補讀的計時器不是以 QUOTA_FOLLOW_UP_DELAY_MS 排的')
        # 計時器觸發的那一次讀取標成 follow-up……
        self.assertIn('this.load(workspaceId, true);', timer.group(1))
        self.assertEqual(code.count('this.load(workspaceId, true)'), 1)
        # ……而 follow-up 讀完不再排下一次：唯一的排程點要求「讀到資料且不是 follow-up」。
        self.assertEqual(code.count('this.scheduleFollowUp('), 1)
        self.assertIn('if (hasData && !isFollowUp) this.scheduleFollowUp(workspaceId);', code)
        # 新的讀取（reloadKey／workspaceId 變動、重新整理）一開始就取消還沒觸發的那一次；
        # 丟棄舊回應沿用同一個 _seq。
        load = re.search(r'\n  async load\(.*?\n  \}\n', code, re.S)
        self.assertIsNotNone(load)
        body = load.group(0)
        self.assertIn('isFollowUp = false', body)
        self.assertLess(body.index('const seq = ++this._seq;'),
                        body.index('this.cancelFollowUp();'))
        self.assertLess(body.index('this.cancelFollowUp();'),
                        body.index('await fetchWorkspaceQuota(workspaceId)'))
        self.assertLess(body.index('if (seq !== this._seq) return;'),
                        body.index('this.scheduleFollowUp('))
        # 補讀沒讀到資料：留著畫面上那一份、不寫 state——不把一塊正常的面板換成
        # 「暫時無法取得」。
        keep = re.search(r'if \(isFollowUp && !hasData\) \{\s*return;\s*\}', body)
        self.assertIsNotNone(keep, '補讀失敗時應保留原本的數字')
        self.assertLess(keep.end(), body.index('this.state.quota ='))
        # 卸載：作廢在途的回應，並取消計時器。
        unmount = re.search(r'onWillUnmount\(\(\) => \{(.*?)\}\);', code, re.S)
        self.assertIsNotNone(unmount)
        self.assertIn('this._seq++;', unmount.group(1))
        self.assertIn('this.cancelFollowUp();', unmount.group(1))
        self.assertIn('clearTimeout(this._followUpTimer);', code)
        # 陽性對照：註解被拿掉了（檔頭的說明提到了這個常數名，程式碼裡只剩 import 與使用）。
        raw = _read('components', 'workspace-quota', 'WorkspaceQuotaPanel.js')
        self.assertGreater(raw.count('QUOTA_FOLLOW_UP_DELAY_MS'),
                           code.count('QUOTA_FOLLOW_UP_DELAY_MS'))
        self.assertEqual(code.count('QUOTA_FOLLOW_UP_DELAY_MS'), 2)

    def test_progressbar_value_stays_within_its_range(self):
        """review display-B5：超額時 percent 大於 100，aria-valuenow 不可超過 aria-valuemax。"""
        xml = _read('components', 'workspace-quota', 'WorkspaceQuotaPanel.xml')
        bar = re.search(r'<div class="o_woow_quota_panel__bar"[^>]*>', xml)
        self.assertIsNotNone(bar)
        tag = bar.group(0)
        self.assertIn('role="progressbar"', tag)
        self.assertIn('aria-valuemin="0"', tag)
        self.assertIn('aria-valuemax="100"', tag)
        self.assertIn('t-att-aria-valuenow="row.ariaNow"', tag)
        self.assertNotIn('row.percent"', tag, 'aria-valuenow 不可直接綁沒夾過的 percent')
        # 實際的數字（可能是 133%）仍念得出來。
        self.assertIn('t-att-aria-valuetext="row.percentText"', tag)
        code = _js_code(_read('components', 'workspace-quota', 'WorkspaceQuotaPanel.js'))
        self.assertIn('const barPercent = Math.min(Math.max(percent, 0), 100);', code)
        self.assertIn('ariaNow: barPercent,', code)
        self.assertIn('fillStyle: `width: ${barPercent}%`,', code)

    def test_panel_formats_numbers_with_the_user_locale(self):
        js = _read('components', 'workspace-quota', 'WorkspaceQuotaPanel.js')
        self.assertIn('formatFloat', js)
        self.assertNotIn('toLocale', js)
        self.assertNotIn('Intl.', js)

    def test_template_keeps_copy_out_of_text_nodes(self):
        """文案都在 JS 的 _t()：模板只有 t-esc，沒有會被抽成 msgid 的文字節點。"""
        xml = _read('components', 'workspace-quota', 'WorkspaceQuotaPanel.xml')
        without_comments = re.sub(r'<!--.*?-->', '', xml, flags=re.S)
        text_nodes = [t.strip() for t in re.findall(r'>([^<]*)<', without_comments)]
        self.assertEqual([t for t in text_nodes if t], [])
        self.assertIn('t-if="visible"', xml)
        self.assertIn('role="progressbar"', xml)
        self.assertNotRegex(without_comments, r'<WoowIcon[^>]*\bt-att-')

    def test_every_string_has_zh_tw(self):
        panel = _read('components', 'workspace-quota', 'WorkspaceQuotaPanel.js')
        strings = set(_T_STRING_RE.findall(panel))
        self.assertGreater(len(strings), 10, '陽性對照：面板的 _t() 字串抓得到')

        overview = _read('pages', 'service', 'tabs', 'OverviewTab.js')
        hint = _OVERVIEW_HINT_RE.search(overview)
        self.assertIsNotNone(hint, 'OverviewTab.js 找不到 workspaceQuotaHint 的單行 _t()')
        strings.add(hint.group(1))
        self.assertIn('t-esc="workspaceQuotaHint"',
                      _read('pages', 'service', 'tabs', 'OverviewTab.xml'))

        po_path = os.path.join(_SRC, 'i18n', 'zh_TW.po')
        translated = {entry['src']: entry['value'] for entry in PoFileReader(po_path)
                      if entry.get('type') == 'code'}
        missing = sorted(s for s in strings if not translated.get(s))
        self.assertEqual(missing, [], 'workspace quota strings without zh_TW: %s' % missing)

    def test_placeholders_survive_translation(self):
        """譯文要保留原文的 placeholder，否則 runtime 會少代入一個值。"""
        po_path = os.path.join(_SRC, 'i18n', 'zh_TW.po')
        translated = {entry['src']: entry['value'] for entry in PoFileReader(po_path)
                      if entry.get('type') == 'code'}
        panel = _read('components', 'workspace-quota', 'WorkspaceQuotaPanel.js')
        placeholder = re.compile(r'%\(\w+\)s|%s')
        checked = []
        for source in sorted(set(_T_STRING_RE.findall(panel))):
            wanted = sorted(placeholder.findall(source))
            if not wanted:
                continue
            checked.append(source)
            self.assertEqual(sorted(placeholder.findall(translated.get(source, ''))),
                             wanted, source)
        self.assertEqual(len(checked), 4, '兩條「已佔用／上限」＋兩條「剩餘」：%s' % checked)

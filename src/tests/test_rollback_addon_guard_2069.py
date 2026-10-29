# -*- coding: utf-8 -*-
"""#2069：「回復版本」不得把租戶 Odoo 仍裝著的模組程式碼抽掉。

woow-odoo 租戶的模組程式碼來自 Helm value ``addons.selected``（勾選不代為安裝、取消勾選也不代為
解除安裝）。回復到一個沒勾某模組的 revision ⇒ 程式碼不見、DB 仍記著 installed：同 image 時 Odoo
靜默不載入，換 image 時 upgrade-gate exit 4、Pod 卡 ``Init:Error``。取消勾選那條路有
``_assess_addon_removal``（#477，fail-closed），回復那條路以前完全沒查。

政策（見 ``docs/reference/features/cloud-services.md`` §4.3.1）：

- 拿掉的模組＝「可能正在跑」的 ``addons.selected`` 聯集減掉目標 revision 的（operator config
  projection，#2059）。「可能正在跑」＝最新的 deployed ＋ 每個比它新的 revision（不論狀態）；歷史裡
  沒有 deployed（純 helm rollback 失敗後）⇒ 每一個 revision。只有 woow-odoo 租戶要算，其他服務不多
  打任何 operator 呼叫。
- 沒拿掉任何模組 ⇒ 不查模組狀態，任何 state 都照舊回復。
- running ⇒ 與取消勾選同一個裁決（name-based 核心）；不在 safe 集合 ⇒ ``blocked``；查不成 ⇒
  ``check_failed``；兩者都沒有覆寫方式。
- 非 running ⇒ ``unverified``：預設拒絕，``acknowledge_unverified_addons=true`` 才放行。歷史裡沒有
  任何可用的 revision 號 ⇒ 任何 state 都是 ``check_failed``。
- 被擋下時不寫任何欄位、不呼叫 ``rollback_release``。
- 預檢端點先過與回復相同的前置閘（state、init lease、operator client、範本設定、歷史，以及共用的
  already_current／revision 不在歷史裡／chart 版本下限），任一道擋下都是 ``check_failed``、訊息與
  回復 API 相同；``unverified`` 只留給「回復做得了、但服務不是 running 查不了模組狀態」。
"""
import json
import logging
from unittest.mock import MagicMock, Mock, patch

from odoo.exceptions import UserError
from odoo.tests.common import HttpCase, TransactionCase, tagged

from odoo.addons.woow_paas_platform.controllers.paas import (
    PaasController,
    _ADDON_REMOVAL_SAFE_STATES,
)
from ..models.cloud_service import _internal_provision
from ..models.workspace_access import ROLE_ADMIN, ROLE_GUEST, ROLE_USER
from ..services.chart_version import possibly_live_revisions, rollback_request_problem
from ..services.paas_operator import PaaSOperatorAPIError, PaaSOperatorConnectionError

_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'
_CLIENT_FACTORY = _CONTROLLER + '.get_paas_operator_client'
ADDONS_PATH = 'addons.selected'

_CODES = {'no_removal', 'safe', 'blocked', 'unverified', 'check_failed'}
_CONTRACT_KEYS = {'success', 'error', 'code', 'applicable', 'removed', 'modules', 'blocking'}


def _rev(number, status, version='0.2.7'):
    return {'revision': number, 'chart': 'woow-odoo-%s' % version, 'status': status,
            'updated': '2026-10-29T00:00:00Z', 'app_version': '18.0', 'description': ''}


# rev3 部署中；rev2 少了 qa_sale_2069（被拿掉的那一個）；rev1 什麼都沒勾。
HISTORY = [_rev(1, 'superseded'), _rev(2, 'superseded'), _rev(3, 'deployed')]
SELECTIONS = {
    1: [],
    2: ['qa_base_2069'],
    3: ['qa_base_2069', 'qa_sale_2069'],
}


# rev3 部署中只勾 base；比它新的 rev4（failed／pending-upgrade）多勾了 qa_sale_2069——一般升級不帶
# --atomic／--wait，rev4 可能已經在跑、租戶可能已經裝了 sale。
NEWER_STATUSES = ('failed', 'pending-upgrade')
NEWER_SELECTIONS = {
    1: [],
    2: ['qa_base_2069'],
    3: ['qa_base_2069'],
    4: ['qa_base_2069', 'qa_sale_2069'],
}


def _history_with_newer(status):
    return [_rev(1, 'superseded'), _rev(2, 'superseded'), _rev(3, 'deployed'), _rev(4, status)]


# 純 helm rollback（不帶 --wait）套用時失敗後的真實 helm 帳（沙盒以 helm 3.19.5 實測）：原本 deployed
# 的 rev2 被改標 superseded、新的 rev3 標 failed，叢集上跑的仍是 rev2。rev3 存的是回復目標 rev1 的
# values。再失敗一次 ⇒ helm 把當時最新的 rev3 也改標 superseded、新的 rev4 標 failed，叢集仍是 rev2。
FAILED_ROLLBACK_HISTORY = [_rev(1, 'superseded'), _rev(2, 'superseded'), _rev(3, 'failed')]
TWICE_FAILED_ROLLBACK_HISTORY = [_rev(1, 'superseded'), _rev(2, 'superseded'),
                                 _rev(3, 'superseded'), _rev(4, 'failed')]
FAILED_ROLLBACK_SELECTIONS = {
    1: ['qa_base_2069'],
    2: ['qa_base_2069', 'qa_sale_2069'],
    3: ['qa_base_2069'],
    4: ['qa_base_2069'],
}
INSTALLED_SALE = {'qa_sale_2069': {'state': 'installed', 'dependents': []}}


class _FakeOperator:
    """回復會用到的 operator 呼叫，逐一記錄；其餘屬性存取一律炸掉（抓多打的 operator 呼叫）。"""

    def __init__(self, history=None, selections=None, module_states=None):
        self.history = HISTORY if history is None else history
        self.selections = dict(SELECTIONS if selections is None else selections)
        self.module_states = module_states
        self.calls = []
        self.projection_failures = {}  # revision → exception
        self.projection_overrides = {}  # revision → raw response
        self.module_state_error = None
        self.module_state_response = None
        self.history_error = None  # get_revisions 要拋的例外
        self.history_response = None  # get_revisions 的原始回應（覆寫 {'revisions': history}）
        self.rollback_release = Mock(return_value={
            'message': 'ok', 'mode': 'rollback', 'revision': 4, 'chart_version': '0.2.7'})

    def get_revisions(self, namespace, release_name):
        self.calls.append(('get_revisions',))
        if self.history_error is not None:
            raise self.history_error
        if self.history_response is not None:
            return self.history_response
        return {'revisions': self.history}

    def get_config_projection(self, namespace, release_name, revision, paths, excluded_paths=None):
        self.calls.append(('get_config_projection', revision, tuple(paths)))
        if revision in self.projection_failures:
            raise self.projection_failures[revision]
        if revision in self.projection_overrides:
            return self.projection_overrides[revision]
        return {'revision': revision, 'values': {ADDONS_PATH: list(self.selections[revision])},
                'missing': []}

    def get_odoo_module_states(self, namespace, release_name, module_names):
        self.calls.append(('get_odoo_module_states', tuple(module_names)))
        if self.module_state_error is not None:
            raise self.module_state_error
        if self.module_state_response is not None:
            return self.module_state_response
        return {'success': True, 'modules': {
            name: dict(self.module_states.get(name, {'state': 'not_found', 'dependents': []}))
            for name in module_names}}

    def state_queries(self):
        return [call for call in self.calls if call[0] == 'get_odoo_module_states']

    def projections(self):
        return [call[1] for call in self.calls if call[0] == 'get_config_projection']


class _GuardFixture:
    """共用 fixture：admin 名下工作區＋一台 woow-odoo 租戶（目前部署 rev3，勾 base＋sale）。"""

    def _setup_fixture(self):
        self.admin_user = self.env.ref('base.user_admin')
        self.tpl = self.env.ref('woow_paas_platform.cloud_app_odoo')
        self.assertTrue(self.tpl._is_woow_odoo_chart(), '前提：seed 的 odoo 範本是 woow-odoo chart')
        self.ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 2069', 'owner_id': self.admin_user.id})
        Addon = self.env['woow_paas_platform.cloud_odoo_addon']
        self.base_addon = Addon.create({
            'name': 'Base 2069', 'technical_name': 'qa_base_2069', 'source_type': 'third_party'})
        self.sale_addon = Addon.create({
            'name': 'Sale 2069', 'technical_name': 'qa_sale_2069', 'source_type': 'third_party'})

    def _make_service(self, state='running', template=None, helm_values=None, tag='svc'):
        return _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'workspace_id': self.ws.id, 'template_id': (template or self.tpl).id,
            'name': 'svc-2069-%s' % tag, 'reference_id': 'test-2069-%s' % tag,
            'state': state, 'subdomain': 'svc-2069-%s' % tag,
            'helm_namespace': 'paas-ws-test-2069', 'helm_release_name': 'svc-2069-%s' % tag,
            'helm_values': json.dumps(helm_values or {}), 'helm_revision': 3,
            'helm_chart_version': '0.2.7',
            'odoo_addon_ids': [(6, 0, [self.base_addon.id, self.sale_addon.id])],
        })


@tagged('post_install', '-at_install')
class TestRollbackAddonGuard2069(_GuardFixture, TransactionCase):
    """權威那一道：``_rollback_service`` 動手前重新裁決。"""

    def setUp(self):
        super().setUp()
        self._setup_fixture()
        self.controller = PaasController()
        self.service = self._make_service()

    # ---- 驅動 --------------------------------------------------------------

    def _rollback(self, operator, service=None, revision=2, acknowledge=False):
        service = service or self.service
        with patch(_CLIENT_FACTORY, return_value=operator), \
                patch(f'{_CONTROLLER}.request', new=MagicMock(env=self.env(user=self.admin_user))):
            return self.controller._rollback_service(
                service, revision, acknowledge_unverified_addons=acknowledge)

    def _snapshot(self, service=None):
        service = (service or self.service).sudo()
        service.invalidate_recordset()
        return (service.state, service.error_message, service.helm_revision,
                service.last_upgraded_at, service.rollback_config_sync,
                service.helm_chart_version, tuple(sorted(service.odoo_addon_ids.ids)),
                service.init_generation)

    def _assert_refused(self, operator, response, data_code, before, service=None):
        """被擋下：data.code 對、沒有呼叫 rollback_release、欄位一個都沒動。"""
        self.assertFalse(response.get('success'), response)
        self.assertEqual(response.get('data', {}).get('code'), data_code, response)
        self.assertTrue(response.get('error'), response)
        operator.rollback_release.assert_not_called()
        self.assertEqual(self._snapshot(service), before, '被擋下的回復不得寫任何欄位')

    def _assert_proceeded(self, operator, response, service=None, revision=2):
        """真的回復了：rollback_release 被呼叫一次，且回復後的寫入真的發生。

        state 一律變 upgrading——但服務本來就是 upgrading 時那條斷言恆真，所以另外釘住回復才會寫的
        欄位：``helm_revision`` 換成 operator 回報的新 revision（fake 回 4，fixture 起始 3），
        ``last_upgraded_at`` 從空值變成有值。
        """
        service = service or self.service
        self.assertTrue(response.get('success'), response)
        operator.rollback_release.assert_called_once()
        self.assertEqual(operator.rollback_release.call_args.kwargs['revision'], revision)
        self.assertEqual(service.state, 'upgrading')
        self.assertEqual(service.helm_revision, 4, '回復後帳本要記 operator 回報的新 revision')
        self.assertTrue(service.last_upgraded_at, '回復要寫 last_upgraded_at')

    # ---- blocked -----------------------------------------------------------

    def test_module_still_in_odoo_blocks_the_rollback(self):
        for state in ('installed', 'to upgrade', 'to install', 'to remove'):
            with self.subTest(state=state):
                service = self._make_service(tag=state.replace(' ', '-'))
                operator = _FakeOperator(module_states={
                    'qa_sale_2069': {'state': state, 'dependents': ['qa_report_2069']}})
                before = self._snapshot(service)

                response = self._rollback(operator, service=service)

                self._assert_refused(
                    operator, response, 'rollback_removes_installed_addons', before, service)
                self.assertEqual(response['data']['blocking'], {'qa_sale_2069': state})
                self.assertEqual(response['data']['modules'], {'qa_sale_2069': {
                    'state': state, 'dependents': ['qa_report_2069'],
                    'addon_id': self.sale_addon.id}})
                self.assertIn('qa_sale_2069', response['error'])
                self.assertIn('qa_report_2069', response['error'], '要列出依賴它的已安裝模組')
                self.assertIn('Uninstall them in Odoo first', response['error'])
                self.assertEqual(operator.state_queries(),
                                 [('get_odoo_module_states', ('qa_sale_2069',))],
                                 '只查被拿掉的模組（qa_base_2069 兩邊都有）')

    def test_acknowledge_does_not_override_blocked(self):
        operator = _FakeOperator(module_states={
            'qa_sale_2069': {'state': 'installed', 'dependents': []}})
        before = self._snapshot()

        response = self._rollback(operator, acknowledge=True)

        self._assert_refused(operator, response, 'rollback_removes_installed_addons', before)

    def test_module_missing_from_the_platform_catalog_is_still_assessed(self):
        """目錄沒有（或已封存）的模組不得被 id 形外殼靜默濾掉——回復拿掉的是 Helm 的技術名。"""
        operator = _FakeOperator(
            selections={3: ['qa_base_2069', 'qa_ghost_2069'], 2: ['qa_base_2069']},
            module_states={'qa_ghost_2069': {'state': 'installed', 'dependents': []}})
        before = self._snapshot()

        response = self._rollback(operator)

        self._assert_refused(operator, response, 'rollback_removes_installed_addons', before)
        self.assertEqual(response['data']['modules']['qa_ghost_2069']['addon_id'], None)

    # ---- safe --------------------------------------------------------------

    def test_uninstalled_or_unknown_to_odoo_is_safe(self):
        for state in ('uninstalled', 'not_found', 'uninstallable'):
            with self.subTest(state=state):
                self.assertIn(state, _ADDON_REMOVAL_SAFE_STATES)
                service = self._make_service(tag=state)
                operator = _FakeOperator(module_states={
                    'qa_sale_2069': {'state': state, 'dependents': []}})

                response = self._rollback(operator, service=service)

                self._assert_proceeded(operator, response, service)
                self.assertEqual(len(operator.state_queries()), 1, '真的查過才放行')

    # ---- 沒有被拿掉的模組 --------------------------------------------------

    def test_no_removal_never_queries_module_states_in_any_state(self):
        """往前回復／只增不減：不查模組狀態，任何 state 都照舊回復（含 operator 查不了狀態時）。"""
        for state in ('running', 'error', 'deploying', 'initializing', 'upgrading'):
            with self.subTest(state=state):
                service = self._make_service(state=state, tag='nr-%s' % state)
                operator = _FakeOperator(selections={3: ['qa_base_2069'], 2: ['qa_base_2069',
                                                                              'qa_sale_2069']})
                operator.module_state_error = PaaSOperatorConnectionError('unreachable')

                response = self._rollback(operator, service=service)

                self._assert_proceeded(operator, response, service)
                self.assertEqual(operator.state_queries(), [], '沒有被拿掉的模組就不得查模組狀態')
                self.assertEqual(sorted(operator.projections()), [2, 3])

    # ---- unverified ----------------------------------------------------------

    def test_not_running_refuses_unless_acknowledged(self):
        for state in ('error', 'deploying', 'initializing', 'upgrading'):
            with self.subTest(state=state):
                service = self._make_service(state=state, tag='uv-%s' % state)
                operator = _FakeOperator(module_states={})
                before = self._snapshot(service)

                refused = self._rollback(operator, service=service)

                self._assert_refused(operator, refused, 'rollback_addon_unverified', before, service)
                self.assertIn('qa_sale_2069', refused['error'])
                self.assertEqual(operator.state_queries(), [], 'Pod 不是 running ⇒ 不去 exec')

                acknowledged = self._rollback(operator, service=service, acknowledge=True)

                self._assert_proceeded(operator, acknowledged, service)

    def test_unreadable_selection_while_not_running_is_unverified(self):
        service = self._make_service(state='error', tag='uv-unreadable')
        operator = _FakeOperator()
        operator.projection_failures[2] = PaaSOperatorAPIError('boom', status_code=503)
        before = self._snapshot(service)

        refused = self._rollback(operator, service=service)
        self._assert_refused(operator, refused, 'rollback_addon_unverified', before, service)

        acknowledged = self._rollback(operator, service=service, acknowledge=True)
        self._assert_proceeded(operator, acknowledged, service)

    # ---- check_failed --------------------------------------------------------

    def _assert_check_failed(self, operator, service=None):
        service = service or self.service
        before = self._snapshot(service)
        response = self._rollback(operator, service=service)
        self._assert_refused(operator, response, 'rollback_addon_check_failed', before, service)
        self.assertNotIn('cannot be restored', response['error'],
                         '守衛自己的失敗不得被誤報成 revision 問題')
        acknowledged = self._rollback(operator, service=service, acknowledge=True)
        self._assert_refused(operator, acknowledged, 'rollback_addon_check_failed', before, service)
        return response

    def test_module_state_query_raising_is_check_failed(self):
        operator = _FakeOperator(module_states={})
        operator.module_state_error = PaaSOperatorAPIError('down', status_code=404)
        response = self._assert_check_failed(operator)
        self.assertIn('qa_sale_2069', response['error'])

    def test_operator_success_false_is_check_failed(self):
        operator = _FakeOperator(module_states={})
        operator.module_state_response = {'success': False, 'error': '[find_pod] no pod'}
        self._assert_check_failed(operator)

    def test_malformed_module_state_answer_is_check_failed(self):
        for bad in ({'success': True},
                    {'success': True, 'modules': None},
                    {'success': True, 'modules': []},
                    {'success': True, 'modules': {'qa_other_2069': {'state': 'uninstalled'}}},
                    {'success': True, 'modules': {'qa_sale_2069': 'uninstalled'}},
                    'not a dict'):
            with self.subTest(bad=bad):
                operator = _FakeOperator(module_states={})
                operator.module_state_response = bad
                self._assert_check_failed(operator)

    def test_missing_state_blocks_rather_than_passes(self):
        """state 缺值／非字串 ⇒ 'unknown'（不在 safe 集合）⇒ 擋；不得因 unhashable 值炸成 500。"""
        for info in ({'dependents': []}, {'state': None}, {'state': ['installed']}):
            with self.subTest(info=info):
                operator = _FakeOperator(module_states={})
                operator.module_state_response = {'success': True, 'modules': {'qa_sale_2069': info}}
                before = self._snapshot()
                response = self._rollback(operator)
                self._assert_refused(operator, response, 'rollback_removes_installed_addons', before)
                self.assertEqual(response['data']['blocking'], {'qa_sale_2069': 'unknown'})

    def test_unreadable_current_selection_is_check_failed(self):
        operator = _FakeOperator(module_states={})
        operator.projection_failures[3] = PaaSOperatorAPIError('boom', status_code=404)
        self._assert_check_failed(operator)
        self.assertEqual(operator.state_queries(), [])

    def test_unreadable_target_selection_is_check_failed(self):
        operator = _FakeOperator(module_states={})
        operator.projection_failures[2] = PaaSOperatorAPIError('boom', status_code=400)
        self._assert_check_failed(operator)
        self.assertEqual(operator.state_queries(), [])

    def test_malformed_projection_is_check_failed(self):
        """路徑不存在、回別的 revision、清單形狀不對：都不是「空清單」。"""
        for side in (2, 3):
            for bad in ({'revision': side, 'values': {}, 'missing': [ADDONS_PATH]},
                        {'revision': 99, 'values': {ADDONS_PATH: []}, 'missing': []},
                        {'revision': side, 'values': {ADDONS_PATH: 'qa_sale_2069'}, 'missing': []},
                        {'revision': side, 'values': {ADDONS_PATH: ['qa_sale_2069', 7]}, 'missing': []},
                        {'revision': side, 'values': None, 'missing': []},
                        None):
                with self.subTest(side=side, bad=bad):
                    operator = _FakeOperator(module_states={})
                    operator.projection_overrides[side] = bad
                    self._assert_check_failed(operator)

    def test_history_without_any_revision_is_check_failed(self):
        operator = _FakeOperator(history=[], module_states={})
        response = self._assert_check_failed(operator)
        self.assertIn('version history of this service could not be read', response['error'])

    def test_history_without_usable_numbers_is_check_failed_in_every_state(self):
        """歷史空的、或沒有任何可用的 revision 號：任何 state 都是 check_failed（與預檢端點同一句），
        確認也放不行——回復本身沒有東西可以對照，帶確認送出只會讓 operator 回 404。"""
        for label, history in (('empty', []),
                               ('no usable number', [{'revision': None, 'status': 'deployed'},
                                                     {'status': 'failed'}, 'garbage'])):
            for state in ('running', 'error', 'deploying', 'initializing', 'upgrading'):
                with self.subTest(case=label, state=state):
                    service = self._make_service(state=state, tag='nohist-%s-%s' % (
                        label.replace(' ', '-'), state))
                    operator = _FakeOperator(history=history, module_states={})
                    before = self._snapshot(service)
                    for acknowledge in (False, True):
                        response = self._rollback(operator, service=service, acknowledge=acknowledge)
                        self._assert_refused(
                            operator, response, 'rollback_addon_check_failed', before, service)
                        self.assertEqual(response['error'], (
                            'The version history of this service could not be read, so it cannot be '
                            'checked which modules rolling back to revision 2 would remove. Please try '
                            'again later.'))
                    self.assertEqual(operator.calls, [('get_revisions',)] * 2,
                                     '歷史沒有可用號碼就不讀 projection、不查模組狀態')

    def test_unexpected_exception_in_the_guard_is_check_failed(self):
        """守衛內部例外（含 operator 的 400／404）不得掉進外層「Revision … cannot be restored」。"""
        Service = type(self.env['woow_paas_platform.cloud_service'])
        for exc in (PaaSOperatorAPIError('nope', status_code=404), RuntimeError('bug'), KeyError('x')):
            with self.subTest(exc=exc):
                operator = _FakeOperator(module_states={})
                with patch.object(Service, '_rollback_removed_odoo_addons', side_effect=exc), \
                        self.assertLogs(_CONTROLLER, level=logging.ERROR):
                    self._assert_check_failed(operator)

    def test_unexpected_exception_while_not_running_is_unverified(self):
        Service = type(self.env['woow_paas_platform.cloud_service'])
        service = self._make_service(state='error', tag='uv-exc')
        operator = _FakeOperator(module_states={})
        before = self._snapshot(service)
        with patch.object(Service, '_rollback_removed_odoo_addons', side_effect=RuntimeError('bug')), \
                self.assertLogs(_CONTROLLER, level=logging.ERROR):
            refused = self._rollback(operator, service=service)
        self._assert_refused(operator, refused, 'rollback_addon_unverified', before, service)

    # ---- 基準：目前部署中的 revision，不是帳本 -------------------------------

    def test_current_side_is_the_deployed_revision_not_the_ledger(self):
        """最新的 rev4 升級失敗（failed）、帳本記 4：基準是 deployed 的 rev3（rev4 少了 qa_sale_2069，
        但 rev3 還在跑它）。rev4 比基準新 ⇒ 也算「可能正在跑」，一起讀；帳本的值不影響判斷。"""
        history = HISTORY + [_rev(4, 'failed')]
        selections = {**SELECTIONS, 4: ['qa_base_2069']}
        operator = _FakeOperator(history=history, selections=selections, module_states={
            'qa_sale_2069': {'state': 'installed', 'dependents': []}})
        self.service.sudo().write({'helm_revision': 4})
        before = self._snapshot()

        response = self._rollback(operator)

        self._assert_refused(operator, response, 'rollback_removes_installed_addons', before)
        self.assertEqual(response['data']['blocking'], {'qa_sale_2069': 'installed'})
        self.assertEqual(sorted(operator.projections()), [2, 3, 4])

    # ---- 可能正在跑的 revision：比基準新的 failed／pending-* ------------------
    #
    # 平台的一般升級不帶 --atomic／--wait：升級失敗（failed）或還在進行（pending-upgrade）的 revision
    # 可能已經把 Deployment 換成它的勾選，租戶接著把只在那一版的模組裝起來。只看 deployed 的基準會把
    # 「退回原本那一版」誤判成沒有模組被拿掉（rev4 failed 多勾 qa_sale_2069 ⇒ 回復到 rev3／rev2 照樣
    # 送出，error 狀態連確認都不問）。

    def _assert_rolled_back_to(self, operator, response, revision, service):
        self._assert_proceeded(operator, response, service=service, revision=revision)

    def _assert_each_projection_read_once(self, operator):
        projections = operator.projections()
        self.assertEqual(len(projections), len(set(projections)), '同一個 revision 不得讀兩次：%s'
                         % projections)

    def test_newer_unfinished_revision_adding_an_installed_module_blocks(self):
        for status in NEWER_STATUSES:
            for target in (3, 2):
                with self.subTest(status=status, target=target):
                    service = self._make_service(tag='nw-%s-%s' % (status, target))
                    operator = _FakeOperator(
                        history=_history_with_newer(status), selections=NEWER_SELECTIONS,
                        module_states={'qa_sale_2069': {'state': 'installed', 'dependents': []}})
                    before = self._snapshot(service)

                    response = self._rollback(operator, service=service, revision=target)

                    self._assert_refused(
                        operator, response, 'rollback_removes_installed_addons', before, service)
                    self.assertEqual(response['data']['blocking'], {'qa_sale_2069': 'installed'})
                    self.assertEqual(operator.state_queries(),
                                     [('get_odoo_module_states', ('qa_sale_2069',))])
                    self.assertEqual(sorted(operator.projections()), sorted({target, 3, 4}))
                    self._assert_each_projection_read_once(operator)

                    acknowledged = self._rollback(operator, service=service, revision=target,
                                                  acknowledge=True)
                    self._assert_refused(
                        operator, acknowledged, 'rollback_removes_installed_addons', before, service)

    def test_newer_unfinished_revision_while_not_running_is_unverified(self):
        for status in NEWER_STATUSES:
            for target in (3, 2):
                with self.subTest(status=status, target=target):
                    service = self._make_service(state='error', tag='nwu-%s-%s' % (status, target))
                    operator = _FakeOperator(history=_history_with_newer(status),
                                             selections=NEWER_SELECTIONS, module_states={})
                    before = self._snapshot(service)

                    refused = self._rollback(operator, service=service, revision=target)

                    self._assert_refused(operator, refused, 'rollback_addon_unverified', before, service)
                    self.assertIn('qa_sale_2069', refused['error'])
                    self.assertEqual(operator.state_queries(), [])

                    acknowledged = self._rollback(operator, service=service, revision=target,
                                                  acknowledge=True)
                    self._assert_rolled_back_to(operator, acknowledged, target, service)

    def test_newer_failed_revision_with_the_same_selection_proceeds(self):
        operator = _FakeOperator(
            history=_history_with_newer('failed'),
            selections={**NEWER_SELECTIONS, 4: ['qa_base_2069']},
            module_states={})
        operator.module_state_error = PaaSOperatorConnectionError('unreachable')

        response = self._rollback(operator, revision=3)

        self._assert_rolled_back_to(operator, response, 3, self.service)
        self.assertEqual(operator.state_queries(), [], '沒有被拿掉的模組就不得查模組狀態')
        self.assertEqual(sorted(operator.projections()), [3, 4], '目標就是基準：沿用，不重讀')

    def test_unreadable_newer_revision_selection_is_check_failed(self):
        for label, broken in (
                ('raises', lambda op: op.projection_failures.__setitem__(
                    4, PaaSOperatorAPIError('boom', status_code=503))),
                ('missing path', lambda op: op.projection_overrides.__setitem__(
                    4, {'revision': 4, 'values': {}, 'missing': [ADDONS_PATH]})),
                ('wrong shape', lambda op: op.projection_overrides.__setitem__(
                    4, {'revision': 4, 'values': {ADDONS_PATH: 'qa_sale_2069'}, 'missing': []}))):
            for target in (3, 2):
                with self.subTest(case=label, target=target):
                    service = self._make_service(tag='nwx-%s-%s' % (label.replace(' ', '-'), target))
                    operator = _FakeOperator(history=_history_with_newer('failed'),
                                             selections=NEWER_SELECTIONS, module_states={})
                    broken(operator)
                    before = self._snapshot(service)
                    response = self._rollback(operator, service=service, revision=target)
                    self._assert_refused(
                        operator, response, 'rollback_addon_check_failed', before, service)
                    self.assertEqual(operator.state_queries(), [])
                    acknowledged = self._rollback(operator, service=service, revision=target,
                                                  acknowledge=True)
                    self._assert_refused(
                        operator, acknowledged, 'rollback_addon_check_failed', before, service)

    def test_several_newer_revisions_are_all_counted(self):
        """rev4 failed 多勾 sale、rev5 pending-upgrade 多勾 crm：兩個都可能在跑，都算被拿掉。"""
        history = _history_with_newer('failed') + [_rev(5, 'pending-upgrade')]
        selections = {**NEWER_SELECTIONS, 5: ['qa_base_2069', 'qa_crm_2069']}
        operator = _FakeOperator(history=history, selections=selections, module_states={
            'qa_crm_2069': {'state': 'installed', 'dependents': []}})
        before = self._snapshot()

        response = self._rollback(operator, revision=2)

        self._assert_refused(operator, response, 'rollback_removes_installed_addons', before)
        self.assertEqual(response['data']['blocking'], {'qa_crm_2069': 'installed'})
        self.assertEqual(operator.state_queries(),
                         [('get_odoo_module_states', ('qa_crm_2069', 'qa_sale_2069'))])
        self.assertEqual(sorted(operator.projections()), [2, 3, 4, 5])
        self._assert_each_projection_read_once(operator)

    def test_target_equal_to_the_baseline_with_a_newer_failed_revision(self):
        """目標＝基準 rev3、rev4 failed：不是 already_current（最新的不是 deployed），會走到守衛；
        守衛不得再以「目標就是目前」短路成沒有模組被拿掉。"""
        history = _history_with_newer('failed')
        self.assertIsNone(rollback_request_problem(history, 3), '前提：rev3 不是 already_current')

        operator = _FakeOperator(history=history, selections=NEWER_SELECTIONS)
        applicable, removed = self.service._rollback_removed_odoo_addons(operator, history, 3)
        self.assertEqual((applicable, removed), (True, ['qa_sale_2069']))
        self.assertEqual(sorted(operator.projections()), [3, 4], '目標 rev3 已在聯集裡讀過，不重讀')

        operator = _FakeOperator(history=history, selections=NEWER_SELECTIONS, module_states={
            'qa_sale_2069': {'state': 'installed', 'dependents': []}})
        before = self._snapshot()
        response = self._rollback(operator, revision=3)
        self.assertNotEqual(response.get('data', {}).get('code'), 'already_current', response)
        self._assert_refused(operator, response, 'rollback_removes_installed_addons', before)

        # 回復到失敗的那一版本身（API 收得到）：聯集減掉 rev4 的勾選，rev3 的都還在 ⇒ 沒有被拿掉的
        operator = _FakeOperator(history=history, selections=NEWER_SELECTIONS)
        self.assertEqual(self.service._rollback_removed_odoo_addons(operator, history, 4), (True, []))
        self.assertEqual(sorted(operator.projections()), [3, 4])

    def test_no_extra_operator_reads_when_nothing_is_newer(self):
        # 目標＝基準、沒有更新的 revision ⇒ 一次 projection 都不讀
        operator = _FakeOperator()
        self.assertEqual(self.service._rollback_removed_odoo_addons(operator, HISTORY, 3), (True, []))
        self.assertEqual(operator.calls, [])

        # 往回：只讀基準與目標兩個
        operator = _FakeOperator()
        self.assertEqual(self.service._rollback_removed_odoo_addons(operator, HISTORY, 2),
                         (True, ['qa_sale_2069']))
        self.assertEqual(sorted(operator.projections()), [2, 3])

        # 比基準**舊**的 failed 不算（已被基準取代）；沒有可用 revision 號的列略過
        history = [_rev(1, 'superseded'), _rev(2, 'failed'), _rev(3, 'deployed'),
                   {'revision': None, 'status': 'failed'}, {'revision': 'x', 'status': 'pending-upgrade'},
                   {'status': 'failed'}, 'garbage']
        operator = _FakeOperator(history=history)
        self.assertEqual(self.service._rollback_removed_odoo_addons(operator, history, 1),
                         (True, ['qa_base_2069', 'qa_sale_2069']))
        self.assertEqual(sorted(operator.projections()), [1, 3], '不讀 rev2（比基準舊）')

        operator = _FakeOperator(history=history)
        self.assertEqual(self.service._rollback_removed_odoo_addons(operator, history, 3), (True, []))
        self.assertEqual(operator.calls, [], '壞掉的歷史列不得讓短路失效')

    # ---- 歷史裡沒有 deployed：純 helm rollback 失敗之後 -----------------------
    #
    # 純 helm rollback（不帶 --wait，woow-odoo 租戶走這條）套用時失敗：helm 把原本 deployed 的那一版
    # 改標 superseded、新的那一版標 failed，叢集上跑的卻仍是原本那一版（沙盒 helm 3.19.5 實測，
    # 兩次失敗都一樣）。基準若取「最新的那一列」只會讀到失敗的副本，漏掉仍在跑的 rev2——它勾著已安裝
    # 的 qa_sale_2069。沒有 deployed 時每一個 revision 都要算。

    def _assert_no_deployed_history_is_guarded(self, history):
        expected_projections = sorted({1, *(row['revision'] for row in history)})
        for state, data_code in (('running', 'rollback_removes_installed_addons'),
                                 ('error', 'rollback_addon_unverified')):
            with self.subTest(state=state):
                service = self._make_service(state=state, tag='nodep-%s-%s' % (len(history), state))
                operator = _FakeOperator(history=history, selections=FAILED_ROLLBACK_SELECTIONS,
                                         module_states=INSTALLED_SALE)
                before = self._snapshot(service)

                refused = self._rollback(operator, service=service, revision=1)

                self._assert_refused(operator, refused, data_code, before, service)
                self.assertIn('qa_sale_2069', refused['error'])
                self.assertEqual(sorted(operator.projections()), expected_projections,
                                 '沒有 deployed ⇒ 每一個 revision 都可能在跑，都要讀')
                self._assert_each_projection_read_once(operator)
                if state == 'running':
                    self.assertEqual(refused['data']['blocking'], {'qa_sale_2069': 'installed'})
                    self.assertEqual(operator.state_queries(),
                                     [('get_odoo_module_states', ('qa_sale_2069',))])
                    acknowledged = self._rollback(operator, service=service, revision=1,
                                                  acknowledge=True)
                    self._assert_refused(operator, acknowledged, data_code, before, service)
                else:
                    self.assertEqual(operator.state_queries(), [])
                    acknowledged = self._rollback(operator, service=service, revision=1,
                                                  acknowledge=True)
                    self._assert_rolled_back_to(operator, acknowledged, 1, service)

    def test_failed_plain_rollback_without_deployed_row_is_guarded(self):
        """[1 superseded, 2 superseded, 3 failed]：rev2 仍在跑、勾著已安裝的 sale ⇒ 回復到 rev1 要擋。"""
        self._assert_no_deployed_history_is_guarded(FAILED_ROLLBACK_HISTORY)

    def test_twice_failed_plain_rollback_without_deployed_row_is_guarded(self):
        """[1 s, 2 s, 3 s, 4 failed]：第二次失敗把 rev3（第一次的失敗副本）也標成 superseded。
        「最新的 superseded」會指到 rev3、漏掉仍在跑的 rev2——這條就是用來分辨那種起點的。"""
        self._assert_no_deployed_history_is_guarded(TWICE_FAILED_ROLLBACK_HISTORY)

    # ---- 不適用的服務 --------------------------------------------------------

    def test_other_templates_get_no_extra_operator_calls(self):
        other = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Other 2069', 'slug': 'qa-other-2069', 'category': 'ai_llm',
            'helm_repo_url': '', 'helm_chart_name': 'oci://qa/not-odoo-2069',
            'helm_chart_version': '0.2.7', 'mcp_enabled': False, 'helm_default_values': '{}'})
        service = self._make_service(template=other, tag='other')
        operator = _FakeOperator(history=[
            {'revision': n, 'chart': 'not-odoo-2069-0.2.7', 'status': s}
            for n, s in ((1, 'superseded'), (2, 'superseded'), (3, 'deployed'))])

        response = self._rollback(operator, service=service)

        self.assertTrue(response.get('success'), response)
        self.assertEqual(operator.calls, [('get_revisions',)], '其他服務不得多打 operator')
        operator.rollback_release.assert_called_once()

    def test_legacy_bitnami_release_is_untouched(self):
        service = self._make_service(helm_values={'postgresql': {'enabled': True}}, tag='legacy')
        self.assertFalse(service._is_live_woow_odoo_tenant(), '前提：Bitnami 時代的存量')
        operator = _FakeOperator()

        response = self._rollback(operator, service=service)

        self.assertTrue(response.get('success'), response)
        self.assertEqual(operator.calls, [('get_revisions',)])

    # ---- 取消勾選那條路不變 --------------------------------------------------

    def test_id_based_untick_assessment_is_unchanged(self):
        """id 形外殼：同樣的判準、同樣的回傳形狀；目錄找不到的 id 照舊視為沒有要拿掉的。"""
        operator = _FakeOperator(module_states={
            'qa_sale_2069': {'state': 'installed', 'dependents': ['qa_report_2069']}})
        with patch(_CLIENT_FACTORY, return_value=operator), \
                patch(f'{_CONTROLLER}.request', new=MagicMock(env=self.env(user=self.admin_user))):
            verdict = self.controller._assess_addon_removal(self.service, [self.sale_addon.id])
            nothing = self.controller._assess_addon_removal(self.service, [999999999])
        self.assertEqual(verdict, {
            'can_assess': True, 'error': None,
            'modules': {'qa_sale_2069': {'addon_id': self.sale_addon.id, 'state': 'installed',
                                         'dependents': ['qa_report_2069']}},
            'blocking': {'qa_sale_2069': {'addon_id': self.sale_addon.id, 'state': 'installed',
                                          'dependents': ['qa_report_2069']}},
        })
        self.assertEqual(nothing, {'can_assess': True, 'error': None, 'modules': {}, 'blocking': {}})

    def test_id_based_untick_assessment_is_fail_closed_on_an_incomplete_answer(self):
        """#2069 順手收緊取消勾選那條路：operator 漏答要查的模組、或 modules 不是 dict ⇒ 無法裁決，
        不得當成「沒有要擋的」（以前只迭代回應裡有的鍵，漏答的模組會被靜默放行）。"""
        for answer in ({'success': True, 'modules': {}},
                       {'success': True},
                       {'success': True, 'modules': {'qa_other_2069': {'state': 'uninstalled'}}}):
            with self.subTest(answer=answer):
                operator = _FakeOperator(module_states={})
                operator.module_state_response = answer
                with patch(_CLIENT_FACTORY, return_value=operator), \
                        patch(f'{_CONTROLLER}.request', new=MagicMock(env=self.env(user=self.admin_user))):
                    verdict = self.controller._assess_addon_removal(self.service, [self.sale_addon.id])
                self.assertFalse(verdict['can_assess'], verdict)
                self.assertEqual((verdict['modules'], verdict['blocking']), ({}, {}))
                self.assertIn('incomplete answer', verdict['error'])
                self.assertEqual(operator.state_queries(), [('get_odoo_module_states', ('qa_sale_2069',))])

    # ---- 補強：重複名稱、混合狀態、失敗原因、確認留痕 -------------------------------

    def test_duplicate_names_in_addons_selected_are_assessed_once(self):
        """addons.selected 有重複名稱：每個模組只查一次、只列一次。"""
        operator = _FakeOperator(
            selections={3: ['qa_base_2069', 'qa_sale_2069', 'qa_sale_2069'],
                        2: ['qa_base_2069', 'qa_base_2069']},
            module_states={'qa_sale_2069': {'state': 'installed', 'dependents': []}})
        before = self._snapshot()

        response = self._rollback(operator)

        self._assert_refused(operator, response, 'rollback_removes_installed_addons', before)
        self.assertEqual(operator.state_queries(), [('get_odoo_module_states', ('qa_sale_2069',))])
        self.assertEqual(response['data']['blocking'], {'qa_sale_2069': 'installed'})
        self.assertEqual(response['error'].count("'qa_sale_2069'"), 1)

    def test_mixed_states_block_only_the_unsafe_modules_and_report_all_of_them(self):
        """幾個被拿掉的模組、狀態不一：一次查完（名稱排序）；blocking 只有不安全的那個，modules 全列。"""
        operator = _FakeOperator(
            selections={3: ['qa_base_2069', 'qa_crm_2069', 'qa_sale_2069', 'qa_stock_2069'],
                        2: ['qa_base_2069']},
            module_states={
                'qa_crm_2069': {'state': 'uninstalled', 'dependents': []},
                'qa_sale_2069': {'state': 'to upgrade', 'dependents': ['qa_report_2069']},
                # qa_stock_2069 沒登記 ⇒ _FakeOperator 回 not_found（safe）
            })
        before = self._snapshot()

        response = self._rollback(operator)

        self._assert_refused(operator, response, 'rollback_removes_installed_addons', before)
        self.assertEqual(operator.state_queries(), [(
            'get_odoo_module_states', ('qa_crm_2069', 'qa_sale_2069', 'qa_stock_2069'))])
        self.assertEqual(response['data']['blocking'], {'qa_sale_2069': 'to upgrade'})
        self.assertEqual({name: entry['state'] for name, entry in response['data']['modules'].items()},
                         {'qa_crm_2069': 'uninstalled', 'qa_sale_2069': 'to upgrade',
                          'qa_stock_2069': 'not_found'})
        self.assertIn('qa_sale_2069', response['error'])
        self.assertIn('qa_report_2069', response['error'])
        self.assertNotIn('qa_crm_2069', response['error'])
        self.assertNotIn('qa_stock_2069', response['error'])

    def test_check_failed_after_the_state_query_names_the_modules(self):
        """失敗發生在「查模組狀態」這一步（不是 projection、不是守衛內部例外）：只查一次，訊息點名被拿掉的
        模組；「勾選讀不到」那條路的訊息不得混進來——兩種原因不能被同一個斷言蓋過去。"""
        cases = (
            ('raises', {'error': PaaSOperatorConnectionError('unreachable')}),
            ('success false', {'response': {'success': False, 'error': '[find_pod] no pod'}}),
            ('empty modules', {'response': {'success': True, 'modules': {}}}),
        )
        for label, setup in cases:
            with self.subTest(case=label):
                operator = _FakeOperator(module_states={})
                operator.module_state_error = setup.get('error')
                operator.module_state_response = setup.get('response')
                before = self._snapshot()

                response = self._rollback(operator)

                self._assert_refused(operator, response, 'rollback_addon_check_failed', before)
                self.assertEqual(len(operator.state_queries()), 1)
                self.assertIn("'qa_sale_2069'", response['error'])
                self.assertIn('could not be checked', response['error'])
                self.assertNotIn('could not be read', response['error'])

    def test_unreadable_selection_says_the_selection_could_not_be_read(self):
        operator = _FakeOperator(module_states={})
        operator.projection_failures[2] = PaaSOperatorAPIError('boom', status_code=503)
        before = self._snapshot()

        response = self._rollback(operator)

        self._assert_refused(operator, response, 'rollback_addon_check_failed', before)
        self.assertIn('module selection of the revisions could not be read', response['error'])
        self.assertIn('revision 2', response['error'])
        self.assertEqual(operator.state_queries(), [])

    def test_acknowledged_unverified_rollback_leaves_a_warning_in_the_log(self):
        """確認放行是「請求方承擔風險」：要在 log 留下放行了哪些模組（事後追查用）。"""
        service = self._make_service(state='error', tag='uv-log')
        operator = _FakeOperator(module_states={})

        with self.assertLogs(_CONTROLLER, level=logging.WARNING) as logs:
            response = self._rollback(operator, service=service, acknowledge=True)

        self._assert_proceeded(operator, response, service)
        self.assertTrue(
            any('acknowledged by the requester' in line and 'qa_sale_2069' in line
                for line in logs.output), logs.output)


@tagged('post_install', '-at_install')
class TestPossiblyLiveRevisions2069(TransactionCase):
    """純函式 ``possibly_live_revisions``：有 deployed ⇒ 最新的 deployed ＋ 每個比它新的；沒有 deployed
    ⇒ 每一個有可用號碼的 revision。由小到大、不重複；壞掉的列略過。"""

    def test_table(self):
        def rows(*pairs):
            return [_rev(number, status) for number, status in pairs]

        s, d, f = 'superseded', 'deployed', 'failed'
        cases = (
            # 沒有 deployed（純 helm rollback 失敗之後）⇒ 全部
            ('one failed plain rollback', rows((1, s), (2, s), (3, f)), [1, 2, 3]),
            ('two failed plain rollbacks', rows((1, s), (2, s), (3, s), (4, f)), [1, 2, 3, 4]),
            ('single failed row', rows((1, f)), [1]),
            ('only superseded rows', rows((1, s), (2, s)), [1, 2]),
            # 有 deployed ⇒ 最新的 deployed 起算，加上每一個比它新的（不論狀態）
            ('normal', rows((1, s), (2, s), (3, d)), [3]),
            ('deployed then failed upgrade', rows((1, d), (2, f)), [1, 2]),
            ('pending upgrade above deployed', rows((1, s), (2, d), (3, 'pending-upgrade')), [2, 3]),
            ('deployed below superseded and failed', rows((1, d), (2, s), (3, f)), [1, 2, 3]),
            ('older failed does not count', rows((1, f), (2, d)), [2]),
            ('two deployed rows: the newest wins', rows((1, d), (2, s), (3, d)), [3]),
            ('unordered', rows((3, f), (1, s), (2, d)), [2, 3]),
            ('duplicates', rows((2, d), (3, f), (3, f), (2, d), (1, s)), [2, 3]),
            ('upper-case statuses', rows((1, 'SUPERSEDED'), (2, 'DEPLOYED'), (3, 'FAILED')), [2, 3]),
            ('mixed-case deployed', rows((1, 'Deployed'), (2, 'Pending-Rollback')), [1, 2]),
            ('numbers as strings', [{'revision': '3', 'status': 'deployed'},
                                    {'revision': 4, 'status': 'failed'},
                                    {'revision': '4', 'status': 'pending-upgrade'},
                                    {'revision': '2', 'status': 'superseded'}], [3, 4]),
            ('numbers as strings without deployed', [{'revision': '1', 'status': 'superseded'},
                                                     {'revision': '2', 'status': 'failed'}], [1, 2]),
            ('junk rows are ignored', [None, 'garbage', 7, {'status': 'deployed'},
                                       {'revision': None, 'status': 'deployed'},
                                       {'revision': 'x', 'status': 'deployed'},
                                       *rows((1, d), (2, f))], [1, 2]),
            ('a junk deployed row does not pin the start', [{'revision': None, 'status': 'deployed'},
                                                            *rows((1, s), (2, f))], [1, 2]),
            ('row without status', [{'revision': 1}, {'revision': 2, 'status': 'deployed'}], [2]),
        )
        for label, history, expected in cases:
            with self.subTest(case=label):
                self.assertEqual(possibly_live_revisions(history), expected)

    def test_nothing_usable_is_an_empty_list(self):
        for history in (None, [], [{'status': 'deployed'}], [{'revision': None, 'status': 'failed'}],
                        ['garbage'], 'x'):
            with self.subTest(history=history):
                self.assertEqual(possibly_live_revisions(history), [])


class _RouteCase(_GuardFixture, HttpCase):
    """advisory 預檢端點：真的走 HTTP（受測的包含 JSON-RPC params 展開那段 plumbing，#474）。"""

    service_state = 'running'

    def setUp(self):
        super().setUp()
        self._setup_fixture()
        self.service = self._make_service(state=self.service_state, tag='route')
        self.authenticate('admin', 'admin')

    def _member(self, login, role):
        user = self.env['res.users'].create({
            'name': login, 'login': login, 'password': login + '-pw-2069',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        if role:
            self.env['woow_paas_platform.workspace_access'].create({
                'workspace_id': self.ws.id, 'user_id': user.id, 'role': role})
        return user

    def _rpc(self, url, params):
        resp = self.url_open(url, data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call', 'params': params, 'id': 2069,
        }), headers={'Content-Type': 'application/json'})
        parsed = resp.json()
        self.assertIn('result', parsed, '端點沒回 JSON-RPC result：%s' % parsed)
        return parsed['result']

    def _check(self, params, operator=None, service=None):
        url = '/api/cloud-services/services/%s/check-rollback-addons' % (service or self.service).id
        with patch(_CLIENT_FACTORY, return_value=operator):
            return self._rpc(url, params)

    def _rollback_rpc(self, params, operator=None, service=None):
        service = service or self.service
        url = '/api/workspaces/%s/services/%s/rollback' % (self.ws.id, service.id)
        with patch(_CLIENT_FACTORY, return_value=operator):
            return self._rpc(url, params)

    def _assert_contract(self, result):
        """回應形狀＝前端契約：鍵、型別、code 與 success 的對應。"""
        self.assertLessEqual(set(result), _CONTRACT_KEYS, result)
        self.assertIsInstance(result['success'], bool)
        self.assertIn(result['code'], _CODES)
        self.assertIsInstance(result['applicable'], bool)
        self.assertIsInstance(result['modules'], dict)
        self.assertIsInstance(result['blocking'], dict)
        self.assertEqual(result['success'], result['code'] in ('no_removal', 'safe', 'blocked'))
        if result['success']:
            self.assertNotIn('error', result)
        else:
            self.assertIsInstance(result['error'], str)
            self.assertTrue(result['error'])
        if 'removed' in result:
            self.assertIsInstance(result['removed'], list)
            self.assertEqual(result['removed'], sorted(result['removed']))
            self.assertTrue(all(isinstance(name, str) for name in result['removed']))
        for name, entry in result['modules'].items():
            self.assertEqual(set(entry), {'state', 'dependents', 'addon_id'}, entry)
            self.assertIsInstance(entry['state'], str)
            self.assertIsInstance(entry['dependents'], list)
            self.assertTrue(entry['addon_id'] is None or isinstance(entry['addon_id'], int))
        for name, state in result['blocking'].items():
            self.assertEqual(state, result['modules'][name]['state'])
            self.assertNotIn(state, _ADDON_REMOVAL_SAFE_STATES)
        self.assertEqual(result['code'] == 'blocked', bool(result['blocking']))
        if not result['applicable']:
            self.assertEqual((result.get('removed', []), result['modules'], result['blocking']),
                             ([], {}, {}))


@tagged('post_install', '-at_install')
class TestCheckRollbackAddonsRoute2069(_RouteCase):

    def test_blocked_shape(self):
        operator = _FakeOperator(module_states={
            'qa_sale_2069': {'state': 'installed', 'dependents': ['qa_report_2069']}})
        result = self._check({'revision': 2}, operator)
        self._assert_contract(result)
        self.assertEqual(result, {
            'success': True, 'code': 'blocked', 'applicable': True, 'removed': ['qa_sale_2069'],
            'modules': {'qa_sale_2069': {'state': 'installed', 'dependents': ['qa_report_2069'],
                                         'addon_id': self.sale_addon.id}},
            'blocking': {'qa_sale_2069': 'installed'}})
        operator.rollback_release.assert_not_called()

    def test_safe_shape(self):
        operator = _FakeOperator(module_states={
            'qa_sale_2069': {'state': 'uninstalled', 'dependents': []}})
        result = self._check({'revision': 2}, operator)
        self._assert_contract(result)
        self.assertEqual(result, {
            'success': True, 'code': 'safe', 'applicable': True, 'removed': ['qa_sale_2069'],
            'modules': {'qa_sale_2069': {'state': 'uninstalled', 'dependents': [],
                                         'addon_id': self.sale_addon.id}},
            'blocking': {}})

    def test_no_removal_shape(self):
        operator = _FakeOperator(selections={3: ['qa_base_2069'], 2: ['qa_base_2069', 'qa_sale_2069']})
        result = self._check({'revision': 2}, operator)
        self._assert_contract(result)
        self.assertEqual(result, {'success': True, 'code': 'no_removal', 'applicable': True,
                                  'removed': [], 'modules': {}, 'blocking': {}})
        self.assertEqual(operator.state_queries(), [])

    def test_not_applicable_shape_without_operator_calls(self):
        other = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Other route 2069', 'slug': 'qa-other-route-2069', 'category': 'ai_llm',
            'helm_repo_url': '', 'helm_chart_name': 'oci://qa/not-odoo-route-2069',
            'helm_chart_version': '0.2.7', 'mcp_enabled': False, 'helm_default_values': '{}'})
        service = self._make_service(template=other, tag='route-other')
        operator = _FakeOperator()
        result = self._check({'revision': 2}, operator, service=service)
        self._assert_contract(result)
        self.assertEqual(result, {'success': True, 'code': 'no_removal', 'applicable': False,
                                  'removed': [], 'modules': {}, 'blocking': {}})
        self.assertEqual(operator.calls, [], '不適用的服務不打任何 operator 呼叫')

    def test_check_failed_shape(self):
        operator = _FakeOperator(module_states={})
        operator.module_state_error = PaaSOperatorAPIError('down', status_code=500)
        result = self._check({'revision': 2}, operator)
        self._assert_contract(result)
        self.assertEqual(result['code'], 'check_failed')
        self.assertEqual(result['removed'], ['qa_sale_2069'])
        self.assertEqual((result['modules'], result['blocking']), ({}, {}))

    def test_unreadable_projection_is_check_failed_without_removed(self):
        operator = _FakeOperator(module_states={})
        operator.projection_failures[3] = PaaSOperatorAPIError('boom', status_code=503)
        result = self._check({'revision': 2}, operator)
        self._assert_contract(result)
        self.assertEqual(result['code'], 'check_failed')
        self.assertNotIn('removed', result, '算不出來就不帶 removed')

    def test_operator_not_configured_is_check_failed(self):
        result = self._check({'revision': 2}, None)
        self._assert_contract(result)
        self.assertEqual(result['code'], 'check_failed')
        self.assertIn('not configured', result['error'])

    def test_bad_or_missing_revision(self):
        operator = _FakeOperator()
        for params in ({}, {'revision': None}, {'revision': 0}, {'revision': -1},
                       {'revision': 'abc'}, {'revision': [2]}):
            with self.subTest(params=params):
                result = self._check(params, operator)
                self._assert_contract(result)
                self.assertEqual(result['code'], 'check_failed')
                self.assertIn('whole number', result['error'])
        self.assertEqual(operator.calls, [])

    def test_revision_not_in_history(self):
        operator = _FakeOperator()
        result = self._check({'revision': 9999}, operator)
        self._assert_contract(result)
        self.assertEqual(result['code'], 'check_failed')
        self.assertIn('9999', result['error'])
        self.assertIn('was not found', result['error'], '要說清楚是 revision 不存在，不是查詢失敗')
        self.assertEqual(operator.projections(), [], '不存在的 revision 不必再讀 projection')
        self.assertEqual(operator.state_queries(), [])

    def test_extra_params_do_not_break_dispatch(self):
        operator = _FakeOperator(module_states={'qa_sale_2069': {'state': 'uninstalled'}})
        result = self._check({'revision': 2, 'noise_2069': True}, operator)
        self.assertEqual(result['code'], 'safe', result)

    def test_rollback_api_refusal_for_installed_modules_over_http(self):
        """回復 API 真的走 HTTP：data 帶 code／modules／blocking（可序列化），確認旗標對它無效。"""
        url = '/api/workspaces/%s/services/%s/rollback' % (self.ws.id, self.service.id)
        operator = _FakeOperator(module_states={
            'qa_sale_2069': {'state': 'installed', 'dependents': ['qa_report_2069']}})
        for params in ({'revision': 2}, {'revision': 2, 'acknowledge_unverified_addons': True}):
            with self.subTest(params=params):
                with patch(_CLIENT_FACTORY, return_value=operator):
                    refused = self._rpc(url, params)
                self.assertFalse(refused.get('success'), refused)
                self.assertEqual(refused['data'], {
                    'code': 'rollback_removes_installed_addons',
                    'modules': {'qa_sale_2069': {'state': 'installed',
                                                 'dependents': ['qa_report_2069'],
                                                 'addon_id': self.sale_addon.id}},
                    'blocking': {'qa_sale_2069': 'installed'}})
                self.assertIn('qa_sale_2069', refused['error'])
        operator.rollback_release.assert_not_called()
        self.assertEqual(len(operator.state_queries()), 2, '兩次請求都真的查了模組狀態')

    def test_route_is_read_only(self):
        operator = _FakeOperator(module_states={'qa_sale_2069': {'state': 'installed'}})
        service = self.service.sudo()
        before = (service.state, service.helm_revision, service.last_upgraded_at,
                  service.rollback_config_sync, tuple(service.odoo_addon_ids.ids))
        self._check({'revision': 2}, operator)
        service.invalidate_recordset()
        self.assertEqual((service.state, service.helm_revision, service.last_upgraded_at,
                          service.rollback_config_sync, tuple(service.odoo_addon_ids.ids)), before)
        operator.rollback_release.assert_not_called()

    def test_permission_matches_the_rollback_action(self):
        """與回復本身同一道：成員＋owner／admin；user／guest 權限不足，非成員看不到服務。"""
        operator = _FakeOperator(module_states={'qa_sale_2069': {'state': 'uninstalled'}})
        for login, role, expected in (('rb2069-user', ROLE_USER, 'Permission denied'),
                                      ('rb2069-guest', ROLE_GUEST, 'Permission denied'),
                                      ('rb2069-outsider', None, 'Service not found')):
            with self.subTest(role=role):
                self._member(login, role)
                self.authenticate(login, login + '-pw-2069')
                result = self._check({'revision': 2}, operator)
                self._assert_contract(result)
                self.assertFalse(result['success'])
                self.assertEqual(result['error'], expected)
                self.assertEqual(result['code'], 'check_failed')
        self.assertEqual(operator.calls, [], '權限不足不得打 operator')

        self._member('rb2069-admin', ROLE_ADMIN)
        self.authenticate('rb2069-admin', 'rb2069-admin-pw-2069')
        result = self._check({'revision': 2}, operator)
        self.assertEqual(result['code'], 'safe', result)


@tagged('post_install', '-at_install')
class TestCheckRollbackAddonsRouteNotRunning2069(_RouteCase):
    service_state = 'error'

    def test_unverified_shape(self):
        operator = _FakeOperator(module_states={})
        result = self._check({'revision': 2}, operator)
        self._assert_contract(result)
        self.assertEqual(result['code'], 'unverified')
        self.assertEqual(result['removed'], ['qa_sale_2069'])
        self.assertIn('not running', result['error'])
        self.assertEqual(operator.state_queries(), [])

    def test_no_removal_while_not_running(self):
        operator = _FakeOperator(selections={3: ['qa_base_2069'], 2: ['qa_base_2069']})
        result = self._check({'revision': 2}, operator)
        self.assertEqual(result, {'success': True, 'code': 'no_removal', 'applicable': True,
                                  'removed': [], 'modules': {}, 'blocking': {}})

    def test_rollback_api_acknowledge_plumbing(self):
        """回復 API：預設拒絕（data.code）；只有 JSON true 才算確認，字串 "true" 不算。"""
        url = '/api/workspaces/%s/services/%s/rollback' % (self.ws.id, self.service.id)
        for params in ({'revision': 2}, {'revision': 2, 'acknowledge_unverified_addons': 'true'},
                       {'revision': 2, 'acknowledge_unverified_addons': 1}):
            with self.subTest(params=params):
                operator = _FakeOperator(module_states={})
                with patch(_CLIENT_FACTORY, return_value=operator):
                    refused = self._rpc(url, params)
                self.assertFalse(refused.get('success'), refused)
                self.assertEqual(refused['data'], {'code': 'rollback_addon_unverified'})
                operator.rollback_release.assert_not_called()

        operator = _FakeOperator(module_states={})
        with patch(_CLIENT_FACTORY, return_value=operator):
            accepted = self._rpc(url, {'revision': 2, 'acknowledge_unverified_addons': True})
        self.assertTrue(accepted.get('success'), accepted)
        operator.rollback_release.assert_called_once()

    def test_rollback_api_acknowledge_must_be_exactly_true(self):
        """只有 JSON true 算確認：null、"True"、"true"、"1"、1、1.0、0、陣列、物件都等於沒確認。"""
        url = '/api/workspaces/%s/services/%s/rollback' % (self.ws.id, self.service.id)
        for value in (None, 'True', 'true', '1', 1, 1.0, 0, [True], {'ok': True}):
            with self.subTest(value=value):
                operator = _FakeOperator(module_states={})
                with patch(_CLIENT_FACTORY, return_value=operator):
                    refused = self._rpc(url, {'revision': 2, 'acknowledge_unverified_addons': value})
                self.assertFalse(refused.get('success'), refused)
                self.assertEqual(refused['data'], {'code': 'rollback_addon_unverified'})
                operator.rollback_release.assert_not_called()


@tagged('post_install', '-at_install')
class TestCheckRollbackAddonsRouteGates2069(_RouteCase):
    """預檢端點先過與回復相同的前置閘，任一道擋下都是 check_failed（不是 unverified）。

    沒有這些閘時，例如沒有 operator client，error 狀態的服務預檢回 unverified（對話框請租戶勾選確認），
    回復本身卻回「PaaS Operator not configured」——兩邊都擋，但確認框是白問。這裡每一道閘都拿預檢與
    回復 API（帶 ``acknowledge_unverified_addons: true``）的回應對照：同一句訊息、都不打 operator。
    """

    def _other_service(self, state):
        other = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Other gate 2069 %s' % state, 'slug': 'qa-other-gate-2069-%s' % state,
            'category': 'ai_llm', 'helm_repo_url': '',
            'helm_chart_name': 'oci://qa/not-odoo-gate-2069', 'helm_chart_version': '0.2.7',
            'mcp_enabled': False, 'helm_default_values': '{}'})
        return self._make_service(template=other, state=state, tag='gate-other-%s' % state)

    def _assert_gate(self, service, operator, expected_error=None):
        """預檢 check_failed、與回復 API 同一句拒絕訊息，兩邊都沒有任何 operator 呼叫。

        ``expected_error`` 為 ``None`` ⇒ 只要求兩邊的訊息相同（訊息本身由別的測試釘）。回傳該訊息。
        """
        advisory = self._check({'revision': 2}, operator, service=service)
        self._assert_contract(advisory)
        self.assertEqual(advisory['code'], 'check_failed', advisory)
        self.assertTrue(advisory['applicable'])
        if expected_error is not None:
            self.assertEqual(advisory['error'], expected_error)
        authoritative = self._rollback_rpc(
            {'revision': 2, 'acknowledge_unverified_addons': True}, operator, service=service)
        self.assertFalse(authoritative.get('success'), authoritative)
        self.assertEqual(authoritative['error'], advisory['error'], '預檢與回復要說同一個理由')
        if operator is not None:
            self.assertEqual(operator.calls, [], '前置閘擋下就不得打 operator')
            operator.rollback_release.assert_not_called()
        return advisory['error']

    def test_state_gate_matches_the_rollback(self):
        for state in ('pending', 'deleting', 'suspended'):
            with self.subTest(state=state):
                service = self._make_service(state=state, tag='gate-%s' % state)
                self._assert_gate(service, _FakeOperator(module_states={}),
                                  'Cannot rollback service in %s state' % state)

    def test_init_lease_gate_matches_the_rollback(self):
        Service = type(self.env['woow_paas_platform.cloud_service'])
        for state in ('running', 'error'):
            with self.subTest(state=state):
                service = self._make_service(state=state, tag='gate-lease-%s' % state)
                with patch.object(Service, '_init_lease_active', return_value=True):
                    error = self._assert_gate(service, _FakeOperator(module_states={}))
                self.assertNotIn('not running', error)
                self.assertNotIn('not configured', error)

    def test_no_operator_client_is_check_failed_in_every_state(self):
        for state in ('running', 'error', 'deploying', 'initializing', 'upgrading'):
            with self.subTest(state=state):
                service = self._make_service(state=state, tag='gate-noop-%s' % state)
                self._assert_gate(service, None, 'PaaS Operator not configured')

    def test_unreadable_history_is_check_failed_in_every_state(self):
        def raising(exc):
            def setup(op):
                op.history_error = exc
            return setup

        def answering(response):
            def setup(op):
                op.history_response = response
            return setup

        cases = (
            ('api error', raising(PaaSOperatorAPIError('boom', status_code=500))),
            ('not found', raising(PaaSOperatorAPIError('gone', status_code=404))),
            ('unreachable', raising(PaaSOperatorConnectionError('unreachable'))),
            ('empty', answering({'revisions': []})),
            ('no key', answering({})),
            ('not a dict', answering('garbage')),
            ('no usable number', answering({'revisions': [{'revision': None, 'status': 'deployed'},
                                                          {'status': 'failed'}]})),
        )
        for state in ('running', 'error'):
            service = self._make_service(state=state, tag='gate-hist-%s' % state)
            for label, setup in cases:
                with self.subTest(state=state, case=label):
                    operator = _FakeOperator(module_states={})
                    setup(operator)
                    result = self._check({'revision': 2}, operator, service=service)
                    self._assert_contract(result)
                    self.assertEqual(result['code'], 'check_failed', result)
                    self.assertIn('version history', result['error'])
                    self.assertNotIn('removed', result)
                    self.assertEqual(operator.calls, [('get_revisions',)], '歷史讀不到就不再讀別的')

    def test_not_applicable_services_skip_the_gates(self):
        """不是 woow-odoo 租戶：照舊直接 no_removal、零 operator 呼叫——前置閘不適用於它們。"""
        Service = type(self.env['woow_paas_platform.cloud_service'])
        for state in ('running', 'suspended', 'error'):
            service = self._other_service(state)
            for label, operator, lease in (('operator', _FakeOperator(), False),
                                           ('no client', None, False),
                                           ('lease', _FakeOperator(), True)):
                with self.subTest(state=state, case=label), \
                        patch.object(Service, '_init_lease_active', return_value=lease):
                    result = self._check({'revision': 2}, operator, service=service)
                    self.assertEqual(result, {'success': True, 'code': 'no_removal', 'applicable': False,
                                              'removed': [], 'modules': {}, 'blocking': {}})
                    if operator is not None:
                        self.assertEqual(operator.calls, [])

    # ---- 讀到歷史之後的前置閘（與回復共用 `_rollback_history_refusal`）------------
    #
    # 這些閘要先讀歷史，operator 一定有 get_revisions 那一次；之後兩邊都不得再打 operator。

    def _assert_history_gate(self, service, history, revision, expected_error):
        advisory = self._check({'revision': revision}, _FakeOperator(history=history, module_states={}),
                               service=service)
        self._assert_contract(advisory)
        self.assertEqual(advisory['code'], 'check_failed', advisory)
        self.assertNotIn('removed', advisory)
        self.assertEqual(advisory['error'], expected_error)
        operator = _FakeOperator(history=history, module_states={})
        authoritative = self._rollback_rpc(
            {'revision': revision, 'acknowledge_unverified_addons': True}, operator, service=service)
        self.assertFalse(authoritative.get('success'), authoritative)
        self.assertEqual(authoritative['error'], expected_error, '預檢與回復要說同一個理由')
        self.assertEqual(operator.calls, [('get_revisions',)], '前置閘擋下就不得再讀 projection')
        operator.rollback_release.assert_not_called()
        return authoritative

    def test_already_current_is_check_failed_in_every_state(self):
        for state in ('running', 'error'):
            with self.subTest(state=state):
                service = self._make_service(state=state, tag='gate-cur-%s' % state)
                authoritative = self._assert_history_gate(
                    service, HISTORY, 3,
                    'Revision 3 is the version currently deployed; there is nothing to roll back.')
                self.assertEqual(authoritative['data'], {'code': 'already_current'})

    def test_revision_not_in_history_is_check_failed_with_the_rollback_wording(self):
        service = self._make_service(state='error', tag='gate-nf')
        authoritative = self._assert_history_gate(
            service, HISTORY, 9,
            "Revision 9 was not found in this service's version history. "
            "Refresh the list and choose another revision.")
        self.assertEqual(authoritative['data'], {'code': 'revision_not_found'})

    def test_chart_floor_violation_is_check_failed_in_every_state(self):
        """chart 降版的目標：預檢不得回 unverified 邀租戶勾確認，回復本身會以 chart 版本下限拒絕。"""
        history = [_rev(1, 'superseded', '0.2.6'), _rev(2, 'superseded', '0.2.6'), _rev(3, 'deployed')]
        for state in ('running', 'error'):
            with self.subTest(state=state):
                service = self._make_service(state=state, tag='gate-floor-%s' % state)
                authoritative = self._assert_history_gate(service, history, 2, (
                    'Revision 2 uses chart version 0.2.6, which is older than the version this '
                    'service runs now (0.2.7). Rolling back to an older chart version is not allowed '
                    'because it would undo platform security fixes. Contact support if you need an '
                    'older version.'))
                self.assertNotIn('data', authoritative)

    def test_empty_history_is_check_failed_for_the_rollback_too(self):
        """歷史空的：預檢早就回 check_failed；回復本身（帶確認）也要以同一句拒絕，不得送到 operator。"""
        for state in ('running', 'error'):
            with self.subTest(state=state):
                service = self._make_service(state=state, tag='gate-empty-%s' % state)
                authoritative = self._assert_history_gate(service, [], 2, (
                    'The version history of this service could not be read, so it cannot be checked '
                    'which modules rolling back to revision 2 would remove. Please try again later.'))
                self.assertEqual(authoritative['data'], {'code': 'rollback_addon_check_failed'})

    def test_unreadable_template_settings_are_check_failed_like_the_rollback(self):
        """範本 helm_value_specs 讀不懂時回復本身以 UserError 拒絕；預檢同一道閘、同一句訊息，不讀歷史。"""
        Service = type(self.env['woow_paas_platform.cloud_service'])
        message = 'qa-2069 settings definition unreadable'
        for state in ('running', 'error'):
            with self.subTest(state=state):
                service = self._make_service(state=state, tag='gate-specs-%s' % state)
                with patch.object(Service, '_rollback_preserved_value_paths',
                                  side_effect=UserError(message)):
                    self._assert_gate(service, _FakeOperator(module_states={}), message)

    def test_history_without_deployed_row_is_counted_by_the_advisory_too(self):
        """純 helm rollback 失敗一次／兩次之後（沒有 deployed）：預檢也把仍在跑的 rev2 算進來；
        error 狀態的 unverified 帶確認送出，回復本身真的走到 rollback_release。"""
        for label, history in (('once', FAILED_ROLLBACK_HISTORY), ('twice', TWICE_FAILED_ROLLBACK_HISTORY)):
            for state, code in (('running', 'blocked'), ('error', 'unverified')):
                with self.subTest(failed=label, state=state):
                    service = self._make_service(state=state, tag='gate-nodep-%s-%s' % (label, state))
                    operator = _FakeOperator(history=history, selections=FAILED_ROLLBACK_SELECTIONS,
                                             module_states=INSTALLED_SALE)
                    result = self._check({'revision': 1}, operator, service=service)
                    self._assert_contract(result)
                    self.assertEqual(result['code'], code, result)
                    self.assertEqual(result['removed'], ['qa_sale_2069'])
                    self.assertIn(2, operator.projections(), '仍在跑的 rev2 一定要讀')
                    rollback_operator = _FakeOperator(
                        history=history, selections=FAILED_ROLLBACK_SELECTIONS, module_states=INSTALLED_SALE)
                    rollback = self._rollback_rpc({'revision': 1, 'acknowledge_unverified_addons': True},
                                                  rollback_operator, service=service)
                    if code == 'blocked':
                        self.assertEqual(rollback['data']['code'], 'rollback_removes_installed_addons')
                        rollback_operator.rollback_release.assert_not_called()
                    else:
                        self.assertTrue(rollback.get('success'), rollback)
                        rollback_operator.rollback_release.assert_called_once()

    def test_newer_failed_revision_is_counted_by_the_advisory_too(self):
        """比基準新的 failed revision 在預檢端點也算「可能正在跑」（同一支 `_rollback_addon_verdict`）。"""
        for state, code in (('running', 'blocked'), ('error', 'unverified')):
            for target in (3, 2):
                with self.subTest(state=state, target=target):
                    service = self._make_service(state=state, tag='gate-nw-%s-%s' % (state, target))
                    operator = _FakeOperator(
                        history=_history_with_newer('failed'), selections=NEWER_SELECTIONS,
                        module_states={'qa_sale_2069': {'state': 'installed', 'dependents': []}})
                    result = self._check({'revision': target}, operator, service=service)
                    self._assert_contract(result)
                    self.assertEqual(result['code'], code, result)
                    self.assertEqual(result['removed'], ['qa_sale_2069'])


@tagged('post_install', '-at_install')
class TestRollbackAdvisoryParity2069(_RouteCase):
    """預檢端點與回復本身（帶 ``acknowledge_unverified_addons: true``）對同一組輸入的結論一致。

    - 預檢回 ``unverified`` ⇒ 回復不得停在守衛之前的任何一道閘，要一路走到 ``rollback_release``；
      否則對話框請租戶勾確認，送出後才被別的理由擋下。
    - 預檢回 ``check_failed`` ⇒ 回復也拒絕、``error`` 一字不差、不送 operator。
    - ``blocked`` ⇒ 回復以 ``rollback_removes_installed_addons`` 拒絕；``no_removal``／``safe`` ⇒ 回復送出。

    另外以 ``_expected_code`` 釘住每組輸入的預檢結論，免得整張表退化成「兩邊一起錯」。
    """

    HISTORIES = (
        # (label, history, selections, target)
        ('normal', HISTORY, SELECTIONS, 2),
        ('no deployed', FAILED_ROLLBACK_HISTORY, FAILED_ROLLBACK_SELECTIONS, 1),
        ('empty', [], SELECTIONS, 2),
        ('revision not found', HISTORY, SELECTIONS, 9),
        ('already current', HISTORY, SELECTIONS, 3),
        ('chart floor', [_rev(1, 'superseded', '0.2.6'), _rev(2, 'superseded', '0.2.6'),
                         _rev(3, 'deployed')], SELECTIONS, 2),
    )
    CONDITIONS = (
        # (label, state, lease active, operator client)
        ('running', 'running', False, True),
        ('error', 'error', False, True),
        ('upgrading', 'upgrading', False, True),
        ('pending', 'pending', False, True),
        ('suspended', 'suspended', False, True),
        ('lease active', 'error', True, True),
        ('no operator client', 'error', False, False),
    )

    @staticmethod
    def _expected_code(history_label, state, lease, has_client):
        if state in ('pending', 'deleting', 'suspended') or lease or not has_client:
            return 'check_failed'
        if history_label in ('normal', 'no deployed'):
            return 'blocked' if state == 'running' else 'unverified'
        return 'check_failed'

    def test_advisory_and_rollback_agree(self):
        Service = type(self.env['woow_paas_platform.cloud_service'])
        seen = set()
        for h_label, history, selections, target in self.HISTORIES:
            for c_label, state, lease, has_client in self.CONDITIONS:
                with self.subTest(history=h_label, condition=c_label):
                    service = self._make_service(state=state, tag='par-%s-%s' % (
                        h_label.replace(' ', '-'), c_label.replace(' ', '-')))

                    def operator():
                        if not has_client:
                            return None
                        return _FakeOperator(history=history, selections=selections,
                                             module_states=INSTALLED_SALE)

                    advisory_operator, rollback_operator = operator(), operator()
                    with patch.object(Service, '_init_lease_active', return_value=lease):
                        advisory = self._check({'revision': target}, advisory_operator,
                                               service=service)
                        rollback = self._rollback_rpc(
                            {'revision': target, 'acknowledge_unverified_addons': True},
                            rollback_operator, service=service)

                    self._assert_contract(advisory)
                    code = advisory['code']
                    seen.add(code)
                    self.assertEqual(code, self._expected_code(h_label, state, lease, has_client),
                                     advisory)
                    released = (rollback_operator.rollback_release.call_count
                                if rollback_operator is not None else 0)
                    if code in ('unverified', 'no_removal', 'safe'):
                        self.assertTrue(rollback.get('success'), (
                            'advisory %s but the rollback stopped before rollback_release: %s'
                            % (code, rollback)))
                        self.assertEqual(released, 1)
                        self.assertEqual(
                            rollback_operator.rollback_release.call_args.kwargs['revision'], target)
                    elif code == 'blocked':
                        self.assertFalse(rollback.get('success'), rollback)
                        self.assertEqual(rollback['data']['code'], 'rollback_removes_installed_addons')
                        self.assertEqual(released, 0)
                    else:
                        self.assertFalse(rollback.get('success'), rollback)
                        self.assertEqual(rollback['error'], advisory['error'],
                                         'check_failed 的理由要與回復本身一字不差')
                        self.assertEqual(released, 0)
        self.assertEqual(seen, {'blocked', 'unverified', 'check_failed'}, '整張表要涵蓋三種結論')

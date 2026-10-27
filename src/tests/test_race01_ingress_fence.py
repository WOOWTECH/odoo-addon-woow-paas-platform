# -*- coding: utf-8 -*-
"""RACE-01（live 18.0.1.0.263 來源）：延遲的 sibling 發布 vs 已提交的 primary 隔離。

## 這份測試與歷史 `test_race01_dev` 的差別（判準被收緊了）

歷史那份的契約**接受兩種結果**：A 兩方都提交且外部是 503，或 B 停權方因鎖忙
fail-closed、下一輪重試成功。`.251` 的列鎖候選一直走的是 **B**——`_do_suspend` 吃到
`LockNotAvailable` → UserError，停權被推遲；原案要求的「sibling 發布與 primary 隔離
**兩方都提交**，且 primary 維持隔離」一次都沒被證明過。

本檔**只接受 A**，而且把原案的每一條前提都寫成斷言：

1. 兩個**不同**的 PostgreSQL backend PID；
2. 兩邊都是真的 ``REPEATABLE READ``；
3. **兩方都成功提交**——停權方不得有任何錯誤（fail-closed 在這裡是 FAIL，不是 PASS）；
4. 已提交的 primary 狀態是 ``suspended``（隔離成立）；
5. **外部實際生效端**是 ``http_status:503``（隔離沒有被延遲的 live 抹掉）；
6. 時序真的是「延遲的 sibling 發布」——歷史事件裡停權方的 commit **早於** sibling
   最後一次 ``applied``。沒有第 6 條，一次幸運的排序也會綠。

斷言點是外部生效端（``External.targets``），不是 DB 欄位：DB 回捲得掉，Cloudflare
那半回捲不掉。operator 全程是行程內替身（``PaaSOperatorClient._request`` 被換成會
raise 的守衛，禁止任何真 HTTP）；閘門用 ``threading.Event``，不用 sleep。
"""
import os
import threading
import traceback
import uuid
from contextlib import ExitStack
from unittest.mock import patch

import odoo
from odoo import api
from odoo.modules.registry import Registry
from odoo.tests.common import BaseCase, get_db_name, tagged

from odoo.addons.woow_paas_platform.models.smart_home import (
    _internal_identity as _sh_identity,
)
from odoo.addons.woow_paas_platform.models.smart_home_route import (
    _internal_sm_route_identity,
    _sm_skip_access_sync,
)
from odoo.addons.woow_paas_platform.services.paas_operator import (
    PaaSOperatorClient,
)
from .publication_receipt_fixture import (
    access_receipt,
    configure_receipt,
    routes_receipt,
)
from .smart_home_fixture import sh_model

HOME = 'woow_paas_platform.smart_home'
ROUTE = 'woow_paas_platform.smart_home_route'
WS = 'woow_paas_platform.workspace'
CRON = 'woow_paas_platform.access_resync_cron'
CLIENT = 'odoo.addons.woow_paas_platform.models.smart_home.get_paas_operator_client'
PARKED = 'http_status:503'
#: 閘門與 join 的上限。只用來把 harness 卡死與產品缺陷分開，不是產品參數。
HOLD = 30
JOIN = 40


def _deny_http(*args, **kwargs):
    raise AssertionError('RACE-01 fake-only guard blocked real operator HTTP')


class External:
    """外部生效端。``targets`` 只能被 ``configure_tunnel`` 改，DB rollback 倒不回去。"""

    def __init__(self):
        self.actor = threading.local()
        self.lock = threading.Lock()
        self.history = []
        self.targets = {}
        #: 哪個 actor 的 Access 發布外呼要被卡住（＝「延遲的 sibling 發布」）。
        self.hold_access_for = None
        self.access_offered = threading.Event()
        self.resume = threading.Event()
        self.effect_url = os.environ.get('RACE01_EFFECT_URL', '').rstrip('/')
        self.effect_client = (PaaSOperatorClient(self.effect_url, 'batch54')
                              if self.effect_url else None)

    def _ev(self, phase, **kwargs):
        with self.lock:
            self.history.append({
                'seq': len(self.history),
                'actor': getattr(self.actor, 'name', '?'),
                'phase': phase, **kwargs})

    def list_identity_providers(self):
        return {'identity_providers': [
            {'id': 'idp-google', 'name': 'Google', 'type': 'google'},
            {'id': 'idp-otp', 'name': 'OTP', 'type': 'onetimepin'}]}

    def sync_access_app(self, payload):
        """Access 發布外呼。被指定的 actor 卡在這裡＝發布在途中。

        閘門刻意放在**這一支**而不是 `configure_tunnel`：RED 與 GREEN 必須走完全
        相同的編排。修好之後 sibling 最後發布的 target 會變成 503，若閘門綁在
        「offered 一個非 503 的 target」上，GREEN 根本不會觸發閘門，harness 會自己
        超時——那樣的綠不是修補的證據。
        """
        self._ev('access', enabled=payload['enabled'])
        if (self.hold_access_for
                and getattr(self.actor, 'name', None) == self.hold_access_for):
            self.access_offered.set()
            if not self.resume.wait(HOLD):
                raise AssertionError('harness: access gate timed out')
        return access_receipt(
            payload, app_id=payload.get('existing_app_id') or 'app-x')

    def configure_tunnel(self, tunnel_id, *, hostname, service_url,
                         tunnel_name):
        self._ev('offered', target=service_url)
        if self.effect_client:
            # Batch54 deployment-like lane: use the production HTTP client and
            # a separately scheduled effect-end pod.  The actor header is only
            # a harness correlation label; the product request body and
            # receipt stay byte-for-byte on the real configure_tunnel wire.
            self.effect_client._session.headers['X-RACE01-Actor'] = (
                getattr(self.actor, 'name', '?'))
            receipt = self.effect_client.configure_tunnel(
                tunnel_id, hostname=hostname, service_url=service_url,
                tunnel_name=tunnel_name)
            with self.lock:
                self.targets[tunnel_id] = self.remote_target(tunnel_id)
        else:
            with self.lock:
                self.targets[tunnel_id] = service_url
            receipt = configure_receipt(tunnel_id, hostname, service_url)
        self._ev('applied', target=service_url)
        return receipt

    def sync_tunnel_routes(self, tunnel_id, routes, **kwargs):
        return routes_receipt(tunnel_id, routes, **kwargs)

    def control(self, action):
        if not self.effect_url:
            raise AssertionError('RACE01_EFFECT_URL is required for remote lane')
        response = self.effect_client._session.post(
            self.effect_url + '/control/' + action, timeout=10)
        response.raise_for_status()
        return response.json()

    def remote_state(self):
        response = self.effect_client._session.get(
            self.effect_url + '/control/state', timeout=10)
        response.raise_for_status()
        return response.json()

    def remote_target(self, tunnel_id):
        return self.remote_state().get('targets', {}).get(tunnel_id)


@tagged('post_install', '-at_install', '-standard', 'race01_fence')
class TestRace01IngressFence(BaseCase):

    def setUp(self):
        super().setUp()
        self.registry = Registry(get_db_name())
        self.assertFalse(self.registry.in_test_mode(),
                         'harness: needs real committed cursors')
        self.marker = 'r01f-' + uuid.uuid4().hex[:10]
        self.ext = External()
        self.ids = {}
        self.stack = ExitStack()
        self.addCleanup(self._cleanup)
        self.stack.enter_context(patch(CLIENT, return_value=self.ext))
        self.stack.enter_context(patch(
            'odoo.addons.woow_paas_platform.services.paas_operator'
            '.get_paas_operator_client', return_value=self.ext))
        if not self.ext.effect_url:
            self.stack.enter_context(patch.object(
                PaaSOperatorClient, '_request', side_effect=_deny_http))
        self._seed()

    def _env(self, cr):
        cr.execute("SET LOCAL lock_timeout = '3000ms'")
        cr.execute("SET LOCAL statement_timeout = '20000ms'")
        return api.Environment(cr, odoo.SUPERUSER_ID, {})

    def _seed(self):
        """一台 active SH：primary 健康受保護，secondary 是 cron 會選中的 sibling。"""
        with self.registry.cursor() as cr:
            env = self._env(cr)
            env['ir.config_parameter'].sudo().set_param(
                'woow_paas_platform.paas_domain', 'woowtech.io')
            ws = env[WS].create({'name': self.marker})
            home = sh_model(env).create({
                'name': self.marker, 'workspace_id': ws.id,
                'state': 'pending'}).with_env(env)
            home.subdomain = home._generate_subdomain()
            target = 'http://%s.invalid:8123' % self.marker
            _sh_identity(home).write({
                'state': 'active', 'tunnel_id': 'tun-' + self.marker,
                'tunnel_name': 'tun-' + self.marker,
                'original_service_url': target})
            primary = _sm_skip_access_sync(env[ROUTE]).create({
                'smart_home_id': home.id, 'is_primary': True,
                'is_protected': True, 'login_google': True,
                'login_email_otp': False,
                'session_duration': '24h'}).with_env(env)
            _internal_sm_route_identity(primary).access_app_id = (
                'app-' + self.marker)
            secondary = _sm_skip_access_sync(env[ROUTE]).create({
                'smart_home_id': home.id, 'is_primary': False,
                'subdomain_prefix': self.marker + '-b', 'is_protected': True,
                'login_google': True, 'login_email_otp': False,
                'session_duration': '24h'}).with_env(env)
            # 已有 app（否則 sync 只走 park 分支、不會有放寬發布）。
            _internal_sm_route_identity(secondary).access_app_id = (
                'app-' + self.marker + '-b')
            # cron 的選取條件；同時讓 `needs_release` 為真 ⇒ 這一輪會重新發布。
            _internal_sm_route_identity(secondary).access_protection_lost = True
            self.ids.update(ws=ws.id, home=home.id, primary=primary.id,
                            secondary=secondary.id, tunnel=home.tunnel_id,
                            target=target)
            self.ext.targets[home.tunnel_id] = target   # 起始：live（健康）
            env.flush_all()
            cr.commit()

    def _cleanup(self):
        self.ext.resume.set()
        self.stack.close()
        try:
            with self.registry.cursor() as cr:
                for sql in (
                        'DELETE FROM '
                        'woow_paas_platform_smart_home_access_revision '
                        'WHERE smart_home_ref = %s',
                        'DELETE FROM woow_paas_platform_smart_home_route '
                        'WHERE smart_home_id = %s',
                        'DELETE FROM woow_paas_platform_smart_home '
                        'WHERE id = %s'):
                    cr.execute(sql, (self.ids.get('home'),))
                cr.execute(
                    'DELETE FROM woow_paas_platform_workspace WHERE id = %s',
                    (self.ids.get('ws'),))
        except Exception:
            pass

    def _pid(self, cr):
        cr.execute('SELECT pg_backend_pid(), current_setting(%s)',
                   ('transaction_isolation',))
        return cr.fetchone()

    def _sibling(self, box):
        """R：cron 重推 sibling route 的 Access desired state，然後提交。"""
        self.ext.actor.name = 'R'
        try:
            with self.registry.cursor() as cr:
                env = self._env(cr)
                box['R_pid'], box['R_iso'] = self._pid(cr)
                counts = dict(pushed=0, failed=0, gated=0)
                env[CRON]._resync_one(
                    env[ROUTE].browse(self.ids['secondary']), counts)
                box['R_counts'] = counts
                env.flush_all()
                cr.commit()
                self.ext._ev('R-commit')
                box['R_committed'] = True
        except BaseException:
            box['R_error'] = traceback.format_exc()

    def _primary_quarantine(self, box):
        """M：對同一台 SH 停權（503 ＋ state=suspended），然後提交。"""
        self.ext.actor.name = 'M'
        try:
            with self.registry.cursor() as cr:
                env = self._env(cr)
                box['M_pid'], box['M_iso'] = self._pid(cr)
                env[HOME].browse(self.ids['home'])._do_suspend()
                env.flush_all()
                cr.commit()
                self.ext._ev('M-commit')
                box['M_committed'] = True
        except BaseException as exc:
            box['M_error'] = '%s: %s' % (type(exc).__name__, str(exc)[:300])
            box['M_pgcode'] = getattr(exc, 'pgcode', None)

    def _committed(self):
        with self.registry.cursor() as cr:
            env = self._env(cr)
            row = env[ROUTE].browse(self.ids['primary']).read(
                ['is_protected', 'access_publish_pending',
                 'access_app_id'])[0]
            row['home_state'] = env[HOME].browse(self.ids['home']).state
            return row

    def _hist(self):
        return [(h['actor'], h['phase'], h.get('target'))
                for h in self.ext.history]

    def _evidence(self, box):
        import logging
        logging.getLogger(__name__).info('RACE01FENCE EVIDENCE %r', {
            'R_pid': box.get('R_pid'), 'M_pid': box.get('M_pid'),
            'iso': (box.get('R_iso'), box.get('M_iso')),
            'R_committed': box.get('R_committed'),
            'M_committed': box.get('M_committed'),
            'R_error': box.get('R_error'), 'M_error': box.get('M_error'),
            'M_pgcode': box.get('M_pgcode'), 'R_counts': box.get('R_counts'),
            'final_target': self.ext.targets[self.ids['tunnel']],
            'committed_primary': self._committed(),
            'history': self._hist()})

    def _seq_of(self, actor, phase, last=False):
        hits = [h['seq'] for h in self.ext.history
                if h['actor'] == actor and h['phase'] == phase]
        if not hits:
            return None
        return hits[-1] if last else hits[0]

    # ---------------------------------------------------------------- #
    #  原案：兩方都提交，且 primary 維持隔離                            #
    # ---------------------------------------------------------------- #

    def test_RACE01_both_commit_and_primary_stays_quarantined(self):
        self.ext.hold_access_for = 'R'
        box = {}
        sibling = threading.Thread(target=self._sibling, args=(box,), name='R')
        sibling.start()
        try:
            if not self.ext.access_offered.wait(20):
                sibling.join(JOIN)
                self.fail('harness: sibling never reached its publication '
                          'call | box=%r | hist=%r' % (box, self._hist()))
            quarantine = threading.Thread(
                target=self._primary_quarantine, args=(box,), name='M')
            quarantine.start()
            quarantine.join(JOIN)
            self.assertFalse(quarantine.is_alive(),
                             'harness: quarantine thread hung; hist=%s'
                             % self._hist())
            self.ext.resume.set()
            sibling.join(JOIN)
            self.assertFalse(sibling.is_alive(),
                             'harness: sibling thread hung')
        finally:
            self.ext.resume.set()
            sibling.join(JOIN)
        self._evidence(box)
        hist = self._hist()

        # 前提 1-2：真的是兩個不同的 PG backend，兩邊都 REPEATABLE READ。
        self.assertNotEqual(box.get('R_pid'), box.get('M_pid'),
                            'harness: need distinct PG backends')
        self.assertEqual(box.get('R_iso'), 'repeatable read')
        self.assertEqual(box.get('M_iso'), 'repeatable read')

        # 前提 3：兩方都提交。拒絕任何一方都不算修好——尤其停權方 fail-closed
        # （鎖忙／LockNotAvailable）在這裡是 FAIL。
        self.assertNotIn('R_error', box, box.get('R_error'))
        self.assertNotIn(
            'M_error', box,
            'RACE-01 要求 sibling 發布與 primary 隔離兩方都提交；停權方卻失敗了'
            '（拒絕其中一方不是本案的修法）：%s' % box.get('M_error'))
        self.assertTrue(box.get('R_committed'))
        self.assertTrue(box.get('M_committed'))

        # 前提 6：時序真的是「延遲的 sibling 發布」——停權方的 commit 早於
        # sibling 最後一次 applied。
        m_commit = self._seq_of('M', 'M-commit')
        r_applied = self._seq_of('R', 'applied', last=True)
        self.assertIsNotNone(m_commit, 'hist=%s' % hist)
        self.assertIsNotNone(r_applied, 'hist=%s' % hist)
        self.assertLess(
            m_commit, r_applied,
            'harness: sibling 的最後一次發布並沒有落在 primary 隔離提交之後，'
            '這一輪沒有重現原案時序；hist=%s' % hist)

        # 前提 4-5：隔離在帳上成立，而且外部實際生效端沒有被抹掉。
        committed = self._committed()
        self.assertEqual(committed['home_state'], 'suspended',
                         'hist=%s' % hist)
        self.assertEqual(
            self.ext.targets[self.ids['tunnel']], PARKED,
            'RACE-01：DB 已提交 state=suspended，但外部實際生效的是延遲到達的 '
            'live=%r；兩方都提交而 primary 的隔離被抹掉；hist=%s'
            % (self.ext.targets[self.ids['tunnel']], hist))

    def test_RACE01_remote_effect_late_live_apply_then_quarantine_wins(self):
        """真 HTTP effect-end 已收 live、延後 apply 時，停權仍是最後效果。

        與原案卡在 Access 呼叫不同，這條把 R 卡在遠端 ``PUT /config`` 已接收、
        尚未套用的位置。M 必須等 R 的短圍籬離開，然後以另一個 RR backend 推 503
        並提交；兩方都提交後，遠端效果端的精確最後狀態只能是 503。
        """
        if not self.ext.effect_url:
            self.skipTest('RACE01_EFFECT_URL is only set in the Batch54 lane')
        self.ext.hold_access_for = None
        self.ext.control('reset')
        self.ext.control('arm-live')
        box = {}
        sibling = threading.Thread(target=self._sibling, args=(box,), name='R')
        sibling.start()
        try:
            # Poll the remote process, not an in-process Event: this proves the
            # request crossed the pod/network boundary and is waiting there.
            for _ in range(200):
                state = self.ext.remote_state()
                if state.get('held'):
                    break
                threading.Event().wait(0.05)
            else:
                self.fail('remote effect-end never held the live PUT: %r' % state)
            quarantine = threading.Thread(
                target=self._primary_quarantine, args=(box,), name='M')
            quarantine.start()
            # Let M contend while R's remote effect is genuinely in flight.
            threading.Event().wait(0.2)
            self.ext.control('release')
            sibling.join(JOIN)
            quarantine.join(JOIN)
        finally:
            self.ext.control('release')
            sibling.join(JOIN)
            if 'quarantine' in locals():
                quarantine.join(JOIN)
        self.assertFalse(sibling.is_alive(), 'remote sibling thread hung')
        self.assertFalse(quarantine.is_alive(), 'remote quarantine thread hung')
        state = self.ext.remote_state()
        import logging
        logging.getLogger(__name__).info('RACE01REMOTE EVIDENCE %r', {
            'R_pid': box.get('R_pid'), 'M_pid': box.get('M_pid'),
            'iso': (box.get('R_iso'), box.get('M_iso')),
            'R_committed': box.get('R_committed'),
            'M_committed': box.get('M_committed'),
            'R_error': box.get('R_error'), 'M_error': box.get('M_error'),
            'remote': state, 'committed_primary': self._committed()})
        self.assertNotEqual(box.get('R_pid'), box.get('M_pid'))
        self.assertEqual(box.get('R_iso'), 'repeatable read')
        self.assertEqual(box.get('M_iso'), 'repeatable read')
        self.assertNotIn('R_error', box, box.get('R_error'))
        self.assertNotIn('M_error', box, box.get('M_error'))
        self.assertTrue(box.get('R_committed'))
        self.assertTrue(box.get('M_committed'))
        events = state.get('history', [])
        live_apply = [row['seq'] for row in events
                      if row.get('phase') == 'applied'
                      and row.get('target') != PARKED]
        park_apply = [row['seq'] for row in events
                      if row.get('phase') == 'applied'
                      and row.get('target') == PARKED]
        self.assertTrue(live_apply, events)
        self.assertTrue(park_apply, events)
        self.assertLess(live_apply[-1], park_apply[-1], events)
        self.assertEqual(state['targets'][self.ids['tunnel']], PARKED)
        self.assertEqual(self._committed()['home_state'], 'suspended')

    # ---------------------------------------------------------------- #
    #  正控制：序列執行時行為不變（這條紅＝harness／fixture 有問題）      #
    # ---------------------------------------------------------------- #

    def test_RACE01_serial_control_suspend_stays_parked(self):
        self.ext.hold_access_for = None
        box = {}
        self._sibling(box)
        self.assertNotIn('R_error', box, box.get('R_error'))
        self.assertEqual(self.ext.targets[self.ids['tunnel']],
                         self.ids['target'],
                         'sibling 在序列情境下必須真的發布 live；hist=%s'
                         % self._hist())
        self._primary_quarantine(box)
        self.assertNotIn('M_error', box, box.get('M_error'))
        self.assertEqual(self.ext.targets[self.ids['tunnel']], PARKED)
        self.assertEqual(self._committed()['home_state'], 'suspended')

    # ---------------------------------------------------------------- #
    #  相鄰：圍籬不得把正常的放寬發布擋掉                                #
    # ---------------------------------------------------------------- #

    def test_RACE01_fence_does_not_block_healthy_live_publication(self):
        """沒有併發時，放寬發布照常把 live 推出去（圍籬不是永久 park）。"""
        self.ext.hold_access_for = None
        with self.registry.cursor() as cr:
            env = self._env(cr)
            home = env[HOME].browse(self.ids['home'])
            self.ext.targets[self.ids['tunnel']] = PARKED
            home._sync_access_ingress()
            env.flush_all()
            cr.commit()
        self.assertEqual(self.ext.targets[self.ids['tunnel']],
                         self.ids['target'],
                         'hist=%s' % self._hist())

    def test_RACE01_own_uncommitted_insert_still_publishes_live(self):
        """成因 (a)：本交易自己還沒提交的 INSERT 不得被圍籬誤 park。

        列還沒提交 ⇒ 不可能有任何**已提交**的隔離存在 ⇒ 沒有東西需要被保護。
        早一版把「已提交快照裡沒有這一列」一律當成要擋，就是在這一格誤判，連帶
        反轉了 AC-1011 的 `access_publish_pending` 契約。
        """
        self.ext.hold_access_for = None
        marker = self.marker + '-fresh'
        target = 'http://%s.invalid:8123' % marker
        tunnel = 'tun-' + marker
        self.ext.targets[tunnel] = PARKED
        with self.registry.cursor() as cr:
            env = self._env(cr)
            ws = env[WS].create({'name': marker})
            home = sh_model(env).create({
                'name': marker, 'workspace_id': ws.id,
                'state': 'pending'}).with_env(env)
            home.subdomain = home._generate_subdomain()
            _sh_identity(home).write({
                'state': 'active', 'tunnel_id': tunnel,
                'tunnel_name': tunnel, 'original_service_url': target})
            _sm_skip_access_sync(env[ROUTE]).create({
                'smart_home_id': home.id, 'is_primary': True,
                'is_protected': False})
            env.flush_all()
            # 刻意**不** commit：這一列只存在於本交易裡。前提用裸 SQL 驗，不呼叫
            # 修補才有的 helper——否則這條測試在 RED 樹上會是 AttributeError
            # （harness 錯誤），而不是對產品行為的觀察。
            cr.execute(
                'SELECT xmin = pg_current_xact_id_if_assigned()::xid '
                'FROM woow_paas_platform_smart_home WHERE id = %s',
                (home.id,))
            self.assertTrue(cr.fetchone()[0],
                            'harness: 這一列應該是本交易自己未提交的 INSERT')
            home._sync_access_ingress()
            cr.rollback()
        self.assertEqual(
            self.ext.targets[tunnel], target,
            '本交易未提交的新列被圍籬誤 park；hist=%s' % self._hist())

    def test_RACE01_committed_delete_keeps_ingress_parked(self):
        """成因 (b)：別的交易已提交 DELETE ⇒ 不得再把 live 推出去。

        與成因 (a) 在 fresh cursor 上**長得一樣**（都讀不到那一列），差別只有主
        cursor 上的 ``xmin`` 是不是本交易的 xid。這一條就是那個分辨的反向對照。
        """
        self.ext.hold_access_for = None
        tunnel = self.ids['tunnel']
        with self.registry.cursor() as cr:
            env = self._env(cr)
            home = env[HOME].browse(self.ids['home'])
            # 先讓本交易的 REPEATABLE READ 快照成立（列此刻還在）。
            self.assertEqual(home.state, 'active')
            # 前提：這一列不是本交易插入的（裸 SQL，理由同上一條測試）。
            cr.execute(
                'SELECT xmin = pg_current_xact_id_if_assigned()::xid '
                'FROM woow_paas_platform_smart_home WHERE id = %s',
                (self.ids['home'],))
            own = cr.fetchone()
            self.assertIsNotNone(own, 'harness: 本交易的快照應該還看得到這一列')
            self.assertFalse(own[0], 'harness: 這一列不該是本交易插入的')
            # 另一筆交易刪掉它並提交。
            with self.registry.cursor() as other:
                for sql in (
                        'DELETE FROM '
                        'woow_paas_platform_smart_home_access_revision '
                        'WHERE smart_home_ref = %s',
                        'DELETE FROM woow_paas_platform_smart_home_route '
                        'WHERE smart_home_id = %s',
                        'DELETE FROM woow_paas_platform_smart_home '
                        'WHERE id = %s'):
                    other.execute(sql, (self.ids['home'],))
                other.commit()
            self.ext.targets[tunnel] = PARKED
            home._sync_access_ingress()
            cr.rollback()
        self.assertEqual(
            self.ext.targets[tunnel], PARKED,
            '已提交的刪除之後仍把 live 推出去；hist=%s' % self._hist())

    def test_RACE01_committed_quarantine_keeps_ingress_parked(self):
        """已提交的停權狀態下，放寬發布必須自己收斂成 503（圍籬的裁決點）。"""
        self.ext.hold_access_for = None
        with self.registry.cursor() as cr:
            env = self._env(cr)
            env[HOME].browse(self.ids['home'])._do_suspend()
            env.flush_all()
            cr.commit()
        self.assertEqual(self.ext.targets[self.ids['tunnel']], PARKED)
        with self.registry.cursor() as cr:
            env = self._env(cr)
            # 停權中的放寬嘗試（allow_halted=False）：必須維持 503。
            env[HOME].browse(self.ids['home'])._sync_access_ingress()
            env.flush_all()
            cr.commit()
        self.assertEqual(self.ext.targets[self.ids['tunnel']], PARKED,
                         'hist=%s' % self._hist())

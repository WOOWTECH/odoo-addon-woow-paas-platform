# -*- coding: utf-8 -*-
"""RACE-01 獨立覆核（batch46）：ingress 圍籬鎖的**壽命**，不是它的存在。

`test_race01_ingress_fence` 已經證明圍籬會把「延遲的 sibling 發布」擋成 503。本檔
覆核的是**同一把鎖被握多久**——那是上一批的設計說明裡唯一與真 PG 行為不符的斷言：

> 「放寬方只在真的要發布 live 的那一小段取，刻意不包住 `park=True` 的收緊發布與
>  整段 Access 外呼——否則 cron 的長交易會把那把鎖握到最後。」

xact advisory lock **沒有**提早釋放的手段：savepoint 回捲放不掉它，只有該筆交易
commit／rollback 才會（真 PG 實測，見 batch46 的 `probe-xact-lock.json`）。所以
「在方法裡晚一點才取鎖」縮的是**哪些路徑**會取，不是**取了之後握多久**。取在主
cursor 上就等於握到呼叫端整筆交易結束，而 `access_resync_cron.
_cron_resync_access_protection` 一輪最多 200 列／腿、**全程單一交易**、逐列只包
savepoint ⇒ 它推過的每一台都會把圍籬握到整輪結束。

本檔三條測試分工（三條都走真的兩個 PG backend，operator 全程行程內替身）：

1. `..._open_transaction_must_not_refuse_quarantine`（**本批的 RED/GREEN 判準**）
   sibling 的放寬發布**已經做完**、但它的交易還開著（＝cron 還在跑剩下的列）。
   此時對同一台停權**必須成功**。候選 `a419b791` 在這裡失敗：鎖還握著，`_do_suspend`
   只能輪詢到逾時並 raise `AccessPublicationError` ⇒ 停權被推遲到下一個每日 cron。
2. `..._quarantine_in_flight_still_parks_the_sibling_publication`（**安全性對照**）
   提早釋放不得把 RACE-01 放回來：**收緊方正在臨界區內**（已取鎖、503 外呼在途中、
   還沒提交）時，放寬發布必須拿不到鎖而 fail-closed，外部維持 503。兩棵樹都該綠。
3. `..._same_transaction_quarantine_then_loosen_is_not_self_refused`（**不回歸對照**）
   候選把鎖取在主 cursor 上，天生容許「同一筆交易先收緊、之後再放寬發布」（PG 對
   同一筆交易重複取同一把 advisory lock 放行）。改成取在另一條連線上會變成跟自己
   互斥 ⇒ 必然逾時。這一條把那個語意釘住，兩棵樹都該綠。

⚠ seed 與 `_env`／`_cleanup` 刻意**複製**而不是從 `test_race01_ingress_fence` 繼承：
那一檔是本批的**不變對照**（原案六條在候選樹與修補樹上都必須維持 6/6），所以它一個
位元都不能動；而 `BaseCase` 子類會把父類的 `test_*` 一起收走，繼承等於把原案六條
在本類別名下再跑一遍。共用的只有「不碰產品語意」的 operator 替身（`External`）。
"""
import threading
import time
import traceback
import uuid
from contextlib import ExitStack
from unittest.mock import patch

import odoo
from odoo import api
from odoo.modules.registry import Registry
from odoo.tests.common import BaseCase, get_db_name, tagged

from odoo.addons.woow_paas_platform.models.smart_home import (
    _INGRESS_FENCE_LOCK_WAIT_S,
    _internal_identity as _sh_identity,
)
from odoo.addons.woow_paas_platform.models.smart_home_route import (
    _internal_sm_route_identity,
    _sm_skip_access_sync,
)
from odoo.addons.woow_paas_platform.services.paas_operator import (
    PaaSOperatorClient,
)
from .smart_home_fixture import sh_model
from .test_race01_ingress_fence import (
    CLIENT,
    CRON,
    External,
    HOLD,
    HOME,
    JOIN,
    PARKED,
    ROUTE,
    WS,
    _deny_http,
)


class GatedExternal(External):
    """`External` ＋ 一道 `configure_tunnel` 閘門。

    原版只在 `sync_access_app` 有閘門（那是原案要卡的點）。測試 2 要卡的是**收緊方
    的 503 外呼**——`_do_suspend` 在那之前已經取得圍籬鎖、而且還沒提交，正是臨界區
    內的那一刻。
    """

    def __init__(self):
        super().__init__()
        #: 哪個 actor 的 `configure_tunnel` 要被卡住（``None`` ＝不卡）。
        self.hold_configure_for = None
        #: 只卡 target 等於這個值的那一次（``None`` ＝第一次就卡）。
        self.hold_configure_when = None
        self.configure_offered = threading.Event()
        self.configure_resume = threading.Event()

    def configure_tunnel(self, tunnel_id, *, hostname, service_url,
                         tunnel_name):
        if (self.hold_configure_for
                and getattr(self.actor, 'name', None) == self.hold_configure_for
                and (self.hold_configure_when is None
                     or service_url == self.hold_configure_when)):
            self.hold_configure_for = None   # 只卡一次
            self._ev('configure-gate', target=service_url)
            self.configure_offered.set()
            if not self.configure_resume.wait(HOLD):
                raise AssertionError('harness: configure gate timed out')
        return super().configure_tunnel(
            tunnel_id, hostname=hostname, service_url=service_url,
            tunnel_name=tunnel_name)


@tagged('post_install', '-at_install', '-standard', 'race01_fence_scope')
class TestRace01FenceScope(BaseCase):

    def setUp(self):
        super().setUp()
        self.registry = Registry(get_db_name())
        self.assertFalse(self.registry.in_test_mode(),
                         'harness: needs real committed cursors')
        self.marker = 'r01s-' + uuid.uuid4().hex[:10]
        self.ext = GatedExternal()
        self.ids = {}
        self.published = threading.Event()
        self.release = threading.Event()
        self.stack = ExitStack()
        self.addCleanup(self._cleanup)
        self.stack.enter_context(patch(CLIENT, return_value=self.ext))
        self.stack.enter_context(patch(
            'odoo.addons.woow_paas_platform.services.paas_operator'
            '.get_paas_operator_client', return_value=self.ext))
        self.stack.enter_context(patch.object(
            PaaSOperatorClient, '_request', side_effect=_deny_http))
        self._seed()

    def _env(self, cr):
        cr.execute("SET LOCAL lock_timeout = '3000ms'")
        cr.execute("SET LOCAL statement_timeout = '30000ms'")
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
            _internal_sm_route_identity(secondary).access_app_id = (
                'app-' + self.marker + '-b')
            _internal_sm_route_identity(secondary).access_protection_lost = True
            self.ids.update(ws=ws.id, home=home.id, primary=primary.id,
                            secondary=secondary.id, tunnel=home.tunnel_id,
                            target=target)
            self.ext.targets[home.tunnel_id] = target   # 起始：live（健康）
            env.flush_all()
            cr.commit()

    def _cleanup(self):
        self.release.set()
        self.ext.resume.set()
        self.ext.configure_resume.set()
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

    def _hist(self):
        return [(h['actor'], h['phase'], h.get('target'))
                for h in self.ext.history]

    def _seq_of(self, actor, phase, last=False):
        hits = [h['seq'] for h in self.ext.history
                if h['actor'] == actor and h['phase'] == phase]
        if not hits:
            return None
        return hits[-1] if last else hits[0]

    def _committed_home_state(self):
        with self.registry.cursor() as cr:
            env = self._env(cr)
            return env[HOME].browse(self.ids['home']).state

    def _committed_route(self, key):
        with self.registry.cursor() as cr:
            env = self._env(cr)
            return env[ROUTE].browse(self.ids[key]).read(
                ['access_publish_pending', 'access_protection_lost'])[0]

    def _evidence(self, name, box):
        import logging
        logging.getLogger(__name__).info('RACE01SCOPE EVIDENCE %s %r', name, {
            'R_pid': box.get('R_pid'), 'M_pid': box.get('M_pid'),
            'iso': (box.get('R_iso'), box.get('M_iso')),
            'R_committed': box.get('R_committed'),
            'M_committed': box.get('M_committed'),
            'R_error': box.get('R_error'), 'M_error': box.get('M_error'),
            'R_counts': box.get('R_counts'),
            'R_published_target': box.get('R_published_target'),
            'M_seconds': box.get('M_seconds'),
            'fence_wait_budget_s': _INGRESS_FENCE_LOCK_WAIT_S,
            'final_target': self.ext.targets[self.ids['tunnel']],
            'committed_home_state': self._committed_home_state(),
            'committed_secondary': self._committed_route('secondary'),
            'history': self._hist()})

    # ---------------------------------------------------------------- #
    #  R / M 兩個 actor                                                 #
    # ---------------------------------------------------------------- #

    def _sibling_publish_then_hold(self, box):
        """R：resync 一列（含放寬發布）後**不提交**，模擬 cron 還在跑剩下的列。"""
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
                box['R_published_target'] = self.ext.targets[self.ids['tunnel']]
                # 這一列做完了，但交易還開著（cron 一輪一個交易、逐列只包 savepoint）。
                self.published.set()
                if not self.release.wait(HOLD):
                    raise AssertionError('harness: hold gate timed out')
                cr.commit()
                self.ext._ev('R-commit')
                box['R_committed'] = True
        except BaseException:
            box['R_error'] = traceback.format_exc()
        finally:
            self.published.set()

    def _sibling_resync(self, box):
        """R：一次完整的 resync（取不到圍籬時由 `_sync_access_to_operator` 自行處置）。"""
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

    def _quarantine(self, box):
        """M：對同一台 SH 停權（503 ＋ state=suspended），然後提交。"""
        self.ext.actor.name = 'M'
        started = time.monotonic()
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
        finally:
            box['M_seconds'] = time.monotonic() - started

    # ---------------------------------------------------------------- #
    #  1. 本批判準：sibling 的交易還開著，不得拒絕停權                   #
    # ---------------------------------------------------------------- #

    def test_RACE01_sibling_open_transaction_must_not_refuse_quarantine(self):
        box = {}
        sibling = threading.Thread(
            target=self._sibling_publish_then_hold, args=(box,), name='R')
        sibling.start()
        try:
            if not self.published.wait(JOIN):
                self.fail('harness: sibling never finished its publication '
                          '| box=%r | hist=%r' % (box, self._hist()))
            self.assertNotIn('R_error', box, box.get('R_error'))
            quarantine = threading.Thread(
                target=self._quarantine, args=(box,), name='M')
            quarantine.start()
            quarantine.join(JOIN)
            self.assertFalse(quarantine.is_alive(),
                             'harness: quarantine thread hung; hist=%s'
                             % self._hist())
        finally:
            self.release.set()
            sibling.join(JOIN)
        self.assertFalse(sibling.is_alive(), 'harness: sibling thread hung')
        self._evidence('open-transaction', box)
        hist = self._hist()

        # 前提：真的是兩個不同的 PG backend，兩邊都 REPEATABLE READ。
        self.assertNotEqual(box.get('R_pid'), box.get('M_pid'),
                            'harness: need distinct PG backends')
        self.assertEqual(box.get('R_iso'), 'repeatable read')
        self.assertEqual(box.get('M_iso'), 'repeatable read')

        # 前提：R 真的做了一次**放寬**發布（＝它真的進過圍籬並取得鎖）。發布的是
        # 503 的話這一輪根本沒有碰到圍籬，測的就不是本案。
        self.assertEqual(
            box.get('R_published_target'), self.ids['target'],
            'harness: sibling 沒有發布 live origin，這一輪沒有進圍籬；hist=%s'
            % hist)

        # 判準：停權不得被拒。候選樹在這裡紅——圍籬鎖取在 R 的主 cursor 上，
        # xact lock 只有在 R 提交／回捲時才釋放，而 R 的交易還開著。
        self.assertNotIn(
            'M_error', box,
            'sibling 的放寬發布已經結束、只是交易還開著（cron 還在跑剩下的列），'
            '停權卻被圍籬鎖拒絕 ⇒ 停權被推遲到下一個每日 cron。圍籬的臨界區只需要'
            '涵蓋「重讀已提交狀態 → 發布」，不該跟著呼叫端整筆交易：%s'
            % box.get('M_error'))
        self.assertTrue(box.get('M_committed'))

        # 而且不是靠「等到 R 提交才成功」——M 必須在 R 的交易還開著時就提交完成。
        m_commit = self._seq_of('M', 'M-commit')
        r_commit = self._seq_of('R', 'R-commit')
        self.assertIsNotNone(m_commit, 'hist=%s' % hist)
        self.assertIsNotNone(r_commit, 'hist=%s' % hist)
        self.assertLess(
            m_commit, r_commit,
            'harness: 停權是在 sibling 提交之後才完成的，這一輪沒有重現'
            '「交易還開著」的形狀；hist=%s' % hist)
        self.assertLess(
            box.get('M_seconds', 1e9), _INGRESS_FENCE_LOCK_WAIT_S,
            '停權雖然成功，卻是把整個等鎖預算耗完才成功 ⇒ 圍籬仍被握著；'
            'M_seconds=%s budget=%s' % (box.get('M_seconds'),
                                        _INGRESS_FENCE_LOCK_WAIT_S))

        # 隔離在帳上成立，外部實際生效端也是 503。
        self.assertEqual(self._committed_home_state(), 'suspended')
        self.assertEqual(self.ext.targets[self.ids['tunnel']], PARKED,
                         'hist=%s' % hist)
        self.assertTrue(box.get('R_committed'))

    # ---------------------------------------------------------------- #
    #  2. 安全性對照：收緊方在臨界區內時，放寬發布必須 fail-closed       #
    # ---------------------------------------------------------------- #

    def test_RACE01_quarantine_in_flight_still_parks_the_sibling_publication(
            self):
        """提早釋放圍籬不得把 RACE-01 放回來。

        收緊方已取得圍籬鎖、503 外呼在途中、**尚未提交**。此刻放寬發布必須拿不到
        鎖（fail-closed ⇒ `access_publish_pending`），外部維持 503。
        """
        box = {}
        self.ext.hold_configure_for = 'M'
        self.ext.hold_configure_when = PARKED
        quarantine = threading.Thread(
            target=self._quarantine, args=(box,), name='M')
        quarantine.start()
        try:
            if not self.ext.configure_offered.wait(JOIN):
                self.fail('harness: quarantine never reached its 503 call '
                          '| box=%r | hist=%r' % (box, self._hist()))
            sibling = threading.Thread(
                target=self._sibling_resync, args=(box,), name='R')
            sibling.start()
            sibling.join(JOIN)
            self.assertFalse(sibling.is_alive(),
                             'harness: sibling thread hung; hist=%s'
                             % self._hist())
        finally:
            self.ext.configure_resume.set()
            quarantine.join(JOIN)
        self.assertFalse(quarantine.is_alive(),
                         'harness: quarantine thread hung')
        self._evidence('quarantine-in-flight', box)
        hist = self._hist()

        self.assertNotEqual(box.get('R_pid'), box.get('M_pid'),
                            'harness: need distinct PG backends')
        self.assertNotIn('M_error', box, box.get('M_error'))
        self.assertNotIn('R_error', box, box.get('R_error'))
        self.assertTrue(box.get('M_committed'))
        self.assertTrue(box.get('R_committed'))

        # 放寬發布被圍籬拒絕 ⇒ 呼叫端既有處置是標 pending 等下一輪。
        self.assertTrue(
            self._committed_route('secondary')['access_publish_pending'],
            'sibling 的放寬發布沒有被圍籬擋下來（或擋下來卻沒標 pending）；'
            'hist=%s' % hist)
        # 外部實際生效端：隔離沒有被抹掉。
        self.assertEqual(self.ext.targets[self.ids['tunnel']], PARKED,
                         'hist=%s' % hist)
        self.assertEqual(self._committed_home_state(), 'suspended')
        # 真的有一次放寬嘗試發生在收緊方的臨界區內（否則這一輪沒測到東西）。
        self.assertIsNotNone(self._seq_of('M', 'configure-gate'),
                             'harness: 收緊方沒有被卡在 503 外呼；hist=%s' % hist)
        self.assertGreater(
            len([h for h in self.ext.history if h['actor'] == 'R']), 0,
            'harness: sibling 根本沒跑；hist=%s' % hist)

    # ---------------------------------------------------------------- #
    #  3. 不回歸對照：同一筆交易先收緊、之後放寬，不得自己擋自己         #
    # ---------------------------------------------------------------- #

    def test_RACE01_same_transaction_quarantine_then_loosen_is_not_self_refused(
            self):
        """圍籬鎖對**同一筆交易**必須是可重入的。

        候選把鎖取在主 cursor 上，PG 對同一筆交易重複取同一把 advisory lock 放行
        （實測 `A_reentrant=true`），所以這個形狀本來就是通的。把鎖改到另一條連線上
        之後它會變成跟自己互斥 ⇒ 必然耗完預算並 raise。這一條把原語意釘住。
        """
        self.ext.actor.name = 'S'
        started = time.monotonic()
        error = None
        with self.registry.cursor() as cr:
            env = self._env(cr)
            home = env[HOME].browse(self.ids['home'])
            home._do_suspend()          # 主 cursor 取得圍籬鎖，持到本交易結束
            env.flush_all()
            self.assertEqual(self.ext.targets[self.ids['tunnel']], PARKED)
            # ⚠ `_do_suspend` 自己會把 `original_service_url` 改寫成
            # `http://localhost:<ha_port>`，所以放寬發布的預期 target 要從記錄現值
            # 讀，不能拿 seed 時的值來比（實測踩過：差異是
            # `http://localhost:8123` vs seed 的 `http://<marker>.invalid:8123`）。
            expected_live = home.original_service_url
            self.assertNotEqual(expected_live, PARKED)
            try:
                # 同一筆交易的放寬發布（`allow_halted=True`＝`_do_resume` 的形狀）。
                home._sync_access_ingress(allow_halted=True)
            except BaseException as exc:
                error = '%s: %s' % (type(exc).__name__, str(exc)[:300])
            target = self.ext.targets[self.ids['tunnel']]
            cr.rollback()
        elapsed = time.monotonic() - started
        import logging
        logging.getLogger(__name__).info(
            'RACE01SCOPE EVIDENCE reentrant %r',
            {'error': error, 'target': target, 'seconds': elapsed,
             'budget': _INGRESS_FENCE_LOCK_WAIT_S, 'history': self._hist()})
        self.assertIsNone(
            error,
            '同一筆交易已經持有圍籬鎖，之後的放寬發布卻被同一把鎖擋住（自己擋自己）'
            '：%s' % error)
        self.assertNotEqual(
            target, PARKED,
            '同一筆交易的放寬發布沒有把 live origin 推出去（還是 503）；history=%s'
            % self._hist())
        self.assertEqual(
            target, expected_live,
            '放寬發布的 target 不是記錄上的 original_service_url；history=%s'
            % self._hist())
        self.assertLess(
            elapsed, _INGRESS_FENCE_LOCK_WAIT_S,
            '放寬發布耗掉了整個等鎖預算 ⇒ 它在等自己放掉的鎖；seconds=%s' % elapsed)

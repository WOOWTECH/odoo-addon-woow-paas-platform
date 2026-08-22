# -*- coding: utf-8 -*-
"""#785：SH route 的 teardown 捕獲——``None`` 只准有一個語意。

## 守什麼

`smart_home_route._capture_access_teardown()` 原本有**兩個** `return None`：

1. 列上沒有 `access_app_id` 也沒有任何 `cf_policy_id` ——「真的沒東西要拆」；
2. `if not self._access_is_live(forced=True)` ——「有東西要拆，但我判斷現在
   推不動」。

兩者共用同一個回傳值，而兩個消費端（`smart_home_route.unlink()` 與
`smart_home._teardown_routes_access()`）都是 `if not info: continue`、**不計入
`failed`**。`failed` 又是兩個呼叫端唯一的判準——`unlink()` 的 fail-loud（非空才
raise，讓 savepoint 把列留下當重試錨點）與 `_do_cleanup()` 的落 `error` 分支。
於是第 2 格會讓呼叫端判定「這一輪乾淨」繼續往下刪列，結果是 CF 上的 Access app
＋ reusable policy 留著、DB 列沒了、**沒有重試錨點也沒有任何訊號**。

⇒ 「捕獲不到所以沒拆」與「本來就沒東西要拆」在輸出上完全不可區分。本檔把這兩格
釘成可區分的：

| | 回傳 | 訊號 |
| --- | --- | --- |
| 列上沒有 id | `None` | **無**（A1 用 `assertNoLogs` 鎖） |
| 有 id、推得動 | 捕獲成功 | **無**（A3 鎖——這格就是「不該出現」那格） |
| 有 id、推不動 | **捕獲成功** | `WARNING`，且**指名是哪條 route**（A2） |

## 三格判定，不是兩格

A3 存在的唯一理由是：只有「A1 沒有 warning」＋「A2 有 warning」兩格時，一個
**無條件**發 warning 的實作會讓 A2 綠、A1 紅——看起來像「A1 寫錯了」。A3 是
「有東西要拆、而且推得動」那一格，它必須**沒有** warning。三格同時成立，warning
才真的是「推不動」的簽名。

同理 B 系列每一條都先斷言 `client.sync_access_app.called`：這條是**前提檢查**，
不成立時結論是「這一條沒測到任何東西」，不是「通過」。修法沒被執行與修法有效在
`state`／`raises` 上長得一模一樣。

## C 系列：那條沒有任何 gate 的跨方法不變式

issue 本文的「目前為什麼還沒出事」講的是一條**跨方法的順序紀律**：清
`tunnel_id` 之前一定先清掉 `cf_policy_id`（也就是 Access teardown 一定跑在
`_cleanup_tunnel()` 之前）。PR #791 的覆核（留言 7951）確認它**零 gate、零
assert、零測試**。

`test_sh_access_sync_768.py::test_D2` 守的是 `sh.unlink()` 路徑上的**呼叫順序**
（`sync_access_app` 的 index < `delete_tunnel` 的 index）。本檔 C1 守的是同一條
不變式的**DB 狀態版**：在 `delete_tunnel` 真的被呼叫的那一刻，去問資料庫——每一
條 route 的 `access_app_id` 與每一顆 policy 的 `cf_policy_id` 都必須已經是
`NULL`，而 `tunnel_id` 必須**還在**（它是下一步才被清的）。呼叫順序對、但清欄位
的時機錯，D2 照樣綠，C1 會紅。

C1 的探針有它自己的失效模式：`delete_tunnel` 沒被呼叫時它一次都不會執行，而
「沒有觀測到違反」與「觀測到沒有違反」在斷言上不可區分 ⇒ C1 第一條斷言就是
**探針到底跑了沒**（`self.assertTrue(seen, ...)`）。

## ⚠ patch 點：`models.smart_home`，不是 `services.paas_operator`

`models/smart_home.py` 頂部 `from ..services.paas_operator import
get_paas_operator_client` ⇒ 名字在 import 當下就綁進 `models.smart_home` 的
namespace，patch 來源模組動不到它
（`docs/decisions/patterns/unittest-mock-dual-namespace-patch.md`）。
`smart_home_route.py` **自己沒有** import 它——`_push_access_teardown()` 走
`info['smart_home']._get_operator_client()`，所以這一腿只有一個 lookup 點。

## ⚠ `assertLogs` / `assertNoLogs` 的 level 一律傳**數值**

Odoo 的 `odoo/netsvc.py` 用 `logging.addLevelName(logging.RUNBOT, "INFO")` 覆寫
了 stdlib 的名稱表，而 unittest 的 `_log.py` 正是查那張表 ⇒ 傳字串 `'WARNING'`
會被解析成別的門檻，`assertLogs` 恆紅、`assertNoLogs` 恆綠
（repo-lint `assertlogs-numeric-level`）。
"""
import logging
import uuid
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

from ..models.security_access import _skip_route_sync
from ..models.smart_home_route import (
    _internal_sm_route_identity,
    _sm_skip_access_sync,
)
from .smart_home_fixture import sh_model

# #798：`cf_policy_id` 有欄位級寫入守衛（create 壓值／write 拒絕，對 su 也生效）。
# fixture 擺狀態必須經開閘 helper，否則會撞到自己要測的守衛。
from ..models.security_access import (
    _internal_policy_cf_write as _internal_policy_cf,
)

#: 🔴 見檔頭「patch 點」。寫錯這裡不會讓否定性斷言變紅。
MOCK_GET_CLIENT = (
    'odoo.addons.woow_paas_platform.models.smart_home.'
    'get_paas_operator_client'
)

LOG_SH = 'odoo.addons.woow_paas_platform.models.smart_home'
LOG_SH_ROUTE = 'odoo.addons.woow_paas_platform.models.smart_home_route'
LOG_REAP = 'odoo.addons.woow_paas_platform.models.cf_access_policy_reap'

_SH = 'woow_paas_platform.smart_home'
_ROUTE = 'woow_paas_platform.smart_home_route'
_POLICY = 'woow_paas_platform.security_access_access_policy'

#: capture 在「有 id 但推不動」那一格留下的訊號。斷言錨在這個片語上，不是錨在
#: 整句——整句包含 route id 等會變的東西。
#: ⚠ 這個片語刻意**不是**既有兩個 SH teardown warning 的超字串／子字串
#: （`[SH] Access teardown failed sh=…` 與
#: `[SH] Access teardown sh=… left N orphan policy(ies)`），否則 `in` 比對分不開
#: 三者，而「新 marker 是舊 marker 的超字串」正是最自然也最容易假綠的命名衝動。
_NOT_LIVE_MARK = 'is NOT live'


def _sync_result(app_id=None, policy_ids=None, orphaned=None):
    """operator `PUT /api/access/sync` 的**真實**回傳形狀（五個 key 一個不少）。

    🔴 不用裸 `MagicMock`：回存那段會拿到 MagicMock 而不是字串／list，測試綠但
    prod 炸（`orphaned_policy_ids` 尤其——裸 mock 永遠 truthy，會憑空落一筆帳）。
    """
    return {
        'app_id': app_id,
        'policy_ids': list(policy_ids or []),
        'allowed_idps': [],
        'unresolved_idp_types': [],
        'orphaned_policy_ids': list(orphaned or []),
    }


@tagged('post_install', '-at_install')
class _ShTeardownFixture(TransactionCase):
    """一台 SH ＋ 它的 route。``tunnel`` 決定這條 route 推不推得動。"""

    def setUp(self):
        super().setUp()
        self.ICP = self.env['ir.config_parameter'].sudo()
        # 沙盒的 seed 會把 paas_domain 設成 `<slug>.woowtech.io`（#804 家族的
        # 已知假紅）。本檔不斷言任何網址，但 hostname 是 liveness 判準的一格，
        # 顯式擺定值讓「hostname 有值」這個前提與環境無關。
        self.ICP.set_param('woow_paas_platform.paas_domain', 'woowtech.io')
        self.ICP.set_param('woow_paas_platform.deploy_env', 'prod')

        self.SmartHome = sh_model(self.env)
        self.ShRoute = self.env[_ROUTE]
        self.Policy = self.env[_POLICY]
        self.workspace = self.env[
            'woow_paas_platform.workspace'].sudo().create(
                {'name': 'WS 785 %s' % uuid.uuid4().hex[:6]})

    # ------------------------------------------------------------------ #
    #  fixture builders                                                   #
    # ------------------------------------------------------------------ #

    def _smart_home(self, tunnel=True, state='active'):
        """建一台 SH。``tunnel=False`` ⇒ 它底下的 route 全部 **not live**。

        `tunnel_id` 是身分錨點，只能經 `sh_model()` 開的閘在 create 當下落地；
        建完立刻 `.with_env(self.env)` 把閘洗掉，免得 fixture 把受測守衛一起
        關掉（`with_context` 是 merge，`.sudo()` / `.browse()` 都保留它）。

        `subdomain` 走 production 公式現算：primary route 的 `hostname` 由它
        compute，而 hostname 也是 liveness 判準的一格——隨手塞字串會讓「為什麼
        不 live」這件事變得說不清楚。
        """
        vals = {
            'name': 'sh-785-%s' % uuid.uuid4().hex[:6],
            'workspace_id': self.workspace.id,
            'state': state,
        }
        if tunnel:
            vals['tunnel_id'] = 'tun-785-%s' % uuid.uuid4().hex[:8]
        sh = self.SmartHome.create(vals)
        sh.write({'subdomain': sh._generate_subdomain()})
        return sh.with_env(self.env)

    def _route(self, sh, primary=True, **extra):
        """建一條 route。

        🔴 `create()` 回的 recordset **帶著呼叫時的 context** ⇒ 直接回它的話，
        之後每一次 write 都還套著 `sm_skip_access_sync=True`，於是「sync 沒接好」
        與「fixture 把 sync 關掉了」在輸出上長得一模一樣。回傳前顯式關掉。
        """
        vals = {'smart_home_id': sh.id, 'is_primary': primary}
        if not primary:
            vals.setdefault('subdomain_prefix',
                            'r785-%s' % uuid.uuid4().hex[:6])
        vals.update(extra)
        route = _sm_skip_access_sync(self.ShRoute).create(vals)
        return route.with_context(sm_skip_access_sync=False)

    def _app_id(self, route, app_id):
        """擺上「CF 上已經有一個 app」這個前提（身分錨點，走 sentinel）。"""
        _internal_sm_route_identity(route).access_app_id = app_id
        return route

    def _policy(self, route, cf_policy_id=None):
        pol = _skip_route_sync(self.Policy.sudo()).create({
            'smart_home_route_id': route.id,
            'name': 'pol-785-%s' % uuid.uuid4().hex[:6],
            'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                                 'rule_type': 'email_list',
                                 'rule_value': 'a@example.com'})],
        })
        if cf_policy_id:
            _internal_policy_cf(pol).cf_policy_id = cf_policy_id
        return pol.with_context(sa_skip_route_sync=False)

    def _client(self, result=None, sync_side_effect=None):
        client = MagicMock()
        client.sync_access_app.return_value = (
            _sync_result() if result is None else result)
        if sync_side_effect is not None:
            client.sync_access_app.side_effect = sync_side_effect
        client.delete_tunnel.return_value = {}
        return client

    # ------------------------------------------------------------------ #
    #  raw-SQL 觀測（ORM cache 與 savepoint 在刪除路徑上不可信）            #
    # ------------------------------------------------------------------ #

    def _route_row(self, route_id):
        """直接問 DB：這一列現在還在不在交易裡（順便回它的 app id）。

        刻意不用 `browse().exists()`：`unlink()` 之後 recordset 的 `exists()`
        受 ORM cache 與 savepoint `cr.clear()` 影響，而這裡測的正是「已經
        `super().unlink()` 掉、又被 savepoint 救回來」——那恰好是兩者最容易分歧
        的一格。
        """
        self.env.flush_all()
        self.env.cr.execute(
            'SELECT access_app_id FROM %s WHERE id = %%s'
            % self.env[_ROUTE]._table, (route_id,))
        return self.env.cr.fetchone()

    def _snapshot(self, sh_id):
        """此刻 DB 上的三件事：route 的 app id、policy 的 cf id、SH 的 tunnel。"""
        self.env.flush_all()
        cr = self.env.cr
        cr.execute(
            'SELECT id, access_app_id FROM %s WHERE smart_home_id = %%s '
            'ORDER BY id' % self.env[_ROUTE]._table, (sh_id,))
        routes = cr.fetchall()
        route_ids = tuple(r[0] for r in routes) or (0,)
        cr.execute(
            'SELECT id, cf_policy_id FROM %s WHERE smart_home_route_id IN %%s '
            'ORDER BY id' % self.env[_POLICY]._table, (route_ids,))
        policies = cr.fetchall()
        cr.execute('SELECT tunnel_id FROM %s WHERE id = %%s'
                   % self.env[_SH]._table, (sh_id,))
        row = cr.fetchone()
        return {'routes': routes, 'policies': policies,
                'tunnel_id': row and row[0]}


# ====================================================================== #
#  A 系列：capture 的回傳語意（三格必須互相可區分）                       #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestShCaptureNoneSemantics(_ShTeardownFixture):

    def test_A1_none_means_exactly_one_thing_nothing_on_the_row(self):
        """列上沒有任何 id ⇒ 回 `None`，而且**一聲不吭**。

        同一個方法裡帶陽性對照：擺上 app id 之後必須捕獲得到。少了它，「回
        None」可能只是因為 `_capture_access_teardown()` 對所有輸入都回 None，
        那樣本條的 `assertNoLogs` 也會為了錯的理由變綠。
        """
        sh = self._smart_home(tunnel=True)
        route = self._route(sh)
        self.assertFalse(route.access_app_id, '前提：列上不該有 app id')

        with self.assertNoLogs(LOG_SH_ROUTE, level=logging.WARNING):
            self.assertIsNone(
                route._capture_access_teardown(),
                '沒有任何 id 要拆 ⇒ 回 None（這是 None 唯一合法的語意）')

        self._app_id(route, 'app-a1')
        self.assertIsNotNone(
            route._capture_access_teardown(),
            '陽性對照：擺上 app id 之後必須捕獲得到，否則上面那個 None 沒有'
            '鑑別力')

    def test_A2_ids_on_a_not_live_route_are_captured_and_announced(self):
        """🔴 本 issue 的那一格：有 id、推不動 ⇒ **照樣捕獲**，而且出聲指名。

        修法前這裡回 `None`，於是消費端 `continue`、不計入 `failed`，呼叫端的
        fail-loud 判成「這一輪乾淨」——CF 上的 app ＋ policy 留著、列被刪掉、
        沒有重試錨點也沒有訊號。
        """
        sh = self._smart_home(tunnel=False)
        route = self._route(sh)
        self._app_id(route, 'app-a2')
        self._policy(route, cf_policy_id='cf-a2')
        self.assertFalse(
            route._access_is_live(forced=True),
            '前提檢查：這條 route 必須真的推不動，否則本條測到的是另一格')

        with self.assertLogs(LOG_SH_ROUTE, level=logging.WARNING) as captured:
            info = route._capture_access_teardown()

        self.assertIsNotNone(
            info, '有 id 就必須捕獲得到——「推不動」是 operator 那一趟的結果，'
                  '不是這裡預測得出來的事')
        self.assertEqual(info['app_id'], 'app-a2')
        self.assertEqual(info['policy_ids'], ['cf-a2'])
        blob = '\n'.join(captured.output)
        self.assertIn(
            _NOT_LIVE_MARK, blob,
            '推不動的那一格必須留下訊號，否則它與「本來就沒東西要拆」在輸出上'
            '不可區分')
        self.assertIn(
            'route=%s' % route.id, blob,
            '訊號必須指名是**哪一條** route——沒有 id 的 warning 沒有重試錨點'
            '可指')

    def test_A3_a_live_route_is_captured_without_the_anomaly_warning(self):
        """對照組／「這個結果不該出現」那一格：有 id、推得動 ⇒ 捕獲且**不出聲**。

        沒有這一格的話，一個**無條件**發 warning 的實作會讓 A2 綠、A1 紅，而 A1
        紅看起來像是 A1 自己寫錯。三格同時成立，warning 才真的是「推不動」的
        簽名。
        """
        sh = self._smart_home(tunnel=True)
        route = self._route(sh)
        self._app_id(route, 'app-a3')
        self._policy(route, cf_policy_id='cf-a3')
        self.assertTrue(
            route._access_is_live(forced=True),
            '前提檢查：這條 route 必須真的推得動')

        with self.assertNoLogs(LOG_SH_ROUTE, level=logging.WARNING):
            info = route._capture_access_teardown()

        self.assertIsNotNone(
            info, '前提檢查：有東西要拆就必須捕獲得到，否則 assertNoLogs 是'
                  '為了錯的理由綠的')
        self.assertEqual(info['app_id'], 'app-a3')

    def test_A4_the_row_state_gate_is_the_single_decision_point(self):
        """`_has_access_teardown()` ＝「這一列還有沒有 id 要拆」，三腿同名同義。

        判準是**手上有沒有 id**，不是 liveness、也不是 `is_protected`——「未保護
        但仍持有 stored app_id / cf_policy_id」是這支程式自己承認的合法持久狀態
        （`_sync_access_to_operator()` 的 docstring）。這一條把「未保護」與「推
        不動」兩格都釘住：兩者都不得讓判準回 False。
        """
        sh = self._smart_home(tunnel=False)
        route = self._route(sh)
        self.assertFalse(
            route._has_access_teardown(), '乾淨的列：沒有東西要拆')

        self._app_id(route, 'app-a4')
        self.assertFalse(route.is_protected, '前提：這條 route 未保護')
        self.assertFalse(route._access_is_live(forced=True), '前提：推不動')
        self.assertTrue(
            route._has_access_teardown(),
            '未保護 ＋ 推不動，但手上有 app id ⇒ 仍然有東西要拆')

        _internal_sm_route_identity(route).access_app_id = False
        pol = self._policy(route, cf_policy_id='cf-a4')
        self.assertTrue(
            route._has_access_teardown(),
            'app_id 空、但 policy 還帶著 cf id ⇒ 仍然有東西要拆'
            '（SA 三處各自寫判準時漏掉的正是這一格）')

        _internal_policy_cf(pol).cf_policy_id = False
        self.assertFalse(
            route._has_access_teardown(),
            'id 都清光了才算乾淨；否則 `_do_cleanup` 的殘留判定會永遠回 True')


# ====================================================================== #
#  B 系列：呼叫端不再把「推不動」判成「這一輪乾淨」                       #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestShTeardownCallersFailLoud(_ShTeardownFixture):

    @mute_logger(LOG_SH_ROUTE)
    def test_B1_route_unlink_pushes_the_teardown_even_when_not_live(self):
        """推不動的 route 被刪掉時，teardown **必須真的送出去**。

        修法前：capture 回 None ⇒ `continue` ⇒ operator 一次都沒被打，列照樣被
        刪掉，CF 上的 app ＋ policy 變成永久孤兒（連重試錨點都不剩）。
        """
        sh = self._smart_home(tunnel=False)
        route = self._route(sh, primary=False)
        self._app_id(route, 'app-b1')
        self._policy(route, cf_policy_id='cf-b1')
        route_id = route.id
        client = self._client(_sync_result(app_id=None))

        with patch(MOCK_GET_CLIENT, return_value=client):
            route.unlink()

        self.assertTrue(
            client.sync_access_app.called,
            '🔴 推不動不代表拆不掉：operator 的 teardown 分支只吃 id，不碰 '
            'tunnel、不讀 hostname')
        payload = client.sync_access_app.call_args.args[0]
        self.assertFalse(payload['enabled'], 'teardown 要送 enabled=False')
        self.assertEqual(
            payload['existing_app_id'], 'app-b1',
            'id 必須在列消失之前捕獲——拿到 None ＝ 永久孤兒 ＋ 無重試錨點')
        self.assertEqual(payload['existing_policy_ids'], ['cf-b1'])
        self.assertIsNone(
            self._route_row(route_id),
            '拆成功了，列就該真的消失（否則本條與 B2 不可區分）')

    @mute_logger(LOG_SH_ROUTE, LOG_SH, LOG_REAP)
    def test_B2_route_unlink_fails_loud_when_the_push_fails(self):
        """推不動 ＋ 推出去失敗 ⇒ fail-loud，列留下來當重試錨點。

        修法前：整段被 `continue` 吃掉 ⇒ 沒有 raise、列消失、零訊號。
        """
        sh = self._smart_home(tunnel=False)
        route = self._route(sh, primary=False)
        self._app_id(route, 'app-b2')
        self._policy(route, cf_policy_id='cf-b2')
        route_id = route.id
        client = self._client(sync_side_effect=Exception('operator down'))

        with patch(MOCK_GET_CLIENT, return_value=client):
            with self.assertRaises(UserError) as ctx:
                route.unlink()

        self.assertTrue(
            client.sync_access_app.called,
            '前提檢查：teardown 必須真的被推出去過，否則這個 UserError 是別的'
            '守衛丟的')
        self.assertIn(
            'Cloudflare Access teardown failed', str(ctx.exception),
            '擋下來的必須是 Access teardown 那道，不是 primary 不可刪那道')
        self.assertEqual(
            self._route_row(route_id), ('app-b2',),
            '🔴 列與它的 app id 必須被 savepoint 救回來——那是唯一的重試錨點')

    @mute_logger(LOG_SH_ROUTE, LOG_SH)
    def test_B3_do_cleanup_lands_error_instead_of_silently_deleting(self):
        """cron 清理路徑：推不動 ＋ 推失敗 ⇒ 落 `error` 等下一輪，不是 `deleting`。

        `_do_cleanup()` 是訂閱取消後 cron 走的路，**不經過** `unlink()` 的
        fail-loud。修法前 capture 回 None ⇒ `failed_access` 空 ⇒ 直接寫
        `state='deleting'`：一個「這台已經清乾淨了」的假成功訊號，而 cron 之後
        再也不會回來看這筆。
        """
        sh = self._smart_home(tunnel=False)
        route = self._route(sh)
        self._app_id(route, 'app-b3')
        self._policy(route, cf_policy_id='cf-b3')
        client = self._client(sync_side_effect=Exception('operator down'))

        with patch(MOCK_GET_CLIENT, return_value=client):
            sh._do_cleanup()

        self.assertTrue(
            client.sync_access_app.called,
            '🔴 前提檢查：teardown 必須真的被推出去過。這一格不成立時，下面的'
            '結論是「本條沒測到任何東西」，不是「通過」')
        self.assertEqual(
            sh.state, 'error',
            '拆不掉就得留在 error 讓下一輪 cron 重試；寫 deleting ＝ 假成功'
            '訊號，而訂閱已取消、cron 不會再回來')
        self.assertIn(
            'Access teardown failed', sh.error_message or '',
            'error_message 要說得出是哪一段失敗的')
        self.assertEqual(
            self._route_row(route.id), ('app-b3',),
            '列上的 id 必須留著——它是 CF 那組物件唯一的把手')

    @mute_logger(LOG_SH_ROUTE, LOG_SH)
    def test_B4_do_cleanup_still_converges_when_the_push_succeeds(self):
        """對照組：同樣推不動，但 operator 拆成功 ⇒ 照常收斂到 `deleting`。

        沒有這一格，B3 的 `state == 'error'` 可能只是因為「推不動的 route 一律
        擋刪」——那是把無聲漏拆換成無謂擋刪，同樣是錯的修法。本條證明修法擋的
        是**拆失敗**，不是**推不動**。
        """
        sh = self._smart_home(tunnel=False)
        route = self._route(sh)
        self._app_id(route, 'app-b4')
        self._policy(route, cf_policy_id='cf-b4')
        client = self._client(_sync_result(app_id=None))

        with patch(MOCK_GET_CLIENT, return_value=client):
            sh._do_cleanup()

        self.assertTrue(
            client.sync_access_app.called,
            '前提檢查：teardown 必須真的被推出去過')
        self.assertEqual(
            sh.state, 'deleting',
            '拆成功就該收斂——「推不動」本身不得阻擋刪除')
        self.assertEqual(
            self._route_row(route.id), (None,),
            '拆成功之後列上的 app id 要清乾淨，否則下一次 sync 拿死 id 去操作')


# ====================================================================== #
#  C 系列：「清 tunnel_id 之前一定先清 cf_policy_id」——DB 狀態版          #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestShTeardownOrderingInvariant(_ShTeardownFixture):
    """留言 7951：這條不變式在 #785 之前**零 gate、零 assert、零測試**。

    `test_sh_access_sync_768.py::test_D2` 守的是 `sh.unlink()` 上的**呼叫順序**
    （mock call index）。本組守的是同一條不變式的**狀態**版：`delete_tunnel`
    真的被呼叫的那一刻，DB 上的 id 必須已經清乾淨。順序對、清欄位的時機錯，
    D2 綠、本組紅。
    """

    @mute_logger(LOG_SH_ROUTE, LOG_SH)
    def test_C1_cf_ids_are_already_null_when_delete_tunnel_is_called(self):
        sh = self._smart_home(tunnel=True)
        route = self._route(sh)
        self._app_id(route, 'app-c1')
        self._policy(route, cf_policy_id='cf-c1')
        sh_id = sh.id
        tunnel_before = sh.tunnel_id
        self.assertTrue(tunnel_before, '前提：這台 SH 必須有 tunnel')
        self.assertEqual(
            self._route_row(route.id), ('app-c1',),
            '前提：列上必須真的有 id 要拆，否則本條是空轉')

        seen = []
        client = self._client(_sync_result(app_id=None))

        def _probe(*args, **kwargs):
            seen.append(self._snapshot(sh_id))
            return {}

        client.delete_tunnel.side_effect = _probe

        with patch(MOCK_GET_CLIENT, return_value=client):
            sh._do_cleanup()

        # 🔴 探針壞掉時的輸出與「沒有違反」一模一樣 ⇒ 先問探針跑了沒。
        self.assertTrue(
            seen, '探針一次都沒跑到（`delete_tunnel` 沒被呼叫）⇒ 本條什麼都'
                  '沒觀測到，結論是「未驗到」不是「通過」')
        snap = seen[0]
        self.assertTrue(
            snap['routes'], '前提：快照時 route 列必須還在（`_do_cleanup` 不刪列）')
        self.assertEqual(
            [app for _rid, app in snap['routes']], [None] * len(snap['routes']),
            '🔴 `delete_tunnel` 的當下，每一條 route 的 access_app_id 都必須'
            '已經是 NULL——Access 拆在 tunnel 之前，是這條不變式的內容本身')
        self.assertEqual(
            [cf for _pid, cf in snap['policies']],
            [None] * len(snap['policies']),
            '🔴 policy 的 cf_policy_id 同上')
        self.assertEqual(
            snap['tunnel_id'], tunnel_before,
            'tunnel_id 必須在這一刻**還在**——它是下一步才清的。這一條把方向'
            '釘死：不是「兩者都空」而是「id 先空、tunnel 後空」')
        self.assertEqual(sh.state, 'deleting')
        self.assertFalse(sh.tunnel_id, '收尾：tunnel_id 最後才被清掉')

    @mute_logger(LOG_SH_ROUTE, LOG_SH)
    def test_C2_a_failed_access_teardown_never_reaches_delete_tunnel(self):
        """順序的**用途**：Access 拆不掉時，那顆還在計費的 tunnel 不准被碰。

        反過來的話，一顆已經被刪掉的 tunnel 會在後續 raise 時被 savepoint 把
        `tunnel_id` 回捲回來 ⇒ DB 宣稱有一顆 CF 上已不存在的 tunnel，重試只會
        對空氣發 delete。
        """
        sh = self._smart_home(tunnel=True)
        route = self._route(sh)
        self._app_id(route, 'app-c2')
        client = self._client(sync_side_effect=Exception('operator down'))

        with patch(MOCK_GET_CLIENT, return_value=client):
            sh._do_cleanup()

        self.assertTrue(
            client.sync_access_app.called,
            '前提檢查：Access teardown 必須真的被試過')
        self.assertFalse(
            client.delete_tunnel.called,
            '🔴 Access 拆不掉就不准往下刪 tunnel')
        self.assertEqual(sh.state, 'error')
        self.assertTrue(sh.tunnel_id, 'tunnel 留著給下一輪 cron 重試')

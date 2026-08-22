"""#794：Cloudflare Access app 被拆掉之後 DB 回捲 ⇒ 無聲 fail-open。

## 守什麼

**五處**的形狀相同：對 Cloudflare 送出**不可回捲**的 Access teardown，之後任一步
raise ⇒ 整筆交易被丟棄（`odoo/service/model.py::retrying` 只有正常返回才 commit，
而 `UserError` 不在 retry 的例外集合裡）⇒ 已經拆成功的那幾列帶著
`is_protected=True` 與指向死 app 的 `access_app_id` 復活。租戶介面讀的是
`is_protected` 這個**意圖**欄位 ⇒ 顯示「已保護」，而該 hostname 實際上任何人不需
登入即可存取。

五處是：`SecurityAccessRoute.unlink()`（A 系列）、`SmartHomeRoute.unlink()`（B）、
`CloudService.unlink()`（C）、`SmartHome.unlink()`（B3）、以及
**`SecurityAccess.unlink()` / `action_delete()`**（F 系列，PR #802 覆核 [HIGH]
補上）。🔴 第五腿一度被排除，理由是「`_cleanup_cloudflare` 自己不 raise」——那句
話為真，但**驗錯了對象**：造成回捲的 `raise` 在**呼叫端**，且發生在它 partial
success 之後。它也比 A 腿**更直接可達**（單筆刪整個 SA 即可，不需要 multi-id
RPC；租戶從 `/woow` 按刪除走的 `action_delete()` 就是這條）。

本檔守四件事：

1. **補償有跑**：拆成功那幾條的 `access_app_id` 與 policy `cf_policy_id` 被清掉、
   `access_protection_lost` 被打開，而且**是在交易回捲之後仍然存在的寫入**。
2. **不自動降級**：`is_protected` 一個字都不能動（那是租戶設定的意圖，平台改掉
   它是產品可見變更，#794 標為待拍板）。
3. **不誤傷重試錨點**：拆**失敗**那條的列與它的 `access_app_id` 必須原樣留著
   （`test_protected_route_unlink_teardown_failure_rolls_back_keeps_row` 的既有
   契約）。補償只作用在「同批中真的拆成功」那幾條。
4. **補償失敗不得取代原本的錯誤**：呼叫端在 `except` 裡、正準備重拋原錯。

## 🔴 這一檔最容易寫成假綠的四格

* **補償恆執行就會讓 (1) 假綠**：所以每一組都配一條陽性對照（全部成功時補償
  **一次都沒被呼叫**）。
* **「列復活了」不能用 `browse().exists()` 讀**：`unlink()` 之後 recordset 的
  `exists()` 受 ORM cache 與 savepoint `cr.clear()` 影響，而本檔測的正是「已經
  `super().unlink()` 掉、又被 savepoint 救回來」那一格。一律走 raw SQL 問交易。
* **補償寫在另一個 `Environment` 上**：本 env 的 cache 讀不到它。斷言前一律
  `invalidate_all()`，或直接 raw SQL。
* 🔴 **本檔在 `TransactionCase` 下看不到補償的 DB 效果，而且原因與直覺相反**
  ——實測（沙盒 odoo 18.0-20260803）：

      TransactionCase.setUpClass enters test mode: False
      HttpCase enters test mode: True

  Odoo 18 只有 `HttpCase` 會 `registry.enter_test_mode()`；`TransactionCase`
  用的是**真的 `Cursor`**（`odoo/tests/common.py` 的 `setUpClass` 改成把
  `cr.commit/rollback/close` patch 成 `forbidden`，每個 test method 只是一個裸
  `SAVEPOINT`）。所以 `registry.cursor()` 在本檔底下回的是**真的第二條連線**，
  它看不到測試那筆尚未 commit 的 fixture ⇒ `browse(id).exists()` 為空 ⇒
  **補償整支安靜地什麼都不做**。

  ⚠ 這與「TestCursor 假綠」是相反的失效模式：不是「看起來有效其實沒活過
  rollback」，而是「在單元測試裡根本不會發生」。所以本檔**不斷言補償寫下去的
  值**（那樣只會得到一組永遠紅的斷言），改成斷言**交還給補償入口的錨點清單是
  對的**——那是本層唯一觀測得到、而且是實作最容易寫錯的一格。補償的實際效果
  與「活過 rollback」由 `src/tests/integration/test_failopen_real_rollback_794.py`
  （`BaseCase`＋三筆真交易）守。
"""
import logging
import uuid
from unittest.mock import MagicMock, patch

from psycopg2 import OperationalError

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

from ..models.security_access import (
    _internal_identity as _internal_sa, _skip_route_sync,
)
from ..models.smart_home_route import (
    _internal_sm_route_identity, _sm_skip_access_sync,
)
from ..models.cloud_service import (
    _cs_skip_access_sync, _internal_cs_access_identity, _internal_provision,
)
from ..services.naming import make_sm_access_canonical_name  # noqa: F401
from ..services.reconciler import (
    LeftSet, collect_left_set, compute_dangling_protected_apps,
)
from .smart_home_fixture import sh_model

# #798：`cf_policy_id` 有欄位級寫入守衛（create 壓值／write 拒絕，對 su 也生效）。
# fixture 擺狀態必須經開閘 helper，否則會撞到自己要測的守衛。
from ..models.security_access import (
    _internal_policy_cf_write as _internal_policy_cf,
)

#: patch 點的判準是「**那個 model** 怎麼取得 client」，不是「client 定義在哪」。
#: 三腿各自 `from ..services.paas_operator import get_paas_operator_client`，
#: 名字在 import 當下就綁進各自的 namespace 了 ⇒ patch 來源模組動不到它們。
#: 寫錯 patch 點時，「應該打 operator」那些會吵（PaaS Operator is not
#: configured），但「**不該**打 operator」那些會**無聲通過**。
MOCK_SA_CLIENT = ('odoo.addons.woow_paas_platform.models.security_access.'
                  'get_paas_operator_client')
MOCK_SH_CLIENT = ('odoo.addons.woow_paas_platform.models.smart_home.'
                  'get_paas_operator_client')
MOCK_CS_SEAM = ('odoo.addons.woow_paas_platform.models.cloud_service.'
                'CloudService._push_access_teardown')

#: 補償入口本身（陽性對照要斷言它**一次都沒被呼叫**）。
COMPENSATE = ('odoo.addons.woow_paas_platform.models.cf_access_policy_reap.'
              'CfAccessPolicyReap._compensate_fail_open_after_rollback')
#: per-leg 縫（(e) 用來注入補償自己的失敗）。
SA_MARK = ('odoo.addons.woow_paas_platform.models.security_access.'
           'SecurityAccessRoute._access_mark_protection_lost')

LOG_SA = 'odoo.addons.woow_paas_platform.models.security_access'
LOG_SH = 'odoo.addons.woow_paas_platform.models.smart_home'
LOG_SH_ROUTE = 'odoo.addons.woow_paas_platform.models.smart_home_route'
LOG_CS = 'odoo.addons.woow_paas_platform.models.cloud_service'
LOG_REAP = 'odoo.addons.woow_paas_platform.models.cf_access_policy_reap'

_ROUTE = 'woow_paas_platform.security_access_route'
_POLICY = 'woow_paas_platform.security_access_access_policy'
_SH_ROUTE = 'woow_paas_platform.smart_home_route'
_CS = 'woow_paas_platform.cloud_service'


def _sync_result(app_id='app-794', policy_ids=None, orphaned=None):
    """operator `PUT /api/access/sync` 的真實回傳形狀（五個 key 一個都不少）。

    🔴 不用裸 MagicMock：回存那段會拿到 MagicMock 而不是字串／list，測試綠但
    prod 炸（`orphaned_policy_ids` 尤其——裸 mock 永遠 truthy）。
    """
    return {
        'app_id': app_id,
        'policy_ids': list(policy_ids or []),
        'allowed_idps': [],
        'unresolved_idp_types': [],
        'orphaned_policy_ids': list(orphaned or []),
    }


def _teardown_result(orphaned=None):
    """`enabled=False` 那一趟的回傳（operator 把 app_id 收成 None）。"""
    return _sync_result(app_id=None, policy_ids=[], orphaned=orphaned)


# ====================================================================== #
#  A 系列：SA route（#794 開單時查證的那一腿）                            #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestSaRouteFailOpenCompensation(TransactionCase):

    def setUp(self):
        super().setUp()
        self.workspace = self.env['woow_paas_platform.workspace'].sudo(
        ).create({'name': 'WS 794 %s' % uuid.uuid4().hex[:6]})
        self.sa = self.env['woow_paas_platform.security_access'].sudo().create(
            {'name': 'SA 794 %s' % uuid.uuid4().hex[:6],
             'workspace_id': self.workspace.id})
        _internal_sa(self.sa).write(
            {'state': 'active', 'tunnel_id': 'tun-794'})

    # ------------------------------------------------------------------ #
    #  fixture                                                            #
    # ------------------------------------------------------------------ #
    def _route(self, app_id, protected=True, policy_cf_id=None):
        """一條帶著 CF app id 的 route。全程 `_skip_route_sync`，所以 fixture
        自己不會打 operator——被測的動作才會。"""
        route = _skip_route_sync(self.env[_ROUTE].sudo()).create({
            'security_access_id': self.sa.id,
            'subdomain_prefix': 'r%s' % uuid.uuid4().hex[:8],
            'is_protected': protected})
        route = route.with_context(sa_skip_route_sync=False)
        _internal_sa(route).access_app_id = app_id
        if policy_cf_id:
            pol = _skip_route_sync(self.env[_POLICY].sudo()).create({
                'route_id': route.id,
                'name': 'pol-%s' % uuid.uuid4().hex[:6],
                'decision': 'allow',
                'rule_ids': [(0, 0, {'group': 'include',
                                     'rule_type': 'email_list',
                                     'rule_value': 'a@example.com'})]})
            _internal_policy_cf(pol).cf_policy_id = policy_cf_id
        return route

    def _row(self, route_id):
        """raw SQL 問交易：這一列現在還在不在，以及它的三個關鍵值。

        ⚠ 刻意不用 `browse().exists()`：`unlink()` 之後 recordset 的 `exists()`
        受 ORM cache 與 savepoint `cr.clear()` 影響，而本檔測的正是「已經
        `super().unlink()` 掉、又被 savepoint 救回來」那一格。而且補償寫在另一個
        `Environment` 上，本 env 的 cache 天生看不到它。
        """
        self.env.flush_all()
        self.env.cr.execute(
            'SELECT access_app_id, is_protected, access_protection_lost '
            'FROM woow_paas_platform_security_access_route WHERE id = %s',
            (route_id,))
        return self.env.cr.fetchone()

    def _policy_cf(self, route_id):
        self.env.flush_all()
        self.env.cr.execute(
            'SELECT cf_policy_id FROM '
            'woow_paas_platform_security_access_access_policy '
            'WHERE route_id = %s ORDER BY id', (route_id,))
        return [r[0] for r in self.env.cr.fetchall()]

    def _client(self, side_effect):
        client = MagicMock()
        client.sync_access_app.side_effect = side_effect
        client.sync_tunnel_routes.return_value = {'routes': []}
        return client

    @staticmethod
    def _sent(client):
        return [c.args[0] for c in client.sync_access_app.call_args_list]

    # ------------------------------------------------------------------ #
    #  A1 ＝ 工單 (a) ＋ (d)                                              #
    # ------------------------------------------------------------------ #
    @mute_logger(LOG_SA, LOG_REAP)
    def test_A1_only_really_torn_down_rows_are_handed_to_the_compensation(self):
        """批次刪除、p1 拆成功 p2 拆失敗 ⇒ 補償只拿到 p1，且 p2 一個字沒被動。

        🔴 **前提斷言先行**（工單 (d)）：少了它，「補償只拿到 p1」在「p1 根本
        沒被拆」的情況下也會綠——而那正好是相反的世界。

        ⚠ 斷言的是**交還給補償入口的清單**而不是補償寫下去的值：見檔頭，本層
        結構上看不到那個寫入（補償跑在真的第二條連線上，看不到本測試尚未
        commit 的 fixture）。實際寫入由 integration 探針守。
        """
        p1 = self._route('app-p1', policy_cf_id='cf-p1')
        p2 = self._route('app-p2')
        p1_id, p2_id = p1.id, p2.id

        def _side_effect(payload):
            if payload.get('enabled'):
                raise Exception('cf access still down')
            if payload.get('existing_app_id') == 'app-p1':
                return _teardown_result()
            raise Exception('cf access down')

        client = self._client(_side_effect)
        with patch(MOCK_SA_CLIENT, return_value=client):
            with patch(COMPENSATE, autospec=True) as comp:
                with self.assertRaises(UserError):
                    (p1 | p2).unlink()

        sent = self._sent(client)
        # (d) 前提：p1 的 teardown 真的送出去過，而且帶的是它自己的 app id。
        self.assertTrue(
            any(c.get('enabled') is False
                and c.get('existing_app_id') == 'app-p1' for c in sent),
            '前提檢查：p1 的 teardown 必須真的被送出去過，否則本測試沒有鑑別力')

        comp.assert_called_once()
        # args[0] 是被 `@api.model` 綁進來的 recordset，args[1] 才是錨點清單。
        self.assertEqual(
            comp.call_args.args[1], [(_ROUTE, p1_id)],
            '只有「真的對 CF 拆掉了」那一條該進補償清單：p2 拆失敗，它的列與 '
            'access_app_id 是真的重試錨點，動它會踩掉 '
            'test_protected_route_unlink_teardown_failure_rolls_back_keeps_row')

        # 🔴 不誤傷重試錨點（這一格本層看得到——補償被 patch 掉了，但即使沒被
        # patch，它也只會動 p1）。
        app_id2, protected2, lost2 = self._row(p2_id)
        self.assertEqual(app_id2, 'app-p2')
        self.assertTrue(protected2)
        self.assertFalse(lost2)
        # 兩列都被 savepoint 救回來了（＝本 issue 的形狀本身）。
        self.assertIsNotNone(self._row(p1_id))

    # ------------------------------------------------------------------ #
    #  A2 ＝ 工單 (b)：陽性對照                                            #
    # ------------------------------------------------------------------ #
    def test_A2_positive_control_clean_batch_never_compensates(self):
        """兩條都拆得掉 ⇒ 兩列都消失，補償路徑**一次都沒被呼叫**。

        沒有這一條，A1 可能只是因為補償恆執行而綠。
        """
        p1 = self._route('app-ok-1')
        p2 = self._route('app-ok-2')
        p1_id, p2_id = p1.id, p2.id
        client = self._client(lambda payload: _teardown_result())
        with patch(MOCK_SA_CLIENT, return_value=client):
            with patch(COMPENSATE, autospec=True) as comp:
                (p1 | p2).unlink()
        self.assertEqual(
            comp.call_count, 0,
            '沒有任何失敗時補償不得被呼叫——補償恆執行會讓 A1 假綠')
        self.assertIsNone(self._row(p1_id))
        self.assertIsNone(self._row(p2_id))

    # ------------------------------------------------------------------ #
    #  A3 ＝ 工單 (c)：清掉 id 之後推的是「建立」語意                       #
    # ------------------------------------------------------------------ #
    def test_A3_clearing_the_id_makes_the_next_sync_a_create(self):
        """清掉死 id 的意義：下一次 sync 的 payload 是**建立**語意。

        `existing_app_id` 為 None ⇒ operator 走 `POST /apps` 建新 app，而不是
        拿死 id 去 `PUT /apps/<dead>` 吃 404。這是「窗口被關掉」的機制本身。

        ⚠ 這裡直接對 `_access_mark_protection_lost()` 之後的列組 payload，
        **不經過補償的獨立交易**——那一段在本層跑不起來（見檔頭）。補償真的
        重推一次的行為由 integration 探針守。
        """
        route = self._route('app-dead')
        self.assertEqual(route._build_access_payload()['existing_app_id'],
                         'app-dead', '前提：清掉之前 payload 帶的是那顆死 id')
        route._access_mark_protection_lost()
        payload = route._build_access_payload()
        self.assertIsNone(
            payload['existing_app_id'],
            '清掉之後必須是建立語意——留著死 id 只會讓下一次 sync 吃 404')
        self.assertTrue(payload['enabled'],
                        '不得自動降 is_protected ⇒ payload 仍是 enabled=True')

    # ------------------------------------------------------------------ #
    #  A4 ＝ 工單 (e)：補償自己失敗時不得吃掉原本的錯誤                     #
    # ------------------------------------------------------------------ #
    @mute_logger(LOG_SA, LOG_REAP)
    def test_A4_compensation_failure_never_replaces_the_original_error(self):
        """補償整段失敗 ⇒ 外層仍必須拋原本那個 UserError。

        補償的例外若外拋，租戶看到的會是一句與他的動作無關的話，而真正的原因
        （Cloudflare Access teardown failed…）就此消失。

        🔴 注入點選在 `Registry.cursor`（補償**真的會走到**的第一步），不是
        `_access_mark_protection_lost`——後者在本層根本到不了（補償看不到本測試
        尚未 commit 的列，見檔頭），拿它注入的話這條測試會為了錯的理由變綠。
        注入式 RED 驗過：把 helper 的 `except` 改成 `raise` 之後本條確實變紅。
        """
        p1 = self._route('app-p1')
        p2 = self._route('app-p2')

        def _side_effect(payload):
            if payload.get('enabled'):
                raise Exception('cf access still down')
            if payload.get('existing_app_id') == 'app-p1':
                return _teardown_result()
            raise Exception('cf access down')

        client = self._client(_side_effect)
        with patch(MOCK_SA_CLIENT, return_value=client):
            with patch('odoo.modules.registry.Registry.cursor',
                       side_effect=RuntimeError('compensation blew up')):
                with self.assertRaises(UserError) as cm:
                    (p1 | p2).unlink()
        self.assertIn(
            'Cloudflare Access teardown failed', str(cm.exception),
            '外層必須拋原本那個 UserError，不得被補償自己的例外取代')

    # ------------------------------------------------------------------ #
    #  A5：pass 1 short-circuit（受害者從整批降到失敗那條之前）             #
    # ------------------------------------------------------------------ #
    @mute_logger(LOG_SA, LOG_REAP)
    def test_A5_pass1_stops_at_the_first_failure(self):
        """三條 pass-1 route、第二條失敗 ⇒ 第三條**一根手指都沒被碰過**。

        🔴 鑑別力來自「三條」：兩條的話，eager 與 short-circuit 送出去的
        teardown 數量相同（都是 2），測試對這個改動天生盲目。
        """
        p1 = self._route('app-s1')
        p2 = self._route('app-s2')
        p3 = self._route('app-s3')

        def _side_effect(payload):
            if payload.get('enabled'):
                raise Exception('cf access still down')
            if payload.get('existing_app_id') == 'app-s2':
                raise Exception('cf access down')
            return _teardown_result()

        client = self._client(_side_effect)
        with patch(MOCK_SA_CLIENT, return_value=client):
            with patch(COMPENSATE, autospec=True) as comp:
                with self.assertRaises(UserError):
                    (p1 | p2 | p3).unlink()

        sent = self._sent(client)
        teardowns = [c for c in sent if c.get('enabled') is False]
        self.assertEqual(
            [c.get('existing_app_id') for c in teardowns],
            ['app-s1', 'app-s2'],
            'pass 1 必須在第一條失敗處停下——app-s3 的 teardown 是回捲不掉的 '
            'CF 刪除，送出去就多一個受害者')
        self.assertFalse(
            any(c.get('existing_app_id') == 'app-s3' for c in sent),
            'app-s3 不得出現在任何一次 payload 裡')
        # 停在 p2 ⇒ 只有 p1 需要補償；p3 一根手指都沒被碰過。
        comp.assert_called_once()
        self.assertEqual(
            comp.call_args.args[1], [(_ROUTE, p1.id)],
            'short-circuit 的直接後果：受害者只有 p1，不是整批')
        self.assertEqual(self._row(p3.id)[0], 'app-s3')
        self.assertFalse(self._row(p3.id)[2])

    # ------------------------------------------------------------------ #
    #  A6：既有契約回歸（單列失敗 ⇒ 列與 app id 都留著，且不補償）          #
    # ------------------------------------------------------------------ #
    @mute_logger(LOG_SA, LOG_REAP)
    def test_A6_single_route_failure_keeps_the_retry_anchor_untouched(self):
        """單條 route 拆失敗 ⇒ 列與 `access_app_id` 都留著，補償不得被呼叫。

        `test_protected_route_unlink_teardown_failure_rolls_back_keeps_row`
        鎖的就是這一格；本條額外釘住「補償沒有把它一起清掉」。
        """
        p = self._route('app-solo')
        client = self._client(lambda payload: (_ for _ in ()).throw(
            Exception('cf access down')))
        with patch(MOCK_SA_CLIENT, return_value=client):
            with patch(COMPENSATE, autospec=True) as comp:
                with self.assertRaises(UserError):
                    p.unlink()
        self.assertEqual(
            comp.call_count, 1,
            '補償入口仍會被呼叫（形狀一致），但它拿到的必須是空清單')
        self.assertEqual(comp.call_args.args[1], [],
                         '沒有任何一條真的拆成功 ⇒ 補償的清單必須是空的')
        app_id, is_protected, lost = self._row(p.id)
        self.assertEqual(app_id, 'app-solo')
        self.assertTrue(is_protected)
        self.assertFalse(lost)

    # ------------------------------------------------------------------ #
    #  A7：訊號欄位的兩個出口（to_dict ／ sync 清旗標）                     #
    # ------------------------------------------------------------------ #
    def test_A7_flag_is_surfaced_and_cleared_by_a_successful_sync(self):
        """`to_dict()` 要吐；一次成功的 sync 要把它清掉。

        不吐的話 UI 只讀得到 `is_protected` 這個意圖欄位，「以為受保護、實際
        完全開放」就沒有任何載體說得出口——那正是本 issue 的形狀。
        """
        route = self._route('app-flag')
        self.assertIn('access_protection_lost', route.to_dict())
        self.assertFalse(route.to_dict()['access_protection_lost'])

        route._access_mark_protection_lost()
        self.assertTrue(route.to_dict()['access_protection_lost'])
        self.assertFalse(route.access_app_id)
        self.assertTrue(route.is_protected, '標記不得動 is_protected')

        client = self._client(lambda payload: _sync_result(app_id='app-new'))
        with patch(MOCK_SA_CLIENT, return_value=client):
            self.assertTrue(route._sync_access_to_operator())
        self.assertFalse(
            route.access_protection_lost,
            '旗標由 sync 結果驅動：推成功了就代表 CF 上的狀態與列一致')
        self.assertEqual(route.access_app_id, 'app-new')


# ====================================================================== #
#  F 系列：SecurityAccess.unlink() / action_delete()（第五腿）            #
#  —— PR #802 覆核 [HIGH]：`_cleanup_cloudflare()` 自己不 raise，但它的    #
#     呼叫端在它 partial success **之後**才 raise。                        #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestSecurityAccessUnlinkFailOpen(TransactionCase):
    """第五腿：刪整個 SA 時 tunnel 刪不掉 ⇒ 已拆的 route 帶死 id 復活。

    🔴 **比 route 那一腿更直接可達**：不需要 multi-id RPC，租戶從 `/woow` 按刪除
    （controller 走 `action_delete()`）就是這條路徑；tunnel 刪除偶發失敗
    （operator 暫時掛掉／逾時）即觸發。
    """

    def setUp(self):
        super().setUp()
        self.workspace = self.env['woow_paas_platform.workspace'].sudo(
        ).create({'name': 'WS 794F %s' % uuid.uuid4().hex[:6]})

    def _sa_with_routes(self, *app_ids):
        sa = self.env['woow_paas_platform.security_access'].sudo().create(
            {'name': 'SA 794F %s' % uuid.uuid4().hex[:6],
             'workspace_id': self.workspace.id})
        _internal_sa(sa).write({'state': 'active', 'tunnel_id': 'tun-794f'})
        routes = self.env[_ROUTE].sudo()
        for app_id in app_ids:
            route = _skip_route_sync(self.env[_ROUTE].sudo()).create({
                'security_access_id': sa.id,
                'subdomain_prefix': 'f%s' % uuid.uuid4().hex[:8],
                'is_protected': True})
            route = route.with_context(sa_skip_route_sync=False)
            _internal_sa(route).access_app_id = app_id
            routes |= route
        return sa, routes

    def _row(self, route_id):
        """raw SQL 問交易——列是被 savepoint 救回來的，`exists()` 讀不準。"""
        self.env.flush_all()
        self.env.cr.execute(
            'SELECT access_app_id, is_protected, access_protection_lost '
            'FROM woow_paas_platform_security_access_route WHERE id = %s',
            (route_id,))
        return self.env.cr.fetchone()

    def _client(self, tunnel_ok):
        client = MagicMock()
        client.sync_access_app.return_value = _teardown_result()
        client.sync_tunnel_routes.return_value = {'routes': []}
        if tunnel_ok:
            client.delete_tunnel.return_value = {'success': True}
        else:
            client.delete_tunnel.side_effect = Exception('cf tunnel down')
        return client

    @mute_logger(LOG_SA, LOG_REAP)
    def test_F1_unlink_tunnel_failure_compensates_the_torn_down_routes(self):
        """單筆刪 SA、tunnel 刪除失敗 ⇒ 已拆的 route 必須交給補償。

        前提斷言：兩條 route 的 Access teardown 都**真的送出去過**，且
        `delete_tunnel` **真的被試過**——少了它，本測試在「根本沒拆」或「沒走到
        tunnel 那步」的世界也會綠。
        """
        sa, routes = self._sa_with_routes('app-f1a', 'app-f1b')
        client = self._client(tunnel_ok=False)
        with patch(MOCK_SA_CLIENT, return_value=client):
            with patch(COMPENSATE, autospec=True) as comp:
                with self.assertRaises(UserError):
                    sa.unlink()

        sent = [c.args[0] for c in client.sync_access_app.call_args_list]
        for app_id in ('app-f1a', 'app-f1b'):
            self.assertTrue(
                any(c.get('enabled') is False
                    and c.get('existing_app_id') == app_id for c in sent),
                '前提檢查：%s 的 teardown 必須真的被送出去過' % app_id)
        self.assertTrue(client.delete_tunnel.called,
                        '前提檢查：tunnel 刪除必須真的被嘗試過')

        comp.assert_called_once()
        self.assertEqual(
            sorted(comp.call_args.args[1]),
            sorted((_ROUTE, r.id) for r in routes),
            '兩條都真的在 CF 上被拆掉了 ⇒ 兩條都要進補償清單')
        for route in routes:
            self.assertTrue(self._row(route.id)[1],
                            '不得自動降 is_protected')

    def test_F2_positive_control_clean_teardown_never_compensates(self):
        """陽性對照：tunnel 也刪得掉 ⇒ SA 與 route 都消失，補償一次都沒被呼叫。"""
        sa, routes = self._sa_with_routes('app-f2a')
        route_id = routes.id
        client = self._client(tunnel_ok=True)
        with patch(MOCK_SA_CLIENT, return_value=client):
            with patch(COMPENSATE, autospec=True) as comp:
                sa.unlink()
        self.assertEqual(
            comp.call_count, 0,
            '沒有 fail-open 時補償不得被呼叫——補償恆執行會讓 F1／F3 假綠')
        self.assertIsNone(self._row(route_id), 'route 列必須真的消失')

    @mute_logger(LOG_SA, LOG_REAP)
    def test_F3_action_delete_compensates_its_own_pre_call_teardown(self):
        """🔴 `action_delete()` 那一層**不能只靠 `unlink()`**。

        tunnel 刪不掉那一格的順序是：`action_delete` 的 pre-call 把 Access 全部
        拆掉並清掉 `access_app_id` → `unlink()` 內的第二次 `_cleanup_cloudflare()`
        **已經沒有東西可拆** ⇒ `unlink()` 自己的累加器是**空的**。所以真正的
        受害者只有 `action_delete` 這一層看得到。

        本條的鑑別式就是這個：斷言補償**確實拿到了那條 route**。若 `action_delete`
        沒有自己那一層，補償會被呼叫（來自 `unlink()`）但清單是空的。
        """
        sa, routes = self._sa_with_routes('app-f3a')
        route_id = routes.id
        client = self._client(tunnel_ok=False)
        with patch(MOCK_SA_CLIENT, return_value=client):
            with patch(COMPENSATE, autospec=True) as comp:
                with self.assertRaises(UserError):
                    sa.action_delete()

        sent = [c.args[0] for c in client.sync_access_app.call_args_list]
        self.assertTrue(
            any(c.get('enabled') is False
                and c.get('existing_app_id') == 'app-f3a' for c in sent),
            '前提檢查：pre-call 的 teardown 必須真的被送出去過')
        # 前提斷言②：unlink 那一層的第二次 cleanup 確實沒東西可拆
        # （否則本條測到的不是它要測的那一格）。
        teardowns = [c for c in sent if c.get('enabled') is False]
        self.assertEqual(
            len(teardowns), 1,
            'pre-call 拆完就清掉了 id ⇒ unlink 內的第二次 cleanup 不該再拆一次'
            '；若這裡是 2，代表本條測到的是另一格')
        self.assertTrue(
            any((_ROUTE, route_id) in call.args[1]
                for call in comp.call_args_list),
            'action_delete 必須有自己那一層補償——只靠 unlink 的話清單是空的')
        self.assertTrue(self._row(route_id)[1], '不得自動降 is_protected')


# ====================================================================== #
#  B 系列：SH route（窗口比 SA 更寬——連 pass1/pass2 分組都沒有）          #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestShRouteFailOpenCompensation(TransactionCase):

    def setUp(self):
        super().setUp()
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.ICP.set_param('woow_paas_platform.paas_domain', 'woowtech.io')
        self.ICP.set_param('woow_paas_platform.deploy_env', 'prod')
        self.workspace = self.env[
            'woow_paas_platform.workspace'].sudo().create(
                {'name': 'WS 794 SH %s' % uuid.uuid4().hex[:6]})
        SmartHome = sh_model(self.env)
        sh = SmartHome.create({
            'name': 'sh-794-%s' % uuid.uuid4().hex[:6],
            'workspace_id': self.workspace.id,
            'state': 'active',
            'tunnel_id': 'tun-794-%s' % uuid.uuid4().hex[:8]})
        sh.write({'subdomain': sh._generate_subdomain()})
        self.sh = sh.with_env(self.env)
        # primary route 不可刪，本組測的是非 primary 那條。
        _sm_skip_access_sync(self.env[_SH_ROUTE]).create(
            {'smart_home_id': self.sh.id, 'is_primary': True})

    def _route(self, app_id):
        route = _sm_skip_access_sync(self.env[_SH_ROUTE]).create({
            'smart_home_id': self.sh.id, 'is_primary': False,
            'subdomain_prefix': 'x%s' % uuid.uuid4().hex[:8],
            'is_protected': True})
        route = route.with_context(sm_skip_access_sync=False)
        _internal_sm_route_identity(route).access_app_id = app_id
        return route

    def _row(self, route_id):
        self.env.flush_all()
        self.env.cr.execute(
            'SELECT access_app_id, is_protected, access_protection_lost '
            'FROM woow_paas_platform_smart_home_route WHERE id = %s',
            (route_id,))
        return self.env.cr.fetchone()

    @mute_logger(LOG_SH, LOG_SH_ROUTE, LOG_REAP)
    def test_B1_only_really_torn_down_sh_routes_are_compensated(self):
        """SH route 批次刪除：p1 拆成功、p2 失敗 ⇒ 補償只拿到 p1。"""
        p1 = self._route('app-sh1')
        p2 = self._route('app-sh2')
        p1_id, p2_id = p1.id, p2.id
        client = MagicMock()

        def _side_effect(payload):
            if payload.get('enabled'):
                raise Exception('cf access still down')
            if payload.get('existing_app_id') == 'app-sh1':
                return _teardown_result()
            raise Exception('cf access down')

        client.sync_access_app.side_effect = _side_effect
        with patch(MOCK_SH_CLIENT, return_value=client):
            with patch(COMPENSATE, autospec=True) as comp:
                with self.assertRaises(UserError):
                    (p1 | p2).unlink()

        sent = [c.args[0] for c in client.sync_access_app.call_args_list]
        self.assertTrue(
            any(c.get('enabled') is False
                and c.get('existing_app_id') == 'app-sh1' for c in sent),
            '前提檢查：p1 的 teardown 必須真的被送出去過')
        # 🔴 SH 這一腿原本是 eager list comprehension、連 SA 的 pass1/pass2
        # 分組都沒有 ⇒ 舊碼會把 p2 也送去拆。斷言只送了兩次（p1 成功、p2 失敗
        # 就停）本身就把 short-circuit 釘住。
        teardowns = [c for c in sent if c.get('enabled') is False]
        self.assertEqual(
            [c.get('existing_app_id') for c in teardowns],
            ['app-sh1', 'app-sh2'])
        comp.assert_called_once()
        self.assertEqual(comp.call_args.args[1], [(_SH_ROUTE, p1_id)])
        # 不誤傷：拆失敗那條的 app id 是重試錨點。
        self.assertEqual(self._row(p2_id)[0], 'app-sh2')
        self.assertTrue(self._row(p2_id)[1], '不得自動降 is_protected')
        self.assertFalse(self._row(p2_id)[2])

    def test_B2_positive_control_clean_sh_batch_never_compensates(self):
        """陽性對照：兩條都拆得掉 ⇒ 補償一次都沒被呼叫。"""
        p1 = self._route('app-shok1')
        p2 = self._route('app-shok2')
        p1_id, p2_id = p1.id, p2.id
        client = MagicMock()
        client.sync_access_app.return_value = _teardown_result()
        with patch(MOCK_SH_CLIENT, return_value=client):
            with patch(COMPENSATE, autospec=True) as comp:
                (p1 | p2).unlink()
        self.assertEqual(comp.call_count, 0)
        self.assertIsNone(self._row(p1_id))
        self.assertIsNone(self._row(p2_id))

    @mute_logger(LOG_SH, LOG_SH_ROUTE, LOG_REAP)
    def test_B3_smart_home_unlink_leg_compensates_too(self):
        """第四處：`SmartHome.unlink()` 的 `_teardown_routes_access()` 迴圈。

        這一格比 route 那條更重——它的 raise 有一個很可達的來源是「tunnel 刪不
        掉」，而 **tunnel 活著正代表 hostname 照常服務**，Access 卻已經被拆掉。
        本 method 原本沒有 savepoint，是本修法加的。
        """
        route = self._route('app-shu')
        client = MagicMock()
        client.sync_access_app.side_effect = (
            lambda payload: (_teardown_result() if not payload.get('enabled')
                             else _sync_result(app_id='app-shu-rebuilt')))
        # tunnel 刪不掉 ⇒ `_cleanup_tunnel` 保留 tunnel_id ⇒ unlink raise。
        client.delete_tunnel.side_effect = Exception('cf tunnel down')
        with patch(MOCK_SH_CLIENT, return_value=client):
            with patch(COMPENSATE, autospec=True) as comp:
                with self.assertRaises(UserError):
                    self.sh.unlink()
        sent = [c.args[0] for c in client.sync_access_app.call_args_list]
        self.assertTrue(
            any(c.get('enabled') is False
                and c.get('existing_app_id') == 'app-shu' for c in sent),
            '前提檢查：Access teardown 必須真的送出去過，而且 tunnel 刪除必須'
            '真的被嘗試過（否則本測試測到的是另一格）')
        self.assertTrue(client.delete_tunnel.called)
        comp.assert_called_once()
        self.assertIn(
            (_SH_ROUTE, route.id), comp.call_args.args[1],
            'tunnel 刪不掉導致整筆交易被丟棄 ⇒ 這條 route 就是「宣稱受保護、'
            'CF 上沒有 app、而 hostname 照常服務」那一格')
        self.assertTrue(self._row(route.id)[1], '不得自動降 is_protected')


# ====================================================================== #
#  C 系列：CloudService（issue 未涵蓋、本輪新納入的第三條腿）              #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestCsFailOpenCompensation(TransactionCase):

    def setUp(self):
        super().setUp()
        self.workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'WS 794 CS %s' % uuid.uuid4().hex[:6]})
        self.template = self.env[
            'woow_paas_platform.cloud_app_template'].create({
                'name': 'CS 794 tmpl',
                'slug': 'cs-794-%s' % uuid.uuid4().hex[:6],
                'helm_repo_url': '',
                'helm_chart_name': 'oci://example.com/cs-794',
                # ⚠ 必須釘版：未釘版的 template 會被 create() 的供給閘擋下。
                'helm_chart_version': '1.0.0',
                'workload_type': 'container',
                'post_deploy_init_type': 'none',
                'mcp_enabled': False,
            })

    def _service(self, name, subdomain, app_id):
        svc = _internal_provision(self.env[_CS]).create({
            'name': name, 'workspace_id': self.workspace.id,
            'template_id': self.template.id, 'state': 'running',
            'subdomain': subdomain})
        _cs_skip_access_sync(_internal_cs_access_identity(svc)).write(
            {'is_protected': True, 'access_app_id': app_id})
        return svc

    def _row(self, svc_id):
        self.env.flush_all()
        self.env.cr.execute(
            'SELECT access_app_id, is_protected, access_protection_lost '
            'FROM woow_paas_platform_cloud_service WHERE id = %s', (svc_id,))
        return self.env.cr.fetchone()

    @mute_logger(LOG_CS, LOG_REAP)
    def test_C1_only_really_torn_down_services_are_compensated(self):
        """批次刪除兩台服務：第一台拆成功、第二台失敗 ⇒ 補償只拿到第一台。

        ⚠ CS 這一腿原本**沒有 savepoint**（拆在 `super().unlink()` 之前，
        「什麼都還沒刪」只涵蓋失敗的那一台）。savepoint 是本修法加的，而它有
        功能性作用：外層交易對那幾列的 UPDATE 若沒回捲，補償的獨立交易就會撞
        PG 寫寫衝突等到 lock_timeout。
        """
        s1 = self._service('c794a', 'c794a-%s' % uuid.uuid4().hex[:6],
                           'app-cs1')
        s2 = self._service('c794b', 'c794b-%s' % uuid.uuid4().hex[:6],
                           'app-cs2')

        def _seam(self_svc, app_id=None, policy_ids=None, app_name=None,
                  hostname=None):
            if app_id == 'app-cs1':
                return _teardown_result()
            raise RuntimeError('operator down')

        with patch(MOCK_CS_SEAM, autospec=True, side_effect=_seam) as seam:
            with patch(COMPENSATE, autospec=True) as comp:
                with self.assertRaises(UserError):
                    (s1 | s2).unlink()
        self.assertTrue(
            any(c.kwargs.get('app_id') == 'app-cs1'
                for c in seam.call_args_list),
            '前提檢查：第一台的 teardown 必須真的被送出去過')
        comp.assert_called_once()
        self.assertEqual(comp.call_args.args[1], [(_CS, s1.id)])
        # 不誤傷：拆失敗那台的 app id 是重試錨點。
        self.assertEqual(self._row(s2.id)[0], 'app-cs2')
        self.assertTrue(self._row(s2.id)[1], '不得自動降 is_protected')
        self.assertFalse(self._row(s2.id)[2])

    def test_C2_positive_control_clean_cs_batch_never_compensates(self):
        """陽性對照：兩台都拆得掉 ⇒ 補償一次都沒被呼叫。"""
        s1 = self._service('c794c', 'c794c-%s' % uuid.uuid4().hex[:6],
                           'app-csok1')
        s2 = self._service('c794d', 'c794d-%s' % uuid.uuid4().hex[:6],
                           'app-csok2')
        s1_id, s2_id = s1.id, s2.id
        with patch(MOCK_CS_SEAM, autospec=True,
                   return_value=_teardown_result()):
            with patch(COMPENSATE, autospec=True) as comp:
                (s1 | s2).unlink()
        self.assertEqual(comp.call_count, 0)
        self.assertIsNone(self._row(s1_id))
        self.assertIsNone(self._row(s2_id))

    def test_C3_service_without_access_is_not_marked(self):
        """沒有 Access app 的服務不得被算進「拆成功」——它根本沒東西要拆。

        少了這一條，`_teardown_access_if_any()` 的 ``True``（同時代表「拆乾淨
        了」與「本來就沒東西要拆」）會讓補償對整批服務無差別標記。
        """
        s1 = self._service('c794e', 'c794e-%s' % uuid.uuid4().hex[:6],
                           'app-cse1')
        plain = _internal_provision(self.env[_CS]).create({
            'name': 'c794f', 'workspace_id': self.workspace.id,
            'template_id': self.template.id, 'state': 'running',
            'subdomain': 'c794f-%s' % uuid.uuid4().hex[:6]})
        self.assertFalse(plain._has_access_teardown())
        with patch(MOCK_CS_SEAM, autospec=True,
                   return_value=_teardown_result()):
            with patch(COMPENSATE, autospec=True) as comp:
                with self.assertRaises(UserError):
                    # 讓 s1 拆成功之後在 helm 那格炸掉：構造「補償清單只該有
                    # s1」的情境。
                    with patch(
                        'odoo.addons.woow_paas_platform.models.cloud_service.'
                        'CloudService._cleanup_helm',
                        autospec=True, return_value=False,
                    ):
                        _cs_skip_access_sync(plain).write(
                            {'helm_release_name': 'r', 'helm_namespace': 'n'})
                        (s1 | plain).unlink()
        self.assertEqual(comp.call_count, 1)
        self.assertEqual(
            comp.call_args.args[1], [(_CS, s1.id)],
            '只有真的對 CF 拆過的那一台才進補償清單')


# ====================================================================== #
#  G 系列：落帳失敗時「CF 已拆成功」的事實不得跟著丟掉（二輪覆核 [HIGH]）   #
# ====================================================================== #

SETTLE_TEARDOWN = (
    'odoo.addons.woow_paas_platform.models.cf_access_policy_reap.'
    'CfAccessPolicyReap._settle_teardown_after_commit')


@tagged('post_install', '-at_install')
class TestTornDownSurvivesLedgerFailure(TransactionCase):
    """🔴 `_settle_teardown_after_commit` **明文 re-raise `OperationalError`**。

    三腿的 `_teardown_*` 都是「CF 拆成功 → 落帳 → `return True`」。落帳撞到並發
    例外（40001／40P01／55P03——正是 `retrying()` 存在的理由）時它**不會**
    `return True` ⇒ 舊寫法把 `torn_down.append()` 掛在呼叫端的 `if ok:` 裡
    ⇒ **補償拿到空清單、`if not torn_down: return False` 當場空轉**。

    淨效果：CF 上的 app 已經真的被刪、列回捲復活帶著 `is_protected=True`、
    而補償一次都沒做事——與「補償根本沒被接上」在列狀態上**完全不可區分**。

    ⚠ 可達性不低：它需要的只是**落帳那一步**撞到並發例外，而落帳寫的
    `cf_access_policy_reap` 表同時被每小時的 dead-anchor cron 掃描與更新。

    修法的不變式：**「CF 已經拆成功」這個事實的記錄，不能依賴其後任何一步是否
    成功**——所以 append 移到 operator 回 2xx 的當下、落帳之前。回傳值語意不動。
    """

    def setUp(self):
        super().setUp()
        self.workspace = self.env['woow_paas_platform.workspace'].sudo(
        ).create({'name': 'WS 794G %s' % uuid.uuid4().hex[:6]})
        self.sa = self.env['woow_paas_platform.security_access'].sudo().create(
            {'name': 'SA 794G %s' % uuid.uuid4().hex[:6],
             'workspace_id': self.workspace.id})
        _internal_sa(self.sa).write(
            {'state': 'active', 'tunnel_id': 'tun-794g'})

    def _route(self, app_id):
        route = _skip_route_sync(self.env[_ROUTE].sudo()).create({
            'security_access_id': self.sa.id,
            'subdomain_prefix': 'g%s' % uuid.uuid4().hex[:8],
            'is_protected': True})
        route = route.with_context(sa_skip_route_sync=False)
        _internal_sa(route).access_app_id = app_id
        return route

    def _client(self):
        client = MagicMock()
        client.sync_access_app.return_value = _teardown_result()
        client.sync_tunnel_routes.return_value = {'routes': []}
        return client

    @mute_logger(LOG_SA, LOG_REAP)
    def test_G1_ledger_operational_error_still_records_the_torn_down_anchor(self):
        """落帳拋 `OperationalError` ⇒ 補償仍必須拿到**非空**清單。

        前提斷言：teardown 真的送出去過、而且落帳真的被呼叫過（否則本條測到的
        是另一格）。
        """
        p1 = self._route('app-g1')
        p1_id = p1.id
        client = self._client()
        with patch(MOCK_SA_CLIENT, return_value=client):
            with patch(SETTLE_TEARDOWN, autospec=True,
                       side_effect=OperationalError('40001 serialization')
                       ) as settle:
                with patch(COMPENSATE, autospec=True) as comp:
                    with self.assertRaises(OperationalError):
                        p1.unlink()

        sent = [c.args[0] for c in client.sync_access_app.call_args_list]
        self.assertTrue(
            any(c.get('enabled') is False
                and c.get('existing_app_id') == 'app-g1' for c in sent),
            '前提檢查：CF teardown 必須真的送出去過（＝那顆 app 真的沒了）')
        self.assertTrue(settle.called, '前提檢查：落帳必須真的被呼叫過')

        comp.assert_called_once()
        self.assertEqual(
            comp.call_args.args[1], [(_ROUTE, p1_id)],
            '🔴 落帳失敗**不得**讓「CF 已拆成功」這個事實跟著丟掉——清單為空時'
            '補償當場空轉，而那個結局與「補償根本沒被接上」不可區分')

    def test_G2_positive_control_normal_ledger_path_unchanged(self):
        """陽性對照：落帳正常時行為一個字都沒變（列消失、補償不被呼叫）。"""
        p1 = self._route('app-g2')
        p1_id = p1.id
        client = self._client()
        with patch(MOCK_SA_CLIENT, return_value=client):
            with patch(COMPENSATE, autospec=True) as comp:
                p1.unlink()
        self.assertEqual(comp.call_count, 0)
        self.env.flush_all()
        self.env.cr.execute(
            'SELECT id FROM woow_paas_platform_security_access_route '
            'WHERE id = %s', (p1_id,))
        self.assertIsNone(self.env.cr.fetchone(), 'route 列必須真的消失')

    @mute_logger(LOG_SA, LOG_REAP)
    def test_G3_return_value_semantics_unchanged_on_ledger_failure(self):
        """回傳值語意**不得**為了補這格而提前 `return True`。

        落帳失敗時 `_teardown_route_access` 應該讓 `OperationalError` 外拋
        （#518：吞掉它 Odoo 的 `retrying()` 永遠看不到），而不是回 True 假裝
        整趟成功。少了這條，「把 append 移前面」很容易被順手寫成「把 return
        也移前面」。
        """
        sa = self.sa
        with patch(MOCK_SA_CLIENT, return_value=self._client()):
            with patch(SETTLE_TEARDOWN, autospec=True,
                       side_effect=OperationalError('40001 serialization')):
                with self.assertRaises(OperationalError):
                    sa._teardown_route_access(
                        app_id='app-g3', policy_ids=[], app_name='g3',
                        hostname='g3.example', anchor_model=_ROUTE,
                        anchor_id=999999, torn_down=[])


# ---------------------------------------------------------------------- #
#  G4-G7：CS 與 SH route 兩腿的**同型回歸鎖**                             #
# ---------------------------------------------------------------------- #
#
# 🔴 為什麼要逐腿鎖，而不是「SA route 那腿綠了就好」：
#
# 四腿的 `_teardown_*` 是**逐字同型**的三段（CF 拆成功 → 記錄副作用 → 落帳 →
# `return True`），而這個缺陷的形狀正是「其中一腿的 append 被挪回 `if ok:` 裡」。
# 本 repo 自己在 `_ANCHOR_FIELDS` 的註解裡逐字記過同一個模式：**第 N 個腿上線時
# 會漏改其中一處，而症狀是沒有輸出**。#794 花了兩輪覆核才找到它，只鎖四分之一
# 等於把剩下四分之三留給下一次重構。
#
# G4/G5＝CS 腿的 G1/G3；G6/G7＝SH route 腿的 G1/G3。陽性對照沿用 G2 與各腿既有
# 的 `test_*_positive_control_*`，不逐腿重複。


@tagged('post_install', '-at_install')
class TestTornDownSurvivesLedgerFailureCs(TransactionCase):
    """CS 腿（`_teardown_cs_access`）的落帳失敗回歸鎖。"""

    def setUp(self):
        super().setUp()
        self.workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'WS 794Gcs %s' % uuid.uuid4().hex[:6]})
        self.template = self.env[
            'woow_paas_platform.cloud_app_template'].create({
                'name': 'CS 794G tmpl',
                'slug': 'cs-794g-%s' % uuid.uuid4().hex[:6],
                'helm_repo_url': '',
                'helm_chart_name': 'oci://example.com/cs-794g',
                # ⚠ 必須釘版：未釘版的 template 會被 create() 的供給閘擋下。
                'helm_chart_version': '1.0.0',
                'workload_type': 'container',
                'post_deploy_init_type': 'none',
                'mcp_enabled': False,
            })

    def _service(self, app_id):
        svc = _internal_provision(self.env[_CS]).create({
            'name': 'g794cs', 'workspace_id': self.workspace.id,
            'template_id': self.template.id, 'state': 'running',
            'subdomain': 'g794cs-%s' % uuid.uuid4().hex[:6]})
        _cs_skip_access_sync(_internal_cs_access_identity(svc)).write(
            {'is_protected': True, 'access_app_id': app_id})
        return svc

    @mute_logger(LOG_CS, LOG_REAP)
    def test_G4_cs_ledger_operational_error_still_records_the_anchor(self):
        """CS：落帳拋 `OperationalError` ⇒ 補償仍必須拿到**非空**清單。"""
        svc = self._service('app-g4')
        svc_id = svc.id
        with patch(MOCK_CS_SEAM, autospec=True,
                   return_value=_teardown_result()) as seam:
            with patch(SETTLE_TEARDOWN, autospec=True,
                       side_effect=OperationalError('40001 serialization')
                       ) as settle:
                with patch(COMPENSATE, autospec=True) as comp:
                    with self.assertRaises(OperationalError):
                        svc.unlink()
        self.assertTrue(
            any(c.kwargs.get('app_id') == 'app-g4'
                for c in seam.call_args_list),
            '前提檢查：CF teardown 必須真的送出去過（＝那顆 app 真的沒了）')
        self.assertTrue(settle.called, '前提檢查：落帳必須真的被呼叫過')
        comp.assert_called_once()
        self.assertEqual(
            comp.call_args.args[1], [(_CS, svc_id)],
            '🔴 落帳失敗不得讓「CF 已拆成功」跟著丟掉——清單為空時補償空轉，'
            '而那個結局與「補償根本沒被接上」不可區分')

    @mute_logger(LOG_CS, LOG_REAP)
    def test_G5_cs_return_value_semantics_unchanged_on_ledger_failure(self):
        """CS：落帳失敗時 `OperationalError` 必須外拋，不得偽裝成 `True`。

        釘住「把 append 移前面」不會被順手寫成「把 return 也移前面」。
        """
        svc = self._service('app-g5')
        with patch(MOCK_CS_SEAM, autospec=True,
                   return_value=_teardown_result()):
            with patch(SETTLE_TEARDOWN, autospec=True,
                       side_effect=OperationalError('40001 serialization')):
                with self.assertRaises(OperationalError):
                    svc._teardown_cs_access(
                        app_id='app-g5', policy_ids=[], app_name='g5',
                        hostname='g5.example', torn_down=[])


@tagged('post_install', '-at_install')
class TestTornDownSurvivesLedgerFailureShRoute(TransactionCase):
    """SH route 腿（`_push_access_teardown`）的落帳失敗回歸鎖。"""

    def setUp(self):
        super().setUp()
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.ICP.set_param('woow_paas_platform.paas_domain', 'woowtech.io')
        self.ICP.set_param('woow_paas_platform.deploy_env', 'prod')
        self.workspace = self.env[
            'woow_paas_platform.workspace'].sudo().create(
                {'name': 'WS 794Gsh %s' % uuid.uuid4().hex[:6]})
        SmartHome = sh_model(self.env)
        sh = SmartHome.create({
            'name': 'sh-794g-%s' % uuid.uuid4().hex[:6],
            'workspace_id': self.workspace.id,
            'state': 'active',
            'tunnel_id': 'tun-794g-%s' % uuid.uuid4().hex[:8]})
        sh.write({'subdomain': sh._generate_subdomain()})
        self.sh = sh.with_env(self.env)
        _sm_skip_access_sync(self.env[_SH_ROUTE]).create(
            {'smart_home_id': self.sh.id, 'is_primary': True})

    def _route(self, app_id):
        route = _sm_skip_access_sync(self.env[_SH_ROUTE]).create({
            'smart_home_id': self.sh.id, 'is_primary': False,
            'subdomain_prefix': 'y%s' % uuid.uuid4().hex[:8],
            'is_protected': True})
        route = route.with_context(sm_skip_access_sync=False)
        _internal_sm_route_identity(route).access_app_id = app_id
        return route

    def _client(self):
        client = MagicMock()
        client.sync_access_app.return_value = _teardown_result()
        return client

    @mute_logger(LOG_SH, LOG_SH_ROUTE, LOG_REAP)
    def test_G6_sh_ledger_operational_error_still_records_the_anchor(self):
        """SH route：落帳拋 `OperationalError` ⇒ 補償仍必須拿到**非空**清單。"""
        route = self._route('app-g6')
        route_id = route.id
        client = self._client()
        with patch(MOCK_SH_CLIENT, return_value=client):
            with patch(SETTLE_TEARDOWN, autospec=True,
                       side_effect=OperationalError('40001 serialization')
                       ) as settle:
                with patch(COMPENSATE, autospec=True) as comp:
                    with self.assertRaises(OperationalError):
                        route.unlink()
        sent = [c.args[0] for c in client.sync_access_app.call_args_list]
        self.assertTrue(
            any(c.get('enabled') is False
                and c.get('existing_app_id') == 'app-g6' for c in sent),
            '前提檢查：CF teardown 必須真的送出去過')
        self.assertTrue(settle.called, '前提檢查：落帳必須真的被呼叫過')
        comp.assert_called_once()
        self.assertEqual(
            comp.call_args.args[1], [(_SH_ROUTE, route_id)],
            '🔴 落帳失敗不得讓「CF 已拆成功」跟著丟掉')

    @mute_logger(LOG_SH, LOG_SH_ROUTE, LOG_REAP)
    def test_G7_sh_return_value_semantics_unchanged_on_ledger_failure(self):
        """SH route：落帳失敗時 `OperationalError` 必須外拋，不得偽裝成 `True`。"""
        route = self._route('app-g7')
        info = route._capture_access_teardown()
        self.assertIsNotNone(
            info, '前提檢查：捕獲必須成功，否則本條測到的是另一格')
        with patch(MOCK_SH_CLIENT, return_value=self._client()):
            with patch(SETTLE_TEARDOWN, autospec=True,
                       side_effect=OperationalError('40001 serialization')):
                with self.assertRaises(OperationalError):
                    self.env[_SH_ROUTE]._push_access_teardown(
                        info, torn_down=[])


# ====================================================================== #
#  D 系列：補償 helper 自己的契約（它是本 repo model 層唯一自開 cursor 的地方） #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestCompensationHelperContract(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Reap = self.env['woow_paas_platform.cf_access_policy_reap']
        self.logger = logging.getLogger(LOG_REAP)

    def test_D1_never_raises_so_it_can_never_hide_the_original_error(self):
        """開不了 cursor ⇒ 回 False、出聲、**不 raise**。

        呼叫端一律在 `except` 裡、正準備重拋原本那個 UserError。補償的例外若
        外拋，租戶看到的會是一句與他的動作無關的話，而真正的原因（Cloudflare
        Access teardown failed…）就此消失。
        """
        with patch('odoo.modules.registry.Registry.cursor',
                   side_effect=RuntimeError('no connection')):
            with self.assertLogs(LOG_REAP, level='ERROR'):
                result = self.Reap._compensate_fail_open_after_rollback(
                    [(_ROUTE, 2147483000)], self.logger, 'probe-D1')
        self.assertFalse(result, '開不了 cursor ⇒ 必須回 False')

    def test_D2_empty_list_is_a_no_op(self):
        """沒有任何一條真的拆成功 ⇒ 連 cursor 都不該開。

        陽性對照同時在這裡：patch 住的 `Registry.cursor` 一旦被呼叫就會 raise，
        所以「沒有炸」證明的是真的沒開；而 D1 證明那個 patch 真的接得上。
        """
        with patch('odoo.modules.registry.Registry.cursor',
                   side_effect=RuntimeError('should not be called')):
            self.assertFalse(self.Reap._compensate_fail_open_after_rollback(
                [], self.logger, 'probe-D2'))

    def test_D3_arms_a_lock_timeout_before_touching_any_row(self):
        """🔴 補償交易進場的第一件事必須是 `SET LOCAL lock_timeout`。

        這不是效能參數是安全參數：外層交易剛在一個已回捲的 savepoint 裡對那幾
        列發過 DELETE／UPDATE，PG 若判定那個 subxid「還在進行中」（subxid cache
        溢位時會退化成這樣），補償的 UPDATE 會去等外層交易的 xact lock，而外層
        正同步等補償回來 ⇒ 互鎖成 hang，deadlock detector 看不到那個環。
        `lock_timeout` 把 hang 換成一個接得住的錯誤。

        少了這條測試，把那一行刪掉不會有任何測試變紅（症狀是**掛住**，而掛住
        的 CI 看起來像「跑很久」）。
        """
        from odoo.modules.registry import Registry as _Registry
        real_cursor = _Registry.cursor
        seen = []

        def _spy(registry_self, *args, **kwargs):
            cr = real_cursor(registry_self, *args, **kwargs)
            original = cr.execute

            def execute(query, params=None, log_exceptions=True):
                seen.append((query, params))
                return original(query, params)

            cr.execute = execute
            return cr

        with patch('odoo.modules.registry.Registry.cursor', _spy):
            self.assertTrue(self.Reap._compensate_fail_open_after_rollback(
                [(_ROUTE, 2147483000)], self.logger, 'probe-D3'))
        self.assertTrue(seen, '前提檢查：spy 必須真的接在被測路徑上')
        first_query, first_params = seen[0]
        self.assertIn(
            'lock_timeout', first_query,
            '補償交易的第一個 statement 必須是 SET LOCAL lock_timeout')
        self.assertEqual(
            first_params,
            (self.Reap._COMPENSATE_LOCK_TIMEOUT,),
            '逾時值必須來自那個具名常數，不是散在呼叫點的字面值')


# ====================================================================== #
#  E 系列：reconciler 的「左 − 右」方向（補償失敗時的偵測 backstop）        #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestReconcilerDanglingProtectedApps(TransactionCase):

    def _left(self, **apps):
        ls = LeftSet()
        ls.protected_access_apps.update(apps)
        return ls

    def test_E1_protected_app_missing_from_inventory_is_reported(self):
        """DB 宣稱受保護、CF inventory 沒有那顆 app ⇒ 上報表。"""
        ls = self._left(**{'app-dead': 'SA route 1 (x.woowtech.io)'})
        out = compute_dangling_protected_apps(
            [{'id': 'app-live', 'kind': 'access_app', 'name': 'other'}], ls)
        self.assertEqual([c.id for c in out], ['app-dead'])
        self.assertEqual(out[0].kind, 'access_app_missing',
                         'kind 必須與方向 A 的 access_app 分得開——兩個方向的'
                         '語意與人工處置剛好相反')
        self.assertFalse(out[0].reconcilable, '本報表零刪除')

    def test_E2_negative_control_live_app_is_not_reported(self):
        """陽性對照：app 還在 inventory 裡就不該上報。

        少了它，E1 可能只是因為這個函式對任何輸入都回一筆。
        """
        ls = self._left(**{'app-live': 'SA route 1 (x.woowtech.io)'})
        out = compute_dangling_protected_apps(
            [{'id': 'app-live', 'kind': 'access_app', 'name': 'x'}], ls)
        self.assertEqual(out, [])

    def test_E3_partial_scan_skips_the_whole_direction(self):
        """🔴 方向一反，安全性質也反：右集不完整時整個方向必須跳過。

        方向 A（右 − 左）退化的方向是少報（safe miss）；本方向退化的方向是
        **把活著的 app 誤報成已死**。所以 partial 不是「儘量報」，是不報。
        """
        ls = self._left(**{'app-dead': 'SA route 1 (x.woowtech.io)'})
        self.assertEqual(
            compute_dangling_protected_apps([], ls, partial=True), [],
            'partial 時必須整個方向跳過')
        self.assertEqual(
            len(compute_dangling_protected_apps([], ls, partial=False)), 1,
            '陽性對照：同一份輸入在完整掃描下確實會產出候選——否則 partial '
            '那半可能只是因為函式對任何輸入都回空')

    def test_E4_left_set_only_collects_rows_that_claim_protection(self):
        """左集只收「宣稱受保護且握著 app id」那一子集。

        未保護卻仍持有殘 id 的列下一次 sync 會送 `enabled=False` 自癒，不是
        安全問題；收進來只會製造雜訊，而雜訊會讓真正的那一筆被忽略。
        """
        ws = self.env['woow_paas_platform.workspace'].sudo().create(
            {'name': 'WS 794 rec %s' % uuid.uuid4().hex[:6]})
        sa = self.env['woow_paas_platform.security_access'].sudo().create(
            {'name': 'SA rec 794', 'workspace_id': ws.id})
        _internal_sa(sa).write({'state': 'active', 'tunnel_id': 'tun-rec'})
        protected = _skip_route_sync(self.env[_ROUTE].sudo()).create({
            'security_access_id': sa.id,
            'subdomain_prefix': 'rp%s' % uuid.uuid4().hex[:8],
            'is_protected': True})
        _internal_sa(protected).access_app_id = 'app-protected'
        unprotected = _skip_route_sync(self.env[_ROUTE].sudo()).create({
            'security_access_id': sa.id,
            'subdomain_prefix': 'ru%s' % uuid.uuid4().hex[:8],
            'is_protected': False})
        _internal_sa(unprotected).access_app_id = 'app-residual'

        ls = collect_left_set(self.env)
        self.assertIn('app-protected', ls.protected_access_apps)
        self.assertNotIn('app-residual', ls.protected_access_apps)
        # 方向 A 的白名單仍然兩顆都收（它的語意沒有變）。
        self.assertIn('app-protected', ls.access_app_ids)
        self.assertIn('app-residual', ls.access_app_ids)

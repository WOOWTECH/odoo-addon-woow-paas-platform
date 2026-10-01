# -*- coding: utf-8 -*-
"""#740：`SmartHome` 刪除路徑的**授權前置檢查**（authorization preflight）。

## 缺陷（修復前實測，真 HTTP ＋ 只掛 `base.group_user` 的租戶 ＋ operator 全 mock）

租戶按下刪除 → 端點回失敗 → **Cloudflare 那半的動作已經發生而且回捲不掉**。
第一個擋下來的位置**依記錄形態而不同**，所以三種形態各量一次（本檔的 fixture
builder 就是照這三種形態寫的）：

| 形態 | 修復前：被拒之前已送出的 operator 呼叫 | 修復前：交易落定後 DB 還留下什麼 |
| --- | --- | --- |
| **A** 不經 provision（無 route、有 tunnel_id） | `delete_tunnel(<自己的 uuid>, tunnel_name=…)` | 列還在、`state='deleting'`、`tunnel_id` **還在**（指著一顆已被刪除的 tunnel） |
| **B** 跑過 provision（有 primary route、route 上沒有 CF Access app） | **無** | 列還在、`state='deleting'` |
| **C** 跑過 provision ＋ `is_protected=True`（route 上有真的 `access_app_id`） | `sync_access_app({'enabled': False, …, 'existing_app_id': <app id>})` | 列還在、`state='deleting'`；route 的 `access_app_id` 被清空、`access_protection_lost=True` |

🔴 **C 才是本題最嚴重的一格，而 issue 沒有寫它**：一個**被拒絕**的刪除，實際效果
是「把這個 hostname 的登入強制關掉」——tunnel 沒動，hostname 照常服務，CF 上的
Access app 卻已經沒了。那兩個欄位變更是 `#794` 的補償在**獨立交易**寫的，回捲不掉。

🔴 **A 的錯誤訊息是假的**：`delete_tunnel` 成功之後，`_cleanup_tunnel` 那次清錨點的
write 含 `tunnel_token`（`groups='base.group_system'`）⇒ 租戶身分撞欄位級 ACL ⇒ 被
該方法的 `except Exception` 吞成「保留待重試」⇒ `tunnel_id` 留著 ⇒ fail-loud guard
拿著 *"Cloudflare cleanup failed; the tunnel is still live and billed"* raise，**而那顆
tunnel 已經沒了**。每一次重試都重複同一句假話（operator 對 404 當成功）。

⚠ 與 issue（2026-08-15 @ `5c641b7f`）的差異：issue 記的 `tunnel_id=False` 與
「端點回 `You are not allowed to delete 'Smart Home Instance'`」**今天三種形態都不成立**。
`#524`（清 3 個錨點）與 `#768`（route teardown ＋ route 的顯式 unlink）之後形狀就變了。
逐格差異寫在 PR body。

## 修法：授權前置檢查

在送出第一個外部副作用**之前**，把這次 unlink 會用到的每一個 unlink 權限先驗完。
**枚舉用提權身分**（teardown 迴圈走的就是提權後的集合，用呼叫者看得見的集合會讓
「看不到 ⇒ 不用檢查」變成 fail-open）、**檢查用呼叫者身分**。形狀取自本 repo 既有
的前置檢查前例 `cloud_service._assert_reverse_binding_authorized()`（#698）。

⚠ #740 當時**不改授權姿態**（租戶刪除仍然失敗），本檔守的是「它失敗得乾淨」，
不是「它會成功」。

## #1294 之後的重新錨定（被拒的身分換人、驅動面換成 RPC）

#1294 把 `access_smart_home_user` 改成 `unlink=1`：租戶 **owner/admin 刪得掉自己
工作區的 SH**（成功路徑的回歸測試在 `test_sh_tenant_delete_1294.py`）。本檔原本的
被拒身分是 alice（W1 owner）走刪除端點——修完之後她**應該成功**，那條路就不再是
「被拒」的樣本。而端點層對其他身分（user／guest／跨工作區）在呼叫 `action_delete()`
**之前**就以 role gate／workspace 歸屬擋掉，走不到前置檢查。

⇒ 被拒身分換成 **carol（W1 的 role=user 成員）**：她讀得到這台 SH（member_read
rule），所以被拒的理由**只會是** unlink 授權（`smart_home_rule_owner_admin_manage`
是唯一 `perm_unlink=True` 的 rule），不是「看不到」。驅動面換成真 HTTP 的
`/web/dataset/call_kw`——那正是前置檢查唯一還是**最後一道**守線的入口
（D 系列把它拿掉之後，外部副作用照樣送出）：

- A1-A4、B1-B3、D1-D2：`call_kw unlink`。
- A5：`call_kw action_delete`——前置檢查必須擋在 `state='deleting'` 那次 write
  **之前**（歸因：被拒的操作是 unlink，不是 write）。
- F 系列：`with_user(carol).unlink()`（model 層）。

另外兩件 #1294 改掉的事：route 的連帶刪除改以平台身分執行（route ACL 刻意維持
`unlink=0`），所以前置檢查只剩主表那一張；`_cleanup_tunnel` 清錨點改以平台身分寫
（#859 的可達格）。

## 這裡的斷言與 `test_smart_home_authz_524.py::test_C5` 不同，兩者不可互相頂替

C5 問的是「有沒有把**別的租戶**的 id 送出去」（`assertNotIn(b_uuid, called)`）——那個
斷言對**空清單恆真** ⇒ 本題修好之後它照樣綠，但綠的理由從「清單裡只有自己的」變成
「清單是空的」。C5 守的是 #524 的性質，維持原樣不動；本題的強斷言（外部呼叫數為零、
且逐一列名哪些 method 沒被呼叫）在本檔。

## TDD 紀律

- **租戶那一側一律走真 HTTP**：`check_access()` 的第一行是 `if not self.env.su`
  （Odoo 18 `odoo/models.py`）⇒ 本題的守衛在提權環境下是 no-op，用 `TransactionCase`
  預設 env 寫的「守住了」**測不到守衛**（`docs/decisions/patterns/su-env-exempt-guard-false-green.md`）。
- **每一格「沒有發生」都配同形態的陽性對照**（C 系列）：同一個 fixture 形態改用提權
  身分跑同一條路徑，外部呼叫必須照常送出、記錄必須照常被刪。少了它，「守住了」與
  「這條路徑在本測試環境根本不作用」不可區分。租戶 owner/admin 身分的陽性對照
  （#1294）在 `test_sh_tenant_delete_1294.py`。
- **注入紅燈**（D 系列）：把前置檢查換成 no-op，修復前的行為必須整個回來。
- **DB 一律裸 SQL 重讀**（`_db()`）：先 `invalidate_all(flush=False)` 丟掉 ORM 欄位
  快取（`flush=False` ⇒ 不會把快取裡的舊值反寫回 DB），再對 PostgreSQL 下 SELECT。
- **`@tagged` 掛在每一個具體 test class 上，fixture 不掛**——與隔壁
  `test_smart_home_authz_524.py`（掛在 fixture、具體類別靠繼承拿）**刻意相反**，
  兩種都會跑，這裡不是筆誤：明示在具體類別上，「有人把 fixture 換掉／改繼承鏈」
  時 tag 不會跟著消失。⚠ 順帶記著同一族的第三種失效機制（#533）：**寫在
  mixin／基底類別裡的 `test_*` 一條都不會被收集**（Odoo loader 取 class `__dict__`，
  不含繼承來的方法），而輸出與全綠不可區分 ⇒ `_Sh740Fixture` 只放 helper，
  18 條 `test_*` 全部住在具體類別自己的 body。
"""
import json
import logging
import uuid
from unittest.mock import MagicMock, patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from .test_smart_home_authz_524 import _SmartHome524Fixture

_logger = logging.getLogger(__name__)
_MARK = '740'
_MODEL = 'woow_paas_platform.smart_home'
_ROUTE_MODEL = 'woow_paas_platform.smart_home_route'

#: 這條路徑上**所有**會打到 Cloudflare 的 operator method（逐一列名——「呼叫數為零」
#: 這種斷言必須說得出零的是哪幾個，否則換一個新 method 進來時它還是綠的）。
_EXTERNAL_METHODS = ('sync_access_app', 'delete_tunnel', 'create_tunnel',
                     'get_tunnel_status')

#: 被拒身分要能**讀到**這台 SH（否則被拒的理由會是「看不到」而不是 unlink 授權）
#: ⇒ W1 的 role=user 成員 carol（見檔頭 #1294 重新錨定）。
_DENIED_ROLE_LABEL = 'carol（W1 role=user）'


class _Sh740Fixture(_SmartHome524Fixture):
    """三種形態的 builder ＋ 觀測 helper（量測與斷言共用同一份取值方式）。"""

    # ------------------------------------------------------------------ #
    #  operator mock                                                     #
    # ------------------------------------------------------------------ #
    def _client(self):
        """每個回傳值都明寫——`MagicMock` 的自動回傳值是 truthy，會讓
        `result.get('orphaned_policy_ids')` 之類的判斷走進與實際不同的分支。"""
        c = MagicMock()
        c.create_tunnel.side_effect = lambda **kw: {
            'tunnel_id': 'tun-%s' % uuid.uuid4().hex[:8],
            'tunnel_token': 'tok-%s' % uuid.uuid4().hex[:6],
            'tunnel_name': kw.get('name') or 'tunnel-name',
        }
        c.delete_tunnel.return_value = None
        c.sync_access_app.return_value = {
            'app_id': 'cf-app-%s' % uuid.uuid4().hex[:8],
            'policy_ids': [],
            'orphaned_policy_ids': [],
            'unresolved_idp_types': [],
        }
        c.get_tunnel_status.return_value = {'connections': []}
        return c

    @staticmethod
    def _calls(client):
        """列名：`method(args, kwargs)`，**保持呼叫順序**（順序本身是斷言對象）。"""
        out = []
        for name, args, kwargs in client.mock_calls:
            out.append('%s(%s)' % (
                name or '<client>',
                ', '.join(
                    [repr(a) for a in args]
                    + ['%s=%r' % (k, v) for k, v in sorted(kwargs.items())])))
        return out

    @staticmethod
    def _external_calls(client):
        """只留真的會打到 Cloudflare 的那幾個 method（過濾掉 `__bool__` 之類）。"""
        return [c for c in _Sh740Fixture._calls(client)
                if c.split('(', 1)[0] in _EXTERNAL_METHODS]

    # ------------------------------------------------------------------ #
    #  觀測                                                               #
    # ------------------------------------------------------------------ #
    def _db(self, sh_id):
        """裸 SQL 重讀（見檔頭 TDD 紀律最後一條）。"""
        self.env.invalidate_all(flush=False)
        cr = self.env.cr
        cr.execute(
            "SELECT id, state, tunnel_id, tunnel_name, subdomain, "
            "       (tunnel_token IS NOT NULL) AS has_token, error_message "
            "  FROM woow_paas_platform_smart_home WHERE id = %s", (sh_id,))
        sh = cr.dictfetchall()
        cr.execute(
            "SELECT id, is_primary, is_protected, access_app_id, "
            "       access_protection_lost "
            "  FROM woow_paas_platform_smart_home_route "
            " WHERE smart_home_id = %s ORDER BY id", (sh_id,))
        routes = cr.dictfetchall()
        cr.execute(
            "SELECT p.id, p.smart_home_route_id, p.cf_policy_id "
            "  FROM woow_paas_platform_security_access_access_policy p "
            "  JOIN woow_paas_platform_smart_home_route r "
            "    ON r.id = p.smart_home_route_id "
            " WHERE r.smart_home_id = %s ORDER BY p.id", (sh_id,))
        policies = cr.dictfetchall()
        return {'smart_home': sh, 'routes': routes, 'policies': policies}

    def _reap_rows(self):
        """待回收帳本（#528／#794）——被拒的刪除不得在上面留下任何一列。"""
        self.env.invalidate_all(flush=False)
        Reap = self.env['woow_paas_platform.cf_access_policy_reap'].sudo()
        return [
            {'id': r.id, 'anchor_model': r.anchor_model,
             'anchor_id': r.anchor_id, 'kind': r.kind,
             'cf_object_id': r.cf_object_id}
            for r in Reap.search([])
        ]

    def _report(self, title, payload):
        """把觀測整包印出來——斷言失敗時，光看 assert 訊息看不出是哪一格變了。"""
        _logger.info('%s %s\n%s', _MARK, title,
                     json.dumps(payload, indent=2, ensure_ascii=False,
                                default=str))

    # ------------------------------------------------------------------ #
    #  三種形態                                                           #
    # ------------------------------------------------------------------ #
    def _mk_no_route(self, label):
        """形態 A：不經 provision（＝C5 fixture 的形狀），有 tunnel_id、**沒有** route。"""
        sh = self._smart_home(
            self.w1, label,
            tunnel_id='own-tunnel-%s' % uuid.uuid4().hex[:8],
            tunnel_name='own-name-%s' % uuid.uuid4().hex[:6])
        self.assertTrue(sh.sudo().tunnel_id,
                        'fixture 前提：要有 tunnel 可刪，否則「沒送出 '
                        'delete_tunnel」是零資訊')
        self.assertFalse(sh.sudo().route_ids,
                         'fixture 前提：形態 A **不得**有 route')
        return sh

    def _mk_with_route(self, label, protected, workspace=None):
        """形態 B／C：真的跑過 provision ⇒ 有 primary route。"""
        home = self._order_smart_home(
            self.alice_login, workspace or self.w1, label)
        client = self._client()
        with self._patched_operator(client):
            home.sudo().action_provision()
            home.invalidate_recordset()
            self.assertTrue(home.sudo().tunnel_id,
                            'fixture 前提：provision 要真的存下 tunnel')
            route = home.sudo().route_ids
            self.assertEqual(
                len(route), 1,
                'fixture 前提：provision 要建出 primary route（沒有的話這一組'
                '就退化成形態 A，第一個擋點會不一樣）')
            if protected:
                route.write({'is_protected': True})
                route.invalidate_recordset()
                self.assertTrue(
                    route.access_app_id,
                    'fixture 前提：形態 C 要有真的可拆的 CF Access app id，'
                    '否則「沒送出 sync_access_app」是零資訊')
            else:
                self.assertFalse(
                    route.access_app_id,
                    'fixture 前提：形態 B 刻意沒有 Access app')
        return home

    # ------------------------------------------------------------------ #
    #  驅動                                                               #
    # ------------------------------------------------------------------ #
    def _denied_rpc(self, home, tag, method='unlink'):
        """被拒身分（carol，只掛 `base.group_user`、W1 role=user）走真 HTTP
        `/web/dataset/call_kw` 對這台 SH 呼叫 ``method``（#1294 重新錨定）。"""
        client = self._client()
        before = self._db(home.id)
        self.authenticate(self.carol_login, self.carol_login)
        with self._patched_operator(client):
            res = self._call_kw(_MODEL, method, [[home.id]])
        after = self._db(home.id)
        self._report('DENIED %s %s' % (method, tag), {
            'rpc': res,
            'external_calls_in_order': self._external_calls(client),
            'all_mock_calls_in_order': self._calls(client),
            'db_before': before, 'db_after': after,
            'reap_ledger': self._reap_rows(),
        })
        return client, res, before, after

    # ------------------------------------------------------------------ #
    #  斷言 helper                                                        #
    # ------------------------------------------------------------------ #
    def _assert_rpc_denied(self, result, tag):
        """RPC 必須回 `AccessError`，而且擋下來的是**主表** `smart_home`。

        回 error 之外還要歸因：「零外部呼叫」可能只是請求根本沒打到這條路徑
        （例如 method 名打錯回的是別種 error）。

        判別式刻意同時要求「點名主表」與「**不**點名 route 子表」：
        `woow_paas_platform.smart_home_route` 這個字串包含
        `woow_paas_platform.smart_home` ⇒ 只驗前者的話 route 那條訊息也會通過，
        而那正是 #740 修復前 B／C 兩形態被擋在 teardown **之後**的形狀。
        """
        self.assertIn('error', result,
                      '%s：%s 的 RPC 刪除**必須**被拒（回 %r）'
                      % (tag, _DENIED_ROLE_LABEL, result))
        text = self._error_text(result)
        self.assertIn('AccessError', text,
                      '%s：被拒的原因必須是授權（AccessError），回 %r' % (tag, text))
        self.assertIn('woow_paas_platform.smart_home', text,
                      '%s：歸因——擋下來的必須是主表的 unlink 權，回 %r'
                      % (tag, text))
        self.assertNotIn('smart_home_route', text,
                         '%s：歸因——不得是 route 子表（那是 #740 修前被擋在 '
                         'teardown 之後的形狀），回 %r' % (tag, text))
        return text

    def _assert_no_external_call(self, client, tag):
        """列名：逐一斷言每一個 method 都沒被呼叫（不是只比總數）。"""
        self.assertEqual(
            self._external_calls(client), [],
            '%s：授權被拒時不得對 operator 送出任何呼叫（實送 %r）'
            % (tag, self._external_calls(client)))
        for method in _EXTERNAL_METHODS:
            self.assertFalse(
                getattr(client, method).called,
                '%s：`%s` 不得被呼叫（實際被呼叫 %d 次）'
                % (tag, method, getattr(client, method).call_count))

    def _assert_row_untouched(self, before, after, tag):
        """裸 SQL 前後逐欄比對——「不得有任何欄位變更落地」的判準。"""
        self.assertTrue(after['smart_home'],
                        '%s：列必須還在（被刪掉了）' % tag)
        self.assertEqual(
            after, before,
            '%s：授權被拒時不得有任何欄位變更落地。before=%r after=%r'
            % (tag, before, after))


# ---------------------------------------------------------------------- #
#  A 系列：授權被拒 ⇒ 零外部呼叫                                          #
# ---------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestSh740NoExternalCallWhenDenied(_Sh740Fixture):
    """被拒身分（#1294 起＝carol，W1 role=user）的 RPC 刪除：零外部呼叫。

    #740 修復前紅在：A1（`delete_tunnel` 被送出）、A3（`sync_access_app` 被送出）。
    鑑別力來源是 D 系列：同一個身分、同一條 RPC，拿掉前置檢查之後外部呼叫照樣送出。
    """

    def test_A1_no_route_denied_delete_sends_nothing(self):
        home = self._mk_no_route('740 A1')
        client, res, _b, _a = self._denied_rpc(home, 'A1/no-route')
        self._assert_rpc_denied(res, 'A1')
        self._assert_no_external_call(client, 'A1')

    def test_A2_with_route_denied_delete_sends_nothing(self):
        """形態 B：route 上沒有 Access app。#740 修復前這一格是被 route 的 ACL 擋在
        teardown 之後；#1294 起 route 改以平台身分連帶刪除，擋點只剩前置檢查。"""
        home = self._mk_with_route('740 A2', protected=False)
        client, res, _b, _a = self._denied_rpc(home, 'A2/route-plain')
        self._assert_rpc_denied(res, 'A2')
        self._assert_no_external_call(client, 'A2')

    def test_A3_access_teardown_is_not_pushed_when_denied(self):
        home = self._mk_with_route('740 A3', protected=True)
        client, res, _b, _a = self._denied_rpc(home, 'A3/route-access')
        self._assert_rpc_denied(res, 'A3')
        self._assert_no_external_call(client, 'A3')

    def test_A4_denied_delete_never_enters_the_teardown_savepoint(self):
        """前置檢查必須在 `try:`／savepoint **之外**：被拒時整條補償路徑不得被進入。

        判準不是「補償沒有留下痕跡」（`torn_down` 為空時補償本來就 no-op ⇒ 零鑑別
        力），而是「savepoint 裡的第一步 `_teardown_routes_access()` **一次都沒被
        呼叫**」。用形態 C（唯一一個那一步真的會對 CF 動手的形態）。
        """
        home = self._mk_with_route('740 A4', protected=True)
        seen = []
        registry_cls = type(self.env[_MODEL])
        original = registry_cls._teardown_routes_access

        def _spy(record, torn_down=None):
            seen.append(record.ids)
            return original(record, torn_down=torn_down)

        with patch.object(registry_cls, '_teardown_routes_access', _spy):
            client, res, _b, _a = self._denied_rpc(home, 'A4/savepoint')
        self._assert_rpc_denied(res, 'A4')
        self.assertEqual(
            seen, [],
            'A4：授權被拒時不得進入 savepoint —— `_teardown_routes_access()` '
            '被呼叫了 %r' % (seen,))
        self.assertEqual(
            self._reap_rows(), [],
            'A4：被拒的刪除不得在待回收帳本上留下任何一列（實留 %r）'
            % (self._reap_rows(),))
        self._assert_no_external_call(client, 'A4')

    def test_A5_action_delete_is_stopped_before_its_state_write(self):
        """`action_delete()` 那一層：前置檢查必須擋在 `state='deleting'` 之前。

        歸因用**被拒的操作名**：carol 對這台 SH 既沒有 write 也沒有 unlink 權
        （兩者都只有 owner/admin）⇒ 少了 `action_delete()` 第一行的前置檢查，她會
        先撞 `state` 那次 write 的 rule（操作名 write）——結果一樣是被拒、一樣零
        呼叫，但守住的不是同一件事。這裡要求擋下來的是 unlink、而且不是 write。
        """
        home = self._mk_with_route('740 A5', protected=True)
        client, res, before, after = self._denied_rpc(
            home, 'A5/action_delete', method='action_delete')
        text = self._assert_rpc_denied(res, 'A5')
        self.assertTrue(
            'unlink' in text or 'delete' in text.lower(),
            'A5：擋下來的必須是 unlink 授權（前置檢查），回 %r' % (text,))
        self.assertNotIn(
            "'write'", text,
            'A5：被 write 擋下＝前置檢查不在 `action_delete()` 的 state write '
            '之前，回 %r' % (text,))
        self._assert_no_external_call(client, 'A5')
        self._assert_row_untouched(before, after, 'A5')


# ---------------------------------------------------------------------- #
#  B 系列：授權被拒 ⇒ 零持久欄位變更                                       #
# ---------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestSh740NoFieldChangeWhenDenied(_Sh740Fixture):
    """被拒的 RPC 刪除：零持久欄位變更（裸 SQL 前後逐欄比對）。

    #740 修復前（當時經刪除端點）三條全紅，紅的欄位不同：B1／B2 的 `state` 被
    `action_delete()` 在 savepoint **之外**寫成 `'deleting'`；B3 另外有 route 的
    `access_app_id` 被清空、`access_protection_lost` 被打開（#794 補償在**獨立交易**
    寫的，回捲不掉）。#1294 起驅動面是 `call_kw unlink`（見檔頭），`state` 那一格
    改由 A5 守；B3 那兩個欄位在拿掉前置檢查之後照樣會被打開（D2）。
    """

    def test_B1_no_route_denied_delete_lands_no_field_change(self):
        home = self._mk_no_route('740 B1')
        _c, res, before, after = self._denied_rpc(home, 'B1/no-route')
        self._assert_rpc_denied(res, 'B1')
        self._assert_row_untouched(before, after, 'B1')

    def test_B2_with_route_denied_delete_lands_no_field_change(self):
        home = self._mk_with_route('740 B2', protected=False)
        _c, res, before, after = self._denied_rpc(home, 'B2/route-plain')
        self._assert_rpc_denied(res, 'B2')
        self._assert_row_untouched(before, after, 'B2')

    def test_B3_access_protection_is_not_lost_when_denied(self):
        """🔴 本題最嚴重的一格：被**拒絕**的刪除不得把這個 hostname 的登入強制關掉。"""
        home = self._mk_with_route('740 B3', protected=True)
        _c, res, before, after = self._denied_rpc(home, 'B3/route-access')
        self._assert_rpc_denied(res, 'B3')
        # 先逐欄點名那兩個欄位（失敗訊息要說得出是哪一格），再整包比對。
        self.assertEqual(
            [r['access_app_id'] for r in after['routes']],
            [r['access_app_id'] for r in before['routes']],
            'B3：被拒的刪除不得清掉 route 的 `access_app_id`'
            '（CF 上的 Access app 已經被拆掉了，這是那件事的 DB 痕跡）')
        self.assertEqual(
            [r['access_protection_lost'] for r in after['routes']],
            [r['access_protection_lost'] for r in before['routes']],
            'B3：被拒的刪除不得打開 `access_protection_lost`'
            '（打開＝平台自己承認這一列宣稱的保護沒有生效）')
        self._assert_row_untouched(before, after, 'B3')


# ---------------------------------------------------------------------- #
#  C 系列：陽性對照 —— 有權限的身分走同一條路徑必須照常成功                 #
# ---------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestSh740AuthorizedPathStillWorks(_Sh740Fixture):
    """少了這一系列，「守住了」與「這條路徑在本測試環境根本不作用」不可區分。

    三條修復前後都必須綠——它們同時是 §3.4「提權路徑的行為不得改變」的釘子。
    """

    def _sudo_delete(self, home, tag):
        client = self._client()
        home_id = home.id
        with self._patched_operator(client):
            home.sudo().action_delete()
        after = self._db(home_id)
        self._report('SUDO %s' % tag, {
            'external_calls_in_order': self._external_calls(client),
            'db_after': after})
        self.assertEqual(after['smart_home'], [],
                         '%s：提權身分必須真的把記錄刪掉' % tag)
        return client

    def test_C1_sudo_no_route_still_deletes_the_tunnel(self):
        home = self._mk_no_route('740 C1')
        tunnel_id = home.sudo().tunnel_id
        tunnel_name = home.sudo().tunnel_name
        client = self._sudo_delete(home, 'C1/no-route')
        client.delete_tunnel.assert_called_once_with(
            tunnel_id, tunnel_name=tunnel_name)

    def test_C2_sudo_with_route_still_deletes_route_and_tunnel(self):
        home = self._mk_with_route('740 C2', protected=False)
        tunnel_id = home.sudo().tunnel_id
        route_ids = home.sudo().route_ids.ids
        client = self._sudo_delete(home, 'C2/route-plain')
        self.assertEqual(
            [c.args[0] for c in client.delete_tunnel.call_args_list],
            [tunnel_id], 'C2：tunnel 必須照常被拆')
        self.env.invalidate_all(flush=False)
        self.assertFalse(
            self.env[_ROUTE_MODEL].sudo().browse(route_ids).exists(),
            'C2：route 必須跟著被刪掉（%r 還在）' % (route_ids,))

    def test_C3_sudo_with_access_pushes_teardown_before_the_tunnel(self):
        """順序也是斷言對象：Access 必須在 tunnel **之前**拆（#785 C2 的性質）。"""
        home = self._mk_with_route('740 C3', protected=True)
        app_id = home.sudo().route_ids.access_app_id
        self.assertTrue(app_id, 'C3 前提：route 上要有 app id')
        client = self._sudo_delete(home, 'C3/route-access')
        ordered = [c.split('(', 1)[0] for c in self._external_calls(client)]
        self.assertEqual(
            ordered, ['sync_access_app', 'delete_tunnel'],
            'C3：兩個外部呼叫都要送出，且 Access 在 tunnel 之前（實際 %r）'
            % (ordered,))
        self.assertEqual(
            client.sync_access_app.call_args.args[0]['existing_app_id'],
            app_id, 'C3：送出去的必須是這一列上的那顆 app id')

    def test_C4_platform_driven_workspace_delete_is_unaffected(self):
        """平台自己驅動的刪除（`workspace.unlink()` 那條 `sudo().unlink()`）必須是 no-op。

        與 C1-C3 是不同的呼叫端，不是重複：那三條走 `action_delete()`，這條走
        `workspace.unlink()` 內部的 `search(...).unlink()`。
        """
        ws = self.Workspace.with_user(self.alice).create({'name': 'W3 740'})
        home = self._mk_with_route('740 C4', protected=False, workspace=ws)
        home_id = home.id
        client = self._client()
        with self._patched_operator(client):
            ws.sudo().unlink()
        self.assertEqual(
            self._db(home_id)['smart_home'], [],
            'C4：平台驅動的 workspace 刪除必須照常把 SH 一起刪掉')
        self.assertTrue(client.delete_tunnel.called,
                        'C4：tunnel 必須照常被拆（沒拆＝這條路徑沒真的跑）')


# ---------------------------------------------------------------------- #
#  D 系列：注入紅燈 —— 證明擋下來的真的是新守衛                            #
# ---------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestSh740GuardIsWhatStopsIt(_Sh740Fixture):
    """把前置檢查換成 no-op，外部副作用必須整個回來。

    #1294 重新錨定：同一個被拒身分（carol）、同一條 `call_kw unlink`。拿掉前置
    檢查之後，她一路走進 savepoint、對 CF 送出 teardown／delete，最後才在
    `super().unlink()` 被 record rule 擋下——**授權照樣被拒，副作用已經發生**，
    那正是 #740 的缺陷形狀。⇒ A／B 兩系列的鑑別力來源就是這兩條，**不要改弱、
    更不要刪**（少了它們，A／B 全綠也可能只是「這條路徑在本環境不作用」）。

    ⚠ 舊版（經刪除端點、alice）還釘了 `state='deleting'` 與 #859／#860 的行為。
    #860 已落地；#859 的可達格（清錨點撞 `tunnel_token` 欄位 ACL）由 #1294 改成
    平台身分寫；`state` 那一格改由 A5 守。
    """

    def _without_the_preflight(self):
        return patch.object(type(self.env[_MODEL]),
                            '_assert_unlink_authorized', lambda record: None)

    def test_D1_without_the_preflight_the_tunnel_is_deleted_again(self):
        home = self._mk_no_route('740 D1')
        with self._without_the_preflight():
            client, res, _b, _after = self._denied_rpc(home, 'D1')
        self.assertIn('error', res,
                      'D1：拿掉前置檢查之後授權仍然要被拒（擋在 super().unlink()），'
                      '回 %r' % (res,))
        self.assertTrue(
            client.delete_tunnel.called,
            'D1：拿掉前置檢查之後 `delete_tunnel` 必須又被送出——沒有的話，'
            '「守住了」的功勞不在這道守衛身上，本檔的 A 系列零鑑別力')

    def test_D2_without_the_preflight_the_access_app_is_torn_down_again(self):
        """判準是「teardown 真的送出去了、拆的是這一列的 app」。

        ⚠ 舊版另外斷言 `access_protection_lost=True`（#794 補償的 DB 痕跡）。#1294
        之後被拒的位置移到 tunnel 刪除之後的 `super().unlink()`，補償接著跑的
        `_resync_after_fail_open()` 對 mock operator 會重推成功、把旗標自己清掉
        （CI run 12142 實測為 False）——那是補償機制的收斂行為，不是本題的訊號，
        所以改錨在外部呼叫本身。
        """
        home = self._mk_with_route('740 D2', protected=True)
        app_id = home.sudo().route_ids.access_app_id
        self.assertTrue(app_id, 'D2 前提：route 上要有 app id')
        with self._without_the_preflight():
            client, res, _b, _after = self._denied_rpc(home, 'D2')
        self.assertIn('error', res,
                      'D2：拿掉前置檢查之後授權仍然要被拒，回 %r' % (res,))
        teardowns = [c.args[0] for c in client.sync_access_app.call_args_list
                     if c.args and c.args[0].get('enabled') is False]
        self.assertTrue(
            any(p.get('existing_app_id') == app_id for p in teardowns),
            'D2：拿掉前置檢查之後，這一列的 Access app 必須又被拆掉'
            '（實送 teardown %r）' % (teardowns,))


# ---------------------------------------------------------------------- #
#  E 系列：既有 fail-loud orphan guard 的語意逐字不動                      #
# ---------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestSh740FailLoudOrphanGuardUnchanged(_Sh740Fixture):
    """`test_smart_home.py` 那三條鎖的是型別與列存活，**沒有**鎖措辭。

    本 PR 在同一個 method 裡加東西 ⇒ 把措辭與觸發條件補釘住。修復前後都必須綠。
    """

    def test_E1_tunnel_teardown_failure_still_fails_loud_with_the_same_message(self):
        """⚠ 這條把 fail-loud 的措辭**逐字**釘住，而那句話已經開單要改（**#859**：
        `_cleanup_tunnel` 把授權錯誤吞成「保留待重試」之後，這句「the tunnel is
        still live and billed」在 tunnel 其實已被刪掉時是**假的**）。

        **這條紅了先確認是不是 #859 落地**；若是，把措辭斷言重新錨定到新文案，
        **不要改弱成只斷言例外型別**——型別那半 `test_smart_home.py` 已經有了，
        本條存在的唯一理由就是措辭與觸發條件不得被本 PR（或下一個改同一個 method
        的人）順手改掉。
        """
        from odoo.addons.woow_paas_platform.services.paas_operator import (
            PaaSOperatorError)
        home = self._mk_no_route('740 E1')
        home_id = home.id
        tunnel_id = home.sudo().tunnel_id
        client = self._client()
        client.delete_tunnel.side_effect = PaaSOperatorError(
            message='upstream 503')
        # 裸 try/except（不用 `assertRaises`）：例外之後還要驗 DB，而
        # `assertRaises` 的區塊離開時 savepoint 已經回捲，容易驗到回捲後的狀態。
        raised = None
        with self._patched_operator(client):
            try:
                home.sudo().action_delete()
            except UserError as e:
                raised = e
            else:
                self.fail('E1：tunnel 拆不掉時必須 fail-loud（沒有 raise）')
        self.assertIn(
            'Cloudflare cleanup failed; the tunnel is still live and billed.',
            str(raised),
            'E1：既有 fail-loud 的措辭不得變樣（回 %r）' % (str(raised),))
        after = self._db(home_id)
        self.assertTrue(after['smart_home'], 'E1：列必須保住當重試錨點')
        self.assertEqual(
            [r['tunnel_id'] for r in after['smart_home']], [tunnel_id],
            'E1：`tunnel_id` 必須保住當重試錨點')


# ---------------------------------------------------------------------- #
#  F 系列：守衛必須長在 model 層（`unlink()`），不是只長在端點層           #
# ---------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestSh740UnlinkIsWhereTheInvariantLives(_Sh740Fixture):
    """A／B 兩系列全部經 `action_delete()`（真 HTTP 端點）驅動 ⇒ 一個**只在
    `action_delete()` 做前置檢查、`unlink()` 完全不動**的實作可以讓它們全綠——
    而那個實作錯在本題的核心：`unlink()` 才是這條不變式所在的地方，它有**繞得過
    `action_delete()` 的呼叫端**（`workspace.unlink()` 的 cascade、任何直接拿
    recordset 呼叫 `unlink()` 的碼、未來的新端點）。本系列直接驅動 `unlink()`。

    ⚠ **必須用 `with_user()`，不能用預設的測試 env**：後者 `su=True`，
    `check_access()` 第一行就 return ⇒ 守衛在它底下是 no-op，測了等於沒測
    （`docs/decisions/patterns/su-env-exempt-guard-false-green.md`）。下面的
    `assertFalse(env.su)` 把這個前提釘住。

    ⚠ 這**不取代** `action_delete()` 那一層的檢查，兩層守的性質不同：
    `unlink()` 守的是 model 不變式（外部副作用不得先於授權，對所有呼叫端）；
    `action_delete()` 守的是「還不知道能不能做，就不要先改狀態」——`state='deleting'`
    那次 write 發生在 `unlink()` **被呼叫之前**，model 層的守衛救不到它（B1／B2）。

    歸因判準用**結構性訊號**（savepoint 的第一步 `_teardown_routes_access()` 一次都
    沒被呼叫），不是字串：字串會被 #859／#860 改掉，這個訊號不會。
    """

    def _tenant_unlink(self, home, tag):
        client = self._client()
        before = self._db(home.id)
        seen = []
        registry_cls = type(self.env[_MODEL])
        original = registry_cls._teardown_routes_access

        def _spy(record, torn_down=None):
            seen.append(record.ids)
            return original(record, torn_down=torn_down)

        as_tenant = home.with_user(self.carol)
        self.assertFalse(
            as_tenant.env.su,
            '%s fixture 前提：驅動身分必須是非提權的，否則守衛整條被跳過' % tag)
        raised = None
        with self._patched_operator(client), \
                patch.object(registry_cls, '_teardown_routes_access', _spy):
            try:
                as_tenant.unlink()
            except Exception as e:  # noqa: BLE001 —— 型別在下面斷言
                raised = e
            else:
                self.fail('%s：%s 直接 `unlink()` 必須被拒（只有 owner/admin '
                          '刪得掉，#1294）' % (tag, _DENIED_ROLE_LABEL))
        after = self._db(home.id)
        self._report('TENANT-UNLINK %s' % tag, {
            'raised': '%s: %s' % (type(raised).__name__, raised),
            'external_calls_in_order': self._external_calls(client),
            'teardown_entered_with': seen,
            'db_before': before, 'db_after': after,
            'reap_ledger': self._reap_rows(),
        })
        return client, raised, seen, before, after

    def _assert_stopped_before_the_savepoint(self, client, raised, seen, tag):
        self._assert_no_external_call(client, tag)
        self.assertEqual(
            seen, [],
            '%s：`unlink()` 被拒時不得進入 savepoint —— `_teardown_routes_access()` '
            '被呼叫了 %r（＝守衛不在 model 層，或不在 try 之外）' % (tag, seen))
        self.assertIsInstance(
            raised, AccessError,
            '%s：被拒的原因必須是授權，而不是 tunnel fail-loud guard 那個'
            '（修前無 route 形態就是被後者擋的，而它那句話是假的）。實際 %s: %s'
            % (tag, type(raised).__name__, raised))

    def test_F1_no_route_direct_unlink_by_tenant_sends_nothing(self):
        home = self._mk_no_route('740 F1')
        client, raised, seen, before, after = self._tenant_unlink(
            home, 'F1/no-route')
        self._assert_stopped_before_the_savepoint(client, raised, seen, 'F1')
        self._assert_row_untouched(before, after, 'F1')

    def test_F2_with_route_direct_unlink_by_tenant_sends_nothing(self):
        home = self._mk_with_route('740 F2', protected=False)
        client, raised, seen, before, after = self._tenant_unlink(
            home, 'F2/route-plain')
        self._assert_stopped_before_the_savepoint(client, raised, seen, 'F2')
        self._assert_row_untouched(before, after, 'F2')

    def test_F3_with_access_direct_unlink_by_tenant_sends_nothing(self):
        home = self._mk_with_route('740 F3', protected=True)
        client, raised, seen, before, after = self._tenant_unlink(
            home, 'F3/route-access')
        self._assert_stopped_before_the_savepoint(client, raised, seen, 'F3')
        self._assert_row_untouched(before, after, 'F3')

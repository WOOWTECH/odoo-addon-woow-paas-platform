# -*- coding: utf-8 -*-
"""#658：`woow_paas_platform.workspace`（workspace **本體**）的寫入授權。

#556 收窄的是 `workspace_access`（成員關係列），**沒有動到被保護的資源本身**。
本體這一側直到本 PR 之前只有一條 `workspace_rule_member_access`：
`perm_read` + `perm_write` 合一、domain `[('access_ids.user_id', '=', user.id)]`、
**完全沒有 role 條件** ⇒ 任何 guest／user 成員都能寫 workspace 本體。

## issue #658 本文的清單是不完整的

本文寫「能改 name / description / state（含 archive）」。實測（本檔 X 系列）還包括：

- **`owner_id`** —— 它不是顯示欄位，是**計費與配額的錨**：
  `paas.py::_create_and_confirm_subscription` 用 `workspace.owner_id.partner_id`
  當訂閱帳單 partner（`security_access.py` / `smart_home.py` 同型）；
  `cloud_service.py` 的 `max_per_partner` 上限與 `_find_active_for_partner` 都以
  `workspace_id.owner_id.partner_id` 計數；`paas.py::_delete_workspace` 的授權判準
  就是 `workspace.owner_id.id != user.id`。
- **`slug`** —— 本 PR 當時它是 K8s namespace 與公開 subdomain 的唯一輸入
  （`src/services/naming.py::make_namespace` / `make_service_subdomain`），而兩者
  都是 `md5(slug)[:8]` ＝ **32 bit 截斷**，`UNIQUE(slug)` 只擋得住字面相同的
  slug，擋不住兩個不同 slug 映到同一個 namespace。
  📌 **#677 之後這段是歷史**：namespace／subdomain 改吃 stored `ws_hash`
  （伺服器產生 ＋ DB `UNIQUE`），`slug` 只剩公開識別字的角色。錨死 slug 的理由
  也隨之改成「它是唯一識別字、改寫可以踩掉別人的重試路徑（#525 A2）」。
- **`access_ids` / `service_ids` 兩個 One2many** —— 對 workspace 寫 o2m command
  會穿透到子模型。

## 兩層修法（缺一層就是只修一半）

- **Layer 1（ir.rule）**：讀寫拆開。`workspace_rule_member_read`（全體成員讀）＋
  `workspace_rule_owner_admin_manage`（owner/admin 讀寫）＋
  `workspace_rule_anyone_create`＋`workspace_rule_owner_unlink`（縱深防禦，
  ACL 今天仍擋 unlink）。舊的兩條由 `src/migrations/18.0.1.0.129/post-migrate.py`
  DELETE 退役（`ir_model_data.noupdate` 是 sticky 的，改 XML 修不動）。
- **Layer 2（model 層 identity anchor）**：只有 Layer 1 的話，**admin 仍然能改
  `owner_id`** ⇒ admin 可以把擁有權連同計費歸屬搶走，且完全繞過
  `transfer_ownership()`。`workspace.py` 的 `write()` 錨住 `owner_id` / `slug`；
  `create()` 同樣錨住（見下方「為什麼 create 方向也要守」）。

## 為什麼 create 方向也要守（X7-X10）

`create()` 原本 `if not vals.get('slug')` 才產生，呼叫端自帶 slug 就照收，而
`create` 對租戶是開放的（ACL create=1 ＋ `workspace_rule_anyone_create` domain
`[(1,'=',1)]`）。攻擊者用 `/web/dataset/call_kw` 直接 create 一個 slug 撞到受害者
namespace 的 workspace，之後在自己的 workspace 部署服務就會落進受害者的 K8s
namespace。X7-X10、X14 關掉的是「呼叫端**直接指定** slug／owner_id」這一族通道。

🔴 **但「slug 由伺服器決定」這句話即使加了 create 守衛也仍然是假的**，不要那樣讀：
`_generate_slug()` 對 `[a-z0-9_-]` 字串是恆等函式，租戶把同一個字串放進 `name`
就得到逐字相同的 slug（`name` 本來就該由租戶自由指定）。這條通道**本 PR 沒有關**，
它是先前就存在的，修它要動 slug 生成或 namespace 推導（會波及既有部署與 #525 A2
已鎖死的行為）⇒ 另開 issue。守門測試見 `test_X8b_*`。

而 create 方向的守衛**不能看 raw vals**：context 的 `default_<field>` 是 RPC
用戶端可控的（`call_kw` 整份取代 context、不是 merge），default 要到
`_prepare_create_values` → `_add_missing_default_values` → `default_get` 才補上
（`default_get` 第一順位就是讀 `default_<field>`）。X9/X10 覆蓋這個形態
（#657 F2 / #664 的同型；本 repo 已經在同一行踩過兩次）。

⚠ 兩者的鑑別對象**不同**，實測分辨過，不要混為一談：
- **X10（`default_owner_id`）在 develop 上真的打得穿**（實測：uid=1 的呼叫下
  `default_owner_id=2` 落地成 `owner_id=2`）——那是一條活的洞。
- **X9（`default_slug`）在 develop 上打不穿**：develop 的 `create()` 在
  `super().create()` 之前就把 `vals['slug']` 填好了，context 補不進來。X9 鑑別的是
  **本 PR 的守衛讀 `resolved` 還是讀 raw `vals`**——把它改回 `vals`，X9 是唯一會紅的。

## TDD 紀律（照抄 #556 / #657，一字不可省）

- **全部走真 HTTP RPC**：`/web/dataset/call_kw` 或 `/api/workspaces/*` 端點。
  **禁止 `TransactionCase`**——它的 env `uid == SUPERUSER_ID` ⇒
  `env.is_superuser()` 恆真 ⇒ `workspace.py` 兩道守衛的 deny 分支結構上不可達，
  恆綠且與修復好壞無關。本檔唯一的例外是 `test_P9_*`（測的正是
  `env.is_superuser()` 這個 HTTP 結構上不可能觸發的豁免分支本身）。
- fixture user 一律只掛 `base.group_user`。`test_P8_*` 是刻意的例外：它測的正是
  `base.group_system` 的 bypass 通道本身，用 `authenticate('admin','admin')`
  （uid 2、group_system、非 superuser）。
- **否定性斷言一律配陽性對照**，並且**逐條標明是哪一層擋的**：
  `_assert_model_guard_denied`（訊息帶 `workspace.py` 守衛的特徵字串）vs
  `_assert_ir_rule_denied`（AccessError 但**不是**上述特徵字串 ⇒ 框架的 ir.rule）。
  兩個 helper 互斥，所以「我以為是 model 擋的、其實是 ir.rule 擋的」會被抓出來。

## fixture 的一個刻意設計：W1 同時有 guest 與 admin

`workspace_rule_owner_admin_manage` 的 domain 若寫成點鏈
`[('access_ids.user_id','=',user.id), ('access_ids.role','in',[...])]`，在一個
workspace 有多列 access 時是**跨列**匹配（gary 那列滿足 user_id、dave 那列滿足
role）⇒ 等於沒有 role 條件。W1 刻意同時掛 gary(guest) 與 dave(admin)，所以
`test_X1_*` / `test_X2_*`（guest/user 寫入必須被擋）對這個寫錯的形態有鑑別力。
這比斷言 domain 的字面形狀強：對方把 domain 改寫成別的等價形態時，形狀斷言會
假紅，行為斷言不會。
"""
import json
import uuid

from odoo.tests.common import HttpCase, tagged

from odoo.addons.woow_paas_platform.models.workspace_access import (
    ROLE_ADMIN,
    ROLE_GUEST,
    ROLE_OWNER,
    ROLE_USER,
)
# `generate_ws_hash` ＝ **修復前**（#677 之前）的 namespace 推導公式。本檔只在
# 「記錄當初為什麼要錨死 slug」與「證明兩個 slug 在舊公式下相撞」時用它；今天的
# namespace 身分是 `workspace.ws_hash`，不經這支函式。
from odoo.addons.woow_paas_platform.services.naming import generate_ws_hash

# `workspace.py` 兩道守衛的訊息特徵字串。用它區分「model 層守衛擋的」與「ir.rule
# 擋的」——兩者在 RPC 上都是 AccessError，不分辨的話會把「其實是 ir.rule 擋的」
# 誤讀成「我的 anchor 有效」（#657 覆核抓到過的形態）。
GUARD_OWNER_NEEDLE = 'Cannot change the owner of workspace'
GUARD_SLUG_NEEDLE = 'Cannot change the slug of workspace'
GUARD_CREATE_SLUG_NEEDLE = 'Cannot supply a workspace slug'
GUARD_CREATE_OWNER_NEEDLE = 'Cannot create a workspace owned by another user'
ALL_GUARD_NEEDLES = (
    GUARD_OWNER_NEEDLE, GUARD_SLUG_NEEDLE,
    GUARD_CREATE_SLUG_NEEDLE, GUARD_CREATE_OWNER_NEEDLE,
)


def _new_group_user(env, name):
    """建一個**只有** `base.group_user` 的租戶帳號（可 `authenticate`）。"""
    suffix = uuid.uuid4().hex[:8]
    login = '%s_%s' % (name, suffix)
    user = env['res.users'].sudo().create({
        'name': '%s 658' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


@tagged('post_install', '-at_install')
class _Workspace658Fixture(HttpCase):
    """W1（alice owner、dave admin、carol user、gary guest）＋ W2（bob owner）＋
    mallory（不屬任何 workspace）。六人皆只掛 `base.group_user`。
    """

    def setUp(self):
        super().setUp()
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']

        self.alice, self.alice_login = _new_group_user(self.env, 'alice658')
        self.bob, self.bob_login = _new_group_user(self.env, 'bob658')
        self.carol, self.carol_login = _new_group_user(self.env, 'carol658')
        self.dave, self.dave_login = _new_group_user(self.env, 'dave658')
        self.gary, self.gary_login = _new_group_user(self.env, 'gary658')
        self.mallory, self.mallory_login = _new_group_user(self.env, 'mallory658')

        for u in (self.alice, self.bob, self.carol, self.dave, self.gary, self.mallory):
            self.assertFalse(
                u.has_group('base.group_system'),
                'fixture 前提：租戶只能有 base.group_user（%s 撞了）' % u.login)

        # 用 `with_user(alice)` 而不是 superuser env 建立：這條路徑本身就是
        # `create()` 新守衛的陽性對照（owner_id 走 default ⇒ 等於 env.uid、
        # slug 由 `_generate_slug` 產生），fixture 建得起來就已經證明合法建立
        # 沒有被打死。
        self.w1 = self.Workspace.with_user(self.alice).create({'name': 'W1 658'})
        self.w2 = self.Workspace.with_user(self.bob).create({'name': 'W2 658'})

        # W1 掛滿四種角色（見檔頭「fixture 的一個刻意設計」）。
        for user, role in ((self.dave, ROLE_ADMIN), (self.carol, ROLE_USER),
                           (self.gary, ROLE_GUEST)):
            self.WorkspaceAccess.sudo().create({
                'workspace_id': self.w1.id, 'user_id': user.id, 'role': role})

        self.assertEqual(
            self.w1.sudo().owner_id, self.alice,
            'fixture 前提：W1 的 owner 必須是 alice')
        self.assertEqual(
            sorted(self.w1.sudo().access_ids.mapped('role')),
            sorted([ROLE_OWNER, ROLE_ADMIN, ROLE_USER, ROLE_GUEST]),
            'fixture 前提：W1 必須同時掛 owner/admin/user/guest 四列'
            '（少了 admin 那列，跨列匹配的 domain 寫法就不會被 X1/X2 抓到）')

    # ------------------------------------------------------------------ #
    #  RPC helper                                                         #
    # ------------------------------------------------------------------ #
    def _call_kw(self, model, method, args, kwargs=None):
        """`/web/dataset/call_kw`——ORM 層攻擊必須走這條真實 RPC 路徑。"""
        body = json.dumps({
            'jsonrpc': '2.0', 'method': 'call',
            'params': {'model': model, 'method': method,
                       'args': args, 'kwargs': kwargs or {}},
        })
        resp = self.url_open(
            '/web/dataset/call_kw', data=body,
            headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        return resp.json()

    def _post_json(self, url, payload):
        """打 `/api/workspaces/*` 這種 `type="json"` 的產品端點。"""
        body = json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': payload})
        resp = self.url_open(
            url, data=body, headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        return resp.json()

    # ------------------------------------------------------------------ #
    #  斷言 helper —— 逐條標明是哪一層擋的                                 #
    # ------------------------------------------------------------------ #
    def _error_text(self, result):
        data = result.get('error', {}).get('data', {}) or {}
        return '%s %s' % (data.get('name', ''), data.get('message', ''))

    def _assert_model_guard_denied(self, result, needle, msg):
        """斷言被 `workspace.py` 的 model 層守衛擋下（不是 ir.rule）。"""
        self.assertIn('error', result, msg + '（沒有被擋，回 %r）' % result)
        text = self._error_text(result)
        self.assertIn('AccessError', text,
                      msg + '（期望 AccessError，回 %r）' % result)
        self.assertIn(needle, text,
                      msg + '（期望是 model 層守衛擋的，回 %r）' % result)

    def _assert_ir_rule_denied(self, result, msg):
        """斷言被 ir.rule（列層級規則）擋下，**不是**本 PR 的 model 層守衛。

        兩個 helper 互斥是刻意的：否則「我以為 anchor 生效、其實是 ir.rule 擋的」
        這種歸因錯誤會全程隱形，而修掉 anchor 之後測試照樣綠。
        """
        self.assertIn('error', result, msg + '（沒有被擋，回 %r）' % result)
        text = self._error_text(result)
        self.assertIn('AccessError', text,
                      msg + '（期望 AccessError，回 %r）' % result)
        for needle in ALL_GUARD_NEEDLES:
            self.assertNotIn(
                needle, text,
                msg + '（這條應該由 ir.rule 擋，但拿到的是 model 層守衛的訊息，'
                      '歸因錯了：%r）' % result)

    def _assert_ok(self, result, msg):
        self.assertNotIn('error', result, msg + '（回 %r）' % result)


@tagged('post_install', '-at_install')
class TestWorkspaceAttackVectors658(_Workspace658Fixture):
    """X 系列——攻擊向量。

    本 class 共 **15 條**：X1-X14 ＋ X8b。

    **實測 RED（`origin/develop` @ dfd991ba，只換測試檔）＝ X1-X10、X13、X14 會紅
    （12 條）；X11／X12／X8b 不會。**

    - X11／X12 是**回歸對照**，各自 docstring 標明對本 PR 零鑑別力（X11 由 #556 的
      子模型守衛擋、X12 由成員 domain 擋，兩者在修復前就是綠的）。
    - X8b 是**已知殘留缺口的守門測試**，它斷言的是今天仍然通的那條通道，兩邊都綠。

    不標的話，下一個人會把「攻擊測試全綠」讀成「這些攻擊都被本 PR 擋住」，而其中
    三條在修復前就已經是綠的。條數與紅綠歸屬都是實測填的，不是數 docstring 數出來的。
    """

    # ---------------------------------------------------------------- #
    #  Layer 1：ir.rule 把寫入收窄到 owner/admin（X1-X3）                 #
    # ---------------------------------------------------------------- #
    def test_X1_guest_cannot_write_workspace_name(self):
        """guest 成員改 workspace name —— 修復前成功（issue 本文那一條）。

        對「跨列匹配」的錯誤 domain 寫法有鑑別力：W1 同時掛著 dave(admin)，
        點鏈形態的 domain 會讓 gary 借 dave 那列的 role 過關。
        """
        self.authenticate(self.gary_login, self.gary_login)
        result = self._call_kw(
            'woow_paas_platform.workspace', 'write',
            [[self.w1.id], {'name': 'pwned by guest'}])
        self._assert_ir_rule_denied(result, 'guest 不得改 workspace name')
        self.assertEqual(
            self.w1.sudo().name, 'W1 658',
            '除了回錯誤，值也必須沒落地（守衛必須在 super().write() 之前）')

    def test_X2_user_cannot_archive_workspace(self):
        """role=user 成員 archive workspace（改 `state`）—— 修復前成功。"""
        self.authenticate(self.carol_login, self.carol_login)
        result = self._call_kw(
            'woow_paas_platform.workspace', 'write',
            [[self.w1.id], {'state': 'archived'}])
        self._assert_ir_rule_denied(result, 'role=user 不得 archive workspace')
        self.assertEqual(self.w1.sudo().state, 'active', 'state 不得落地')

    def test_X3_guest_cannot_seize_owner_id(self):
        """guest 把 `owner_id` 改成自己 —— 修復前成功。

        後果不是「改個名字」：`paas.py::_delete_workspace` 的授權判準就是
        `workspace.owner_id.id != user.id`，寫成自己就拿到 archive 權；
        訂閱帳單 partner 與 `max_per_partner` 配額也一起搬走。

        🔎 **實測的分層歸因（第一輪我寫錯了，被本檔的互斥 helper 抓出來）**：
        擋下這條的是 **Layer 2**，不是 Layer 1。`Workspace.write()` 的守衛跑在
        `super().write()` **之前**，而 ir.rule 的 `check_access` 在 `super().write()`
        內部——所以只要 payload 帶了 `owner_id`／`slug`，不論呼叫者是 guest 還是
        admin，一律是 model 層先拒。實測 traceback：`call_kw` →
        `workspace.py::write` → raise，`super().write()` 從未進入。

        ⇒ 推論：Layer 1 對這兩個欄位的 role 收窄在行為上**觀察不到**（Layer 2 永遠
        先贏）。Layer 1 不是多餘的——它守的是 `name` / `description` / `state` 與兩個
        o2m 反向通道（X1／X2／X11／X13），那些欄位 Layer 2 刻意不錨。不要因為
        「anchor 已經擋住了」就把 Layer 1 收窄簡化掉。
        """
        self.authenticate(self.gary_login, self.gary_login)
        result = self._call_kw(
            'woow_paas_platform.workspace', 'write',
            [[self.w1.id], {'owner_id': self.gary.id}])
        self._assert_model_guard_denied(
            result, GUARD_OWNER_NEEDLE, 'guest 不得搶 owner_id')
        self.assertEqual(self.w1.sudo().owner_id, self.alice, 'owner 不得被搶走')

    # ---------------------------------------------------------------- #
    #  Layer 2：model 層 identity anchor（X4-X6）                        #
    #  —— 這三條是「只做 Layer 1 就以為修好了」會漏掉的                   #
    # ---------------------------------------------------------------- #
    def test_X4_admin_cannot_seize_owner_id(self):
        """🔴 **admin** 把 `owner_id` 改成自己——Layer 1 放行，只有 Layer 2 擋得住。

        admin 通過 `workspace_rule_owner_admin_manage`（他本來就該能改
        name/description/state），所以這條攻擊在「只收窄 ir.rule」的修法下
        **完全不會變紅**。這也是繞過 `transfer_ownership()` 守衛的唯一路徑。
        """
        self.authenticate(self.dave_login, self.dave_login)
        result = self._call_kw(
            'woow_paas_platform.workspace', 'write',
            [[self.w1.id], {'owner_id': self.dave.id}])
        self._assert_model_guard_denied(
            result, GUARD_OWNER_NEEDLE, 'admin 不得搶 owner_id')
        self.assertEqual(self.w1.sudo().owner_id, self.alice, 'owner 不得被搶走')

    def test_X5_admin_cannot_repoint_slug_to_victim_namespace(self):
        """🔴 **admin** 改 `slug` —— Layer 1 放行，只有 Layer 2 擋得住。

        ⚠ **這條 docstring 的原始動機在 #677 修好後已經過期了**，改寫如下，不要照
        原樣讀：slug 曾經是 `make_namespace()` 的唯一輸入，兩個不同的 slug 字面值
        可以映到同一個 K8s namespace（`md5(slug)[:8]` ＝ 32 bit 截斷）。#677 之後
        namespace 由 stored `ws_hash` 決定，slug **不再**決定 namespace，所以本條
        現在守的是 slug 作為公開識別字／`UNIQUE(slug)` 佔位的 immutability
        （改寫別人的 slug 可以踩掉他的重試路徑，見 #525 A2）。

        下面保留的碰撞前提斷言改用 `generate_ws_hash`（＝**修復前**的推導公式），
        它記錄的是「當初為什麼要錨死 slug」這段歷史，不是今天的推導路徑。
        """
        # 歷史前提：修復前兩個不同 slug 真的會映到同一個 namespace。
        self.assertNotEqual('ws-83256', 'ws-137425')
        self.assertEqual(
            generate_ws_hash('ws-83256'), generate_ws_hash('ws-137425'),
            '前提（修復前的推導）：md5(slug)[:8] 是 32 bit 截斷，不同 slug 可映到'
            '同一 namespace。#677 之後 namespace 不再吃 slug，這條只記錄歷史動機')

        self.authenticate(self.dave_login, self.dave_login)
        result = self._call_kw(
            'woow_paas_platform.workspace', 'write',
            [[self.w1.id], {'slug': 'ws-137425'}])
        self._assert_model_guard_denied(
            result, GUARD_SLUG_NEEDLE, 'admin 不得改 slug')
        self.assertEqual(self.w1.sudo().slug, 'w1-658', 'slug 不得被改')

    def test_X6_admin_cannot_repoint_owner_id_via_o2m_on_access_ids(self):
        """admin 用 `access_ids` o2m command 改成員角色的同時搶 owner_id。

        單獨測 o2m 通道：payload 同時帶合法的 o2m 與非法的 owner_id，證明守衛
        不是靠「vals 只有一個鍵」這種偶然性擋下的。
        """
        gary_row = self.WorkspaceAccess.sudo().search([
            ('workspace_id', '=', self.w1.id), ('user_id', '=', self.gary.id)])
        self.assertTrue(gary_row, '前提：gary 在 W1 有 access 列')

        self.authenticate(self.dave_login, self.dave_login)
        result = self._call_kw(
            'woow_paas_platform.workspace', 'write',
            [[self.w1.id], {
                'owner_id': self.dave.id,
                'access_ids': [(1, gary_row.id, {'role': ROLE_USER})],
            }])
        self._assert_model_guard_denied(
            result, GUARD_OWNER_NEEDLE,
            'owner_id 與合法 o2m 同 payload 時仍須被擋')
        self.assertEqual(self.w1.sudo().owner_id, self.alice, 'owner 不得被搶走')
        self.assertEqual(
            gary_row.sudo().role, ROLE_GUEST,
            '整個 write 必須原子失敗，o2m 那半邊也不得落地')

    # ---------------------------------------------------------------- #
    #  create 方向的 anchor（X7-X10）                                    #
    # ---------------------------------------------------------------- #
    def test_X7_tenant_cannot_create_workspace_owned_by_victim(self):
        """租戶 create 時把 `owner_id` 指向別人 —— 修復前成功。

        後果：workspace 連同它的 owner access 列（`create()` 的 sudo bootstrap
        照 `workspace.owner_id` 建）被種到受害者帳號上，計入受害者的配額歸屬。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        result = self._call_kw(
            'woow_paas_platform.workspace', 'create',
            [{'name': 'planted on alice', 'owner_id': self.alice.id}])
        self._assert_model_guard_denied(
            result, GUARD_CREATE_OWNER_NEEDLE,
            'create 不得指定別人當 owner')
        self.assertFalse(
            self.Workspace.sudo().search([('name', '=', 'planted on alice')]),
            '記錄不得落地')

    def test_X8_tenant_cannot_supply_slug_on_create(self):
        """🔴 租戶 create 時自帶 `slug` —— 修復前成功。

        `create()` 原本是 `if not vals.get('slug')` 才產生 ⇒ 呼叫端自帶就照收。

        ⚠ **本條只證明「vals 的 `slug` 鍵這條通道被關掉」，不證明租戶無法選
        namespace。** 第一版的 docstring 寫「直接撞到受害者 namespace」，那是假的
        ——W1 的 slug 是 `w1-658`（當時的公式 `md5(slug)[:8]` → `paas-ws-b8182a2d`），
        而 payload 的 `ws-137425` → `paas-ws-7d82ce00`，兩者不相等；那句話沒有任何
        斷言支撐它。第一版還附了一句
        `assertTrue(victim_ns.startswith('paas-ws-'))` 當「前提」，但 namespace
        的回傳值恆以該前綴開頭 ⇒ **恆真式**，量不到任何東西。兩者都已移除。
        （上面兩個 `paas-ws-*` 實值是 #677 修法前的推導結果，留著是為了讓這段
        自我更正的敘事讀得懂；今天的 namespace 不再由 slug 決定。）

        真正的殘留缺口（`name` 鍵可推導出同樣的 slug）見
        `test_X8b_name_derives_the_slug_verbatim_documented_gap` 與
        `workspace.py::_check_tenant_create_anchors` 的 docstring。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        result = self._call_kw(
            'woow_paas_platform.workspace', 'create',
            [{'name': 'ns squatter', 'slug': 'ws-137425'}])
        self._assert_model_guard_denied(
            result, GUARD_CREATE_SLUG_NEEDLE, 'create 不得自帶 slug')
        self.assertFalse(
            self.Workspace.sudo().search([('name', '=', 'ns squatter')]),
            '記錄不得落地')

    def test_X8b_name_derives_the_slug_verbatim_documented_gap(self):
        """📌 **已知殘留缺口的守門測試**（不是攻擊測試，也不是回歸對照）。

        `_generate_slug()` 對 `[a-z0-9_-]` 字串是恆等函式，所以租戶把字串從
        `slug` 鍵搬到 `name` 鍵，就拿到逐字相同的 slug ——X8 關掉的通道旁邊還開著
        一條。本條把這個**現況**釘住，目的是：日後有人修好 slug 生成（讓它不再由
        租戶完全決定）時，這條會**變紅**，逼他回來更新
        `workspace.py::_check_tenant_create_anchors` 的 docstring 與相關 issue，
        而不是讓那段「殘留缺口」的說明默默過期成假話。

        📌 **#677 的更新（缺口的後果變了，機制沒變）**：`slug` 仍然由租戶完全決定
        （所以本條第一個斷言照舊、也照舊是綠的），但 namespace／公開子網域已經改
        由伺服器產生的 stored `ws_hash` 決定 ⇒ 租戶決定 slug 不再等於租戶決定
        namespace。原本第三個斷言 `make_namespace(ws.slug) == …`（＝「namespace
        完全由租戶提供的字串決定」）**在修法後為假**，換成下面那條反向斷言。

        ⚠ 這條刻意**不**斷言 namespace 真的撞上受害者（那要靠 preimage 搜尋，
        不是單元測試該做的事）。跨租戶碰撞的守門測試在
        `test_workspace_ns_hash_677.py`。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        result = self._call_kw(
            'woow_paas_platform.workspace', 'create',
            [{'name': 'ws-137425'}])
        self._assert_ok(result, 'name 這條通道今天是通的（這正是缺口本身）')
        ws = self.Workspace.sudo().browse(result['result'])
        self.assertEqual(
            ws.slug, 'ws-137425',
            '缺口的機制：slug 逐字等於租戶給的 name。這條變紅代表 slug 生成被改'
            '過了 —— 請一併更新 workspace.py 的 docstring 與 follow-up issue')
        # #677：同一個攻擊者再建一個「舊公式下與上面那個相撞」的 slug。斷言用
        # 兩列相異（由 `UNIQUE(ws_hash)` **確定性**保證），不用
        # `ws_hash != generate_ws_hash(slug)`——後者有 2^-32 的隨機假紅面。
        second = self._call_kw(
            'woow_paas_platform.workspace', 'create', [{'name': 'ws-83256'}])
        self._assert_ok(second, '第二個 workspace 也該建得起來')
        ws2 = self.Workspace.sudo().browse(second['result'])
        self.assertEqual(
            generate_ws_hash(ws.slug), generate_ws_hash(ws2.slug),
            '前提：這兩個 slug 在**修復前**的公式下相撞（否則下一條斷言無意義）')
        self.assertNotEqual(
            ws.ws_hash, ws2.ws_hash,
            '#677：namespace 身分不得再是租戶所提供字串的函數。這條變紅代表有人'
            '把 ws_hash 改回從 slug 推導 —— 那會把 #677 整條洞放回來')

    def test_X9_tenant_cannot_inject_slug_via_context_default(self):
        """🔴 `with_context(default_slug=...)` ＋ vals **不放** slug。

        這是 #657 F2／#664 的同型：context 的 `default_<field>` 是 RPC 用戶端
        可控的，而 default 是在 `super().create()` 內部才補上的。

        ⚠ **本條鑑別的是「我的守衛讀 `resolved` 還是讀 raw `vals`」，不是
        「develop 上這條通道打得穿」。** 實測（odoo shell，develop 源碼）：

            vals 不放 slug ＋ context default_slug='ws-137425' -> slug='x9-probe-a'

        也就是 develop 上 `default_slug` **不會落地**——develop 的 `create()` 在
        `super().create()` 之前就把 `vals['slug']` 填成由 name 推導的值，
        `_add_missing_default_values` 因此不會再從 context 補。所以本條在 develop
        上變紅是因為**整個守衛不存在**（create 直接成功），不是因為注入成功。
        對照組 `test_X10_*` 才是真的在 develop 上打得穿的那一半（實測
        `default_owner_id=2` 在 uid=1 的呼叫下確實落地成 owner_id=2）。

        留著它的理由：把 `resolved` 改回 `vals` 的那一天，這條會是唯一變紅的測試。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        result = self._call_kw(
            'woow_paas_platform.workspace', 'create',
            [{'name': 'ctx slug squatter'}],
            {'context': {'default_slug': 'ws-137425'}})
        self._assert_model_guard_denied(
            result, GUARD_CREATE_SLUG_NEEDLE,
            'context default_slug 必須與 vals 走同一道守衛')
        self.assertFalse(
            self.Workspace.sudo().search([('slug', '=', 'ws-137425')]),
            '該 slug 不得落地')

    def test_X10_tenant_cannot_inject_owner_via_context_default(self):
        """`with_context(default_owner_id=<受害者>)` ＋ vals **不放** owner_id。

        與 X9 同一個機制的另一半。兩個欄位都測，是因為 #657 的教訓正是
        「補好的那半會產出漂亮的 AccessError，讓人以為整條守住了」——
        `_add_missing_default_values` 這個單一真相源修法保證的是同一欄位不會漂，
        不是自動涵蓋所有欄位。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        result = self._call_kw(
            'woow_paas_platform.workspace', 'create',
            [{'name': 'ctx owner squatter'}],
            {'context': {'default_owner_id': self.alice.id}})
        self._assert_model_guard_denied(
            result, GUARD_CREATE_OWNER_NEEDLE,
            'context default_owner_id 必須與 vals 走同一道守衛')
        self.assertFalse(
            self.Workspace.sudo().search([('name', '=', 'ctx owner squatter')]),
            '記錄不得落地')

    def test_X14_tenant_cannot_smuggle_anchors_through_copy(self):
        """`copy()` 繞道——它底層走 `create()`，所以必須落在同一道守衛裡。

        `slug` 與 `owner_id` 都**沒有** `copy=False`（`workspace.py` 的欄位定義），
        所以 `copy_data()` 會把兩個 anchor 原樣帶進 create 的 vals。少了 create
        方向的守衛，「slug 由伺服器決定」在這條路徑上一樣是假的
        （同 #657 的 X7 形態）。

        歸因斷言很重要：develop 上這條**也會出錯**，但錯的是
        `UNIQUE(slug)` 撞出來的 IntegrityError，不是授權拒絕。只斷言「有 error」
        的測試在 develop 上會假綠。
        """
        self.authenticate(self.gary_login, self.gary_login)
        result = self._call_kw(
            'woow_paas_platform.workspace', 'copy',
            [[self.w1.id], {'name': 'copied W1'}])
        self._assert_model_guard_denied(
            result, GUARD_CREATE_SLUG_NEEDLE,
            'copy() 必須與 create() 走同一道守衛')
        self.assertFalse(
            self.Workspace.sudo().search([('name', '=', 'copied W1')]),
            '複本不得落地')

    # ---------------------------------------------------------------- #
    #  o2m 反向通道（X11 / X13）                                         #
    # ---------------------------------------------------------------- #
    def test_X11_guest_cannot_grant_self_owner_via_access_ids_o2m_regression(self):
        """guest 透過 workspace 的 `access_ids` o2m 幫自己新增一列 owner。

        ⚠ **這條在 `origin/develop` 上就是綠的（＝已擋著），對本 PR 零鑑別力**
        ——實測 RED 輪確認（11 紅裡沒有它）。擋它的是 #556 給
        `WorkspaceAccess.create()` 的 `_check_assignable_role`（grantor=guest ⇒
        assignable=[] ⇒ AccessError），也就是**子模型那一側**，不是 #658 的任何
        改動。留著是回歸對照：`access_ids` 這條反向通道的後果由子模型守衛決定，
        本 PR 只是讓它在 workspace 這一層也走不到。

        ⚠ 連帶：`_assert_ir_rule_denied` 對這條只能證明「不是 workspace.py 的
        anchor 擋的」，無法分辨 ir.rule 與 workspace_access 的 model 守衛——
        兩者在 develop／branch 上還是不同的那一層。這個侷限寫在這裡，不要把它
        讀成「已完成分層歸因」。
        """
        self.authenticate(self.gary_login, self.gary_login)
        # 可達性陽性對照：guest 讀得到 W1（read 方向刻意沒被收窄）。
        reachable = self._call_kw(
            'woow_paas_platform.workspace', 'read',
            [[self.w1.id], ['name']])
        self._assert_ok(
            reachable, '可達性前提：guest 必須讀得到 W1，否則下面的否定斷言無意義')

        result = self._call_kw(
            'woow_paas_platform.workspace', 'write',
            [[self.w1.id], {'access_ids': [(0, 0, {
                'user_id': self.gary.id, 'role': ROLE_OWNER})]}])
        self._assert_ir_rule_denied(
            result, 'guest 不得經 workspace 的 o2m 反向通道自封 owner')
        self.assertEqual(
            len(self.w1.sudo().access_ids), 4,
            '不得多出一列 access（W1 應維持 owner/admin/user/guest 四列）')

    def test_X13_guest_cannot_mutate_cloud_service_via_service_ids_o2m(self):
        """🔴 guest 透過 workspace 的 `service_ids` o2m 改子模型 —— 修復前成功。

        與 X11 相反，這條**真的有鑑別力**：`cloud_service_rule_workspace_member`
        的 domain 是 `[('workspace_id.access_ids.user_id', '=', user.id)]`，
        **沒有 role 條件**（perm read/write/create/unlink 全 True）⇒ 子模型那一側
        對 guest 完全不設限，develop 上整條 o2m 打得穿。修復後由 workspace 這一層
        擋下。

        歸因斷言（`workspace` 而不是 `cloud_service`）是刻意的：證明擋下來的是
        **workspace 的 ir.rule**，不是子模型碰巧也擋。

        ⚠ 本 PR 只關掉 workspace 這條反向通道，**沒有**修 `cloud_service` 自己那條
        規則——guest 直接對 `cloud_service` 下手的路徑仍然開著。那是不同的爆炸半徑
        （工單 §9 明列不在本單範圍），另開 issue 處理。這裡刻意**不**寫一條斷言
        「guest 直接寫 cloud_service 會成功」的測試：那等於把現存的洞寫死成契約，
        修它的人會先撞到一條紅的測試。
        """
        template = self.env['woow_paas_platform.cloud_app_template'].sudo().create({
            'name': 'Tmpl 658', 'slug': 'tmpl-658-%s' % uuid.uuid4().hex[:6],
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'tmpl-658', 'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        svc = self.env['woow_paas_platform.cloud_service'].sudo().create({
            'name': 'Svc in W1 658',
            'workspace_id': self.w1.id, 'template_id': template.id,
        })

        self.authenticate(self.gary_login, self.gary_login)
        reachable = self._call_kw(
            'woow_paas_platform.workspace', 'read', [[self.w1.id], ['name']])
        self._assert_ok(reachable, '可達性前提：guest 必須讀得到 W1')

        result = self._call_kw(
            'woow_paas_platform.workspace', 'write',
            [[self.w1.id], {'service_ids': [(1, svc.id, {'name': 'pwned via o2m'})]}])
        self._assert_ir_rule_denied(
            result, 'guest 不得經 workspace 的 service_ids o2m 通道改子模型')
        text = self._error_text(result)
        self.assertIn(
            'woow_paas_platform.workspace', text,
            '歸因：擋下來的必須是 workspace 這一層（回 %r）' % result)
        self.assertEqual(
            svc.sudo().name, 'Svc in W1 658', '子模型的值不得落地')

    # ---------------------------------------------------------------- #
    #  非成員（X12）—— 修復前就擋著，純回歸對照                           #
    # ---------------------------------------------------------------- #
    def test_X12_non_member_cannot_write_workspace_regression(self):
        """完全不是成員的人寫 W1。

        ⚠ 這條在 `origin/develop` 上**本來就是紅的（＝擋著的）**，對本 PR 的
        修復零鑑別力，留著純粹是防止收窄 domain 時把成員判準一起弄丟的回歸對照。
        本檔其餘 X 系列才是真正的 RED。
        """
        self.authenticate(self.mallory_login, self.mallory_login)
        result = self._call_kw(
            'woow_paas_platform.workspace', 'write',
            [[self.w1.id], {'name': 'outsider'}])
        self._assert_ir_rule_denied(result, '非成員不得寫 W1')


@tagged('post_install', '-at_install')
class TestWorkspacePositiveControls658(_Workspace658Fixture):
    """P 系列——證明修復沒有把合法路徑打死（over-correction 對照）。"""

    def test_P1_admin_updates_name_via_product_endpoint(self):
        """🟢 工單指定的陽性對照：admin 走 `/api/workspaces/<id>`（`_update_workspace`，
        已要求 `required_role=ROLE_ADMIN`）改 name/description 必須**仍然成功**。
        """
        self.authenticate(self.dave_login, self.dave_login)
        result = self._post_json(
            '/api/workspaces/%d' % self.w1.id,
            {'action': 'update', 'name': 'W1 renamed by admin',
             'description': 'by dave'})
        self._assert_ok(result, 'admin 走產品端點改名必須成功')
        self.assertTrue(result['result'].get('success'), result)
        self.assertEqual(self.w1.sudo().name, 'W1 renamed by admin')
        self.assertEqual(self.w1.sudo().description, 'by dave')

    def test_P2_owner_updates_name_via_product_endpoint(self):
        """🟢 owner 走同一個端點（角色階序 index(owner) >= index(admin)）。"""
        self.authenticate(self.alice_login, self.alice_login)
        result = self._post_json(
            '/api/workspaces/%d' % self.w1.id,
            {'action': 'update', 'name': 'W1 renamed by owner'})
        self._assert_ok(result, 'owner 走產品端點改名必須成功')
        self.assertTrue(result['result'].get('success'), result)
        self.assertEqual(self.w1.sudo().name, 'W1 renamed by owner')

    def test_P3_admin_writes_name_via_orm(self):
        """🟢 admin 直接走 ORM 改 name —— 證明 ir.rule 的 manage 條真的放行 admin
        （不是靠 controller 層才過的）。"""
        self.authenticate(self.dave_login, self.dave_login)
        result = self._call_kw(
            'woow_paas_platform.workspace', 'write',
            [[self.w1.id], {'name': 'admin orm write'}])
        self._assert_ok(result, 'admin 直接 ORM 改 name 必須成功')
        self.assertEqual(self.w1.sudo().name, 'admin orm write')

    def test_P4_owner_archives_via_product_endpoint(self):
        """🟢 owner 走 `_delete_workspace`（soft delete ＝ `action_archive()` →
        `write({'state': 'archived'})`）必須仍然成功。"""
        self.authenticate(self.alice_login, self.alice_login)
        result = self._post_json(
            '/api/workspaces/%d' % self.w1.id, {'action': 'delete'})
        self._assert_ok(result, 'owner archive 必須成功')
        self.assertTrue(result['result'].get('success'), result)
        self.assertEqual(self.w1.sudo().state, 'archived')

    def test_P5_guest_and_user_can_still_read(self):
        """🟢 讀方向刻意**沒有**收窄：guest / user 仍看得到自己的 workspace。

        這條防的是「把 member_read 也收窄到 owner/admin」這種 over-correction
        ——那會讓一般成員的整個 `/woow` 前台空掉。
        """
        for login in (self.gary_login, self.carol_login):
            self.authenticate(login, login)
            result = self._call_kw(
                'woow_paas_platform.workspace', 'read',
                [[self.w1.id], ['name', 'slug', 'state']])
            self._assert_ok(result, '%s 必須仍讀得到 W1' % login)
            self.assertEqual(result['result'][0]['name'], 'W1 658')

    def test_P6_owner_noop_write_of_anchors_is_not_blocked(self):
        """🟢 owner 把**未修改**的 anchor 值連同 name 一起送出（前端整批送出的形態）。

        守衛比對的是「新值 != 現值」而不是「key 有沒有出現」；寫成後者的話這條會紅。
        """
        current_owner = self.w1.sudo().owner_id.id
        current_slug = self.w1.sudo().slug
        self.authenticate(self.alice_login, self.alice_login)
        result = self._call_kw(
            'woow_paas_platform.workspace', 'write',
            [[self.w1.id], {'name': 'noop anchors',
                            'owner_id': current_owner,
                            'slug': current_slug}])
        self._assert_ok(result, 'owner 送出未修改的 anchor 值不得被誤擋')
        self.assertEqual(self.w1.sudo().name, 'noop anchors')

    def test_P7_clean_tenant_bootstrap_still_works(self):
        """🟢 乾淨的 `base.group_user` 自建 workspace（`_create_workspace` 路徑）：
        建得起來、slug 由伺服器產生、自己拿到 owner 列。"""
        self.authenticate(self.mallory_login, self.mallory_login)
        result = self._post_json(
            '/api/workspaces', {'action': 'create', 'name': 'Mallory WS'})
        self._assert_ok(result, '租戶自建 workspace 必須成功')
        self.assertTrue(result['result'].get('success'), result)
        ws = self.Workspace.sudo().browse(result['result']['data']['id'])
        self.assertEqual(ws.owner_id, self.mallory, '自建者必須是 owner')
        self.assertTrue(ws.slug, 'slug 必須由伺服器產生（不得留空）')
        row = self.WorkspaceAccess.sudo().search([
            ('workspace_id', '=', ws.id), ('user_id', '=', self.mallory.id)])
        self.assertEqual(row.role, ROLE_OWNER, 'bootstrap 的 owner 列必須建起來')

    def test_P8_group_system_member_may_change_owner_id(self):
        """🟢 `base.group_system`（uid 2、**非** superuser）的豁免通道。

        兩個前提要一起成立才走得到：(a) ir.rule ——本 PR 刻意不加
        `workspace_rule_admin_full`，所以平台管理員必須先是 owner/admin 成員；
        (b) model 層守衛 —— `has_group('base.group_system')` 豁免。
        少了 (b) 這條會紅，證明豁免分支真的在跑而不是死碼。
        """
        admin_user = self.env.ref('base.user_admin')
        self.assertTrue(admin_user.has_group('base.group_system'),
                        '前提：uid 2 必須有 group_system')
        self.WorkspaceAccess.sudo().create({
            'workspace_id': self.w1.id, 'user_id': admin_user.id,
            'role': ROLE_ADMIN})

        self.authenticate('admin', 'admin')
        result = self._call_kw(
            'woow_paas_platform.workspace', 'write',
            [[self.w1.id], {'owner_id': self.dave.id}])
        self._assert_ok(result, 'group_system 必須能改 owner_id（豁免通道）')
        self.assertEqual(self.w1.sudo().owner_id, self.dave)

    def test_P9_superuser_exemption_unrestricted(self):
        """🟢 `env.is_superuser()` 豁免——HTTP 結構上不可能觸發，只能直呼 ORM。

        用 `with_user(mallory).sudo()` 而不是裸 `self.env`：`HttpCase.self.env`
        本身就是 uid=1，直接 `.sudo()` 是 no-op，測到的會是「本來就是 superuser」
        而不是「`.sudo()` 這個切換動作」（#657 第二輪修訂踩過的用詞問題）。
        """
        ws = self.w1.with_user(self.mallory).sudo()
        ws.write({'owner_id': self.bob.id, 'slug': 'su-rewritten-658'})
        self.assertEqual(self.w1.sudo().owner_id, self.bob)
        self.assertEqual(self.w1.sudo().slug, 'su-rewritten-658')

    def test_P10_admin_invite_still_recomputes_member_count(self):
        """🟢 admin 邀請成員後 `member_count`（stored compute）仍然更新。

        `member_count` 的重算會對 workspace 本體產生一次寫入。它走的是 flush 期的
        低階 `_write`（不經公開 `write()`、不查 ir.rule），所以收窄寫入權限不該
        影響它——但這是**推論**，這條測試是它的實測對照。
        """
        before = self.w1.sudo().member_count
        self.authenticate(self.dave_login, self.dave_login)
        result = self._post_json(
            '/api/workspaces/%d/members' % self.w1.id,
            {'action': 'invite', 'email': self.mallory.email, 'role': ROLE_USER})
        self._assert_ok(result, 'admin 邀請成員必須成功')
        self.assertTrue(result['result'].get('success'), result)
        self.assertEqual(
            self.w1.sudo().member_count, before + 1,
            'member_count 這個 stored compute 必須仍會重算')


@tagged('post_install', '-at_install')
class TestWorkspaceRuleLifecycle658(HttpCase):
    """退役／新生的 ir.rule 在 DB 裡的實際狀態。

    ⚠ 這個 class 在 **fresh install** 上驗的是「XML 宣告面」；`ir_model_data.noupdate`
    的 sticky 語意只有在**真的裝過舊版再升級**的 DB 上才看得出來，那條路徑不是單元
    測試能涵蓋的（migration 只在 `-u` 跑），實測輸出見 PR 內文的升級路徑段落。
    這裡先斷言**前提**（新規則抓得到），再斷言缺席——否則「查不到」與「探針壞了」
    輸出一模一樣。
    """

    def test_new_rules_present_and_retired_absent(self):
        IMD = self.env['ir.model.data'].sudo()
        Rule = self.env['ir.rule'].sudo()

        expected = {
            'workspace_rule_member_read': (True, False, False, False),
            'workspace_rule_owner_admin_manage': (True, True, False, False),
            'workspace_rule_anyone_create': (False, False, True, False),
            'workspace_rule_owner_unlink': (False, False, False, True),
        }
        # 前提：四條新規則都在，而且 perm_* 是預期的形狀。抓不到就是探針壞了。
        for name, perms in expected.items():
            imd = IMD.search([('module', '=', 'woow_paas_platform'),
                              ('model', '=', 'ir.rule'), ('name', '=', name)])
            self.assertTrue(imd, '前提：新規則 %s 必須存在（抓不到＝探針壞了）' % name)
            rule = Rule.browse(imd.res_id)
            self.assertEqual(
                (rule.perm_read, rule.perm_write, rule.perm_create, rule.perm_unlink),
                perms, '%s 的 perm_* 形狀不對' % name)
            self.assertFalse(
                imd.noupdate,
                '%s 必須 noupdate=False，否則日後改不動（#556 的可修復性根因）' % name)

        # 前提成立之後，才輪到缺席斷言。
        for name in ('workspace_rule_member_access', 'workspace_rule_create'):
            self.assertFalse(
                IMD.search([('module', '=', 'woow_paas_platform'),
                            ('model', '=', 'ir.rule'), ('name', '=', name)]),
                '退役的 %s 不得存在（與新規則以 OR 語意併存＝修復靜默失效）' % name)

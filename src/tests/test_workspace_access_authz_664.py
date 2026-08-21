# -*- coding: utf-8 -*-
"""#664：`workspace_access.create()` 的 context-default 繞過——`#556`/`#657`
同型缺口的第三個著陸點，且本次有**兩個**同型半邊，都要補。

## 兩個半邊（都是 issue #664 補正留言，2026-08-11，重新界定過的 scope）

`create()`（`src/models/workspace_access.py`）修復前：

```python
effective_role = vals.get('role') or ROLE_USER
self._check_assignable_role(effective_role, vals.get('workspace_id'))
```

Odoo 的 default 是在 `super().create()` **內部**才補的（`BaseModel.create` →
`_prepare_create_values` → `_add_missing_default_values` → `default_get`，
`default_get` 第一順位是 context 的 `default_<field>`，`call_kw` 的 context
用戶端可整包替換，非合併）。兩個參數都只看 raw `vals`：

- **role 半邊**：`with_context(default_role='owner')` ＋ vals 不放 `role`
  ⇒ 守衛判到的是 `ROLE_USER`（放行），實際落地的是 `owner`。
- **workspace_id 半邊**：`with_context(default_workspace_id=W)` ＋ vals 不放
  `workspace_id` ⇒ `_check_assignable_role` 第一行 `if not workspace_id:
  return` ⇒ **整道守衛沒跑**（不管 role 是什麼值）。

修法照抄 `src/models/project_access.py::ProjectAccess.create`（#657 第二輪，
PR #666 留言 6583/6595 的正確樣板）：不逐欄位重演 Odoo 的解析順序（那正是
#657 第一版漏掉 `project_id` 半邊的形狀），改用 Odoo 自己那支解析函式一次
解析全欄位：

```python
resolved = self._add_missing_default_values(vals)
self._check_assignable_role(resolved.get('role') or ROLE_USER, resolved.get('workspace_id'))
```

## 本單特有難點：`_check_owner_count` 會「碰巧」擋住一部分攻擊

`workspace_access` 有 `@api.constrains('role', 'workspace_id')` 的
`_check_owner_count`（一個 workspace 只能有一個 owner）；`project_access`
沒有這道不變式。所以在**正常 workspace**（已有 owner 列）上，
`default_role='owner'` 之類的攻擊在修復前會被這道**不相干**的不變式意外擋
下——回應看起來是「錯誤」，但擋下它的不是本次要補的守衛，是
`ValidationError`（不變式），不是 `AccessError`（授權）。兩者必須可區分，
否則會誤判「這條原本就是綠的、fix 沒有效果」。

⇒ 攻擊測試依前提分兩組，每個半邊各一對：

| ID | 前提 | pre-fix | post-fix | 說明 |
| --- | --- | --- | --- | --- |
| **Y0** | 正常 workspace（有 owner） | `ValidationError`（`_check_owner_count`「碰巧」擋下） | `AccessError`（`_check_assignable_role`） | role 半邊。斷言**例外型別**——pre-fix 拿到的是 ValidationError，`assertRaises`-等價的型別檢查會紅，這就是合格的 RED |
| **Y1** | orphan workspace（無 owner 列） | 攻擊成功，真的多一列 role=owner | `AccessError` | role 半邊，真正的提權 RED（不變式的「碰巧」保護在這裡失守，因為 owner_count 真的是 0） |
| **Y2**（normal / orphan 各一條） | 同上兩組，走 workspace_id 半邊 | 同上 | `AccessError` | workspace_id 半邊，`vals.get('workspace_id')` 為 `None` 時整道守衛**不管 role 是什麼**都不執行 |
| **Y3** | 對照組：role／workspace_id 全部明寫在 vals，不透過任何 context default | `AccessError`（守衛本來就會擋） | `AccessError` | 單一差異變數對照——Y0/Y2 與這條的唯一差異就是欄位在 vals 還是 context，這就是洞的存在證明 |

## 機制陽性對照（leader1 裁決，2026-08-11，覆核 §Q2）

Y0/Y1/Y2/Y2-orphan 全部斷言的是「被擋」，Y3 斷言的是「值寫在 vals 也被擋」——沒有任何一
條斷言「context default 這個機制本身還有效」。若日後 Odoo 的 context-default 解析機制被
改壞（版本升級、context 被清洗、`with_context` 用錯），Y0-Y2 會因為 payload 根本沒驅動任
何東西而**全部意外變綠**，外觀上與「守衛擋住了」一模一樣——develop 上跑出的 RED 只證明
了當時那個 payload 可驅動，是一次性證據，不是會持續跑的測試。`test_context_default_
mechanism_is_effective` 用一個豁免身分（`base.group_system`，繞過 `_check_assignable_
role` 本身，讓這條測試只測「機制」不測「守衛」）送出與 Y0/Y1 同型的 context-default
payload（vals 完全不放 `role`／`workspace_id`，兩者都只放在 context），斷言 (a) 列真的被
建出來、(b) 落地的 role 確實是 `owner`、(c) `workspace_id` 確實是 context 給的那個。三個
斷言缺一不可——只斷言「建出來了」不足以證明 context default 真的被套用（也可能是欄位
default 或某個巧合值）。目標選 orphan workspace（借用 Y1/Y2-orphan 的 fixture）是為了避開
`_check_owner_count`——那道不變式對 superuser／group_system 沒有豁免，若在有 owner 列的
workspace 上跑，這條陽性對照本身就會被不相干的不變式擋下。這條同時覆蓋了本次修法新程式
路徑對 superuser／group_system 的豁免分支（`_add_missing_default_values` 現在對這兩種身
分也會跑）。

## orphan workspace 怎麼建（#664 補正留言 §3）

租戶自己造不出「無 owner 列的 workspace」——刪 `res.users` 是
`group_system` 權限，生產上的孤兒只能來自管理端刪帳號（#525 那種：
`ondelete='cascade'` 在 DB 層直接抹掉那一列，不經 Python `unlink()`）。
fixture 照抄 `test_workspace_create_rollback_525.py` 的手法，用 **DB 層
DELETE** 造，不用 `.sudo().unlink()`——後者會撞 `unlink()` 自己的『不得移
除最後一個 owner』不變式（#556 B2，連 sudo 都擋），而且與生產成因不同
（生產是 cascade 帶走，不是主動呼叫 unlink）。

## 順手修的假話（#664 新發現，issue 本文與補正留言都沒提）

修復前 `workspace_access.py:137-142` 的註解宣稱：role 省略、只靠
`with_context(default_role=...)` 補值的情況「still reaches this check
correctly」。這句話是錯的——守衛確實被「呼叫到」，但拿到的是
`ROLE_USER`，實際落地的是 context 給的值；那正是 role 半邊的洞本身，不是
「正確處理」。本檔連同 model 端一起把這段改寫成與 `project_access.py` 同
構的正確敘述。

## TDD 紀律（照抄 #556／#657，一字不可省）

- **全部走真 HTTP RPC**：`/web/session/authenticate` → `/web/dataset/
  call_kw`。**禁止 `TransactionCase`**——其 `env` 的 `uid == SUPERUSER_ID`
  ⇒ 守衛的 deny 分支結構上不可達，恆綠且與修復好壞無關。
- fixture user 一律只掛 `base.group_user`（alice/dave/eve/frank）。
"""
import uuid

from odoo.tests.common import HttpCase, tagged

from odoo.addons.woow_paas_platform.models.workspace_access import (
    ROLE_ADMIN,
    ROLE_GUEST,
    ROLE_OWNER,
    ROLE_USER,
)

import json


def _new_group_user(env, name):
    """建一個**只有** `base.group_user` 的租戶帳號（可 `authenticate`）。

    刻意不用 `admin`／uid 2：guard 的 `has_group('base.group_system')` 豁免
    會讓那個身分對本檔要驗的租戶攻擊向零鑑別力。
    """
    suffix = uuid.uuid4().hex[:8]
    login = '%s_%s' % (name, suffix)
    user = env['res.users'].sudo().create({
        'name': '%s 664' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })
    return user, login


@tagged('post_install', '-at_install')
class _WorkspaceAccess664Fixture(HttpCase):
    """ws1（alice=owner、dave=admin，正常 workspace，有 owner 列）＋ eve
    （共犯／被鑄一列的目標，不屬任何 workspace）＋ frank（orphan workspace
    的原始 owner，供 `_make_orphan_workspace` 使用）。四人皆只掛
    `base.group_user`。
    """

    def setUp(self):
        super().setUp()
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']
        self.Workspace = self.env['woow_paas_platform.workspace']

        self.alice, self.alice_login = _new_group_user(self.env, 'alice664')
        self.dave, self.dave_login = _new_group_user(self.env, 'dave664')
        self.eve, self.eve_login = _new_group_user(self.env, 'eve664')
        self.frank, self.frank_login = _new_group_user(self.env, 'frank664')

        for u in (self.alice, self.dave, self.eve, self.frank):
            self.assertFalse(
                u.has_group('base.group_system'),
                'fixture 前提：租戶只能有 base.group_user（%s 撞了）' % u.login)

        # setUp 走 HttpCase 預設的 superuser env，純粹用來搭 fixture，不是
        # 本檔要驗的路徑（workspace.create() 本身的 owner-access bootstrap
        # 走 .sudo()，不受本次修改影響）。
        self.ws1 = self.Workspace.create({'name': 'WS1-664', 'owner_id': self.alice.id})
        self.alice_access = self.WorkspaceAccess.search([
            ('workspace_id', '=', self.ws1.id), ('user_id', '=', self.alice.id)])
        self.assertEqual(self.alice_access.role, ROLE_OWNER, 'fixture 前提：alice 是 WS1 owner')
        self.dave_access = self.WorkspaceAccess.sudo().create({
            'workspace_id': self.ws1.id, 'user_id': self.dave.id, 'role': ROLE_ADMIN})

    # ------------------------------------------------------------------ #
    #  使用者唯一入口：真 HTTP JSON-RPC                                    #
    # ------------------------------------------------------------------ #
    def _call_kw(self, model, method, args, kwargs=None):
        """`/web/dataset/call_kw`——ORM 層攻擊（create/write/unlink）必須走
        這條真實 RPC 路徑，不可用 `TransactionCase` + `with_user()` 模擬
        （見檔頭 TDD 紀律）。"""
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

    def _make_orphan_workspace(self, owner_user, admin_user, name):
        """建構『無 owner 列』的 workspace（#664 補正留言 §3 的成因：管理端
        刪帳號、DB 層 cascade 直接抹掉該列，不經 Python `unlink()`）。用 DB
        層 DELETE 忠實重現，不用 `.sudo().unlink()`（見檔頭說明）。"""
        ws = self.Workspace.sudo().create({'name': name, 'owner_id': owner_user.id})
        self.WorkspaceAccess.sudo().create({
            'workspace_id': ws.id, 'user_id': admin_user.id, 'role': ROLE_ADMIN})
        self.env.cr.execute(
            "DELETE FROM woow_paas_platform_workspace_access "
            "WHERE workspace_id = %s AND role = 'owner'", (ws.id,))
        self.env.invalidate_all()
        self.assertFalse(
            self.WorkspaceAccess.sudo().search([
                ('workspace_id', '=', ws.id), ('role', '=', ROLE_OWNER)]),
            'fixture 前提：orphan workspace 不得有任何 owner 列')
        self.assertTrue(
            self.WorkspaceAccess.sudo().search([
                ('workspace_id', '=', ws.id), ('user_id', '=', admin_user.id),
                ('role', '=', ROLE_ADMIN)]),
            'fixture 前提：orphan workspace 仍保留 admin 列')
        return ws

    # ------------------------------------------------------------------ #
    #  斷言 helper                                                        #
    # ------------------------------------------------------------------ #
    def _assert_access_error(self, result, msg):
        self.assertIn('error', result, msg + '（回 %r）' % result)
        name = result['error'].get('data', {}).get('name', '')
        self.assertIn('AccessError', name,
                      msg + '（期望 AccessError，回 %r）' % result)

    def _assert_call_kw_ok(self, result, msg):
        self.assertNotIn('error', result, msg + '（回 %r）' % result)


@tagged('post_install', '-at_install')
class TestWorkspaceAccess664AttackVectors(_WorkspaceAccess664Fixture):
    """檔頭表 Y0-Y3：role 半邊（Y0/Y1）與 workspace_id 半邊（Y2 normal/
    orphan）的 context-default 繞過，加單一差異變數對照（Y3）。
    """

    # ---------------------------------------------------------------- #
    #  role 半邊（Y0 正常 workspace／Y1 orphan workspace）                #
    # ---------------------------------------------------------------- #
    def test_Y0_role_half_context_default_role_owner_denied_on_normal_workspace(self):
        """dave（WS1 admin）用 `with_context(default_role='owner')` 建立一列
        給共犯 eve，vals 裡刻意不放 `role`（workspace_id 仍在 vals 裡，只孤立
        role 半邊）。

        pre-fix RED：`effective_role = vals.get('role') or ROLE_USER` 讀不到
        vals 裡沒有的 role ⇒ `'user'`；`_check_assignable_role('user',
        ws1.id)` 對 dave（admin）而言 `'user'` 在
        `ASSIGNABLE_ROLES_BY_ROLE[admin]` 內 ⇒ 放行；`super().create()` 之
        後才從 context 補上 `role='owner'` ⇒ INSERT 出 role='owner' 的列；
        `_check_owner_count` 發現 WS1 已有 owner（alice）⇒ `ValidationError`
        ——不是本守衛擋下的，是「碰巧」被一道不相干的不變式擋住。本測試斷言
        的是 `AccessError`，pre-fix 拿到 `ValidationError` 型別不符，這就是
        合格的 RED。
        """
        self.authenticate(self.dave_login, self.dave_login)
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'create',
            [{'workspace_id': self.ws1.id, 'user_id': self.eve.id}],
            {'context': {'default_role': ROLE_OWNER}})
        self._assert_access_error(
            result, 'dave（admin）不得用 default_role context trick 在正常 '
                    'workspace 上幫共犯 eve 建出 owner 列（role 半邊，Y0）')
        self.assertFalse(
            self.WorkspaceAccess.sudo().search([
                ('workspace_id', '=', self.ws1.id), ('user_id', '=', self.eve.id)]),
            '拒絕必須發生在 INSERT 之前——不得留下殘留列')

    def test_Y1_role_half_context_default_role_owner_privesc_on_orphan_workspace(self):
        """同上攻擊，但前提換成 orphan workspace（無 owner 列，只剩 dave 這個
        admin）——`_check_owner_count` 這次不會「碰巧」擋下，真正的提權 RED。

        pre-fix RED：guard 同樣放行（'user' 在 admin 的 allowed 表內）；
        INSERT 出 role='owner' 的列；`_check_owner_count` 的
        `owner_count == 0`（真的沒有 owner 列）⇒ 不變式也放行 ⇒ 攻擊成功，
        真的多出一列 role='owner' 的授權列。
        """
        orphan = self._make_orphan_workspace(self.frank, self.dave, 'Y1-orphan-664')
        self.authenticate(self.dave_login, self.dave_login)
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'create',
            [{'workspace_id': orphan.id, 'user_id': self.eve.id}],
            {'context': {'default_role': ROLE_OWNER}})
        self._assert_access_error(
            result, 'dave（orphan workspace 僅存的 admin）不得用 default_role '
                    'context trick 在無 owner 列的 workspace 上鑄出 owner 列'
                    '（role 半邊，Y1，真提權）')
        self.assertFalse(
            self.WorkspaceAccess.sudo().search([
                ('workspace_id', '=', orphan.id), ('user_id', '=', self.eve.id)]),
            '拒絕必須發生在 INSERT 之前——pre-fix 這條真的會多一列 role=owner')

    # ---------------------------------------------------------------- #
    #  workspace_id 半邊（Y2 正常 workspace／orphan workspace）           #
    # ---------------------------------------------------------------- #
    def test_Y2_workspace_id_half_context_default_workspace_id_denied_on_normal_workspace(self):
        """dave（WS1 admin）用 `with_context(default_workspace_id=W)` 建立一
        列給共犯 eve，vals 裡刻意不放 `workspace_id`（role='owner' 明寫在
        vals，只孤立 workspace_id 半邊）。

        pre-fix RED：`_check_assignable_role('owner', vals.get('workspace_id'))`
        的第二個參數是 `None`（vals 沒有這個 key）⇒ `if not workspace_id:
        return` ⇒ **整道守衛沒跑，不管 role 是什麼值**；`super().create()`
        之後才從 context 補上 `workspace_id=ws1.id`；`_check_owner_count`
        發現 WS1 已有 owner ⇒ `ValidationError`——同 Y0，是不變式「碰巧」擋
        下，不是本守衛。
        """
        self.authenticate(self.dave_login, self.dave_login)
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'create',
            [{'user_id': self.eve.id, 'role': ROLE_OWNER}],
            {'context': {'default_workspace_id': self.ws1.id}})
        self._assert_access_error(
            result, 'dave（admin）不得用「workspace_id 只放 context」的方式在'
                    '正常 workspace 上鑄出 owner 列（workspace_id 半邊，Y2）')
        self.assertFalse(
            self.WorkspaceAccess.sudo().search([
                ('workspace_id', '=', self.ws1.id), ('user_id', '=', self.eve.id)]),
            '拒絕必須發生在 INSERT 之前——不得留下殘留列')

    def test_Y2_workspace_id_half_context_default_workspace_id_privesc_on_orphan_workspace(self):
        """同上攻擊，前提換成 orphan workspace——真正的提權 RED（workspace_id
        半邊版本）。"""
        orphan = self._make_orphan_workspace(self.frank, self.dave, 'Y2-orphan-664')
        self.authenticate(self.dave_login, self.dave_login)
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'create',
            [{'user_id': self.eve.id, 'role': ROLE_OWNER}],
            {'context': {'default_workspace_id': orphan.id}})
        self._assert_access_error(
            result, 'dave（orphan workspace 僅存的 admin）不得用「workspace_id '
                    '只放 context」的方式在無 owner 列的 workspace 上鑄出 '
                    'owner 列（workspace_id 半邊，Y2，真提權）')
        self.assertFalse(
            self.WorkspaceAccess.sudo().search([
                ('workspace_id', '=', orphan.id), ('user_id', '=', self.eve.id)]),
            '拒絕必須發生在 INSERT 之前——pre-fix 這條真的會多一列 role=owner')

    # ---------------------------------------------------------------- #
    #  Y3 — 單一差異變數對照                                             #
    # ---------------------------------------------------------------- #
    def test_Y3_control_role_and_workspace_id_explicit_in_vals_denied(self):
        """對照組：同樣的攻擊意圖——dave（admin）想幫 eve 在 WS1 鑄一列
        owner——但 `role` 與 `workspace_id` 都明寫在 vals，不透過任何 context
        default。這條在修復前就已經被擋（`_check_assignable_role` 本來就有
        跑：dave 是 admin，`ASSIGNABLE_ROLES_BY_ROLE[admin]` 不含 owner）。
        Y0／Y2 與這條的唯一差異就是欄位在 vals 還是 context——這就是本洞的
        存在證明。
        """
        self.authenticate(self.dave_login, self.dave_login)
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'create',
            [{'workspace_id': self.ws1.id, 'user_id': self.eve.id, 'role': ROLE_OWNER}])
        self._assert_access_error(
            result, 'admin 明寫 role=owner + workspace_id 的 create 應早已被'
                    '擋（陽性對照，修復前後都必須是 AccessError）')
        self.assertFalse(
            self.WorkspaceAccess.sudo().search([
                ('workspace_id', '=', self.ws1.id), ('user_id', '=', self.eve.id)]),
            '拒絕必須發生在 INSERT 之前——不得留下殘留列')

    # ---------------------------------------------------------------- #
    #  機制陽性對照（leader1 裁決，2026-08-11，見檔頭「機制陽性對照」段）  #
    # ---------------------------------------------------------------- #
    def test_context_default_mechanism_is_effective(self):
        """證明 context-default 解析機制本身還活著，不只是「守衛擋住了」的
        外觀。用 `group_system`（豁免身分，繞過 `_check_assignable_role`
        本身）送出與 Y0/Y1 同型的 payload——vals 完全不放 `role` 與
        `workspace_id`，兩者都只放在 context——三個斷言缺一不可：
        (a) 列真的被建出來，(b) 落地的 role 確實是 `owner`（不是欄位
        default 的 `user`），(c) `workspace_id` 確實是 context 給的那個
        （不是 False 或某個巧合值）。目標用 orphan workspace 避開
        `_check_owner_count`（那道不變式對 group_system 沒有豁免）。
        """
        orphan = self._make_orphan_workspace(self.frank, self.dave, 'PMech-orphan-664')
        self.authenticate('admin', 'admin')
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'create',
            [{'user_id': self.eve.id}],
            {'context': {'default_role': ROLE_OWNER, 'default_workspace_id': orphan.id}})
        self._assert_call_kw_ok(
            result, 'group_system 用純 context-default payload（vals 不放 '
                    'role/workspace_id）建列應成功（機制陽性對照，回 %r）' % result)
        new_id = result['result']
        row = self.WorkspaceAccess.sudo().browse(new_id)
        self.assertEqual(
            row.role, ROLE_OWNER,
            'context 的 default_role 必須真的落地成 owner——若不是，代表 '
            'context-default 機制本身沒被驅動，Y0/Y1/Y2 對它零鑑別力')
        self.assertEqual(
            row.workspace_id.id, orphan.id,
            'context 的 default_workspace_id 必須真的落地成 orphan——同上')


@tagged('post_install', '-at_install')
class TestWorkspaceAccess664PositiveControls(_WorkspaceAccess664Fixture):
    """P1-P3：防 over-correction——修復不得誤傷既有合法流程。P4（既有
    `test_workspace_access_authz_556.py` 全綠）／P5（`group_system` 全權
    CRUD）／P6（write-pivot anchor 仍擋得住）由既有測試套件涵蓋，不在本檔
    重寫（見檔頭工單 §4）。
    """

    def test_P1_owner_can_invite_user_and_guest(self):
        self.authenticate(self.alice_login, self.alice_login)
        for role in (ROLE_USER, ROLE_GUEST):
            invitee, _login = _new_group_user(self.env, 'p1invitee%s664' % role)
            result = self._call_kw(
                'woow_paas_platform.workspace_access', 'create',
                [{'workspace_id': self.ws1.id, 'user_id': invitee.id, 'role': role}])
            self._assert_call_kw_ok(
                result, 'owner 邀請 role=%s 應成功（回 %r）' % (role, result))

    def test_P2_admin_can_invite_admin(self):
        """鎖 #556 的刻意偏離：`workspace_access` 的
        `ASSIGNABLE_ROLES_BY_ROLE[admin]` 含 `admin`（與 `project_access` 的
        嚴格表不同，`paas.py::_invite_member` 一直允許 admin 邀 admin）。本
        單不可把它『對齊』掉。"""
        self.authenticate(self.dave_login, self.dave_login)
        invitee, _login = _new_group_user(self.env, 'p2invitee664')
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'create',
            [{'workspace_id': self.ws1.id, 'user_id': invitee.id, 'role': ROLE_ADMIN}])
        self._assert_call_kw_ok(
            result, 'admin 邀請 role=admin 應成功（回 %r，#556 的刻意偏離不可'
                    '被本單改掉）' % result)

    def test_P3_owner_default_workspace_id_context_form_idiom_still_works(self):
        """表單慣用法陽性對照：owner 用 `with_context(default_workspace_id=
        W)`、vals 不放 `workspace_id` 邀一個 user——這條若紅，代表修法把功能
        鎖死了。"""
        self.authenticate(self.alice_login, self.alice_login)
        invitee, _login = _new_group_user(self.env, 'p3invitee664')
        result = self._call_kw(
            'woow_paas_platform.workspace_access', 'create',
            [{'user_id': invitee.id, 'role': ROLE_USER}],
            {'context': {'default_workspace_id': self.ws1.id}})
        self._assert_call_kw_ok(
            result, 'owner 用 default_workspace_id context 慣用法邀請 user '
                    '應成功（回 %r）' % result)
        new_id = result['result']
        row = self.WorkspaceAccess.sudo().browse(new_id)
        self.assertEqual(row.workspace_id.id, self.ws1.id, 'workspace_id 必須正確解析成 ws1')

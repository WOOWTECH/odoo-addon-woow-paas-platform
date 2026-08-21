# -*- coding: utf-8 -*-
"""#787 A 段最後一把：`woow_force_issue` 的租戶可達性探針（force 型）。

## 這把鑰匙是什麼

`src/models/account_move.py::AccountMove.create_ecpay_invoice()` 的讀取點：

    if (self.woow_skip_upstream_hook
            and not _is_invoice_issue_control(self.env, _CTX_WOOW_FORCE_ISSUE)):
        return                                   # 早退，不開發票
    return super().create_ecpay_invoice()        # ← 真的打 ECPay B2CInvoice/Issue

（修法前那個判定是裸的 `self.env.context.get(<key>)`——只看 truthy。）

生產側唯一的正當開閘點是非同步的 `ecpay_invoice_cron._issue_one()`，它走
`models/account_move.py` 的模組私有 helper `_forced_ecpay_issue()`。
⚠ 修法後**裸的 `with_context(<key>=True)` 不再開閘、而且會拋 `UserError`**——本
檔刻意不把那個寫法逐字寫成可照抄的形狀，因為讀者會照抄範本而不是照做警告。

與 #787 A 段前四把（`cs_skip_access_sync` 與 SH 三把）**方向相反**：那四把是
skip 型——偽造 ⇒ 該做的事被跳過（fail-open 且無聲）；這一把是 **force 型**——
偽造 ⇒ 一件本來不會發生、而且**不可逆的對外動作**被做出來：向 ECPay 開立一張
真的電子發票。所以本檔的結論不能照抄那四把的形狀，可達性也必須自己實測。

## 🔴 為什麼判定用離線替身，而不是「有沒有真的開出發票」

`super()` 落在第三方 addon `ecpay_invoice_tw` 上，它的方法體會組出
`https://einvoice.ecpay.com.tw/B2CInvoice/Issue` 並送出，而發票開立不可逆。
本檔因此把觀測點放在**上游那支方法本身**（`_UPSTREAM`）：被攔截的是整個方法體，
`EcpayInvoice()` 一個物件都不會被建、一個 request 都不會送出。
`upstream.called` 與「會不會對外送出」是同一件事的兩面——走進 `super()` ＝ 會送、
沒走進去 ＝ 不會送——所以它是這個命題上**鑑別力最強且不觸外**的判準。

## 為什麼一定要走真 HTTP RPC

理由與 `test_cs_sync_ctx_forgery_787.py` / `test_sh_sync_ctx_forgery_787.py` 逐字
相同：這條攻擊的**前提**就是「`/web/dataset/call_kw` 會把 client 的 context 換進
env」（`odoo/api.py::call_kw` → `split_context()` → `recs.with_context(context)`）。
在 `TransactionCase` 裡直接用 in-process 的 helper 開閘只證明了「這個 key 有作
用」（`test_action_post_hook_isolation.py` 已經在證這件事），證明不了**租戶構造
得出它並送得到讀取點**——後者才是本案的命題，而它的答案是「看身分」（見上表）。

## 分層（每一格都要能分辨「守住了」與「探針壞了」）

| 組 | 身分 | context | 期望 |
| --- | --- | --- | --- |
| L1 | 租戶（只有 `base.group_user`） | — | 連 `read` 都被擋 ⇒ 釘住 blocker 是 ACL 那一道 |
| R1 | 租戶 | 不帶 key | 被擋（對照組） |
| R2 | 租戶 | **偽造**這把 key（JSON `true`） | 必須與 R1 **同型**被擋，且 upstream 不得被呼叫 |
| T0 | **portal** 租戶（`base.group_portal`） | — | 讀得到自己已 post 的發票（前提格） |
| T1 | portal | 不帶 key | 短路，upstream 不被呼叫（對照組） |
| T2 | portal | **偽造** key | upstream 不得被呼叫 🔴 |
| T3 | portal | — | 建不出 `uniform.invoice` ⇒ 量出守衛之後那道牆 |
| T4 | portal | **偽造** key | 必須留下點名這把 key 的 WARNING 🔴 |
| P1 | 具 `account.group_account_invoice` 的內部身分 | 不帶 key | 短路，upstream 不被呼叫（對照組） |
| P2 | 同上 | **偽造** key | upstream 不得被呼叫 🔴——判準不是「你是誰」 |
| G1 | in-process（superuser）＋ sentinel | — | upstream **被**呼叫（逃生門仍開得了） |
| G2 | in-process ＋ 裸 `True` | — | 不開閘，且拋 `UserError`（🔴 fail-safe 方向） |
| G3 | in-process ＋ `=False` | — | 短路（語意原封不動） |

**T 組為什麼不能省**：`base.group_user` 對 `account.move` 一列 ACL 都沒有，但
Odoo 18 的 `account` 明給 `base.group_portal` 一列 `read=1`
（`access_account_invoice_portal`），而本 repo 的 signup 樣板使用者正是只有
`base.group_portal`（`test_acl_p9.py`）。「R 組被擋」**推導不出**「T 組也被擋」
——兩者的 ACL 出處不同，只驗 R 組會得到一個比實際窄的否定性結論。

**觀測點的陽性對照是 G1**：同一個 mock、同一張發票，用模組私有 helper 開閘時
`upstream.called` 必須為 `True`。少了它，所有否定性格（R2／T2／P2／G2）的綠燈與
「patch 點寫錯 ⇒ `called` 恆為 `False`」完全不可區分——後者會讓本檔在這把鑰匙
**真的可達**的時候仍然全綠。

**「呼叫端的 context 真的到得了讀取點」的證據是 T4**：判定函式看到那個值才會印
WARNING，所以那行 WARNING 本身就是「值從 wire 一路送到判定點」的回執。它同時是
偽造的訊號，也是「還有 in-process 的碼照舊傳 `True`」的訊號。

**P1 是 P2 的陰性對照**：同一個身分、同一支呼叫、同一個 mock，只差 context 裡那
一個 key。少了它，「守住了」與「這條路徑本來就不會走進 `super()`」分不出來。

## 界線（本檔驗到什麼、沒驗到什麼）

* 驗到的是 **`/web/dataset/call_kw` 這一條載體** ＋ **`base.group_user` 與
  `base.group_portal` 這兩個身分** ＋ **本沙盒安裝的這組 addon**
  （`account.move` 的 ACL 由 Odoo 18 core 提供，`uniform.invoice` 的由
  `ecpay_invoice_tw` 提供）。
* 🔴 R 組的否定性結論**只對「真的只有 `base.group_user`」的身分成立**，而那不是
  Odoo 的預設——`base.default_user` 樣板掛著 `account.group_account_manager`
  （implies `group_account_invoice` ⇒ `account.move` 1,1,1,1）。R0 就是在量這件
  事，P 組則守「就算拿得到 ACL，這把 key 一樣不是授權憑證」。
* **沒有**驗：`sales_team.group_sale_salesman`／`purchase.group_purchase_user`
  ／`point_of_sale.group_pos_user` 這些也帶 `account.move` ACL 的群組；也沒有驗
  `call_kw` 以外、會把呼叫端 context 換進 env 的其它 endpoint。
* **沒有**驗但已知存在的**間接載體**：`woow.wallet.topup.charge_via_token()`
  （租戶有該 model 的 ACL）→ `_create_topup_invoice()` → `action_post()` → 上游
  inline hook → 本讀取點。它今天到不了，靠的是「`call_kw` 的 args 是 JSON、傳不
  進 `payment.token` recordset」這個**參數形態**的偶然，而不是任何一層存取控制
  ——把簽名改成收 id 再 `browse()` 的重構會在零 ACL 變更、零測試訊號的情況下打通
  它。本檔的修法對載體無關（判準在讀取點上），所以那條路一併被關掉。
* 因此本檔任何否定性斷言都是**範圍限定**的：它說的是「這些身分、這條路徑上打不
  進去」，不是「這把鑰匙沒有風險」。
"""
import json
import uuid
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import HttpCase, tagged

from ..models.account_move import (
    _CTX_WOOW_FORCE_ISSUE,
    _forced_ecpay_issue,
)

_MOVE = 'account.move'
#: 判定函式印 WARNING 的 logger（fail-loud 那一格的觀測點）。
_MOVE_LOGGER = 'odoo.addons.woow_paas_platform.models.account_move'

#: 🔴 判定「有沒有走進 `super()`」的唯一觀測點，也是不觸外的邊界：patch 的是上游
#: 那支方法**本身**，所以它的方法體（含 ECPay HTTP 呼叫）一行都不會執行。
#: patch 點寫錯不會讓否定性斷言變紅（`called` 會恆為 False）⇒ P2 是它的對照。
_UPSTREAM = (
    'odoo.addons.ecpay_invoice_tw.models.account_invoice.AccountMove'
    '.create_ecpay_invoice'
)

#: 租戶在 JSON payload 裡表達得出來的那個值（JSON 表達不出物件同一性）。
_FORGED = {'context': {'woow_force_issue': True}}


def _new_user(env, name, extra_groups=(), user_type='base.group_user',
              inherit_default_groups=False):
    """建一個可 `authenticate` 的帳號。

    `user_type` 是那唯一一個 user-type 群組（`base.group_user` 或
    `base.group_portal`）；`extra_groups` 之外不給任何其它群組。

    `inherit_default_groups=True` 則**完全不指定** `groups_id` —— 讓 Odoo 走
    `base.default_user` 樣板。那是 R0 在量的東西：預設樣板給的權限遠比
    「只有 `base.group_user`」多。
    """
    suffix = uuid.uuid4().hex[:8]
    login = '%s_%s' % (name, suffix)
    vals = {
        'name': '%s 787' % name,
        'login': login,
        'password': login,
        'email': '%s@test.example' % login,
    }
    if not inherit_default_groups:
        groups = [env.ref(user_type).id]
        for xmlid in extra_groups:
            groups.append(env.ref(xmlid).id)
        vals['groups_id'] = [(6, 0, groups)]
    user = env['res.users'].sudo().create(vals)
    return user, login


@tagged('post_install', '-at_install')
class _ForceIssue787Fixture(HttpCase):
    """一張 PaaS 形狀的發票（`woow_skip_upstream_hook=True`）＋兩個身分。

    母體刻意**不** post：`action_post()` 會觸發上游 inline hook，而本檔要觀測的是
    「RPC 這一次呼叫有沒有走進 super()」——母體自己先走一次會讓 `called` 在攻擊格
    之前就是 True，整個觀測失去鑑別力。
    """

    def setUp(self):
        super().setUp()
        # 上游 inline hook 只在 automatic 模式下會 inline 呼叫 create_ecpay_invoice；
        # 這正是 woow_skip_upstream_hook 這面盾牌存在的前提情境。
        self.env.company.auto_invoice = 'automatic'

        self.partner = self.env['res.partner'].sudo().create(
            {'name': 'ForceIssue787 partner'})
        self.invoice = self._make_invoice(self.partner)
        self.assertTrue(
            self.invoice.woow_skip_upstream_hook,
            '前提：母體必須是「帶著 skip 旗標」的 PaaS 發票，否則讀取點的那個 '
            '`and` 短路掉，本檔每一格都測不到 woow_force_issue')

        # 攻擊者：只有 base.group_user 的一般租戶。
        self.tenant, self.tenant_login = _new_user(self.env, 'tenant787')
        for grp in ('account.group_account_invoice',
                    'account.group_account_readonly',
                    'account.group_account_manager',
                    'base.group_system'):
            self.assertFalse(
                self.tenant.has_group(grp),
                '前提：攻擊者身分不得帶 %s——帶了的話本檔驗的就不是「一般租戶」'
                % grp)

        # 陽性對照的身分：真的有 account.move ACL 的人。
        self.accountant, self.accountant_login = _new_user(
            self.env, 'acct787', extra_groups=('account.group_account_invoice',))
        self.assertTrue(
            self.accountant.has_group('account.group_account_invoice'),
            '前提：陽性對照的身分必須真的拿到 account.group_account_invoice，'
            '否則 P1／P2 兩格會因為 ACL 而不是因為 context 而分不開')

        # 🔴 第二個租戶身分：portal。本 repo 的 signup 樣板使用者**只有**
        #    `base.group_portal`（見 `test_acl_p9.py`），而 Odoo 18 的 `account`
        #    給 `base.group_portal` 一列 `account.move` 的 **read=1**
        #    （`addons/account/security/ir.model.access.csv` 的
        #    `access_account_invoice_portal`）＋ `account_invoice_rule_portal`
        #    限縮到「自己的、已 post 的」那幾張。所以 base.group_user 打不進去
        #    **推導不出** portal 也打不進去——兩者的 ACL 出處根本不同。
        self.portal_partner = self.env['res.partner'].sudo().create(
            {'name': 'ForceIssue787 portal partner'})
        self.portal_user, self.portal_login = _new_user(
            self.env, 'portal787', user_type='base.group_portal')
        self.portal_user.sudo().write({'partner_id': self.portal_partner.id})
        self.portal_invoice = self._make_invoice(self.portal_partner)
        # portal 的 ir.rule 要 `state not in ('cancel','draft')` ＋ 自己是 follower。
        # post 這一步會觸發上游 inline hook（company 是 automatic），所以整段包在
        # 離線替身裡跑——母體的建置本身絕不允許有任何一絲對外送出的可能。
        with patch(_UPSTREAM, autospec=True) as upstream:
            self.portal_invoice.sudo().action_post()
        self.assertFalse(
            upstream.called,
            '母體自檢：帶 skip 旗標的發票在 action_post 時不得走進上游——這一格若'
            '變紅，代表短路已經壞了，本檔其餘每一格的觀測都會被母體污染')
        self.portal_invoice.sudo().message_subscribe(
            partner_ids=[self.portal_partner.id])
        self.env.flush_all()

    # ------------------------------------------------------------------ #
    #  Helpers                                                           #
    # ------------------------------------------------------------------ #

    def _make_invoice(self, partner):
        """一張 PaaS 形狀的 out_invoice（`woow_skip_upstream_hook=True`）。"""
        product = self.env['product.product'].sudo().search([], limit=1)
        if not product:
            product = self.env['product.product'].sudo().create(
                {'name': 'ForceIssue787 product'})
        return self.env[_MOVE].sudo().create({
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'woow_skip_upstream_hook': True,
            'woow_invoice_issue_state': 'pending',
            'invoice_line_ids': [(0, 0, {
                'product_id': product.id,
                'name': 'ForceIssue787',
                'quantity': 1,
                'price_unit': 100.0,
            })],
        })

    def _call_kw(self, model, method, args, kwargs=None):
        """`/web/dataset/call_kw` —— context 偽造攻擊的真實路徑。"""
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

    @staticmethod
    def _error_name(result):
        return ((result.get('error', {}) or {}).get('data', {}) or {}).get(
            'name', '')

    @staticmethod
    def _error_text(result):
        data = (result.get('error', {}) or {}).get('data', {}) or {}
        return '%s %s' % (data.get('name', ''), data.get('message', ''))

    def _assert_ok(self, result, msg):
        self.assertNotIn(
            'error', result, msg + '（回 %s）' % self._error_text(result))
        return result.get('result')

    def _issue_via_rpc(self, kwargs=None):
        return self._call_kw(
            _MOVE, 'create_ecpay_invoice', [[self.invoice.id]], kwargs)


class TestForceIssueTenantReach787(_ForceIssue787Fixture):
    """R／L：一般租戶（`base.group_user`）打得到這個讀取點嗎？"""

    def test_L1_tenant_cannot_even_read_the_move(self):
        """釘住 blocker 是哪一道：租戶對 `account.move` 連 `read` 都沒有。

        少了這一格，R1／R2 的「被擋下來」只是一個沒有出處的事實——它可能是 ACL、
        可能是 ir.rule、可能是方法名守衛，而三者哪天鬆開的條件完全不同。
        """
        self.authenticate(self.tenant_login, self.tenant_login)
        result = self._call_kw(_MOVE, 'read', [[self.invoice.id], ['name']])
        self.assertIn(
            'error', result,
            '前提：一般租戶不該讀得到 account.move。若這一格變綠，代表這個 DB 給了 '
            'base.group_user account.move 的讀取權——R1／R2 的結論必須整組重驗')
        self.assertIn(
            'AccessError', self._error_name(result),
            '擋下來的必須是 ACL（AccessError），實際收到 %r'
            % self._error_text(result))

    def test_R0_the_clean_group_user_identity_is_not_odoo_s_default(self):
        """🔴 釘住 R 組結論的**範圍**，別讓它被讀成「內部使用者都打不到」。

        R1／R2 的綠燈只對「真的只有 `base.group_user`」的身分成立，而那是本檔**強制
        造出來**的（`groups_id: [(6, 0, [...])]`）。Odoo 18 的 `account` 把
        `account.group_account_manager` 加進 `base.default_user` 樣板，而它
        `implied_ids` 含 `group_account_invoice`——後者對 `account.move` 是
        `1,1,1,1`。⇒ 任何**沒有顯式指定 `groups_id`** 的 `res.users.create()` 建出
        來的內部帳號，都拿得到 `account.move` 全 CRUD。

        這一格因此不是在測本 addon 的行為，是在**量一個前提**：把它寫成測試而不是
        寫進 PR 的散文，是因為散文會腐爛而且腐爛時沒有訊號。

        ⚠ 它變紅時代表 Odoo 改了預設樣板 ⇒ 要改的是結論的措辭（R 組的適用範圍），
        不是本 addon 的程式。
        """
        default_user, _login = _new_user(self.env, 'default787',
                                         inherit_default_groups=True)
        self.assertTrue(
            default_user.has_group('account.group_account_invoice'),
            '前提量測：不指定 groups_id 建出來的內部帳號應該從 base.default_user '
            '樣板繼承到 account.group_account_invoice（實際 groups=%r）'
            % default_user.groups_id.mapped('full_name'))
        self.assertFalse(
            self.tenant.has_group('account.group_account_invoice'),
            '對照：本檔的攻擊者身分是刻意剝乾淨的，兩者不可混為一談')

    def test_R1_control_tenant_without_key_is_blocked(self):
        """對照：不帶 key 的同一支呼叫也被擋。

        這一格是 R2 的鑑別力來源：少了它，「偽造沒生效」與「這條路徑對這個身分
        本來就走不通」分不出來。
        """
        self.authenticate(self.tenant_login, self.tenant_login)
        with patch(_UPSTREAM, autospec=True) as upstream:
            result = self._issue_via_rpc()
        self.assertIn(
            'error', result,
            '對照：租戶不帶 key 呼叫 create_ecpay_invoice 就該被擋')
        self.assertFalse(
            upstream.called,
            '對照：被擋下來的呼叫不得走進 super()（那會真的打 ECPay）')

    def test_R2_forged_key_does_not_get_the_tenant_into_super(self):
        """🔴 #787 主格：租戶在 call_kw 的 context 塞 `woow_force_issue`。

        「這個結果不該出現」那一格＝`upstream.called` 為 True：那代表租戶用一行
        JSON 就讓平台向 ECPay 開出一張**不可逆**的電子發票。

        判準刻意有兩層：`called` 必須為 False（沒走進 super()），而且**被擋下來
        的型別必須與 R1 相同**——若兩格的例外型別不一樣，代表偽造的 key 在中途
        真的改變了控制流（SH 那把 `sm_route_cascade_unlink` 的實測簽名正是這種
        「例外型別翻轉」），那時「沒開成發票」只是撞到了另一道守衛，不是這道。
        """
        self.authenticate(self.tenant_login, self.tenant_login)
        with patch(_UPSTREAM, autospec=True) as upstream:
            forged = self._issue_via_rpc(_FORGED)
            control = self._issue_via_rpc()
        self.assertFalse(
            upstream.called,
            '🔴 #787：偽造的 `woow_force_issue` 讓租戶走進了上游 '
            'create_ecpay_invoice —— 這把鑰匙**租戶可達**，等於一個「任意觸發真'
            '發票開立」的後門')
        self.assertIn(
            'error', forged,
            '偽造的 key 不得讓這支呼叫變成成功（回 %s）'
            % self._error_text(forged))
        self.assertEqual(
            self._error_name(forged), self._error_name(control),
            '偽造與不偽造必須被**同一道**守衛以同一種型別擋下。不同型別代表 key '
            '真的改變了控制流：forged=%r control=%r'
            % (self._error_text(forged), self._error_text(control)))


class TestForceIssueInternalIdentityForgery787(_ForceIssue787Fixture):
    """P：判準不是「你是誰」——連拿得到 `account.move` ACL 的身分也不得靠 wire 開閘。

    這一組守的是**修法的形狀**。若哪天有人把判定改成「呼叫端是不是內部使用者」
    之類的替代設計，T2（portal）會照樣綠，只有這一格會紅。

    ⚠ 這一組刻意**不是**在說「會計人員不該開發票」——他要開有的是正當按鈕
    （`action_woow_retry_ecpay_invoice`，走 cron 的 `_issue_one`）。它說的是
    「**這把 context key** 不是任何人的授權憑證」。
    """

    def test_P1_control_without_key_short_circuits(self):
        """對照：有 ACL 的身分不帶 key ⇒ 短路，upstream 不被呼叫。"""
        self.authenticate(self.accountant_login, self.accountant_login)
        with patch(_UPSTREAM, autospec=True) as upstream:
            self._assert_ok(
                self._issue_via_rpc(),
                'account.group_account_invoice 呼叫 create_ecpay_invoice 不該回錯')
        self.assertFalse(
            upstream.called,
            '對照：帶 skip 旗標的發票在沒有開閘時必須早退——這一格若變紅，代表'
            '短路本身壞了，P2 的否定性斷言就沒有意義')

    def test_P2_forged_key_does_not_open_the_hatch_for_an_internal_user(self):
        """🔴 帶著 `account.move` 全 CRUD 的身分，偽造的 key 一樣不得開閘。

        修法前這一格是綠的（那正是「讀取點吃 wire 上的 context」的鐵證）；修法後
        它必須翻紅再翻綠——判準從「context 裡有沒有那個值」換成「那個值是不是
        伺服器端程式碼放進去的物件」。
        """
        self.authenticate(self.accountant_login, self.accountant_login)
        with patch(_UPSTREAM, autospec=True) as upstream:
            self._issue_via_rpc(_FORGED)
        self.assertFalse(
            upstream.called,
            '🔴 #787：wire 上送來的 truthy 值把閘打開了。修法的判準是**值的物件'
            '身分**，JSON 表達不出它——不論呼叫端是誰')


class TestForceIssuePortalReach787(_ForceIssue787Fixture):
    """T：**portal** 租戶（`base.group_portal`）打得到這個讀取點嗎？

    為什麼要單獨一組：`base.group_user` 打不進去**推導不出** portal 也打不進去。
    兩者的 ACL 出處完全不同——`base.group_user` 對 `account.move` 一列都沒有，而
    Odoo 18 的 `account` 明給 `base.group_portal` 一列 read=1
    （`access_account_invoice_portal`），再由 `account_invoice_rule_portal` 限縮到
    「自己的、已 post 的」那幾張。本 repo 的 signup 樣板使用者正是只有
    `base.group_portal`（`test_acl_p9.py`）⇒ 它是真的會存在的租戶身分。

    ⚠ 這一組的判準是「**守衛有沒有被繞過**」（有沒有走進 `super()`），不是「發票
    有沒有真的開出去」。兩者是不同的問題：讀取點被繞過之後，上游方法體還要先
    `uniform.invoice.create(...)`，而那個 model 的 ACL 只給 `base.group_user`
    （`ecpay_invoice_tw/security/ir.model.access.csv`）——T3 就是在量那一道。
    拿「發票沒開成」當觀測會把「守衛擋住了」與「守衛被關掉、只是撞到別的牆」混成
    同一格，而這兩件事在哪一天會出事上完全不同（SH 的 `sm_route_cascade_unlink`
    正是這個形狀：實測簽名是例外型別翻轉，不是「刪掉了沒」）。
    """

    def test_T0_portal_can_read_its_own_posted_invoice(self):
        """前提格：portal 真的看得到這張發票。

        少了它，T1／T2 的「沒走進 super()」與「這個 portal 根本讀不到這張發票、
        連讀取點都沒到」不可區分——後者會讓 T2 在這把鑰匙真的可達時仍然是綠的。
        """
        self.authenticate(self.portal_login, self.portal_login)
        result = self._call_kw(
            _MOVE, 'read', [[self.portal_invoice.id], ['name', 'state']])
        rows = self._assert_ok(
            result, 'portal 應該讀得到自己已 post 的 out_invoice')
        self.assertEqual(
            len(rows or []), 1,
            '前提：portal 必須讀得到這張發票（回 %r）。讀不到的話 T1／T2 測的是'
            '一個到不了讀取點的呼叫，兩格會恆綠' % (rows,))

    def test_T1_control_portal_without_key_short_circuits(self):
        """對照：portal 不帶 key ⇒ 短路，upstream 不被呼叫。"""
        self.authenticate(self.portal_login, self.portal_login)
        with patch(_UPSTREAM, autospec=True) as upstream:
            self._call_kw(
                _MOVE, 'create_ecpay_invoice', [[self.portal_invoice.id]])
        self.assertFalse(
            upstream.called,
            '對照：不帶 force key 的呼叫必須早退。這一格若變紅，T2 的「帶了才'
            '放行」就沒有意義了')

    def test_T2_portal_forged_key_bypasses_the_guard(self):
        """🔴 portal 身分 ＋ 偽造 key ⇒ 守衛有沒有被關掉？

        「這個結果不該出現」那一格＝`upstream.called` 為 True：那代表一個
        **只有 `base.group_portal`** 的租戶，用一行 JSON 就把
        `woow_skip_upstream_hook` 這面盾牌拆掉、讓流程走進真的會打 ECPay 的那支
        方法。此時這把鑰匙就是**租戶可達**的，與 `base.group_user` 的結論相反。
        """
        self.authenticate(self.portal_login, self.portal_login)
        with patch(_UPSTREAM, autospec=True) as upstream:
            forged = self._call_kw(
                _MOVE, 'create_ecpay_invoice', [[self.portal_invoice.id]],
                _FORGED)
        self.assertFalse(
            upstream.called,
            '🔴 #787：只有 base.group_portal 的租戶用偽造的 `woow_force_issue` '
            '走進了上游 create_ecpay_invoice —— 守衛被關掉了（回 %s）'
            % self._error_text(forged))

    def test_T3_portal_cannot_create_the_uniform_invoice_row(self):
        """量出「守衛之後的那道牆」是什麼，而不是假設它在。

        上游 `create_ecpay_invoice` 的方法體在送出之前會先
        `self.env['uniform.invoice'].create(...)`，而 `ecpay_invoice_tw` 只給
        `base.group_user` 那個 model 的 ACL。這一格把「portal 到底過不過得了那
        道牆」變成實測值——它是 T2 的結論在真實世界裡的爆炸半徑，而不是推論。

        ⚠ 這一格**不是** T2 的替代品：它量的是「就算守衛被繞過，對外送出能不能
        成立」；守衛本身有沒有被關掉只有 T2 答得出來。
        """
        self.authenticate(self.portal_login, self.portal_login)
        result = self._call_kw(
            'uniform.invoice', 'create', [{}])
        self.assertIn(
            'error', result,
            'portal 不該建得出 uniform.invoice —— 若這一格變綠，T2 的爆炸半徑'
            '就從「守衛被繞過」升級成「真的送得出去」，結論必須重寫')
        self.assertIn(
            'AccessError', self._error_name(result),
            '擋下來的必須是 ACL（AccessError），實際收到 %r'
            % self._error_text(result))

    def test_T4_forged_key_is_fail_loud(self):
        """🔴 偽造必須留下點名這把 key 的 WARNING。

        兩個作用，缺一不可：

        1. **偽造的訊號**——少了它，「守住了」與「守衛把整個 key 當成不存在」不可
           區分，而後者會讓「還有 in-process 的碼照舊傳 `True`」永遠沒有訊號。
        2. **本檔其餘每一格的前提**——判定函式是看到那個值才印這行的，所以這行
           WARNING 就是「呼叫端送的 context 真的一路送到了判定點」的回執。修法之
           後，`upstream.called` 在每一個攻擊格都是 `False`，這一格是唯一還能證明
           「RPC 不是根本沒到讀取點」的觀測。
        """
        self.authenticate(self.portal_login, self.portal_login)
        with patch(_UPSTREAM, autospec=True):
            with self.assertLogs(_MOVE_LOGGER, level='WARNING') as captured:
                self._call_kw(
                    _MOVE, 'create_ecpay_invoice',
                    [[self.portal_invoice.id]], _FORGED)
        self.assertTrue(
            any(_CTX_WOOW_FORCE_ISSUE in line for line in captured.output),
            '#787：偽造的開關必須點名 key 記一行 WARNING，實際收到 %r'
            % captured.output)


class TestForceIssueSentinelStillWorks787(_ForceIssue787Fixture):
    """G：修法的陰性對照——這把逃生門對**伺服器端程式碼**必須照舊開得了。

    少了這一組，「偽造打不開」與「這個功能被整個刪掉了」在 R／T／P 每一格的輸出
    上完全一樣，而後者的後果是**所有 PaaS 發票從此都開不出來**。

    ⚠ 這一組刻意**不**走 RPC：sentinel 的全部意義就是「呼叫端構造不出來」，所以
    它只可能在 in-process 呼叫端出現。判準是 context 值的物件同一性，與 ACL／欄位
    權限無關 ⇒ 用 in-process env 不是取巧，是它唯一正確的形狀。
    """

    def test_G1_sentinel_still_reaches_upstream(self):
        """🔴 觀測點的陽性對照：helper 開閘 ⇒ upstream 必須被呼叫。

        這是全檔唯一一格會讓 `upstream.called` 為 `True` 的測試。它一紅，所有否定
        性格的綠燈都作廢——因為那時「守住了」與「patch 點寫錯、`called` 恆為
        `False`」不可區分。
        """
        with patch(_UPSTREAM, autospec=True) as upstream:
            _forced_ecpay_issue(self.invoice).create_ecpay_invoice()
        self.assertTrue(
            upstream.called,
            '陰性對照：sentinel 路徑必須仍然走得進 super()，否則修法等於把 PaaS '
            '的發票開立整個關掉（patch 點 %r 寫錯也會落在這一格）' % _UPSTREAM)

    def test_G2_plain_true_does_not_open_the_hatch_even_in_process(self):
        """🔴 守衛比對的是**值的同一性**，不是「呼叫端是誰」，而且不開閘要**大聲**。

        這一格同時釘住兩件事：

        * 若哪天有人把判定改回 `ctx.get(key)`，R2／T2／P2 會紅；但若改成「in-process
          就放行」之類的替代設計，那幾格仍會綠，只有這一格會紅。
        * **force 型的 fail-safe 方向**：不開閘 ＝「該開的發票沒開」，而唯一的生產
          呼叫端在沒有例外時會把它記成 `issued`。所以這條路徑必須拋例外而不是靜靜
          回 `False`——否則會產生一個帳本說謊、沒有任何重試會回頭撿的無聲漏開。
          skip 型那四把的「靜靜不開閘」在這裡是**錯的方向**。
        """
        with patch(_UPSTREAM, autospec=True) as upstream:
            with self.assertLogs(_MOVE_LOGGER, level='WARNING') as captured:
                with self.assertRaises(UserError):
                    self.invoice.with_context(
                        **{_CTX_WOOW_FORCE_ISSUE: True}).create_ecpay_invoice()
        self.assertFalse(
            upstream.called,
            '#787：`True` 不是 sentinel——即使是 in-process 呼叫端也不得開閘')
        self.assertTrue(
            any(_CTX_WOOW_FORCE_ISSUE in line for line in captured.output),
            'in-process 傳 True 也必須留下點名 key 的 WARNING，實際收到 %r'
            % captured.output)

    def test_G3_explicit_false_keeps_its_meaning(self):
        """`=False` 的「照常早退」語意原封不動（key 名沒改，改的是值）。

        它同時是 G2 的對照：同一個 key、同一支呼叫，只差值是 `False` 還是 `True`
        —— `False` 一直都是「不強制」，所以它**不該**拋例外、也不該印 WARNING。
        """
        with patch(_UPSTREAM, autospec=True) as upstream:
            self.invoice.with_context(
                **{_CTX_WOOW_FORCE_ISSUE: False}).create_ecpay_invoice()
        self.assertFalse(
            upstream.called,
            '`=False` 一直都是「照常早退」，修法不得改動它')

    def test_G4_cron_hatch_is_not_poisoned_by_an_ambient_forged_context(self):
        """cron 的開閘不受呼叫端遺留在 env 裡的偽造值影響。

        `with_context()` 是 **merge**，而 helper 是用同一個 key 覆蓋成 sentinel ⇒
        就算 `_issue_one` 在一個帶著偽造值的 env 底下被呼叫（例如未來有人從
        request 觸發它），開閘走的仍然是伺服器端放進去的那個物件。

        沒有這一格的話，「helper 蓋得掉外面那個值」只是讀 code 讀出來的印象；而它
        一旦不成立，偽造者就能靠汙染 ambient context 讓 cron 拋例外、把每一張發票
        都推進重試與告警（一個安靜的計費 DoS）。
        """
        cron = self.env['woow_paas_platform.ecpay_invoice_cron']
        with patch(_UPSTREAM, autospec=True) as upstream:
            self.invoice.sudo().action_post()
            cron.with_context(
                **{_CTX_WOOW_FORCE_ISSUE: True})._issue_one(
                    self.invoice.sudo())
        self.assertTrue(
            upstream.called,
            'cron 的正當路徑必須照舊開得了閘，即使 env 裡有呼叫端留下的偽造值')
        self.assertEqual(
            self.invoice.woow_invoice_issue_state, 'issued',
            'cron 成功開立後必須落帳為 issued（回 %r）'
            % self.invoice.woow_invoice_issue_state)

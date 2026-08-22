# -*- coding: utf-8 -*-
"""#822：`action_provision` 第一次 provision 的「孤兒窗口」——`create_tunnel` 已在
Cloudflare 建出 tunnel、而它的 id 還沒落進 DB 的那一段——任何失敗都零補償。

## 缺陷（修復前）

`models/security_access.py::action_provision` 的第一次 provision 分支序列是

1. `self.write({'state': 'provisioning', ...})`（在 `try` **之外**）
2. `result = client.create_tunnel(...)` —— **CF 上的 tunnel 此刻已經存在、計費中**
3. `_internal_identity(self).write({'tunnel_id', 'tunnel_token', 'tunnel_name',
   'deployed_at'})` —— 四個欄位**同一次 write**
4. `_sync_routes_to_operator()` / `_sync_all_routes_access()`
5. `self.write({'state': ...})`

第 2 步與第 3 步落地之間就是本檔說的**孤兒窗口**。第 3 步整筆炸掉時 `tunnel_id`
與 `tunnel_token` 一起沒落地，而函式開頭的冪等守衛是 `if self.tunnel_id:` ⇒ 守衛
不成立、DB 沒有任何東西指向剛建的那顆 tunnel，四支 `except` 也一律零補償
（實查：`except AccessError` 只有 `raise`；`except Exception` 只寫 `state='error'`）。

⚠ `except Exception` 那段的註解宣稱「tunnel_id is retained, so the idempotent
retry-from-error path can finish without orphaning the tunnel」——那句話只對**第 3
步之後**失敗的形狀成立。第 3 步**本身**炸掉時 `tunnel_id` 沒 retained，該註解對這
個形狀是假的。本檔 A/B 兩組鎖的正是那個形狀。

## 本檔鎖的不變式（一句話，與修法無關）

> 一次 `action_provision` 結束後，**本次 `create_tunnel` 建出來的每一顆 tunnel，
> 要嘛被 DB 參照（`sa.tunnel_id` 等於它），要嘛被 `delete_tunnel` 回收**。

刻意寫成「參照 or 回收」而不是「一定要刪掉」或「一定要存起來」：#822 §8 列的三條
修法方向（拆 write / 補償刪除 / 交對帳）在這個判準下都算通過，判準不預選修法。
`_assert_no_orphan_tunnel()` 就是它的機械版——它比對的是**每一次 create_tunnel 的
回傳 id**（mock 每次回不同的 id，見 `_new_client`），不是「create_tunnel 被叫幾次」。
用固定 `return_value` 的 mock 在這裡會假綠：重試建的第二顆與第一顆同 id，刪掉一顆
看起來像兩顆都清了。

## 四組的分工

- **A 組（AccessError 形狀，真機制不注入）**：`with_user(<只有 base.group_user 的
  workspace owner>)` 直呼 `action_provision`，第 3 步真的撞 `tunnel_token` 的欄位
  遮罩（`groups='base.group_system'`）。這是 #805 (b) 修完之後**仍然存在**的觸發
  路徑——`action_provision` 是 public method，Odoo 原生的 `/web/dataset/call_kw`
  到得了它、不經過 BFF controller（那條路的真 HTTP 版在
  `test_sa_provision_tenant_rpc_805.py` C1）。
- **B 組（非 AccessError 形狀，注入）**：把 identity write 打成 `ValidationError`
  ——DB IntegrityError / 並發例外 / route rebuild 的 ValidationError 都是這一族。
  注入點刻意選在「vals 含 `tunnel_id`」那一次 write 上，讓紅**只可能**落在第 3 步。
- **C 組（陽性對照 ＝ 不得修過頭）**：守衛生效、happy path、以及**窗口外**的失敗
  （第 3 步已落地之後才炸）。C 組全部在修復前就是綠的，修完必須**還是**綠。
  補償一旦寫得太寬（例如不分窗口內外都刪），C2/C3/C4 會紅。
- **D 組（不可判定格）**：`create_tunnel` **自己**丟 `PaaSOperatorError` /
  `PaaSOperatorConnectionError` 時，tunnel 到底有沒有被建出來**不可知**（HTTP 可能
  在 CF 已建立之後才斷）。這一格**無法**在流程內補償（沒有 id 可刪，而盲刪同名
  tunnel 是更危險的操作）。本檔對它的表態是：**留下可對帳的記錄**——`error_message`
  帶上 `_ORPHAN_MARK` ＋ `canonical_name`，讓既有的 L4 反向對帳
  （`services/reconciler.py` 的左集是 `sa.mapped('tunnel_id')`，`kind='tunnel'`）
  把它列成孤兒候選（#385 那條路）。C4 是它的反向對照：窗口**外**的 operator 失敗
  不得帶這個標記，否則標記會退化成雜訊。
- **E 組（補償自己失敗）**：`delete_tunnel` 也炸掉時要留什麼。判準是「不得靜默」
  ——記錄要點名那顆收不回來的 tunnel id，而且**不得**因為補償失敗就把記錄推回
  `pending`（推回去等於邀請下一次重試再開一顆，而前一顆還沒收回來）。

## anti-probe 階梯（「被擋住」與「探針壞了」輸出長得一樣）

`_assert_no_orphan_tunnel()` 第一行就先斷言 `create_tunnel` **有**被呼叫過：孤兒
不變式在「一顆都沒建」時是**恆真**的，所以漏 patch、被前面某道閘擋掉、fixture 建
錯——這些都會讓本檔整組假綠。C1（守衛生效 ⇒ 0 次）與 C2（happy path ⇒ 1 次且欄位
真的落地）是這條階梯的另外兩格：C1 證「0 次」這個讀數有鑑別力，C2 證整條 mock
管線活著。

## 為什麼是 `TransactionCase` 直呼而不是 HttpCase

本檔鎖的是 **model 層**的補償語意，身分只是拿來製造第 3 步失敗的手段之一。端點層
（租戶經 `/woow` 打 provision 必須成功、三道閘一條都沒少）的覆蓋整組在
`test_sa_provision_tenant_rpc_805.py`，不在這裡。⚠ Odoo 18 的 `TransactionCase`
用的是**真** cursor，被測鏈上若有 `registry.cursor()` / `cr.commit()` 會看不到未
commit 的 fixture 而整段安靜地什麼都不做——`models/security_access.py` 與
`src/services/` 對 `registry` / `.cursor(` / `cr.commit` / `new_cr` 全零命中，
本檔的鏈不含 controller，所以這一格不成立。

🔴 **本檔不使用 `self.assertRaises` 驅動受測路徑**（`_provision_expect_access_error`
用裸 try/except 取代）。Odoo 的 `BaseCase.assertRaises` 把整個 with 區塊包進
`self.cr.savepoint(flush=False)`：例外一穿出來，區塊內做過的事**連同尚未 flush 的
cache／towrite 一起被丟掉**。#822 RED 第一輪實測到這一格：

    at_create:         ORM='provisioning' / raw SQL=('pending',)  ← 尚未 flush，值在 towrite
    after AccessError: ORM='pending'      / raw SQL=('pending',)（再 flush 一次仍是）
    對照：先 flush_all() 再讓 AccessError 丟出 ⇒ 'provisioning' 存活

⇒ 用 `assertRaises` 的話，「例外之後這一列 DB 應該長怎樣」的斷言全部是對一個**被
回捲的世界**做的：補償寫進去的 `state` / `error_message` 會一起被抹掉，那些測試
**永遠不可能綠**。而且第一輪它會以最難察覺的方式出錯——A4 假**綠**（回捲後的值
剛好等於期望的 'pending'），我因此差點把「守線已成立」寫進報告。AccessError 在
任何 SQL 之前就丟出來，cursor 不會進 aborted 狀態，所以不需要那個 savepoint 保護。

⚠ 本檔在 A 組**不**先以 su 身分讀 `tunnel_token`：su 讀會把該欄位灌進
transaction-wide 的 field cache，之後非 sudo recordset 的屬性讀取會直接命中快取而
不再重查遮罩（`controllers/smart_home.py::_provision_smarthome` 有那次 odoo-shell
實測的完整版）。只有 C2（su env 的 happy path）讀它，那裡沒有租戶身分要保護。
"""
from unittest.mock import MagicMock, patch

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.models.security_access import (
    _internal_identity as _internal,
)
from odoo.addons.woow_paas_platform.services.paas_operator import (
    PaaSOperatorConnectionError, PaaSOperatorError,
)

_MODEL = 'woow_paas_platform.security_access'

# `_get_operator_client()` 走的是 `models/security_access.py` 頂部 import 進自己
# namespace 的那一份 `get_paas_operator_client`（實查：該方法內就是
# `get_paas_operator_client(self.env)`）⇒ patch 這一個 namespace 就夠。
# 本檔不經過 controller，所以不需要 `services.paas_operator` 那一份的第二把
# patch（那把是 `test_sa_provision_tenant_rpc_805.py` 走真 HTTP 才需要的）。
MOCK_GET_CLIENT = (
    'odoo.addons.woow_paas_platform.models.security_access.'
    'get_paas_operator_client'
)

#: 「這次失敗可能在 CF 留下一顆沒有 DB 參照的 tunnel」的訊息標記（契約字串，
#: model 側同名常數 `_ORPHAN_MARK`）。刻意在測試裡寫死字面量而不是 import：
#: import 不存在的常數會讓整個模組 ImportError，連 C 組的陽性對照都一起變成
#: error ⇒ RED 那一輪就分不出「守線沒做」與「測試檔本身壞了」。
#: ⚠ 這個字串不得成為任何既有標記的超字串（`grep -rn unreferenced src/` 零命中）。
ORPHAN_MARK = 'unreferenced-tunnel'


class _Sa822Fixture(TransactionCase):
    """workspace ＋ 一個只有 `base.group_user` 的 owner ＋ 一張還沒 provision 的 SA。"""

    def setUp(self):
        super().setUp()
        self.SA = self.env[_MODEL]
        self.tenant = self.env['res.users'].create({
            'name': 'SA 822 Tenant',
            'login': 'sa_822_tenant',
            'email': 'sa_822_tenant@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'SA 822 WS', 'owner_id': self.tenant.id,
        })
        Access = self.env['woow_paas_platform.workspace_access']
        if not Access.search([('workspace_id', '=', self.workspace.id),
                              ('user_id', '=', self.tenant.id)]):
            Access.create({
                'workspace_id': self.workspace.id,
                'user_id': self.tenant.id, 'role': 'owner',
            })
        self.sa = self.SA.create({
            'name': '822 Gateway', 'workspace_id': self.workspace.id,
        })
        self.created = []

    # ---------------------------------------------------------------- #
    #  probes                                                           #
    # ---------------------------------------------------------------- #

    def _new_client(self):
        """每次 `create_tunnel` 回**不同**的 tunnel_id（孤兒不變式的前提）。"""
        client = MagicMock()

        def _create(**kwargs):
            n = len(self.created) + 1
            rec = {
                'tunnel_id': 'tun-822-%d' % n,
                'tunnel_token': 'tok-822-%d' % n,
                'tunnel_name': kwargs.get('name') or 'paas-sa-822-%d' % n,
            }
            self.created.append(rec)
            return rec

        client.create_tunnel.side_effect = _create
        client.sync_tunnel_routes.return_value = {'routes': []}
        client.delete_tunnel.return_value = {'status': 'deleted'}
        return client

    def _as_tenant(self):
        return self.SA.with_user(self.tenant).browse(self.sa.id)

    def _provision_expect_access_error(self):
        """以租戶身分呼叫 `action_provision`，要求它丟 AccessError，回傳該例外。

        🔴 刻意**不**用 `self.assertRaises`——理由見模組 docstring 那段實測：它的
        `cr.savepoint(flush=False)` 會把例外之前尚未 flush 的寫入一起丟掉，讓所有
        「例外之後 DB 長怎樣」的斷言變成對回捲後的世界做的。
        """
        try:
            self._as_tenant().action_provision()
        except AccessError as exc:
            return exc
        self.fail('前提不成立：預期 tunnel_token 欄位遮罩丟 AccessError，'
                  '但 action_provision 正常返回了 ⇒ 先查身分／ACL，'
                  '本條的結論在這個前提下沒有意義。')

    def _fresh(self):
        """以 su env 重讀本列（不讀 `tunnel_token`，見模組 docstring 的快取註記）。"""
        return self.SA.browse(self.sa.id)

    def _deleted_ids(self, client):
        return {c.args[0] for c in client.delete_tunnel.call_args_list if c.args}

    def _assert_no_orphan_tunnel(self, client, note=''):
        """本檔的核心判準。**第一行是 anti-probe**，不是不變式本身。"""
        created_ids = [r['tunnel_id'] for r in self.created]
        self.assertTrue(
            created_ids,
            '探針故障：create_tunnel 一次都沒被呼叫過 ⇒ 孤兒不變式在此恆真、'
            '這條斷言零鑑別力。可能是漏 patch、被 provision 之前的某道閘擋掉、'
            'or fixture 建錯。%s' % note,
        )
        deleted = self._deleted_ids(client)
        referenced = {t for t in (self._fresh().tunnel_id,) if t}
        orphans = [t for t in created_ids
                   if t not in deleted and t not in referenced]
        self.assertEqual(
            orphans, [],
            '孤兒 tunnel：%s 在 CF 上被建出來，但既沒有被 DB 參照'
            '（sa.tunnel_id=%r）也沒有被 delete_tunnel 回收（已刪=%r）。'
            'created=%r %s' % (orphans, self._fresh().tunnel_id, sorted(deleted),
                               created_ids, note),
        )


class TestSa822AccessErrorShape(_Sa822Fixture):
    """A 組：非 system 身分驅動 ⇒ 第 3 步撞 tunnel_token 欄位遮罩（真機制）。"""

    def test_A0_tenant_record_level_write_is_allowed(self):
        """前提對照：這個租戶對 SA 的 **record 級** write 是通的。

        沒有這一條，A1 的紅就分不出兩件事：炸在第 1 步 `state='provisioning'` 的
        record-level ACL（那樣 `create_tunnel` 根本不會被呼叫、也就沒有孤兒可談），
        還是炸在第 3 步的欄位遮罩。A1 另外用「`create_tunnel` 被呼叫過」把擋點釘在
        create_tunnel 之後，兩條合起來才鎖得住落點。
        """
        self._as_tenant().write({'name': '822 Renamed By Tenant'})
        self.assertEqual(self._fresh().name, '822 Renamed By Tenant')

    def test_A1_access_error_lands_on_the_token_field_after_create_tunnel(self):
        """重現：AccessError 點名 `tunnel_token`，且 `create_tunnel` 已經跑過。

        這一條**不是** RED——它在修復前後都該綠，是 A2/A3 的落點證明。
        """
        client = self._new_client()
        with patch(MOCK_GET_CLIENT, return_value=client):
            exc = self._provision_expect_access_error()
        self.assertEqual(
            client.create_tunnel.call_count, 1,
            '擋點若在 create_tunnel 之前，本檔談的孤兒窗口根本沒被走到。',
        )
        # 只斷言欄位名：訊息其餘部分（"Operation: write"、group 名）會隨 Odoo
        # 版本與語系漂，欄位名是這個遮罩唯一穩定的指紋。
        self.assertIn(
            'tunnel_token', str(exc),
            '紅的落點不對：AccessError 沒有點名 tunnel_token ⇒ 它可能來自 record '
            'rule 或別的欄位，而不是第 3 步的 groups=base.group_system 遮罩。'
            '實際訊息=%s' % exc,
        )
        self.assertFalse(
            self._fresh().tunnel_id,
            'tunnel_id 竟然落地了 —— 那 A2 談的孤兒形狀就不成立，本檔要重寫。',
        )

    def test_A2_access_error_must_not_leave_an_orphan_tunnel(self):
        """🔴 RED：AccessError 形狀不得留下孤兒 tunnel。"""
        client = self._new_client()
        with patch(MOCK_GET_CLIENT, return_value=client):
            self._provision_expect_access_error()
        self._assert_no_orphan_tunnel(client, note='(A2 AccessError 單次)')

    def test_A3_retry_must_not_accumulate_orphans(self):
        """🔴 RED：重試再走一次完整 create_tunnel（守衛不成立）——但不得**累積**孤兒。

        `create_tunnel` 累計 2 次本身不是缺陷，補償型修法下它本來就會是 2
        （每次自己建、自己收）。缺陷是那兩顆**都沒人指向、也沒人回收**。
        """
        client = self._new_client()
        with patch(MOCK_GET_CLIENT, return_value=client):
            for _ in range(2):
                self._provision_expect_access_error()
        self.assertEqual(
            client.create_tunnel.call_count, 2,
            '前提：守衛 `if self.tunnel_id:` 在 tunnel_id 沒落地時不成立 ⇒ 第二次'
            '仍會重走完整 create_tunnel。這條前提若不再成立（例如改採「id 先落地」'
            '的修法），本測試要改寫成該修法的形狀，不要直接放寬。',
        )
        self.assertEqual(len(self.created), 2)
        self._assert_no_orphan_tunnel(client, note='(A3 AccessError 重試兩次)')

    def test_A4_reclaimed_failure_leaves_a_retryable_state(self):
        """🔴 RED ＋ ⚠ 設計取捨：補償成功後記錄要回得去可重試的狀態。

        #822 §2 (A)：`state='provisioning'` 在 `try` 之外先落地，`except AccessError`
        只 `raise` 不改狀態，而 BFF controller 的 `except Exception` 把它吞成 HTTP
        200 ⇒ 交易 commit、`'provisioning'` 留在 DB；detail page 的 Provision 鈕只認
        `pending` / `error`、`action_refresh_status` 在無 tunnel_id 時 early-return、
        也沒有 cron 收斂 ⇒ 使用者看到的是一個沒有出口的狀態。

        補償成功之後 CF 上什麼都沒有了，`'pending'` 才是這一列的**真實**狀態。
        ⚠ 這條與「AccessError 不得降級」的守則相容——例外照樣 re-raise（A1 已鎖），
        改的只是狀態欄位的準確性，不是把 denial 說成 benign。**但這仍是一個取捨**
        （見 PR body 的「⚠ 有取捨待裁」）：另一種讀法是「停在 provisioning 讓人工
        看得見有東西壞了」。E1 鎖住它的邊界——補償**失敗**時不得推回 pending。
        """
        client = self._new_client()
        with patch(MOCK_GET_CLIENT, return_value=client):
            self._provision_expect_access_error()
        self.assertEqual(
            self._fresh().state, 'pending',
            '補償成功（CF 上已無此 tunnel）之後記錄仍停在 %r ⇒ Provision 鈕不會'
            '出現、refresh 也 early-return，使用者沒有出口（#822 §2 (A)）。'
            % self._fresh().state,
        )


class TestSa822NonAccessErrorShape(_Sa822Fixture):
    """B 組：identity write 炸非 AccessError（DB IntegrityError / 並發 / 驗證）。"""

    def _patched_identity_write(self):
        """只在 vals 含 `tunnel_id` 的那一次 write 上引爆。

        釘落點用的：`action_provision` 全程只有第 3 步那一次 write 帶
        `tunnel_id`（第 1 / 5 步寫的是 `state` / `error_message`），所以紅**只可能**
        落在孤兒窗口內，不可能是 setup 或收尾寫壞。補償自己要寫的 `state` /
        `error_message` 也照樣放行。
        """
        model_cls = type(self.SA)
        original = model_cls.write

        def _explode(record_self, vals):
            if 'tunnel_id' in vals:
                raise ValidationError(
                    '#822 injected: identity write exploded (vals=%s)'
                    % sorted(vals))
            return original(record_self, vals)

        return patch.object(model_cls, 'write', _explode)

    def test_B1_injection_lands_inside_the_orphan_window(self):
        """前提：注入的例外確實落在 create_tunnel 之後、tunnel_id 之前。"""
        client = self._new_client()
        with patch(MOCK_GET_CLIENT, return_value=client), \
                self._patched_identity_write():
            self.sa.action_provision()
        self.assertEqual(client.create_tunnel.call_count, 1)
        self.assertFalse(self._fresh().tunnel_id)
        self.assertEqual(
            self._fresh().state, 'error',
            '非 AccessError 應該走 `except Exception` 落 error（fail loud）。'
            'state=%r error_message=%r'
            % (self._fresh().state, self._fresh().error_message),
        )
        self.assertIn('#822 injected', self._fresh().error_message or '')

    def test_B2_non_access_error_must_not_leave_an_orphan_tunnel(self):
        """🔴 RED：非 AccessError 形狀同樣不得留下孤兒。"""
        client = self._new_client()
        with patch(MOCK_GET_CLIENT, return_value=client), \
                self._patched_identity_write():
            self.sa.action_provision()
        self._assert_no_orphan_tunnel(client, note='(B2 注入 ValidationError)')

    def test_B3_retry_must_not_accumulate_orphans(self):
        """🔴 RED：從 `error` 重試再建一顆，兩顆都不得成為孤兒。"""
        client = self._new_client()
        with patch(MOCK_GET_CLIENT, return_value=client), \
                self._patched_identity_write():
            self.sa.action_provision()
            self.sa.action_provision()
        self.assertEqual(client.create_tunnel.call_count, 2)
        self._assert_no_orphan_tunnel(client, note='(B3 注入重試兩次)')


class TestSa822CompensationMustNotOverreach(_Sa822Fixture):
    """C 組：陽性對照。修復前全綠，修完必須**還是**全綠。"""

    def test_C1_idempotency_guard_still_short_circuits(self):
        """`tunnel_id` 已設 ⇒ create_tunnel 0 次（守衛活著＝「0 次」這讀數有鑑別力）。"""
        _internal(self.sa).write({'tunnel_id': 'tun-822-existing',
                                  'tunnel_name': 'paas-sa-822-existing'})
        client = self._new_client()
        with patch(MOCK_GET_CLIENT, return_value=client):
            self.sa.action_provision()
        self.assertEqual(client.create_tunnel.call_count, 0)
        self.assertEqual(client.delete_tunnel.call_count, 0)
        self.assertEqual(self._fresh().tunnel_id, 'tun-822-existing')

    def test_C2_happy_path_stores_the_tunnel_and_deletes_nothing(self):
        """整條 mock 管線活著＋補償不得誤刪成功的 tunnel。"""
        client = self._new_client()
        with patch(MOCK_GET_CLIENT, return_value=client):
            self.sa.action_provision()
        self.assertEqual(client.create_tunnel.call_count, 1)
        self.assertEqual(
            client.delete_tunnel.call_count, 0,
            '成功的 provision 竟然呼叫了 delete_tunnel ⇒ 補償寫得太寬，'
            '會把剛建好的 tunnel 刪掉。',
        )
        fresh = self._fresh()
        self.assertEqual(fresh.tunnel_id, 'tun-822-1')
        self.assertEqual(fresh.tunnel_token, 'tok-822-1')
        self.assertEqual(fresh.state, 'active_no_route')
        self._assert_no_orphan_tunnel(client, note='(C2 happy path)')

    def test_C3_failure_after_the_window_keeps_the_tunnel(self):
        """窗口**外**失敗（tunnel_id 已落地）⇒ 不得補償刪除，tunnel_id 必須留著。

        這正是原始 docstring 說的 stage-2 形狀：retry-from-error 靠的就是那顆留著的
        tunnel_id 走 re-sync 分支收斂。補償若不分窗口內外，這條會紅。
        """
        client = self._new_client()
        client.sync_tunnel_routes.side_effect = PaaSOperatorError(
            'stage-2 sync boom')
        with patch(MOCK_GET_CLIENT, return_value=client):
            self.sa.action_provision()
        fresh = self._fresh()
        self.assertEqual(fresh.tunnel_id, 'tun-822-1',
                         '窗口外的失敗把 tunnel_id 弄丟了 ⇒ 重試會再開一顆。')
        self.assertEqual(client.delete_tunnel.call_count, 0)
        self.assertEqual(fresh.state, 'error')
        self._assert_no_orphan_tunnel(client, note='(C3 stage-2 失敗)')

    def test_C4_failure_after_the_window_carries_no_orphan_mark(self):
        """窗口外的 operator 失敗**不得**帶「可能有孤兒」標記（否則標記變雜訊）。

        D1 那個標記的價值全在於它稀少且精確——每一次 sync 失敗都掛上它，人工就
        再也不會去看它了。這是 D1 的反向對照。
        """
        client = self._new_client()
        client.sync_tunnel_routes.side_effect = PaaSOperatorConnectionError(
            'stage-2 connection boom')
        with patch(MOCK_GET_CLIENT, return_value=client):
            self.sa.action_provision()
        self.assertNotIn(ORPHAN_MARK, self._fresh().error_message or '')

    def test_C5_generic_failure_after_the_window_keeps_the_tunnel(self):
        """窗口外的**非** operator 失敗（走 `except Exception`）同樣不得補償刪除。"""
        client = self._new_client()
        client.sync_tunnel_routes.side_effect = ValueError('stage-2 generic boom')
        with patch(MOCK_GET_CLIENT, return_value=client):
            self.sa.action_provision()
        self.assertEqual(self._fresh().tunnel_id, 'tun-822-1')
        self.assertEqual(client.delete_tunnel.call_count, 0)
        self.assertEqual(self._fresh().state, 'error')


class TestSa822IndeterminateCreateFailure(_Sa822Fixture):
    """D 組：`create_tunnel` 自己炸 ⇒ CF 上有沒有東西**不可知**（#822 §3 尾註）。"""

    def _assert_indeterminate(self, client):
        fresh = self._fresh()
        self.assertEqual(fresh.state, 'error')
        self.assertEqual(
            client.delete_tunnel.call_count, 0,
            '沒有 tunnel_id 可刪，任何「猜一個 id / 依名字盲刪」都是比原缺陷更'
            '危險的操作（同名 tunnel 可能是別人的）。',
        )
        self.assertIn(
            ORPHAN_MARK, fresh.error_message or '',
            '🔴 這一格補償不了，唯一能做的是**留下可對帳的記錄**。'
            'error_message=%r' % fresh.error_message,
        )
        self.assertIn(
            fresh.canonical_name, fresh.error_message or '',
            '記錄要帶 canonical_name —— 那是 CF 上那顆 tunnel 的名字'
            '（`create_tunnel(name=self.canonical_name)`），也是人工／L4 反向對帳'
            '（services/reconciler.py，左集＝sa.mapped("tunnel_id")，kind="tunnel"）'
            '唯一能把它認出來的鍵。error_message=%r' % fresh.error_message,
        )

    def test_D1_operator_error_records_a_reconcilable_marker(self):
        """🔴 RED：`PaaSOperatorError` 從 create_tunnel 丟出 ⇒ 留對帳記錄。"""
        client = self._new_client()
        client.create_tunnel.side_effect = PaaSOperatorError('CF 500 after create')
        with patch(MOCK_GET_CLIENT, return_value=client):
            self.sa.action_provision()
        self._assert_indeterminate(client)

    def test_D2_connection_error_records_a_reconcilable_marker(self):
        """🔴 RED：連線中斷更是典型——CF 可能已建好、回應在路上斷掉。"""
        client = self._new_client()
        client.create_tunnel.side_effect = PaaSOperatorConnectionError('timeout')
        with patch(MOCK_GET_CLIENT, return_value=client):
            self.sa.action_provision()
        self._assert_indeterminate(client)


class TestSa822CompensationItselfFails(_Sa822Fixture):
    """E 組：補償自己失敗時留什麼（#822 §8 (b) 的那個 ⚠）。"""

    def test_E1_unreclaimed_tunnel_is_recorded_and_not_pushed_back_to_pending(self):
        """🔴 RED：`delete_tunnel` 也炸 ⇒ 記錄要點名那顆 id，且不得回 `pending`。

        回 `pending` 等於邀請下一次重試再建一顆，而前一顆還在 CF 上沒收回來——那
        會把 #822 §2 (A) 的「一次性一顆」變回初版誤判的「每次一顆」。
        """
        client = self._new_client()
        client.delete_tunnel.side_effect = PaaSOperatorError('delete refused')
        with patch(MOCK_GET_CLIENT, return_value=client):
            self._provision_expect_access_error()
        fresh = self._fresh()
        self.assertEqual(
            client.delete_tunnel.call_count, 1,
            '補償根本沒被嘗試 ⇒ 本條零鑑別力（探針階梯）。',
        )
        self.assertNotEqual(
            fresh.state, 'pending',
            '補償失敗（CF 上那顆還在）卻把記錄推回 pending ⇒ 下一次重試會再開一顆。',
        )
        self.assertIn(ORPHAN_MARK, fresh.error_message or '')
        self.assertIn(
            'tun-822-1', fresh.error_message or '',
            '收不回來的那顆 tunnel id 必須寫進記錄，否則它就真的沒有任何東西'
            '指向它了。error_message=%r' % fresh.error_message,
        )

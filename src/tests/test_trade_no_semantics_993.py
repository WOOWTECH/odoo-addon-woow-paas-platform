"""#993 — `woow.billing.incident.trade_no` 統一存 **provider** TradeNo（Eugene 裁決 A）。

病：`trade_no`（`src/models/billing_incident.py:371`）是 `fields.Char(index=True)`
——**無 help、無 UNIQUE、沒有任何語意宣告**，於是兩個寫入者各自假設了不同意義：

* PR-E（`src/models/wallet_topup.py:447`）寫 `tx.reference`＝我方 MerchantTradeNo
  （與同一個 dict 裡的 `'external_ref': tx.reference` **完全同值** ⇒ 冗餘）；
* PR-G（`src/controllers/ecpg_callback.py:98`）寫 `order.get('TradeNo')`＝**ECPay 配發**的交易序號。

覆寫怎麼發生：`_upsert_incident_snapshot` 的
`explicit_evidence = [name for name in values if name not in ('external_ref','kind','state')]`
（`billing_incident.py:1200-1203`）把 `trade_no` 展成 `trade_no = EXCLUDED.trade_no`
（`:1219-1223`）⇒ callback evidence 先寫入真 TradeNo（`kind` 保持 `intent`）之後，
PR-E 的 promote 會**把它覆寫成 `tx.reference`**。

🔴 損害不只是「對帳可讀性」：vendored addon 只在**查得到 tx** 時才把真 TradeNo 寫進
`tx.provider_reference`，而 PR-E 的場景正是「查不到 tx」⇒ **`incident.trade_no` 是平台
唯一一份 ECPay 交易編號**，被覆寫等於銷毀。而且**完全無聲**——`_TRADE_NO_RE`
（`billing_incident.py:589`）對兩種形狀都放行，validator 不會擋、log 不會叫。

## 本檔驗什麼（RED 階段：T1–T3）

| # | 性質 | RED 預期 |
| --- | --- | --- |
| T1 | callback evidence 收到的真 TradeNo，**不會**被 PR-E 的 promote 覆寫 | 🔴 **紅**（現行碼會覆寫成 `tx.reference`） |
| T2 | 探針自驗：那個序列**真的走到了 promote** | 🟢 綠（否則 T1 的紅可能只是「promote 沒跑」） |
| T3 | 沒有 callback evidence 時，PR-E 路徑**不寫** `trade_no` | 🔴 紅（現行碼會寫 `tx.reference`） |

T1 與 T3 是同一個根因的兩條不同路徑（有／沒有 callback evidence）。

⚠ **走生產碼、不自己組 snapshot**：T1／T3 呼叫的是
`woow.wallet.topup._log_charged_not_credited_incident()`（`wallet_topup.py:400`）
——⛔ 不直接呼叫 `billing_incident._log_charged_not_credited()`。理由：測試若自己把
`'trade_no': ...` 放進 snapshot，那改生產碼**不會**讓測試轉綠，這一組就完全沒有鑑別力。

讀回用**獨立 cursor**（沿 `test_billing_incident_864.py` 的 `_read` 慣例）——`_log()` 系列
的 commit 走另一條連線，而 `TransactionCase` 自己的 `self.env.cr` 是 REPEATABLE READ，
同一個 test 內用 ORM 讀不到剛被別的 cursor commit 的列。
"""
from types import SimpleNamespace

from odoo.tests.common import TransactionCase
from odoo.tests import tagged


@tagged('post_install', '-at_install')
class TestTradeNoSemantics993(TransactionCase):
    """`trade_no` 只裝 provider TradeNo；我方 reference 是 `external_ref` 的職責。"""

    #: callback evidence 寫入的「ECPay 真單號」代表值。刻意**不是** `TOPUP` 開頭
    #: ——它要與我方 reference（`TOPUP` + 15 碼 hex）在形狀上可區分，
    #: 否則 T1 分不出「保住真值」與「剛好覆寫成一樣的東西」。
    PROVIDER_TRADE_NO = '2409021234567890'

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({
            'name': '993 TradeNo Semantics',
        })
        self.partner.sudo().add_wallet_points(100.0, description='993 seed')
        self.partner.invalidate_recordset(['wallet_balance'])
        self._refs_to_purge = []
        self.addCleanup(self._purge)

    # ── fixture ───────────────────────────────────────────────────────
    def _purge(self):
        """incident 由獨立 cursor commit，TransactionCase 的回捲清不掉它。"""
        if not self._refs_to_purge:
            return
        with self.env.registry.cursor() as cr:
            cr.execute(
                'DELETE FROM woow_billing_incident WHERE external_ref = ANY(%s)',
                (self._refs_to_purge,),
            )
            cr.commit()

    def _read_incident(self, external_ref):
        """用新 cursor 讀（外層是 REPEATABLE READ 快照，看不見獨立 cursor 的 commit）。"""
        with self.env.registry.cursor() as cr:
            cr.execute(
                'SELECT kind, state, trade_no, external_ref '
                'FROM woow_billing_incident WHERE external_ref = %s',
                (external_ref,),
            )
            row = cr.fetchone()
        if not row:
            return None
        return dict(zip(('kind', 'state', 'trade_no', 'external_ref'), row))

    def _make_topup(self, amount=50.0):
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=amount,
        )
        return topup

    def _fake_tx(self, reference):
        """`_log_charged_not_credited_incident` 只讀 `tx.reference` 與 `tx.id`。

        用替身而不是真的 `payment.transaction`：這一組要驗的是**那支 method 組
        snapshot 的邏輯**，不是 ECPay provider 的設定；建真 tx 會把整組測試綁上
        provider fixture（既有 `test_charged_not_credited_incident_864.py` 因此
        帶了 `skipTest("ECPay provider not configured")` 的逃生門，而 skip 掉的
        測試在 `of N tests` 裡看起來與通過完全一樣）。
        """
        return SimpleNamespace(reference=reference, id=0)

    def _pre_register_intent(self, reference, topup):
        """PR-G 的預登記：`kind='intent'`，⛔ 不帶 `trade_no`（同生產碼）。"""
        self._refs_to_purge.append(reference)
        return self.env['woow.billing.incident']._log({
            'external_ref': reference,
            'kind': 'intent',
            'partner_ref': self.partner.id,
            'amount': 50.0,
            'source_model': 'woow.wallet.topup',
            'source_ref': topup.id,
            'detail': '993 test intent',
        })

    def _record_callback_evidence(self, reference, trade_no):
        """PR-G 的 callback evidence：更新 evidence 欄位，**保留該列原本的 kind**。"""
        return self.env['woow.billing.incident']._log_callback_evidence({
            'external_ref': reference,
            'kind': 'callback_tx_missing',
            'rtn_code': '1',
            'trade_no': trade_no,
            'detail': '993 test callback evidence',
        })

    def _run_pr_e_write(self, topup, tx):
        """走**生產碼**的 CHARGED_NOT_CREDITED 寫入點（⛔ 不自己組 snapshot）。"""
        topup._log_charged_not_credited_incident(
            tx, None, 50.0, self.env.company.currency_id,
        )

    # ── T2：探針自驗（先放，因為 T1 的鑑別力依賴它）──────────────────
    def test_T2_probe_the_promote_really_happens(self):
        """序列真的走到 `intent → charged_not_credited` 的 promote。

        沒有這一條，T1 的紅可能只是「promote 根本沒跑」——那樣 T1 綠不綠
        都與缺陷無關。
        """
        topup = self._make_topup()
        reference = 'TOPUP993PROBE0000001'
        self._pre_register_intent(reference, topup)
        self._record_callback_evidence(reference, self.PROVIDER_TRADE_NO)

        before = self._read_incident(reference)
        self.assertIsNotNone(before, '前提：預登記必須真的落一列')
        self.assertEqual(
            before['kind'], 'intent',
            'callback evidence ⛔ 不得改動該列的 kind（它只更新 evidence 欄位）')

        self._run_pr_e_write(topup, self._fake_tx(reference))

        after = self._read_incident(reference)
        self.assertIsNotNone(after)
        self.assertEqual(
            after['kind'], 'charged_not_credited',
            'promote 必須真的發生（intent → charged_not_credited），'
            '否則 T1 驗的是一條沒被走到的路徑')

    # ── T1：核心（RED 這一趟必須紅）──────────────────────────────────
    def test_T1_provider_trade_no_survives_the_charged_not_credited_promote(self):
        """🔴 callback evidence 收到的真 TradeNo ⛔ 不得被 PR-E 的 promote 覆寫。

        **RED 這一趟這條會紅**，且紅在這個確切的值上：
        `trade_no` 會變成 `tx.reference`（我方 MerchantTradeNo）而不是
        `PROVIDER_TRADE_NO`。那正是 #993 的缺陷。

        修法（裁決 A）：PR-E 不再把 `trade_no` 放進 snapshot ⇒ `explicit_evidence`
        不再含它 ⇒ `ON CONFLICT DO UPDATE SET` 不再觸碰該欄 ⇒ 真值保住。
        """
        topup = self._make_topup()
        reference = 'TOPUP993SURVIVE00001'
        self._pre_register_intent(reference, topup)
        self._record_callback_evidence(reference, self.PROVIDER_TRADE_NO)

        seeded = self._read_incident(reference)
        self.assertEqual(
            seeded['trade_no'], self.PROVIDER_TRADE_NO,
            '前提：callback evidence 必須先真的把 provider TradeNo 寫進去，'
            '否則下面那條斷言是恆真的')

        self._run_pr_e_write(topup, self._fake_tx(reference))

        after = self._read_incident(reference)
        self.assertEqual(
            after['trade_no'], self.PROVIDER_TRADE_NO,
            'provider TradeNo 被 PR-E 的寫入覆寫掉了——它是平台唯一一份 ECPay '
            '交易編號（vendored addon 只在查得到 tx 時才寫 tx.provider_reference，'
            '而這個場景正是查不到）。實際值 %r 若等於 external_ref %r，'
            '就是 #993 的缺陷本身。' % (after['trade_no'], reference))

    # ── T3：同根因的另一條路徑（RED 這一趟也應紅）────────────────────
    def test_T3_pr_e_does_not_write_our_own_reference_into_trade_no(self):
        """🔴 沒有 callback evidence 時，PR-E 路徑 ⛔ 不得往 `trade_no` 寫東西。

        `trade_no` 的語意是「ECPay 那邊確實有這筆」的證據；我方 reference 已經
        在 `external_ref` 裡了（同一列、同一個值），寫進來既冗餘、又會在下一次
        callback evidence 到達前冒充成 provider 值。

        **RED 這一趟這條會紅**：現行碼會讓 `trade_no == external_ref`。
        """
        topup = self._make_topup()
        reference = 'TOPUP993NOWRITE00001'
        self._pre_register_intent(reference, topup)

        self._run_pr_e_write(topup, self._fake_tx(reference))

        after = self._read_incident(reference)
        self.assertIsNotNone(after, '前提：promote 必須真的落地')
        self.assertEqual(
            after['kind'], 'charged_not_credited',
            '前提：promote 必須真的發生，否則本條驗的是沒被走到的路徑')
        self.assertIsNone(
            after['trade_no'],
            'PR-E 路徑不得寫 trade_no（沒有 provider callback 就沒有 provider '
            'TradeNo 可寫）。實際值 %r —— 若等於 external_ref %r，'
            '就是把我方 reference 冒充成 provider 單號。'
            % (after['trade_no'], reference))

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
import importlib.util
import logging
import os
from types import SimpleNamespace
from unittest.mock import patch

import odoo
from odoo.modules.migration import MigrationManager
from odoo.tests.common import TransactionCase
from odoo.tests import tagged

#: 本 PR 新增的 migration。⚠ 目錄名與 `src/__manifest__.py` 的版號必須同步
#: （`manifest-version-gate` 的規則 B；且 Odoo 對「目錄版號 > manifest」的
#: migration 是**靜默 no-op**）。T7 就是在守這一格。
_MIGRATION_VERSION = '18.0.1.0.177'
_MODULE = 'woow_paas_platform'
_MIGRATION_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', 'migrations',
                 _MIGRATION_VERSION)
)

def _load_migration_module():
    """按路徑載入 migration 模組**物件**（沿 test_agent_pod_phase_a_migration 慣例）。

    回**模組**而不是只回 `migrate`：T4 要對它的 `_logger` 做 `assertLogs`，
    而那個 logger 的**名字是猜不到的**（見 T4 docstring）。

    ⚠ 這種載入法**證明不了 Odoo 的 loader 會發現這個檔**——目錄名或檔名寫錯
    （`post_migrate.py` vs `post-migrate.py`、版號段多一位）時它照樣載得到，
    而 prod 升版靜默不跑。那一格由 T7 用 Odoo 自己的 loader 守。
    """
    path = os.path.join(_MIGRATION_DIR, 'post-migrate.py')
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location(
        'woow_993_post_migrate_%s' % _MIGRATION_VERSION.replace('.', '_'), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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

    # ── T4–T6：backfill migration（合成資料的陽性＋陰性對照）──────────
    def _seed_incident_row(self, external_ref, trade_no,
                           kind='charged_not_credited'):
        """用生產碼的 `_log()` seed 一列（⛔ 不自己寫 INSERT）。

        ⚠ 第一版我手寫 `INSERT INTO woow_billing_incident (...)`，漏掉
        `skip_count`（NOT NULL 無 default）⇒ 整條 `NotNullViolation`，T4／T5
        在 CI 是 **ERROR 不是 FAIL**。教訓：seed 的欄位清單是對 schema 的
        **假設**，而生產碼的 `_log()` 已經把那份假設維護好了（`insert_values`
        的那串 `setdefault`）——用它就不會隨 schema 演進而腐爛。
        """
        self._refs_to_purge.append(external_ref)
        snapshot = {
            'external_ref': external_ref,
            'kind': kind,
            'partner_ref': self.partner.id,
            'detail': '993 backfill fixture',
        }
        if trade_no is not None:
            snapshot['trade_no'] = trade_no
        incident_id = self.env['woow.billing.incident']._log(snapshot)
        self.assertTrue(
            incident_id,
            'fixture seed 失敗（_log 回 False ⇒ snapshot 被 validator 拒收）；'
            'external_ref=%r trade_no=%r' % (external_ref, trade_no))
        return incident_id

    def _read_trade_no(self, external_ref):
        with self.env.registry.cursor() as cr:
            cr.execute(
                'SELECT trade_no FROM woow_billing_incident '
                'WHERE external_ref = %s', (external_ref,))
            row = cr.fetchone()
        return row[0] if row else None

    def test_T4_backfill_is_idempotent_and_logs_its_rowcount(self):
        """跑兩次：第一次清掉、第二次 0 列；兩次都必須 **log 出 rowcount**。

        🔴 為什麼一定要 log 數字：沒有它，「跑了但 0 列」與「根本沒跑到」在 log 上
        完全不可區分——那正是本 issue 家族一直在修的那種無聲。

        ⚠ `assertLogs` 的 level 傳**數值** `logging.INFO`，⛔ 不傳字串：Odoo 的
        `addLevelName(RUNBOT=25, "INFO")` 覆寫了 stdlib 的名稱表，字串 `'INFO'`
        會被解析成 25 ⇒ `_logger.info()`（20）落在門檻外 ⇒ 恆抓不到。

        🔴 **`assertLogs` 吃的是 Logger 物件，⛔ 不是猜出來的 dotted 名。**
        migration 模組的 `logging.getLogger(__name__)` 綁的是**載入時的 `__name__`**，
        而那個值有三種互不相同、且**非父子關係**的可能：

        * 本檔用 `spec_from_file_location('woow_993_post_migrate_18_0_1_0_177', …)`
          載入 ⇒ `__name__` 就是那個字串；
        * Odoo 真跑時走 `odoo.modules.migration.load_script`，給的 `__name__`
          只是 `'post-migrate'`；
        * 「看起來合理」的 `odoo.addons.woow_paas_platform.migrations.<版本>.post-migrate`
          **從來不是**其中任何一個。

        ⇒ 傳名字進 `assertLogs` 會抓不到任何一條 record，錯誤訊息是
        `no logs of level INFO or higher triggered on …`——那長得像「migration 沒印
        log」（生產碼的鍋），實際上是測試自己對名字的假設錯了。
        傳 `module._logger` 就完全跳過命名這一層。
        """
        module = _load_migration_module()
        self.assertIsNotNone(
            module, 'src/migrations/%s/post-migrate.py 必須存在' % _MIGRATION_VERSION)
        migrate = getattr(module, 'migrate', None)
        self.assertIsNotNone(
            migrate, 'post-migrate.py 必須 expose migrate(cr, version)')
        migration_logger = module._logger

        ref = 'TOPUP993BACKFILL0001'
        self._seed_incident_row(ref, trade_no=ref)  # trade_no == external_ref

        with self.env.registry.cursor() as cr:
            with self.assertLogs(migration_logger, level=logging.INFO) as first:
                migrate(cr, '18.0.1.0.176')
            cr.commit()
        self.assertIsNone(
            self._read_trade_no(ref),
            '第一次跑必須把「trade_no == external_ref」的列清成 NULL')
        self.assertTrue(
            any('cleared 1 row' in line for line in first.output),
            'migration 必須 log 出實際影響列數（第一次應為 1）；實際：%s'
            % first.output)

        with self.env.registry.cursor() as cr:
            with self.assertLogs(migration_logger, level=logging.INFO) as second:
                migrate(cr, '18.0.1.0.176')
            cr.commit()
        self.assertTrue(
            any('cleared 0 row' in line for line in second.output),
            '第二次跑必須是 no-op（0 列）且仍然出聲；實際：%s' % second.output)

    def test_T5_backfill_clears_only_the_masquerading_rows(self):
        """🔴 陽性＋陰性對照：該清的清、真 TradeNo ⛔ 不可誤殺、NULL 不變。

        只驗「(a) 被清掉」是不夠的——那條斷言在一個「把整欄都清成 NULL」的
        錯誤實作下**照樣綠**。必須同時釘住 (b) 不被動。
        """
        module = _load_migration_module()
        self.assertIsNotNone(module)
        migrate = getattr(module, 'migrate', None)
        self.assertIsNotNone(migrate)

        ref_a = 'TOPUP993MASQUERADE01'          # (a) trade_no == external_ref ⇒ 該清
        ref_b = 'TOPUP993REALTRADE001'          # (b) 真 provider TradeNo ⇒ ⛔ 不可動
        ref_c = 'TOPUP993NULLTRADE001'          # (c) 本來就 NULL ⇒ 不變
        self._seed_incident_row(ref_a, trade_no=ref_a)
        self._seed_incident_row(ref_b, trade_no=self.PROVIDER_TRADE_NO)
        self._seed_incident_row(ref_c, trade_no=None)

        with self.env.registry.cursor() as cr:
            migrate(cr, '18.0.1.0.176')
            cr.commit()

        self.assertIsNone(
            self._read_trade_no(ref_a), '(a) 冒充 provider 單號的我方 reference 必須被清')
        self.assertEqual(
            self._read_trade_no(ref_b), self.PROVIDER_TRADE_NO,
            '(b) 🔴 真的 provider TradeNo ⛔ 不得被誤殺——它是平台唯一一份 '
            'ECPay 交易編號')
        self.assertIsNone(
            self._read_trade_no(ref_c), '(c) 本來就 NULL 的不變')

    def test_T6_backfill_survives_a_missing_table(self):
        """表不存在時 ⛔ 不得拋例外（有人可能從沒有 PR-D 的舊 DB 升上來）。

        用 patch 讓 `to_regclass` 那一步回空，等價於「這個 DB 還沒有那張表」
        ——⛔ 不真的 DROP TABLE（那會炸掉同一個 DB 上的其他測試）。
        """
        module = _load_migration_module()
        self.assertIsNotNone(module)
        migrate = getattr(module, 'migrate', None)
        self.assertIsNotNone(migrate)

        class _NoTableCursor:
            """只回答 to_regclass ⇒ NULL；其餘 execute 一律視為不該被呼叫。"""

            def __init__(self):
                self.executed = []

            def execute(self, query, params=None):
                self.executed.append(query)
                if 'to_regclass' not in query:
                    raise AssertionError(
                        '表不存在時 ⛔ 不該再下任何查詢，實際下了：%s' % query)

            def fetchone(self):
                return (None,)

        fake = _NoTableCursor()
        try:
            migrate(fake, '18.0.1.0.176')
        except Exception as exc:  # noqa: BLE001 — 這裡就是要證明不拋
            self.fail('表不存在時 migration 不得拋例外，實際：%r' % exc)
        self.assertEqual(
            len(fake.executed), 1,
            '應該只問一次 to_regclass 就 return；實際：%s' % fake.executed)

    # ── T7：Odoo 自己的 loader 真的看得到這支 migration ───────────────
    def test_T7_odoo_loader_lists_the_migration_in_the_post_stage(self):
        """🔴 用 **Odoo 自己的 loader** 證明這支會被列進 post 階段。

        ⚠ 為什麼 T4–T6 不夠：它們用 `spec_from_file_location` **按路徑**載入，
        目錄名或檔名寫錯（`post_migrate.py` vs `post-migrate.py`、版號段多一位、
        目錄版號 > manifest）時 T4–T6 **照樣全綠**，而 prod 升版**靜默不跑**
        ——那是「測試從未執行」那一族。

        ⛔ 不自己抄 `VERSION_RE`／檔名規則當判準：那只是把同一個假設寫兩遍。
        這裡實例化真的 `MigrationManager`，讓它自己去掃檔案，再讓
        `migrate_module(pkg, 'post')` 自己去挑版本與 stage；我們只攔 `exec_script`
        看它最後**要執行哪些檔**。
        """
        manifest_version = odoo.modules.module.get_manifest(_MODULE)['version']
        # ⚠ 這裡要的是 `目錄版本 <= manifest`，**不是相等**：目錄版號比 manifest 大
        # 才會靜默 no-op（manifest-version-gate 規則 B 守的也是這個）。寫成相等的話，
        # 下一次發版把 manifest 升上去（release PR 只 bump 版號、不帶 migration）
        # 這格就會紅——2026-09-24 的 18.0.1.0.178 就是這樣紅在 develop 上，
        # 而 migration 本身完全正常（.177 <= .178，視窗內照跑）。
        from odoo.tools.parse_version import parse_version

        self.assertLessEqual(
            parse_version(_MIGRATION_VERSION), parse_version(manifest_version),
            'migration 目錄版號（%s）比 manifest（%s）大——Odoo 的執行視窗是 '
            '`installed < 目錄版本 <= manifest`，超出的那一側**靜默 no-op**'
            % (_MIGRATION_VERSION, manifest_version))

        pkg = SimpleNamespace(
            name=_MODULE,
            state='to upgrade',
            installed_version='18.0.1.0.176',
            load_version='18.0.1.0.176',
            data={'version': manifest_version},
        )
        manager = MigrationManager(self.env.cr, [pkg])

        # (1) loader 有沒有**發現**這個檔
        found = manager.migrations.get(_MODULE, {}).get('module', {})
        self.assertIn(
            _MIGRATION_VERSION, found,
            'Odoo 的 migration loader 沒有發現 %s 這個版本目錄——檢查目錄名是否'
            '符合它的 VERSION_RE。它掃到的版本：%s'
            % (_MIGRATION_VERSION, sorted(found)))
        self.assertTrue(
            any(os.path.basename(f) == 'post-migrate.py'
                for f in found[_MIGRATION_VERSION]),
            'loader 在 %s 底下沒看到 post-migrate.py，掃到的是：%s'
            % (_MIGRATION_VERSION, found[_MIGRATION_VERSION]))

        # (2) 它會不會被列進 **post** 階段執行
        executed = []

        def _capture(cr, installed_version, pyfile, addon, stage, version=None):
            executed.append((stage, os.path.basename(pyfile), version))

        with patch('odoo.modules.migration.exec_script', _capture):
            manager.migrate_module(pkg, 'post')

        self.assertTrue(
            any(stage == 'post' and name == 'post-migrate.py'
                and _MIGRATION_VERSION in (version or '')
                for stage, name, version in executed),
            'Odoo 沒有把 %s/post-migrate.py 排進 post 階段（installed=%s → '
            'manifest=%s 的視窗內）。它排到的是：%s'
            % (_MIGRATION_VERSION, pkg.installed_version, manifest_version,
               executed))

        # (3) 陰性對照：同一支 ⛔ 不該出現在 pre 階段
        executed.clear()
        with patch('odoo.modules.migration.exec_script', _capture):
            manager.migrate_module(pkg, 'pre')
        self.assertFalse(
            [e for e in executed if e[1] == 'post-migrate.py'],
            'post-migrate.py ⛔ 不該被排進 pre 階段；實際：%s' % executed)

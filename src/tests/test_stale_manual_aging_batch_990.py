"""#990：老化告警 cron 的 batch 化——unbounded 掃描 ＋ 逐 move 獨立 cursor 的 N+1。

病（#990，承 #988 ccsx 覆核 Medium-B）：`_cron_alert_stale_manual_invoices`
對 posted+manual 的 move 做 **unbounded** search，再逐 move 呼叫
`Incident._log()`，而 `_log` 每次都開一條全新 DB 連線／獨立交易 ⇒ N 筆 move
＝ N 條獨立交易。

🔴 而**簡單加 `limit` 會更糟**：逾期條件不會自消（沒人手動開票前它一直符合
domain），`_order` 讓最舊的那批永遠排在前面 ⇒ 每個 tick 的 limit 視窗都被同一
批舊 move 佔住 ⇒ **新變老的 move 永遠拿不到告警**，而那個狀態與「今天沒事做」
在輸出上完全一樣。這與 #791 覆核 F1（`access_resync_cron`）是同一個形狀。

本組測的是修法的四個性質，每一條都配探針自驗：

1. **視窗會前進**（`test_window_advances_across_runs`）——本組的核心判準。
   naive `limit` 版在這一條會紅（後排永遠拿不到 incident）。
2. **掃描與寫入分層**（`test_independent_cursor_count_matches_writes_not_scans`）
   ——直接量 `registry.cursor()` 的呼叫次數，這就是 N+1 的定義。
3. **截斷用實查判定**（`test_max_scan_truncation_*`）——陽性／陰性各一條。
4. **告警訊息報總數不報這批**（`test_activity_note_reports_total_not_batch`）
   ——分批之後 `len(moves)` 會把「總逾期數」縮水成 `limit`，而縮水的那一側
   完全無聲，正是這條告警本來要偵測的那種靜默。
"""
import logging
import re
from datetime import date, timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase
from odoo.tests import tagged

#: 生產碼的 logger 名，`assertLogs` 用。
#:
#: 🔴 底下的 `assertLogs` 一律傳**數值** `logging.INFO` / `logging.WARNING`，
#: ⛔ 不傳字串：Odoo 的 `odoo/netsvc.py` 用
#: `logging.addLevelName(logging.RUNBOT, "INFO")`（RUNBOT=25）覆寫了 stdlib 的
#: `logging._nameToLevel`，而 unittest 的 `_log.py` 正是查那張表 ⇒ 字串 'INFO'
#: 會被解析成 25、`_logger.info()`（20）全部落在門檻外 ⇒ `assertLogs` 恆紅
#: （而訊息長得像生產碼掉了 log）、`assertNoLogs` 恆綠。
#: 依據 `docs/tracking/pitfalls/assertlogs-string-level-shadowed-by-odoo.md`。
_CRON_LOGGER = (
    'odoo.addons.woow_paas_platform.models.account_move_invoice_status')


@tagged('post_install', '-at_install')
class TestStaleManualAgingBatch990(TransactionCase):
    """老化掃描的分批、去重 marker 與計數正確性。"""

    #: 與 models/account_move_invoice_status.py 的 `_MANUAL_AGING_SUMMARY`
    #: 刻意雙寫，當同步哨兵（沿 test_invoice_issue_policy_864.py 的慣例）。
    SUMMARY = '手動開票逾期：發票已過帳但 ECPay 發票號碼仍為空'

    def setUp(self):
        super().setUp()
        self.Move = self.env['account.move']
        self.partner = self.env['res.partner'].create({'name': 'AgingBatch990'})
        self._incident_refs = set()
        self.addCleanup(self._purge_incidents)

    # ── fixture ───────────────────────────────────────────────────────
    def _purge_incidents(self):
        """incident 由獨立 cursor commit，TransactionCase 的回捲清不掉它。

        形狀抄 `test_invoice_issue_policy_864.TestStaleManualInvoiceAlert864`。
        """
        if not self._incident_refs:
            return
        refs = list(self._incident_refs)
        with self.env.registry.cursor() as cr:
            cr.execute(
                'DELETE FROM mail_activity WHERE res_model = %s AND res_id IN '
                '(SELECT id FROM woow_billing_incident '
                ' WHERE external_ref = ANY(%s))',
                ('woow.billing.incident', refs))
            for table, model_column in (
                    ('mail_followers', 'res_model'),
                    ('mail_message', 'model')):
                cr.execute(
                    'DELETE FROM %s WHERE %s = %%s AND res_id IN '
                    '(SELECT id FROM woow_billing_incident '
                    ' WHERE external_ref = ANY(%%s))' % (table, model_column),
                    ('woow.billing.incident', refs))
            cr.execute(
                'DELETE FROM woow_billing_incident WHERE external_ref = ANY(%s)',
                (refs,))
            cr.commit()

    def _aged_manual_invoice(self, days_old=10):
        move = self.Move.sudo().create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'invoice_date': date.today(),
            'invoice_line_ids': [(0, 0, {
                'name': 'aging batch probe',
                'quantity': 1.0,
                'price_unit': 100.0,
            })],
        })
        move.action_post()
        move.write({'woow_invoice_issue_state': 'manual'})
        # create_date 由 ORM 自動填當下，只能用 SQL 往回撥。
        self.env.cr.execute(
            'UPDATE account_move SET create_date = %s WHERE id = %s',
            (fields.Datetime.now() - timedelta(days=days_old), move.id))
        move.invalidate_recordset(['create_date'])
        self._incident_refs.add('invoice:%s' % move.id)
        return move

    def _aged_batch(self, count, days_old=10):
        """建 `count` 張逾期發票，回傳依 id 遞增排序的 recordset。"""
        moves = self.Move.browse()
        for _i in range(count):
            moves |= self._aged_manual_invoice(days_old=days_old)
        return moves

    def _has_incident(self, move):
        """用新 cursor 讀：外層 TransactionCase 是 REPEATABLE READ 快照，
        看不見獨立 cursor 在它開始之後 commit 的列。"""
        with self.env.registry.cursor() as cr:
            cr.execute(
                'SELECT occurrences FROM woow_billing_incident '
                'WHERE external_ref = %s', ('invoice:%s' % move.id,))
            row = cr.fetchone()
        return row[0] if row else None

    def _marked(self, moves):
        """回傳 (已標 marker 的, 未標的) 兩個 recordset。"""
        moves.invalidate_recordset(['woow_aging_alerted_at'])
        marked = moves.filtered(lambda m: m.woow_aging_alerted_at)
        return marked, moves - marked

    def _activity_notes(self):
        return self.env['mail.activity'].sudo().search(
            [('summary', '=', self.SUMMARY)]).mapped('note')

    # ── 探針自驗（這兩條不綠，底下的判準都是恆真的）────────────────────
    def test_probe_fixture_really_produces_stale_rows(self):
        """fixture 建出來的 move 真的落在掃描 domain 裡。

        否則「視窗會前進」那條會因為「什麼都沒掃到」而恆真。
        """
        moves = self._aged_batch(3)
        cutoff = fields.Datetime.now() - timedelta(
            days=self.Move._woow_manual_aging_days())
        found = self.Move.sudo().search(
            self.Move._woow_stale_manual_domain(cutoff) +
            [('id', 'in', moves.ids)])
        self.assertEqual(
            found, moves,
            'fixture 必須真的落在 domain 內，否則本組其餘斷言全是恆真')

    def test_probe_limit_is_really_enforced(self):
        """`limit` 真的截住這一輪的寫入筆數。

        若 limit 沒生效（一輪就掃完），「視窗會前進」那條就退化成恆真——
        一次做完當然「前進」了。

        ⚠ 觀測用 **incident 存在性**（外部事實）而不是 marker（修法的實作
        細節）：naive-limit 的 RED 版本一樣要能跑這條並且**綠**，否則 RED
        那一輪分不出「limit 沒生效」與「修法還沒寫」。
        """
        moves = self._aged_batch(5)

        self.Move._cron_alert_stale_manual_invoices(limit=2)

        logged = [m.id for m in moves if self._has_incident(m)]
        self.assertEqual(
            len(logged), 2,
            'limit=2 這一輪只能寫 2 筆 incident，實際寫了 %s 筆' % len(logged))

    # ── 🔴 核心判準：視窗會前進 ────────────────────────────────────────
    def test_window_advances_across_runs(self):
        """🔴 本單的核心：後排不會被舊批餓死。

        naive `search(domain, limit=N)` 版在這一條會**紅**——第 2、3 輪撈到的
        還是同一批最舊的 move（逾期條件不自消 ⇒ 它們永遠在 domain 裡、永遠排
        最前面），後 3 筆一輩子拿不到 incident。

        修法讓視窗前進靠兩件事同時成立：`woow_aging_alerted_at` 讓已告警的
        離開 domain，`id > last_id` 游標讓這一輪不重掃同一列。
        """
        moves = self._aged_batch(5)

        # 🔴 觀測用 **incident 存在性**，⛔ 不用 marker：marker 是修法自己的
        # 實作細節，拿它當判準時 RED 那一輪會紅在「欄位沒被寫」而不是紅在
        # 「後排拿不到告警」——紅的原因必須是缺陷本身。
        self.Move._cron_alert_stale_manual_invoices(limit=2)
        after_first = [m.id for m in moves if self._has_incident(m)]
        self.assertEqual(
            len(after_first), 2, '前提檢查：第 1 輪應寫 2 筆')

        self.Move._cron_alert_stale_manual_invoices(limit=2)
        after_second = [m.id for m in moves if self._has_incident(m)]
        self.assertEqual(
            len(after_second), 4,
            '第 2 輪必須推進到**新的** 2 筆；naive limit 版會停在 2（撈到的'
            '還是同一批最舊的），實際 %s 筆' % len(after_second))

        self.Move._cron_alert_stale_manual_invoices(limit=2)
        starved = [m.id for m in moves if self._has_incident(m) is None]

        self.assertFalse(
            starved,
            '三輪 × limit=2 ＝ 6 次寫入機會，5 筆必須全部拿到告警；'
            '仍然餓死的 move：%s' % starved)
        for move in moves:
            self.assertEqual(
                self._has_incident(move), 1,
                'move %s 必須恰好一筆 incident 且 occurrences=1'
                '（已告警的不該被重複掃到）' % move.id)

    def test_already_alerted_rows_leave_the_scan_window(self):
        """已告警的列**離開 domain**——這是「視窗前進」的機制那一半。

        ⛔ 不是「掃到了但跳過」：留在 domain 裡只是把餓死推遲到 max_scan，
        掃描量仍會隨已告警集合線性成長。
        """
        moves = self._aged_batch(3)
        self.Move._cron_alert_stale_manual_invoices(limit=3)

        cutoff = fields.Datetime.now() - timedelta(
            days=self.Move._woow_manual_aging_days())
        still_in_domain = self.Move.sudo().search(
            self.Move._woow_stale_manual_domain(cutoff) +
            [('id', 'in', moves.ids)])

        self.assertFalse(
            still_in_domain,
            '已告警的 move 必須離開掃描 domain，否則掃描量會線性成長')

    # ── N+1 收斂：掃描與寫入分層 ──────────────────────────────────────
    def test_independent_cursor_count_matches_writes_not_scans(self):
        """直接量 N+1 的定義：獨立 cursor 只在**寫入**時開，不隨掃描量成長。

        建 6 筆逾期、limit=2 ⇒ 掃描會看到 6 筆，但只能開 2 條獨立 cursor。
        修前（unbounded ＋ 逐 move `_log`）這裡會是 6。
        """
        self._aged_batch(6)
        registry = self.env.registry
        original_cursor = registry.cursor
        opened = []

        def counting_cursor(*args, **kwargs):
            opened.append(1)
            return original_cursor(*args, **kwargs)

        with patch.object(registry, 'cursor', counting_cursor):
            self.Move._cron_alert_stale_manual_invoices(limit=2)

        self.assertEqual(
            len(opened), 2,
            '獨立 cursor 次數必須等於這一輪的寫入筆數（2），不是掃描筆數；'
            '實際開了 %s 條' % len(opened))

    def test_failed_log_still_counts_against_the_budget(self):
        """`_log` 失敗也算一次預算——⛔ 不可以「只算成功的」。

        只算成功的話，`_log` 持續失敗時這一輪會一路打到 `max_scan`，變成
        失敗時做得比成功時還多（同 #792 二輪覆核 F1(a) 的教訓）。
        """
        moves = self._aged_batch(5)
        Incident = type(self.env['woow.billing.incident'])

        with patch.object(Incident, '_log', lambda _self, _snapshot: False):
            self.Move._cron_alert_stale_manual_invoices(limit=2)

        marked, unmarked = self._marked(moves)
        self.assertFalse(
            marked, '`_log` 失敗時 ⛔ 不得標 marker（否則是永久漏告警）')
        self.assertEqual(
            len(unmarked), 5, '失敗的列必須留在 domain 裡等下一輪')

    def test_failed_log_leaves_the_row_for_the_next_run(self):
        """失敗一輪之後，下一輪（`_log` 恢復正常）撿得回同一批。"""
        moves = self._aged_batch(2)
        Incident = type(self.env['woow.billing.incident'])

        with patch.object(Incident, '_log', lambda _self, _snapshot: False):
            self.Move._cron_alert_stale_manual_invoices(limit=2)
        self.Move._cron_alert_stale_manual_invoices(limit=2)

        marked, _unmarked = self._marked(moves)
        self.assertEqual(
            len(marked), 2, '`_log` 恢復後必須把上一輪失敗的補回來')

    # ── 截斷：陽性／陰性對照各一 ──────────────────────────────────────
    def test_max_scan_truncation_warns_when_rows_remain(self):
        """踩到 `max_scan` 且後面**真的還有列** ⇒ 必須出聲（陽性）。"""
        self._aged_batch(4)

        with self.assertLogs(_CRON_LOGGER, level=logging.WARNING) as captured:
            self.Move._cron_alert_stale_manual_invoices(
                limit=100, scan_batch=2, max_scan=2)

        self.assertTrue(
            any('aging scan cap' in line for line in captured.output),
            '踩到掃描上界且後面還有列時必須 warning，實際輸出：%s'
            % captured.output)

    def test_max_scan_does_not_warn_when_the_batch_just_fits(self):
        """掃描量剛好等於 `max_scan` 但後面**沒有列**了 ⇒ ⛔ 不得發假警報（陰性）。

        🔴 這一條就是「不拿『這一批是滿的』當代理判準」的證明：用代理判準時
        剛好整除就會發假截斷警報，而訊號通膨與訊號缺席一樣糟。
        """
        self._aged_batch(2)

        with self.assertLogs(_CRON_LOGGER, level=logging.INFO) as captured:
            self.Move._cron_alert_stale_manual_invoices(
                limit=100, scan_batch=2, max_scan=2)

        self.assertFalse(
            any('aging scan cap' in line for line in captured.output),
            '後面沒有列時不得發截斷警報（假截斷），實際輸出：%s'
            % captured.output)

    # ── 每一輪都出聲 ──────────────────────────────────────────────────
    def test_empty_run_still_logs_its_counters(self):
        """零命中那輪也要出聲，且自帶計數。

        「掃了 0 列」與「掃了 500 列全被排除」是兩件事，同一句話要能分開；
        完全不出聲則與「今天沒事做」不可區分（#791 F1 的那一格）。
        """
        with self.assertLogs(_CRON_LOGGER, level=logging.INFO) as captured:
            self.Move._cron_alert_stale_manual_invoices(limit=5)

        scan_lines = [ln for ln in captured.output if 'aging scan:' in ln]
        self.assertTrue(
            scan_lines, '零命中也必須印出掃描計數行，實際：%s' % captured.output)
        self.assertTrue(
            any('alerted=0' in ln for ln in scan_lines),
            '計數行必須帶 alerted=0（而不是省略不印），實際：%s' % scan_lines)

    # ── 告警訊息的數字 ────────────────────────────────────────────────
    def test_activity_note_reports_total_not_batch(self):
        """🔴 note 的總數必須是全量，不是這一批。

        分批之後 `len(moves)` 只是這輪處理的筆數；用它當總數，管理員看到的
        數字會從「總逾期數」縮水成 `limit`——而縮水那一側完全無聲。
        """
        self._aged_batch(5)

        self.Move._cron_alert_stale_manual_invoices(limit=2)

        notes = self._activity_notes()
        self.assertTrue(notes, '必須建 mail.activity')
        note = notes[0]
        total_match = re.search(r'(\d+)\s+posted invoice', note)
        self.assertTrue(total_match, 'note 格式改了，抓不到總數：%s' % note)
        self.assertEqual(
            total_match.group(1), '5',
            'note 要報總逾期數 5（不是這批的 2）；實際 note：%s' % note)
        self.assertIn(
            '2 of them were logged in this run', note,
            'note 也要說明這一輪處理了幾筆，否則管理員無法判斷是否還在追趕')

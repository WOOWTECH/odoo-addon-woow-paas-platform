"""#864 PR-G — callback 預登記／inbox／replay 的真跨連線探針。

PR-G 的三件事全部靠**獨立 cursor 的真 commit** 才成立：

1. 付款意圖預登記（``kind='intent'``）在不可逆外呼**之前**用獨立交易寫入，
   所以外層 request 交易之後回捲也留得住。
2. ``pay_callback`` route override 把 provider 事實寫進同一列（inbox），
   ⛔ 對外 status／body 逐字元不變。
3. ``_cron_replay_callback_inbox`` 逐筆開獨立 cursor、對 topup 列
   ``FOR UPDATE NOWAIT``、鎖後重讀、走唯一入帳 choke point
   ``topup._on_payment_done(tx)``、同交易標記 incident。

⛔ **不可以用 `TransactionCase`**：它的 cursor 是真 cursor，但被測碼呼叫的
``self.env.registry.cursor()`` 是**第二條連線**，看不到本測試尚未 commit 的
fixture ⇒ 整段安靜地什麼都不做而測試全綠。
⛔ **不可以用 `HttpCase`**：``enter_test_mode`` 把 ``registry.cursor()`` 退化成
``TestCursor``（savepoint 語意）⇒ 獨立交易的斷言假綠／假紅兩種都可能。
⇒ 本檔沿 ``test_billing_incident_real_rollback_864.py`` /
``test_double_charge_cooldown_864.py`` 的 ``BaseCase`` ＋ 真 ``Registry`` cursor
骨架。

唯一允許的 mock ＝ **真正的外部 ECPay 邊界**
（``payment.transaction._send_payment_request``，沿 PR-E 既有前例）。
⛔ 零 self-mock：route／replay／``_log``／``_on_payment_done`` 一律跑真的。

執行：

    odoo -u woow_paas_platform --test-enable \\
      --test-tags callback_replay_864 --stop-after-init ...

本 tag 刻意 ``-standard``；G 不改 CI workflow（``.gitea/workflows/odoo-tests.yml``
的 ``on:`` 無 ``pull_request``，且 ``--test-tags /woow_paas_platform`` 隱含
``standard``）⇒ 必須在沙盒明確選 tag 實跑，raw log 落 durable evidence。
"""

import logging
import threading
import uuid
from datetime import timedelta

import odoo
from odoo import api, fields
from odoo.modules.registry import Registry
from odoo.tests.common import BaseCase, get_db_name, tagged

_logger = logging.getLogger(__name__)

_INCIDENT_MODEL = 'woow.billing.incident'
_INCIDENT_TABLE = 'woow_billing_incident'

# Luhn-valid test PAN (Visa test number).  Never a real card.
_LUHN_PAN = '4111111111111111'


@tagged('post_install', '-at_install', '-standard', 'callback_replay_864')
class TestCallbackEvidenceUpsert864(BaseCase):
    """G1：callback 事實的原子 upsert（⛔ 無 pre-read TOCTOU）。"""

    def setUp(self):
        super().setUp()
        self.registry = Registry(get_db_name())
        # external_ref 走 raw-MTN 值域（`_RAW_MTN_RE` = [A-Za-z0-9_-]{1,20}），
        # 與生產路徑同形；marker 讓 purge 只刪本 run 自己建的列。
        self.marker = 'G864%s' % uuid.uuid4().hex[:10].upper()
        self.addCleanup(self._purge)

    def _purge(self):
        try:
            with self.registry.cursor() as cr:
                cr.execute(
                    'DELETE FROM %s WHERE external_ref LIKE %%s'
                    % _INCIDENT_TABLE,
                    (self.marker + '%',),
                )
                cr.commit()
        except Exception:
            _logger.exception(
                '#864 PR-G callback evidence fixture cleanup failed '
                '(marker=%s)', self.marker,
            )

    # --- helpers ------------------------------------------------------- #

    def _ref(self, suffix):
        return '%s-%s' % (self.marker, suffix)

    def _intent_snapshot(self, ref, **overrides):
        values = {
            'external_ref': ref,
            'kind': 'intent',
            'partner_ref': 4242,
            'amount': 600.0,
            'credits': 600.0,
            'bonus': 60.0,
            'currency': 'TWD',
            'source_model': 'woow.wallet.topup',
            'source_ref': 909,
            'detail': '付款意圖預登記（測試 fixture）。',
        }
        values.update(overrides)
        return values

    def _callback_snapshot(self, ref, **overrides):
        values = {
            'external_ref': ref,
            'kind': 'callback_tx_missing',
            'rtn_code': '1',
            'trade_no': '%s-TN' % self.marker,
            'detail': 'ECPay callback 事實已登錄（測試 fixture）。',
        }
        values.update(overrides)
        return values

    def _call(self, method, *args, **kwargs):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            return getattr(env[_INCIDENT_MODEL], method)(*args, **kwargs)

    def _row(self, ref):
        with self.registry.cursor() as cr:
            cr.execute(
                'SELECT id, kind, state, rtn_code, trade_no, occurrences, '
                'detail, skip_reason, skip_count, attempts, last_skipped_at '
                'FROM %s WHERE external_ref = %%s' % _INCIDENT_TABLE,
                (ref,),
            )
            row = cr.fetchone()
            if not row:
                return None
            return dict(zip((
                'id', 'kind', 'state', 'rtn_code', 'trade_no', 'occurrences',
                'detail', 'skip_reason', 'skip_count', 'attempts',
                'last_skipped_at',
            ), row))

    def _force_state(self, ref, state):
        """把列推到 resolved 等終態（測試前置，⛔ 不經被測 API）。"""
        with self.registry.cursor() as cr:
            cr.execute(
                'UPDATE %s SET state = %%s, resolved_at = %%s '
                'WHERE external_ref = %%s' % _INCIDENT_TABLE,
                (state,
                 fields.Datetime.now() if state == 'resolved' else None,
                 ref),
            )
            cr.commit()

    # --- (1) 已有 intent 列：同列更新 evidence，kind 原地不動 --------------- #

    def test_callback_evidence_updates_existing_intent_row_in_place(self):
        ref = self._ref('01')
        intent_id = self._call('_log', self._intent_snapshot(ref))
        self.assertTrue(intent_id, '前置：intent 預登記必須先落地')

        evidence_id = self._call(
            '_log_callback_evidence', self._callback_snapshot(ref))

        self.assertEqual(
            evidence_id, intent_id,
            'callback 事實必須落在同一列（裁決 G1：單列生命週期），'
            '⛔ 不得另開一列',
        )
        row = self._row(ref)
        self.assertEqual(
            row['kind'], 'intent',
            'kind 不在 SET 清單裡 ⇒ 既有 intent 列的 kind 必須原地不動；'
            '若被改成 callback_tx_missing，裁決 G1 的「真孤兒專用」語意就沒了',
        )
        self.assertEqual(row['rtn_code'], '1')
        self.assertEqual(row['trade_no'], '%s-TN' % self.marker)
        self.assertEqual(
            row['occurrences'], 2,
            'evidence 更新算一次新觀測 ⇒ occurrences 必須 +1',
        )
        self.assertEqual(
            row['state'], 'open',
            'evidence 更新 ⛔ 不得動生命週期欄位（state 只由 transition 改）',
        )

    # --- (2) 完全無預登記的真孤兒：插 callback_tx_missing ------------------ #

    def test_callback_evidence_creates_orphan_row_when_no_intent_exists(self):
        ref = self._ref('02')
        self.assertIsNone(self._row(ref), '前置：這個 key 必須不存在')

        evidence_id = self._call(
            '_log_callback_evidence', self._callback_snapshot(ref))

        self.assertTrue(evidence_id)
        row = self._row(ref)
        self.assertEqual(
            row['kind'], 'callback_tx_missing',
            '裁決 G1：完全無預登記的真孤兒才用 callback_tx_missing',
        )
        self.assertEqual(row['rtn_code'], '1')
        self.assertEqual(row['occurrences'], 1)

    # --- (3) 錨1：`_log` 的異 kind collision 契約一字不動 ------------------- #

    def test_log_still_refuses_cross_kind_collision(self):
        ref = self._ref('03')
        intent_id = self._call('_log', self._intent_snapshot(ref))
        self.assertTrue(intent_id)

        result = self._call('_log', self._callback_snapshot(ref))

        self.assertIs(
            result, False,
            '⛔ PR-D 的 `_log` 對異 kind collision 仍必須拒收（回 False）；'
            '新的 `_log_callback_evidence` 是另一顆入口，'
            '⛔ 不得順手放寬 `_log` 自己的契約',
        )
        row = self._row(ref)
        self.assertEqual(row['kind'], 'intent')
        self.assertIsNone(
            row['rtn_code'],
            '被拒收的 `_log` ⛔ 不得留下任何部分寫入',
        )
        self.assertEqual(row['occurrences'], 1)

    # --- (4) 錨2：PCI allowlist 對新 upsert 路徑同樣生效 -------------------- #

    def test_callback_evidence_rejects_luhn_pan_in_detail(self):
        ref = self._ref('04')
        intent_id = self._call('_log', self._intent_snapshot(ref))
        self.assertTrue(intent_id)

        result = self._call('_log_callback_evidence', self._callback_snapshot(
            ref, detail='ECPay 回覆卡號 %s' % _LUHN_PAN))

        self.assertIs(
            result, False,
            'PAN-like free text 必須被 PR-D 的同一套 validation 擋下 —— '
            '新路徑 ⛔ 不得繞過守門',
        )
        row = self._row(ref)
        self.assertIsNone(
            row['rtn_code'],
            '整筆 snapshot 拒收 ⇒ ⛔ 不得有任何欄位落地',
        )

    def test_callback_evidence_rejects_int_rtn_code(self):
        """callback payload 的 RtnCode 是 int，⛔ 不轉 str 會整筆靜默拒收。"""
        ref = self._ref('05')
        self.assertTrue(self._call('_log', self._intent_snapshot(ref)))

        rejected = self._call(
            '_log_callback_evidence', self._callback_snapshot(ref, rtn_code=1))
        self.assertIs(
            rejected, False,
            'rtn_code 是 Char 欄位：int 必須被型別檢查擋下（這一格最容易'
            '寫成靜默失敗）',
        )

        accepted = self._call(
            '_log_callback_evidence',
            self._callback_snapshot(ref, rtn_code=str(1)))
        self.assertTrue(accepted, '轉成 str 之後必須被接受（陽性對照）')
        self.assertEqual(self._row(ref)['rtn_code'], '1')

    # --- (5) 已 resolved 的列：evidence ⛔ 不得重開 ------------------------- #

    def test_callback_evidence_does_not_reopen_resolved_row(self):
        ref = self._ref('06')
        intent_id = self._call('_log', self._intent_snapshot(ref))
        self._force_state(ref, 'resolved')

        result = self._call(
            '_log_callback_evidence', self._callback_snapshot(ref))

        self.assertEqual(
            result, intent_id,
            '晚到的重複觀測是證據不是重開許可：回同一個穩定 id',
        )
        row = self._row(ref)
        self.assertEqual(row['state'], 'resolved')
        self.assertIsNone(
            row['rtn_code'],
            '⛔ 已 resolved 的列不得被新 evidence 覆寫',
        )


@tagged('post_install', '-at_install', '-standard', 'callback_replay_864')
class TestIntentToChargedNotCreditedPromotion864(BaseCase):
    """🔴 跨 PR 交互：intent 與 charged_not_credited 撞同一個 external_ref。

    `UNIQUE(external_ref)` ＋ `_log()` 的 kind 在 INSERT 當下定死 ⇒ 一個 MTN
    只能有一列、而且 kind 改不了。PR-G 的預登記與 PR-E 的 CHARGED_NOT_CREDITED
    用的**是同一個值**（`tx.reference` ＝ MTN）⇒ 若不處理，預登記會先佔住 key，
    PR-E 那筆 `_log` 撞 kind collision 被丟棄 ⇒
    `sh_subscription_fix._double_charge_cooldown_active` 查的
    `kind='charged_not_credited'` 永遠查不到 ⇒ **PR-E 要擋的重複扣款回來了**。

    裁決 Q3＝B：原地升級 kind（`intent → charged_not_credited`），單一 statement
    原子完成，⛔ 無反向、⛔ 無其他來源 kind。
    """

    def setUp(self):
        super().setUp()
        self.registry = Registry(get_db_name())
        self.marker = 'G864C%s' % uuid.uuid4().hex[:9].upper()
        self.addCleanup(self._purge)

    def _purge(self):
        try:
            with self.registry.cursor() as cr:
                cr.execute(
                    'DELETE FROM %s WHERE external_ref LIKE %%s'
                    % _INCIDENT_TABLE, (self.marker + '%',))
                env = api.Environment(cr, odoo.SUPERUSER_ID, {})
                env['res.partner'].sudo().search(
                    [('name', 'like', self.marker)]).unlink()
                cr.commit()
        except Exception:
            _logger.exception(
                '#864 PR-G promotion fixture cleanup failed (marker=%s)',
                self.marker,
            )

    def _ref(self, suffix):
        return '%s-%s' % (self.marker, suffix)

    def _call(self, method, *args, **kwargs):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            return getattr(env[_INCIDENT_MODEL], method)(*args, **kwargs)

    def _row(self, ref):
        with self.registry.cursor() as cr:
            cr.execute(
                'SELECT id, kind, state, partner_ref, rtn_code, trade_no, '
                'failure_code, occurrences, last_seen_at '
                'FROM %s WHERE external_ref = %%s' % _INCIDENT_TABLE, (ref,))
            row = cr.fetchone()
            return row and dict(zip(
                ('id', 'kind', 'state', 'partner_ref', 'rtn_code', 'trade_no',
                 'failure_code', 'occurrences', 'last_seen_at'), row))

    def _intent(self, ref, partner_ref=4242):
        return {
            'external_ref': ref, 'kind': 'intent',
            'partner_ref': partner_ref, 'amount': 500.0,
            'source_model': 'woow.wallet.topup', 'source_ref': 909,
            'detail': '付款意圖預登記（測試 fixture）。',
        }

    def _cni(self, ref, partner_ref=4242):
        return {
            'external_ref': ref, 'kind': 'charged_not_credited',
            'partner_ref': partner_ref, 'amount': 500.0,
            'failure_code': 'wallet_credit_failed',
            'source_model': 'woow.wallet.topup', 'source_ref': 909,
            'detail': 'ECPay 已接受扣款但入帳失敗（測試 fixture）。',
        }

    # --- (1) 原地升級 kind ------------------------------------------------ #

    def test_intent_is_promoted_to_charged_not_credited_in_place(self):
        ref = self._ref('01')
        intent_id = self._call('_log', self._intent(ref))
        self.assertTrue(intent_id)

        promoted = self._call('_log_charged_not_credited', self._cni(ref))

        self.assertEqual(
            promoted, intent_id,
            '同一筆錢＝同一列（UNIQUE(external_ref) 的前提）：⛔ 不另開一列',
        )
        row = self._row(ref)
        self.assertEqual(
            row['kind'], 'charged_not_credited',
            '🔴 沒有這一步，PR-E 的 incident 會被 kind collision 丟掉，'
            '重複扣款冷卻閘就永遠查不到東西',
        )
        self.assertEqual(row['failure_code'], 'wallet_credit_failed')
        self.assertEqual(row['occurrences'], 2)

    def test_charged_not_credited_inserts_directly_when_no_intent_exists(self):
        ref = self._ref('02')
        self.assertIsNone(self._row(ref), '前置：這個 key 必須不存在')

        incident_id = self._call('_log_charged_not_credited', self._cni(ref))

        self.assertTrue(incident_id)
        row = self._row(ref)
        self.assertEqual(row['kind'], 'charged_not_credited')
        self.assertEqual(row['state'], 'open')
        self.assertEqual(row['occurrences'], 1)

    # --- (2) 只准 intent → CNI 這一個方向 --------------------------------- #

    def test_promotion_refuses_a_row_of_any_other_kind(self):
        ref = self._ref('03')
        self.assertTrue(self._call('_log_callback_evidence', {
            'external_ref': ref, 'kind': 'callback_tx_missing',
            'rtn_code': '1', 'trade_no': '%sTN' % self.marker,
            'detail': 'ECPay callback 事實（測試 fixture）。',
        }))

        result = self._call('_log_charged_not_credited', self._cni(ref))

        self.assertIs(
            result, False,
            '⛔ 只准 intent → charged_not_credited 這一個方向；'
            '其他來源 kind 一律拒收（fail-soft，但必須出聲）',
        )
        self.assertEqual(self._row(ref)['kind'], 'callback_tx_missing')

    def test_promotion_refuses_a_resolved_row(self):
        ref = self._ref('04')
        self._call('_log', self._intent(ref))
        with self.registry.cursor() as cr:
            cr.execute(
                'UPDATE %s SET state = %%s, resolved_at = %%s '
                'WHERE external_ref = %%s' % _INCIDENT_TABLE,
                ('resolved', fields.Datetime.now(), ref))
            cr.commit()

        result = self._call('_log_charged_not_credited', self._cni(ref))

        self.assertIs(result, False, '已結案的列 ⛔ 不得被重開成 CNI')
        self.assertEqual(self._row(ref)['kind'], 'intent')

    def test_promotion_refusal_is_logged_not_silent(self):
        ref = self._ref('05')
        self._call('_log_callback_evidence', {
            'external_ref': ref, 'kind': 'callback_tx_missing',
            'detail': 'ECPay callback 事實（測試 fixture）。',
        })

        with self.assertLogs(
                'odoo.addons.woow_paas_platform.models.billing_incident',
                level=logging.ERROR) as logs:
            self._call('_log_charged_not_credited', self._cni(ref))

        self.assertTrue(
            any('promotion' in line and 'refus' in line
                for line in logs.output),
            '拒收分支 fail-soft ⛔ 不代表可以靜默：'
            '「錢已經扣了但記不下來」必須出聲 logs=%r' % (logs.output,),
        )

    # --- (3) 錨4：升級之後 callback 只補 facts，kind 不回改 ---------------- #

    def test_callback_after_promotion_adds_facts_and_keeps_the_kind(self):
        ref = self._ref('06')
        self._call('_log', self._intent(ref))
        self._call('_log_charged_not_credited', self._cni(ref))

        evidence_id = self._call('_log_callback_evidence', {
            'external_ref': ref, 'kind': 'callback_tx_missing',
            'rtn_code': '1', 'trade_no': '%sTN' % self.marker,
            'detail': 'ECPay callback 事實（測試 fixture）。',
        })

        self.assertTrue(evidence_id)
        row = self._row(ref)
        self.assertEqual(
            row['kind'], 'charged_not_credited',
            '陽性對照：晚到的 callback 只補事實，⛔ 不得把 kind 改回去',
        )
        self.assertEqual(row['rtn_code'], '1')
        self.assertEqual(row['trade_no'], '%sTN' % self.marker)

    # --- (4) 錨2：等價性＝**攔截行為**相等，不是「查得到列」 ---------------- #

    def test_promoted_row_blocks_the_cooldown_exactly_like_a_fresh_insert(
            self):
        """升級出來的列，對 PR-E 冷卻閘的攔截行為必須等於直接 insert 的列。

        ⛔ 不能只斷言「列查得到」——那把「欄位語意等價」換成了「有東西在」。
        判別式是三個 partner 各自跑一次真的 `_double_charge_cooldown_active`：
        升級來的擋、直接 insert 的擋、沒有 incident 的**不**擋。
        """
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            promoted_partner = env['res.partner'].create(
                {'name': '%s-promoted' % self.marker}).id
            fresh_partner = env['res.partner'].create(
                {'name': '%s-fresh' % self.marker}).id
            control_partner = env['res.partner'].create(
                {'name': '%s-control' % self.marker}).id
            cr.commit()

        promoted_ref, fresh_ref = self._ref('P1'), self._ref('F1')
        self._call('_log', self._intent(promoted_ref, promoted_partner))
        self.assertTrue(self._call(
            '_log_charged_not_credited',
            self._cni(promoted_ref, promoted_partner)))
        self.assertTrue(self._call(
            '_log_charged_not_credited', self._cni(fresh_ref, fresh_partner)))

        def _blocked(partner_id):
            with self.registry.cursor() as cr:
                env = api.Environment(cr, odoo.SUPERUSER_ID, {})
                partner = env['res.partner'].browse(partner_id)
                return env['sh.subscription.subscription'] \
                    ._double_charge_cooldown_active(partner)

        self.assertIs(
            _blocked(promoted_partner), True,
            '🔴 升級來的列必須真的擋下重複扣款——這是本裁決的整個重點',
        )
        self.assertIs(
            _blocked(fresh_partner), True,
            '對照：直接 insert 的列（PR-E 原本的形狀）當然要擋',
        )
        self.assertIs(
            _blocked(control_partner), False,
            '陽性對照：沒有 incident 的 partner ⛔ 不可以被擋——'
            '否則上面兩個 True 只是「這支 gate 恆真」',
        )
        promoted_row, fresh_row = self._row(promoted_ref), self._row(fresh_ref)
        for field in ('kind', 'state'):
            self.assertEqual(
                promoted_row[field], fresh_row[field],
                '冷卻閘讀的 %s 欄位語意必須等價' % field,
            )


@tagged('post_install', '-at_install', '-standard', 'callback_replay_864')
class TestReplayOutcomeMarkers864(BaseCase):
    """replay 的「決定不入帳」痕跡：⛔ 不可裸 continue。

    兩形（裁決 Q2-ii）：

    * ``_mark_replay_skip_current_cursor`` —— 已持 topup 行鎖的那條 per-record
      交易內標記，與入帳同交易語意一致；
    * ``_mark_replay_skip_independent`` —— **只**給 ``LockNotAvailable`` /
      40001 把原 cursor 打成 aborted 之後用；那條 cursor 上任何 SQL 都會死在
      ``InFailedSqlTransaction``，所以「跳過」必須換一條連線才寫得下去。

    兩形都：白名單 reason／state ＋ ``state <> 'resolved'`` guard ＋ fail-soft ＋
    ``skip_count`` 在 SQL 層原子 +1（⛔ 不做 read-modify-write）。
    """

    def setUp(self):
        super().setUp()
        self.registry = Registry(get_db_name())
        self.marker = 'G864M%s' % uuid.uuid4().hex[:9].upper()
        self.addCleanup(self._purge)

    def _purge(self):
        try:
            with self.registry.cursor() as cr:
                cr.execute(
                    'DELETE FROM %s WHERE external_ref LIKE %%s'
                    % _INCIDENT_TABLE,
                    (self.marker + '%',),
                )
                cr.commit()
        except Exception:
            _logger.exception(
                '#864 PR-G replay marker fixture cleanup failed (marker=%s)',
                self.marker,
            )

    def _seed_intent(self, suffix, **overrides):
        ref = '%s-%s' % (self.marker, suffix)
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            incident_id = env[_INCIDENT_MODEL]._log({
                'external_ref': ref,
                'kind': 'intent',
                'partner_ref': 4242,
                'amount': 600.0,
                'source_model': 'woow.wallet.topup',
                'source_ref': 909,
                'detail': '付款意圖預登記（測試 fixture）。',
            })
        self.assertTrue(incident_id, '前置：intent 必須先落地')
        return ref, incident_id

    def _row(self, ref):
        with self.registry.cursor() as cr:
            cr.execute(
                'SELECT state, skip_reason, skip_count, attempts, '
                'last_skipped_at FROM %s WHERE external_ref = %%s'
                % _INCIDENT_TABLE,
                (ref,),
            )
            row = cr.fetchone()
            return row and dict(zip(
                ('state', 'skip_reason', 'skip_count', 'attempts',
                 'last_skipped_at'), row))

    def _raw_update(self, ref, **values):
        assigns = ', '.join('%s = %%s' % name for name in values)
        with self.registry.cursor() as cr:
            cr.execute(
                'UPDATE %s SET %s WHERE external_ref = %%s'
                % (_INCIDENT_TABLE, assigns),
                list(values.values()) + [ref],
            )
            cr.commit()

    def _mark(self, method, *args, **kwargs):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            result = getattr(env[_INCIDENT_MODEL], method)(*args, **kwargs)
            cr.commit()
            return result

    # --- (1) 確切 reason ＋ SQL 層原子 +1 ---------------------------------- #

    def test_skip_marker_writes_exact_reason_and_increments_atomically(self):
        ref, _incident_id = self._seed_intent('01')
        # 由**別條連線**先把計數推到 7 並 commit：正確實作在 SQL 層寫
        # `skip_count = skip_count + 1` ⇒ 8；若改用 `_log` 傳絕對值
        # （read-modify-write 的典型寫法）會得到 1。
        self._raw_update(ref, skip_count=7)

        ok = self._mark(
            '_mark_replay_skip_current_cursor', ref, 'lock_not_available')

        self.assertIs(ok, True)
        row = self._row(ref)
        self.assertEqual(row['skip_reason'], 'lock_not_available')
        self.assertEqual(
            row['skip_count'], 8,
            'skip_count 必須是 SQL 層 `skip_count + 1`；'
            '⛔ 不得由呼叫端讀出來再寫回去',
        )
        self.assertIsNotNone(
            row['last_skipped_at'], '每次跳過都要留時戳')
        self.assertEqual(
            row['state'], 'open', '未指定 state 時 ⛔ 不得動生命週期')
        self.assertEqual(
            row['attempts'], 0,
            'attempts 是「入帳嘗試失敗」的計數，跳過 ⛔ 不算一次嘗試',
        )

    # --- (2) 白名單 state：needs_human / orphan --------------------------- #

    def test_skip_marker_transitions_to_whitelisted_state(self):
        ref, _incident_id = self._seed_intent('02')

        ok = self._mark(
            '_mark_replay_skip_current_cursor', ref,
            'cancelled_needs_human', 'needs_human')

        self.assertIs(ok, True)
        row = self._row(ref)
        self.assertEqual(row['state'], 'needs_human')
        self.assertEqual(row['skip_reason'], 'cancelled_needs_human')

    def test_skip_marker_rejects_unknown_reason(self):
        ref, _incident_id = self._seed_intent('03')

        ok = self._mark(
            '_mark_replay_skip_current_cursor', ref, 'whatever_i_felt_like')

        self.assertIs(
            ok, False,
            'reason 走白名單：自由字串會讓「skipped_by_reason」這個對帳面'
            '長出無限值域，⛔ 不收',
        )
        self.assertIsNone(self._row(ref)['skip_reason'])

    def test_skip_marker_rejects_non_whitelisted_state(self):
        ref, _incident_id = self._seed_intent('04')

        ok = self._mark(
            '_mark_replay_skip_current_cursor', ref,
            'lock_not_available', 'resolved')

        self.assertIs(
            ok, False,
            '⛔ skip 兩形都不得寫出成功態：resolved 只能走既有的 '
            'current-cursor resolved transition（裁決 Q2-ii 錨1）',
        )
        self.assertEqual(self._row(ref)['state'], 'open')

    # --- (3) 錨1：wrapper 對「成功態 reason」拒收 --------------------------- #

    def test_independent_wrapper_rejects_resolution_reason(self):
        ref, _incident_id = self._seed_intent('05')

        ok = self._mark(
            '_mark_replay_skip_independent', ref, 'already_credited')

        self.assertIs(
            ok, False,
            '`already_credited` 是**解決**不是跳過：它必須走 resolved '
            'transition，⛔ 不得由 skip wrapper 寫出來',
        )
        row = self._row(ref)
        self.assertIsNone(row['skip_reason'])
        self.assertEqual(row['skip_count'], 0)

    # --- (4) 錨4：已 resolved 的列不被 skip 覆寫 --------------------------- #

    def test_skip_marker_never_touches_resolved_row(self):
        ref, _incident_id = self._seed_intent('06')
        self._raw_update(ref, state='resolved',
                         resolved_at=fields.Datetime.now())

        current = self._mark(
            '_mark_replay_skip_current_cursor', ref, 'lock_not_available')
        independent = self._mark(
            '_mark_replay_skip_independent', ref, 'lock_not_available')

        self.assertIs(current, False)
        self.assertIs(independent, False)
        row = self._row(ref)
        self.assertEqual(row['state'], 'resolved')
        self.assertIsNone(
            row['skip_reason'],
            '已結案的列 ⛔ 不得被後到的 skip 標記污染',
        )
        self.assertEqual(row['skip_count'], 0)

    # --- (5) wrapper 存在的理由：原 cursor 已 aborted --------------------- #

    def test_independent_wrapper_persists_after_caller_cursor_aborted(self):
        """LockNotAvailable/40001 會把整條交易標成 aborted。

        ⚠ 這一條是「為什麼需要第二形」的**鑑別式**：同一條 aborted cursor 上
        current-cursor 形必須失敗（陽性對照），independent 形必須成功。
        """
        ref, _incident_id = self._seed_intent('07')

        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            try:
                # 真的把這條交易打成 aborted（⛔ 不是 mock 一個假狀態）。
                cr.execute('SELECT 1 / 0')
                self.fail('前置：除以零必須拋例外，否則本測沒有鑑別力')
            except Exception:
                pass

            same_cursor = env[_INCIDENT_MODEL].\
                _mark_replay_skip_current_cursor(ref, 'lock_not_available')
            self.assertIs(
                same_cursor, False,
                '陽性對照：aborted 交易上任何 SQL 都死在 '
                'InFailedSqlTransaction ⇒ current-cursor 形寫不進去',
            )

            wrapper = env[_INCIDENT_MODEL]._mark_replay_skip_independent(
                ref, 'lock_not_available')
            self.assertIs(
                wrapper, True,
                'independent 形換一條連線 ⇒ 跳過痕跡仍留得下來；'
                '沒有它，「搶不到鎖」這個分支就是零痕跡的裸 continue',
            )

        row = self._row(ref)
        self.assertEqual(row['skip_reason'], 'lock_not_available')
        self.assertEqual(row['skip_count'], 1)


@tagged('post_install', '-at_install', '-standard', 'callback_replay_864')
class TestTopupIntentPreRegistration864(BaseCase):
    """auto 路徑（``charge_via_token``）的外呼前預登記 ＋ fail-closed。

    裁決 Q1：``_log`` 回 ``False`` ⇒ ⛔ 絕不外呼。驗收錨是「外部呼叫
    count == 0」，⛔ 不是只斷言回了錯誤。

    唯一 mock ＝ 真正的 ECPay 邊界 ``payment.transaction._send_payment_request``
    （沿 PR-E ``test_double_charge_cooldown_864`` 既有前例）；它同時當
    **call-order recorder**：被呼叫的那一刻用**另一條連線**去讀 incident，
    讀得到才證明預登記是「外呼之前 ＋ 已經 commit」。
    """

    def setUp(self):
        super().setUp()
        self.registry = Registry(get_db_name())
        self.marker = 'G864P%s' % uuid.uuid4().hex[:9].upper()
        self.addCleanup(self._purge)
        self._patchers = []
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            provider = env['payment.provider'].sudo().search(
                [('code', '=', 'ECPay')], limit=1)
            if not provider:
                self.skipTest('ECPay provider not configured in this DB')
            provider.write({
                'enable_ecpg': True,
                'MerchantID': '3002607',
                'HashKey': 'pwFHCqoQZGmho4w6',
                'HashIV': 'EkRm7iFT261dpevs',
                'state': 'test',
            })
            self.provider_id = provider.id
            self.payment_method_id = (
                env.ref('payment.payment_method_card', raise_if_not_found=False)
                or env.ref('payment.payment_method_unknown')
            ).id
            self.twd_id = env.ref('base.TWD').id
            # 名稱含空白 ⇒ 過不了 `_SNAPSHOT_CODE_RE`（`[A-Za-z0-9][\w._:/-]*`）
            # ⇒ `_log` 走真的 validation 路徑回 False。⛔ 不 mock `_log`。
            self.bad_currency_id = env['res.currency'].sudo().create({
                'name': 'T W', 'symbol': 'TW#', 'active': True,
            }).id
            cr.commit()

    def tearDown(self):
        for patcher in self._patchers:
            patcher.stop()
        super().tearDown()

    def _purge(self):
        try:
            with self.registry.cursor() as cr:
                cr.execute(
                    'DELETE FROM %s WHERE external_ref LIKE %%s'
                    % _INCIDENT_TABLE,
                    ('TOPUP%',),
                )
                env = api.Environment(cr, odoo.SUPERUSER_ID, {})
                partners = env['res.partner'].sudo().search(
                    [('name', 'like', self.marker)])
                if partners:
                    env['woow.wallet.topup'].sudo().search(
                        [('partner_id', 'in', partners.ids)]).unlink()
                    env['payment.transaction'].sudo().search(
                        [('partner_id', 'in', partners.ids)]).unlink()
                    env['payment.token'].sudo().with_context(
                        active_test=False).search(
                        [('partner_id', 'in', partners.ids)]).unlink()
                    env['account.move'].sudo().search(
                        [('partner_id', 'in', partners.ids)]).unlink()
                    partners.unlink()
                env['res.currency'].sudo().browse(
                    self.bad_currency_id).exists().unlink()
                cr.commit()
        except Exception:
            _logger.exception(
                '#864 PR-G pre-registration fixture cleanup failed '
                '(marker=%s)', self.marker,
            )

    # --- fixtures ------------------------------------------------------ #

    def _make_partner_token_topup(self, currency_id=None):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            partner = env['res.partner'].create({
                'name': '%s-p' % self.marker,
                'email': '%s@example.test' % self.marker,
            })
            token = env['payment.token'].sudo().create({
                'provider_id': self.provider_id,
                'partner_id': partner.id,
                'payment_method_id': self.payment_method_id,
                'payment_details': '**** **** **** 4242',
                'provider_ref': '%s-BIND' % self.marker,
                'ecpg_bindcard_id': '%s-BIND' % self.marker,
                'ecpg_member_id': str(partner.id),
            })
            topup = env['woow.wallet.topup'].sudo().create({
                'partner_id': partner.id,
                'amount': 500.0,
                'credits': 500.0,
                'bonus': 50.0,
                'currency_id': currency_id or self.twd_id,
                'state': 'draft',
                'source': 'auto',
            })
            ids = (partner.id, token.id, topup.id)
            cr.commit()
        return ids

    def _patch_ecpay_boundary(self, recorder=None):
        """Patch the single external ECPay boundary; return the mock."""
        from unittest.mock import patch
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            tx_model = type(env['payment.transaction'])

        def _send(tx_self):
            if recorder is not None:
                recorder(tx_self)
            return {'redirect_url': 'https://ecpay.example/3ds'}

        patcher = patch.object(
            tx_model, '_send_payment_request', autospec=True,
            side_effect=_send,
        )
        mock = patcher.start()
        self._patchers.append(patcher)
        return mock

    def _intent_rows(self):
        with self.registry.cursor() as cr:
            cr.execute(
                'SELECT external_ref, kind, state, amount, partner_ref, '
                'source_ref FROM %s WHERE source_model = %%s '
                "AND kind = 'intent' AND source_ref = %%s" % _INCIDENT_TABLE,
                ('woow.wallet.topup', self._topup_id),
            )
            return cr.fetchall()

    def _charge(self, topup_id, token_id):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            topup = env['woow.wallet.topup'].sudo().browse(topup_id)
            token = env['payment.token'].sudo().browse(token_id)
            result = topup.charge_via_token(token)
            cr.commit()
            return result

    def _topup_state(self, topup_id):
        with self.registry.cursor() as cr:
            cr.execute(
                'SELECT state, failure_code FROM woow_wallet_topup '
                'WHERE id = %s', (topup_id,),
            )
            return cr.fetchone()

    # --- (1) 預登記在外呼之前、且已跨連線可見 ------------------------------ #

    def test_intent_is_committed_before_the_irreversible_outbound_call(self):
        _partner_id, token_id, self._topup_id = self._make_partner_token_topup()
        seen = {}

        def _recorder(tx_self):
            # 外呼發生的那一刻，用**另一條連線**看得到預登記嗎？
            with self.registry.cursor() as probe:
                probe.execute(
                    "SELECT kind, state FROM %s WHERE external_ref = %%s"
                    % _INCIDENT_TABLE,
                    (tx_self.reference,),
                )
                seen['row'] = probe.fetchone()

        mock = self._patch_ecpay_boundary(recorder=_recorder)

        ok, code = self._charge(self._topup_id, token_id)

        self.assertEqual(mock.call_count, 1, '前置：外呼必須真的發生一次')
        self.assertEqual(
            seen.get('row'), ('intent', 'open'),
            '外呼當下必須已經有一筆 committed 的 intent 預登記——'
            '若寫在外層那顆 request cursor 上，另一條連線這時看不到它，'
            'callback 早到就沒有網接得住',
        )
        self.assertFalse(ok)
        self.assertEqual(code, '3ds_required', '3DS 是本路徑的正常結局')
        rows = self._intent_rows()
        self.assertEqual(len(rows), 1, '一次外呼只准一筆預登記')
        self.assertEqual(rows[0][3], 500.0)

    # --- (2) 裁決 Q1：`_log` 回 False ⇒ 外部呼叫 count == 0 --------------- #

    def test_intent_write_failure_blocks_the_charge_with_zero_calls(self):
        _partner_id, token_id, self._topup_id = self._make_partner_token_topup(
            currency_id=self.bad_currency_id)
        mock = self._patch_ecpay_boundary()

        ok, code = self._charge(self._topup_id, token_id)

        self.assertEqual(
            mock.call_count, 0,
            '🔴 裁決 Q1 fail-closed：預登記寫不進去就 ⛔ 不准外呼——'
            '沒有網卻把錢送出去，正是本 PR 要消滅的那個洞',
        )
        self.assertFalse(ok)
        self.assertEqual(code, 'intent_not_recorded')
        state, failure_code = self._topup_state(self._topup_id)
        self.assertEqual(state, 'failed')
        self.assertEqual(failure_code, 'intent_not_recorded')
        self.assertEqual(
            self._intent_rows(), [],
            '被拒收的預登記 ⛔ 不得留下半筆',
        )


@tagged('post_install', '-at_install', '-standard', 'callback_replay_864')
class TestCallbackReplayEngine864(BaseCase):
    """replay：把 inbox 裡的成功 callback 補成入帳，且只補一次。

    形狀（G 定形狀、F 沿用）：候選 query 走獨立 cursor 繞 RR 快照 → 每筆自己
    一條 ``registry.cursor()`` → 第一個 statement 設 ``lock_timeout`` →
    對 topup 列 ``FOR UPDATE NOWAIT`` → **鎖到之後才重讀狀態** → 唯一入帳
    choke point ``topup._on_payment_done(tx)`` → 同交易標記 incident →
    commit。鎖序固定 topup → incident。
    """

    def setUp(self):
        super().setUp()
        self.registry = Registry(get_db_name())
        self.marker = 'G864R%s' % uuid.uuid4().hex[:9].upper()
        self.addCleanup(self._purge)
        self._patchers = []
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            provider = env['payment.provider'].sudo().search(
                [('code', '=', 'ECPay')], limit=1)
            if not provider:
                self.skipTest('ECPay provider not configured in this DB')
            provider.write({'enable_ecpg': True, 'state': 'test',
                            'MerchantID': '3002607',
                            'HashKey': 'pwFHCqoQZGmho4w6',
                            'HashIV': 'EkRm7iFT261dpevs'})
            self.provider_id = provider.id
            method = provider.payment_method_ids[:1] or env.ref(
                'payment.payment_method_unknown')
            if not method.active:
                cr.execute('UPDATE payment_method SET active = TRUE '
                           'WHERE id = %s', (method.id,))
            self.payment_method_id = method.id
            self.currency_id = env.ref('base.TWD').id
            # `_on_payment_done` → `_ensure_topup_invoice` 會惰性建出一顆
            # **共用**的 `WALLET_TOPUP` product 並 commit。本檔是真 commit 的
            # integration test ⇒ 不清掉的話，同一個測試 DB 裡
            # `TestEcpayInvoiceTopup.test_topup_product_is_lazy_created_once`
            # 的「第一次 topup 之前不該存在」前置就不成立。只刪**本檔建的那顆**。
            self.topup_product_preexisted = bool(
                env['product.product'].sudo().search(
                    [('default_code', '=', 'WALLET_TOPUP')], limit=1))
            cr.commit()

    def tearDown(self):
        for patcher in self._patchers:
            patcher.stop()
        super().tearDown()

    def _purge(self):
        try:
            with self.registry.cursor() as cr:
                cr.execute(
                    'DELETE FROM %s WHERE external_ref LIKE %%s'
                    % _INCIDENT_TABLE, (self.marker + '%',))
                env = api.Environment(cr, odoo.SUPERUSER_ID, {})
                partners = env['res.partner'].sudo().search(
                    [('name', 'like', self.marker)])
                if partners:
                    env['wallet.transaction'].sudo().search(
                        [('partner_id', 'in', partners.ids)]).unlink()
                    env['woow.wallet.topup'].sudo().search(
                        [('partner_id', 'in', partners.ids)]).unlink()
                    env['payment.transaction'].sudo().search(
                        [('partner_id', 'in', partners.ids)]).unlink()
                    env['account.move'].sudo().search(
                        [('partner_id', 'in', partners.ids)]).unlink()
                    env['loyalty.card'].sudo().search(
                        [('partner_id', 'in', partners.ids)]).unlink()
                    partners.unlink()
                if not self.topup_product_preexisted:
                    env['product.product'].sudo().search(
                        [('default_code', '=', 'WALLET_TOPUP')]).unlink()
                cr.commit()
        except Exception:
            _logger.exception(
                '#864 PR-G replay engine fixture cleanup failed (marker=%s)',
                self.marker,
            )

    # --- fixtures ------------------------------------------------------ #

    def _new_partner(self, env, suffix):
        return env['res.partner'].create({
            'name': '%s-%s' % (self.marker, suffix),
            'email': '%s-%s@example.test' % (self.marker, suffix),
        })

    def _new_topup_and_tx(self, env, partner, reference, topup_state='pending',
                          tx_state='pending'):
        topup = env['woow.wallet.topup'].sudo().create({
            'partner_id': partner.id,
            'amount': 500.0, 'credits': 500.0, 'bonus': 50.0,
            'currency_id': self.currency_id,
            'state': 'draft', 'source': 'manual',
        })
        tx = env['payment.transaction'].sudo().create({
            'provider_id': self.provider_id,
            'payment_method_id': self.payment_method_id,
            'partner_id': partner.id,
            'amount': 500.0,
            'currency_id': self.currency_id,
            'reference': reference,
            'woow_topup_id': topup.id,
        })
        topup.write({'payment_transaction_id': tx.id, 'state': topup_state})
        if tx_state != 'draft':
            env.cr.execute(
                'UPDATE payment_transaction SET state = %s WHERE id = %s',
                (tx_state, tx.id))
            tx.invalidate_recordset(['state'])
        return topup, tx

    def _seed_case(self, suffix, topup_state='pending', tx_state='pending',
                   rtn_code='1', trade_no=True, with_topup=True,
                   first_seen_at=None):
        """建立「callback 已到達、tx 沒被更新」的完整局面並 commit。"""
        reference = '%s%s' % (self.marker, suffix)
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            partner = self._new_partner(env, suffix)
            topup_id = False
            if with_topup:
                topup, _tx = self._new_topup_and_tx(
                    env, partner, reference, topup_state, tx_state)
                topup_id = topup.id
            env[_INCIDENT_MODEL]._log({
                'external_ref': reference,
                'kind': 'intent',
                'partner_ref': partner.id,
                'amount': 500.0, 'credits': 500.0, 'bonus': 50.0,
                'currency': 'TWD',
                'source_model': 'woow.wallet.topup',
                'source_ref': topup_id or 999999999,
                'detail': '付款意圖預登記（測試 fixture）。',
            })
            evidence = {'external_ref': reference,
                        'kind': 'callback_tx_missing',
                        'detail': 'ECPay callback 事實（測試 fixture）。'}
            if rtn_code is not None:
                evidence['rtn_code'] = rtn_code
            if trade_no:
                evidence['trade_no'] = '%sTN' % self.marker
            env[_INCIDENT_MODEL]._log_callback_evidence(evidence)
            partner_id = partner.id
            cr.commit()
        if first_seen_at is not None:
            # 🔴 必須換一條 cursor：incident 是 `_log` 用**獨立交易**在本
            # cursor 的 RR 快照**之後**才 commit 的 ⇒ 在原 cursor 上 UPDATE
            # 會匹配 0 列而且**不報錯**（本 PR 要修的正是同一個陷阱；本測試
            # 自己先踩過一次）。
            with self.registry.cursor() as aging_cr:
                aging_cr.execute(
                    'UPDATE %s SET first_seen_at = %%s WHERE external_ref = %%s'
                    % _INCIDENT_TABLE, (first_seen_at, reference))
                self.assertEqual(
                    aging_cr.rowcount, 1,
                    '前置：推老 first_seen_at 必須真的改到那一列')
                aging_cr.commit()
        return reference, partner_id, topup_id

    # --- probes -------------------------------------------------------- #

    def _points(self, partner_id):
        with self.registry.cursor() as cr:
            cr.execute(
                'SELECT COALESCE(SUM(points), 0) FROM loyalty_card '
                'WHERE partner_id = %s', (partner_id,))
            return float(cr.fetchone()[0])

    def _wallet_tx_count(self, partner_id):
        with self.registry.cursor() as cr:
            cr.execute(
                'SELECT COUNT(*) FROM wallet_transaction '
                "WHERE partner_id = %s AND transaction_type = 'topup'",
                (partner_id,))
            return cr.fetchone()[0]

    def _incident(self, reference):
        with self.registry.cursor() as cr:
            cr.execute(
                'SELECT state, skip_reason, skip_count, attempts '
                'FROM %s WHERE external_ref = %%s' % _INCIDENT_TABLE,
                (reference,))
            row = cr.fetchone()
            return row and dict(zip(
                ('state', 'skip_reason', 'skip_count', 'attempts'), row))

    def _topup_state(self, topup_id):
        with self.registry.cursor() as cr:
            cr.execute('SELECT state FROM woow_wallet_topup WHERE id = %s',
                       (topup_id,))
            row = cr.fetchone()
            return row and row[0]

    def _run_replay(self):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            summary = env['woow.wallet.topup']._cron_replay_callback_inbox()
            cr.commit()
            return summary

    # --- (1) 旗艦情境：callback 早於主交易 commit 抵達 --------------------- #

    def test_callback_before_commit_is_caught_and_replayed_once(self):
        reference = '%sRACE' % self.marker
        # 連線 A：開著交易建 topup + tx，**故意不 commit**（＝真實 race 窗口）。
        with self.registry.cursor() as cr_a:
            env_a = api.Environment(cr_a, odoo.SUPERUSER_ID, {})
            partner = self._new_partner(env_a, 'race')
            topup, _tx = self._new_topup_and_tx(env_a, partner, reference)
            partner_id, topup_id = partner.id, topup.id
            # 生產路徑就是在這個時點（外呼之前、主交易尚未 commit）寫預登記，
            # 而 `_log` 走的是獨立 cursor ⇒ 立刻對別的連線可見。
            self.assertTrue(topup._log_topup_intent(reference))

            # 連線 B：callback 抵達。此刻 tx 對 B 還不存在（A 未 commit），
            # vendored route 會回 1|OK ⇒ 沒有 inbox 就永遠掉這筆錢。
            with self.registry.cursor() as cr_b:
                env_b = api.Environment(cr_b, odoo.SUPERUSER_ID, {})
                self.assertFalse(
                    env_b['payment.transaction'].sudo().search(
                        [('reference', '=', reference)]),
                    '前置：callback 那一刻 tx 必須真的還看不到，'
                    '否則這條測的不是本題的 race',
                )
                self.assertTrue(env_b[_INCIDENT_MODEL]._log_callback_evidence({
                    'external_ref': reference,
                    'kind': 'callback_tx_missing',
                    'rtn_code': '1',
                    'trade_no': '%sTN' % self.marker,
                    'detail': 'ECPay callback 事實（測試 fixture）。',
                }))
                cr_b.commit()

            # 連線 C：跨連線可見性——證明事實真的 commit 了，不是掛在 A 上。
            with self.registry.cursor() as cr_c:
                cr_c.execute(
                    'SELECT kind, rtn_code FROM %s WHERE external_ref = %%s'
                    % _INCIDENT_TABLE, (reference,))
                self.assertEqual(cr_c.fetchone(), ('intent', '1'))

            cr_a.commit()

        before = self._points(partner_id)

        summary = self._run_replay()

        # ⚠ 這是共用測試 DB：同一輪其他 test class 的 intent 列也會被同一支
        # cron 撈到 ⇒ 全域計數對「我這一筆怎麼了」沒有鑑別力。精確斷言一律
        # 掛在 marker 自己的列上（下面四條），計數只當「這輪真的有動作」。
        self.assertGreaterEqual(summary['credited'], 1, summary)
        self.assertEqual(
            self._points(partner_id) - before, 550.0,
            'credits 500 + bonus 50 必須真的進錢包',
        )
        self.assertEqual(self._wallet_tx_count(partner_id), 1)
        self.assertEqual(self._topup_state(topup_id), 'done')
        self.assertEqual(self._incident(reference)['state'], 'resolved')

    # --- (2) 冪等：第二輪不再入帳 ------------------------------------------ #

    def test_second_replay_round_is_a_no_op(self):
        reference, partner_id, topup_id = self._seed_case('IDEM')
        first = self._run_replay()
        self.assertGreaterEqual(first['credited'], 1, first)
        self.assertEqual(self._incident(reference)['state'], 'resolved')
        points_after_first = self._points(partner_id)
        self.assertEqual(self._wallet_tx_count(partner_id), 1)

        self._run_replay()

        self.assertEqual(
            self._points(partner_id), points_after_first,
            '已 resolved 的 incident ⛔ 不得再被撈出來補一次',
        )
        self.assertEqual(
            self._wallet_tx_count(partner_id), 1,
            'wallet.transaction 必須恰好一筆——重複入帳在帳上是多一列',
        )
        self.assertEqual(self._topup_state(topup_id), 'done')

    # --- (3) 每輪計數 log：零也要印 ---------------------------------------- #

    def test_summary_counters_are_emitted_even_when_nothing_matched(self):
        # level 必須傳**數值**：Odoo 的 netsvc 把 "INFO" 這個名字重新綁到
        # RUNBOT=25，字串會被解析成 25 ⇒ `_logger.info()` 全部落在門檻外。
        with self.assertLogs(
                'odoo.addons.woow_paas_platform.models.wallet_topup',
                level=logging.INFO) as logs:
            summary = self._run_replay()

        self.assertEqual(
            set(summary), {'scanned', 'credited', 'already_credited',
                           'failed', 'skipped_by_reason'},
            '計數面是對帳契約的一部分，⛔ 不可以只在有事時才有',
        )
        self.assertTrue(
            any('[TOPUP_REPLAY] scanned=' in line for line in logs.output),
            '每輪收尾必須無條件印一行計數——這是讓「今天沒事」與'
            '「每一筆都被擋掉」在日誌上可分辨的唯一辦法',
        )

    # --- (4) 逐狀態矩陣：每一格都有確切痕跡，⛔ 沒有裸 continue ------------ #

    def test_state_matrix_leaves_an_exact_marker_on_every_branch(self):
        aged = fields.Datetime.now() - timedelta(
            minutes=self.env_hard_cancel_min() + 5)
        # ⚠ suffix 要短：external_ref 走 raw-MTN 值域（最長 20 字元），
        #   marker 已佔 14 字元，超過就會被 `_log` 拒收、整格靜默不存在。
        cases = {
            'draft': dict(suffix='D1', kwargs=dict(topup_state='draft'),
                          state='needs_human',
                          reason='intent_without_charge'),
            'failed': dict(suffix='F1', kwargs=dict(topup_state='failed'),
                           state='open', reason='failed_belongs_to_pr_f'),
            'cancelled': dict(suffix='C1',
                              kwargs=dict(topup_state='cancelled'),
                              state='needs_human',
                              reason='cancelled_needs_human'),
            'norow': dict(suffix='N1', kwargs=dict(with_topup=False),
                          state='open', reason='no_topup_row'),
            'agedrow': dict(suffix='A1',
                            kwargs=dict(with_topup=False, first_seen_at=aged),
                            state='orphan', reason='intent_aged_out'),
            'rtnbad': dict(suffix='R1', kwargs=dict(rtn_code='10100058'),
                           state='open', reason='callback_not_successful'),
            'notrade': dict(suffix='T1', kwargs=dict(trade_no=False),
                            state='open', reason='missing_trade_no'),
        }
        seeded = {}
        for name, spec in cases.items():
            reference, partner_id, topup_id = self._seed_case(
                spec['suffix'], **spec['kwargs'])
            self.assertIsNotNone(
                self._incident(reference),
                '前置：%s 的 incident 必須真的建起來（external_ref 過長會被'
                '靜默拒收，那會讓整格變成假綠）' % name)
            if spec['kwargs'].get('first_seen_at') is not None:
                with self.registry.cursor() as probe:
                    probe.execute(
                        'SELECT first_seen_at FROM %s WHERE external_ref = %%s'
                        % _INCIDENT_TABLE, (reference,))
                    self.assertLess(
                        probe.fetchone()[0], aged + timedelta(minutes=1),
                        '前置：%s 的 first_seen_at 必須真的被推老，'
                        '否則測到的是別的分支' % name)
            seeded[name] = (reference, partner_id, topup_id)
        # 🔴 陽性對照：同一批裡混一筆合法的 pending。只有它該被補，
        #    其餘分毫未動——⛔ 不能只靠「什麼都沒發生」當綠燈。
        good_ref, good_partner, good_topup = self._seed_case('G1')
        before = {name: self._points(pid)
                  for name, (_r, pid, _t) in seeded.items()}
        good_before = self._points(good_partner)

        self._run_replay()

        for name, spec in cases.items():
            reference, partner_id, _topup_id = seeded[name]
            row = self._incident(reference)
            self.assertEqual(
                (row['state'], row['skip_reason']),
                (spec['state'], spec['reason']),
                '分支 %s 必須留下確切的 state/skip_reason' % name,
            )
            self.assertGreaterEqual(row['skip_count'], 1, name)
            self.assertEqual(
                self._points(partner_id), before[name],
                '分支 %s ⛔ 不得動到任何點數' % name,
            )
        self.assertEqual(
            self._points(good_partner) - good_before, 550.0,
            '陽性對照：合法的 pending 必須在同一批裡真的被補',
        )
        self.assertEqual(self._incident(good_ref)['state'], 'resolved')

    def env_hard_cancel_min(self):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            return env['woow.wallet.topup']._CLEANUP_HARD_CANCEL_MIN

    def test_aged_but_uncreditable_intent_leaves_the_scan_set_as_orphan(self):
        """逾時仍補不了的預登記轉 orphan：收斂掃描集合，⛔ 不是結案。

        沒有這一格的話，每一筆被拒的刷卡都會留下一列永遠 ``open`` 的 intent，
        每輪被撈出來再跳過一次，最後把 batch 名額佔滿、把真正該補的餓死。
        ⚠ 人工清單的判準是**年齡**（``state != 'resolved'``），所以這些列在
        人工清單上一個都不會少——收斂的只有 cron 的掃描集合。
        """
        aged = fields.Datetime.now() - timedelta(
            minutes=self.env_hard_cancel_min() + 5)
        old_ref, old_partner, _old_topup = self._seed_case(
            'X1', rtn_code='10100058', first_seen_at=aged)
        fresh_ref, _fresh_partner, _fresh_topup = self._seed_case(
            'X2', rtn_code='10100058')

        self._run_replay()

        old_row = self._incident(old_ref)
        self.assertEqual(
            (old_row['state'], old_row['skip_reason']),
            ('orphan', 'callback_not_successful'),
            '逾時的那一筆要離開掃描集合，但**確切 reason 必須留著**——'
            '換成籠統的 aged 值就把診斷資訊丟了',
        )
        fresh_row = self._incident(fresh_ref)
        self.assertEqual(
            (fresh_row['state'], fresh_row['skip_reason']),
            ('open', 'callback_not_successful'),
            '陽性對照：還沒逾時的同型列仍留在掃描集合裡（可能還會等到'
            '後續的成功 callback），⛔ 不可以一律轉 orphan',
        )
        self.assertEqual(self._points(old_partner), 0.0)

    def test_already_credited_topup_is_resolved_not_recredited(self):
        reference, partner_id, topup_id = self._seed_case(
            'DONE', topup_state='done')
        before = self._points(partner_id)

        self._run_replay()

        self.assertEqual(self._points(partner_id), before)
        self.assertEqual(self._wallet_tx_count(partner_id), 0)
        self.assertEqual(
            self._incident(reference)['state'], 'resolved',
            '已入帳的 incident 要收工結案，⛔ 不可以永遠留在 open 每輪重掃',
        )

    def test_missing_transaction_goes_to_human_review(self):
        reference, partner_id, topup_id = self._seed_case('NOTX')
        with self.registry.cursor() as cr:
            cr.execute('UPDATE woow_wallet_topup SET payment_transaction_id '
                       '= NULL WHERE id = %s', (topup_id,))
            cr.commit()

        self._run_replay()

        row = self._incident(reference)
        self.assertEqual(
            (row['state'], row['skip_reason']),
            ('needs_human', 'missing_payment_transaction'),
            '沒有 tx 就沒有 `_on_payment_done` 的入口，⛔ 不得猜著補',
        )
        self.assertEqual(self._points(partner_id), 0.0)

    # --- (5) 行鎖：搶不到就跳過，且 ⛔ 不毒到後面的筆 --------------------- #

    def test_locked_topup_is_skipped_with_lock_not_available(self):
        reference, partner_id, topup_id = self._seed_case('LOCK')
        before = self._points(partner_id)

        with self.registry.cursor() as holder:
            holder.execute(
                'SELECT id FROM woow_wallet_topup WHERE id = %s FOR UPDATE',
                (topup_id,))
            self._run_replay()

        row = self._incident(reference)
        self.assertEqual(row['skip_reason'], 'lock_not_available')
        self.assertEqual(row['skip_count'], 1)
        self.assertEqual(
            row['state'], 'open',
            '搶不到鎖只是這一輪跳過，⛔ 不是終態',
        )
        self.assertEqual(self._points(partner_id), before)

    def test_a_locked_row_does_not_poison_the_rest_of_the_batch(self):
        """🔴 這條就是「為什麼每筆自己一條 cursor」的 RED 定義。

        共用一條 cursor 時，``LockNotAvailable`` 會把整條交易標成 aborted ⇒
        中間那筆之後的每一筆第一個 SQL 都死在 ``InFailedSqlTransaction``。
        """
        first = self._seed_case('B1')
        middle = self._seed_case('B2')
        last = self._seed_case('B3')
        before = {ref: self._points(pid) for ref, pid, _t in
                  (first, middle, last)}

        with self.registry.cursor() as holder:
            holder.execute(
                'SELECT id FROM woow_wallet_topup WHERE id = %s FOR UPDATE',
                (middle[2],))
            self._run_replay()

        for name, case in (('first', first), ('last', last)):
            self.assertEqual(
                self._points(case[1]) - before[case[0]], 550.0,
                '被鎖住的那一筆 ⛔ 不得毒到 %s 筆' % name,
            )
            self.assertEqual(self._incident(case[0])['state'], 'resolved')
        self.assertEqual(self._points(middle[1]), before[middle[0]])
        self.assertEqual(
            self._incident(middle[0])['skip_reason'], 'lock_not_available')

    # --- (6) 真競態：兩條連線同時 replay 同一筆 ---------------------------- #

    def test_concurrent_contender_on_the_same_row_credits_at_most_once(self):
        """🔴 真競態：另一條連線與 replay 同時搶同一列的 topup 行鎖。

        ⚠ **為什麼對手用 raw SQL 而不是也跑一次 replay**：Odoo 的 `-u` 測試載入
        期間，**主執行緒持有 ``Registry._lock``**，worker thread 一旦建立
        ``api.Environment`` 就會卡在那顆 harness 鎖上直到主執行緒放手
        （本輪實測：worker 停在 ``api.Environment(...)``，恰好在主執行緒
        ``join`` 逾時那一刻才前進；PostgreSQL 端全程 idle、零 lock wait）。
        ⇒ 「兩邊都跑 ORM replay」在這個 harness 下結構上做不到，測出來的會是
        harness 鎖不是 PostgreSQL 併發。同一份紀錄也寫在
        ``test_billing_incident_real_rollback_864.py`` 的 race 註解裡。

        ⇒ 對手改成**在主執行緒建立的裸 cursor 上跑 raw SQL 行鎖**——那正是
        production 上另一個競爭者（callback 走 ``_set_done`` 寫 topup 列）在
        資料庫層的形狀。兩邊在 barrier 上對齊後由 PostgreSQL 決勝負。

        不變式（⛔ 不押誰贏）：這一輪**最多**入帳一次；補跑一輪之後**恰好**
        入帳一次。
        """
        reference, partner_id, topup_id = self._seed_case('RC2')
        before = self._points(partner_id)
        barrier = threading.Barrier(2)
        contender_cr = self.registry._db.cursor()
        contender = {}

        def _contend():
            try:
                contender_cr.execute('SET LOCAL lock_timeout = %s', ('1s',))
                barrier.wait(timeout=30)
                contender_cr.execute(
                    'SELECT id FROM woow_wallet_topup WHERE id = %s '
                    'FOR UPDATE NOWAIT', (topup_id,))
                contender['won'] = bool(contender_cr.fetchone())
            except Exception as exc:
                contender['error'] = type(exc).__name__
            finally:
                try:
                    contender_cr.rollback()
                except Exception:
                    pass

        thread = threading.Thread(target=_contend, name='g864-contender')
        thread.start()
        try:
            barrier.wait(timeout=30)
            race_summary = self._run_replay()
            thread.join(timeout=30)
            self.assertFalse(
                thread.is_alive(), '前置：對手執行緒必須跑完')
        finally:
            if not contender_cr.closed:
                contender_cr.close()

        raced_delta = self._points(partner_id) - before
        self.assertIn(
            raced_delta, (0.0, 550.0),
            '競態這一輪只可能「補了一次」或「這輪讓給對手」，'
            '⛔ 不可能是別的數字 summary=%r contender=%r'
            % (race_summary, contender),
        )
        self.assertLessEqual(self._wallet_tx_count(partner_id), 1)
        self.assertTrue(
            contender.get('won') or contender.get('error')
            == 'LockNotAvailable',
            '前置：對手要嘛拿到鎖、要嘛被 NOWAIT 當場拒絕——'
            '兩者皆非代表這一格根本沒有競爭 contender=%r' % (contender,),
        )

        # 補跑一輪（此時無競爭）：不論剛剛誰贏，總帳必須恰好一次。
        self._run_replay()

        self.assertEqual(
            self._points(partner_id) - before, 550.0,
            '🔴 不論競態誰贏，錢包最終只准增加一次',
        )
        self.assertEqual(
            self._wallet_tx_count(partner_id), 1,
            'wallet.transaction 恰一筆——重複入帳在帳上就是多一列',
        )
        self.assertEqual(self._incident(reference)['state'], 'resolved')

    # --- (7) 與 cleanup cron 的互動：replay 先跑、兩段互不連坐 ------------- #

    def _age_topup(self, topup_id, minutes):
        with self.registry.cursor() as cr:
            cr.execute(
                'UPDATE woow_wallet_topup SET create_date = %s WHERE id = %s',
                (fields.Datetime.now() - timedelta(minutes=minutes), topup_id))
            self.assertEqual(cr.rowcount, 1)
            cr.commit()

    def _run_cleanup(self):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            env['woow.wallet.topup']._cron_cleanup_pending_topups()
            cr.commit()

    def test_cleanup_keeps_a_topup_that_has_successful_callback_evidence(self):
        """有成功 callback 事實的 ⛔ 不得被 hard-cancel；沒有的照舊 cancel。"""
        hard = self.env_hard_cancel_min()
        with_ref, with_partner, with_topup = self._seed_case('K1')
        self._age_topup(with_topup, hard + 5)
        # 陽性對照：同齡、同樣卡在 pending，但**沒有**任何 callback 事實。
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            partner = self._new_partner(env, 'K2')
            plain, _tx = self._new_topup_and_tx(
                env, partner, '%sK2' % self.marker)
            plain_id = plain.id
            cr.commit()
        self._age_topup(plain_id, hard + 5)

        self._run_cleanup()

        self.assertEqual(
            self._topup_state(with_topup), 'done',
            '🔴 replay 必須跑在 hard-cancel **之前**：有成功 callback 事實的'
            '這一筆要被補成 done，⛔ 不是被標成 cancelled（那正是本病的末期'
            '形態，看起來像使用者自己放棄）',
        )
        self.assertEqual(self._incident(with_ref)['state'], 'resolved')
        self.assertEqual(
            self._topup_state(plain_id), 'cancelled',
            '陽性對照：沒有任何事實的同齡 topup 仍必須照舊被 cancel——'
            '否則擋下來的是「cleanup 整個壞了」而不是「有預登記」',
        )

    def test_replay_segment_failure_does_not_kill_the_cleanup_segment(self):
        """兩段互不連坐（裁決②）：replay 炸掉，cleanup 照跑。

        用**真的**故障製造：另一條連線對 incident 表取 ``ACCESS EXCLUSIVE``，
        replay 的候選 query 撞 1 秒 ``lock_timeout`` 而拋。⛔ 沒有 mock 我們
        自己的任何一段。
        """
        hard = self.env_hard_cancel_min()
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            partner = self._new_partner(env, 'K3')
            orphan = env['woow.wallet.topup'].sudo().create({
                'partner_id': partner.id,
                'amount': 500.0, 'credits': 500.0, 'bonus': 50.0,
                'currency_id': self.currency_id,
                'state': 'pending', 'source': 'manual',
            })
            orphan_id = orphan.id
            cr.commit()
        self._age_topup(orphan_id, hard + 5)

        with self.registry.cursor() as blocker:
            blocker.execute(
                'LOCK TABLE woow_billing_incident IN ACCESS EXCLUSIVE MODE')
            self._run_cleanup()

        self.assertEqual(
            self._topup_state(orphan_id), 'cancelled',
            'replay 段拋例外 ⛔ 不得讓 cleanup 段死掉——兩段是各自的'
            'failure domain（同 PR-E 的紀律）',
        )

    def test_cleanup_segment_failure_does_not_roll_back_a_committed_replay(
            self):
        """反向：cleanup 段炸掉，已 commit 的 replay 仍在。

        毒藥用**真的**故障：一筆 credits/bonus 皆 0 的 topup 讓
        ``add_wallet_points`` 依契約拋 ``UserError``（金額必須大於 0），
        而 cleanup 的 catch-up 分支沒有包 try。
        """
        hard = self.env_hard_cancel_min()
        good_ref, good_partner, good_topup = self._seed_case('K4')
        self._age_topup(good_topup, hard + 5)
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            partner = self._new_partner(env, 'K5')
            poison = env['woow.wallet.topup'].sudo().create({
                'partner_id': partner.id,
                'amount': 500.0, 'credits': 0.0, 'bonus': 0.0,
                'currency_id': self.currency_id,
                'state': 'draft', 'source': 'manual',
            })
            tx = env['payment.transaction'].sudo().create({
                'provider_id': self.provider_id,
                'payment_method_id': self.payment_method_id,
                'partner_id': partner.id,
                'amount': 500.0,
                'currency_id': self.currency_id,
                'reference': '%sK5' % self.marker,
                'woow_topup_id': poison.id,
            })
            poison.write({'payment_transaction_id': tx.id,
                          'state': 'pending'})
            cr.execute('UPDATE payment_transaction SET state = %s '
                       'WHERE id = %s', ('done', tx.id))
            poison_id = poison.id
            cr.commit()
        self._age_topup(poison_id, hard + 5)

        raised = False
        try:
            self._run_cleanup()
        except Exception:
            raised = True
        self.assertTrue(
            raised,
            '前置：cleanup 段必須真的炸開，否則這條測不到「不連坐」',
        )

        self.assertEqual(
            self._topup_state(good_topup), 'done',
            '🔴 replay 是在**自己的獨立 cursor** 上 commit 的 ⇒ 之後 cleanup '
            '段在 cron cursor 上拋例外，結構上回捲不掉它',
        )
        self.assertEqual(self._incident(good_ref)['state'], 'resolved')
        self.assertEqual(self._wallet_tx_count(good_partner), 1)

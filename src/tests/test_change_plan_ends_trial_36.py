# -*- coding: utf-8 -*-
"""#36：試用中變更方案**一定**會結束試用並開出首期帳單——SA / SH 兩個 surface。

## 這個檔在守什麼

`ChangePlanModal` 在試用中會跳一段警告，逐字對使用者保證三件事：立刻結束試用、
現在開始第一個計費週期、產生首期帳單（見
`static/src/paas/components/modal/ChangePlanModal.xml` 的 `__trial_warning`）。

**那個「一定」不是 UI 保證的，是後端保證的。** 機制是
`subscription_lifecycle_mixin._create_and_confirm_subscription`：

    entity_has_prior_sub = bool(self._sub())
    ...
    if plan.sh_trial and not entity_has_prior_sub:
        self._force_trial_dates_from_plan(subscription, plan)
    else:
        self._force_active_dates_from_plan(subscription, plan)

三個 change-plan endpoint 都先 gate「entity 的 `subscription_id` 為 truthy 且
`state=='in_progress'`」才往下走，而那正是 `entity_has_prior_sub` 讀的同一個欄位；
gate 與 guard 之間沒有任何一段會清它 ⇒ 在 change-plan 路徑上該旗標**結構性恆為
True** ⇒ 目標方案的 `sh_trial` 完全不影響結果，一律走
`_force_active_dates_from_plan`（寫 `sh_trial=False`、`sh_start_date=today`）。

⇒ **試用中換到「標榜有試用」的方案，一樣當場結束試用並開帳單。** 若哪天有人改了
後端讓 trial→trial 保留試用，本檔會紅——而那正是該紅的時候，因為 modal 的文案會
在同一刻變成謊言。改後端請連同文案一起改。

## 為什麼是 SA 與 SH，不含 CS

CS 已有等價守線：`test_trial_reset_guard_367.py` 的
`test_returning_partner_change_plan_no_trial`（＋席數變體版），兩半都斷言，
本檔刻意不重寫一條同義的。

盤點結果（2026-08-23）：
- **SA 完全沒有**——`api_sa_change_plan` 全 `src/tests/` 只出現在
  `test_security_access_subscription_flow.py`，五條全是 ACL/gate 測試，沒有一條
  看新訂閱長什麼樣；該檔 `sh_trial` 唯一出現處是 plan fixture 欄位，不是斷言。
- **SH 只有一半**——`test_smart_home_subscription_flow.py` 的
  `test_trial_to_trial_change_plan_no_longer_restarts_trial` 斷言了「非 trial ＋
  立即計費日期」，但**沒有任何發票斷言**，而且是刻意的（該檔自陳不驅動
  wallet/journal/invoice 前置）。本檔補的是**帳單那一半**，不動那個檔的取態。

「兩個 controller 共用同一個 mixin，測一邊就夠」在本 repo **不成立**：issue #34
的 Q6 處理的正是「CS 曾用 inline refund calc、與 SH 的 `_compute_unused_refund`
不一致」，PR #35 才把兩邊拉齊——這兩條 controller 歷史上真的漂移過。本檔守的是
controller→mixin→confirm 整條，不是 mixin 單點。

## 判準的鑑別力（為什麼每個 surface 有兩條而不是一條）

`test_*_change_plan_during_trial_ends_trial_and_bills` 單獨看是有漏洞的：
若平台其實對**任何**新訂閱都開發票，`assertTrue(invoices)` 就是恆真、等於沒測。
所以每個 surface 配一條陽性對照 `test_*_fresh_purchase_keeps_trial_and_bills_nothing`
——全新 entity 首購**必須**進 trial 且**零發票**。兩條方向相反，發票斷言才有鑑別力。

⚠ 對照那條在「拿掉 `and not entity_has_prior_sub`」的注入下應**維持綠**
（全新 entity 本來就沒有 prior sub），只有主張那條會紅——這是刻意的，它把注入
定位在 change-plan 路徑，而不是「整個 trial 機制壞了」。

Refs #36 #34 #367
"""
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.controllers.security_access import (
    SecurityAccessController,
)
from odoo.addons.woow_paas_platform.controllers.smart_home import (
    SmartHomeController,
)
from .smart_home_fixture import sh_model
from .wallet_fixture import fund_wallet


def _patch_sa_request(env):
    return patch(
        'odoo.addons.woow_paas_platform.controllers.security_access.request',
        new=MagicMock(env=env),
    )


def _patch_sh_request(env):
    return patch(
        'odoo.addons.woow_paas_platform.controllers.smart_home.request',
        new=MagicMock(env=env),
    )


def _make_plan(env, name, price, trial_days, **extra):
    """建 plan ＋ 它的訂閱 product（`_create_and_confirm_subscription` 前置：
    沒有對應 product.template 會 raise "No subscription product found"）。"""
    vals = {
        'name': name,
        'sh_plan_price': price,
        'sh_duration': 1,
        'sh_unit': 'month',
        'sh_no_of_billing_cycle': -1,
        'sh_trial': bool(trial_days),
    }
    if trial_days:
        vals['sh_trial_duration'] = trial_days
        vals['sh_trial_unit'] = 'day'
    vals.update(extra)
    plan = env['sh.subscription.plan'].sudo().create(vals)
    env['product.template'].sudo().create({
        'name': name + ' Product',
        'type': 'service',
        'sh_product_subscribe': True,
        'sh_subscription_plan_id': plan.id,
        'list_price': price,
    })
    return plan


def _ensure_journal(env):
    """`_create_and_confirm_subscription` 要求 company.sh_journal_id 已設定
    （post_init_hook 會設；fresh/測試 DB 不保證，故防禦性補上）。"""
    company = env.company.sudo()
    if not company.sh_journal_id:
        journal = env['account.journal'].sudo().search(
            [('type', '=', 'sale')], limit=1)
        if journal:
            company.sh_journal_id = journal.id


def _make_owner(env, tag):
    user = env['res.users'].sudo().create({
        'name': '36 Owner %s' % tag,
        'login': 'chg36_owner_%s' % tag,
        'email': 'chg36_owner_%s@example.test' % tag,
    })
    user.groups_id = [(6, 0, [env.ref('base.group_user').id])]
    return user


class TestSaChangePlanEndsTrial36(TransactionCase):
    """Security Access：試用中換方案 ⇒ 非 trial ＋ 首期帳單金額 > 0。"""

    def setUp(self):
        super().setUp()
        self.controller = SecurityAccessController()
        self.SecurityAccess = self.env['woow_paas_platform.security_access']
        self.Move = self.env['account.move'].sudo()
        _ensure_journal(self.env)

        # 目標方案**刻意也帶試用**（14 天）——這是本檔的判別點：照 Q9 字面
        # 「trial→trial 靜默」會判定沒事，實際卻結束試用並扣款。
        self.plan_a = _make_plan(
            self.env, 'SA 36 A (7d trial)', 100.0, 7, sa_max_routes=3)
        self.plan_b = _make_plan(
            self.env, 'SA 36 B (14d trial)', 200.0, 14, sa_max_routes=5)
        self.template = self.env[
            'woow_paas_platform.security_access_template'].sudo().create({
                'name': 'SA 36 Tmpl',
                'subscription_plan_ids': [
                    (6, 0, [self.plan_a.id, self.plan_b.id])],
            })

    def _fresh_sa(self, tag):
        owner = _make_owner(self.env, 'sa_%s' % tag)
        ws = self.env['woow_paas_platform.workspace'].sudo().create({
            'name': 'SA 36 WS %s' % tag, 'owner_id': owner.id,
        })
        sa = self.SecurityAccess.sudo().create({
            'name': 'SA 36 GW %s' % tag,
            'workspace_id': ws.id,
            'template_id': self.template.id,
        })
        # 換方案餘額閘（#1677 A1）：本檔測試用結束與帳單（見 wallet_fixture）。首購是試用
        # 方案（不開發票），先補點不影響「首購零發票」的對照組。
        fund_wallet(ws.owner_id.partner_id)
        return owner, ws.owner_id.partner_id, sa

    def _invoices_of(self, sub):
        return self.Move.search([('sh_subscription_id', '=', sub.id)])

    def test_sa_change_plan_during_trial_ends_trial_and_bills(self):
        """主張：SA 試用中換到**另一個也有試用的**方案 ⇒ 新訂閱非 trial、
        今天起算計費、且真的開出金額 > 0 的首期帳單。"""
        owner, partner, sa = self._fresh_sa('ends')
        sa._create_and_confirm_subscription(partner, self.plan_a)

        # 前置：舊訂閱確實在試用中。不成立的話下面整條測試沒有意義——大聲斷言，
        # 不要讓「fixture 根本沒進 trial」偽裝成「行為正確」。
        old_sub = sa.sudo().subscription_id
        self.assertTrue(
            old_sub.sh_trial,
            '前置不成立：fixture 的舊訂閱不在試用中，本測試無從判別',
        )
        self.env.invalidate_all()

        with _patch_sa_request(self.env(user=owner)):
            res = self.controller.api_sa_change_plan(
                workspace_id=sa.workspace_id.id, sa_id=sa.id,
                plan_id=self.plan_b.id)
        self.assertTrue(res['success'], res.get('error'))

        new_sub = sa.sudo().subscription_id
        self.assertNotEqual(
            new_sub.id, old_sub.id, 'change-plan 應開出一筆新訂閱')
        self.assertFalse(
            new_sub.sh_trial,
            '#36：試用中換方案必須結束試用（即使目標方案自己有 trial）——'
            'modal 的警告文案正是以此為前提',
        )
        self.assertFalse(new_sub.sh_trial_end_date)

        invoices = self._invoices_of(new_sub)
        self.assertTrue(
            invoices,
            '#36：modal 對使用者保證「產生首期帳單」，這裡必須真的有帳單',
        )
        self.assertGreater(
            sum(invoices.mapped('amount_total')), 0.0,
            '#36：首期帳單金額必須 > 0（trial 的 0 元不算「開始計費」）',
        )

    def test_sa_fresh_purchase_keeps_trial_and_bills_nothing(self):
        """陽性對照：全新 SA 首購**仍給**試用且**零帳單**。

        沒有這條，上面那條的 `assertTrue(invoices)` 可能只是反映「平台對任何
        新訂閱都開發票」而恆真。兩條方向相反，發票那半才有鑑別力。
        """
        _, partner, sa = self._fresh_sa('fresh')
        sa._create_and_confirm_subscription(partner, self.plan_a)

        sub = sa.sudo().subscription_id
        self.assertTrue(sub.sh_trial, '全新 SA 首購應享有自己的試用')
        self.assertFalse(
            self._invoices_of(sub),
            '試用首購不應產生帳單——否則上一條的發票斷言恆真、等於沒測',
        )


class TestShChangePlanEndsTrial36(TransactionCase):
    """Smart Home：補既有測試缺的「首期帳單」那一半。

    ⚠ `test_*` 刻意各自宣告在本 class（不放共用 mixin）——Odoo 的 test loader 走
    `test_case_class.__dict__`，掛在 mixin 上的 `test_*` 不會被執行。共用的只有
    module-level helper function。
    """

    def setUp(self):
        super().setUp()
        self.controller = SmartHomeController()
        self.SmartHome = sh_model(self.env)
        self.Move = self.env['account.move'].sudo()
        _ensure_journal(self.env)

        self.plan_a = _make_plan(self.env, 'SH 36 A (7d trial)', 100.0, 7)
        self.plan_b = _make_plan(self.env, 'SH 36 B (14d trial)', 200.0, 14)
        self.template = self.env[
            'woow_paas_platform.smart_home_template'].sudo().create({
                'name': 'SH 36 Tmpl',
                'subscription_plan_ids': [
                    (6, 0, [self.plan_a.id, self.plan_b.id])],
            })

    def _fresh_home(self, tag):
        owner = _make_owner(self.env, 'sh_%s' % tag)
        ws = self.env['woow_paas_platform.workspace'].sudo().create({
            'name': 'SH 36 WS %s' % tag, 'owner_id': owner.id,
        })
        home = self.SmartHome.sudo().create({
            'name': 'SH 36 Home %s' % tag, 'workspace_id': ws.id,
        })
        fund_wallet(ws.owner_id.partner_id)  # 同上（#1677 A1 餘額閘）
        return owner, ws.owner_id.partner_id, home

    def _invoices_of(self, sub):
        return self.Move.search([('sh_subscription_id', '=', sub.id)])

    def test_sh_change_plan_during_trial_ends_trial_and_bills(self):
        """主張：SH 試用中換到**另一個也有試用的**方案 ⇒ 非 trial ＋ 帳單 > 0。

        「非 trial」那半 `test_smart_home_subscription_flow.py` 已有；本條的
        load-bearing 部分是**帳單**那半（該檔刻意不驅動 invoice 生成）。
        """
        owner, partner, home = self._fresh_home('ends')
        home._create_and_confirm_subscription(partner, self.plan_a)

        old_sub = home.sudo().subscription_id
        self.assertTrue(
            old_sub.sh_trial,
            '前置不成立：fixture 的舊訂閱不在試用中，本測試無從判別',
        )
        self.env.invalidate_all()

        with _patch_sh_request(self.env(user=owner)):
            res = self.controller.api_smarthome_change_plan(
                workspace_id=home.workspace_id.id,
                smarthome_id=home.id,
                plan_id=self.plan_b.id)
        self.assertTrue(res['success'], res.get('error'))

        new_sub = home.sudo().subscription_id
        self.assertNotEqual(
            new_sub.id, old_sub.id, 'change-plan 應開出一筆新訂閱')
        self.assertFalse(
            new_sub.sh_trial,
            '#36：試用中換方案必須結束試用（即使目標方案自己有 trial）',
        )

        invoices = self._invoices_of(new_sub)
        self.assertTrue(
            invoices,
            '#36：modal 對使用者保證「產生首期帳單」，這裡必須真的有帳單',
        )
        self.assertGreater(
            sum(invoices.mapped('amount_total')), 0.0,
            '#36：首期帳單金額必須 > 0（trial 的 0 元不算「開始計費」）',
        )

    def test_sh_fresh_purchase_keeps_trial_and_bills_nothing(self):
        """陽性對照：全新 SH 首購**仍給**試用且**零帳單**（見 SA 版同名說明）。"""
        _, partner, home = self._fresh_home('fresh')
        home._create_and_confirm_subscription(partner, self.plan_a)

        sub = home.sudo().subscription_id
        self.assertTrue(sub.sh_trial, '全新 SH 首購應享有自己的試用')
        self.assertFalse(
            self._invoices_of(sub),
            '試用首購不應產生帳單——否則上一條的發票斷言恆真、等於沒測',
        )

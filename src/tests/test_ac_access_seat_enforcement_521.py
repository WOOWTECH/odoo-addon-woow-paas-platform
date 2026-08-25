"""#521 第 1 層：``ac_max_access_seats`` 的 enforcement 接線。

## 判準（1-A 清單基準，Eugene 2026-08-25 拍板）

``docs/plans/2026-Q3/2026-07-10-plan-taxonomy-pricing.md:270`` 逐字：
「enforcement 比對『**distinct email 位址數 ≤ 內含＋已購**』」；同檔 ``:258``
把計數範圍寫成「跨該訂閱全部 route 的 email **去重**」。超額行為＝**擋寫入**
（``ValidationError``、不做自動計費，2026-08-15 已拍）。

「內含＋已購」在本 repo 的載體是 ``plan.ac_max_access_seats`` 自己：內含 M=0，
已購走 ``{base}_ac{n}_{cycle}`` 隱藏席數變體 plan（見
``sh_subscription_plan_visibility.py:35-52``），一筆訂閱恰好掛一個 plan ⇒ 該
plan 上的欄位值就是這筆訂閱的席位總額。

## 🔴 0 的語意與兩條相鄰 quota **相反**，這不是筆誤

``_check_policy_quota``（``sa_max_access_policies``）與
``_check_emails_per_policy``（``sa_max_emails_per_policy``）在 #743 之後都是
**0 ＝ 不限**。``ac_max_access_seats`` 是 **0 ＝ 一席都沒有**——三個載體一致：

- 欄位 help（``sh_subscription_plan_ac.py:20-27``）逐字「上限＝0＋已購 AC 席數」
  「0＝未加購任何席位」；
- pricing ``:270`` 的「內含 M=0」；
- 前端能力 gate ``ServiceDetailPage.js:229`` 是 ``> 0`` 才顯示 AC 頁籤。

``_check_emails_per_policy`` 自己的註解已經寫明這**不是** module 級慣例
（``sa_max_routes`` 的 0 也是「一條都不准」）⇒ 每個欄位各自認定。

## 🔴 爆炸半徑：本檔的 ``TestAcSeatBasePlanBlastRadius`` 是**刻意攤開**的

seed 的 base 方案全部 ``ac_max_access_seats = 0``，所以「base 方案 ＋ 既有
email_list policy ⇒ 一碰就被擋」。**這個行為未經拍板**（#521 決策點 4 的
grandfather／全擋／只擋新增三選一還在 Eugene 手上）。本檔不做任何 grandfather、
不加任何豁免，只把現況**斷言成可見的**，讓 review 時它是攤開的而不是隱含的。

## 唯一的豁免是 migration cursor flag，而它是機制強制、不是產品寬免

``cr.sa_skip_policy_quota`` 由 ``18.0.1.0.37`` / ``.39`` / ``.150`` 三支 migration
設定。既有 DB 的 plan 一律 ``ac_max_access_seats = 0``，不沿用同一顆 flag 的話，
這三支 migration 在任何帶 email_list policy 的 DB 上都會**當場中斷升版**。
flag 清掉之後租戶的下一次編輯照擋 ⇒ 不構成 grandfather。

## RED 紀律：怎麼證明紅在**這條**分支上

同一個 ``ValidationError`` 型別有五條以上既有 constraint 會丟。只斷言型別的話，
「因為別的 constraint 紅」與「因為席位 constraint 紅」輸出相同。本檔的判別式是
**訊息 marker** ``Access seat quota exceeded``：

- 它**不是**任何既有 marker 的子字串或超字串（既有兩條是
  ``Email quota exceeded`` 與 ``Access policy quota exceeded``）⇒ ``in`` 比對分得開；
- ``test_sibling_email_quota_still_owns_its_own_violation`` 是反向對照：讓
  ``sa_max_emails_per_policy`` 先擋，斷言訊息**不含**本 marker ⇒ 證明 marker 有
  鑑別力，而不是「什麼紅都會命中」。

⚠ 本檔刻意**不**在 ``assertRaises`` 區塊之後斷言「那筆資料沒落地」。
``BaseCase.assertRaises`` 在區塊外開 flushing savepoint，例外穿出即回捲 ⇒ 那種
否定式斷言回捲後**恆真**、鑑別力為零（見
``docs/tracking/pitfalls/odoo-assertraises-savepoint-rollback.md``）。本檔對例外
只斷言例外自己（型別＋訊息），那是該 doc 明列仍然正確的用法。
"""
from unittest.mock import MagicMock, patch

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.controllers.security_access import (
    SecurityAccessController,
)

#: 本次 enforcement 的訊息 marker。與既有兩條 quota 的 marker 互不為子字串。
SEAT_MARKER = 'Access seat quota exceeded'
#: 反向對照用：per-policy email 上限那條的 marker。
EMAIL_MARKER = 'Email quota exceeded'
#: 反向對照用：policy 數量那條的 marker。
POLICY_MARKER = 'Access policy quota exceeded'


def _patch_sa_request(env):
    return patch(
        'odoo.addons.woow_paas_platform.controllers.security_access.request',
        new=MagicMock(env=env),
    )


class _SeatFixture:
    """SA ＋ 兩條 route ＋ 可調 plan 的共用 fixture。

    刻意**不**繼承 ``TransactionCase``：Odoo 的 loader 會把抽象基底也收去跑。
    ⚠ 同理，本 class 內**不得**出現 ``test_*`` 方法——寫在 mixin 裡的 test 一條
    都不會被執行（``loader.py::get_module_test_cases`` 只看 class 自己的
    ``__dict__``），輸出與「跑過且全綠」不可區分。
    """

    def setUp(self):
        super().setUp()
        self.SA = self.env['woow_paas_platform.security_access']
        self.Route = self.env['woow_paas_platform.security_access_route']
        self.Policy = self.env[
            'woow_paas_platform.security_access_access_policy']
        self.Rule = self.env[
            'woow_paas_platform.security_access_access_policy_rule']
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Seat WS', 'owner_id': self.env.user.id,
        })
        self.sa = self.SA.create({
            'name': 'Seat GW', 'workspace_id': self.workspace.id,
        })
        self.r1 = self.Route.create({
            'security_access_id': self.sa.id, 'subdomain_prefix': 'seat-r1'})
        self.r2 = self.Route.create({
            'security_access_id': self.sa.id, 'subdomain_prefix': 'seat-r2'})

    # ---------------- fixture helpers ---------------- #

    def _subscribe(self, seats, **plan_vals):
        """把 SA 掛上一個帶 ``ac_max_access_seats = seats`` 的方案。

        兩條姊妹 quota 的預設值刻意放寬（policies=10、emails/policy=0＝不限），
        讓任何紅燈只可能來自席位那條。要測姊妹 constraint 時再自己覆寫。
        """
        vals = {
            'name': 'Seat Plan %s' % seats,
            'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 100, 'sh_no_of_billing_cycle': -1,
            'sa_max_access_policies': 10,
            'sa_max_emails_per_policy': 0,
            'ac_max_access_seats': seats,
        }
        vals.update(plan_vals)
        plan = self.env['sh.subscription.plan'].create(vals)
        sub = self.env['sh.subscription.subscription'].create({
            'sh_partner_id': self.env.user.partner_id.id,
            'state': 'in_progress'})
        sub.sh_subscription_plan_id = plan.id
        self.sa.subscription_id = sub.id
        return plan

    def _emails_rule(self, value):
        return (0, 0, {'group': 'include', 'rule_type': 'email_list',
                       'rule_value': value})

    def _policy(self, route, name, emails):
        return self.Policy.create({
            'route_id': route.id, 'name': name, 'decision': 'allow',
            'rule_ids': [self._emails_rule(emails)]})

    # ---------------- RED 判別式 ---------------- #

    def _expect_seat_block(self, callable_):
        """要求被擋，**且**擋它的是席位那條 constraint。

        兩種紅法各自可辨：
        - 完全沒丟例外 ⇒ ``assertRaises`` 印 "ValidationError not raised"
          ⇒ 席位分支根本沒被走到（實作還沒接線，或 trigger 沒掛對）。
        - 丟了但 marker 不符 ⇒ 斷言訊息會把**實際擋它的那條**原文印出來
          ⇒ 紅在別條分支上，本條的結論不成立。
        """
        with self.assertRaises(ValidationError) as ctx:
            callable_()
        msg = str(ctx.exception)
        self.assertIn(
            SEAT_MARKER, msg,
            '擋是擋了，但不是席位那條 constraint 擋的。實際訊息：%s' % msg)
        return msg

    def _assert_not_blocked(self, callable_):
        """要求**不**被擋；被擋時把原文印出來（否則只看到 stack trace）。"""
        try:
            return callable_()
        except ValidationError as exc:
            self.fail('不該被擋，但被擋了：%s' % exc)

    def _pool_policy_count(self):
        return self.Policy.search_count(
            [('security_access_id', '=', self.sa.id)])


class TestAcSeatQuotaModel(_SeatFixture, TransactionCase):
    """model 層：policy 寫入面的席位配額。"""

    allow_inherited_tests_method = True

    def test_pool_over_seats_is_blocked_across_routes(self):
        """主場景：席位 2，跨兩條 route 湊到 3 個相異位址 ⇒ 擋。

        **預期 RED（實作前）**：完全沒有例外 ⇒ "ValidationError not raised"。
        跨 route 是刻意的——判準逐字是「跨該訂閱全部 route 去重」，只在單一
        policy 內測的話，「pooled 生效」與「per-policy 上限剛好也擋住」兩種
        情況輸出相同。
        """
        self._subscribe(seats=2)
        self._assert_not_blocked(
            lambda: self._policy(self.r1, 'p1', 'a@x.com, b@x.com'))
        msg = self._expect_seat_block(
            lambda: self._policy(self.r2, 'p2', 'c@x.com'))
        # 訊息要說得出「上限是多少 / 現在是多少」，否則租戶看不懂要買幾席。
        self.assertIn('2', msg, msg)
        self.assertIn('3', msg, msg)

    def test_pooled_count_dedupes_the_same_address_across_routes(self):
        """去重確實發生：同一位址出現在兩條 route 上仍只算一席。

        **預期 GREEN（實作前後皆綠）**——單看它沒有鑑別力（沒接線時也綠）。
        它的意義是與上一條配對：上一條證明「3 個相異位址會擋」，本條證明
        「第 3 筆**不是**新位址就不擋」⇒ 兩條合起來才排除「其實是在數 rule
        筆數／policy 筆數」這個假說。
        """
        self._subscribe(seats=2)
        self._policy(self.r1, 'p1', 'a@x.com, b@x.com')
        self._assert_not_blocked(
            lambda: self._policy(self.r2, 'p2', 'a@x.com'))
        self.assertEqual(self._pool_policy_count(), 2)

    def test_dedupe_is_case_insensitive_but_a_new_address_still_blocks(self):
        """大小寫視為同一人（CF 的 email 比對不分大小寫 ⇒ 同一人＝同一席）。

        **預期 RED（實作前）**：後半段沒有例外。前半段（大小寫去重）實作前也
        會綠，所以本測試把兩半放在一起——只有後半段有鑑別力，前半段負責釘住
        「不是靠『把去重整個關掉』來讓後半段變紅」。
        """
        self._subscribe(seats=2)
        self._policy(self.r1, 'p1', 'a@x.com, b@x.com')
        self._assert_not_blocked(
            lambda: self._policy(self.r2, 'p2-upper', 'A@X.COM'))
        self._expect_seat_block(
            lambda: self._policy(self.r2, 'p2-new', 'c@x.com'))

    def test_zero_seats_blocks_the_very_first_address(self):
        """0 ＝ 一席都沒有（**不是** unlimited）。

        **預期 RED（實作前）**：沒有例外。這條是 0 語意的唯一判別式——若有人
        照兩條姊妹 quota 的慣例把 0 寫成豁免，本測試會紅，而其他每一條都還是綠。
        """
        self._subscribe(seats=0)
        msg = self._expect_seat_block(
            lambda: self._policy(self.r1, 'p-zero', 'a@x.com'))
        self.assertIn('0', msg, msg)

    def test_no_subscription_is_grandfathered_like_the_sibling_quotas(self):
        """無訂閱／無方案 ⇒ 不擋，與 ``_check_policy_quota`` 同款。

        **預期 GREEN（實作前後皆綠）**。實作後它才有鑑別力：漏掉
        ``if not plan: continue`` 的話，沒訂閱的 SA 會把 ``ac_max_access_seats``
        讀成空 recordset 的 0 而被全擋。
        """
        self.assertFalse(self.sa.subscription_id)
        self._assert_not_blocked(
            lambda: self._policy(self.r1, 'p-orphan', 'a@x.com, b@x.com'))

    def test_sibling_email_quota_still_owns_its_own_violation(self):
        """🔴 marker 鑑別力的反向對照：讓姊妹 constraint 先擋。

        席位放寬到 10、per-policy email 上限收到 2，然後一次塞 3 個位址 ⇒
        必須是 ``Email quota exceeded`` 擋的，訊息**不得**含席位 marker。

        **預期 GREEN（實作前後皆綠）**，而它正是「另一種紅法」的來源：若席位
        constraint 寫成無差別擋，本條會紅在「訊息不該含 SEAT_MARKER」上，
        與其他測試的紅法完全不同，一眼分得出是哪條分支出事。
        """
        self._subscribe(seats=10, sa_max_emails_per_policy=2)
        with self.assertRaises(ValidationError) as ctx:
            self._policy(self.r1, 'p-email-cap', 'a@x.com, b@x.com, c@x.com')
        msg = str(ctx.exception)
        self.assertIn(EMAIL_MARKER, msg, msg)
        self.assertNotIn(SEAT_MARKER, msg, msg)

    def test_sibling_policy_quota_still_owns_its_own_violation(self):
        """同上，對 ``sa_max_access_policies`` 那條再做一次。

        席位放到 10（夠用）、policy 數收到 1 ⇒ 第二筆必須是
        ``Access policy quota exceeded`` 擋的。
        """
        self._subscribe(seats=10, sa_max_access_policies=1)
        self._policy(self.r1, 'p1', 'a@x.com')
        with self.assertRaises(ValidationError) as ctx:
            self._policy(self.r2, 'p2', 'b@x.com')
        msg = str(ctx.exception)
        self.assertIn(POLICY_MARKER, msg, msg)
        self.assertNotIn(SEAT_MARKER, msg, msg)

    def test_migration_cursor_flag_bypasses_the_seat_check(self):
        """``cr.sa_skip_policy_quota`` 沿用同一顆 flag。

        **預期 GREEN（實作前後皆綠）**，實作後才有鑑別力：不沿用的話，
        ``.37`` / ``.39`` / ``.150`` 三支 migration 在任何帶 email_list policy
        的既有 DB（plan 一律 ``ac_max_access_seats = 0``）上都會當場中斷升版。

        ⚠ 這**不是** grandfather：flag 只活在 migration 那一顆 transaction 裡，
        清掉之後租戶的下一次編輯照擋（見 ``TestAcSeatBasePlanBlastRadius``）。
        """
        self._subscribe(seats=0)
        self.env.cr.sa_skip_policy_quota = True
        try:
            self._assert_not_blocked(
                lambda: self._policy(self.r1, 'p-mig', 'a@x.com, b@x.com'))
        finally:
            self.env.cr.sa_skip_policy_quota = False
        # flag 清掉後同樣的寫入必須擋——證明剛才綠的是豁免、不是整條沒接線。
        self._expect_seat_block(
            lambda: self._policy(self.r2, 'p-after-mig', 'c@x.com'))


class TestAcSeatQuotaRuleLayer(_SeatFixture, TransactionCase):
    """rule 層：直接對 rule 表寫入的兩條路徑（#352 同款繞過面）。

    policy 上的 ``@api.constrains`` 不會因為「單獨建／改一條 rule」而觸發，
    必須由 rule 的 ``create`` / ``write`` 顯式回呼。漏掉這層的話，「先建一個
    合法 policy、再單獨塞一條 email_list rule」整條繞過席位計數。
    """

    allow_inherited_tests_method = True

    def test_standalone_rule_create_is_counted(self):
        """**預期 RED（實作前）**：``Rule.create`` 沒有例外。"""
        self._subscribe(seats=1)
        pol = self._policy(self.r1, 'p1', 'a@x.com')
        self._expect_seat_block(lambda: self.Rule.create({
            'policy_id': pol.id, 'group': 'include',
            'rule_type': 'email_list', 'rule_value': 'b@x.com'}))

    def test_standalone_rule_write_is_counted(self):
        """**預期 RED（實作前）**：``rule.write`` 沒有例外。"""
        self._subscribe(seats=1)
        pol = self._policy(self.r1, 'p1', 'a@x.com')
        rule = pol.rule_ids.filtered(lambda r: r.rule_type == 'email_list')
        self.assertTrue(rule, '前提不成立：policy 上沒有 email_list rule')
        self._expect_seat_block(
            lambda: rule.write({'rule_value': 'a@x.com, b@x.com'}))

    def test_rule_unlink_lowering_the_count_is_not_blocked(self):
        """移除位址只會讓計數下降 ⇒ 不擋（與姊妹 constraint 的 unlink 同款）。

        **預期 GREEN（實作前後皆綠）**。它守的是「別把 unlink 也接上去」——
        接上去的話，租戶想從超額狀態縮回合規會被自己的超額卡住，變成不可修復。
        """
        self._subscribe(seats=2)
        p1 = self._policy(self.r1, 'p1', 'a@x.com, b@x.com')
        p2 = self._policy(self.r2, 'p2', 'a@x.com')
        self._assert_not_blocked(lambda: p2.rule_ids.unlink())
        self.assertTrue(p1.exists())


class TestAcSeatQuotaBff(_SeatFixture, TransactionCase):
    """BFF 可達性：model constraint 的訊息要真的走得到 API 回應。

    ``_create_policy`` 用 ``except Exception`` 包住 ``Policy.create`` 並回
    ``{"success": False, "error": str(e)}`` ⇒ 席位那條**不需要**自己的 BFF
    pre-check（``sa_max_access_policies`` 需要，是因為它的 0 豁免必須在
    pre-check 之前生效）。本測試把「不需要」這個結論釘成可證偽的。
    """

    allow_inherited_tests_method = True

    def test_bff_create_policy_surfaces_the_seat_quota_error(self):
        """**預期 RED（實作前）**：回的是 ``success: True``。"""
        self._subscribe(seats=1)
        self._policy(self.r1, 'p1', 'a@x.com')
        controller = SecurityAccessController()
        with _patch_sa_request(self.env):
            result = controller._create_policy(self.sa, {
                'name': 'bff-over', 'route_id': self.r2.id,
                'decision': 'allow',
                'include': [{'rule_type': 'email_list',
                             'rule_value': 'b@x.com'}]})
        self.assertFalse(result.get('success'), result)
        self.assertIn(SEAT_MARKER, result.get('error', ''), result)


class TestAcSeatBasePlanBlastRadius(_SeatFixture, TransactionCase):
    """🔴 **此 class 斷言的行為未經拍板，待覆核裁決**（#521 決策點 4）。

    seed 的 base 方案全部 ``ac_max_access_seats = 0``（欄位 help 逐字「seed 一律
    不寫此欄」）⇒ 照 1-A 逐字接線之後，**任何 base 方案訂閱上既有的 email_list
    policy，一旦被編輯就會被擋**。

    grandfather ／ 全擋 ／ 只擋新增三種走法屬產品語意、在 Eugene 手上。本 class
    **不做任何一種**，只把「照判準逐字實作出來的行為」斷言成可見的：

    - ``test_pre_existing_policy_is_blocked_on_edit`` — 一碰就擋（**這一格未拍板**）
    - ``test_pre_existing_policy_survives_untouched`` — 沒碰就還在（不會被追溯刪除）

    兩條合起來才是完整的爆炸半徑：**存量資料不動，但存量資料不可再編輯。**
    """

    allow_inherited_tests_method = True

    def _pre_existing_policy(self):
        """先在**無訂閱**狀態建 policy（走 grandfather 分支），再掛上 base 方案。

        這是在重演升級路徑：既有 DB 的 policy 早於本次 enforcement 存在，而它
        們的方案是 ``ac_max_access_seats = 0`` 的 base。
        """
        self.assertFalse(self.sa.subscription_id,
                         '前提不成立：建 policy 前 SA 不該有訂閱')
        pol = self._policy(self.r1, 'legacy', 'a@x.com, b@x.com')
        self._subscribe(seats=0)
        return pol

    def test_pre_existing_policy_is_blocked_on_edit(self):
        """**未經拍板**：base 方案（0 席）上的既有 policy，改 email 就被擋。

        **預期 RED（實作前）**：沒有例外。
        """
        pol = self._pre_existing_policy()
        self._expect_seat_block(lambda: pol.write({
            'rule_ids': [(5, 0, 0), self._emails_rule('a@x.com')]}))

    def test_pre_existing_policy_survives_untouched(self):
        """沒被編輯的既有 policy 仍在、內容不變（不做追溯刪除或清空）。

        **預期 GREEN（實作前後皆綠）**。它是上一條的另一半：少了它，「一碰就擋」
        會被誤讀成「既有資料也一起沒了」。
        """
        pol = self._pre_existing_policy()
        self.assertTrue(pol.exists())
        self.assertEqual(
            pol.rule_ids.filtered(
                lambda r: r.rule_type == 'email_list').rule_value,
            'a@x.com, b@x.com')

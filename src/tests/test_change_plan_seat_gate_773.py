"""#773：換方案的降級前置合規 gate ＋ #521 的「換方案路徑席位失明」。

Eugene 裁決（#773 留言 **10716**）逐字：降級「由使用者自己去刪到符合規格才可以換
plan，降級前應該先警告使用者，不由系統全刪除，所以系統要能偵測是否合規格」。
本檔把那個偵測釘成可證偽的行為。

## 判別式設計（為什麼這些斷言不是零鑑別力）

1. **兩個 marker 刻意互不包含**：寫入守衛的訊息是 ``Access seat quota exceeded``，
   gate 的是 ``Delete access rules or policies`` / ``Delete every email allow-list rule``。
   任一方的訊息都**不是**另一方的子字串 ⇒ 「被擋了」這件事無法同時滿足兩邊的斷言，
   拿 gate 的斷言去測守衛（或反過來）一定紅。⛔ 新增 marker 時不得寫成既有 marker 的超字串。
2. **「不該被擋」一律斷言到具體結果**，不是斷言「沒丟例外」——後者被任何提前 return 滿足。
3. **gate 拒絕之後必須斷言「什麼都沒被刪」**：本單的裁決核心是「系統不自動刪」，
   而「擋住了」與「擋住且順手刪了」在只看回傳值時完全同形。
## ⛔ 本檔**沒有**涵蓋（明列，避免被讀成「四條路徑都端到端驗過了」）

- **CS／SH 兩個 HTTP 端點的端到端行為**：本檔只用 AST 釘住它們「有呼叫 gate 且
  排在第一個寫入之前」（``TestSeatGateIsWiredBeforeFirstWrite``），
  ⛔ 沒有實際打過那兩個端點。SA 端點同理。
  wizard 那條是唯一有端到端行為斷言的（``TestSeatGateWizardWiring``）。
- **前端**：`ac_used_access_seats` 進 payload、PlanSelector 標註——無測試。
- **i18n**：msgstr 有沒有真的被 ``_()`` 讀到（承載體正確與否）需要真 DB，未驗。

4. **最尖銳的一條是 `test_gate_uses_target_plan_not_current_plan`**：
   若有人把 gate 改成走 ``_anchor_plan()``（讀錨點**當下**的方案），
   降級時拿到的是**舊** cap ⇒ 永遠放行、gate 形同不存在，而所有「升級不擋」
   「合規放行」的測試**照樣全綠**。那一條是唯一能把這個假綠抓出來的。
"""
import ast
import pathlib

from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase

from .test_ac_access_seat_enforcement_521 import SEAT_MARKER, _SeatFixture

# gate 的兩則訊息各自的 marker（⛔ 皆非 SEAT_MARKER 的子字串／超字串）
GATE_MARKER = 'Delete access rules or policies'
GATE_MARKER_ZERO = 'Delete every email allow-list rule'


class TestSeatGateRuler(_SeatFixture, TransactionCase):
    """gate 用的那把尺：與寫入守衛同一把，且 cap 由呼叫端給。"""

    allow_inherited_tests_method = True

    def _plan_with_seats(self, seats):
        """只造一個 plan，**不**掛到 SA 上（gate 要拿它當『目標方案』）。"""
        return self.env['sh.subscription.plan'].create({
            'name': 'Target Plan %s' % seats,
            'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 100, 'sh_no_of_billing_cycle': -1,
            'sa_max_access_policies': 10,
            'sa_max_emails_per_policy': 0,
            'ac_max_access_seats': seats,
        })

    # ---------------- 尺的一致性 ---------------- #

    def test_usage_helper_counts_the_whole_anchor_pool_deduped(self):
        """用量＝**整個錨點池**去重後的位址數，不是單一 policy 的。

        兩條 route 各一個 policy、位址有重疊：3 個字面、2 個相異。
        若 helper 改成只算被傳入的那批或不去重，這裡會得到 1／2／4 而不是 2。
        """
        self._subscribe(seats=9)
        self._policy(self.r1, 'p1', 'a@x.com, b@x.com')
        self._policy(self.r2, 'p2', 'B@x.com')      # 與 b@x.com 同一人（大小寫）
        used = self.Policy._seat_usage_for_anchor(self.sa)
        self.assertEqual(
            used, {'a@x.com', 'b@x.com'},
            '用量必須是錨點池跨 policy 去重、且大小寫折疊後的集合')

    def test_helper_and_write_guard_agree_on_the_same_fixture(self):
        """同一份資料、同一個 cap，gate 與寫入守衛必須給**同一個判定**。

        這是「一把尺」的可證偽形式：兩者若各自數各自的，遲早在某個 fixture 上分岔。
        判定要兩個方向都測，只測「都說擋」會被「兩邊都無差別擋」滿足。
        """
        self._subscribe(seats=5)
        self._policy(self.r1, 'p1', 'a@x.com, b@x.com')   # 池內 2 個相異位址
        generous = self._plan_with_seats(5)
        tight = self._plan_with_seats(1)

        # 方向一：cap 放得下 ⇒ gate 放行，且實際掛上去之後守衛也放行
        self.assertIsNone(self.Policy._seat_gate_denial(self.sa, generous))
        self._assert_not_blocked(
            lambda: self._policy(self.r2, 'p2', 'a@x.com'))  # 同位址、不增席

        # 方向二：cap 放不下 ⇒ gate 擋；把 SA 實際換到同一個 cap 之後守衛也擋
        self.assertIsNotNone(
            self.Policy._seat_gate_denial(self.sa, tight),
            'gate 應判超額：池內 2 個相異位址、目標方案只有 1 席')
        self.sa.subscription_id.sh_subscription_plan_id = tight.id
        self._expect_seat_block(
            lambda: self._policy(self.r1, 'p3', 'zzz@x.com'))

    def test_boundary_is_strict_greater_than(self):
        """``used == seats`` 放行、``used == seats + 1`` 擋——與守衛同一個比較子。"""
        self._subscribe(seats=2)
        self._policy(self.r1, 'p1', 'a@x.com, b@x.com')
        exactly_two = self._plan_with_seats(2)
        self.assertIsNone(
            self.Policy._seat_gate_denial(self.sa, exactly_two),
            '用量等於席數應放行（比較子必須是嚴格 >）')
        one_seat = self._plan_with_seats(1)
        self.assertIsNotNone(
            self.Policy._seat_gate_denial(self.sa, one_seat),
            '用量 2 > 席數 1 應被擋')

    def test_zero_seats_is_zero_not_unlimited(self):
        """``ac_max_access_seats == 0`` ＝一席都沒有（#521 留言 9960 §4 拍板）。

        ⚠ 相鄰的 ``sa_max_access_policies`` / ``sa_max_emails_per_policy`` 的 0
        **是** unlimited，兩種語意刻意相反。若有人「順手對齊」，本條會紅。
        """
        self._subscribe(seats=5)
        self._policy(self.r1, 'p1', 'a@x.com')
        denial = self.Policy._seat_gate_denial(self.sa, self._plan_with_seats(0))
        self.assertIsNotNone(denial, '0 席必須擋，不得當成 unlimited')
        self.assertIn(GATE_MARKER_ZERO, denial['error'], denial)
        self.assertEqual(denial['target_seats'], 0)
        self.assertEqual(denial['current_seats'], 1)

    def test_gate_uses_target_plan_not_current_plan(self):
        """🔴 **本檔最尖銳的一條**：gate 必須拿**目標** plan 的 cap，⛔ 不是錨點當下的。

        重演真正的降級：SA 現在掛 5 席方案、已列 3 個位址（完全合規），
        目標是 1 席方案。若 gate 誤走 ``_anchor_plan()``（讀當下的 5 席），
        它會判「合規、放行」——而那正是本單要修的失明本身，
        **且所有其他測試照樣全綠**（升級不擋、合規放行都仍成立）。
        """
        self._subscribe(seats=5)
        self._policy(self.r1, 'p1', 'a@x.com, b@x.com, c@x.com')
        denial = self.Policy._seat_gate_denial(self.sa, self._plan_with_seats(1))
        self.assertIsNotNone(
            denial,
            '拿目標方案（1 席）比對用量（3）必須擋；若這裡放行，代表 gate 讀的是'
            '錨點當下的方案（5 席）＝ #521 的失明沒有被修掉')
        self.assertEqual(denial['current_seats'], 3)
        self.assertEqual(denial['target_seats'], 1)
        self.assertIn(GATE_MARKER, denial['error'], denial)

    def test_upgrade_passes_without_any_downgrade_classification(self):
        """升級天然通過——⇒ 實作**不需要**先判「這次是不是降級」（陽性對照）。"""
        self._subscribe(seats=1)
        self._policy(self.r1, 'p1', 'a@x.com')
        self.assertIsNone(
            self.Policy._seat_gate_denial(self.sa, self._plan_with_seats(7)),
            '席數變大時 gate 不該擋')

    def test_gate_message_is_not_the_write_guard_message(self):
        """兩個 marker 互不包含 ⇒ 用 gate 的斷言測守衛（或反過來）必紅。

        這一條保護的是**其他所有測試的鑑別力**：marker 一旦重疊，
        「被擋了」就會同時滿足兩邊，wiring 測試全部退化成零鑑別力。
        """
        self._subscribe(seats=5)
        self._policy(self.r1, 'p1', 'a@x.com')
        gate_msg = self.Policy._seat_gate_denial(
            self.sa, self._plan_with_seats(0))['error']
        self.assertNotIn(SEAT_MARKER, gate_msg,
                         'gate 訊息不得含守衛 marker，否則兩者無法分辨')
        # 守衛的訊息：SA 現在掛 1 席（見上），加第 2 個相異位址即超額
        self.sa.subscription_id.sh_subscription_plan_id = \
            self._plan_with_seats(1).id
        guard_msg = self._expect_seat_block(
            lambda: self._policy(self.r2, 'p2', 'b@x.com'))
        self.assertNotIn(GATE_MARKER, guard_msg,
                         '守衛訊息不得含 gate marker，否則兩者無法分辨')
        self.assertNotIn(GATE_MARKER_ZERO, guard_msg)

    # ---------------- 裁決的核心：不自動刪 ---------------- #

    def test_gate_denial_deletes_nothing(self):
        """🔴 裁決核心：gate 只**回報**，⛔ 不刪、不停用任何存取權。

        「擋住了」與「擋住且順手刪了」在只看回傳值時完全同形 ⇒ 必須另外斷言存量。
        """
        self._subscribe(seats=5)
        self._policy(self.r1, 'p1', 'a@x.com, b@x.com')
        self._policy(self.r2, 'p2', 'c@x.com')
        before_policies = self._pool_policy_count()
        before_addresses = self.Policy._seat_usage_for_anchor(self.sa)
        self.assertTrue(before_policies and before_addresses, '前提：池非空')

        self.assertIsNotNone(
            self.Policy._seat_gate_denial(self.sa, self._plan_with_seats(0)))

        self.assertEqual(self._pool_policy_count(), before_policies,
                         'gate 不得刪除任何 policy')
        self.assertEqual(self.Policy._seat_usage_for_anchor(self.sa),
                         before_addresses,
                         'gate 不得移除任何位址')


class TestSeatGateWizardWiring(_SeatFixture, TransactionCase):
    """第 4 條換方案路徑（後台 wizard）真的有掛 gate，且擋在**副作用之前**。

    wizard 是四條路徑裡唯一沒有 plan 白名單閘的一條；只補三個 HTTP 端點的話，
    gate 的目的（付費界線生效）會被它直接繞過。
    """

    allow_inherited_tests_method = True

    def setUp(self):
        super().setUp()
        self.CS = self.env['woow_paas_platform.cloud_service']
        self.Wizard = self.env['woow_paas_platform.change_plan_wizard']

    def _cs_with_policies(self, seats, addresses):
        """建一台 CS、掛上 ``seats`` 席方案，並在它底下列出 ``addresses``。"""
        template = self.env['woow_paas_platform.cloud_app_template'].sudo().create({
            'name': 'Seat Gate 773 Tmpl',
            'slug': 'seat-gate-773',
            'helm_repo_url': 'oci://example.invalid',
            'helm_chart_name': 'seat-gate-773',
            'helm_chart_version': '0.0.0',
            'is_active': True,
        })
        svc = self.CS.create({
            'name': 'Seat Gate CS',
            'workspace_id': self.workspace.id,
            'template_id': template.id,
        })
        plan = self.env['sh.subscription.plan'].create({
            'name': 'CS Seat Plan %s' % seats,
            'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 100, 'sh_no_of_billing_cycle': -1,
            'sa_max_access_policies': 10,
            'sa_max_emails_per_policy': 0,
            'ac_max_access_seats': seats,
        })
        sub = self.env['sh.subscription.subscription'].create({
            'sh_partner_id': self.env.user.partner_id.id,
            'state': 'in_progress'})
        sub.sh_subscription_plan_id = plan.id
        svc.subscription_id = sub.id
        self.Policy.create({
            'cloud_service_id': svc.id, 'name': 'cs-pol', 'decision': 'allow',
            'rule_ids': [self._emails_rule(addresses)]})
        return svc, plan, sub

    def test_wizard_blocks_downgrade_before_any_side_effect(self):
        """降級被擋，**且**退點／關舊訂閱都沒有發生。

        ⚠ 只斷言「丟了 UserError」是不夠的：wizard 上游三道閘也丟 UserError，
        而且 gate 若插錯位置（退點之後）一樣會丟 —— 差別在副作用有沒有先發生。
        所以本條同時斷言 marker、舊訂閱狀態、以及錢包餘額。
        """
        svc, _plan, sub = self._cs_with_policies(seats=3,
                                                 addresses='a@x.com, b@x.com')
        target = self.env['sh.subscription.plan'].create({
            'name': 'CS Seat Plan target 0',
            'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 50, 'sh_no_of_billing_cycle': -1,
            'sa_max_access_policies': 10, 'sa_max_emails_per_policy': 0,
            'ac_max_access_seats': 0,
        })
        partner = self.env.user.partner_id
        balance_before = partner.sudo().wallet_balance
        policies_before = self.Policy.search_count(
            [('cloud_service_id', '=', svc.id)])

        wizard = self.Wizard.create({
            'cloud_service_id': svc.id,
            'current_plan_id': sub.sh_subscription_plan_id.id,
            'new_plan_id': target.id,
        })
        with self.assertRaises(UserError) as ctx:
            wizard.action_change_plan()

        msg = str(ctx.exception)
        self.assertIn(
            GATE_MARKER_ZERO, msg,
            '擋是擋了，但不是 gate 擋的（可能是上游三道閘）。實際訊息：%s' % msg)
        # 擋在副作用之前：舊訂閱沒被關、錢包沒動、存取權沒被刪
        sub.invalidate_recordset(['state'])
        self.assertEqual(sub.state, 'in_progress',
                         'gate 應擋在關閉舊訂閱之前')
        partner.sudo().invalidate_recordset(['wallet_balance'])
        self.assertEqual(partner.sudo().wallet_balance, balance_before,
                         'gate 應擋在退點之前')
        self.assertEqual(
            self.Policy.search_count([('cloud_service_id', '=', svc.id)]),
            policies_before, 'gate ⛔ 不得刪除任何存取權')

    def test_wizard_allows_upgrade(self):
        """陽性對照：席數變大時 wizard 不該被 gate 擋。

        斷言「不是 gate 的錯」而不是「完全沒有例外」——wizard 後半還會走
        `_create_and_confirm_subscription` 等獨立關卡，那些不在本條射程內。
        """
        svc, _plan, sub = self._cs_with_policies(seats=1, addresses='a@x.com')
        target = self.env['sh.subscription.plan'].create({
            'name': 'CS Seat Plan target 9',
            'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 200, 'sh_no_of_billing_cycle': -1,
            'sa_max_access_policies': 10, 'sa_max_emails_per_policy': 0,
            'ac_max_access_seats': 9,
        })
        wizard = self.Wizard.create({
            'cloud_service_id': svc.id,
            'current_plan_id': sub.sh_subscription_plan_id.id,
            'new_plan_id': target.id,
        })
        try:
            wizard.action_change_plan()
        except UserError as exc:
            self.assertNotIn(GATE_MARKER, str(exc))
            self.assertNotIn(GATE_MARKER_ZERO, str(exc))


class TestSeatGateIsWiredBeforeFirstWrite(TransactionCase):
    """四條換方案路徑都呼叫 gate，**且呼叫點排在第一個寫入之前**（靜態判準）。

    ## 為什麼用 AST 而不是端到端打四次端點

    這裡要釘的性質是**順序**：「gate 在退點之前」。端到端測試對它的鑑別力很弱——
    gate 就算被移到退點之後，只要交易最後整包回捲，端到端看到的仍然是
    「被擋了、資料沒變」，與正確實作**在輸出上不可區分**。真正的差別是
    「有沒有先產生副作用再撤」，那要靠讀順序才看得清楚。

    ⚠ 這是靜態檢查，涵蓋的是「呼叫點的相對位置」；
    ⛔ 它**不**證明 gate 的判定正確（那由 ``TestSeatGateRuler`` 負責），
    也不涵蓋 CS／SH 兩個 HTTP 端點的端到端行為（見本檔 module docstring 的
    未涵蓋說明）。
    """

    # 四條路徑：(相對路徑, 函式名)
    PATHS = [
        ('controllers/paas.py', 'api_service_change_plan'),
        ('controllers/security_access.py', 'api_sa_change_plan'),
        ('controllers/smart_home.py', 'api_smarthome_change_plan'),
        ('wizard/change_subscription_plan_wizard.py', 'action_change_plan'),
    ]
    # 該路徑上第一個 DB 副作用（退點）——gate 必須排在它前面
    FIRST_WRITE = 'refund_wallet_points'
    GATE_CALL = '_seat_gate_denial'

    def _func_node(self, rel_path, func_name):
        root = pathlib.Path(__file__).resolve().parent.parent
        tree = ast.parse((root / rel_path).read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == func_name:
                return node
        self.fail('%s 裡找不到 %s（函式被改名？路徑變了？）'
                  % (rel_path, func_name))

    def _first_line_calling(self, func_node, attr_name):
        lines = [n.lineno for n in ast.walk(func_node)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == attr_name]
        return min(lines) if lines else None

    def test_all_four_paths_call_the_gate_before_the_first_write(self):
        for rel_path, func_name in self.PATHS:
            with self.subTest(path=rel_path, func=func_name):
                node = self._func_node(rel_path, func_name)
                gate_line = self._first_line_calling(node, self.GATE_CALL)
                self.assertIsNotNone(
                    gate_line,
                    '%s::%s 沒有呼叫 %s ⇒ 這條換方案路徑繞得過 gate'
                    % (rel_path, func_name, self.GATE_CALL))
                write_line = self._first_line_calling(node, self.FIRST_WRITE)
                self.assertIsNotNone(
                    write_line,
                    '%s::%s 找不到 %s——本測試的錨點沒了，判準已失效，'
                    '請重新確認該路徑第一個副作用是什麼'
                    % (rel_path, func_name, self.FIRST_WRITE))
                self.assertLess(
                    gate_line, write_line,
                    '%s::%s 的 gate 在第 %s 行、第一個寫入在第 %s 行——'
                    'gate 必須排在寫入之前，否則只能「跑一半再整包回捲」，'
                    '而裁決要的是擋住' % (rel_path, func_name,
                                          gate_line, write_line))

    def test_the_anchor_probe_has_discriminating_power(self):
        """陽性對照：上面那條的兩個錨點都真的抓得到東西。

        若 ``_first_line_calling`` 因為 AST 形狀改變而永遠回 None，
        上面那條會在 assertIsNotNone 就紅；但若它永遠回同一個小數字，
        ``assertLess`` 可能被巧合滿足。這裡用一個**已知順序**的對照確認探針有效。
        """
        node = self._func_node('wizard/change_subscription_plan_wizard.py',
                               'action_change_plan')
        gate_line = self._first_line_calling(node, self.GATE_CALL)
        write_line = self._first_line_calling(node, self.FIRST_WRITE)
        self.assertIsNotNone(gate_line)
        self.assertIsNotNone(write_line)
        self.assertNotEqual(
            gate_line, write_line,
            '兩個錨點回同一行 ⇒ 探針沒有分辨力，assertLess 的結果不可信')

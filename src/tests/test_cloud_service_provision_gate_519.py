"""#519 提案 A：cloud_service 只能由平台的開通流程建立。

不變式：**沒有任何 RPC 呼叫端**（裸 ORM／XML-RPC／JSON-RPC／`call_kw`，不論
身分、不論送什麼 vals 或 context）能讓一筆 cloud_service 落地。

為什麼守線只能長在 model 的 `create()`：owner 資格是**可自我滿足**的——任何
`base.group_user` 帳號免費自建 workspace 就自動成為該 workspace 的 owner，滿足
#676 的 `cloud_service_rule_owner_admin_manage`。而「自建 workspace 即為 owner」
是產品設計、不是缺陷（2026-08-14 裁決），所以 ACL／`ir.rule` 那層對本案結構性
無效。

## 這一檔自己的假綠面，逐個配對照

* **su 豁免**：`TransactionCase` 預設就是 su env ⇒ 守衛若讀 `env.su`，這裡每一條
  deny 都會因為守衛被靜默停用、create 成功而變紅。所以 `test_A1` 本身就是「沒有
  su 豁免」的證明。`assertTrue(self.env.su)` 把那個前提釘住：哪天
  `TransactionCase` 不再是 su env，這條測試證明的東西就換了，必須有人重新看過。
* **一律 raise**：一個「什麼都擋」的守衛會讓每一條 deny 斷言都通過。所以每一組
  deny 都配一條走開閘 helper 的 A3 型陽性對照。
* **歸因**：基底 `AccessError` 也是 ACL 拒絕會丟的型別。斷言用獨立型別
  `ProvisionGateError`，不是 `AccessError`——否則「被 ACL 擋下」會冒充「被本閘
  擋下」，而現有的 ACL 測試本來就在斷言 `AccessError`。
* **沒 raise ≠ 沒寫進去**：deny 之後另外用 raw SQL 數列數，兩個宣稱分開驗。
* **判準錨點**：context key 的字面值在本檔硬寫一份（那是攻擊者會送的東西），並
  斷言它等於 model 的常數。少了這條對帳，key 一改名，本檔的偽造測試就會在送著
  一個誰也不看的 key 的情況下繼續全綠。
"""

from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.models.cloud_service import (
    _INTERNAL_PROVISION,
    _INTERNAL_PROVISION_CTX_KEY,
    ProvisionGateError,
    _internal_provision,
)

# 硬寫一份：這是 RPC 攻擊者實際會塞進 payload 的字串。與 model 常數的對帳見
# test_ctx_key_literal_is_the_one_the_model_reads。
_CTX_KEY = 'woow_internal_provision'

# JSON-RPC / XML-RPC 表達得出來的東西。sentinel 的整個安全性就建立在「這個集合
# 裡沒有任何一個能通過 `is` 比對」上。
_JSON_EXPRESSIBLE = (True, 1, 'true', _CTX_KEY, [1], {'x': 1}, [], {}, 0.0)


class _GateFixture(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Service = self.env['woow_paas_platform.cloud_service']
        self.Template = self.env['woow_paas_platform.cloud_app_template']
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.Project = self.env['project.project']

        self.workspace = self.Workspace.create({'name': 'Gate 519 WS'})
        self.template = self.Template.create({
            'name': 'Gate 519 Template',
            'slug': 'gate-519',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'gate519',
            # 必須釘版：未釘版會被供給閘擋下，fixture 死在抵達受測步驟之前，
            # 而那個 error 長得跟「建立閘擋住了」一模一樣。
            'helm_chart_version': '1.0.0',
            'min_vcpu': 2,
            'min_ram_gb': 4.0,
            'min_storage_gb': 10,
        })

    # ------------------------------------------------------------------ #
    #  Helpers                                                           #
    # ------------------------------------------------------------------ #

    def _vals(self, name):
        return {
            'name': name,
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        }

    def _rows_named(self, name):
        """直接數 Postgres 的列數，繞開 ORM cache。

        刻意先 `flush_all()`：被接受的寫入若還在 ORM 的 write buffer 裡，光看
        cache 會讀回「沒有」而讓守衛看起來有效。「沒有 raise」與「沒有寫進去」
        是兩個不同的宣稱，這支探針把它們綁在一起。
        """
        self.env.flush_all()
        self.env.cr.execute(
            'SELECT count(*) FROM woow_paas_platform_cloud_service WHERE name = %s',
            (name,))
        return self.env.cr.fetchone()[0]


class TestProvisionGateDeniesEveryone(_GateFixture):
    """A1／A4：不論身分，裸 ORM create 一律被擋。"""

    def test_A1_bare_create_is_rejected_in_su_env(self):
        # 前提釘樁：TransactionCase 是 su env。守衛若寫成 `env.su` 豁免，這條會
        # 綠得毫無意義——而下面的 assertRaises 會直接紅，這就是它的鑑別力來源。
        self.assertTrue(
            self.env.su,
            '前提：TransactionCase 預設是 su env。這個前提變了的話，本檔「守衛對 '
            'su 也生效」的證明就換了意思，必須有人重新看過。')
        with self.assertRaises(ProvisionGateError):
            self.Service.create(self._vals('A1 bare'))
        self.assertEqual(self._rows_named('A1 bare'), 0,
                         'A1：「沒有 raise」與「沒有寫進去」是兩件事，這裡驗後者')

    def test_A1b_sudo_does_not_open_the_gate(self):
        with self.assertRaises(ProvisionGateError):
            self.Service.sudo().create(self._vals('A1b sudo'))
        self.assertEqual(self._rows_named('A1b sudo'), 0)

    def test_A3_positive_control_internal_provision_creates(self):
        """陽性對照。**沒有這條，上面每一條 deny 都可能只是「守衛一律 raise」。**"""
        service = _internal_provision(self.Service).create(self._vals('A3 ok'))
        self.assertTrue(service.exists())
        self.assertEqual(self._rows_named('A3 ok'), 1)

    def test_A4_tenant_bare_create_is_rejected_by_the_gate_not_by_acl(self):
        """租戶身分走裸 ORM：必須被**本閘**擋，且歸因要精確。

        歸因為什麼重要：這個租戶對 cloud_service 本來就可能被 ACL／ir.rule 擋，
        兩者都是 `AccessError`。只斷言 `AccessError` 的話，就算本閘整條被刪掉，
        這條測試照樣綠——那是零鑑別力。所以斷言獨立型別，並在同一條測試裡用
        開閘 helper 證明**這個身分本來建得起來**（⇒ 上面的拒絕確實來自本閘）。
        """
        tenant = self.env['res.users'].create({
            'name': 'Gate 519 Tenant', 'login': 'gate519_tenant',
            'email': 'gate519_tenant@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.assertFalse(tenant.has_group('base.group_system'),
                         '前提：這是租戶不是系統管理員')
        # workspace 不經本閘（#519 只守 cloud_service），刻意用裸 create——
        # 這同時是「自建 workspace 即為 owner 是產品設計」那條前提的現場證據。
        ws = self.Workspace.create({
            'name': 'Gate 519 Tenant WS', 'owner_id': tenant.id,
        })
        vals = {
            'name': 'A4 tenant',
            'workspace_id': ws.id,
            'template_id': self.template.id,
        }
        with self.assertRaises(ProvisionGateError):
            self.Service.with_user(tenant).create(dict(vals))
        self.assertEqual(self._rows_named('A4 tenant'), 0)

        # 陽性對照：同一個身分、同一份 vals，經開閘 helper 就建得起來 ⇒ 上面被拒
        # 的原因是本閘，不是 ACL、不是 ir.rule、不是壞掉的 fixture。
        ok_vals = dict(vals, name='A4 tenant ok')
        service = _internal_provision(self.Service.with_user(tenant)).create(ok_vals)
        self.assertTrue(service.exists())


class TestProvisionGateCannotBeForged(_GateFixture):
    """A2：JSON 表達得出來的值一個都打不開這道閘。"""

    def test_ctx_key_literal_is_the_one_the_model_reads(self):
        """本檔硬寫的 key 必須就是 model 讀的那個。

        少了這條對帳，把 model 的 key 改名之後，下面的偽造測試會繼續送一個誰也
        不看的字串、繼續全綠——測試還在，鑑別力沒了。
        """
        self.assertEqual(_CTX_KEY, _INTERNAL_PROVISION_CTX_KEY)

    def test_A2_json_expressible_values_do_not_open_the_gate(self):
        for forged in _JSON_EXPRESSIBLE:
            with self.subTest(context_value=forged):
                name = 'A2 forged %r' % (forged,)
                with self.assertRaises(ProvisionGateError):
                    self.Service.with_context(
                        **{_CTX_KEY: forged}).create(self._vals(name))
                self.assertEqual(self._rows_named(name), 0)

    def test_A2b_a_look_alike_object_does_not_open_the_gate(self):
        """連「長得一樣的另一個實例」都不行——判準是身分不是型別。

        這一條擋的是把 `is` 寫鬆成 `isinstance`／`==` 的改法：那樣改之後上面那
        組 JSON 值仍然全部被擋（型別不對），本組卻會通——沒有這條，那個改法會
        全綠通過。
        """
        look_alike = type(_INTERNAL_PROVISION)()
        self.assertIsNot(look_alike, _INTERNAL_PROVISION)
        with self.assertRaises(ProvisionGateError):
            self.Service.with_context(
                **{_CTX_KEY: look_alike}).create(self._vals('A2b look alike'))
        self.assertEqual(self._rows_named('A2b look alike'), 0)

    def test_gate_error_is_an_access_error_subclass(self):
        """型別契約：對外仍是 `AccessError`（RPC 端看到的錯誤語意不變），對內
        可分辨。改成別的基底會讓 controller 既有的例外分類漂掉。"""
        self.assertTrue(issubclass(ProvisionGateError, AccessError))


class TestSentinelSurvivesTheChainsProductionUses(_GateFixture):
    """PR1 的機械遷移是否安全，取決於 sentinel 撐不撐得過這些鏈式呼叫。"""

    def test_sudo_with_company_and_kwargs_context_all_keep_it(self):
        company = self.env.company
        # `controllers/ha_paas.py` 的形狀
        svc = _internal_provision(
            self.Service.sudo().with_company(company)
        ).create(self._vals('chain ha'))
        self.assertTrue(svc.exists())
        # `controllers/paas.py` 的形狀：開閘之後再 with_context(**kwargs)（merge）
        svc2 = _internal_provision(self.Service).with_context(
            mcp_admin_password='x').create(self._vals('chain marketplace'))
        self.assertTrue(svc2.exists())
        # 反過來：先 with_context 再開閘，一樣要成立
        svc3 = _internal_provision(
            self.Service.with_context(mcp_admin_password='x')
        ).create(self._vals('chain reversed'))
        self.assertTrue(svc3.exists())

    def test_positional_with_context_drops_the_sentinel(self):
        """`with_context(<dict>)` 是**取代**不是 merge ⇒ 把 sentinel 洗掉。

        這不是缺陷，是 Odoo 的語意；釘住它是因為它同時解釋了整道閘為什麼擋得住
        RPC——`call_kw` 對呼叫端 context 做的正是這個位置參數取代。哪天有人把開閘
        點後面接一個位置參數 `with_context({...})`，這條會紅。
        """
        opened = _internal_provision(self.Service)
        with self.assertRaises(ProvisionGateError):
            opened.with_context({'lang': 'en_US'}).create(
                self._vals('ctx replaced'))
        self.assertEqual(self._rows_named('ctx replaced'), 0)


class TestGateCoversNonObviousLandingPaths(_GateFixture):
    """`.create()` 不是唯一會讓記錄落地的路。"""

    def test_copy_goes_through_the_gate(self):
        service = _internal_provision(self.Service).create(self._vals('copy src'))
        # 重新 browse 一次拿到**沒有** sentinel 的 handle——直接 copy 原本那個
        # recordset 會沿用它的 context，測不到東西。
        plain = self.Service.browse(service.id)
        with self.assertRaises(ProvisionGateError):
            plain.copy({'name': 'copy forged'})
        self.assertEqual(self._rows_named('copy forged'), 0)
        # 陽性對照：走開閘 helper 就複製得出來
        self.assertTrue(_internal_provision(plain).copy({'name': 'copy ok'}))

    def test_nested_create_through_project_m2m_goes_through_the_gate(self):
        """`project.cloud_service_ids` 的 `(0, 0, vals)` 也會建出 cloud_service。

        純粹掃 `.create(` 的枚舉抓不到這條路，而它正是 #698 那組 RPC 測試走的
        入口。context 由被寫入的 recordset 帶下去，所以開閘 helper 掛在 project
        端就會傳播到內層的 cloud_service create——這條同時把那個傳播行為釘住。
        """
        with self.assertRaises(ProvisionGateError):
            self.Project.create({
                'name': 'Gate 519 P',
                'cloud_service_ids': [(0, 0, self._vals('nested forged'))],
            })
        self.assertEqual(self._rows_named('nested forged'), 0)

        project = _internal_provision(self.Project).create({
            'name': 'Gate 519 P ok',
            'cloud_service_ids': [(0, 0, self._vals('nested ok'))],
        })
        self.assertEqual(self._rows_named('nested ok'), 1,
                         '陽性對照：開閘掛在 project 端要能傳播到內層 create')
        self.assertTrue(project.cloud_service_ids)

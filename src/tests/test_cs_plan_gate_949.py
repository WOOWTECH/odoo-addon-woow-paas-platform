"""#949：CS 兩條 plan 白名單閘 fail-open —— template 掛零個 plan 時整段被跳過。

兩條閘寫成 `if template.subscription_plan_ids and plan not in template.subscription_plan_ids:`，
前半是 **truthiness 判斷** ⇒ 空集合時整條 if 為 False、檢查被跳過、任何 plan 都通得過：

- `paas.py::api_service_change_plan`（換方案）
- `paas.py::_create_service`（建立服務）——1157 的註解自陳與它 symmetric，兩條確實對稱，
  只是**對稱地錯**。

修法＝把 truthiness 前綴刪掉（`plan not in <空集合>` 自然為 True ⇒ 擋），與 SA 端
`security_access.py` 的「template 存在」那一格同語意。
⛔ **不是**把 `and` 改成 `or`——那會擋掉「template 有 plan 且 plan 在白名單內」的正常情況。
本檔的 `..._lets_whitelisted_plan_through` 兩條就是釘死這個錯誤修法的反向控制。

CS **沒有** template-less 這一格（`cloud_service.template_id` `required=True`，DB 側
`is_nullable=NO`；stg 實測 11/11 皆非 NULL），所以不需要 SA 的 `else` 分支（家族判準）。

## 判別式：不看「有沒有失敗」，看「停在哪一關」

兩條閘後面各自緊接著一個**確定性、零副作用**的下一關。測試斷言的是回哪一個錯誤字串：

| 閘 | 修前（閘被跳過，停在下一關） | 修後（閘擋住） |
| --- | --- | --- |
| change-plan | `No active subscription` | `Plan not available for this template` |
| create | `Password must be at least 8 characters`（n8n init 密碼檢查） | 同上 |

⇒ 修前的紅**證明控制流真的走過了那一行**，而不是「碰巧也失敗了」。
建立路徑刻意讓 template 帶 `post_deploy_init_type='n8n'`：停在密碼檢查 ⇒ 全程不建任何
service／subscription 記錄、不呼叫 operator（HttpCase 共用 transaction 不被污染）。

## 為什麼兩組各自一套 fixture

⛔ 不共用一格綠燈：任一條閘被改回 truthiness 形態時，只有**那一組**會紅。
（stg 量測與可達性分析見 PR body 與 #949 留言；今天 stg 上唯一「上架中且零 plan」的
template 是 HA PaaS，它 `conceal_instances=True` ⇒ 更早的 gate 就擋住 ⇒ 本缺陷今天
**沒有**可達路徑。本檔守的是「資料一變就會敞開」的那條線。）
"""
from odoo.tests.common import HttpCase, tagged

from ..models.cloud_service import _internal_provision

# 閘本身的錯誤訊息（修後兩條閘都該回這個）
_GATE_ERROR = 'Plan not available for this template'
# 兩條閘各自的「下一關」——看到它＝閘沒擋、控制流已經走過去了
_CHANGE_PLAN_NEXT_GATE = 'No active subscription'
_CREATE_NEXT_GATE = 'Password must be at least 8 characters'


def make_cs_template(env, slug, plans=None):
    """建一個可正常販售的 CS template（上架、不隱藏、不 conceal）。

    ``plans=None`` ⇒ 零 plan（本單的缺陷條件）。

    - 不隱藏且不 conceal 是刻意的：`api_service_change_plan` 與 `_create_service`
      都有更早的 `conceal_instances` / `is_hidden` 閘，設了就打不到受測那一行。
    - `post_deploy_init_type='n8n'` 讓建立路徑停在密碼檢查（見檔頭判別式表），
      不會真的建記錄。
    - helm_* 三欄 `required=True`（NOT NULL），不給會撞 NotNullViolation。
    """
    return env['woow_paas_platform.cloud_app_template'].sudo().create({
        'name': 'Plan Gate Fixture 949 %s' % slug,
        'slug': slug,
        'helm_repo_url': 'oci://example.invalid',
        'helm_chart_name': 'plan-gate-fixture',
        'helm_chart_version': '0.0.0',
        'is_active': True,
        'is_hidden': False,
        'conceal_instances': False,
        'admin_password_carrier': 'helm_secret',
        'post_deploy_init_type': 'n8n',
        'subscription_plan_ids': [(6, 0, plans.ids if plans else [])],
    })


class _PlanGateCase(HttpCase):
    """共用 fixture：admin session ＋ 自己名下的 workspace。

    刻意讓 owner 就是登入者本人——證明擋/不擋是 plan 歸屬判準，不是權限不足的副作用。
    """

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        self.admin_user = self.env.ref('base.user_admin')
        self.partner = self.env.ref('base.partner_admin')
        self.ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 949 %s' % self.__class__.__name__,
            'owner_id': self.admin_user.id,
        })
        # 白名單內的 plan（同 cloud_service 家族，seed 既有）
        self.own_plan = self.env.ref('woow_paas_platform.plan_odoo_monthly')
        # 別家家族的 plan——閘敞開時租戶換得到的正是這種
        self.foreign_plan = self.env.ref(
            'woow_paas_platform.plan_security_access_r1_monthly')

    def _result(self, url, params='{}'):
        """打 JSON-RPC 端點——鍵一律住在 ``params`` 裡（＝前端的形狀）。"""
        body = '{"jsonrpc":"2.0","method":"call","params":' + params + '}'
        resp = self.url_open(
            url, data=body, headers={'Content-Type': 'application/json'})
        parsed = resp.json()
        self.assertIn('result', parsed, 'resp: %s' % parsed)
        return parsed['result']

    def _assert_blocked_by_plan_gate(self, r, label):
        self.assertFalse(r.get('success'), '%s 應被 plan 白名單閘擋（回 %r）' % (label, r))
        self.assertEqual(
            r.get('error'), _GATE_ERROR,
            '%s 應回 plan 白名單閘的錯，而非後面某一關的錯——回別的錯就代表'
            '閘被跳過了（回 %r）' % (label, r))

    def _assert_passed_plan_gate(self, r, next_gate, label):
        """反向控制：閘**不該**擋住合法的 plan。

        斷言「停在下一關」而不是 success=True——閘之後還有訂閱閘／狀態閘等獨立關卡，
        測試環境本來就會停在那裡。受測面向只有一個：回的**不是** plan 白名單閘的錯。
        """
        self.assertNotEqual(
            r.get('error'), _GATE_ERROR,
            '%s 的 plan 在白名單內，不該被 plan 閘擋（回 %r）' % (label, r))
        self.assertEqual(
            r.get('error'), next_gate,
            '%s 應通過 plan 閘並停在下一關 %r（回 %r）' % (label, next_gate, r))


@tagged('post_install', '-at_install')
class TestChangePlanTemplateWhitelistGate(_PlanGateCase):
    """A 組 —— `api_service_change_plan` 的 plan 白名單閘。

    RED（修前）：`test_zero_plan_template_*` 兩條會回 `No active subscription`
    ——那是閘**後面**那一關的錯，證明閘整條被跳過。
    """

    def setUp(self):
        super().setUp()
        self.zero_plan_tpl = make_cs_template(self.env, 'plan-gate-949-a-zero')
        self.stocked_tpl = make_cs_template(
            self.env, 'plan-gate-949-a-stocked', plans=self.own_plan)

    def _svc_on(self, tpl, ref_suffix):
        return _internal_provision(
            self.env['woow_paas_platform.cloud_service']
        ).create({
            'workspace_id': self.ws.id,
            'template_id': tpl.id,
            'name': 'svc-949-%s' % ref_suffix,
            'reference_id': 'test-949-%s' % ref_suffix,
            'state': 'running',
            'subdomain': 'svc-949-%s' % ref_suffix,
            'provision_partner_id': self.partner.id,
        })

    def _change_plan(self, svc, plan):
        return self._result(
            '/api/cloud-services/services/%s/change-plan' % svc.id,
            '{"plan_id":%s}' % plan.id)

    def test_zero_plan_template_rejects_foreign_family_plan(self):
        """零 plan template 的服務不得換到別家家族（SA）的 plan。"""
        svc = self._svc_on(self.zero_plan_tpl, 'a-zero-foreign')
        self._assert_blocked_by_plan_gate(
            self._change_plan(svc, self.foreign_plan),
            'change-plan（零 plan template → SA 家族 plan）')

    def test_zero_plan_template_rejects_other_cs_template_plan(self):
        """同家族但屬於**別的 template** 的 plan 一樣不得換——閘管的是 template 歸屬。"""
        svc = self._svc_on(self.zero_plan_tpl, 'a-zero-othercs')
        self._assert_blocked_by_plan_gate(
            self._change_plan(svc, self.own_plan),
            'change-plan（零 plan template → 別的 CS template 的 plan）')

    def test_stocked_template_still_rejects_foreign_plan(self):
        """控制組：template 有掛 plan 時的既有行為不變（修前修後都綠）。"""
        svc = self._svc_on(self.stocked_tpl, 'a-stocked-foreign')
        self._assert_blocked_by_plan_gate(
            self._change_plan(svc, self.foreign_plan),
            'change-plan（有 plan 的 template → 白名單外的 plan）')

    def test_stocked_template_lets_whitelisted_plan_through(self):
        """反向控制：白名單內的 plan 不得被擋。

        ⛔ 這條釘死「把 `and` 改成 `or`」那種錯誤修法——那樣改會讓本條紅。
        """
        svc = self._svc_on(self.stocked_tpl, 'a-stocked-ok')
        self._assert_passed_plan_gate(
            self._change_plan(svc, self.own_plan),
            _CHANGE_PLAN_NEXT_GATE,
            'change-plan（有 plan 的 template → 白名單內的 plan）')


@tagged('post_install', '-at_install')
class TestCreateServiceTemplateWhitelistGate(_PlanGateCase):
    """B 組 —— `_create_service` 的 plan 白名單閘（與 A 組同缺陷、獨立一條）。

    RED（修前）：`test_zero_plan_template_*` 兩條會回
    `Password must be at least 8 characters`——閘後面 n8n init 的密碼檢查，
    證明閘整條被跳過。
    """

    def setUp(self):
        super().setUp()
        self.zero_plan_tpl = make_cs_template(self.env, 'plan-gate-949-b-zero')
        self.stocked_tpl = make_cs_template(
            self.env, 'plan-gate-949-b-stocked', plans=self.own_plan)

    def _create(self, tpl, plan, name):
        return self._result(
            '/api/workspaces/%s/services' % self.ws.id,
            '{"action":"create","template_id":%s,"name":"%s","plan_id":%s,"values":{}}'
            % (tpl.id, name, plan.id))

    def test_zero_plan_template_rejects_foreign_family_plan(self):
        self._assert_blocked_by_plan_gate(
            self._create(self.zero_plan_tpl, self.foreign_plan, 'svc949b1'),
            'create（零 plan template → SA 家族 plan）')

    def test_zero_plan_template_rejects_other_cs_template_plan(self):
        self._assert_blocked_by_plan_gate(
            self._create(self.zero_plan_tpl, self.own_plan, 'svc949b2'),
            'create（零 plan template → 別的 CS template 的 plan）')

    def test_stocked_template_still_rejects_foreign_plan(self):
        """控制組：template 有掛 plan 時的既有行為不變（修前修後都綠）。"""
        self._assert_blocked_by_plan_gate(
            self._create(self.stocked_tpl, self.foreign_plan, 'svc949b3'),
            'create（有 plan 的 template → 白名單外的 plan）')

    def test_stocked_template_lets_whitelisted_plan_through(self):
        """反向控制：白名單內的 plan 不得被擋（釘死 `or` 型錯誤修法）。"""
        self._assert_passed_plan_gate(
            self._create(self.stocked_tpl, self.own_plan, 'svc949b4'),
            _CREATE_NEXT_GATE,
            'create（有 plan 的 template → 白名單內的 plan）')

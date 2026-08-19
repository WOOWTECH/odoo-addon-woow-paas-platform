"""#778: 上游 sh_subscription_wallet SSOT（PR #14）對撞防護的平台側錨點。

上游 #14 會在 wallet 的 post_init_hook／migration 停用 base 的
``sh_subscription.sh_ir_cron_subscription_auto_invoice``——那顆 cron 是平台
唯一計費路徑（#324 後的 dunning-aware ``_sh_create_auto_invoice``）的觸發
點。那兩個時點都在 module graph 的 wallet 節點上執行，平台的 method
override 當時不在 MRO，攔不到——防線必須是 data 層 re-assert：平台的
post_init_hook／data ``<function>`` 在拓撲序上永遠跑在 wallet 之後，把
cron 開回、把 ``sh_subscription_wallet.billing_ssot`` 固定為 opt-out。

紅→綠敘事：防線不存在時，任何一次 wallet install/upgrade 都會讓兩條計費
路徑同死且零錯誤訊息（wallet cron 被平台 no-op、base cron 被上游關掉）。
本檔的 re-assert 測試都帶陽性對照（先製造「被關掉」的狀態再驗「開回來」），
不接受「跑一遍看乾不乾淨」式的恆綠。
"""
from odoo.tests.common import TransactionCase

# 與實作端常數刻意雙寫：上游 param 名／cron xmlid 是跨 repo contract，
# 上游改名時實作端只改常數會讓這裡先紅，逼出同步檢查。
BILLING_SSOT_PARAM = 'sh_subscription_wallet.billing_ssot'
BASE_CRON_XMLID = 'sh_subscription.sh_ir_cron_subscription_auto_invoice'


class _GuardFixture(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Sub = self.env['sh.subscription.subscription'].sudo()
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.cron = self.env.ref(BASE_CRON_XMLID)


class TestBaseCronReassert(_GuardFixture):
    def test_reasserts_disabled_cron_back_to_active(self):
        # 陽性對照：先複現上游 #14 的殺傷結果（active=False），再驗錨點救回。
        self.cron.sudo().write({'active': False})
        self.assertFalse(self.cron.active)

        result = self.Sub._ensure_base_auto_invoice_cron_active()

        self.assertTrue(result)
        self.assertTrue(
            self.cron.active,
            "base auto-invoice cron 是平台唯一計費路徑的觸發點，"
            "re-assert 錨點必須把被上游關掉的 cron 開回來",
        )

    def test_cron_reassert_is_idempotent_when_active(self):
        self.assertTrue(self.cron.active)

        result = self.Sub._ensure_base_auto_invoice_cron_active()

        self.assertTrue(result)
        self.assertTrue(self.cron.active)


class TestBillingSsotOptout(_GuardFixture):
    def test_optout_param_set_when_missing(self):
        self.ICP.search([('key', '=', BILLING_SSOT_PARAM)]).unlink()
        self.assertFalse(self.ICP.get_param(BILLING_SSOT_PARAM))

        self.Sub._ensure_billing_ssot_optout()

        self.assertEqual(
            self.ICP.get_param(BILLING_SSOT_PARAM), '0',
            "上游語意「未設定視同啟用 SSOT」——平台裝著的 DB 必須顯式 opt-out",
        )

    def test_optout_param_overrides_enabled_value(self):
        # 無條件 re-assert：這是平台存亡 invariant，不是可被手動翻掉的偏好。
        # （要吃回 wallet SSOT 是「移除本錨點」的重構，不是改 param。）
        self.ICP.set_param(BILLING_SSOT_PARAM, '1')

        self.Sub._ensure_billing_ssot_optout()

        self.assertEqual(self.ICP.get_param(BILLING_SSOT_PARAM), '0')


class TestGuardWiring(_GuardFixture):
    """接線驗證：install（post_init_hook）／upgrade（data ``<function>``）任一
    路都應把 opt-out param 落地。本測試不呼叫 method——跑在 install/upgrade
    過的 DB 上直接驗結果面，method 存在但沒接線時這裡紅。"""

    def test_optout_param_seeded_after_install(self):
        self.assertEqual(
            self.ICP.get_param(BILLING_SSOT_PARAM), '0',
            "install/upgrade 後 billing_ssot 應已被平台接線設為 opt-out（#778）；"
            "紅＝method 存在但 data <function>／post_init_hook 沒接",
        )


class TestBaseCronCanary(_GuardFixture):
    """Canary：install/upgrade 完成後 base cron 必須是 active。

    在上游 #14 合併前這條恆綠（cron 沒人動）；合併後的測試 DB 會經歷
    「wallet post_init 關掉 → 平台 post_init 開回」，此時它才有鑑別力——
    平台 hook 沒接好就紅。留著它，別因「現在必綠」而刪。
    """

    def test_base_auto_invoice_cron_active_after_install(self):
        self.assertTrue(
            self.cron.active,
            "平台裝完後 base auto-invoice cron 必須 active，"
            "否則平台唯一計費路徑無人觸發（#778 對撞的直接症狀）",
        )

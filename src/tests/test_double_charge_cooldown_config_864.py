"""#864 PR-E — double-charge 冷卻閘窗口的 config resolver + fail-safe。

比照 PR-C 的 `_woow_manual_aging_days` 模式：`_double_charge_cooldown_hours`
缺值／非整數／`<=0` 全部 fail-safe 回內建 24（Eugene 裁決 2026-09-01，
comment 11215）。同一份 fail-safe 慣例：⛔ 沒有 disable 逃生門——這是防
double-charge 的安全機制，跟 `billing_silence_alert_days` 那種「'0' 停用」
的通知節流語意不同。

`_double_charge_cooldown_active` 本身的「真讀到 incident」行為交給
`test_double_charge_cooldown_gate_864.py`（走 BaseCase／真 registry cursor，
證明 RR 快照盲區與 partner-only 判準）；本檔只驗 resolver 的四個 fail-safe
分支 + 卸載清理登記。
"""
from odoo.tests.common import TransactionCase


class TestDoubleChargeCooldownHoursResolver864(TransactionCase):

    PARAM = 'woow_paas_platform.double_charge_cooldown_hours'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Sub = cls.env['sh.subscription.subscription']
        cls.ICP = cls.env['ir.config_parameter']

    def test_default_is_24_hours(self):
        """缺值＝內建預設，Eugene 裁決（comment 11215）：24。"""
        self.ICP.sudo().search([('key', '=', self.PARAM)]).unlink()
        self.assertEqual(self.Sub._double_charge_cooldown_hours(), 24)

    def test_valid_positive_value_is_honoured(self):
        self.ICP.sudo().set_param(self.PARAM, '6')
        self.assertEqual(self.Sub._double_charge_cooldown_hours(), 6)

    def test_invalid_value_falls_back_to_24(self):
        self.ICP.sudo().set_param(self.PARAM, 'not-a-number')
        self.assertEqual(self.Sub._double_charge_cooldown_hours(), 24)

    def test_zero_falls_back_to_24_not_disabled(self):
        """🔴 `'0'` 不是 disable，是 fail-safe 回 24——這是防重複扣款的安全機制。"""
        self.ICP.sudo().set_param(self.PARAM, '0')
        self.assertEqual(self.Sub._double_charge_cooldown_hours(), 24)

    def test_negative_falls_back_to_24(self):
        self.ICP.sudo().set_param(self.PARAM, '-5')
        self.assertEqual(self.Sub._double_charge_cooldown_hours(), 24)

    def test_missing_param_falls_back_to_24(self):
        self.ICP.sudo().search([('key', '=', self.PARAM)]).unlink()
        self.assertEqual(self.Sub._double_charge_cooldown_hours(), 24)


class TestDoubleChargeCooldownParamIsRegisteredForUninstall864(TransactionCase):
    """新 config_parameter 必須進 ``hooks.py`` 的 ``_CONFIG_PARAMETERS``。

    Odoo 卸載 addon 不會自動清 ``ir.config_parameter``；這條寫法走「模組常數
    ＋ ``sudo().get_param()``」（同 PR-C ``manual_invoice_aging_days`` 先例），
    repo-lint 的 ``check_config_parameter_uninstall`` 只掃 ``config_parameter=``
    字面 kwarg，對這種寫法是盲區——這條測試補上。
    """

    def test_param_is_in_config_parameters_list(self):
        from ..hooks import _CONFIG_PARAMETERS

        self.assertIn(
            'woow_paas_platform.double_charge_cooldown_hours',
            _CONFIG_PARAMETERS,
            'double_charge_cooldown_hours 沒進 hooks.py 的 _CONFIG_PARAMETERS '
            '⇒ 卸載後留孤兒紀錄，而 repo-lint 那條 gate 對這種寫法是盲區、'
            '不會替你抓',
        )

"""AI 模型設定的寫入守衛與變更稽核（DESIGN §2.11，〔REV〕15；LLM-01b、LLM-02）。

``newapi_proxy_url`` 決定 PaaS 把簽過章的請求（含 UP 與開戶密碼）送到哪裡：寫成外部主機
就等於把憑證交出去。所以只接受固定的叢集內代理位址與 neutralize 的黑洞值，其他值在
``ir.config_parameter`` 的 ORM 寫入當下就拒絕（``set_param`` 走 create／write）；
``llm_public_base_url``（顯示給使用者的 API 網址）同樣只收兩個白名單值。SQL 直改擋不住，但
``llm_common.llm_config`` 讀取時會再驗一次、不合法就當「未設定」／不顯示。

**變更稽核**（LLM-02）：``newapi_proxy_url``、``llm_public_base_url``、``newapi_username_prefix``
（``llm_common.CONFIG_CHANGE_CODES``）的值真的變了（含 ``set_param(key, False)`` 的刪除）⇒ 這個交易
**commit 之後**以獨立 cursor 寫一筆 ``llm_event(config_change)``（uid、對象＝那一列設定、錯誤碼＝哪一個
key；不記值）、更新 ``llm_runtime_state.last_config_fingerprint``，並告警 ``config_changed``（未設定時
告警照 N27 不發，事件與指紋照寫）。commit 之後才寫：rollback 的交易（含所有測試）什麼都不留。

只看本分頁的 key（``llm_common.LLM_CONFIG_PARAMETERS``），其他設定的行為完全不變。
"""
import logging
import traceback

from odoo import api, models

from ..services import llm_redact
from .llm_common import (
    CONFIG_CHANGE_CODES,
    LLM_CONFIG_PARAMETERS,
    config_fingerprint,
    validate_llm_param,
)

_logger = logging.getLogger(__name__)


def _norm(value):
    return (value or '').strip() if isinstance(value, str) or value is None else str(value)


class IrConfigParameter(models.Model):
    _inherit = 'ir.config_parameter'

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('key') in LLM_CONFIG_PARAMETERS:
                validate_llm_param(vals.get('key'), vals.get('value'))
        records = super().create(vals_list)
        for record in records:
            if record.key in CONFIG_CHANGE_CODES and _norm(record.value):
                self._llm_config_changed(record.key, record.id)
        return records

    def write(self, vals):
        changed = []
        if 'key' in vals or 'value' in vals:
            for record in self:
                key = vals.get('key', record.key)
                if key in LLM_CONFIG_PARAMETERS:
                    validate_llm_param(key, vals.get('value', record.value))
                new_value = _norm(vals.get('value', record.value))
                if key in CONFIG_CHANGE_CODES and (
                        key != record.key or new_value != _norm(record.value)):
                    changed.append((key, record.id))
                if record.key in CONFIG_CHANGE_CODES and key != record.key:
                    changed.append((record.key, record.id))
        result = super().write(vals)
        for key, record_id in changed:
            self._llm_config_changed(key, record_id)
        return result

    def unlink(self):
        removed = [(record.key, record.id) for record in self
                   if record.key in CONFIG_CHANGE_CODES and _norm(record.value)]
        result = super().unlink()
        for key, record_id in removed:
            self._llm_config_changed(key, record_id)
        return result

    def _llm_config_changed(self, key, record_id):
        """commit 之後：稽核事件＋設定指紋＋告警（見模組 docstring）。盡力而為、不擋住設定本身。"""
        from .llm_event import EVENT_MODEL, LlmAlert, _env_on
        code = CONFIG_CHANGE_CODES[key]
        uid = self.env.uid
        registry = self.env.registry

        def _after_commit():
            try:
                with registry.cursor() as cr:
                    env = _env_on(registry, cr)
                    env[EVENT_MODEL]._llm_record(
                        'config_change', uid=uid if uid and uid > 0 else None,
                        target_model='ir.config_parameter', target_id=record_id,
                        error_code=code, severity='warning')
                    env['woow_paas_platform.llm_runtime_state']._llm_write(
                        last_config_fingerprint=config_fingerprint(env))
                    env[EVENT_MODEL]._llm_alert(
                        LlmAlert.CONFIG_CHANGED, target_model='ir.config_parameter',
                        target_id=record_id, error_code=code)
            except Exception:  # noqa: BLE001 - the setting itself is already committed
                _logger.error('[LLM] config change %s not recorded: %s', code,
                              llm_redact.redact(traceback.format_exc()))

        self.env.cr.postcommit.add(_after_commit)

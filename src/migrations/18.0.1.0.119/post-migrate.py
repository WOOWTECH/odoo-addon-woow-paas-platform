"""18.0.1.0.119 — #556 workspace_access 授權重構：退役兩條脆弱 rule。

舊的 workspace_access_rule_member_access（perm_write=True、domain 無 role 條件）
與 workspace_access_rule_create（domain [(1,'=',1)]、perm_create=True）住在
ir_rules.xml 的 <data noupdate="1"> 區塊。noupdate 是 sticky 的：
  - odoo/models.py::_load_records — `if not (update and d_noupdate)` ⇒ -u 不重寫欄位
  - ir_model_data::_build_update_xmlids_query — ON CONFLICT DO UPDATE 的 SET
    清單不含 noupdate ⇒ 把 <record> 搬到 noupdate="0" 區塊也翻不了旗標
  - ir_model_data::_process_end — WHERE COALESCE(noupdate,false) != true
    ⇒ 從 XML 刪掉 <record> 也不會自動清除
三條合起來：不下這支 migration，舊 rule 會在既有安裝上永久存活，並與新的
四條 rule 以 OR 語意併存 ⇒ 修復在 -u 後靜默失效，而 fresh install 是綠的。
"""
import logging

_logger = logging.getLogger(__name__)

_RETIRED = ('workspace_access_rule_member_access', 'workspace_access_rule_create')
_NEW = (
    'workspace_access_rule_member_read',
    'workspace_access_rule_owner_admin_manage',
    'workspace_access_rule_self',
    'workspace_access_rule_admin_full',
)


def migrate(cr, version):
    if not version:
        return

    # Step 1 — 退役舊 rule。兩個 DELETE，順序不可反：ir_model_data 不 cascade 到
    # ir_rule，先刪 rule 列、再刪 xmlid 指標（同 18.0.1.0.24 step 5）。
    cr.execute(
        """
        DELETE FROM ir_rule
        WHERE id IN (
            SELECT res_id FROM ir_model_data
            WHERE module = 'woow_paas_platform' AND model = 'ir.rule' AND name IN %s
        )
        """, (_RETIRED,))
    retired_rules = cr.rowcount
    cr.execute(
        """
        DELETE FROM ir_model_data
        WHERE module = 'woow_paas_platform' AND model = 'ir.rule' AND name IN %s
        """, (_RETIRED,))
    _logger.info("#556 retired legacy workspace_access rules: %s ir_rule + %s ir_model_data rows",
                 retired_rules, cr.rowcount)

    # Step 2 — 把四條新 rule 的 xmlid 旗標正規化成 noupdate=false（它們宣告在
    # noupdate="0" 區塊、INSERT 當下就是 false；這步是防禦性冪等，同 .24 step 4）。
    cr.execute(
        """
        UPDATE ir_model_data SET noupdate = false
        WHERE module = 'woow_paas_platform' AND model = 'ir.rule' AND name IN %s
        """, (_NEW,))
    _logger.info("#556 cleared noupdate on new workspace_access rule xmlids (%s rows)", cr.rowcount)

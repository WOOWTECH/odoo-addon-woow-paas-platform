"""18.0.1.0.129 — #658 workspace 本體授權重構：退役兩條脆弱 rule。

🔴 為什麼是 129，而 develop 的歷史上看起來「留了 127 給它」？
（會看到 `git log` 上 develop 從 126 直接跳到 128、中間空著 127 而覺得奇怪的人，
答案在這裡——不要把它改回 127。）

本 migration 的版號在開發期間被推了三次，每次都是同一個機制在咬：Odoo 只執行
`installed_version < 目錄版號 <= manifest 版號` 這個視窗內的 migration
（`odoo/modules/migration.py`），所以目錄版號**必須嚴格大於 develop 當下的實值**。

  - 最初取 126（當時 develop 是 125）。
  - 發現 PR #673 的 manifest 也是 126 且**不帶 migration** ⇒ 它先合併的話 develop 變
    126，已升級環境 `installed=126`，`126 < 126` 為假 ⇒ 本 migration 永不執行。改 127。
    ⚠ 兩個 PR 把同一行改成**同一個值**時 git 不報衝突，這種撞號沒有任何訊號。
  - `ab41d91b`（#679）把 develop 從 126 **直接跳到 128**，develop 從未發布過 127。
    那個跳號很可能就是為了留 127 給本 PR——但**保留已經失效**：任何吃到 128 的環境
    `installed=128`，`128 < 127` 為假 ⇒ 127 一樣永不執行。保留一個版號需要全世界配合
    等這張 PR 合併，而 develop 是共用分支，窗口在 #678／#680 合併時就關掉了。
    ⇒ 最終取 129（嚴格大於 develop 當下的 128）。

⚠ 教訓寫在這裡供下一個人參考：**不要靠「幫你留一個版號」，要在合併的當下重查**
develop 的 manifest 實值。若日後又需要改號，同時要動的有 10 個檔案（manifest、本目錄名、
本 docstring、`src/security/ir_rules.xml` 兩處註解、`src/tests/__init__.py`、
`src/tests/test_workspace_authz_658.py` 檔頭、`scripts/audit/repo-lint.py` 的
`RETIRED_RULE_XMLIDS` 值、docs 三份），漏一個都不會有錯誤訊息；
驗法：`git grep -ln "18\\.0\\.1\\.0\\.1[0-9][0-9]"` 只該剩新版號。

舊的 workspace_rule_member_access（perm_read + perm_write 合一、domain
`[('access_ids.user_id', '=', user.id)]`、**無 role 條件**）與
workspace_rule_create（domain `[(1,'=',1)]`、perm_create=True）住在
ir_rules.xml 的 <data noupdate="1"> 區塊。noupdate 是 sticky 的：
  - odoo/models.py::_load_records — `if not (update and d_noupdate)` ⇒ -u 不重寫欄位
  - ir_model_data::_build_update_xmlids_query — ON CONFLICT DO UPDATE 的 SET
    清單不含 noupdate ⇒ 把 <record> 搬到 noupdate="0" 區塊也翻不了旗標
  - ir_model_data::_process_end — WHERE COALESCE(noupdate,false) != true
    ⇒ 從 XML 刪掉 <record> 也不會自動清除
三條合起來：不下這支 migration，舊 rule 會在既有安裝上永久存活，並與新的
四條 rule 以 OR 語意併存 ⇒ 收窄在 -u 後靜默失效，而 fresh install 是綠的。
（形態逐字沿用 #556 的 18.0.1.0.122/post-migrate.py，那條路徑已驗過。）

🔴 為什麼新規則必須用**新 xmlid**、不能同名搬區塊：post-migrate 跑在 XML 資料
載入**之後**。同名搬區塊的順序是「XML load 先跑 → 撞 sticky → 跳過 → 然後
post-migrate DELETE 掉」⇒ 那條 rule 在升級後直接消失、不會被重建，等於把
授權規則整條刪掉。所以走 #556 驗證過的路：舊名 DELETE 退役、新規則用新
xmlid 宣告在 <data noupdate="0"> 區塊。
"""
import logging

_logger = logging.getLogger(__name__)

_RETIRED = ('workspace_rule_member_access', 'workspace_rule_create')
_NEW = (
    'workspace_rule_member_read',
    'workspace_rule_owner_admin_manage',
    'workspace_rule_anyone_create',
    'workspace_rule_owner_unlink',
)


def migrate(cr, version):
    if not version:
        return

    # Step 1 — 退役舊 rule。兩個 DELETE，順序不可反：ir_model_data 不 cascade 到
    # ir_rule，先刪 rule 列、再刪 xmlid 指標（同 18.0.1.0.122 step 1）。
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
    _logger.info("#658 retired legacy workspace rules: %s ir_rule + %s ir_model_data rows",
                 retired_rules, cr.rowcount)

    # Step 2 — 把四條新 rule 的 xmlid 旗標正規化成 noupdate=false（它們宣告在
    # noupdate="0" 區塊、INSERT 當下就是 false；這步是防禦性冪等，同 .122 step 2）。
    cr.execute(
        """
        UPDATE ir_model_data SET noupdate = false
        WHERE module = 'woow_paas_platform' AND model = 'ir.rule' AND name IN %s
        """, (_NEW,))
    _logger.info("#658 cleared noupdate on new workspace rule xmlids (%s rows)", cr.rowcount)

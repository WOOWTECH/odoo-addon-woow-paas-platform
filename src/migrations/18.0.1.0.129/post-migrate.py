"""18.0.1.0.129 — #658 workspace 本體授權重構：退役兩條脆弱 rule ＋ #677 ws_hash 回填。

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
develop 的 manifest 實值。若日後又需要改號，載體遠不只 manifest 與本目錄名：版號
字面散在 `src/security/ir_rules.xml` 的註解、`src/tests/` 數個檔、`scripts/audit/`
數個檔、docs 數份…，漏一個都不會有任何錯誤訊息。

⚠ **這裡刻意不寫「共 N 個檔」**——那是個會單調變動的純量，寫下就會漂（本行原本
寫「10 個檔案」並列舉其中 7 個；#675 覆核 F4 自檢時實測 `git grep -l` 是 **14** 個，
naming.py／test_repo_lint_security_rules.py／test_workspace_ns_hash_677.py 都不在
那份列舉裡）。要清單就現算：

    git grep -ln "18\\.0\\.1\\.0\\.129"        # 新版號的全部載體
    git grep -ln "18\\.0\\.1\\.0\\.<舊版號>"   # 必須**零命中**才算改完

（原本的驗法寫 `git grep -ln "18\\.0\\.1\\.0\\.1[0-9][0-9]"` 只該剩新版號，那也是
假的：`.122` / `.124` 等舊 migration 的版號本來就合法留在樹上，同一次實測是 20 個
檔、10 個不同版號。範圍要收在「這一支的舊版號」，不是整個 1xx 區間。）

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

────────────────────────────────────────────────────────────────────────────
Step 3（#677）—— 回填 `woow_paas_platform_workspace.ws_hash`

#677 把 K8s namespace／公開子網域的身分從「租戶決定的 `slug` 的 `md5[:8]`」改成
一個 stored、伺服器產生、DB `UNIQUE` 的 `ws_hash` 欄位。

🔴 **回填值必須逐字等於舊公式的結果 `md5(slug)[:8]`**，這是「改推導不會動到既有
部署」的依據——但機制**不是**「有些路徑會每次重新推導」。實查全部四個產名點
（`git grep make_namespace / make_service_subdomain / make_security_access_subdomain
/ make_smarthome_subdomain -- src`，排除 tests）：

  - `cloud_service.helm_namespace` / `subdomain`：在 `controllers/paas.py`、
    `controllers/ha_paas.py` 建立 service 的當下算一次、存進欄位；
  - `security_access.canonical_name`：`_generate_canonical_name()` **只**被
    `create()` 呼叫，算一次寫進 stored 欄位（之後由 write guard 鎖住）；
  - `smart_home.subdomain` / `tunnel_name`：`_generate_subdomain()` /
    `_generate_tunnel_name()` **只**被 provision 路徑呼叫，結果存進欄位，而該路徑
    對已有 `tunnel_id` 的列會先 raise。

⇒ **既有列一律不重推**。（原文寫那三支「是每次呼叫重新推導的」，並據此說用隨機值
回填會讓既有 SmartHome 子網域「整批改變」——與實況相反：它們是不讀 stored 值的
推導函式沒錯，但呼叫點只在 create／provision。#675 覆核 F4 自檢時抓到。）

真正需要回填成舊值的是**下一次推導**：一個既有 workspace 在升級後新建 CloudService
時，`helm_namespace` 是拿當下的 `ws_hash` 現算的 ⇒ 隨機值會讓新服務落進
`paas-ws-{新值}`，與同一個 workspace 既有服務所在的 `paas-ws-{md5(slug)[:8]}`
**分家**（既有服務不會動，但那個 workspace 從此橫跨兩個 namespace）。回填成舊值
之後那條路徑算出來的字串與修復前 byte-identical。

`slug` 為 NULL 的列用 `''` 參與雜湊（＝ `md5('')[:8]` = `d41d8cd9`），與修復前
`generate_ws_hash(slug)` 對空字串的行為一致。
⚠ **`''` 只會有一列（`UNIQUE(slug)` 擋得住字面重複），但 NULL 不受 `UNIQUE` 限制**
——Postgres 的 UNIQUE 允許任意多列 NULL。實測（直接對 `unique_slug` 這條 constraint
下 INSERT，非經 ORM）：兩列 `slug=NULL` → `INSERT 0 2`；對照組兩列 `slug=''` →
`duplicate key value violates unique constraint "..._unique_slug"`。
所以「空 slug 最多一列」是假的：`''` 那一列與任意多列 NULL 會**一起**落在
`d41d8cd9` 上並觸發下面那個 abort。行為是對的（fail-closed），錯的只有理由——
原文寫「`UNIQUE(slug)` 只保證一列這種資料」，那句話會讓日後撞到這個 abort 的人
跑去找「為什麼有兩列 NULL，UNIQUE 不是擋著嗎」（#675 覆核 F4）。今天 `create()`
兩個版本都把 slug 填成 `''` 而非 NULL，所以多列 NULL 不是可達路徑。

⚠ **既有資料就已經碰撞的話，這支 migration 會 raise 而不是自作主張改名。** 那是
事故（兩個租戶已經共用一個 K8s namespace）不是 bug，處置要人來決定；靜默挑一列
改名會讓那一列的既有部署當場失聯。訊息裡直接列出撞在一起的 workspace id。
────────────────────────────────────────────────────────────────────────────
"""
import hashlib
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

    _backfill_ws_hash(cr)


def _backfill_ws_hash(cr):
    """Step 3（#677）—— 見檔頭。回填 `ws_hash = md5(slug)[:8]`，碰撞則 raise。

    只回填 `ws_hash IS NULL` 的列 ⇒ 冪等，重跑不會覆寫任何已經有值的列
    （包含後來新建、拿到隨機值的 workspace）。
    """
    cr.execute(
        "SELECT id, COALESCE(slug, '') FROM woow_paas_platform_workspace "
        "WHERE ws_hash IS NULL ORDER BY id")
    rows = cr.fetchall()
    if not rows:
        _logger.info("#677 ws_hash backfill: nothing to do")
        return

    by_hash = {}
    for ws_id, slug in rows:
        legacy = hashlib.md5(slug.encode()).hexdigest()[:8]
        by_hash.setdefault(legacy, []).append(ws_id)

    # 既有列之間互撞。
    clashes = {h: ids for h, ids in by_hash.items() if len(ids) > 1}
    # 與已經有值的列相撞（重跑、或人工修過資料的情況）。
    cr.execute(
        "SELECT ws_hash, id FROM woow_paas_platform_workspace "
        "WHERE ws_hash IN %s", (tuple(by_hash),))
    for taken_hash, taken_id in cr.fetchall():
        clashes.setdefault(taken_hash, list(by_hash[taken_hash])).append(taken_id)

    if clashes:
        raise ValueError(
            "#677 ws_hash backfill aborted: %s legacy namespace hash(es) are "
            "already shared by more than one workspace — these tenants are "
            "ALREADY colliding in Kubernetes today: %s. This is an incident, not "
            "a migration problem: renaming either side would silently orphan its "
            "deployed Helm releases / Cloudflare records, so it needs a human "
            "decision. Resolve the collisions, then re-run the upgrade."
            % (len(clashes),
               '; '.join('%s -> workspace ids %s' % (h, sorted(ids))
                         for h, ids in sorted(clashes.items()))))

    cr.execute(
        """
        UPDATE woow_paas_platform_workspace
        SET ws_hash = SUBSTRING(MD5(COALESCE(slug, '')) FROM 1 FOR 8)
        WHERE ws_hash IS NULL
        """)
    _logger.info(
        "#677 ws_hash backfill: %s workspace row(s) stamped with their legacy "
        "md5(slug)[:8] (byte-identical to the pre-fix derivation)", cr.rowcount)

"""#784：待回收帳本的 ``cf_policy_id`` 欄位改名為 ``cf_object_id``。

## 為什麼一定要有這一支（不是「順手改個名字」）

帳本（#528，`cf_access_policy_reap`）從本版起也收 **Access application id**
（新增 ``kind`` 欄位區分）。欄位名如果留著叫 ``cf_policy_id``，任何漏掉 ``kind``
條件的查詢都會把一顆 app id 餵進 operator 的 ``existing_policy_ids``——CF 會拿它
去 DELETE 一顆不存在的 policy 並回 404，而 404 在 operator 端**視為成功**
⇒ 帳被消掉、app 永遠留在 CF，全程零訊號。改名是為了讓那個誤用寫不出來。

## 為什麼是 pre-migrate 的 SQL RENAME 而不是讓 ORM 自己長出新欄位

ORM 不會刪舊欄位：它只會 ADD 一個 ``cf_object_id``，而舊的 ``cf_policy_id`` 帶著
``NOT NULL`` 留在表上、又不再有任何 field 會填它 ⇒ **之後每一筆 INSERT 都會炸**。
所以這裡必須先把欄位搬過去（資料一併帶走），ORM 再接手。

三個分支都要處理，因為升級可能從任何一個狀態進來（含 dev sandbox 反覆升降版）：
舊欄位在 → 改名；兩個都在（半途）→ 只補資料不動結構；都不在 → 這個 DB 還沒建過
這張表，ORM 會自己建，什麼都不用做。
"""
import logging

_logger = logging.getLogger(__name__)

_TABLE = 'woow_paas_platform_cf_access_policy_reap'


def _has_column(cr, table, column):
    cr.execute("""
        SELECT 1 FROM information_schema.columns
         WHERE table_name = %s AND column_name = %s
    """, (table, column))
    return bool(cr.fetchone())


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = %s",
        (_TABLE,))
    if not cr.fetchone():
        return  # 表還不存在（#528 之前的 DB）：ORM 會直接建成新結構
    old = _has_column(cr, _TABLE, 'cf_policy_id')
    new = _has_column(cr, _TABLE, 'cf_object_id')
    if old and not new:
        cr.execute(
            'ALTER TABLE %s RENAME COLUMN cf_policy_id TO cf_object_id' % _TABLE)
        _logger.info("[#784] %s.cf_policy_id renamed to cf_object_id", _TABLE)
    elif old and new:
        # 半途狀態：把還沒搬過去的資料補齊，再把舊欄位放掉（NOT NULL 的殭屍
        # 欄位會讓之後所有 INSERT 失敗）。
        cr.execute(
            'UPDATE %s SET cf_object_id = cf_policy_id '
            ' WHERE cf_object_id IS NULL' % _TABLE)
        cr.execute('ALTER TABLE %s DROP COLUMN cf_policy_id' % _TABLE)
        _logger.info("[#784] %s: merged legacy cf_policy_id into cf_object_id",
                     _TABLE)

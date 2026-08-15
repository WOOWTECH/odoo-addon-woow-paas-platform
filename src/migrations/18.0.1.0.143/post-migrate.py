"""#744：policy 錨點 exclusive arc 落地後的驗證（不搬資料，必要時補建 CHECK）。

## 為什麼不搬資料

`route_id` 由 `required=True` 改成可空、`cloud_service_id` 新增、SQL CHECK
`anchor_arc` 新增——這三件事 Odoo 的 `_auto_init` 都會自己做完，既有資料
**一列都不用動**：升級前 `route_id` 是 NOT NULL，所以每一列都恰好帶一個錨點，
新 CHECK 對它們全部成立。這支的工作是**證明那三件事真的發生了**，因為每一件
失敗時都是靜默的：

1. **CHECK 沒建起來 ⇒ 只會留一行 warning。**
   `_add_sql_constraints` 走 `registry.post_constraint`，第一次失敗就把它排進
   `_constraint_queue`；`finalize_constraints()` 在整個 registry cycle 結束時
   重試，再失敗只 `_schema.warning(...)`（`odoo/modules/registry.py ::
   Registry.finalize_constraints`）。升級照樣「成功」，而 arc 從此只剩 Python
   `@api.constrains` 這半層——擋不住裸 SQL、擋不住批次 migration。
   ⚠ 本檔的處置是**補建**而不是直接 raise，因為「沒建起來」有一種**正常**的
   成因：從 P2.5 之前一路升上來時，`_auto_init` 的當下 legacy policy 兩個錨點
   都還是空的（`route_id` 由 `18.0.1.0.37` 的 post-migrate 才填），constraint
   本來就加不上、被排進 queue，而 `finalize_constraints()` 晚於本檔
   （post-migrate 在 `load_module_graph` 內、finalize 在 `load_modules` 外，
   見 `odoo/modules/loading.py`）。那個中間態與「真的加失敗」在這裡不可區分，
   所以先驗「沒有違規列」再補建：補得起來就兩種情況一起收斂，補不起來才是真
   故障。

2. **NOT NULL 沒被拿掉 ⇒ 症狀出現在很久以後。**
   `Field.update_db_notnull`（`odoo/fields.py`）在 `required` 變 False 時會
   `drop_not_null`。萬一沒發生，DB 照舊跑、既有功能全綠，只有「第一筆 CS 錨定
   的 policy」會炸——而那是 T3/T4 才會發生的事，屆時沒有人會回頭懷疑 T2 的
   升級。

3. **既有列真的都合法**：這是本票對「零影響」的正面舉證，不是推論。

三項任一不成立就 raise：升級停在一個講得清楚的地方，比帶著半套守線繼續跑好。

## 驗不到什麼

本檢查證明「schema 就位」，證明不了「ir.rule 的 dual-leg domain 有生效」——
那是 `ir_rules.xml`（`noupdate="0"`）自己的更新路徑，且有測試守（見
`src/tests/test_cs_access_acl_744.py`，走真 HTTP RPC）。
"""
import logging

from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_TABLE = 'woow_paas_platform_security_access_access_policy'
#: Odoo 的 constraint 實際名字＝`{table}_{key}`；`anchor_arc` 讓全名 59 字元，
#: 沒有超過 63 ⇒ 不會被 `sql.make_identifier` 換成雜湊名，可以直接查。
#: 改動 model 上的 constraint key 時，這個常數要一起改（不然驗證會恆假）。
_CONSTRAINT = '%s_anchor_arc' % _TABLE
#: 與 model 上 `_sql_constraints` 的定義**必須逐字一致**（補建路徑會用它）。
_CHECK_DEF = 'CHECK((route_id IS NULL) <> (cloud_service_id IS NULL))'


def migrate(cr, version):
    if not version:
        return

    cr.execute("SELECT to_regclass(%s)", (_TABLE,))
    if not cr.fetchone()[0]:
        _logger.info("[744] %s 尚未建表，略過 arc 驗證", _TABLE)
        return

    problems = []

    # 1) 既有列都恰好帶一個錨點——**必須先驗這一項**：它同時是下一步「補建
    #    constraint」的前提，順序反過來的話補建會被違規列擋下，而拿到的錯誤
    #    訊息是 PG 的 CheckViolation（說不出是哪幾列）。
    cr.execute(
        """
        SELECT count(*)
          FROM %s
         WHERE (route_id IS NULL) = (cloud_service_id IS NULL)
        """ % _TABLE
    )
    bad = cr.fetchone()[0]
    if bad:
        problems.append(
            "有 %d 列不滿足 exclusive arc（兩個錨點皆空或皆填）。" % bad)

    # 2) CHECK constraint 真的在 pg_constraint 裡；不在就**自己補建**。
    #
    # 為什麼是補建而不是直接 raise：從 **P2.5 之前**的版本一路升上來時，
    # `_auto_init` 跑在所有 post-migrate 之前，那當下 legacy policy 的兩個錨點
    # 都還是空的（route_id 由 `18.0.1.0.37` 的 post-migrate 才填），constraint
    # 加不上 ⇒ 被排進 `Registry._constraint_queue`，要等整個 registry cycle 結束
    # 的 `finalize_constraints()` 才補。那一刻**晚於**本檔，所以此處查不到
    # constraint 是那條路徑的正常中間態，不是故障。
    #
    # 但「正常中間態」與「真的加失敗」在這裡長得一模一樣，而後者
    # `finalize_constraints()` 只會留一行 warning 就放過（`odoo/modules/
    # registry.py`）。既然上一步已經證明沒有任何違規列，補建必然成功——補得起來
    # 就兩種情況一起收斂，補不起來才是真故障。
    if not bad:
        cr.execute(
            """
            SELECT 1
              FROM pg_constraint c
              JOIN pg_class t ON t.oid = c.conrelid
             WHERE t.relname = %s AND c.conname = %s AND c.contype = 'c'
            """,
            (_TABLE, _CONSTRAINT),
        )
        if not cr.fetchone():
            _logger.info(
                "[744] CHECK `%s` 尚未存在（多半是從 P2.5 前一路升上來、"
                "Odoo 把它排進 constraint queue）——就地補建", _CONSTRAINT)
            try:
                # 🔴 必須包 savepoint。上一步已經證明沒有違規列，所以「補建
                # 因為壞資料而失敗」不會發生——但**別的原因**會（lock timeout、
                # 權限、名稱衝突…）。任何 SQL 失敗都會把整個 transaction 打成
                # aborted 狀態，之後第 3 步查 route_id 可空性的那句 SELECT 會
                # 丟 `InFailedSqlTransaction`——一個**沒有人捕捉**的例外，它會
                # 蓋掉底下精心組出來的 UserError，把「講得清楚地停下來」換成
                # 一個看不出所以然的 psycopg2 錯誤。整支腳本的價值就在那句
                # 訊息上，不能讓它被自己的修復動作弄丟。
                #
                # `flush=False`：這裡沒有 ORM env 可 flush（migration 的 cursor
                # 不見得掛得到 registry），且 Odoo 自己在同一種操作上就是這樣
                # 用的（`odoo/tools/sql.py :: add_constraint`）。
                #
                # `Savepoint.__exit__` 只 rollback、**不吞例外**
                # （`odoo/sql_db.py :: Savepoint`），所以順序是：savepoint 先
                # 回捲把 transaction 救回乾淨狀態，再由下面的 except 接住轉成
                # 可讀訊息。兩者缺一不可。
                with cr.savepoint(flush=False):
                    cr.execute(
                        "ALTER TABLE %s ADD CONSTRAINT %s %s"
                        % (_TABLE, _CONSTRAINT, _CHECK_DEF))
            except Exception as exc:  # noqa: BLE001 — 轉成可讀訊息後 raise
                problems.append(
                    "SQL CHECK `%s` 不存在且補建失敗：%s" % (_CONSTRAINT, exc))

    # 3) route_id 的 NOT NULL 已經被拿掉（否則 CS 錨定的 policy 永遠建不出來）
    cr.execute(
        """
        SELECT is_nullable
          FROM information_schema.columns
         WHERE table_name = %s AND column_name = 'route_id'
        """,
        (_TABLE,),
    )
    row = cr.fetchone()
    if not row:
        problems.append("欄位 route_id 不存在（schema 與本 migration 不一致）。")
    elif row[0] != 'YES':
        problems.append(
            "route_id 仍是 NOT NULL。exclusive arc 的另一半（cloud_service_id "
            "錨定）因此永遠寫不進去，而既有功能會全部照常運作 ⇒ 這個失敗會一直"
            "潛伏到 T3/T4 才爆。")

    if problems:
        detail = '\n'.join('  - %s' % p for p in problems)
        _logger.error("[744] policy 錨點 exclusive arc 驗證失敗：\n%s", detail)
        raise UserError(
            '#744：Access policy 錨點 exclusive arc 未正確落地。\n\n%s\n\n'
            '這支 migration 不搬資料——失敗代表升級過程中有一段靜默失效了，'
            '請先排除再重跑升級。' % detail
        )

    cr.execute("SELECT count(*) FROM %s" % _TABLE)
    total = cr.fetchone()[0]
    _logger.info(
        "[744] exclusive arc 驗證通過：CHECK `%s` 已建立、route_id 可空、"
        "既有 %d 列全部恰好帶一個錨點", _CONSTRAINT, total)

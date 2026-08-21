"""#768：policy 錨點擴成三腿的驗證 ＋ SH primary route 回填。

三件事，每一件失敗時都是靜默的：

## 1) `anchor_arc` CHECK 必須換成「恰好一個」的三腿定義

`_auto_init` 在 `_sql_constraints` 的定義字串變動時會嘗試 drop + 重建，失敗只留
一行 `_schema.warning` 就繼續跑（`odoo/modules/registry.py ::
Registry.finalize_constraints`）⇒ 升級「成功」、Python 那層照常擋、所有行為測試
全綠，而 DB 那層停在**舊的二元定義**上：第一筆 SH 錨定的 policy 會被 PG 拒絕，
而那要等 T4 的前端接上去才會有人撞到。

🔴 **存在性驗證查的是 `pg_get_constraintdef` 而不是 `conname`**。#744 那支只查
名字（`WHERE conname = %s AND contype='c'`），而本次要換的正是**同名不同定義**的
constraint ⇒ 只查名字會讓舊定義原封不動地通過驗證。這一格是本檔與 144 唯一
的實質差異，也是唯一分得出「換好了」與「沒換」的判準。

## 2) 既有 policy 列在新謂詞下仍然全部合法

升級前每一列恰好帶一個錨點（`route_id` XOR `cloud_service_id`），新增的
`smart_home_route_id` 對它們全部是 NULL ⇒ `num_nonnulls(...) = 1` 對每一列成立。
這是正面舉證，不是推論——順序上也必須**先驗再補建**：反過來的話補建會被違規列
擋下，拿到的是 PG 的 CheckViolation（說不出是哪幾列）。

## 3) 既有 SmartHome 要回填它的 primary route

route 是 Access 的錨點載體。既有 SH 是在本功能之前 provision 的，沒有這一步，
它們的租戶就算方案帶席位也會看到一個空的 Access 分頁（後端沒有錨點可掛）。

- **來源是 `subdomain` 不是 `tunnel_route`**：後者是寫一次的 denormalised 快照，
  `dns_failed` 分支也照寫 ⇒ 可能存著一個 NXDOMAIN 的 URL
  （`models/smart_home.py::action_provision`、`test_named_failure_fields_484.py`）。
  而 route 的 hostname 是 **compute**（衍生自 `smart_home.subdomain`），所以這一步
  只建列、不搬字串。
- 只回填**已經 provision 過**（`subdomain IS NOT NULL`）的 SH：沒有 subdomain 的
  記錄還沒有對外 hostname，它的 route 會在 `action_provision` 成功時建立。
- 冪等：`WHERE NOT EXISTS` 掉已經有 primary 的 SH，重跑不會長出第二條。

## 為什麼用裸 SQL 建列而不是 ORM

`smart_home_route.create()` 會觸發 `_sync_access_to_operator()`。升級視窗內對
operator 發出一批真實 HTTP 呼叫是不能接受的（operator 不可達時整個升級會被
re-raise 打斷），而這些新列一律 `is_protected=False`、沒有 `access_app_id`，
sync 本來就是 no-op——用 SQL 建列讓「不呼叫 operator」成為結構事實而不是靠
一個 context flag 記得要帶。

## partial unique index

「一台 SH 至多一條 primary route」的**真正**守線（Python 的 `@api.constrains`
只負責訊息可讀，擋不住併發與裸 SQL）。

⚠ 它的**主要**建立點是 model 的 `SmartHomeRoute.init()`，不是這裡——只放在
migration 裡的話 fresh install 永遠不會有它。這裡再建一次是為了讓升級路徑上
「有沒有建起來」在本檔就地可驗（`IF NOT EXISTS` ⇒ 冪等）；順序上它必須跑在
回填之前，否則回填自己可能寫出兩列 primary 而 index 之後建不起來。
"""
import logging

from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_POLICY_TABLE = 'woow_paas_platform_security_access_access_policy'
#: Odoo 的 constraint 實際名字＝`{table}_{key}`；table 名 48 字元 ＋ `anchor_arc`
#: ＝ 59 字元，未超過 63 ⇒ 不會被 `sql.make_identifier` 換成雜湊名，可直接查。
#: ⚠ **不要改 constraint key 的名字**：取更長的 key 會越過 63 而被換成雜湊名，
#: 本檔與三個測試檔的存在性驗證從此恆假。
_CONSTRAINT = '%s_anchor_arc' % _POLICY_TABLE
#: 與 model 上 `_sql_constraints` 的定義**必須逐字一致**（補建路徑會用它）。
_CHECK_DEF = ('CHECK(num_nonnulls(route_id, cloud_service_id, '
              'smart_home_route_id) = 1)')
#: 判斷「現有 constraint 是不是新定義」的識別字。PG 會把定義正規化（大小寫、
#: 括號），所以比對的是**這幾個必要成分同時出現**，不是整串字面相等。
_CHECK_MARKERS = ('num_nonnulls', 'route_id', 'cloud_service_id',
                  'smart_home_route_id')

_SH_TABLE = 'woow_paas_platform_smart_home'
_ROUTE_TABLE = 'woow_paas_platform_smart_home_route'
_PRIMARY_INDEX = 'woow_paas_platform_smart_home_route_one_primary'


def _constraint_def(cr):
    cr.execute(
        """
        SELECT pg_get_constraintdef(c.oid)
          FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
         WHERE t.relname = %s AND c.conname = %s AND c.contype = 'c'
        """,
        (_POLICY_TABLE, _CONSTRAINT),
    )
    row = cr.fetchone()
    return row[0] if row else None


def _migrate_arc(cr, problems):
    cr.execute("SELECT to_regclass(%s)", (_POLICY_TABLE,))
    if not cr.fetchone()[0]:
        _logger.info("[768] %s 尚未建表，略過 arc 驗證", _POLICY_TABLE)
        return

    # 新欄位存在是後面每一步的前提
    cr.execute(
        """
        SELECT 1 FROM information_schema.columns
         WHERE table_name = %s AND column_name = 'smart_home_route_id'
        """,
        (_POLICY_TABLE,),
    )
    if not cr.fetchone():
        problems.append(
            "policy 表沒有 smart_home_route_id 欄位（schema 與本 migration "
            "不一致）——第三個錨點整條不存在。")
        return

    # 1) 既有列在**新**謂詞下都恰好帶一個錨點。先驗再補建（順序是設計的一部分）。
    cr.execute(
        """
        SELECT count(*) FROM %s
         WHERE num_nonnulls(route_id, cloud_service_id, smart_home_route_id) <> 1
        """ % _POLICY_TABLE
    )
    bad = cr.fetchone()[0]
    if bad:
        problems.append(
            "有 %d 列不滿足「恰好一個錨點」（全空或多於一個）。" % bad)
        return

    # 2) CHECK 必須是**新定義**。查定義而不是查名字——同名舊定義會通過只查名字
    #    的驗證，而那正是本次要換掉的東西。
    definition = _constraint_def(cr)
    if definition and all(m in definition for m in _CHECK_MARKERS):
        return

    if definition:
        _logger.info(
            "[768] CHECK `%s` 仍是舊定義（%s）——drop 後以三腿定義重建",
            _CONSTRAINT, definition)
    else:
        _logger.info(
            "[768] CHECK `%s` 尚未存在（多半被 Odoo 排進 constraint queue）"
            "——就地補建", _CONSTRAINT)

    try:
        # 🔴 必須包 savepoint：上一步已證明沒有違規列，所以「因壞資料失敗」不會
        # 發生，但別的原因會（lock timeout、權限…）。任何 SQL 失敗會把 transaction
        # 打成 aborted，之後的 SELECT 會丟 `InFailedSqlTransaction`——一個沒有人
        # 捕捉的例外，會蓋掉底下精心組出來的 UserError。`Savepoint.__exit__` 只
        # rollback 不吞例外，所以 savepoint ＋ except 兩者缺一不可。
        with cr.savepoint(flush=False):
            cr.execute(
                "ALTER TABLE %s DROP CONSTRAINT IF EXISTS %s"
                % (_POLICY_TABLE, _CONSTRAINT))
            cr.execute(
                "ALTER TABLE %s ADD CONSTRAINT %s %s"
                % (_POLICY_TABLE, _CONSTRAINT, _CHECK_DEF))
    except Exception as exc:  # noqa: BLE001 — 轉成可讀訊息後 raise
        problems.append(
            "SQL CHECK `%s` 的三腿定義補建失敗：%s" % (_CONSTRAINT, exc))
        return

    definition = _constraint_def(cr)
    if not definition or not all(m in definition for m in _CHECK_MARKERS):
        problems.append(
            "SQL CHECK `%s` 補建後仍不是三腿定義（實際：%r）。"
            % (_CONSTRAINT, definition))


def _backfill_primary_routes(cr, problems):
    cr.execute("SELECT to_regclass(%s)", (_ROUTE_TABLE,))
    if not cr.fetchone()[0]:
        problems.append(
            "%s 尚未建表——SH 的 Access 錨點載體整個不存在。" % _ROUTE_TABLE)
        return

    try:
        with cr.savepoint(flush=False):
            cr.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS %s
                    ON %s (smart_home_id) WHERE is_primary
                """ % (_PRIMARY_INDEX, _ROUTE_TABLE))
    except Exception as exc:  # noqa: BLE001
        problems.append(
            "primary route 的 partial unique index 建立失敗：%s" % exc)
        return

    cr.execute(
        """
        INSERT INTO %(route)s (smart_home_id, is_primary, is_protected,
                               session_duration, login_google, login_email_otp,
                               create_uid, create_date, write_uid, write_date)
        SELECT sh.id, TRUE, FALSE, '24h', FALSE, FALSE,
               1, now() AT TIME ZONE 'UTC', 1, now() AT TIME ZONE 'UTC'
          FROM %(sh)s sh
         WHERE sh.subdomain IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM %(route)s r
                WHERE r.smart_home_id = sh.id AND r.is_primary)
        """ % {'route': _ROUTE_TABLE, 'sh': _SH_TABLE}
    )
    created = cr.rowcount

    cr.execute(
        """
        SELECT count(*) FROM %(sh)s sh
         WHERE sh.subdomain IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM %(route)s r
                WHERE r.smart_home_id = sh.id AND r.is_primary)
        """ % {'route': _ROUTE_TABLE, 'sh': _SH_TABLE}
    )
    missing = cr.fetchone()[0]
    if missing:
        problems.append(
            "回填後仍有 %d 台已 provision 的 Smart Home 沒有 primary route。"
            % missing)
        return

    _logger.info(
        "[768] primary route 回填完成：新建 %d 列（已 provision 的 SH 全部覆蓋）",
        created)


def migrate(cr, version):
    if not version:
        return

    # pre-migrate 在 cursor 上設了這支旗標，讓新增的 stored related 對全表重算時
    # 不去重跑既有列的配額檢查（理由見 pre-migrate.py）。載入已經結束，這裡清掉
    # ——不清的話同一個 transaction 之後的任何寫入都會少一層守衛。
    if getattr(cr, 'sa_skip_policy_quota', False):
        cr.sa_skip_policy_quota = False
        _logger.info("[768] 已清除 sa_skip_policy_quota")

    problems = []
    _migrate_arc(cr, problems)
    if not problems:
        _backfill_primary_routes(cr, problems)

    if problems:
        detail = '\n'.join('  - %s' % p for p in problems)
        _logger.error("[768] SH Access 錨點擴充驗證失敗：\n%s", detail)
        raise UserError(
            '#768：SH 的 Access 錨點（policy 三腿 arc ＋ smart_home_route）未'
            '正確落地。\n\n%s\n\n這支 migration 只建 route 列、不搬任何字串——'
            '失敗代表升級過程中有一段靜默失效了，請先排除再重跑升級。' % detail
        )

    _logger.info(
        "[768] 三腿 arc 驗證通過：CHECK `%s` 為 num_nonnulls 形式、既有 policy "
        "列全部恰好帶一個錨點、primary route 已回填", _CONSTRAINT)

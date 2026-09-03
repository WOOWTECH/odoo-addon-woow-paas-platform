"""#993：把 `woow_billing_incident.trade_no` 裡冒充 provider 單號的我方 reference 清成 NULL。

## 為什麼一定要有這一支（不是「順手清資料」）

`trade_no` 這一欄從 PR-D 建立起就**沒有語意宣告**（`fields.Char(index=True)`，
無 help、無 UNIQUE），於是兩個寫入者各自假設了不同意義：

* PR-E（`wallet_topup._log_charged_not_credited_incident`）寫 `tx.reference`
  ＝**我方** MerchantTradeNo；
* PR-G（`ecpg_callback`）寫 `order['TradeNo']` ＝**ECPay 配發**的交易序號。

而 `_upsert_incident_snapshot` 會把 snapshot 裡除 `external_ref`／`kind`／`state`
以外的每一欄展成 `ON CONFLICT DO UPDATE SET <欄> = EXCLUDED.<欄>`
⇒ callback evidence 先寫入真 TradeNo（`kind` 仍是 `intent`）之後，PR-E 的 promote
會**把它覆寫成 `tx.reference`**。

🔴 損害不只是對帳可讀性：在「callback 早到、tx 還查不到」的場景，vendored addon
不會寫 `tx.provider_reference` ⇒ **`incident.trade_no` 是平台唯一一份 ECPay 交易
編號**，蓋掉就是銷毀。而且完全無聲——`_TRADE_NO_RE` 對兩種形狀都放行。

同版的生產碼已經把 PR-E 那一行拿掉（不再寫 `trade_no`），本支負責**既有列**：
把已經被寫進去的我方 reference 清掉，讓這一欄的語意從此只有一種。

## 判別式：`trade_no = external_ref`

PR-E 的唯一寫入點在**同一個 dict 字面量**裡把 `external_ref` 與 `trade_no` 都設成
同一個 `tx.reference`；而 upsert 的 `explicit_evidence` **刻意排除 `external_ref`**
⇒ 該欄永不被改寫 ⇒ **PR-E 寫入的列必然滿足 `trade_no = external_ref`**。

* **漏殺**：沒有。唯一「不等」的情況是 callback evidence 在 promote **之後**才到並
  覆寫成真 TradeNo——那時該欄已是合法值，本支正確地不動它。
* **誤殺**：`external_ref` 是 `TOPUP` + 15 碼 hex（20 字元純 alnum），ECPay TradeNo
  是另一個值域；要誤殺需要 ECPay 剛好配發一個與我方 reference 逐字相同的序號。
  ⚠ 這一格的依據是 vendored repo 的 fixture 形狀，**不是 ECPay 官方規格**（推論，非文件級證據）。

⛔ **不用 `kind='charged_not_credited'` 當判別式**：它會誤含「後到的 callback 已經把
值修正成真 TradeNo」的列。輔助欄位只適合做計數層面的交叉驗證。

## 清成 NULL 而不是「還原真值」

被覆寫掉的真 TradeNo **救不回來**（沒有備份）。清成 NULL 是刻意的取捨：
NULL ＝「我們沒有 ECPay 單號」（誠實），留著假值 ＝「查得到卻查不到」
——人工對帳時會浪費時間，甚至誤判成 ECPay 那邊沒這筆。Eugene 裁決 A 的原文即
「拿不到就清 NULL，清掉的本來就是假值」。

## 影響面：部署當下預期 0 列

prod／stg 在 2026-09-02 實測時 `woow_billing_incident` **表根本不存在**
（`relation ... does not exist`）——那張表會隨含 PR-D 的那次升版**新建成空表**，
本支在同一次升版跑 ⇒ 空表 ⇒ 0 列。本支的價值是**防禦性**的：已經跑過 PR-D/E 的
開發 sandbox，以及「建表與本支被拆成兩次發版」的情境。

⚠ Odoo 對 fresh install（`state == 'to install'`）**根本不呼叫 `migrate()`**
⇒ 全新環境不會跑本支，也不需要（表是空的；repo 內零 demo/seed 建 incident 記錄）。
"""
import logging

_logger = logging.getLogger(__name__)

_TABLE = 'woow_billing_incident'


def _has_table(cr, table):
    cr.execute("SELECT to_regclass(%s)", (table,))
    row = cr.fetchone()
    return bool(row and row[0])


def _has_column(cr, table, column):
    cr.execute(
        """
        SELECT 1 FROM information_schema.columns
         WHERE table_name = %s AND column_name = %s
        """,
        (table, column),
    )
    return bool(cr.fetchone())


def migrate(cr, version):
    # `version` 是**升級前**的舊版號。fresh install 時 Odoo 根本不會叫到這裡
    # （`odoo/modules/migration.py` 的 `state == 'to install'` 直接 return），
    # 這一行只是沿用 repo 既有的防禦性慣例。
    if not version:
        return

    # ⚠ 表可能不存在：有人可能從**沒有 PR-D** 的舊 DB 升上來，那時這張表是
    # 這一次才由 ORM 建的（而且是空的）。沒有這道防禦，升版整個炸掉。
    if not _has_table(cr, _TABLE):
        _logger.info(
            "[993] %s 尚未建表（這個 DB 之前沒有 PR-D）⇒ 無既有列需要清理", _TABLE)
        return

    # ⚠ 欄位也可能不存在（同上，半途狀態）。
    for column in ('trade_no', 'external_ref'):
        if not _has_column(cr, _TABLE, column):
            _logger.info(
                "[993] %s.%s 不存在 ⇒ 略過 backfill", _TABLE, column)
            return

    # 冪等：第二次跑時符合條件的列已經是 NULL，而 `NULL = external_ref` 不成立
    # ⇒ 0 rows。⚠ 仍然**必須 log rowcount**——否則「跑了但 0 列」與「根本沒跑到」
    # 在 log 上完全不可區分（那正是本 issue 家族一直在修的那種無聲）。
    cr.execute(
        """
        UPDATE %s SET trade_no = NULL
         WHERE trade_no IS NOT NULL AND trade_no = external_ref
        """ % _TABLE
    )
    _logger.info(
        "[993] trade_no backfill done: cleared %s row(s) whose trade_no "
        "equalled external_ref (our own MerchantTradeNo masquerading as a "
        "provider trade number); table=%s", cr.rowcount, _TABLE)

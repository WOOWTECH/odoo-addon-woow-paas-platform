"""#768：新增 stored related 之前，先關掉 policy 的配額守衛。

## 為什麼需要這一支

本次在 policy 與其 rule 子模型上各新增一支 **stored related** `smart_home_id`
（`related='smart_home_route_id.smart_home_id'` / `related='policy_id.smart_home_id'`）。
Odoo 會在模組載入時對**全表**重算並 flush，而 flush 會跑 `_validate_fields`
⇒ `_check_policy_quota`（它的 `@api.constrains` 清單含 `smart_home_id`）對**每一列
既有 policy** 重跑一次。

那些列在寫入當下都通過過一次，重跑不是新的授權判斷。但任何一列**事後**才變成違規
都會讓升級整個中斷，而中斷的理由與 #768 一點關係都沒有：

- 租戶換到上限更低的方案（`sa_max_access_policies` 變小，既有列 grandfathered）；
- 從備份還原的舊 DB，資料早於某次守衛加嚴。

## 旗標為什麼放 cursor 不放 context

重算是在 **context-stripped env** 裡跑的——`security_access_access_policy.py` 的
`_check_policy_quota` 註解有實測紀錄：context-only 的旗標在那幾趟會被靜默丟掉，而
「被丟掉」的症狀就是升級在一個看不出所以然的 ValidationError 上中斷。cursor 是整個
transaction 共用的，重算重建 env 也殺不掉它。

post-migrate 負責清掉（見同目錄 `post-migrate.py` 的 `migrate()` 開頭）。pre-migrate
拋出例外時整個 transaction 本來就會回滾，旗標不會外溢。

⚠ 只關**配額**這一條。`_check_anchor_exclusive_arc`（arc）刻意不關：它的 trigger 是
`_ANCHOR_FIELDS`，而新增的 `smart_home_route_id` 是一支普通 M2O、不會觸發重算 ⇒ 這條
constraint 在升級路徑上根本不會被叫起來；而萬一它真的被叫起來並且紅了，那代表資料
真的壞了，那正是我們想要停下來的時候。
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.sa_skip_policy_quota = True
    _logger.info(
        "[768] 已在 cursor 上設 sa_skip_policy_quota——新增的 stored related "
        "`smart_home_id` 會對全表重算並重跑配額 constraint，既有列的合規性不是"
        "本次升級要判斷的事（post-migrate 會清掉這個旗標）")

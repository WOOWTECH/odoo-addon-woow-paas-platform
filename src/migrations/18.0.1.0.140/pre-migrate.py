"""#519 提案 B：建 `subscription_id` partial unique index 前的重複列偵測。

索引本體由 `CloudService.init()` 建（`CREATE UNIQUE INDEX IF NOT EXISTS`，fresh
install 與 `-u` 兩條路徑都涵蓋、冪等）。這支只做一件事：**在那句 SQL 之前**先把
既有的重複列找出來，換成讀得懂的錯誤。

為什麼需要它：`CREATE UNIQUE INDEX` 撞到重複列時丟的是 psycopg2 的
``UniqueViolation: could not create unique index ...``——那個訊息說不出是**哪幾筆**
service、更說不出該怎麼處理，而升級當下沒有人有機會去查。

為什麼是 raise 而不是「偵測到就跳過建索引」：跳過的話升級會成功、`init()` 靜默
不建索引，於是**大家都以為並發保護在那裡、其實不在**——那正是 #519 這一輪要修掉
的假綠形態（Python 端的 check-after-act 在 REPEATABLE READ 下無效，唯一的保證就是
這個索引）。寧可讓升級停在一個講得清楚的地方。

處理方式（給維運）：訊息裡列出每一組重複的 (subscription_id, service ids)。一筆
訂閱對應多筆 service 是不該存在的狀態，正解是決定**哪一筆**服務真的對應那筆訂閱，
其餘的重新開通或刪除。**不要**用 `UPDATE ... SET subscription_id = NULL` 清掉——
`_cron_enforce_subscription_lifecycle` 的 domain 是 `subscription_id != False`，
清空指標會讓那些服務永久脫離停權／結算。
"""
import logging

from odoo import _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_TABLE = 'woow_paas_platform_cloud_service'


def migrate(cr, version):
    if not version:
        return

    # 表還不存在（極早期資料庫）就沒事可做——`init()` 會在建表之後自己建索引。
    cr.execute("SELECT to_regclass(%s)", (_TABLE,))
    if not cr.fetchone()[0]:
        _logger.info("[519-B] %s 尚未建表，略過重複列偵測", _TABLE)
        return

    cr.execute(
        """
        SELECT subscription_id, array_agg(id ORDER BY id)
          FROM woow_paas_platform_cloud_service
         WHERE subscription_id IS NOT NULL
         GROUP BY subscription_id
        HAVING count(*) > 1
         ORDER BY subscription_id
        """
    )
    duplicates = cr.fetchall()
    if not duplicates:
        _logger.info(
            "[519-B] subscription_id 無重複列，partial unique index 可安全建立")
        return

    detail = '\n'.join(
        '  subscription_id=%s → cloud_service ids=%s' % (sub_id, list(ids))
        for sub_id, ids in duplicates
    )
    _logger.error("[519-B] subscription_id 有重複列，無法建立唯一索引：\n%s", detail)
    raise UserError(_(
        "#519: several cloud_service rows point at the same subscription, so "
        "the `woow_paas_cloud_service_subscription_uniq` unique index cannot "
        "be created.\n\n%(detail)s\n\nDecide which service each "
        "subscription really belongs to, then re-provision or delete the "
        "rest. Do NOT clear subscription_id to NULL - that would detach those "
        "services from the suspension / billing cron for good.",
        detail=detail,
    ))

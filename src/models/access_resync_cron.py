# -*- coding: utf-8 -*-
"""#803：`access_protection_lost` 的週期性收斂者。

## 這條 cron 存在的理由

`access_protection_lost`（#794 建立，三腿同名）記的是一件很具體的事：**Cloudflare
Access application 已經被拆掉、而列本身還在**。此時 `is_protected` 只剩**意圖**——
CF 上沒有任何東西在強制登入，而 hostname 照常由 tunnel 服務。也就是：**對外可達、
且未受保護**。

那個旗標的消費路徑逐字寫在 `security_access.py::_access_mark_protection_lost` 的
docstring 裡：

    平台沒有週期性 Access re-sync，只有 provision / resume / 直接寫
    `is_protected`／`login_*`／`session_duration` 三種事件會推。

三種**都要租戶或維運自己動手**。沒人動，窗口就不會自己關——而旗標唯一的寫入者是
`cf_access_policy_reap._compensate_fail_open_after_rollback()`（獨立交易），它出現的
時機正是「使用者剛做的那個刪除動作失敗了」，也就是最不會有人接著再動一次設定的
時候。本 cron 就是那條缺席的收斂路徑。

## 三張表不是四張

issue #803 的四腿表格裡，SmartHome 那腿的錨點就是它底下的 route 列 ⇒ 掃
`smart_home_route` 即涵蓋。

## 為什麼是 AbstractModel

本 cron **不存任何資料**，所以不需要表。`cf_access_policy_reap` 是 `models.Model`
是因為它要存待回收帳本（`_record` / `_pending_ids`），那是資料需求不是 cron 通例；
照它抄會憑空多一張永遠空著的表。同模組的 AbstractModel cron 先例：
`ecpay_invoice_cron` / `low_balance_warning_cron` / `subscription_lifecycle_cron`。

⚠ **ACL 不加**：AbstractModel 無表，`ir.model.access` 對它不生效。
（repo 內現況**不一致**——`subscription_lifecycle_cron` 有一行 ACL、另外兩個沒有。
這裡依性質判定而不是依多數先例，理由寫在這裡讓覆核者不必猜。）

## 頻率

推導寫在 `src/data/access_resync_cron.xml` 的註解裡，**只有那一處**。
這裡不複述——同一個推導寫兩處保證會漂移，而漂移之後兩份都像權威。
"""
import logging

from odoo import api, models

_logger = logging.getLogger(__name__)

#: 掃描標的：三腿各自的 model 名。三張表都有同名的 `access_protection_lost`
#: 欄位與同名的 `_sync_access_to_operator()` / `_access_sync_skip_reason()`。
#:
#: ⚠ 順序即掃描順序，但**不影響公平性**：預算是逐腿各自計的（見
#: `_cron_resync_access_protection` 的 docstring），所以排在後面的腿不會被前面那腿
#: 吃光額度。
_RESYNC_LEGS = (
    'woow_paas_platform.security_access_route',
    'woow_paas_platform.smart_home_route',
    'woow_paas_platform.cloud_service',
)

#: 只掃打開了旗標的列。**這就是全部的 domain**——不加「卡多久算不正常」的時間
#: 條件（三腿旗標是純 Boolean、沒有時間欄位，要加就得新增欄位＝新增 migration，
#: 形狀變太多，另開單），也不加終態淘汰（終態定義未查證；那只改 domain、不受
#: `noupdate` 綁，有資料再說）。
_RESYNC_DOMAIN_FIELD = 'access_protection_lost'


class AccessResyncCron(models.AbstractModel):
    _name = 'woow_paas_platform.access_resync_cron'
    _description = 'PaaS: periodic Access protection re-sync'

    # ------------------------------------------------------------------ #
    #  cron 入口                                                          #
    # ------------------------------------------------------------------ #

    @api.model
    def _cron_resync_access_protection(self, limit=200, scan_batch=1000,
                                       max_scan=20000):
        """掃三張表上的 `access_protection_lost` 列，逐列重推 Access 狀態。

        回傳這一輪**真的推動**了幾列（被 gate 擋掉與推失敗的不算）。

        ## 🔴 為什麼是 cursor 掃描而不是 `search(domain, limit=N)`（#791 覆核 F1）

        被 liveness gate 永久擋住的旗標列（SA/SH 停權中、CS 已進 `deleting`）
        **永遠不會消失**——它們推不動，所以旗標永遠不會被清掉。而 `_order` 讓它們
        佔住最小的 id ⇒ 累積到 N 筆之後，每一輪撈到的都是同一批推不動的列，真正
        該推的一次都輪不到。更糟的是那個狀態與「今天沒事做」在輸出上完全一樣
        （回 0、零 log）——收斂機制癱瘓而沒有任何訊號。

        所以窗口不能套在判定**之前**：這裡沿著 id 游標一批批掃過去。

        ## 預算是**逐腿**各自計的

        `limit` / `max_scan` 對每一腿各算一次，不是三腿共用一份。共用的話，只要
        第一腿長期有滿額的可推列，後面兩腿就永遠輪不到——那是 F1 的同一個形狀
        搬到腿的層級（而症狀同樣是「另外兩張表看起來沒事做」）。代價是一輪最壞
        情況的外呼次數是 `3 * limit`，這是刻意的取捨。

        `limit` 限的是「這一腿這一輪**對 operator 發出幾次**推送」，**成功與失敗
        都算**——只算成功的話，operator 持續失敗時這一輪會一路打到 `max_scan`
        為止，變成失敗時打得比成功時還多（同 #792 二輪覆核 F1(a)）。被 gate 擋掉
        的列一次外呼都不會發，所以不計入預算。

        ## 不開 force 旁路

        直接呼叫三腿都有的 `_sync_access_to_operator()`，**不**開
        `_force_access_sync` 之類的旁路。被 gate 擋掉時「保護沒生效」本來就是實話
        ——那一刻對外根本沒有東西可達（SA/SH 停權會把 ingress 覆寫成
        `http_status:503`，CS 的 `deleting` 是 teardown 已經跑完）——旗標留著讓 UI
        說出來，由那條腿自己的收斂點（resume / 下一次成功 sync）清掉才是對的。
        開旁路等於在停權中／`deleting` 中把 Access app 建回去。

        ## 旗標由誰清

        **不是這裡。** 推成功時 `_sync_access_to_operator()` 自己會清（#794 的既有
        行為，旗標由 sync 結果驅動）。本方法一行清除碼都沒有——多寫一處就是第二個
        真相來源，而兩者不一致時沒有任何訊號。
        """
        totals = {'scanned': 0, 'pushed': 0, 'gated': 0, 'failed': 0}
        truncated = []
        per_leg = []
        for model_name in _RESYNC_LEGS:
            counts, cut = self._resync_leg(
                model_name, limit=limit, scan_batch=scan_batch,
                max_scan=max_scan)
            for key in totals:
                totals[key] += counts[key]
            if cut:
                truncated.append(model_name)
            per_leg.append('%s(s=%d,p=%d,g=%d,f=%d)' % (
                model_name.rsplit('.', 1)[-1], counts['scanned'],
                counts['pushed'], counts['gated'], counts['failed']))

        if truncated:
            _logger.warning(
                "[ACCESS_RESYNC] scan cap (%d) reached before the flagged rows "
                "were exhausted on: %s — the rest wait for the next run",
                max_scan, ', '.join(truncated))

        # 🔴 **每一輪都出聲，零命中那輪也是**，而且空手那一行同樣自帶三個計數。
        #
        # 兩個相反的失效模式把這個形狀夾出來：
        #   * 逐列出聲 ⇒ log 洪水。`cf_access_policy_reap._warn_if_deferred` 的
        #     docstring 逐字說明過那個下場——「無條件 warning 會變成 log 洪水，
        #     而洪水等於沒有訊號」。
        #   * 完全不出聲 ⇒ 與「今天沒事做」不可區分，正是 #791 F1 說的那一格。
        #     `_cron_reap_dead_anchors` 的彙總掛在 `if reaped or failed_anchors:`
        #     底下，零命中那輪就是安靜的；這裡刻意**不**照抄那一格。
        # 一輪一行、15 分鐘一次 ＝ 96 行／日，遠低於洪水量級。
        #
        # ⚠ 空手那一行不得寫成 "nothing to do"：「掃了 0 列」與「掃了 500 列全被
        # 擋」是兩件事，同一句話要能分開，所以計數永遠印出來。
        #
        # 🔴 **升級成 WARNING 的判準只看 `failed`，`gated` 不參與**（#961）。
        # `gated` ＝被 liveness gate 擋掉、這輪推不動（SA/SH 停權中、CS 已進
        # `deleting`），那是**預期狀態**不是異常——本方法 docstring 的「不開 force
        # 旁路」那段逐字說過：那一刻對外根本沒有東西可達，旗標留著是實話。長期停權
        # 的租戶會讓這一行每 15 分鐘 warn 一次＝96 次／日，正好長成上面那個「洪水
        # 等於沒有訊號」的形狀。三個計數照印 ⇒ 訊號不丟，只是不再每輪拉警報。
        (_logger.warning if totals['failed'] else _logger.info)(
            "[ACCESS_RESYNC] scanned=%d pushed=%d gated=%d failed=%d | %s",
            totals['scanned'], totals['pushed'], totals['gated'],
            totals['failed'], ' '.join(per_leg))
        return totals['pushed']

    # ------------------------------------------------------------------ #
    #  單腿掃描                                                           #
    # ------------------------------------------------------------------ #

    @api.model
    def _resync_leg(self, model_name, limit, scan_batch, max_scan):
        """掃一張表。回傳 ``({counts}, 是否踩到掃描上界)``。"""
        Leg = self.env[model_name].sudo()
        counts = {'scanned': 0, 'pushed': 0, 'gated': 0, 'failed': 0}
        last_id = 0
        attempted = 0
        truncated = False
        while attempted < limit:
            # `order='id'` 顯式帶上，不靠 `_order` 的預設：游標式掃描的正確性完全
            # 建立在「這一批的最大 id 之後還有沒有列」上——日後有人改 `_order`
            # 就會讓游標原地空轉，而症狀是「每輪都回 0」，與真的沒有旗標列不可
            # 區分。（`smart_home_route._order` 現在就是 `is_primary desc, id`。）
            rows = Leg.search(
                [('id', '>', last_id), (_RESYNC_DOMAIN_FIELD, '=', True)],
                limit=scan_batch, order='id')
            if not rows:
                break
            last_id = rows[-1].id
            counts['scanned'] += len(rows)
            for row in rows:
                if self._resync_one(row, counts):
                    attempted += 1
                if attempted >= limit:
                    break
            if counts['scanned'] >= max_scan:
                # 「是不是真的被截斷」用**實查**不用猜：拿「這一批是滿的」當代理
                # 判準時，旗標列數剛好整除 `scan_batch`（最後一批是滿的、但後面
                # 其實沒有了）就會發假截斷警報。訊號通膨與訊號缺席一樣糟。
                truncated = bool(Leg.search(
                    [('id', '>', last_id), (_RESYNC_DOMAIN_FIELD, '=', True)],
                    limit=1, order='id'))
                break
        return counts, truncated

    @api.model
    def _resync_one(self, row, counts):
        """重推一列。回傳「這一列有沒有用掉外呼預算」。

        savepoint 是必須的：`_sync_access_to_operator()` 在真的打到 operator 之後
        才回存 CF id、消帳、清旗標，中途拋例外時那些半成品必須跟著回捲，否則下一
        輪面對的是一個「旗標還在、但列上狀態已經被改過一半」的列。

        ⚠ #1052 標記（非裁定）：這個 savepoint 同時包住 operator 外呼與 2xx 之後的
        落帳——落帳失敗會回捲到「外部已生效、DB 未記」的狀態，這正是
        docs/decisions/patterns/post-commit-bookkeeping-rollback-unit.md 要避免的
        形態；且下方 `except Exception` 未分流 OperationalError（該 doc 規則一）。
        是否因為下一輪 resync 會重推而刻意如此，待裁定；裁定前別照抄本站形狀。
        """
        try:
            with self.env.cr.savepoint():
                pushed = row._sync_access_to_operator()
        except Exception as e:  # noqa: BLE001 — 一列失敗不得中斷整輪
            # ⛔ 不得吞掉失敗：安靜地跳過與「推成功了」在輸出上不可區分，而那正是
            # 本 issue 要修的那一格的翻版。訊息要指名是哪一列——只說「有一列失敗」
            # 的 warning 沒有人查得下去。
            counts['failed'] += 1
            _logger.warning(
                "[ACCESS_RESYNC] re-sync failed for %s#%s: %s — "
                "access_protection_lost stays set, will retry next round",
                row._name, row.id, e)
            return True
        if pushed:
            counts['pushed'] += 1
            return True
        # `_sync_access_to_operator()` 回 False ＝ 被 liveness gate 擋掉，一次外呼
        # 都沒發生（#528 把三道 gate 收斂成 `_access_sync_skip_reason()` 一個裁決
        # 處，並讓「被擋掉」與「推了而且成功」在回傳值上分得開）。旗標留著是對的
        # ——保護確實沒生效，那是實話，見本類方法的 docstring。
        counts['gated'] += 1
        return False

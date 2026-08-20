"""待回收的 Cloudflare Access 物件帳本（#528 建立、#784 擴充到 app）。

## 這張表存在的理由

CF 的 reusable policy **只能經 operator 的 ``existing_policy_ids`` 被刪掉**
（delete-then-recreate 是唯一的刪除通道）。而那份清單是從**尚存的** policy 列
推導出來的 ⇒ 一列被 unlink 之後，它的 ``cf_policy_id`` 就再也進不了那份清單。
`SecurityAccessAccessPolicy.unlink()` 因此在 ``super().unlink()`` 之前把 cf id
撈出來，落進本表，讓之後任何一次成功的 sync 帶出去刪。

（歷史：#528 之前那批 id 是經 ``sa_unlinked_cf_policy_ids`` context 注入**那一次**
re-sync 的。#528 打穿的正是「那一次」：三腿的 ``_sync_access_to_operator()`` 各自
有一組 liveness gate（SA／SH 是停權狀態、CS 是 ``deleting`` / 沒有 subdomain），
gate 一擋，operator 一次都不會被呼叫，而 context 用完即丟、DB 列已經不在
⇒ **那顆 CF policy 從此沒有任何記錄指向它，之後任何一次 re-sync 都不會回收**
（2026-08-02 在 stg 實際漏了兩顆，其中一條是 allow everyone）。那把 context key
已於 #783 整條移除，理由見下方設計決定 1。）

所以帳要**離開 context、落到 DB**：unlink 當下記帳，之後**任何一次成功的**
sync 把帳上的 id 併進 ``existing_policy_ids`` 一起送去刪，operator 回報確實刪掉
了才消帳。sync 失敗或被 gate 擋掉時帳原封不動 —— 帳的壽命不再綁在「當次 sync
有沒有跑」上，這就是本表的全部價值。

## #784：兩件事變了

1. **帳也收 Access application id**（``kind='app'``）。刪整條 route 時丟掉的不
   只是 policy：``access_app_id`` 一起消失，而它跟 policy 一樣只能經 operator
   的 ``existing_app_id`` 被刪。欄位因此從 ``cf_policy_id`` 改名為
   ``cf_object_id`` —— 讓一個叫 policy 的欄位裝 app id 正是「同名不同物」那類
   陷阱：任何漏了 ``kind`` 條件的查詢都會把 app id 餵進 ``existing_policy_ids``，
   operator 拿它去 DELETE 一顆不存在的 policy、CF 回 404（視為成功）⇒ 帳被消掉
   而 app 永遠留在 CF，**靜默**。所有讀寫入口一律帶 ``kind``，預設 ``'policy'``
   讓 #528 的既有呼叫端語意不變。
2. **dead anchor 有人收了**（``_cron_reap_dead_anchors``）。#528 的消費路徑是
   「錨點的下一次成功 sync」——錨點列被刪掉之後**沒有任何 sync 會再為它跑**，
   那筆帳原本永遠沒人處理。這條 cron 就是那一格的回收者，見它自己的 docstring。

## 兩個刻意的設計決定

1. **doomed id 的唯一來源是「列上讀到的 ``cf_object_id``」，沒有 context 通道。**
   ``sa_unlinked_cf_policy_ids`` 曾經是一把裸 context key，而
   ``/web/dataset/call_kw`` 把呼叫端的 context 原封不動交給 ORM（#522／#536 已就
   同族 key 實測過）⇒ 租戶在 RPC payload 多塞一個 key 就能決定 operator 要刪哪些
   CF policy，而 CF reusable policy 是**帳號級**物件
   （``delete_reusable_policy`` 打的是 ``/accounts/{id}/access/policies/{pid}``，
   沒有 app 也沒有租戶邊界）。

   🔴 **#783 把那條腿整個拿掉**，而不是給它加一把 sentinel。理由是它在生產路徑上
   本來就**冗餘**：``unlink()`` 在同一個 savepoint 內對每一個錨點**無條件**先
   ``_record_ids()`` 落帳，之後才推 sync，而 context 只注入給 ``exists()`` 的那個
   子集 ⇒ 帳本是 context 的**嚴格超集**，``_pending_for()`` 的 context 迴圈一顆也
   加不到。留著它只是留一條沒人需要的偽造通道。

   ⚠ 偽造面不在 ``unlink()`` 那一行：``_pending_for()`` 讀的是「錨點當下的
   ``env.context``」，而**任何**會觸發 ``_sync_access_to_operator()`` 的操作都沿用
   呼叫端的 context（``_access_sync_targets()`` 用 ``.mapped()``，不重置 env）
   ⇒ 修的必須是**讀取端**。只在注入端包一層 marker 會讓那些路徑原封不動可攻擊，
   而且測試不會紅。

2. **租戶對本表零 ACL row（只給 ``base.group_system``），平台碼一律 ``sudo()``。**
   與 ``cloud_service_secret`` / ``reconcile_report`` 同型。這比在欄位上加一把
   sentinel guard 更強：租戶連 model 都碰不到，不必再維護第四把 sentinel，也就
   沒有「sentinel 命名互為子字串害 repo-lint 說謊」那類問題
   （見 ``src/models/smart_home_route.py`` 的命名禁令）。#783 選「拿掉通道」而不是
   「sentinel 化通道」用的是同一條論證。

## 錨點為什麼是 ``(model, id)`` 而不是三支 Many2one

三個錨點是三個不同 model（``security_access_route`` / ``cloud_service`` /
``smart_home_route``），Many2one 版本要把 policy 那套 exclusive-arc 機制再抄一遍。
更關鍵的是**壽命**：Many2one 帶 ``ondelete='cascade'`` 會讓帳隨錨點一起消失，
而「錨點被刪掉、帳還沒回收」正是最不能靜默丟掉的那一格（#784 整張 issue 就是
這一格）。純量 pair 讓那筆帳留下來（reconcile 報表也會從 CF 那側把同一顆 id 列成
孤兒候選，兩個信源對得起來）。
"""
import logging
from datetime import timedelta

from psycopg2 import OperationalError

from odoo import api, fields, models

from ..services.paas_operator import get_paas_operator_client

_logger = logging.getLogger(__name__)

#: 帳卡在**活錨點**上多久算不正常（#791 覆核 F2）。
#:
#: 這個值是推導出來的不是拍板的：帳的正常消費路徑是「錨點的下一次成功 sync」，
#: 而最慢的**合法**路徑是停權 → 續訂 → resume，resume 由
#: `subscription_lifecycle_cron`（每日一次）驅動 ⇒ 一兩天內還在帳上完全正常。
#: 取一週：足夠蓋掉「續訂拖幾天才 resume」這種正常情形，又遠短於真正危險的那
#: 一類——錨點進了永久終態（如 `deleting`）之後 sync 再也不會跑，那種帳**永遠**
#: 不會消。門檻的作用是把後者從前者裡分出來，不是精準預測。
_STUCK_PENDING_DAYS = 7

# 🔴 **`sa_unlinked_cf_policy_ids` 這把 context key 已於 #783 移除，不要加回來。**
#
# 它曾經是 policy unlink 注入 doomed id 的通道，值會原封不動進
# `existing_policy_ids`，而那是 operator 唯一的刪除通道、打的是**帳號級**端點
# ⇒ 任何能觸發一次 access sync 的租戶（改自己 route／CS／SH route 的一個 Access
# 欄位就夠）都能指定平台去刪它知道 id 的任何一顆 CF reusable policy。
#
# 拿掉不減功能：`unlink()` 在同一個 savepoint 內**無條件**先落帳才推 sync，帳本是
# 那條 context 的嚴格超集（見上方設計決定 1）。
#
# 機器守線：`scripts/audit/repo-lint.py` 的 `INTERNAL_CTX_KEY_REGISTRY` 把這把 key
# 登記為 `status='removed'` ⇒ 只要有人在 `src/`（`src/tests/` 除外）重新寫出它的
# `context.get(...)` 讀取點或 `with_context(...)` 注入點，第 28 條就會紅。

#: 一列記的是哪一種 CF Access 物件。**兩者的刪除通道不同**（`existing_policy_ids`
#: vs `existing_app_id`），混在一起送出去不會報錯只會靜默失敗，所以每一個讀寫
#: 入口都要顯式指定。
KIND_POLICY = 'policy'
KIND_APP = 'app'


class CfAccessPolicyReap(models.Model):
    """一列 ＝ 一顆「DB 已刪、CF 尚未確認回收」的 Access 物件（policy 或 app）。"""

    _name = 'woow_paas_platform.cf_access_policy_reap'
    _description = 'Pending Cloudflare Access Object Reap'
    #: FIFO：先欠的先送。`_pending_ids` 依賴這個順序，改成 desc 會讓
    #: `existing_policy_ids` 的順序在不同呼叫間漂動（不影響正確性，但會讓
    #: payload 側錄型測試莫名其妙地不穩）。
    _order = 'id'

    anchor_model = fields.Char(
        string='Anchor Model', required=True, index=True,
        help="錨點的 model 名（`security_access_route` / `cloud_service` / "
             "`smart_home_route` 之一）。與 anchor_id 合起來決定「哪一次 sync "
             "會把這顆 id 送出去」——刻意不做成帳號級全域清單：CF 的刪除是帳號級 "
             "的，全域清單會讓 A 租戶的 sync 夾帶 B 租戶的 id。",
    )
    anchor_id = fields.Integer(
        string='Anchor ID', required=True, index=True,
        help='錨點列的 id。錨點列可能已經被刪掉——那正是本表要留住的情形，'
             '所以刻意用純量而不是 Many2one。',
    )
    kind = fields.Selection(
        [(KIND_POLICY, 'Reusable Policy'), (KIND_APP, 'Access Application')],
        string='Object Kind', required=True, default=KIND_POLICY, index=True,
        help='這顆 id 是 reusable policy 還是 Access application。operator 的'
             '刪除通道不同（existing_policy_ids vs existing_app_id），送錯欄位'
             '會被 CF 當成「刪一顆不存在的東西」回 404＝成功，帳被消掉而物件'
             '留著（#784）。',
    )
    cf_object_id = fields.Char(
        string='CF Object ID', required=True, index=True,
        help='Cloudflare 上待回收的物件 id（reusable policy 或 Access '
             'application，由 kind 決定）。',
    )

    # 🔴 **刻意沒有 UNIQUE(anchor_model, anchor_id, kind, cf_object_id)**，去重靠
    # `_record_ids()` 的先查後寫。理由是失敗形狀而不是效能：`_settle()` 跑在
    # **operator 已經回 2xx 之後**——CF 那邊的刪除已經發生、回捲不掉——此時一個
    # unique violation 會把整個交易 abort 掉，把「記帳失敗」升級成「操作失敗」，
    # 換來的是 DB 與 CF 不一致 ＋ 呼叫端收到假的錯誤
    # （`docs/decisions/patterns/post-commit-bookkeeping-rollback-unit.md`）。
    # 而重複列在這裡是**無害的**：`_build_existing_policy_ids()` 逐顆去重才 append，
    # `_settle()` 是 `('cf_object_id', 'in', ...)` 整批刪。拿「不可能發生的重複」
    # 去換「已經發生的外部變更被回捲」不划算。

    # ------------------------------------------------------------------ #
    #  記帳 / 讀帳 / 消帳（三腿共用的單一裁決處）                          #
    # ------------------------------------------------------------------ #

    @api.model
    def _record(self, anchor, cf_policy_ids):
        """把 doomed policy id 記進帳（同一錨點同一 id 只記一次）。

        ⚠ ``cf_policy_ids`` 必須來自**被刪列上讀到的** ``cf_policy_id``。
        不要餵 context 進來，理由見模組 docstring 的設計決定 1。
        """
        if not anchor:
            return self.browse()
        return self._record_ids(anchor._name, anchor.id, cf_policy_ids)

    @api.model
    def _record_ids(self, anchor_model, anchor_id, cf_ids, kind=KIND_POLICY):
        """``_record`` 的純值版：錨點列可能已經不在了（teardown 路徑）。"""
        if not anchor_model or not anchor_id:
            return self.browse()
        wanted = [i for i in (cf_ids or []) if i]
        if not wanted:
            return self.browse()
        known = set(self._pending_ids(anchor_model, anchor_id, kind=kind))
        vals_list = [{
            'anchor_model': anchor_model,
            'anchor_id': anchor_id,
            'kind': kind,
            'cf_object_id': cf_id,
        } for cf_id in dict.fromkeys(wanted) if cf_id not in known]
        if not vals_list:
            return self.browse()
        return self.sudo().create(vals_list)

    @api.model
    def _pending_ids(self, anchor_model, anchor_id, kind=KIND_POLICY):
        """該錨點帳上還沒確認回收的 cf id（FIFO），只回 ``kind`` 那一種。

        取 ``(model, id)`` 而不是 record：錨點列被刪掉之後仍要查得到帳。
        """
        if not anchor_model or not anchor_id:
            return []
        rows = self.sudo().search([
            ('anchor_model', '=', anchor_model),
            ('anchor_id', '=', anchor_id),
            ('kind', '=', kind),
        ])
        # `dict.fromkeys`：保序去重。表上沒有 UNIQUE（見上方註解），競態下可能
        # 有重複列，而呼叫端拿到的應該是「還欠哪幾顆」而不是「帳上有幾列」。
        return list(dict.fromkeys(
            r.cf_object_id for r in rows if r.cf_object_id))

    @api.model
    def _pending_for(self, anchor):
        """這個錨點帳上還欠的 doomed **policy** id（record 版的 ``_pending_ids``）。

        ⚠ 只回 policy：這個回傳值的唯一去處是 ``existing_policy_ids``。

        🔴 **這裡刻意不讀任何 context**（#783）。這條路徑的輸出會變成 operator 的
        帳號級刪除清單，而 ``/web/dataset/call_kw`` 把呼叫端的 context 整包換進
        env ⇒ 讀 context 等於讓呼叫端指定要刪哪些**別人帳號上的** CF policy。
        舊的 ``sa_unlinked_cf_policy_ids`` 通道已整條移除，理由與「為什麼不是改成
        sentinel」見模組 docstring 的設計決定 1。

        存量收斂請走 ``_record_ids()`` 直接落帳（那條只吃呼叫端**明確傳進來的參數**
        ——那是伺服器端程式碼的呼叫，不是 RPC 可觸達的 context）。
        """
        if not anchor:
            return []
        return self._pending_ids(anchor._name, anchor.id)

    @api.model
    def _settle(self, anchor_model, anchor_id, submitted_ids, orphaned_ids,
                trusted_ids):
        """一次成功的 sync 之後結帳。**呼叫時機＝operator 回 2xx 之後**。

        兩個方向一起做，因為它們讀的是同一份事實（operator 的回應）：

        * **消帳**：這一輪送進 ``existing_policy_ids``、而 operator **沒有**把它
          列進 ``orphaned_policy_ids`` 的 ⇒ CF 上已經沒有它了，帳可以消。
          CF 的 DELETE 把 404 也當成功（``services/cloudflare.py`` 的
          ``delete_reusable_policy``），所以「早就不在了」不會落在孤兒清單裡，
          帳不會因此永遠清不掉。
        * **記帳**：被列進 ``orphaned_policy_ids`` 的是「送去刪但刪不掉」的，
          它們**現在**就是 CF 孤兒。今天三腿對這格的處置是 log 一行然後永遠忘記
          （註解自己寫著 "permanent CF orphans ... no later re-sync reconciles"）
          ——記進帳之後，下一次成功的 sync 會再送一次，log 也還是照發。

        取 ``(model, id)`` 而不是 record：teardown 路徑呼叫到這裡時錨點列可能
        已經不在了。

        🔴 ``trusted_ids``（#791 覆核 F9，**必填、刻意沒有預設值**）＝這一輪送出去
        的 id 裡「來自 DB 列上真實存在過的值」那個子集。記帳只認
        ``trusted_ids ∪ 帳上已知``，其餘一律拒收並出聲。

        🔴 **這是第二道，不是唯一一道**。#783 已經把可偽造的 context 通道整條拿掉
        ⇒ ``existing_policy_ids`` 現在是 ``列值 ∪ 帳本``，兩邊都只裝伺服器端讀到的
        值。但 ``orphaned_policy_ids`` 是**外部輸入**（operator 的回應），它裡面冒
        出一顆我們根本沒送過的 id 這件事不會因為 #783 而消失 ⇒ 結帳仍然要自己判，
        否則一次性的異常會**經由結帳落進 DB**升級成持久的刪除指令，而且從此每小時
        被重送到刪掉為止（本表會把爆炸半徑放大，所以這道過濾是本表該收的尾）。

        沒有預設值是刻意的：預設「就信 submitted」會讓忘記傳的呼叫端**靜默**回到
        有洞的行為，而那正是最容易被寫成例外的那一格。
        """
        still_orphaned = [i for i in (orphaned_ids or []) if i]
        submitted = [i for i in (submitted_ids or []) if i]
        orphan_set = set(still_orphaned)
        reaped = [i for i in submitted if i not in orphan_set]
        if reaped:
            rows = self.sudo().search([
                ('anchor_model', '=', anchor_model),
                ('anchor_id', '=', anchor_id),
                ('kind', '=', KIND_POLICY),
                ('cf_object_id', 'in', reaped),
            ])
            if rows:
                rows.unlink()
        if not still_orphaned:
            return
        allowed = set(trusted_ids or []) | set(
            self._pending_ids(anchor_model, anchor_id))
        refused = [i for i in still_orphaned if i not in allowed]
        still_orphaned = [i for i in still_orphaned if i in allowed]
        if refused:
            _logger.warning(
                "[CF_REAP] refused to book %d operator-reported orphan id(s) "
                "for %s#%s that came from neither a DB row nor the ledger: %s "
                "— an id we never submitted must not become a durable delete "
                "order against an account-level Cloudflare object (#791 F9; "
                "the forgeable context channel itself was removed in #783)",
                len(refused), anchor_model, anchor_id, ', '.join(refused))
        if still_orphaned:
            self._record_ids(anchor_model, anchor_id, still_orphaned)

    @api.model
    def _trusted_ids_for(self, anchor):
        """``_settle`` 的 ``trusted_ids``：錨點列上**真實存在**的 cf policy id。

        三腿的 policy 子表是同一個 model，欄位名一致（``access_policy_ids`` /
        ``cf_policy_id``），所以這個推導只寫一次。刻意**不**經 ``_pending_for``
        ——那條路會把 context 的值一起帶進來，而這裡要的正好是「排除 context」。
        """
        if not anchor:
            return []
        return [p.cf_policy_id for p in anchor.access_policy_ids
                if p.cf_policy_id]

    @api.model
    def _warn_if_deferred(self, anchor, logger, label, reason):
        """sync 被 gate 擋掉、而手上還有待回收 id 時大聲說出來（#528 方向 3）。

        **只在有待回收 id 時才出聲**是刻意的：停權狀態下 sync 被 gate 擋掉在很多
        正常流程裡是預期行為（每一次 write 都會走到），無條件 warning 會變成
        log 洪水，而洪水等於沒有訊號。危險的是「帶著待回收 id 卻被略過」這一格
        ——今天那一格與正常的 no-op 在輸出上完全不可區分，#528 就是因此被誤判成
        「同步成功」。

        ``logger`` 由呼叫端傳自己那支：訊息落在各腿自己的 module logger 底下，
        既有的 `[CLOUD_SVC]` / `[SH]` grep 慣例不會斷，而「要不要出聲」這個判斷
        仍然只有這一處。回傳 bool 方便測試與呼叫端判別。
        """
        pending = self._pending_for(anchor)
        if not pending:
            return False
        logger.warning(
            "Cloudflare Access sync SKIPPED for %s: %s — %d CF Access "
            "policy(ies) are still pending reap and were NOT deleted this "
            "round: %s. They stay on the reap ledger and will be retried on "
            "the next successful sync (#528).",
            label, reason, len(pending), ', '.join(pending))
        return True

    # ------------------------------------------------------------------ #
    #  #784：刪除路徑的記帳／消帳（app ＋ policy 一起）                    #
    # ------------------------------------------------------------------ #

    @api.model
    def _record_teardown(self, anchor_model, anchor_id, app_id, policy_ids):
        """刪除路徑上拆不掉的東西一次記完（app id ＋ 剩下的 policy id）。

        呼叫端一律是「錨點列即將／已經消失，而這些 id 沒有被 CF 確認回收」那一
        格。**不判斷錨點還在不在**：判斷錯的方向是靜默丟失，而多記一筆的代價
        只是 cron 多跑一次冪等的刪除（CF 的 DELETE 把 404 當成功）。
        """
        recorded = self._record_ids(anchor_model, anchor_id, [app_id],
                                    kind=KIND_APP)
        recorded |= self._record_ids(anchor_model, anchor_id, policy_ids)
        return recorded

    @api.model
    def _settle_teardown(self, anchor_model, anchor_id, app_id,
                         submitted_policy_ids, orphaned_policy_ids):
        # teardown 路徑送出去的 id 全部來自「捕獲當下列上讀到的值」或帳本
        # （沒有任何一條 teardown 讀 context），所以 submitted 本身就是可信集合。
        """一次成功的 teardown 之後結帳（app 那半 ＋ policy 那半）。

        app 這半沒有「orphaned」對應物：operator 的 teardown 分支對 app 只有
        「刪掉了（含 404 已不在）」與「丟例外」兩種結果，回得到 2xx 就代表這顆
        app id 在 CF 上已經沒有了 ⇒ 直接消帳。
        """
        if app_id:
            rows = self.sudo().search([
                ('anchor_model', '=', anchor_model),
                ('anchor_id', '=', anchor_id),
                ('kind', '=', KIND_APP),
                ('cf_object_id', '=', app_id),
            ])
            if rows:
                rows.unlink()
        self._settle(anchor_model, anchor_id,
                     submitted_policy_ids, orphaned_policy_ids,
                     trusted_ids=submitted_policy_ids)

    # ------------------------------------------------------------------ #
    #  提交點之後的結帳包裝（#791 覆核 F4／F7 的單一裁決處）               #
    # ------------------------------------------------------------------ #
    #
    # operator 回 2xx 之後 CF 那側的刪除已經發生、回捲不掉 ⇒ 之後每一步都只是
    # **記帳**。記帳失敗不得翻轉呼叫端的語意，也不得把整個交易拖下水
    # （[[post-commit-bookkeeping-rollback-unit]]）。
    #
    # ⚠ `OperationalError` **必須外拋**（#518）：serialization / 連線類例外被吞掉
    # 的話 Odoo 的 `retrying()` 永遠看不到，而此刻 PG 交易已 aborted，硬回一個值
    # 只會讓後續 flush 再炸一次。這條對呼叫端是「回 bool 的函式仍可能拋
    # OperationalError」——三腿的 teardown docstring 都寫明了這個例外。
    #
    # 包成兩個 helper 而不是在七個呼叫點各寫一次 try/savepoint：判準（哪些例外
    # 吞、哪些外拋、回捲單位多大）只該有一處，否則第八個呼叫點就會漏。

    @api.model
    def _settle_after_commit(self, anchor_model, anchor_id, submitted_ids,
                             orphaned_ids, trusted_ids, logger, label):
        """`_settle` 的 post-commit 包裝。回傳有沒有結成功（供測試判別）。"""
        # 🔴 進場 flush 明確做在 try **之外**（#792 二輪覆核 item9(a)）：
        # `cr.savepoint()` 預設會先 flush，而那個 flush 失敗代表**交易已經壞掉**，
        # 不是「記帳失敗」。包在 try 裡會把它吞成記帳失敗，呼叫端於是以為只是帳
        # 沒記到、繼續往下寫欄位——寫進一個已經 abort 的交易。
        self.env.flush_all()
        try:
            with self.env.cr.savepoint(flush=False):
                self._settle(anchor_model, anchor_id, submitted_ids,
                             orphaned_ids, trusted_ids)
            return True
        except OperationalError:
            raise
        except Exception:
            logger.exception(
                "%s: Cloudflare Access sync SUCCEEDED but settling the reap "
                "ledger failed — the ledger may be out of date; the next "
                "successful sync re-sends whatever is still on it", label)
            return False

    @api.model
    def _settle_teardown_after_commit(self, anchor_model, anchor_id, app_id,
                                      submitted_policy_ids,
                                      orphaned_policy_ids, logger, label):
        """`_settle_teardown` 的 post-commit 包裝。回傳有沒有結成功。"""
        # 進場 flush 移出 try，理由見 `_settle_after_commit`（item9(a)）。
        self.env.flush_all()
        try:
            with self.env.cr.savepoint(flush=False):
                self._settle_teardown(anchor_model, anchor_id, app_id,
                                      submitted_policy_ids,
                                      orphaned_policy_ids)
            return True
        except OperationalError:
            raise
        except Exception:
            logger.exception(
                "%s: CF teardown SUCCEEDED but booking the reap ledger failed "
                "— some cf id(s) may be untracked", label)
            return False

    # ------------------------------------------------------------------ #
    #  #784：dead anchor 的回收者（這條帳唯一會被走到的出口）              #
    # ------------------------------------------------------------------ #

    @api.model
    def _cron_reap_dead_anchors(self, limit=200, scan_batch=1000,
                                max_scan=20000):
        """把「錨點列已經不存在」的帳送去 operator 刪掉。

        ## 為什麼非有這條不可

        #528 的消費路徑是**錨點自己的下一次成功 sync**（`_build_existing_policy_
        ids()` 把帳併進 `existing_policy_ids`）。那條路徑有一個沒寫出來的前提：
        **錨點還活著**。錨點被刪掉之後沒有任何 sync 會再為它跑，那筆帳就永遠沒
        有人處理——而「刪整條 route」正是丟最多 id 的那條路徑（#784）。所以刪除
        路徑落的帳必須有一個**不依賴錨點**的回收者，否則落帳只是把「靜默丟失」
        換成「靜默堆積」，一樣是一次性機制（#528 的根因）。

        ## 誰、何時、憑什麼

        * **誰**：本方法，由 `data/cf_access_reap_cron.xml` 的 ir.cron 驅動。
        * **何時**：每小時一次。頻率的判準是「孤兒在 CF 上多留一小時的成本」而
          不是延遲——待回收物件不影響任何線上流量，只是帳號級的殘留與計數。
        * **憑什麼**：operator 的 teardown 分支（`enabled=False`）**只吃 id**，
          不碰 tunnel、不需要 hostname，而且 CF 的 DELETE 把 404 當成功
          （`extra/paas-operator/src/services/cloudflare.py`）⇒ 用一份純 id 的
          payload 就刪得掉，重送也永遠安全。

        ## 只收 dead anchor，是刻意的

        錨點還活著的帳**不歸這裡管**：它有自己的消費路徑（下一次 sync），而且
        它的 app 可能還在線上服務——這條 cron 若對活錨點送 `existing_app_id`
        就會把一顆**正在保護租戶服務的** Access app 拆掉。「錨點不存在」是這個
        危險動作唯一安全的前提，所以它是硬條件而不是最佳化。

        ## 🔴 為什麼是 cursor 掃描而不是 `search([], limit=N)`（#791 覆核 F1）

        第一版是「撈前 N 筆再判斷錨點死了沒」。那個形狀會**自己把自己堵死**：
        兩類列永遠消不掉（錨點活著但已進永久終態、`anchor_model` 已不存在），
        而 `_order = 'id'` 讓它們永遠佔住最小的 id ⇒ 累積到 N 筆之後，每一輪
        撈到的都是同一批不可回收列，真正該收的帳一次都輪不到。更糟的是那個
        狀態與「今天沒事做」在輸出上完全一樣（回 0、零 log）——回收機制癱瘓而
        沒有任何訊號。

        所以窗口不能套在判定**之前**：這裡改成沿著 id 游標一批批掃過去，
        `limit` 限的是「這一輪**對 operator 發出幾次** dead-anchor teardown」
        （工作量），**成功與失敗都算**——只算成功的話，operator 持續失敗時這一輪
        會一路打到 `max_scan` 為止，變成失敗時打得比成功時還多（#792 二輪覆核
        F1(a)）。`max_scan` 只是防跑不完的上界，而且**踩到它一定出聲**——被截斷
        卻安靜等於謊報「都收完了」。
        """
        Reap = self.sudo()
        stale_before = fields.Datetime.now() - timedelta(
            days=_STUCK_PENDING_DAYS)
        last_id = 0
        # `attempted` ＝ 這一輪對 operator 發出的 dead-anchor teardown 次數（成敗
        # 都算），也就是 `limit` 真正管的那個量；`reaped` 只是其中成功的部分。
        scanned = attempted = reaped = failed_anchors = 0
        live_anchors = set()
        unknown_models = {}
        stuck = []
        truncated = False
        no_client = False
        client = None
        while attempted < limit:
            # (b) `order='id'` 顯式帶上，不靠 `_order` 的預設。游標式掃描的正確性
            # 完全建立在「這一批的最大 id 之後還有沒有列」上——日後有人改 `_order`
            # 就會讓游標原地空轉，而症狀是「每輪都回 0」，與帳本真的空掉不可區分。
            rows = Reap.search([('id', '>', last_id)], limit=scan_batch,
                               order='id')
            if not rows:
                break  # (e) 空批先 break：帳本剛好整除 scan_batch 時不該報截斷
            last_id = rows[-1].id
            scanned += len(rows)
            by_anchor = {}
            for row in rows:
                by_anchor.setdefault(
                    (row.anchor_model, row.anchor_id), []).append(row)
            for (anchor_model, anchor_id), group in by_anchor.items():
                if anchor_model not in self.env:
                    # model 被拿掉了（重構／解除安裝）。帳留著——這種列沒有任何
                    # 自動處置是對的：連該打哪個 endpoint 都推導不出來。統計起來
                    # 在最後一次出聲，不要每個 anchor 洗一行。
                    unknown_models[anchor_model] = (
                        unknown_models.get(anchor_model, 0) + len(group))
                    continue
                if self.env[anchor_model].sudo().browse(anchor_id).exists():
                    # 活錨點：它自己的下一次 sync 會收，見 docstring。
                    # #791 覆核 F2：但「進了永久終態的活錨點」那一格 sync 永遠
                    # 不會再跑，帳會卡到天荒地老。這裡只**出聲**不動手——判準寫
                    # 錯的代價是拆掉一顆正在保護租戶服務的 Access app，而讓維運
                    # 看見之後把錨點列刪掉，帳自然變成 dead anchor 由本 cron 收。
                    live_anchors.add((anchor_model, anchor_id))
                    stuck.extend(
                        r for r in group
                        if r.create_date and r.create_date < stale_before)
                    continue
                if client is None:
                    client = get_paas_operator_client(self.env)
                    if not client:
                        no_client = True
                        break
                # 🔴 預算算的是**外呼次數**不是成功次數（#792 二輪覆核 F1(a)）。
                # 舊寫法只把成功的計進 limit，於是 operator 持續失敗時這一輪會
                # 一路打到 max_scan 為止——與 docstring 自己宣稱的「limit 限的是
                # 這一輪處理幾個 dead anchor」直接矛盾，而且失敗時打得比成功時多。
                attempted += 1
                if self._reap_dead_anchor(client, anchor_model, anchor_id,
                                          group):
                    reaped += 1
                else:
                    failed_anchors += 1
                if attempted >= limit:
                    break
            if no_client:
                break
            # (e) cap 檢查移到批次處理**之後**：先確定這一批真的處理完了。
            # 🔴 而「是不是真的被截斷」用**實查**不用猜：早一版拿「這一批是滿的」
            # 當代理判準，帳本筆數剛好整除 `scan_batch` 時（最後一批是滿的、但
            # 後面其實沒有了）就會發假截斷警報。訊號通膨與訊號缺席一樣糟，而這
            # 一查只在踩到上界時發生、走的是 id 索引。
            if scanned >= max_scan:
                truncated = bool(
                    Reap.search([('id', '>', last_id)], limit=1, order='id'))
                break

        # (d) 訊號一律在早退之後、回傳之前統一發：舊寫法在「operator 未設定」那
        # 條 `return 0` 早退，把同一輪已經算出來的 unknown_models／stuck 兩段
        # warning 一起吞掉——那正好是「有東西要講卻剛好講不出來」的那一格。
        if unknown_models:
            _logger.warning(
                "[CF_REAP] %d ledger row(s) are anchored on model(s) that no "
                "longer exist and can never be reaped automatically: %s",
                sum(unknown_models.values()),
                ', '.join('%s (%d)' % kv for kv in sorted(
                    unknown_models.items())))
        if stuck:
            _logger.warning(
                "[CF_REAP] %d ledger row(s) have been pending for more than "
                "%d day(s) on anchors that still exist — their only consumer "
                "is the anchor's next successful sync, so an anchor stuck in "
                "a terminal state never settles them. Delete the anchor row "
                "(or force a sync) and this cron will reap them: %s",
                len(stuck), _STUCK_PENDING_DAYS,
                ', '.join('%s#%s:%s' % (r.anchor_model, r.anchor_id,
                                        r.cf_object_id) for r in stuck[:20]))
        if no_client:
            _logger.warning(
                "[CF_REAP] PaaS Operator is not configured — dead-anchor CF "
                "Access objects stay on the ledger (%d row(s) scanned)",
                scanned)
        if truncated:
            _logger.warning(
                "[CF_REAP] scan cap reached (%d rows) before the ledger was "
                "exhausted — %d dead anchor(s) reaped this round, the rest "
                "wait for the next run", scanned, reaped)
        if reaped or failed_anchors:
            _logger.info(
                "[CF_REAP] reaped %d dead anchor(s), %d still failing, %d live "
                "anchor(s) skipped, %d row(s) scanned",
                reaped, failed_anchors, len(live_anchors), scanned)
        return reaped


    @api.model
    def _reap_dead_anchor(self, client, anchor_model, anchor_id, rows):
        """送出一個 dead anchor 的 teardown。回傳「這一輪有沒有全部收乾淨」。

        一顆 app 一次呼叫（正常只有一顆；兩顆以上只會出現在「拆失敗之後又換過
        一次 app id」那種殘留）。policy 掛在第一次呼叫上一起送，沒有 app 時就
        單獨送一次純 policy 的 teardown。
        """
        # 去重（#791 覆核 F8）：表上刻意沒有 UNIQUE（見上方註解），競態下同一顆
        # id 可能有兩列 ⇒ 不去重就會對同一顆 app 多送一趟（無害但白費），而
        # policy 那半本來就有去重、兩半不一致只會讓人以為其中一邊寫錯了。
        app_ids = list(dict.fromkeys(
            r.cf_object_id for r in rows if r.kind == KIND_APP))
        policy_ids = list(dict.fromkeys(
            r.cf_object_id for r in rows if r.kind == KIND_POLICY))
        clean = True
        for idx, app_id in enumerate(app_ids or [None]):
            batch = policy_ids if idx == 0 else []
            try:
                result = client.sync_access_app({
                    'enabled': False,
                    # teardown 分支不讀這三個欄位（operator 只用 id 呼叫 CF 的
                    # DELETE），但 schema 是必填。填成看得懂的來源標記，讓
                    # operator 側的 log 也指得回這筆帳。
                    'canonical_name': 'reap:%s:%s' % (anchor_model, anchor_id),
                    'app_name': 'reap:%s:%s' % (anchor_model, anchor_id),
                    'primary_hostname': '',
                    'protected_hostnames': [],
                    'session_duration': '24h',
                    'login_idp_types': [],
                    'policies': [],
                    'existing_app_id': app_id,
                    'existing_policy_ids': batch,
                })
            except Exception as e:  # noqa: BLE001 — 帳留著，下一輪再試
                _logger.warning(
                    "[CF_REAP] dead-anchor teardown failed for %s#%s "
                    "(app=%s, %d policy): %s — kept on the ledger",
                    anchor_model, anchor_id, app_id, len(batch), e)
                return False
            orphaned = result.get('orphaned_policy_ids') or []
            if orphaned:
                _logger.warning(
                    "[CF_REAP] dead-anchor teardown for %s#%s left %d orphan "
                    "policy(ies): %s — kept on the ledger",
                    anchor_model, anchor_id, len(orphaned), ', '.join(orphaned))
                clean = False
            if not self._settle_teardown_after_commit(
                    anchor_model, anchor_id, app_id, batch, orphaned,
                    _logger, '[CF_REAP] %s#%s' % (anchor_model, anchor_id)):
                clean = False
        if clean:
            _logger.info(
                "[CF_REAP] reaped dead anchor %s#%s (app=%s, %d policy)",
                anchor_model, anchor_id, ', '.join(app_ids) or None,
                len(policy_ids))
        return clean

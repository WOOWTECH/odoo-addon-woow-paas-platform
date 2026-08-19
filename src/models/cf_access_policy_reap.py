"""待回收的 Cloudflare Access reusable policy 帳本（#528）。

## 這張表存在的理由

CF 的 reusable policy **只能經 operator 的 ``existing_policy_ids`` 被刪掉**
（delete-then-recreate 是唯一的刪除通道）。而那份清單是從**尚存的** policy 列
推導出來的 ⇒ 一列被 unlink 之後，它的 ``cf_policy_id`` 就再也進不了那份清單。
`SecurityAccessAccessPolicy.unlink()` 因此在 ``super().unlink()`` 之前把 cf id
撈出來、經 ``sa_unlinked_cf_policy_ids`` context 注入**那一次** re-sync。

#528 打穿的正是「那一次」：三腿的 ``_sync_access_to_operator()`` 各自有一組
liveness gate（SA／SH 是停權狀態、CS 是 ``deleting`` / 沒有 subdomain），gate
一擋，operator 一次都不會被呼叫，而 context 用完即丟、DB 列已經不在
⇒ **那顆 CF policy 從此沒有任何記錄指向它，之後任何一次 re-sync 都不會回收**
（2026-08-02 在 stg 實際漏了兩顆，其中一條是 allow everyone）。

所以帳要**離開 context、落到 DB**：unlink 當下記帳，之後**任何一次成功的**
sync 把帳上的 id 併進 ``existing_policy_ids`` 一起送去刪，operator 回報確實刪掉
了才消帳。sync 失敗或被 gate 擋掉時帳原封不動 —— 帳的壽命不再綁在「當次 sync
有沒有跑」上，這就是本表的全部價值。

## 兩個刻意的設計決定

1. **只記從列上讀到的 ``cf_policy_id``，永遠不記 context 傳進來的值。**
   ``sa_unlinked_cf_policy_ids`` 是一把裸 context key，而 ``/web/dataset/call_kw``
   把呼叫端的 context 原封不動交給 ORM（#522／#536 已就同族 key 實測過）
   ⇒ 租戶在 RPC payload 多塞一個 key 就能決定 operator 要刪哪些 CF policy，
   而 CF reusable policy 是**帳號級**物件。把那個值寫進 DB 等於把一次性的偽造
   升級成持久的偽造，所以 ``_record()`` 的呼叫端只有 policy 的 ``unlink()``，
   餵進來的一律是列上真實存在過的值。
   （既有那把 context key 本身的偽造面不在 #528 範圍內，見 PR 說明。）

2. **租戶對本表零 ACL row（只給 ``base.group_system``），平台碼一律 ``sudo()``。**
   與 ``cloud_service_secret`` / ``reconcile_report`` 同型。這比在欄位上加一把
   sentinel guard 更強：租戶連 model 都碰不到，不必再維護第四把 sentinel，也就
   沒有「sentinel 命名互為子字串害 repo-lint 說謊」那類問題
   （見 ``src/models/smart_home_route.py`` 的命名禁令）。

## 錨點為什麼是 ``(model, id)`` 而不是三支 Many2one

三個錨點是三個不同 model（``security_access_route`` / ``cloud_service`` /
``smart_home_route``），Many2one 版本要把 policy 那套 exclusive-arc 機制再抄一遍。
更關鍵的是**壽命**：Many2one 帶 ``ondelete='cascade'`` 會讓帳隨錨點一起消失，
而「錨點被刪掉、帳還沒回收」正是最不能靜默丟掉的那一格。純量 pair 讓那筆帳留下來
（reconcile 報表也會從 CF 那側把同一顆 id 列成孤兒候選，兩個信源對得起來）。
"""
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

#: policy unlink 注入 doomed id 的 context key —— 三腿共用的既有通道。
#: 這裡集中一份是為了讓「帳上的」與「本次注入的」在同一個地方聯集，
#: 三腿各自再 `context.get()` 一次就會漂。
DOOMED_CTX_KEY = 'sa_unlinked_cf_policy_ids'


class CfAccessPolicyReap(models.Model):
    """一列 ＝ 一顆「DB 已刪、CF 尚未確認回收」的 reusable policy。"""

    _name = 'woow_paas_platform.cf_access_policy_reap'
    _description = 'Pending Cloudflare Access Policy Reap'
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
    cf_policy_id = fields.Char(
        string='CF Policy ID', required=True, index=True,
        help='Cloudflare reusable policy id（待回收）。',
    )

    _sql_constraints = [
        (
            'cf_access_policy_reap_uniq',
            'UNIQUE(anchor_model, anchor_id, cf_policy_id)',
            'The same Cloudflare policy id can only be pending once per anchor.',
        ),
    ]

    # ------------------------------------------------------------------ #
    #  記帳 / 讀帳 / 消帳（三腿共用的單一裁決處）                          #
    # ------------------------------------------------------------------ #

    @api.model
    def _record(self, anchor, cf_policy_ids):
        """把 doomed id 記進帳（同一錨點同一 id 只記一次）。

        ⚠ ``cf_policy_ids`` 必須來自**被刪列上讀到的** ``cf_policy_id``。
        不要餵 context 進來，理由見模組 docstring 的設計決定 1。
        """
        if not anchor:
            return self.browse()
        return self._record_ids(anchor._name, anchor.id, cf_policy_ids)

    @api.model
    def _record_ids(self, anchor_model, anchor_id, cf_policy_ids):
        """``_record`` 的純值版：錨點列可能已經不在了（teardown 路徑）。"""
        if not anchor_model or not anchor_id:
            return self.browse()
        wanted = [i for i in (cf_policy_ids or []) if i]
        if not wanted:
            return self.browse()
        known = set(self._pending_ids(anchor_model, anchor_id))
        vals_list = [{
            'anchor_model': anchor_model,
            'anchor_id': anchor_id,
            'cf_policy_id': cf_id,
        } for cf_id in dict.fromkeys(wanted) if cf_id not in known]
        if not vals_list:
            return self.browse()
        return self.sudo().create(vals_list)

    @api.model
    def _pending_ids(self, anchor_model, anchor_id):
        """該錨點帳上還沒確認回收的 cf policy id（FIFO）。

        取 ``(model, id)`` 而不是 record：錨點列被刪掉之後仍要查得到帳。
        """
        if not anchor_model or not anchor_id:
            return []
        rows = self.sudo().search([
            ('anchor_model', '=', anchor_model),
            ('anchor_id', '=', anchor_id),
        ])
        return [r.cf_policy_id for r in rows if r.cf_policy_id]

    @api.model
    def _pending_for(self, anchor):
        """帳上的 ∪ 本次 context 注入的 doomed id。

        兩者聯集而不是二選一：unlink 當下兩邊都有（帳是剛寫的、context 是既有
        通道），但「手動帶 context 強制收斂」這條路徑（#528 留言記錄的那次存量
        清理就是這樣收的）帳上是空的，只有 context 有值。
        """
        if not anchor:
            return []
        ids = self._pending_ids(anchor._name, anchor.id)
        for cf_id in anchor.env.context.get(DOOMED_CTX_KEY) or []:
            if cf_id and cf_id not in ids:
                ids.append(cf_id)
        return ids

    @api.model
    def _settle(self, anchor_model, anchor_id, submitted_ids, orphaned_ids):
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
        """
        still_orphaned = [i for i in (orphaned_ids or []) if i]
        submitted = [i for i in (submitted_ids or []) if i]
        orphan_set = set(still_orphaned)
        reaped = [i for i in submitted if i not in orphan_set]
        if reaped:
            rows = self.sudo().search([
                ('anchor_model', '=', anchor_model),
                ('anchor_id', '=', anchor_id),
                ('cf_policy_id', 'in', reaped),
            ])
            if rows:
                rows.unlink()
        if still_orphaned:
            self._record_ids(anchor_model, anchor_id, still_orphaned)

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

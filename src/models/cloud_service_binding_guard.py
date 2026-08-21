"""#698 — CloudService 綁定授權守衛（`project` / `task` 共用的唯一實作）。

## 為什麼守線只能落在 model 層

`project.cloud_service_ids` 與 `task.extra_cloud_service_ids` 是 Many2many。
Odoo 的 ``Many2many.write_real``（``odoo/fields.py``）對 x2many 指令的處理：

- ``(0)`` → ``comodel.create()``、``(1)`` → ``comodel.write()``、
  ``(2)`` → ``comodel.unlink()`` ⇒ 這三個**會**觸發 cloud_service 的權限檢查
  （#676 的 `cloud_service_rule_owner_admin_manage` 擋得到）。
- ``(3)`` / ``(4)`` / ``(5)`` / ``(6)`` **只改 relation 表**（直接 SQL
  ``INSERT`` / ``DELETE FROM project_cloud_service_rel``），程式碼路徑上
  **沒有任何 comodel 權限檢查——連 ``read`` 都不檢查**。

⇒ 對 comodel 收 ir.rule 對這幾個指令**完全無效**。#698 實測：任何
``base.group_user``（**不需要屬於任何 workspace**）建一個自己的 project、
對它下 ``(4, <別的租戶的 service id>)`` 就綁得上，只要知道那個整數 id。
後果不只是 DB 欄位：``workspace_ids`` 是 `compute_sudo` 的 stored compute
（`Field._setup_attrs` 讓它預設等於 `store`）⇒ 以 superuser 重算、不套 read
rule ⇒ 受害者 workspace 進入 reach 集合，`_reconcile_agent_runtime_reach`
再把它推給 operator，變成 pod egress allowlist ＋ agent ns 的
``reach-ws-*`` label（operator 側零歸屬驗證，見 #703）。

## 形狀：比對「寫入前後的實際綁定」，不解析指令

守衛**刻意不重演** x2many 的指令語意。本 repo 有明確教訓（#657，同一行修了
兩次）：逐一重演框架的解析規則，就是會漏掉下一個形態。這裡改成
**snapshot before → `super()` → snapshot after → 比對實際差異**：

- ``(4)`` / ``(6)`` / ``(0)`` 的新增、``(3)`` / ``(5)`` / ``(6)`` / ``(2)``
  的移除，一律以「relation 實際變成什麼」被看見；
- 未來多出來的指令形態不需要回頭改這裡。

**比對範圍＝呼叫者這次指名的載體欄位**（``_cs_binding_diff_fields``），不是
「record 上全部的載體欄位」。差別只在 create 側看得出來，但很致命：
``task.primary_cloud_service_id`` 是 compute 導出的，全比對會把「系統從
project 既有合法綁定算出來的值」當成呼叫者新增的綁定（#705 F2）。而閘門本身
看的是**框架解析後**的 vals（``_add_missing_default_values``），否則
``default_<field>`` context ／ ``ir.default`` 整族來源都繞得過去（#705 F1）。

⚠ create 側的「指名」是**逐筆**的（#716 ①）。``@api.model_create_multi``
收的是 ``vals_list``，舊實作對整個 list 取 key 的**聯集**再套回**整批**
records ⇒ 同一次 RPC 裡「別筆指名了載體欄位」會讓「這筆沒指名」的 compute
導出值也被送去授權，F2 的 false positive 原樣復發在批次路徑上。授權範圍
必須跟著 vals 走，不跟著 batch 走。

## create 與 write 的判準刻意不同（#715）

- **create**：呼叫者只為**自己指名的**載體值負責。沒有「之前的綁定」可被
  破壞，系統從 project 既有合法綁定導出的值不算他綁的（F2）。
- **write**：改的是**既有**綁定，所以**實際發生的變化**都要授權——包括經由
  非載體欄位間接觸發重算的那些（``_cs_binding_write_triggers``）。

這個不對稱是本檔最容易被「統一一下」改壞的地方，兩個方向各有一次實測：
把 write 的觸發器搬到 create ⇒ F2 復發（每一次 task create 都指名
``project_id``）；把 write 收成只看指名欄位 ⇒ #715 的 move 旁路。

代價是「先執行再檢查」，靠 ``AccessError`` 讓整個交易回滾——與
``_reconcile_agent_runtime_reach`` 的 GRANT fail-loud 同一個模式。守衛跑在
``super().write()`` 之內，所以 ``ProjectProject.write`` 那層的 operator 推送
（在 ``super()`` **之後**）不會被執行到。

## 兩個方向都守

新增（把別人的 service 綁進來，跨租戶提權）與**移除**（#698 E7：workspace
的 guest 解掉某 project 的最後一個關聯 ⇒ `project_rule_workspace_member`
從 bound 分支翻到 unbound 分支 ⇒ 整個 workspace 連 admin 都讀不到那個
project，一般路徑救不回）都套同一個判準。只守一半等於沒守。

## 界線

owner／admin（與 #676 一致，Eugene 拍板）。**guest／user 連自己 workspace
的 service 也不能綁**。判準走 `workspace.check_user_access(user,
required_role=ROLE_ADMIN)`（`ROLE_HIERARCHY` ⇒ admin 與 owner 通過），不自己
寫第二套 role 比較——兩套判準遲早會漂。

⚠ 上面那句界線的**精確範圍**（#715 覆核指出舊版檔頭在 move 這條路上為假，
這裡逐字寫清楚，不要再壓縮回一句話）：

- 守的是**呼叫者造成的綁定變化**——不論他是直接指名載體欄位，還是換掉
  `task.project_id` 讓 stored compute 改綁（#715）。
- **不**守「create 時由系統從 project 既有合法綁定導出的值」。同一個 guest
  在一個已合法綁定的 project 裡建 task，primary 仍會被算成該 project 的
  第一顆 service，而且**不需要**授權（#705 F2 的修法，刻意）。
  ⇒ 界線不是「guest 名下不會出現任何 service 關聯」，是「guest 動不了
  既有綁定，也塞不進自己挑的 service」。

``self.env.su`` 豁免：seed / migration / cron / controller 的 sudo 產品路徑
（例如 `ai_assistant._create_project`）照常。本守衛堵的是 ORM 直呼那條旁路
（`/web/dataset/call_kw`）。
"""
from odoo import api, models


class CloudServiceBindingGuard(models.AbstractModel):
    """繼承本 mixin 的 model，其 ``_cs_binding_fields`` 所列欄位受守衛保護。

    新增第五個入口時**只要把欄位名加進那個 tuple**——不需要（也不應該）在
    該 model 的 ``write`` / ``create`` 裡另外呼叫守衛。#698 一口氣找到四個
    入口，逐一手動掛的漏接機率是 100%，所以攔截點放在 mixin 自己的
    ``write`` / ``create`` 上。
    """
    _name = 'woow_paas_platform.cloud_service_binding_guard'
    _description = 'CloudService Binding Authorization Guard (#698)'

    # 綁定的**載體**欄位：snapshot 與 diff 都看這些。由繼承者覆寫；空 tuple
    # ⇒ 守衛完全不啟動（mixin 本身零行為）。
    _cs_binding_fields = ()
    # 額外會**觸發** snapshot、但自己不參與 diff 的欄位（別名／投影）。
    #
    # `project.cloud_service_id` 屬於這一類：它是 `cloud_service_ids[:1]` 的
    # stored compute，寫它會由 `_inverse_cloud_service_id` 轉成一個內層
    # ``write({'cloud_service_ids': [(6, 0, [id])]})``，真正的綁定變化在載體
    # 欄位上就看得見。把它放進 diff 反而會製造偽陽性——多綁一個 service 會
    # 讓 primary 位移，看起來像「移除了原本那個 primary」，但 relation 裡
    # 它其實還在。
    _cs_binding_triggers = ()
    # 只在 **write** 側觸發全比對的欄位（create 側刻意不吃，見下）。
    #
    # `task.project_id` 屬於這一類：它自己不是載體，但
    # `primary_cloud_service_id` 是 `@api.depends('project_id.
    # cloud_service_ids')` 的 stored compute ⇒ 換 project 會讓綁定**實際**
    # 改變。#715 實測：guest 把 task 從 P1 搬到 P2，primary 靜默從 svc_a
    # 換成 svc_a2，而守衛連 snapshot 都沒做（呼叫者一個載體欄位都沒指名）。
    #
    # 🔴 **不可以搬進 `_cs_binding_triggers`**：那個 tuple 兩側都吃，而
    # **每一次 task create 都會指名 `project_id`** ⇒ create 側退回全比對
    # ⇒ #705 F2 原樣復活（在已合法綁定的 project 裡建 task 會被要求
    # owner／admin）。兩側判準為什麼本來就不同，見檔頭「create 與 write 的
    # 判準刻意不同」。
    _cs_binding_write_triggers = ()

    # ------------------------------------------------------------------ #
    #  snapshot / diff                                                     #
    # ------------------------------------------------------------------ #
    def _cs_binding_diff_fields(self, keys, triggers=None):
        """這次寫入要比對**哪幾個載體欄位**（回傳 tuple，空 ⇒ 守衛不啟動）。

        只比對「呼叫者這次**指名要改**的」載體欄位。這一格同時撐起兩件事：

        1. **compute 導出的綁定不算呼叫者新增的**（#705 F2）。
           ``task.primary_cloud_service_id`` 是
           ``@api.depends('project_id.cloud_service_ids')`` 的 stored compute：
           在一個**已合法綁定**的 project 裡建 task，它會被自動算成該 project
           的第一顆 service。呼叫者沒有指名它 ⇒ 不是他綁的 ⇒ 不該要求他是
           owner／admin。舊實作在 create 側把 ``before`` 當成空的、又比對
           **全部**載體欄位，於是每一個這樣的 task create 都被拒絕。
        2. **觸發器欄位**（``project.cloud_service_id``）的效果落在載體上而不是
           自己身上 ⇒ 只要 vals 提到觸發器就退回「全部載體都比對」。

        ⚠ 這**不是**在解析 x2many 指令語意（#657 的陷阱）——它只問「vals 裡有
        沒有這個 key」，授權判斷仍然一律看 ``super()`` 之後的實際 diff。

        ``triggers`` 由呼叫端指定，因為 create 與 write 吃的不是同一組
        （#715）：create 只吃 ``_cs_binding_triggers``，write 另外吃
        ``_cs_binding_write_triggers``。預設值＝create 側那組，所以任何忘了
        傳的呼叫端拿到的是**較窄**的那一組，不會意外放大 create 的比對面。
        """
        keys = set(keys)
        if triggers is None:
            triggers = self._cs_binding_triggers
        if keys & set(triggers):
            return tuple(self._cs_binding_fields)
        return tuple(f for f in self._cs_binding_fields if f in keys)

    def _cs_binding_snapshot(self, fnames):
        """回傳 ``{record_id: {fname: frozenset(service_ids)}}``（實際落地值）。

        ``flush`` + ``invalidate`` 是必要的：欄位值可能還在待重算佇列裡，而
        cache **不區分 su**——先前被 read rule 過濾過的值會命中 cache，讓
        snapshot 少算幾筆，diff 就會把「本來就在的」誤判成新增。

        讀完之後再 invalidate 一次：上面那次是以 ``sudo()`` 讀的，未過濾的
        id 集合若留在 cache，呼叫者後續讀同一個欄位就會拿到不該看見的
        record（cache 不分 su 的另一面）。
        """
        fnames = tuple(fnames)
        if not fnames or not self:
            return {}
        self.flush_recordset(fnames)
        self.invalidate_recordset(fnames)
        snapshot = {
            record.id: {f: frozenset(record[f].ids) for f in fnames}
            for record in self.sudo()
        }
        self.invalidate_recordset(fnames)
        return snapshot

    def _cs_binding_assert(self, before, fnames):
        """比對 ``before`` 與現況，對**新增與移除**的 service 各自要求授權。"""
        fnames = tuple(fnames)
        if not fnames or self.env.su:
            return
        after = self._cs_binding_snapshot(fnames)
        touched = set()
        for record_id, fields_after in after.items():
            fields_before = before.get(record_id, {})
            for fname, ids_after in fields_after.items():
                ids_before = fields_before.get(fname, frozenset())
                touched |= (ids_after ^ ids_before)   # 新增 ∪ 移除
        if touched:
            self.env['woow_paas_platform.cloud_service'] \
                ._assert_binding_authorized(touched)

    # ------------------------------------------------------------------ #
    #  攔截點                                                              #
    # ------------------------------------------------------------------ #
    @api.model_create_multi
    def create(self, vals_list):
        """create 也要守。

        #698 實測「create 一步到位帶 ``(4, victim_id)``」在修復前**看起來**被擋
        了——但那是 Odoo 對 create 的 ir.rule 檢查發生在寫入**之後**（新記錄
        必須落在 domain 內）的副作用，不是任何人設計的防線：只要換一條
        `perm_create` 較寬的 rule、或該路徑改走 sudo，它就靜默打開。

        🔴 **閘門看的必須是框架解析後的 vals，不是呼叫者送進來的 raw vals**
        （#705 F1）。``default_<field>`` context 與 ``ir.default`` 是在
        ``BaseModel.create`` **內部**（``_prepare_create_values`` →
        ``_add_missing_default_values``）才被展開成欄位值的，也就是在下面那句
        ``super().create()`` **底下**。看 raw vals 的閘門在那一刻 key 還不存在
        ⇒ 守衛整道不執行 ⇒ 綁定照樣落地。實測（#705 F1）：把 payload 從
        ``args`` 搬到 ``kwargs.context`` 的 ``default_cloud_service_ids``，
        #698 的主攻擊完整復原，三個 model 都中。

        ⚠ 這裡呼叫 ``self._add_missing_default_values(vals)`` 是**刻意不自己
        重演解析規則**：``default_`` context、``ir.default``、欄位 ``default=``
        是三個來源，逐一重演就是會漏掉下一個（#657，同一行修了兩次）。代價是
        這一輪 ``default_get`` 會被算兩次（我們一次、``super().create()`` 內
        一次）；我們不把解析結果餵回 ``super()``，因為那會改動 create 真正收到
        的 vals，副作用面比多算一次 ``default_get`` 大得多。

        🔴 **比對範圍逐筆決定，不對整個 ``vals_list`` 取聯集**（#716 ①）。
        舊實作把聯集套回**整批** records，於是同一次 RPC 裡只要有**任何一筆**
        指名了載體欄位，**其他每一筆**的 compute 導出值也會被送去授權——
        #705 F2 的 false positive 原樣復發在批次路徑上。實測 payload：
        ``[{'name': 't1', 'project_id': p, 'primary_cloud_service_id': False},
        {'name': 't2', 'project_id': p}]`` ⇒ guest 建不了 ``t2``。

        ``super().create(vals_list)`` 回傳的順序與 ``vals_list`` 一致
        （``BaseModel.create`` 依序組 ``data_list`` → ``BaseModel._create``
        依 INSERT 順序累加 ``ids`` → ``browse(ids)`` 保序），所以下面的
        ``zip`` 對得起來；同一組 fnames 的 records 併成一批送檢，省掉逐筆的
        flush／invalidate。
        """
        per_record_fnames = [
            self._cs_binding_diff_fields(self._add_missing_default_values(vals))
            for vals in vals_list
        ]
        records = super().create(vals_list)
        batches = {}
        for record, fnames in zip(records, per_record_fnames):
            if fnames:
                batches.setdefault(fnames, []).append(record.id)
        for fnames, ids in batches.items():
            records.browse(ids)._cs_binding_assert({}, fnames)
        return records

    def write(self, vals):
        """write 側不受 ``default_`` 影響（default 只在 create 套用）。

        觸發器多吃一組 ``_cs_binding_write_triggers``（#715）：改的是**既有**
        綁定，所以連「非載體欄位間接觸發 stored compute 重算」也算一次綁定
        變更。create 側不吃那一組，理由見檔頭。
        """
        fnames = self._cs_binding_diff_fields(
            vals,
            tuple(self._cs_binding_triggers) + tuple(self._cs_binding_write_triggers),
        )
        before = self._cs_binding_snapshot(fnames) if fnames else None
        res = super().write(vals)
        if fnames:
            self._cs_binding_assert(before, fnames)
        return res

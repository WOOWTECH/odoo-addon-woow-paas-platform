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

    # ------------------------------------------------------------------ #
    #  snapshot / diff                                                     #
    # ------------------------------------------------------------------ #
    def _cs_binding_snapshot(self):
        """回傳 ``{record_id: {fname: frozenset(service_ids)}}``（實際落地值）。

        ``flush`` + ``invalidate`` 是必要的：欄位值可能還在待重算佇列裡，而
        cache **不區分 su**——先前被 read rule 過濾過的值會命中 cache，讓
        snapshot 少算幾筆，diff 就會把「本來就在的」誤判成新增。

        讀完之後再 invalidate 一次：上面那次是以 ``sudo()`` 讀的，未過濾的
        id 集合若留在 cache，呼叫者後續讀同一個欄位就會拿到不該看見的
        record（cache 不分 su 的另一面）。
        """
        fnames = tuple(self._cs_binding_fields)
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

    def _cs_binding_assert(self, before):
        """比對 ``before`` 與現況，對**新增與移除**的 service 各自要求授權。"""
        fnames = tuple(self._cs_binding_fields)
        if not fnames or self.env.su:
            return
        after = self._cs_binding_snapshot()
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
    def _cs_binding_in(self, keys):
        """vals 是否觸及受保護欄位。

        只用 key 交集決定「要不要 snapshot」，**不用它決定授權**——授權一律
        看實際 diff。間接路徑也蓋得到：`project.cloud_service_id` 的 inverse
        會發出一個內層 ``write({'cloud_service_ids': ...})``，那一層自己會被
        這個攔截點抓到。
        """
        return bool(set(keys) & (set(self._cs_binding_fields)
                                 | set(self._cs_binding_triggers)))

    @api.model_create_multi
    def create(self, vals_list):
        """create 也要守。

        #698 實測「create 一步到位帶 ``(4, victim_id)``」在修復前**看起來**被擋
        了——但那是 Odoo 對 create 的 ir.rule 檢查發生在寫入**之後**（新記錄
        必須落在 domain 內）的副作用，不是任何人設計的防線：只要換一條
        `perm_create` 較寬的 rule、或該路徑改走 sudo，它就靜默打開。
        """
        records = super().create(vals_list)
        if any(self._cs_binding_in(vals) for vals in vals_list):
            records._cs_binding_assert({})
        return records

    def write(self, vals):
        guarded = self._cs_binding_in(vals)
        before = self._cs_binding_snapshot() if guarded else None
        res = super().write(vals)
        if guarded:
            self._cs_binding_assert(before)
        return res

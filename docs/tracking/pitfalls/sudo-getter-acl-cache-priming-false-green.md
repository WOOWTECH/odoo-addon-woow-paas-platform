# Pitfall：sudo getter 的 ACL 測試——sandbox admin 群組 + ORM cache priming 雙重假綠

> **一句話**：測「model method 內 `self.sudo().<field>` getter」類 ACL fix 時有兩個獨立的假綠陷阱：① sandbox / `default_user_template` 會給新建測試使用者灌 admin 群組 → 「受限 owner」根本不受限，斷言 no-op；② admin context 先讀過的欄位值留在 ORM cache → owner env 讀時**命中 cache 直接回值、跳過 ACL 檢查** → 就算 sudo fix 不存在測試照樣綠。兩個陷阱疊加時，pre-fix 也全綠——測試等於沒測到 fix。
>
> **來源**：`docs/plans/2026-Q3/2026-07-09-smart-home-mcp-platform.md`（與 spec 的差異段＋測試 docstring 段）(extracted 2026-07-19)
> **出處案例**：#270 Smart Home MCP（PR #325）`test_to_dict_mcp_readable_by_restricted_owner`
> **相關 pattern**：[[orm-vs-endpoint-acl-test-coverage]]（controller sudo bypass 的 endpoint 側；本篇是 ORM 測試自身的 false-green 側）

## 場景

Model method 內用 `self.sudo()` 讀 ACL-restricted 欄位（getter 型 sudo fix），例：

```python
# src/models/smart_home.py:320-331
def _get_mcp_integration(self):
    self.ensure_one()
    sub = self.sudo().subscription_id   # ← subscription_id 有 ACL，portal user 非 sudo 會 AccessError
    plan = sub.sh_subscription_plan_id if sub else False
    return (plan.mcp_integration or '') if plan else ''
```

要驗證這條 sudo 真的必要（pre-fix 紅／post-fix 綠），天真寫法是「建一個 owner user → `with_user(owner)` 呼叫 → 斷言回傳正確」。這種寫法會被以下兩個陷阱同時弄成假綠。

## 陷阱 ①：sandbox 給測試使用者灌 admin 群組 → 受限 owner 不受限

`res.users.create()` 的預設群組來自 `default_user_template`；sandbox / demo 環境的模板常帶 admin 級群組（如 `sh_subscription` 的 manager group）。結果「受限 owner」天生就有讀 subscription 的權限，非 sudo 讀也不會 AccessError——把 fix 拔掉測試照樣綠。

**修法**：建完 user 後把 groups **硬重設**為只有 `base.group_user`，並反向斷言 manager group 不在其中：

```python
# src/tests/test_smart_home_mcp.py:139-145
owner.groups_id = [(6, 0, [self.env.ref('base.group_user').id])]
manager_group = self.env.ref(
    'sh_subscription.group_manager_sh_subscription', raise_if_not_found=False)
if manager_group:
    self.assertNotIn(manager_group, owner.groups_id)
```

## 陷阱 ②：admin context 先讀過 → owner env 命中 ORM cache 繞過 ACL

Odoo 的 record cache 是 **per-transaction、跨 env 共享**（同一 cursor）。測試 setup 用 admin/sudo 建資料、指派 `subscription_id` 時，欄位值已進 cache；之後 `home.with_user(owner)` 讀同欄位會**命中暖 cache 直接回值**，ACL 檢查根本沒被觸發——非 sudo 讀在 pre-fix 也不會炸。

更隱蔽的變體：即使沒在 setup 讀過，**經 `to_dict()` 間接測 getter 也會自我 priming**——sibling helper（如 `_get_subscription_info()`）先 `self.sudo()` 讀了 `subscription_id`，把值灌進 cache，輪到待測 getter 的非 sudo 讀時已是暖 cache（見 `src/tests/test_smart_home_mcp.py:165-168` 註解）。

**修法**：`self.env.invalidate_all()` 清空 cache 後**直呼 getter**（不透過 to_dict 之類的複合入口）：

```python
# src/tests/test_smart_home_mcp.py:169-173
self.env.invalidate_all()
self.assertEqual(owner_home._get_mcp_integration(), 'ha_mcp_tools')
# 附帶 wire 級：整個 to_dict() 對受限 owner 也不拋
self.env.invalidate_all()
self.assertEqual(owner_home.to_dict()['mcp'], 'ha_mcp_tools')
```

**為什麼清 cache 才是貼近真實**：生產環境每個 HTTP request 都從空 cache 開始，portal user 的第一讀必然走 ACL。測試不清 cache 反而製造了真實世界不存在的暖 cache 條件——`invalidate_all()` 不是 hack，是還原真實。

## 判準：pre-fix 紅／post-fix 綠

ACL 測試的唯一實證標準是**把 fix 暫時拔掉（或 checkout pre-fix）跑一次必須紅**，fix 在時綠。只看 post-fix 綠無法排除上述兩個 no-op 路徑。#270 的 `test_to_dict_mcp_readable_by_restricted_owner`（`src/tests/test_smart_home_mcp.py:127`）即以此標準設計，docstring 明寫兩個關鍵手法。

## Prevention checklist

- 建「受限使用者」後：groups 硬重設 `[(6, 0, [base.group_user])]` ＋ 反向斷言敏感 group 不在。
- 斷言前：`env.invalidate_all()` ＋ **直呼**待測 getter（勿經會自我 priming 的複合方法）。
- 交付前：拔掉 fix 跑一次確認紅（pre-fix 紅／post-fix 綠）。
- Review 時看到 `with_user()` ACL 測試沒有 invalidate 也沒有 groups 重設 → 質疑是否假綠。

## See also

- [[orm-vs-endpoint-acl-test-coverage]] — 對偶面：ORM 測試綠 ≠ endpoint 安全（controller `.sudo()` 繞 ir.rule）。本篇則是 ORM 測試**自身**可能假綠的兩個機制；兩篇合起來才是 ACL 測試的完整覆蓋觀。

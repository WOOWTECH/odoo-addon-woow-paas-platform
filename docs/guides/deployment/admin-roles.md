# PaaS Admin Roles & Subscription Management

設定 PaaS 平台 admin 啟用 subscription 後台管理權限的步驟，以及一般使用者
（portal / internal user）的權限說明。

## 概念

`woow_paas_platform` 依賴上游 `sh_subscription` 模組管理訂閱資料。各角色預設權限：

| 角色 | 透過 `/woow` PaaS 入口 | 透過 `/web` 後台直接看 subscription | 透過 RPC (`/web/dataset/call_kw`) 看 subscription |
|------|----------------------|----------------------------------|------------------------------------------------|
| Public（未登入） | ❌ `/woow` 強制登入 | ❌ | ❌ |
| Portal user（一般客戶/租戶） | ✅ 對外 PaaS 流程，依 workspace 角色／訂閱判斷 | ❌ portal 不能進後台 | ✅ 只看自己的（上游 record rule 隔離） |
| Internal user 未加 sh_subscription role | ✅ 完整功能（controller `.sudo()`） | ❌ menu 隱藏（無 ACL） | ❌ AccessError |
| Internal user + `sh_subscription.group_user_sh_subscription` | ✅ 完整功能 | ✅ 看後台 menu | ⚠️ **看得到同 company 內所有人 subscription** |
| Internal user + `sh_subscription.group_manager_sh_subscription` | ✅ 完整功能 | ✅ 看後台 menu | ⚠️ 同上，且可 unlink |

`/woow` 的 Portal 存取由私有 capability/BFF 驗證；不授予原始 PaaS model CRUD、ERP 或系統群組。Project/AI 分享及 HA machine/OAuth 不因網站入口開放而擴權。部署與實測範圍見 [Portal 修復驗證](../../plans/2026-Q4/2026-10-01-portal-paas-access-verification.md)。

## 設計原則

1. **portal user 不疊加 sh_subscription functional group**。上游已直接給
   `base.group_portal` ACL + record rule，portal user 只能看自己的訂閱，
   不需也不該疊加 functional group（疊了會讓他們看到後台 menu，違反 portal 邊界）。
2. **internal user 預設不加 sh_subscription role**。PaaS 自家 controller
   透過 `.sudo()` 操作 subscription，正常 PaaS 操作不需要 role。加上 role
   會讓他在後台/RPC 看到**所有**租戶的 subscription（多租戶後門），慎用。
3. **PaaS 平台 admin 才需要 manager role**。負責跨租戶管理（改 plan 價格、
   強制終止異常訂閱等）的人才加入。

## 部署後啟用步驟

### 1. PaaS 平台 admin 啟用後台 subscription 管理

部署 PaaS 完成後，**手動**把 PaaS 平台維運者（不是租戶 admin）加入
`sh_subscription.group_manager_sh_subscription`：

#### 方法 A：UI 操作

```
Settings → Users & Companies → Users → [選擇 admin 帳號]
  → Other → Subscription → Manager
  → Save
```

#### 方法 B：CLI 指令

```bash
# 在 odoo container 內執行
odoo shell -d <db_name> << 'EOF'
admin = env.ref('base.user_admin')  # 或指定其他 internal user
manager_group = env.ref('sh_subscription.group_manager_sh_subscription')
admin.write({'groups_id': [(4, manager_group.id)]})
env.cr.commit()
EOF
```

啟用後該 user 即可：

- 在 `/web` 後台看到 `Subscription` menu
- 透過 RPC / ORM 直接 CRUD 任一租戶的 subscription
- 在 list view 改 plan 價格、調整訂閱狀態、unlink 訂閱

### 2.（不推薦）給 internal user 加 sh_subscription role

⚠️ **多數情況不需要做這步**。給 internal user 加 `sh_subscription.group_user_sh_subscription`
會讓他**看到同 company 內所有人的訂閱**（包括其他租戶的），等於開全租戶後門。

唯一合理的場景：

- 該 internal user 是 PaaS 平台客服/支援人員，需要查看其他租戶訂閱協助處理問題
- 但仍不該給 manager role（無 unlink 權更安全）

## /web/signup 註冊新 user 的角色

Odoo 18 預設，`/web/signup` 註冊的新 user：

- 透過 config parameter `base.template_portal_user_id`（值為 Portal 樣板使用者的 record ID）複製 group
- 該 template user 只擁有 `base.group_portal`
- → 新註冊的 user **只是 portal user**，符合 PaaS 多租戶設計

### admin 改 template 的影響

`base.template_portal_user_id` 是 admin 可改的 config_parameter。
若 admin 把它改指向某個 internal user，未來 `/web/signup` 註冊的人會
拿到 internal user 權限。**這是 admin 的責任**，PaaS 不對此做防呆
（admin 是 trusted role）。

驗證目前 template 是否正確：

```bash
odoo shell -d <db_name> << 'EOF'
param = env['ir.config_parameter'].sudo().get_param('base.template_portal_user_id')
template_user = env['res.users'].sudo().browse(int(param))
print(f"Template user: {template_user.login}")
print(f"Groups: {template_user.groups_id.mapped('name')}")
EOF
```

預期輸出：

```
Template user: portaltemplate
Groups: ['Portal']
```

## 相關 ACL 測試

`src/tests/test_acl_p9.py` 鎖定上游 `sh_subscription` 的 portal record rule
+ ACL 行為，避免上游升級時 silent break。執行：

```bash
/woow-dev:k8s-dev-sandbox test <sandbox-name> --module woow_paas_platform
# 或手動在 Odoo pod 內執行
NS=dev-sandbox-<slug>; ODOO=$(kubectl get deploy -n $NS -o name | grep odoo | head -1)
kubectl exec -n $NS $ODOO -- odoo --test-enable --test-tags woow_paas_platform \
  --stop-after-init -d woow_<branch>
```

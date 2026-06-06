---
title: Skill 上傳 Pipeline（Phase 2 — 後台 wizard / system skill）設計
category: plans
status: draft
created: 2026-05-30
updated: 2026-05-30
topic: skill-upload-phase2
phase: design
related:
  - docs/plans/2026-Q2/2026-05-30-skill-dual-scope-design.md
  - src/models/skill.py
  - src/views/skill_views.xml
  - src/wizard/change_subscription_plan_wizard.py
  - docs/guides/paas-skill-system/03-skill-design.md
---

# Skill 上傳 Pipeline（Phase 2 — 後台 wizard / system skill）設計

## 背景

Phase 1（dual-scope 地基）已讓 `woow_paas_platform.skill` 支援 system/user scope、pod 載入、ACL、migration、demo seed，但 **system skill 仍只能靠 demo data / ORM 建立**——後台 Skills 頁是 `create="false"` 唯讀。本 Phase 補上**後台上傳入口**，讓平台維運者直接上傳一個 skill bundle 建立 system skill。

對應使用者最初的需求：「在後台 Skills 頁加一個上傳 skill 的 button」。

## 關鍵設計前提（使用者澄清）

> **Odoo 不解析、也不執行 bundle——那是 pod 的事。Odoo 只需「存 bytes」+ 算對 `bundle_sha256`；`name`/`version` 由 admin 在 wizard 直接填。**

因此本設計**不**解壓 tar.gz、**不**讀 SKILL.md、**不**驗證 bundle 結構、**不**依賴任何 parser。Odoo 把 bundle 當**不透明 bytes** 存進 `ir.attachment`，僅計算 sha256。完整解壓 + sha256 驗證 + zip-bomb/path-traversal 防護全在 pod（`paas-agent-runtime` `cache.py` 已實作：fetch 後驗 sha256 才 extract）。

**這帶來最乾淨的安全姿態**：Odoo 端對 bundle 內容零處理 → 零解析攻擊面（無 tar/zip-bomb/path-traversal 風險），僅一個 raw size cap。

> **設計權衡（記錄）**：`name`/`version` 由 admin 手填，因此「DB 的 name/version」與「bundle 內 SKILL.md 的 name/version」可能不一致。這是刻意接受的——pod 載入時以 snapshot 的 `{name,version,sha256}` 為準（Odoo 給的），bundle 內 SKILL.md 的 frontmatter 由 pod runtime 自行解讀；兩者語意分層。admin 有責任填對。

## 已核准的決策

| 決策 | 結果 | 來源 |
|------|------|------|
| 範圍 | **只做後台 wizard（system skill）**；`/woow` 前台 user skill 為獨立後續 phase | user |
| Odoo 是否解析 bundle | **否**——只存 bytes + 算 sha256，不解壓不讀 SKILL.md | user |
| name/version 來源 | **wizard 欄位由 admin 填**（非從 bundle 提取） | user |
| 重複上傳（同 scope+name+version） | **硬錯誤** `UserError`，要求改版號（尊重 bundle immutability + partial unique index） | user |
| 上傳後狀態 | **draft**，admin 再用 Phase 1 既有 Publish button 發布 | user |
| sha256 | 對 **decode 後 raw bytes** 算，須與 pod `resp.read()` sha256 一致 | Phase 1 契約 |
| 上傳者 gate | `base.group_system`（後台 Skills menu 已限此 group） | Phase 1 |
| wizard 落點 | 復用既有 `src/wizard/`（已有 `change_subscription_plan_wizard.py`），非新建目錄 | 勘查 |

## 設計

### ① Wizard model（新增 `src/wizard/skill_upload_wizard.py`）

`TransientModel`，`_name = 'woow_paas_platform.skill_upload_wizard'`，對齊既有 `change_subscription_plan_wizard.py` 的 pattern。

**欄位：**

- `name = fields.Char(string='Skill Name', required=True, help='Slug，須與 SKILL.md 一致')`
- `version = fields.Char(string='Version', required=True, default='0.1.0')`
- `title = fields.Char(string='Display Title')`
- `description = fields.Text(string='Description')`
- `bundle_file = fields.Binary(string='Skill Bundle (.tar.gz)', required=True)`
- `bundle_filename = fields.Char(string='Filename')`（Binary widget 的檔名伴隨欄位）

**`action_upload` method：**

```
self.ensure_one()
1. raw = base64.b64decode(self.bundle_file)
2. size cap：len(raw) > MAX_BUNDLE_SIZE (10 MB) → UserError("Bundle 超過 10 MB 上限")
3. bundle_sha256 = hashlib.sha256(raw).hexdigest()    # 關鍵：對 raw bytes
   bundle_size_kb = -(-len(raw) // 1024)              # ceil division
4. 重複檢查（sudo search）：
     [('scope','=','system'), ('name','=',self.name), ('version','=',self.version)]
   存在 → UserError("System skill '%s' v%s 已存在，請提升版號。" % (name, version))
5. attachment = ir.attachment.sudo().create({
       'name': self.bundle_filename or '%s-%s.tar.gz' % (name, version),
       'datas': self.bundle_file,                     # 已是 base64
       'mimetype': 'application/gzip',
   })
6. skill = skill.sudo().create({
       'scope': 'system', 'project_id': False, 'state': 'draft',
       'name': self.name, 'version': self.version,
       'title': self.title or self.name,
       'description': self.description or '',
       'bundle_sha256': bundle_sha256,
       'bundle_size_kb': bundle_size_kb,
       'bundle_attachment_id': attachment.id,
   })
   # skill.create() hook（Phase 1）自動 anchor attachment + recompute is_latest
7. return {
       'type': 'ir.actions.act_window',
       'res_model': 'woow_paas_platform.skill',
       'res_id': skill.id, 'view_mode': 'form', 'target': 'current',
   }   # 開啟新建 skill 的 form，admin 檢視 + Publish
```

> 步驟 5-6 用 `sudo()`：attachment 受 HC-4 row-level rule 管、`bundle_attachment_id` 是 `groups='base.group_system'` field-mask、`bundle_sha256` required+readonly。上傳者已是 `base.group_system`，sudo 確保 create 路徑乾淨（與 Phase 1 demo seed 同路徑）。`scope='system'` create 經此 sudo 路徑，不受 Phase 1 scope-flip write guard 影響（guard 只擋非 admin/非 sudo 的 write）。

> **不解壓**：步驟全程不 `tarfile.open`、不 `extractall`、不讀任何 member。`bundle_file` 是否為合法 tar.gz 不由 Odoo 把關——pod fetch 後 sha256 不符或 extract 失敗會自然擋下。Odoo 唯一把關是 size cap。

### ② View（`src/views/skill_views.xml`）

- 新增 wizard form view：`name` / `version` / `title` / `description` / `bundle_file`（widget `binary`，filename=`bundle_filename`）+ 說明文字（「上傳 .tar.gz bundle；name/version 須與 bundle 內 SKILL.md 一致；上傳後為 draft，需手動 Publish」）+ footer：`action_upload`（btn-primary）+ Cancel（`special="cancel"`）。
- 新增 `ir.actions.act_window` `action_skill_upload_wizard`（`target='new'`、`res_model='woow_paas_platform.skill_upload_wizard'`），**綁定到 Skills 的 ⚙️ Action 下拉**（contextual action，使用者偏好）：

```xml
<record id="action_skill_upload_wizard" model="ir.actions.act_window">
    <field name="name">上傳 Skill</field>
    <field name="res_model">woow_paas_platform.skill_upload_wizard</field>
    <field name="view_mode">form</field>
    <field name="target">new</field>
    <field name="binding_model_id" ref="model_woow_paas_platform_skill"/>
    <field name="binding_view_types">list,form</field>
</record>
```

`binding_model_id` 讓此 action 出現在 Skills list/form 的 **Action（⚙️）下拉**，不需選取列即可觸發（wizard 不讀 `active_ids`）。`action_skill` list 維持 `create="false"`——上傳一律走 ⚙️ Action → wizard，不開後台手動 create（避免破壞 `bundle_sha256` required / is_latest 不變式）。

### ③ ACL（`src/security/ir.model.access.csv`）

- 新增 wizard access 列：`access_skill_upload_wizard,...,model_woow_paas_platform_skill_upload_wizard,base.group_system,1,1,1,1`（TransientModel 標準，限 admin）。
- 不需動 skill 的 ACL（Phase 1 已備；system skill create 經 wizard 的 sudo 路徑）。

### ④ 註冊（`src/wizard/__init__.py` + `src/__manifest__.py`）

- `src/wizard/__init__.py` 加 `from . import skill_upload_wizard`（`src/__init__.py` 已 `from . import wizard`，不需動）。
- `src/__manifest__.py` `data` 清單：wizard view 併入既有 `views/skill_views.xml`（同檔新增 record），不另開檔——故 manifest **不需改 data 清單**（`views/skill_views.xml` 已註冊）。

### ⑤ Tests（新增 `src/tests/test_skill_upload_wizard.py`，並在 `src/tests/__init__.py` import）

以**真實小 tar.gz**（測試內即時用 `tarfile` 產生）餵 wizard 的 `bundle_file`，驗證：

- `test_upload_creates_draft_system_skill` — 上傳 → skill state=draft、scope=system、project_id 空、name/version/title/description = wizard 填的值、attachment 已 anchor（`res_model='woow_paas_platform.skill'`、`res_id=skill.id`、`public=False`）
- `test_sha256_matches_raw_bytes` — `skill.bundle_sha256 == hashlib.sha256(raw_targz_bytes).hexdigest()`（**與 pod 契約對齊的核心斷言**）
- `test_bundle_size_kb_computed` — `bundle_size_kb` = ceil(len(raw)/1024)
- `test_duplicate_name_version_hard_error` — 同 (system,name,version) 第二次上傳 → `UserError`
- `test_oversize_bundle_rejected` — `bundle_file` decode 後 > 10MB → `UserError`
- `test_returns_form_action_to_new_skill` — `action_upload` 回傳指向新 skill 的 act_window
- `test_upload_does_not_extract_bundle` — 餵一個「非 tar.gz」的隨機 bytes，仍成功建 draft skill（證明 Odoo 不解壓、不驗結構；sha256 照算）

> 最後一條鎖住「Odoo 不解析 bundle」的設計前提——避免未來有人「順手加」解壓驗證而偏離 pod-owns-parsing 的分層。

### ⑥ Doc（`docs/guides/paas-skill-system/03-skill-design.md`）

在「🔄 發布生命週期」後補一小節「後台上傳 system skill」：Skills 頁 → ⚙️ Action → 上傳 Skill → 填 name/version + 選 .tar.gz → 建 draft → 手動 Publish；並註明 Odoo 只存 bytes、name/version 須與 SKILL.md 一致。

## 安全考量

- **零解析攻擊面**：Odoo 不 `tarfile.open` / 不 extract → 無 tar/zip-bomb/path-traversal/symlink 風險。唯一輸入把關是 raw size cap（10MB）。
- **sha256 完整性**：對 raw bytes 算，與 pod `resp.read()` 一致；Odoo→pod 間若 bundle 被竄改，pod sha256 驗證會擋下。
- **bundle pods-only**：attachment 經 HC-4 rule + field-mask 保持只有 pod（經 skill-bundle 端點）能取得 binary；租戶不可經 `/web/content` 下載 system bundle。
- **gate**：wizard ACL `base.group_system`；menu 入口本就限 admin。

## 非目標（Non-goals / YAGNI）

- **`/woow` 前台 user skill 上傳**（OWL section + multipart endpoint + project-role gate）——獨立後續 phase。（2026-06-05：本項由 skill-binding-ux 決議取消 — 後台 wizard 已支援 user-scope，見 `docs/superpowers/specs/2026-06-05-skill-binding-ux-design.md`。）
- **Odoo 端 bundle 解壓 / SKILL.md 解析 / 結構驗證**——pod 負責。
- **從 bundle 自動帶出 name/version**——admin 手填（使用者決策）。
- **後台手動 create skill**（非 wizard 路徑）——維持 `create="false"`。
- **覆蓋既有 skill / auto-bump 版號**——重複一律硬錯誤。
- **上傳即 publish**——一律 draft，手動 Publish。

## 檔案異動清單

| 檔案 | 動作 |
|------|------|
| `src/wizard/skill_upload_wizard.py` | 新增：wizard model + `action_upload` |
| `src/wizard/__init__.py` | 修改：`from . import skill_upload_wizard` |
| `src/views/skill_views.xml` | 修改：wizard form + `action_skill_upload_wizard`（⚙️ binding） |
| `src/security/ir.model.access.csv` | 修改：wizard access 列（base.group_system） |
| `src/tests/test_skill_upload_wizard.py` | 新增：上傳行為 + sha256 契約 + 不解壓測試 |
| `src/tests/__init__.py` | 修改：import 新測試 |
| `docs/guides/paas-skill-system/03-skill-design.md` | 修改：補後台上傳流程小節 |

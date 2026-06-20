# Cloud Odoo Addon 管理指南

本文件說明 Cloud Odoo Addon 從新增、建置到同步的完整流程，以及日常管理的標準作業程序。

## 架構總覽

```
┌─────────────────────────────────────────────────────────────────────┐
│                        paas-odoo-ci repo                            │
│                                                                     │
│  candidate-addons/          scripts/                                │
│  ├── sh_subscription/       ├── generate-catalog.sh                 │
│  ├── web_responsive/        └── generate-requirements.sh            │
│  └── ...                                                            │
│         │                          │                                │
│         ▼                          ▼                                │
│  ┌──────────────┐   ┌──────────────────────────┐                   │
│  │ __manifest__.py│   │ addon-catalog.json        │                   │
│  │ (每個 addon)  │──▶│ (所有 addon metadata)     │                   │
│  └──────────────┘   └──────────────────────────┘                   │
│                              │                                      │
│                     Docker build COPY                                │
│                              ▼                                      │
│                   ┌─────────────────────┐                           │
│                   │ cloud-odoo image    │                           │
│                   │ /all-addons/        │                           │
│                   │  ├── catalog.json   │                           │
│                   │  ├── sh_subscription/│                           │
│                   │  └── ...            │                           │
│                   └─────────────────────┘                           │
└─────────────────────────────────────────────────────────────────────┘
                              │
                         部署到 K8s
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        Odoo (K8s Pod)                               │
│                                                                     │
│  Odoo Backend                                                       │
│  ├── Cloud > Odoo Addons                                            │
│  │   └── [Sync Odoo Addons] 按鈕                                    │
│  │         │                                                        │
│  │         ▼                                                        │
│  │   讀取 /all-addons/catalog.json                                  │
│  │         │                                                        │
│  │         ▼                                                        │
│  │   ┌──────────────────────────────┐                               │
│  │   │ cloud_odoo_addon 資料表       │                               │
│  │   │ (name, technical_name, ...)  │                               │
│  │   └──────────────────────────────┘                               │
│  │                                                                  │
│  └── 使用者部署 Cloud Service 時，從此表選擇要安裝的 addon              │
└─────────────────────────────────────────────────────────────────────┘
```

## 核心概念

| 概念 | 說明 |
|------|------|
| `candidate-addons/` | paas-odoo-ci repo 中存放第三方 addon 原始碼的目錄 |
| `catalog.json` | 由腳本掃描所有 addon 的 `__manifest__.py` 自動產生的 metadata 索引 |
| `cloud_odoo_addon` | Odoo model，儲存可用 addon 清單，供前端 Addon Selector 使用 |
| `source_type` | addon 來源分類：`built_in`（Odoo 內建）、`third_party`、`custom` |

### catalog.json 格式

```json
{
  "sh_subscription": {
    "name": "Subscription Management System",
    "version": "18.0.0.1",
    "depends": ["sale", "account"],
    "category": "Sales",
    "summary": "Manage recurring subscriptions",
    "license": "LGPL-3",
    "application": true,
    "python_dependencies": ["stripe"]
  }
}
```

每個 key 是 addon 的技術名稱（目錄名），value 從 `__manifest__.py` 解析而來。

## SOP：新增第三方 Addon

### 流程圖

```
開始
 │
 ▼
┌──────────────────────┐
│ 1. 取得 addon 原始碼   │
│    確認支援 Odoo 18    │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│ 2. 放入              │
│    candidate-addons/ │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│ 3. 本地驗證           │     NG
│    generate-catalog  │────────┐
│    確認 manifest 格式 │        │
└──────────┬───────────┘        │
           │ OK                 ▼
           ▼              ┌──────────┐
┌──────────────────────┐  │ 修正      │
│ 4. Commit & Push     │  │ manifest │
│    到 paas-odoo-ci   │  └──────────┘
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│ 5. 觸發 CI build     │
│    (push tag 或手動)  │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│ 6. 部署新 image       │
│    到 K8s 環境        │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│ 7. Odoo 後台按        │
│    Sync Odoo Addons  │
└──────────┬───────────┘
           │
           ▼
         完成
```

### 詳細步驟

#### Step 1：取得 addon 原始碼

```bash
# 範例：從 OCA GitHub 下載
git clone https://github.com/OCA/web.git --branch 18.0 --depth 1 /tmp/oca-web

# 只複製需要的 addon
cp -r /tmp/oca-web/web_responsive /path/to/paas-odoo-ci/candidate-addons/
```

**檢查項目：**

- [ ] 確認 addon 支援 Odoo 18（查看 `__manifest__.py` 中的 version 是否以 `18.0` 開頭）
- [ ] 確認授權相容（OCA 通常是 LGPL/AGPL）
- [ ] 確認 `__manifest__.py` 是 pure Python dict literal（不能有 `import` 語句）

#### Step 2：放入 candidate-addons/

```
paas-odoo-ci/
└── candidate-addons/
    ├── sh_subscription/      # 既有
    │   ├── __manifest__.py
    │   ├── __init__.py
    │   └── ...
    └── web_responsive/       # ← 新增
        ├── __manifest__.py
        ├── __init__.py
        └── ...
```

#### Step 3：本地驗證

```bash
cd /path/to/paas-odoo-ci

# 產生 catalog，確認新 addon 有被收錄
bash scripts/generate-catalog.sh
cat addon-catalog.json   # 應該看到新的 web_responsive 條目

# 如果 addon 有 Python 依賴，也檢查 requirements
bash scripts/generate-requirements.sh
cat requirements-cloud.txt
```

> **常見錯誤**：`__manifest__.py` 裡有 `import` 語句會導致 `ast.literal_eval` 失敗。
> 解法：移除 import，改用 pure dict literal。

#### Step 4：Commit & Push

```bash
git add candidate-addons/web_responsive/
git add addon-catalog.json requirements-cloud.txt
git commit -m "feat: add web_responsive addon to cloud-odoo candidates"
git push origin main
```

#### Step 5：觸發 CI Build

```bash
# 方法 A：push tag 觸發
TAG=$(date -u +'%Y%m%d.%H%M')
git tag "cloud-v${TAG}"
git push origin "cloud-v${TAG}"

# 方法 B：透過 Gitea UI 或 tea CLI 手動觸發 workflow_dispatch
```

CI 會執行 `build-cloud-image.yml`，將 `addon-catalog.json` COPY 到 image 的 `/all-addons/catalog.json`。

#### Step 6：部署新 Image

使用新 build 的 image 部署到 K8s 環境（參考 `/woow:deploy-stg` 或 `/woowprd:k8s-dev-sandbox`）。

#### Step 7：在 Odoo 後台同步

1. 登入 Odoo 後台
2. 進入 **Woow PaaS > Cloud > Odoo Addons**
3. 點擊 **Sync Odoo Addons** 按鈕
4. 系統會顯示通知：`N created, N updated, N deactivated (N total)`

## Sync 按鈕的運作細節

`sync_from_catalog` 方法（`src/models/cloud_odoo_addon.py`）執行以下邏輯：

```
讀取 /all-addons/catalog.json
          │
          ▼
    遍歷每筆 addon
          │
    ┌─────┴─────┐
    │           │
    ▼           ▼
  已存在?     不存在?
    │           │
    ▼           ▼
  UPDATE      CREATE
    │           │
    └─────┬─────┘
          │
          ▼
   停用不在 catalog 中的 addon
   (設為 active=False)
          │
          ▼
   解析 depends_on 關聯
   (建立 addon 間的依賴 M2M)
          │
          ▼
   顯示結果通知
```

**Category 映射**：Odoo 原生分類會自動映射到系統定義的 selection 值。例如 `Sales/Sales` → `sales`、`Human Resources` → `hr`。完整映射表見 `src/models/cloud_odoo_addon.py` 頂部的 `_CATEGORY_MAP`。

## 日常管理

### 查看目前 Addon 清單

在 Odoo 後台 **Woow PaaS > Cloud > Odoo Addons** 頁面，list view 預設顯示所有 addon（含 inactive）。

### 設為預設 Addon

編輯 addon 記錄，勾選 **Default** 欄位。標記為 default 的 addon 會在使用者建立新 Cloud Service 時自動勾選。

### 停用 Addon

- **自動停用**：Sync 時，不在 catalog 中的 addon 會自動被停用
- **手動停用**：編輯 addon 記錄，取消勾選 **Active** 欄位

### 移除 Addon

1. 從 `paas-odoo-ci/candidate-addons/` 刪除目錄
2. 重新執行 `generate-catalog.sh`
3. Commit & Push → 觸發 CI build
4. 部署新 image 後，按 Sync → 該 addon 會被自動停用

> **注意**：已經部署的 Cloud Service 如果正在使用該 addon，不會受到影響。停用只影響新部署時的可選清單。

## 注意事項

| 項目 | 說明 |
|------|------|
| `__manifest__.py` 格式 | 必須是 pure Python dict literal，不能有 `import` 語句 |
| Python 依賴 | `external_dependencies.python` 列出的套件會在 build 時自動 `pip install` |
| Odoo 版本 | 需自行確認 addon 支援 Odoo 18 |
| 授權合規 | OCA addon 通常是 LGPL/AGPL，需確認和專案授權相容 |
| 本機開發 | `/all-addons/catalog.json` 在本機不存在，Sync 會直接跳過 |

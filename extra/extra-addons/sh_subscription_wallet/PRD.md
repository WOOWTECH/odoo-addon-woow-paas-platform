# 訂閱錢包 Portal 頁面修改 PRD

## 版本
- 日期：2026-02-10
- 版本：18.0.1.2.0
- 模組：sh_subscription_wallet

---

## Phase 2 需求變更（新增）

### 需求來源
用戶截圖反饋，錢包頁面需要進一步調整。

### 變更項目

#### 1. 錢包頁面 - 移除「更換信用卡」區塊
**原狀**：錢包頁面顯示「綁定的信用卡」區塊，包含更換信用卡按鈕
**新需求**：移除整個「更換信用卡」區塊

#### 2. 錢包頁面 - 顯示訂閱綁定的信用卡列表
**原狀**：顯示 Partner 層級的單張信用卡
**新需求**：顯示用戶所有「進行中」訂閱所綁定的信用卡（可能多張）

**資料來源**：
- 取得用戶所有 `state='in_progress'` 的訂閱
- 每個訂閱可能有不同的綁定信用卡
- 顯示格式：訂閱名稱 + 信用卡末四碼

#### 3. 錢包頁面 - 移除交易記錄區塊
**原狀**：錢包頁面底部有交易記錄
**新需求**：移除交易記錄區塊（已在訂閱詳情頁面顯示）

#### 4. 訂閱詳情頁面 - 信用卡卡片加入「更換信用卡」
**原狀**：訂閱詳情頁面的信用卡卡片只顯示資訊
**新需求**：在信用卡卡片中加入「更換信用卡」按鈕

---

## 技術實作方案

### 資料模型分析

**目前結構**：
```
res.partner
├── ecpay_card_token (char) - ECPay 信用卡 Token
├── ecpay_card_last4 (char) - 信用卡末四碼
├── ecpay_card_expiry (char) - 信用卡有效期
└── ecpay_card_bound_date (datetime) - 綁定日期

sh.subscription.subscription
├── sh_partner_id (many2one -> res.partner)
├── partner_has_credit_card (related -> partner.ecpay_card_token)
├── partner_card_last4 (related -> partner.ecpay_card_last4)
└── partner_card_expiry (related -> partner.ecpay_card_expiry)
```

**結論**：目前信用卡綁定在 Partner 層級，所有訂閱共用同一張卡。
如需支援每個訂閱獨立綁定信用卡，需要：
1. 在 `sh.subscription.subscription` 新增信用卡欄位
2. 修改綁卡流程，將 token 存到訂閱而非 partner

### 簡化方案（維持現有結構）

由於目前所有訂閱共用 Partner 的信用卡：
- 錢包頁面顯示「我的訂閱使用的信用卡」
- 列出所有進行中的訂閱 + 該 Partner 的信用卡
- 「更換信用卡」功能移到訂閱詳情頁面

### 修改的模板

#### 1. portal_wallet (ID: 6929)
**變更內容**：
- 移除「綁定的信用卡」卡片（含更換按鈕）
- 新增「訂閱付款方式」卡片，列出所有進行中訂閱及其信用卡
- 移除交易記錄區塊（如果有）

#### 2. subscription_deduction_records (ID: 6946)
**變更內容**：
- 在信用卡卡片中加入「更換信用卡」按鈕
- 按鈕連結到 `/my/wallet/bind_card?subscription_id=X`

---

## 驗證清單

### Phase 2
- [x] 錢包頁面移除「更換信用卡」區塊
- [x] 錢包頁面顯示訂閱綁定的信用卡列表（訂閱付款方式卡片）
- [x] 錢包頁面移除交易記錄區塊
- [x] 訂閱詳情頁面信用卡卡片加入「更換信用卡」按鈕

### Phase 1（已完成）
- [x] 錢包頁面儲值按鈕連結已更新
- [x] eWallet 商品已發佈到網站
- [x] 訂閱詳情頁面繼承視圖已建立
- [x] 扣款記錄使用 subscription_id 篩選
- [x] 錢包餘額與信用卡資訊卡片已加入訂閱詳情

---

## 相關文件

- 模組路徑：`sh_subscription/sh_subscription_wallet/`
- 模板檔案：`views/portal_templates.xml`
- 資料庫：woowtech
- 測試帳號：REDACTED_CREDENTIALS

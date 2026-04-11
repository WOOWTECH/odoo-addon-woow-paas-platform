# sh_subscription_wallet 測試說明

## 測試結構

```
tests/
├── __init__.py              # 測試模組初始化
├── common.py                # 共用測試基礎類別與資料
├── test_wallet_balance.py   # 錢包點數操作測試 (WB-001 ~ WB-005)
├── test_wallet_transaction.py # 交易記錄測試 (WT-001 ~ WT-004)
├── test_subscription_deduct.py # 訂閱扣款流程測試 (SD-001 ~ SD-006)
└── test_ecpay_token.py      # ECPay Token 扣款測試 (ET-001 ~ ET-004)
```

## 執行測試

### 方法 1: 使用 Odoo Test Runner

```bash
# 安裝模組並執行測試
./odoo-bin -c odoo.conf -d test_db \
    --test-enable --stop-after-init \
    -i sh_subscription_wallet \
    --test-tags wallet

# 執行特定測試標籤
./odoo-bin -c odoo.conf -d test_db \
    --test-enable --stop-after-init \
    -i sh_subscription_wallet \
    --test-tags wallet_balance   # 只執行餘額測試
    --test-tags wallet_transaction   # 只執行交易測試
    --test-tags subscription_deduct  # 只執行扣款測試
    --test-tags ecpay_token      # 只執行 Token 測試
```

### 方法 2: 使用 pytest

```bash
cd sh_subscription
python -m pytest sh_subscription_wallet/tests/ -v
```

## 測試案例

### test_wallet_balance.py
| ID | 名稱 | 說明 |
|---|------|------|
| WB-001 | 新增點數成功 | 驗證 add_wallet_points 正確增加點數 |
| WB-002 | 扣除點數成功 | 驗證 deduct_wallet_points 正確扣除點數 |
| WB-003 | 點數不足扣款 | 驗證餘額不足時只扣可用餘額 |
| WB-004 | 取得 Loyalty Card | 驗證 get_wallet_loyalty_card 方法 |
| WB-005 | 餘額計算一致性 | 驗證 wallet_balance 與 loyalty_card.points 一致 |

### test_wallet_transaction.py
| ID | 名稱 | 說明 |
|---|------|------|
| WT-001 | 建立儲值交易 | 驗證儲值交易記錄建立 |
| WT-002 | 建立扣款交易 | 驗證扣款交易記錄建立 |
| WT-003 | 交易編號自動產生 | 驗證 ir.sequence 自動產生編號 |
| WT-004 | 交易後餘額記錄 | 驗證 balance_after 正確記錄 |

### test_subscription_deduct.py
| ID | 名稱 | 說明 |
|---|------|------|
| SD-001 | 點數足夠完全扣款 | 驗證純點數扣款成功 |
| SD-002 | 點數不足觸發信用卡 | 驗證混合扣款（點數+信用卡） |
| SD-003 | 點數為零信用卡全額 | 驗證純信用卡扣款 |
| SD-004 | 無點數無綁卡失敗 | 驗證扣款失敗處理 |
| SD-005 | 信用卡扣款失敗重試 | 驗證失敗記錄與重試機制 |
| SD-006 | Cron 批次扣款 | 驗證排程批次處理 |

### test_ecpay_token.py
| ID | 名稱 | 說明 |
|---|------|------|
| ET-001 | Token 扣款成功 | 驗證 ECPay Token 扣款成功 |
| ET-002 | Token 扣款失敗 | 驗證 Token 扣款失敗處理 |
| ET-003 | 儲存綁定資訊 | 驗證信用卡綁定資料儲存 |
| ET-004 | 解除綁定 | 驗證 action_unbind_credit_card |

## 測試資料

### 測試客戶
- `test.customer.a@example.com`: 既有客戶，初始餘額 1000 點，已綁卡
- `wallet.test.new@example.com`: 新客戶，餘額 0，未綁卡

### ECPay 測試環境
- 商店代號: 2000132
- 測試卡號: 4311-9522-2222-2222
- 有效期限: 任意未來月份
- 安全碼: 222

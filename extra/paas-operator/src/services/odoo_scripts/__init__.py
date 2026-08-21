"""在租戶 Odoo 容器內執行的 script payload。

⚠ 這個 package 底下的模組**不是 operator 進程的一部分**：它們的原始碼會被讀成
字串，透過 ``kubectl exec ... python3 -c <source>`` 丟進租戶的 Odoo 容器裡跑。
所以每一支都必須遵守下列限制（違反時 operator 的單元測試看不出來，只有真的
部署到租戶才會炸）：

- 只能 import stdlib 與容器內的 ``odoo`` package，不得 import operator 自己的模組
- 不得依賴 ``__file__``（``python3 -c`` 沒有這個變數）
- stdout 的最後一個非空行必須是 JSON（呼叫端只解析那一行）
"""

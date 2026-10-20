# -*- coding: utf-8 -*-
"""測試 fixture：把錢包補到夠用（換方案餘額閘，#1677 A1／AUDITFIX）。

**這個模組刻意不在 `tests/__init__.py` 裡註冊**——它沒有 TestCase，只是給其他測試檔
import 的共用件（同 `smart_home_fixture.py`）。

## 為什麼需要它

三個 change-plan 入口從 AUDITFIX change-plan-insufficient-balance 起，在第一個寫入之前
檢查「錢包＋這次退點」付不付得起新方案首期（含稅）。既有測試在 0 點錢包上換方案，測的是
席數閘、試用結束、規格重套、退點型別等**別的**行為；錢包不足的那條路由
`test_change_plan_balance_gate_auditfix.py` 專門測。這些 fixture 一律在建好訂閱之後補點，
讓它們繼續測原本的東西。

⚠ 補點時機放在「首購訂閱建立之後」：首購若不是試用，先補點會讓首期發票從錢包付清，
改變退點（`_compute_unused_refund` 只退有成功扣款的那一期）——那是另一個前提。
"""

AMPLE_POINTS = 1_000_000.0


def fund_wallet(partner, points=AMPLE_POINTS):
    """讓 ``partner`` 的錢包至少有 ``points`` 點（只補差額，不扣）。"""
    partner = partner.sudo()
    partner.invalidate_recordset(['wallet_balance'])
    missing = points - (partner.wallet_balance or 0.0)
    if missing > 0:
        partner.add_wallet_points(missing, description='test fixture: fund wallet')
        partner.invalidate_recordset(['wallet_balance'])
    return partner

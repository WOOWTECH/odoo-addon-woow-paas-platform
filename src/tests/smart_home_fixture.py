# -*- coding: utf-8 -*-
"""#524：測試 fixture 建立 smart_home 用的開閘 helper。

**這個模組刻意不在 `tests/__init__.py` 裡註冊**——它沒有 TestCase，只是給其他
測試檔 import 的共用件。

## 為什麼需要它

`SmartHome.create()` 從 #524 起有一道**對所有入口一律生效、無 su 豁免**的建立閘
（`models/smart_home.py` 的 `_INTERNAL_PROVISION`）。既有測試裡有 130 處直接
`create()` 的 fixture，它們建的是「已存在的 smart_home」這個**前提**，不是在測
建立路徑本身 ⇒ 一律經此 helper 開閘。

`with_context` 是 merge，所以 `.sudo()` / `.with_user()` / 後續 `with_context`
都保留把手；因此 fixture 可以照原樣寫 `_sh_model(self.env).sudo().create(...)`。

## ⚠ 兩件事不要用它

1. **測建立閘本身**（「RPC 呼叫端建不出來」）——那必須用**沒有**開閘的 handle，
   而且要走真 HTTP，否則測的是 helper 不是守線。
2. **測身分錨點不可變**（`tunnel_id` / `tunnel_name` / `tunnel_token`）——本 helper
   連 `_internal_identity` 一起開了（fixture 常需要在 create 時就把 tunnel 欄位
   擺好，否則 `_strip_identity_anchors` 會**靜默**丟掉它們，測試會在一個與預期
   不同的前提上跑）。要測錨點就用 `env['woow_paas_platform.smart_home']` 原生
   handle 加 `_internal_provision` 單獨開建立閘。
"""
from ..models.smart_home import _internal_identity, _internal_provision

_MODEL = 'woow_paas_platform.smart_home'


def sh_model(env):
    """回傳一個「建立閘 ＋ 身分錨點閘」都開著的 smart_home model handle。"""
    return _internal_provision(_internal_identity(env[_MODEL]))


def open_gates(records):
    """同 `sh_model`，但作用在既有的 recordset／handle 上。"""
    return _internal_provision(_internal_identity(records))

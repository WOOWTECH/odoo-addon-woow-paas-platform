# Pattern: mock 要打在被測碼實際 lookup 的 namespace；一個 symbol 經 N 個 namespace 使用就要 N 個 patch

> 適用：用 `unittest.mock.patch` 攔截一個被多處使用的 helper（如 `get_paas_operator_client`），而該 symbol 透過**不同 import 形式**（頂部 `from ... import name` vs function-local `from ... import name`）進入**不同 module namespace**。`patch` 是「對某個 namespace 上的 attribute 做暫時 rebind」，不是「對 function object 本身改寫」。被測碼在哪個 namespace 查名字、就必須 patch 那個 namespace；同一個 symbol 被 N 個 namespace 各自 bind 一份，就需要 N 個 patch target。少打一個 → 那條路徑 fall through 到真 client，mock 變死碼（false confidence），或在 sandbox 觸發真實 HTTP 被 Odoo `BlockedRequest` 擋下而紅。

## 問題敘事（Security Access P1，2026-06-07）

`get_paas_operator_client` 是包 PaaS Operator HTTP client 的 factory，定義在 `src/services/paas_operator.py`。它有**兩條使用路徑，且兩條用不同 import 形式**：

1. **model 路徑**：`src/models/security_access.py:10-11` 頂部 `from ..services.paas_operator import get_paas_operator_client`。import 當下就把 function object **複製一份 binding 進 model module 的 namespace**。route 的 create/write/unlink → `_sync_routes_to_operator()`（`security_access.py:153`、`:529`）用的是 model namespace 這份 copy。
2. **controller 路徑**：`src/controllers/security_access.py:446 _check_route_prefix` 內用 **function-local import**（`security_access.py:460`：`from ..services.paas_operator import get_paas_operator_client`）。它**呼叫時**才從 `services.paas_operator` 模組現抓 attribute，名字解析發生在來源模組。

撞牆兩次：

- **第一次錯（只 patch model namespace）**：測試 `MOCK_GET_CLIENT` 指向 `...models.security_access.get_paas_operator_client`，只攔了 model 那份 copy。controller 走 `_check_route_prefix` 時從 `services.paas_operator` 現抓 → 拿到**沒被 patch 的真 client**，mock 對 controller 路徑變死碼。對抗審查抓到「false confidence」：測試綠是因為 controller 路徑根本沒跑到斷言，不是因為 mock 生效。
- **第二次錯（把 patch「搬」到 services 而非「兩個都 patch」）**：fix agent 把 `MOCK_GET_CLIENT` 從 `models.security_access` **改成** `services.paas_operator`（commit `f5f973a`，subject 自稱 "patch ... at source module, not model namespace"）。這修好了 controller 路徑，但 model 路徑用的是頂部 import 進 model namespace 的 copy，patch 來源模組**不影響**它 → model 路徑 fall through 到真 client → 在 sandbox（有設 operator URL）直接打 HTTP → Odoo `BlockedRequest`（External requests are forbidden in tests）紅測。**修反了**：把一個半套換成另一個半套，沒有同時覆蓋兩條。
- **最終解（雙常數雙 patch，commit `64e18fb`）**：兩條路徑都要攔，所以兩個 patch target 都要在。見 `src/tests/test_security_access_subscription_flow.py:17-33`：
  - `MOCK_GET_CLIENT = '...services.paas_operator.get_paas_operator_client'`（controller function-local import 現抓的來源模組）
  - `MOCK_GET_CLIENT_MODEL = '...models.security_access.get_paas_operator_client'`（model 頂部 import 進自己 namespace 的那份 copy）

## 機制解釋（為什麼會這樣）

`patch('a.b.name')` 的語意是：暫時把 `a.b` 這個 module object 的 `name` attribute 重新指向 mock，離開 context 還原。它**完全不碰** function object 本身，也不知道別處還有誰 bind 了同一個 function。

- `from m import name` 在 **import 當下**執行一次 name lookup，把結果 binding 進**當前 module 的 namespace**。之後該 module 用 `name` 是查自己 namespace，**與 `m.name` 之後是否被 rebind 無關**。所以要攔頂部 import 的使用者，必須 patch **使用者所在的 module**（`models.security_access.get_paas_operator_client`），patch 來源 `services.paas_operator` 沒用。
- function-local `from m import name`（在函式 body 內）在**每次呼叫時**才執行 lookup，從 `m` 現抓 attribute。所以要攔它，patch **來源模組** `services.paas_operator` 才有效，patch 呼叫者所在 module 沒用（呼叫者 namespace 裡根本沒這個 top-level name）。

同一個 symbol、兩種 import 形式混用 → 兩個不同的 lookup 點 → 需要兩個 patch target。這是「mock 被測碼 lookup 名字的地方，不是定義它的地方」的教科書 corner case，被「同一個 function」的直覺掩蓋。

## 守則（何時適用、怎麼做）

當下列**同時**成立：

1. 被測流程跨**多個 module**，且它們都用到同一個共用 helper（client factory、`requests.post`、`datetime.now`、`uuid4` 之類）。
2. 這些 module 對該 helper 用**不同 import 形式**（有的頂部 `from ... import`、有的 function-local import / `import module` 後 `module.helper()`）。
3. 測試想用 `patch` 攔掉它（避免真 IO / 不確定性）。

→ 怎麼做：

- **patch 目標 = 被測碼 lookup 名字的 namespace，不是定義處**。先看被測 module 怎麼 import 該 symbol，再決定 patch 哪個路徑：
  - 頂部 `from m import name` → patch `<using_module>.name`。
  - function-local `from m import name` 或 `import m; m.name()` → patch `m.name`（來源模組）。
- **一個 symbol 經 N 個 namespace 使用，就列 N 個 patch target**，全部進測試（巢狀 `with` 或 `@patch` 疊用，或像本案兩個命名常數各自 `patch`）。
- **fix 既有半套 mock 時，是「補上缺的那個」不是「把它搬去別處」**——搬只是換一條路徑漏掉，總數還是少一個。

## 證據（commit / 檔案:行號）

- 定義處：`src/services/paas_operator.py` `get_paas_operator_client`
- model 頂部 import（namespace copy）：`src/models/security_access.py:10-11`；使用點 `:153`、`:529`（`_sync_routes_to_operator`，經 route create/write/unlink 觸發）
- controller function-local import（呼叫時現抓）：`src/controllers/security_access.py:460`（在 `_check_route_prefix`，定義於 `:446`）
- 雙常數雙 patch 最終解 + 註解：`src/tests/test_security_access_subscription_flow.py:17-33`（`MOCK_GET_CLIENT` = services 來源模組；`MOCK_GET_CLIENT_MODEL` = model namespace）
- commit `f5f973a` `fix: patch get_paas_operator_client at source module, not model namespace`——**修反**，把 model namespace 單 patch 換成 services 單 patch，model 路徑 fall through 真 client → `BlockedRequest` 紅
- commit `64e18fb` `fix: patch operator client in both controller and model namespaces`——**正解**，兩個 target 同時 patch

## 偵測 / 測試建議

- **審 mock 時 grep target 對照 import 形式**：對每個 `patch('X.Y.symbol')`，grep `symbol` 在被測 module 群裡所有出現處，逐一確認 import 形式（`from ... import` 在哪個 module / 是否 function-local），核對 patch target 是否涵蓋每條 lookup 路徑。漏一條就是潛在死碼 mock。
- **fail-loud 哨兵：讓「沒被 mock 到」會炸而非靜默通過**。本案的隱性守門是 Odoo 測試環境的 `BlockedRequest`——真 client 一旦被呼叫就 raise，逼出漏 patch 的路徑。若被測碼的真實依賴在測試環境**不會炸**（純函式、本地檔案），漏 patch 會 silent false confidence，更危險：可給 mock 設一個 `side_effect=AssertionError("unexpected real call")` 的反向哨兵，或斷言 `mock.called` / `call_count`，確認每個 mock 真的被走到。
- **regression lock**：route create/write/unlink（觸發 model 路徑）與 `check_route_availability` / provision（觸發 controller 路徑）兩組測試都要在，且都斷言對應 mock 被呼叫——任一路徑未來改 import 形式，少打的那個 patch 會立刻讓對應測試紅。

## See also

- 同 family（測試綠 ≠ 真的守住）：[[orm-vs-endpoint-acl-test-coverage]]——ORM-level test 綠遮蔽 controller layer sudo bypass。本 pattern 是 unit-test 層的對應症狀：mock target 錯 → 被測路徑根本沒跑到斷言，綠是假的。
- 原始來源（合成材料、不複製）：commit `f5f973a` / `64e18fb` + 測試註解 `src/tests/test_security_access_subscription_flow.py:17-33`。

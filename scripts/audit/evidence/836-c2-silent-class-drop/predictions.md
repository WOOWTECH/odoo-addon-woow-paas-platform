# 任務一：預測值（跑之前釘死，2026-08-22，基底 16b5fc04）

範圍定義（我自己的 plain-ast walker，不經 _test_tree_facts）：
  src/tests/**/*.py（排除 __pycache__、排除檔名 __init__.py）的 **module 頂層** ClassDef

## 釘死的預測
P1  U（我的 walker 枚舉）                                = 737
P2  E（git show HEAD:baselines/test-collection.txt 的資料行數） = 653
P3  E 的 key 集 == scan_expected()[0] 的 key 集（逐字相同，不只個數）
P4  X（真 scan_expected() 回傳 excluded 的 ident 集）      = 6
P5  E ⊆ U、X ⊆ U、E ∩ X = ∅                （分割前提，非只有基數）
P6  S = U − E − X                                        = 78
P7  S 按「kind==tagged 且 effective tags 含 standard」切開 → 56 / 22
P8  C2 現況輸出：PASS 行印「（653 項）」、註記 header 印「6 個 class」、
    註記 body 恰 6 行 "  ~ " ＋ 6 行 "      因為："

## 真正的預測（leader 沒產出過的執行期觀測）
P9  把 _expected_methods 換成「只對 S56 成員回傳 1 元素假集」→ 真 check_baseline()
    翻 FAIL，"  + " 行**恰 56 行**，其 ident 集 == S56 逐字相同；註記仍 6 條
P10 把 _test_own_test_methods 對 S22 的 node 回非空（key = AST node identity）→
    註記 header 從「6 個」變「28 個」，且雙向差集仍 PASS

## 自我檢查（leader 指定，防「靜默拿到 0」）
G1  facts 必須共用同一顆：scan_expected 內部拿到的 node 物件 id 要與我手上的相同
G2  node-identity map 的命中總數必須 == 範圍內 class 總數（737），不等於就是 key 機制壞了

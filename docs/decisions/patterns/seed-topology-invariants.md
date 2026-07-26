---
title: "seed 拓撲斷言鎖不變式，不鎖計數（非 monotonic 量的守法）"
status: accepted
date: 2026-07-26
deciders: [platform]
tags: [testing, seed, subscription, false-green, repo-lint, invariant]
supersedes: []
related:
  - docs/decisions/patterns/phase-versioned-test-assertion.md
  - docs/tracking/pitfalls/seed-noupdate-sticky-template-links.md
  - docs/decisions/patterns/template-vs-plan-property-boundary.md
---

# seed 拓撲斷言鎖不變式，不鎖計數（非 monotonic 量的守法）

> 適用：test 想守住「seed 出來的商品拓撲是對的」（base plan 數、掛 template 的 plan 數、
> 訂閱 product 總數、tier 數），而寫成 `assertEqual(len(...), <某個當時的數字>)`。
> 這種斷言在本 repo 的節奏下（8 天內動兩次 seed）必然腐化，且**放寬成 `>=` 或 `> 0` 都是錯的修法**。
> 正解是改成三類真不變式：**集合等值**、**零丟棄**、**結構關係**。

## Context

#433：五個硬編計數同時恆紅。

| 位置 | 硬編值 | 撞紅時實值 |
|---|---|---|
| `test_plan_group_keys_368.py:42`（可見且掛 template 的 base） | 34 | 48 |
| `test_plan_group_keys_368.py:74`（tier 數） | 17 | 24 |
| `test_subscription_tax_clear_366.py:50/:70/:87`（訂閱 product） | 251 | 267 |

`#368` 的 34/17 快照在落地（`bd434695`，2026-07-19 11:04）後 **9 小時 40 分**就被同日
20:44 的 `#375`（三型 MCP 上架）打破——快照斷言的腐化速度是以小時計的，不是以季度計。

真正的成因不是「有人寫了硬編值」，而是：**改 seed 時 bump 了 3 個持有計數的檔案、
漏掉 2 個，因為沒有共用清單**。

## When to use

被守的量滿足以下任一條時，就**不要**用計數斷言（`==` 和 `>=` 都不要）：

1. **量非 monotonic**——它會下降。
2. **量可在「零 record 增刪」的情況下變動**（純欄位翻轉、封存、過濾條件改變）。
3. **下游斷言是 `filtered` 出來的**（集合縮水時下游 vacuously pass）。

seed 拓撲三條全中，詳見下節。

## Why not `>=`（「seed 只增不減」是假的）

這是本 pattern 最重要的一段——`>=` 是直覺上的修法，但在此處**守衛會裝反**。

實測 develop first-parent 的可見 base / tier 序列：

```
352bb342(07-10) 32/16 → d13e4602(07-12) 26/13   ← 下降 −6/−3
```

`d13e4602`（PR #341，#336 Wave A 隱藏 6 筆 Hermes）**完全沒有動
`subscription_template_links.xml`**，只在 `subscription_products.xml` 翻了
`storefront_hidden` 旗標——**發生在主線、零 record 增刪、純欄位翻轉造成的下降**。

若把斷言改成 `assertGreaterEqual(48)`，第一次有人重設 `storefront_hidden`
就再度恆紅——**正是這兩條斷言最初出錯的同一個機制**。

另外兩個會讓集合在「seed 一字未改」下縮水的機制：

- **`active_test` 陷阱**：`sh.subscription.plan` **有** Odoo 魔術欄位 `active`
  （`sh_subscription/models/sh_subscription_plan.py:17`）。Odoo 18 的 Many2many
  讀取會套 `active_test`（`odoo/fields.py:4434-4444`），所以
  `template.subscription_plan_ids` 會**靜默過濾掉被封存的 plan**。
  （諷刺點：`src/models/sh_subscription_plan_visibility.py:1-6` 的 docstring 正是說
  `storefront_hidden` 刻意不用 `active`，就是為了避開 `active_test`。）
- **seed xmlid 曾被真刪過一次**：`2bcea232`（2026-07-13）移除 `plan_ha_paas` /
  `product_ha_paas`。對 `noupdate=1` 的 seed，從 XML 移除 record 會在 `-u` 時連同
  `ir.model.data` 一起刪 → 真實縮 DB。

## Why not `> 0`（「非空即可」）

看似安全，實則等於刪掉 test 只留註解。這些計數的作用**不是守數量，是守覆蓋率**：
`assertFalse(taxed)` 的 `taxed` 是 `filtered` 出來的，集合縮水時它 vacuously 綠。
`#366` 的 docstring 開宗明義寫「全庫零跳過斷言……把這條 invariant 對未來釘死」——
降成 `> 0` 就把那條 invariant 丟了。

## 三類真不變式

### 1. 集合等值（對「DB 鏡射檔案」的 seed）

```python
expected = _seeded_link_refs() - _INTENTIONALLY_HIDDEN
self.assertTrue(expected, '前置：links XML 應解析出 base plan（防解析器壞掉變空集合）')
actual = {xmlid_by_res[b.id] for b in bases}
self.assertEqual(actual, expected, 'seed 掛載的可見 base 與 links.xml 不一致')
```

抓得到而 `>=34` 全漏的 regression：清空某 template 的 link、重新 hide 某些 base、
誤設 `ac_max_access_seats`、migration 誤刪。且**新增 template 時完全不用回頭改 test**。

「刻意隱藏」用顯式的 `_INTENTIONALLY_HIDDEN` 常數表達，讓它變成**可 review 的動作**，
而不是靠一個浮動計數默默表達。

> ⚠️ **這招只對 `noupdate="0"` ＋ 全量 `(6, 0, [...])` reset 的 seed 檔成立。**
> 見下節的不對稱。

### 2. 零丟棄（對「集合會被靜默縮小」的下游）

威脅是集合被靜默縮小導致斷言 vacuous，而 `>=` 恰恰對縮小最寬容。正解是把
**丟棄量本身**斷言成 0：

```python
res_ids = imd.mapped('res_id')
alive = env['product.product'].sudo().browse(res_ids).exists()
dangling = sorted(set(res_ids) - set(alive.ids))
self.assertFalse(dangling, 'ir.model.data 指向已不存在的 product（seed 被刪）：%s' % dangling)

products = alive.filtered('sh_product_subscribe')
self.assertEqual(len(products), len(alive),
                 '每個 seed product 都應是訂閱 product；漏網 %s'
                 % (alive - products).mapped('name'))
```

第 2 條是關鍵：它把「靜默丟棄」轉成硬失敗，抓得到「某族變體被誤寫
`sh_product_subscribe=False` 導致 16 筆靜默退出稅務檢查」——**任何數值下界都抓不到**。

允許保留一個 `>=` 的**下界 backstop**（抓「seed 整批沒載入」的災難），但必須在
命名／註解明示它**不是不變式**、可隨 seed 成長調高。

### 3. 結構關係（用經驗算取代計數）

```python
self.assertEqual(set(by_group), expected_groups, 'group_key 集合應等於 seed link 的 tier 集合')
self.assertEqual(len(by_group) * 2, len(bases), '每個 tier 恰 month+year 兩筆')
```

`len(by_group) * 2 == len(bases)` 比原本的 `== 17` **更強**：
`by_group` 每個 base 恰 `setdefault` 一次 ⇒ `Σ|group| == len(bases)`；
緊接的迴圈斷言每 group 的 `cycles == {'monthly','yearly'}` ⇒ 每 group ≥ 2；
「平均恰 2」∧「每個 ≥ 2」⇒「每個恰 2」。
原本的 `== 17` 反而**放行**「某 group 3 筆（2 monthly + 1 yearly）」這種畸形——
`cycles` 是 set，重複的 monthly 會被靜默吃掉。

## ⚠️ noupdate 不對稱：links 能解析 XML，products 不能

這是最容易照抄錯的一格，**必須寫進兩處的註解**：

| seed 檔 | `<data>` | 寫法 | 能否「解析 XML 當 expected」 |
|---|---|---|---|
| `subscription_template_links*.xml` | `noupdate="0"` | 全量 `(6, 0, [...])` reset | ✅ 每次 `-u` 把 DB 的 M2M 重寫成檔案內容 ⇒ **DB 鏡射檔案**，fresh install 與升級兩軌都成立 |
| `subscription_products*.xml` | `noupdate="1"` | record 定義 | ❌ 升級軌上「從 XML 刪掉的 record 仍留在 DB」是正常的 ⇒ **XML 數 ≠ DB 數**，照抄會在 `-u` 軌假紅 |

products 側要用的是上面的「零丟棄」，不是解析 XML。

## 恆真斷言比沒有斷言更糟

`#366` 原有：

```python
checked = 0
for product in products:
    ...
    checked += 1          # 無條件、迴圈內無 continue/break
self.assertEqual(checked, 251)   # 可證恆真
```

恆真斷言讀起來像守衛、review 時會被當成已覆蓋，而未來有人加 `if xxx: continue` 時
它**還是綠的**（`checked` 跟著少），正好在最需要它的那一刻失效。改成對**結果面**斷言：

```python
self.assertEqual(set(results), set(products.ids), '每個訂閱 product 都應被計算（零跳過）')
```

## generator 樣板的結構性缺陷（衍生陷阱）

`src/tests/test_plan_variants_generated.py` 檔頭寫著
**「GENERATED … DO NOT EDIT BY HAND」**，但計數字面量是寫死在
`scripts/seed/generate_plan_variants.py` 的 `_TESTS_TEMPLATE` 裡的
（`267` 裡的 base `51` 更在 generator 的輸入域外）。

後果：**撞紅的人照檔頭指示重跑 generator 是 no-op**；真正要改的是 generator 樣板；
而改產出檔又會被下次 regenerate 蓋掉。一次合法的 base plan 增加要在三個檔手改，
紅卻只出現在第四個「不准手改」的檔案。

修法：樣板內零計數字面量，一律綁 `len(VARIANTS)` 或集合等值；並在 generator 自測加
`assertIn('len(VARIANTS)', src)` 讓字面量無法回流。

## Gate（機械化落點）

`scripts/audit/repo-lint.py` 的 `check_seed_topology`：純靜態 parse
`src/data/subscription_{products,template_links}*.xml`，算六個數
（base plan / 變體 plan / 訂閱 product 總數 / 掛 template 的 distinct base /
其中可見者 / tier 數）對 `scripts/audit/baselines/seed-topology.txt`。

數字一變就 fail，**訊息直接列出所有持有相關計數／集合斷言的位置**請作者逐一檢視，
`--update-baseline` 一併更新。

為什麼 gate 比散文強：#433 的成因是「沒有共用清單」，gate 把
「三週後 CI 才紅、且紅在無關的 test 名下」提前成
「改 seed 當下就被擋、附帶待檢清單」。

## 與 [[phase-versioned-test-assertion]] 的分野

兩者同根（「把某一刻的快照誤鎖成永久不變式」），但**修法相反**，別抄錯：

| | phase-versioned-test-assertion | 本 pattern |
|---|---|---|
| 被守的量 | **monotonic**（manifest version 只增不減） | **非 monotonic**（seed 拓撲會下降） |
| 正解 | `>=` 下界 / 存在性 | 集合等值 / 零丟棄 / 結構關係 |
| `>=` 的效果 | ✅ 真守線（版號掉回門檻以下仍紅） | ❌ 守衛裝反（對縮小最寬容，而縮小正是威脅） |

判準沿用那份 doc 的問法，再加一問：
**「這個量會不會下降？下降時該紅嗎？」** 會下降且該紅 → `>=` 不可用，走本 pattern。

## Anti-patterns（具體別做）

- ❌ `assertEqual(len(bases), 34)` — 快照當不變式（#433 原型）。
- ❌ `assertGreaterEqual(len(bases), 48)` — 對「純翻 `storefront_hidden`」的下降完全無感，
  而那正是 `d13e4602` 真實發生過的事。
- ❌ `assertTrue(len(products) > 0)` — 守覆蓋率的斷言被降成守存在性，等於刪掉 test。
- ❌ 對 `noupdate="1"` 的 seed 檔解析 XML 當 expected（`-u` 軌假紅）。
- ❌ 留 `assertEqual(checked, N)` 這種可證恆真的「守衛」。
- ❌ 撞紅後只把 `34` 改成 `48` 當修法——那只是把未爆彈往後挪一次 seed 變動。

## See also

- 落地：#433（PR 收斂 12 個斷言點＝5 個已紅 + 7 個未爆）；
  `src/tests/test_plan_group_keys_368.py`、`src/tests/test_subscription_tax_clear_366.py`、
  `scripts/seed/generate_plan_variants.py` 的 `_TESTS_TEMPLATE`。
- [[phase-versioned-test-assertion]] — monotonic 量的對應守法（見上節分野表）。
- [[seed-noupdate-sticky-template-links]] — `noupdate` sticky 的 pitfall 敘事，
  本 pattern 的「XML 數 ≠ DB 數」不對稱源於此。
- [[template-vs-plan-property-boundary]] — 新商品變體上架該開新 template 還是加 plan，
  決定了拓撲往哪個維度長。

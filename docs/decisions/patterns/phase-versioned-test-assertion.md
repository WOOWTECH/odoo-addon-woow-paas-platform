# Pattern: phase-versioned test assertion — 鎖 intent 不鎖當時 schema 快照值

> 適用：在 **multi-phase / multi-cycle 的長期演進**中，某個 phase 的 test 想守住「這個 phase 帶進來的 schema 變更生效了」（migration 目錄被 Odoo loader 撿到、欄位存在、constraint 生效）。若這個 test 用「**斷言當時 schema 的精確快照值**」（manifest version `== "18.0.1.0.26"`、constraint 名、欄位 count、UNIQUE 形狀）來表達，它會與**後續 phase 對同一 schema 的合法演進**互斥 — 後面 phase 合法 bump 版號 / 加欄位 / 改 constraint 時，這個舊 phase 的 test 變紅。這**不是 regression**，是 test 把「某一刻的快照」誤當「不變式」。改用 **monotonic（`>=`）/ 存在性（dir/欄位在）/ 結構（parse 得出）** 斷言，鎖 test 的**真實 intent**而非當時值。

## When to use

當下列條件同時成立：

1. **Test 屬於某個 phase / cycle 的「schema change landed」守線**（typical：migration 目錄存在、manifest version bump、新欄位/constraint 生效）。
2. **同一個 addon 會在後續 phase 繼續演進同一塊 schema**（version 還會再 bump、欄位還會再加、constraint 還會再改）— 即這不是 one-shot schema，是 living schema。
3. **Test 的斷言寫成「精確 == 當時值」**（`assertIn('"version": "18.0.1.0.26"')`、`assertEqual(constraint_name, 'agent_session_project_namespace_hash_uniq')`、`assertEqual(len(fields), 7)`）。

→ 三者成立時，這個 test 是**未爆彈**：它今天綠，但下一個動同塊 schema 的 phase 一定讓它紅，且紅得 confusing（test 名說「Phase A migration」，卻因「Phase C 合法 bump 版號」而紅 — **test-vs-test red**，root cause 與 test 名八竿子打不著）。

**不適用**：真的要鎖死「就是這個值、不准變」的 invariant（如 frozen cross-repo contract 的 envelope schema count、一個故意凍結的 wire format）— 那種要的就是精確值，且應有獨立的「凍結」語意（見 [[constraint-input-trust-expansion]] 之外的 frozen-contract 守法）。判準：**問「下個 phase 合法演進這塊 schema 時，這個 test 該紅嗎？」** 該紅 → 鎖精確值；不該紅 → 用本 pattern。

## What it looks like

### 反例（Phase C M.9 撞牆前）

Phase A 的 M.9 `test_manifest_version_bumped` 想守「`migrations/18.0.1.0.26/` 會被 Odoo upgrade path 撿到」，但寫成鎖死當時版號：

```python
# Phase A 簽核當下對，但鎖死「那一刻的精確值」
self.assertIn(
    '"version": "18.0.1.0.26"', content,
    "M.9: __manifest__.py must declare version '18.0.1.0.26' ...",
)
```

Phase C 合法把 manifest bump 到 `18.0.1.0.27`（plan §Phase C Task 13 / impl `ec226b7`，因為 Phase C 自己有 `migrations/18.0.1.0.27/`）→ M.9 立刻紅。M.9 名為「Phase A migration」卻因「Phase C 版號 bump」而失敗，是 **test-vs-test red**（兩個都對的 test 互斥）。

### 正解（`d3aa60e` 改版號無關）

拆成 test 的**真實 intent** — 兩條都是 phase 演進不會破壞的：

```python
# (a) 存在性：Phase A migration 目錄仍在 Odoo upgrade path 上
self.assertTrue(
    os.path.isdir(_MIGRATION_DIR),
    "M.9: src/migrations/18.0.1.0.26/ directory missing — the Phase A "
    "migration would be dropped from Odoo's upgrade path.",
)

# (b) monotonic：manifest version >= (18,0,1,0,26)，不 pin
match = re.search(r'["\']version["\']\s*:\s*["\']([0-9]+(?:\.[0-9]+)*)["\']', content)
version_tuple = tuple(int(p) for p in match.group(1).split('.'))
self.assertGreaterEqual(
    version_tuple, (18, 0, 1, 0, 26),
    "M.9: version %s must be >= 18.0.1.0.26 so the migrations/18.0.1.0.26/ "
    "directory is honoured ... Later phases may bump higher "
    "(Phase C → 18.0.1.0.27)." % match.group(1),
)
```

**保住的原始 guarantee**：「版號不會掉到啟動 Phase A migration 的門檻以下」（`>=` 下界守住 — 若有人不小心把版號改回 `.25`，Phase A migration 會被 loader 跳過，這條仍紅）。**鬆開的多餘約束**：「版號剛好 == .26」這個與 Phase A intent 無關的快照。

### 原則（三種 intent-preserving 斷言）

| 想守的 intent | 別寫（快照） | 改寫（intent） |
|--------------|-------------|---------------|
| migration 生效門檻 | `version == "18.0.1.0.26"` | `version_tuple >= (18,0,1,0,26)`（monotonic 下界）|
| 某 schema 物件在 | `len(fields) == 7` / `constraint_name == 'x_uniq'` | `'last_synced_at' in fields`（存在性）/ `os.path.isdir(migration_dir)` |
| 結構可解析 | `content.split(...)[3] == '...'` | `re.search(version_re, content) is not None`（結構）|

## Why this pattern

- **快照斷言把「某一刻的事實」誤鎖成「永久不變式」**。phase test 真正要守的是「我這個 phase 的變更 landed 且沒被後續退掉」，不是「全世界的版號永遠停在我簽核這天」。`>=` / 存在性 精確表達前者、不誤含後者。
- **test-vs-test red 的 debug 成本極高**：紅的 test 名（「Phase A migration」）與真因（「Phase C 合法 bump」）無關，撞到的人會先懷疑自己的 phase 壞了、grep migration、查 schema，繞一大圈才發現是舊 test 的斷言太緊。改 intent 斷言直接消滅這類假陽性。
- **比「每次 bump 就改舊 test」省**：若維持快照斷言，每個後續 phase 都得回去改前面所有 phase 的版號 test（N phase → O(N²) 維護）。monotonic 下界一次寫對，後續 phase 不用回頭動它。
- **下界仍是真守線**：`>=` 不是「放棄檢查」。把版號改回門檻以下（migration 被跳過）這個**真正的 regression** 仍會被抓 — 只是不再為「合法往上演進」誤報。

## Anti-patterns（具體別做）

- ❌ `assertIn('"version": "18.0.1.0.26"', manifest)` — 鎖死當時版號（M.9 撞牆原型）。
- ❌ `assertEqual(version, "18.0.1.0.26")` — 同上，連 `>=` 都不是。
- ❌ 斷言「constraint 名 / 欄位 count / enum 成員數**剛好等於**當時值」，當這塊 schema 後續還會長。
- ❌ 撞紅後「把 .26 改成 .27」當修法 — 這只是把未爆彈往後挪一個 phase，下次 bump 再炸。要改成 intent 斷言**根治**。
- ✅ 例外：真的要凍結的 wire/cross-repo contract（如 WS envelope frozen count）——那種精確值是**刻意凍結**，應在 test 命名 / 註解明示「frozen，動到要跨 repo 對齊」，與本 pattern 的「living schema 快照」區分。

## 兩次出現（recurrence — 為何值得抽 pattern）

本 pattern 在本 epic / repo 內**第二次撞牆才抽**（pattern 的價值在 cross-instance 重現，非單次）：

1. **Cycle 1 — `test_ac2`（UNIQUE）**：一個 test 硬編當時的 UNIQUE constraint 形狀，schema 演進後 retire（provenance：`d3aa60e` commit docstring 自陳「same model as Cycle 1 retire of test_ac2」+ leader Phase C task brief 「Cycle 1 retire test_ac2（UNIQUE）」；該 test 已 retire，不在現行 code）。
2. **Phase C — M.9 `test_manifest_version_bumped`**：硬編 manifest version `== "18.0.1.0.26"`，Phase C 合法 bump `.27` 時撞紅，`d3aa60e` 改版號無關（本 doc §What it looks like）。

兩次同一根因（快照當不變式），證明這是 multi-phase 演進的**結構性陷阱**而非單一疏忽。epic 後續仍有 Phase D-F，每次 version bump / schema 演進都可能再撞 → 抽 pattern 讓寫 phase test 的人事前避開。

## See also

- 落地 case：`docs/tracking/agent-runtime-pod-phase-c-wiring-2026-05-24.md` §5（M.9 第二次撞牆敘事）；impl commit `d3aa60e`（`src/tests/test_agent_pod_phase_a_migration.py` M.9 改寫）。
- [[tag-scoped-test-misses-full-u-failures]] — 另一個「test 守法不對 → 漏抓 / 誤報」的 pitfall（驗證**範圍**錯 vs 本 pattern 的斷言**內容**太緊）；兩者同屬「test 本身的工程品質」family。
- [[plan-vs-odoo-schema-mismatch]] — 互補：該 pitfall 是「plan 引用不存在的 schema 值（phantom）」，本 pattern 是「test 鎖死存在但會演進的 schema 值」；都根於「把某一刻的 schema 當永久事實」。
- `docs/plans/2026-Q2/agent-runtime-pod-plan.md` §Phase A Task 19（M.9 的 original intent — manifest bump 啟動 migration）。

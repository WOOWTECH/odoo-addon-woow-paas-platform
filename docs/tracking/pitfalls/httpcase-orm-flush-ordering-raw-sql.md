# Pitfall: HttpCase 內用 raw SQL 製造 drift sample 被 ORM buffered write flush 覆蓋

> 來源：`skill-list` cycle（PR #81 follow-up，is_latest drift fix e2e，2026-05-28）。tester 在 HttpCase test 內為了驗 migration「修補既有 drift 樣本」，flow 是「ORM `action_publish` 把 `is_latest=True` → raw `cr.execute UPDATE ... SET is_latest=FALSE` 模擬資料庫層的 drift → 跑 migration 應修回 True」。實際撞到：drift sample **製造階段就 assert 失敗** —— `is_latest` 仍是 True，raw SQL 看似沒生效。根因是 ORM `write()` 在 buffer、未 flush；raw SQL 直寫 DB；後續 ORM read 觸發 flush 把 buffered `True` 蓋回 raw SQL 的 `False`。`invalidate_recordset()` 只清 cache、**不清 buffered write**。

## Symptom

在 Odoo HttpCase（或任何 TransactionCase 變體）內想用 raw SQL 製造一個違反 ORM 預期狀態的「drift 樣本」，典型 pattern：

```python
def test_migration_fixes_existing_drift_sample(self):
    # Step 1: ORM 建一筆 published skill（觸發 action_publish → is_latest=True）
    skill = self.env['woow_paas_platform.skill'].create({
        'project_id': self.project.id, 'name': 's1', 'version': '1.0', ...
    })
    skill.action_publish()

    # Step 2: raw SQL 模擬 DB-level drift（外部把 is_latest 改回 False）
    self.env.cr.execute(
        "UPDATE woow_paas_platform_skill SET is_latest = FALSE WHERE id = %s",
        [skill.id],
    )
    skill.invalidate_recordset()
    self.assertFalse(skill.is_latest, "drift sample 製造成功")  # ← RED here
    # 期望 False（剛 raw SQL 寫的）；實際 True（被 buffered ORM write flush 蓋回）

    # Step 3 (從沒走到): 跑 migration
    mig.migrate(self.env.cr, '18.0.1.0.28')
    skill.invalidate_recordset()
    self.assertTrue(skill.is_latest, "migration 修補成功")
```

訊號：

- assert 「drift sample 製造成功」紅 —— `is_latest` 仍是 raw SQL 之前的 ORM 值。
- 你可能花一小時懷疑「migration 是不是有問題」「raw SQL 有沒有 commit」「是不是 cache 沒清」—— 全錯。**raw SQL 真的寫到 DB 了**，只是後來被覆蓋。

## Why it happens

Odoo ORM `write()` 是 **lazy**：對 `is_latest` 這類欄位的更新先存進 transaction-local **write buffer**（`env.all.tocompute` / `env.all.towrite`），不一定立刻 `UPDATE` 到 DB。觸發 flush 的時機：

- 某些 ORM read（特別跨 `recordset.something` 的 attribute access）會 implicit flush 整個 buffer 後才從 DB read。
- 顯式 `env.flush_all()` / `recordset.flush_recordset()`。
- transaction commit。
- HttpCase 內 `self.url_open(...)` 之類跨 controller 邊界的呼叫（controller 內 read 觸發 flush）。

而 `record.invalidate_recordset()` / `record.invalidate_cache()` **只清** `env.cache`（在 Python process 端的記憶體快取），**不清** `env.all.towrite`（pending writes）。所以這個經典 pattern 失效：

```
1. ORM write({'is_latest': True})  → buffer 中 pending write: (skill.id, True)
2. raw cr.execute UPDATE SET False → DB 已是 False，但 buffer 仍有 (id, True)
3. invalidate_recordset()          → 清 cache（沒用，cache 與 buffer 是兩回事）
4. skill.is_latest                 → ORM read → implicit flush buffer → DB 回 True
                                       ↑ 此時被覆蓋
```

`action_publish()` 內 `_recompute_is_latest` 透過 ORM `skill.write({'is_latest': True})` 寫入 → buffered。raw SQL 直接 DB-level 寫入 → 不經 buffer。後續任何觸發 flush 的 read 都會把 buffered `True` 寫回 DB，蓋掉 raw SQL 的 `False`。

## How to avoid

### 方案 A — flush before/after raw SQL（最少侵入、本 epic 採）

```python
skill.action_publish()
self.env.flush_all()  # 把 ORM buffer 寫到 DB
self.env.cr.execute(
    "UPDATE woow_paas_platform_skill SET is_latest = FALSE WHERE id = %s",
    [skill.id],
)
skill.invalidate_recordset()
self.env.flush_all()  # 後手再 flush，防 raw SQL 之後又有 buffered write 蓋回
self.assertFalse(skill.is_latest, "drift sample 製造成功")
```

**前手 `flush_all()` 必要、後手 `flush_all()` 視情況**：前手是 root cause 解法；後手在「raw SQL 之後 + assertion 之前沒有其他 ORM write」時邏輯上 no-op，但 fixture 機械層的清晰度比省一行重要 —— 留著當 invariant 守線（後人加程式碼到中間時不會踩雷）。

### 方案 B — raw INSERT 完全繞 ORM 製造 drift sample

```python
self.env.cr.execute(
    "INSERT INTO woow_paas_platform_skill "
    "(project_id, name, version, state, is_latest, published_at, ...) "
    "VALUES (%s, %s, '1.0', 'published', FALSE, NOW(), ...) RETURNING id",
    [self.project.id, 's1'],
)
skill_id = self.env.cr.fetchone()[0]
self.env.invalidate_all()
```

最 robust（root cause 解法：不讓 ORM 知道有這筆 record 之前就先寫死）。代價是 fixture 寫法跟既有 test 不對稱（需要重複所有 NOT NULL 欄位）+ 跳過 model 層 default / compute，要小心對其他 invariant 的副作用。

### 方案 C — 抽 classmethod，繞 fixture mechanics

把 migration 的核心演算抽成 `Skill._backfill_is_latest_invariant_for_all_skills()` classmethod；test 不再走「raw SQL 製造 drift 樣本」→「跑 migration」這套，改成直接呼叫 classmethod、用 ORM 介面 assert。

**通常不該為了 fixture 雷改產品 code** —— 改 product code 動機應由產品需求驅動，不是測試方便。本 epic spec 也明示拍板採方案 A（plan §Task 3 Step 1 備註）。但若同 model 已有其他理由要拆 classmethod（e.g. cron / shell 需要直呼），方案 C 順道收 fixture 雷是 bonus。

### 判準

| 情境 | 建議 |
|------|------|
| Test 只要短期通過、不想改產品 code | 方案 A（`flush_all` 前後夾） |
| Fixture 寫法本就用 raw INSERT 灌資料、或 drift 樣本複雜到 ORM 路徑難重現 | 方案 B |
| 同 model 已有獨立理由要把演算抽 classmethod | 方案 C |
| 任何情境 | **不要**只加 `invalidate_recordset()` 後就期望 raw SQL 生效 —— 不會 |

## Related

- 落地 case：spec `docs/superpowers/specs/2026-05-28-skill-e2e-and-is-latest-drift-fix.md` §④-a + 「實作注意」最後一條；impl commit `c018e92` `test(skill): fix drift sample buffer-ordering in migration e2e`（採方案 A）；tester report `/tmp/skill-list-tester-report.md`（首次撞紅 + 三方案分析）。
- 配套 pattern：[[invariant-recompute-on-orm-write-hook]] — 本 pitfall 是為該 pattern 寫 regression test 時撞到的 fixture mechanics 雷；兩者一組（pattern 是「修法本身」、pitfall 是「為此 pattern 寫 e2e 時撞到的雷」）。
- 守線 pattern：[[plan-gap-stop-and-report]] — tester 撞此雷時的處置（不 bend impl 湊綠、原樣 escalate，由 implementation 階段選方案 A/B/C）。本 pitfall 是該守線在「fixture mechanics layer」的延伸 instance：tester 階段如果不停下回報，最自然的反應是把 assertion 拿掉 / 改寫期望 → 偽綠（migration 是否真修補從未被驗）。
- 同 family（ORM cache 與 Python state 不同步的雷）：
  - Odoo 官方 docs `recordset.flush()` / `env.flush_all()` API（沒 anti-pattern 範例，這條 pitfall 補上）。
- **chrome of「invariant for invariant」**：對任何「raw SQL 製造預期狀態 + 後續 ORM assertion」的 test 結構，**永遠** flush before/after raw SQL，再清 cache，再 assert。`invalidate_recordset()` 是必要但不充分。

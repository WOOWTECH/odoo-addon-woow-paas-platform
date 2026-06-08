# Pitfall: skill 同名雙綁 → snapshot 撞名 + 下拉無法區分同名 system/user skill

> 來源：F-SBX-5 / Gitea issue [#156](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/156)，2026-06-06 stg 手測（project 28 'odd2'）。PR #153 兩層綁定 UX 落地後，admin 在 project form 綁 skill 時下拉出現**兩顆一模一樣的 `test-echo`**（id 4 system 0.1.0 vs id 7 own user 0.1.0；跨租戶過濾正常，**非 data leak**）。選錯選到 system 顆 → pod 載入 system 版 bundle、自己那行 `Active in Pod` 不亮（症狀離原因遠）。更深的 runtime 地雷：同名兩顆**都**綁進 template 時，snapshot 輸出兩個同名 entry，pod 按 name 落地撞名（last-wins / sha-mismatch）。
>
> 修復（2026-06-07，本 pitfall 同 PR 落地）：四層 lock-step，見下。

## 根因

1. **跨桶同名是 DB 刻意允許的**：`skill_uniq_user_proj_name_version`（per-project）+ `skill_uniq_system_name_version`（system 桶）兩個 partial unique index 之間互不約束（`src/models/skill.py`），同名跨 scope / 跨 version 合法 —— 這是 dual-scope 設計的一部分（bound 蓋 system 的同名 shadow 就是建立在它之上），不可改 schema。
2. **顯示層沒跟上**：skill 無 `display_name` 覆寫，many2many_tags / 下拉只顯 `name` → 同名兩顆完全無法區分。
3. **生效層沒守**：`_effective_skill_ids` 的同名 shadow 只處理「bound 蓋 system」單向，對「bound 集合自身含同名兩顆」零去重；view domain 與 `_check_skill_subset` 都只篩歸屬不去重 name → 同名雙綁可寫入。
4. **wire 後果**：`_build_skill_snapshot` 1:1 序列化 → 兩個同名 entry（sha256 不同）。skill-bundle endpoint 是 deterministic 的（project leg 蓋 system leg `[:1]`），所以 snapshot 中 system 那顆宣告的 sha256 **永遠**對不上 endpoint 實回的 user bytes → pod 端 last-wins / sha-mismatch。

## 修復 — 四層 lock-step

| 層 | 位置 | 行為 |
|---|---|---|
| ① 顯示可區分 | `skill._compute_display_name`（`src/models/skill.py`） | 後綴 version+scope：`test-echo v0.1.0 (System)` / `(User)`；`_rec_names_search = ['name','version']` 保按 slug 搜＋版號可搜。**display_name 永不進 wire**（snapshot/bundle 全鏈讀 `skill.name` 原欄位） |
| ② 擋新雙綁 | `agent_template._check_no_duplicate_skill_name`（`src/models/agent_template.py`） | `@api.constrains('skill_ids')`，同 template 內 bound names 唯一；**不對 superuser 放行**（資料形狀不變式，對齊 `_check_not_platform_default_when_bound`；無任何 legitimate 同名雙綁路徑）。一條 constraint 守全部 3 條寫入路徑（template form 直寫 / project form inverse / fork `copy(default=)`） |
| ③ runtime 兜存量 | `agent_session._build_skill_snapshot` | dict-by-name dedup：同名保 **user-scope 蓋 system**（與 endpoint tie-break 同向，見下方不變式）+ `_logger.warning` 留痕；**絕不 raise**（chat path `ai_assistant.py` 無兜底，raise = 整個 project 不能聊天）。`_effective_skill_ids` frozen 公式**未動** |
| ④ 存量清理 | `src/migrations/18.0.1.0.34/post-migrate.py` | 保守 migration：明確的 (user, system) 同名配對 → raw SQL 刪 system rel row（保 user，同向）；**同 scope 同名配對 AMBIGUOUS，只 warning 不刪**（留人工，見下） |

## 不變式（改任何一邊都要 lock-step）

**snapshot dedup 方向 ↔ bundle endpoint tie-break 方向必須同向**（user/project 蓋 system）：

- dedup：`agent_session._build_skill_snapshot`（user 蓋 system）
- endpoint：`agent_runtime_skill.py` `skill_bundle` 的 `candidates.filtered(project)[:1] or candidates.filtered(system)[:1]`

若兩邊方向不一致，snapshot 宣告的 sha256 會對不上 endpoint 實回 bytes → pod sha-mismatch 重現。改任一邊 tie-break 時必須同步另一邊 + 測試（`TestSnapshotDedup`）。

## Constraint 上線後的「鎖死」是預期行為

存量已雙綁的 template（migration 處理不了的同 scope 配對、或 migration 前 odoo-shell 操作產生的），**在人工清理前任何觸發 `@api.constrains('skill_ids')` 的 write 都會 raise** —— admin 會看到「為何這個 template 改不了 skill 綁定」。這是 fail-loud 的預期效果，不是 bug。錯誤訊息會列出衝突的 skill names 並引導 unbind。

## 同 scope 同名存量的人工清理

migration 只 log 不刪（無權威來源能判 admin 要留哪版）。warning 格式含 `(template_id, skill_name, scope, skill_ids)`。人工清理 odoo-shell snippet：

```python
# 找出仍有同名雙綁的 template
env.cr.execute("""
    SELECT rel.template_id, sk.name, sk.scope,
           array_agg(sk.id ORDER BY sk.id) AS skill_ids
    FROM agent_template_skill_rel rel
    JOIN woow_paas_platform_skill sk ON sk.id = rel.skill_id
    GROUP BY rel.template_id, sk.name, sk.scope
    HAVING count(*) > 1
""")
for tpl_id, name, scope, ids in env.cr.fetchall():
    print(tpl_id, name, scope, ids)

# 裁決後解綁不要的那顆（走 ORM 會被 constraint 擋 → 雙綁狀態下要直刪 rel）
env.cr.execute(
    "DELETE FROM agent_template_skill_rel WHERE template_id=%s AND skill_id=%s",
    (TPL_ID, UNWANTED_SKILL_ID))
env.cr.commit()
```

清理後記得讓 pod 收斂：下次 chat attach 會自動 refresh snapshot，或對該 project 重發佈任一 skill 觸發 push。

## 已知顯示行為（不是 bug）

存量同名雙綁清理前，兩顆 bound skill 的 `is_effective` **都顯 True**（`_effective_skill_ids` frozen 公式對 bound 內部同名零去重；is_effective 與它同源），但 runtime snapshot 實際只載一顆（user 蓋 system）→ 後台 `Active in Pod` 與實載短暫不一致。**不要為此動 frozen 公式**（anti-pattern #3 風險 > 收益）；清理存量後自然消失。

## 守線測試

`src/tests/test_skill_binding_admin.py`：`TestSkillDisplayName`（7）/ `TestTemplateNoDuplicateSkillName`（8，含 3 寫入路徑 negative + superuser 不放行 + 批次 create）/ `TestSnapshotDedup`（5，含三顆同名）/ `TestDualBindMigration`（3，importlib 直呼 `migrate()` 驗刪向 + 同 scope 不動 + 冪等）。`test_snapshot_characterization` 驗 frozen 公式未動。

## 相關

- [[default-agent-template-project-bind-silent-autoenable]] — 同為 agent_template constraint fail-loud 家族（platform default 不放行 superuser 的先例）
- `docs/decisions/patterns/two-layer-acl-lockstep.md` — 本修復的多層 lock-step 紀律依據
- `docs/decisions/patterns/constraint-input-trust-expansion.md` — constraint 自足 / 完整 membership / 訊息不洩衍生資訊
- follow-up 列冊：`docs/tracking/followups/follow-ups.md` F-SBX-5（已修）

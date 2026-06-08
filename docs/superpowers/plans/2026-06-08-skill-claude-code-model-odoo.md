# Skill Claude Code 模型重設計 — Odoo 端 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 Odoo skill 上傳精靈接受「只有 SKILL.md」的最小 skill（移除 entrypoint 要求），並把 demo skill 與驗證合約對齊新的 Claude Code skill 模型。

**Architecture:** 本 repo（`woow_paas_platform`）只負責「上傳驗證」與「demo seed」。runtime 端（`paas-agent-runtime`，另一 repo / canvas agent）才是執行模型 SSOT，見姊妹計畫 `2026-06-08-skill-claude-code-model-runtime.md`。Odoo 端鏡像 runtime 合約：移除 entrypoint 預設與「缺檔警告」，保留所有結構/安全硬擋。

**Tech Stack:** Odoo 18 addon、Python 3.11、pytest 風格的 Odoo `TransactionCase`、tarfile/gzip。

**Spec:** `docs/superpowers/specs/2026-06-08-skill-claude-code-model-redesign-design.md`（§7 bundle 合約、§8 Odoo 改動）。

---

## 部署順序（重要，見 spec §9）

- **Task 1（移除 warning A）低風險**：warning A 是軟警告，移除它不改變「上傳會成功」這件事，只是不再顯示一則對舊 runtime 才準確的警告。可先行。
- **Task 3-4（demo re-pack + migration）必須與 runtime 模型同窗口或在其後**：若 Odoo 把 demo 改成 SKILL.md-only 但 runtime 仍跑舊 subprocess 模型，該 demo skill invoke 會 `SKILL_NOT_FOUND`。故 Task 3-4 標記 **gated on runtime landing**（runtime 計畫完成、pod rollout 後再合併部署）。

## File Structure

| 檔案 | 責任 | 動作 |
| --- | --- | --- |
| `src/wizard/skill_bundle_validation.py` | bundle 結構/安全驗證 | 移除 `DEFAULT_ENTRYPOINT` + warning A；改 docstring |
| `src/wizard/skill_upload_wizard.py` | 上傳精靈 entrypoint 合約字句 | 改 docstring（無行為變動） |
| `src/tests/test_skill_upload_wizard.py` | 驗證測試 | 改寫 `test_warn_missing_entrypoint_still_creates` → 純 SKILL.md 0 警告 |
| `src/data/demo_skills/hello-world-0.1.0.tar.gz` | demo bundle 位元 | 重打包為 SKILL.md-only |
| `src/data/demo_system_skill.xml` | demo seed 紀錄 | 更新 base64 / sha256 / metadata；刪除 subprocess 合約註解 |
| `src/migrations/18.0.1.0.35/post-migrate.py` | 升級路徑修舊 demo bytes | 新增（鏡像 `.33` idempotent in-place 模式） |
| `src/__manifest__.py` | 版號 | bump `18.0.1.0.34` → `18.0.1.0.35` |

## 如何跑測試

```bash
# 本機 k8s-dev-sandbox（CLAUDE.md 指定路徑）：
./.claude/skills/k8s-dev-sandbox/scripts/test.sh <sandbox-name> --module woow_paas_platform
# 或 odoo-bin（從 odoo-server root）：
./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform --stop-after-init
```

單一測試類別可用 `--test-tags /woow_paas_platform:TestSkillUploadWizard`（Odoo test-tags 語法）。

---

## Task 1: 移除 entrypoint 警告與 DEFAULT_ENTRYPOINT（含測試改寫）

**Files:**
- Modify: `src/tests/test_skill_upload_wizard.py:452-463`（改寫該測試）
- Modify: `src/wizard/skill_bundle_validation.py`（移除 `:60-61`、`:403-411`、改 docstring）

- [ ] **Step 1: 改寫測試 — 純 SKILL.md → 0 警告（先寫會失敗的測試）**

把 `test_skill_upload_wizard.py` 的 `test_warn_missing_entrypoint_still_creates`（行 452-463）整段換成：

```python
    def test_skill_md_only_no_warnings(self):
        # Claude Code 模型：skill 只要 SKILL.md（無 script）即合法，且不應有任何
        # 警告（移除舊的「缺 entrypoint」warning A）。回傳純 act_window、非警告通知。
        raw = _make_targz({
            'SKILL.md': _compliant_skill_md(name=f'up-{self.suffix}'),
        })
        wiz = self._new_wizard(bundle_file=base64.b64encode(raw))
        action = wiz.action_upload()
        skill = self.Skill.search([('name', '=', f'up-{self.suffix}')], limit=1)
        self.assertEqual(len(skill), 1)
        # 純 act_window（非 display_notification）＝ 0 警告
        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(action['res_model'], 'woow_paas_platform.skill')
        self.assertEqual(action['res_id'], skill.id)
```

- [ ] **Step 2: 跑新測試，確認失敗**

Run: `./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform:TestSkillUploadWizard --stop-after-init`（或 k8s-dev-sandbox test.sh）
Expected: FAIL — 目前 code 對缺 `scripts/main.py` 仍會產生 warning A → `action_upload` 回 `ir.actions.client`（display_notification），`assertEqual(action['type'], 'ir.actions.act_window')` 失敗。

- [ ] **Step 3: 移除 DEFAULT_ENTRYPOINT 常數**

在 `src/wizard/skill_bundle_validation.py` 刪除這兩行（約 `:60-61`）：

```python
# --- entrypoint 預設（鏡像 loader.py parse_skill_frontmatter）---
DEFAULT_ENTRYPOINT = "scripts/main.py"
```

- [ ] **Step 4: 移除 warning A 區塊**

在同檔 `_scan_members` 函式尾段刪除整段「軟警告 A」（約 `:403-411`）：

```python
    # 軟警告 A：entrypoint 檔不存在
    entrypoint = (frontmatter.get("entrypoint") or DEFAULT_ENTRYPOINT)
    entrypoint_norm = posixpath.normpath(str(entrypoint))
    if entrypoint_norm not in file_paths:
        result.warnings.append(
            "Declared entrypoint '%s' is not present in the bundle; the pod "
            "will fail to launch this skill until the file is added." % entrypoint
        )
```

> 刪除後，緊接其後的「軟警告 B：AppleDouble」（`_append_appledouble_warning`）成為第一個軟警告，邏輯自洽。`file_paths` 仍被其他結構檢查使用，不要一併刪。

- [ ] **Step 5: 更新 module docstring（移除 entrypoint 合約第 3 點）**

在 `skill_bundle_validation.py` 檔頭 docstring，刪除「3. **entrypoint 預設值**…」整段（描述 loader 預設 `scripts/main.py` 的那段），並把開頭「從 runtime SSOT 精確鏡像的規則」段落改為反映「不再含 entrypoint 規則、改鏡像 Claude Code skill bundle 結構」。其餘規則（1 SKILL.md at root、2 frontmatter、4 解壓防護）保留並順移編號。

- [ ] **Step 6: 跑整個測試類別，確認新測試過、其他不回歸**

Run: `./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform:TestSkillUploadWizard --stop-after-init`
Expected: PASS 全部。特別確認 `test_skill_md_only_no_warnings`（新）、`test_warn_appledouble_still_creates`、`test_warn_name_mismatch_still_creates`、`test_upload_creates_draft_system_skill` 皆綠（後三者 bundle 仍含 `scripts/main.py`，移除 warning A 後不影響其 warning B/C/0-warning 行為）。

- [ ] **Step 7: Commit**

```bash
git add src/wizard/skill_bundle_validation.py src/tests/test_skill_upload_wizard.py
git commit -m "feat: skill 上傳移除 entrypoint 要求（Claude Code 模型，純 SKILL.md 即合法）"
```

---

## Task 2: 更新上傳精靈 docstring（無行為變動）

**Files:**
- Modify: `src/wizard/skill_upload_wizard.py`（`action_upload` docstring）

- [ ] **Step 1: 改 docstring 字句**

在 `skill_upload_wizard.py` 的 `action_upload` docstring 中，把這句：

```python
        Validation rules mirror paas-agent-runtime ``runtime/skills/{cache,
        loader}.py`` (the contract SSOT); see ``skill_bundle_validation``.
```

改為：

```python
        Validation rules mirror the paas-agent-runtime Claude Code skill bundle
        contract (SKILL.md + optional files, no required entrypoint); see
        ``skill_bundle_validation``.
```

- [ ] **Step 2: Commit**

```bash
git add src/wizard/skill_upload_wizard.py
git commit -m "docs: 上傳精靈 docstring 對齊 Claude Code skill 合約"
```

---

## Task 3: 重打包 demo skill 為 SKILL.md-only（gated on runtime landing）

**Files:**
- Modify: `src/data/demo_skills/hello-world-0.1.0.tar.gz`（重建位元）
- Modify: `src/data/demo_system_skill.xml`（base64 / sha256 / metadata / 刪註解）

> **前置**：此 task 須在 runtime 新模型部署後再合併（spec §9），否則舊 runtime 跑這個無 script 的 demo 會 `SKILL_NOT_FOUND`。

- [ ] **Step 1: 重建 bundle 並印出 sha256 + base64**

在 repo root 跑此腳本（建 SKILL.md-only bundle、寫檔、印新 sha256 與 base64）：

```python
# scripts/_repack_demo_skill.py（一次性；跑完可刪）
import io, tarfile, hashlib, base64

skill_md = (
    "---\n"
    "name: hello-world\n"
    "version: 0.1.0\n"
    "description: A demo system skill that greets the user.\n"
    "author: woow-platform\n"
    "---\n"
    "# Hello World\n"
    "\n"
    "A minimal demo skill available to every project. When the user greets you\n"
    "or asks for a hello-world demo, respond with a friendly greeting noting\n"
    "this is the platform's starter skill.\n"
)
buf = io.BytesIO()
# gzip mtime=0 讓重建可重現（避免 sha 飄移）
with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.GNU_FORMAT) as tf:
    data = skill_md.encode("utf-8")
    info = tarfile.TarInfo(name="SKILL.md")
    info.size = len(data)
    info.mtime = 0
    tf.addfile(info, io.BytesIO(data))
raw = buf.getvalue()
with open("src/data/demo_skills/hello-world-0.1.0.tar.gz", "wb") as fh:
    fh.write(raw)
print("SHA256:", hashlib.sha256(raw).hexdigest())
print("BASE64:", base64.b64encode(raw).decode("ascii"))
```

Run: `python scripts/_repack_demo_skill.py`
Expected: 印出 `SHA256: <hex>` 與 `BASE64: <b64>`；`src/data/demo_skills/hello-world-0.1.0.tar.gz` 被覆寫。記下這兩個值供 Step 2/3。

- [ ] **Step 2: 驗證新 bundle 過驗證、0 警告**

Run（repo root；validation 模組刻意無 odoo 依賴，用 importlib 直接載入檔案，避免 `src` 非 package 的 import 問題）：

```bash
python -c "import importlib.util as u; s=u.spec_from_file_location('sbv','src/wizard/skill_bundle_validation.py'); m=u.module_from_spec(s); s.loader.exec_module(m); r=m.validate_skill_bundle(open('src/data/demo_skills/hello-world-0.1.0.tar.gz','rb').read(), wizard_name='hello-world'); print('errors:', r.errors); print('warnings:', r.warnings)"
```
Expected: `errors: []` 且 `warnings: []`。

- [ ] **Step 3: 更新 demo_system_skill.xml**

在 `src/data/demo_system_skill.xml`：
1. **刪除** `<data noupdate="1">` 下方那段 CONTRACT 註解（行 7-16，描述 subprocess/entrypoint 合約者）。
2. 把 `demo_attachment_hello_world` 的 `<field name="datas">…</field>` 內容換成 Step 1 印出的 **BASE64**。
3. 把 `demo_skill_hello_world` 的 `<field name="bundle_sha256">…</field>` 換成 Step 1 的 **SHA256**。
4. `skill_metadata` 移除已不存在的鍵（保留 name/version/description/author，**移除任何 entrypoint**）：

```xml
            <field name="skill_metadata">{"name": "hello-world", "version": "0.1.0", "description": "A demo system skill that greets the user.", "author": "woow-platform"}</field>
```

- [ ] **Step 4: Commit**

```bash
rm -f scripts/_repack_demo_skill.py
git add src/data/demo_skills/hello-world-0.1.0.tar.gz src/data/demo_system_skill.xml
git commit -m "feat: demo skill 重打包為純 SKILL.md（Claude Code 模型）"
```

---

## Task 4: Migration 18.0.1.0.35 + manifest bump（gated on runtime landing）

**Files:**
- Create: `src/migrations/18.0.1.0.35/post-migrate.py`
- Modify: `src/__manifest__.py:3`（version）

- [ ] **Step 1: 建 migration（鏡像 .33 idempotent in-place 改寫）**

Create `src/migrations/18.0.1.0.35/post-migrate.py`：

```python
"""18.0.1.0.35 — re-pack demo hello-world skill bundle: SKILL.md-only.

Claude Code 模型下 skill 不再需要可執行 entrypoint；demo hello-world 改為純
SKILL.md（指令本體進 agent context，無 scripts/main.py）。

demo_skill_hello_world 是 noupdate="1"，模組升級不會用 XML 覆寫既有 attachment，
故此 migration 以「直接 ORM write attachment.datas + bundle_sha256」就地更新
（noupdate 只擋 XML data 重載，不擋直接 ORM write — 與 .33 同模式）。

Idempotent：skill 缺席（admin 刪除 / fresh install 由 XML seed 新 bytes）或已是
新 sha 時 no-op。
"""
import base64
import hashlib
import logging

from odoo import SUPERUSER_ID, api
from odoo.tools import file_open

_logger = logging.getLogger(__name__)

_BUNDLE_RESOURCE = (
    "woow_paas_platform/data/demo_skills/hello-world-0.1.0.tar.gz"
)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    skill = env.ref(
        "woow_paas_platform.demo_skill_hello_world", raise_if_not_found=False
    )
    if not skill:
        _logger.info(
            "18.0.1.0.35 demo-bundle re-pack: demo_skill_hello_world absent "
            "(admin-deleted or fresh install) — nothing to migrate."
        )
        return

    with file_open(_BUNDLE_RESOURCE, "rb") as fh:
        raw = fh.read()
    new_sha = hashlib.sha256(raw).hexdigest()

    if skill.bundle_sha256 == new_sha:
        _logger.info(
            "18.0.1.0.35 demo-bundle re-pack: already SKILL.md-only bundle "
            "(sha %s) — no-op.",
            new_sha,
        )
        return

    old_sha = skill.bundle_sha256
    attachment = skill.bundle_attachment_id
    if attachment:
        attachment.write({"datas": base64.b64encode(raw).decode("ascii")})
    skill.write({"bundle_sha256": new_sha})
    _logger.info(
        "18.0.1.0.35 demo-bundle re-pack: updated demo_skill_hello_world to "
        "SKILL.md-only bundle (sha %s -> %s).",
        old_sha,
        new_sha,
    )
```

- [ ] **Step 2: bump manifest 版號**

在 `src/__manifest__.py` 把 `"version": "18.0.1.0.34",` 改為 `"version": "18.0.1.0.35",`。

- [ ] **Step 3: 驗證升級路徑（先裝舊後升）**

在 k8s-dev-sandbox（或本機 Odoo）：先以舊版裝好（demo skill = 舊 executable bundle），再 `-u woow_paas_platform` 升到 .35。

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --stop-after-init`
Expected: log 出現 `18.0.1.0.35 demo-bundle re-pack: updated demo_skill_hello_world to SKILL.md-only bundle (sha … -> …)`；再次 `-u` 應出現 `no-op`（冪等）。DB 中 `demo_skill_hello_world.bundle_sha256` == Task 3 的新 sha。

- [ ] **Step 4: 驗證 fresh install 路徑**

全新 DB 裝 `woow_paas_platform`：`demo_skill_hello_world.bundle_sha256` 應直接 == 新 sha（XML seed 新 bytes），且 attachment bytes == 新 tar.gz。

- [ ] **Step 5: Commit**

```bash
git add src/migrations/18.0.1.0.35/post-migrate.py src/__manifest__.py
git commit -m "feat: migration 18.0.1.0.35 就地升級 demo skill 為純 SKILL.md"
```

---

## Self-Review（plan author 自檢，已執行）

- **Spec 覆蓋**：§8.1→Task1；§8.2→Task2；§8.3→Task1 Step1；§8.4→Task3；§8.5/§8.6→Task4。§7 bundle 合約由 Task1（驗證放寬）+ Task3（demo 示範）體現。✓
- **Placeholder 掃描**：無 TBD/TODO；sha256/base64 為「建置時產生值」（同既有 demo bundle 做法），由 Step 1 腳本印出後貼入，非 placeholder。✓
- **型別/名稱一致**：新測試名 `test_skill_md_only_no_warnings`；migration 版號 `18.0.1.0.35` 與 manifest 一致；`_BUNDLE_RESOURCE` 路徑與 Task3 寫檔路徑一致。✓
- **順序風險**：Task3-4 明確標 gated on runtime landing（spec §9）。✓

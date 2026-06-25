# Skill is_latest drift 修正 + E2E 測試補強 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修正 `woow_paas_platform.skill` 的 `is_latest` drift（model 層 `create()` / `write()` 直接設 `state='published'` 時繞過 `_recompute_is_latest` 的根因缺口），補上幂等資料修補 migration，並新增含瀏覽器 UI 的 e2e 測試（後端 HttpCase 走「lifecycle ↔ download endpoint 可見性 ↔ is_latest」串接 + playwright-cli 操作後台 form 驗證 UI wiring）。

**Architecture:**
- `Skill.create()` / `Skill.write()` override 偵測 `state == 'published'` 時觸發 `_recompute_is_latest` 並補 `published_at`（單一維護點，涵蓋所有現存與未來繞過 transition method 的程式化路徑）
- 新增 `src/migrations/18.0.1.0.28/post-fix-is-latest.py` 幂等修補（無資料 no-op、有資料自動修補；rollback 安全）
- manifest 27→28、連帶把 `test_agent_pod_phase_c_skill_bundle.py:201` 硬編版本 assertion 改 monotonic `>= 27`（`phase-versioned-test-assertion` pattern）
- 後端 e2e（HttpCase）借 Phase C 的 HS256 token 工具，驗證 lifecycle transition 後 download endpoint 可見性切換 + is_latest 同步更新 + migration 修補 drift 樣本
- 瀏覽器 e2e（playwright-cli spec）登入 admin → 點 form transition button → 驗 statusbar / badge / is_latest 視覺反映；headless + screenshot

**Tech Stack:** Odoo 18、Python（`odoo.tests.common.TransactionCase` / `HttpCase`）、Odoo migration（`migrate(cr, version)`）、Playwright（spec via playwright-cli）。

**Spec:** `docs/superpowers/specs/2026-05-28-skill-e2e-and-is-latest-drift-fix.md`

---

## 如何跑測試（docker-desktop sandbox 流程）

### 前置（一次性確認）

- **Sandbox**：`feat-skill-list`（namespace `dev-sandbox-feat-skill-list`）已建立並完成 DB init（PR #81 plan §17-25 已設好；若被銷毀需重建）。
- **第三方 addon 路徑**：`create.sh` 必須帶 `PAAS_CI_ADDONS_PATH=/Users/eugene/Documents/woow/woow-paas-repo/paas-odoo-ci/addons`（缺則 DB init 失敗）。
- **OOM 釋放**：docker-desktop node 記憶體 ~4.7Gi，多 sandbox 會 OOM。執行前確認：
  ```bash
  kubectl --context docker-desktop get ns | grep dev-sandbox
  ```
  若除 `dev-sandbox-feat-skill-list` 外還有其他 sandbox 占用，必要時 `kubectl --context docker-desktop delete ns dev-sandbox-<other>` 釋放。

### 後端測試（Odoo test runner）

**全模組 `-u`（schema 變更如 manifest bump、新 migration 必走這條）**：
```bash
bash .claude/skills/k8s-dev-sandbox/scripts/test.sh feat-skill-list --module woow_paas_platform
```

**單 class 快測（不更新模組，本地 .py 改完 hostPath 即時反映）**：
```bash
kubectl --context docker-desktop exec -n dev-sandbox-feat-skill-list \
  deploy/feat-skill-list-odoo-dev-sandbox-odoo -- \
  odoo -u woow_paas_platform --test-enable \
       --test-tags /woow_paas_platform:TestSkillIsLatestDriftRegression \
       --stop-after-init --http-port=18069 --gevent-port=18072 \
       -d woow_feat_skill_list -c /etc/odoo/odoo.conf
```
（DB 名請依 sandbox 實際情況；單 class 取代 `TestSkillIsLatestDriftRegression`。）

### 瀏覽器 e2e（playwright-cli）

**強制前置：rollout restart（leader 強調，否則 serve 進程用舊 code 會誤判 button 行為）**：
```bash
kubectl --context docker-desktop rollout restart deploy/feat-skill-list-odoo-dev-sandbox-odoo \
  -n dev-sandbox-feat-skill-list
kubectl --context docker-desktop rollout status deploy/feat-skill-list-odoo-dev-sandbox-odoo \
  -n dev-sandbox-feat-skill-list
```

**沙盒對外入口**（playwright 連的 base URL）：依 sandbox `list.sh` / `status.sh` 顯示的對外 ingress / nodePort 取得。

**播放 playwright spec**（具體 CLI 依 playwright-cli skill 用法；JS/TS spec 範例）：
```bash
# 用 playwright-cli skill 跑
# 詳見 Task 4
```

**截圖落地**：`/tmp/skill-e2e-screenshots/` 供視覺驗證。

---

## File Structure

| 檔案 | 責任 | 動作 |
|------|------|------|
| `src/models/skill.py` | `create()` / `write()` 收斂 `_recompute_is_latest` + 補 `published_at` | 修改 |
| `src/migrations/18.0.1.0.28/post-fix-is-latest.py` | 幂等資料修補 migration | 新增 |
| `src/__manifest__.py` | version 27 → 28 | 修改 |
| `src/tests/test_agent_pod_phase_c_skill_bundle.py` | (a) :201 monotonic `>= 27` (b) :261-267 setUp 加 fixture 行為註解 | 修改 |
| `src/tests/test_skill_is_latest_drift_regression.py` | drift 修法回歸（TransactionCase）| 新增 |
| `src/tests/test_skill_e2e_lifecycle_download.py` | 後端 e2e（HttpCase；lifecycle ↔ download ↔ is_latest 串接 + migration 驗證）| 新增 |
| `tests/e2e/skill/skill-admin-form.spec.ts` | playwright-cli 瀏覽器 e2e（form UI wiring + screenshot）| 新增 |
| `src/tests/__init__.py` | 註冊兩個新 Python test module | 修改 |

---

## Task 1: Model 層 create / write hook + drift 回歸 test（TDD）

**Files:**
- Create: `src/tests/test_skill_is_latest_drift_regression.py`
- Modify: `src/tests/__init__.py`
- Modify: `src/models/skill.py`

### Step 1: 寫 failing test

Create `src/tests/test_skill_is_latest_drift_regression.py`:

```python
"""TDD — is_latest drift regression locks.

Spec: docs/superpowers/specs/2026-05-28-skill-e2e-and-is-latest-drift-fix.md §①

Rationale: PR #81 維護 is_latest 不變式只在三個 transition method 結尾收斂。
任何繞過 transition 直接 `create({'state':'published'})` 或
`write({'state':'published'})` 的程式化路徑（test fixture / 未來上傳 API /
demo data / admin 匯入）都會 drift — published skill 的 is_latest 恆 default
False、published_at 為 NULL。

本 test 鎖修法（model 層 create/write override 自動 recompute + 補
published_at），讓不變式對所有 ORM 寫入路徑成立。
"""
import hashlib
import uuid

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestSkillIsLatestDriftRegression(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if 'woow_paas_platform.skill' not in cls.env.registry:
            raise RuntimeError(
                "prerequisite: 'woow_paas_platform.skill' not registered."
            )
        cls.Skill = cls.env['woow_paas_platform.skill']
        cls.project = cls.env['project.project'].sudo().create(
            {'name': 'Skill-Drift %s' % uuid.uuid4().hex[:8]}
        )

    def _digest(self, name, version):
        return hashlib.sha256(
            ('%s-%s' % (name, version)).encode()
        ).hexdigest()

    def _latest_count(self, name):
        return self.Skill.sudo().search_count([
            ('project_id', '=', self.project.id),
            ('name', '=', name),
            ('is_latest', '=', True),
        ])

    # ---- create 直建 published 路徑（drift sample） ---------------------- #
    def test_create_published_directly_sets_is_latest_and_published_at(self):
        """直接 create state=published（繞過 action_publish）→ 修法後
        is_latest 必為 True、published_at 必有時戳。"""
        skill = self.Skill.sudo().create({
            'name': 'alpha-create',
            'version': '1.0.0',
            'project_id': self.project.id,
            'bundle_sha256': self._digest('alpha-create', '1.0.0'),
            'state': 'published',
        })
        self.assertEqual(skill.state, 'published')
        self.assertTrue(
            skill.is_latest,
            "create({'state':'published'}) 必須自動 recompute is_latest "
            "（drift 防護：spec §①）",
        )
        self.assertTrue(
            skill.published_at,
            "create({'state':'published'}) 必須自動補 published_at",
        )

    # ---- write 改 state 路徑（drift sample） ---------------------------- #
    def test_write_state_to_published_sets_is_latest_and_published_at(self):
        """draft skill 用 write({'state':'published'}) 切換（繞過
        action_publish）→ 修法後 is_latest 必為 True、published_at 必有。"""
        skill = self.Skill.sudo().create({
            'name': 'beta-write',
            'version': '1.0.0',
            'project_id': self.project.id,
            'bundle_sha256': self._digest('beta-write', '1.0.0'),
            'state': 'draft',
        })
        self.assertFalse(skill.is_latest)
        skill.write({'state': 'published'})
        self.assertTrue(
            skill.is_latest,
            "write({'state':'published'}) 必須自動 recompute is_latest",
        )
        self.assertTrue(
            skill.published_at,
            "write({'state':'published'}) 必須自動補 published_at",
        )

    # ---- 多版本程式化建 published：不變式收斂 -------------------------- #
    def test_multiple_programmatic_published_keeps_invariant(self):
        """連續直 create 三個 published 版本 → 至多一個 is_latest=True。"""
        v1 = self.Skill.sudo().create({
            'name': 'gamma-multi', 'version': '1.0.0',
            'project_id': self.project.id,
            'bundle_sha256': self._digest('gamma-multi', '1.0.0'),
            'state': 'published',
        })
        v2 = self.Skill.sudo().create({
            'name': 'gamma-multi', 'version': '2.0.0',
            'project_id': self.project.id,
            'bundle_sha256': self._digest('gamma-multi', '2.0.0'),
            'state': 'published',
        })
        v3 = self.Skill.sudo().create({
            'name': 'gamma-multi', 'version': '3.0.0',
            'project_id': self.project.id,
            'bundle_sha256': self._digest('gamma-multi', '3.0.0'),
            'state': 'published',
        })
        self.assertEqual(
            self._latest_count('gamma-multi'), 1,
            "至多一個 is_latest=True per (project, name) group",
        )
        # 最新 create 的 v3 應為 winner（同秒粒度時 id desc tiebreak）
        v3.invalidate_recordset()
        self.assertTrue(v3.is_latest, "最新 published 應為 latest（id desc tiebreak）")

    # ---- published_at 已給定時不被覆蓋 --------------------------------- #
    def test_create_published_with_explicit_published_at_keeps_it(self):
        """create 時若顯式給 published_at，hook 不應覆蓋（只在缺時補）。"""
        from datetime import datetime
        explicit_ts = datetime(2025, 1, 1, 12, 0, 0)
        skill = self.Skill.sudo().create({
            'name': 'delta-explicit',
            'version': '1.0.0',
            'project_id': self.project.id,
            'bundle_sha256': self._digest('delta-explicit', '1.0.0'),
            'state': 'published',
            'published_at': explicit_ts,
        })
        self.assertEqual(
            skill.published_at, explicit_ts,
            "顯式 published_at 不應被 hook 覆蓋（只在缺時補）",
        )
        self.assertTrue(skill.is_latest)

    # ---- 非 published 不觸發 hook（無副作用） -------------------------- #
    def test_create_draft_does_not_set_is_latest_or_published_at(self):
        """create draft skill → is_latest 維持 False、published_at 維持 NULL。"""
        skill = self.Skill.sudo().create({
            'name': 'eps-draft',
            'version': '1.0.0',
            'project_id': self.project.id,
            'bundle_sha256': self._digest('eps-draft', '1.0.0'),
            'state': 'draft',
        })
        self.assertFalse(skill.is_latest)
        self.assertFalse(skill.published_at)

    # ---- 既有 action_publish 行為仍正確（hook 不重複疊加破壞） ---------- #
    def test_action_publish_still_works_after_hook(self):
        """確認新 hook 不破壞既有 action_publish 行為（防迴歸）。"""
        skill = self.Skill.sudo().create({
            'name': 'zeta-action', 'version': '1.0.0',
            'project_id': self.project.id,
            'bundle_sha256': self._digest('zeta-action', '1.0.0'),
            'state': 'draft',
        })
        skill.action_publish()
        self.assertEqual(skill.state, 'published')
        self.assertTrue(skill.is_latest)
        self.assertTrue(skill.published_at)
```

### Step 2: 註冊 test module

Edit `src/tests/__init__.py` — 在 `test_skill_admin_views` import 後加：

```python
from . import test_skill_admin_views                       # skill backend views smoke
from . import test_skill_is_latest_drift_regression        # is_latest drift regression
```

### Step 3: 跑測試確認紅

```bash
kubectl --context docker-desktop exec -n dev-sandbox-feat-skill-list \
  deploy/feat-skill-list-odoo-dev-sandbox-odoo -- \
  odoo -u woow_paas_platform --test-enable \
       --test-tags /woow_paas_platform:TestSkillIsLatestDriftRegression \
       --stop-after-init --http-port=18069 --gevent-port=18072 \
       -d woow_feat_skill_list -c /etc/odoo/odoo.conf
```

Expected: 4 紅（drift_directly / write_state / multiple_programmatic / explicit_published_at 失敗 — create/write override 還沒收斂 recompute）+ 2 綠（draft_no_op / action_publish_still_works，既有行為）。

### Step 4: 實作 model 修改

Edit `src/models/skill.py` — 修改 `create()` 與 `write()` override：

**`create()`**（修改 `:103-108`）：

```python
@api.model_create_multi
def create(self, vals_list):
    skills = super().create(vals_list)
    for skill in skills:
        skill._anchor_bundle_attachment()
        # Drift 防護（spec §①）：若直接建 state=published（繞過
        # action_publish），補 published_at 並維護 is_latest 不變式。
        if skill.state == 'published':
            if not skill.published_at:
                skill.published_at = fields.Datetime.now()
            skill._recompute_is_latest(skill.project_id.id, skill.name)
    return skills
```

**`write()`**（修改 `:110-115`）：

```python
def write(self, vals):
    result = super().write(vals)
    if 'bundle_attachment_id' in vals:
        for skill in self:
            skill._anchor_bundle_attachment()
    # Drift 防護（spec §①）：若 vals 含 state（含程式化路徑），補
    # published_at（若結果 state=published 且缺）+ recompute。
    # _recompute_is_latest 內部 write 的 vals 是 {'is_latest': ...}，
    # 不含 state → 不踩此條件，自然不遞迴。
    if 'state' in vals:
        for skill in self:
            if skill.state == 'published' and not skill.published_at:
                skill.published_at = fields.Datetime.now()
            skill._recompute_is_latest(skill.project_id.id, skill.name)
    return result
```

### Step 5: 跑測試確認綠（全 skill 相關 test）

```bash
# 1. 跑 drift regression（本 task 新增）
kubectl --context docker-desktop exec -n dev-sandbox-feat-skill-list \
  deploy/feat-skill-list-odoo-dev-sandbox-odoo -- \
  odoo -u woow_paas_platform --test-enable \
       --test-tags /woow_paas_platform:TestSkillIsLatestDriftRegression \
       --stop-after-init -d woow_feat_skill_list -c /etc/odoo/odoo.conf

# 2. 跑 PR #81 既有 lifecycle test 防迴歸（hook 不該破壞既有 action_*）
kubectl --context docker-desktop exec -n dev-sandbox-feat-skill-list \
  deploy/feat-skill-list-odoo-dev-sandbox-odoo -- \
  odoo -u woow_paas_platform --test-enable \
       --test-tags /woow_paas_platform:TestSkillAdminLifecycle \
       --stop-after-init -d woow_feat_skill_list -c /etc/odoo/odoo.conf
```

Expected: `TestSkillIsLatestDriftRegression` 全 PASS（6 個 test）+ `TestSkillAdminLifecycle` 仍綠（10 個 test 全 PASS，無迴歸）。

### Step 6: Commit

```bash
git add src/models/skill.py src/tests/test_skill_is_latest_drift_regression.py src/tests/__init__.py
git commit -m "fix(skill): close is_latest drift on create/write to published state"
```

---

## Task 2: Migration + manifest bump + 改硬編 assertion

**Files:**
- Create: `src/migrations/18.0.1.0.28/post-fix-is-latest.py`
- Modify: `src/__manifest__.py:3`
- Modify: `src/tests/test_agent_pod_phase_c_skill_bundle.py:201` + setUp comment

### Step 1: 寫 migration

Create `src/migrations/18.0.1.0.28/post-fix-is-latest.py`:

```python
"""Backfill is_latest invariant for all skills (idempotent).

Spec: docs/superpowers/specs/2026-05-28-skill-e2e-and-is-latest-drift-fix.md §②

幂等：對每個 (project_id, name) group 重算 is_latest，保證不變式
（at most one is_latest=True per group, must be the most-recently-published
published skill, by published_at desc + id desc tiebreak）。

跑兩次結果一致（所有 skill 已 desired，內部 if-guard 不寫 → no-op）。
無資料 → early return no-op。

Rollback safety: is_latest 是 derived 欄位，重算即修正；不需 down migration。
若 rollback model code（移除 create/write recompute），舊資料仍正確直到
下次 transition 自動重算。
"""
from odoo import api, SUPERUSER_ID, fields as o_fields


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Skill = env['woow_paas_platform.skill'].sudo()
    all_skills = Skill.search([])
    if not all_skills:
        return  # 無資料 no-op
    epoch = o_fields.Datetime.to_datetime('1970-01-01 00:00:00')
    groups = {}
    for s in all_skills:
        groups.setdefault((s.project_id.id, s.name), []).append(s)
    for (project_id, name), members in groups.items():
        published = [s for s in members if s.state == 'published']
        winner = sorted(
            published,
            key=lambda s: (s.published_at or epoch, s.id),
            reverse=True,
        )[:1]
        winner_id = winner[0].id if winner else None
        for s in members:
            desired = s.id == winner_id
            if s.is_latest != desired:
                s.is_latest = desired
```

### Step 2: Bump manifest 27→28

Edit `src/__manifest__.py`, line 3:

```python
# 從：
"version": "18.0.1.0.27",
# 改為：
"version": "18.0.1.0.28",
```

### Step 3: 改 Phase C 硬編 assertion 為 monotonic

Edit `src/tests/test_agent_pod_phase_c_skill_bundle.py` — `test_MIG1_manifest_version_bumped_to_27`（約 :194-206）:

```python
def test_MIG1_manifest_version_bumped_to_27(self):
    """MIG.1: __manifest__.py declares >= 18.0.1.0.27 (monotonic).

    用 monotonic >= 鎖 intent，不硬編當時 phase 的快照值；本次與未來
    每次 bump 都不撞紅（phase-versioned-test-assertion pattern，
    docs/decisions/patterns/phase-versioned-test-assertion.md）。
    """
    import re
    manifest_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), '..', '__manifest__.py')
    )
    with open(manifest_path, 'r', encoding='utf-8') as f:
        content = f.read()
    m = re.search(r'"version":\s*"18\.0\.1\.0\.(\d+)"', content)
    self.assertIsNotNone(
        m, "MIG.1: manifest must declare an 18.0.1.0.N version",
    )
    self.assertGreaterEqual(
        int(m.group(1)), 27,
        "MIG.1: manifest version must be >= 18.0.1.0.27 (Phase C anchor; "
        "monotonic — future phases may bump higher).",
    )
```

### Step 4: 加 fixture 行為註解（spec §⑤）

Edit `src/tests/test_agent_pod_phase_c_skill_bundle.py` — 在 `setUp` 的 `self.skill = self.Skill.sudo().create({...})` 上方加註解（約 :261 之前）：

```python
# Note (post 2026-05-28 drift fix, spec
# docs/superpowers/specs/2026-05-28-skill-e2e-and-is-latest-drift-fix.md):
# 此 fixture 直接 create({'state':'published'}) 走的是「程式化捷徑」，
# 修法後 Skill.create() override 會自動觸發 _recompute_is_latest 並補
# published_at → 該 fixture skill 現在 is_latest=True、published_at 有值。
# 但 download endpoint 用 state == 'published'（非 is_latest）判定，所以
# SB.1-6 / C-1 既有斷言不受影響。改走 action_publish 為可選 follow-up。
self.skill = self.Skill.sudo().create({
    'name': f'deploy-{self.suffix}',
    ...
```

### Step 5: 跑全模組 `-u` 確認 migration 跑 + 既有 Phase C test 仍綠

```bash
bash .claude/skills/k8s-dev-sandbox/scripts/test.sh feat-skill-list --module woow_paas_platform
```

Expected:
- `-u` 期間 migration 18.0.1.0.28/post-fix-is-latest.py 被執行（無資料時 no-op 不爆）
- `TestPhaseCAgentSessionFields::test_MIG1_manifest_version_bumped_to_27` 綠（monotonic `>= 27` 對 28 成立）
- `TestPhaseCSkillBundleEndpoint`（SB.1-6 + C-1）全綠（fixture is_latest 行為改變不影響 state-based 斷言）
- `TestSkillAdminLifecycle`（PR #81 10 個 test）全綠
- `TestSkillIsLatestDriftRegression`（Task 1 新增 6 個 test）全綠

### Step 6: Commit

```bash
git add src/migrations/18.0.1.0.28/post-fix-is-latest.py \
        src/__manifest__.py \
        src/tests/test_agent_pod_phase_c_skill_bundle.py
git commit -m "feat(skill): add idempotent migration to backfill is_latest invariant"
```

---

## Task 3: 後端 HttpCase e2e — lifecycle ↔ download ↔ is_latest 串接

**Files:**
- Create: `src/tests/test_skill_e2e_lifecycle_download.py`
- Modify: `src/tests/__init__.py`

### Step 1: 寫 e2e test class（HttpCase）

Create `src/tests/test_skill_e2e_lifecycle_download.py`:

```python
"""E2E — skill lifecycle ↔ download endpoint visibility ↔ is_latest invariant.

Spec: docs/superpowers/specs/2026-05-28-skill-e2e-and-is-latest-drift-fix.md §④-a

測什麼：
- lifecycle transition（action_publish / deprecate / archive_skill）→
  既有 download endpoint /api/agent-runtimes/skill-bundle 可見性切換
- drift 防護的 HTTP 鏈路驗證：直接 create/write({'state':'published'}) →
  download 立刻 200（state gate 過）+ is_latest=True（修法後）
- migration 對 drift 樣本的修補

不測什麼（信任既有覆蓋）：
- SB.1-6 / C-1（token auth / token_use scope / 跨 project 防護）→
  既有 TestPhaseCSkillBundleEndpoint 覆蓋
- lifecycle 純單元邏輯（多版本 recompute / illegal guards）→
  PR #81 TestSkillAdminLifecycle 覆蓋
- HTTP 上傳 API → 不存在（scope 不擴）
"""
import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from urllib.parse import urlencode

from odoo.tests.common import HttpCase, tagged


# 借 Phase C 的 HS256 signer / token-crafter（pattern 對齊，不重造）
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _sign_hs256(payload: dict, secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    seg = _b64url(json.dumps(header).encode()) + "." + _b64url(json.dumps(payload).encode())
    sig = hmac.new(secret.encode(), seg.encode(), hashlib.sha256).digest()
    return seg + "." + _b64url(sig)


def _skill_bundle_token(project_id, secret, exp_offset=3600):
    now = int(time.time())
    return _sign_hs256({
        "iss": "paas-operator",
        "project_id": project_id,
        "workspace_ids": [],
        "iat": now,
        "exp": now + exp_offset,
        "jti": uuid.uuid4().hex,
        "token_use": "skill_bundle",
    }, secret)


_AUTH_SECRET = "e2e-skill-lifecycle-secret-0123456789"
_ENV_SECRET_CURRENT = "AGENT_RUNTIME_AUTH_SECRET_CURRENT"
_ENV_SECRET_PREVIOUS = "AGENT_RUNTIME_AUTH_SECRET_PREVIOUS"


@tagged('post_install', '-at_install')
class TestSkillE2ELifecycleDownload(HttpCase):
    """E2E lifecycle ↔ download visibility ↔ is_latest."""

    _ENDPOINT = '/api/agent-runtimes/skill-bundle'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for required in ('woow_paas_platform.skill',):
            if required not in cls.env.registry:
                raise RuntimeError(
                    f"prerequisite: {required} not registered."
                )
        cls.Project = cls.env['project.project']
        cls.Skill = cls.env['woow_paas_platform.skill']
        cls.Attachment = cls.env['ir.attachment']

    def _set_env(self, key, value):
        prev = os.environ.get(key)
        os.environ[key] = value
        self.addCleanup(
            lambda: os.environ.__setitem__(key, prev) if prev is not None
            else os.environ.pop(key, None)
        )

    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:8]
        self._set_env(_ENV_SECRET_CURRENT, _AUTH_SECRET)
        self._set_env(_ENV_SECRET_PREVIOUS, "previous-not-used")
        self.project = self.Project.sudo().create(
            {'name': f'E2E-Proj {self.suffix}'}
        )

    def _make_skill(self, name, version, state='draft',
                    bundle_bytes=None):
        """Build a skill + attached bundle. state default draft."""
        if bundle_bytes is None:
            bundle_bytes = (b'\x1f\x8b\x08RAW-' + name.encode()
                            + b'-' + version.encode())
        skill = self.Skill.sudo().create({
            'name': name,
            'version': version,
            'project_id': self.project.id,
            'bundle_sha256': hashlib.sha256(bundle_bytes).hexdigest(),
            'state': state,
        })
        att = self.Attachment.sudo().create({
            'name': f'{name}-{version}.tar.gz',
            'datas': base64.b64encode(bundle_bytes),
            'res_model': 'woow_paas_platform.skill',
            'res_id': skill.id,
            'mimetype': 'application/gzip',
            'public': False,
        })
        skill.sudo().write({'bundle_attachment_id': att.id})
        return skill, bundle_bytes

    def _get_bundle(self, skill_name, version):
        token = _skill_bundle_token(self.project.id, _AUTH_SECRET)
        url = f"{self._ENDPOINT}?" + urlencode({
            'project_hash': 'p-irrelevant',
            'skill_name': skill_name,
            'version': version,
        })
        return self.url_open(
            url, headers={'Authorization': f'Bearer {token}'}, timeout=15,
        )

    # -- lifecycle ↔ download visibility ------------------------------- #

    def test_draft_skill_download_returns_404(self):
        """state=draft → download endpoint 404（state gate 過濾）。"""
        skill, _ = self._make_skill('alpha', '1.0.0', state='draft')
        resp = self._get_bundle('alpha', '1.0.0')
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(skill.is_latest)

    def test_action_publish_then_download_200_and_is_latest_true(self):
        """draft → action_publish → download 200 + raw bytes + is_latest=True。"""
        skill, raw = self._make_skill('beta', '1.0.0', state='draft')
        skill.action_publish()
        resp = self._get_bundle('beta', '1.0.0')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, raw,
                         "body 必為 raw bundle bytes（非 base64）")
        skill.invalidate_recordset()
        self.assertTrue(skill.is_latest)
        self.assertTrue(skill.published_at)

    def test_deprecate_then_download_404_and_latest_reassigned(self):
        """v1+v2 publish（v2 latest）→ deprecate v2 → v2 download 404、
        v2 is_latest=False、v1 接手 is_latest=True。"""
        v1, _ = self._make_skill('gamma', '1.0.0')
        v1.action_publish()
        v2, _ = self._make_skill('gamma', '2.0.0')
        v2.action_publish()
        v2.action_deprecate()
        resp = self._get_bundle('gamma', '2.0.0')
        self.assertEqual(resp.status_code, 404)
        v1.invalidate_recordset()
        v2.invalidate_recordset()
        self.assertFalse(v2.is_latest)
        self.assertTrue(v1.is_latest, "is_latest must fall back to v1")

    def test_archive_then_download_404(self):
        """publish → archive → download 404。"""
        skill, _ = self._make_skill('delta', '1.0.0')
        skill.action_publish()
        skill.action_archive_skill()
        resp = self._get_bundle('delta', '1.0.0')
        self.assertEqual(resp.status_code, 404)
        skill.invalidate_recordset()
        self.assertFalse(skill.is_latest)

    # -- drift 防護 HTTP 鏈路 ---------------------------------------- #

    def test_create_published_directly_drift_via_http(self):
        """直接 create({'state':'published'}) → download 200 + is_latest=True
        （修法後 create override 觸發 recompute）。"""
        skill, raw = self._make_skill('eps', '1.0.0', state='published')
        # 修法後 create override 已補 published_at + recompute
        skill.invalidate_recordset()
        self.assertTrue(skill.is_latest, "drift 修法後必為 latest")
        self.assertTrue(skill.published_at)
        resp = self._get_bundle('eps', '1.0.0')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, raw)

    def test_write_state_to_published_drift_via_http(self):
        """create(draft) → write({'state':'published'}) → download 200
        + is_latest=True（修法後 write override 觸發 recompute）。"""
        skill, raw = self._make_skill('zeta', '1.0.0', state='draft')
        skill.write({'state': 'published'})
        skill.invalidate_recordset()
        self.assertTrue(skill.is_latest)
        self.assertTrue(skill.published_at)
        resp = self._get_bundle('zeta', '1.0.0')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, raw)

    # -- migration drift-fix 驗證 ----------------------------------- #

    def test_migration_fixes_existing_drift_sample(self):
        """純 SQL 製造 drift（published skill 但 is_latest=False）→
        呼叫 migration 重算 → drift 修復。"""
        skill, _ = self._make_skill('eta', '1.0.0', state='draft')
        skill.action_publish()
        self.assertTrue(skill.is_latest)
        # 製造 drift：直接 SQL 把 is_latest 改 False（繞過任何 hook）
        self.env.cr.execute(
            "UPDATE woow_paas_platform_skill SET is_latest = FALSE WHERE id = %s",
            [skill.id],
        )
        skill.invalidate_recordset()
        self.assertFalse(skill.is_latest, "drift sample 製造成功")
        # 呼叫 migration（動態載入避免硬綁 module path）
        import importlib.util
        import os as _os
        manifest_dir = _os.path.dirname(__import__('odoo').addons.woow_paas_platform.__file__)
        mig_path = _os.path.join(
            manifest_dir, 'migrations', '18.0.1.0.28', 'post-fix-is-latest.py',
        )
        spec = importlib.util.spec_from_file_location("mig28", mig_path)
        mig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mig)
        mig.migrate(self.env.cr, '18.0.1.0.28')
        skill.invalidate_recordset()
        self.assertTrue(skill.is_latest, "migration 應修復 drift")
```

> **實作備註**：若 `importlib.util.spec_from_file_location` 載入 migration 因 module path 包含 `-` 等字元在某些 Odoo 版本上有 issue，fallback 方案是把 migration 函數核心邏輯抽到 `src/models/skill.py` 的 `_backfill_is_latest_invariant_for_all_skills(cls)` classmethod，migration 只是薄殼呼叫它 → test 直接呼叫 classmethod 即可。由 tester 在 Step 1 落地時擇一。

### Step 2: 註冊 test module

Edit `src/tests/__init__.py` — 在 `test_skill_is_latest_drift_regression` import 後加：

```python
from . import test_skill_is_latest_drift_regression        # is_latest drift regression
from . import test_skill_e2e_lifecycle_download            # e2e lifecycle ↔ download
```

### Step 3: 跑 e2e + 既有 test 防迴歸

```bash
# 跑本 task 新增 e2e（HttpCase 需走 -u 確保 routes 載入）
bash .claude/skills/k8s-dev-sandbox/scripts/test.sh feat-skill-list --module woow_paas_platform
```

Expected:
- `TestSkillE2ELifecycleDownload` 全 PASS（7 個 test）
- `TestPhaseCSkillBundleEndpoint`（SB.* + C-1）仍綠
- `TestSkillAdminLifecycle` 仍綠
- `TestSkillIsLatestDriftRegression` 仍綠

### Step 4: Commit

```bash
git add src/tests/test_skill_e2e_lifecycle_download.py src/tests/__init__.py
git commit -m "test(skill): add backend e2e for lifecycle, download, is_latest invariant"
```

---

## Task 4: 瀏覽器 e2e — playwright-cli 後台 form UI wiring

**Files:**
- Create: `tests/e2e/skill/skill-admin-form.spec.ts`（或 `.spec.js` / `.sh`，由 tester 在落地時擇一）
- Create: `tests/e2e/skill/README.md`（執行指南）

> **格式選擇**：playwright-cli 支援 spec（JS/TS）+ 互動腳本兩種。本 plan 範例給 TS spec；若 tester 偏好純 shell + playwright-cli 子命令互動，等價方案參見 `tests/e2e/skill/README.md` 一節（落地時補）。

### Step 1: 寫 playwright spec

Create `tests/e2e/skill/skill-admin-form.spec.ts`:

```typescript
/**
 * E2E (browser) — skill admin form transition button UI wiring.
 *
 * Spec: docs/superpowers/specs/2026-05-28-skill-e2e-and-is-latest-drift-fix.md §④-b
 *
 * 測什麼（UI wiring）：
 * - admin 登入 → Skills menu → list → 開 draft skill form
 * - draft 初態：Publish + Archive 可見、Deprecate 隱藏、badge=Draft
 * - 點 Publish → badge=Published、statusbar 推進、is_latest=True、
 *   Publish 隱藏、Deprecate 出現
 * - 點 Deprecate → badge=Deprecated、is_latest=False、Publish 重現、
 *   Deprecate 隱藏
 * - 點 Archive → badge=Archived、所有 transition button 隱藏
 *
 * 每個關鍵切換點 screenshot 落地 /tmp/skill-e2e-screenshots/。
 *
 * 不測什麼（model test / Phase C / view smoke 已覆蓋）：
 * - lifecycle 規則正確性、跨多版本 recompute、menu admin-only、
 *   bundle 下載 / token auth、ACL/ir.rule。
 *
 * 前置：
 * 1. ORM 預建一筆 draft skill（建議透過 odoo shell / fixture script）
 * 2. 改 .py 後務必 rollout restart deploy/feat-skill-list-odoo-dev-sandbox-odoo
 *    並等 rollout 完成，否則 serve 進程跑舊 code 會誤判 button 行為
 *    （leader 強調曾因此誤判 action_deprecate）
 */
import { test, expect } from '@playwright/test';

const BASE_URL = process.env.SANDBOX_BASE_URL ?? 'http://localhost';
const ADMIN_LOGIN = process.env.ADMIN_LOGIN ?? 'admin';
const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD ?? 'admin';
const DB_NAME = process.env.DB_NAME ?? 'woow_feat_skill_list';
const SKILL_NAME = process.env.SKILL_E2E_NAME ?? 'browser-e2e-skill';
const SCREENSHOT_DIR = '/tmp/skill-e2e-screenshots';

async function loginAdmin(page) {
  await page.goto(`${BASE_URL}/web/login`);
  await page.fill('input[name="db"]', DB_NAME).catch(() => {});  // 單 DB 可能無此欄位
  await page.fill('input[name="login"]', ADMIN_LOGIN);
  await page.fill('input[name="password"]', ADMIN_PASSWORD);
  await page.click('button[type="submit"]');
  await page.waitForURL(/\/odoo|\/web/);
}

async function openSkillForm(page, skillName) {
  // 進 Woow PaaS → Skills menu（admin only） → list → 開 form
  await page.goto(`${BASE_URL}/odoo`);
  // 點 Woow PaaS app（具體 selector 依 admin theme 微調）
  await page.getByRole('link', { name: /woow paas/i }).click();
  await page.getByRole('link', { name: /^skills$/i }).click();
  // list 中點該 skill 進 form
  await page.getByText(skillName, { exact: false }).first().click();
  await page.waitForSelector('.o_form_view');
}

test.describe('Skill admin form — transition button UI wiring', () => {
  test('draft → publish → deprecate → archive', async ({ page }) => {
    await loginAdmin(page);
    await openSkillForm(page, SKILL_NAME);

    // ---- draft 初態 ----
    await expect(page.locator('.o_statusbar_buttons button:has-text("Publish")'))
      .toBeVisible();
    await expect(page.locator('.o_statusbar_buttons button:has-text("Archive")'))
      .toBeVisible();
    await expect(page.locator('.o_statusbar_buttons button:has-text("Deprecate")'))
      .toBeHidden();
    // statusbar 高亮 draft
    await expect(page.locator('.o_statusbar_status .btn[data-value="draft"]'))
      .toHaveClass(/active|btn-primary/);
    await page.screenshot({ path: `${SCREENSHOT_DIR}/01-draft.png` });

    // ---- 點 Publish ----
    await page.locator('.o_statusbar_buttons button:has-text("Publish")').click();
    await page.waitForLoadState('networkidle');
    await expect(page.locator('.o_statusbar_status .btn[data-value="published"]'))
      .toHaveClass(/active|btn-primary/);
    await expect(page.locator('.o_statusbar_buttons button:has-text("Publish")'))
      .toBeHidden();
    await expect(page.locator('.o_statusbar_buttons button:has-text("Deprecate")'))
      .toBeVisible();
    // is_latest checkbox / toggle 應為 True
    // 具體 selector 依 Odoo widget；boolean field 通常是 input[type=checkbox] 或 .o_boolean_toggle
    await expect(page.locator('[name="is_latest"] input').first())
      .toBeChecked();
    await page.screenshot({ path: `${SCREENSHOT_DIR}/02-published.png` });

    // ---- 點 Deprecate ----
    await page.locator('.o_statusbar_buttons button:has-text("Deprecate")').click();
    await page.waitForLoadState('networkidle');
    await expect(page.locator('.o_statusbar_status .btn[data-value="deprecated"]'))
      .toHaveClass(/active|btn-primary/);
    await expect(page.locator('.o_statusbar_buttons button:has-text("Publish")'))
      .toBeVisible();  // deprecated 可 re-publish
    await expect(page.locator('.o_statusbar_buttons button:has-text("Deprecate")'))
      .toBeHidden();
    await expect(page.locator('[name="is_latest"] input').first())
      .not.toBeChecked();
    await page.screenshot({ path: `${SCREENSHOT_DIR}/03-deprecated.png` });

    // ---- 點 Archive ----
    await page.locator('.o_statusbar_buttons button:has-text("Archive")').click();
    await page.waitForLoadState('networkidle');
    await expect(page.locator('.o_statusbar_status .btn[data-value="archived"]'))
      .toHaveClass(/active|btn-primary/);
    await expect(page.locator('.o_statusbar_buttons button:has-text("Publish")'))
      .toBeHidden();
    await expect(page.locator('.o_statusbar_buttons button:has-text("Deprecate")'))
      .toBeHidden();
    await expect(page.locator('.o_statusbar_buttons button:has-text("Archive")'))
      .toBeHidden();
    await page.screenshot({ path: `${SCREENSHOT_DIR}/04-archived.png` });
  });
});
```

> 上面 selectors 是「合理猜測」（Odoo 18 後台 form `.o_statusbar_buttons`、`.o_statusbar_status`、`.o_form_view`），實際落地時 tester 用 playwright codegen 或 page.inspector 對齊 sandbox 內真實 DOM 為準（不同 Odoo theme 可能略異）。

### Step 2: 寫執行指南

Create `tests/e2e/skill/README.md`:

```markdown
# Skill Admin Form — Browser E2E

## 前置

1. **Sandbox 已啟動且 .py 已 rollout restart**：

   ```bash
   kubectl --context docker-desktop rollout restart \
     deploy/feat-skill-list-odoo-dev-sandbox-odoo -n dev-sandbox-feat-skill-list
   kubectl --context docker-desktop rollout status \
     deploy/feat-skill-list-odoo-dev-sandbox-odoo -n dev-sandbox-feat-skill-list
   ```

2. **取得 sandbox 對外 URL**（用 list/status 腳本看 ingress / nodePort）：

   ```bash
   bash .claude/skills/k8s-dev-sandbox/scripts/status.sh feat-skill-list
   # 把 URL 設成 SANDBOX_BASE_URL
   export SANDBOX_BASE_URL=<URL>
   ```

3. **ORM 預建 draft skill**（透過 odoo shell or REST；fixture script 待落地）：

   ```bash
   # 範例：odoo shell
   kubectl --context docker-desktop exec -it -n dev-sandbox-feat-skill-list \
     deploy/feat-skill-list-odoo-dev-sandbox-odoo -- \
     odoo shell -d woow_feat_skill_list -c /etc/odoo/odoo.conf
   # 進 shell 後：
   # project = env['project.project'].create({'name': 'e2e-proj'})
   # env['woow_paas_platform.skill'].create({'name': 'browser-e2e-skill',
   #     'version': '1.0.0', 'project_id': project.id,
   #     'bundle_sha256': 'a' * 64, 'state': 'draft'})
   # env.cr.commit()
   ```

4. **建立 screenshot 目錄**：

   ```bash
   mkdir -p /tmp/skill-e2e-screenshots
   ```

## 執行

```bash
# 用 playwright-cli skill
# 詳見 .claude/skills/playwright-cli/

cd tests/e2e/skill
playwright test skill-admin-form.spec.ts --headed=false
```

## 截圖落地

- `/tmp/skill-e2e-screenshots/01-draft.png`
- `/tmp/skill-e2e-screenshots/02-published.png`
- `/tmp/skill-e2e-screenshots/03-deprecated.png`
- `/tmp/skill-e2e-screenshots/04-archived.png`

每張用於肉眼複查 statusbar / button visibility / badge / is_latest 渲染是否正確。

## 常見 pitfall

- **改 .py 沒 rollout restart**：serve 進程跑舊 code，看到的 button 行為是舊版的 — 必須先 restart 並等 rollout status ready。
- **fixture skill 不存在**：步驟 3 沒跑或 commit。
- **base URL 不對**：sandbox ingress 與 cluster 內部 service 不同，playwright 必須走對外 URL。
```

### Step 3: 跑前置 + 跑 spec + 看截圖

```bash
# (a) rollout restart（leader 強調）
kubectl --context docker-desktop rollout restart \
  deploy/feat-skill-list-odoo-dev-sandbox-odoo -n dev-sandbox-feat-skill-list
kubectl --context docker-desktop rollout status \
  deploy/feat-skill-list-odoo-dev-sandbox-odoo -n dev-sandbox-feat-skill-list

# (b) 建立 fixture draft skill（odoo shell，見 README 步驟 3）

# (c) 跑 playwright spec
cd tests/e2e/skill
playwright test skill-admin-form.spec.ts --headed=false

# (d) 開截圖人工複查
open /tmp/skill-e2e-screenshots/
```

Expected: spec 通過 + 4 張截圖落地、視覺上 statusbar / badge / button visibility / is_latest 符合預期。

### Step 4: Commit

```bash
git add tests/e2e/skill/
git commit -m "test(skill): add browser e2e for admin form transition UI wiring"
```

---

## Self-Review

### 1. Spec coverage

| Spec 段 | 對應 task |
|---------|-----------|
| ① Model `create()` / `write()` hook | Task 1 |
| ② Migration（幂等）| Task 2 Step 1 |
| ③ Manifest bump + monotonic assertion | Task 2 Steps 2-3 |
| ④-a 後端 e2e HttpCase | Task 3 |
| ④-b 瀏覽器 e2e playwright | Task 4 |
| ⑤ 既有 fixture 加註解（不改 code） | Task 2 Step 4 |
| ⑥ 測試邊界（測什麼/不測什麼） | Tasks 3/4 開頭 docstring 已明示 |

全段覆蓋，無遺漏。

### 2. Acceptance criteria 對應

| Spec AC | 對應 test |
|---------|-----------|
| AC1 — admin form button UI 反映 state | Task 4 playwright spec |
| AC2 — 阻止程式化 drift（create/write 直建 published 仍維持不變式） | Task 1 regression + Task 3 HTTP 鏈路 drift test |
| AC3 — migration 幂等 + drift 修補 | Task 3 `test_migration_fixes_existing_drift_sample` |
| AC4 — lifecycle ↔ download 可見性切換 | Task 3 publish/deprecate/archive download tests |
| AC5 — 既有 SB.* + C-1 + lifecycle 無迴歸 | Task 2 Step 5 + Task 3 Step 3 全模組 -u |

### 3. Placeholder / TBD scan

- Task 3 migration test 動態載入 module 路徑備援方案（抽 classmethod）已標出，由 tester 擇一。
- Task 4 playwright selectors 是合理猜測，需 tester 對 sandbox 真實 DOM 微調（用 playwright codegen）。
- Task 4 fixture 建立透過 odoo shell（README 給範例），具體落地可由 tester 改 Python fixture script。
- 其餘 code 完整。

### 4. Naming consistency

- `_recompute_is_latest` / `published_at` / `is_latest` / `action_publish` / `action_deprecate` / `action_archive_skill` — 在 test / model / migration / view 中命名一致（PR #81 已對齊，本 plan 沿用）。
- xmlid：本 plan 不新增 xmlid。
- migration 路徑：`src/migrations/18.0.1.0.28/post-fix-is-latest.py` — 對齊 Odoo migration 慣例（`migrations/<version>/post-<descriptor>.py`）。

### 5. Known boundaries / 待實作時驗證

- `_recompute_is_latest` 內部 write 的 vals 是 `{'is_latest': ...}`，不踩新 write override 的 `'state' in vals` 條件 → 不遞迴。實作完 Task 1 跑 PR #81 既有 lifecycle 10 個 test 應仍綠（防迴歸）。
- migration 路徑帶 `-`（`post-fix-is-latest.py`），from-file dynamic import 可能在某些 Odoo 版本上有 issue；Task 3 給的 `importlib.util` 寫法應可工作，若不行，fallback 為把核心邏輯抽 `Skill._backfill_is_latest_invariant_for_all_skills` classmethod。
- 瀏覽器 e2e 跑前必 rollout restart — Task 4 README 已明示，但仍有手動執行者忘記的風險；建議封裝成 wrapper script（可選 follow-up）。
- fixture 行為改變（`test_agent_pod_phase_c.py:261` setUp）— Task 2 Step 4 加註解，但既有 SB.* 不受影響（state-based gate），已驗證。

### 6. Out-of-scope confirmation

- 不擴 HTTP 上傳 API（Gap 3 拍板 (A) 不採 (B)/(C)）
- 不動 skill ACL / ir.rule / std field 型態
- 不動 PR #81 既有 spec / plan / lifecycle test code
- 不盤點 prod 資料（使用者拍板靠幂等 migration）
- 不改 `test_agent_pod_phase_c.py:setUp` 走 action_publish（spec §⑤ 列可選 follow-up）

---

## 完成判準（hand-off 回 phase-leader 的條件）

- Tasks 1-4 全部 commit、CI（本機 sandbox -u）綠
- Task 3/4 e2e 全 PASS、Task 4 截圖落地且視覺驗證符合
- PR #81 既有 test（lifecycle 10 + view smoke 2 + Phase C 11+）全綠（無迴歸）
- Migration 跑於 `-u` 全模組期間（無資料 no-op、有資料修補成功）
- `__manifest__.py` 版本為 `18.0.1.0.28`、Phase C monotonic assertion 不撞紅
- 兩個新 test module 已註冊於 `src/tests/__init__.py`
- 文件路徑 `tests/e2e/skill/` 含 spec + README

完成後 phase-leader 接手帶 PR、開 issue / pull request、視需要併入 main。

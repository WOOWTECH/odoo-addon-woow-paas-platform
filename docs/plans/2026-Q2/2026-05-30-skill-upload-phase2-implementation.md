# Skill Upload Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a backend wizard that lets a platform admin upload a `.tar.gz` skill bundle (filling name/version manually) to create a draft **system** skill — Odoo stores the bytes and computes sha256, never parsing the bundle.

**Architecture:** A `TransientModel` wizard (`woow_paas_platform.skill_upload_wizard`) in the existing `src/wizard/` dir. Its `action_upload` base64-decodes the binary, enforces a 10 MB cap, computes sha256 over the raw bytes, rejects duplicate (system, name, version), creates an anchored `ir.attachment` + a draft `scope='system'` skill via sudo, and opens the new skill's form. Exposed through the Skills list/form ⚙️ Action dropdown via an `act_window` binding. No tar/gzip parsing anywhere in Odoo.

**Tech Stack:** Odoo 18, Python (`base64`, `hashlib`), XML wizard view + `act_window` binding, `ir.model.access.csv`.

**Spec:** `docs/plans/2026-Q2/2026-05-30-skill-upload-phase2-design.md`

**Branch:** continue on `feat/skill-dual-scope` (Phase 1 + fixes live here; PR #96 open to develop). Phase 2 commits stack on top.

---

## File Structure

| File | Responsibility | Action |
|------|----------------|--------|
| `src/wizard/skill_upload_wizard.py` | wizard model + `action_upload` | Create |
| `src/wizard/__init__.py` | import the new wizard | Modify |
| `src/security/ir.model.access.csv` | wizard ACL row (base.group_system) | Modify |
| `src/views/skill_views.xml` | wizard form + `action_skill_upload_wizard` ⚙️ binding | Modify |
| `src/tests/test_skill_upload_wizard.py` | upload behaviour + sha256 contract + no-extract lock | Create |
| `src/tests/__init__.py` | import the new test | Modify |
| `docs/guides/paas-skill-system/03-skill-design.md` | document the backend upload flow | Modify |

**Test command (k8s-dev-sandbox, docker-desktop):**
```bash
export PAAS_CI_ADDONS_PATH=/Users/eugene/Documents/woow/woow-paas-repo/paas-odoo-ci/addons
bash .claude/skills/k8s-dev-sandbox/scripts/create.sh --context docker-desktop      # if no sandbox yet
bash .claude/skills/k8s-dev-sandbox/scripts/test.sh feat-skill-dual-scope --module woow_paas_platform
```
Single class during dev (faster) — run inside the odoo pod:
```bash
kubectl --context docker-desktop exec -n dev-sandbox-feat-skill-dual-scope deploy/feat-skill-dual-scope-odoo-dev-sandbox-odoo -- \
  odoo -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestSkillUploadWizard --stop-after-init --no-http -d woow_feat_skill_dual_scope
```

---

## Task 1: Wizard model + ACL

**Files:**
- Create: `src/wizard/skill_upload_wizard.py`
- Modify: `src/wizard/__init__.py`
- Modify: `src/security/ir.model.access.csv`
- Create: `src/tests/test_skill_upload_wizard.py`
- Modify: `src/tests/__init__.py`

- [ ] **Step 1: Write the failing tests**

Create `src/tests/test_skill_upload_wizard.py`:

```python
import base64
import hashlib
import io
import tarfile
import uuid

from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


def _make_targz(files):
    """Build an in-memory .tar.gz from {path: text} and return raw bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tf:
        for path, content in files.items():
            data = content.encode('utf-8')
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@tagged('post_install', '-at_install')
class TestSkillUploadWizard(TransactionCase):
    """Backend wizard uploads a .tar.gz → draft system skill (Odoo stores
    bytes only, never parses the bundle)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Wizard = cls.env['woow_paas_platform.skill_upload_wizard']
        cls.Skill = cls.env['woow_paas_platform.skill']

    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:8]
        self.raw = _make_targz({
            'SKILL.md': '---\nname: demo\nversion: 0.1.0\n---\n# Demo\n',
            'src/main.py': 'def run(x=""):\n    return x\n',
        })

    def _new_wizard(self, **overrides):
        vals = {
            'name': f'up-{self.suffix}',
            'version': '0.1.0',
            'title': 'Uploaded Skill',
            'description': 'via wizard',
            'bundle_file': base64.b64encode(self.raw),
            'bundle_filename': 'demo.tar.gz',
        }
        vals.update(overrides)
        return self.Wizard.create(vals)

    def test_upload_creates_draft_system_skill(self):
        wiz = self._new_wizard()
        wiz.action_upload()
        skill = self.Skill.search([
            ('name', '=', f'up-{self.suffix}'), ('version', '=', '0.1.0')])
        self.assertEqual(len(skill), 1)
        self.assertEqual(skill.scope, 'system')
        self.assertFalse(skill.project_id)
        self.assertEqual(skill.state, 'draft')
        self.assertEqual(skill.title, 'Uploaded Skill')
        self.assertEqual(skill.description, 'via wizard')
        # attachment anchored by skill.create() hook (Phase 1)
        self.assertTrue(skill.bundle_attachment_id)
        self.assertEqual(
            skill.bundle_attachment_id.res_model, 'woow_paas_platform.skill')
        self.assertEqual(skill.bundle_attachment_id.res_id, skill.id)
        self.assertFalse(skill.bundle_attachment_id.public)

    def test_sha256_matches_raw_bytes(self):
        wiz = self._new_wizard()
        wiz.action_upload()
        skill = self.Skill.search([('name', '=', f'up-{self.suffix}')], limit=1)
        self.assertEqual(
            skill.bundle_sha256, hashlib.sha256(self.raw).hexdigest(),
            "sha256 must be computed over the raw tar.gz bytes (pod contract)")

    def test_bundle_size_kb_computed(self):
        wiz = self._new_wizard()
        wiz.action_upload()
        skill = self.Skill.search([('name', '=', f'up-{self.suffix}')], limit=1)
        self.assertEqual(skill.bundle_size_kb, -(-len(self.raw) // 1024))

    def test_duplicate_name_version_hard_error(self):
        self._new_wizard().action_upload()
        with self.assertRaises(UserError):
            self._new_wizard().action_upload()

    def test_oversize_bundle_rejected(self):
        big = base64.b64encode(b'x' * (10 * 1024 * 1024 + 1))
        wiz = self._new_wizard(bundle_file=big)
        with self.assertRaises(UserError):
            wiz.action_upload()

    def test_returns_form_action_to_new_skill(self):
        wiz = self._new_wizard()
        action = wiz.action_upload()
        skill = self.Skill.search([('name', '=', f'up-{self.suffix}')], limit=1)
        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(action['res_model'], 'woow_paas_platform.skill')
        self.assertEqual(action['res_id'], skill.id)

    def test_upload_does_not_extract_bundle(self):
        # Feed non-tar.gz random bytes: Odoo must NOT try to parse it.
        # The skill is still created; sha256 is computed over the raw bytes.
        junk = b'this is not a tar gz at all \x00\x01\x02'
        wiz = self._new_wizard(bundle_file=base64.b64encode(junk))
        wiz.action_upload()
        skill = self.Skill.search([('name', '=', f'up-{self.suffix}')], limit=1)
        self.assertEqual(skill.state, 'draft')
        self.assertEqual(
            skill.bundle_sha256, hashlib.sha256(junk).hexdigest(),
            "Odoo stores bytes verbatim and does not validate bundle structure")
```

- [ ] **Step 2: Register the test module**

In `src/tests/__init__.py`, add after the last line (`from . import test_skill_is_latest_drift_regression`):

```python
from . import test_skill_upload_wizard
```

- [ ] **Step 3: Run tests to verify they fail**

Run (inside the odoo pod, see Test command above):
```bash
... --test-tags /woow_paas_platform:TestSkillUploadWizard ...
```
Expected: FAIL/ERROR — `woow_paas_platform.skill_upload_wizard` model does not exist yet (KeyError on `self.env[...]` in setUpClass).

- [ ] **Step 4: Create the wizard model**

Create `src/wizard/skill_upload_wizard.py`:

```python
import base64
import hashlib

from odoo import fields, models
from odoo.exceptions import UserError


class SkillUploadWizard(models.TransientModel):
    _name = 'woow_paas_platform.skill_upload_wizard'
    _description = 'Upload Skill Bundle (system scope)'

    MAX_BUNDLE_SIZE = 10 * 1024 * 1024  # 10 MB

    name = fields.Char(
        string='Skill Name',
        required=True,
        help='Slug; must match the name in the bundle SKILL.md.',
    )
    version = fields.Char(string='Version', required=True, default='0.1.0')
    title = fields.Char(string='Display Title')
    description = fields.Text(string='Description')
    bundle_file = fields.Binary(string='Skill Bundle (.tar.gz)', required=True)
    bundle_filename = fields.Char(string='Filename')

    def action_upload(self):
        """Store the uploaded bundle bytes as a draft system skill.

        Odoo never parses the bundle: it decodes the binary, enforces a size
        cap, computes sha256 over the RAW bytes (the digest the pod verifies),
        and persists an anchored attachment + a draft scope='system' skill.
        """
        self.ensure_one()
        raw = base64.b64decode(self.bundle_file)
        if len(raw) > self.MAX_BUNDLE_SIZE:
            raise UserError("Bundle exceeds the 10 MB limit.")

        bundle_sha256 = hashlib.sha256(raw).hexdigest()
        bundle_size_kb = -(-len(raw) // 1024)  # ceil division

        Skill = self.env['woow_paas_platform.skill'].sudo()
        if Skill.search([
            ('scope', '=', 'system'),
            ('name', '=', self.name),
            ('version', '=', self.version),
        ], limit=1):
            raise UserError(
                "System skill '%s' v%s already exists; bump the version."
                % (self.name, self.version)
            )

        attachment = self.env['ir.attachment'].sudo().create({
            'name': self.bundle_filename or '%s-%s.tar.gz' % (
                self.name, self.version),
            'datas': self.bundle_file,
            'mimetype': 'application/gzip',
        })
        skill = Skill.create({
            'scope': 'system',
            'project_id': False,
            'state': 'draft',
            'name': self.name,
            'version': self.version,
            'title': self.title or self.name,
            'description': self.description or '',
            'bundle_sha256': bundle_sha256,
            'bundle_size_kb': bundle_size_kb,
            'bundle_attachment_id': attachment.id,
        })
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'woow_paas_platform.skill',
            'res_id': skill.id,
            'view_mode': 'form',
            'target': 'current',
        }
```

- [ ] **Step 5: Register the wizard model import**

In `src/wizard/__init__.py`, add after `from . import change_subscription_plan_wizard`:

```python
from . import skill_upload_wizard
```

- [ ] **Step 6: Add the ACL row**

In `src/security/ir.model.access.csv`, add after the `access_change_plan_wizard` line:

```csv
access_skill_upload_wizard,woow_paas_platform.skill_upload_wizard,model_woow_paas_platform_skill_upload_wizard,base.group_system,1,1,1,1
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `... --test-tags /woow_paas_platform:TestSkillUploadWizard ...` (requires `-u woow_paas_platform` so the new model + ACL load).
Expected: PASS (7 tests).

- [ ] **Step 8: Commit**

```bash
git add src/wizard/skill_upload_wizard.py src/wizard/__init__.py \
        src/security/ir.model.access.csv \
        src/tests/test_skill_upload_wizard.py src/tests/__init__.py
git commit -m "feat: skill upload wizard model (store bytes + sha256, draft system skill)"
```

---

## Task 2: Wizard view + ⚙️ Action binding

**Files:**
- Modify: `src/views/skill_views.xml`

> XML view changes can't be unit-tested for rendering; verification is loading the module clean (no XML/parse error) + a manual UI check. The model behaviour is already covered by Task 1.

- [ ] **Step 1: Add the wizard form view + action binding**

In `src/views/skill_views.xml`, insert immediately before the closing `</odoo>` (after the `menu_skills` menuitem block):

```xml
    <!-- Skill Upload Wizard -->
    <record id="view_skill_upload_wizard_form" model="ir.ui.view">
        <field name="name">woow_paas_platform.skill_upload_wizard.form</field>
        <field name="model">woow_paas_platform.skill_upload_wizard</field>
        <field name="arch" type="xml">
            <form string="Upload Skill">
                <p class="text-muted">
                    Upload a <code>.tar.gz</code> skill bundle. Name / version
                    must match the bundle's SKILL.md. The skill is created as
                    <b>draft</b> (system scope) — publish it afterwards.
                </p>
                <group>
                    <group>
                        <field name="name"/>
                        <field name="version"/>
                    </group>
                    <group>
                        <field name="title"/>
                    </group>
                </group>
                <group>
                    <field name="description"/>
                    <field name="bundle_file" filename="bundle_filename"
                           widget="binary"/>
                    <field name="bundle_filename" invisible="1"/>
                </group>
                <footer>
                    <button name="action_upload" type="object"
                            string="Upload" class="btn-primary"/>
                    <button string="Cancel" class="btn-secondary"
                            special="cancel"/>
                </footer>
            </form>
        </field>
    </record>

    <record id="action_skill_upload_wizard" model="ir.actions.act_window">
        <field name="name">Upload Skill</field>
        <field name="res_model">woow_paas_platform.skill_upload_wizard</field>
        <field name="view_mode">form</field>
        <field name="target">new</field>
        <field name="binding_model_id" ref="model_woow_paas_platform_skill"/>
        <field name="binding_view_types">list,form</field>
    </record>
```

- [ ] **Step 2: Verify the module loads + view parses**

Run a module update (inside the odoo pod):
```bash
odoo -u woow_paas_platform --stop-after-init --no-http -d woow_feat_skill_dual_scope
```
Expected: no `ParseError` / `ValidationError` in the log; update completes (`Modules loaded.`).

- [ ] **Step 3: Manual UI check**

Port-forward and open the Skills page:
```bash
kubectl --context docker-desktop port-forward -n dev-sandbox-feat-skill-dual-scope \
  svc/feat-skill-dual-scope-odoo-dev-sandbox-nginx 8080:80
```
Open `http://localhost:8080` → login admin/admin → Woow PaaS → Skills. Confirm:
- The **⚙️ Action** dropdown (top of the list) shows **Upload Skill**.
- Clicking it opens the wizard dialog; uploading a small `.tar.gz` with name/version creates a draft skill and lands on its form.

- [ ] **Step 4: Commit**

```bash
git add src/views/skill_views.xml
git commit -m "feat: skill upload wizard view + Action-menu binding on Skills"
```

---

## Task 3: Documentation

**Files:**
- Modify: `docs/guides/paas-skill-system/03-skill-design.md`

- [ ] **Step 1: Add the backend-upload section**

In `docs/guides/paas-skill-system/03-skill-design.md`, insert after the "🔄 發布生命週期" section (before the next `##` heading):

```markdown
## ⬆️ 後台上傳 system skill

平台維運者（`base.group_system`）可在後台直接上傳 system skill bundle：

1. Woow PaaS → **Skills** → 右上 **⚙️ Action → 上傳 Skill**。
2. 填 **name / version**（須與 bundle 內 SKILL.md 一致）、選填 title / description，選 `.tar.gz`。
3. 送出 → 建立一筆 **draft** 的 system skill，停在其 form。
4. 檢查無誤後按 **Publish** 發布（之後所有 project 的 pod 即可載入）。

> Odoo 端**只儲存 bundle bytes 並計算 `bundle_sha256`**，不解壓、不解析 SKILL.md——
> 完整解壓與 sha256 驗證在 pod 端進行。因此 name / version 由上傳者負責填對；
> 同一 `(system, name, version)` 重複上傳會被擋下（請提升版號）。
```

- [ ] **Step 2: Commit**

```bash
git add docs/guides/paas-skill-system/03-skill-design.md
git commit -m "docs: document backend skill upload wizard flow"
```

---

## Task 4: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full module test suite in the seeded sandbox**

```bash
export PAAS_CI_ADDONS_PATH=/Users/eugene/Documents/woow/woow-paas-repo/paas-odoo-ci/addons
bash .claude/skills/k8s-dev-sandbox/scripts/test.sh feat-skill-dual-scope --module woow_paas_platform
```
Expected: `TestSkillUploadWizard` (7) green; the 21 Phase 1 skill tests stay green; **no new failures** vs the known baseline (oauth2 / ecpay_topup / smarthome_template / SSE5 are pre-existing environment failures unrelated to this change — confirm the failure set did not grow).

- [ ] **Step 2: Confirm no regression in the diff scope**

```bash
git diff --name-only origin/develop..HEAD
```
Expected: only wizard / skill_views / ACL / test / doc files from Phases 1–2. Any failing test outside these subsystems is pre-existing baseline, not a regression.

- [ ] **Step 3: (optional) Destroy the sandbox to free resources**

```bash
bash .claude/skills/k8s-dev-sandbox/scripts/destroy.sh feat-skill-dual-scope --context docker-desktop --force
```

---

## Self-Review Checklist (completed during plan authoring)

- **Spec coverage:** ① wizard model → T1; ② view + ⚙️ binding → T2; ③ ACL → T1 Step 6; ④ `__init__`/manifest → T1 Step 5 (manifest needs no change — wizard view lives in already-registered `skill_views.xml`, ACL csv already in data list); ⑤ tests → T1; ⑥ doc → T3. All spec sections mapped.
- **Placeholder scan:** no TBD/TODO; every code step shows complete code; `MAX_BUNDLE_SIZE`, `action_upload`, `_make_targz` defined where first used.
- **Type/name consistency:** model `woow_paas_platform.skill_upload_wizard` + method `action_upload` + fields `name/version/title/description/bundle_file/bundle_filename` identical across T1 (model), T1 (tests), T2 (view), ACL row, and `__init__` import. `binding_model_id` references `model_woow_paas_platform_skill` (the auto-generated model external id, confirmed in use by existing skill ACL rows).

## Out of scope (future phase)

`/woow` frontend user-skill upload (OWL section + multipart endpoint + project-role gate). See design doc §Non-goals.

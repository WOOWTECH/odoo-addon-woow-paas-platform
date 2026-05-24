"""TDD red phase — Phase A: 4 new Odoo models schema + key constraints.

Plan: docs/plans/2026-Q2/agent-runtime-pod-plan.md §Phase A Tasks 7-11
Design: docs/plans/2026-Q2/agent-runtime-pod-design.md
  - §2.12 ``agent_session`` model (per-project lifecycle + namespace_hash)
  - §3.2 ``skill`` model (per-project + bundle + state)
  - §4.1 ``agent_template`` model (system_prompt + bindings + is_public)
  - §4.3 ``project_access`` model (first-class ACL, partner_id + role)
  - §4.7 ``agent_session.primary_workspace_id``
  - §4.8 ``agent_session.quota_override_*``

Acceptance criteria covered:

N.1  ``woow_paas_platform.agent_template`` model registered with
     required fields (name, system_prompt, model_config_id, skill_ids
     M2M, mcp_server_ids M2M, project_ids M2M, is_public, state).
N.2  ``agent_template`` constraint ``_check_visibility_consistency`` —
     private template (is_public=False) without bound projects cannot
     transition to state='published'.
N.3  ``agent_template`` constraint ``_check_mcp_subset`` — bound
     ``mcp_server_ids`` must be subset of (project.mcp_server_ids ∪
     scope='system').
N.4  ``woow_paas_platform.agent_session`` model registered with
     project_id (M2O cascade), project_namespace_hash (Char unique),
     state (Selection), workspace_ids_snapshot, agent_template_id,
     timestamps, primary_workspace_id, quota_override_*.
N.5  ``agent_session`` SQL constraints: UNIQUE(project_id) +
     UNIQUE(project_namespace_hash) (design §2.12).
N.6  ``agent_session.project_namespace_hash`` is auto-computed on
     create from ``hmac_sha256(project_id, secret)[:10] → base32 16
     chars`` (length and char-set assertions only — secret is
     cluster-managed; tests just assert shape).
N.7  ``woow_paas_platform.skill`` model registered with name + version
     + project_id + bundle_attachment_id + bundle_sha256 + is_latest +
     state.
N.8  ``skill`` SQL constraint: UNIQUE(project_id, name, version).
N.9  ``woow_paas_platform.project_access`` model registered with
     project_id + partner_id + user_id (computed from partner_id) +
     role (Selection: owner/admin/user/guest).
N.10 ``project_access`` SQL constraint: UNIQUE(project_id, partner_id).
N.11 ``project_access._check_at_least_one_owner`` — unlinking the last
     owner row of a project raises ValidationError.
N.12 ``project.project`` create hook bootstraps an owner
     ``project_access`` row for the creating user (Plan §Phase A —
     project_access bootstrap; design §4.3).

Red expectation today (Phase A implementation has NOT started):

* N.1, N.4, N.7, N.9 → RED: ``self.env['woow_paas_platform.<model>']``
  raises KeyError because the model does not yet exist in the registry.
* All other AC depend on N.1/N.4/N.7/N.9 and are skipped when the
  prereq fails (no spurious noise).

Boundary:

* ACL behaviour of new models — covered by ``test_agent_pod_phase_a_acl.py``.
* Migration post-migrate bootstrap of existing projects' project_access —
  covered by ``test_agent_pod_phase_a_migration.py``.
* agent_template ``M:N project + skill_ids + mcp_server_ids`` cross-ref
  semantics beyond subset constraint — Phase B/C scope.
* Skill bundle upload pipeline (controller side) — Phase B/C scope.
"""
import os
import re
import uuid

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged


_BASE32_HASH_RE = re.compile(r'^[a-z2-7]{16}$')


@tagged('post_install', '-at_install')
class TestPhaseAAgentTemplateModel(TransactionCase):
    """N.1 ~ N.3 — agent_template schema and constraints."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if 'woow_paas_platform.agent_template' not in cls.env.registry:
            raise RuntimeError(
                "Phase A new model 'woow_paas_platform.agent_template' not "
                "registered. Plan §Phase A Task 7 — create "
                "src/models/agent_template.py per design §4.1."
            )
        cls.Template = cls.env['woow_paas_platform.agent_template']

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:8]
        self.suffix = suffix

    def _make_minimal_ai_config(self):
        """Acquire a valid ``ai.config`` id for required FK on template."""
        return self.env['ai.config'].sudo().search([], limit=1)

    def test_agent_template_has_required_fields(self):
        """N.1: agent_template declares the required schema."""
        fields_ = self.Template._fields
        for fname, ftype in (
            ('name', 'char'),
            ('system_prompt', 'text'),
            ('model_config_id', 'many2one'),
            ('skill_ids', 'many2many'),
            ('mcp_server_ids', 'many2many'),
            ('project_ids', 'many2many'),
            ('is_public', 'boolean'),
            ('state', 'selection'),
        ):
            self.assertIn(
                fname, fields_,
                f"N.1: agent_template missing field '{fname}'. "
                f"Design §4.1.",
            )
            self.assertEqual(
                fields_[fname].type, ftype,
                f"N.1: agent_template.{fname} expected type {ftype}.",
            )
        self.assertEqual(
            fields_['mcp_server_ids'].comodel_name,
            'woow_paas_platform.mcp_server',
        )
        self.assertEqual(
            fields_['skill_ids'].comodel_name,
            'woow_paas_platform.skill',
        )
        self.assertEqual(
            fields_['project_ids'].comodel_name,
            'project.project',
        )

    def test_agent_template_visibility_consistency(self):
        """N.2: private template (is_public=False) without project_ids
        cannot transition to state='published'.

        Red today: model absent. Post-impl: ValidationError raised by
        ``_check_visibility_consistency``.
        """
        ai_cfg = self._make_minimal_ai_config()
        if not ai_cfg:
            self.skipTest(
                "N.2 prerequisite: no ai.config available to attach to "
                "model_config_id (required FK). Seed at least one "
                "ai.config in module data."
            )

        tmpl = self.Template.sudo().create({
            'name': f'N.2 private orphan {self.suffix}',
            'is_public': False,
            'state': 'draft',
            'model_config_id': ai_cfg.id,
        })
        with self.assertRaises(
            ValidationError,
            msg="N.2: private template without project_ids must NOT "
                "publish. Design §4.1 _check_visibility_consistency.",
        ):
            tmpl.sudo().write({'state': 'published'})

    def test_agent_template_mcp_subset_constraint(self):
        """N.3: bound ``mcp_server_ids`` must be ⊆ (project allowed ∪
        system-scope).

        Red today: model absent. Post-impl: assigning a user-scope MCP
        from project NOT in template.project_ids raises ValidationError.
        """
        ai_cfg = self._make_minimal_ai_config()
        if not ai_cfg:
            self.skipTest("N.3 prerequisite: no ai.config available.")

        # Seed two unrelated projects + a user-scope MCP attached to
        # project_other → bind template to project_bound only → reject.
        Project = self.env['project.project']
        Mcp = self.env['woow_paas_platform.mcp_server']

        project_bound = Project.sudo().create({
            'name': f'N.3 bound {self.suffix}',
        })
        project_other = Project.sudo().create({
            'name': f'N.3 other {self.suffix}',
        })
        unrelated_mcp = Mcp.sudo().create({
            'name': f'N.3 unrelated MCP {self.suffix}',
            'url': 'https://mcp.example.com/sse',
            'transport': 'sse',
            'scope': 'user',
            'project_id': project_other.id,
        })

        tmpl = self.Template.sudo().create({
            'name': f'N.3 tmpl {self.suffix}',
            'state': 'draft',
            'model_config_id': ai_cfg.id,
            'project_ids': [(6, 0, [project_bound.id])],
        })
        with self.assertRaises(
            ValidationError,
            msg="N.3: template.mcp_server_ids must be subset of bound "
                "projects' user-scope MCP ∪ scope='system'. Design §4.1 "
                "_check_mcp_subset.",
        ):
            tmpl.sudo().write({
                'mcp_server_ids': [(6, 0, [unrelated_mcp.id])],
            })


@tagged('post_install', '-at_install')
class TestPhaseAAgentSessionModel(TransactionCase):
    """N.4 ~ N.6 — agent_session schema, unique constraints, hash compute."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if 'woow_paas_platform.agent_session' not in cls.env.registry:
            raise RuntimeError(
                "Phase A new model 'woow_paas_platform.agent_session' not "
                "registered. Plan §Phase A Task 8 — create "
                "src/models/agent_session.py per design §2.12."
            )
        cls.Session = cls.env['woow_paas_platform.agent_session']
        cls.Project = cls.env['project.project']

    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:8]
        # Forward-compat with the F-PC-3 fail-closed fix: creating an
        # agent_session now requires the injected namespace secret env.
        os.environ['AGENT_RUNTIME_NAMESPACE_SECRET'] = 'phase-a-test-ns-secret'
        self.addCleanup(os.environ.pop, 'AGENT_RUNTIME_NAMESPACE_SECRET', None)

    def test_agent_session_required_fields(self):
        """N.4: agent_session declares required field shape."""
        fields_ = self.Session._fields
        expectations = {
            'project_id': 'many2one',
            'project_namespace_hash': 'char',
            'state': 'selection',
            'helm_release_name': 'char',
            'helm_namespace': 'char',
            'workspace_ids_snapshot': 'char',
            'agent_template_id': 'many2one',
            'spawn_at': 'datetime',
            'last_active': 'datetime',
            'reaped_since': 'datetime',
            'primary_workspace_id': 'many2one',
            'quota_override_cpu': 'integer',
            'quota_override_memory_gi': 'integer',
            'quota_override_storage_gi': 'integer',
        }
        for fname, ftype in expectations.items():
            self.assertIn(
                fname, fields_,
                f"N.4: agent_session missing field '{fname}'. "
                f"Design §2.12 + §4.7 + §4.8.",
            )
            self.assertEqual(
                fields_[fname].type, ftype,
                f"N.4: agent_session.{fname} expected {ftype}.",
            )
        # FK 必須是 cascade（design §2.12 explicit）
        self.assertEqual(
            fields_['project_id'].ondelete, 'cascade',
            "N.4: agent_session.project_id ondelete='cascade' "
            "(design §2.12 SQL constraint).",
        )

    def test_agent_session_unique_project(self):
        """N.5: ``UNIQUE(project_id)`` — at most one session per project
        (per-project pod model).
        """
        project = self.Project.sudo().create({
            'name': f'N.5 uniq-project {self.suffix}',
        })
        self.Session.sudo().create({'project_id': project.id})
        with self.assertRaises(
            Exception,  # noqa: BLE001 — IntegrityError or ValidationError
            msg="N.5: second agent_session on same project must fail "
                "(UNIQUE(project_id) constraint). Design §2.12.",
        ):
            self.Session.sudo().create({'project_id': project.id})

    def test_agent_session_unique_namespace_hash(self):
        """N.5: ``UNIQUE(project_namespace_hash)`` — collision is rare
        but enforced (design §2.12 + §2.15 risk row 4).
        """
        project1 = self.Project.sudo().create({
            'name': f'N.5b uniq-hash A {self.suffix}',
        })
        project2 = self.Project.sudo().create({
            'name': f'N.5b uniq-hash B {self.suffix}',
        })

        s1 = self.Session.sudo().create({'project_id': project1.id})
        # Force collision by writing the same hash on a second row.
        with self.assertRaises(
            Exception,  # noqa: BLE001
            msg="N.5b: second agent_session with same "
                "project_namespace_hash must fail (UNIQUE constraint).",
        ):
            self.Session.sudo().create({
                'project_id': project2.id,
                'project_namespace_hash': s1.project_namespace_hash,
            })

    def test_agent_session_namespace_hash_shape(self):
        """N.6: hash is 16-char base32 (lower-cased, ``a-z 2-7`` set).

        Plan §Phase A Task 8 + design §2.7 — auto-computed from
        ``hmac_sha256(project_id, secret)[:10] → base32 16 chars``.
        Hash is computed once at create.
        """
        project = self.Project.sudo().create({
            'name': f'N.6 hash shape {self.suffix}',
        })
        session = self.Session.sudo().create({'project_id': project.id})
        self.assertIsNotNone(
            session.project_namespace_hash,
            "N.6: project_namespace_hash must be auto-computed on create.",
        )
        self.assertTrue(
            _BASE32_HASH_RE.match(session.project_namespace_hash or ''),
            "N.6: hash must match pattern ``[a-z2-7]{16}`` (base32 "
            "lower-cased, 16 chars). Got %r." % session.project_namespace_hash,
        )


@tagged('post_install', '-at_install')
class TestPhaseASkillModel(TransactionCase):
    """N.7, N.8 — skill schema and (project, name, version) uniqueness."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if 'woow_paas_platform.skill' not in cls.env.registry:
            raise RuntimeError(
                "Phase A new model 'woow_paas_platform.skill' not "
                "registered. Plan §Phase A Task 9 — create "
                "src/models/skill.py per design §3.2."
            )
        cls.Skill = cls.env['woow_paas_platform.skill']
        cls.Project = cls.env['project.project']

    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:8]
        self.project = self.Project.sudo().create({
            'name': f'Skill project {self.suffix}',
        })

    def test_skill_required_fields(self):
        """N.7: skill declares minimal required schema."""
        fields_ = self.Skill._fields
        for fname, ftype in (
            ('name', 'char'),
            ('version', 'char'),
            ('project_id', 'many2one'),
            ('bundle_attachment_id', 'many2one'),
            ('bundle_sha256', 'char'),
            ('is_latest', 'boolean'),
            ('state', 'selection'),
        ):
            self.assertIn(
                fname, fields_,
                f"N.7: skill missing field '{fname}'. Design §3.2.",
            )
            self.assertEqual(
                fields_[fname].type, ftype,
                f"N.7: skill.{fname} expected {ftype}.",
            )
        self.assertEqual(
            fields_['project_id'].ondelete, 'cascade',
            "N.7: skill.project_id ondelete='cascade'.",
        )

    def test_skill_unique_name_version_per_project(self):
        """N.8: UNIQUE(project_id, name, version)."""
        self.Skill.sudo().create({
            'name': 'duplicate-test',
            'version': '0.1.0',
            'project_id': self.project.id,
            'bundle_sha256': 'a' * 64,
        })
        with self.assertRaises(
            Exception,  # noqa: BLE001
            msg="N.8: duplicate (project_id, name, version) must fail.",
        ):
            self.Skill.sudo().create({
                'name': 'duplicate-test',
                'version': '0.1.0',
                'project_id': self.project.id,
                'bundle_sha256': 'b' * 64,
            })


@tagged('post_install', '-at_install')
class TestPhaseAProjectAccessModel(TransactionCase):
    """N.9 ~ N.12 — project_access schema, constraints, bootstrap."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if 'woow_paas_platform.project_access' not in cls.env.registry:
            raise RuntimeError(
                "Phase A new model 'woow_paas_platform.project_access' "
                "not registered. Plan §Phase A Task 10 — create "
                "src/models/project_access.py per design §4.3."
            )
        cls.PAccess = cls.env['woow_paas_platform.project_access']
        cls.Project = cls.env['project.project']

    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:8]

    def test_project_access_required_fields(self):
        """N.9: project_access declares partner_id / role / user_id."""
        fields_ = self.PAccess._fields
        for fname, ftype in (
            ('project_id', 'many2one'),
            ('partner_id', 'many2one'),
            ('user_id', 'many2one'),
            ('role', 'selection'),
        ):
            self.assertIn(
                fname, fields_,
                f"N.9: project_access missing field '{fname}'. "
                f"Design §4.3.",
            )
            self.assertEqual(fields_[fname].type, ftype)
        self.assertEqual(
            fields_['partner_id'].comodel_name, 'res.partner',
            "N.9: partner_id → res.partner.",
        )
        self.assertTrue(
            getattr(fields_['user_id'], 'compute', None),
            "N.9: user_id must be computed from partner_id "
            "(design §4.3 D3=A idiom).",
        )

        # Role selection values — owner/admin/user/guest.
        sel = fields_['role'].selection or []
        roles = [r[0] for r in (sel() if callable(sel) else sel)]
        for required_role in ('owner', 'admin', 'user', 'guest'):
            self.assertIn(
                required_role, roles,
                f"N.9: role selection must include '{required_role}'.",
            )

    def test_project_access_unique_partner_per_project(self):
        """N.10: UNIQUE(project_id, partner_id)."""
        project = self.Project.sudo().create({
            'name': f'N.10 project {self.suffix}',
        })
        partner = self.env['res.partner'].sudo().create({
            'name': f'N.10 partner {self.suffix}',
        })
        self.PAccess.sudo().create({
            'project_id': project.id,
            'partner_id': partner.id,
            'role': 'user',
        })
        with self.assertRaises(
            Exception,  # noqa: BLE001
            msg="N.10: duplicate (project_id, partner_id) must fail.",
        ):
            self.PAccess.sudo().create({
                'project_id': project.id,
                'partner_id': partner.id,
                'role': 'guest',
            })

    def test_project_access_last_owner_cannot_be_unlinked(self):
        """N.11: unlinking the LAST role='owner' row of a project raises
        ValidationError.

        Plan §Phase A Task 10 + design §4.3 ``_check_at_least_one_owner``.
        """
        project = self.Project.sudo().create({
            'name': f'N.11 project {self.suffix}',
        })
        owner_partner = self.env['res.partner'].sudo().create({
            'name': f'N.11 owner {self.suffix}',
        })
        owner_row = self.PAccess.sudo().create({
            'project_id': project.id,
            'partner_id': owner_partner.id,
            'role': 'owner',
        })
        with self.assertRaises(
            ValidationError,
            msg="N.11: cannot unlink the last owner role row "
                "(design §4.3 — project must always have ≥1 owner).",
        ):
            owner_row.sudo().unlink()

    def test_project_create_bootstraps_owner_project_access(self):
        """N.12: ``project.project.create()`` auto-creates a
        ``role='owner'`` project_access for the creating user's partner.

        Plan §Phase A Test scope — project_access bootstrap. Design §4.3
        "Project 自動 bootstrap owner".
        """
        # Use a fresh non-superuser to make the assertion meaningful
        # (super-user is normally auto-attached).
        user = self.env['res.users'].sudo().create({
            'name': f'N.12 user {self.suffix}',
            'login': f'n12_{self.suffix}',
            'email': f'n12_{self.suffix}@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        project = self.Project.with_user(user).sudo().create({
            'name': f'N.12 bootstrap project {self.suffix}',
        })
        access_rows = self.PAccess.sudo().search([
            ('project_id', '=', project.id),
            ('partner_id', '=', user.partner_id.id),
            ('role', '=', 'owner'),
        ])
        self.assertEqual(
            len(access_rows), 1,
            "N.12: project create must bootstrap exactly one owner "
            "project_access row for the creator. Plan §Phase A — "
            "project_access bootstrap.",
        )

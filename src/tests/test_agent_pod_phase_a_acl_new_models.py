"""TDD red phase — Phase A B-1: ir.rule behaviour for 4 new models.

CAQA BLOCKER B-1 (2026-05-22, user sign-off):

Plan §Phase A Task 13 + design §3.2 / §4.1 / §4.3 explicit require 4
rule × 4 new models = **16 ir.rule** to scope ``base.group_user``
access to records owned by the user's projects (per project_access
ACL) plus an admin_full bypass and (for agent_template) a
``is_public=True`` carve-out.

Impl shipped CSV rows for the 4 new models at ``1,1,1,1`` for
``base.group_user`` but did **not** add the corresponding ir.rule —
that leaves every authenticated user with full RWCU on every record
of these models, regardless of project ownership. CAQA caught this
during invariant verify.

Source pitfall: ``docs/tracking/pitfalls/cross-workspace-task-data-leak.md``
B1 — *first time* opening ``base.group_user`` CSV ACL on a self-owned
model MUST be paired with ir.rule scope-restriction. Self-owned
models have no std rule OR-bypass to inherit from (unlike PR #47 B1
where Odoo's project rule did the OR-leak), but the leak SEVERITY is
WORSE because the default is "allow all" — every authenticated user
defaults to admin until ir.rule narrows the scope.

Pattern: ``docs/decisions/patterns/two-layer-acl-lockstep.md`` — CSV +
ir.rule are TWO LAYERS that must be locked-step at the same commit.

Acceptance criteria (behaviour-locked, not rule-shape):

agent_template (design §4.1; 8 methods):
  B1.AT-1  Non-member cannot read private template (bound to other project)
  B1.AT-2  Non-member cannot write private template (cross-project)
  B1.AT-3  Non-member cannot create template bound to other project
  B1.AT-4  Non-member cannot unlink private template
  B1.AT-5  is_public=True template readable by ALL internal users
  B1.AT-6  is_public=True template writeable ONLY by group_system admin
  B1.AT-7  project admin can RWCU on own project template
  B1.AT-8  project user can only READ own project template (no write)

agent_session (design §2.12; 5 methods):
  B1.AS-1  Non-member cannot read agent_session of other project
  B1.AS-2  Project member can read own project agent_session
  B1.AS-3  Non-member cannot write agent_session (state / namespace_hash)
  B1.AS-4  Project user cannot modify quota_override fields (admin only)
  B1.AS-5  Non-member cannot unlink agent_session

skill (design §3.2; 8 methods):
  B1.SK-1  Non-member cannot read skill of other project (any state)
  B1.SK-2  Non-member cannot write skill of other project
  B1.SK-3  Non-member cannot create skill on other project
  B1.SK-4  Non-member cannot unlink skill of other project
  B1.SK-5  Project user (non-author) cannot write someone else's draft skill
  B1.SK-6  Project guest can read skill but not download bundle binary
           (bundle_attachment_id field-level group restriction)
  B1.SK-7  Project admin can publish skill (state draft → published)
  B1.SK-8  Project user cannot publish skill (draft → published)

project_access (design §4.3; 8 methods — MOST CRITICAL privilege
escalation surface):
  B1.PA-1  Non-member cannot read project_access of other project
  B1.PA-2  Self introspection: user can read own project_access row
  B1.PA-3  Non-member cannot create project_access for other project
           (privilege escalation block)
  B1.PA-4  User role cannot create role='owner' for any partner on any
           project (privilege escalation block — per ROLE_HIERARCHY
           ASSIGNABLE_ROLES_BY_USER = [], grantee != self to avoid
           unique(project_id, partner_id) SQL constraint masking the
           ACL gate)
  B1.PA-5  Project admin cannot grant role='admin' (per ROLE_HIERARCHY
           ASSIGNABLE_ROLES_BY_ADMIN — admin can only assign user/guest)
  B1.PA-6  Non-member cannot unlink project_access of other project
  B1.PA-7  Project admin can unlink role='user' / role='guest' on own project
  B1.PA-8  Owner is the only role that can manage other owners

Red expectation today (Phase A impl shipped models + CSV but NO ir.rule
for 4 new models):

* All B1.* deny tests fail with AccessError / behavioural mismatch
  because base.group_user has 1,1,1,1 CSV cap and NO restriction rule
  → every test user sees + modifies every record of these 4 models.
* B1.AT-5 (public read) + B1.AT-7 / B1.SK-7 / B1.PA-7 (own-project
  positive cases) probably ACCIDENTALLY pass today (allow-all gives
  them access), but they become regression locks after impl: they
  must STAY green post-impl, locking the positive-path of the new
  rules.

Boundary（刻意不測）:

* Exhaustive 4 perm × 4 role × N model matrix (256+ cells) — Phase A
  test scope picks the **representative privilege escalation cells**
  per role docstring "Boundary 取捨". Full matrix is left to phase
  B/C/D test expansion when models are populated with real fixtures.
* mcp_server / mcp_tool ACL — already covered by refactor branch
  ``test_mcp_acl_isolation.py`` + ``test_mcp_acl_unbound_isolation.py``;
  this file is for the FOUR NEW models only.
* Field-level group restriction on ``mcp_server.api_key`` / similar —
  inherited from refactor branch; B1.SK-6 covers the new
  ``skill.bundle_attachment_id`` analogue.
* agent_session lifecycle creation by paas-operator webhook — that's
  a Phase C concern (webhook auth + HMAC verify); B1.AS-3 only
  covers ORM-level non-member write deny.

Fixture / env assumptions:

* Phase A impl has shipped the 4 new models (verified by Phase A new-model
  tests N.1/N.4/N.7/N.9 PASS).
* CSV rows are in place at ``1,1,1,1`` for base.group_user (verified by
  test_agent_pod_phase_a_acl.py R.8).
* No ir.rule for the 4 new models (verified by impl CAQA report).
* refactor branch invariants (D4, Inv #9/#10/#11, 6-rule mcp split)
  unchanged — not touched by this test file.
"""
import os
import uuid
from unittest.mock import patch

from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, tagged


def _new_internal_user(env, login, name=None):
    """Create internal user (helper, mirrors existing test files)."""
    return env['res.users'].sudo().create({
        'name': name or login,
        'login': login,
        'email': f'{login}@test.example',
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })


class _NewModelAclFixture(TransactionCase):
    """Shared fixture for the four B-1 ACL test classes.

    Layout::

        W1 (Alice = owner)
          ├ Charlie (W1 workspace member + P1 project user)
          ├ Bob is NOT in W1
          ├ project P1 bound to svc_w1 (workspace W1)
          └ project_access P1: { Alice: owner, Charlie: user }

        W2 (Bob = owner)
          ├ project P2 bound to svc_w2 (workspace W2)
          └ project_access P2: { Bob: owner }

    Charlie is the "cross-project attacker" — W1 member, P1 user, but
    has NO project_access on P2. Tests assert he cannot read/write/etc.
    P2's records of the 4 new models.

    Eve is an admin (group_system) used for the public-template and
    admin-only paths.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.WAccess = cls.env['woow_paas_platform.workspace_access']
        cls.PAccess = cls.env['woow_paas_platform.project_access']
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        cls.Project = cls.env['project.project']
        cls.AgentTemplate = cls.env['woow_paas_platform.agent_template']
        cls.AgentSession = cls.env['woow_paas_platform.agent_session']
        cls.Skill = cls.env['woow_paas_platform.skill']

        # Schema guard — fail-fast if Phase A models not yet shipped.
        for required in (
            'woow_paas_platform.agent_template',
            'woow_paas_platform.agent_session',
            'woow_paas_platform.skill',
            'woow_paas_platform.project_access',
        ):
            if required not in cls.env.registry:
                raise RuntimeError(
                    f"B-1 prerequisite: model '{required}' not registered. "
                    f"Phase A impl must ship 4 new models before B-1 ACL "
                    f"tests can exercise their record rules."
                )

    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:8]

        # Users
        self.alice = _new_internal_user(self.env, f'b1_alice_{self.suffix}', 'Alice (B-1)')
        self.bob = _new_internal_user(self.env, f'b1_bob_{self.suffix}', 'Bob (B-1)')
        self.charlie = _new_internal_user(self.env, f'b1_charlie_{self.suffix}', 'Charlie (B-1)')
        self.dave = _new_internal_user(self.env, f'b1_dave_{self.suffix}', 'Dave (B-1)')
        # Eve = admin (group_system)
        self.eve = self.env['res.users'].sudo().create({
            'name': f'Eve (B-1) {self.suffix}',
            'login': f'b1_eve_{self.suffix}',
            'email': f'b1_eve_{self.suffix}@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_system').id])],
        })

        # Workspaces
        self.ws1 = self.Workspace.sudo().create({
            'name': f'B-1 W1 {self.suffix}',
            'owner_id': self.alice.id,
        })
        self.ws2 = self.Workspace.sudo().create({
            'name': f'B-1 W2 {self.suffix}',
            'owner_id': self.bob.id,
        })
        self.WAccess.sudo().create({
            'workspace_id': self.ws1.id,
            'user_id': self.charlie.id,
            'role': 'user',
        })

        # Cloud services
        self.tmpl = self.Template.sudo().create({
            'name': f'B-1 Tmpl {self.suffix}',
            'slug': f'b1-tmpl-{self.suffix}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': f'b1-tmpl-{self.suffix}',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.svc_w1 = self.Service.sudo().create({
            'name': f'B-1 Svc W1 {self.suffix}',
            'workspace_id': self.ws1.id,
            'template_id': self.tmpl.id,
            'helm_release_name': f'b1-svc-w1-{self.suffix}',
            'helm_namespace': f'paas-ws-b1-w1-{self.suffix}',
        })
        self.svc_w2 = self.Service.sudo().create({
            'name': f'B-1 Svc W2 {self.suffix}',
            'workspace_id': self.ws2.id,
            'template_id': self.tmpl.id,
            'helm_release_name': f'b1-svc-w2-{self.suffix}',
            'helm_namespace': f'paas-ws-b1-w2-{self.suffix}',
        })

        # Projects (binding via M:N created by impl) — fall back to
        # legacy M2O column if M:N isn't yet on the model fields.
        m2m_supported = 'cloud_service_ids' in self.Project._fields
        if m2m_supported:
            project_kwargs = {
                'cloud_service_ids': [(6, 0, [self.svc_w1.id])],
            }
            project_p2_kwargs = {
                'cloud_service_ids': [(6, 0, [self.svc_w2.id])],
            }
        else:
            project_kwargs = {'cloud_service_id': self.svc_w1.id}
            project_p2_kwargs = {'cloud_service_id': self.svc_w2.id}

        self.project_p1 = self.Project.sudo().create({
            'name': f'B-1 P1 (W1) {self.suffix}',
            **project_kwargs,
        })
        self.project_p2 = self.Project.sudo().create({
            'name': f'B-1 P2 (W2) {self.suffix}',
            **project_p2_kwargs,
        })

        # Project access — Alice owner of P1, Charlie user of P1, Bob owner P2.
        # NOTE: impl bootstrap may already create owner rows on create();
        # filter-then-search-or-create idiom keeps tests idempotent.
        def _ensure_paccess(project, partner, role):
            existing = self.PAccess.sudo().search([
                ('project_id', '=', project.id),
                ('partner_id', '=', partner.id),
            ], limit=1)
            if existing:
                existing.sudo().write({'role': role})
                return existing
            return self.PAccess.sudo().create({
                'project_id': project.id,
                'partner_id': partner.id,
                'role': role,
            })

        _ensure_paccess(self.project_p1, self.alice.partner_id, 'owner')
        _ensure_paccess(self.project_p1, self.charlie.partner_id, 'user')
        _ensure_paccess(self.project_p2, self.bob.partner_id, 'owner')


@tagged('post_install', '-at_install')
class TestPhaseAAclAgentTemplate(_NewModelAclFixture):
    """B1.AT-1 ~ B1.AT-8 — agent_template cross-project denial + public carve-out."""

    def setUp(self):
        super().setUp()
        ai_cfg = self.env['ai.config'].sudo().search([], limit=1)
        self._ai_cfg_id = ai_cfg.id if ai_cfg else False

        # Private template bound to P2 (W2) — Charlie (P1 user, no access
        # to P2) is the attacker.
        self.private_tmpl_p2 = self.AgentTemplate.sudo().create({
            'name': f'B-1 private P2 tmpl {self.suffix}',
            'is_public': False,
            'state': 'draft',
            'model_config_id': self._ai_cfg_id or False,
            'project_ids': [(6, 0, [self.project_p2.id])],
        })
        # Public (system) template — readable by all
        self.public_tmpl = self.AgentTemplate.sudo().create({
            'name': f'B-1 public tmpl {self.suffix}',
            'is_public': True,
            'state': 'published',
            'model_config_id': self._ai_cfg_id or False,
        })
        # Own-project template (P1 — Alice owns, Charlie is user)
        self.own_tmpl_p1 = self.AgentTemplate.sudo().create({
            'name': f'B-1 own P1 tmpl {self.suffix}',
            'is_public': False,
            'state': 'draft',
            'model_config_id': self._ai_cfg_id or False,
            'project_ids': [(6, 0, [self.project_p1.id])],
        })

    # ---------------- cross-project denial ---------------- #

    def test_AT1_non_member_cannot_read_private_template(self):
        """B1.AT-1: Charlie (P1 user, no P2 access) must NOT see private
        template bound to P2.

        Red today: no ir.rule → search returns the row.
        """
        ids = self.AgentTemplate.with_user(self.charlie).search([
            ('id', '=', self.private_tmpl_p2.id),
        ]).ids
        self.assertEqual(
            ids, [],
            "B1.AT-1: cross-project private agent_template must NOT be "
            "readable. Plan §Phase A Task 13 — add "
            "agent_template_rule_project_admin/user with "
            "``project_ids.project_access_ids`` walk.",
        )

    def test_AT2_non_member_cannot_write_private_template(self):
        """B1.AT-2: Charlie cannot write to P2's private template."""
        with self.assertRaises(
            AccessError,
            msg="B1.AT-2: cross-project agent_template write must raise "
                "AccessError. Today no rule = allow.",
        ):
            self.private_tmpl_p2.with_user(self.charlie).write({
                'description': 'evil',
            })

    def test_AT3_non_member_cannot_create_template_bound_to_other_project(self):
        """B1.AT-3: Charlie cannot create a template bound to P2."""
        with self.assertRaises(
            AccessError,
            msg="B1.AT-3: cross-project agent_template create must raise "
                "AccessError. Privilege escalation surface.",
        ):
            self.AgentTemplate.with_user(self.charlie).create({
                'name': f'B-1 evil cross-project {self.suffix}',
                'is_public': False,
                'state': 'draft',
                'project_ids': [(6, 0, [self.project_p2.id])],
            })

    def test_AT4_non_member_cannot_unlink_private_template(self):
        """B1.AT-4: Charlie cannot unlink P2's template."""
        with self.assertRaises(
            AccessError,
            msg="B1.AT-4: cross-project agent_template unlink must raise "
                "AccessError. DoS attack surface.",
        ):
            self.private_tmpl_p2.with_user(self.charlie).unlink()

    # ---------------- is_public carve-out ---------------- #

    def test_AT5_public_template_readable_by_all_internal_users(self):
        """B1.AT-5: ``is_public=True`` template is readable by every
        internal user (including those without any project_access).

        After impl: this is a regression lock — public_readonly rule
        must NOT be dropped.
        """
        ids = self.AgentTemplate.with_user(self.charlie).search([
            ('id', '=', self.public_tmpl.id),
        ]).ids
        self.assertEqual(
            ids, [self.public_tmpl.id],
            "B1.AT-5: public agent_template must be readable by all "
            "internal users (design §4.1 ``is_public`` carve-out).",
        )

    def test_AT6_public_template_writeable_only_by_admin(self):
        """B1.AT-6: Charlie (non-admin) cannot write to public template."""
        with self.assertRaises(
            AccessError,
            msg="B1.AT-6: non-admin user must NOT write public template. "
                "Public read-only carve-out is read-only for "
                "base.group_user; only group_system bypasses.",
        ):
            self.public_tmpl.with_user(self.charlie).write({
                'description': 'evil rewrite of public template',
            })

    # ---------------- own-project positive cases (regression lock) ---------------- #

    def test_AT7_project_owner_can_rwcu_own_template(self):
        """B1.AT-7: Alice (owner of P1) can read + write the P1 template."""
        # Read
        ids = self.AgentTemplate.with_user(self.alice).search([
            ('id', '=', self.own_tmpl_p1.id),
        ]).ids
        self.assertEqual(
            ids, [self.own_tmpl_p1.id],
            "B1.AT-7 read: project owner must read own template.",
        )
        # Write
        self.own_tmpl_p1.with_user(self.alice).write({
            'description': 'Alice updates her template',
        })
        self.assertEqual(
            self.own_tmpl_p1.description, 'Alice updates her template',
        )

    def test_AT8_project_user_read_only_on_own_template(self):
        """B1.AT-8: Charlie (role=user on P1) can read own template but
        cannot write — write is admin/owner only per §4.4 matrix.
        """
        # Charlie can read
        ids = self.AgentTemplate.with_user(self.charlie).search([
            ('id', '=', self.own_tmpl_p1.id),
        ]).ids
        self.assertEqual(
            ids, [self.own_tmpl_p1.id],
            "B1.AT-8 read: project user must read own template.",
        )
        # Charlie cannot write
        with self.assertRaises(
            AccessError,
            msg="B1.AT-8 write: project role='user' must NOT modify "
                "agent_template. Design §4.4 — template management is "
                "admin/owner only.",
        ):
            self.own_tmpl_p1.with_user(self.charlie).write({
                'description': 'role=user attempts write',
            })


@tagged('post_install', '-at_install')
class TestPhaseAAclAgentSession(_NewModelAclFixture):
    """B1.AS-1 ~ B1.AS-5 — agent_session cross-project isolation."""

    def setUp(self):
        super().setUp()
        # Forward-compat with the F-PC-3 fail-closed fix: creating an
        # agent_session now requires the injected namespace secret env.
        patcher = patch.dict(
            os.environ, {'AGENT_RUNTIME_NAMESPACE_SECRET': 'phase-a-test-ns-secret'})
        patcher.start()
        self.addCleanup(patcher.stop)
        # Two sessions — one per project.
        self.session_p1 = self.AgentSession.sudo().create({
            'project_id': self.project_p1.id,
        })
        self.session_p2 = self.AgentSession.sudo().create({
            'project_id': self.project_p2.id,
        })

    def test_AS1_non_member_cannot_read_other_project_session(self):
        """B1.AS-1: Charlie (P1 user) cannot search P2's session."""
        ids = self.AgentSession.with_user(self.charlie).search([
            ('id', '=', self.session_p2.id),
        ]).ids
        self.assertEqual(
            ids, [],
            "B1.AS-1: cross-project agent_session must NOT be readable. "
            "Plan §Phase A Task 13 — agent_session rule must derive "
            "from project_access (design §2.12).",
        )

    def test_AS2_project_member_can_read_own_session(self):
        """B1.AS-2: Charlie (P1 user) can read P1's session.

        Regression lock — must stay green after impl.
        """
        ids = self.AgentSession.with_user(self.charlie).search([
            ('id', '=', self.session_p1.id),
        ]).ids
        self.assertEqual(
            ids, [self.session_p1.id],
            "B1.AS-2: project member must read own project agent_session.",
        )

    def test_AS3_non_member_cannot_write_session(self):
        """B1.AS-3: Charlie cannot write P2's session state."""
        with self.assertRaises(
            AccessError,
            msg="B1.AS-3: cross-project agent_session write must raise.",
        ):
            self.session_p2.with_user(self.charlie).write({
                'state': 'reaped',
            })

    def test_AS4_project_user_cannot_modify_quota_override(self):
        """B1.AS-4: Charlie (role=user on P1) cannot set
        quota_override_* fields on P1's session — quota override is
        owner/admin only per design §4.8.
        """
        with self.assertRaises(
            AccessError,
            msg="B1.AS-4: role=user cannot modify agent_session quota "
                "override fields. Design §4.8 — owner/admin only.",
        ):
            self.session_p1.with_user(self.charlie).write({
                'quota_override_cpu': 999,
            })

    def test_AS5_non_member_cannot_unlink_session(self):
        """B1.AS-5: Charlie cannot unlink P2's session."""
        with self.assertRaises(
            AccessError,
            msg="B1.AS-5: cross-project agent_session unlink must raise.",
        ):
            self.session_p2.with_user(self.charlie).unlink()


@tagged('post_install', '-at_install')
class TestPhaseAAclSkill(_NewModelAclFixture):
    """B1.SK-1 ~ B1.SK-8 — skill cross-project denial + role gates."""

    def setUp(self):
        super().setUp()
        # Skills in both projects, mix of draft / published.
        self.skill_p2_draft = self.Skill.sudo().create({
            'name': f'b1-skill-p2-draft-{self.suffix}',
            'version': '0.1.0',
            'project_id': self.project_p2.id,
            'bundle_sha256': 'a' * 64,
            'state': 'draft',
        })
        self.skill_p1_draft = self.Skill.sudo().create({
            'name': f'b1-skill-p1-draft-{self.suffix}',
            'version': '0.1.0',
            'project_id': self.project_p1.id,
            'bundle_sha256': 'b' * 64,
            'state': 'draft',
        })
        # P1 user "Charlie" did NOT author this — Alice did (owner).
        # That makes Charlie a "non-author project user" for B1.SK-5.

    def test_SK1_non_member_cannot_read_other_project_skill(self):
        ids = self.Skill.with_user(self.charlie).search([
            ('id', '=', self.skill_p2_draft.id),
        ]).ids
        self.assertEqual(
            ids, [],
            "B1.SK-1: cross-project skill must NOT be readable.",
        )

    def test_SK2_non_member_cannot_write_other_project_skill(self):
        with self.assertRaises(AccessError, msg="B1.SK-2: cross-project skill write."):
            self.skill_p2_draft.with_user(self.charlie).write({
                'description': 'evil',
            })

    def test_SK3_non_member_cannot_create_skill_on_other_project(self):
        with self.assertRaises(
            AccessError,
            msg="B1.SK-3: cross-project skill create — privilege escalation.",
        ):
            self.Skill.with_user(self.charlie).create({
                'name': f'b1-evil-skill-{self.suffix}',
                'version': '0.0.1',
                'project_id': self.project_p2.id,
                'bundle_sha256': 'c' * 64,
            })

    def test_SK4_non_member_cannot_unlink_other_project_skill(self):
        with self.assertRaises(AccessError, msg="B1.SK-4: cross-project skill unlink."):
            self.skill_p2_draft.with_user(self.charlie).unlink()

    def test_SK5_project_user_cannot_write_others_draft_skill(self):
        """B1.SK-5: Charlie (P1 user) cannot modify Alice's draft skill on P1.

        Per design §4.4 — "Upload draft skill" is allowed for project
        user but only on their OWN drafts. Cross-author modification
        on draft requires admin/owner.
        """
        with self.assertRaises(
            AccessError,
            msg="B1.SK-5: project user cannot write someone else's draft skill.",
        ):
            self.skill_p1_draft.with_user(self.charlie).write({
                'description': 'Charlie attempts to modify Alice draft',
            })

    def test_SK6_guest_cannot_read_bundle_attachment(self):
        """B1.SK-6: Project guest can see skill name + description but
        bundle_attachment_id is field-level group-restricted.

        Per design §4.4 — guest cannot download bundle binary.
        """
        # Add Dave as guest of P1.
        self.PAccess.sudo().create({
            'project_id': self.project_p1.id,
            'partner_id': self.dave.partner_id.id,
            'role': 'guest',
        })
        # Dave reads the skill — must succeed for name but bundle_attachment
        # field must be either masked or empty.
        rec = self.Skill.with_user(self.dave).search([
            ('id', '=', self.skill_p1_draft.id),
        ])
        self.assertEqual(
            rec.ids, [self.skill_p1_draft.id],
            "B1.SK-6 read: guest can search the skill.",
        )
        # Reading bundle_attachment_id should either deny or return
        # empty value through field-level group restriction.
        # Today no field-level restriction => assertion fails.
        with self.assertRaises(
            AccessError,
            msg="B1.SK-6 field: guest must NOT read bundle_attachment_id "
                "(field-level groups='base.group_system' mask).",
        ):
            _ = rec.with_user(self.dave).bundle_attachment_id

    def test_SK7_project_admin_can_publish_skill(self):
        """B1.SK-7: Alice (owner of P1) can transition draft → published.

        Regression lock — owner/admin publish path must stay green.
        """
        self.skill_p1_draft.with_user(self.alice).write({
            'state': 'published',
        })
        self.skill_p1_draft.invalidate_recordset()
        self.assertEqual(
            self.skill_p1_draft.state, 'published',
            "B1.SK-7: project owner must be able to publish skill.",
        )

    def test_SK8_project_user_cannot_publish_skill(self):
        """B1.SK-8: Charlie (P1 user) cannot publish draft skill —
        publish is admin/owner only per design §4.4.
        """
        with self.assertRaises(
            AccessError,
            msg="B1.SK-8: role=user must NOT publish skill (admin/owner only).",
        ):
            self.skill_p1_draft.with_user(self.charlie).write({
                'state': 'published',
            })


@tagged('post_install', '-at_install')
class TestPhaseAAclProjectAccess(_NewModelAclFixture):
    """B1.PA-1 ~ B1.PA-8 — project_access privilege escalation block.

    MOST CRITICAL of the 4 ACL surfaces — any leak here lets user A
    grant themselves owner role on user B's project, then takeover.
    """

    def test_PA1_non_member_cannot_read_other_project_access(self):
        """B1.PA-1: Charlie cannot read project_access of P2 (W2 / Bob)."""
        # Get P2 project_access rows directly via sudo, then verify
        # Charlie's view is empty.
        p2_rows = self.PAccess.sudo().search([
            ('project_id', '=', self.project_p2.id),
        ])
        self.assertTrue(
            p2_rows, "B1.PA-1 fixture sanity: P2 must have project_access rows.",
        )
        charlie_view = self.PAccess.with_user(self.charlie).search([
            ('project_id', '=', self.project_p2.id),
        ])
        self.assertEqual(
            charlie_view.ids, [],
            "B1.PA-1: cross-project project_access must NOT be readable. "
            "Leaking row leaks membership graph + partner_id mapping.",
        )

    def test_PA2_self_introspection_can_read_own_row(self):
        """B1.PA-2: Charlie can read his OWN project_access row (the one
        granting him P1 user role).

        Regression lock — self-introspection must stay allowed.
        """
        own_row = self.PAccess.sudo().search([
            ('project_id', '=', self.project_p1.id),
            ('partner_id', '=', self.charlie.partner_id.id),
        ], limit=1)
        self.assertTrue(
            own_row,
            "B1.PA-2 fixture sanity: Charlie must have a row on P1.",
        )
        charlie_view = self.PAccess.with_user(self.charlie).search([
            ('id', '=', own_row.id),
        ])
        self.assertEqual(
            charlie_view.ids, [own_row.id],
            "B1.PA-2: self-introspection must remain allowed.",
        )

    def test_PA3_non_member_cannot_create_project_access_for_other_project(self):
        """B1.PA-3: Charlie cannot create a project_access row attaching
        himself (or anyone) to P2.

        PRIVILEGE ESCALATION BLOCK — if this passes pre-impl, attacker
        can grant themselves arbitrary role on victim project.
        """
        with self.assertRaises(
            AccessError,
            msg="B1.PA-3: cross-project project_access create — primary "
                "privilege escalation vector. Plan §Phase A Task 13 — "
                "rule must check user is admin/owner of target project.",
        ):
            self.PAccess.with_user(self.charlie).create({
                'project_id': self.project_p2.id,
                'partner_id': self.charlie.partner_id.id,
                'role': 'user',
            })

    def test_PA4_user_cannot_grant_owner_role(self):
        """B1.PA-4: Charlie (P1 role=user) cannot create a role='owner'
        row on P1 — for anyone, not just himself.

        Per ROLE_HIERARCHY ASSIGNABLE_ROLES_BY_*: only OWNER can assign
        owner role. user role = no assign rights at all.

        Use Dave (not yet a member of P1) as the grantee so the test
        exercises the ACL gate, not the unique(project_id, partner_id)
        SQL constraint (Charlie self-grant would hit unique first).
        """
        with self.assertRaises(
            AccessError,
            msg="B1.PA-4: role=user must NOT create role='owner' row "
                "(privilege escalation — grants attacker arbitrary "
                "ownership on a project they only have user role on).",
        ):
            self.PAccess.with_user(self.charlie).create({
                'project_id': self.project_p1.id,
                'partner_id': self.dave.partner_id.id,
                'role': 'owner',
            })

    def test_PA5_admin_cannot_grant_admin_role(self):
        """B1.PA-5: Per ROLE_HIERARCHY ASSIGNABLE_ROLES_BY_ADMIN =
        ['user', 'guest'] — admin can only assign user/guest, not
        admin/owner.

        We promote Charlie to admin first to exercise the admin path.
        """
        # Promote Charlie to admin on P1 (via sudo).
        charlie_row = self.PAccess.sudo().search([
            ('project_id', '=', self.project_p1.id),
            ('partner_id', '=', self.charlie.partner_id.id),
        ], limit=1)
        charlie_row.sudo().write({'role': 'admin'})

        # Now Charlie (admin) tries to grant Dave admin role on P1.
        with self.assertRaises(
            AccessError,
            msg="B1.PA-5: admin can only assign user/guest "
                "(ROLE_HIERARCHY ASSIGNABLE_ROLES_BY_ADMIN). Assigning "
                "admin or owner must raise.",
        ):
            self.PAccess.with_user(self.charlie).create({
                'project_id': self.project_p1.id,
                'partner_id': self.dave.partner_id.id,
                'role': 'admin',
            })

    def test_PA6_non_member_cannot_unlink_other_project_access(self):
        """B1.PA-6: Charlie cannot unlink Bob's project_access on P2.

        DoS attack vector — un-granting victim's owner row.
        """
        bob_row = self.PAccess.sudo().search([
            ('project_id', '=', self.project_p2.id),
            ('partner_id', '=', self.bob.partner_id.id),
        ], limit=1)
        self.assertTrue(bob_row, "B1.PA-6 fixture sanity: Bob owner row on P2.")
        with self.assertRaises(
            AccessError,
            msg="B1.PA-6: cross-project project_access unlink must "
                "raise. DoS attack vector.",
        ):
            bob_row.with_user(self.charlie).unlink()

    def test_PA7_project_admin_can_unlink_user_role(self):
        """B1.PA-7: Alice (P1 owner) can unlink Charlie's user role row
        on P1 (legitimate admin action).

        Regression lock — owner removal of user/guest must stay green.
        """
        charlie_row = self.PAccess.sudo().search([
            ('project_id', '=', self.project_p1.id),
            ('partner_id', '=', self.charlie.partner_id.id),
        ], limit=1)
        self.assertTrue(charlie_row)
        try:
            charlie_row.with_user(self.alice).unlink()
        except Exception as exc:  # noqa: BLE001
            self.fail(
                f"B1.PA-7: P1 owner must be able to unlink P1 user role. "
                f"Got {type(exc).__name__}: {exc}"
            )

    def test_PA8_user_cannot_unlink_another_users_row(self):
        """B1.PA-8: Charlie (role=user) cannot unlink another row on P1
        (no privilege to manage others' rows).

        Use Dave (just added as second user on P1 in this test) as the
        victim row — avoids accidentally triggering the
        ``_check_at_least_one_owner`` constraint which would mask the
        ACL deny path with a ValidationError on the last-owner case.
        """
        # Add Dave as second 'user' on P1 (admin-managed setup).
        dave_row = self.PAccess.sudo().create({
            'project_id': self.project_p1.id,
            'partner_id': self.dave.partner_id.id,
            'role': 'user',
        })

        with self.assertRaises(
            AccessError,
            msg="B1.PA-8: role=user must NOT unlink another user's row "
                "(would let user kick out other project members).",
        ):
            dave_row.with_user(self.charlie).unlink()


@tagged('post_install', '-at_install')
class TestPhaseAAclAgentTemplateCrossProjectInjection(_NewModelAclFixture):
    """HC-3 — agent_template.project_ids cross-project injection guard.

    Reviewer HC-3 (2026-05-22, user sign-off option C — hotfix):

    Attack vector: ir.rule on agent_template scopes RWCU per
    ``project_ids.project_access_ids.user_id`` walk. An admin of P_A
    can legitimately create a template T bound to P_A. The walk
    matches → admin passes rule check. Now admin writes
    ``T.project_ids = [(4, P_B.id)]`` to extend the template to a
    project they have NO access to. The write triggers per-row rule
    re-check, but the rule's domain matches if EITHER existing
    project (P_A) walks to admin's user_id — so the M2M extension is
    silently allowed.

    Outcome: T becomes visible / invokable on P_B for that admin's
    instance, and worse, T's binding now appears in P_B owner's
    backend (project_ids contains victim P_B). Privilege escalation
    + cross-project information leak.

    Per HC-3 hotfix design (user sign-off C):

      Add Python ``@api.constrains('project_ids')`` that on every
      write/create iterates new project_ids and asserts the writer
      has ``role IN ('owner', 'admin')`` on EACH bound project, or
      is group_system / sudo-context.

    Methods:

    * HC3.1  admin in A only — write({'project_ids': [(4, B)]})
             raises ValidationError
    * HC3.2  admin in both A and B — write succeeds
    * HC3.3  sudo() bypass — even cross-project bind succeeds in
             sudo-context (template seeding / migration paths)
    * HC3.4  group_system bypass — admin user with base.group_system
             succeeds regardless of project_access

    Red expectation today: impl has NOT added the @api.constrains
    guard → all four method test the constraint:
      * HC3.1, HC3.2: HC3.1 expects ValidationError but write goes
        through silently → assertRaises fails (RED).
      * HC3.2: write succeeds today AND post-impl → PASS (regression
        lock; reviewer must verify still passes after constraint adds
        proper authorization carve-out).
      * HC3.3, HC3.4: sudo / group_system path silently succeeds
        today AND post-impl → PASS (regression locks for legitimate
        bypass).
    """

    def setUp(self):
        super().setUp()
        ai_cfg = self.env['ai.config'].sudo().search([], limit=1)
        self._ai_cfg_id = ai_cfg.id if ai_cfg else False

        # Charlie is currently P1 role=user — promote to admin so he
        # can legitimately create/manage templates on P1.
        self._charlie_p1_row = self.PAccess.sudo().search([
            ('project_id', '=', self.project_p1.id),
            ('partner_id', '=', self.charlie.partner_id.id),
        ], limit=1)
        self._charlie_p1_row.sudo().write({'role': 'admin'})

        # Charlie's legitimate template bound to P1 only.
        self.tmpl_charlie_p1 = self.AgentTemplate.sudo().create({
            'name': f'HC3 Charlie tmpl P1 {self.suffix}',
            'is_public': False,
            'state': 'draft',
            'model_config_id': self._ai_cfg_id or False,
            'project_ids': [(6, 0, [self.project_p1.id])],
        })

    # ------------------------------------------------------------------ #
    # HC3.1 — admin of A cannot inject B into project_ids                #
    # ------------------------------------------------------------------ #
    def test_HC3_1_admin_in_A_cannot_extend_template_to_unrelated_B(self):
        """HC3.1: Charlie (P1 admin, NO access to P2) cannot extend
        his P1 template to include P2.

        Red today: no @api.constrains on project_ids → write goes
        through, template now visible on P2 for Charlie's instance
        + leaks into P2 owner's backend list.
        """
        with self.assertRaises(
            ValidationError,
            msg="HC3.1: writing project_ids to add an unrelated project "
                "must raise ValidationError. Reviewer HC-3 — add "
                "@api.constrains on agent_template.project_ids that "
                "checks writer has admin/owner role on every newly-"
                "added project.",
        ):
            self.tmpl_charlie_p1.with_user(self.charlie).write({
                'project_ids': [(4, self.project_p2.id)],
            })

    # ------------------------------------------------------------------ #
    # HC3.2 — admin of A AND B can legitimately extend                   #
    # ------------------------------------------------------------------ #
    def test_HC3_2_admin_in_both_projects_can_extend(self):
        """HC3.2: If Charlie holds admin role on BOTH P1 and P2, the
        extension is legitimate (template authored by a person trusted
        on both projects).

        Regression lock — the constraint must NOT block this case.
        """
        # Promote Charlie to admin on P2 too (admin-managed setup).
        self.PAccess.sudo().create({
            'project_id': self.project_p2.id,
            'partner_id': self.charlie.partner_id.id,
            'role': 'admin',
        })

        try:
            self.tmpl_charlie_p1.with_user(self.charlie).write({
                'project_ids': [(4, self.project_p2.id)],
            })
        except Exception as exc:  # noqa: BLE001
            self.fail(
                "HC3.2: write must succeed when writer is admin of "
                "every newly-bound project. Got %s: %s. The constraint "
                "carve-out must allow legitimate cross-project owners "
                "/ admins." % (type(exc).__name__, exc),
            )
        self.tmpl_charlie_p1.invalidate_recordset()
        self.assertIn(
            self.project_p2.id, self.tmpl_charlie_p1.project_ids.ids,
            "HC3.2 fixture sanity: extension landed in M2M.",
        )

    # ------------------------------------------------------------------ #
    # HC3.3 — sudo() context bypass                                       #
    # ------------------------------------------------------------------ #
    def test_HC3_3_sudo_context_bypasses_constraint(self):
        """HC3.3: sudo() write bypasses the constraint — used by
        template seeding cron / migration / paas-operator webhook etc.

        Regression lock — sudo path must NOT be blocked.
        """
        try:
            self.tmpl_charlie_p1.sudo().write({
                'project_ids': [(4, self.project_p2.id)],
            })
        except Exception as exc:  # noqa: BLE001
            self.fail(
                "HC3.3: sudo() context must bypass the project_ids "
                "constraint (legitimate seeding path). Got %s: %s."
                % (type(exc).__name__, exc),
            )

    # ------------------------------------------------------------------ #
    # HC3.4 — group_system user bypass                                    #
    # ------------------------------------------------------------------ #
    def test_HC3_4_group_system_user_bypasses_constraint(self):
        """HC3.4: A user with base.group_system can extend template
        cross-project without holding per-project project_access —
        the constraint must recognize group_system as authorized.

        Eve is the group_system user from the shared fixture.

        Regression lock — admin backend maintenance path must NOT be
        blocked. Without this carve-out the backend "Apps → Agent
        Template → bind project" workflow breaks for the platform
        admin who curates system templates.
        """
        try:
            self.tmpl_charlie_p1.with_user(self.eve).write({
                'project_ids': [(4, self.project_p2.id)],
            })
        except Exception as exc:  # noqa: BLE001
            self.fail(
                "HC3.4: group_system user must bypass the project_ids "
                "constraint. Got %s: %s. Carve-out must check "
                "``self.env.user.has_group('base.group_system')``."
                % (type(exc).__name__, exc),
            )

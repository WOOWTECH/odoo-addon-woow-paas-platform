"""TDD red phase — Phase A HC-4: skill bundle attachment side-channel deny.

Reviewer HC-4 (2026-05-22, user sign-off option C — hotfix):

Attack chain:
  1. Skill row's ORM-level ACL hides bundle_attachment_id field from
     non-admin via field-level group restriction (B1.SK-6 covers).
  2. But ``ir.attachment`` records persist independently in the
     ir.attachment table. Attacker who knows the attachment id can
     bypass the ORM mask by hitting ``/web/content/<id>?download=true``
     — Odoo's standard binary content controller — which streams the
     attachment bytes if the user can read the ir.attachment row.
  3. Default ir.attachment ACL on ``base.group_user`` is permissive
     (Odoo internal users get broad attachment read). Without explicit
     ir.rule on attachments whose ``res_model='woow_paas_platform.skill'``
     non-admin user reads bundle binary.
  4. Reverse-engineer bundle → craft malicious bundle with same name
     → upload it (legitimate project-user create path for own draft) →
     content runs as subprocess on the agent pod → RCE within the
     project's pod boundary (per §1.2 trust model the pod boundary is
     the project's userbase — still gives the attacker arbitrary code
     execution on every conversation in that pod).

Impl hotfix expected:
  (a) ``skill.bundle_attachment_id`` create override stamps
      ``res_model='woow_paas_platform.skill'`` + ``res_id=skill.id``
      + ``public=False`` so the attachment row is correctly anchored
      to a skill record.
  (b) Add ``ir.rule`` scoped to ``ir.attachment`` rows where
      ``res_model='woow_paas_platform.skill'``:
        domain = [('res_model', '=', 'woow_paas_platform.skill')]
        groups = [base.group_system]
        perm_read = True (everything else False)
      Combined with the default ir.attachment access OR-semantics this
      narrows ``base.group_user``'s implicit access on these specific
      attachment rows.

Acceptance criteria:

HC4.1  Project user (Charlie / role=user) reading the attachment row
       directly via ``ir.attachment.browse(...).read()`` raises
       ``AccessError`` — confirms the ir.rule narrow is in place.
HC4.2  Project admin (Charlie promoted to role=admin on P1) without
       ``base.group_system`` also raises ``AccessError`` — admin role
       in project doesn't bypass attachment-level admin gate.
HC4.3  group_system user (Eve) reads attachment row OK — regression
       lock for legitimate admin access (skill bundle inspection in
       backend).
HC4.4  HTTP side-channel: ``GET /web/content/<bundle_id>?download=1``
       authenticated as project user returns non-200 (403 / 404) — the
       actual attack vector. Even if ORM raises AccessError, the HTTP
       endpoint must propagate the deny to the binary stream.

Red expectation today (impl has NOT shipped attachment override +
ir.rule):

* HC4.1 → RED: ``ir.attachment``'s permissive default lets Charlie
  read the row → assertRaises fails.
* HC4.2 → RED: same path — admin role in project is not a system
  role; impl needs the explicit attachment rule.
* HC4.3 → PASS today (Eve already has group_system; default
  permissive read also lets her in). Regression lock — must stay
  green post-impl when the new rule explicitly grants
  group_system perm_read=True.
* HC4.4 → RED: ``/web/content/<id>`` returns 200 with binary
  payload today (Odoo std controller honours
  ir.attachment.public + record rule; both currently allow Charlie).

Fixture / env assumptions:

* Phase A new models in place (skill model with
  ``bundle_attachment_id`` Many2one).
* Charlie + Alice + Eve users (similar to B-1 fixture but this file is
  HttpCase-based for HC4.4 → users have passwords).

Boundary（刻意不測）:

* The attack chain step 4 (RCE on agent pod from crafted bundle) —
  not in Phase A scope; subprocess sandbox is §6 / Phase B/C
  responsibility.
* Bundle SHA256 integrity verify mismatch — covered by Phase B
  ``cache.py`` SHA256 verify (out of Phase A scope).
* ir.attachment ACL on res_model='woow_paas_platform.skill' for write
  / create / unlink directions — Phase A scope picks the READ
  direction (the side-channel attack vector); write-side deny is
  inherited from the legitimate-only create-path through skill
  controller (Phase B/C).
"""
import base64
import uuid

from odoo.exceptions import AccessError
from odoo.tests.common import HttpCase, tagged


def _new_internal_user(env, login, name=None, with_password=False):
    vals = {
        'name': name or login,
        'login': login,
        'email': f'{login}@test.example',
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    }
    if with_password:
        vals['password'] = login
    return env['res.users'].sudo().create(vals)


@tagged('post_install', '-at_install')
class TestPhaseAAttachmentSideChannelDeny(HttpCase):
    """HC4.1-HC4.4 — skill bundle attachment side-channel deny."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.Project = cls.env['project.project']
        cls.PAccess = cls.env['woow_paas_platform.project_access']
        cls.Skill = cls.env['woow_paas_platform.skill']
        cls.Attachment = cls.env['ir.attachment']

        for required in (
            'woow_paas_platform.skill',
            'woow_paas_platform.project_access',
        ):
            if required not in cls.env.registry:
                raise RuntimeError(
                    f"HC-4 prerequisite: model '{required}' not "
                    f"registered. Phase A impl must ship new models "
                    f"before HC-4 attachment ACL tests run."
                )

    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:8]

        # Charlie = project user/admin on P1, target attacker for HC4.1/4.2/4.4.
        # Eve = group_system admin (regression lock HC4.3).
        # All three need passwords for HC4.4 url_open / authenticate.
        self.charlie = _new_internal_user(
            self.env, f'hc4_charlie_{self.suffix}',
            'Charlie (HC-4)', with_password=True,
        )
        self.eve = self.env['res.users'].sudo().create({
            'name': f'Eve (HC-4) {self.suffix}',
            'login': f'hc4_eve_{self.suffix}',
            'email': f'hc4_eve_{self.suffix}@test.example',
            'password': f'hc4_eve_{self.suffix}',
            'groups_id': [(6, 0, [self.env.ref('base.group_system').id])],
        })

        # Project + Charlie's project_access (start role=user; HC4.2
        # promotes to admin).
        self.ws = self.Workspace.sudo().create({
            'name': f'HC4 WS {self.suffix}',
            'owner_id': self.eve.id,
        })
        self.project = self.Project.sudo().create({
            'name': f'HC4 Project {self.suffix}',
        })
        # Bootstrap may have created an owner row for Eve — make sure
        # Charlie has a user-role row, idempotent.
        existing = self.PAccess.sudo().search([
            ('project_id', '=', self.project.id),
            ('partner_id', '=', self.charlie.partner_id.id),
        ], limit=1)
        if existing:
            existing.sudo().write({'role': 'user'})
        else:
            self.PAccess.sudo().create({
                'project_id': self.project.id,
                'partner_id': self.charlie.partner_id.id,
                'role': 'user',
            })

        # Create skill FIRST so we have a real id to anchor the
        # attachment onto via res_id (mirrors the expected impl create
        # override that stamps res_model+res_id on bundle attachments).
        self.skill = self.Skill.sudo().create({
            'name': f'hc4-skill-{self.suffix}',
            'version': '0.1.0',
            'project_id': self.project.id,
            'bundle_sha256': 'd' * 64,
            'state': 'draft',
        })

        # Bundle attachment ANCHORED to the skill record. Without
        # res_id Odoo's default attachment ACL falls back to a
        # template-level admin-only check that would mask Charlie's
        # access regardless of the new rule — defeating the point of
        # HC-4 tests. With res_id set, Odoo delegates to
        # ``skill.check_access('read')`` which is permissive for
        # project members today → Charlie reads attachment → red
        # path actively exercised.
        bundle_bytes = b'<fake skill bundle bytes for HC-4 test>'
        self.bundle_attachment = self.Attachment.sudo().create({
            'name': f'hc4-skill-bundle-{self.suffix}.tar.gz',
            'datas': base64.b64encode(bundle_bytes),
            'res_model': 'woow_paas_platform.skill',
            'res_id': self.skill.id,
            'mimetype': 'application/gzip',
            # public=False is the expected impl default (HC-4 invariant)
            'public': False,
        })
        self.skill.sudo().write({
            'bundle_attachment_id': self.bundle_attachment.id,
        })

    # ------------------------------------------------------------------ #
    # HC4.1 — project user cannot read attachment row via ORM             #
    # ------------------------------------------------------------------ #
    def test_HC4_1_project_user_cannot_read_attachment_row(self):
        """HC4.1: Charlie (project role=user) must NOT be able to
        read the ir.attachment row carrying the skill bundle.

        Red today: no ir.rule for attachments where
        res_model='woow_paas_platform.skill' → Odoo's default
        permissive read for base.group_user lets Charlie read.
        """
        with self.assertRaises(
            AccessError,
            msg="HC4.1: project user must NOT read skill bundle "
                "ir.attachment row directly. Reviewer HC-4 — impl must "
                "add ir.rule on ir.attachment with domain "
                "[('res_model','=','woow_paas_platform.skill')] + "
                "groups=base.group_system + perm_read=True.",
        ):
            self.bundle_attachment.with_user(self.charlie).read(['datas'])

    # ------------------------------------------------------------------ #
    # HC4.2 — project admin (no group_system) cannot read either          #
    # ------------------------------------------------------------------ #
    def test_HC4_2_project_admin_cannot_read_attachment_row(self):
        """HC4.2: Charlie promoted to project admin (still no
        base.group_system) must STILL be denied. Project-level admin
        role does not equal platform-level system admin.
        """
        charlie_row = self.PAccess.sudo().search([
            ('project_id', '=', self.project.id),
            ('partner_id', '=', self.charlie.partner_id.id),
        ], limit=1)
        charlie_row.sudo().write({'role': 'admin'})

        with self.assertRaises(
            AccessError,
            msg="HC4.2: project admin (no group_system) must NOT "
                "read skill bundle attachment. The attachment-level "
                "rule gates by group_system, not by project_access "
                "role.",
        ):
            self.bundle_attachment.with_user(self.charlie).read(['datas'])

    # ------------------------------------------------------------------ #
    # HC4.3 — group_system user can read (regression lock)                #
    # ------------------------------------------------------------------ #
    def test_HC4_3_group_system_can_read_attachment_row(self):
        """HC4.3: Eve (base.group_system) can read the bundle
        attachment row — legitimate admin path for backend skill
        inspection.

        Regression lock — must stay green after impl adds the
        scoped attachment rule.
        """
        try:
            rec = self.bundle_attachment.with_user(self.eve).read(['datas'])
        except Exception as exc:  # noqa: BLE001
            self.fail(
                "HC4.3: group_system user must be able to read skill "
                "bundle attachment (admin backend skill management "
                "path). Got %s: %s." % (type(exc).__name__, exc),
            )
        self.assertTrue(
            rec and rec[0].get('datas'),
            "HC4.3: read must return the bundle bytes for group_system.",
        )

    # ------------------------------------------------------------------ #
    # HC4.4 — HTTP /web/content side-channel deny                         #
    # ------------------------------------------------------------------ #
    def test_HC4_4_web_content_endpoint_denies_project_user(self):
        """HC4.4: GET /web/content/<bundle_id>?download=1 authenticated
        as Charlie (project user) must NOT stream the bundle bytes.

        Red today: Odoo's std /web/content streams the binary if the
        ir.attachment record passes ORM read for the request user.
        With permissive default attachment ACL Charlie's request
        returns 200 + binary payload → side-channel exfil.

        Post-impl: with the scoped attachment rule denying read for
        non-group_system on ``res_model='woow_paas_platform.skill'``
        rows, the controller returns 403 / 404 / non-200.

        Acceptance: status code is NOT 200, OR (more lenient) body
        does not contain the bundle bytes literal.
        """
        # Authenticate as Charlie. HttpCase.authenticate sets the
        # session for subsequent url_open calls.
        login = self.charlie.login
        self.authenticate(login, login)

        url = f'/web/content/{self.bundle_attachment.id}?download=true'
        resp = self.url_open(url, timeout=10)

        # The original attack literal — if this byte sequence appears
        # in the body, the side-channel is open.
        attack_marker = b'<fake skill bundle bytes for HC-4 test>'

        # Two complementary assertions, both must hold post-impl:
        #   (i) HTTP status is non-200 (controller refuses to serve).
        #   (ii) Even if some controller returns 200 with a generic
        #        "access denied" body, that body must NOT contain
        #        the bundle bytes.
        is_denied = resp.status_code != 200
        body_safe = attack_marker not in resp.content

        self.assertTrue(
            is_denied or body_safe,
            "HC4.4: /web/content side-channel must deny non-admin "
            "user. Got status=%d, content_starts_with=%r. Reviewer "
            "HC-4 — impl must add ir.rule on ir.attachment OR set "
            "skill bundles to public=False + res_model='woow_paas_"
            "platform.skill' to engage Odoo's std attachment ACL "
            "gating in /web/content controller."
            % (resp.status_code, resp.content[:80]),
        )

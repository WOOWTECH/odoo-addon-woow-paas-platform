"""TDD red-phase tests — Frontend + Controller decouple收尾 (D1=A + D2=A).

Plan: ``docs/plans/2026-Q2/2026-05-28-decouple-project-from-workspace-frontend-plan.md``
(sign-off 2026-05-28, branch ``fix/decuple-project``).

Sister doc: [[cloud-service-project-decouple]] — 後端 model + ir.rule
(``project_rule_workspace_member`` dual-leg) 已在 D3=A refactor 落地。
本 plan 補 controller create path + frontend Modal 收尾。

----

Scope locked here (2 new tests):

* **T1 — unbound create endpoint success**
  POST ``/api/support/projects`` ``action=create`` with name only
  (no ``workspace_id`` URL segment, no ``cloud_service_id`` param)
  must:
    - return JSON-RPC ``result.success = True``
    - persist a ``project.project`` row with
      ``cloud_service_id = False`` and ``workspace_id = False``
    - **subscribe the calling user as follower** so the
      ``project_rule_workspace_member`` dual-leg ``message_partner_ids``
      sub-leg passes for them (D2=A contract — creator authority via
      follower, controller keeps ``sudo().create()`` per leader sign-off).

* **T2 — unbound project ACL isolation**
  After T1's endpoint create as Alice:
    - Alice ``with_user(alice).search(...)`` finds her own unbound
      project (creator authority works);
    - Bob (cross-workspace user, not subscribed) cannot search-find
      Alice's unbound project (ir.rule dual-leg correctly denies).

----

Red expectation today (pre-implementation):

* T1 — fails at first ``self.assertTrue(result.get('success'))``:
  ``ai_assistant.py:1038-1040`` dispatcher rejects::

      if not cloud_service and not workspace:
          return {'success': False, 'error':
                  'cloud_service_id is required for create'}

  → ``result['success']`` is False → assertion red.
  Even if dispatcher guard were bypassed (call passes through to
  ``_create_project``), the follower assertion would still fail because
  ``ai_assistant._create_project`` does ``sudo().create(vals)`` with no
  ``message_subscribe`` (hidden trap per plan §Root cause B).
  (Symbol, not line number — #586: the old ``:1228`` had drifted ~295
  lines. See #586 for whether that follower subscribe is still needed.)

* T2.alice — fails at ``self.assertEqual(visible, project_id_set)``:
  no follower subscribe → the follower sub-leg evaluates False for Alice.
  ⚠ CORRECTION (#586, Refs #547): this bullet used to add
  "``create_uid=SUPERUSER``" and conclude "**both** dual-leg sub-legs
  evaluate False". That premise is FALSE — ``sudo()`` does not change
  uid, so create_uid is Alice and the creator sub-leg is TRUE. Whether
  that sub-leg alone already makes the project visible (making the
  follower subscribe redundant *for ACL*) is what #586 measures.

* T2.bob — passes trivially today (Bob never subscribed; ir.rule denies
  him). Regression lock so a future leg-2 widening that accidentally
  matches non-followers immediately fires red.

----

Note on plan brief inconsistency (deliberate divergence — flagged in
leader report): the hand-off brief mentioned asserting ``create_uid ==
portal_user``. Plan D2=A explicitly keeps ``sudo().create()`` and uses
``message_subscribe`` as the trust route, so these tests assert the
follower-membership contract rather than a create_uid identity.

⚠ CORRECTION (#586, Refs #547): the sentence that used to justify that
choice — "``create_uid`` will **remain** SUPERUSER post-fix" — is FALSE.
``sudo()`` does not change uid (see the ``sudo()`` docstring on
``BaseModel``), so ``create_uid`` is the calling tenant both before and
after D2=A. The *choice* to assert followers rather than create_uid is
still defensible (it pins the D2=A mechanism), but the stated reason for
it was wrong, and the ir.rule's unbound leg is ``follower OR creator`` —
so the creator sub-leg may already carry the visibility on its own.
Measuring that is exactly what #586 does.

Test 2 is the load-bearing creator-authority test — it verifies the
*outcome* (creator can read own project) regardless of which mechanism
establishes the authority, so even if implementation later switches
D2=B the contract assertion still locks.
"""
import json as json_lib
import uuid

from odoo.tests.common import HttpCase, tagged


# --------------------------------------------------------------------------- #
# Shared fixture helpers (mirror test_project_detail_endpoints._new_user)     #
# --------------------------------------------------------------------------- #


def _new_user(env, login, name=None):
    """Create an internal user with password == login (HttpCase auth)."""
    return env['res.users'].sudo().create({
        'name': name or login,
        'login': login,
        'password': login,
        'email': f'{login}@test.example',
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })


# =========================================================================== #
# T1 + T2 — endpoint create unbound + cross-user ACL                          #
# =========================================================================== #


@tagged('post_install', '-at_install')
class TestProjectDecoupleFrontendEndpoint(HttpCase):
    """Two-user fixture for unbound create + ACL isolation.

    Layout (intentionally minimal — only what the two new tests need)::

        Alice  — internal user, no workspace overlap with Bob.
        Bob    — internal user, owns his own workspace W_bob (so he is
                 NOT a "system-wide blank" user — proves the deny is
                 follower-keyed, not "no workspace at all"-keyed).

    Bob owning his own workspace also keeps Bob from accidentally being
    a base.group_user with literally zero access records, which could
    otherwise let a future ir.rule "any internal user, no workspace"
    short-circuit pass undetected.
    """

    def setUp(self):
        super().setUp()
        Workspace = self.env['woow_paas_platform.workspace']

        # Suffix users with a uuid so reruns inside the same DB don't collide
        # with previously seeded login rows (worktree dev DB isn't always wiped).
        sfx = uuid.uuid4().hex[:6]
        self.alice = _new_user(
            self.env, f'fed_alice_{sfx}', 'Alice (FE-Decouple)',
        )
        self.bob = _new_user(
            self.env, f'fed_bob_{sfx}', 'Bob (FE-Decouple)',
        )

        # Bob owns his own workspace — proves cross-user isolation isn't
        # just "Bob has no workspace at all" trivially-empty result.
        self.ws_bob = Workspace.sudo().create({
            'name': f'FED W Bob {sfx}', 'owner_id': self.bob.id,
        })

    # ------------------------------------------------------------------ #
    # JSON-RPC helper — same idiom as test_project_detail_endpoints      #
    # ------------------------------------------------------------------ #
    def _post(self, url, params):
        response = self.url_open(
            url,
            data=json_lib.dumps({
                'jsonrpc': '2.0', 'method': 'call', 'params': params,
            }),
            headers={'Content-Type': 'application/json'},
            timeout=30,
        )
        self.assertEqual(
            response.status_code, 200,
            f"Endpoint {url!r} returned status={response.status_code}. "
            f"Body excerpt: {response.text[:200]!r}",
        )
        body = response.json()
        self.assertIn('result', body, f"Body: {body!r}")
        return body['result']

    # ================================================================= #
    # T1 — unbound create via endpoint must succeed + subscribe creator  #
    # ================================================================= #
    def test_t1_endpoint_create_unbound_project_succeeds_and_subscribes_creator(self):
        """T1 (D1=A + D2=A): ``/api/support/projects`` ``action=create``
        with no workspace + no cloud_service → success, project rows
        unbound, creator subscribed as follower.

        Red today (two layered fail points — first one fires the assert):

        1. ``ai_assistant.py:1038-1040`` dispatcher returns
           ``{'success': False, 'error': 'cloud_service_id is required
           for create'}`` for completely unbound payloads → first
           ``assertTrue(result.success)`` fails.
        2. After dispatcher fix, ``_create_project`` (symbol, not a line
           number — #586) still
           does ``sudo().create(vals)`` without any
           ``message_subscribe`` — so the
           ``creator partner in message_partner_ids`` assertion fails.

        Post-impl (推薦組合 A+A+A):
          - dispatcher guard removed → call dispatches to
            ``_create_project`` with both ``cloud_service`` and
            ``workspace`` = None
          - ``_create_project`` accepts the fully-unbound vals + calls
            ``project.sudo().message_subscribe(
                partner_ids=[request.env.user.partner_id.id])``
          - persisted row has ``cloud_service_id = False`` (and via
            compute ``workspace_id = False``)
        """
        self.authenticate(self.alice.login, self.alice.login)
        result = self._post(
            '/api/support/projects',
            {
                'action': 'create',
                'name': 'T1 Unbound Personal Project',
                'description': 'T1 seeded via endpoint with no ws/svc.',
            },
        )

        # ---- Endpoint-level contract --------------------------------- #
        self.assertTrue(
            result.get('success'),
            "T1 (plan D1=A): /api/support/projects action=create with "
            "no workspace_id URL segment and no cloud_service_id param "
            "must succeed. Today's dispatcher guard at "
            "ai_assistant.py:1038-1040 returns "
            "{'success': False, 'error': 'cloud_service_id is required "
            "for create'} — must be removed per plan §2 Controller "
            f"必改 1. Got: {result!r}",
        )
        data = result.get('data') or {}
        self.assertIn(
            'id', data,
            "T1: success payload must include project id "
            f"(for frontend list refresh). Got: {data!r}",
        )
        project_id = data['id']
        self.assertFalse(
            data.get('cloud_service_id'),
            "T1: response data.cloud_service_id must be None/False "
            f"for unbound create. Got: {data.get('cloud_service_id')!r}",
        )
        self.assertFalse(
            data.get('workspace_id'),
            "T1: response data.workspace_id must be None/False "
            "(computed from empty cloud_service_id). "
            f"Got: {data.get('workspace_id')!r}",
        )

        # ---- ORM-level contract -------------------------------------- #
        # Read with sudo for ACL-independent sanity (we test ACL in T2).
        project = self.env['project.project'].sudo().browse(project_id)
        self.assertTrue(
            project.exists(),
            "T1: project row must persist after endpoint create",
        )
        self.assertFalse(
            project.cloud_service_id,
            "T1: persisted project.cloud_service_id must be empty "
            "(unbound). Otherwise dispatcher swallowed an unintended "
            "default binding.",
        )
        self.assertFalse(
            project.workspace_id,
            "T1: persisted project.workspace_id must be empty for "
            "unbound (workspace_id is computed from cloud_service_id). "
            f"Got: {project.workspace_id!r}",
        )

        # ---- Hidden-trap contract (D2=A — follower subscribe) --------- #
        # ⚠ CORRECTION (#586, Refs #547): the plan's §Hidden trap reasoning
        # that used to be quoted here — "sudo().create() leaves
        # create_uid=SUPERUSER so the ir.rule create_uid sub-leg is False
        # for the real user" — is FALSE, and the controller says so at the
        # call site. ``BaseModel.create()`` does
        # ``vals.setdefault('create_uid', self.env.uid)`` and ``sudo()``
        # does not change uid (see the ``sudo()`` docstring on
        # ``BaseModel``), so create_uid IS the tenant here.
        #
        # The rule's unbound leg is an OR of two sub-legs (see
        # ir_rules.xml :: project_rule_workspace_member domain_force):
        #     cloud_service_ids == False
        #     AND (message_partner_ids in [partner] OR create_uid == user)
        # so the creator sub-leg alone may already satisfy it, which would
        # make the follower sub-leg redundant *for visibility*.
        #
        # 🔴 That is a reading of the code, NOT a measurement — #586 is the
        # ticket that measures it. Until it concludes, this assertion stays
        # and pins the current implementation (message_subscribe). Note it
        # pins a *mechanism*, not an outcome; T2 below pins the outcome and
        # survives a change of mechanism. If #586 concludes the call can go,
        # THIS assertion is the one to rewrite — and the follower/chatter
        # notification behaviour has to be judged separately from ACL.
        self.assertIn(
            self.alice.partner_id,
            project.message_partner_ids,
            "T1: creator's partner is NOT a follower of the freshly-created "
            "unbound project. ``ai_assistant._create_project`` must call "
            "``project.sudo().message_subscribe(partner_ids=["
            "request.env.user.partner_id.id])`` immediately after its "
            "``sudo().create(vals)`` (symbol, not a line number — the old "
            "message named a line that had drifted ~295 lines). "
            "#586 measured why this is load-bearing, and it is NOT ir.rule "
            "visibility: the rule's unbound leg is ``follower OR creator`` "
            "and the creator leg alone keeps T2 green. It is the CONTROLLER "
            "that needs the follower — ``_check_project_access`` resolves an "
            "unbound project purely by ``user_partner in "
            "project.message_partner_ids``, with no create_uid leg — so "
            "without this subscribe the creator can see her project but gets "
            "'Access denied' from the task endpoints (T4/T5 setup go red). "
            "That helper is NOT missing a leg by accident: its docstring "
            "names ``mcp_server_rule_workspace_user`` leg 2 as what it "
            "mirrors, and that rule is follower-only. The rule set itself is "
            "split (project/project_task carry create_uid, mcp_server/"
            "mcp_tool do not), so aligning them is a four-way decision.",
        )

    # ================================================================= #
    # T2 — cross-user ACL isolation on unbound project                    #
    # ================================================================= #
    def test_t2_unbound_project_visible_only_to_creator_follower(self):
        """T2 (regression守線 for plan §Test scope ACL bullet): unbound
        project created via the endpoint is visible to its creator
        (Alice) but not to an unrelated user (Bob).

        This is the load-bearing creator-authority test — it verifies
        the *outcome* (creator can read own project / cross-user can't)
        regardless of which D2 mechanism establishes the authority. So
        if a future tightening swaps D2=A's ``message_subscribe`` for
        a ``with_user`` create or any other approach, T2 still locks
        the contract.

        Red today (two layered fail points — Alice's assert fires first):

        1. Endpoint call as Alice fails at the dispatcher (same as T1)
           → ``project_id is None`` → KeyError in the follow-up search.
           To keep the failure trace clean and pin the actual ACL bug,
           we ``assertTrue(result.success)`` first.
        2. After dispatcher fix but before message_subscribe: no follower
           was wired up, so the follower sub-leg is False for Alice.

           ⚠ CORRECTION (#586, Refs #547): the original wording here said
           ``project.create_uid = SUPERUSER`` and concluded "both dual-leg
           sub-legs False". That premise is FALSE — ``sudo()`` does not
           change uid, so create_uid is Alice, and the creator sub-leg is
           TRUE. Whether Alice could therefore already read her project
           without the follower sub-leg is exactly what #586 measures; the
           historical red described above is kept as narrative, but do not
           reason from its stated cause.

        Bob's assertion is a regression lock: today's ir.rule already
        denies him (single-leg path returns nothing for unbound), and
        post-impl dual-leg must still deny him. If a future widening
        breaks isolation, this fires immediately.
        """
        # ---- Create unbound project as Alice via the endpoint -------- #
        self.authenticate(self.alice.login, self.alice.login)
        create_result = self._post(
            '/api/support/projects',
            {
                'action': 'create',
                'name': 'T2 Alice Personal Project',
                'description': 'T2 unbound for ACL isolation check.',
            },
        )
        self.assertTrue(
            create_result.get('success'),
            "T2 setup: Alice's endpoint create must succeed before "
            "we can probe ACL. Same root cause as T1 if this fails — "
            "dispatcher guard at ai_assistant.py:1038-1040 must be "
            f"removed. Got: {create_result!r}",
        )
        project_id = (create_result.get('data') or {}).get('id')
        self.assertTrue(
            project_id,
            "T2 setup: endpoint must return data.id for the freshly "
            f"created project. Got: {create_result!r}",
        )

        Project = self.env['project.project']

        # ---- Alice (creator) must see her own unbound project -------- #
        visible_alice = Project.with_user(self.alice).search([
            ('id', '=', project_id),
        ])
        self.assertEqual(
            visible_alice.ids, [project_id],
            "T2 (load-bearing outcome test): Alice cannot read her own "
            "unbound project after endpoint create. ⚠ CORRECTION (#586, "
            "Refs #547): this message used to blame "
            "'create_uid=SUPERUSER' — that premise is FALSE. sudo() does "
            "not change uid, so create_uid IS Alice and the create_uid "
            "sub-leg of ir.rule project_rule_workspace_member is TRUE. "
            "If this assertion is red, the unbound leg as a whole failed "
            "— check the rule's domain_force (cloud_service_ids == False "
            "AND (follower OR creator)) rather than assuming the "
            "follower sub-leg is the only thing holding it up. "
            f"Got visible={visible_alice.ids!r}.",
        )

        # ---- Bob (cross-user, non-follower) must NOT see ------------- #
        visible_bob = Project.with_user(self.bob).search([
            ('id', '=', project_id),
        ])
        self.assertFalse(
            visible_bob,
            "T2 (regression lock): Bob — a different internal user who "
            "is neither workspace-overlapping with Alice nor a follower "
            "of Alice's unbound project — must NOT be able to see it. "
            "Today this passes trivially (single-leg ir.rule denies "
            "everyone on unbound); post-impl dual-leg must keep denying "
            "non-followers. A future widening that matches "
            "base.group_user generically would cross-tenant-leak every "
            f"personal project. Got visible={visible_bob.ids!r}.",
        )

    # ================================================================= #
    # T3 — controller endpoint LIST path cross-user ACL (TC4 equivalent)  #
    #                                                                    #
    # Plan gap surfaced by implementation Playwright TC4 (2026-05-28):    #
    # ``ai_assistant._list_projects`` (line ~1186) uses                  #
    # ``request.env['project.project'].sudo().search(domain)`` — this    #
    # **completely bypasses ir.rule** at the controller endpoint layer.  #
    #                                                                    #
    # B1-B5 ORM tests + T1/T2 ORM-level assertions all pass because they #
    # call ``Project.with_user(user).search(...)`` (ir.rule applies).    #
    # They miss the controller endpoint surface: any authenticated user  #
    # hitting ``POST /api/support/projects`` action='list' (no workspace_#
    # id URL segment) gets *every* project row in the DB — including     #
    # unbound personal projects owned by unrelated users (cross-tenant   #
    # data leak).                                                        #
    #                                                                    #
    # T3 is the controller-layer守線 mirror of B3/B4/B5 (ORM-layer).      #
    # ================================================================= #
    def test_t3_endpoint_list_blocks_cross_user_unbound_and_bound(self):
        """T3 (plan gap, ai_assistant._list_projects:1186 sudo bypass):
        ``POST /api/support/projects`` ``action='list'`` must respect
        ir.rule — Bob (no workspace overlap with Alice, no follower
        relationship) must NOT see Alice's projects (unbound or bound)
        in the returned list.

        Brief asked for unbound + workspace-only + bound (3 shapes); the
        schema in this repo makes a pure "workspace-only" project
        impossible — ``project.project.workspace_id`` is computed from
        ``cloud_service_id.workspace_id`` (see
        ``src/models/project_project.py:_compute_workspace_id`` and
        invariant #3 of [[cloud-service-project-decouple]]). So we cover
        the two shapes that actually exist on disk:

          - p_unbound_a / p_unbound_b — no cloud_service (personal /
            "unbound" projects, the most sensitive cross-tenant surface
            per plan §Hidden trap)
          - p_bound — cloud_service bound to Alice's workspace (the
            workspace-membership path)

        Two unbound projects (not just one) prove the leak is systemic
        — a single-row regression could conceivably be explained by
        fixture noise, but two locks the intent.

        Red today (pre-fix to ``_list_projects``):
          - ``_list_projects(None, None)`` (both params empty because
            Bob's URL has no workspace_id segment + no cloud_service_id
            param) → ``domain = []`` → ``sudo().search([])`` returns
            every project row regardless of ir.rule → Bob's response
            includes all 3 Alice projects → ``assert not bob_sees_any``
            fails red with the leaked ids dumped.

        Post-fix (drop ``sudo()`` from the search):
          - Bob's ir.rule dual-leg denies all 3 (he is neither workspace
            member nor follower / creator) → returned list excludes them
            → green.

        Bonus regression: Alice's own ``action='list'`` returns at least
        her 3 ids (subset assertion, not strict equality — DB may carry
        baseline rows from other test fixtures or demo data). This
        guards against an overcorrection where dropping ``sudo()`` also
        accidentally hides projects from their legitimate owner.
        """
        # --- Alice's bound-project prerequisites ---------------------- #
        # Create a workspace + cloud_service so Alice can hit the
        # ``/api/support/cloud-services/<cs_id>/project`` URL form for
        # the bound case. Done in test body (not setUp) to keep T1/T2
        # fixtures untouched.
        Workspace = self.env['woow_paas_platform.workspace']
        Template = self.env['woow_paas_platform.cloud_app_template']
        Service = self.env['woow_paas_platform.cloud_service']

        sfx = uuid.uuid4().hex[:6]
        ws_alice = Workspace.sudo().create({
            'name': f'FED T3 W Alice {sfx}', 'owner_id': self.alice.id,
        })
        template = Template.sudo().create({
            'name': f'FED T3 Tpl {sfx}',
            'slug': f'fed-t3-tpl-{sfx}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': f'fed-t3-{sfx}',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        svc_alice = Service.sudo().create({
            'name': f'FED T3 Svc {sfx}',
            'workspace_id': ws_alice.id,
            'template_id': template.id,
            'helm_release_name': f'fed-t3-svc-{sfx}',
            'helm_namespace': f'paas-ws-fed-t3-{sfx}',
        })

        # --- Alice seeds 3 projects via the endpoint ------------------ #
        self.authenticate(self.alice.login, self.alice.login)

        unbound_a = self._post(
            '/api/support/projects',
            {
                'action': 'create',
                'name': 'T3 Alice Unbound A',
                'description': 'T3 unbound personal #1.',
            },
        )
        self.assertTrue(
            unbound_a.get('success'),
            "T3 setup: Alice unbound#A create must succeed (post-T1 "
            f"impl already green). Got: {unbound_a!r}",
        )
        pid_unbound_a = (unbound_a.get('data') or {}).get('id')

        unbound_b = self._post(
            '/api/support/projects',
            {
                'action': 'create',
                'name': 'T3 Alice Unbound B',
                'description': 'T3 unbound personal #2.',
            },
        )
        self.assertTrue(
            unbound_b.get('success'),
            f"T3 setup: Alice unbound#B create must succeed. Got: {unbound_b!r}",
        )
        pid_unbound_b = (unbound_b.get('data') or {}).get('id')

        bound = self._post(
            f'/api/support/cloud-services/{svc_alice.id}/project',
            {
                'action': 'create',
                'name': 'T3 Alice Bound to svc_alice',
                'description': 'T3 workspace-bound project.',
            },
        )
        self.assertTrue(
            bound.get('success'),
            "T3 setup: Alice bound-project create must succeed via the "
            f"cloud-services URL form. Got: {bound!r}",
        )
        pid_bound = (bound.get('data') or {}).get('id')

        alice_pids = {pid_unbound_a, pid_unbound_b, pid_bound}
        self.assertNotIn(
            None, alice_pids,
            f"T3 setup sanity: all 3 Alice project ids populated. Got: {alice_pids!r}",
        )

        # --- Bob calls list (no workspace URL, no cs param) ----------- #
        # Switch to Bob's session — this is the M-R2-style cross-user
        # attack surface: any authenticated internal user hitting the
        # bare ``/api/support/projects`` endpoint.
        self.authenticate(self.bob.login, self.bob.login)
        bob_list = self._post(
            '/api/support/projects',
            {'action': 'list'},
        )
        self.assertTrue(
            bob_list.get('success'),
            "T3: Bob's list call must return success=True (endpoint "
            "does not gate by membership at the dispatcher for the "
            f"bare-URL list path). Got: {bob_list!r}",
        )
        bob_data = bob_list.get('data') or []
        bob_returned_ids = {row.get('id') for row in bob_data}

        leaked = alice_pids & bob_returned_ids
        self.assertFalse(
            leaked,
            "T3 (plan gap, MAJOR cross-tenant leak): Bob can see "
            "Alice's projects via ``POST /api/support/projects`` "
            "``action='list'``. Root cause is "
            "``src/controllers/ai_assistant.py:1186`` — "
            "``request.env['project.project'].sudo().search(domain)`` "
            "bypasses ir.rule entirely. Fix: drop the ``sudo()`` so "
            "``project_rule_workspace_member`` filters the recordset "
            "per the calling user. ORM-layer tests (B1-B5, T1/T2) miss "
            "this because they call ``Project.with_user(user).search()``"
            " — they exercise the ir.rule but not the controller endpoint"
            " path that bypasses it. This test is the controller-layer "
            "mirror of B3/B4/B5. Leaked ids (Alice→Bob): "
            f"{sorted(leaked)!r}. Total Bob saw: {len(bob_returned_ids)} "
            "rows (post-fix expected: 0 — Bob owns ws_bob with no "
            "projects, is not a follower of any Alice project, and has "
            "no workspace overlap).",
        )

        # --- Bonus regression: Alice still sees her own 3 ------------- #
        # Use subset (⊆) not strict equality because the DB may carry
        # baseline rows from other test fixtures / addons; the
        # load-bearing invariant is "owner is not accidentally hidden
        # from herself" when ``sudo()`` is dropped.
        self.authenticate(self.alice.login, self.alice.login)
        alice_list = self._post(
            '/api/support/projects',
            {'action': 'list'},
        )
        self.assertTrue(
            alice_list.get('success'),
            f"T3 regression: Alice's list call must succeed. Got: {alice_list!r}",
        )
        alice_returned_ids = {
            row.get('id') for row in (alice_list.get('data') or [])
        }
        missing_from_alice = alice_pids - alice_returned_ids
        self.assertFalse(
            missing_from_alice,
            "T3 regression: Alice cannot see her own projects after "
            "the fix lands — dropping ``sudo()`` from "
            "``_list_projects`` over-corrected and hid creator's own "
            "rows. Check the post-fix domain still passes ir.rule "
            "``project_rule_workspace_member`` for: (a) bound projects "
            "via workspace member leg, (b) unbound projects via "
            "follower / creator sub-legs. Missing from Alice's view: "
            f"{sorted(missing_from_alice)!r}.",
        )

    # ================================================================= #
    # T4 — controller endpoint LIST TASKS cross-user ACL (A1 follow-on)   #
    #                                                                    #
    # CAQA BLOCKER #1 (2026-05-28): ``ai_assistant._list_tasks``         #
    # (line ~1325) is the **task-layer mirror** of the                   #
    # ``_list_projects:1186`` sudo-bypass — same root cause, different   #
    # endpoint surface, independent ir.rule (``project_task_rule_        #
    # workspace_member``, ir_rules.xml:108-117). Bare-URL                #
    # ``POST /api/support/tasks`` ``action='list'`` with no              #
    # workspace_id URL segment and no project_id param gives any         #
    # authenticated user the entire ``project.task`` table.              #
    #                                                                    #
    # Why a separate test (cannot ride on T3):                            #
    #   - T3 fix lands on ``_list_projects`` only. ``_list_tasks``       #
    #     keeps its independent ``sudo().search()`` call site.           #
    #   - The dual-leg ir.rule for tasks is structurally different from  #
    #     projects (walks via ``project_id.``), so even if               #
    #     ``project_rule_workspace_member`` were perfect, a task-rule    #
    #     bypass via sudo() would still leak.                            #
    #                                                                    #
    # T4 mirrors T3's idiom: seed Alice tasks under both unbound and     #
    # bound projects, then verify Bob's bare-URL list does NOT include   #
    # any of them.                                                       #
    # ================================================================= #
    def test_t4_endpoint_list_tasks_blocks_cross_user_leak(self):
        """T4 (CAQA BLOCKER #1, ai_assistant._list_tasks:1325 sudo bypass):
        ``POST /api/support/tasks`` ``action='list'`` must respect
        ``project_task_rule_workspace_member`` — Bob (no workspace
        overlap with Alice, no project follower relationship) must NOT
        see Alice's tasks (across unbound and bound projects) in the
        returned list.

        Red today (pre-fix to ``_list_tasks``):
          - Bob's bare-URL list → dispatcher → ``_list_tasks(None,
            {})`` → both ``workspace`` and ``project_id`` empty →
            ``domain = []`` → ``sudo().search([])`` returns every
            task row regardless of ir.rule → Bob's response includes
            all Alice tasks → ``assert not bob_sees_any`` fires red
            with leaked ids dumped.

        Post-fix (drop ``sudo()`` from line 1325):
          - Bob's ir.rule dual-leg
            (``project_task_rule_workspace_member``) denies all of
            Alice's tasks: (a) bound-project leg
            ``project_id.cloud_service_ids.workspace_id.access_ids.user_id``
            fails (Bob not in Alice's workspace), (b) unbound-project
            leg ``project_id.message_partner_ids in user.partner_id``
            / ``project_id.create_uid = user.id`` fails (Bob not
            follower, not creator) → returned list excludes them.

        Bonus regression: Alice sees at least her own tasks (subset
        assertion). The leg-by-leg expectations for Alice:
          - bound-project tasks: workspace member leg passes (Alice
            owns the workspace, is an access_ids member);
          - unbound-project tasks: follower leg passes (T1 fix in
            ``_create_project`` auto-subscribes Alice when she creates
            an unbound project — that subscription propagates to
            tasks under that project via the
            ``project_id.message_partner_ids`` walk).

        Setup mirrors T3 (2 unbound projects + 1 bound) and adds a
        task per project so the leak surface is exercised across both
        ir.rule legs. Like T3, "workspace-only" project shape is
        skipped — schema-impossible per
        ``project.project._compute_workspace_id`` + invariant #3.
        """
        # --- Alice's bound-project prerequisites (parity with T3) ----- #
        Workspace = self.env['woow_paas_platform.workspace']
        Template = self.env['woow_paas_platform.cloud_app_template']
        Service = self.env['woow_paas_platform.cloud_service']

        sfx = uuid.uuid4().hex[:6]
        ws_alice = Workspace.sudo().create({
            'name': f'FED T4 W Alice {sfx}', 'owner_id': self.alice.id,
        })
        template = Template.sudo().create({
            'name': f'FED T4 Tpl {sfx}',
            'slug': f'fed-t4-tpl-{sfx}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': f'fed-t4-{sfx}',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        svc_alice = Service.sudo().create({
            'name': f'FED T4 Svc {sfx}',
            'workspace_id': ws_alice.id,
            'template_id': template.id,
            'helm_release_name': f'fed-t4-svc-{sfx}',
            'helm_namespace': f'paas-ws-fed-t4-{sfx}',
        })

        # --- Alice seeds projects via the project endpoint ------------ #
        self.authenticate(self.alice.login, self.alice.login)

        unbound_a = self._post(
            '/api/support/projects',
            {'action': 'create', 'name': 'T4 Alice Unbound A'},
        )
        self.assertTrue(
            unbound_a.get('success'),
            f"T4 setup: Alice unbound#A project create must succeed "
            f"(T1 impl already green). Got: {unbound_a!r}",
        )
        pid_unbound_a = (unbound_a.get('data') or {}).get('id')

        unbound_b = self._post(
            '/api/support/projects',
            {'action': 'create', 'name': 'T4 Alice Unbound B'},
        )
        self.assertTrue(
            unbound_b.get('success'),
            f"T4 setup: Alice unbound#B project create must succeed. "
            f"Got: {unbound_b!r}",
        )
        pid_unbound_b = (unbound_b.get('data') or {}).get('id')

        bound = self._post(
            f'/api/support/cloud-services/{svc_alice.id}/project',
            {'action': 'create', 'name': 'T4 Alice Bound Project'},
        )
        self.assertTrue(
            bound.get('success'),
            f"T4 setup: Alice bound project create must succeed. "
            f"Got: {bound!r}",
        )
        pid_bound = (bound.get('data') or {}).get('id')

        # --- Alice seeds 1 task per project via the task endpoint ----- #
        # ``POST /api/support/tasks`` ``action='create'`` requires
        # project_id and is gated by ``_check_project_access`` (line
        # 1351) — Alice qualifies for all 3 of her projects.
        alice_tids = set()
        for pid, label in (
            (pid_unbound_a, 'T4 Alice Task on Unbound A'),
            (pid_unbound_b, 'T4 Alice Task on Unbound B'),
            (pid_bound, 'T4 Alice Task on Bound'),
        ):
            self.assertTrue(
                pid,
                f"T4 setup sanity: project id for '{label}' is None — "
                f"upstream create failed silently.",
            )
            task_result = self._post(
                '/api/support/tasks',
                {
                    'action': 'create',
                    'name': label,
                    'project_id': pid,
                },
            )
            self.assertTrue(
                task_result.get('success'),
                f"T4 setup: Alice task create must succeed for project "
                f"{pid}. Got: {task_result!r}",
            )
            tid = (task_result.get('data') or {}).get('id')
            self.assertTrue(
                tid,
                f"T4 setup: task create returned no id. Got: {task_result!r}",
            )
            alice_tids.add(tid)

        self.assertEqual(
            len(alice_tids), 3,
            f"T4 setup sanity: expected 3 Alice tasks seeded, got "
            f"{len(alice_tids)} ids ({alice_tids!r}).",
        )

        # --- Bob calls list (bare URL, no workspace, no project_id) --- #
        self.authenticate(self.bob.login, self.bob.login)
        bob_list = self._post(
            '/api/support/tasks',
            {'action': 'list'},
        )
        self.assertTrue(
            bob_list.get('success'),
            f"T4: Bob's task-list call must return success=True (the "
            f"endpoint dispatcher does not gate by membership for the "
            f"bare-URL list path). Got: {bob_list!r}",
        )
        bob_data = bob_list.get('data') or []
        bob_returned_tids = {row.get('id') for row in bob_data}
        leaked = alice_tids & bob_returned_tids

        self.assertFalse(
            leaked,
            "T4 (CAQA BLOCKER #1, plan gap mirror of T3): Bob can see "
            "Alice's tasks via ``POST /api/support/tasks`` "
            "``action='list'``. Root cause is "
            "``src/controllers/ai_assistant.py:1325`` — "
            "``request.env['project.task'].sudo().search(domain)`` "
            "bypasses ir.rule entirely (same shape as the "
            "``_list_projects:1186`` leak T3 catches). Fix: drop the "
            "``sudo()`` so ``project_task_rule_workspace_member`` "
            "(ir_rules.xml:108-117) filters per caller. The task-layer "
            "ir.rule is independent of the project-layer rule — T3's "
            "fix on ``_list_projects`` does NOT close this surface, "
            "even though both bugs share the same root pattern. "
            "ORM-layer task tests (cross-workspace task isolation B "
            "suite) miss this because they call "
            "``Task.with_user(user).search()`` — they exercise the "
            "ir.rule but not the controller endpoint path that "
            f"bypasses it. Leaked task ids (Alice→Bob): "
            f"{sorted(leaked)!r}. Total Bob saw: "
            f"{len(bob_returned_tids)} rows (post-fix expected: 0 — "
            "Bob owns ws_bob with no projects/tasks, is not a follower "
            "of any Alice project, and has no workspace overlap).",
        )

        # --- Bonus regression: Alice still sees her own tasks --------- #
        # Same idiom as T3: subset assertion guards against an
        # overcorrection where dropping ``sudo()`` hides creator's own
        # rows. Both ir.rule legs must keep Alice in scope:
        #   (a) bound-task: workspace member leg via
        #       project_id.cloud_service_ids.workspace_id.access_ids
        #   (b) unbound-task: follower leg via
        #       project_id.message_partner_ids (T1 subscribe propagates)
        self.authenticate(self.alice.login, self.alice.login)
        alice_list = self._post(
            '/api/support/tasks',
            {'action': 'list'},
        )
        self.assertTrue(
            alice_list.get('success'),
            f"T4 regression: Alice's task-list call must succeed. "
            f"Got: {alice_list!r}",
        )
        alice_returned_tids = {
            row.get('id') for row in (alice_list.get('data') or [])
        }
        missing_from_alice = alice_tids - alice_returned_tids
        self.assertFalse(
            missing_from_alice,
            "T4 regression: Alice cannot see her own tasks after the "
            "fix lands — dropping ``sudo()`` from ``_list_tasks`` "
            "over-corrected and hid creator's own rows. Check the "
            "post-fix domain still passes "
            "``project_task_rule_workspace_member`` for: (a) bound "
            "tasks via workspace member leg, (b) unbound tasks via "
            "project_id follower / creator sub-legs (relies on T1's "
            "_create_project message_subscribe propagating along the "
            f"project_id walk). Missing from Alice's view: "
            f"{sorted(missing_from_alice)!r}.",
        )

    # ================================================================= #
    # T5 — api_support_stats unbound coverage (CAQA CAVEAT #3, F-DP-2)    #
    #                                                                    #
    # ``ai_assistant.api_support_stats`` (line ~965) computes per-user   #
    # task stats by deriving an ``accessible_projects`` set then        #
    # counting tasks under it. Today (line 974-976) the derivation is   #
    # ``sudo().search([('cloud_service_id.workspace_id.access_ids.user_#
    # id', '=', user.id)])`` — a **bound-leg-only** filter. Unbound     #
    # projects (no cloud_service_id, even ones the user created and is  #
    # the follower of via T1's _create_project subscribe) are silently  #
    # excluded from accessible_projects → tasks under them are not      #
    # counted → stats are systematically under-reported.                #
    #                                                                   #
    # Same root-cause family as T3 / T4:                                #
    #   - T3 ``_list_projects:1186`` — sudo over-access (Bob sees       #
    #     Alice's projects)                                             #
    #   - T4 ``_list_tasks:1325`` — sudo over-access (Bob sees Alice's  #
    #     tasks)                                                        #
    #   - T5 ``api_support_stats:974`` — sudo + manual bound-only       #
    #     filter under-access (Alice does not see her own unbound       #
    #     project's tasks)                                              #
    #                                                                   #
    # T3 / T4 are over-access (rule too loose); T5 is under-access      #
    # (filter too tight). Both are the same anti-pattern:               #
    # controller bypassing ir.rule with sudo() + hand-rolled domain.    #
    # ================================================================= #
    def test_t5_api_support_stats_includes_unbound_owned_projects(self):
        """T5 (CAQA CAVEAT #3, family [[cross-workspace-task-data-leak]]
        instance #4, F-DP-2): ``POST /api/support/stats`` must include
        Alice's tasks under her own *unbound* project — not just tasks
        under bound-via-workspace projects.

        Endpoint response shape note: the JSON envelope exposes
        ``data.total / active / completion`` (task counts only); the
        underlying ``accessible_projects`` recordset is not surfaced
        directly. So this test detects the under-counting symptom by
        seeding **exactly one task on each project shape** (1 unbound
        + 1 bound = 2 tasks) and asserting ``data.total >= 2``. Pre-
        fix the bound-only filter at line 974-976 returns just the
        bound project → task count = 1 → assertion red. Post-fix the
        ir.rule dual-leg includes the unbound project via the
        follower leg → task count = 2 → green.

        Why ``>=`` not ``==``: paranoid guard against shared-DB
        baseline tasks from other addons / demo data drifting the
        absolute count. The load-bearing invariant is "Alice's
        unbound-project task is counted", and "Alice sees at least
        her two seeded tasks" captures that without coupling to
        baseline state.

        Bonus regression — Bob's stats: bare ``/api/support/stats``
        call as Bob must return ``data.total == 0``. Bob owns
        ``ws_bob`` (no projects), is not follower of any Alice
        project, has no workspace overlap. Today: bound-only filter
        with Bob's user.id matches nothing → 0. Post-fix: ir.rule
        dual-leg evaluates both legs False for Bob → 0. Stays GREEN
        pre and post — guards against a future over-correction (e.g.
        dropping the user.id leg entirely and counting all tasks DB-
        wide) that would surface as Bob seeing > 0.

        Brief vs schema note: brief said "accessible_projects count".
        The endpoint returns task counts (total/active/completion),
        not a project count — under-counting tasks IS the
        accessible_projects evidence. Test asserts on ``total`` per
        the actual schema; no escalation needed.
        """
        # --- Alice's bound-project prerequisites (parity with T3/T4) -- #
        Workspace = self.env['woow_paas_platform.workspace']
        Template = self.env['woow_paas_platform.cloud_app_template']
        Service = self.env['woow_paas_platform.cloud_service']

        sfx = uuid.uuid4().hex[:6]
        ws_alice = Workspace.sudo().create({
            'name': f'FED T5 W Alice {sfx}', 'owner_id': self.alice.id,
        })
        template = Template.sudo().create({
            'name': f'FED T5 Tpl {sfx}',
            'slug': f'fed-t5-tpl-{sfx}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': f'fed-t5-{sfx}',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        svc_alice = Service.sudo().create({
            'name': f'FED T5 Svc {sfx}',
            'workspace_id': ws_alice.id,
            'template_id': template.id,
            'helm_release_name': f'fed-t5-svc-{sfx}',
            'helm_namespace': f'paas-ws-fed-t5-{sfx}',
        })

        # --- Alice seeds 1 unbound + 1 bound project via endpoint ----- #
        self.authenticate(self.alice.login, self.alice.login)

        unbound = self._post(
            '/api/support/projects',
            {'action': 'create', 'name': 'T5 Alice Unbound'},
        )
        self.assertTrue(
            unbound.get('success'),
            f"T5 setup: Alice unbound project create must succeed "
            f"(T1 impl already green). Got: {unbound!r}",
        )
        pid_unbound = (unbound.get('data') or {}).get('id')

        bound = self._post(
            f'/api/support/cloud-services/{svc_alice.id}/project',
            {'action': 'create', 'name': 'T5 Alice Bound'},
        )
        self.assertTrue(
            bound.get('success'),
            f"T5 setup: Alice bound project create must succeed. "
            f"Got: {bound!r}",
        )
        pid_bound = (bound.get('data') or {}).get('id')

        # --- 1 task per project (2 total for Alice) ------------------- #
        alice_tids = set()
        for pid, label in (
            (pid_unbound, 'T5 Alice Task on Unbound'),
            (pid_bound, 'T5 Alice Task on Bound'),
        ):
            self.assertTrue(
                pid,
                f"T5 setup sanity: project id for '{label}' is None.",
            )
            task_result = self._post(
                '/api/support/tasks',
                {
                    'action': 'create',
                    'name': label,
                    'project_id': pid,
                },
            )
            self.assertTrue(
                task_result.get('success'),
                f"T5 setup: Alice task create must succeed for "
                f"project {pid}. Got: {task_result!r}",
            )
            tid = (task_result.get('data') or {}).get('id')
            self.assertTrue(
                tid,
                f"T5 setup: task create returned no id. Got: {task_result!r}",
            )
            alice_tids.add(tid)
        self.assertEqual(
            len(alice_tids), 2,
            f"T5 setup sanity: expected 2 Alice tasks (1 per project), "
            f"got {len(alice_tids)} ({alice_tids!r}).",
        )

        # --- Alice calls stats ---------------------------------------- #
        alice_stats = self._post('/api/support/stats', {})
        self.assertTrue(
            alice_stats.get('success'),
            f"T5: Alice stats call must return success=True. "
            f"Got: {alice_stats!r}",
        )
        alice_data = alice_stats.get('data') or {}
        alice_total = alice_data.get('total')
        self.assertIsInstance(
            alice_total, int,
            f"T5 schema sanity: data.total must be int. "
            f"Got: {alice_data!r}",
        )
        self.assertGreaterEqual(
            alice_total, 2,
            "T5 (CAQA CAVEAT #3, F-DP-2): Alice's stats total under-"
            "counts — her unbound project's task is missing from the "
            "``accessible_projects`` recordset. Root cause is "
            "``src/controllers/ai_assistant.py:974-976`` — "
            "``request.env['project.project'].sudo().search([('cloud_"
            "service_id.workspace_id.access_ids.user_id', '=', user."
            "id)])`` is a **bound-leg-only** filter (walks via "
            "cloud_service_id chain → unbound projects with empty "
            "cloud_service_id never match). Fix: drop the ``sudo()`` "
            "+ replace the hand-rolled domain with an empty domain (or "
            "no domain) so ``project_rule_workspace_member`` dual-leg "
            "filters per the calling user — same idiom as T3 / T4 fix. "
            "The dual-leg ir.rule already covers both bound (workspace "
            "member leg) and unbound (follower / creator leg, relies "
            "on T1's _create_project message_subscribe) — no new ACL "
            "design needed. Alice seeded 2 tasks (1 unbound + 1 "
            f"bound), stats returned total={alice_total} — expected "
            ">=2 post-fix, missing count = 2 - total = "
            f"{2 - alice_total} (= number of unbound-project tasks "
            "silently dropped). This is the same family as "
            "[[cross-workspace-task-data-leak]] but the symptom is "
            "*under-access* (creator missing own data) instead of "
            "over-access (cross-tenant leak). Full stats payload: "
            f"{alice_data!r}.",
        )

        # --- Bob calls stats — regression lock (must stay 0) ---------- #
        self.authenticate(self.bob.login, self.bob.login)
        bob_stats = self._post('/api/support/stats', {})
        self.assertTrue(
            bob_stats.get('success'),
            f"T5: Bob stats call must return success=True. "
            f"Got: {bob_stats!r}",
        )
        bob_total = (bob_stats.get('data') or {}).get('total')
        self.assertEqual(
            bob_total, 0,
            "T5 regression: Bob's stats total must be 0 (Bob has no "
            "projects, no follower memberships, no workspace overlap "
            "with Alice). Both pre-fix (bound-only filter excludes "
            "everything for Bob) and post-fix (ir.rule dual-leg "
            "rejects on both legs) must keep this at 0. A non-zero "
            "value indicates the fix went too far — e.g. dropped the "
            "user.id leg entirely and is counting all tasks DB-wide. "
            f"Got total={bob_total!r}. Full payload: "
            f"{bob_stats.get('data')!r}.",
        )

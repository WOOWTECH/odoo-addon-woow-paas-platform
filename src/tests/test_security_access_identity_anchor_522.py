"""#522: Security Access identity anchors must be unwritable by a tenant.

Two defects, one shape.

1. The create()/write() guards protected exactly two hand-listed fields
   (``canonical_name``, ``reference_id``). ``tunnel_id`` — which decides WHICH
   Cloudflare tunnel the row drives — was never listed, so a plain
   ``base.group_user`` workspace owner could repoint their own Security Access
   at an arbitrary tunnel over ordinary ``/web/dataset/call_kw``, and the value
   landed in Postgres. Protection was applied per field, by hand, so the field
   nobody remembered was the unprotected one. (An older test,
   ``test_update_access_config_whitelist_only``, already sent
   ``tunnel_id: attacker-tunnel`` and asserted it was dropped — but it only
   covered the controller whitelist; the ORM path underneath was open and that
   test stayed green the whole time.)

2. The guards' escape hatch was ``context.get('sa_internal_identity_write')``
   being truthy — and ``call_kw`` hands the CALLER's context straight to the
   ORM, so a tenant adding one key to the RPC payload opened the guard for
   every field it covered, ``canonical_name`` included. Measured against 18.0
   before the fix: that request returned ``true`` and the forged canonical_name
   was committed. The hatch is now a private sentinel object, which JSON cannot
   express.

These tests are the mechanical gate for both. They derive from the model's
declarative ``_IDENTITY_ANCHOR_FIELDS`` / ``_ROUTE_IDENTITY_ANCHOR_FIELDS``, so
a future anchor is covered the moment it is added to those tuples — no test
edit needed. Two anti-vacuity guards keep that derivation honest:

* the ``_REQUIRED_*`` floors below are folded INTO the loops, not merely
  compared against them. Dropping a field from a model tuple therefore does not
  shrink the test — the dropped field is still exercised and fails with the
  real symptom ("the tenant wrote it"), instead of silently vanishing from the
  iteration and leaving a green run;
* every listed name must resolve to a real field on its model. A rename or typo
  would otherwise turn the guards' ``field in vals`` test — and this whole loop
  — into a silent no-op.

Positive controls, in every direction, because a record that is unwritable for
an unrelated reason (ACL, ir.rule, a broken fixture) and a guard that simply
refuses everything would both make every negative assertion above pass for the
wrong reason: the same tenant must still be able to write an ordinary field;
the legitimate in-process hatch must still work; and create() must still assign
a canonical_name.
"""

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.models.security_access import (
    _IDENTITY_ANCHOR_FIELDS,
    _INTERNAL_IDENTITY_WRITE,
    _ROUTE_IDENTITY_ANCHOR_FIELDS,
    # 建立閘（豁免 su／group_system）：下面兩條以非 su 租戶 create，測的是錨點，
    # 不是建立閘 ⇒ 用下單流程同一把開閘 helper（只開建立閘、不開錨點閘）。
    _sa_platform_order,
)

# Floors, NOT a mirror of the model tuples. Adding an anchor to the model needs
# no change here (the loops read the model tuple); REMOVING one from the model
# must fail this file, which is the entire point — #522 happened because a
# field silently was not on the list. Keep a floor entry only for fields whose
# unprotection is a live security regression:
#   canonical_name / reference_id — the CF tunnel name and the hash salt the
#     subdomain derives from (squatting another workspace's namespace);
#   tunnel_id / tunnel_name       — WHICH Cloudflare tunnel this row drives
#     (#522: repointing hands this tenant's ingress to the victim's tunnel);
#   tunnel_token                  — the tunnel credential itself;
#   dns_record_id / access_app_id — WHICH CF DNS record / Access application
#     this route owns, i.e. what a later reconcile or teardown will mutate.
_REQUIRED_SA_ANCHORS = frozenset({
    'canonical_name', 'reference_id', 'tunnel_id', 'tunnel_name',
    'tunnel_token',
})
_REQUIRED_ROUTE_ANCHORS = frozenset({'dns_record_id', 'access_app_id'})

_CTX_KEY = 'sa_internal_identity_write'


class TestSecurityAccessIdentityAnchors(TransactionCase):
    """A tenant workspace owner (base.group_user only, not base.group_system)
    must not be able to move any identity anchor — on the SA or on its routes,
    by create or by write, with or without a forged context — while ordinary
    fields stay writable for that same user.
    """

    def setUp(self):
        super().setUp()
        User = self.env['res.users']
        group_user = self.env.ref('base.group_user')
        # alice: the tenant. Exactly base.group_user — this is the principal
        # the #522 sandbox run used.
        self.alice = User.create({
            'name': 'Anchor Alice', 'login': 'anchor_alice',
            'email': 'anchor_alice@test.example',
            'groups_id': [(6, 0, [group_user.id])],
        })
        # mallory: same group, NOT a member of the workspace — the ir.rule
        # control (the layer that DOES work; it must keep working).
        self.mallory = User.create({
            'name': 'Anchor Mallory', 'login': 'anchor_mallory',
            'email': 'anchor_mallory@test.example',
            'groups_id': [(6, 0, [group_user.id])],
        })
        # Pin the fixture's premise: these are tenants, not system users. A
        # test asserting "a non-system user cannot write X" says nothing if the
        # user quietly gained base.group_system.
        self.assertFalse(self.alice.has_group('base.group_system'))
        self.assertFalse(self.mallory.has_group('base.group_system'))

        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Anchor WS', 'owner_id': self.alice.id,
        })
        Access = self.env['woow_paas_platform.workspace_access']
        if not Access.search([('workspace_id', '=', self.workspace.id),
                              ('user_id', '=', self.alice.id)]):
            Access.create({
                'workspace_id': self.workspace.id,
                'user_id': self.alice.id, 'role': 'owner',
            })

        self.SA = self.env['woow_paas_platform.security_access']
        self.Route = self.env['woow_paas_platform.security_access_route']

        self.sa = self.SA.create({
            'name': 'Anchor Gateway', 'workspace_id': self.workspace.id,
        })
        # Seed the server-owned anchors the way provisioning does, through the
        # sentinel hatch. The SA deliberately stays 'pending': every operator
        # sync in this model is gated on state in (active / active_no_route /
        # error), so the route create/write below reach no operator client.
        self._internal(self.sa).write({
            'tunnel_id': 'tun-legit-522',
            'tunnel_name': 'paas-sa-legit-522',
            'tunnel_token': 'tok-legit-522',
        })
        self.route = self.Route.create({
            'security_access_id': self.sa.id,
            'subdomain_prefix': 'anchor-route-522',
        })
        self._internal(self.route).write({
            'dns_record_id': 'rec-legit-522',
            'access_app_id': 'app-legit-522',
        })

    # ------------------------------------------------------------------ #
    #  Helpers                                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _internal(records):
        """The legitimate in-process hatch: the sentinel, not True."""
        return records.with_context(**{_CTX_KEY: _INTERNAL_IDENTITY_WRITE})

    def _db_value(self, record, field_name):
        """Read one column straight out of Postgres.

        Deliberately NOT ``env.cr.clear()``: that throws away the not-yet
        flushed ORM write buffer, so a value that WAS accepted reads back
        unchanged and the guard looks effective when it is not — "no exception
        was raised" and "nothing was written" are different claims and this
        probe has to keep them coupled. ``flush_all()`` pushes any pending
        write down to the database first; the raw SELECT then bypasses the ORM
        cache entirely, so what comes back is what Postgres actually holds.

        Also the only way to observe ``tunnel_token``, which carries
        ``groups='base.group_system'`` and is unreadable as the tenant.
        """
        self.env.flush_all()
        # Names come from the models' own _fields, never from user input.
        self.env.cr.execute(
            'SELECT "%s" FROM "%s" WHERE id = %%s'
            % (field_name, record._table),
            (record.id,),
        )
        return self.env.cr.fetchone()[0]

    @staticmethod
    def _anchors(model_tuple, floor):
        """The fields the loops exercise: the model's declarative list UNION
        the floor. The union — not the model list alone — is what makes
        dropping a field from the model list fail here rather than quietly
        disappear from the iteration."""
        return sorted(set(model_tuple) | set(floor))

    def _sa_anchors(self):
        return self._anchors(_IDENTITY_ANCHOR_FIELDS, _REQUIRED_SA_ANCHORS)

    def _route_anchors(self):
        return self._anchors(
            _ROUTE_IDENTITY_ANCHOR_FIELDS, _REQUIRED_ROUTE_ANCHORS)

    # ------------------------------------------------------------------ #
    #  Anti-vacuity: the lists themselves                                #
    # ------------------------------------------------------------------ #

    def test_anchor_lists_name_real_fields(self):
        """Every declared anchor must resolve to a real field on its model.

        The guards test ``field in vals``; a renamed or misspelled entry
        matches nothing, silently protects nothing, and would equally silently
        drop out of every loop below."""
        for model_name, anchors in (
            ('woow_paas_platform.security_access', self._sa_anchors()),
            ('woow_paas_platform.security_access_route',
             self._route_anchors()),
        ):
            model_fields = self.env[model_name]._fields
            self.assertTrue(anchors, "anchor list for %s is empty" % model_name)
            for field_name in anchors:
                with self.subTest(model=model_name, field=field_name):
                    self.assertIn(
                        field_name, model_fields,
                        "%r is declared an identity anchor of %s but is not a "
                        "field on it — the guard silently protects nothing"
                        % (field_name, model_name))

    def test_anchor_lists_cover_required_floor(self):
        """Bookkeeping half of the anti-vacuity pair: the model lists may grow
        freely, but must never drop below the floor."""
        self.assertLessEqual(
            _REQUIRED_SA_ANCHORS, set(_IDENTITY_ANCHOR_FIELDS),
            "security_access identity anchors lost: %s"
            % sorted(_REQUIRED_SA_ANCHORS - set(_IDENTITY_ANCHOR_FIELDS)))
        self.assertLessEqual(
            _REQUIRED_ROUTE_ANCHORS, set(_ROUTE_IDENTITY_ANCHOR_FIELDS),
            "security_access_route identity anchors lost: %s"
            % sorted(_REQUIRED_ROUTE_ANCHORS
                     - set(_ROUTE_IDENTITY_ANCHOR_FIELDS)))

    # ------------------------------------------------------------------ #
    #  write(): every anchor is refused, for the tenant owner            #
    # ------------------------------------------------------------------ #

    def test_tenant_owner_cannot_write_sa_identity_anchors(self):
        """#522 core: a workspace owner writing ANY SA anchor must be refused,
        AND the stored value must be unchanged in Postgres."""
        sa_as_alice = self.SA.with_user(self.alice).browse(self.sa.id)
        for field_name in self._sa_anchors():
            with self.subTest(field=field_name):
                before = self._db_value(self.sa, field_name)
                forged = 'attacker-%s' % field_name.replace('_', '-')
                self.assertNotEqual(before, forged)
                with self.assertRaises(UserError):
                    sa_as_alice.write({field_name: forged})
                self.assertEqual(
                    self._db_value(self.sa, field_name), before,
                    "%s changed in the database despite the guard" % field_name)

    def test_tenant_owner_cannot_write_route_identity_anchors(self):
        """Same guard on the route sub-model: dns_record_id / access_app_id
        name the CF objects a later reconcile or teardown will act on."""
        route_as_alice = self.Route.with_user(self.alice).browse(self.route.id)
        for field_name in self._route_anchors():
            with self.subTest(field=field_name):
                before = self._db_value(self.route, field_name)
                forged = 'attacker-%s' % field_name.replace('_', '-')
                self.assertNotEqual(before, forged)
                with self.assertRaises(UserError):
                    route_as_alice.write({field_name: forged})
                self.assertEqual(
                    self._db_value(self.route, field_name), before,
                    "%s changed in the database despite the guard" % field_name)

    # ------------------------------------------------------------------ #
    #  The escape hatch must not be reachable from the wire              #
    # ------------------------------------------------------------------ #

    def test_forged_context_does_not_open_the_hatch(self):
        """``/web/dataset/call_kw`` puts the caller's context into env.context
        verbatim (odoo/api.py::call_kw), so a hatch keyed on any
        JSON-expressible value is opened by one extra key in the RPC payload.
        Every JSON-reachable spelling must be refused; only the sentinel works.
        """
        sa_as_alice = self.SA.with_user(self.alice).browse(self.sa.id)
        route_as_alice = self.Route.with_user(self.alice).browse(self.route.id)
        for forged_ctx_value in (True, 1, 'true', [1], {'x': 1}):
            with self.subTest(context_value=forged_ctx_value):
                before = self._db_value(self.sa, 'tunnel_id')
                with self.assertRaises(UserError):
                    sa_as_alice.with_context(
                        **{_CTX_KEY: forged_ctx_value},
                    ).write({'tunnel_id': 'victim-tunnel-uuid'})
                self.assertEqual(self._db_value(self.sa, 'tunnel_id'), before)
                # and the field that was already guarded before #522
                with self.assertRaises(UserError):
                    sa_as_alice.with_context(
                        **{_CTX_KEY: forged_ctx_value},
                    ).write({'canonical_name': 'paas-sa-hijack-forged'})
                # and the route sub-model's anchors
                with self.assertRaises(UserError):
                    route_as_alice.with_context(
                        **{_CTX_KEY: forged_ctx_value},
                    ).write({'access_app_id': 'attacker-app'})

    def test_prefix_is_no_longer_an_immutability_guard(self):
        """#1196 起 subdomain_prefix 是可改的——這裡釘住「為什麼那不是退步」。

        取代的是舊的 `test_forged_context_does_not_open_the_prefix_hatch`。那條
        斷言租戶改名會被 UserError 擋下，而 #1196 把改名變成正式功能（使用者自行
        改 subdomain，如同在 Cloudflare 平台上），前提消失。

        關鍵是 prefix **本來就不是身分錨點**（見 model 檔頭 `_IDENTITY_ANCHOR_FIELDS`
        的 NOT anchors 註記）：先前擋它的理由是**正確性**（hostname／DNS／ingress
        全從它推導，改了值卻不搬 CF 就靜默分岔），不是授權。#1196 的解法是「放行
        + 保證搬」，所以本檔真正該守的東西沒有變——**route 的兩根真錨點仍然不可
        偽造**，那才是 #522 的標的。這條測試就守那個分界。
        """
        route_as_alice = self.Route.with_user(self.alice).browse(self.route.id)

        # 改名：現在是合法操作（值真的落地）。
        route_as_alice.write({'subdomain_prefix': 'renamed-prefix-1196'})
        self.assertEqual(
            self._db_value(self.route, 'subdomain_prefix'),
            'renamed-prefix-1196')

        # 但格式仍然驗——放行不等於不檢查。
        # ⚠ Odoo 覆寫過 assertRaises，**不吃例外 tuple**（會炸
        # `issubclass() arg 1 must be a class`）。格式錯在 write() 的守衛就被
        # 攔下，所以這裡是 UserError，走不到 @api.constrains 的 ValidationError。
        with self.assertRaises(UserError):
            route_as_alice.write({'subdomain_prefix': 'Bad Prefix!!'})

        # 而真錨點沒有因此鬆動：偽造 context 一樣寫不進 route 的 CF 物件 id。
        # （#522 的標的是這兩根，不是 prefix。）
        for anchor, value in (('dns_record_id', 'attacker-dns'),
                              ('access_app_id', 'attacker-app')):
            with self.assertRaises(UserError):
                route_as_alice.with_context(
                    sa_internal_identity_write=True,
                ).write({anchor: value})

    def test_forged_context_does_not_open_the_hatch_at_create(self):
        """Same for the create()-side strip — otherwise the squat is still
        available, just with one extra key in the payload."""
        sa = _sa_platform_order(self.SA.with_user(self.alice).with_context(
            **{_CTX_KEY: True},
        )).create({
            'name': 'Forged Create GW',
            'workspace_id': self.workspace.id,
            'tunnel_id': 'victim-tunnel-uuid',
            'canonical_name': 'paas-sa-victim-squat',
        })
        self.assertFalse(self._db_value(sa, 'tunnel_id'))
        self.assertNotEqual(
            self._db_value(sa, 'canonical_name'), 'paas-sa-victim-squat')

    # ------------------------------------------------------------------ #
    #  Positive controls — the same user, the same records               #
    # ------------------------------------------------------------------ #

    def test_tenant_owner_can_still_write_plain_sa_field(self):
        """POSITIVE CONTROL for the SA loop. ``name`` is an ordinary
        user-editable field; if this user could not write it either, every
        negative assertion above would pass for the wrong reason."""
        sa_as_alice = self.SA.with_user(self.alice).browse(self.sa.id)
        sa_as_alice.write({'name': 'Renamed By Owner'})
        self.assertEqual(self._db_value(self.sa, 'name'), 'Renamed By Owner')

    def test_tenant_owner_can_still_write_plain_route_field(self):
        """POSITIVE CONTROL for the route loop. The SA is 'pending', so the
        service_url write fires no operator sync."""
        route_as_alice = self.Route.with_user(self.alice).browse(self.route.id)
        route_as_alice.write({'service_url': 'http://localhost:8080'})
        self.assertEqual(
            self._db_value(self.route, 'service_url'), 'http://localhost:8080')

    def test_internal_sentinel_still_writes_server_owned_anchors(self):
        """POSITIVE CONTROL for the hatch: provisioning stores the ids
        Cloudflare just handed back through it. A guard that refused everything
        would pass every negative test above and break provisioning."""
        self._internal(self.sa).write({'tunnel_id': 'tun-reprovisioned'})
        self.assertEqual(
            self._db_value(self.sa, 'tunnel_id'), 'tun-reprovisioned')
        self._internal(self.route).write({'dns_record_id': 'rec-resynced'})
        self.assertEqual(
            self._db_value(self.route, 'dns_record_id'), 'rec-resynced')

    def test_create_still_assigns_canonical_name(self):
        """POSITIVE CONTROL for the create-time internal write: create() has to
        stamp canonical_name through the very hatch this change narrowed. If
        the sentinel were wrong the row would be born nameless — and every
        "the tenant could not inject one" assertion would still pass."""
        sa = _sa_platform_order(self.SA.with_user(self.alice)).create({
            'name': 'Fresh Gateway', 'workspace_id': self.workspace.id,
        })
        stored = self._db_value(sa, 'canonical_name')
        self.assertTrue(stored, "create() left canonical_name unset")
        self.assertTrue(stored.startswith('paas-'), stored)
        self.assertTrue(self._db_value(sa, 'reference_id'))

    # ------------------------------------------------------------------ #
    #  create(): anchors are not injectable either                       #
    # ------------------------------------------------------------------ #

    def test_tenant_owner_cannot_inject_sa_anchors_at_create(self):
        """super().create() never routes through write(), so the write guard
        alone would leave a create-time squat open: a brand-new row bound to a
        victim's tunnel before anything is provisioned (action_provision's
        ``if self.tunnel_id:`` branch would then skip tunnel creation and push
        this tenant's routes onto it)."""
        forged = {
            field_name: 'attacker-%s' % field_name.replace('_', '-')
            for field_name in self._sa_anchors()
        }
        sa = _sa_platform_order(self.SA.with_user(self.alice)).create(dict(
            forged, name='Injected Gateway', workspace_id=self.workspace.id))
        for field_name, forged_value in forged.items():
            with self.subTest(field=field_name):
                self.assertNotEqual(
                    self._db_value(sa, field_name), forged_value,
                    "%s was injectable at create" % field_name)

    def test_tenant_owner_cannot_inject_route_anchors_at_create(self):
        forged = {
            field_name: 'attacker-%s' % field_name.replace('_', '-')
            for field_name in self._route_anchors()
        }
        route = self.Route.with_user(self.alice).create(dict(
            forged,
            security_access_id=self.sa.id,
            subdomain_prefix='injected-route-522'))
        for field_name, forged_value in forged.items():
            with self.subTest(field=field_name):
                self.assertNotEqual(
                    self._db_value(route, field_name), forged_value,
                    "%s was injectable at create" % field_name)

    # ------------------------------------------------------------------ #
    #  The layer that already worked must keep working                   #
    # ------------------------------------------------------------------ #

    def test_non_member_cannot_write_plain_field(self):
        """ir.rule control: a non-member is stopped at the record level, on an
        ordinary field, before any anchor question arises. This is the leg that
        was already sound in the #522 sandbox run — it must stay sound."""
        with self.assertRaises(AccessError):
            self.SA.with_user(self.mallory).browse(self.sa.id).write(
                {'name': 'Hijacked By Non Member'})
        self.assertEqual(self._db_value(self.sa, 'name'), 'Anchor Gateway')

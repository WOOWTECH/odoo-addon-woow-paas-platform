import ipaddress
import re

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

# #536：見 policy 子模型同一段註解——sync-control 的兩個 key 只認模組私有
# sentinel，租戶從 RPC payload 塞 `true` 打不開。
from .security_access import (
    _CTX_RULE_REPLACE,
    _CTX_SKIP_ROUTE_SYNC,
    _is_sync_control,
)

# Shared with the policy格式驗證：bare domain（不可帶 @），如 acme.com。
_DOMAIN_RE = re.compile(r'^[a-z0-9.-]+\.[a-z]{2,}$', re.IGNORECASE)
# ISO 3166-1 Alpha-2 國碼，如 TW / US（大小寫不拘，存前 upper）。
_COUNTRY_RE = re.compile(r'^[A-Za-z]{2}$')


class SecurityAccessAccessPolicyRule(models.Model):
    """One selector rule inside an Access policy（P-rule 子模型）.

    P-rule（2026-06）把「1 policy = 1 flat rule」拆成「1 policy → N rules」，
    對齊 Cloudflare Access 原生 policy 結構：每條 rule 屬於 include / require /
    exclude 三組之一（OR / AND / NOT 語意），policy 是 rule 的容器並承載
    decision + precedence（sequence）。

    rule 不直接呼叫 operator——父 policy 的 route 以宣告式 full-state 推送
    （鏡像 policy 子模型）。route_id 經 policy_id 兩跳 stored related 物化
    （policy_id.security_access_id），讓 ir.rule workspace walk 與 SA-level
    email 配額 count 可 traverse。

    #744：父 policy 的錨點變成 exclusive arc 之後，本子模型**必須對等鏡射**
    第二個錨點（`cloud_service_id`），否則 CS 錨定的 policy 底下這張表的
    ir.rule domain 兩腿皆假 ⇒ 租戶連自己那條 selector rule 都建不出來、讀不到。
    這是 [[unbound-project-acl-dual-leg]] 明列的「sub-model 必須對等改」那一條
    （那邊的教訓是漏改 = leak；本案方向相反是 fail-closed，但同樣是壞的）。

    #768：同一條規矩再套一次——第三個錨點（`smart_home_id`）也必須對等鏡射，
    否則 SH 錨定的 policy 底下這張表的 ir.rule domain 三腿皆假，租戶連自己那條
    selector rule 都建不出來，而錯誤訊息指向的是**子表**，沒人會聯想到 parent
    換了錨點。
    """

    _name = 'woow_paas_platform.security_access_access_policy_rule'
    _description = 'Security Access — Access Policy Rule'
    _order = 'sequence, id'

    policy_id = fields.Many2one(
        comodel_name='woow_paas_platform.security_access_access_policy',
        string='Access Policy', required=True, ondelete='cascade', index=True,
        help='The policy this rule belongs to. The policy carries the '
             'allow/deny/bypass decision; this rule is one selector within '
             'its include/require/exclude group.',
    )
    security_access_id = fields.Many2one(
        comodel_name='woow_paas_platform.security_access',
        string='Security Access',
        related='policy_id.security_access_id', store=True, index=True,
        help='Derived from policy_id (two hops); kept stored so the ir.rule '
             'workspace walk and the SA-level email quota can traverse it. '
             'Empty when the parent policy is anchored on a cloud service.',
    )
    cloud_service_id = fields.Many2one(
        comodel_name='woow_paas_platform.cloud_service',
        string='Cloud Service',
        related='policy_id.cloud_service_id', store=True, index=True,
        help='#744：父 policy 的另一半錨點，同樣 store=True 讓 ir.rule 走得到。',
    )
    smart_home_id = fields.Many2one(
        comodel_name='woow_paas_platform.smart_home',
        string='Smart Home',
        related='policy_id.smart_home_id', store=True, index=True,
        help='#768：父 policy 的第三個錨點，同樣 store=True 讓 ir.rule 走得到。',
    )
    group = fields.Selection(
        selection=[
            ('include', 'Include'),
            ('require', 'Require'),
            ('exclude', 'Exclude'),
        ],
        string='Group', default='include', required=True,
        help='Include = OR (match any), Require = AND (match all), '
             'Exclude = NOT (match none). A policy must have at least one '
             'Include rule.',
    )
    rule_type = fields.Selection(
        selection=[
            ('email_domain', 'Email domain'),
            ('email_list', 'Email list'),
            ('ip_range', 'IP range (CIDR)'),
            ('everyone', 'Everyone'),
            ('country', 'Country'),
        ],
        string='Rule Type', default='email_domain', required=True,
    )
    rule_value = fields.Char(
        string='Rule Value',
        help='Email domain (no @) / comma-separated emails / CIDR / '
             'comma-separated ISO-2 country codes, per rule_type. '
             'Empty for "everyone".',
    )
    sequence = fields.Integer(string='Display Order', default=10)

    @api.constrains('rule_type', 'rule_value', 'group')
    def _check_rule_value(self):
        for rule in self:
            val = (rule.rule_value or '').strip()
            if rule.rule_type == 'everyone':
                # everyone 是「所有人」selector，不帶值；有值多半是誤填。
                if val:
                    raise ValidationError(
                        _("The 'Everyone' rule takes no value — got %r.")
                        % rule.rule_value)
                continue
            if rule.rule_type == 'email_domain':
                if val.startswith('@') or not _DOMAIN_RE.match(val):
                    raise ValidationError(
                        _("Email domain must be a bare domain (no @), e.g. "
                          "acme.com — got %r") % rule.rule_value)
            elif rule.rule_type == 'email_list':
                emails = [e.strip() for e in val.split(',') if e.strip()]
                if not emails or any('@' not in e for e in emails):
                    raise ValidationError(
                        _("Email list must be comma-separated email addresses."))
            elif rule.rule_type == 'ip_range':
                try:
                    ipaddress.ip_network(val, strict=False)
                except ValueError:
                    raise ValidationError(
                        _("IP range must be a valid CIDR, e.g. 203.0.113.0/24 — "
                          "got %r") % rule.rule_value)
            elif rule.rule_type == 'country':
                codes = [c.strip() for c in val.split(',') if c.strip()]
                if not codes or any(not _COUNTRY_RE.match(c) for c in codes):
                    raise ValidationError(
                        _("Country must be comma-separated ISO 3166-1 alpha-2 "
                          "codes, e.g. TW,US — got %r") % rule.rule_value)

    def _suppress_route_sync(self):
        """Whether a per-rule operator sync should be skipped for this op.

        Both hatches open ONLY for the module's own sentinel (#536) — open them
        with `_skip_route_sync()` / `_rule_replace()` from models/security_access,
        never by putting `True` in the context: `call_kw` hands the caller's
        context straight to the ORM, so a truthy-valued key is forgeable from
        any tenant's RPC payload.

        sa_skip_route_sync: migration / provision bulk row moves converge CF via
        their own explicit re-sync, not per-row here.

        sa_rule_replace: a policy-driven whole-rule-set replace (controller
        _update_policy writes (5,0,0)+creates). Odoo runs the (5,0,0) unlink
        FULLY — including this per-rule sync — BEFORE the (0,0,) creates, so a
        per-rule sync in that window pushes the policy in its transient
        empty-include state and the operator rejects "≥1 include" (422), aborting
        the whole edit. The policy's own write() issues the final full-state sync
        after the replace settles, so per-rule syncs here are wrong AND redundant.
        """
        return (_is_sync_control(self.env, _CTX_SKIP_ROUTE_SYNC)
                or _is_sync_control(self.env, _CTX_RULE_REPLACE))

    # Atomicity + sync: mirror the policy sub-model. A rule create/write/unlink
    # re-syncs its policy's ANCHOR — the route, or (#745) the cloud_service —
    # which owns the declarative full-state push to the operator; the dispatch
    # itself lives in policy._access_sync_targets(). Wrapped in a savepoint so a
    # sync raise rolls the rule row back. The liveness gate lives inside the
    # anchor's own _sync_access_to_operator (no-ops without a live tunnel /
    # unprotected route / a CS with no subdomain), so we always call it and let
    # the anchor decide. _suppress_route_sync() skips the per-rule call
    # for migration/provision bulk moves (sa_skip_route_sync) and for a
    # policy-driven rule-set replace (sa_rule_replace), where the policy's own
    # write() owns the final full-state sync.
    #
    # Note: creating a policy WITH rule_ids fires BOTH the nested rule sync and
    # the policy's own post-create sync; the duplicate is benign — the route
    # push is an idempotent full-state declaration and the liveness gate inside
    # route._sync keeps a pending SA from calling the operator at all.

    @api.model_create_multi
    def create(self, vals_list):
        with self.env.cr.savepoint():
            rules = super().create(vals_list)
            policies = rules.mapped('policy_id')
            # Adding rules can only push email count OVER the cap or attach an
            # email selector to a bypass policy — re-check those (partial-state
            # safe: both only raise on a real violation, never on an undercount).
            # The "≥1 include" check is NOT re-run here: a fresh policy create
            # may add a non-include rule first, and the policy-level constraint
            # already covers the final state.
            policies._check_emails_per_policy()
            policies._check_decision_selector_compat()
            # #352：新增一條 include 規則就可能把 allow 政策變成整批放行。
            # policy 上的 @api.constrains 不會因為「單獨建 rule」而觸發，所以
            # 這裡與上面兩條同樣要顯式再跑一次——否則只擋得到走 policy.rule_ids
            # 的寫法，直接對 rule 表寫入的路徑（含 API）整條漏掉。
            policies._check_allow_policy_no_bulk_grant()
            if not self._suppress_route_sync():
                # #745：走 policy 的錨點分派（route 或 cloud_service），不是
                # `mapped('policy_id.route_id')`——CS 錨定時那個 mapped 是空集，
                # 症狀是「規則改了但 Cloudflare 不知道」且完全無聲。
                for anchor in policies._access_sync_targets():
                    anchor._sync_access_to_operator()
        return rules

    def write(self, vals):
        policies = self.mapped('policy_id')
        with self.env.cr.savepoint():
            result = super().write(vals)
            policies = (policies | self.mapped('policy_id'))
            policies._check_at_least_one_include()
            policies._check_emails_per_policy()
            policies._check_decision_selector_compat()
            # #352：把既有規則改成 email_domain / everyone、或把它從 exclude
            # 搬進 include，都是同一個繞過（見 policy 上的 constraint）。
            policies._check_allow_policy_no_bulk_grant()
            if (not self._suppress_route_sync()
                    and any(k in vals for k in (
                        'group', 'rule_type', 'rule_value', 'sequence',
                        'policy_id'))):
                for anchor in policies._access_sync_targets():
                    anchor._sync_access_to_operator()
        return result

    def unlink(self):
        policies = self.mapped('policy_id')
        anchors = policies._access_sync_targets()
        with self.env.cr.savepoint():
            result = super().unlink()
            # Removing a rule can drop the last Include — re-validate surviving
            # policies (a policy deleted by cascade no longer exists → skipped).
            # sa_rule_replace: a whole-rule-set replace (controller _update_policy
            # does (5,0,0)+creates) transiently empties the policy before the new
            # rules land; suppress the per-rule check during that window — the
            # policy-level @api.constrains validates the final state.
            if not _is_sync_control(self.env, _CTX_RULE_REPLACE):
                policies.exists()._check_at_least_one_include()
            if not self._suppress_route_sync():
                for anchor in anchors:
                    if anchor.exists():
                        anchor._sync_access_to_operator()
        return result

    def to_dict(self):
        self.ensure_one()
        return {
            'id': self.id, 'group': self.group,
            'rule_type': self.rule_type, 'rule_value': self.rule_value,
            'sequence': self.sequence,
        }

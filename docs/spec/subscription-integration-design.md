# Subscription Integration Design

Cloud Service 與 Smart Home 實體的訂閱生命週期管理。

## 1. Architecture Overview

```
┌──────────────────────────────────────────────────────────────┐
│                    woow_paas_platform                        │
│                                                              │
│  ┌──────────────┐  1:1  ┌────────────────────────────────┐  │
│  │ CloudService │──────▶│ sh.subscription.subscription   │  │
│  └──────────────┘       │ (sh_subscription addon)        │  │
│                         │                                │  │
│  ┌──────────────┐  1:1  │  ┌─────────────────────┐       │  │
│  │  SmartHome   │──────▶│  │ sh.subscription.plan │       │  │
│  └──────────────┘       │  └─────────────────────┘       │  │
│                         │  ┌─────────────────────┐       │  │
│  ┌──────────────┐  1:1  │  │  product.product     │       │  │
│  │ CloudApp     │──────▶│  │  (sh_product_subscribe│      │  │
│  │ Template     │       │  │   = True)            │       │  │
│  └──────────────┘       │  └─────────────────────┘       │  │
│                         └────────────────────────────────┘  │
│                                                              │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ sh_subscription_wallet                              │    │
│  │  ├─ eWallet 點數扣款                                │    │
│  │  ├─ ECPay 信用卡補扣                                │    │
│  │  └─ wallet.transaction 記錄                         │    │
│  └─────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────┘
```

### Module Dependency

```python
# __manifest__.py
"depends": [
    "base", "web", "project", "mail", "bus",
    "ai_base_gt", "odoo_ai_assistant_chatgpt_connector",
    "sh_subscription",            # NEW
    "sh_subscription_wallet",     # NEW
],
```

**Why direct dependency (not bridge module)**:
- 訂閱是核心業務需求，每個 service/smart home 必須有 subscription
- `sh_subscription` 已包含在 paas-odoo-ci Docker image 中
- Bridge module 增加不必要的複雜度

---

## 2. Data Model Changes

### 2.1 SubscriptionLifecycleMixin (New Abstract Model)

CloudService 和 SmartHome 共用的訂閱邏輯。

```python
# src/models/subscription_lifecycle_mixin.py

class SubscriptionLifecycleMixin(models.AbstractModel):
    _name = 'woow_paas_platform.subscription_lifecycle_mixin'
    _description = 'Subscription Lifecycle Mixin'

    # --- Fields ---
    subscription_id = fields.Many2one(
        'sh.subscription.subscription',
        string='Subscription',
        ondelete='set null',
        copy=False,
    )

    # Computed convenience fields (for UI display)
    subscription_state = fields.Selection(related='subscription_id.state', store=True)
    subscription_plan_name = fields.Char(related='subscription_id.sh_subscription_plan_id.name')
    subscription_end_date = fields.Date(related='subscription_id.sh_end_date')
    subscription_next_payment = fields.Date(related='subscription_id.sh_date_of_next_payment')
    subscription_is_trial = fields.Boolean(related='subscription_id.sh_trial')
    subscription_trial_end = fields.Date(related='subscription_id.sh_trial_end_date')

    # --- Core Methods ---

    def _create_and_confirm_subscription(self, partner, plan):
        """Create a subscription and immediately confirm it.

        Args:
            partner: res.partner record (workspace owner)
            plan: sh.subscription.plan record
        Returns:
            sh.subscription.subscription record
        """
        Subscription = self.env['sh.subscription.subscription']
        product = plan.product_ids[0] if plan.product_ids else plan.env['product.product']

        # If the plan is linked via product, find the product
        if not product:
            # Fallback: find product linked to this plan
            product = self.env['product.product'].search([
                ('sh_product_subscribe', '=', True),
                ('sh_subscription_plan_id', '=', plan.id),
            ], limit=1)

        if not product:
            raise UserError("No subscription product found for plan: %s" % plan.name)

        sub_vals = {
            'sh_partner_id': partner.id,
            'product_id': product.id,
            'sh_subscription_plan_id': plan.id,
            'sh_source': 'manual',
        }
        subscription = Subscription.sudo().create(sub_vals)
        # Trigger onchanges to populate dates, prices, etc.
        subscription._onchange_sh_subscription_plan_id()
        subscription._onchange_sh_trial_subcription_start_date()
        subscription._onchange_sh_trial_subcription_end_date()
        # Confirm → in_progress (starts trial)
        subscription.sh_subscription_confirm()

        self.write({'subscription_id': subscription.id})
        return subscription

    def _is_subscription_active(self):
        """Check if subscription allows normal operations."""
        self.ensure_one()
        sub = self.subscription_id
        if not sub:
            return True  # No subscription = unrestricted (admin/legacy)
        return sub.state == 'in_progress'

    def _is_in_grace_period(self):
        """Check if subscription is in grace period (expired but within N days)."""
        self.ensure_one()
        sub = self.subscription_id
        if not sub or not sub.sh_end_date:
            return False
        if sub.state not in ('expire', 'close'):
            return False
        grace_days = int(self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.grace_period_days', '7'
        ))
        days_expired = (fields.Date.today() - sub.sh_end_date).days
        return 0 < days_expired <= grace_days

    def _is_past_grace_period(self):
        """Check if subscription expired beyond grace period."""
        self.ensure_one()
        sub = self.subscription_id
        if not sub or not sub.sh_end_date:
            return False
        if sub.state not in ('expire', 'close', 'cancel'):
            return False
        if sub.state == 'cancel':
            return True
        grace_days = int(self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.grace_period_days', '7'
        ))
        days_expired = (fields.Date.today() - sub.sh_end_date).days
        return days_expired > grace_days

    def _check_operation_allowed(self, operation='write'):
        """Check if the current user can perform an operation.

        Args:
            operation: 'read' or 'write'
        Raises:
            UserError if operation not allowed
        """
        self.ensure_one()
        # Admin bypasses all restrictions
        if self.env.user.has_group('base.group_system'):
            return

        if self._is_subscription_active():
            return  # Full access

        if self._is_in_grace_period():
            if operation == 'read':
                return  # Read-only during grace
            raise UserError(
                "Your subscription has expired. "
                "You have a grace period to renew. "
                "During this time, modifications are not allowed."
            )

        if self._is_past_grace_period():
            raise UserError(
                "Your subscription has expired and the service is suspended. "
                "Please renew your subscription to restore access."
            )

    # --- Abstract methods (implemented by CloudService / SmartHome) ---

    def _do_suspend(self):
        """Suspend the service. Implemented by subclass."""
        raise NotImplementedError

    def _do_resume(self):
        """Resume the service. Implemented by subclass."""
        raise NotImplementedError

    def _do_cleanup(self):
        """Permanently remove the service. Implemented by subclass."""
        raise NotImplementedError
```

### 2.2 CloudService Changes

```python
# src/models/cloud_service.py (modifications)

class CloudService(models.Model):
    _name = 'woow_paas_platform.cloud_service'
    _inherit = [
        'woow_paas_platform.cloud_service',
        'woow_paas_platform.subscription_lifecycle_mixin',  # NEW
    ]

    # NEW state: 'suspended'
    state = fields.Selection(
        selection=[
            ('pending', 'Pending'),
            ('deploying', 'Deploying'),
            ('initializing', 'Initializing'),
            ('running', 'Running'),
            ('error', 'Error'),
            ('upgrading', 'Upgrading'),
            ('suspended', 'Suspended'),       # NEW
            ('deleting', 'Deleting'),
        ],
    )

    # Store pre-suspend state for resume
    pre_suspend_state = fields.Selection(
        selection=[
            ('running', 'Running'),
            ('error', 'Error'),
        ],
        help='State before suspension, used to restore on resume',
    )

    def _do_suspend(self):
        """Scale down Helm release to 0 replicas."""
        self.ensure_one()
        if self.state == 'suspended':
            return
        client = self.env['woow_paas_platform.paas_operator_client']._get_client()
        if client and self.helm_release_name:
            client.scale_release(self.helm_release_name, self.helm_namespace, replicas=0)
        self.write({
            'pre_suspend_state': self.state if self.state in ('running', 'error') else 'running',
            'state': 'suspended',
        })

    def _do_resume(self):
        """Scale up Helm release to restore replicas."""
        self.ensure_one()
        if self.state != 'suspended':
            return
        client = self.env['woow_paas_platform.paas_operator_client']._get_client()
        if client and self.helm_release_name:
            client.scale_release(self.helm_release_name, self.helm_namespace, replicas=1)
        restore_state = self.pre_suspend_state or 'running'
        self.write({
            'state': restore_state,
            'pre_suspend_state': False,
        })

    def _do_cleanup(self):
        """Helm uninstall and mark as terminated."""
        self.ensure_one()
        client = self.env['woow_paas_platform.paas_operator_client']._get_client()
        if client and self.helm_release_name:
            try:
                client.uninstall_release(self.helm_release_name, self.helm_namespace)
            except Exception as e:
                _logger.warning("Cleanup failed for service %s: %s", self.name, e)
        self.write({'state': 'deleting'})  # Will be cleaned up by existing cron
```

### 2.3 SmartHome Changes

```python
# src/models/smart_home.py (modifications)

class SmartHome(models.Model):
    _name = 'woow_paas_platform.smart_home'
    _inherit = [
        'woow_paas_platform.smart_home',
        'woow_paas_platform.subscription_lifecycle_mixin',  # NEW
    ]

    state = fields.Selection(
        selection=[
            ('pending', 'Pending'),
            ('provisioning', 'Provisioning'),
            ('active', 'Active'),
            ('error', 'Error'),
            ('suspended', 'Suspended'),       # NEW
            ('deleting', 'Deleting'),
        ],
    )

    pre_suspend_state = fields.Selection(
        selection=[
            ('active', 'Active'),
            ('error', 'Error'),
        ],
    )

    # Store original service_url for resume
    original_service_url = fields.Char(
        help='Original service URL before suspension',
    )

    def _do_suspend(self):
        """Update tunnel ingress to return 503."""
        self.ensure_one()
        if self.state == 'suspended':
            return
        client = self._get_operator_client()
        if self.tunnel_id:
            service_url = f"http://localhost:{self.ha_port}"
            client.configure_tunnel(
                self.tunnel_id,
                hostname=f"{self.subdomain}.{self._get_paas_domain()}",
                service_url="http_status:503",
            )
        self.write({
            'original_service_url': f"http://localhost:{self.ha_port}",
            'pre_suspend_state': self.state if self.state in ('active', 'error') else 'active',
            'state': 'suspended',
        })

    def _do_resume(self):
        """Restore tunnel ingress to original service URL."""
        self.ensure_one()
        if self.state != 'suspended':
            return
        client = self._get_operator_client()
        service_url = self.original_service_url or f"http://localhost:{self.ha_port}"
        if self.tunnel_id:
            client.configure_tunnel(
                self.tunnel_id,
                hostname=f"{self.subdomain}.{self._get_paas_domain()}",
                service_url=service_url,
            )
        restore_state = self.pre_suspend_state or 'active'
        self.write({
            'state': restore_state,
            'pre_suspend_state': False,
        })

    def _do_cleanup(self):
        """Delete tunnel and unlink record."""
        self.ensure_one()
        if self.tunnel_id:
            try:
                client = self._get_operator_client()
                client.delete_tunnel(self.tunnel_id)
            except Exception as e:
                _logger.warning("Cleanup failed for smart home %s: %s", self.name, e)
        self.write({'state': 'deleting'})
```

### 2.4 CloudAppTemplate Changes

```python
# src/models/cloud_app_template.py (modifications)

class CloudAppTemplate(models.Model):
    _name = 'woow_paas_platform.cloud_app_template'

    # REMOVE: monthly_price field

    # NEW: Link to subscription product
    subscription_product_id = fields.Many2one(
        'product.product',
        string='Subscription Product',
        domain=[('sh_product_subscribe', '=', True)],
        help='Subscription product for billing this template',
    )

    # Computed: available plans for this template's product
    subscription_plan_ids = fields.One2many(
        related='subscription_product_id.product_tmpl_id.sh_subscription_plan_ids',
        string='Available Plans',
    )
```

Note: `sh_subscription_plan_ids` 可能需要透過另一種方式取得，取決於 `sh_subscription` 如何將 plan 連結到 product。實作時需確認 `product.template` 是否有反向 One2many 到 `sh.subscription.plan`。

### 2.5 ResConfigSettings Changes

```python
# src/models/res_config_settings.py (additions)

class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # ... existing fields ...

    # NEW: Subscription lifecycle settings
    woow_subscription_grace_days = fields.Integer(
        string='Subscription Grace Period (Days)',
        config_parameter='woow_paas_platform.grace_period_days',
        default=7,
        help='Days after subscription expiry before service suspension',
    )
    woow_subscription_cleanup_months = fields.Integer(
        string='Auto Cleanup After (Months)',
        config_parameter='woow_paas_platform.cleanup_after_months',
        default=6,
        help='Months after cancellation before permanent service removal',
    )
```

### 2.6 hooks.py Update

```python
_CONFIG_PARAMETERS = [
    # ... existing ...
    'woow_paas_platform.grace_period_days',       # NEW
    'woow_paas_platform.cleanup_after_months',     # NEW
]
```

---

## 3. Subscription Lifecycle Cron

Single cron handles all subscription enforcement logic.

```python
# src/models/subscription_lifecycle_cron.py

class SubscriptionLifecycleCron(models.AbstractModel):
    _name = 'woow_paas_platform.subscription_lifecycle_cron'
    _description = 'Subscription Lifecycle Enforcement'

    def _cron_enforce_subscription_lifecycle(self):
        """Daily cron: enforce subscription states on services and smart homes.

        Flow for each entity:
          subscription in_progress + entity suspended → resume
          subscription expired/closed > grace period → suspend
          subscription cancelled → suspend
          subscription cancelled > cleanup months → cleanup
          subscription renewed → follow renewal chain + resume
        """
        grace_days = int(self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.grace_period_days', '7'
        ))
        cleanup_months = int(self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.cleanup_after_months', '6'
        ))
        today = fields.Date.today()

        # Process both CloudService and SmartHome
        for Model in [
            self.env['woow_paas_platform.cloud_service'],
            self.env['woow_paas_platform.smart_home'],
        ]:
            entities = Model.search([('subscription_id', '!=', False)])
            for entity in entities:
                try:
                    self._enforce_single(entity, grace_days, cleanup_months, today)
                    self.env.cr.commit()
                except Exception:
                    self.env.cr.rollback()
                    _logger.exception(
                        "Subscription enforcement failed for %s (id=%s)",
                        entity._name, entity.id,
                    )

    def _enforce_single(self, entity, grace_days, cleanup_months, today):
        sub = entity.subscription_id

        # 1. Follow renewal chain
        if sub.state == 'renewed':
            new_sub = self.env['sh.subscription.subscription'].search([
                ('sh_subscription_id', '=', sub.id),
                ('state', '=', 'in_progress'),
            ], limit=1)
            if new_sub:
                entity.write({'subscription_id': new_sub.id})
                sub = new_sub

        # 2. Active subscription → resume if suspended
        if sub.state == 'in_progress':
            if entity.state == 'suspended':
                entity._do_resume()
                _logger.info("Resumed %s %s (subscription active)", entity._name, entity.id)
            return

        # 3. Expired/Closed → check grace period
        if sub.state in ('expire', 'close'):
            if not sub.sh_end_date:
                return
            days_expired = (today - sub.sh_end_date).days
            if days_expired > grace_days and entity.state != 'suspended':
                entity._do_suspend()
                _logger.info(
                    "Suspended %s %s (expired %d days, grace=%d)",
                    entity._name, entity.id, days_expired, grace_days,
                )
            return

        # 4. Cancelled → suspend + check cleanup
        if sub.state == 'cancel':
            if entity.state != 'suspended':
                entity._do_suspend()
                _logger.info("Suspended %s %s (cancelled)", entity._name, entity.id)

            # Check cleanup threshold
            cancel_date = sub.write_date.date() if sub.write_date else today
            months_since_cancel = (today - cancel_date).days / 30.0
            if months_since_cancel > cleanup_months:
                entity._do_cleanup()
                _logger.info(
                    "Cleaned up %s %s (cancelled %.1f months ago)",
                    entity._name, entity.id, months_since_cancel,
                )
```

### Cron XML Definition

```xml
<!-- data/subscription_lifecycle_cron.xml -->
<odoo>
    <data noupdate="1">
        <record id="ir_cron_subscription_lifecycle" model="ir.cron">
            <field name="name">PaaS: Enforce Subscription Lifecycle</field>
            <field name="model_id"
                   ref="model_woow_paas_platform_subscription_lifecycle_cron"/>
            <field name="state">code</field>
            <field name="code">model._cron_enforce_subscription_lifecycle()</field>
            <field name="interval_number">1</field>
            <field name="interval_type">days</field>
            <field name="numbercall">-1</field>
            <field name="active" eval="True"/>
        </record>
    </data>
</odoo>
```

---

## 4. Plan Change Wizard

### Backend Wizard

```python
# src/wizard/change_subscription_plan_wizard.py

class ChangeSubscriptionPlanWizard(models.TransientModel):
    _name = 'woow_paas_platform.change_plan_wizard'
    _description = 'Change Subscription Plan'

    # Context: which entity
    cloud_service_id = fields.Many2one('woow_paas_platform.cloud_service')
    smart_home_id = fields.Many2one('woow_paas_platform.smart_home')

    # Current state (readonly display)
    current_plan_id = fields.Many2one('sh.subscription.plan', readonly=True)
    current_price = fields.Float(readonly=True)
    current_end_date = fields.Date(readonly=True)

    # User selection
    new_plan_id = fields.Many2one(
        'sh.subscription.plan',
        required=True,
        string='New Plan',
    )
    new_price = fields.Float(related='new_plan_id.sh_plan_price')

    # Computed refund
    refund_amount = fields.Float(compute='_compute_refund', string='Refund Points')
    effective_date = fields.Date(default=fields.Date.today)

    @api.depends('current_plan_id', 'current_end_date', 'effective_date')
    def _compute_refund(self):
        for wizard in self:
            if not wizard.current_end_date or not wizard.current_plan_id:
                wizard.refund_amount = 0
                continue
            sub = wizard._get_subscription()
            if not sub or not sub.sh_start_date:
                wizard.refund_amount = 0
                continue
            total_days = (sub.sh_end_date - sub.sh_start_date).days or 1
            remaining_days = max(0, (sub.sh_end_date - wizard.effective_date).days)
            wizard.refund_amount = round(
                sub.sh_plan_price * remaining_days / total_days, 2
            )

    def _get_entity(self):
        return self.cloud_service_id or self.smart_home_id

    def _get_subscription(self):
        entity = self._get_entity()
        return entity.subscription_id if entity else None

    def action_change_plan(self):
        """Execute plan change: close old → refund → create new."""
        self.ensure_one()
        entity = self._get_entity()
        old_sub = self._get_subscription()

        if not entity or not old_sub:
            raise UserError("No active subscription found.")
        if old_sub.state != 'in_progress':
            raise UserError("Can only change plan on active subscriptions.")

        partner = old_sub.sh_partner_id

        # 1. Refund unused points to wallet
        if self.refund_amount > 0:
            partner.sudo().add_wallet_points(
                amount=self.refund_amount,
                description="Plan change refund: %s → %s" % (
                    self.current_plan_id.name, self.new_plan_id.name
                ),
            )

        # 2. Close old subscription
        old_sub.sudo().write({
            'state': 'close',
            'sh_reason': 'Plan changed to: %s' % self.new_plan_id.name,
        })

        # 3. Create new subscription with new plan
        entity._create_and_confirm_subscription(partner, self.new_plan_id)

        return {'type': 'ir.actions.act_window_close'}
```

### API Endpoint (for OWL frontend)

```python
# Added to src/controllers/paas.py

@route('/api/cloud-services/services/<int:service_id>/change-plan',
       type='json', auth='user', methods=['POST'])
def api_service_change_plan(self, service_id, plan_id=None, **kw):
    """Change subscription plan for a cloud service."""
    if not plan_id:
        return {'success': False, 'error': 'plan_id is required'}

    service = request.env['woow_paas_platform.cloud_service'].browse(service_id)
    if not service.exists():
        return {'success': False, 'error': 'Service not found'}

    # Permission check
    workspace = service.workspace_id
    if not self._check_workspace_access(workspace, required_role='admin'):
        return {'success': False, 'error': 'Admin access required'}

    plan = request.env['sh.subscription.plan'].browse(plan_id)
    if not plan.exists():
        return {'success': False, 'error': 'Plan not found'}

    old_sub = service.subscription_id
    if not old_sub or old_sub.state != 'in_progress':
        return {'success': False, 'error': 'No active subscription'}

    # Calculate refund
    total_days = (old_sub.sh_end_date - old_sub.sh_start_date).days or 1
    remaining_days = max(0, (old_sub.sh_end_date - fields.Date.today()).days)
    refund = round(old_sub.sh_plan_price * remaining_days / total_days, 2)

    partner = old_sub.sh_partner_id

    # Refund
    if refund > 0:
        partner.sudo().add_wallet_points(
            amount=refund,
            description="Plan change refund: %s" % old_sub.sh_subscription_plan_id.name,
        )

    # Close old
    old_sub.sudo().write({
        'state': 'close',
        'sh_reason': 'Plan changed to: %s' % plan.name,
    })

    # Create new
    service._create_and_confirm_subscription(partner, plan)

    return {
        'success': True,
        'refund_amount': refund,
        'new_plan': plan.name,
    }
```

Smart Home 的 change-plan endpoint 結構相同，路由不同。

---

## 5. PaaS Operator Changes

### 5.1 New Endpoint: Scale Release

```python
# extra/paas-operator/src/api/releases.py (addition)

@router.post("/api/releases/{name}/scale")
async def scale_release(
    name: str,
    namespace: str = Query(...),
    replicas: int = Query(..., ge=0),
    api_key: str = Depends(verify_api_key),
):
    """Scale a Helm release's deployment replicas.

    Used for subscription suspend (replicas=0) and resume (replicas=1).
    """
    helm = HelmService()
    await helm.scale_release(name, namespace, replicas)
    return {"success": True, "release": name, "replicas": replicas}
```

### 5.2 HelmService.scale_release

```python
# extra/paas-operator/src/services/helm.py (addition)

async def scale_release(self, name: str, namespace: str, replicas: int):
    """Scale deployment replicas via kubectl.

    Helm doesn't have a native scale command, so we use kubectl
    to scale the deployment directly.
    """
    # Find deployment(s) in this release
    result = await self._run_command([
        "kubectl", "get", "deployment",
        "-n", namespace,
        "-l", f"app.kubernetes.io/instance={name}",
        "-o", "jsonpath={.items[*].metadata.name}",
    ])
    deployments = result.strip().split()

    for deployment in deployments:
        if not deployment:
            continue
        await self._run_command([
            "kubectl", "scale", "deployment", deployment,
            "-n", namespace,
            f"--replicas={replicas}",
        ])
```

### 5.3 Tunnel Ingress Update

`configure_tunnel` 已存在於 `cloudflare.py`，直接使用：

```python
# Suspend: service_url="http_status:503"
await cloudflare.configure_tunnel(tunnel_id, hostname, "http_status:503")

# Resume: service_url="http://localhost:8123"
await cloudflare.configure_tunnel(tunnel_id, hostname, "http://localhost:8123")
```

PaaS Operator 的 `POST /api/tunnels` 中的 `configure_tunnel` 呼叫
已支持此功能，不需額外端點。只需確保 Odoo 端的
`PaaSOperatorClient` 有對應的 `configure_tunnel(tunnel_id, hostname, service_url)` 方法。

---

## 6. API Changes

### 6.1 Modified: Create Service

```
POST /api/workspaces/{id}/services
  Body: {
    template_id: int,
    name: string,
    plan_id: int,           // NEW: required
    values: {...},
    odoo_addon_ids: [...]
  }
```

Controller 流程：
1. (existing) 驗證 template, name
2. (existing) 建立 CloudService record
3. **NEW**: `service._create_and_confirm_subscription(partner, plan)`
4. (existing) 觸發 deploy cron

### 6.2 Modified: Create Smart Home

```
POST /api/workspaces/{id}/smarthomes
  Body: {
    name: string,
    plan_id: int,           // NEW: required
    ha_port: int
  }
```

### 6.3 Modified: Service/SmartHome Operations

所有 write 操作（upgrade, redeploy, delete, start, stop）需加入 subscription check：

```python
# In controller, before executing any write operation:
service._check_operation_allowed(operation='write')
```

### 6.4 Modified: Service Detail Response

```python
def _format_service(self, service):
    data = {
        # ... existing fields ...

        # NEW: Subscription info
        'subscription': {
            'id': sub.id if sub else None,
            'state': sub.state if sub else None,
            'plan_name': sub.sh_subscription_plan_id.name if sub else None,
            'plan_price': sub.sh_plan_price if sub else None,
            'is_trial': sub.sh_trial if sub else False,
            'trial_end_date': sub.sh_trial_end_date.isoformat() if sub and sub.sh_trial_end_date else None,
            'start_date': sub.sh_start_date.isoformat() if sub and sub.sh_start_date else None,
            'end_date': sub.sh_end_date.isoformat() if sub and sub.sh_end_date else None,
            'next_payment': sub.sh_date_of_next_payment.isoformat() if sub and sub.sh_date_of_next_payment else None,
            'last_payment_status': sub.sh_last_payment_status if sub else None,
        } if (sub := service.subscription_id) else None,
    }
    return data
```

### 6.5 New: Available Plans Endpoint

```
GET /api/cloud-services/templates/{id}/plans
  Response: {
    success: true,
    plans: [
      {
        id: 1,
        name: "Monthly",
        price: 299,
        unit: "month",
        duration: 1,
        trial_days: 7,
        has_trial: true,
      },
      {
        id: 2,
        name: "Yearly",
        price: 2990,
        unit: "year",
        duration: 1,
        trial_days: 7,
        has_trial: true,
      }
    ]
  }
```

---

## 7. Data Files (XML)

### 7.1 Subscription Products & Plans

```xml
<!-- data/subscription_products.xml -->
<odoo>
    <data noupdate="1">

        <!-- ========== Products ========== -->
        <record id="product_subscription_anythingllm" model="product.product">
            <field name="name">AnythingLLM Subscription</field>
            <field name="type">service</field>
            <field name="sh_product_subscribe" eval="True"/>
            <field name="list_price">299</field>
            <field name="sale_ok" eval="True"/>
            <field name="purchase_ok" eval="False"/>
        </record>

        <record id="product_subscription_n8n" model="product.product">
            <field name="name">n8n Subscription</field>
            <field name="type">service</field>
            <field name="sh_product_subscribe" eval="True"/>
            <field name="list_price">199</field>
            <field name="sale_ok" eval="True"/>
            <field name="purchase_ok" eval="False"/>
        </record>

        <record id="product_subscription_postgresql" model="product.product">
            <field name="name">PostgreSQL Subscription</field>
            <field name="type">service</field>
            <field name="sh_product_subscribe" eval="True"/>
            <field name="list_price">99</field>
            <field name="sale_ok" eval="True"/>
            <field name="purchase_ok" eval="False"/>
        </record>

        <record id="product_subscription_odoo" model="product.product">
            <field name="name">Odoo Subscription</field>
            <field name="type">service</field>
            <field name="sh_product_subscribe" eval="True"/>
            <field name="list_price">499</field>
            <field name="sale_ok" eval="True"/>
            <field name="purchase_ok" eval="False"/>
        </record>

        <record id="product_subscription_smarthome" model="product.product">
            <field name="name">Smart Home Subscription</field>
            <field name="type">service</field>
            <field name="sh_product_subscribe" eval="True"/>
            <field name="list_price">100</field>
            <field name="sale_ok" eval="True"/>
            <field name="purchase_ok" eval="False"/>
        </record>

        <!-- ========== Plans (Monthly) ========== -->
        <record id="plan_anythingllm_monthly" model="sh.subscription.plan">
            <field name="name">AnythingLLM Monthly</field>
            <field name="sh_duration">1</field>
            <field name="sh_unit">month</field>
            <field name="sh_plan_price">299</field>
            <field name="sh_override_product" eval="True"/>
            <field name="sh_no_of_billing_cycle">-1</field>
            <field name="sh_trial" eval="True"/>
            <field name="sh_trial_duration">7</field>
            <field name="sh_trial_unit">day</field>
            <field name="require_credit_card" eval="False"/>
        </record>

        <!-- (Same pattern for n8n, PostgreSQL, Odoo, SmartHome monthly) -->

        <!-- ========== Plans (Yearly) ========== -->
        <record id="plan_anythingllm_yearly" model="sh.subscription.plan">
            <field name="name">AnythingLLM Yearly</field>
            <field name="sh_duration">1</field>
            <field name="sh_unit">year</field>
            <field name="sh_plan_price">2990</field>
            <field name="sh_override_product" eval="True"/>
            <field name="sh_no_of_billing_cycle">-1</field>
            <field name="sh_trial" eval="True"/>
            <field name="sh_trial_duration">7</field>
            <field name="sh_trial_unit">day</field>
            <field name="require_credit_card" eval="False"/>
        </record>

        <!-- (Same pattern for n8n, PostgreSQL, Odoo, SmartHome yearly) -->

        <!-- ========== Reminders ========== -->
        <record id="reminder_3days" model="sh.reminder.template">
            <field name="sh_reminder">3</field>
            <field name="sh_reminder_unit">day(s)</field>
        </record>

        <record id="reminder_1day" model="sh.reminder.template">
            <field name="sh_reminder">1</field>
            <field name="sh_reminder_unit">day(s)</field>
        </record>

        <!-- Link reminders to all plans (via plan's sh_reminder M2M) -->

        <!-- ========== Link templates to products ========== -->
        <!-- Updated via post-migration or by setting
             subscription_product_id on existing cloud_app_template records -->

    </data>
</odoo>
```

### 7.2 Template-Product Linking

```xml
<!-- data/subscription_template_links.xml -->
<odoo>
    <data noupdate="0">
        <!-- Link each cloud_app_template to its subscription product -->
        <record id="cloud_app_template_anythingllm" model="woow_paas_platform.cloud_app_template">
            <field name="subscription_product_id" ref="product_subscription_anythingllm"/>
        </record>
        <!-- ... same for n8n, postgresql, odoo ... -->
    </data>
</odoo>
```

Note: 需確認現有 template XML 的 id 名稱。如果 template 是在
`data/cloud_app_templates.xml` 中定義的，直接在同一個或新的 XML 中
用相同 id 更新 `subscription_product_id` 欄位。

---

## 8. Frontend Changes (OWL)

### 8.1 API Service Layer

```javascript
// src/static/src/paas/services/cloud_service.js (additions)

// Fetch available plans for a template
async fetchPlans(templateId) {
    const result = await this.rpc('/api/cloud-services/templates/' + templateId + '/plans');
    return result.success ? result.plans : [];
}

// Change subscription plan
async changePlan(workspaceId, serviceId, planId) {
    return this.rpc('/api/cloud-services/services/' + serviceId + '/change-plan', {
        plan_id: planId,
    });
}
```

### 8.2 Deploy Modal — Plan Selection

```
┌─────────────────────────────────────────────┐
│  Deploy AnythingLLM                         │
│                                             │
│  Service Name: [________________]           │
│                                             │
│  Select Plan:                               │
│  ┌─────────────────┐ ┌─────────────────┐   │
│  │ ○ Monthly       │ │ ○ Yearly        │   │
│  │   299 pts/mo    │ │   2,990 pts/yr  │   │
│  │   7-day trial   │ │   7-day trial   │   │
│  └─────────────────┘ └─────────────────┘   │
│                                             │
│  [Cancel]                        [Deploy]   │
└─────────────────────────────────────────────┘
```

### 8.3 Subscription Status Badge

```xml
<!-- components/SubscriptionBadge.xml -->
<t t-name="woow_paas_platform.SubscriptionBadge">
    <span t-attf-class="o_woow_subscription_badge o_woow_subscription_badge--#{state}">
        <t t-if="state === 'in_progress' and isTrial">Trial</t>
        <t t-elif="state === 'in_progress'">Active</t>
        <t t-elif="state === 'expire' or state === 'close'">Expired</t>
        <t t-elif="state === 'cancel'">Cancelled</t>
        <t t-elif="state === 'renewed'">Renewed</t>
    </span>
</t>
```

### 8.4 Service Card — Add Subscription Info

```
┌─────────────────────────────────────────┐
│  🔵 n8n                    [Active] ✓   │  ← subscription badge
│  automation.woowtech.io                 │
│                                         │
│  Status: Running                        │
│  Plan: Monthly (199 pts/mo)             │  ← NEW
│  Next payment: 2026-05-15               │  ← NEW
└─────────────────────────────────────────┘
```

### 8.5 Grace Period Warning Banner

```
┌─────────────────────────────────────────┐
│  ⚠️ Your subscription expired on        │
│  2026-04-10. You have 4 days left       │
│  to renew before service suspension.    │
│                            [Renew Now]  │
└─────────────────────────────────────────┘
```

### 8.6 Suspended Banner

```
┌─────────────────────────────────────────┐
│  🔴 Service suspended                   │
│  Your subscription has expired.         │
│  Renew to restore access.              │
│                            [Renew Now]  │
└─────────────────────────────────────────┘
```

---

## 9. File Change Summary

### New Files

| File | Description |
|------|-------------|
| `src/models/subscription_lifecycle_mixin.py` | Shared subscription logic mixin |
| `src/models/subscription_lifecycle_cron.py` | Lifecycle enforcement cron |
| `src/wizard/change_subscription_plan_wizard.py` | Plan change wizard |
| `src/wizard/__init__.py` | Wizard package init |
| `data/subscription_products.xml` | Products, plans, reminders |
| `data/subscription_lifecycle_cron.xml` | Cron definition |
| `views/change_plan_wizard_views.xml` | Wizard form view |
| `static/src/paas/components/SubscriptionBadge.js` | Status badge component |
| `static/src/paas/components/SubscriptionBadge.xml` | Status badge template |
| `static/src/paas/components/PlanSelector.js` | Plan selection component |
| `static/src/paas/components/PlanSelector.xml` | Plan selection template |
| `static/src/paas/styles/components/_subscription.scss` | Subscription styles |

### Modified Files

| File | Changes |
|------|---------|
| `__manifest__.py` | Add depends, data files |
| `models/__init__.py` | Import new modules |
| `models/cloud_service.py` | Add mixin, suspended state, suspend/resume/cleanup |
| `models/smart_home.py` | Add mixin, suspended state, suspend/resume/cleanup |
| `models/cloud_app_template.py` | Remove monthly_price, add subscription_product_id |
| `models/res_config_settings.py` | Add grace_period_days, cleanup_after_months |
| `hooks.py` | Add new config_parameter keys |
| `controllers/paas.py` | Add plan_id to create, subscription check, new endpoints |
| `controllers/smart_home.py` | Add plan_id to create, subscription check |
| `security/ir.model.access.csv` | Add wizard access rules |
| `views/res_config_settings_views.xml` | Add subscription settings section |
| `static/src/paas/services/cloud_service.js` | Add plan/subscription methods |
| `static/src/paas/services/workspace_service.js` | Add plan_id to create smart home |
| `static/src/paas/pages/workspace/WorkspaceDetailPage.js` | Show subscription badges |
| `static/src/paas/pages/workspace/WorkspaceDetailPage.xml` | Subscription UI elements |
| `static/src/paas/components/cloud-service/ServiceDetailPage.js` | Subscription info section |
| `static/src/paas/components/smart-home/SmartHomeDetailPage.js` | Subscription info section |
| `static/src/paas/components/cloud-service/CreateServiceModal.js` | Plan selection |
| `static/src/paas/components/smart-home/CreateSmartHomeModal.js` | Plan selection |
| `extra/paas-operator/src/api/releases.py` | Add scale endpoint |
| `extra/paas-operator/src/services/helm.py` | Add scale_release method |

---

## 10. Migration Strategy

### Database Migration

```python
# src/migrations/18.0.1.0.7/pre-migrate.py

def migrate(cr, version):
    """Add subscription-related columns before module update."""
    # Add subscription_id columns (nullable)
    cr.execute("""
        ALTER TABLE woow_paas_platform_cloud_service
        ADD COLUMN IF NOT EXISTS subscription_id INTEGER;
    """)
    cr.execute("""
        ALTER TABLE woow_paas_platform_smart_home
        ADD COLUMN IF NOT EXISTS subscription_id INTEGER;
    """)
```

```python
# src/migrations/18.0.1.0.7/post-migrate.py

def migrate(cr, version):
    """Remove deprecated monthly_price field."""
    cr.execute("""
        ALTER TABLE woow_paas_platform_cloud_app_template
        DROP COLUMN IF EXISTS monthly_price;
    """)
```

### Existing Services (No Subscription)

現有已部署的 service 和 smart home 不會有 subscription。
Mixin 的 `_is_subscription_active()` 在 `subscription_id` 為空時回傳 `True`，
確保向後相容：

```python
def _is_subscription_active(self):
    if not self.subscription_id:
        return True  # Legacy entities: unrestricted
```

Admin 可以手動為現有 service 建立 subscription，或保持不受限。

---

## 11. Sequence Diagrams

### Deploy Service with Subscription

```
User          Frontend        Controller         Model              sh_subscription
  │              │                │                 │                      │
  │─ Deploy ────▶│                │                 │                      │
  │ (name,       │─ POST ───────▶│                 │                      │
  │  template,   │  /services    │                 │                      │
  │  plan_id)    │               │─ create() ─────▶│                      │
  │              │               │                 │                      │
  │              │               │                 │─ _create_and_confirm │
  │              │               │                 │   _subscription() ──▶│
  │              │               │                 │                      │─ create()
  │              │               │                 │                      │─ confirm()
  │              │               │                 │                      │  → in_progress
  │              │               │                 │◀── subscription ─────│  (with trial)
  │              │               │                 │                      │
  │              │               │─ trigger cron ─▶│                      │
  │              │◀── response ──│                 │                      │
  │◀── UI ──────│               │                 │                      │
  │              │               │                 │                      │
  │              │    [Cron picks up pending service and deploys via Helm] │
```

### Subscription Expiry → Suspend → Renew

```
sh_subscription        Lifecycle Cron          CloudService        PaaS Operator
     │                      │                      │                    │
     │─ state=expire ──────▶│                      │                    │
     │  (after billing      │                      │                    │
     │   cycle ends)        │                      │                    │
     │                      │                      │                    │
     │        [Day 1-7: Grace period - no action]  │                    │
     │                      │                      │                    │
     │        [Day 8: Past grace period]           │                    │
     │                      │─ _do_suspend() ─────▶│                    │
     │                      │                      │─ scale(0) ────────▶│
     │                      │                      │  state=suspended   │
     │                      │                      │                    │
     │        [User renews subscription]           │                    │
     │                      │                      │                    │
     │─ state=renewed ─────▶│                      │                    │
     │  (new sub created)   │─ follow chain ──────▶│                    │
     │                      │─ _do_resume() ──────▶│                    │
     │                      │                      │─ scale(1) ────────▶│
     │                      │                      │  state=running     │
```

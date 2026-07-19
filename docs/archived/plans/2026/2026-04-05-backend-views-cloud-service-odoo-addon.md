---
status: archived
archived-from: docs/plans/2026-Q2/2026-04-05-backend-views-cloud-service-odoo-addon.md
archived: 2026-07-19
archived-reason: "已落地；規格現行權威=docs/reference/features/cloud-services.md"
---

# Backend Views: Cloud Service & Odoo Addon — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add backend list/form views for Cloud Service and Cloud Odoo Addon models, organized under a new "Cloud" submenu group.

**Architecture:** Create two new view XML files following the existing pattern in `cloud_app_template_views.xml` and `mcp_server_views.xml`. Add a "Cloud" parent menuitem in `menu.xml`, then re-parent the existing Templates menu and add Services + Odoo Addons menus under it.

**Tech Stack:** Odoo 18 XML views, ir.actions.act_window, menuitem hierarchy

---

### Task 1: Add Cloud submenu group to menu.xml

**Files:**
- Modify: `src/views/menu.xml`

**Step 1: Add the Cloud parent menuitem**

In `src/views/menu.xml`, add a `menuitem` for the "Cloud" group under the root menu:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<odoo>
    <!-- 主選單 -->
    <menuitem
        id="menu_woow_paas_platform_root"
        name="Woow PaaS"
        sequence="100"
        web_icon="woow_paas_platform,static/description/icon.png"/>

    <!-- Cloud 子選單群組 -->
    <menuitem
        id="menu_cloud"
        name="Cloud"
        parent="menu_woow_paas_platform_root"
        sequence="30"/>
</odoo>
```

**Step 2: Commit**

```bash
git add src/views/menu.xml
git commit -m "feat: add Cloud submenu group to Woow PaaS menu"
```

---

### Task 2: Move Cloud App Templates menu under Cloud group

**Files:**
- Modify: `src/views/cloud_app_template_views.xml:119-124`

**Step 1: Change the parent of the existing Templates menuitem**

In `src/views/cloud_app_template_views.xml`, change the `<menuitem>` at the bottom from:

```xml
    <menuitem
        id="menu_cloud_app_templates"
        name="Cloud App Templates"
        parent="menu_woow_paas_platform_root"
        action="action_cloud_app_template"
        sequence="40"/>
```

to:

```xml
    <menuitem
        id="menu_cloud_app_templates"
        name="Templates"
        parent="menu_cloud"
        action="action_cloud_app_template"
        sequence="10"/>
```

Key changes:
- `name` shortened to "Templates" (parent "Cloud" provides context)
- `parent` changed to `menu_cloud`
- `sequence="10"` (first in Cloud group)

**Step 2: Commit**

```bash
git add src/views/cloud_app_template_views.xml
git commit -m "refactor: move Cloud App Templates menu under Cloud group"
```

---

### Task 3: Create Cloud Service views

**Files:**
- Create: `src/views/cloud_service_views.xml`

**Step 1: Create the view file**

Create `src/views/cloud_service_views.xml` with list view, form view, action, and menuitem:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<odoo>
    <!-- Cloud Service Form View -->
    <record id="view_cloud_service_form" model="ir.ui.view">
        <field name="name">woow_paas_platform.cloud_service.form</field>
        <field name="model">woow_paas_platform.cloud_service</field>
        <field name="arch" type="xml">
            <form string="Cloud Service">
                <header>
                    <field name="state" widget="statusbar"
                           statusbar_visible="pending,deploying,running"/>
                </header>
                <sheet>
                    <div class="oe_title">
                        <h1>
                            <field name="name" placeholder="Service Name"/>
                        </h1>
                    </div>
                    <group>
                        <group string="Basic Info">
                            <field name="workspace_id"/>
                            <field name="template_id"/>
                            <field name="reference_id"/>
                            <field name="subdomain"/>
                            <field name="custom_domain"/>
                        </group>
                        <group string="Timestamps">
                            <field name="deployed_at"/>
                            <field name="last_upgraded_at"/>
                        </group>
                    </group>
                    <notebook>
                        <page string="Helm Config" name="helm">
                            <group>
                                <group>
                                    <field name="helm_release_name"/>
                                    <field name="helm_namespace"/>
                                    <field name="helm_revision"/>
                                    <field name="helm_chart_version"/>
                                </group>
                            </group>
                            <group string="Helm Values">
                                <field name="helm_values" nolabel="1"/>
                            </group>
                        </page>
                        <page string="Resources" name="resources">
                            <group>
                                <group>
                                    <field name="allocated_vcpu"/>
                                    <field name="allocated_ram_gb"/>
                                    <field name="allocated_storage_gb"/>
                                </group>
                            </group>
                        </page>
                        <page string="Odoo Addons" name="odoo_addons">
                            <field name="odoo_addon_ids">
                                <list>
                                    <field name="name"/>
                                    <field name="technical_name"/>
                                    <field name="category"/>
                                    <field name="source_type"/>
                                </list>
                            </field>
                        </page>
                        <page string="Errors" name="errors"
                              invisible="not error_message and not init_error">
                            <group>
                                <field name="error_message"/>
                                <field name="init_error"/>
                                <field name="init_retries"/>
                            </group>
                        </page>
                    </notebook>
                </sheet>
            </form>
        </field>
    </record>

    <!-- Cloud Service List View -->
    <record id="view_cloud_service_list" model="ir.ui.view">
        <field name="name">woow_paas_platform.cloud_service.list</field>
        <field name="model">woow_paas_platform.cloud_service</field>
        <field name="arch" type="xml">
            <list string="Cloud Services">
                <field name="name"/>
                <field name="workspace_id"/>
                <field name="template_id"/>
                <field name="state" widget="badge"
                       decoration-success="state == 'running'"
                       decoration-info="state in ('pending', 'deploying', 'initializing', 'upgrading')"
                       decoration-danger="state == 'error'"
                       decoration-warning="state == 'deleting'"/>
                <field name="subdomain"/>
                <field name="helm_namespace"/>
                <field name="deployed_at"/>
            </list>
        </field>
    </record>

    <!-- Cloud Service Action -->
    <record id="action_cloud_service" model="ir.actions.act_window">
        <field name="name">Services</field>
        <field name="res_model">woow_paas_platform.cloud_service</field>
        <field name="view_mode">list,form</field>
        <field name="help" type="html">
            <p class="o_view_nocontent_smiling_face">
                No cloud services deployed yet
            </p>
            <p>
                Cloud services are deployed from application templates via the marketplace.
            </p>
        </field>
    </record>

    <!-- Menu under Cloud -->
    <menuitem
        id="menu_cloud_services"
        name="Services"
        parent="menu_cloud"
        action="action_cloud_service"
        sequence="20"/>
</odoo>
```

**Step 2: Commit**

```bash
git add src/views/cloud_service_views.xml
git commit -m "feat: add Cloud Service backend list and form views"
```

---

### Task 4: Create Cloud Odoo Addon views

**Files:**
- Create: `src/views/cloud_odoo_addon_views.xml`

**Step 1: Create the view file**

Create `src/views/cloud_odoo_addon_views.xml`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<odoo>
    <!-- Cloud Odoo Addon Form View -->
    <record id="view_cloud_odoo_addon_form" model="ir.ui.view">
        <field name="name">woow_paas_platform.cloud_odoo_addon.form</field>
        <field name="model">woow_paas_platform.cloud_odoo_addon</field>
        <field name="arch" type="xml">
            <form string="Cloud Odoo Addon">
                <sheet>
                    <div class="oe_title">
                        <h1>
                            <field name="name" placeholder="Addon Name"/>
                        </h1>
                        <field name="technical_name" placeholder="technical_name"/>
                    </div>
                    <group>
                        <group string="Basic Info">
                            <field name="category"/>
                            <field name="source_type"/>
                            <field name="icon"/>
                            <field name="description"/>
                        </group>
                        <group string="Configuration">
                            <field name="is_default"/>
                            <field name="active"/>
                        </group>
                    </group>
                    <notebook>
                        <page string="Dependencies" name="dependencies">
                            <field name="depends_on">
                                <list>
                                    <field name="name"/>
                                    <field name="technical_name"/>
                                </list>
                            </field>
                        </page>
                        <page string="Compatible Templates" name="templates">
                            <field name="template_ids">
                                <list>
                                    <field name="name"/>
                                    <field name="slug"/>
                                    <field name="category"/>
                                </list>
                            </field>
                        </page>
                    </notebook>
                </sheet>
            </form>
        </field>
    </record>

    <!-- Cloud Odoo Addon List View -->
    <record id="view_cloud_odoo_addon_list" model="ir.ui.view">
        <field name="name">woow_paas_platform.cloud_odoo_addon.list</field>
        <field name="model">woow_paas_platform.cloud_odoo_addon</field>
        <field name="arch" type="xml">
            <list string="Cloud Odoo Addons">
                <field name="name"/>
                <field name="technical_name"/>
                <field name="category"/>
                <field name="source_type"/>
                <field name="is_default"/>
                <field name="active"/>
            </list>
        </field>
    </record>

    <!-- Cloud Odoo Addon Action -->
    <record id="action_cloud_odoo_addon" model="ir.actions.act_window">
        <field name="name">Odoo Addons</field>
        <field name="res_model">woow_paas_platform.cloud_odoo_addon</field>
        <field name="view_mode">list,form</field>
        <field name="help" type="html">
            <p class="o_view_nocontent_smiling_face">
                No Odoo addons found
            </p>
            <p>
                Odoo addons are synced automatically from the addon catalog.
            </p>
        </field>
    </record>

    <!-- Menu under Cloud -->
    <menuitem
        id="menu_cloud_odoo_addons"
        name="Odoo Addons"
        parent="menu_cloud"
        action="action_cloud_odoo_addon"
        sequence="30"/>
</odoo>
```

**Step 2: Commit**

```bash
git add src/views/cloud_odoo_addon_views.xml
git commit -m "feat: add Cloud Odoo Addon backend list and form views"
```

---

### Task 5: Register new view files in __manifest__.py

**Files:**
- Modify: `src/__manifest__.py:26-29`

**Step 1: Add new view files to the data list**

In `src/__manifest__.py`, add the two new view files in the `data` list. Insert them after `cloud_app_template_views.xml` (line 25):

Change:

```python
        "views/cloud_app_template_views.xml",
        "views/mcp_server_views.xml",
```

to:

```python
        "views/cloud_app_template_views.xml",
        "views/cloud_service_views.xml",
        "views/cloud_odoo_addon_views.xml",
        "views/mcp_server_views.xml",
```

**Step 2: Commit**

```bash
git add src/__manifest__.py
git commit -m "feat: register Cloud Service and Odoo Addon views in manifest"
```

---

### Task 6: Verify by updating the module

**Step 1: Update the Odoo module**

```bash
# If using Docker dev environment:
./scripts/start-dev.sh
# Or manual update:
./odoo-bin -c odoo.conf -u woow_paas_platform --stop-after-init
```

**Step 2: Verify in browser**

1. Navigate to Woow PaaS menu
2. Confirm "Cloud" submenu group exists with:
   - Templates (existing, renamed)
   - Services (new list view)
   - Odoo Addons (new list view)
3. Click into each to verify list and form views render correctly

**Step 3: Final commit (if any fixups needed)**

```bash
git add -A
git commit -m "fix: adjust views after verification"
```

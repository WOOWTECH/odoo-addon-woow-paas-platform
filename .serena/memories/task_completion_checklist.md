# Task Completion Checklist

## After Completing a Task

1. **Verify changes compile/load**: Module should load without errors
2. **Run tests if applicable**:
   - Odoo: `./scripts/test-addon.sh` or `./odoo-bin -c odoo.conf --test-enable --test-tags woow_paas_platform --stop-after-init`
   - PaaS Operator: `cd extra/paas-operator && pytest tests/ -v`
3. **Check security**: Update `src/security/ir.model.access.csv` if new models added
4. **Update manifest**: Add new data/views files to `src/__manifest__.py`
5. **Import chain**: Add new model imports to `src/models/__init__.py`
6. **Commit message**: Use English, format `{prefix}: {message}`, no AI attribution

## For New Models
- [ ] Model file in `src/models/`
- [ ] Import in `src/models/__init__.py`
- [ ] Security rules in `src/security/ir.model.access.csv`
- [ ] Views in `src/views/`
- [ ] Update `src/__manifest__.py` data list

## For Frontend Changes
- [ ] OWL components in `src/static/src/paas/components/`
- [ ] Register in `src/__manifest__.py` under assets
- [ ] SCSS follows `.o_woow_` prefix convention

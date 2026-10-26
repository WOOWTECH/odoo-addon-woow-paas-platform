"""Regression coverage for marketplace configure-field accessibility (#2006)."""

from pathlib import Path

from lxml import etree

from odoo.modules import get_module_path
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


_PAAS = Path(get_module_path('woow_paas_platform')) / 'static' / 'src' / 'paas'
_ERROR_ID = "hasError(spec.key) ? 'helm_' + spec.key + '_error' : false"
_ERROR_STATE = "hasError(spec.key) ? 'true' : 'false'"


def _template(path):
    return etree.parse(str(_PAAS / path)).getroot()


def _helm_controls(root, section):
    return root.xpath(
        "//t[@t-foreach='%s']//input[@t-att-id=\"'helm_' + spec.key\"] | "
        "//t[@t-foreach='%s']//select[@t-att-id=\"'helm_' + spec.key\"]"
        % (section, section)
    )


def _error_nodes(root, section):
    return root.xpath(
        "//t[@t-foreach='%s']//p["
        "contains(concat(' ', normalize-space(@class), ' '), ' o_woow_field_error ')]"
        % section
    )


@tagged('post_install', '-at_install')
class TestConfigureRequiredA11y2006(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.page = _template('pages/configure/AppConfigurationPage.xml')
        cls.form = _template('components/config/HelmValueForm.xml')

    def test_service_name_exposes_required_and_error_semantics(self):
        field = self.page.xpath("//input[@id='service_name']")
        self.assertEqual(len(field), 1)
        self.assertEqual(field[0].get('required'), 'required')
        self.assertEqual(field[0].get('aria-required'), 'true')
        self.assertEqual(
            field[0].get('t-att-aria-invalid'),
            "state.errors.name ? 'true' : 'false'",
        )
        self.assertEqual(
            field[0].get('t-att-aria-describedby'),
            "state.errors.name ? 'service_name_error' : false",
        )
        error = self.page.xpath("//p[@id='service_name_error']")
        self.assertEqual(len(error), 1)
        self.assertEqual(error[0].get('role'), 'alert')

    def test_every_helm_control_links_its_validation_error(self):
        controls = _helm_controls(self.form, 'requiredSpecs') + _helm_controls(
            self.form, 'optionalSpecs')
        self.assertTrue(controls)
        for control in controls:
            with self.subTest(tag=control.tag, css_class=control.get('class')):
                self.assertEqual(control.get('t-att-aria-invalid'), _ERROR_STATE)
                self.assertEqual(control.get('t-att-aria-describedby'), _ERROR_ID)

        errors = _error_nodes(self.form, 'requiredSpecs') + _error_nodes(
            self.form, 'optionalSpecs')
        self.assertTrue(errors)
        for error in errors:
            with self.subTest(section=error.getparent().get('t-if')):
                self.assertEqual(
                    error.get('t-att-id'), "'helm_' + spec.key + '_error'")
                self.assertEqual(error.get('role'), 'alert')

    def test_required_text_and_select_follow_spec_required_flag(self):
        controls = self.form.xpath(
            "//t[@t-foreach='requiredSpecs']//input[not(@type='checkbox')] | "
            "//t[@t-foreach='requiredSpecs']//select"
        )
        self.assertTrue(controls)
        for control in controls:
            with self.subTest(tag=control.tag):
                self.assertEqual(
                    control.get('t-att-required'), 'spec.required !== false')
                self.assertEqual(
                    control.get('t-att-aria-required'),
                    "spec.required !== false ? 'true' : 'false'",
                )

    def test_boolean_false_remains_a_valid_required_value(self):
        checkboxes = self.form.xpath(
            "//input[@type='checkbox'][@t-att-id=\"'helm_' + spec.key\"]"
        )
        self.assertTrue(checkboxes)
        for checkbox in checkboxes:
            with self.subTest(css_class=checkbox.get('class')):
                self.assertIsNone(checkbox.get('required'))
                self.assertIsNone(checkbox.get('t-att-required'))
                self.assertIsNone(checkbox.get('aria-required'))
                self.assertIsNone(checkbox.get('t-att-aria-required'))

    def test_optional_controls_do_not_gain_required_semantics(self):
        controls = _helm_controls(self.form, 'optionalSpecs')
        self.assertTrue(controls)
        for control in controls:
            with self.subTest(tag=control.tag, css_class=control.get('class')):
                self.assertIsNone(control.get('required'))
                self.assertIsNone(control.get('t-att-required'))
                self.assertIsNone(control.get('aria-required'))
                self.assertIsNone(control.get('t-att-aria-required'))

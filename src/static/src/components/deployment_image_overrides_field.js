/** @odoo-module **/

import { Component, xml } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useInputField } from "@web/views/fields/input_field_hook";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { parseImageOverrides } from "./deployment_image_overrides_parser";

export class DeploymentImageOverridesField extends Component {
  static props = { ...standardFieldProps };
  static template = xml`
    <pre t-if="props.readonly" class="mb-0" t-esc="formattedValue"/>
    <textarea t-else="" t-ref="input" class="o_input font-monospace"
      rows="8" spellcheck="false" t-att-id="props.id"
      t-att-aria-label="props.record.fields[props.name].string"/>
  `;

  setup() {
    useInputField({
      getValue: () => this.formattedValue,
      parse: parseImageOverrides,
    });
  }

  get formattedValue() {
    return JSON.stringify(this.props.record.data[this.props.name] || {}, null, 2);
  }
}

registry.category("fields").add("woow_deployment_image_overrides", {
  component: DeploymentImageOverridesField,
  supportedTypes: ["json"],
});

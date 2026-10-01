/** @odoo-module **/
import { whenReady } from "@odoo/owl";
import { mountComponent } from "@web/env";
import { Root } from "./root";
import { bootPaaS } from "./boot";

whenReady(() => bootPaaS(
    (target) => mountComponent(Root, target),
    document.getElementById("woow-paas-root"),
    document.getElementById("woow-paas-boot"),
));

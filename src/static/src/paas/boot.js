/** @odoo-module **/

/**
 * Keep the server-rendered recovery panel outside Owl's mount target. In
 * particular, startServices() may reject before Owl constructs any component.
 * Retry is a full navigation: never reuse a partially started service registry.
 */
export async function bootPaaS(mount, target, fallback) {
  try {
    await mount(target);
    target.dataset.bootState = "ready";
    fallback.remove();
    return true;
  } catch {
    // Never expose the exception (RPC errors may contain server internals).
    target.replaceChildren();
    target.dataset.bootState = "failed";
    fallback.querySelector("[data-boot-message]").textContent =
      fallback.querySelector("[data-boot-failure]").textContent;
    fallback.setAttribute("role", "alert");
    fallback.setAttribute("aria-busy", "false");
    return false;
  }
}

/** @odoo-module **/

export const RESOURCE_REFRESH_MS = 30000;

/** One poll per page, never per card; visibility and destruction bound its lifetime. */
export function createResourcePoller({ load, onData, onError, documentRef = document,
  schedule = setTimeout, cancel = clearTimeout, interval = RESOURCE_REFRESH_MS }) {
  let active = false;
  let inFlight = false;
  let timer = null;
  let queued = false;
  const clear = () => {
    if (timer !== null) cancel(timer);
    timer = null;
  };
  const visible = () => documentRef.visibilityState !== "hidden";
  const refresh = async () => {
    clear();
    if (!active || !visible()) return;
    if (inFlight) {
      queued = true;
      return;
    }
    inFlight = true;
    try {
      const data = await load();
      if (active && visible()) onData(data);
    } catch {
      if (active && visible()) onError();
    } finally {
      inFlight = false;
      if (active && visible()) {
        const delay = queued ? 0 : interval;
        queued = false;
        timer = schedule(refresh, delay);
      }
    }
  };
  const visibilityChanged = () => {
    clear();
    if (visible()) refresh();
    else queued = false;
  };
  return {
    refresh,
    start() {
      if (active) return;
      active = true;
      documentRef.addEventListener("visibilitychange", visibilityChanged);
      refresh();
    },
    stop() {
      active = false;
      queued = false;
      clear();
      documentRef.removeEventListener("visibilitychange", visibilityChanged);
    },
  };
}

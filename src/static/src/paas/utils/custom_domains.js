/** @odoo-module **/

export const CUSTOM_DOMAIN_POLL_INTERVAL_MS = 10000;

/** Poll a custom-domain list without overlapping requests. */
export function createCustomDomainPoller({ load, onData, onError, schedule = setTimeout, cancel = clearTimeout }) {
  let active = false;
  let loading = false;
  let refreshPending = false;
  let timer = null;
  let generation = 0;

  const clearTimer = () => {
    if (timer !== null) {
      cancel(timer);
      timer = null;
    }
  };
  const run = async () => {
    if (!active) return;
    if (loading) {
      refreshPending = true;
      return;
    }
    clearTimer();
    loading = true;
    const requestGeneration = generation;
    try {
      const value = await load();
      if (active && requestGeneration === generation) onData(value);
    } catch (error) {
      if (active && requestGeneration === generation) onError(error);
    } finally {
      loading = false;
      if (!active || requestGeneration !== generation) return;
      if (refreshPending) {
        refreshPending = false;
        void run();
      } else {
        timer = schedule(run, CUSTOM_DOMAIN_POLL_INTERVAL_MS);
      }
    }
  };
  return {
    start() {
      if (active) return;
      active = true;
      generation += 1;
      void run();
    },
    refresh() {
      if (active) void run();
    },
    stop() {
      active = false;
      generation += 1;
      refreshPending = false;
      clearTimer();
    },
  };
}

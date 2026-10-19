/** @odoo-module **/

export const LOG_REFRESH_MS = 5000;

/** Bounded snapshots: never overlap calls or publish a stale source/hidden view. */
export function createLogPoller({ load, onData, onError, documentRef = document,
  schedule = setTimeout, cancel = clearTimeout }) {
  let active = false;
  let paused = false;
  let pending = false;
  let queued = false;
  let revision = 0;
  let timer = null;
  const clear = () => {
    if (timer !== null) cancel(timer);
    timer = null;
  };
  const visible = () => documentRef.visibilityState !== "hidden";
  const run = async (manual = false) => {
    clear();
    if (!active || !visible() || (paused && !manual)) return;
    if (pending) {
      queued = true;
      return;
    }
    const requestRevision = revision;
    pending = true;
    queued = false;
    try {
      const data = await load();
      if (active && visible() && requestRevision === revision) onData(data);
    } catch {
      if (active && visible() && requestRevision === revision) onError();
    } finally {
      pending = false;
      if (active && visible() && (queued || !paused)) {
        const manualNext = queued;
        timer = schedule(() => run(manualNext), manualNext ? 0 : LOG_REFRESH_MS);
      }
    }
  };
  const visibilityChanged = () => {
    revision++;
    clear();
    queued = false;
    if (visible()) run();
  };
  return {
    start() {
      if (active) return;
      active = true;
      documentRef.addEventListener("visibilitychange", visibilityChanged);
      run();
    },
    refresh() {
      revision++;
      run(true);
    },
    setPaused(value) {
      paused = value;
      revision++;
      queued = false;
      clear();
      if (!paused) run();
    },
    stop() {
      active = false;
      revision++;
      queued = false;
      clear();
      documentRef.removeEventListener("visibilitychange", visibilityChanged);
    },
  };
}

export function filterLogLines(text, query) {
  if (!query.trim()) return text;
  const needle = query.trim().toLocaleLowerCase();
  return text.split("\n").filter((line) => line.toLocaleLowerCase().includes(needle)).join("\n");
}

export function logDownloadFilename(serviceId, pod, container, now = new Date()) {
  const clean = (value) => String(value || "logs").replace(/[^a-zA-Z0-9_.-]/g, "_").slice(0, 100);
  return `service-${clean(serviceId)}-${clean(pod)}-${clean(container)}-${now.toISOString().replace(/[:.]/g, "-")}.log`;
}

export function downloadLogSnapshot(text, filename, { documentRef = document, urlRef = URL,
  schedule = setTimeout } = {}) {
  const url = urlRef.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  const link = documentRef.createElement("a");
  link.href = url;
  link.download = filename;
  documentRef.body.appendChild(link);
  link.click();
  link.remove();
  schedule(() => urlRef.revokeObjectURL(url), 1000);
}

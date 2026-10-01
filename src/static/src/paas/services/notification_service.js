/** @odoo-module **/
import { reactive } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { jsonRpc } from "./rpc";

/**
 * notificationService — reactive store behind the header bell (#1310).
 *
 * `POST /api/notifications` returns items with a stable `id` per event. Read
 * state lives in this browser only (localStorage): opening the panel marks the
 * current items as seen, and the badge counts actionable (error / warning)
 * items the viewer has not seen yet. Storage may be unavailable (private mode),
 * so every access is guarded and the badge then simply shows all actionable
 * items.
 */
const SEEN_KEY = "woow_paas_notifications_seen";
const SEEN_LIMIT = 200;
const REFRESH_MS = 5 * 60 * 1000;

function readSeen() {
  try {
    const raw = window.localStorage.getItem(SEEN_KEY);
    const ids = raw ? JSON.parse(raw) : [];
    return Array.isArray(ids) ? ids : [];
  } catch {
    return [];
  }
}

function writeSeen(ids) {
  try {
    window.localStorage.setItem(SEEN_KEY, JSON.stringify(ids.slice(-SEEN_LIMIT)));
  } catch {
    // Storage blocked: read state just won't persist across reloads.
  }
}

export const notificationService = reactive({
  items: [],
  loading: false,
  loaded: false,
  error: null,
  open: false,
  seen: readSeen(),
  _fetchPromise: null,
  _timer: null,

  get unseenActionableCount() {
    const seen = new Set(this.seen);
    return this.items.filter(
      (item) => (item.severity === "error" || item.severity === "warning") && !seen.has(item.id),
    ).length;
  },

  isUnseen(item) {
    return !this.seen.includes(item.id);
  },

  async fetch() {
    if (this._fetchPromise) return this._fetchPromise;
    this._fetchPromise = (async () => {
      this.loading = true;
      this.error = null;
      try {
        const result = await jsonRpc("/api/notifications", {});
        if (!result.success) {
          throw new Error(result.error || _t("Failed to load notifications. Please try again."));
        }
        this.items = result.data.items || [];
        this.loaded = true;
      } catch (err) {
        this.error = err.message || _t("Failed to load notifications. Please try again.");
      } finally {
        this.loading = false;
        this._fetchPromise = null;
      }
    })();
    return this._fetchPromise;
  },

  /** Start background refresh; safe to call more than once. */
  start() {
    this.fetch();
    if (!this._timer) {
      this._timer = setInterval(() => {
        if (!document.hidden) this.fetch();
      }, REFRESH_MS);
    }
  },

  markAllSeen() {
    const ids = new Set(this.seen);
    for (const item of this.items) ids.add(item.id);
    this.seen = [...ids];
    writeSeen(this.seen);
  },

  async toggle() {
    if (this.open) {
      this.close();
      return;
    }
    this.open = true;
    await this.fetch();
  },

  close() {
    if (!this.open) return;
    this.open = false;
    this.markAllSeen();
  },
});

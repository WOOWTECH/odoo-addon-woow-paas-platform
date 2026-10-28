/** @odoo-module **/

/**
 * Envelope of an Access mutation (policy writes, route / service access-config writes).
 *
 * While an Access change is still being applied (a session revocation hold, an
 * unconfirmed origin publication) the backend answers `success: false` although the
 * change itself is stored, and says so with `saved: true`; `error` then explains what
 * is going on. Callers must refresh their view (the change is there) and show that
 * message, instead of treating the write as failed and leaving a stale screen.
 */
export function accessMutation(result) {
  if (result.success) {
    return { success: true, data: result.data };
  }
  return {
    success: false,
    error: result.error,
    saved: result.saved === true,
    data: result.data,
  };
}

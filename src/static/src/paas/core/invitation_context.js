/** @odoo-module **/

// The email link carries its secret only in a URL fragment. Keep it outside
// reactive router state and remove it from the address bar before app startup.
// Reloading the clean URL requires reopening the invitation email.
const ACCEPT_PATH = "workspace-invitation/accept";
let invitationToken = null;
let invitationRevision = 0;

export function captureInvitationFragment() {
  const prefix = `#/${ACCEPT_PATH}/`;
  if (!window.location.hash.startsWith(prefix)) return;
  const candidate = window.location.hash.slice(prefix.length);
  invitationToken = /^[A-Za-z0-9_-]{43}$/.test(candidate) ? candidate : null;
  invitationRevision++;
  window.history.replaceState(
    null, "", `${window.location.pathname}${window.location.search}#/${ACCEPT_PATH}`,
  );
}

export function getInvitationToken() {
  return invitationToken;
}

export function getInvitationRevision() {
  return invitationRevision;
}

export function clearInvitationToken(revision = invitationRevision) {
  if (revision === invitationRevision) invitationToken = null;
}

captureInvitationFragment();

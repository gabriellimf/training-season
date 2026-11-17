import { apiPost, requireApiFailure } from "./api.js";
import { act, goOffline } from "./flow.js";
import { refresh } from "./sync.js";
import { setNote, view } from "./view.js";

export function submitMfaCode(code) {
  act({
    job: "garmin_mfa",
    allowBusy: true,
    request: () => apiPost("/api/garmin/mfa", { code }),
    handle: applyGarminOutcome,
    onFailure: mfaFailure,
  });
}

export function requestGarminLogin() {
  act({
    job: "garmin_login",
    allowBusy: true,
    request: () => apiPost("/api/garmin/login"),
    handle: applyGarminOutcome,
    onFailure: mfaFailure,
  });
}

export function openMfaPanel(focusId) {
  view.mfa.open = true;
  view.focusNext = focusId;
}

function setMfaResult(text, tone, needsNewCode) {
  Object.assign(view.mfa, { open: true, result: text, tone, needsNewCode });
}

async function applyGarminOutcome(outcome) {
  view.state.garmin = outcome.garmin;
  const pending = outcome.garmin.state === "mfa_pending";
  if (outcome.status === "connected") {
    Object.assign(view.mfa, { open: false, result: "", needsNewCode: false });
    setNote(outcome.message);
  } else if (outcome.status === "mfa_required") {
    setMfaResult(outcome.message, "info", false);
    view.focusNext = "mfa-code";
  } else {
    setMfaResult(outcome.message, "error", !pending);
    view.focusNext = outcome.status === "code_rejected" || pending ? "mfa-code" : "mfa-new-code";
  }
  await refresh();
}

async function mfaFailure(failure) {
  requireApiFailure(failure);
  if (failure.kind === "network") return goOffline(null, null);
  setMfaResult(failure.message, failure.kind === "busy" ? "info" : "error", view.mfa.needsNewCode);
  view.focusNext = view.mfa.needsNewCode ? "mfa-new-code" : "mfa-code";
}

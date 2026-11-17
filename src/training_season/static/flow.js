import { requireApiFailure } from "./api.js";
import { FINE_POINTER, JOB_TEXT, TEXT } from "./constants.js";
import { refresh } from "./sync.js";
import { announce, requestRender, restoreText, setNote, view } from "./view.js";

export function focusAfterReply() {
  if (FINE_POINTER.matches) view.focusNext = "composer-input";
}

export async function act({
  job = null,
  pending = null,
  restore = pending,
  allowBusy = false,
  request,
  handle,
  onFailure = null,
}) {
  if (view.inFlight || (!allowBusy && view.state?.busy)) return;
  view.note = null;
  view.lost = null;
  view.pending = pending === null ? null : { text: pending, flag: null };
  view.inFlight = true;
  view.ownJob = job ? { job, startedAt: Date.now() } : null;
  view.scrollNext = true;
  if (job) announce(JOB_TEXT[job]);
  requestRender();
  try {
    const body = await request();
    settleOwn();
    await handle(body);
  } catch (error) {
    settleOwn();
    await (onFailure ?? ((failure) => handleFailure(failure, pending, restore)))(error);
  } finally {
    requestRender();
  }
}

function settleOwn() {
  view.inFlight = false;
  view.ownJob = null;
}

async function handleFailure(failure, pending, restore) {
  requireApiFailure(failure);
  view.pending = null;
  if (failure.kind === "network") return goOffline(pending, restore);
  restoreText(restore);
  setNote(failure.message, failure.kind === "server" ? "error" : "info");
  if (failure.kind === "busy" || failure.kind === "conflict") await refresh();
}

export function goOffline(pending, restore) {
  view.offline = true;
  if (!pending) return restoreText(restore);
  view.pending = { text: pending, flag: TEXT.noResponse };
  view.lost = pending;
}

export function markUnsaved(text, message) {
  view.pending = { text, flag: TEXT.unsaved };
  restoreText(text);
  setNote(message);
}

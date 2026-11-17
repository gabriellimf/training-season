import { apiGet, requireApiFailure } from "./api.js";
import { POLL_MS, TEXT, TICK_MS } from "./constants.js";
import { requestRender, restoreText, setNote, view } from "./view.js";

let pollTimer = null;
let tickTimer = null;

export async function loadState() {
  view.state = await apiGet("/api/state");
  view.offline = false;
}

export async function loadForMode() {
  const mode = view.state.mode;
  if (mode !== "onboarding") view.sessionTooLong = false;
  if (mode === "needs_onboarding" || mode === "onboarding") return loadOnboarding();
  return loadHistory();
}

async function loadOnboarding() {
  const body = await apiGet("/api/onboarding");
  view.base = [{ role: "assistant", content: body.intro }, ...body.messages];
  view.plan = view.state.reopening ? await apiGet("/api/plan") : null;
  view.checkin.phase = "none";
}

async function loadHistory() {
  const [history, plan] = await Promise.all([apiGet("/api/messages?limit=50"), apiGet("/api/plan")]);
  view.base = history.messages;
  view.plan = plan;
  if (view.state.checkin_open) return loadCheckinConversation();
  if (view.checkin.phase === "conversation") view.checkin.phase = "none";
}

async function loadCheckinConversation() {
  const body = await apiGet("/api/checkin");
  const checkin = view.checkin;
  if (checkin.phase !== "conversation") {
    Object.assign(checkin, { phase: "conversation", intro: body.intro, answers: [], kind: null });
    checkin.thread = body.saved_message ? [{ role: "assistant", content: body.saved_message }] : [];
  }
  checkin.messages = body.messages;
}

export async function refresh() {
  try {
    await loadState();
    await loadForMode();
  } catch (error) {
    requireApiFailure(error);
    if (error.kind === "network") view.offline = true;
    else setNote(error.message, "error");
  }
}

function needsPoll() {
  return view.offline || view.state === null || Boolean(view.state.busy && !view.inFlight);
}

export function schedulePoll() {
  if (pollTimer !== null || !needsPoll()) return;
  pollTimer = window.setTimeout(pollOnce, POLL_MS);
}

async function pollOnce() {
  pollTimer = null;
  if (view.state === null) await retryBoot();
  else await pollState();
  requestRender();
}

async function retryBoot() {
  view.note = null;
  await refresh();
}

async function pollState() {
  try {
    await loadState();
    if (!view.state.busy) {
      await loadForMode();
      settleLost();
    }
  } catch (error) {
    requireApiFailure(error);
    view.offline = error.kind === "network" || view.offline;
  }
}

function settleLost() {
  if (view.lost === null) return;
  const list = view.checkin.phase === "conversation" ? view.checkin.messages : view.base;
  const lastAthlete = [...list].reverse().find((message) => message.role === "user");
  if (!lastAthlete || lastAthlete.content.trim() !== view.lost.trim()) {
    restoreText(view.lost);
    setNote(TEXT.maybeLost);
  }
  view.lost = null;
  view.pending = null;
}

export async function onVisible() {
  if (document.visibilityState !== "visible" || view.inFlight) return;
  await refresh();
  requestRender();
}

export function syncTicker(shown, tick) {
  if (shown && tickTimer === null) tickTimer = window.setInterval(tick, TICK_MS);
  if (!shown && tickTimer !== null) {
    window.clearInterval(tickTimer);
    tickTimer = null;
  }
}

import {
  DAY_FORMAT,
  FINE_POINTER,
  JOB_TEXT,
  LATER_AFTER_MS,
  REDUCED_MOTION,
  TEXT,
  TIME_FORMAT,
} from "./constants.js";
import { renderPlan } from "./plan.js";
import { $, announce, el, isBusy, updateSendButton, view } from "./view.js";

let renderedCount = 0;

export function render() {
  renderHeader();
  renderTabs();
  renderMessages();
  renderWaiting();
  renderMfa();
  renderDock();
  renderPlan();
  applyFocus();
}

const GARMIN_TEXT = { token_cached: TEXT.garminConnected, no_token: TEXT.garminNotConnected };

function renderHeader() {
  const garmin = view.state?.garmin.state ?? "unknown";
  const action = $("garmin-status-action");
  $("garmin-status").dataset.state = garmin;
  $("garmin-status-text").textContent = GARMIN_TEXT[garmin] ?? "";
  $("garmin-status-text").hidden = garmin === "mfa_pending";
  $("garmin-status-text").classList.toggle("visually-hidden", garmin === "no_token");
  action.hidden = garmin !== "no_token" && garmin !== "mfa_pending";
  action.textContent = garmin === "mfa_pending" ? TEXT.garminWaiting : TEXT.garminLogin;
  action.disabled = garmin === "no_token" && view.inFlight;
  $("connection-banner").hidden = !view.offline;
}

export function renderTabs() {
  for (const [tab, name] of [["chat", "tab-chat"], ["plan", "tab-plan"]]) {
    const selected = view.tab === tab;
    $(name).setAttribute("aria-selected", String(selected));
    $(name).tabIndex = selected ? 0 : -1;
    $(`panel-${tab}`).hidden = !selected;
  }
}

function messageItem({ role, content }) {
  if (role === "hint") {
    const hint = el("li", "message message-hint");
    const bubble = el("div", "bubble bubble-hint");
    const mark = el("span", "hint-mark", "!");
    mark.setAttribute("aria-hidden", "true");
    bubble.append(mark, el("span", "", content));
    hint.append(bubble);
    return hint;
  }
  const athlete = role === "user";
  const item = el("li", `message ${athlete ? "message-athlete" : "message-coach"}`);
  item.append(
    el("span", "visually-hidden", athlete ? TEXT.you : TEXT.coach),
    el("div", `bubble ${athlete ? "bubble-athlete" : "bubble-coach"}`, content),
  );
  return item;
}

function systemItem(text, tone) {
  const item = el("li", "message message-system");
  item.dataset.tone = tone;
  item.append(el("p", "", text));
  return item;
}

function dayItem(text, className = "message-day") {
  const item = el("li", className);
  item.append(el("span", "", text));
  return item;
}

function baseItems() {
  const items = [];
  let lastDay = null;
  for (const message of view.base) {
    const day = message.created_at ? DAY_FORMAT.format(new Date(message.created_at)) : null;
    if (day && day !== lastDay) items.push(dayItem(day));
    lastDay = day ?? lastDay;
    items.push(messageItem(message));
  }
  return items;
}

function checkinItems() {
  const checkin = view.checkin;
  const turns = [{ role: "assistant", content: checkin.intro }, ...checkin.thread, ...checkin.messages];
  return [dayItem(TEXT.checkinDivider, "message-day checkin-divider"), ...turns.map(messageItem)];
}

function pendingItem() {
  const item = messageItem({ role: "user", content: view.pending.text });
  if (view.pending.flag) item.append(el("p", "message-flag", view.pending.flag));
  return item;
}

function conversationItems() {
  if (!view.state) return [systemItem(TEXT.loading, "info")];
  const items = view.checkin.phase === "none" ? baseItems() : checkinItems();
  if (view.pending) items.push(pendingItem());
  return items;
}

function messageItems() {
  const items = conversationItems();
  if (view.note) items.push(systemItem(view.note.text, view.note.tone));
  return items;
}

function announceNewCoach() {
  const checkin = view.checkin;
  const turns = checkin.phase === "none" ? view.base : [...checkin.thread, ...checkin.messages];
  const last = [...turns].reverse().find((turn) => turn.role === "assistant");
  const content = last ? last.content : null;
  const isNew = view.lastCoach !== null && content !== null && content !== view.lastCoach;
  if (isNew) announce(`${TEXT.coach} ${content}`);
  if (view.state) view.lastCoach = content ?? "";
}

function renderMessages() {
  const items = messageItems();
  $("message-list").replaceChildren(...items);
  announceNewCoach();
  if ((items.length > renderedCount || view.scrollNext) && view.tab === "chat") {
    const behavior = REDUCED_MOTION.matches ? "auto" : "smooth";
    window.scrollTo({ top: document.documentElement.scrollHeight, behavior });
  }
  renderedCount = items.length;
  view.scrollNext = false;
}

function waitingJob() {
  if (view.ownJob) return view.ownJob;
  const busy = view.state?.busy;
  return busy ? { job: busy.job, startedAt: Date.parse(busy.started_at) } : null;
}

function elapsedText(milliseconds) {
  const minutes = Math.floor(Math.max(0, milliseconds) / 60000);
  if (minutes < 1) return "";
  if (minutes < 60) return ` · ${minutes} min`;
  return ` · ${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

export function isWaiting() {
  return waitingJob() !== null;
}

export function renderWaiting() {
  const running = waitingJob();
  const waiting = $("waiting");
  waiting.hidden = running === null;
  if (running === null) return;
  const elapsed = Date.now() - running.startedAt;
  waiting.querySelector(".waiting-text").textContent = JOB_TEXT[running.job] ?? JOB_TEXT.onboarding_turn;
  waiting.querySelector(".waiting-elapsed").textContent = elapsedText(elapsed);
  waiting.querySelector(".waiting-later").hidden = elapsed < LATER_AFTER_MS;
}

function requestedText(requestedAt) {
  const date = new Date(requestedAt ?? "");
  if (!requestedAt || Number.isNaN(date.getTime())) return "";
  return `${TEXT.mfaRequestedAt}${TIME_FORMAT.format(date)}.`;
}

export function renderMfa() {
  const garmin = view.state?.garmin;
  const panel = $("mfa-panel");
  const requested = requestedText(garmin?.mfa_requested_at);
  panel.hidden = !garmin || (garmin.state !== "mfa_pending" && !view.mfa.open);
  panel.dataset.needsNewCode = String(view.mfa.needsNewCode);
  $("mfa-form").hidden = view.mfa.needsNewCode;
  $("mfa-requested").hidden = requested === "";
  $("mfa-requested").textContent = requested;
  $("mfa-result").textContent = view.mfa.result;
  $("mfa-result").dataset.tone = view.mfa.tone;
  $("mfa-code").disabled = view.inFlight;
  $("mfa-submit").disabled = view.inFlight || $("mfa-code").value.trim() === "";
  $("mfa-new-code").disabled = view.inFlight;
}

const CHECKIN_DOCK = {
  questions: { composer: TEXT.placeholderAnswer, links: ["action-checkin-cancel"], quick: true },
  conversation: { composer: TEXT.placeholderCheckinChat, links: ["action-checkin-end"] },
};

function dockLayout() {
  const phase = view.checkin.phase;
  if (!view.state) return {};
  if (CHECKIN_DOCK[phase]) return CHECKIN_DOCK[phase];
  const byMode = {
    needs_onboarding: { actions: ["action-start"] },
    onboarding: { composer: TEXT.placeholderOnboarding, links: ["action-discard"] },
    week_one_pending: { hint: TEXT.weekOneHint, actions: ["action-week-one"] },
  };
  return byMode[view.state.mode] ?? { actions: ["action-checkin", "action-reopen"] };
}

function showGroup(groupId, ids, shown) {
  $(groupId).hidden = shown.length === 0;
  for (const id of ids) {
    $(id).hidden = !shown.includes(id);
    $(id).disabled = isBusy();
  }
}

const DOCK_LINKS = ["action-discard", "action-checkin-cancel", "action-checkin-end"];
const DOCK_ACTIONS = ["action-start", "action-week-one", "action-checkin", "action-reopen"];

function renderDock() {
  const layout = dockLayout();
  $("dock-hint").hidden = !layout.hint;
  $("dock-hint").textContent = layout.hint ?? "";
  showGroup("dock-links", DOCK_LINKS, layout.links ?? []);
  showGroup("action-row", DOCK_ACTIONS, layout.actions ?? []);
  $("action-discard").className = view.sessionTooLong ? "button-primary" : "button-text";
  $("action-checkin-cancel").disabled = view.inFlight;
  renderComposer(layout.composer);
  renderQuickReplies(layout.quick ? view.checkin.kind : null);
}

function renderComposer(placeholder) {
  const input = $("composer-input");
  const answering = view.checkin.phase === "questions";
  $("composer").hidden = !placeholder;
  input.placeholder = placeholder ?? "";
  const tooLong = view.state?.mode === "onboarding" && view.sessionTooLong && view.checkin.phase === "none";
  input.disabled = isBusy() || tooLong;
  input.maxLength = answering ? 400 : 16000;
  input.inputMode = answering && view.checkin.kind === "scale" ? "numeric" : "text";
  input.enterKeyHint = answering || FINE_POINTER.matches ? "send" : "enter";
  updateSendButton();
}

function quickReplyButton(answer, label) {
  const button = el("button", "quick-reply", label);
  button.type = "button";
  button.dataset.answer = answer;
  button.disabled = isBusy();
  return button;
}

function renderQuickReplies(kind) {
  const box = $("quick-replies");
  const scale = Array.from({ length: 10 }, (_, index) => [String(index + 1), String(index + 1)]);
  const choices = { scale, yes_no: [["s", TEXT.yes], ["n", TEXT.no]] }[kind] ?? [];
  box.hidden = choices.length === 0;
  box.dataset.kind = kind ?? "";
  box.replaceChildren(...choices.map(([answer, label]) => quickReplyButton(answer, label)));
}

function applyFocus() {
  const target = view.focusNext ? $(view.focusNext) : null;
  view.focusNext = null;
  if (target && !target.disabled && target.offsetParent !== null) target.focus();
}

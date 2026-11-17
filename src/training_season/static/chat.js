import { cancelCheckin, endCheckin, sendCheckinAnswer, sendCheckinMessage, startCheckin } from "./checkin.js";
import { FINE_POINTER } from "./constants.js";
import { openMfaPanel, requestGarminLogin, submitMfaCode } from "./mfa.js";
import { confirmDiscard, openSession, sendOnboardingMessage, sendWeekOne } from "./onboarding.js";
import { isWaiting, render, renderMfa, renderTabs, renderWaiting } from "./render.js";
import { onVisible, refresh, schedulePoll, syncTicker } from "./sync.js";
import { $, requestRender, setRenderer, updateSendButton, view } from "./view.js";

function renderPage() {
  render();
  syncTicker(isWaiting(), renderWaiting);
  schedulePoll();
}

function selectTab(tab, focus = false) {
  view.tab = tab;
  history.replaceState(null, "", tab === "plan" ? "#plano" : `${location.pathname}${location.search}`);
  renderTabs();
  if (focus) $(tab === "plan" ? "tab-plan" : "tab-chat").focus();
}

function submitComposer(event) {
  event.preventDefault();
  const input = $("composer-input");
  const text = input.value;
  if (text.trim() === "" || input.disabled) return;
  input.value = "";
  updateSendButton();
  if (view.checkin.phase === "questions") return sendCheckinAnswer(text, text, true);
  if (view.checkin.phase === "conversation") return sendCheckinMessage(text);
  sendOnboardingMessage(text);
}

function onComposerKey(event) {
  if (event.key !== "Enter" || event.isComposing) return;
  const sends = view.checkin.phase === "questions" || (FINE_POINTER.matches && !event.shiftKey);
  if (!sends) return;
  event.preventDefault();
  $("composer").requestSubmit();
}

function submitMfa(event) {
  event.preventDefault();
  const input = $("mfa-code");
  const code = input.value.trim();
  input.value = "";
  if (code !== "") submitMfaCode(code);
}

function onQuickReply(event) {
  const button = event.target.closest("button.quick-reply");
  if (!button || button.disabled) return;
  sendCheckinAnswer(button.dataset.answer, button.textContent, false);
}

function onGarminStatus() {
  selectTab("chat");
  if (view.state?.garmin.state !== "mfa_pending") return requestGarminLogin();
  openMfaPanel("mfa-code");
  requestRender();
}

function openDiscardDialog() {
  $("discard-dialog").returnValue = "";
  $("discard-dialog").showModal();
}

function onDiscardClosed() {
  $("action-discard").focus();
  if ($("discard-dialog").returnValue === "confirm") confirmDiscard();
}

function bindEvents() {
  $("composer").addEventListener("submit", submitComposer);
  $("composer-input").addEventListener("input", updateSendButton);
  $("composer-input").addEventListener("keydown", onComposerKey);
  $("mfa-form").addEventListener("submit", submitMfa);
  $("mfa-code").addEventListener("input", () => renderMfa());
  $("mfa-new-code").addEventListener("click", requestGarminLogin);
  $("garmin-status-action").addEventListener("click", onGarminStatus);
  $("quick-replies").addEventListener("click", onQuickReply);
  $("action-start").addEventListener("click", () => openSession(false));
  $("action-reopen").addEventListener("click", () => openSession(true));
  $("action-week-one").addEventListener("click", sendWeekOne);
  $("action-checkin").addEventListener("click", startCheckin);
  $("action-checkin-cancel").addEventListener("click", cancelCheckin);
  $("action-checkin-end").addEventListener("click", endCheckin);
  $("action-discard").addEventListener("click", openDiscardDialog);
  $("discard-dialog").addEventListener("close", onDiscardClosed);
  bindTabs();
}

function bindTabs() {
  $("tab-chat").addEventListener("click", () => selectTab("chat"));
  $("tab-plan").addEventListener("click", () => selectTab("plan"));
  document.querySelector(".tabs").addEventListener("keydown", (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    selectTab(view.tab === "chat" ? "plan" : "chat", true);
  });
  window.addEventListener("hashchange", () => selectTab(location.hash === "#plano" ? "plan" : "chat"));
  document.addEventListener("visibilitychange", onVisible);
}

// The first render shows "Carregando…" only; polling starts after the first load answers.
async function boot() {
  bindEvents();
  view.tab = location.hash === "#plano" ? "plan" : "chat";
  render();
  setRenderer(renderPage);
  await refresh();
  requestRender();
}

boot();

export const view = {
  state: null,
  base: [],
  checkin: { phase: "none", intro: "", answers: [], kind: null, thread: [], messages: [] },
  plan: null,
  pending: null,
  note: null,
  mfa: { open: false, result: "", tone: "info", needsNewCode: false },
  offline: false,
  tab: "chat",
  inFlight: false,
  ownJob: null,
  lost: null,
  sessionTooLong: false,
  focusNext: null,
  scrollNext: false,
  lastCoach: null,
};

let renderPage = () => {};

// chat.js sets the page renderer at boot, so no module below it imports render.js.
export function setRenderer(renderer) {
  renderPage = renderer;
}

export function requestRender() {
  renderPage();
}

export const $ = (id) => document.getElementById(id);

export function el(tag, className = "", text = null) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== null) node.textContent = text;
  return node;
}

export function announce(text) {
  const announcer = $("announcer");
  announcer.textContent = "";
  window.requestAnimationFrame(() => {
    announcer.textContent = text;
  });
}

export function isBusy() {
  return Boolean(view.inFlight || view.state?.busy);
}

export function setNote(text, tone = "info") {
  view.note = { text, tone };
  announce(text);
}

export function updateSendButton() {
  const input = $("composer-input");
  $("composer-send").disabled = input.disabled || input.value.trim() === "";
}

export function restoreText(text) {
  if (!text) return;
  $("composer-input").value = text;
  updateSendButton();
}

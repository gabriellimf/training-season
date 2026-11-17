import { apiPost } from "./api.js";
import { UNSAVED_ONBOARDING } from "./constants.js";
import { act, focusAfterReply, markUnsaved } from "./flow.js";
import { openMfaPanel } from "./mfa.js";
import { refresh } from "./sync.js";
import { setNote, view } from "./view.js";

export function openSession(reopening) {
  act({
    job: "onboarding_session",
    request: () => apiPost("/api/onboarding/session", { reopening }),
    handle: async () => {
      await refresh();
      focusAfterReply();
    },
  });
}

export function sendOnboardingMessage(text) {
  act({
    job: "onboarding_turn",
    pending: text,
    request: () => apiPost("/api/onboarding/messages", { text }),
    handle: (reply) => applyOnboardingReply(reply, text),
  });
}

async function applyOnboardingReply(reply, text) {
  if (UNSAVED_ONBOARDING.has(reply.status)) {
    view.sessionTooLong = reply.status === "session_too_long";
    return markUnsaved(text, reply.message);
  }
  view.pending = null;
  await refresh();
  focusAfterReply();
  if (reply.status === "completed") await sendWeekOne();
}

export function sendWeekOne() {
  return act({
    job: "week_one",
    request: () => apiPost("/api/week-one"),
    handle: async (outcome) => {
      if (outcome.status === "mfa_required") openMfaPanel("mfa-code");
      await refresh();
    },
  });
}

export function confirmDiscard() {
  act({
    request: () => apiPost("/api/onboarding/discard"),
    handle: async (body) => {
      view.sessionTooLong = false;
      await refresh();
      setNote(body.message);
    },
  });
}

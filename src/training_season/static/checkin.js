import { apiGet, apiPost } from "./api.js";
import { ENDING_CHECKIN, TEXT } from "./constants.js";
import { act, focusAfterReply, markUnsaved } from "./flow.js";
import { refresh } from "./sync.js";
import { requestRender, setNote, view } from "./view.js";

export function startCheckin() {
  act({
    request: async () => ({
      checkin: await apiGet("/api/checkin"),
      step: await apiPost("/api/checkin/answers", { answers: [] }),
    }),
    handle: ({ checkin, step }) => {
      const thread = [{ role: "assistant", content: step.question }];
      const kind = step.answer_kind;
      view.checkin = { phase: "questions", intro: checkin.intro, answers: [], kind, thread, messages: [] };
    },
  });
}

export function sendCheckinAnswer(answer, shown, typed) {
  const answers = [...view.checkin.answers, answer];
  act({
    restore: typed ? answer : null,
    request: () => apiPost("/api/checkin/answers", { answers }),
    handle: (step) => applyCheckinStep(step, answers, shown),
  });
}

async function applyCheckinStep(step, answers, shown) {
  const checkin = view.checkin;
  checkin.thread.push({ role: "user", content: shown });
  checkin.answers = answers.slice(0, step.accepted);
  if (step.status === "hint") checkin.thread.push({ role: "hint", content: step.hint });
  if (step.status !== "saved") {
    checkin.thread.push({ role: "assistant", content: step.question });
    checkin.kind = step.answer_kind;
    return;
  }
  checkin.thread.push({ role: "assistant", content: step.message });
  Object.assign(checkin, { phase: "conversation", kind: null });
  await refresh();
}

export function cancelCheckin() {
  view.checkin.phase = "none";
  view.pending = null;
  setNote(TEXT.checkinCanceled);
  requestRender();
}

export function sendCheckinMessage(text) {
  act({
    job: "checkin_reply",
    pending: text,
    request: () => apiPost("/api/checkin/messages", { text }),
    handle: (reply) => applyCheckinReply(reply, text),
  });
}

async function applyCheckinReply(reply, text) {
  if (reply.status === "message_too_long") return markUnsaved(text, reply.message);
  view.pending = null;
  if (ENDING_CHECKIN.has(reply.status)) view.checkin.phase = "none";
  await refresh();
  if (reply.status !== "replied") setNote(reply.message);
  focusAfterReply();
}

export function endCheckin() {
  act({
    request: () => apiPost("/api/checkin/end"),
    handle: async (body) => {
      view.checkin.phase = "none";
      await refresh();
      setNote(body.message);
    },
  });
}

import { TEXT } from "./constants.js";
import { $, el, view } from "./view.js";

function planEmptyText(weeks) {
  if (!view.state || weeks.length > 0) return null;
  const { mode, reopening } = view.state;
  if (mode === "week_one_pending") return TEXT.planWeekOnePending;
  if (mode === "ready" || (mode === "onboarding" && reopening)) return TEXT.planNoWeek;
  return TEXT.planNone;
}

let lastBuild = [];

// A rebuild closes every open <details>, so the cards are built only when their inputs change.
function planChanged() {
  const build = [view.plan, view.state?.mode, view.state?.reopening];
  const changed = build.some((part, index) => part !== lastBuild[index]);
  lastBuild = build;
  return changed;
}

export function renderPlan() {
  if (!planChanged()) return;
  const weeks = view.plan?.weeks ?? [];
  const empty = planEmptyText(weeks);
  $("plan-empty").hidden = empty === null;
  $("plan-empty").textContent = empty ?? "";
  $("plan-weeks").replaceChildren(...weeks.map((week) => weekCard(week, view.plan.today)));
}

function weekCard(week, today) {
  const card = el("article", "week-card");
  const head = el("header", "week-card-head");
  const heading = el("h3", "week-heading", week.heading);
  heading.id = `week-${week.week_start}`;
  card.setAttribute("aria-labelledby", heading.id);
  const current = week.week_start <= today && today <= week.week_end;
  head.append(
    el("p", "week-tag", current ? TEXT.thisWeek : TEXT.nextWeek),
    heading,
    el("p", "week-volume", week.volume_line),
    el("p", "week-split", week.split_line),
  );
  const hasWorkouts = week.workouts.length > 0;
  card.append(head, hasWorkouts ? workoutList(week.workouts, today) : el("p", "week-empty", TEXT.weekEmpty));
  return card;
}

function dayOf(date, today) {
  if (date < today) return "past";
  return date === today ? "today" : "future";
}

function workoutList(workouts, today) {
  const days = workouts.map((workout) => dayOf(workout.date, today));
  const openIndex = days.includes("today") ? days.indexOf("today") : days.indexOf("future");
  const list = el("ol", "workout-list");
  list.append(...workouts.map((workout, index) => workoutItem(workout, days[index], index === openIndex)));
  return list;
}

function workoutItem(workout, day, open) {
  const item = el("li", "workout");
  Object.assign(item.dataset, { intensity: workout.intensity, day });
  const details = el("details");
  details.open = open;
  const summary = el("summary", "workout-summary");
  summary.append(
    el("span", "workout-day", workout.weekday),
    el("span", "workout-name", workout.name),
    el("span", "intensity-badge", workout.intensity === "hard" ? TEXT.hard : TEXT.easy),
  );
  if (day === "today") summary.append(el("span", "today-tag", TEXT.today));
  details.append(summary, syncLine(workout.scheduled));
  if (workout.description) details.append(el("p", "workout-description", workout.description));
  details.append(stepList(workout.steps, false));
  item.append(details);
  return item;
}

function syncLine(scheduled) {
  const line = el("p", "workout-sync");
  line.dataset.scheduled = String(scheduled);
  if (!scheduled) return Object.assign(line, { textContent: TEXT.notScheduled });
  const mark = el("span", "", "✓ ");
  mark.setAttribute("aria-hidden", "true");
  line.append(mark, TEXT.scheduled);
  return line;
}

function stepList(steps, repeat) {
  const list = el("ol", repeat ? "step-list step-list-repeat" : "step-list");
  for (const step of steps) {
    const item = el("li", "step");
    item.append(el("span", "step-text", step.text));
    if (step.notes) item.append(el("p", "step-notes", step.notes));
    if (step.steps) item.append(stepList(step.steps, true));
    list.append(item);
  }
  return list;
}

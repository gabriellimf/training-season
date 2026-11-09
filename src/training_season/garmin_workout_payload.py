"""Map one validated workout to the typed Garmin Connect running workout."""

from __future__ import annotations

from typing import TYPE_CHECKING

from garminconnect.workout import (
    CadenceTarget,
    ConditionType,
    ExecutableStep,
    HeartRateZoneTarget,
    PaceTarget,
    RunningWorkout,
    SportType,
    StepType,
    WorkoutSegment,
)
from garminconnect.workout import RepeatGroup as GarminRepeatGroup

from training_season.workout_schema import RepeatGroup, count_steps

if TYPE_CHECKING:
    from garminconnect.workout import IntensityTarget

    from training_season.workout_schema import (
        StepDuration,
        TimedStep,
        Workout,
        WorkoutStep,
    )

GarminTypeField = dict[str, int | str | bool]
GarminStep = ExecutableStep | GarminRepeatGroup

_STEP_TYPES: dict[str, GarminTypeField] = {
    "warmup": {
        "stepTypeId": StepType.WARMUP,
        "stepTypeKey": "warmup",
        "displayOrder": 1,
    },
    "cooldown": {
        "stepTypeId": StepType.COOLDOWN,
        "stepTypeKey": "cooldown",
        "displayOrder": 2,
    },
    "interval": {
        "stepTypeId": StepType.INTERVAL,
        "stepTypeKey": "interval",
        "displayOrder": 3,
    },
    "recovery": {
        "stepTypeId": StepType.RECOVERY,
        "stepTypeKey": "recovery",
        "displayOrder": 4,
    },
}
_REPEAT_STEP_TYPE: GarminTypeField = {
    "stepTypeId": StepType.REPEAT,
    "stepTypeKey": "repeat",
    "displayOrder": 6,
}
_TIME_CONDITION: GarminTypeField = {
    "conditionTypeId": ConditionType.TIME,
    "conditionTypeKey": "time",
    "displayOrder": 2,
    "displayable": True,
}
_DISTANCE_CONDITION: GarminTypeField = {
    "conditionTypeId": ConditionType.DISTANCE,
    "conditionTypeKey": "distance",
    "displayOrder": 3,
    "displayable": True,
}
_ITERATIONS_CONDITION: GarminTypeField = {
    "conditionTypeId": ConditionType.ITERATIONS,
    "conditionTypeKey": "iterations",
    "displayOrder": 7,
    "displayable": False,
}
_RUNNING_SPORT: GarminTypeField = {
    "sportTypeId": SportType.RUNNING,
    "sportTypeKey": "running",
    "displayOrder": 1,
}
_PRIMARY_TARGET_ORDER = 1
_SECONDARY_TARGET_ORDER = 2


def running_workout(workout: Workout) -> RunningWorkout:
    """
    Build the Garmin running workout: pace is the primary target of every step.

    This is the only module that knows the Garmin workout shape. The name is
    sent as is, so it must already follow the `DD/MM <token>` rule.

    Used as `client.upload_running_workout(running_workout(workout))`
    """
    segment = WorkoutSegment(
        segmentOrder=1,
        sportType=_RUNNING_SPORT,
        workoutSteps=_garmin_steps(workout.steps),
    )
    return RunningWorkout(
        workoutName=workout.name,
        description=workout.description,
        estimatedDurationInSecs=round(sum(map(_estimated_seconds, workout.steps))),
        workoutSegments=[segment],
    )


def _garmin_steps(steps: list[WorkoutStep]) -> list[GarminStep]:
    garmin_steps: list[GarminStep] = []
    next_order = 1
    for step in steps:
        garmin_steps.append(_garmin_step(step, next_order))
        next_order += count_steps([step])
    return garmin_steps


def _garmin_step(step: WorkoutStep, step_order: int) -> GarminStep:
    if isinstance(step, RepeatGroup):
        return _garmin_repeat(step, step_order)
    return _executable_step(step, step_order)


def _garmin_repeat(group: RepeatGroup, step_order: int) -> GarminRepeatGroup:
    children: list[GarminStep] = [
        _executable_step(child, step_order + offset)
        for offset, child in enumerate(group.steps, start=1)
    ]
    return GarminRepeatGroup(
        stepOrder=step_order,
        stepType=_REPEAT_STEP_TYPE,
        numberOfIterations=group.count,
        workoutSteps=children,
        endCondition=_ITERATIONS_CONDITION,
        endConditionValue=float(group.count),
    )


def _executable_step(step: TimedStep, step_order: int) -> ExecutableStep:
    pace = PaceTarget(lower_limit=step.pace_mps.min, upper_limit=step.pace_mps.max)
    end_condition, end_value = _end_condition(step.duration)
    executable = ExecutableStep(
        stepOrder=step_order,
        stepType=_STEP_TYPES[step.type],
        endCondition=end_condition,
        endConditionValue=end_value,
        targetType=_target_type(pace, _PRIMARY_TARGET_ORDER),
        targetValueOne=pace.lower_limit,
        targetValueTwo=pace.upper_limit,
        description=_step_description(step),
    )
    return _with_secondary_target(executable, _secondary_target(step))


def _end_condition(duration: StepDuration) -> tuple[GarminTypeField, float]:
    if duration.seconds is not None:
        return _TIME_CONDITION, float(duration.seconds)
    if duration.meters is not None:
        return _DISTANCE_CONDITION, float(duration.meters)
    raise ValueError(
        f"step duration has neither meters nor seconds, got {duration!r}, "
        "expected exactly one of them"
    )


def _target_type(target: IntensityTarget, display_order: int) -> GarminTypeField:
    return {
        "workoutTargetTypeId": target.target_type,
        "workoutTargetTypeKey": target.target_type_key,
        "displayOrder": display_order,
    }


def _secondary_target(step: TimedStep) -> IntensityTarget | None:
    if step.heart_rate_zone is not None:
        return HeartRateZoneTarget(zone_number=step.heart_rate_zone)
    if step.cadence_spm is not None:
        return CadenceTarget(
            lower_limit=step.cadence_spm.min, upper_limit=step.cadence_spm.max
        )
    return None


def _with_secondary_target(
    executable: ExecutableStep, target: IntensityTarget | None
) -> ExecutableStep:
    if target is None:
        return executable
    return executable.model_copy(
        update={
            "secondaryTargetType": _target_type(target, _SECONDARY_TARGET_ORDER),
            "secondaryTargetValueOne": target.lower_limit,
            "secondaryTargetValueTwo": target.upper_limit,
            "secondaryZoneNumber": target.zone_number,
        }
    )


def _step_description(step: TimedStep) -> str | None:
    lines = [line for line in (step.notes, _cadence_note(step)) if line]
    return "\n".join(lines) or None


def _cadence_note(step: TimedStep) -> str | None:
    if step.heart_rate_zone is None or step.cadence_spm is None:
        return None
    return f"Cadência {step.cadence_spm.min}-{step.cadence_spm.max} spm"


def _estimated_seconds(step: WorkoutStep) -> float:
    if isinstance(step, RepeatGroup):
        return step.count * sum(map(_timed_step_seconds, step.steps))
    return _timed_step_seconds(step)


def _timed_step_seconds(step: TimedStep) -> float:
    _, end_value = _end_condition(step.duration)
    if step.duration.seconds is not None:
        return end_value
    return end_value / ((step.pace_mps.min + step.pace_mps.max) / 2)

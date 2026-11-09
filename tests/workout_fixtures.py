from __future__ import annotations

import json

JsonObject = dict[str, object]


def easy_step(step_type: str = "warmup") -> JsonObject:
    return {
        "type": step_type,
        "duration": {"meters": 1000},
        "pace_mps": {"min": 2.6, "max": 2.9},
    }


def repeat_group(count: int, steps: list[JsonObject]) -> JsonObject:
    return {"type": "repeat", "count": count, "steps": steps}


def workout_with_steps(
    steps: list[JsonObject], name: str = "26/10 10x400"
) -> JsonObject:
    return {"name": name, "steps": steps}


def example_workout() -> JsonObject:
    return {
        "name": "26/10 10x400",
        "description": "Tiros de 400 m com trote de recuperação.",
        "steps": [
            {
                "type": "warmup",
                "duration": {"meters": 2000},
                "pace_mps": {"min": 2.6, "max": 2.9},
                "notes": "Trote leve",
            },
            repeat_group(
                10,
                [
                    {
                        "type": "interval",
                        "duration": {"meters": 400},
                        "pace_mps": {"min": 4.0, "max": 4.3},
                        "cadence_spm": {"min": 175, "max": 185},
                    },
                    {
                        "type": "recovery",
                        "duration": {"seconds": 90},
                        "pace_mps": {"min": 2.2, "max": 2.8},
                        "heart_rate_zone": 2,
                    },
                ],
            ),
            {
                "type": "cooldown",
                "duration": {"seconds": 600},
                "pace_mps": {"min": 2.6, "max": 2.9},
            },
        ],
    }


def example_workout_text() -> str:
    return json.dumps(example_workout())

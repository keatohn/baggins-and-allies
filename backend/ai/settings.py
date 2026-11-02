"""
Admin defaults for the computer player, and the values stamped onto each game.

Difficulty and algorithm live on the game state (and the config snapshot) so a
finished game can be scored later without guessing which knobs were on. The
admin page edits the default used for new games. A set AI_ALGORITHM or
AI_MCTS_DIFFICULTY env var still wins, so self-play sweeps stay explicit.
"""

from __future__ import annotations

import json
from typing import Any

AI_DEFAULTS_KEY = "ai_defaults"
AI_SIM_RUNS_KEY = "ai_sim_runs"
DIFFICULTIES = ("easy", "normal", "hard")
ALGORITHMS = ("heuristic", "mcts")
MATCHUPS = (
    "mcts-vs-mcts",
    "mcts-vs-heuristic",
    "heuristic-vs-mcts",
    "heuristic-vs-heuristic",
)
MAX_STORED_SIMS = 20


def normalize_difficulty(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    return text if text in DIFFICULTIES else "normal"


def normalize_algorithm(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    return text if text in ALGORITHMS else "heuristic"


def normalize_matchup(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    return text if text in MATCHUPS else "mcts-vs-mcts"


def default_ai_settings() -> dict[str, str]:
    return {"difficulty": "normal", "algorithm": "heuristic"}


def load_ai_defaults(db) -> dict[str, str]:
    """Defaults for new games. Missing or broken storage falls back to normal/heuristic."""
    if db is None:
        return default_ai_settings()
    from backend.api.models import AppSetting

    row = db.query(AppSetting).filter(AppSetting.key == AI_DEFAULTS_KEY).first()
    if row is None or not row.value_json:
        return default_ai_settings()
    try:
        data = json.loads(row.value_json)
    except json.JSONDecodeError:
        return default_ai_settings()
    if not isinstance(data, dict):
        return default_ai_settings()
    return {
        "difficulty": normalize_difficulty(data.get("difficulty")),
        "algorithm": normalize_algorithm(data.get("algorithm")),
    }


def save_ai_defaults(db, raw: Any) -> dict[str, str]:
    from backend.api.models import AppSetting

    body = raw if isinstance(raw, dict) else {}
    settings = {
        "difficulty": normalize_difficulty(body.get("difficulty")),
        "algorithm": normalize_algorithm(body.get("algorithm")),
    }
    payload = json.dumps(settings)
    row = db.query(AppSetting).filter(AppSetting.key == AI_DEFAULTS_KEY).first()
    if row is None:
        db.add(AppSetting(key=AI_DEFAULTS_KEY, value_json=payload))
    else:
        row.value_json = payload
    db.commit()
    return settings


def load_sim_runs(db) -> list[dict[str, Any]]:
    if db is None:
        return []
    from backend.api.models import AppSetting

    row = db.query(AppSetting).filter(AppSetting.key == AI_SIM_RUNS_KEY).first()
    if row is None or not row.value_json:
        return []
    try:
        data = json.loads(row.value_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def save_sim_run(db, record: dict[str, Any]) -> list[dict[str, Any]]:
    from backend.api.models import AppSetting

    runs = [record, *load_sim_runs(db)][:MAX_STORED_SIMS]
    payload = json.dumps(runs)
    row = db.query(AppSetting).filter(AppSetting.key == AI_SIM_RUNS_KEY).first()
    if row is None:
        db.add(AppSetting(key=AI_SIM_RUNS_KEY, value_json=payload))
    else:
        row.value_json = payload
    db.commit()
    return runs


def stamp_ai_on_state(state, difficulty: str, algorithm: str) -> None:
    state.ai_difficulty = normalize_difficulty(difficulty)
    state.ai_algorithm = normalize_algorithm(algorithm)

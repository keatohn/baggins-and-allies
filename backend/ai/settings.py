"""
Admin defaults for the computer player, and the values stamped onto each game.

Difficulty and algorithm live on the game state (and the config snapshot) so a
finished game can be scored later without guessing which knobs were on. The
admin page edits the default used for new games. A set AI_ALGORITHM or
AI_MCTS_DIFFICULTY env var still wins, so self-play sweeps stay explicit.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from typing import Any

AI_DEFAULTS_KEY = "ai_defaults"
AI_SIM_RUNS_KEY = "ai_sim_runs"
DIFFICULTIES = ("easy", "normal", "hard")
ALGORITHMS = ("heuristic", "mcts")
MAX_LEVELS = 8
_LEVEL_ID = re.compile(r"^[a-z0-9_]{1,32}$")
MATCHUPS = (
    "mcts-vs-mcts",
    "mcts-vs-heuristic",
    "heuristic-vs-mcts",
    "heuristic-vs-heuristic",
)
MAX_STORED_SIMS = 20


class AiSettingsError(ValueError):
    """The admin AI form sent a level list that cannot be stored."""


def default_levels() -> list[dict[str, Any]]:
    """Built-in levels, easiest first. These are the caps before an admin edits them."""
    from backend.ai.mcts.budget import BUDGETS

    labels = (("easy", "Easy"), ("normal", "Normal"), ("hard", "Hard"))
    style = default_style()
    options = _branch_options({}, "")
    return [
        {
            "id": key,
            "name": label,
            "iterations": BUDGETS[key].max_iterations,
            "seconds": BUDGETS[key].max_time_seconds,
            **style,
            **options,
        }
        for key, label in labels
    ]


def _branch_options(item: dict[str, Any], name: str) -> dict[str, int]:
    """Per-phase option caps. Missing keys keep the built-in default. Values are clamped to the ceiling."""
    from backend.ai.mcts.budget import BRANCH_FIELDS, BRANCH_LIMITS

    out: dict[str, int] = {}
    for phase, field in BRANCH_FIELDS.items():
        default, ceiling = BRANCH_LIMITS[phase]
        raw = item.get(field) if isinstance(item, dict) else None
        if raw is None:
            out[field] = default
            continue
        try:
            count = int(raw)
        except (TypeError, ValueError):
            raise AiSettingsError(f"{name}: {field.replace('_', ' ')} must be a whole number") from None
        if count < 1 or count > ceiling:
            label = field.replace("_", " ")
            raise AiSettingsError(f"{name}: {label} must be between 1 and {ceiling}")
        out[field] = count
    return out


def stored_difficulty(raw: Any, default: str = "normal") -> str:
    """Keep a saved level id even if that level was later renamed or removed."""
    text = str(raw or "").strip().lower()
    return text if _LEVEL_ID.fullmatch(text) else default


def normalize_difficulty(raw: Any, levels: list[dict[str, Any]] | None = None) -> str:
    """Map a choice onto a current level id. Unknown names fall back to normal, or the easiest level."""
    catalog = default_levels() if levels is None else levels
    ids = [str(level["id"]) for level in catalog if isinstance(level, dict) and level.get("id")]
    text = str(raw or "").strip().lower()
    if text in ids:
        return text
    if "normal" in ids:
        return "normal"
    return ids[0] if ids else "normal"


def _unique_level_id(name: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")[:24] or "level"
    candidate = base
    number = 2
    while candidate in used or not _LEVEL_ID.fullmatch(candidate):
        suffix = f"_{number}"
        number += 1
        candidate = f"{base[: 32 - len(suffix)]}{suffix}"
    return candidate


def normalize_levels(raw: Any, style_fallback: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Validate the ordered level list. Index 0 is the easiest. Ids stay put across renames.

    Randomness, aggression, and purchase variety live on each level. A level
    that omits them keeps `style_fallback` (older saves stored one shared set).
    """
    if not isinstance(raw, list) or not raw:
        raise AiSettingsError("Add at least one difficulty level")
    if len(raw) > MAX_LEVELS:
        raise AiSettingsError(f"At most {MAX_LEVELS} difficulty levels")
    out: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    used_names: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise AiSettingsError(f"Level {index + 1} is not valid")
        name = str(item.get("name") or "").strip()
        if not name or len(name) > 24:
            raise AiSettingsError("Each level needs a name of 1–24 characters")
        name_key = name.casefold()
        if name_key in used_names:
            raise AiSettingsError(f"Two levels are both named {name}")
        used_names.add(name_key)
        incoming_id = str(item.get("id") or "").strip().lower()
        if incoming_id and _LEVEL_ID.fullmatch(incoming_id) and incoming_id not in used_ids:
            level_id = incoming_id
        else:
            level_id = _unique_level_id(name, used_ids)
        used_ids.add(level_id)
        try:
            iterations = int(item.get("iterations"))
        except (TypeError, ValueError):
            raise AiSettingsError(f"{name}: step count must be a whole number") from None
        try:
            seconds = float(item.get("seconds"))
        except (TypeError, ValueError):
            raise AiSettingsError(f"{name}: time cap must be a number") from None
        if iterations < 1 or iterations > 20000:
            raise AiSettingsError(f"{name}: step count must be between 1 and 20000")
        if seconds < 0.05 or seconds > 30 or seconds != seconds:
            raise AiSettingsError(f"{name}: time cap must be between 0.05 and 30 seconds")
        style_body = {
            name_key: item[name_key]
            for name_key, _default in STYLE_FIELDS
            if item.get(name_key) is not None
        }
        style = normalize_style({**normalize_style(style_fallback), **style_body})
        out.append({
            "id": level_id,
            "name": name,
            "iterations": iterations,
            "seconds": round(seconds, 3),
            **style,
            **_branch_options(item, name),
        })
    return out


def style_for_difficulty(levels: list[dict[str, Any]] | None, difficulty: str) -> dict[str, float]:
    """Style knobs for one difficulty level. Unknown ids use the built-in defaults."""
    wanted = stored_difficulty(difficulty)
    for level in levels or []:
        if isinstance(level, dict) and str(level.get("id") or "") == wanted:
            return normalize_style(level)
    return default_style()


def normalize_algorithm(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    return text if text in ALGORITHMS else "heuristic"


def normalize_matchup(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    return text if text in MATCHUPS else "mcts-vs-mcts"


# name, default, used by the Difficulty tab. All three are 0–1.
STYLE_FIELDS: tuple[tuple[str, float], ...] = (
    ("randomness", 0.0),
    ("aggression", 0.5),
    ("purchase_variety", 0.0),
)

STYLE_ENV: dict[str, str] = {
    "randomness": "AI_RANDOMNESS",
    "aggression": "AI_AGGRESSION",
    "purchase_variety": "AI_PURCHASE_VARIETY",
}


def default_style() -> dict[str, float]:
    return {name: default for name, default in STYLE_FIELDS}


def normalize_style(raw: Any) -> dict[str, float]:
    body = raw if isinstance(raw, dict) else {}
    out: dict[str, float] = {}
    for name, default in STYLE_FIELDS:
        try:
            value = float(body.get(name, default))
        except (TypeError, ValueError):
            value = default
        if value != value or value < 0.0 or value > 1.0:
            value = default
        out[name] = round(value, 3)
    return out


def default_ai_settings() -> dict[str, Any]:
    levels = default_levels()
    return {
        "difficulty": "normal",
        "algorithm": "heuristic",
        "levels": levels,
        **style_for_difficulty(levels, "normal"),
    }


def load_ai_defaults(db) -> dict[str, Any]:
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
    shared = normalize_style(data)
    if isinstance(data.get("levels"), list):
        try:
            levels = normalize_levels(data.get("levels"), shared)
        except AiSettingsError:
            levels = default_levels()
    else:
        levels = default_levels()
    difficulty = normalize_difficulty(data.get("difficulty"), levels)
    return {
        "difficulty": difficulty,
        "algorithm": normalize_algorithm(data.get("algorithm")),
        "levels": levels,
        **style_for_difficulty(levels, difficulty),
    }


def save_ai_defaults(db, raw: Any) -> dict[str, Any]:
    from backend.api.models import AppSetting

    body = raw if isinstance(raw, dict) else {}
    current = load_ai_defaults(db)
    shared = normalize_style(body if any(name in body for name, _default in STYLE_FIELDS) else current)
    if isinstance(body.get("levels"), list):
        levels = normalize_levels(body.get("levels"), shared)
    else:
        levels = current["levels"]
    difficulty = normalize_difficulty(body.get("difficulty"), levels)
    settings = {
        "difficulty": difficulty,
        "algorithm": normalize_algorithm(body.get("algorithm")),
        "levels": levels,
        **style_for_difficulty(levels, difficulty),
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


def stamp_ai_on_state(state, difficulty: str, algorithm: str, levels: list[dict[str, Any]] | None = None) -> None:
    state.ai_difficulty = normalize_difficulty(difficulty, levels)
    state.ai_algorithm = normalize_algorithm(algorithm)


# Short name, default, minimum, maximum (None = no maximum).
# These are the MCTS board-score knobs. The heuristic player does not read them.
WEIGHT_FIELDS: tuple[tuple[str, float, float, float | None], ...] = (
    ("units", 1.0, 0.0, None),
    ("stronghold", 3.0, 0.0, None),
    ("capital", 3.0, 0.0, None),
    ("power_prod", 0.5, 0.0, None),
    ("board_scale", 1.0, 0.0, None),
    ("purchase_attack", 0.5, 0.0, 1.0),
)

WEIGHT_ENV: dict[str, str] = {
    "units": "AI_MCTS_W_UNITS",
    "capital": "AI_MCTS_W_CAPITAL",
    "stronghold": "AI_MCTS_W_STRONGHOLD",
    "power_prod": "AI_MCTS_W_POWER_PROD",
    "board_scale": "AI_MCTS_W_BOARD_SCALE",
    "purchase_attack": "AI_MCTS_W_PURCHASE_ATTACK",
}


def default_weights() -> dict[str, float]:
    return {name: default for name, default, _lo, _hi in WEIGHT_FIELDS}


def _clamp_weight(raw: Any, default: float, lo: float, hi: float | None) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value != value or value in (float("inf"), float("-inf")):
        return default
    if value < lo:
        return lo
    if hi is not None and value > hi:
        return hi
    return value


def normalize_faction_ai(
    raw: Any,
    fallback_difficulty: str = "normal",
    levels: list[dict[str, Any]] | None = None,
    fallback_algorithm: str = "mcts",
    style_fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One faction's algorithm, MCTS weights, and the style of its difficulty.

    Board-score weights are read only by MCTS. Randomness, aggression, and
    purchase variety are read by both algorithms, from that faction's difficulty
    level. With no level catalog, missing style fields fall back to
    `style_fallback`, then to the built-in defaults. A faction body can override
    those only when no catalog is passed.
    """
    body = raw if isinstance(raw, dict) else {}
    incoming = body.get("weights") if isinstance(body.get("weights"), dict) else {}
    if incoming.get("units") is None and incoming.get("material") is not None:
        incoming = {**incoming, "units": incoming.get("material")}
    if incoming.get("purchase_attack") is None and incoming.get("purchase_defense") is not None:
        old = _clamp_weight(incoming.get("purchase_defense"), 0.5, 0.0, 1.0)
        incoming = {**incoming, "purchase_attack": 1.0 - old}
    weights = {
        name: _clamp_weight(incoming.get(name), default, lo, hi)
        for name, default, lo, hi in WEIGHT_FIELDS
    }
    raw_algorithm = body.get("algorithm")
    if isinstance(raw_algorithm, str) and raw_algorithm.strip():
        algorithm = normalize_algorithm(raw_algorithm)
    else:
        algorithm = normalize_algorithm(fallback_algorithm)
    raw_difficulty = body.get("difficulty") or fallback_difficulty
    difficulty = (
        normalize_difficulty(raw_difficulty, levels)
        if levels is not None
        else stored_difficulty(raw_difficulty)
    )
    if levels is not None:
        style = style_for_difficulty(levels, difficulty)
    else:
        base_style = normalize_style(style_fallback)
        style_body = {
            name: body[name]
            for name, _default in STYLE_FIELDS
            if body.get(name) is not None
        }
        style = normalize_style({**base_style, **style_body})
    return {
        "difficulty": difficulty,
        "algorithm": algorithm,
        "weights": weights,
        **style,
    }


def faction_settings_for(
    faction_ids: list[str],
    difficulty: str,
    algorithm: str = "mcts",
    levels: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Same starting knobs for every faction. The lobby edits them one faction at a time."""
    return {
        fid: normalize_faction_ai(
            {"difficulty": difficulty, "algorithm": algorithm},
            difficulty,
            levels,
            algorithm,
        )
        for fid in faction_ids
        if fid
    }


def normalize_faction_settings_map(
    raw: Any,
    faction_ids: list[str],
    difficulty: str,
    levels: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Keep only factions in this game, and fill any missing faction with the defaults."""
    incoming = raw if isinstance(raw, dict) else {}
    out: dict[str, dict[str, Any]] = {}
    for fid in faction_ids:
        if not fid:
            continue
        out[fid] = normalize_faction_ai(incoming.get(fid), difficulty, levels)
    return out


@contextmanager
def style_env(settings: dict[str, Any] | None):
    """Apply the admin randomness, aggression, and purchase-variety knobs for one decide()."""
    style = normalize_style(settings)
    saved = {env_name: os.environ.get(env_name) for env_name in STYLE_ENV.values()}
    for name, env_name in STYLE_ENV.items():
        os.environ[env_name] = str(style[name])
    try:
        yield
    finally:
        for env_name, previous in saved.items():
            if previous is None:
                os.environ.pop(env_name, None)
            else:
                os.environ[env_name] = previous


@contextmanager
def faction_search_env(setting: dict[str, Any] | None):
    """Apply one faction's difficulty and weights for a single decide() call.

    MCTS reads these from the environment when a search starts. The previous
    values are restored so a sweep or another game is not left with this
    faction's knobs. Callers serialize this around decide().
    """
    if not setting:
        yield
        return
    normalized = normalize_faction_ai(setting)
    keys = ["AI_MCTS_DIFFICULTY", "AI_ALGORITHM", *WEIGHT_ENV.values()]
    saved = {key: os.environ.get(key) for key in keys}
    os.environ["AI_MCTS_DIFFICULTY"] = normalized["difficulty"]
    os.environ["AI_ALGORITHM"] = normalized["algorithm"]
    for short, env_name in WEIGHT_ENV.items():
        os.environ[env_name] = str(normalized["weights"][short])
    try:
        yield
    finally:
        for key, previous in saved.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous

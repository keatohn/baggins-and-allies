"""
FastAPI backend for Baggins & Allies.
Provides REST API endpoints for game state management and actions.
"""

import json
import os
import random
import secrets
import string
import uuid
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .database import get_db, get_db_file_path, init_db, SessionLocal
from .models import Game as GameModel, Player
from .auth import (
    create_access_token,
    get_current_player,
    get_current_admin,
    get_current_player_optional,
    hash_password,
    validate_username,
    verify_password,
)

from backend.engine.state import GameState, PendingMove
from backend.engine.actions import (
    Action,
    purchase_units,
    purchase_camp,
    repair_stronghold,
    place_camp,
    queue_camp_placement,
    cancel_camp_placement,
    move_units,
    cancel_move,
    cancel_mobilization,
    initiate_combat,
    continue_combat,
    retreat,
    set_territory_defender_casualty_order,
    mobilize_units,
    end_phase,
    end_turn,
    skip_turn,
)
from backend.engine.reducer import apply_action, get_state_after_pending_moves
from backend.engine.combat import (
    _is_naval_unit as combat_is_naval_unit,
    get_attacker_effective_dice_and_bombikazi_self_destruct,
    get_bombikazi_pairing,
    get_defender_hit_flat_indices,
    get_eff_def_per_flat_index,
    get_terror_reroll_targets,
    get_siegework_dice_counts,
    get_siegework_attacker_rolling_units,
    get_siegework_round_attacker_display_units,
    get_siegework_round_defender_display_units,
    group_dice_by_stat,
    sort_attackers_for_ladder_dice_order,
    _is_siegework_unit as combat_is_siegework_unit,
    compute_terrain_stat_modifiers,
    compute_anti_cavalry_stat_modifiers,
    compute_captain_stat_modifiers,
    compute_sea_raider_stat_modifiers,
    merge_stat_modifiers,
    _count_hits as combat_count_hits,
    _has_special as combat_has_special,
)
from backend.config import DEFAULT_SETUP_ID
from backend.engine.definitions import (
    load_static_definitions,
    load_starting_setup,
    definitions_from_snapshot,
    TerritoryDefinition,
    parse_prefire_penalty_from_manifest,
)
from backend.setup_data import (
    create_setup,
    get_admin_setup_bundle,
    list_all_setups_admin,
    save_setup_bundle,
    try_list_setups_menu,
    try_load_setup,
    try_load_specials,
    try_load_static_definitions,
    try_scenario_display,
)
from backend.setup_validation import validate_setup_payload
from dataclasses import asdict
from backend.engine.queries import (
    validate_action,
    get_purchasable_units,
    get_movable_units,
    get_unit_move_targets,
    get_aerial_units_must_move,
    get_mobilization_territories,
    get_mobilization_sea_zones,
    get_mobilization_capacity,
    get_contested_territories,
    get_sea_raid_targets,
    get_retreat_options,
    get_purchased_units,
    get_faction_stats,
)
from backend.engine.utils import (
    initialize_game_state,
    generate_dice_rolls_for_units,
    get_unit_faction,
    has_unit_special,
    backfill_liberation_metadata,
    is_aerial_unit,
)

# Siegework units only roll in the dedicated siegeworks round, not in standard combat.
NORMAL_COMBAT_EXCLUDE_ARCHETYPES = frozenset({"siegework"})


def _combat_territory_stronghold_hp(territory, tdef) -> int | None:
    """Current stronghold HP for ram/siegework eligibility; None if not a stronghold territory."""
    if not tdef or not getattr(tdef, "is_stronghold", False):
        return None
    base = getattr(tdef, "stronghold_base_health", 0) or 0
    if base <= 0:
        return None
    cur = getattr(territory, "stronghold_current_health", None) if territory else None
    return int(cur) if cur is not None else base
from backend.engine.movement import (
    _is_sea_zone,
    get_forced_naval_combat_instance_ids,
    resolve_territory_key_in_state,
)
from backend.engine.queries import _is_naval_unit, get_valid_offload_sea_zones, participates_in_sea_hex_naval_combat
from backend.engine.combat_sim import run_simulation, SimOptions
from backend.engine.combat_specials import (
    compute_battle_specials_and_modifiers,
    stacks_to_synthetic_units,
    BattleSpecialsResult,
    ram_special_applicable_for_active_combat,
    stealth_prefire_applicable_for_active_combat,
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init DB, seed setups if empty, sync module default defs from DB when present."""
    init_db()
    from backend.setup_data import db_has_any_setup

    db = SessionLocal()
    try:
        global unit_defs, territory_defs, faction_defs, camp_defs, port_defs, starting_setup
        if db_has_any_setup(db):
            try:
                unit_defs, territory_defs, faction_defs, camp_defs, port_defs = try_load_static_definitions(
                    DEFAULT_SETUP_ID, db
                )
                su = try_load_setup(DEFAULT_SETUP_ID, db)
                if su:
                    starting_setup = su["starting_setup"]
            except FileNotFoundError:
                pass
    finally:
        db.close()
    yield


app = FastAPI(
    title="Baggins & Allies API",
    description="Backend API for Baggins & Allies - a turn-based strategy game",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS configuration for frontend (add production origins via CORS_ORIGINS env, comma-separated).
# Also accept CORS_ORIGIN (singular) if CORS_ORIGINS is unset — common dashboard typo.
_default_origins = ["http://localhost:5173", "http://localhost:5174", "http://localhost:3000"]
_extra = os.environ.get("CORS_ORIGINS", "") or os.environ.get("CORS_ORIGIN", "")
if _extra:
    _default_origins = [o.strip() for o in _extra.split(",") if o.strip()] + _default_origins
CORS_ORIGINS = _default_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


@app.middleware("http")
async def log_requests(request, call_next):
    """Log method and path so 500s can be traced to the failing endpoint."""
    method = getattr(request, "method", "?")
    url = getattr(request, "url", None)
    path = url.path if url else "?"
    try:
        response = await call_next(request)
        if response.status_code >= 500:
            print(f"[500] {method} {path}", flush=True)
        return response
    except Exception:
        print(f"[500] {method} {path} (exception)", flush=True)
        raise


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    """Return 500 with CORS headers and full traceback so the frontend can read the error."""
    import traceback
    tb = traceback.format_exc()
    traceback.print_exc()
    origin = request.headers.get("origin")
    allow_origin = origin if origin in CORS_ORIGINS else CORS_ORIGINS[0]
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc), "traceback": tb},
        headers={
            "Access-Control-Allow-Origin": allow_origin,
            "Access-Control-Allow-Credentials": "true",
        },
    )

# Fallback definitions for games without config snapshot (e.g. legacy). New games load by setup_id.
unit_defs, territory_defs, faction_defs, camp_defs, port_defs = load_static_definitions(setup_id=DEFAULT_SETUP_ID)
starting_setup = load_starting_setup(setup_id=DEFAULT_SETUP_ID)

# In-memory cache of loaded game state (also persisted in DB)
games: dict[str, GameState] = {}

# Per-game definitions (from config snapshot); key = game_id, value = (unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
game_defs: dict[str, tuple] = {}

# Alphanumeric for game codes (uppercase + digits)
GAME_CODE_CHARS = string.ascii_uppercase + string.digits
GAME_CODE_LENGTH = 4


# ===== Pydantic Models =====

class RegisterRequest(BaseModel):
    email: str
    username: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class AudioSettingsPatch(BaseModel):
    menu_music_volume: float | None = Field(None, ge=0.0, le=1.0)
    game_music_volume: float | None = Field(None, ge=0.0, le=1.0)
    music_volume: float | None = Field(None, ge=0.0, le=1.0)
    sfx_volume: float | None = Field(None, ge=0.0, le=1.0)
    master_volume: float | None = Field(None, ge=0.0, le=1.0)
    """Legacy: same as setting music_volume."""
    muted: bool | None = None


class PatchProfileRequest(BaseModel):
    username: str | None = None
    audio: AudioSettingsPatch | None = None


class CreateGameRequest(BaseModel):
    name: str
    is_multiplayer: bool = False
    """Setup id from GET /setups (e.g. '0.0', '0.1'). Omitted = default from backend.config.DEFAULT_SETUP_ID."""
    setup_id: str | None = None
    """Deprecated: map base name. If setup_id is set, map_asset is ignored and derived from setup."""
    map_asset: str | None = None
    """Faction IDs controlled by AI (single-player or fill slots). Omitted = no AI factions."""
    ai_factions: list[str] | None = None


class JoinGameRequest(BaseModel):
    game_code: str


class ClaimFactionRequest(BaseModel):
    faction_id: str
    claim: bool  # True to claim, False to unclaim


class NewGameRequest(BaseModel):
    game_id: str
    map_asset: str | None = None


class PurchaseRequest(BaseModel):
    game_id: str
    purchases: dict[str, int]  # unit_id -> count


class RepairStrongholdRequest(BaseModel):
    game_id: str
    repairs: list[dict]  # [{"territory_id": str, "hp_to_add": int}, ...]


class MoveRequest(BaseModel):
    game_id: str
    from_territory: str
    to_territory: str
    unit_instance_ids: list[str]
    charge_through: list[str] | None = None  # Cavalry: empty enemy territory IDs to conquer (order)
    load_onto_boat_instance_id: str | None = None  # Load: assign passengers only to this boat in the destination sea zone
    offload_sea_zone_id: str | None = None  # Sea->land: when multiple sea zones can offload to this land, client sends which one to sail to
    avoid_forced_naval_combat: bool | None = None  # Combat move: sail away from mobilization standoff instead of fighting


class CombatRequest(BaseModel):
    game_id: str
    territory_id: str
    sea_zone_id: str | None = None  # For sea raid: attackers are in this sea zone, target is territory_id (land)
    # When False, bomb carriers skip the siegeworks round (no detonation); pairing survives until standard combat
    fuse_bomb: bool = True


class ContinueCombatRequest(BaseModel):
    game_id: str
    casualty_order: str | None = None  # "best_unit" | "best_attack" for this round
    must_conquer: bool | None = None


class RetreatRequest(BaseModel):
    game_id: str
    retreat_to: str


class MobilizeRequest(BaseModel):
    game_id: str
    destination: str
    units: list[dict]  # [{"unit_id": str, "count": int}]


class EndPhaseRequest(BaseModel):
    game_id: str


class CancelMoveRequest(BaseModel):
    game_id: str
    move_index: int


class CancelMobilizationRequest(BaseModel):
    game_id: str
    mobilization_index: int


class PlaceCampRequest(BaseModel):
    game_id: str
    camp_index: int
    territory_id: str


class CancelCampPlacementRequest(BaseModel):
    game_id: str
    placement_index: int


class SetTerritoryDefenderCasualtyOrderRequest(BaseModel):
    game_id: str
    territory_id: str
    casualty_order: str  # "best_unit" | "best_defense"


class SimulateCombatOptionsRequest(BaseModel):
    """Optional combat sim options. All fields optional."""
    casualty_order_attacker: str | None = None  # "best_unit" | "best_attack"
    casualty_order_defender: str | None = None  # "best_unit" | "best_defense"
    must_conquer: bool | None = None
    max_rounds: int | None = None
    is_sea_raid: bool | None = None  # land combat: Sea Raider special +attack; not naval combat
    retreat_when_attacker_units_le: int | None = None  # retreat when attacker count <= this after a round
    stronghold_initial_hp: int | None = None  # when set, defender stronghold starts at this HP for the sim


class SimulateCombatRequest(BaseModel):
    """Request body for POST /simulate-combat."""
    attacker_stacks: list[dict[str, Any]]  # [{"unit_id": str, "count": int}, ...]
    defender_stacks: list[dict[str, Any]]
    territory_id: str
    game_id: str | None = None  # when set, use this game's definitions (same as actual combat) so archer prefire etc. match
    setup_id: str | None = None  # used only when game_id not set; default from config
    n_trials: int = 10000
    seed: int = 8  # fixed seed for deterministic, repeatable results
    options: SimulateCombatOptionsRequest | None = None
    include_outcomes: bool = False  # when True, response includes per-trial outcomes for client-side merge (chunked progress)


class SimulateCombatPercentileOutcome(BaseModel):
    """Single battle outcome at a percentile of attacker casualties (for outcome summary box)."""
    percentile: int  # 5, 25, 50, 75, 95
    winner: str  # "attacker" | "defender"
    conquered: bool
    retreat: bool
    attacker_casualties: dict[str, int]  # unit_id -> count lost
    defender_casualties: dict[str, int]


# Battle context: specials and shelves from combat_specials engine (single source of truth). Populated when Calc runs.
SPECIAL_KEY_TO_FRONTEND: dict[str, str] = {
    "terrainMountain": "mountain",
    "terrainForest": "forest",
    "antiCavalry": "anti_cavalry",
    "seaRaider": "sea_raider",
}


class BattleContextSpecialEntry(BaseModel):
    side: str  # "attacker" | "defender"
    unit_id: str
    unit_name: str
    count: int


class BattleContextStackEntry(BaseModel):
    unit_id: str
    name: str
    icon: str
    count: int
    special_codes: list[str]  # special ids for frontend to map to display_code
    faction_id: str


class BattleContextShelf(BaseModel):
    stat_value: int
    stacks: list[BattleContextStackEntry]


class BattleContext(BaseModel):
    terrain_label: str = ""
    specials_in_battle: dict[str, list[BattleContextSpecialEntry]] = {}  # special_id -> entries
    effective_attacker_shelves: list[BattleContextShelf] = []
    effective_defender_shelves: list[BattleContextShelf] = []


def _build_battle_context(
    attacker_units: list,
    defender_units: list,
    result: BattleSpecialsResult,
    unit_defs: dict,
    territory_def: Any,
) -> BattleContext:
    """Build battle context (specials in battle + effective shelves) from combat_specials result."""
    terrain_type = (getattr(territory_def, "terrain_type", None) or "").lower()
    terrain_label = terrain_type.capitalize() if terrain_type else ""

    def _unit_name(uid: str) -> str:
        ud = unit_defs.get(uid)
        return getattr(ud, "display_name", uid) if ud else uid

    def _unit_icon(uid: str) -> str:
        ud = unit_defs.get(uid)
        return getattr(ud, "icon", "") or "" if ud else ""

    def _unit_faction(uid: str) -> str:
        ud = unit_defs.get(uid)
        return getattr(ud, "faction", "") or "" if ud else ""

    def _special_key_to_frontend(k: str) -> str:
        return SPECIAL_KEY_TO_FRONTEND.get(k, k)

    # Aggregate specials_in_battle: special_id -> [ { side, unit_id, unit_name, count } ]
    by_special: dict[str, dict[tuple[str, str], int]] = {}  # special_id -> (side, unit_id) -> count
    for instance_id, flags in result.specials_attacker.items():
        u = next((u for u in attacker_units if u.instance_id == instance_id), None)
        if not u:
            continue
        uid = u.unit_id
        for key, on in flags.items():
            if not on:
                continue
            sid = _special_key_to_frontend(key)
            if sid not in by_special:
                by_special[sid] = {}
            key_agg = ("attacker", uid)
            by_special[sid][key_agg] = by_special[sid].get(key_agg, 0) + 1
    for instance_id, flags in result.specials_defender.items():
        u = next((u for u in defender_units if u.instance_id == instance_id), None)
        if not u:
            continue
        uid = u.unit_id
        for key, on in flags.items():
            if not on:
                continue
            sid = _special_key_to_frontend(key)
            if sid not in by_special:
                by_special[sid] = {}
            key_agg = ("defender", uid)
            by_special[sid][key_agg] = by_special[sid].get(key_agg, 0) + 1

    specials_in_battle: dict[str, list[BattleContextSpecialEntry]] = {}
    for sid, agg in by_special.items():
        # Keys must sort with a homogeneous type (avoid bool vs list if unit_id is ever corrupt).
        specials_in_battle[sid] = [
            BattleContextSpecialEntry(side=s, unit_id=uid, unit_name=_unit_name(uid), count=c)
            for (s, uid), c in sorted(
                agg.items(),
                key=lambda x: (str(x[0][0]), str(x[0][1])),
            )
        ]

    # Effective attacker shelves: stat_value -> [ { unit_id, name, icon, count, special_codes, faction_id } ]
    att_mods = result.stat_modifiers_attacker
    att_specials = result.specials_attacker
    _, _, att_eff_attack_ov = get_attacker_effective_dice_and_bombikazi_self_destruct(
        attacker_units, unit_defs
    )
    by_stat_att: dict[int, dict[str, dict[str, Any]]] = {}  # stat_value -> unit_id -> { count, special_codes, ... }
    for u in attacker_units:
        ud = unit_defs.get(u.unit_id)
        base_attack = getattr(ud, "attack", 0) if ud else 0
        mod = att_mods.get(u.instance_id, 0)
        if att_eff_attack_ov and u.instance_id in att_eff_attack_ov:
            stat = att_eff_attack_ov[u.instance_id]
        else:
            stat = base_attack + mod
        flags = att_specials.get(u.instance_id) or {}
        codes = [_special_key_to_frontend(k) for k, v in flags.items() if v]
        if stat not in by_stat_att:
            by_stat_att[stat] = {}
        rec = by_stat_att[stat].get(u.unit_id)
        if rec:
            rec["count"] += 1
            for c in codes:
                if c not in rec["special_codes"]:
                    rec["special_codes"].append(c)
        else:
            by_stat_att[stat][u.unit_id] = {
                "unit_id": u.unit_id,
                "name": _unit_name(u.unit_id),
                "icon": _unit_icon(u.unit_id),
                "count": 1,
                "special_codes": list(dict.fromkeys(codes)),
                "faction_id": _unit_faction(u.unit_id),
            }
    effective_attacker_shelves = [
        BattleContextShelf(
            stat_value=stat_value,
            stacks=[
                BattleContextStackEntry(
                    unit_id=r["unit_id"],
                    name=r["name"],
                    icon=r["icon"],
                    count=r["count"],
                    special_codes=r["special_codes"],
                    faction_id=r["faction_id"],
                )
                for r in by_stat_att[stat_value].values()
            ],
        )
        for stat_value in sorted(by_stat_att.keys())
        if stat_value != 0
    ]

    def_mods = result.stat_modifiers_defender
    def_specials = result.specials_defender
    by_stat_def: dict[int, dict[str, dict[str, Any]]] = {}
    for u in defender_units:
        ud = unit_defs.get(u.unit_id)
        base_def = getattr(ud, "defense", 0) if ud else 0
        mod = def_mods.get(u.instance_id, 0)
        stat = base_def + mod
        flags = def_specials.get(u.instance_id) or {}
        codes = [_special_key_to_frontend(k) for k, v in flags.items() if v]
        if stat not in by_stat_def:
            by_stat_def[stat] = {}
        rec = by_stat_def[stat].get(u.unit_id)
        if rec:
            rec["count"] += 1
            for c in codes:
                if c not in rec["special_codes"]:
                    rec["special_codes"].append(c)
        else:
            by_stat_def[stat][u.unit_id] = {
                "unit_id": u.unit_id,
                "name": _unit_name(u.unit_id),
                "icon": _unit_icon(u.unit_id),
                "count": 1,
                "special_codes": list(dict.fromkeys(codes)),
                "faction_id": _unit_faction(u.unit_id),
            }
    effective_defender_shelves = [
        BattleContextShelf(
            stat_value=stat_value,
            stacks=[
                BattleContextStackEntry(
                    unit_id=r["unit_id"],
                    name=r["name"],
                    icon=r["icon"],
                    count=r["count"],
                    special_codes=r["special_codes"],
                    faction_id=r["faction_id"],
                )
                for r in by_stat_def[stat_value].values()
            ],
        )
        for stat_value in sorted(by_stat_def.keys())
        if stat_value != 0
    ]

    return BattleContext(
        terrain_label=terrain_label,
        specials_in_battle=specials_in_battle,
        effective_attacker_shelves=effective_attacker_shelves,
        effective_defender_shelves=effective_defender_shelves,
    )


class SimulateCombatResponse(BaseModel):
    """Response for POST /simulate-combat. Prefire hits are averaged across all trials (distinct from normal combat rounds)."""
    n_trials: int
    attacker_wins: int
    defender_wins: int
    attacker_survives: int  # trials with >0 attacking units remaining (excludes mutual wipe)
    defender_survives: int  # trials with >0 defending units remaining (excludes mutual wipe)
    retreats: int
    conquers: int
    p_attacker_win: float
    p_defender_win: float
    p_attacker_survives: float
    p_defender_survives: float
    p_retreat: float
    p_conquer: float
    rounds_mean: float
    rounds_p50: float
    rounds_p90: float
    attacker_casualties_mean: dict[str, float]
    defender_casualties_mean: dict[str, float]
    attacker_casualties_total_mean: float
    defender_casualties_total_mean: float
    attacker_casualties_p90: dict[str, float]
    defender_casualties_p90: dict[str, float]
    attacker_prefire_hits_mean: float | None  # None when no stealth prefire in this battle
    defender_prefire_hits_mean: float | None  # None when no archer prefire in this battle
    attacker_siegework_hits_mean: float | None  # None when no trial had attacker siegework dice
    defender_siegework_hits_mean: float | None  # None when no trial had defender siegework dice
    attacker_casualty_cost_mean: float  # mean power cost of attacker casualties across trials
    defender_casualty_cost_mean: float  # mean power cost of defender casualties across trials
    attacker_casualty_cost_variance_category: str  # Predictable / Moderate / Unpredictable
    defender_casualty_cost_variance_category: str
    percentile_outcomes: list[SimulateCombatPercentileOutcome]  # 5th, 25th, 50th, 75th, 95th by attacker casualties
    battle_context: BattleContext | None = None  # specials + shelves from backend engine; set when sim runs
    outcomes: list[dict[str, Any]] | None = None  # per-trial data when include_outcomes=True (for chunked merge)
    prefire_penalty: bool = True  # mirrors SimOptions; stealth/archer prefire use -1 to stat when True (manifest boolean)


# ===== Helper Functions =====

def generate_game_code(db: Session) -> str:
    """Generate a unique 4-char alphanumeric game code."""
    for _ in range(20):
        code = "".join(secrets.choice(GAME_CODE_CHARS) for _ in range(GAME_CODE_LENGTH))
        if db.query(GameModel).filter(GameModel.game_code == code).first() is None:
            return code
    raise HTTPException(status_code=500, detail="Could not generate unique game code")


def _build_definitions_snapshot(
    ud=None, td=None, fd=None, cd=None, pd=None, start=None,
    specials=None, specials_order=None,
) -> dict:
    """Snapshot of definitions + starting_setup for storing in game config. Uses provided defs or module fallback."""
    ud = ud if ud is not None else unit_defs
    td = td if td is not None else territory_defs
    fd = fd if fd is not None else faction_defs
    cd = cd if cd is not None else camp_defs
    pd = pd if pd is not None else port_defs
    start = start if start is not None else starting_setup
    defs = {
        "units": {k: asdict(v) for k, v in ud.items()},
        "territories": {k: asdict(v) for k, v in td.items()},
        "factions": {k: asdict(v) for k, v in fd.items()},
        "camps": {k: asdict(v) for k, v in cd.items()},
        "ports": {k: asdict(v) for k, v in pd.items()},
    }
    if specials is not None:
        defs["specials"] = specials
    if specials_order is not None:
        defs["specials_order"] = specials_order
    return {
        "definitions": defs,
        "starting_setup": start,
        "lobby_claims": {},  # set by create_game for multiplayer; faction_id -> player_id
    }


def get_game_definitions(game_id: str, db: Session | None = None):
    """Return (unit_defs, territory_defs, faction_defs, camp_defs, port_defs) for this game. Uses snapshot from config if present, else global defs."""
    if game_id in game_defs:
        return game_defs[game_id]
    if db is None:
        db = next(get_db())
    row = db.query(GameModel).filter(GameModel.id == game_id).first()
    if not row or not row.config:
        return (unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    try:
        config = json.loads(row.config) if isinstance(row.config, str) else row.config
        defs_snapshot = config.get("definitions")
        if not defs_snapshot:
            return (unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
        ud, td, fd, cd, port_d = definitions_from_snapshot(defs_snapshot)
        game_defs[game_id] = (ud, td, fd, cd, port_d)
        return (ud, td, fd, cd, port_d)
    except Exception:
        return (unit_defs, territory_defs, faction_defs, camp_defs, port_defs)


def _player_can_act(game_id: str, player: Player, db: Session) -> bool:
    """True if this player is in the game and assigned to the faction whose turn it is."""
    row = db.query(GameModel).filter(GameModel.id == game_id).first()
    if not row:
        return False
    try:
        players_list = json.loads(row.players) if isinstance(row.players, str) else row.players
        raw = json.loads(row.game_state) if isinstance(row.game_state, str) else row.game_state
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(players_list, list) or not isinstance(raw, dict):
        return False
    current_faction = raw.get("current_faction")
    return any(
        str(p.get("player_id")) == str(player.id) and str(p.get("faction_id")) == str(current_faction)
        for p in players_list
    )


def _require_can_act(game_id: str, player: Player, db: Session) -> None:
    """Raise 403 if this player is not allowed to perform actions (not their faction's turn)."""
    if not _player_can_act(game_id, player, db):
        raise HTTPException(status_code=403, detail="Not your turn")


def get_game(game_id: str, db: Session | None = None) -> GameState:
    """Get game state from DB (always fresh when db provided); raise 404 if not found."""
    if db is None:
        if game_id in games:
            return games[game_id]
        db = next(get_db())
    row = db.query(GameModel).filter(GameModel.id == game_id).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Game {game_id} not found")
    try:
        raw = json.loads(row.game_state) if isinstance(row.game_state, str) else row.game_state
        if not isinstance(raw, dict):
            raw = {}
        state = GameState.from_dict(raw)
        try:
            cfg = json.loads(row.config) if isinstance(row.config, str) else row.config
            if isinstance(cfg, dict):
                ss = cfg.get("starting_setup")
                if isinstance(ss, dict):
                    backfill_liberation_metadata(state, ss)
        except Exception:
            pass
        # Prime definitions cache from config so this game uses its snapshot
        get_game_definitions(game_id, db)
    except Exception:
        # Corrupt or legacy state in DB — treat as not found so client can create fresh game
        raise HTTPException(status_code=404, detail=f"Game {game_id} not found")
    games[game_id] = state
    return state


EVENT_LOG_MAX = 1000


def save_game(
    game_id: str,
    state: GameState,
    db: Session | None = None,
    events: list | None = None,
) -> None:
    """Persist game state to DB and cache. If events is provided, append to config event_log (capped)."""
    from backend.engine.events import GameEvent

    games[game_id] = state
    if db is None:
        db = next(get_db())
    row = db.query(GameModel).filter(GameModel.id == game_id).first()
    if row:
        row.game_state = json.dumps(state.to_dict())
        if state.winner is not None:
            row.status = "finished"
        if events:
            try:
                config = json.loads(row.config) if isinstance(row.config, str) else {}
                if not isinstance(config, dict):
                    config = {}
                log = config.get("event_log")
                if not isinstance(log, list):
                    log = []
                for e in events:
                    if isinstance(e, GameEvent):
                        log.append(e.to_dict())
                    elif isinstance(e, dict):
                        log.append(e)
                if len(log) > EVENT_LOG_MAX:
                    log = log[-EVENT_LOG_MAX:]
                config["event_log"] = log
                row.config = json.dumps(config)
            except (TypeError, json.JSONDecodeError):
                pass
        db.commit()


def _sort_attackers_for_ladder_dice_if_needed(
    state, attackers: list, defenders: list, ud, td,
) -> None:
    """Match reducer dice order: off-ladder attackers before on-ladder per attack shelf."""
    combat = state.active_combat
    if not combat or not attackers:
        return
    ladder_ids = set(getattr(combat, "ladder_infantry_instance_ids", []) or [])
    if not ladder_ids:
        return
    territory_def = td.get(combat.territory_id)
    terrain_att, terrain_def = compute_terrain_stat_modifiers(
        territory_def, attackers, defenders, ud
    )
    anticav_att, anticav_def = compute_anti_cavalry_stat_modifiers(
        attackers, defenders, ud
    )
    captain_att, captain_def = compute_captain_stat_modifiers(
        attackers, defenders, ud
    )
    sea_raider_att, _ = compute_sea_raider_stat_modifiers(
        attackers, ud, is_sea_raid=bool(getattr(combat, "sea_zone_id", None))
    )
    attacker_mods = merge_stat_modifiers(
        terrain_att, anticav_att, captain_att, sea_raider_att
    )
    _, _, att_ov = get_attacker_effective_dice_and_bombikazi_self_destruct(attackers, ud)
    sort_attackers_for_ladder_dice_order(
        attackers, ud, ladder_ids, attacker_mods, att_ov or None,
    )


def roll_dice(count: int, sides: int = 10) -> list[int]:
    """Roll dice for combat."""
    return [random.randint(1, sides) for _ in range(count)]


def state_to_dict(state: GameState) -> dict[str, Any]:
    """Convert game state to JSON-serializable dict."""
    return state.to_dict()


def _get_combat_modifiers_and_specials(
    state: GameState,
    ud: dict,
    td: dict,
    fd: dict,
) -> tuple[dict, dict, dict]:
    """Compute combat stat modifiers and special flags for active combat. Uses combat_specials engine (single source of truth).
    Returns (combat_stat_modifiers, combat_specials, combat_attacker_effective_attack_override). The override is for paired bombikazi (bomb's attack) so UI can show them on the bomb's shelf."""
    if not state.active_combat:
        return {}, {}, {}
    territory = state.territories.get(state.active_combat.territory_id)
    if not territory:
        return {}, {}, {}
    attacker_faction = state.current_faction
    attacker_alliance = getattr(fd.get(attacker_faction), "alliance", None) if fd.get(attacker_faction) else None
    attacker_ids = set(state.active_combat.attacker_instance_ids)
    sea_zone_id = getattr(state.active_combat, "sea_zone_id", None)
    if sea_zone_id:
        sea_zone = state.territories.get(sea_zone_id)
        from backend.engine.utils import is_land_unit
        attackers_from_sea: list = []
        if sea_zone:
            attackers_from_sea = [
                u for u in sea_zone.units
                if u.instance_id in attacker_ids
                and is_land_unit(ud.get(u.unit_id))
                and not _is_naval_unit(ud.get(u.unit_id))
            ]
        if attackers_from_sea:
            attackers = sorted(attackers_from_sea, key=lambda u: u.instance_id)
        else:
            # After offload, raiders are on land; sea hex may still have only boats.
            attackers = sorted(
                [
                    u for u in territory.units
                    if u.instance_id in attacker_ids
                    and is_land_unit(ud.get(u.unit_id))
                    and not _is_naval_unit(ud.get(u.unit_id))
                ],
                key=lambda u: u.instance_id,
            )
    else:
        attackers = sorted(
            [u for u in territory.units if u.instance_id in attacker_ids],
            key=lambda u: u.instance_id,
        )
    defenders = sorted(
        [
            u for u in territory.units
            if u.instance_id not in attacker_ids
            and ud.get(u.unit_id)
            and (getattr(fd.get(ud[u.unit_id].faction), "alliance", None) if fd.get(ud[u.unit_id].faction) else None) != attacker_alliance
        ],
        key=lambda u: u.instance_id,
    )
    territory_def = td.get(state.active_combat.territory_id)
    if territory_def and _is_sea_zone(territory_def):
        attackers = [
            u for u in attackers
            if participates_in_sea_hex_naval_combat(u, ud.get(u.unit_id))
        ]
        defenders = [
            u for u in defenders
            if participates_in_sea_hex_naval_combat(u, ud.get(u.unit_id))
        ]
    combat_log = getattr(state.active_combat, "combat_log", []) or []
    first_round = combat_log[0] if len(combat_log) >= 1 else None
    archer_prefire_applicable = bool(
        first_round is not None and getattr(first_round, "is_archer_prefire", False)
    )
    territory_is_sea = _is_sea_zone(territory_def) if territory_def else False
    defender_stronghold_hp_for_ram: int | None = None
    if not territory_is_sea and territory_def:
        base_hp = getattr(territory_def, "stronghold_base_health", 0) or 0
        if getattr(territory_def, "is_stronghold", False) and base_hp > 0:
            cur = getattr(territory, "stronghold_current_health", None)
            defender_stronghold_hp_for_ram = cur if cur is not None else base_hp
    fuse_ram = getattr(state.active_combat, "fuse_bomb", True)
    if not isinstance(fuse_ram, bool):
        fuse_ram = True
    ram_applicable = ram_special_applicable_for_active_combat(
        combat_log,
        getattr(state.active_combat, "round_number", 0),
        attackers,
        defenders,
        territory_def,
        defender_stronghold_hp_for_ram,
        territory_is_sea,
        ud,
        fuse_bomb=fuse_ram,
    )
    stealth_prefire_applicable = stealth_prefire_applicable_for_active_combat(
        combat_log,
        getattr(state.active_combat, "round_number", 0),
        attackers,
        ud,
    )
    result = compute_battle_specials_and_modifiers(
        attackers,
        defenders,
        territory_def,
        ud,
        is_sea_raid=bool(sea_zone_id),
        archer_prefire_applicable=archer_prefire_applicable,
        stealth_prefire_applicable=stealth_prefire_applicable,
        ram_applicable=ram_applicable,
    )
    _, _, attacker_effective_attack_override = get_attacker_effective_dice_and_bombikazi_self_destruct(attackers, ud)
    combat_stat_modifiers = {
        "attacker": result.stat_modifiers_attacker,
        "defender": result.stat_modifiers_defender,
    }
    combat_specials = {
        "attacker": result.specials_attacker,
        "defender": result.specials_defender,
    }
    return combat_stat_modifiers, combat_specials, attacker_effective_attack_override


def state_for_response(state: GameState, game_id: str | None = None, db: Session | None = None) -> dict[str, Any]:
    """State dict including computed faction_stats for the UI. Uses game's definitions if game_id provided.
    When state.turn_order is empty, fills from game config starting_setup so the turn ticker and faction order are correct."""
    out = state_to_dict(state)
    # Ensure pending_camps is always present so frontend can show camp placement during mobilization
    if "pending_camps" not in out:
        out["pending_camps"] = getattr(state, "pending_camps", [])
    if game_id and db is not None and (not out.get("turn_order") or len(out.get("turn_order", [])) == 0):
        row = db.query(GameModel).filter(GameModel.id == game_id).first()
        if row and row.config:
            try:
                config = json.loads(row.config) if isinstance(row.config, str) else row.config
                start = config.get("starting_setup") or {}
                order = start.get("turn_order")
                if isinstance(order, list) and order:
                    out["turn_order"] = order
            except Exception:
                pass
    try:
        if game_id and db is not None:
            ud, td, fd, _, _ = get_game_definitions(game_id, db)
        else:
            ud, td, fd = unit_defs, territory_defs, faction_defs
        out["faction_stats"] = get_faction_stats(state, td, fd, ud)
        if state.active_combat and game_id and db is not None:
            combat_stat_modifiers, combat_specials, combat_attacker_effective_attack_override = _get_combat_modifiers_and_specials(state, ud, td, fd)
            out["combat_stat_modifiers"] = combat_stat_modifiers
            out["combat_specials"] = combat_specials
            out["combat_attacker_effective_attack_override"] = combat_attacker_effective_attack_override
        if state.active_combat:
            ac_out = out.get("active_combat")
            if isinstance(ac_out, dict):
                _enrich_active_combat_siegework_display_ids(ac_out, state, ud, td)
    except Exception:
        out["faction_stats"] = {"factions": {}, "alliances": {}}
    return out


# ===== API Endpoints =====

@app.get("/")
def root():
    return {"message": "Baggins & Allies API", "version": "1.0.0"}


# ----- Auth -----


def _player_prefs_dict(player: Player) -> dict[str, Any]:
    if not player.preferences:
        return {}
    try:
        return json.loads(player.preferences)
    except (json.JSONDecodeError, TypeError):
        return {}


# New players and empty/missing audio prefs: menu 50%, in-game music 25%, SFX 25%.
_DEFAULT_MENU_MUSIC = 0.5
_DEFAULT_GAME_MUSIC = 0.25
_DEFAULT_SFX = 0.25


def _default_player_audio_stored() -> dict[str, Any]:
    """Canonical audio object persisted for new accounts (and same values used when audio prefs are empty)."""
    g = _DEFAULT_GAME_MUSIC
    return {
        "menu_music_volume": _DEFAULT_MENU_MUSIC,
        "game_music_volume": g,
        "music_volume": g,
        "master_volume": g,
        "sfx_volume": _DEFAULT_SFX,
        "muted": False,
    }


def _player_audio_response(player: Player) -> dict[str, Any]:
    prefs = _player_prefs_dict(player)
    raw = prefs.get("audio")
    audio = raw if isinstance(raw, dict) else {}
    if not audio:
        return {**_default_player_audio_stored()}
    try:
        game_music = float(
            audio.get("game_music_volume", audio.get("music_volume", audio.get("master_volume", _DEFAULT_GAME_MUSIC))),
        )
        game_music = max(0.0, min(1.0, game_music))
    except (TypeError, ValueError):
        game_music = _DEFAULT_GAME_MUSIC
    try:
        menu_music = float(audio.get("menu_music_volume", game_music))
        menu_music = max(0.0, min(1.0, menu_music))
    except (TypeError, ValueError):
        menu_music = game_music
    try:
        sfx = float(audio.get("sfx_volume", _DEFAULT_SFX))
        sfx = max(0.0, min(1.0, sfx))
    except (TypeError, ValueError):
        sfx = _DEFAULT_SFX
    muted = bool(audio.get("muted", False))
    return {
        "menu_music_volume": menu_music,
        "game_music_volume": game_music,
        "music_volume": game_music,
        "sfx_volume": sfx,
        "muted": muted,
        "master_volume": game_music,
    }


def _profile_response(player: Player) -> dict[str, Any]:
    return {
        "id": player.id,
        "email": player.email,
        "username": player.username,
        "is_admin": bool(getattr(player, "is_admin", False)),
        "audio": _player_audio_response(player),
    }


@app.post("/auth/register")
def register(request: RegisterRequest, db: Session = Depends(get_db)):
    """Register with email, username (unique, no spaces/special), and password."""
    if not validate_username(request.username):
        raise HTTPException(
            status_code=400,
            detail="Username must be 2–32 characters, letters numbers and underscore only",
        )
    if db.query(Player).filter(Player.email == request.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")
    if db.query(Player).filter(Player.username == request.username).first():
        raise HTTPException(status_code=400, detail="Username already taken")
    try:
        player_id = str(uuid.uuid4())
        player = Player(
            id=player_id,
            email=request.email,
            username=request.username,
            password_hash=hash_password(request.password),
            is_admin=False,
            preferences=json.dumps({"audio": _default_player_audio_stored()}),
        )
        db.add(player)
        db.commit()
        db.refresh(player)
        token = create_access_token(player_id)
        return {"access_token": token, "player": _profile_response(player)}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Registration failed: {str(e)}")


@app.post("/auth/login")
def login(request: LoginRequest, db: Session = Depends(get_db)):
    """Login with email and password."""
    player = db.query(Player).filter(Player.email == request.email).first()
    if not player or not verify_password(request.password, player.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = create_access_token(player.id)
    return {"access_token": token, "player": _profile_response(player)}


@app.get("/auth/me")
def auth_me(player: Player = Depends(get_current_player)):
    """Return current player (email, username, audio preferences)."""
    return _profile_response(player)


@app.patch("/auth/me")
def update_profile(
    request: PatchProfileRequest,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Update username and/or audio preferences (merged into stored JSON)."""
    if request.username is None and request.audio is None:
        raise HTTPException(status_code=400, detail="No fields to update")

    prefs = _player_prefs_dict(player)

    if request.audio is not None:
        audio = prefs.get("audio") or {}
        if request.audio.menu_music_volume is not None:
            audio["menu_music_volume"] = float(request.audio.menu_music_volume)
        if request.audio.game_music_volume is not None:
            audio["game_music_volume"] = float(request.audio.game_music_volume)
        if request.audio.master_volume is not None:
            v = float(request.audio.master_volume)
            audio["music_volume"] = v
            audio["game_music_volume"] = v
        if request.audio.music_volume is not None:
            v = float(request.audio.music_volume)
            audio["music_volume"] = v
            if request.audio.game_music_volume is None:
                audio["game_music_volume"] = v
        if request.audio.sfx_volume is not None:
            audio["sfx_volume"] = float(request.audio.sfx_volume)
        if request.audio.muted is not None:
            audio["muted"] = bool(request.audio.muted)
        prefs["audio"] = audio
        player.preferences = json.dumps(prefs)

    if request.username is not None:
        new_username = request.username.strip()
        if not validate_username(new_username):
            raise HTTPException(
                status_code=400,
                detail="Username must be 2–32 characters, letters numbers and underscore only",
            )
        if new_username != player.username:
            if db.query(Player).filter(Player.username == new_username, Player.id != player.id).first():
                raise HTTPException(status_code=400, detail="Username already taken")
            player.username = new_username

    try:
        db.commit()
        db.refresh(player)
        return _profile_response(player)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Update failed: {str(e)}")


# ----- Games (create, list, join) -----

@app.get("/setups")
def get_setups(db: Session = Depends(get_db)):
    """List available game setups (id, display_name, map_asset). Use setup_id in POST /games/create."""
    return {"setups": try_list_setups_menu(db)}


class AdminSetupPayload(BaseModel):
    manifest: dict[str, Any]
    units: dict[str, Any]
    territories: dict[str, Any]
    factions: dict[str, Any]
    camps: dict[str, Any]
    ports: dict[str, Any]
    starting_setup: dict[str, Any]
    specials: dict[str, Any]


class AdminCreateSetupBody(BaseModel):
    id: str = Field(..., min_length=1, max_length=127)
    duplicate_from: str | None = None


@app.get("/admin/setups")
def admin_list_setups(
    _admin: Player = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    return {"setups": list_all_setups_admin(db)}


@app.post("/admin/setups")
def admin_create_setup(
    body: AdminCreateSetupBody,
    _admin: Player = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Create a new setup: empty draft or duplicate of an existing id. Setup id must be unique."""
    try:
        out = create_setup(db, body.id.strip(), body.duplicate_from)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, **out}


@app.get("/admin/setups/{setup_id}")
def admin_get_setup(
    setup_id: str,
    _admin: Player = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    bundle = get_admin_setup_bundle(db, setup_id)
    if not bundle:
        raise HTTPException(status_code=404, detail="Setup not found")
    return bundle


@app.put("/admin/setups/{setup_id}")
def admin_put_setup(
    setup_id: str,
    body: AdminSetupPayload,
    _admin: Player = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    payload = body.model_dump()
    manifest = dict(payload["manifest"])
    manifest["id"] = setup_id
    payload["manifest"] = manifest
    errs = validate_setup_payload(payload)
    if errs:
        raise HTTPException(status_code=400, detail={"validation_errors": errs})
    try:
        save_setup_bundle(db, setup_id, payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "id": setup_id}


@app.post("/games/create")
def create_game(
    request: CreateGameRequest,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Create a new game (single or multiplayer). Returns game_id and game_code (if multiplayer)."""
    setup_id = request.setup_id if request.setup_id is not None else DEFAULT_SETUP_ID
    setup = try_load_setup(setup_id, db)
    if not setup:
        raise HTTPException(status_code=400, detail=f"Setup not found: {setup_id}")
    try:
        ud, td, fd, cd, port_d = try_load_static_definitions(setup_id, db)
        specials_defs, specials_order = try_load_specials(setup_id, db)
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))
    victory_criteria = setup.get("victory_criteria")
    camp_cost = setup.get("camp_cost")
    stronghold_repair_cost = setup.get("stronghold_repair_cost")
    state = initialize_game_state(
        faction_defs=fd,
        territory_defs=td,
        unit_defs=ud,
        starting_setup=setup["starting_setup"],
        camp_defs=cd,
        victory_criteria=victory_criteria,
        camp_cost=camp_cost,
        stronghold_repair_cost=stronghold_repair_cost,
        prefire_penalty=parse_prefire_penalty_from_manifest(setup.get("prefire_penalty")),
    )
    state.map_asset = setup["map_asset"]
    # Ensure turn_order is never empty for new games (ticker and faction order)
    if not state.turn_order and isinstance(setup.get("starting_setup"), dict):
        order = setup["starting_setup"].get("turn_order")
        if isinstance(order, list) and order:
            state.turn_order = [f for f in order if f in fd]
    if not state.turn_order:
        state.turn_order = sorted(fd.keys())
    # Default defender casualty order: best_defense for strongholds, capitals, camps, ports
    for tid, tdef in td.items():
        if getattr(tdef, "is_stronghold", False):
            state.territory_defender_casualty_order[tid] = "best_defense"
    for fid, fdef in fd.items():
        cap = getattr(fdef, "capital", None)
        if cap and cap in td:
            state.territory_defender_casualty_order[cap] = "best_defense"
    for cid, cdef in cd.items():
        state.territory_defender_casualty_order[cdef.territory_id] = "best_defense"
    for pid, pdef in port_d.items():
        state.territory_defender_casualty_order[pdef.territory_id] = "best_defense"
    game_id = str(uuid.uuid4())
    game_code = generate_game_code(db) if request.is_multiplayer else None
    # Both single-player and multiplayer use lobby: host assigns factions (or You/Computer per faction)
    players_list = [{"player_id": str(player.id), "faction_id": None}]
    status = "lobby"
    players_json = json.dumps(players_list)
    config_snapshot = _build_definitions_snapshot(
        ud, td, fd, cd, port_d, setup["starting_setup"],
        specials=specials_defs, specials_order=specials_order,
    )
    # Always persist resolved setup (including default) so list/meta can show scenario name.
    config_snapshot["setup_id"] = setup_id
    # ai_factions set on start from unclaimed factions (single-player) or not used (multiplayer)
    if request.ai_factions:
        config_snapshot["ai_factions"] = list(request.ai_factions)
    row = GameModel(
        id=game_id,
        name=request.name,
        game_code=game_code,
        created_by=player.id,
        status=status,
        game_state=json.dumps(state.to_dict()),
        players=players_json,
        config=json.dumps(config_snapshot),
    )
    db.add(row)
    db.commit()
    games[game_id] = state
    game_defs[game_id] = (ud, td, fd, cd, port_d)
    state_dict = state_for_response(state, game_id, db)
    turn_order = state_dict.get("turn_order") if isinstance(state_dict.get("turn_order"), list) else None
    return {
        "game_id": game_id,
        "game_code": game_code,
        "name": request.name,
        "state": state_dict,
        "turn_order": turn_order,
    }


DEFAULT_FACTION_STATS = {
    "factions": {},
    "alliances": {
        "good": {"strongholds": 0, "territories": 0, "power": 0, "power_per_turn": 0, "units": 0, "unit_power": 0},
        "evil": {"strongholds": 0, "territories": 0, "power": 0, "power_per_turn": 0, "units": 0, "unit_power": 0},
    },
    "neutral_strongholds": 0,
}


def _get_forfeited_player_ids(row) -> list[str]:
    """Extract forfeited_player_ids from game config. Players who forfeited are excluded from their list."""
    if not getattr(row, "config", None):
        return []
    try:
        config = json.loads(row.config) if isinstance(row.config, str) else row.config
        ids = config.get("forfeited_player_ids")
        if isinstance(ids, list):
            return [str(x) for x in ids]
    except (TypeError, json.JSONDecodeError):
        pass
    return []


def _get_scenario_from_config(row, db: Session) -> dict[str, Any] | None:
    """Return { display_name, context } from setup manifest if config has setup_id."""
    if not getattr(row, "config", None):
        return None
    try:
        config = json.loads(row.config) if isinstance(row.config, str) else row.config
        setup_id = config.get("setup_id")
        if not setup_id or not isinstance(setup_id, str):
            return None
        # DB manifest when setups are in DB; inactive setups still resolve for game cards.
        return try_scenario_display(setup_id, db)
    except (TypeError, json.JSONDecodeError):
        pass
    return None


def _build_games_list(player: Player, db: Session) -> list[dict[str, Any]]:
    """Build list of game dicts for the current player (with faction_stats and current_player_username). Excludes games the player has forfeited."""
    rows = db.query(GameModel).filter(GameModel.status != "finished").all()
    mine = []
    player_ids = set()
    player_id_str = str(player.id)
    for r in rows:
        try:
            pl = json.loads(r.players)
            if not isinstance(pl, list):
                continue
            if not any(str(p.get("player_id")) == player_id_str for p in pl):
                continue
            if player_id_str in _get_forfeited_player_ids(r):
                continue
            player_ids.add(player_id_str)
            for p in pl:
                pid = p.get("player_id")
                if pid is not None:
                    player_ids.add(str(pid))
        except (json.JSONDecodeError, TypeError):
            continue

    players_by_id = {}
    if player_ids:
        id_list = list(player_ids)
        for p_row in db.query(Player).filter(Player.id.in_(id_list)).all():
            players_by_id[str(p_row.id)] = p_row.username

    for r in rows:
        try:
            pl = json.loads(r.players)
            if not isinstance(pl, list):
                continue
            if not any(str(p.get("player_id")) == player_id_str for p in pl):
                continue
            if player_id_str in _get_forfeited_player_ids(r):
                continue
        except (json.JSONDecodeError, TypeError):
            continue

        turn_number = None
        phase = None
        current_faction = None
        current_player_username = None
        current_faction_display_name = None
        current_faction_icon = None
        faction_stats = None
        fd = faction_defs  # fallback to default setup if get_game_definitions not run or fails

        try:
            state_dict = json.loads(r.game_state) if isinstance(r.game_state, str) else {}
            if not isinstance(state_dict, dict):
                state_dict = {}
        except (json.JSONDecodeError, TypeError):
            state_dict = {}

        if state_dict:
            turn_number = state_dict.get("turn_number")
            phase = state_dict.get("phase")
            current_faction = state_dict.get("current_faction")

        try:
            try:
                state = GameState.from_dict(state_dict)
            except Exception:
                state = GameState.from_dict({})
            try:
                ud, td, fd, _, _ = get_game_definitions(str(r.id), db)
            except Exception:
                ud, td, fd = None, territory_defs, faction_defs
            faction_stats = get_faction_stats(state, td, fd, ud)
        except Exception:
            faction_stats = dict(DEFAULT_FACTION_STATS)

        # Use this game's setup faction defs (fd), not global default, so old games with different setups show correct names/icons
        if current_faction and fd and fd.get(current_faction):
            current_fd = fd[current_faction]
            current_faction_display_name = getattr(current_fd, "display_name", None) or current_faction
            icon = getattr(current_fd, "icon", None) or f"{current_faction}.png"
            current_faction_icon = f"/assets/factions/{icon}"
            for p in pl:
                if str(p.get("faction_id")) == str(current_faction):
                    current_player_username = players_by_id.get(str(p.get("player_id")))
                    if current_player_username is not None:
                        break
        if current_player_username is None and pl:
            unique_player_ids = list({str(p.get("player_id")) for p in pl if p.get("player_id") is not None})
            if len(unique_player_ids) == 1:
                current_player_username = players_by_id.get(unique_player_ids[0])
        if r.status == "lobby" and current_faction_display_name is None:
            current_faction_display_name = "Lobby"

        if faction_stats is None:
            faction_stats = dict(DEFAULT_FACTION_STATS)

        # Lobby-only: player count, faction counts for list display
        lobby_players = None
        lobby_factions_claimed = None
        lobby_factions_total = None
        if r.status == "lobby":
            lobby_players = len(pl) if isinstance(pl, list) else 0
            turn_order = (state_dict or {}).get("turn_order") or []
            lobby_factions_total = len(turn_order) if isinstance(turn_order, list) else 0
            lobby_claims = _get_lobby_claims_from_config(r)
            lobby_factions_claimed = len(lobby_claims)

        scenario = _get_scenario_from_config(r, db)

        # Username only from faction match or single-player lookup (no fallback to current user)
        item = {
            "id": str(r.id),
            "name": r.name,
            "game_code": r.game_code,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "created_by": str(r.created_by) if r.created_by else None,
            "turn_number": turn_number,
            "phase": phase,
            "current_faction": current_faction,
            "current_faction_display_name": current_faction_display_name,
            "current_faction_icon": current_faction_icon,
            "current_player_username": current_player_username,
            "faction_stats": faction_stats,
            "lobby_players": lobby_players,
            "lobby_factions_claimed": lobby_factions_claimed,
            "lobby_factions_total": lobby_factions_total,
            "scenario": scenario,
        }
        mine.append(item)
    return mine


@app.get("/games")
def list_my_games(
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """List games the current player is in. Includes turn info, current player username, and faction_stats for the stronghold bar."""
    mine = _build_games_list(player, db)
    # Return plain dict so FastAPI serializes it; include marker so client can confirm this handler ran
    return {"games": mine, "_list_version": 2}


@app.post("/games/join")
def join_game(
    request: JoinGameRequest,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Join a game by 4-char game code."""
    code = request.game_code.strip().upper()
    if len(code) != GAME_CODE_LENGTH:
        raise HTTPException(status_code=400, detail="Game code must be 4 characters")
    row = db.query(GameModel).filter(GameModel.game_code == code).first()
    if not row:
        raise HTTPException(status_code=404, detail="Game not found")
    if row.status != "lobby":
        raise HTTPException(status_code=400, detail="Game already started")
    players_list = json.loads(row.players)
    if any(str(p.get("player_id")) == str(player.id) for p in players_list):
        return {"game_id": row.id, "message": "Already in game"}
    players_list.append({"player_id": str(player.id), "faction_id": None})
    row.players = json.dumps(players_list)
    db.commit()
    return {"game_id": row.id, "name": row.name}


def _safe_asdict_map(defs_dict):
    """Serialize a definitions dict to JSON-serializable form; return {} on any error."""
    try:
        return {k: asdict(v) for k, v in (defs_dict or {}).items()}
    except Exception:
        return {}

@app.get("/definitions")
def get_definitions(db: Session = Depends(get_db)):
    """Get all static game definitions (default setup). Never raises."""
    try:
        ud, td, fd, cd, pd = try_load_static_definitions(DEFAULT_SETUP_ID, db)
        specials_defs, specials_order = try_load_specials(DEFAULT_SETUP_ID, db)
        return {
            "units": _safe_asdict_map(ud),
            "territories": _safe_asdict_map(td),
            "factions": _safe_asdict_map(fd),
            "camps": _safe_asdict_map(cd),
            "ports": _safe_asdict_map(pd),
            "specials": specials_defs,
            "specials_order": specials_order,
        }
    except Exception:
        return {
            "units": {}, "territories": {}, "factions": {}, "camps": {}, "ports": {},
            "specials": {}, "specials_order": [],
        }


@app.post("/simulate-combat", response_model=SimulateCombatResponse)
def simulate_combat(request: SimulateCombatRequest, db: Session = Depends(get_db)):
    """
    Run a combat simulation (Monte Carlo) with the given attacker/defender stacks and options.
    Uses the same combat rules as real gameplay. Prefire hits (stealth prefire, archer prefire)
    are tracked separately from normal combat rounds and returned as means across all trials.
    When game_id is provided, uses that game's definition snapshot (same as actual combat) so
    unit_defs match and archer/stealth prefire is recognized correctly.
    """
    if request.game_id:
        try:
            ud, td, *_ = get_game_definitions(request.game_id, db)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid game_id: {request.game_id}") from e
    else:
        setup_id = request.setup_id or DEFAULT_SETUP_ID
        try:
            ud, td, *_ = try_load_static_definitions(setup_id, db)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid setup_id: {setup_id}") from e
    # Support generic terrain-only battles: "terrain:forest" etc. Use a real territory with that terrain if available, else inject synthetic.
    terrain_prefix = "terrain:"
    normalized_id = (request.territory_id or "").strip().lower()
    territory_id_for_sim = request.territory_id
    if normalized_id.startswith(terrain_prefix):
        raw = normalized_id[len(terrain_prefix) :].strip()
        if not raw:
            raise HTTPException(status_code=400, detail="Invalid terrain territory_id")
        # Prefer a real non-stronghold territory with this terrain type so we don't rely on injection
        for tid, tdef in td.items():
            if getattr(tdef, "terrain_type", "").lower() == raw and not getattr(tdef, "is_stronghold", False):
                territory_id_for_sim = tid
                break
        else:
            synthetic = TerritoryDefinition(
                id=request.territory_id,
                display_name=raw.capitalize(),
                terrain_type=raw,
                adjacent=[],
                produces={},
                is_stronghold=False,
                ownable=False,
                aerial_adjacent=[],
            )
            td = {**td, request.territory_id: synthetic}
            territory_id_for_sim = request.territory_id
    elif request.territory_id not in td:
        raise HTTPException(status_code=400, detail=f"Unknown territory_id: {request.territory_id}")
    prefire_penalty_on = True
    if request.game_id:
        try:
            prefire_penalty_on = bool(getattr(get_game(request.game_id, db), "prefire_penalty", True))
        except Exception:
            prefire_penalty_on = True
    else:
        sid = request.setup_id or DEFAULT_SETUP_ID
        try:
            su = try_load_setup(sid, db)
            if su:
                prefire_penalty_on = parse_prefire_penalty_from_manifest(su.get("prefire_penalty"))
            else:
                prefire_penalty_on = True
        except Exception:
            prefire_penalty_on = True
    n_trials = max(1, min(int(request.n_trials), 100_000))
    o = request.options
    stronghold_hp = getattr(o, "stronghold_initial_hp", None) if o is not None else None
    opts = SimOptions(
        casualty_order_attacker=(o.casualty_order_attacker if o else None) or "best_unit",
        casualty_order_defender=(o.casualty_order_defender if o else None) or "best_unit",
        must_conquer=bool(o.must_conquer) if o and o.must_conquer is not None else False,
        max_rounds=o.max_rounds if o else None,
        is_sea_raid=bool(o.is_sea_raid) if o and o.is_sea_raid is not None else False,
        retreat_when_attacker_units_le=o.retreat_when_attacker_units_le if o else None,
        stronghold_initial_hp=stronghold_hp,
        prefire_penalty=prefire_penalty_on,
    )
    is_sea_raid = bool(opts.is_sea_raid)
    # Build battle context from combat_specials engine (single source of truth) so frontend shows backend-derived specials/shelves
    battle_context: BattleContext | None = None
    try:
        att_units, def_units = stacks_to_synthetic_units(
            [s for s in (request.attacker_stacks or []) if (s.get("count") or 0) > 0],
            [s for s in (request.defender_stacks or []) if (s.get("count") or 0) > 0],
        )
        territory_def = td.get(territory_id_for_sim)
        if att_units and def_units and territory_def is not None:
            attacker_all_stealth = all(
                (lambda d: d and ("stealth" in (getattr(d, "specials", []) or []) or "stealth" in (getattr(d, "tags", []) or [])))(ud.get(u.unit_id))
                for u in att_units
            )
            archer_ok = (
                not attacker_all_stealth
                and any(has_unit_special(ud.get(u.unit_id), "archer") for u in def_units)
            )
            spec_result = compute_battle_specials_and_modifiers(
                att_units,
                def_units,
                territory_def,
                ud,
                is_sea_raid=is_sea_raid,
                archer_prefire_applicable=archer_ok,
                stealth_prefire_applicable=attacker_all_stealth,
            )
            battle_context = _build_battle_context(att_units, def_units, spec_result, ud, territory_def)
    except Exception:
        battle_context = None

    res = run_simulation(
        request.attacker_stacks,
        request.defender_stacks,
        territory_id_for_sim,
        ud,
        td,
        n_trials=n_trials,
        options=opts,
        seed=request.seed,
        return_outcomes=request.include_outcomes,
    )
    total_att_cas = sum(res.attacker_casualties_mean.values())
    total_def_cas = sum(res.defender_casualties_mean.values())
    return SimulateCombatResponse(
        n_trials=res.n_trials,
        attacker_wins=res.attacker_wins,
        defender_wins=res.defender_wins,
        attacker_survives=res.attacker_survives,
        defender_survives=res.defender_survives,
        retreats=res.retreats,
        conquers=res.conquers,
        p_attacker_win=res.p_attacker_win,
        p_defender_win=res.p_defender_win,
        p_attacker_survives=res.p_attacker_survives,
        p_defender_survives=res.p_defender_survives,
        p_retreat=res.p_retreat,
        p_conquer=res.p_conquer,
        rounds_mean=res.rounds_mean,
        rounds_p50=res.rounds_p50,
        rounds_p90=res.rounds_p90,
        attacker_casualties_mean=res.attacker_casualties_mean,
        defender_casualties_mean=res.defender_casualties_mean,
        attacker_casualties_total_mean=total_att_cas,
        defender_casualties_total_mean=total_def_cas,
        attacker_casualties_p90=res.attacker_casualties_p90,
        defender_casualties_p90=res.defender_casualties_p90,
        attacker_prefire_hits_mean=float(res.attacker_prefire_hits_mean) if res.attacker_prefire_hits_mean is not None else None,
        defender_prefire_hits_mean=float(res.defender_prefire_hits_mean) if res.defender_prefire_hits_mean is not None else None,
        attacker_siegework_hits_mean=float(res.attacker_siegework_hits_mean) if res.attacker_siegework_hits_mean is not None else None,
        defender_siegework_hits_mean=float(res.defender_siegework_hits_mean) if res.defender_siegework_hits_mean is not None else None,
        attacker_casualty_cost_mean=float(res.attacker_casualty_cost_mean),
        defender_casualty_cost_mean=float(res.defender_casualty_cost_mean),
        attacker_casualty_cost_variance_category=res.attacker_casualty_cost_variance_category,
        defender_casualty_cost_variance_category=res.defender_casualty_cost_variance_category,
        percentile_outcomes=[
            SimulateCombatPercentileOutcome(
                percentile=po.percentile,
                winner=po.winner,
                conquered=po.conquered,
                retreat=po.retreat,
                attacker_casualties=po.attacker_casualties,
                defender_casualties=po.defender_casualties,
            )
            for po in res.percentile_outcomes
        ],
        battle_context=battle_context,
        outcomes=res.outcomes,
        prefire_penalty=prefire_penalty_on,
    )


@app.post("/games")
def create_game_legacy(request: NewGameRequest):
    """Create a new game (legacy: in-memory only, no auth). For dev / backward compat."""
    if request.game_id in games:
        raise HTTPException(
            status_code=400, detail=f"Game {request.game_id} already exists")
    state = initialize_game_state(
        faction_defs=faction_defs,
        territory_defs=territory_defs,
        unit_defs=unit_defs,
        starting_setup=starting_setup,
        camp_defs=camp_defs,
    )
    state.map_asset = request.map_asset if request.map_asset is not None else "test_map"
    games[request.game_id] = state
    game_defs[request.game_id] = (unit_defs, territory_defs, faction_defs, camp_defs, port_defs)
    return {
        "game_id": request.game_id,
        "state": state_for_response(state, request.game_id, None),
    }


@app.get("/games/{game_id}")
def get_game_state(
    game_id: str,
    db: Session = Depends(get_db),
    player: Player | None = Depends(get_current_player_optional),
):
    """Get current game state (from cache or DB). Includes this game's definitions snapshot when present. can_act is true only if the authenticated player is assigned to the current faction."""
    state = get_game(game_id, db)
    ud, td, fd, cd, port_d = get_game_definitions(game_id, db)
    can_act = _player_can_act(game_id, player, db) if player else False
    state_dict = state_for_response(state, game_id, db)
    turn_order = state_dict.get("turn_order") if isinstance(state_dict.get("turn_order"), list) else None
    pending_camps = state_dict.get("pending_camps") if isinstance(state_dict.get("pending_camps"), list) else getattr(state, "pending_camps", [])
    definitions = {
        "units": _safe_asdict_map(ud),
        "territories": _safe_asdict_map(td),
        "factions": _safe_asdict_map(fd),
        "camps": _safe_asdict_map(cd),
        "ports": _safe_asdict_map(port_d),
    }
    row = db.query(GameModel).filter(GameModel.id == game_id).first()
    if row and row.config:
        try:
            config = json.loads(row.config) if isinstance(row.config, str) else row.config
            defs_snapshot = config.get("definitions") or {}
            definitions["specials"] = defs_snapshot.get("specials", {})
            definitions["specials_order"] = defs_snapshot.get("specials_order", [])
        except (TypeError, json.JSONDecodeError):
            definitions["specials"] = {}
            definitions["specials_order"] = []
    else:
        definitions["specials"] = {}
        definitions["specials_order"] = []
    setup_id: str | None = None
    event_log: list = []
    if row and row.config:
        try:
            config = json.loads(row.config) if isinstance(row.config, str) else row.config
            if isinstance(config, dict):
                setup_id = config.get("setup_id") if isinstance(config.get("setup_id"), str) else None
                el = config.get("event_log")
                if isinstance(el, list):
                    event_log = el
        except (TypeError, json.JSONDecodeError):
            pass
    return {
        "game_id": game_id,
        "state": state_dict,
        "turn_order": turn_order,
        "pending_camps": pending_camps,
        "definitions": definitions,
        "can_act": can_act,
        "setup_id": setup_id,
        "event_log": event_log,
    }


@app.get("/games/{game_id}/debug")
def get_game_debug(game_id: str, db: Session = Depends(get_db)):
    """Return raw map_asset from DB and DB file path (for verifying script vs API use same DB). No auth required for debugging."""
    row = db.query(GameModel).filter(GameModel.id == game_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Game not found")
    try:
        raw = json.loads(row.game_state) if isinstance(row.game_state, str) else row.game_state
        map_asset = raw.get("map_asset") if isinstance(raw, dict) else None
    except (TypeError, json.JSONDecodeError):
        map_asset = None
    return {
        "game_id": game_id,
        "map_asset_in_db": map_asset,
        "db_file": get_db_file_path(),
    }


def _get_lobby_claims_from_config(row) -> dict[str, str]:
    """Extract lobby_claims from game config (faction_id -> player_id)."""
    if not getattr(row, "config", None):
        return {}
    try:
        config = json.loads(row.config) if isinstance(row.config, str) else row.config
        claims = config.get("lobby_claims")
        if isinstance(claims, dict):
            return {str(k): str(v) for k, v in claims.items()}
    except (TypeError, json.JSONDecodeError):
        pass
    return {}


def _get_ai_factions_from_config(row) -> list[str]:
    """Extract ai_factions from game config. Returns list of faction IDs controlled by AI."""
    if not getattr(row, "config", None):
        return []
    try:
        config = json.loads(row.config) if isinstance(row.config, str) else row.config
        ai = config.get("ai_factions")
        if isinstance(ai, list):
            return [str(x) for x in ai if x]
    except (TypeError, json.JSONDecodeError):
        pass
    return []


@app.get("/games/{game_id}/meta")
def get_game_meta(
    game_id: str,
    db: Session = Depends(get_db),
    player: Player | None = Depends(get_current_player_optional),
):
    """Get game metadata (name, status, players, created_by, lobby_claims, player_usernames, scenario, forfeited_player_ids, host_forfeited, is_host) for lobby etc."""
    row = db.query(GameModel).filter(GameModel.id == game_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Game not found")
    try:
        players_list = json.loads(row.players)
    except (json.JSONDecodeError, TypeError):
        players_list = []
    lobby_claims = _get_lobby_claims_from_config(row)
    config = {}
    if row.config:
        try:
            config = json.loads(row.config) if isinstance(row.config, str) else row.config
            if not isinstance(config, dict):
                config = {}
        except (TypeError, json.JSONDecodeError):
            config = {}
    forfeited_player_ids = config.get("forfeited_player_ids")
    if not isinstance(forfeited_player_ids, list):
        forfeited_player_ids = []
    host_forfeited = config.get("host_forfeited") is True
    player_ids = set()
    for p in players_list:
        pid = p.get("player_id")
        if pid:
            player_ids.add(str(pid))
    for pid in lobby_claims.values():
        player_ids.add(str(pid))
    for pid in forfeited_player_ids:
        player_ids.add(str(pid))
    players_by_id = {}
    if player_ids:
        for p_row in db.query(Player).filter(Player.id.in_(list(player_ids))).all():
            players_by_id[str(p_row.id)] = p_row.username
    scenario = _get_scenario_from_config(row, db)
    ai_factions = _get_ai_factions_from_config(row)
    is_host = None
    if player is not None and row.created_by is not None:
        is_host = str(player.id) == str(row.created_by)
    out = {
        "id": row.id,
        "name": row.name,
        "game_code": row.game_code,
        "is_multiplayer": row.game_code is not None,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "created_by": str(row.created_by) if row.created_by else None,
        "players": players_list,
        "lobby_claims": lobby_claims,
        "player_usernames": players_by_id,
        "scenario": scenario,
        "ai_factions": ai_factions,
        "forfeited_player_ids": forfeited_player_ids,
        "host_forfeited": host_forfeited,
    }
    if is_host is not None:
        out["is_host"] = is_host
    return out


@app.post("/games/{game_id}/claim-faction")
def claim_faction(
    game_id: str,
    request: ClaimFactionRequest,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Claim or unclaim a faction in the lobby. One alliance per player."""
    row = db.query(GameModel).filter(GameModel.id == game_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Game not found")
    if row.status != "lobby":
        raise HTTPException(status_code=400, detail="Game already started")
    try:
        players_list = json.loads(row.players)
    except (TypeError, json.JSONDecodeError):
        players_list = []
    if not any(str(p.get("player_id")) == str(player.id) for p in players_list):
        raise HTTPException(status_code=403, detail="Not in this game")
    fid = (request.faction_id or "").strip()
    if not fid:
        raise HTTPException(status_code=400, detail="faction_id required")
    _, _, fd, _, _ = get_game_definitions(game_id, db)
    faction_def = fd.get(fid) if isinstance(fd, dict) else None
    if not faction_def:
        raise HTTPException(status_code=400, detail="Unknown faction")
    alliance = getattr(faction_def, "alliance", None) or "neutral"
    config = json.loads(row.config) if isinstance(row.config, str) else {}
    if not isinstance(config, dict):
        config = {}
    lobby_claims = config.get("lobby_claims")
    if not isinstance(lobby_claims, dict):
        lobby_claims = {}
    lobby_claims = dict(lobby_claims)
    player_id_str = str(player.id)
    if request.claim:
        if lobby_claims.get(fid) and lobby_claims.get(fid) != player_id_str:
            raise HTTPException(status_code=400, detail="Faction already claimed by another player")
        is_single_player = row.game_code is None
        if not is_single_player:
            my_claimed = [f for f, pid in lobby_claims.items() if pid == player_id_str]
            for other_fid in my_claimed:
                other_def = fd.get(other_fid) if isinstance(fd, dict) else None
                other_alliance = getattr(other_def, "alliance", None) if other_def else "neutral"
                if other_alliance != alliance:
                    raise HTTPException(
                        status_code=400,
                        detail="You can only claim factions from one alliance",
                    )
        lobby_claims[fid] = player_id_str
    else:
        if lobby_claims.get(fid) != player_id_str:
            raise HTTPException(status_code=400, detail="You have not claimed this faction")
        del lobby_claims[fid]
    config["lobby_claims"] = lobby_claims
    row.config = json.dumps(config)
    db.commit()
    return {"lobby_claims": lobby_claims}


@app.post("/games/{game_id}/start")
def start_game(
    game_id: str,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Start the game (host only). Lobby claims become player–faction assignments."""
    row = db.query(GameModel).filter(GameModel.id == game_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Game not found")
    if row.status != "lobby":
        raise HTTPException(status_code=400, detail="Game already started")
    if str(row.created_by) != str(player.id):
        raise HTTPException(status_code=403, detail="Only the host can start the game")
    lobby_claims = _get_lobby_claims_from_config(row)
    try:
        state_dict = json.loads(row.game_state) if isinstance(row.game_state, str) else {}
        turn_order = (state_dict or {}).get("turn_order") or []
    except (TypeError, json.JSONDecodeError):
        turn_order = []
    if not isinstance(turn_order, list):
        turn_order = []
    is_single_player = row.game_code is None
    if is_single_player:
        # Unclaimed = computer; at least one faction must be claimed (human) to have someone to play
        claimed = [fid for fid in turn_order if fid and lobby_claims.get(fid)]
        if not claimed:
            raise HTTPException(
                status_code=400,
                detail="Assign yourself to at least one faction to start.",
            )
    else:
        unclaimed = [fid for fid in turn_order if fid and not lobby_claims.get(fid)]
        if unclaimed:
            raise HTTPException(
                status_code=400,
                detail="All factions must be claimed before starting the game.",
            )
    players_list = [{"player_id": pid, "faction_id": fid} for fid, pid in lobby_claims.items()]
    row.players = json.dumps(players_list)
    row.status = "active"
    config = json.loads(row.config) if isinstance(row.config, str) else {}
    if isinstance(config, dict):
        if is_single_player:
            config["ai_factions"] = [fid for fid in turn_order if fid and not lobby_claims.get(fid)]
        config.pop("lobby_claims", None)
        row.config = json.dumps(config)
    db.commit()
    if game_id in games:
        del games[game_id]
    if game_id in game_defs:
        del game_defs[game_id]
    return {"message": "Game started", "status": "active"}


@app.post("/games/{game_id}/forfeit")
def forfeit_game(
    game_id: str,
    request: Request,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Remove yourself from the game. Your faction(s) will be auto-skipped; game stays for others. Forfeited games disappear from your list."""
    row = db.query(GameModel).filter(GameModel.id == game_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Game not found")
    try:
        players_list = json.loads(row.players)
    except (TypeError, json.JSONDecodeError):
        players_list = []
    player_id_str = str(player.id)
    if not any(str(p.get("player_id")) == player_id_str for p in players_list):
        raise HTTPException(status_code=403, detail="Not in this game")
    config = json.loads(row.config) if isinstance(row.config, str) else {}
    if not isinstance(config, dict):
        config = {}
    forfeited = config.get("forfeited_player_ids")
    if not isinstance(forfeited, list):
        forfeited = []
    if player_id_str not in forfeited:
        forfeited = list(forfeited) + [player_id_str]
    config["forfeited_player_ids"] = forfeited
    if row.status == "lobby":
        new_players = [p for p in players_list if str(p.get("player_id")) != player_id_str]
        lobby_claims = config.get("lobby_claims") or {}
        if isinstance(lobby_claims, dict):
            lobby_claims = {f: pid for f, pid in lobby_claims.items() if pid != player_id_str}
            config["lobby_claims"] = lobby_claims
    else:
        new_players = [p for p in players_list if str(p.get("player_id")) != player_id_str]
        # If the forfeiting player is currently up, call skip-turn until current faction is not forfeited
        try:
            raw = json.loads(row.game_state) if isinstance(row.game_state, str) else row.game_state
            if isinstance(raw, dict):
                state = GameState.from_dict(raw)
                faction_to_player = {
                    str(p["faction_id"]): str(p["player_id"])
                    for p in players_list
                    if p.get("faction_id") is not None and p.get("player_id") is not None
                }
                forfeited_set = set(forfeited)
                _, _, fd, _, _ = get_game_definitions(game_id, db)
                faction_ids = state.turn_order if state.turn_order else (sorted(fd.keys()) if fd else [])
                max_skips = len(faction_ids) if faction_ids else 1
                auth = request.headers.get("Authorization") or request.headers.get("authorization")
                headers = {"Authorization": auth} if auth else {}
                with TestClient(app) as client:
                    for _ in range(max_skips):
                        owner = faction_to_player.get(state.current_faction)
                        if owner not in forfeited_set:
                            break
                        r = client.post(f"/games/{game_id}/skip-turn", headers=headers)
                        if r.status_code != 200:
                            break
                        db.refresh(row)
                        raw = json.loads(row.game_state) if isinstance(row.game_state, str) else {}
                        if isinstance(raw, dict):
                            state = GameState.from_dict(raw)
                        else:
                            break
        except Exception:
            pass
    row.players = json.dumps(new_players)
    # If host forfeited, promote first remaining player to host (by turn order in lobby, else first in list)
    if str(row.created_by) == player_id_str and new_players:
        config["host_forfeited"] = True
        try:
            state_dict = json.loads(row.game_state) if isinstance(row.game_state, str) else {}
            turn_order = (state_dict or {}).get("turn_order") or []
        except (TypeError, json.JSONDecodeError):
            turn_order = []
        remaining_ids = list(dict.fromkeys(str(p.get("player_id")) for p in new_players if p.get("player_id")))
        new_host = None
        if isinstance(turn_order, list) and turn_order and row.status == "lobby":
            lobby_claims_after = config.get("lobby_claims") or {}
            for fid in turn_order:
                pid = lobby_claims_after.get(fid)
                if pid and pid in remaining_ids:
                    new_host = pid
                    break
        if new_host is None and remaining_ids:
            new_host = remaining_ids[0]
        if new_host:
            row.created_by = new_host
    row.config = json.dumps(config)
    db.commit()
    if game_id in games:
        del games[game_id]
    if game_id in game_defs:
        del game_defs[game_id]
    return {"message": "You have left the game"}


@app.delete("/games/{game_id}")
def delete_game(
    game_id: str,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Delete a game from DB and cache. Only the host (creator) can delete."""
    row = db.query(GameModel).filter(GameModel.id == game_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Game not found")
    if str(row.created_by) != str(player.id):
        raise HTTPException(status_code=403, detail="Only the host can delete the game")
    try:
        players_list = json.loads(row.players)
    except (TypeError, json.JSONDecodeError):
        players_list = []
    db.delete(row)
    db.commit()
    if game_id in games:
        del games[game_id]
    if game_id in game_defs:
        del game_defs[game_id]
    return {"message": f"Game {game_id} deleted"}


def _build_available_actions(state: GameState, game_id: str, db: Session | None = None) -> dict[str, Any]:
    """Build available-actions dict using this game's definitions. Catches so caller never gets 500."""
    ud, td, fd, cd, port_d = get_game_definitions(game_id, db)
    try:
        faction = state.current_faction or ""
        phase = state.phase or "purchase"
        actions: dict[str, Any] = {
            "faction": faction,
            "phase": phase,
            "can_end_phase": True,
        }
        if phase == "purchase":
            purchasable = get_purchasable_units(state, faction, ud)
            actions["purchasable_units"] = purchasable
            capacity_info = get_mobilization_capacity(state, faction, td, cd, port_d, ud)
            actions["mobilization_capacity"] = capacity_info.get("total_capacity", 0)
            territories_list = capacity_info.get("territories", [])
            camp_land_only = sum(t.get("power", 0) for t in territories_list)
            home_slots = sum(1 for t in territories_list if t.get("home_unit_capacity"))
            land_cap = camp_land_only + home_slots
            port_cap = sum(p.get("power", 0) for p in capacity_info.get("port_territories", []))
            # Land can only mobilize to camps or home; ports mobilize only naval to adjacent sea zones
            actions["mobilization_land_capacity"] = land_cap
            actions["mobilization_camp_land_capacity"] = camp_land_only
            actions["mobilization_sea_capacity"] = port_cap
            # Expose sea_zones so frontend can show Sea tab in purchase modal (faction has a port)
            sea_zone_list = [z["sea_zone_id"] for z in capacity_info.get("sea_zones", [])]
            actions["mobilize_options"] = {"sea_zones": sea_zone_list}
            already_purchased = sum(
                s.count for s in (state.faction_purchased_units or {}).get(faction, [])
            )
            actions["purchased_units_count"] = already_purchased
            actions["camp_cost"] = getattr(state, "camp_cost", 0)
            actions["stronghold_repair_cost"] = getattr(state, "stronghold_repair_cost", 0)
        elif phase in ("combat_move", "non_combat_move"):
            movable = get_movable_units(state, faction, ud)
            actions["moveable_units"] = []
            for unit_info in movable:
                targets, charge_routes = get_unit_move_targets(
                    state, unit_info["instance_id"], ud, td, fd
                )
                actions["moveable_units"].append({
                    "territory": unit_info["territory_id"],
                    "unit": unit_info,
                    "destinations": targets,
                    "charge_routes": charge_routes,
                })
            if phase == "combat_move":
                # Use state after applying pending combat moves so boats that will receive a load (from a pending load move) are included
                state_after_combat_moves = get_state_after_pending_moves(state, "combat_move", ud, td, fd)
                loaded_boat_ids = set(getattr(state_after_combat_moves, "loaded_naval_must_attack_instance_ids", []))
                pending_combat = [pm for pm in (state.pending_moves or []) if getattr(pm, "phase", None) == "combat_move"]
                # Boats that have declared attack: in a pending move from sea to land or from sea to enemy sea
                boat_ids_declared_attack: set[str] = set()
                current_faction = state.current_faction or ""
                current_fd = fd.get(current_faction)
                for pm in pending_combat:
                    from_id = getattr(pm, "from_territory", "")
                    to_id = getattr(pm, "to_territory", "")
                    if not _is_sea_zone(td.get(from_id)):
                        continue
                    to_land = not _is_sea_zone(td.get(to_id))
                    to_territory = state.territories.get(to_id) if to_id else None
                    to_enemy_sea = (
                        _is_sea_zone(td.get(to_id))
                        and to_territory
                        and any(
                            get_unit_faction(u, ud) != current_faction
                            and (not current_fd or not fd.get(get_unit_faction(u, ud)) or fd.get(get_unit_faction(u, ud)).alliance != current_fd.alliance)
                            for u in to_territory.units
                        )
                    )
                    if to_land or to_enemy_sea:
                        from_territory = state.territories.get(from_id)
                        if from_territory:
                            units_by_iid = {u.instance_id: u for u in from_territory.units}
                            for iid in getattr(pm, "unit_instance_ids", []) or []:
                                u = units_by_iid.get(iid)
                                if u and _is_naval_unit(ud.get(u.unit_id)):
                                    boat_ids_declared_attack.add(iid)
                effective_boat_ids = loaded_boat_ids - boat_ids_declared_attack
                actions["loaded_naval_must_attack_instance_ids"] = list(effective_boat_ids)
                forced_naval_ids = get_forced_naval_combat_instance_ids(
                    state_after_combat_moves, faction, ud, td, fd
                )
                actions["forced_naval_combat_instance_ids"] = forced_naval_ids
                standoff_seas: set[str] = set()
                for tid, terr in state_after_combat_moves.territories.items():
                    tkey = resolve_territory_key_in_state(state_after_combat_moves, tid, td)
                    tdef = td.get(tkey) or td.get(tid)
                    if not tdef or not _is_sea_zone(tdef):
                        continue
                    if any(u.instance_id in forced_naval_ids for u in terr.units):
                        standoff_seas.add(tkey)
                actions["forced_naval_standoff_sea_zone_ids"] = sorted(standoff_seas)
                # Allow end phase once every boat that must attack has declared (pending load moves will apply on end_phase)
                actions["can_end_phase"] = len(effective_boat_ids) == 0
            if phase == "non_combat_move":
                aerial_must_move = get_aerial_units_must_move(state, ud, td, fd, faction)
                actions["aerial_units_must_move"] = aerial_must_move
                # Can end phase only if, after applying pending moves, no aerial is left in enemy territory
                state_after_moves = get_state_after_pending_moves(state, "non_combat_move", ud, td, fd)
                aerial_still_stuck = get_aerial_units_must_move(state_after_moves, ud, td, fd, faction)
                actions["can_end_phase"] = len(aerial_still_stuck) == 0
        elif phase == "combat":
            combat_territories = get_contested_territories(state, faction, fd, ud, td)
            actions["combat_territories"] = combat_territories
            actions["sea_raid_targets"] = get_sea_raid_targets(state, faction, fd, ud, td)
            if state.active_combat:
                ac_dict = state.active_combat.to_dict()
                attackers_ac, defenders_ac = _get_active_combat_units(state, td, ud)
                combat_tid = getattr(state.active_combat, "territory_id", "") or ""
                tdef_combat = td.get(combat_tid)
                def_stronghold = bool(tdef_combat and getattr(tdef_combat, "is_stronghold", False))
                terr_ac = state.territories.get(combat_tid)
                def_sh_hp_ac = _combat_territory_stronghold_hp(terr_ac, tdef_combat)
                fuse_ac = getattr(state.active_combat, "fuse_bomb", True)
                if not isinstance(fuse_ac, bool):
                    fuse_ac = True
                att_sw_dice, def_sw_dice = get_siegework_dice_counts(
                    attackers_ac, defenders_ac, ud, def_stronghold,
                    defender_stronghold_hp=def_sh_hp_ac,
                    fuse_bomb=fuse_ac,
                )
                has_ladders_ac = any(
                    combat_is_siegework_unit(ud.get(u.unit_id))
                    and combat_has_special(ud.get(u.unit_id), "ladder")
                    for u in attackers_ac
                )
                combat_log_list = ac_dict.get("combat_log") or []
                siegeworks_pending = (
                    ac_dict.get("round_number", 0) == 0
                    and not any((r.get("is_siegeworks_round") if isinstance(r, dict) else getattr(r, "is_siegeworks_round", False)) for r in combat_log_list)
                    and (att_sw_dice > 0 or def_sw_dice > 0 or has_ladders_ac)
                )
                archer_prefire_pending = (
                    ac_dict.get("round_number", 0) == 0
                    and not siegeworks_pending
                    and not any((r.get("is_archer_prefire") if isinstance(r, dict) else getattr(r, "is_archer_prefire", False)) for r in combat_log_list)
                    and not any((r.get("is_stealth_prefire") if isinstance(r, dict) else getattr(r, "is_stealth_prefire", False)) for r in combat_log_list)
                    and any(has_unit_special(ud.get(u.unit_id), "archer") for u in defenders_ac if ud.get(u.unit_id))
                )
                ac_dict["combat_siegeworks_pending"] = siegeworks_pending
                ac_dict["combat_archer_prefire_pending"] = archer_prefire_pending
                ac_dict["combat_siegeworks_dice"] = {"attacker": att_sw_dice, "defender": def_sw_dice}
                _enrich_active_combat_siegework_display_ids(ac_dict, state, ud, td)
                actions["active_combat"] = ac_dict
                retreat_destinations = get_retreat_options(state, td, fd, ud)
                actions["retreat_options"] = {
                    "can_retreat": len(retreat_destinations) > 0,
                    "valid_destinations": retreat_destinations,
                }
        elif phase == "mobilization":
            mobilize_territories = get_mobilization_territories(state, faction, td, cd, port_d, ud)
            mobilize_sea_zones = get_mobilization_sea_zones(state, faction, td, port_d)
            mobilize_capacity = get_mobilization_capacity(state, faction, td, cd, port_d, ud)
            purchased = get_purchased_units(state, faction)
            actions["mobilize_options"] = {
                "territories": mobilize_territories,
                "sea_zones": mobilize_sea_zones,
                "capacity": mobilize_capacity,
                "pending_units": purchased,
            }
            # Expose pending_camps so frontend can show placement UI even if main state was missing it
            actions["pending_camps"] = getattr(state, "pending_camps", [])
            actions["can_end_turn"] = True
        return actions
    except Exception as e:
        fallback = {
            "faction": getattr(state, "current_faction", "") or "",
            "phase": getattr(state, "phase", "purchase") or "purchase",
            "can_end_phase": True,
            "purchasable_units": [],
            "mobilization_capacity": 0,
            "purchased_units_count": 0,
            "camp_cost": 0,
        }
        if getattr(state, "phase", None) == "mobilization":
            fallback["mobilize_options"] = {
                "territories": [],
                "sea_zones": [],
                "capacity": {"total_capacity": 0, "territories": [], "sea_zones": []},
                "pending_units": [],
            }
        # Log so we fix the root cause instead of relying on fallback shape
        import logging
        logging.getLogger(__name__).exception("available_actions failed: %s", e)
        return fallback


@app.get("/games/{game_id}/available-actions")
def get_available_actions(game_id: str, db: Session = Depends(get_db)):
    """Get available actions for current faction in current phase."""
    state = get_game(game_id, db)
    return _build_available_actions(state, game_id, db)


@app.post("/games/{game_id}/purchase")
def do_purchase(
    game_id: str,
    request: PurchaseRequest,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Purchase units. Only the player assigned to the current faction can act."""
    _require_can_act(game_id, player, db)
    state = get_game(game_id, db)
    ud, td, fd, cd, port_d = get_game_definitions(game_id, db)
    action = purchase_units(state.current_faction, request.purchases)
    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(state, action, ud, td, fd, cd, port_d)
    save_game(game_id, new_state, db, events)
    return {
        "state": state_for_response(new_state, game_id, db),
        "events": [e.to_dict() for e in events],
        "can_act": _player_can_act(game_id, player, db),
    }


@app.post("/games/{game_id}/purchase-camp")
def do_purchase_camp(
    game_id: str,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Purchase one camp (cost from setup). Only in purchase phase."""
    _require_can_act(game_id, player, db)
    state = get_game(game_id, db)
    ud, td, fd, cd, port_d = get_game_definitions(game_id, db)
    action = purchase_camp(state.current_faction)
    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(state, action, ud, td, fd, cd, port_d)
    save_game(game_id, new_state, db, events)
    return {
        "state": state_for_response(new_state, game_id, db),
        "events": [e.to_dict() for e in events],
        "can_act": _player_can_act(game_id, player, db),
    }


@app.post("/games/{game_id}/repair-stronghold")
def do_repair_stronghold(
    game_id: str,
    request: RepairStrongholdRequest,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Purchase stronghold repairs (power per HP from setup). Only in purchase phase. Does not count toward mobilization."""
    _require_can_act(game_id, player, db)
    state = get_game(game_id, db)
    ud, td, fd, cd, port_d = get_game_definitions(game_id, db)
    action = repair_stronghold(state.current_faction, request.repairs)
    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(state, action, ud, td, fd, cd, port_d)
    save_game(game_id, new_state, db, events)
    return {
        "state": state_for_response(new_state, game_id, db),
        "events": [e.to_dict() for e in events],
        "can_act": _player_can_act(game_id, player, db),
    }


@app.post("/games/{game_id}/move")
def do_move(
    game_id: str,
    request: MoveRequest,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Move units. Only the player assigned to the current faction can act."""
    _require_can_act(game_id, player, db)
    to_territory = (request.to_territory or "").strip()
    from_territory = (request.from_territory or "").strip()
    if not to_territory:
        raise HTTPException(status_code=400, detail="No destination specified")
    if not from_territory:
        raise HTTPException(status_code=400, detail="No origin specified")
    state = get_game(game_id, db)
    ud, td, fd, cd, port_d = get_game_definitions(game_id, db)
    from_territory = resolve_territory_key_in_state(state, from_territory, td)
    to_territory = resolve_territory_key_in_state(state, to_territory, td)
    from_sea = _is_sea_zone(td.get(from_territory))
    to_sea = _is_sea_zone(td.get(to_territory))
    from_terr = state.territories.get(from_territory)
    req_ids = [str(x).strip() for x in (request.unit_instance_ids or []) if x is not None and str(x).strip()]
    moving_units: list = []
    if from_terr and req_ids:
        rs = set(req_ids)
        moving_units = [u for u in from_terr.units if u.instance_id in rs]
    sea_to_land_all_aerial = bool(
        ud
        and from_sea
        and not to_sea
        and moving_units
        and all(is_aerial_unit(ud.get(u.unit_id)) for u in moving_units)
    )

    move_type = None
    if not from_sea and to_sea:
        move_type = "load"
    elif from_sea and not to_sea:
        move_type = "aerial" if sea_to_land_all_aerial else "offload"
    elif from_sea and to_sea:
        move_type = "sail"
    else:
        if from_terr and req_ids and ud:
            rid_set = set(req_ids)
            any_aerial = any(
                is_aerial_unit(ud.get(u.unit_id)) for u in from_terr.units if u.instance_id in rid_set
            )
            move_type = "aerial" if any_aerial else "land"
        else:
            move_type = "land"

    # Sea -> land (offload): boat must end in a sea zone adjacent to the land. If boat is not already
    # in such a zone, we sail to one that is reachable and adjacent, then offload. If multiple such
    # zones exist, client must send offload_sea_zone_id (we return need_offload_sea_choice once).
    # Pure aerial stacks fly sea→land as move_type aerial, not naval offload.
    if from_sea and not to_sea and not sea_to_land_all_aerial:
        valid_offload = get_valid_offload_sea_zones(
            from_territory, to_territory, state, request.unit_instance_ids, ud, td, fd, state.phase
        )
        if not valid_offload:
            raise HTTPException(
                status_code=400,
                detail="No valid sea zone to offload to that land from your current position",
            )
        if len(valid_offload) > 1 and not request.offload_sea_zone_id:
            return {
                "need_offload_sea_choice": True,
                "valid_offload_sea_zones": valid_offload,
                "state": state_for_response(state, game_id, db),
                "can_act": _player_can_act(game_id, player, db),
            }
        if len(valid_offload) > 1 and request.offload_sea_zone_id:
            if request.offload_sea_zone_id not in valid_offload:
                raise HTTPException(status_code=400, detail="Invalid offload sea zone choice")
            offload_from_sea = request.offload_sea_zone_id
        else:
            offload_from_sea = from_territory if from_territory in valid_offload else valid_offload[0]

        if offload_from_sea != from_territory:
            # Sail to offload_from_sea, then offload to land (two pending moves).
            # Apply sail (adds sail to pending_moves; units still in from_territory). Validate offload
            # against state with sail applied, then append offload to pending_moves (reducer would fail
            # looking for units in offload_from_sea since they're still in from_territory).
            sail_action = move_units(
                state.current_faction,
                from_territory,
                offload_from_sea,
                request.unit_instance_ids,
                charge_through=None,
                move_type="sail",
                load_onto_boat_instance_id=None,
                sail_to_offload_land_territory_id=to_territory,
            )
            val_sail = validate_action(state, sail_action, ud, td, fd, cd, port_d)
            if not val_sail.valid:
                raise HTTPException(status_code=400, detail=val_sail.error)
            state_after_sail, events_sail = apply_action(state, sail_action, ud, td, fd, cd, port_d)
            # Simulate applying the sail so we can validate offload (units in offload_from_sea)
            state_simulated = get_state_after_pending_moves(
                state_after_sail, state.phase, ud, td, fd
            )
            offload_action = move_units(
                state.current_faction,
                offload_from_sea,
                to_territory,
                request.unit_instance_ids,
                charge_through=None,
                move_type="offload",
                load_onto_boat_instance_id=None,
            )
            val_offload = validate_action(state_simulated, offload_action, ud, td, fd, cd, port_d)
            if not val_offload.valid:
                raise HTTPException(status_code=400, detail=val_offload.error)
            sea_t = state_simulated.territories.get(offload_from_sea)
            primary_unit_id = ""
            if sea_t and request.unit_instance_ids:
                by_iid = {u.instance_id: u.unit_id for u in sea_t.units}
                primary_unit_id = str(by_iid.get(request.unit_instance_ids[0]) or "")
            offload_pending = PendingMove(
                from_territory=offload_from_sea,
                to_territory=to_territory,
                unit_instance_ids=request.unit_instance_ids,
                phase=state.phase,
                move_type="offload",
                primary_unit_id=primary_unit_id,
            )
            state_after_sail.pending_moves = list(state_after_sail.pending_moves) + [offload_pending]
            save_game(game_id, state_after_sail, db, events_sail)
            return {
                "state": state_for_response(state_after_sail, game_id, db),
                "events": [e.to_dict() for e in events_sail],
                "can_act": _player_can_act(game_id, player, db),
            }
        # Boat already in a valid adjacent sea zone; single offload move
        move_type = "offload"

    action = move_units(
        state.current_faction,
        from_territory,
        to_territory,
        request.unit_instance_ids,
        charge_through=request.charge_through,
        move_type=move_type,
        load_onto_boat_instance_id=request.load_onto_boat_instance_id,
        avoid_forced_naval_combat=bool(request.avoid_forced_naval_combat),
    )
    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(state, action, ud, td, fd, cd, port_d)
    save_game(game_id, new_state, db, events)
    return {
        "state": state_for_response(new_state, game_id, db),
        "events": [e.to_dict() for e in events],
        "can_act": _player_can_act(game_id, player, db),
    }


@app.post("/games/{game_id}/cancel-move")
def do_cancel_move(
    game_id: str,
    request: CancelMoveRequest,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Cancel a pending move. Only the player assigned to the current faction can act."""
    _require_can_act(game_id, player, db)
    state = get_game(game_id, db)
    ud, td, fd, cd, port_d = get_game_definitions(game_id, db)
    action = cancel_move(state.current_faction, request.move_index)
    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(state, action, ud, td, fd, cd, port_d)
    save_game(game_id, new_state, db, events)
    return {
        "state": state_for_response(new_state, game_id, db),
        "events": [e.to_dict() for e in events],
        "can_act": _player_can_act(game_id, player, db),
    }


@app.post("/games/{game_id}/cancel-mobilization")
def do_cancel_mobilization(
    game_id: str,
    request: CancelMobilizationRequest,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Cancel a pending mobilization. Only the player assigned to the current faction can act."""
    _require_can_act(game_id, player, db)
    state = get_game(game_id, db)
    ud, td, fd, cd, port_d = get_game_definitions(game_id, db)
    action = cancel_mobilization(state.current_faction, request.mobilization_index)
    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(state, action, ud, td, fd, cd, port_d)
    save_game(game_id, new_state, db, events)
    return {
        "state": state_for_response(new_state, game_id, db),
        "events": [e.to_dict() for e in events],
        "can_act": _player_can_act(game_id, player, db),
    }


@app.post("/games/{game_id}/place-camp")
def do_place_camp(
    game_id: str,
    request: PlaceCampRequest,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Place a purchased camp on a territory during mobilization (immediate). Prefer queue-camp-placement for planned placement at end of phase."""
    _require_can_act(game_id, player, db)
    state = get_game(game_id, db)
    ud, td, fd, cd, port_d = get_game_definitions(game_id, db)
    action = place_camp(state.current_faction, request.camp_index, request.territory_id)
    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(state, action, ud, td, fd, cd, port_d)
    save_game(game_id, new_state, db, events)
    return {
        "state": state_for_response(new_state, game_id, db),
        "events": [e.to_dict() for e in events],
        "can_act": _player_can_act(game_id, player, db),
    }


@app.post("/games/{game_id}/queue-camp-placement")
def do_queue_camp_placement(
    game_id: str,
    request: PlaceCampRequest,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Queue a camp placement (applied at end of mobilization phase, like unit mobilizations)."""
    _require_can_act(game_id, player, db)
    state = get_game(game_id, db)
    ud, td, fd, cd, port_d = get_game_definitions(game_id, db)
    action = queue_camp_placement(state.current_faction, request.camp_index, request.territory_id)
    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(state, action, ud, td, fd, cd, port_d)
    save_game(game_id, new_state, db, events)
    return {
        "state": state_for_response(new_state, game_id, db),
        "events": [e.to_dict() for e in events],
        "can_act": _player_can_act(game_id, player, db),
    }


@app.post("/games/{game_id}/cancel-camp-placement")
def do_cancel_camp_placement(
    game_id: str,
    request: CancelCampPlacementRequest,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Cancel a queued camp placement."""
    _require_can_act(game_id, player, db)
    state = get_game(game_id, db)
    ud, td, fd, cd, port_d = get_game_definitions(game_id, db)
    action = cancel_camp_placement(state.current_faction, request.placement_index)
    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(state, action, ud, td, fd, cd, port_d)
    save_game(game_id, new_state, db, events)
    return {
        "state": state_for_response(new_state, game_id, db),
        "events": [e.to_dict() for e in events],
        "can_act": _player_can_act(game_id, player, db),
    }


def _terror_rerolled_indices_by_stat(
    defenders: list,
    ud: dict,
    defender_mods: dict | None,
    num_rolls: int,
    flat_indices: list[int],
    exclude_archetypes: set[str] | None = None,
) -> dict[str, list[int]]:
    """Build defender_rerolled_indices_by_stat for terror UI: stat -> list of roll indices (within that stat row) that were re-rolled.
    flat_indices are in unit order (same as defender_rolls and get_terror_reroll_targets), so we must build
    flat_to_stat in the same unit order, not stat order."""
    stat_name = "defense"
    mods = defender_mods or {}
    skip = exclude_archetypes or set()
    # Map each flat index (unit order) to (stat_value, idx_within_that_stat_row)
    flat_to_stat: list[tuple[int, int]] = []
    count_per_stat: dict[int, int] = {}
    for u in defenders:
        unit_def = ud.get(u.unit_id)
        if not unit_def:
            continue
        if getattr(unit_def, "archetype", "") in skip:
            continue
        stat_value = getattr(unit_def, stat_name, 0) + mods.get(u.instance_id, 0)
        dice_count = getattr(unit_def, "dice", 1)
        for _ in range(dice_count):
            idx_in_stat = count_per_stat.get(stat_value, 0)
            if len(flat_to_stat) < num_rolls:
                flat_to_stat.append((stat_value, idx_in_stat))
            count_per_stat[stat_value] = idx_in_stat + 1
    rerolled: dict[str, list[int]] = {}
    for flat_idx in flat_indices:
        if flat_idx >= len(flat_to_stat):
            continue
        stat_value, idx_in_stat = flat_to_stat[flat_idx]
        key = str(stat_value)
        if key not in rerolled:
            rerolled[key] = []
        if idx_in_stat not in rerolled[key]:
            rerolled[key].append(idx_in_stat)
    for key in rerolled:
        rerolled[key].sort()
    return rerolled


def _generate_initiate_combat_payload(
    state: GameState,
    territory_id: str,
    sea_zone_id: str | None,
    ud: dict,
    td: dict,
    fd: dict,
    *,
    fuse_bomb: bool = True,
) -> dict[str, Any]:
    """
    Generate dice_rolls and terror fields for initiate_combat.
    Used by do_initiate_combat (HTTP) and do_ai_step (AI returns initiate_combat with empty dice).
    Returns dict with dice_rolls, terror_applied, terror_final_defender_hits (optional).
    """
    territory = state.territories.get(territory_id)
    if not territory:
        raise ValueError(f"Invalid territory: {territory_id}")
    attacker_faction = state.current_faction
    attacker_alliance = getattr(fd.get(attacker_faction), "alliance", None) if fd.get(attacker_faction) else None

    if sea_zone_id:
        sea_zone = state.territories.get(sea_zone_id)
        if not sea_zone or not _is_sea_zone(td.get(sea_zone_id)):
            raise ValueError(f"Invalid sea zone: {sea_zone_id}")
        attackers_from_sea = sorted(
            [
                u for u in sea_zone.units
                if ud.get(u.unit_id) and ud[u.unit_id].faction == attacker_faction
                and not combat_is_naval_unit(ud.get(u.unit_id))
            ],
            key=lambda u: u.instance_id,
        )
        if attackers_from_sea:
            attackers = attackers_from_sea
        else:
            attackers = sorted(
                [
                    u for u in territory.units
                    if ud.get(u.unit_id) and ud[u.unit_id].faction == attacker_faction
                    and not combat_is_naval_unit(ud.get(u.unit_id))
                ],
                key=lambda u: u.instance_id,
            )
        defenders = sorted(
            [
                u for u in territory.units
                if ud.get(u.unit_id)
                and ud[u.unit_id].faction != attacker_faction
                and (getattr(fd.get(ud[u.unit_id].faction), "alliance", None) if fd.get(ud[u.unit_id].faction) else None) != attacker_alliance
            ],
            key=lambda u: u.instance_id,
        )
        if not attackers:
            raise ValueError("Sea raid requires at least one land unit")
    else:
        is_sea_zone_combat = _is_sea_zone(td.get(territory_id))
        attackers = sorted(
            [
                u for u in territory.units
                if ud.get(u.unit_id) and ud[u.unit_id].faction == attacker_faction
                and (
                    not is_sea_zone_combat
                    or participates_in_sea_hex_naval_combat(u, ud.get(u.unit_id))
                )
            ],
            key=lambda u: u.instance_id,
        )
        defenders = sorted(
            [
                u for u in territory.units
                if ud.get(u.unit_id)
                and ud[u.unit_id].faction != attacker_faction
                and (getattr(fd.get(ud[u.unit_id].faction), "alliance", None) if fd.get(ud[u.unit_id].faction) else None) != attacker_alliance
                and (
                    not is_sea_zone_combat
                    or participates_in_sea_hex_naval_combat(u, ud.get(u.unit_id))
                )
            ],
            key=lambda u: u.instance_id,
        )
        if is_sea_zone_combat and (not attackers or not defenders):
            raise ValueError(
                "Sea zone combat requires at least one naval or aerial unit on each side"
            )

    terror_reroll_response: dict[str, Any] = {}
    all_attackers_have_stealth = (
        len(attackers) > 0
        and all(combat_has_special(ud.get(u.unit_id), "stealth") for u in attackers if ud.get(u.unit_id))
    )
    defender_has_archers = any(has_unit_special(ud.get(u.unit_id), "archer") for u in defenders if getattr(u, "unit_id", "") in ud)

    tdef_battle = td.get(territory_id)
    def_sh_battle = bool(tdef_battle and getattr(tdef_battle, "is_stronghold", False))
    def_sh_hp_battle = _combat_territory_stronghold_hp(territory, tdef_battle)
    sw_att_first, sw_def_first = get_siegework_dice_counts(
        attackers, defenders, ud, def_sh_battle, defender_stronghold_hp=def_sh_hp_battle,
        fuse_bomb=fuse_bomb,
    )
    has_lad_first = any(
        combat_is_siegework_unit(ud.get(u.unit_id)) and combat_has_special(ud.get(u.unit_id), "ladder")
        for u in attackers
    )
    siegeworks_needed_first = sw_att_first > 0 or sw_def_first > 0 or has_lad_first

    if all_attackers_have_stealth:
        att_effective_dice, _, _ = get_attacker_effective_dice_and_bombikazi_self_destruct(attackers, ud)
        dice_rolls = {
            "attacker": generate_dice_rolls_for_units(attackers, ud, effective_dice_override=att_effective_dice),
            "defender": [],
        }
    elif siegeworks_needed_first:
        att_rolling = get_siegework_attacker_rolling_units(
            attackers, ud, def_sh_battle, defender_stronghold_hp=def_sh_hp_battle,
            fuse_bomb=fuse_bomb,
        )
        def_sw = [u for u in defenders if combat_is_siegework_unit(ud.get(u.unit_id))]
        dice_rolls = {
            "attacker": generate_dice_rolls_for_units(att_rolling, ud),
            "defender": generate_dice_rolls_for_units(def_sw, ud),
        }
    elif defender_has_archers:
        defender_archer_units = sorted(
            [u for u in defenders if u.unit_id in ud and has_unit_special(ud[u.unit_id], "archer")],
            key=lambda u: u.instance_id,
        )
        dice_rolls = {
            "attacker": [],
            "defender": generate_dice_rolls_for_units(defender_archer_units, ud),
        }
    else:
        att_effective_dice, _, _ = get_attacker_effective_dice_and_bombikazi_self_destruct(attackers, ud)
        _sort_attackers_for_ladder_dice_if_needed(state, attackers, defenders, ud, td)
        dice_rolls = {
            "attacker": generate_dice_rolls_for_units(
                attackers, ud, effective_dice_override=att_effective_dice,
                exclude_archetypes=set(NORMAL_COMBAT_EXCLUDE_ARCHETYPES),
            ),
            "defender": generate_dice_rolls_for_units(
                defenders, ud, exclude_archetypes=set(NORMAL_COMBAT_EXCLUDE_ARCHETYPES),
            ),
        }
        territory_def = td.get(territory_id)
        terrain_att, terrain_def = compute_terrain_stat_modifiers(
            territory_def, attackers, defenders, ud
        )
        anticav_att, anticav_def = compute_anti_cavalry_stat_modifiers(
            attackers, defenders, ud
        )
        captain_att, captain_def = compute_captain_stat_modifiers(
            attackers, defenders, ud
        )
        attacker_mods = merge_stat_modifiers(terrain_att, anticav_att, captain_att)
        defender_mods = merge_stat_modifiers(terrain_def, anticav_def, captain_def)
        terror_count = sum(1 for u in attackers if combat_has_special(ud.get(u.unit_id), "terror"))
        hope_count = sum(1 for u in defenders if combat_has_special(ud.get(u.unit_id), "hope"))
        terror_cap = min(3, max(0, terror_count - hope_count))
        flat_indices, total_reroll_dice = get_terror_reroll_targets(
            attackers, defenders, ud, dice_rolls, defender_mods or None, terror_cap=terror_cap,
            exclude_archetypes_from_rolling=set(NORMAL_COMBAT_EXCLUDE_ARCHETYPES),
        )
        defender_hits_from_rolls = combat_count_hits(
            defenders, dice_rolls.get("defender", []), ud,
            is_attacker=False, stat_modifiers=defender_mods or None,
            exclude_archetypes=set(NORMAL_COMBAT_EXCLUDE_ARCHETYPES),
        )
        hit_flat_set = get_defender_hit_flat_indices(
            defenders, dice_rolls["defender"], ud, defender_mods or None,
            exclude_archetypes=set(NORMAL_COMBAT_EXCLUDE_ARCHETYPES),
        )
        flat_indices = [i for i in flat_indices if i in hit_flat_set][:defender_hits_from_rolls]
        total_reroll_dice = len(flat_indices)
        if flat_indices and total_reroll_dice > 0 and defender_hits_from_rolls > 0:
            defender_dice_initial_grouped = group_dice_by_stat(
                defenders, dice_rolls["defender"], ud,
                is_attacker=False, stat_modifiers=defender_mods or None,
                exclude_archetypes_from_rolling=set(NORMAL_COMBAT_EXCLUDE_ARCHETYPES),
            )
            new_reroll_values = roll_dice(total_reroll_dice)
            defender_rolls = list(dice_rolls["defender"])
            initial_len = len(defender_rolls)
            eff_def_per_idx = get_eff_def_per_flat_index(
                defenders, ud, defender_mods or None,
                exclude_archetypes=set(NORMAL_COMBAT_EXCLUDE_ARCHETYPES),
            )
            hits_from_rerolls = sum(
                1
                for j in range(len(flat_indices))
                if flat_indices[j] < len(eff_def_per_idx)
                and new_reroll_values[j] <= eff_def_per_idx[flat_indices[j]]
            )
            terror_final_defender_hits = (
                defender_hits_from_rolls - total_reroll_dice + hits_from_rerolls
            )
            for i, flat_idx in enumerate(flat_indices):
                if flat_idx < initial_len:
                    defender_rolls[flat_idx] = new_reroll_values[i]
            dice_rolls["defender"] = defender_rolls
            terror_reroll_response = {
                "applied": True,
                "terror_final_defender_hits": terror_final_defender_hits,
                "terror_reroll_count": total_reroll_dice,
            }

    result: dict[str, Any] = {"dice_rolls": dice_rolls, "terror_applied": bool(terror_reroll_response)}
    if terror_reroll_response.get("terror_final_defender_hits") is not None:
        result["terror_final_defender_hits"] = terror_reroll_response["terror_final_defender_hits"]
    if terror_reroll_response.get("terror_reroll_count") is not None:
        result["terror_reroll_count"] = terror_reroll_response["terror_reroll_count"]
    return result


@app.post("/games/{game_id}/combat/initiate")
def do_initiate_combat(
    game_id: str,
    request: CombatRequest,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Initiate combat in a territory. Only the player assigned to the current faction can act."""
    _require_can_act(game_id, player, db)
    state = get_game(game_id, db)
    ud, td, fd, cd, port_d = get_game_definitions(game_id, db)

    territory = state.territories.get(request.territory_id)
    if not territory:
        raise HTTPException(status_code=400, detail="Invalid territory")

    if request.sea_zone_id:
        sea_zone = state.territories.get(request.sea_zone_id)
        if not sea_zone or not _is_sea_zone(td.get(request.sea_zone_id)):
            raise HTTPException(status_code=400, detail="Invalid sea zone for sea raid")
        sea_raid_from = getattr(state, "territory_sea_raid_from", None) or {}
        if sea_raid_from.get(request.territory_id) != request.sea_zone_id:
            sea_def = td.get(request.sea_zone_id)
            land_def = td.get(request.territory_id)
            sea_adj = getattr(sea_def, "adjacent", []) or []
            land_adj = getattr(land_def, "adjacent", []) or []
            if request.territory_id not in sea_adj and request.sea_zone_id not in land_adj:
                raise HTTPException(status_code=400, detail="Territory not adjacent to sea zone")

    try:
        payload = _generate_initiate_combat_payload(
            state, request.territory_id, request.sea_zone_id, ud, td, fd,
            fuse_bomb=request.fuse_bomb,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    action = initiate_combat(
        state.current_faction,
        request.territory_id,
        payload["dice_rolls"],
        terror_applied=payload.get("terror_applied", False),
        terror_final_defender_hits=payload.get("terror_final_defender_hits"),
        sea_zone_id=request.sea_zone_id,
        fuse_bomb=request.fuse_bomb,
    )

    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)

    new_state, events = apply_action(state, action, ud, td, fd, cd, port_d)
    save_game(game_id, new_state, db, events)
    response: dict[str, Any] = {
        "state": state_for_response(new_state, game_id, db),
        "events": [e.to_dict() for e in events],
        "dice_rolls": payload["dice_rolls"],
        "can_act": _player_can_act(game_id, player, db),
    }
    if payload.get("terror_applied") and payload.get("terror_final_defender_hits") is not None:
        response["terror_reroll"] = {
            "applied": True,
            "terror_final_defender_hits": payload["terror_final_defender_hits"],
            **({"terror_reroll_count": payload["terror_reroll_count"]} if payload.get("terror_reroll_count") is not None else {}),
        }
    return response


@app.post("/games/{game_id}/combat/continue")
def do_continue_combat(
    game_id: str,
    request: ContinueCombatRequest,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Continue an active combat. Only the player assigned to the current faction can act."""
    _require_can_act(game_id, player, db)
    state = get_game(game_id, db)
    ud, td, fd, cd, port_d = get_game_definitions(game_id, db)

    if not state.active_combat:
        raise HTTPException(status_code=400, detail="No active combat")

    attackers, defenders = _get_active_combat_units(state, td, ud)
    if not attackers or not defenders:
        raise HTTPException(status_code=400, detail="Invalid combat units")

    combat = state.active_combat
    combat_log = getattr(combat, "combat_log", []) or []
    tdef_c = td.get(combat.territory_id)
    def_sh = bool(tdef_c and getattr(tdef_c, "is_stronghold", False))
    land_terr = state.territories.get(combat.territory_id)
    def_sh_hp_continue = _combat_territory_stronghold_hp(land_terr, tdef_c)
    fuse_cont = getattr(combat, "fuse_bomb", True)
    if not isinstance(fuse_cont, bool):
        fuse_cont = True
    sw_att, sw_def = get_siegework_dice_counts(
        attackers, defenders, ud, def_sh, defender_stronghold_hp=def_sh_hp_continue,
        fuse_bomb=fuse_cont,
    )
    has_lad = any(
        combat_is_siegework_unit(ud.get(u.unit_id)) and combat_has_special(ud.get(u.unit_id), "ladder")
        for u in attackers
    )
    siegeworks_pending = (
        combat.round_number == 0
        and not any(getattr(r, "is_siegeworks_round", False) for r in combat_log)
        and (sw_att > 0 or sw_def > 0 or has_lad)
    )

    archer_prefire_pending = (
        combat.round_number == 0
        and not siegeworks_pending
        and not any(getattr(r, "is_archer_prefire", False) for r in combat_log)
        and not any(getattr(r, "is_stealth_prefire", False) for r in combat_log)
        and any(has_unit_special(ud.get(u.unit_id), "archer") for u in defenders if ud.get(u.unit_id))
    )

    if siegeworks_pending:
        att_rolling = get_siegework_attacker_rolling_units(
            attackers, ud, def_sh, defender_stronghold_hp=def_sh_hp_continue,
            fuse_bomb=fuse_cont,
        )
        def_sw = [u for u in defenders if combat_is_siegework_unit(ud.get(u.unit_id))]
        dice_rolls = {
            "attacker": generate_dice_rolls_for_units(att_rolling, ud),
            "defender": generate_dice_rolls_for_units(def_sw, ud),
        }
    elif archer_prefire_pending:
        defender_archer_units = sorted(
            [u for u in defenders if ud.get(u.unit_id) and has_unit_special(ud[u.unit_id], "archer")],
            key=lambda u: u.instance_id,
        )
        dice_rolls = {
            "attacker": [],
            "defender": generate_dice_rolls_for_units(defender_archer_units, ud),
        }
    else:
        att_effective_dice, _, _ = get_attacker_effective_dice_and_bombikazi_self_destruct(attackers, ud)
        _sort_attackers_for_ladder_dice_if_needed(state, attackers, defenders, ud, td)
        dice_rolls = {
            "attacker": generate_dice_rolls_for_units(
                attackers, ud, effective_dice_override=att_effective_dice,
                exclude_archetypes=set(NORMAL_COMBAT_EXCLUDE_ARCHETYPES),
            ),
            "defender": generate_dice_rolls_for_units(
                defenders, ud, exclude_archetypes=set(NORMAL_COMBAT_EXCLUDE_ARCHETYPES),
            ),
        }

    terror_reroll_response: dict[str, Any] = {}
    is_round_one = combat.round_number == 0 and not siegeworks_pending and not archer_prefire_pending
    if is_round_one:
        territory_def = td.get(state.active_combat.territory_id)
        terrain_att, terrain_def = compute_terrain_stat_modifiers(
            territory_def, attackers, defenders, ud
        )
        anticav_att, anticav_def = compute_anti_cavalry_stat_modifiers(
            attackers, defenders, ud
        )
        captain_att, captain_def = compute_captain_stat_modifiers(
            attackers, defenders, ud
        )
        attacker_mods = merge_stat_modifiers(terrain_att, anticav_att, captain_att)
        defender_mods = merge_stat_modifiers(terrain_def, anticav_def, captain_def)
        # Terror cap: terror units - hope units (hope cancels 1 terror each), then cap at 3
        terror_count = sum(1 for u in attackers if combat_has_special(ud.get(u.unit_id), "terror"))
        hope_count = sum(1 for u in defenders if combat_has_special(ud.get(u.unit_id), "hope"))
        terror_cap = min(3, max(0, terror_count - hope_count))
        flat_indices, total_reroll_dice = get_terror_reroll_targets(
            attackers,
            defenders,
            ud,
            dice_rolls,
            defender_mods or None,
            terror_cap=terror_cap,
            exclude_archetypes_from_rolling=set(NORMAL_COMBAT_EXCLUDE_ARCHETYPES),
        )
        defender_hits_from_rolls = combat_count_hits(
            defenders,
            dice_rolls.get("defender", []),
            ud,
            is_attacker=False,
            stat_modifiers=defender_mods or None,
            exclude_archetypes=set(NORMAL_COMBAT_EXCLUDE_ARCHETYPES),
        )
        # Only re-roll dice that are actually hits; never re-roll misses (would help defender)
        hit_flat_set = get_defender_hit_flat_indices(
            defenders, dice_rolls["defender"], ud, defender_mods or None,
            exclude_archetypes=set(NORMAL_COMBAT_EXCLUDE_ARCHETYPES),
        )
        flat_indices = [i for i in flat_indices if i in hit_flat_set][:defender_hits_from_rolls]
        total_reroll_dice = len(flat_indices)
        if flat_indices and total_reroll_dice > 0 and defender_hits_from_rolls > 0:
            defender_dice_initial_grouped = group_dice_by_stat(
                defenders,
                dice_rolls["defender"],
                ud,
                is_attacker=False,
                stat_modifiers=defender_mods or None,
                exclude_archetypes_from_rolling=set(NORMAL_COMBAT_EXCLUDE_ARCHETYPES),
            )
            new_reroll_values = roll_dice(total_reroll_dice)
            defender_rolls = list(dice_rolls["defender"])
            initial_len = len(defender_rolls)
            # Final defender hits = (hits not re-rolled) + (hits from re-rolls). Re-rolled hits don't count.
            eff_def_per_idx = get_eff_def_per_flat_index(
                defenders, ud, defender_mods or None,
                exclude_archetypes=set(NORMAL_COMBAT_EXCLUDE_ARCHETYPES),
            )
            hits_from_rerolls = sum(
                1
                for j in range(len(flat_indices))
                if flat_indices[j] < len(eff_def_per_idx)
                and new_reroll_values[j] <= eff_def_per_idx[flat_indices[j]]
            )
            terror_final_defender_hits = (
                defender_hits_from_rolls - total_reroll_dice + hits_from_rerolls
            )
            for i, flat_idx in enumerate(flat_indices):
                if flat_idx < initial_len:
                    defender_rolls[flat_idx] = new_reroll_values[i]
            dice_rolls["defender"] = defender_rolls
            rerolled_indices_by_stat = _terror_rerolled_indices_by_stat(
                defenders, ud, defender_mods or None, initial_len, flat_indices,
                exclude_archetypes=set(NORMAL_COMBAT_EXCLUDE_ARCHETYPES),
            )
            terror_reroll_response = {
                "applied": True,
                "defender_dice_initial_grouped": {str(k): v for k, v in defender_dice_initial_grouped.items()},
                "defender_rerolled_indices_by_stat": rerolled_indices_by_stat,
                "terror_final_defender_hits": terror_final_defender_hits,
                "terror_reroll_count": total_reroll_dice,
            }

    action = continue_combat(
        state.current_faction,
        dice_rolls,
        terror_applied=bool(terror_reroll_response),
        terror_final_defender_hits=terror_reroll_response.get("terror_final_defender_hits") if terror_reroll_response else None,
        terror_reroll_count=terror_reroll_response.get("terror_reroll_count") if terror_reroll_response else None,
        casualty_order=getattr(request, "casualty_order", None),
        must_conquer=getattr(request, "must_conquer", None),
    )

    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)

    new_state, events = apply_action(state, action, ud, td, fd, cd, port_d)
    save_game(game_id, new_state, db, events)
    response: dict[str, Any] = {
        "state": state_for_response(new_state, game_id, db),
        "events": [e.to_dict() for e in events],
        "dice_rolls": dice_rolls,
        "can_act": _player_can_act(game_id, player, db),
    }
    if terror_reroll_response:
        response["terror_reroll"] = terror_reroll_response
    return response


@app.post("/games/{game_id}/combat/retreat")
def do_retreat(
    game_id: str,
    request: RetreatRequest,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Retreat from active combat. Only the player assigned to the current faction can act."""
    _require_can_act(game_id, player, db)
    state = get_game(game_id, db)
    ud, td, fd, cd, port_d = get_game_definitions(game_id, db)
    action = retreat(state.current_faction, request.retreat_to)

    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(state, action, ud, td, fd, cd, port_d)
    save_game(game_id, new_state, db, events)
    return {
        "state": state_for_response(new_state, game_id, db),
        "events": [e.to_dict() for e in events],
        "can_act": _player_can_act(game_id, player, db),
    }


@app.post("/games/{game_id}/set-territory-defender-casualty-order")
def do_set_territory_defender_casualty_order(
    game_id: str,
    request: SetTerritoryDefenderCasualtyOrderRequest,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Set defender casualty order for a territory owned by the current faction. Any phase during that faction's turn."""
    _require_can_act(game_id, player, db)
    state = get_game(game_id, db)
    ud, td, fd, cd, port_d = get_game_definitions(game_id, db)
    action = set_territory_defender_casualty_order(
        state.current_faction,
        request.territory_id,
        request.casualty_order,
    )
    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(state, action, ud, td, fd, cd, port_d)
    save_game(game_id, new_state, db, events)
    return {
        "state": state_for_response(new_state, game_id, db),
        "events": [e.to_dict() for e in events],
        "can_act": _player_can_act(game_id, player, db),
    }


@app.post("/games/{game_id}/mobilize")
def do_mobilize(
    game_id: str,
    request: MobilizeRequest,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Mobilize purchased units. Only the player assigned to the current faction can act."""
    _require_can_act(game_id, player, db)
    state = get_game(game_id, db)
    ud, td, fd, cd, port_d = get_game_definitions(game_id, db)
    action = mobilize_units(state.current_faction,
                            request.destination, request.units)

    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(state, action, ud, td, fd, cd, port_d)
    save_game(game_id, new_state, db, events)
    return {
        "state": state_for_response(new_state, game_id, db),
        "events": [e.to_dict() for e in events],
        "can_act": _player_can_act(game_id, player, db),
    }


@app.post("/games/{game_id}/end-phase")
def do_end_phase(
    game_id: str,
    request: EndPhaseRequest,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """End the current phase. Only the player assigned to the current faction can act."""
    _require_can_act(game_id, player, db)
    state = get_game(game_id, db)
    ud, td, fd, cd, port_d = get_game_definitions(game_id, db)
    action = end_phase(state.current_faction)

    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(state, action, ud, td, fd, cd, port_d)
    save_game(game_id, new_state, db, events)
    return {
        "state": state_for_response(new_state, game_id, db),
        "events": [e.to_dict() for e in events],
        "can_act": _player_can_act(game_id, player, db),
    }


@app.post("/games/{game_id}/end-turn")
def do_end_turn(
    game_id: str,
    request: EndPhaseRequest,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """End the current turn. Only the player assigned to the current faction can act."""
    _require_can_act(game_id, player, db)
    state = get_game(game_id, db)
    ud, td, fd, cd, port_d = get_game_definitions(game_id, db)
    action = end_turn(state.current_faction)

    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)

    new_state, events = apply_action(state, action, ud, td, fd, cd, port_d)
    save_game(game_id, new_state, db, events)
    return {
        "state": state_for_response(new_state, game_id, db),
        "events": [e.to_dict() for e in events],
        "can_act": _player_can_act(game_id, player, db),
    }


@app.post("/games/{game_id}/skip-turn")
def do_skip_turn(
    game_id: str,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Force end current faction's turn from any phase (used by forfeit when a player leaves on their turn)."""
    _require_can_act(game_id, player, db)
    state = get_game(game_id, db)
    ud, td, fd, cd, port_d = get_game_definitions(game_id, db)
    action = skip_turn(state.current_faction)
    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(state, action, ud, td, fd, cd, port_d)
    save_game(game_id, new_state, db, events)
    return {
        "state": state_for_response(new_state, game_id, db),
        "events": [e.to_dict() for e in events],
        "can_act": _player_can_act(game_id, player, db),
    }


def _player_in_game(game_id: str, player_id: str, db: Session) -> bool:
    """True if the player is in this game (in players list or lobby_claims)."""
    row = db.query(GameModel).filter(GameModel.id == game_id).first()
    if not row:
        return False
    try:
        players_list = json.loads(row.players) if isinstance(row.players, str) else row.players
    except (TypeError, json.JSONDecodeError):
        players_list = []
    if any(str(p.get("player_id")) == str(player_id) for p in players_list):
        return True
    lobby = _get_lobby_claims_from_config(row)
    if str(player_id) in lobby.values():
        return True
    return False


def _get_active_combat_units(
    state,
    territory_defs: dict | None = None,
    unit_defs: dict | None = None,
):
    """Return (attackers, defenders) for current active combat. Handles sea raid (attackers in sea zone)."""
    if not state.active_combat:
        return [], []
    territory = state.territories.get(state.active_combat.territory_id)
    if not territory:
        return [], []
    attacker_ids = set(state.active_combat.attacker_instance_ids)
    sea_zone_id = getattr(state.active_combat, "sea_zone_id", None)
    if sea_zone_id:
        sea_zone = state.territories.get(sea_zone_id)
        in_sea = [u for u in (sea_zone.units if sea_zone else []) if u.instance_id in attacker_ids]
        attacker_territory = sea_zone if in_sea else territory
    else:
        attacker_territory = territory
    attackers = sorted(
        [u for u in attacker_territory.units if u.instance_id in attacker_ids],
        key=lambda u: u.instance_id,
    )
    defenders = sorted(
        [u for u in territory.units if u.instance_id not in attacker_ids],
        key=lambda u: u.instance_id,
    )
    if (
        territory_defs is not None
        and unit_defs is not None
    ):
        tdef = territory_defs.get(state.active_combat.territory_id)
        if tdef and _is_sea_zone(tdef):
            attackers = [
                u for u in attackers
                if participates_in_sea_hex_naval_combat(u, unit_defs.get(u.unit_id))
            ]
            defenders = [
                u for u in defenders
                if participates_in_sea_hex_naval_combat(u, unit_defs.get(u.unit_id))
            ]
    return attackers, defenders


def _enrich_active_combat_siegework_display_ids(ac_dict: dict, state: GameState, ud, td) -> None:
    """When the next step is the siegeworks round, expose which units belong on shelves (same rules as combat_round_resolved)."""
    if not state.active_combat:
        return
    combat_log_list = ac_dict.get("combat_log") or []
    combat = state.active_combat
    tdef_c = td.get(combat.territory_id)
    def_sh = bool(tdef_c and getattr(tdef_c, "is_stronghold", False))
    attackers, defenders = _get_active_combat_units(state, td, ud)
    land_en = state.territories.get(combat.territory_id)
    def_sh_hp_enrich = _combat_territory_stronghold_hp(land_en, tdef_c)
    fuse_en = getattr(state.active_combat, "fuse_bomb", True)
    if not isinstance(fuse_en, bool):
        fuse_en = True
    sw_att, sw_def = get_siegework_dice_counts(
        attackers, defenders, ud, def_sh, defender_stronghold_hp=def_sh_hp_enrich,
        fuse_bomb=fuse_en,
    )
    has_lad = any(
        combat_is_siegework_unit(ud.get(u.unit_id)) and combat_has_special(ud.get(u.unit_id), "ladder")
        for u in attackers
    )
    siegeworks_pending = (
        ac_dict.get("round_number", 0) == 0
        and not any(
            (r.get("is_siegeworks_round") if isinstance(r, dict) else getattr(r, "is_siegeworks_round", False))
            for r in combat_log_list
        )
        and (sw_att > 0 or sw_def > 0 or has_lad)
    )
    if siegeworks_pending:
        disp_att = get_siegework_round_attacker_display_units(
            attackers, ud, def_sh, defender_stronghold_hp=def_sh_hp_enrich,
            fuse_bomb=fuse_en,
        )
        disp_def = get_siegework_round_defender_display_units(defenders, ud)
        ac_dict["combat_siegeworks_attacker_instance_ids"] = [u.instance_id for u in disp_att]
        ac_dict["combat_siegeworks_defender_instance_ids"] = [u.instance_id for u in disp_def]


def _generate_dice_rolls_for_active_combat(state, ud, td) -> dict:
    """Generate random dice_rolls for current active combat (for AI continue_combat)."""
    if not state.active_combat:
        return {"attacker": [], "defender": []}
    attackers, defenders = _get_active_combat_units(state, td, ud)
    if not attackers or not defenders:
        return {"attacker": [], "defender": []}
    combat = state.active_combat
    combat_log = getattr(combat, "combat_log", []) or []
    tdef_c = td.get(combat.territory_id)
    def_sh = bool(tdef_c and getattr(tdef_c, "is_stronghold", False))
    land_t = state.territories.get(combat.territory_id)
    def_sh_hp_en = _combat_territory_stronghold_hp(land_t, tdef_c)
    fuse_roll = getattr(combat, "fuse_bomb", True)
    if not isinstance(fuse_roll, bool):
        fuse_roll = True
    sw_a, sw_d = get_siegework_dice_counts(
        attackers, defenders, ud, def_sh, defender_stronghold_hp=def_sh_hp_en,
        fuse_bomb=fuse_roll,
    )
    has_lad = any(
        combat_is_siegework_unit(ud.get(u.unit_id)) and combat_has_special(ud.get(u.unit_id), "ladder")
        for u in attackers
    )
    siegeworks_pending = (
        combat.round_number == 0
        and not any(getattr(r, "is_siegeworks_round", False) for r in combat_log)
        and (sw_a > 0 or sw_d > 0 or has_lad)
    )
    if siegeworks_pending:
        att_rolling = get_siegework_attacker_rolling_units(
            attackers, ud, def_sh, defender_stronghold_hp=def_sh_hp_en,
            fuse_bomb=fuse_roll,
        )
        def_sw = [u for u in defenders if combat_is_siegework_unit(ud.get(u.unit_id))]
        return {
            "attacker": generate_dice_rolls_for_units(att_rolling, ud),
            "defender": generate_dice_rolls_for_units(def_sw, ud),
        }
    att_effective_dice, _, _ = get_attacker_effective_dice_and_bombikazi_self_destruct(attackers, ud)
    _sort_attackers_for_ladder_dice_if_needed(state, attackers, defenders, ud, td)
    return {
        "attacker": generate_dice_rolls_for_units(
            attackers, ud, effective_dice_override=att_effective_dice,
            exclude_archetypes=set(NORMAL_COMBAT_EXCLUDE_ARCHETYPES),
        ),
        "defender": generate_dice_rolls_for_units(
            defenders, ud, exclude_archetypes=set(NORMAL_COMBAT_EXCLUDE_ARCHETYPES),
        ),
    }


@app.post("/games/{game_id}/ai-step")
def do_ai_step(
    game_id: str,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Run one AI action for the current faction if it is an AI faction. Any player in the game can trigger. Returns new state and events."""
    from backend.ai import decide
    from backend.ai.context import AIContext

    row = db.query(GameModel).filter(GameModel.id == game_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Game not found")
    if row.status != "active":
        raise HTTPException(status_code=400, detail="Game is not active")
    if not _player_in_game(game_id, str(player.id), db):
        raise HTTPException(status_code=403, detail="Not in this game")

    ai_factions = _get_ai_factions_from_config(row)
    state = get_game(game_id, db)
    if state.winner:
        raise HTTPException(status_code=400, detail="Game is over")
    if state.current_faction not in ai_factions:
        raise HTTPException(
            status_code=400,
            detail=f"Current faction {state.current_faction} is not an AI faction",
        )

    ud, td, fd, cd, port_d = get_game_definitions(game_id, db)
    available_actions = _build_available_actions(state, game_id, db)
    ctx = AIContext(
        state=state,
        unit_defs=ud,
        territory_defs=td,
        faction_defs=fd,
        camp_defs=cd,
        port_defs=port_d,
        available_actions=available_actions,
    )
    action = decide(ctx)
    if action is None:
        raise HTTPException(status_code=400, detail="AI returned no action")

    # AI initiate_combat returns empty dice; server generates them
    if action.type == "initiate_combat":
        pl = action.payload
        if not pl.get("dice_rolls") or (not (pl["dice_rolls"].get("attacker") or pl["dice_rolls"].get("defender"))):
            try:
                fuse_ai = pl.get("fuse_bomb", True)
                if not isinstance(fuse_ai, bool):
                    fuse_ai = True
                payload = _generate_initiate_combat_payload(
                    state, pl["territory_id"], pl.get("sea_zone_id"), ud, td, fd,
                    fuse_bomb=fuse_ai,
                )
                pl["dice_rolls"] = payload["dice_rolls"]
                if payload.get("terror_applied"):
                    pl["terror_applied"] = True
                if payload.get("terror_final_defender_hits") is not None:
                    pl["terror_final_defender_hits"] = payload["terror_final_defender_hits"]
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

    # AI continue_combat returns empty dice; server generates them
    if action.type == "continue_combat" and state.active_combat:
        dr = action.payload.get("dice_rolls") or {}
        if not dr.get("attacker") and not dr.get("defender"):
            action.payload["dice_rolls"] = _generate_dice_rolls_for_active_combat(state, ud, td)

    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    if not validation.valid:
        # Don't get stuck: if this was a move that failed, try end_phase when allowed
        phase = state.phase
        if phase in ("combat_move", "non_combat_move", "mobilization"):
            available_actions = _build_available_actions(state, game_id, db)
            if available_actions.get("can_end_phase"):
                fallback_action = end_phase(state.current_faction)
                fallback_val = validate_action(
                    state, fallback_action, ud, td, fd, cd, port_d
                )
                if fallback_val.valid:
                    new_state, events = apply_action(
                        state, fallback_action, ud, td, fd, cd, port_d
                    )
                    save_game(game_id, new_state, db, events)
                    return {
                        "state": state_for_response(new_state, game_id, db),
                        "events": [e.to_dict() for e in events],
                        "action_type": fallback_action.type,
                    }
        raise HTTPException(status_code=400, detail=validation.error or "AI action invalid")
    new_state, events = apply_action(state, action, ud, td, fd, cd, port_d)
    save_game(game_id, new_state, db, events)
    return {
        "state": state_for_response(new_state, game_id, db),
        "events": [e.to_dict() for e in events],
        "action_type": action.type,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

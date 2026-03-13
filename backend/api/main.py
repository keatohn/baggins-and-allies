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
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .database import get_db, get_db_file_path, init_db
from .models import Game as GameModel, Player
from .auth import (
    create_access_token,
    get_current_player,
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
    ARCHETYPE_ARCHER,
    _is_naval_unit as combat_is_naval_unit,
    get_attacker_effective_dice_and_bombikazi_self_destruct,
    get_bombikazi_pairing,
    get_defender_hit_flat_indices,
    get_eff_def_per_flat_index,
    get_terror_reroll_targets,
    group_dice_by_stat,
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
    load_setup,
    load_specials,
    list_setups,
    definitions_from_snapshot,
)
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
from backend.engine.utils import initialize_game_state, generate_dice_rolls_for_units, get_unit_faction
from backend.engine.movement import _is_sea_zone
from backend.engine.queries import _is_naval_unit, get_valid_offload_sea_zones

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init DB. Shutdown: nothing to do."""
    init_db()
    yield


app = FastAPI(
    title="Baggins & Allies API",
    description="Backend API for Baggins & Allies - a turn-based strategy game",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS configuration for frontend (add production origins via CORS_ORIGINS env, comma-separated)
_default_origins = ["http://localhost:5173", "http://localhost:5174", "http://localhost:3000"]
_extra = os.environ.get("CORS_ORIGINS", "")
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


class CreateGameRequest(BaseModel):
    name: str
    is_multiplayer: bool = False
    """Setup id from GET /setups (e.g. '0.0', '0.1'). Omitted = default from backend.config.DEFAULT_SETUP_ID."""
    setup_id: str | None = None
    """Deprecated: map base name. If setup_id is set, map_asset is ignored and derived from setup."""
    map_asset: str | None = None


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


class MoveRequest(BaseModel):
    game_id: str
    from_territory: str
    to_territory: str
    unit_instance_ids: list[str]
    charge_through: list[str] | None = None  # Cavalry: empty enemy territory IDs to conquer (order)
    load_onto_boat_instance_id: str | None = None  # Load: assign passengers only to this boat in the destination sea zone
    offload_sea_zone_id: str | None = None  # Sea->land: when multiple sea zones can offload to this land, client sends which one to sail to


class CombatRequest(BaseModel):
    game_id: str
    territory_id: str
    sea_zone_id: str | None = None  # For sea raid: attackers are in this sea zone, target is territory_id (land)


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
        # Prime definitions cache from config so this game uses its snapshot
        get_game_definitions(game_id, db)
    except Exception:
        # Corrupt or legacy state in DB — treat as not found so client can create fresh game
        raise HTTPException(status_code=404, detail=f"Game {game_id} not found")
    games[game_id] = state
    return state


def save_game(game_id: str, state: GameState, db: Session | None = None) -> None:
    """Persist game state to DB and cache. Marks game as finished when state.winner is set."""
    games[game_id] = state
    if db is None:
        db = next(get_db())
    row = db.query(GameModel).filter(GameModel.id == game_id).first()
    if row:
        row.game_state = json.dumps(state.to_dict())
        if state.winner is not None:
            row.status = "finished"
        db.commit()


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
) -> tuple[dict, dict]:
    """Compute combat stat modifiers and special flags for active combat. Single source of truth for frontend.
    Returns (combat_stat_modifiers, combat_specials)."""
    if not state.active_combat:
        return {}, {}
    territory = state.territories.get(state.active_combat.territory_id)
    if not territory:
        return {}, {}
    attacker_faction = state.current_faction
    attacker_alliance = getattr(fd.get(attacker_faction), "alliance", None) if fd.get(attacker_faction) else None
    attacker_ids = set(state.active_combat.attacker_instance_ids)
    sea_zone_id = getattr(state.active_combat, "sea_zone_id", None)
    if sea_zone_id:
        sea_zone = state.territories.get(sea_zone_id)
        if sea_zone and sea_zone.units:
            from backend.engine.utils import is_land_unit
            attackers = sorted(
                [
                    u for u in sea_zone.units
                    if u.instance_id in attacker_ids
                    and is_land_unit(ud.get(u.unit_id))
                ],
                key=lambda u: u.instance_id,
            )
        else:
            attackers = sorted(
                [u for u in territory.units if u.instance_id in attacker_ids],
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
    from backend.engine.utils import has_unit_special
    terrain_att, terrain_def = compute_terrain_stat_modifiers(
        territory_def, attackers, defenders, ud
    )
    anticav_att, anticav_def = compute_anti_cavalry_stat_modifiers(attackers, defenders, ud)
    captain_att, captain_def = compute_captain_stat_modifiers(attackers, defenders, ud)
    sea_raider_att, _ = compute_sea_raider_stat_modifiers(attackers, ud, is_sea_raid=bool(sea_zone_id))
    attacker_mods = merge_stat_modifiers(terrain_att, anticav_att, captain_att, sea_raider_att)
    defender_mods = merge_stat_modifiers(terrain_def, anticav_def, captain_def)

    terrain_type = (getattr(territory_def, "terrain_type", None) or "").lower()
    is_mountain = terrain_type in ("mountain", "mountains")
    is_forest = terrain_type == "forest"

    attackers_have_terror = any(
        has_unit_special(ud.get(u.unit_id), "terror") for u in attackers if ud.get(u.unit_id)
    )
    # Stealth: only show ST when all attackers have stealth (prefire is activated)
    stealth_activated = (
        len(attackers) > 0
        and all(has_unit_special(ud.get(u.unit_id), "stealth") for u in attackers if ud.get(u.unit_id))
    )
    # Bombikazi: only show B when unit is paired with a bomb (special activated this round)
    paired_bombikazi_ids = set(get_bombikazi_pairing(attackers, ud)[0]) if attackers else set()

    # Archer: only show AR during defender archer prefire (first round in log is archer prefire)
    combat_log = getattr(state.active_combat, "combat_log", []) or []
    first_round = combat_log[0] if len(combat_log) >= 1 else None
    first_is_archer_prefire = (
        first_round is not None
        and getattr(first_round, "is_archer_prefire", False)
    )
    archer_badge_active = bool(first_is_archer_prefire)

    def _is_archer(unit_def) -> bool:
        if not unit_def:
            return False
        arch = getattr(unit_def, "archetype", "") or ""
        tags = getattr(unit_def, "tags", []) or []
        return arch == "archer" or "archer" in tags

    def build_specials(
        units: list,
        captain_mods: dict,
        anticav_mods: dict,
        terrain_mods: dict,
        is_attacker: bool,
        sea_raider_mods: dict | None = None,
        stealth_activated: bool = False,
        paired_bombikazi_ids: set | None = None,
    ) -> dict:
        out_specials: dict[str, dict[str, bool]] = {}
        sea_raider_mods = sea_raider_mods or {}
        paired_bombikazi_ids = paired_bombikazi_ids or set()
        for u in units:
            unit_def = ud.get(u.unit_id)
            if not unit_def:
                continue
            out_specials[u.instance_id] = {
                "terror": is_attacker and has_unit_special(unit_def, "terror"),
                "terrainMountain": bool(terrain_mods.get(u.instance_id) and is_mountain),
                "terrainForest": bool(terrain_mods.get(u.instance_id) and is_forest),
                "captain": bool(captain_mods.get(u.instance_id, 0) > 0),
                "antiCavalry": bool(anticav_mods.get(u.instance_id, 0) > 0),
                "seaRaider": bool(sea_raider_mods.get(u.instance_id, 0) > 0),
                "archer": (not is_attacker) and _is_archer(unit_def) and archer_badge_active,
                "stealth": is_attacker and has_unit_special(unit_def, "stealth") and stealth_activated,
                "bombikazi": is_attacker and u.instance_id in paired_bombikazi_ids,
                "fearless": (not is_attacker) and has_unit_special(unit_def, "fearless") and attackers_have_terror,
                "hope": (not is_attacker) and has_unit_special(unit_def, "hope") and attackers_have_terror,
            }
        return out_specials

    combat_specials = {
        "attacker": build_specials(
            attackers, captain_att, anticav_att, terrain_att, True,
            sea_raider_att, stealth_activated=stealth_activated, paired_bombikazi_ids=paired_bombikazi_ids,
        ),
        "defender": build_specials(defenders, captain_def, anticav_def, terrain_def, False),
    }
    combat_stat_modifiers = {
        "attacker": dict(attacker_mods),
        "defender": dict(defender_mods),
    }
    return combat_stat_modifiers, combat_specials


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
            combat_stat_modifiers, combat_specials = _get_combat_modifiers_and_specials(state, ud, td, fd)
            out["combat_stat_modifiers"] = combat_stat_modifiers
            out["combat_specials"] = combat_specials
    except Exception:
        out["faction_stats"] = {"factions": {}, "alliances": {}}
    return out


# ===== API Endpoints =====

@app.get("/")
def root():
    return {"message": "Baggins & Allies API", "version": "1.0.0"}


# ----- Auth -----

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
        )
        db.add(player)
        db.commit()
        token = create_access_token(player_id)
        return {"access_token": token, "player": {"id": player_id, "email": player.email, "username": player.username}}
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
    return {"access_token": token, "player": {"id": player.id, "email": player.email, "username": player.username}}


@app.get("/auth/me")
def auth_me(player: Player = Depends(get_current_player)):
    """Return current player (email, username; password not included)."""
    return {"id": player.id, "email": player.email, "username": player.username}


# ----- Games (create, list, join) -----

@app.get("/setups")
def get_setups():
    """List available game setups (id, display_name, map_asset). Use setup_id in POST /games/create."""
    return {"setups": list_setups()}


@app.post("/games/create")
def create_game(
    request: CreateGameRequest,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Create a new game (single or multiplayer). Returns game_id and game_code (if multiplayer)."""
    setup_id = request.setup_id if request.setup_id is not None else DEFAULT_SETUP_ID
    try:
        setup = load_setup(setup_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))
    ud, td, fd, cd, port_d = load_static_definitions(setup_id=setup_id)
    specials_defs, specials_order = load_specials(setup_id=setup_id)
    victory_criteria = setup.get("victory_criteria")
    camp_cost = setup.get("camp_cost")
    state = initialize_game_state(
        faction_defs=fd,
        territory_defs=td,
        unit_defs=ud,
        starting_setup=setup["starting_setup"],
        camp_defs=cd,
        victory_criteria=victory_criteria,
        camp_cost=camp_cost,
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
    if request.is_multiplayer:
        players_list = [{"player_id": str(player.id), "faction_id": None}]
        status = "lobby"
    else:
        players_list = [
            {"player_id": str(player.id), "faction_id": fid}
            for fid in sorted(fd.keys())
        ]
        status = "active"
    players_json = json.dumps(players_list)
    config_snapshot = _build_definitions_snapshot(
        ud, td, fd, cd, port_d, setup["starting_setup"],
        specials=specials_defs, specials_order=specials_order,
    )
    if not request.is_multiplayer:
        config_snapshot.pop("lobby_claims", None)
    if request.setup_id is not None:
        config_snapshot["setup_id"] = request.setup_id
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
def get_definitions():
    """Get all static game definitions (default setup). Never raises."""
    try:
        specials_defs, specials_order = load_specials(setup_id=DEFAULT_SETUP_ID)
        return {
            "units": _safe_asdict_map(unit_defs),
            "territories": _safe_asdict_map(territory_defs),
            "factions": _safe_asdict_map(faction_defs),
            "camps": _safe_asdict_map(camp_defs),
            "ports": _safe_asdict_map(port_defs),
            "specials": specials_defs,
            "specials_order": specials_order,
        }
    except Exception:
        return {
            "units": {}, "territories": {}, "factions": {}, "camps": {}, "ports": {},
            "specials": {}, "specials_order": [],
        }


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
    return {
        "game_id": game_id,
        "state": state_dict,
        "turn_order": turn_order,
        "pending_camps": pending_camps,
        "definitions": definitions,
        "can_act": can_act,
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


def _get_scenario_from_config(row) -> dict[str, Any] | None:
    """Return { display_name, context } from setup manifest if config has setup_id."""
    if not getattr(row, "config", None):
        return None
    try:
        config = json.loads(row.config) if isinstance(row.config, str) else row.config
        setup_id = config.get("setup_id")
        if not setup_id or not isinstance(setup_id, str):
            return None
        for s in list_setups():
            if s.get("id") == setup_id:
                return {
                    "display_name": s.get("display_name", setup_id),
                    "context": s.get("context"),
                }
    except (TypeError, json.JSONDecodeError):
        pass
    return None


@app.get("/games/{game_id}/meta")
def get_game_meta(game_id: str, db: Session = Depends(get_db)):
    """Get game metadata (name, status, players, created_by, lobby_claims, player_usernames, scenario) for lobby etc."""
    row = db.query(GameModel).filter(GameModel.id == game_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Game not found")
    try:
        players_list = json.loads(row.players)
    except (json.JSONDecodeError, TypeError):
        players_list = []
    lobby_claims = _get_lobby_claims_from_config(row)
    player_ids = set()
    for p in players_list:
        pid = p.get("player_id")
        if pid:
            player_ids.add(str(pid))
    for pid in lobby_claims.values():
        player_ids.add(str(pid))
    players_by_id = {}
    if player_ids:
        for p_row in db.query(Player).filter(Player.id.in_(list(player_ids))).all():
            players_by_id[str(p_row.id)] = p_row.username
    scenario = _get_scenario_from_config(row)
    return {
        "id": row.id,
        "name": row.name,
        "game_code": row.game_code,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "created_by": str(row.created_by) if row.created_by else None,
        "players": players_list,
        "lobby_claims": lobby_claims,
        "player_usernames": players_by_id,
        "scenario": scenario,
    }


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
                actions["active_combat"] = state.active_combat.to_dict()
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
    save_game(game_id, new_state, db)
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
    save_game(game_id, new_state, db)
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
    from_sea = _is_sea_zone(td.get(from_territory))
    to_sea = _is_sea_zone(td.get(to_territory))
    move_type = None
    if not from_sea and to_sea:
        move_type = "load"
    elif from_sea and not to_sea:
        move_type = "offload"
    elif from_sea and to_sea:
        move_type = "sail"
    else:
        from_terr = state.territories.get(from_territory)
        if from_terr and request.unit_instance_ids and ud:
            any_aerial = any(
                u and (getattr(ud.get(u.unit_id), "archetype", "") == "aerial" or "aerial" in getattr(ud.get(u.unit_id), "tags", []))
                for iid in request.unit_instance_ids
                for u in [next((x for x in from_terr.units if x.instance_id == iid), None)]
            )
            move_type = "aerial" if any_aerial else "land"
        else:
            move_type = "land"

    # Sea -> land (offload): boat must end in a sea zone adjacent to the land. If boat is not already
    # in such a zone, we sail to one that is reachable and adjacent, then offload. If multiple such
    # zones exist, client must send offload_sea_zone_id (we return need_offload_sea_choice once).
    if from_sea and not to_sea:
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
            offload_pending = PendingMove(
                from_territory=offload_from_sea,
                to_territory=to_territory,
                unit_instance_ids=request.unit_instance_ids,
                phase=state.phase,
                move_type="offload",
            )
            state_after_sail.pending_moves = list(state_after_sail.pending_moves) + [offload_pending]
            save_game(game_id, state_after_sail, db)
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
    )
    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(state, action, ud, td, fd, cd, port_d)
    save_game(game_id, new_state, db)
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
    save_game(game_id, new_state, db)
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
    save_game(game_id, new_state, db)
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
    save_game(game_id, new_state, db)
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
    save_game(game_id, new_state, db)
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
    save_game(game_id, new_state, db)
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
) -> dict[str, list[int]]:
    """Build defender_rerolled_indices_by_stat for terror UI: stat -> list of roll indices (within that stat row) that were re-rolled.
    flat_indices are in unit order (same as defender_rolls and get_terror_reroll_targets), so we must build
    flat_to_stat in the same unit order, not stat order."""
    stat_name = "defense"
    mods = defender_mods or {}
    # Map each flat index (unit order) to (stat_value, idx_within_that_stat_row)
    flat_to_stat: list[tuple[int, int]] = []
    count_per_stat: dict[int, int] = {}
    for u in defenders:
        unit_def = ud.get(u.unit_id)
        if not unit_def:
            flat_to_stat.append((0, 0))
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

    attacker_faction = state.current_faction
    attacker_alliance = getattr(fd.get(attacker_faction), "alliance", None) if fd.get(attacker_faction) else None

    if request.sea_zone_id:
        # Sea raid: attackers from sea zone (land units only) or, after phase end, from territory (already offloaded)
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
        attackers_from_sea = sorted(
            [
                u for u in sea_zone.units
                if ud.get(u.unit_id) and ud[u.unit_id].faction == attacker_faction
                and not combat_is_naval_unit(ud.get(u.unit_id))
            ],
            key=lambda u: u.instance_id,
        )
        # After combat_move phase end, land units were moved to territory; use them if sea has none
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
            raise HTTPException(status_code=400, detail="Sea raid requires at least one land unit (passenger) in the sea zone or on the target territory")
    else:
        # Land combat or naval combat (combat in a sea zone): both sides from same territory
        is_sea_zone_combat = _is_sea_zone(td.get(request.territory_id))
        attackers = sorted(
            [
                u for u in territory.units
                if ud.get(u.unit_id) and ud[u.unit_id].faction == attacker_faction
                and (not is_sea_zone_combat or combat_is_naval_unit(ud.get(u.unit_id)))
            ],
            key=lambda u: u.instance_id,
        )
        defenders = sorted(
            [
                u for u in territory.units
                if ud.get(u.unit_id)
                and ud[u.unit_id].faction != attacker_faction
                and (getattr(fd.get(ud[u.unit_id].faction), "alliance", None) if fd.get(ud[u.unit_id].faction) else None) != attacker_alliance
                and (not is_sea_zone_combat or combat_is_naval_unit(ud.get(u.unit_id)))
            ],
            key=lambda u: u.instance_id,
        )
        if is_sea_zone_combat and (not attackers or not defenders):
            raise HTTPException(
                status_code=400,
                detail="Sea zone combat requires at least one naval unit on each side",
            )

    def _is_archer(unit_def) -> bool:
        if not unit_def:
            return False
        if getattr(unit_def, "archetype", "") == ARCHETYPE_ARCHER:
            return True
        return "archer" in getattr(unit_def, "tags", []) or []

    terror_reroll_response: dict[str, Any] = {}

    # Stealth prefire takes precedence: if ALL attackers have stealth, only attackers roll (defender archer prefire is cancelled)
    all_attackers_have_stealth = (
        len(attackers) > 0
        and all(combat_has_special(ud.get(u.unit_id), "stealth") for u in attackers if ud.get(u.unit_id))
    )
    defender_has_archers = any(
        _is_archer(ud.get(u.unit_id)) for u in defenders if u.unit_id in ud
    )

    if all_attackers_have_stealth:
        att_effective_dice, _ = get_attacker_effective_dice_and_bombikazi_self_destruct(attackers, ud)
        dice_rolls = {
            "attacker": generate_dice_rolls_for_units(attackers, ud, effective_dice_override=att_effective_dice),
            "defender": [],
        }
    elif defender_has_archers:
        defender_archer_units = sorted(
            [u for u in defenders if u.unit_id in ud and _is_archer(ud[u.unit_id])],
            key=lambda u: u.instance_id,
        )
        dice_rolls = {
            "attacker": [],
            "defender": generate_dice_rolls_for_units(defender_archer_units, ud),
        }
    else:
        att_effective_dice, _ = get_attacker_effective_dice_and_bombikazi_self_destruct(attackers, ud)
        dice_rolls = {
            "attacker": generate_dice_rolls_for_units(attackers, ud, effective_dice_override=att_effective_dice),
            "defender": generate_dice_rolls_for_units(defenders, ud),
        }

        # Terror (round 1 only): attackers with "terror" force lowest effective-defense hit defenders to re-roll (cap 3). Fearless immune.
        territory_def = td.get(request.territory_id)
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
        )
        # Only apply terror when at least one defender actually scored a hit (re-roll cancels that hit)
        defender_hits_from_rolls = combat_count_hits(
            defenders,
            dice_rolls.get("defender", []),
            ud,
            is_attacker=False,
            stat_modifiers=defender_mods or None,
        )
        # Only re-roll dice that are actually hits; never re-roll misses (would help defender)
        hit_flat_set = get_defender_hit_flat_indices(
            defenders, dice_rolls["defender"], ud, defender_mods or None
        )
        flat_indices = [i for i in flat_indices if i in hit_flat_set][:defender_hits_from_rolls]
        total_reroll_dice = len(flat_indices)
        if flat_indices and total_reroll_dice > 0 and defender_hits_from_rolls > 0:
            # Build initial defender dice (grouped) for UI to show before re-roll, then replace only hit dice.
            defender_dice_initial_grouped = group_dice_by_stat(
                defenders,
                dice_rolls["defender"],
                ud,
                is_attacker=False,
                stat_modifiers=defender_mods or None,
            )
            new_reroll_values = roll_dice(total_reroll_dice)
            defender_rolls = list(dice_rolls["defender"])
            initial_len = len(defender_rolls)
            # Final defender hits = (hits not re-rolled) + (hits from re-rolls). Re-rolled hits don't count.
            eff_def_per_idx = get_eff_def_per_flat_index(
                defenders, ud, defender_mods or None
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
                defenders, ud, defender_mods or None, initial_len, flat_indices
            )
            terror_reroll_response = {
                "applied": True,
                "defender_dice_initial_grouped": {str(k): v for k, v in defender_dice_initial_grouped.items()},
                "defender_rerolled_indices_by_stat": rerolled_indices_by_stat,
                "terror_final_defender_hits": terror_final_defender_hits,
            }

    action = initiate_combat(
        state.current_faction,
        request.territory_id,
        dice_rolls,
        terror_applied=bool(terror_reroll_response),
        terror_final_defender_hits=terror_reroll_response.get("terror_final_defender_hits") if terror_reroll_response else None,
        sea_zone_id=request.sea_zone_id,
    )

    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)

    new_state, events = apply_action(state, action, ud, td, fd, cd, port_d)
    save_game(game_id, new_state, db)
    response: dict[str, Any] = {
        "state": state_for_response(new_state, game_id, db),
        "events": [e.to_dict() for e in events],
        "dice_rolls": dice_rolls,
        "can_act": _player_can_act(game_id, player, db),
    }
    if terror_reroll_response:
        response["terror_reroll"] = terror_reroll_response
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

    territory = state.territories.get(state.active_combat.territory_id)
    if not territory:
        raise HTTPException(status_code=400, detail="Invalid combat territory")

    attacker_ids = set(state.active_combat.attacker_instance_ids)
    attackers = sorted(
        [u for u in territory.units if u.instance_id in attacker_ids],
        key=lambda u: u.instance_id,
    )
    defenders = sorted(
        [u for u in territory.units if u.instance_id not in attacker_ids],
        key=lambda u: u.instance_id,
    )

    att_effective_dice, _ = get_attacker_effective_dice_and_bombikazi_self_destruct(attackers, ud)
    dice_rolls = {
        "attacker": generate_dice_rolls_for_units(attackers, ud, effective_dice_override=att_effective_dice),
        "defender": generate_dice_rolls_for_units(defenders, ud),
    }

    terror_reroll_response: dict[str, Any] = {}
    is_round_one = state.active_combat.round_number == 0
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
        )
        defender_hits_from_rolls = combat_count_hits(
            defenders,
            dice_rolls.get("defender", []),
            ud,
            is_attacker=False,
            stat_modifiers=defender_mods or None,
        )
        # Only re-roll dice that are actually hits; never re-roll misses (would help defender)
        hit_flat_set = get_defender_hit_flat_indices(
            defenders, dice_rolls["defender"], ud, defender_mods or None
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
            )
            new_reroll_values = roll_dice(total_reroll_dice)
            defender_rolls = list(dice_rolls["defender"])
            initial_len = len(defender_rolls)
            # Final defender hits = (hits not re-rolled) + (hits from re-rolls). Re-rolled hits don't count.
            eff_def_per_idx = get_eff_def_per_flat_index(
                defenders, ud, defender_mods or None
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
                defenders, ud, defender_mods or None, initial_len, flat_indices
            )
            terror_reroll_response = {
                "applied": True,
                "defender_dice_initial_grouped": {str(k): v for k, v in defender_dice_initial_grouped.items()},
                "defender_rerolled_indices_by_stat": rerolled_indices_by_stat,
                "terror_final_defender_hits": terror_final_defender_hits,
            }

    action = continue_combat(
        state.current_faction,
        dice_rolls,
        terror_applied=bool(terror_reroll_response),
        terror_final_defender_hits=terror_reroll_response.get("terror_final_defender_hits") if terror_reroll_response else None,
        casualty_order=getattr(request, "casualty_order", None),
        must_conquer=getattr(request, "must_conquer", None),
    )

    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)

    new_state, events = apply_action(state, action, ud, td, fd, cd, port_d)
    save_game(game_id, new_state, db)
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
    save_game(game_id, new_state, db)
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
    save_game(game_id, new_state, db)
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
    save_game(game_id, new_state, db)
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
    save_game(game_id, new_state, db)
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
    save_game(game_id, new_state, db)
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
    """Force end current faction's turn from any phase (used by forfeit when player leaves on their turn). Next faction gets turn; factions with no capital and no units get turn_skipped. Remove only the Skip Turn button in the UI for production; keep this endpoint."""
    _require_can_act(game_id, player, db)
    state = get_game(game_id, db)
    ud, td, fd, cd, port_d = get_game_definitions(game_id, db)
    action = skip_turn(state.current_faction)
    validation = validate_action(state, action, ud, td, fd, cd, port_d)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(state, action, ud, td, fd, cd, port_d)
    save_game(game_id, new_state, db)
    return {
        "state": state_for_response(new_state, game_id, db),
        "events": [e.to_dict() for e in events],
        "can_act": _player_can_act(game_id, player, db),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

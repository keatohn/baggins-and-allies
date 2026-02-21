"""
FastAPI backend for Baggins & Allies.
Provides REST API endpoints for game state management and actions.
"""

import json
import random
import secrets
import string
import uuid
from dataclasses import asdict
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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

from backend.engine.state import GameState
from backend.engine.actions import (
    Action,
    purchase_units,
    purchase_camp,
    move_units,
    cancel_move,
    cancel_mobilization,
    initiate_combat,
    continue_combat,
    retreat,
    mobilize_units,
    end_phase,
    end_turn,
)
from backend.engine.reducer import apply_action
from backend.engine.combat import ARCHETYPE_ARCHER
from backend.config import DEFAULT_SETUP_ID
from backend.engine.definitions import (
    load_static_definitions,
    load_starting_setup,
    load_setup,
    list_setups,
    definitions_from_snapshot,
)
from dataclasses import asdict
from backend.engine.queries import (
    validate_action,
    get_purchasable_units,
    get_movable_units,
    get_unit_move_targets,
    get_mobilization_territories,
    get_mobilization_capacity,
    get_contested_territories,
    get_retreat_options,
    get_purchased_units,
    get_faction_stats,
)
from backend.engine.utils import initialize_game_state

app = FastAPI(
    title="Baggins & Allies API",
    description="Backend API for Baggins & Allies - a turn-based strategy game",
    version="1.0.0",
)

# CORS configuration for frontend
CORS_ORIGINS = ["http://localhost:5173", "http://localhost:5174", "http://localhost:3000"]
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
unit_defs, territory_defs, faction_defs, camp_defs = load_static_definitions(setup_id=DEFAULT_SETUP_ID)
starting_setup = load_starting_setup(setup_id=DEFAULT_SETUP_ID)

# In-memory cache of loaded game state (also persisted in DB)
games: dict[str, GameState] = {}

# Per-game definitions (from config snapshot); key = game_id, value = (unit_defs, territory_defs, faction_defs, camp_defs)
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


class CombatRequest(BaseModel):
    game_id: str
    territory_id: str


class ContinueCombatRequest(BaseModel):
    game_id: str


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


# ===== Helper Functions =====

def generate_game_code(db: Session) -> str:
    """Generate a unique 4-char alphanumeric game code."""
    for _ in range(20):
        code = "".join(secrets.choice(GAME_CODE_CHARS) for _ in range(GAME_CODE_LENGTH))
        if db.query(GameModel).filter(GameModel.game_code == code).first() is None:
            return code
    raise HTTPException(status_code=500, detail="Could not generate unique game code")


def _build_definitions_snapshot(ud=None, td=None, fd=None, cd=None, start=None) -> dict:
    """Snapshot of definitions + starting_setup for storing in game config. Uses provided defs or module fallback."""
    ud = ud if ud is not None else unit_defs
    td = td if td is not None else territory_defs
    fd = fd if fd is not None else faction_defs
    cd = cd if cd is not None else camp_defs
    start = start if start is not None else starting_setup
    return {
        "definitions": {
            "units": {k: asdict(v) for k, v in ud.items()},
            "territories": {k: asdict(v) for k, v in td.items()},
            "factions": {k: asdict(v) for k, v in fd.items()},
            "camps": {k: asdict(v) for k, v in cd.items()},
        },
        "starting_setup": start,
    }


def get_game_definitions(game_id: str, db: Session | None = None):
    """Return (unit_defs, territory_defs, faction_defs, camp_defs) for this game. Uses snapshot from config if present, else global defs."""
    if game_id in game_defs:
        return game_defs[game_id]
    if db is None:
        db = next(get_db())
    row = db.query(GameModel).filter(GameModel.id == game_id).first()
    if not row or not row.config:
        return (unit_defs, territory_defs, faction_defs, camp_defs)
    try:
        config = json.loads(row.config) if isinstance(row.config, str) else row.config
        defs_snapshot = config.get("definitions")
        if not defs_snapshot:
            return (unit_defs, territory_defs, faction_defs, camp_defs)
        ud, td, fd, cd = definitions_from_snapshot(defs_snapshot)
        game_defs[game_id] = (ud, td, fd, cd)
        return (ud, td, fd, cd)
    except Exception:
        return (unit_defs, territory_defs, faction_defs, camp_defs)


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
    """Persist game state to DB and cache."""
    games[game_id] = state
    if db is None:
        db = next(get_db())
    row = db.query(GameModel).filter(GameModel.id == game_id).first()
    if row:
        row.game_state = json.dumps(state.to_dict())
        db.commit()


def roll_dice(count: int, sides: int = 10) -> list[int]:
    """Roll dice for combat."""
    return [random.randint(1, sides) for _ in range(count)]


def state_to_dict(state: GameState) -> dict[str, Any]:
    """Convert game state to JSON-serializable dict."""
    return state.to_dict()


def state_for_response(state: GameState, game_id: str | None = None, db: Session | None = None) -> dict[str, Any]:
    """State dict including computed faction_stats for the UI. Uses game's definitions if game_id provided."""
    out = state_to_dict(state)
    try:
        if game_id and db is not None:
            _, td, fd, _ = get_game_definitions(game_id, db)
        else:
            td, fd = territory_defs, faction_defs
        out["faction_stats"] = get_faction_stats(state, td, fd)
    except Exception:
        out["faction_stats"] = {"factions": {}, "alliances": {}}
    return out


@app.on_event("startup")
def on_startup():
    init_db()


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
    ud, td, fd, cd = load_static_definitions(setup_id=setup_id)
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
    config_snapshot = _build_definitions_snapshot(ud, td, fd, cd, setup["starting_setup"])
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
    game_defs[game_id] = (ud, td, fd, cd)
    return {"game_id": game_id, "game_code": game_code, "name": request.name}


DEFAULT_FACTION_STATS = {
    "factions": {},
    "alliances": {
        "good": {"strongholds": 0, "territories": 0, "power": 0, "power_per_turn": 0, "units": 0},
        "evil": {"strongholds": 0, "territories": 0, "power": 0, "power_per_turn": 0, "units": 0},
    },
    "neutral_strongholds": 0,
}


def _build_games_list(player: Player, db: Session) -> list[dict[str, Any]]:
    """Build list of game dicts for the current player (with faction_stats and current_player_username). Username only from player→faction assignment (no fallback to current user)."""
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
        except (json.JSONDecodeError, TypeError):
            continue

        turn_number = None
        phase = None
        current_faction = None
        current_player_username = None
        current_faction_display_name = None
        current_faction_icon = None
        faction_stats = None

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
                _, td, fd, _ = get_game_definitions(str(r.id), db)
            except Exception:
                td, fd = territory_defs, faction_defs
            faction_stats = get_faction_stats(state, td, fd)
        except Exception:
            faction_stats = dict(DEFAULT_FACTION_STATS)

        if current_faction and faction_defs.get(current_faction):
            fd = faction_defs[current_faction]
            current_faction_display_name = getattr(fd, "display_name", None) or current_faction
            icon = getattr(fd, "icon", None) or f"{current_faction}.png"
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

        # Username only from faction match or single-player lookup (no fallback to current user)
        item = {
            "id": str(r.id),
            "name": r.name,
            "game_code": r.game_code,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "turn_number": turn_number,
            "phase": phase,
            "current_faction": current_faction,
            "current_faction_display_name": current_faction_display_name,
            "current_faction_icon": current_faction_icon,
            "current_player_username": current_player_username,
            "faction_stats": faction_stats,
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
    """Get all static game definitions. Never raises."""
    try:
        return {
            "units": _safe_asdict_map(unit_defs),
            "territories": _safe_asdict_map(territory_defs),
            "factions": _safe_asdict_map(faction_defs),
            "camps": _safe_asdict_map(camp_defs),
        }
    except Exception:
        return {"units": {}, "territories": {}, "factions": {}, "camps": {}}


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
    game_defs[request.game_id] = (unit_defs, territory_defs, faction_defs, camp_defs)
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
    ud, td, fd, cd = get_game_definitions(game_id, db)
    can_act = _player_can_act(game_id, player, db) if player else False
    return {
        "game_id": game_id,
        "state": state_for_response(state, game_id, db),
        "definitions": {
            "units": _safe_asdict_map(ud),
            "territories": _safe_asdict_map(td),
            "factions": _safe_asdict_map(fd),
            "camps": _safe_asdict_map(cd),
        },
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


@app.get("/games/{game_id}/meta")
def get_game_meta(game_id: str, db: Session = Depends(get_db)):
    """Get game metadata (name, status, players) for lobby etc."""
    row = db.query(GameModel).filter(GameModel.id == game_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Game not found")
    try:
        players_list = json.loads(row.players)
    except (json.JSONDecodeError, TypeError):
        players_list = []
    return {
        "id": row.id,
        "name": row.name,
        "game_code": row.game_code,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "players": players_list,
    }


@app.delete("/games/{game_id}")
def delete_game(
    game_id: str,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Delete a game from DB and cache. Caller must be in the game."""
    row = db.query(GameModel).filter(GameModel.id == game_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Game not found")
    try:
        players_list = json.loads(row.players)
    except (TypeError, json.JSONDecodeError):
        players_list = []
    if not any(str(p.get("player_id")) == str(player.id) for p in players_list):
        raise HTTPException(status_code=403, detail="Not in this game")
    db.delete(row)
    db.commit()
    if game_id in games:
        del games[game_id]
    if game_id in game_defs:
        del game_defs[game_id]
    return {"message": f"Game {game_id} deleted"}


def _build_available_actions(state: GameState, game_id: str, db: Session | None = None) -> dict[str, Any]:
    """Build available-actions dict using this game's definitions. Catches so caller never gets 500."""
    ud, td, fd, cd = get_game_definitions(game_id, db)
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
            capacity_info = get_mobilization_capacity(state, faction, td, cd)
            actions["mobilization_capacity"] = capacity_info.get("total_capacity", 0)
            already_purchased = sum(
                s.count for s in (state.faction_purchased_units or {}).get(faction, [])
            )
            actions["purchased_units_count"] = already_purchased
            actions["camp_cost"] = getattr(state, "camp_cost", 0)
        elif phase in ("combat_move", "non_combat_move"):
            movable = get_movable_units(state, faction)
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
        elif phase == "combat":
            combat_territories = get_contested_territories(state, faction, fd)
            actions["combat_territories"] = combat_territories
            if state.active_combat:
                actions["active_combat"] = state.active_combat.to_dict()
                retreat_destinations = get_retreat_options(state, td, fd)
                actions["retreat_options"] = {
                    "can_retreat": len(retreat_destinations) > 0,
                    "valid_destinations": retreat_destinations,
                }
        elif phase == "mobilization":
            mobilize_territories = get_mobilization_territories(state, faction, td, cd)
            mobilize_capacity = get_mobilization_capacity(state, faction, td, cd)
            purchased = get_purchased_units(state, faction)
            actions["mobilize_options"] = {
                "territories": mobilize_territories,
                "capacity": mobilize_capacity,
                "pending_units": purchased,
            }
            actions["can_end_turn"] = True
        return actions
    except Exception:
        return {
            "faction": getattr(state, "current_faction", "") or "",
            "phase": getattr(state, "phase", "purchase") or "purchase",
            "can_end_phase": True,
            "purchasable_units": [],
            "mobilization_capacity": 0,
            "purchased_units_count": 0,
            "camp_cost": 0,
        }


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
    ud, td, fd, cd = get_game_definitions(game_id, db)
    action = purchase_units(state.current_faction, request.purchases)
    validation = validate_action(state, action, ud, td, fd, cd)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(state, action, ud, td, fd, cd)
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
    ud, td, fd, cd = get_game_definitions(game_id, db)
    action = purchase_camp(state.current_faction)
    validation = validate_action(state, action, ud, td, fd, cd)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(state, action, ud, td, fd, cd)
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
    state = get_game(game_id, db)
    ud, td, fd, cd = get_game_definitions(game_id, db)
    action = move_units(
        state.current_faction,
        request.from_territory,
        request.to_territory,
        request.unit_instance_ids,
        charge_through=request.charge_through,
    )
    validation = validate_action(state, action, ud, td, fd, cd)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(state, action, ud, td, fd, cd)
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
    ud, td, fd, cd = get_game_definitions(game_id, db)
    action = cancel_move(state.current_faction, request.move_index)
    validation = validate_action(state, action, ud, td, fd, cd)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(state, action, ud, td, fd, cd)
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
    ud, td, fd, cd = get_game_definitions(game_id, db)
    action = cancel_mobilization(state.current_faction, request.mobilization_index)
    validation = validate_action(state, action, ud, td, fd, cd)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(state, action, ud, td, fd, cd)
    save_game(game_id, new_state, db)
    return {
        "state": state_for_response(new_state, game_id, db),
        "events": [e.to_dict() for e in events],
        "can_act": _player_can_act(game_id, player, db),
    }


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
    ud, td, fd, cd = get_game_definitions(game_id, db)

    territory = state.territories.get(request.territory_id)
    if not territory:
        raise HTTPException(status_code=400, detail="Invalid territory")

    attackers = [
        u for u in territory.units if ud.get(u.unit_id) and ud[u.unit_id].faction == state.current_faction]
    defenders = [
        u for u in territory.units if ud.get(u.unit_id) and ud[u.unit_id].faction == territory.owner]

    # If defender has archers, only archers roll in prefire (initiate runs prefire only)
    defender_has_archers = any(
        getattr(ud.get(u.unit_id), "archetype", "") == ARCHETYPE_ARCHER for u in defenders if u.unit_id in ud
    )
    if defender_has_archers:
        defender_archer_dice = sum(
            ud[u.unit_id].dice for u in defenders
            if u.unit_id in ud and getattr(ud[u.unit_id], "archetype", "") == ARCHETYPE_ARCHER
        )
        dice_rolls = {
            "attacker": [],
            "defender": roll_dice(defender_archer_dice),
        }
    else:
        attacker_dice = sum(ud[u.unit_id].dice for u in attackers if u.unit_id in ud)
        defender_dice = sum(ud[u.unit_id].dice for u in defenders if u.unit_id in ud)
        dice_rolls = {
            "attacker": roll_dice(attacker_dice),
            "defender": roll_dice(defender_dice),
        }

    action = initiate_combat(state.current_faction,
                             request.territory_id, dice_rolls)

    validation = validate_action(state, action, ud, td, fd)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)

    new_state, events = apply_action(state, action, ud, td, fd)
    save_game(game_id, new_state, db)
    return {
        "state": state_for_response(new_state, game_id, db),
        "events": [e.to_dict() for e in events],
        "dice_rolls": dice_rolls,
        "can_act": _player_can_act(game_id, player, db),
    }


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
    ud, td, fd, cd = get_game_definitions(game_id, db)

    if not state.active_combat:
        raise HTTPException(status_code=400, detail="No active combat")

    territory = state.territories.get(state.active_combat.territory_id)
    if not territory:
        raise HTTPException(status_code=400, detail="Invalid combat territory")

    attackers = [
        u for u in territory.units if u.instance_id in state.active_combat.attacker_instance_ids]
    defenders = [
        u for u in territory.units if u.instance_id not in state.active_combat.attacker_instance_ids]

    attacker_dice = sum(ud[u.unit_id].dice for u in attackers if u.unit_id in ud)
    defender_dice = sum(ud[u.unit_id].dice for u in defenders if u.unit_id in ud)

    dice_rolls = {
        "attacker": roll_dice(attacker_dice),
        "defender": roll_dice(defender_dice),
    }

    action = continue_combat(state.current_faction, dice_rolls)

    validation = validate_action(state, action, ud, td, fd)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)

    new_state, events = apply_action(state, action, ud, td, fd)
    save_game(game_id, new_state, db)
    return {
        "state": state_for_response(new_state, game_id, db),
        "events": [e.to_dict() for e in events],
        "dice_rolls": dice_rolls,
        "can_act": _player_can_act(game_id, player, db),
    }


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
    ud, td, fd, cd = get_game_definitions(game_id, db)
    action = retreat(state.current_faction, request.retreat_to)

    validation = validate_action(state, action, ud, td, fd, cd)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(state, action, ud, td, fd, cd)
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
    ud, td, fd, cd = get_game_definitions(game_id, db)
    action = mobilize_units(state.current_faction,
                            request.destination, request.units)

    validation = validate_action(state, action, ud, td, fd, cd)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(state, action, ud, td, fd, cd)
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
    ud, td, fd, cd = get_game_definitions(game_id, db)
    action = end_phase(state.current_faction)

    validation = validate_action(state, action, ud, td, fd, cd)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(state, action, ud, td, fd, cd)
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
    ud, td, fd, cd = get_game_definitions(game_id, db)
    action = end_turn(state.current_faction)

    validation = validate_action(state, action, ud, td, fd, cd)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)

    new_state, events = apply_action(state, action, ud, td, fd, cd)
    save_game(game_id, new_state, db)
    return {
        "state": state_for_response(new_state, game_id, db),
        "events": [e.to_dict() for e in events],
        "can_act": _player_can_act(game_id, player, db),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

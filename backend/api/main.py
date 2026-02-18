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

from .database import get_db, init_db
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
from backend.engine.definitions import load_static_definitions, load_starting_setup
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


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc):
    """Return 500 with CORS headers so the frontend can read the error."""
    origin = request.headers.get("origin")
    allow_origin = origin if origin in CORS_ORIGINS else CORS_ORIGINS[0]
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc)},
        headers={
            "Access-Control-Allow-Origin": allow_origin,
            "Access-Control-Allow-Credentials": "true",
        },
    )

# Load static definitions on startup
unit_defs, territory_defs, faction_defs = load_static_definitions()
starting_setup = load_starting_setup()

# In-memory cache of loaded game state (also persisted in DB)
games: dict[str, GameState] = {}

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


class JoinGameRequest(BaseModel):
    game_code: str


class NewGameRequest(BaseModel):
    game_id: str


class PurchaseRequest(BaseModel):
    game_id: str
    purchases: dict[str, int]  # unit_id -> count


class MoveRequest(BaseModel):
    game_id: str
    from_territory: str
    to_territory: str
    unit_instance_ids: list[str]


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


def get_game(game_id: str, db: Session | None = None) -> GameState:
    """Get game state from cache or DB; raise 404 if not found."""
    if game_id in games:
        return games[game_id]
    if db is None:
        db = next(get_db())
    row = db.query(GameModel).filter(GameModel.id == game_id).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Game {game_id} not found")
    state = GameState.from_dict(json.loads(row.game_state))
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


def state_for_response(state: GameState) -> dict[str, Any]:
    """State dict including computed faction_stats for the UI."""
    out = state_to_dict(state)
    out["faction_stats"] = get_faction_stats(state, territory_defs, faction_defs)
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

@app.post("/games/create")
def create_game(
    request: CreateGameRequest,
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """Create a new game (single or multiplayer). Returns game_id and game_code (if multiplayer)."""
    state = initialize_game_state(
        faction_defs=faction_defs,
        territory_defs=territory_defs,
        unit_defs=unit_defs,
        starting_setup=starting_setup,
    )
    game_id = str(uuid.uuid4())
    game_code = generate_game_code(db) if request.is_multiplayer else None
    players_json = json.dumps([{"player_id": player.id, "faction_id": None}])
    row = GameModel(
        id=game_id,
        name=request.name,
        game_code=game_code,
        created_by=player.id,
        status="lobby",
        game_state=json.dumps(state.to_dict()),
        players=players_json,
        config=None,
    )
    db.add(row)
    db.commit()
    games[game_id] = state
    return {"game_id": game_id, "game_code": game_code, "name": request.name}


@app.get("/games")
def list_my_games(
    player: Player = Depends(get_current_player),
    db: Session = Depends(get_db),
):
    """List games the current player is in that are not finished."""
    rows = db.query(GameModel).filter(GameModel.status != "finished").all()
    mine = []
    for r in rows:
        try:
            pl = json.loads(r.players)
            if any(p.get("player_id") == player.id for p in pl):
                mine.append({
                    "id": r.id,
                    "name": r.name,
                    "game_code": r.game_code,
                    "status": r.status,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                })
        except (json.JSONDecodeError, TypeError):
            continue
    return {"games": mine}


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
    if any(p.get("player_id") == player.id for p in players_list):
        return {"game_id": row.id, "message": "Already in game"}
    players_list.append({"player_id": player.id, "faction_id": None})
    row.players = json.dumps(players_list)
    db.commit()
    return {"game_id": row.id, "name": row.name}


@app.get("/definitions")
def get_definitions():
    """Get all static game definitions."""
    return {
        "units": {k: asdict(v) for k, v in unit_defs.items()},
        "territories": {k: asdict(v) for k, v in territory_defs.items()},
        "factions": {k: asdict(v) for k, v in faction_defs.items()},
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
    )
    games[request.game_id] = state
    return {
        "game_id": request.game_id,
        "state": state_for_response(state),
    }


@app.get("/games/{game_id}")
def get_game_state(game_id: str, db: Session = Depends(get_db)):
    """Get current game state (from cache or DB)."""
    state = get_game(game_id, db)
    return {
        "game_id": game_id,
        "state": state_for_response(state),
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
def delete_game(game_id: str):
    """Delete a game and reset to initial state."""
    if game_id in games:
        del games[game_id]
        return {"message": f"Game {game_id} deleted"}
    else:
        raise HTTPException(status_code=404, detail=f"Game {game_id} not found")


@app.get("/games/{game_id}/available-actions")
def get_available_actions(game_id: str, db: Session = Depends(get_db)):
    """Get available actions for current faction in current phase."""
    state = get_game(game_id, db)
    faction = state.current_faction
    phase = state.phase

    actions: dict[str, Any] = {
        "faction": faction,
        "phase": phase,
        "can_end_phase": True,
    }

    if phase == "purchase":
        purchasable = get_purchasable_units(state, faction, unit_defs)
        actions["purchasable_units"] = purchasable
        capacity_info = get_mobilization_capacity(state, faction, territory_defs)
        actions["mobilization_capacity"] = capacity_info["total_capacity"]
        already_purchased = sum(
            s.count for s in state.faction_purchased_units.get(faction, [])
        )
        actions["purchased_units_count"] = already_purchased

    elif phase in ("combat_move", "non_combat_move"):
        movable = get_movable_units(state, faction)
        actions["moveable_units"] = []
        for unit_info in movable:
            targets = get_unit_move_targets(
                state, unit_info["instance_id"], unit_defs, territory_defs, faction_defs
            )
            actions["moveable_units"].append({
                "territory": unit_info["territory_id"],
                "unit": unit_info,
                "destinations": targets,
            })

    elif phase == "combat":
        # Always include combat_territories so the battle list stays visible (e.g. when modal is closed)
        combat_territories = get_contested_territories(
            state, faction, faction_defs)
        actions["combat_territories"] = combat_territories
        if state.active_combat:
            # Active combat - can continue, retreat, or cancel (close modal)
            actions["active_combat"] = state.active_combat.to_dict()
            retreat_destinations = get_retreat_options(
                state, territory_defs, faction_defs)
            actions["retreat_options"] = {
                "can_retreat": len(retreat_destinations) > 0,
                "valid_destinations": retreat_destinations,
            }

    elif phase == "mobilization":
        mobilize_territories = get_mobilization_territories(
            state, faction, territory_defs)
        mobilize_capacity = get_mobilization_capacity(
            state, faction, territory_defs)
        purchased = get_purchased_units(state, faction)
        actions["mobilize_options"] = {
            "territories": mobilize_territories,
            "capacity": mobilize_capacity,
            "pending_units": purchased,
        }
        actions["can_end_turn"] = True

    return actions


@app.post("/games/{game_id}/purchase")
def do_purchase(game_id: str, request: PurchaseRequest, db: Session = Depends(get_db)):
    """Purchase units."""
    state = get_game(game_id, db)
    action = purchase_units(state.current_faction, request.purchases)
    validation = validate_action(
        state, action, unit_defs, territory_defs, faction_defs)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(
        state, action, unit_defs, territory_defs, faction_defs)
    save_game(game_id, new_state, db)
    return {
        "state": state_for_response(new_state),
        "events": [e.to_dict() for e in events],
    }


@app.post("/games/{game_id}/move")
def do_move(game_id: str, request: MoveRequest, db: Session = Depends(get_db)):
    """Move units."""
    state = get_game(game_id, db)
    action = move_units(
        state.current_faction,
        request.from_territory,
        request.to_territory,
        request.unit_instance_ids,
    )
    validation = validate_action(
        state, action, unit_defs, territory_defs, faction_defs)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(
        state, action, unit_defs, territory_defs, faction_defs)
    save_game(game_id, new_state, db)
    return {
        "state": state_for_response(new_state),
        "events": [e.to_dict() for e in events],
    }


@app.post("/games/{game_id}/cancel-move")
def do_cancel_move(game_id: str, request: CancelMoveRequest, db: Session = Depends(get_db)):
    """Cancel a pending move."""
    state = get_game(game_id, db)
    action = cancel_move(state.current_faction, request.move_index)
    validation = validate_action(
        state, action, unit_defs, territory_defs, faction_defs)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(
        state, action, unit_defs, territory_defs, faction_defs)
    save_game(game_id, new_state, db)
    return {
        "state": state_for_response(new_state),
        "events": [e.to_dict() for e in events],
    }


@app.post("/games/{game_id}/cancel-mobilization")
def do_cancel_mobilization(game_id: str, request: CancelMobilizationRequest, db: Session = Depends(get_db)):
    """Cancel a pending mobilization."""
    state = get_game(game_id, db)
    action = cancel_mobilization(state.current_faction, request.mobilization_index)
    validation = validate_action(
        state, action, unit_defs, territory_defs, faction_defs)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(
        state, action, unit_defs, territory_defs, faction_defs)
    save_game(game_id, new_state, db)
    return {
        "state": state_for_response(new_state),
        "events": [e.to_dict() for e in events],
    }


@app.post("/games/{game_id}/combat/initiate")
def do_initiate_combat(game_id: str, request: CombatRequest, db: Session = Depends(get_db)):
    """Initiate combat in a territory."""
    state = get_game(game_id, db)

    # Count units for dice rolls
    territory = state.territories.get(request.territory_id)
    if not territory:
        raise HTTPException(status_code=400, detail="Invalid territory")

    # Count attackers (current faction's units) and defenders (territory owner's units)
    attacker_count = sum(
        1 for u in territory.units
        if u.unit_id.startswith(state.current_faction.split('_')[0]) or
        any(u.unit_id.startswith(f) for f in [state.current_faction])
    )
    # Actually need to check unit ownership properly - using unit_defs
    attackers = [
        u for u in territory.units if unit_defs[u.unit_id].faction == state.current_faction]
    defenders = [
        u for u in territory.units if unit_defs[u.unit_id].faction == territory.owner]

    attacker_dice = sum(unit_defs[u.unit_id].dice for u in attackers)
    defender_dice = sum(unit_defs[u.unit_id].dice for u in defenders)

    dice_rolls = {
        "attacker": roll_dice(attacker_dice),
        "defender": roll_dice(defender_dice),
    }

    action = initiate_combat(state.current_faction,
                             request.territory_id, dice_rolls)

    validation = validate_action(
        state, action, unit_defs, territory_defs, faction_defs)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)

    new_state, events = apply_action(
        state, action, unit_defs, territory_defs, faction_defs)
    save_game(game_id, new_state, db)
    return {
        "state": state_for_response(new_state),
        "events": [e.to_dict() for e in events],
        "dice_rolls": dice_rolls,
    }


@app.post("/games/{game_id}/combat/continue")
def do_continue_combat(game_id: str, request: ContinueCombatRequest, db: Session = Depends(get_db)):
    """Continue an active combat."""
    state = get_game(game_id, db)

    if not state.active_combat:
        raise HTTPException(status_code=400, detail="No active combat")

    # Get current combatants
    territory = state.territories.get(state.active_combat.territory_id)
    if not territory:
        raise HTTPException(status_code=400, detail="Invalid combat territory")

    attackers = [
        u for u in territory.units if u.instance_id in state.active_combat.attacker_instance_ids]
    defenders = [
        u for u in territory.units if u.instance_id not in state.active_combat.attacker_instance_ids]

    attacker_dice = sum(unit_defs[u.unit_id].dice for u in attackers)
    defender_dice = sum(unit_defs[u.unit_id].dice for u in defenders)

    dice_rolls = {
        "attacker": roll_dice(attacker_dice),
        "defender": roll_dice(defender_dice),
    }

    action = continue_combat(state.current_faction, dice_rolls)

    validation = validate_action(
        state, action, unit_defs, territory_defs, faction_defs)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)

    new_state, events = apply_action(
        state, action, unit_defs, territory_defs, faction_defs)
    save_game(game_id, new_state, db)
    return {
        "state": state_for_response(new_state),
        "events": [e.to_dict() for e in events],
        "dice_rolls": dice_rolls,
    }


@app.post("/games/{game_id}/combat/retreat")
def do_retreat(game_id: str, request: RetreatRequest, db: Session = Depends(get_db)):
    """Retreat from active combat."""
    state = get_game(game_id, db)
    action = retreat(state.current_faction, request.retreat_to)

    validation = validate_action(
        state, action, unit_defs, territory_defs, faction_defs)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(
        state, action, unit_defs, territory_defs, faction_defs)
    save_game(game_id, new_state, db)
    return {
        "state": state_for_response(new_state),
        "events": [e.to_dict() for e in events],
    }


@app.post("/games/{game_id}/mobilize")
def do_mobilize(game_id: str, request: MobilizeRequest, db: Session = Depends(get_db)):
    """Mobilize purchased units to a stronghold."""
    state = get_game(game_id, db)
    action = mobilize_units(state.current_faction,
                            request.destination, request.units)

    validation = validate_action(
        state, action, unit_defs, territory_defs, faction_defs)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(
        state, action, unit_defs, territory_defs, faction_defs)
    save_game(game_id, new_state, db)
    return {
        "state": state_for_response(new_state),
        "events": [e.to_dict() for e in events],
    }


@app.post("/games/{game_id}/end-phase")
def do_end_phase(game_id: str, request: EndPhaseRequest, db: Session = Depends(get_db)):
    """End the current phase."""
    state = get_game(game_id, db)
    action = end_phase(state.current_faction)

    validation = validate_action(
        state, action, unit_defs, territory_defs, faction_defs)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    new_state, events = apply_action(
        state, action, unit_defs, territory_defs, faction_defs)
    save_game(game_id, new_state, db)
    return {
        "state": state_for_response(new_state),
        "events": [e.to_dict() for e in events],
    }


@app.post("/games/{game_id}/end-turn")
def do_end_turn(game_id: str, request: EndPhaseRequest, db: Session = Depends(get_db)):
    """End the current turn."""
    state = get_game(game_id, db)
    action = end_turn(state.current_faction)

    validation = validate_action(
        state, action, unit_defs, territory_defs, faction_defs)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)

    new_state, events = apply_action(
        state, action, unit_defs, territory_defs, faction_defs)
    save_game(game_id, new_state, db)
    return {
        "state": state_for_response(new_state),
        "events": [e.to_dict() for e in events],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

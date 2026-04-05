"""
Lines / geography for AI: distance from enemy, connected blobs of our territories,
frontline detection, and direction to nearest enemy stronghold per blob.
Used to push units to borders and toward enemy strongholds while balancing defense/attack.
"""

import copy as copy_std
from collections import deque
from copy import deepcopy
from typing import TYPE_CHECKING

from backend.engine.movement import get_reachable_territories_for_unit
from backend.engine.state import Unit
from backend.engine.utils import get_unit_faction

from backend.ai.formulas import territory_expected_gain_components, territory_reinforce_base_score
from backend.ai.habits import NON_COMBAT_REINFORCE_REACH_BEYOND_LOCAL_CAP

if TYPE_CHECKING:
    from backend.engine.state import GameState
    from backend.engine.definitions import (
        TerritoryDefinition,
        FactionDefinition,
        UnitDefinition,
    )


def _is_enemy_territory(
    territory_id: str,
    state: "GameState",
    faction_id: str,
    fd: dict[str, "FactionDefinition"],
) -> bool:
    """True if territory is owned by enemy alliance (not us, not allied, not neutral)."""
    terr = state.territories.get(territory_id)
    if not terr:
        return False
    owner = getattr(terr, "owner", None)
    if owner is None:
        return False
    if owner == faction_id:
        return False
    our_fd = fd.get(faction_id)
    owner_fd = fd.get(owner)
    if not our_fd or not owner_fd:
        return False
    return getattr(owner_fd, "alliance", "") != getattr(our_fd, "alliance", "")


def min_distance_to_enemy_territory(
    start_territory_id: str,
    state: "GameState",
    faction_id: str,
    fd: dict[str, "FactionDefinition"],
    td: dict[str, "TerritoryDefinition"],
) -> int:
    """
    Minimum number of territory steps (ground adjacency) from start to any enemy territory.
    Returns 0 if start is adjacent to enemy, 1 if one step away, etc. Returns 999 if none reachable.
    """
    if _is_enemy_territory(start_territory_id, state, faction_id, fd):
        return 0
    visited = {start_territory_id}
    queue = deque([(start_territory_id, 0)])
    while queue:
        tid, dist = queue.popleft()
        tdef = td.get(tid)
        if not tdef:
            continue
        for adj_id in getattr(tdef, "adjacent", []) or []:
            if adj_id in visited:
                continue
            visited.add(adj_id)
            if _is_enemy_territory(adj_id, state, faction_id, fd):
                return dist + 1
            queue.append((adj_id, dist + 1))
    return 999


def get_faction_territory_blobs(
    state: "GameState",
    faction_id: str,
    td: dict[str, "TerritoryDefinition"],
) -> list[set[str]]:
    """
    Connected components (blobs) of territories we own. Two territories are in the same blob
    if there is a path of our-owned territories using ground adjacency.
    Sea zones are not included (we use ground adjacency only).
    """
    our_territories = {
        tid
        for tid, terr in (state.territories or {}).items()
        if getattr(terr, "owner", None) == faction_id
    }
    blobs: list[set[str]] = []
    remaining = set(our_territories)

    while remaining:
        start = remaining.pop()
        blob: set[str] = {start}
        queue = deque([start])
        while queue:
            tid = queue.popleft()
            tdef = td.get(tid)
            if not tdef:
                continue
            for adj_id in getattr(tdef, "adjacent", []) or []:
                if adj_id in remaining and adj_id in our_territories:
                    remaining.discard(adj_id)
                    blob.add(adj_id)
                    queue.append(adj_id)
        blobs.append(blob)
    return blobs


def is_frontline(
    territory_id: str,
    state: "GameState",
    faction_id: str,
    fd: dict[str, "FactionDefinition"],
    td: dict[str, "TerritoryDefinition"],
) -> bool:
    """True if we own this territory and it is adjacent to at least one enemy-owned territory (border), even if that hex is empty."""
    if getattr(state.territories.get(territory_id), "owner", None) != faction_id:
        return False
    tdef = td.get(territory_id)
    if not tdef:
        return False
    for adj_id in getattr(tdef, "adjacent", []) or []:
        if _is_enemy_territory(adj_id, state, faction_id, fd):
            return True
    return False


def would_be_frontline_after_conquest(
    territory_id: str,
    state: "GameState",
    faction_id: str,
    fd: dict[str, "FactionDefinition"],
    td: dict[str, "TerritoryDefinition"],
) -> bool:
    """
    True if, after we capture this territory, it would border enemy-owned land (next turn it
    should be garrisoned as part of the line). Used for combat-move empty conquests; does not
    require current ownership.
    """
    tdef = td.get(territory_id)
    if not tdef:
        return False
    for adj_id in getattr(tdef, "adjacent", []) or []:
        if _is_enemy_territory(adj_id, state, faction_id, fd):
            return True
    return False


def territory_threatened_by_enemy_combat_move_next_turn(
    territory_id: str,
    state: "GameState",
    defending_faction_id: str,
    unit_defs: dict[str, "UnitDefinition"],
    territory_defs: dict[str, "TerritoryDefinition"],
    faction_defs: dict[str, "FactionDefinition"],
) -> bool:
    """
    True if any enemy unit could reach this territory as a valid combat_move destination on
    the enemy's next turn (full movement), assuming we own it but left it empty (worst case).
    Uses the same reachability rules as the engine (including cavalry charge paths and M=2).
    """
    our_fd = faction_defs.get(defending_faction_id)
    our_alliance = getattr(our_fd, "alliance", "") if our_fd else ""

    hypo = deepcopy(state)
    t = hypo.territories.get(territory_id)
    if not t:
        return False
    t.owner = defending_faction_id
    t.units = []

    for start_tid, terr in hypo.territories.items():
        for u in terr.units:
            uf = get_unit_faction(u, unit_defs)
            if not uf:
                continue
            ufd = faction_defs.get(uf)
            if not ufd or getattr(ufd, "alliance", "") == our_alliance:
                continue
            ud = unit_defs.get(u.unit_id)
            if not ud:
                continue
            um = copy_std.copy(u)
            try:
                um.remaining_movement = int(getattr(um, "base_movement", 0) or 0)
            except (TypeError, ValueError):
                um.remaining_movement = 0
            enemy_faction = uf
            reachable, _ = get_reachable_territories_for_unit(
                um,
                start_tid,
                hypo,
                unit_defs,
                territory_defs,
                faction_defs,
                "combat_move",
                acting_faction_id=enemy_faction,
            )
            if territory_id in reachable:
                return True
    return False


def count_enemies_that_can_reach_territory_combat_move(
    territory_id: str,
    state: "GameState",
    faction_id: str,
    fd: dict[str, "FactionDefinition"],
    td: dict[str, "TerritoryDefinition"],
    unit_defs: dict[str, "UnitDefinition"],
) -> int:
    """
    Number of enemy units that could reach this territory as a combat_move destination next turn
    (full movement). Same rules as non_combat reinforcement threat.
    """
    our_fd = fd.get(faction_id)
    our_alliance = getattr(our_fd, "alliance", "") if our_fd else ""
    saved_faction = getattr(state, "current_faction", None)
    count = 0
    try:
        for tid, terr in (state.territories or {}).items():
            for u in getattr(terr, "units", []) or []:
                uf = get_unit_faction(u, unit_defs)
                if uf == faction_id:
                    continue
                if uf and fd.get(uf) and getattr(fd.get(uf), "alliance", "") == our_alliance:
                    continue
                base_mov = getattr(u, "base_movement", 0) or 0
                if base_mov <= 0:
                    continue
                unit_full = Unit(
                    instance_id=u.instance_id,
                    unit_id=u.unit_id,
                    remaining_movement=base_mov,
                    remaining_health=getattr(u, "remaining_health", 1),
                    base_movement=base_mov,
                    base_health=getattr(u, "base_health", 1),
                    loaded_onto=getattr(u, "loaded_onto", None),
                )
                state.current_faction = uf
                reachable, _ = get_reachable_territories_for_unit(
                    unit_full,
                    tid,
                    state,
                    unit_defs,
                    td,
                    fd,
                    "combat_move",
                )
                if territory_id in (reachable or {}):
                    count += 1
    finally:
        if saved_faction is not None:
            state.current_faction = saved_faction
    return count


def adjacent_enemy_land_unit_count(
    territory_id: str,
    state: "GameState",
    faction_id: str,
    fd: dict[str, "FactionDefinition"],
    td: dict[str, "TerritoryDefinition"],
    unit_defs: dict[str, "UnitDefinition"],
) -> int:
    """Enemy land units on territories adjacent to this one (local border pressure)."""
    from backend.engine.queries import _is_naval_unit

    tdef = td.get(territory_id)
    if not tdef:
        return 0
    our_fd = fd.get(faction_id)
    our_alliance = getattr(our_fd, "alliance", "") if our_fd else ""
    total = 0
    for adj_id in getattr(tdef, "adjacent", []) or []:
        adj = state.territories.get(adj_id)
        if not adj:
            continue
        for u in getattr(adj, "units", []) or []:
            uf = get_unit_faction(u, unit_defs)
            if not uf or uf == faction_id:
                continue
            ufd = fd.get(uf)
            if ufd and getattr(ufd, "alliance", "") == our_alliance:
                continue
            if _is_naval_unit(unit_defs.get(u.unit_id)):
                continue
            total += 1
    return total


def effective_defensive_reinforce_pressure(
    territory_id: str,
    state: "GameState",
    faction_id: str,
    fd: dict[str, "FactionDefinition"],
    td: dict[str, "TerritoryDefinition"],
    unit_defs: dict[str, "UnitDefinition"],
    *,
    reach_beyond_local_cap: int | None = None,
) -> int:
    """
    Blend global reach count with adjacent enemy land units. Raw reach inflates when many map-wide
    stacks can path here; cap how much reach can exceed local border count for reinforcement math.
    """
    cap = (
        reach_beyond_local_cap
        if reach_beyond_local_cap is not None
        else NON_COMBAT_REINFORCE_REACH_BEYOND_LOCAL_CAP
    )
    reach = count_enemies_that_can_reach_territory_combat_move(
        territory_id, state, faction_id, fd, td, unit_defs
    )
    adj = adjacent_enemy_land_unit_count(
        territory_id, state, faction_id, fd, td, unit_defs
    )
    return max(adj, min(reach, adj + cap))


def exposed_empty_conquest_reinforce_need(
    territory_id: str,
    state: "GameState",
    faction_id: str,
    fd: dict[str, "FactionDefinition"],
    td: dict[str, "TerritoryDefinition"],
    unit_defs: dict[str, "UnitDefinition"],
) -> float:
    """
    How important it is to hold this hex with our units if we capture it on the real line and
    leave it empty: same scale as non_combat reinforce_value (base * (1 + reachable enemy count)).
    Zero if capturing it would not border enemy land or no enemy can reach it next turn.
    """
    if not would_be_frontline_after_conquest(
        territory_id, state, faction_id, fd, td
    ):
        return 0.0
    if not territory_threatened_by_enemy_combat_move_next_turn(
        territory_id, state, faction_id, unit_defs, td, fd
    ):
        return 0.0
    base = territory_reinforce_base_score(territory_id, td, fd)
    threat = count_enemies_that_can_reach_territory_combat_move(
        territory_id, state, faction_id, fd, td, unit_defs
    )
    return base * (1.0 + float(threat))


def worth_empty_conquest_combat_move(
    territory_id: str,
    state: "GameState",
    faction_id: str,
    fd: dict[str, "FactionDefinition"],
    td: dict[str, "TerritoryDefinition"],
    unit_defs: dict[str, "UnitDefinition"],
    camp_defs: dict | None = None,
    port_defs: dict | None = None,
) -> bool:
    """
    False for empty hexes that are not worth a dedicated combat_move. Uses the same
    territory objective + power production model as ``expected_net_gain`` (ports, camps,
    strongholds, etc. come from formulas, not ad-hoc flags here). Also true when holding the
    hex would matter for next-turn line pressure.
    """
    tdef = td.get(territory_id)
    if not tdef or not getattr(tdef, "ownable", True):
        return False
    terr = state.territories.get(territory_id)
    if terr:
        owner = getattr(terr, "owner", None)
        if owner and owner != faction_id:
            our_fd = fd.get(faction_id)
            ow_fd = fd.get(owner)
            if (
                our_fd
                and ow_fd
                and getattr(ow_fd, "alliance", "") != getattr(our_fd, "alliance", "")
            ):
                # Empty enemy-owned: always score the raid. Otherwise 0-power coasts fail the
                # objective/frontline gates and combat_move never considers sea raids into them,
                # while non_combat can still offload onto allied beaches (different rules).
                return True
    obj_bonus, power_pp = territory_expected_gain_components(
        territory_id,
        td,
        fd,
        camp_defs=camp_defs,
        port_defs=port_defs,
        unit_defs=unit_defs,
    )
    if obj_bonus > 0 or power_pp > 0:
        return True
    if (
        exposed_empty_conquest_reinforce_need(
            territory_id, state, faction_id, fd, td, unit_defs
        )
        > 0.0
    ):
        return True
    return False


def is_frontline_threatened_by_enemy_army(
    territory_id: str,
    state: "GameState",
    faction_id: str,
    fd: dict[str, "FactionDefinition"],
    td: dict[str, "TerritoryDefinition"],
    unit_defs: dict[str, "UnitDefinition"],
) -> bool:
    """
    True if we own a frontline hex AND at least one adjacent enemy territory currently
    contains enemy (non-allied) units. Adjacency to empty enemy land alone does not count
    as an attack threat for meat-fodder / overstack heuristics.
    """
    if not is_frontline(territory_id, state, faction_id, fd, td):
        return False
    # Local import: queries does not import geography (avoid cycles).
    from backend.engine.queries import get_unit_faction

    our_fd = fd.get(faction_id)
    our_alliance = getattr(our_fd, "alliance", "") if our_fd else ""
    tdef = td.get(territory_id)
    if not tdef:
        return False
    for adj_id in getattr(tdef, "adjacent", []) or []:
        if not _is_enemy_territory(adj_id, state, faction_id, fd):
            continue
        terr = state.territories.get(adj_id)
        if not terr:
            continue
        for u in getattr(terr, "units", []) or []:
            uf = get_unit_faction(u, unit_defs)
            if not uf:
                continue
            other_fd = fd.get(uf)
            if other_fd and getattr(other_fd, "alliance", "") != our_alliance:
                return True
    return False


def get_frontline_territories(
    state: "GameState",
    faction_id: str,
    fd: dict[str, "FactionDefinition"],
    td: dict[str, "TerritoryDefinition"],
) -> set[str]:
    """Set of our territory IDs that are adjacent to enemy (the border)."""
    return {
        tid
        for tid, terr in (state.territories or {}).items()
        if getattr(terr, "owner", None) == faction_id
        and is_frontline(tid, state, faction_id, fd, td)
    }


def count_our_land_units_on_territory(
    state: "GameState",
    territory_id: str,
    faction_id: str,
    unit_defs: dict[str, "UnitDefinition"],
) -> int:
    """Our land units (non-naval) on this territory."""
    from backend.engine.queries import _is_naval_unit

    terr = state.territories.get(territory_id)
    if not terr:
        return 0
    n = 0
    for u in getattr(terr, "units", []) or []:
        if get_unit_faction(u, unit_defs) != faction_id:
            continue
        if _is_naval_unit(unit_defs.get(u.unit_id)):
            continue
        n += 1
    return n


def frontline_hex_next_turn_outnumbered(
    territory_id: str,
    state: "GameState",
    faction_id: str,
    fd: dict[str, "FactionDefinition"],
    td: dict[str, "TerritoryDefinition"],
    unit_defs: dict[str, "UnitDefinition"],
) -> bool:
    """
    True if this is our frontline and next-turn enemy reach count >= our land garrison here
    (locally outnumbered vs combat_move pressure — same model as frontline_defense_outnumbered
    but for one hex). Used to stop pulling cavalry off a hot tile for a distant attack.
    """
    if not is_frontline(territory_id, state, faction_id, fd, td):
        return False
    threat = count_enemies_that_can_reach_territory_combat_move(
        territory_id, state, faction_id, fd, td, unit_defs
    )
    if threat <= 0:
        return False
    ours = count_our_land_units_on_territory(
        state, territory_id, faction_id, unit_defs
    )
    return ours <= threat


def frontline_defense_outnumbered(
    state: "GameState",
    faction_id: str,
    fd: dict[str, "FactionDefinition"],
    td: dict[str, "TerritoryDefinition"],
    unit_defs: dict[str, "UnitDefinition"],
) -> bool:
    """
    True if some frontline hex is locally outnumbered for *next turn's* combat pressure:
    more enemy units can reach that territory on a combat_move (full movement) than we have
    land defenders there. Uses the same reach count as non_combat reinforce_value / exposed
    empty conquest need — not a new model, just aggregated vs local garrison.
    """
    for tid in get_frontline_territories(state, faction_id, fd, td):
        if frontline_hex_next_turn_outnumbered(
            tid, state, faction_id, fd, td, unit_defs
        ):
            return True
    return False


def blob_nearest_enemy_stronghold(
    blob: set[str],
    state: "GameState",
    faction_id: str,
    fd: dict[str, "FactionDefinition"],
    td: dict[str, "TerritoryDefinition"],
) -> tuple[str, int] | None:
    """
    Find the nearest enemy stronghold to this blob (BFS from all blob territories).
    Returns (stronghold_territory_id, distance) or None if no enemy stronghold reachable.
    """
    # All enemy stronghold territory IDs
    enemy_strongholds = {
        tid
        for tid, terr in (state.territories or {}).items()
        if _is_enemy_territory(tid, state, faction_id, fd)
        and (td.get(tid) and getattr(td.get(tid), "is_stronghold", False))
    }
    if not enemy_strongholds:
        return None
    visited = set(blob)
    queue = deque((tid, 0) for tid in blob)
    while queue:
        tid, dist = queue.popleft()
        if tid in enemy_strongholds:
            return (tid, dist)
        tdef = td.get(tid)
        if not tdef:
            continue
        for adj_id in getattr(tdef, "adjacent", []) or []:
            if adj_id in visited:
                continue
            visited.add(adj_id)
            queue.append((adj_id, dist + 1))
    return None


def territory_to_blob_index(
    state: "GameState",
    faction_id: str,
    td: dict[str, "TerritoryDefinition"],
) -> dict[str, int]:
    """Map each of our territory IDs to its blob index (0, 1, ...). Non-owned not in map."""
    blobs = get_faction_territory_blobs(state, faction_id, td)
    out: dict[str, int] = {}
    for i, blob in enumerate(blobs):
        for tid in blob:
            out[tid] = i
    return out


def territory_power_production(
    territory_id: str,
    td: dict[str, "TerritoryDefinition"],
) -> int:
    """Power production of this territory (0 if none or not ownable)."""
    tdef = td.get(territory_id)
    if not tdef:
        return 0
    produces = getattr(tdef, "produces", None)
    if not isinstance(produces, dict):
        return 0
    return int(produces.get("power", 0) or 0)


def min_distance_between_territories(
    start_id: str,
    end_id: str,
    td: dict[str, "TerritoryDefinition"],
) -> int:
    """BFS: minimum number of ground-adjacency steps from start to end. Returns 999 if unreachable."""
    if start_id == end_id:
        return 0
    visited = {start_id}
    queue = deque([(start_id, 0)])
    while queue:
        tid, dist = queue.popleft()
        tdef = td.get(tid)
        if not tdef:
            continue
        for adj_id in getattr(tdef, "adjacent", []) or []:
            if adj_id == end_id:
                return dist + 1
            if adj_id in visited:
                continue
            visited.add(adj_id)
            queue.append((adj_id, dist + 1))
    return 999


def empty_territory_economic_value(
    territory_id: str,
    td: dict[str, "TerritoryDefinition"],
) -> float:
    """
    Economic value of conquering this empty territory (power production; stronghold/capital
    matter for strategy but 'free money' here is mainly power). Used to prioritize
    charging through open space that has economic value.
    """
    tdef = td.get(territory_id)
    if not tdef:
        return 0.0
    if not getattr(tdef, "ownable", True):
        return 0.0
    power = territory_power_production(territory_id, td)
    # Stronghold/capital have high strategic value too
    if getattr(tdef, "is_stronghold", False):
        return float(power) + 10.0
    return float(power)

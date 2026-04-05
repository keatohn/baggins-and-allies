# Event Log Messages

The event log shows one-line, human-readable summaries of what happened in the game. Events are persisted for the whole game and can be filtered by **Turn**, **Faction**, and **Phase**. In development builds, a fourth filter **Show: Summary only | Summary + debug** lets you hide or include debug-style messages.

---

## What production players see (Summary only)

These are the messages that appear when **Show** is "Summary only" (and in production, where the debug filter is not available).

### Purchase phase
| Action | Message example |
|--------|------------------|
| Purchase units | `Purchased 2 Uruk-hai Warrior, 1 Siege Engine` |
| Purchase nothing | *(no event with message)* |

### Combat move phase
| Action | Message example |
|--------|------------------|
| Move to attack (land) | `Moved 3 Uruk-hai Warrior, 2 Berserker to attack Gap of Rohan` |
| Load into sea | `Loaded 2 Corsair of Umbar from Harondor onto ship in Sea Zone 11` (or **onto ships** if the load uses more than one boat) |
| Sail sea → sea | `Moved 1 Black Ship from Sea Zone 11 to Sea Zone 12` |
| Offload (non-combat) | `Offloaded 2 Gondor Soldier from Sea Zone 12 into Pelargir` |
| Offload (combat / sea raid) | `Offloaded 2 Corsair of Umbar from Sea Zone 11 into Harondor for sea raid` |

### Combat phase
| Action | Message example |
|--------|------------------|
| Battle ends — conquer | `Isengard conquered Fangorn; destroyed 2 Rohan Peasant, 1 Rider of Rohan; lost 2 Uruk-hai Warrior` |
| Battle ends — victory (no conquest, e.g. aerial/sea) | `Isengard was victorious in Fangorn; destroyed …; lost …` |
| Battle ends — retreat | `Isengard retreated from Westfold into Isengard; destroyed 1 Rohan Peasant; lost 1 Uruk Crossbowman` |
| Battle ends — defeat | `Isengard defeated in Fangorn; destroyed …; lost …` |

### Non-combat move phase
| Action | Message example |
|--------|------------------|
| Move units | `Moved 2 Uruk-hai Warrior from Isengard to South Dunland` |

### Mobilization phase
| Action | Message example |
|--------|------------------|
| Mobilize units | `Mobilized 2 Uruk-hai Warrior to Isengard` |

### End of turn / game
| Event | Message example |
|--------|------------------|
| Victory | `evil alliance wins!` |

---

## Debug-only messages (dev only, when "Summary + debug" is on)

These are marked `debug_only` and are hidden in production. In development, they appear when **Show** is "Summary + debug".

| Event | Message example |
|--------|------------------|
| Turn started | `Turn 2 — Isengard` |
| Turn ended | `End of turn 2 (Isengard)` |
| Turn skipped | `Gondor skipped turn` |
| Phase changed | `Phase: combat move` |
| Income calculated | `Income: power: 5` |
| Income collected | `Income collected` |
| Territory captured | `Fangorn captured by isengard` |

---

## Events that never appear in the log

These are either folded into another message or are internal:

- **combat_round_resolved** — Per-round dice/casualties; combat is summarized once per battle in `combat_ended`.
- **combat_started** — Attack is already summarized in the combat-move `units_moved` (land: "Moved … to attack …"; sea transport uses `move_type`: Loaded… / Offloaded… / Moved… between seas).
- **units_retreated** — Covered by `combat_ended` with outcome "retreat".
- **unit_destroyed** — Shown in aggregate in `combat_ended` (destroyed/lost).
- **resources_changed** — Reflected in purchase/combat messages.

---

## Persistence and filtering

- **Persistence:** The backend appends every enriched event to the game’s `event_log` (capped at 1000). The client loads this when fetching the game and merges in new events from action responses. The log is therefore available for the whole game and across sessions.
- **Filters:** Turn, Faction, and Phase come from each event’s payload. The **Show** dropdown (dev only) controls whether `debug_only` events are included.

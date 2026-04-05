"""
DB-backed game setups: seed from disk, load for gameplay and admin API.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.setup_ids import validate_setup_id
from backend.setup_validation import validate_setup_payload

from backend.engine.definitions import (
    SETUPS_DIR,
    definitions_from_snapshot,
    load_setup as load_setup_from_files,
    load_specials as load_specials_from_files,
    load_static_definitions as load_static_definitions_from_files,
    list_setups as list_setups_from_files,
    parse_prefire_penalty_from_manifest,
    scenario_display_from_setup_id as scenario_display_from_files,
)
from backend.api.models import Setup


def _read_json_file(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Expected object in {path}")
    return data


def _default_empty_specials() -> dict[str, Any]:
    return {"order": []}


def import_setup_folder_to_dicts(setup_dir: Path) -> dict[str, Any] | None:
    """Load all setup JSON from a folder; returns None if not a complete setup."""
    manifest_path = setup_dir / "manifest.json"
    starting_path = setup_dir / "starting_setup.json"
    if not manifest_path.is_file() or not starting_path.is_file():
        return None
    manifest = _read_json_file(manifest_path)
    setup_id = manifest.get("id", setup_dir.name)
    if not isinstance(setup_id, str) or not setup_id:
        setup_id = setup_dir.name
    manifest = {**manifest, "id": setup_id}
    units = _read_json_file(setup_dir / "units.json")
    territories = _read_json_file(setup_dir / "territories.json")
    factions = _read_json_file(setup_dir / "factions.json")
    camps_path = setup_dir / "camps.json"
    camps = _read_json_file(camps_path) if camps_path.is_file() else {}
    ports_path = setup_dir / "ports.json"
    ports = _read_json_file(ports_path) if ports_path.is_file() else {}
    starting_setup = _read_json_file(starting_path)
    specials_path = setup_dir / "specials.json"
    specials = _read_json_file(specials_path) if specials_path.is_file() else _default_empty_specials()
    return {
        "id": setup_id,
        "manifest": manifest,
        "units": units,
        "territories": territories,
        "factions": factions,
        "camps": camps,
        "ports": ports,
        "starting_setup": starting_setup,
        "specials": specials,
    }


def seed_setups_if_empty(db: Session) -> int:
    """Insert setups from data/setups/* when the setups table is empty. Returns rows inserted."""
    if db.query(Setup).first() is not None:
        return 0
    if not SETUPS_DIR.is_dir():
        return 0
    n = 0
    for d in sorted(SETUPS_DIR.iterdir()):
        if not d.is_dir():
            continue
        bundle = import_setup_folder_to_dicts(d)
        if not bundle:
            continue
        row = Setup(
            id=bundle["id"],
            manifest_json=json.dumps(bundle["manifest"], ensure_ascii=False),
            units_json=json.dumps(bundle["units"], ensure_ascii=False),
            territories_json=json.dumps(bundle["territories"], ensure_ascii=False),
            factions_json=json.dumps(bundle["factions"], ensure_ascii=False),
            camps_json=json.dumps(bundle["camps"], ensure_ascii=False),
            ports_json=json.dumps(bundle["ports"], ensure_ascii=False),
            starting_setup_json=json.dumps(bundle["starting_setup"], ensure_ascii=False),
            specials_json=json.dumps(bundle["specials"], ensure_ascii=False),
            updated_at=datetime.utcnow(),
        )
        db.add(row)
        n += 1
    if n:
        db.commit()
    return n


def _row_to_parsed(row: Setup) -> dict[str, Any]:
    return {
        "manifest": json.loads(row.manifest_json),
        "units": json.loads(row.units_json),
        "territories": json.loads(row.territories_json),
        "factions": json.loads(row.factions_json),
        "camps": json.loads(row.camps_json),
        "ports": json.loads(row.ports_json),
        "starting_setup": json.loads(row.starting_setup_json),
        "specials": json.loads(row.specials_json),
    }


def load_setup_dict_from_db(db: Session, setup_id: str) -> dict[str, Any] | None:
    """Same shape as engine.definitions.load_setup (id, display_name, map_asset, starting_setup, manifest extras)."""
    row = db.query(Setup).filter(Setup.id == setup_id).first()
    if not row:
        return None
    p = _row_to_parsed(row)
    m = p["manifest"]
    result: dict[str, Any] = {
        "id": m.get("id", setup_id),
        "display_name": m.get("display_name", setup_id),
        "map_asset": m.get("map_asset", setup_id),
        "starting_setup": p["starting_setup"],
    }
    vc = m.get("victory_criteria")
    if isinstance(vc, dict) and vc:
        result["victory_criteria"] = vc
    try:
        result["camp_cost"] = int(m["camp_cost"])
    except (TypeError, ValueError, KeyError):
        pass
    try:
        result["stronghold_repair_cost"] = int(m["stronghold_repair_cost"])
    except (TypeError, ValueError, KeyError):
        pass
    result["prefire_penalty"] = parse_prefire_penalty_from_manifest(m.get("prefire_penalty"))
    return result


def load_static_definitions_from_db(db: Session, setup_id: str):
    row = db.query(Setup).filter(Setup.id == setup_id).first()
    if not row:
        return None
    p = _row_to_parsed(row)
    snap = {
        "units": p["units"],
        "territories": p["territories"],
        "factions": p["factions"],
        "camps": p["camps"],
        "ports": p["ports"],
    }
    return definitions_from_snapshot(snap)


def load_specials_from_db(db: Session, setup_id: str) -> tuple[dict[str, dict], list[str]] | None:
    row = db.query(Setup).filter(Setup.id == setup_id).first()
    if not row:
        return None
    data = json.loads(row.specials_json)
    if not isinstance(data, dict):
        return {}, []
    order = data.get("order")
    if isinstance(order, list):
        order = [k for k in order if isinstance(k, str)]
    else:
        order = None
    definitions = {
        k: {
            "name": v.get("name", k),
            "description": v.get("description", ""),
            "display_code": v.get("display_code", ""),
        }
        for k, v in data.items()
        if k != "order" and isinstance(v, dict)
    }
    if not order:
        order = sorted(definitions.keys())
    return definitions, order


def list_setups_menu_from_db(db: Session) -> list[dict[str, Any]]:
    """Active setups with non-empty context (create-game menu)."""
    out: list[dict[str, Any]] = []
    for row in db.query(Setup).order_by(Setup.id).all():
        try:
            m = json.loads(row.manifest_json)
        except json.JSONDecodeError:
            continue
        if m.get("is_active") is not True:
            continue
        ctx = m.get("context")
        if not isinstance(ctx, dict) or not ctx:
            continue
        out.append(
            {
                "id": m.get("id", row.id),
                "display_name": m.get("display_name", row.id),
                "map_asset": m.get("map_asset", row.id),
                "context": ctx,
            }
        )
    return out


def list_all_setups_admin(db: Session) -> list[dict[str, Any]]:
    rows = db.query(Setup).order_by(Setup.id).all()
    out = []
    for row in rows:
        try:
            m = json.loads(row.manifest_json)
        except json.JSONDecodeError:
            m = {}
        out.append(
            {
                "id": row.id,
                "display_name": m.get("display_name", row.id),
                "is_active": m.get("is_active"),
                "updated_at": row.updated_at.isoformat() + "Z" if row.updated_at else None,
            }
        )
    return out


def scenario_display_from_setup_id_db(db: Session, setup_id: str) -> dict[str, Any] | None:
    row = db.query(Setup).filter(Setup.id == setup_id).first()
    if not row:
        return None
    try:
        m = json.loads(row.manifest_json)
    except json.JSONDecodeError:
        return None
    ctx = m.get("context")
    return {
        "display_name": m.get("display_name", setup_id),
        "context": ctx if isinstance(ctx, dict) else None,
    }


def get_admin_setup_bundle(db: Session, setup_id: str) -> dict[str, Any] | None:
    row = db.query(Setup).filter(Setup.id == setup_id).first()
    if not row:
        return None
    p = _row_to_parsed(row)
    return {
        "id": row.id,
        "manifest": p["manifest"],
        "units": p["units"],
        "territories": p["territories"],
        "factions": p["factions"],
        "camps": p["camps"],
        "ports": p["ports"],
        "starting_setup": p["starting_setup"],
        "specials": p["specials"],
    }


def empty_setup_payload(new_id: str) -> dict[str, Any]:
    """Minimal valid draft (inactive, empty maps) for a new setup id."""
    return {
        "manifest": {
            "id": new_id,
            "display_name": new_id,
            "is_active": False,
            "map_asset": "",
            "context": {},
        },
        "units": {},
        "territories": {},
        "factions": {},
        "camps": {},
        "ports": {},
        "starting_setup": {
            "turn_order": [],
            "territory_owners": {},
            "starting_units": {},
        },
        "specials": {"order": []},
    }


def create_setup(
    db: Session,
    new_id: str,
    duplicate_from: str | None,
) -> dict[str, Any]:
    """Insert a new setup row. Raises ValueError on validation / duplicate / missing source."""
    err = validate_setup_id(new_id)
    if err:
        raise ValueError(err)
    if db.query(Setup).filter(Setup.id == new_id).first():
        raise ValueError("A setup with this id already exists")

    if duplicate_from:
        src = db.query(Setup).filter(Setup.id == duplicate_from).first()
        if not src:
            raise ValueError("Template setup not found")
        p = _row_to_parsed(src)
        manifest = dict(p["manifest"])
        manifest["id"] = new_id
        manifest["is_active"] = False
        if isinstance(manifest.get("display_name"), str):
            manifest["display_name"] = f"{manifest['display_name']} (copy)"
        else:
            manifest["display_name"] = new_id
        payload = {
            "manifest": manifest,
            "units": dict(p["units"]),
            "territories": dict(p["territories"]),
            "factions": dict(p["factions"]),
            "camps": dict(p["camps"]),
            "ports": dict(p["ports"]),
            "starting_setup": dict(p["starting_setup"]),
            "specials": dict(p["specials"]),
        }
    else:
        payload = empty_setup_payload(new_id)

    v_errs = validate_setup_payload(payload)
    if v_errs:
        raise ValueError("; ".join(v_errs[:12]))

    row = Setup(
        id=new_id,
        manifest_json=json.dumps(payload["manifest"], ensure_ascii=False),
        units_json=json.dumps(payload["units"], ensure_ascii=False),
        territories_json=json.dumps(payload["territories"], ensure_ascii=False),
        factions_json=json.dumps(payload["factions"], ensure_ascii=False),
        camps_json=json.dumps(payload["camps"], ensure_ascii=False),
        ports_json=json.dumps(payload["ports"], ensure_ascii=False),
        starting_setup_json=json.dumps(payload["starting_setup"], ensure_ascii=False),
        specials_json=json.dumps(payload["specials"], ensure_ascii=False),
        updated_at=datetime.utcnow(),
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ValueError("A setup with this id already exists") from None
    return {"id": new_id}


def save_setup_bundle(db: Session, setup_id: str, payload: dict[str, Any]) -> None:
    """Persist validated payload; manifest.id is forced to setup_id (immutable id)."""
    row = db.query(Setup).filter(Setup.id == setup_id).first()
    if not row:
        raise ValueError("setup not found")
    manifest = dict(payload["manifest"])
    manifest["id"] = setup_id
    payload = dict(payload)
    payload["manifest"] = manifest

    row.manifest_json = json.dumps(manifest, ensure_ascii=False)
    row.units_json = json.dumps(payload["units"], ensure_ascii=False)
    row.territories_json = json.dumps(payload["territories"], ensure_ascii=False)
    row.factions_json = json.dumps(payload["factions"], ensure_ascii=False)
    row.camps_json = json.dumps(payload["camps"], ensure_ascii=False)
    row.ports_json = json.dumps(payload["ports"], ensure_ascii=False)
    row.starting_setup_json = json.dumps(payload["starting_setup"], ensure_ascii=False)
    row.specials_json = json.dumps(payload["specials"], ensure_ascii=False)
    row.updated_at = datetime.utcnow()
    db.commit()


# ----- Optional DB: helpers used from definitions.py (no Session in tests) -----

def db_has_any_setup(db: Session) -> bool:
    return db.query(Setup).first() is not None


def try_load_setup(setup_id: str, db: Session | None) -> dict[str, Any] | None:
    if db is not None and db_has_any_setup(db):
        return load_setup_dict_from_db(db, setup_id)
    try:
        return load_setup_from_files(setup_id)
    except FileNotFoundError:
        return None


def try_load_static_definitions(setup_id: str, db: Session | None):
    if db is not None and db_has_any_setup(db):
        hit = load_static_definitions_from_db(db, setup_id)
        if hit is None:
            raise FileNotFoundError(f"Setup not found in database: {setup_id}")
        return hit
    return load_static_definitions_from_files(setup_id=setup_id)


def try_load_specials(setup_id: str, db: Session | None):
    if db is not None and db_has_any_setup(db):
        hit = load_specials_from_db(db, setup_id)
        if hit is None:
            raise FileNotFoundError(f"Setup not found in database: {setup_id}")
        return hit
    return load_specials_from_files(setup_id=setup_id)


def try_list_setups_menu(db: Session | None) -> list[dict[str, Any]]:
    if db is not None and db_has_any_setup(db):
        return list_setups_menu_from_db(db)
    return list_setups_from_files()


def try_scenario_display(setup_id: str, db: Session | None) -> dict[str, Any] | None:
    if db is not None and db_has_any_setup(db):
        hit = scenario_display_from_setup_id_db(db, setup_id)
        if hit is not None:
            return hit
    return scenario_display_from_files(setup_id)

"""Bounded, process-local cache of complete admin balance reports."""
from collections import OrderedDict
from concurrent.futures import Future
from copy import deepcopy
import hashlib
import json
from threading import Lock

from backend.engine.openings import OPENING_SIMULATION_VERSION
from backend.balance.opening_projection import compute_opening_projection
from backend.balance.starting_strength import compute_starting_strength

_REPORT_CACHE_MAX = 16
_REPORT_CACHE: OrderedDict[str, dict] = OrderedDict()
_PENDING: dict[str, Future] = {}
_LOCK = Lock()


def compute_balance_report(bundle: dict, *, rings: bool | None = None, opening: bool = False) -> dict:
    """Reuse completed reports and share concurrent requests for the same setup.

    Every setup field participates in the key, including unsaved editor changes.
    Failures are never cached. Cached reports are independent of the much smaller
    Openings cache and are cleared when this backend process restarts.
    """
    raw = json.dumps({"simulation_version": OPENING_SIMULATION_VERSION, "bundle": bundle, "rings": rings is not False, "opening": opening},
                     sort_keys=True, separators=(",", ":"))
    key = hashlib.sha256(raw.encode()).hexdigest()
    with _LOCK:
        if key in _REPORT_CACHE:
            _REPORT_CACHE.move_to_end(key)
            return deepcopy(_REPORT_CACHE[key])
        pending = _PENDING.get(key)
        owner = pending is None
        if owner:
            pending = Future()
            _PENDING[key] = pending
    if not owner:
        return deepcopy(pending.result())
    try:
        report = compute_starting_strength(bundle, rings=rings)
        if opening:
            report["opening_projection"] = compute_opening_projection(bundle, rings=rings)
    except BaseException as exc:
        with _LOCK:
            _PENDING.pop(key, None)
        pending.set_exception(exc)
        raise
    with _LOCK:
        _REPORT_CACHE[key] = deepcopy(report)
        _REPORT_CACHE.move_to_end(key)
        while len(_REPORT_CACHE) > _REPORT_CACHE_MAX:
            _REPORT_CACHE.popitem(last=False)
        _PENDING.pop(key, None)
    pending.set_result(report)
    return deepcopy(report)

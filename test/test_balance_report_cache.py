"""Completed reports survive opening-cache eviction and coalesce duplicate work."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event

import pytest

from backend.balance import report


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    monkeypatch.setattr(report, '_REPORT_CACHE', report.OrderedDict())
    monkeypatch.setattr(report, '_PENDING', {})


@pytest.fixture
def counters(monkeypatch):
    calls = {'baseline': 0, 'opening': 0}

    def baseline(*args, **kwargs):
        calls['baseline'] += 1
        return {'alliances': [{'id': 'good'}]}

    def opening(*args, **kwargs):
        calls['opening'] += 1
        return {'runs': 3, 'alliances': [{'id': 'good'}]}

    monkeypatch.setattr(report, 'compute_starting_strength', baseline)
    monkeypatch.setattr(report, 'compute_opening_projection', opening)
    return calls


def test_complete_report_reused_with_reordered_input_and_defensive_copies(counters):
    bundle = {'manifest': {'name': 'test'}, 'starting_setup': {'turn_order': ['a', 'b']}}
    original = deepcopy(bundle)
    first = report.compute_balance_report(bundle, rings=True, opening=True)
    first['opening_projection']['alliances'].clear()
    second = report.compute_balance_report(dict(reversed(list(bundle.items()))), opening=True)
    assert second['opening_projection']['alliances'] == [{'id': 'good'}]
    assert counters == {'baseline': 1, 'opening': 1}
    assert bundle == original


@pytest.mark.parametrize('field', ['manifest', 'units', 'territories', 'factions', 'camps', 'ports', 'starting_setup'])
def test_setup_edits_invalidate_report(field, counters):
    report.compute_balance_report({field: {'value': 1}}, opening=True)
    report.compute_balance_report({field: {'value': 2}}, opening=True)
    assert counters == {'baseline': 2, 'opening': 2}


def test_rings_and_opening_options_are_distinct(counters):
    report.compute_balance_report({}, rings=True, opening=True)
    report.compute_balance_report({}, rings=False, opening=True)
    assert 'opening_projection' not in report.compute_balance_report({}, rings=True)
    assert counters == {'baseline': 3, 'opening': 2}


def test_lru_retains_recent_reports_and_bounds_memory(monkeypatch, counters):
    monkeypatch.setattr(report, '_REPORT_CACHE_MAX', 2)
    for setup in ('a', 'b', 'a', 'c', 'a'):
        report.compute_balance_report({'setup': setup}, opening=True)
    assert counters['opening'] == 3
    assert len(report._REPORT_CACHE) == 2
    report.compute_balance_report({'setup': 'b'}, opening=True)
    assert counters['opening'] == 4


def test_failure_can_be_retried(monkeypatch, counters):
    opening = report.compute_opening_projection
    def fail(*args, **kwargs):
        raise ValueError('failed combat')
    monkeypatch.setattr(report, 'compute_opening_projection', fail)
    with pytest.raises(ValueError, match='failed combat'):
        report.compute_balance_report({}, opening=True)
    assert not report._PENDING and not report._REPORT_CACHE
    monkeypatch.setattr(report, 'compute_opening_projection', opening)
    assert report.compute_balance_report({}, opening=True)['opening_projection']['runs'] == 3


def test_concurrent_requests_share_one_simulation(monkeypatch, counters):
    started, release, waiting = Event(), Event(), Event()
    opening = report.compute_opening_projection
    future_type = report.Future
    class ObservedFuture(future_type):
        def result(self, timeout=None):
            waiting.set()
            return super().result(timeout=timeout)
    monkeypatch.setattr(report, 'Future', ObservedFuture)

    def slow(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return opening(*args, **kwargs)

    monkeypatch.setattr(report, 'compute_opening_projection', slow)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(report.compute_balance_report, {}, opening=True)
        assert started.wait(5)
        second = pool.submit(report.compute_balance_report, {}, opening=True)
        try:
            assert waiting.wait(5)
        finally:
            release.set()
        a, b = first.result(timeout=5), second.result(timeout=5)
    assert a == b and a is not b
    a['opening_projection'].clear()
    assert b['opening_projection']['runs'] == 3
    assert counters == {'baseline': 1, 'opening': 1}


def test_simulation_version_invalidates_complete_report(monkeypatch, counters):
    report.compute_balance_report({}, opening=True)
    monkeypatch.setattr(report, 'OPENING_SIMULATION_VERSION', report.OPENING_SIMULATION_VERSION + 1)
    report.compute_balance_report({}, opening=True)
    assert counters == {'baseline': 2, 'opening': 2}

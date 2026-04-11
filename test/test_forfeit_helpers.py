"""Unit tests for multiplayer forfeit reassignment helpers."""
from backend.api.main import FORFEIT_ASSIGN_COMPUTER, _normalize_forfeit_assign_target


def test_normalize_forfeit_assign_target_computer_case_insensitive():
    assert _normalize_forfeit_assign_target("computer") == FORFEIT_ASSIGN_COMPUTER
    assert _normalize_forfeit_assign_target("Computer") == FORFEIT_ASSIGN_COMPUTER
    assert _normalize_forfeit_assign_target("  COMPUTER  ") == FORFEIT_ASSIGN_COMPUTER


def test_normalize_forfeit_assign_target_player_id_preserved():
    assert _normalize_forfeit_assign_target("a1b2c3d4-e5f6-7890-abcd-ef1234567890") == (
        "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    )

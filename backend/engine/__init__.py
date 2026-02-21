"""
Middle Earth Turn-Based Strategy Game Engine
V1 - Core engine without web framework, database, or UI
"""

DICE_SIDES = 10

# Victory criteria live in GameState.victory_criteria and setup manifests.
# Shape: {"strongholds": {"good": 2, "evil": 2}, ...} - extensible for future criteria.

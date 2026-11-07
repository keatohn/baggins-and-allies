import type { PlayerProfileCard, PlayerStatsOverall } from '../../services/api';

export type LeaderboardSortKey = 'xp' | 'wins' | 'losses' | 'win_pct' | 'games' | 'strongholds_captured' | 'battles_won' | 'units_destroyed';

export function leaderboardValue(profile: Pick<PlayerProfileCard, 'xp'>, stats: PlayerStatsOverall, key: LeaderboardSortKey): number | null {
  return key === 'xp' ? profile.xp ?? 0 : stats[key] ?? (key === 'win_pct' ? null : 0);
}

export function compareLeaderboardValues(a: number | null, b: number | null, direction: 'asc' | 'desc'): number {
  if (a === null) return b === null ? 0 : 1;
  if (b === null) return -1;
  return direction === 'asc' ? a - b : b - a;
}


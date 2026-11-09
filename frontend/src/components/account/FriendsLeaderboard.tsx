import { useEffect, useMemo, useState } from 'react';
import { compareLeaderboardValues, leaderboardValue } from './friendsLeaderboardOrder';
import { tierRingProps } from './tierRing';
import { api } from '../../services/api';
import type { FriendUser, PlayerProfileCard, PlayerStats, PlayerStatsOverall } from '../../services/api';

const columns = [
  ['xp', 'XP'], ['wins', 'Wins'], ['losses', 'Losses'], ['win_pct', 'Win %'],
  ['games', 'Games'], ['strongholds_captured', 'Strongholds'],
  ['battles_won', 'Battles'], ['units_destroyed', 'Units'],
] as const;
type SortKey = typeof columns[number][0];
type Entry = { profile: PlayerProfileCard; stats: PlayerStats };
type Board = { entries: Entry[]; selfId: string; loading: boolean; error: string | null };

export default function FriendsLeaderboard({ friends, onOpen }: {
  friends: FriendUser[];
  onOpen: (friend: FriendUser) => void;
}) {
  const [board, setBoard] = useState<Board>({ entries: [], selfId: '', loading: true, error: null });
  const [retry, setRetry] = useState(0);
  const [alliance, setAlliance] = useState<'' | 'good' | 'evil'>('');
  const [scenario, setScenario] = useState('');
  const [faction, setFaction] = useState('');
  const [factionOptions, setFactionOptions] = useState<{ id: string; name: string }[]>([]);
  const [sort, setSort] = useState<{ key: SortKey; direction: 'asc' | 'desc' }>({ key: 'xp', direction: 'desc' });
  const [filtered, setFiltered] = useState<{ key: string; stats: Record<string, PlayerStatsOverall>; error: string | null }>({ key: '', stats: {}, error: null });

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setBoard((current) => ({ ...current, loading: true, error: null }));
      try {
        const me = await api.authMe();
        const ids = [...new Set([me.id, ...friends.map((friend) => friend.id)])];
        const entries = await Promise.all(ids.map(async (id) => {
          const [profile, stats] = await Promise.all([api.getPlayerProfile(id), api.getPlayerStats(id)]);
          return { profile, stats };
        }));
        if (!cancelled) {
          setFiltered({ key: '', stats: {}, error: null });
          setBoard({ entries, selfId: me.id, loading: false, error: null });
        }
      } catch (err) {
        if (!cancelled) setBoard((current) => ({ ...current, loading: false, error: err instanceof Error ? err.message : 'Could not load leaderboard' }));
      }
    }
    void load();
    return () => { cancelled = true; };
  }, [friends, retry]);

  const filterKey = JSON.stringify([alliance, scenario, faction]);
  const hasFilters = alliance !== '' || scenario !== '';
  useEffect(() => {
    if (!hasFilters || board.loading || board.error) return;
    let cancelled = false;
    async function load() {
      try {
        const responses = await Promise.all(board.entries.map(async ({ profile }) => [profile.id, (await api.getPlayerStats(profile.id, {
          ...(alliance ? { alliance } : {}), ...(scenario ? { scenario } : {}), ...(faction ? { faction } : {}),
        }))] as const));
        if (!cancelled) {
          setFactionOptions(responses[0]?.[1].faction_options ?? []);
          setFiltered({ key: filterKey, stats: Object.fromEntries(responses.map(([id, stats]) => [id, stats.overall])), error: null });
        }
      } catch (err) {
        if (!cancelled) setFiltered({ key: filterKey, stats: {}, error: err instanceof Error ? err.message : 'Could not filter leaderboard' });
      }
    }
    void load();
    return () => { cancelled = true; };
  }, [alliance, scenario, faction, hasFilters, filterKey, board]);

  const scenarioOptions = useMemo(() => {
    const options = new Map<string, string>();
    for (const entry of board.entries) for (const item of entry.stats.by_scenario) {
      if (item.setup_id) options.set(item.setup_id, item.scenario_name);
    }
    return [...options].sort((a, b) => a[1].localeCompare(b[1]));
  }, [board.entries]);
  const loading = board.loading || (hasFilters && filtered.key !== filterKey);
  const error = board.error ?? (hasFilters && filtered.key === filterKey ? filtered.error : null);
  const rows = board.entries.map((entry) => ({ ...entry, overall: hasFilters ? filtered.stats[entry.profile.id] ?? entry.stats.overall : entry.stats.overall }))
    .sort((a, b) => compareLeaderboardValues(leaderboardValue(a.profile, a.overall, sort.key), leaderboardValue(b.profile, b.overall, sort.key), sort.direction)
      || a.profile.username.localeCompare(b.profile.username) || a.profile.id.localeCompare(b.profile.id));
  const toggleSort = (key: SortKey) => setSort((current) => ({ key, direction: current.key === key && current.direction === 'desc' ? 'asc' : 'desc' }));
  const header = (key: SortKey, label: string) => (
    <th key={key} scope="col" aria-sort={sort.key === key ? (sort.direction === 'desc' ? 'descending' : 'ascending') : 'none'}>
      <button type="button" onClick={() => toggleSort(key)}>{label}{sort.key === key ? (sort.direction === 'desc' ? ' ↓' : ' ↑') : ' ↕'}</button>
    </th>
  );

  return (
    <section className="friends-leaderboard" aria-label="Friends leaderboard" aria-busy={loading}>
      <h3 className="account-section__title">Leaderboard</h3>
      <div className="friends-leaderboard__filters">
        <label>Scenario<select className="account-stats__select" value={scenario} onChange={(e) => { setScenario(e.target.value); setFaction(''); setFactionOptions([]); }}>
          <option value="">All scenarios</option>{scenarioOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select></label>
        <label>Alliance<select className="account-stats__select" value={alliance} onChange={(e) => setAlliance(e.target.value as typeof alliance)}>
          <option value="">All alliances</option><option value="good">Good</option><option value="evil">Evil</option>
        </select></label>
        {scenario && <label>Faction<select className="account-stats__select" value={faction} disabled={loading} onChange={(e) => setFaction(e.target.value)}>
          <option value="">All factions</option>{factionOptions.map((option) => <option key={option.id} value={option.id}>{option.name}</option>)}
        </select></label>}
      </div>
      <p className="account-subtle">Career XP and tiers. Filters apply to game stats.</p>
      {error ? <p className="account-msg account-msg--error" role="alert">{error} <button type="button" className="account-btn account-btn--small" onClick={() => { setFiltered({ key: '', stats: {}, error: null }); setRetry((value) => value + 1); }}>Retry</button></p> : loading ? <p className="account-subtle" role="status">Loading leaderboard…</p> : (
        <div className="friends-leaderboard__scroll">
          <table className="friends-leaderboard__table">
            <caption className="visually-hidden">You and your friends, ranked by {columns.find(([key]) => key === sort.key)?.[1]}, {sort.direction === 'desc' ? 'descending' : 'ascending'}</caption>
            <thead><tr><th scope="col">Username</th>{header('xp', 'XP')}<th scope="col">Tier</th>{columns.slice(1).map(([key, label]) => header(key, label))}</tr></thead>
            <tbody>{rows.map((entry) => {
              const profile = entry.profile;
              const xp = profile.xp ?? 0;
              const floor = profile.tier_floor_xp ?? profile.tier?.min_xp ?? 0;
              const ceiling = profile.next_tier_xp;
              const progress = ceiling == null ? 100 : Math.min(100, Math.max(0, (xp - floor) / Math.max(1, ceiling - floor) * 100));
              const self = profile.id === board.selfId;
              const avatar = profile.avatar ?? { image: 'hobbit.png', color: '#2d5a27' };
              const ring = tierRingProps(profile.tier);
              return <tr key={profile.id} className={self ? 'friends-leaderboard__self' : undefined}>
                <td className="friends-leaderboard__player">
                  <button type="button" className="friends-list__person" onClick={() => onOpen(profile)}>
                    <span className={`friends-list__avatar${ring.className}`} style={{ backgroundColor: avatar.color, ...ring.style }} aria-hidden>
                      <img src={`/assets/units/${avatar.image}`} alt="" />
                    </span>
                    <span className="friends-list__name">{profile.username}{self ? ' (you)' : ''}</span>
                  </button>
                </td>
                <td>{xp.toLocaleString()}</td>
                <td className="friends-leaderboard__progress">
                  <span className="friends-leaderboard__tier">{profile.tier?.name ?? 'Unranked'}</span>
                  <div className="account-xp__bar" role="progressbar" aria-label={`${profile.username}: ${profile.tier?.name ?? 'Unranked'} tier progress`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(progress)}>
                    <span className="account-xp__fill" style={{ width: `${progress}%`, background: profile.tier?.color }} />
                  </div>
                </td>
                {columns.slice(1).map(([key]) => { const value = leaderboardValue(entry.profile, entry.overall, key); return <td key={key}>{value === null ? '—' : key === 'win_pct' ? `${value.toFixed(1)}%` : value.toLocaleString()}</td>; })}
              </tr>;
            })}</tbody>
          </table>
        </div>
      )}
    </section>
  );
}

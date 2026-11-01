import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, setAuthToken } from '../../services/api';
import type { AuthPlayer, PlayerStats } from '../../services/api';
import ModalShell from './ModalShell';
import { tierRingProps } from './tierRing';

interface ProfileModalProps {
  player: AuthPlayer;
  onPlayerChange: (player: AuthPlayer) => void;
  onClose: () => void;
}

/** Discrete background colors. Darker swatches keep the unit readable. */
const COLOR_PALETTE = [
  '#1c3d18', '#2d5a27', '#3f7a34', '#5c8a3a', '#1e4d3a', '#2f6b4f',
  '#16324f', '#1a3a63', '#24507f', '#2f6b6b', '#3d7a8c', '#1f4d5c',
  '#3d2415', '#6b3a1f', '#8a4b24', '#a85a32', '#6b2e1a', '#4a3020',
  '#4a1c1c', '#6b2d2d', '#8a3d3d', '#a85a4a', '#7a3030', '#5c2424',
  '#2a1a40', '#4a2d6b', '#5b3a8a', '#6b4c9a', '#3d3058', '#5c4068',
  '#1f1f1f', '#3a342c', '#5c5346', '#8a6b1f', '#b89a4a', '#6b5a3e',
];

function formatPct(pct: number | null | undefined): string {
  if (pct == null) return '—';
  return `${pct}%`;
}

function formatDuration(totalSeconds: number | null | undefined): string {
  if (totalSeconds == null) return '—';
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.round((totalSeconds % 3600) / 60);
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function RecordLine({
  wins,
  losses,
  pct,
}: {
  wins: number;
  losses: number;
  pct: number | null | undefined;
}) {
  return (
    <>
      {wins}
      <span className="account-stats__sep">–</span>
      {losses}
      <span className="account-stats__pct">({formatPct(pct)})</span>
    </>
  );
}

export default function ProfileModal({ player, onPlayerChange, onClose }: ProfileModalProps) {
  const navigate = useNavigate();
  const [usernameEditing, setUsernameEditing] = useState(false);
  const [usernameDraft, setUsernameDraft] = useState(player.username);
  const [usernameError, setUsernameError] = useState<string | null>(null);
  const [usernameSuccess, setUsernameSuccess] = useState(false);
  const [savingUsername, setSavingUsername] = useState(false);

  const [avatarIcons, setAvatarIcons] = useState<string[]>([]);
  const [iconsError, setIconsError] = useState<string | null>(null);
  const [avatarImage, setAvatarImage] = useState(player.avatar?.image ?? 'hobbit.png');
  const [avatarColor, setAvatarColor] = useState(player.avatar?.color ?? '#2d5a27');
  const [editingAvatar, setEditingAvatar] = useState(false);
  const [colorOpen, setColorOpen] = useState(false);
  const [savingAvatar, setSavingAvatar] = useState(false);
  const [avatarError, setAvatarError] = useState<string | null>(null);
  const [avatarSuccess, setAvatarSuccess] = useState(false);

  const [stats, setStats] = useState<PlayerStats | null>(null);
  const [scenarioChoices, setScenarioChoices] = useState<PlayerStats['by_scenario']>([]);
  const [statsLoading, setStatsLoading] = useState(true);
  const [statsError, setStatsError] = useState<string | null>(null);
  const [scenarioFilter, setScenarioFilter] = useState('');
  const [logoutConfirm, setLogoutConfirm] = useState(false);

  useEffect(() => {
    setAvatarImage(player.avatar?.image ?? 'hobbit.png');
    setAvatarColor(player.avatar?.color ?? '#2d5a27');
  }, [player.avatar?.image, player.avatar?.color]);

  useEffect(() => {
    if (!editingAvatar || avatarIcons.length > 0) return;
    let cancelled = false;
    api
      .getAvatarIcons()
      .then((r) => {
        if (!cancelled) setAvatarIcons(r.icons);
      })
      .catch((e) => {
        if (!cancelled) setIconsError(e instanceof Error ? e.message : 'Could not load unit icons');
      });
    return () => {
      cancelled = true;
    };
  }, [editingAvatar, avatarIcons.length]);

  useEffect(() => {
    let cancelled = false;
    setStatsLoading(true);
    setStatsError(null);
    api
      .getPlayerStats(player.id, scenarioFilter ? { scenario: scenarioFilter } : undefined)
      .then((s) => {
        if (cancelled) return;
        setStats(s);
        if (!scenarioFilter) setScenarioChoices(s.by_scenario);
      })
      .catch((e) => {
        if (!cancelled) setStatsError(e instanceof Error ? e.message : 'Could not load stats');
      })
      .finally(() => {
        if (!cancelled) setStatsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [player.id, scenarioFilter]);

  const avatarDirty =
    avatarImage !== (player.avatar?.image ?? 'hobbit.png') ||
    avatarColor !== (player.avatar?.color ?? '#2d5a27');

  const closeAvatarEdit = () => {
    setEditingAvatar(false);
    setColorOpen(false);
    setAvatarImage(player.avatar?.image ?? 'hobbit.png');
    setAvatarColor(player.avatar?.color ?? '#2d5a27');
    setAvatarError(null);
  };

  const saveAvatar = async () => {
    setAvatarError(null);
    setAvatarSuccess(false);
    setSavingAvatar(true);
    try {
      const updated = await api.updateMyProfile({ avatar: { image: avatarImage, color: avatarColor } });
      onPlayerChange(updated);
      setAvatarSuccess(true);
      setEditingAvatar(false);
      setColorOpen(false);
    } catch (e) {
      setAvatarError(e instanceof Error ? e.message : 'Could not save avatar');
    } finally {
      setSavingAvatar(false);
    }
  };

  const startUsernameEdit = () => {
    setUsernameDraft(player.username);
    setUsernameError(null);
    setUsernameSuccess(false);
    setUsernameEditing(true);
  };

  const cancelUsernameEdit = () => {
    setUsernameEditing(false);
    setUsernameDraft(player.username);
    setUsernameError(null);
  };

  const saveUsername = async () => {
    const trimmed = usernameDraft.trim();
    if (!trimmed) {
      setUsernameError('Username cannot be empty');
      return;
    }
    setUsernameError(null);
    setUsernameSuccess(false);
    setSavingUsername(true);
    try {
      const updated = await api.updateMyProfile({ username: trimmed });
      onPlayerChange(updated);
      setUsernameEditing(false);
      setUsernameSuccess(true);
    } catch (e) {
      setUsernameError(e instanceof Error ? e.message : 'Could not update username');
    } finally {
      setSavingUsername(false);
    }
  };

  const handleLogout = () => {
    setAuthToken(null);
    onClose();
    navigate('/login', { replace: true });
  };

  const xp = player.xp ?? 0;
  const floor = player.tier_floor_xp ?? player.tier?.min_xp ?? 0;
  const ceiling = player.next_tier_xp;
  const span = ceiling != null ? Math.max(1, ceiling - floor) : 1;
  const xpPct = ceiling != null ? Math.min(100, Math.max(0, Math.round(((xp - floor) / span) * 100))) : 100;
  const xpToNext = ceiling != null ? Math.max(0, ceiling - xp) : null;

  const ring = tierRingProps(player.tier);
  const scenarios = stats?.by_scenario ?? [];

  return (
    <ModalShell title="Profile" onClose={onClose} className="account-modal--profile">
      <section className="account-section profile-avatar">
        <div className="profile-avatar-preview-wrap">
          <div className="profile-avatar-stage">
            <div
              className={`profile-avatar-preview${ring.className}`}
              style={{ backgroundColor: avatarColor, ...ring.style }}
              aria-hidden
            >
              <img className="profile-avatar-preview__img" src={`/assets/units/${avatarImage}`} alt="" />
            </div>
            <button
              type="button"
              className={`profile-avatar-pencil${editingAvatar ? ' profile-avatar-pencil--open' : ''}`}
              aria-label={editingAvatar ? 'Close avatar editor' : 'Edit avatar'}
              aria-pressed={editingAvatar}
              onClick={() => (editingAvatar ? closeAvatarEdit() : setEditingAvatar(true))}
            >
              <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden>
                <path
                  fill="currentColor"
                  d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"
                />
              </svg>
            </button>
          </div>
          {player.tier?.name && <p className="profile-avatar-tier">{player.tier.name}</p>}
        </div>

        {editingAvatar && (
          <div className="profile-avatar-editor">
            <p className="account-subtle">Choose a unit icon and background color.</p>
            {iconsError && (
              <p className="account-msg account-msg--error" role="alert">
                {iconsError}
              </p>
            )}
            <div className="profile-avatar-icons" role="listbox" aria-label="Unit icons">
              {avatarIcons.map((name) => {
                const selected = name === avatarImage;
                return (
                  <button
                    key={name}
                    type="button"
                    role="option"
                    aria-selected={selected}
                    aria-label={name.replace(/\.png$/i, '').replace(/_/g, ' ')}
                    className={`profile-avatar-icon${selected ? ' profile-avatar-icon--selected' : ''}`}
                    onClick={() => {
                      setAvatarImage(name);
                      setAvatarSuccess(false);
                    }}
                  >
                    <img src={`/assets/units/${name}`} alt="" />
                  </button>
                );
              })}
            </div>
            <div className="profile-color">
              <button
                type="button"
                className="profile-color-trigger"
                style={{ ['--swatch' as string]: avatarColor }}
                aria-expanded={colorOpen}
                aria-label="Choose background color"
                onClick={() => setColorOpen((open) => !open)}
              >
                <span className="profile-color-trigger__chip" aria-hidden />
                Background color
              </button>
              {colorOpen && (
                <div className="profile-color-grid" role="listbox" aria-label="Background colors">
                  {COLOR_PALETTE.map((color) => {
                    const selected = color.toLowerCase() === avatarColor.toLowerCase();
                    return (
                      <button
                        key={color}
                        type="button"
                        role="option"
                        aria-selected={selected}
                        aria-label={color}
                        className={`profile-color-swatch${selected ? ' profile-color-swatch--selected' : ''}`}
                        style={{ ['--swatch' as string]: color }}
                        onClick={() => {
                          setAvatarColor(color);
                          setAvatarSuccess(false);
                          setColorOpen(false);
                        }}
                      />
                    );
                  })}
                </div>
              )}
            </div>
            {avatarDirty && (
              <div className="profile-username-actions">
                <button type="button" className="account-btn" onClick={() => void saveAvatar()} disabled={savingAvatar}>
                  {savingAvatar ? 'Saving…' : 'Save avatar'}
                </button>
              </div>
            )}
            {avatarError && (
              <p className="account-msg account-msg--error" role="alert">
                {avatarError}
              </p>
            )}
          </div>
        )}
        {avatarSuccess && !editingAvatar && <p className="account-msg account-msg--success">Avatar saved.</p>}
      </section>

      <section className="account-section" aria-label="Tier and XP">
        <div className="account-xp">
          <div className="account-xp__row">
            <span className="account-xp__tier">{player.tier?.name ?? '—'}</span>
            <span className="account-xp__xp">{xp} XP</span>
          </div>
          <div
            className="account-xp__bar"
            role="progressbar"
            aria-valuemin={floor}
            aria-valuemax={ceiling ?? xp}
            aria-valuenow={xp}
            aria-label="XP toward the next tier"
          >
            <span className="account-xp__fill" style={{ width: `${xpPct}%` }} />
          </div>
          <p className="account-subtle">
            {player.next_tier && xpToNext != null
              ? `${xpToNext} XP to ${player.next_tier.name}`
              : 'Highest tier'}
          </p>
        </div>
      </section>

      <section className="account-section">
        <h3 className="account-section__title">Stats</h3>
        {statsLoading && !stats && <p className="account-msg account-msg--subtle">Loading stats…</p>}
        {statsError && (
          <p className="account-msg account-msg--error" role="alert">
            {statsError}
          </p>
        )}
        {stats && (
          <div className="account-stats">
            <div className="account-stats__headline">
              <span className="account-stats__record">
                <RecordLine wins={stats.overall.wins} losses={stats.overall.losses} pct={stats.overall.win_pct} />
              </span>
              <span className="account-stats__games">
                {stats.overall.games} {stats.overall.games === 1 ? 'game' : 'games'}
              </span>
            </div>
            <div className="account-stats__alliances">
              <span className="account-stats__alliance account-stats__alliance--good">
                Good {stats.by_alliance.good.wins}
                <span className="account-stats__sep">–</span>
                {stats.by_alliance.good.games - stats.by_alliance.good.wins}{' '}
                <span className="account-stats__pct">({formatPct(stats.by_alliance.good.win_pct)})</span>
              </span>
              <span className="account-stats__alliance account-stats__alliance--evil">
                Evil {stats.by_alliance.evil.wins}
                <span className="account-stats__sep">–</span>
                {stats.by_alliance.evil.games - stats.by_alliance.evil.wins}{' '}
                <span className="account-stats__pct">({formatPct(stats.by_alliance.evil.win_pct)})</span>
              </span>
            </div>
            {scenarioChoices.length > 0 && (
              <label className="account-stats__row">
                <span className="account-stats__filter-label">Scenario</span>
                <select
                  className="account-stats__select"
                  value={scenarioFilter}
                  onChange={(e) => setScenarioFilter(e.target.value)}
                >
                  <option value="">All scenarios</option>
                  {scenarioChoices.map((row) => (
                    <option key={row.setup_id ?? row.scenario_name} value={row.setup_id ?? ''}>
                      {row.scenario_name} ({row.games})
                    </option>
                  ))}
                </select>
              </label>
            )}
            {scenarios.length > 0 && (
              <div className="account-stats__table-wrap">
                <table className="account-stats__table">
                  <thead>
                    <tr>
                      <th>Scenario</th>
                      <th>Games</th>
                      <th>Wins</th>
                      <th>Win %</th>
                    </tr>
                  </thead>
                  <tbody>
                    {scenarios.map((row) => (
                      <tr key={row.setup_id ?? row.scenario_name}>
                        <td>{row.scenario_name}</td>
                        <td>{row.games}</td>
                        <td>{row.wins}</td>
                        <td>{formatPct(row.win_pct)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {stats.head_to_head.length > 0 && (
              <>
                <h4 className="account-stats__subhead">Head to head</h4>
                <div className="account-stats__table-wrap">
                  <table className="account-stats__table">
                    <thead>
                      <tr>
                        <th>Player</th>
                        <th>Vs/With</th>
                        <th>Games</th>
                        <th>W–L</th>
                        <th>Win %</th>
                      </tr>
                    </thead>
                    <tbody>
                      {stats.head_to_head.map((row) => (
                        <tr key={`${row.kind}-${row.player_id}`}>
                          <td>{row.username ?? 'Unknown'}</td>
                          <td>{row.kind === 'with' ? 'with' : 'vs'}</td>
                          <td>{row.games}</td>
                          <td>
                            {row.wins}–{row.losses}
                          </td>
                          <td>{formatPct(row.win_pct)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
            <dl className="account-stats__extras">
              <div className="account-stats__extra">
                <dt>Best streak</dt>
                <dd>{stats.streaks.best_win_streak}</dd>
              </div>
              <div className="account-stats__extra">
                <dt>Longest game</dt>
                <dd>{formatDuration(stats.longest_game_seconds)}</dd>
              </div>
            </dl>
          </div>
        )}
      </section>

      <section className="account-section">
        <h3 className="account-section__title">Username</h3>
        {usernameEditing ? (
          <div className="profile-username-form">
            <input
              className="account-input"
              type="text"
              aria-label="Username"
              value={usernameDraft}
              onChange={(e) => setUsernameDraft(e.target.value)}
              maxLength={50}
              autoFocus
              disabled={savingUsername}
            />
            <div className="profile-username-actions">
              <button type="button" className="account-btn" onClick={cancelUsernameEdit} disabled={savingUsername}>
                Cancel
              </button>
              <button type="button" className="account-btn" onClick={() => void saveUsername()} disabled={savingUsername}>
                {savingUsername ? 'Saving…' : 'Save'}
              </button>
            </div>
          </div>
        ) : (
          <div className="profile-username-row">
            <p className="profile-username-display">{player.username}</p>
            <button type="button" className="account-btn" onClick={startUsernameEdit}>
              Change
            </button>
          </div>
        )}
        {usernameError && (
          <p className="account-msg account-msg--error" role="alert">
            {usernameError}
          </p>
        )}
        {usernameSuccess && <p className="account-msg account-msg--success">Username updated.</p>}
      </section>

      <section className="account-section account-section--last">
        {logoutConfirm ? (
          <div className="profile-logout-confirm">
            <p className="account-subtle">Log out of this account?</p>
            <div className="profile-username-actions">
              <button type="button" className="account-btn" onClick={() => setLogoutConfirm(false)}>
                Cancel
              </button>
              <button type="button" className="account-btn account-btn--danger" onClick={handleLogout}>
                Log out
              </button>
            </div>
          </div>
        ) : (
          <button type="button" className="account-btn account-btn--danger" onClick={() => setLogoutConfirm(true)}>
            Log out
          </button>
        )}
      </section>
    </ModalShell>
  );
}

import { Fragment, useEffect, useState } from 'react';
import { api } from '../../services/api';
import type { AdminUserItem, ProgressionConfig } from '../../services/api';
import { NumberField } from './NumberField';
import { TierXpPanel } from './TierXpPanel';
import { ColorField } from './ColorField';

function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    });
  } catch {
    return iso;
  }
}

export function UsersReportModal({ onClose }: { onClose: () => void }) {
  const [tab, setTab] = useState<'users' | 'tiers'>('users');
  const [users, setUsers] = useState<AdminUserItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [adjustId, setAdjustId] = useState<string | null>(null);
  const [adjustAmount, setAdjustAmount] = useState('25');
  const [adjustReason, setAdjustReason] = useState('');
  const [adjusting, setAdjusting] = useState(false);

  const [progression, setProgression] = useState<ProgressionConfig | null>(null);
  const [savedProgression, setSavedProgression] = useState('');
  const [progressionError, setProgressionError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [backfillArmed, setBackfillArmed] = useState(false);
  const [backfilling, setBackfilling] = useState(false);
  const [backfillNote, setBackfillNote] = useState<string | null>(null);

  const [defaultColor, setDefaultColor] = useState<string>('#2d5a27');
  const [defaultColorDirty, setDefaultColorDirty] = useState(false);
  const [defaultsLoading, setDefaultsLoading] = useState(true);
  const [defaultsSaving, setDefaultsSaving] = useState(false);
  const [defaultsError, setDefaultsError] = useState<string | null>(null);
  const [defaultsSaved, setDefaultsSaved] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .adminListUsers()
      .then((r) => {
        if (!cancelled) setUsers(r.users ?? []);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Could not load users');
      });
    api
      .adminGetProfileDefaults()
      .then((r) => {
        if (!cancelled) {
          setDefaultColor(r.default_avatar_color);
          setDefaultsLoading(false);
        }
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setDefaultsError(e instanceof Error ? e.message : 'Could not load profile defaults');
          setDefaultsLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (tab !== 'tiers' || progression != null) return;
    let cancelled = false;
    api
      .adminGetProgression()
      .then((cfg) => {
        if (cancelled) return;
        setProgression(cfg);
        setSavedProgression(JSON.stringify(cfg));
      })
      .catch((e: unknown) => {
        if (!cancelled) setProgressionError(e instanceof Error ? e.message : 'Could not load tiers');
      });
    return () => {
      cancelled = true;
    };
  }, [tab, progression]);

  const dirty = progression != null && JSON.stringify(progression) !== savedProgression;

  const saveProgression = async () => {
    if (!progression) return;
    setSaving(true);
    setProgressionError(null);
    try {
      const res = await api.adminPutProgression(progression);
      setProgression(res.progression);
      setSavedProgression(JSON.stringify(res.progression));
      setUsers(null);
      const listed = await api.adminListUsers();
      setUsers(listed.users ?? []);
    } catch (e: unknown) {
      setProgressionError(e instanceof Error ? e.message : 'Could not save');
    } finally {
      setSaving(false);
    }
  };

  const applyAdjust = async (user: AdminUserItem) => {
    const amount = Number(adjustAmount);
    if (!Number.isInteger(amount) || amount === 0) {
      setError('Enter a whole number other than 0. Use a negative number to remove XP.');
      return;
    }
    const reason = adjustReason.trim();
    if (!reason) {
      setError('A reason is required.');
      return;
    }
    setAdjusting(true);
    setError(null);
    try {
      const res = await api.adminAdjustUserXp(user.id, amount, reason);
      setUsers((prev) =>
        (prev ?? []).map((row) =>
          row.id === user.id ? { ...row, xp: res.xp, tier_id: res.tier_id, tier_name: res.tier_name } : row,
        ),
      );
      setAdjustId(null);
      setAdjustReason('');
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not adjust XP');
    } finally {
      setAdjusting(false);
    }
  };

  const runBackfill = async () => {
    if (!backfillArmed) {
      setBackfillArmed(true);
      setBackfillNote('Click again to award XP for finished games that have none yet. This uses the saved rules, not unsaved edits.');
      return;
    }
    setBackfilling(true);
    setProgressionError(null);
    try {
      const res = await api.adminBackfillProgression();
      setBackfillNote(
        `Awarded ${res.xp} XP across ${res.awarded} player${res.awarded === 1 ? '' : 's'}` +
          (res.unreadable ? `. ${res.unreadable} game${res.unreadable === 1 ? '' : 's'} could not be read.` : '.'),
      );
      setBackfillArmed(false);
      const listed = await api.adminListUsers();
      setUsers(listed.users ?? []);
    } catch (e: unknown) {
      setProgressionError(e instanceof Error ? e.message : 'Backfill failed');
    } finally {
      setBackfilling(false);
    }
  };

  const saveDefaults = async () => {
    setDefaultsSaving(true);
    setDefaultsError(null);
    setDefaultsSaved(false);
    try {
      const r = await api.adminPutProfileDefaults(defaultColor);
      setDefaultColor(r.default_avatar_color);
      setDefaultColorDirty(false);
      setDefaultsSaved(true);
    } catch (e: unknown) {
      setDefaultsError(e instanceof Error ? e.message : 'Could not save profile defaults');
    } finally {
      setDefaultsSaving(false);
    }
  };

  return (
    <div className="admin-modal-overlay" role="presentation" onClick={onClose}>
      <div
        className="admin-modal admin-modal--games"
        role="dialog"
        aria-modal="true"
        aria-labelledby="admin-users-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="games-report__header">
          <div>
            <h2 id="admin-users-title" className="admin-modal__title">
              Users
            </h2>
            <p className="admin-form__micro">
              {tab === 'users'
                ? users == null
                  ? 'Loading…'
                  : `${users.length} user${users.length === 1 ? '' : 's'}`
                : 'Tier names, thresholds, frames, and how XP is earned'}
            </p>
          </div>
          <button type="button" className="admin-page__btn" onClick={onClose}>
            Close
          </button>
        </div>

        <div className="games-report__filters" role="tablist" aria-label="User admin">
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'users'}
            className={`games-report__filter${tab === 'users' ? ' games-report__filter--active' : ''}`}
            onClick={() => setTab('users')}
          >
            Users
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'tiers'}
            className={`games-report__filter${tab === 'tiers' ? ' games-report__filter--active' : ''}`}
            onClick={() => setTab('tiers')}
          >
            Tier & XP
          </button>
        </div>

        {tab === 'users' ? (
          <>
            {error ? <p className="admin-page__error games-report__error">{error}</p> : null}
            <div className="users-report__defaults">
              <p className="admin-form__micro">
                Default avatar color for players who have not picked one. A chosen color stays put.
              </p>
              <div className="users-report__defaults-row">
                <ColorField
                  value={defaultColor}
                  ariaLabel="Default avatar color"
                  disabled={defaultsLoading || defaultsSaving}
                  onApply={(color) => {
                    setDefaultColor(color);
                    setDefaultColorDirty(true);
                    setDefaultsSaved(false);
                    setDefaultsError(null);
                  }}
                />
                {defaultColorDirty && (
                  <button
                    type="button"
                    className="admin-page__btn"
                    onClick={() => void saveDefaults()}
                    disabled={defaultsSaving || !/^#[0-9a-fA-F]{6}$/.test(defaultColor.trim())}
                  >
                    {defaultsSaving ? 'Saving…' : 'Save Default Color'}
                  </button>
                )}
                {defaultsSaved && !defaultsError && !defaultColorDirty && (
                  <span className="admin-form__micro">Saved.</span>
                )}
              </div>
              {defaultsError ? (
                <p className="admin-page__error users-report__defaults-error">{defaultsError}</p>
              ) : null}
            </div>
            <div className="games-report__table-scroll">
              {users != null ? (
                users.length === 0 ? (
                  <p className="admin-form__micro">No users found.</p>
                ) : (
                  <table className="games-report__table">
                    <thead>
                      <tr>
                        <th>Username</th>
                        <th>Email</th>
                        <th>Tier</th>
                        <th>XP</th>
                        <th>Last login</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {users.map((u) => (
                        <Fragment key={u.id}>
                          <tr>
                            <td className="games-report__name">{u.username}</td>
                            <td>{u.email}</td>
                            <td>{u.tier_name || '—'}</td>
                            <td>{u.xp ?? 0}</td>
                            <td>{formatDateTime(u.last_login)}</td>
                            <td>
                              <button
                                type="button"
                                className="admin-page__btn"
                                onClick={() => {
                                  setError(null);
                                  setAdjustId(adjustId === u.id ? null : u.id);
                                  setAdjustReason('');
                                }}
                              >
                                Adjust XP
                              </button>
                            </td>
                          </tr>
                          {adjustId === u.id ? (
                            <tr>
                              <td colSpan={6}>
                                <div className="admin-tier-adjust">
                                  <label className="admin-form__label">
                                    Amount
                                    <NumberField
                                      className="admin-form__input admin-form__input--narrow"
                                      aria-label={`XP amount for ${u.username}`}
                                      value={adjustAmount}
                                      onChange={(e) => setAdjustAmount(e.target.value)}
                                    />
                                  </label>
                                  <label className="admin-form__label admin-tier-adjust__reason">
                                    Reason
                                    <input
                                      className="admin-form__input"
                                      value={adjustReason}
                                      maxLength={200}
                                      onChange={(e) => setAdjustReason(e.target.value)}
                                    />
                                  </label>
                                  <button
                                    type="button"
                                    className="admin-page__btn"
                                    disabled={adjusting}
                                    onClick={() => void applyAdjust(u)}
                                  >
                                    {adjusting ? 'Saving…' : 'Apply'}
                                  </button>
                                </div>
                              </td>
                            </tr>
                          ) : null}
                        </Fragment>
                      ))}
                    </tbody>
                  </table>
                )
              ) : null}
            </div>
          </>
        ) : (
          <>
            <div className="games-report__filters">
              <button
                type="button"
                className="admin-page__btn"
                disabled={!dirty || saving || !progression}
                onClick={() => void saveProgression()}
              >
                {saving ? 'Saving…' : 'Save'}
              </button>
              <button
                type="button"
                className="admin-page__btn"
                disabled={!dirty || saving || !savedProgression}
                onClick={() => {
                  setProgression(JSON.parse(savedProgression) as ProgressionConfig);
                  setProgressionError(null);
                }}
              >
                Revert
              </button>
              <button
                type="button"
                className="admin-page__btn"
                disabled={backfilling}
                onClick={() => void runBackfill()}
              >
                {backfilling ? 'Awarding…' : backfillArmed ? 'Confirm backfill' : 'Backfill XP from finished games'}
              </button>
            </div>
            {progressionError ? <p className="admin-page__error games-report__error">{progressionError}</p> : null}
            {backfillNote ? <p className="admin-form__micro">{backfillNote}</p> : null}
            <div className="games-report__table-scroll">
              {progression ? (
                <TierXpPanel config={progression} onChange={setProgression} />
              ) : !progressionError ? (
                <p className="admin-form__micro">Loading…</p>
              ) : null}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

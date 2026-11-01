import { useEffect, useState } from 'react';
import { api } from '../../services/api';
import { ColorField } from './ColorField';
import type { AdminUserItem } from '../../services/api';

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
  const [users, setUsers] = useState<AdminUserItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

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
              {users == null ? 'Loading…' : `${users.length} user${users.length === 1 ? '' : 's'}`}
            </p>
          </div>
          <button type="button" className="admin-page__btn" onClick={onClose}>
            Close
          </button>
        </div>

        {error ? <p className="admin-page__error games-report__error">{error}</p> : null}

        <div className="users-report__defaults">
          <p className="admin-form__micro">
            Default avatar color for new users (users who already picked a color keep theirs).
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
          {users != null && !error ? (
            users.length === 0 ? (
              <p className="admin-form__micro">No users found.</p>
            ) : (
              <table className="games-report__table">
                <thead>
                  <tr>
                    <th>Username</th>
                    <th>Email</th>
                    <th>Last login</th>
                  </tr>
                </thead>
                <tbody>
                  {users.map((u) => (
                    <tr key={u.id}>
                      <td className="games-report__name">{u.username}</td>
                      <td>{u.email}</td>
                      <td>{formatDateTime(u.last_login)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          ) : null}
        </div>
      </div>
    </div>
  );
}

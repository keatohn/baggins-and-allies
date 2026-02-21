import { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { api, getAuthToken, setAuthToken } from '../services/api';
import type { AuthPlayer } from '../services/api';
import './Profile.css';

export default function Profile() {
  const navigate = useNavigate();
  const [player, setPlayer] = useState<AuthPlayer | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!getAuthToken()) {
      navigate('/', { replace: true });
      return;
    }
    api.authMe().then(setPlayer).catch(() => navigate('/', { replace: true })).finally(() => setLoading(false));
  }, [navigate]);

  const handleLogout = () => {
    setAuthToken(null);
    navigate('/', { replace: true });
  };

  if (loading) return <div className="profile-page">Loading…</div>;
  if (!player) return null;

  return (
    <div className="profile-page">
      <h1 className="profile-page__title">Profile</h1>
      <div className="profile-page__card">
        <p><strong>Username</strong> {player.username}</p>
        <p><strong>Email</strong> {player.email}</p>
        <button type="button" className="profile-page__logout" onClick={handleLogout}>
          Log out
        </button>
      </div>
      <Link to="/" className="profile-page__menu-btn">Menu</Link>
    </div>
  );
}

import React, { useState } from 'react';
import { useLocation } from 'react-router-dom';
import { api, setAuthToken } from '../services/api';
import { useMenuScroll } from '../components/MenuScrollContext';
import MenuLink from '../components/MenuLink';
import './Auth.css';

export default function Login() {
  const { navigateWithScroll } = useMenuScroll();
  const location = useLocation();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [resetNotice] = useState(() =>
    (location.state as { passwordReset?: boolean } | null)?.passwordReset
      ? 'Password updated. You can now log in.'
      : null,
  );
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const res = await api.login(email.trim(), password);
      setAuthToken(res.access_token);
      navigateWithScroll('/', { replace: true });
    } catch (err) {
      const raw = err instanceof Error ? err.message : 'Login failed';
      const isNetwork =
        raw.includes('timed out') ||
        raw.includes('backend running') ||
        raw.includes('Failed to fetch') ||
        raw.includes('Load failed') ||
        raw.includes('NetworkError') ||
        raw.includes('Network request failed');
      const devHint = import.meta.env.DEV ? ' Run the full app from project root: npm run dev' : '';
      const prodNetworkHint =
        ' Can’t reach the API. On Railway, set CORS_ORIGINS to include this site’s exact https:// origin (and www if you use it). GitHub Actions must build with VITE_API_URL = your Railway https URL.';
      const message = isNetwork
        ? `${raw}${import.meta.env.DEV ? devHint : prodNetworkHint}`
        : raw || 'Invalid email or password.';
      setError(message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="auth-page">
      <h1 className="auth-page__title">Log in</h1>
      <form className="auth-form" onSubmit={handleSubmit}>
        {resetNotice && <p className="auth-form__notice">{resetNotice}</p>}
        {error && <p className="auth-form__error">{error}</p>}
        <label className="auth-form__label">
          Email
          <input
            type="email"
            className="auth-form__input"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            autoComplete="email"
          />
        </label>
        <label className="auth-form__label">
          Password
          <input
            type="password"
            className="auth-form__input"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            autoComplete="current-password"
          />
        </label>
        <button type="submit" className="auth-form__submit primary" disabled={loading}>
          {loading ? 'Logging in…' : 'Log in'}
        </button>
      </form>
      <p className="auth-page__footer">
        <MenuLink to="/forgot-password">Forgot password?</MenuLink>
      </p>
      <p className="auth-page__footer">
        Don’t have an account? <MenuLink to="/register">Create Account</MenuLink>
      </p>
    </div>
  );
}

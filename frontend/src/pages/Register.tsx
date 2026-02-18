import React, { useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { api, setAuthToken } from '../services/api';
import './Auth.css';

export default function Register() {
  const navigate = useNavigate();
  const [email, setEmail] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!/^[a-zA-Z0-9_]{2,32}$/.test(username.trim())) {
      setError('Username must be 2–32 characters, letters numbers and underscore only');
      return;
    }
    setLoading(true);
    try {
      const res = await api.register(email.trim(), username.trim(), password);
      setAuthToken(res.access_token);
      navigate('/', { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Registration failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="auth-page">
      <h1 className="auth-page__title">Create Account</h1>
      <form className="auth-form" onSubmit={handleSubmit}>
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
          Username
          <input
            type="text"
            className="auth-form__input"
            value={username}
            onChange={(e) => {
              const next = e.target.value.replace(/[^a-zA-Z0-9_]/g, '').slice(0, 32);
              setUsername(next);
            }}
            required
            minLength={2}
            maxLength={32}
            pattern="[a-zA-Z0-9_]+"
            title="Letters, numbers and underscore only"
            autoComplete="username"
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
            autoComplete="new-password"
          />
        </label>
        <button type="submit" className="auth-form__submit primary" disabled={loading}>
          {loading ? 'Creating account…' : 'Create Account'}
        </button>
      </form>
      <p className="auth-page__footer">
        Already have an account? <Link to="/login">Log in</Link>
      </p>
    </div>
  );
}

import React, { useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { api } from '../services/api';
import './Auth.css';

/**
 * Forgot-password flow: request a temp password by email, then paste it
 * together with a new password. The temp password is never a login credential.
 */
export default function ForgotPassword() {
  const navigate = useNavigate();
  const [step, setStep] = useState<'request' | 'reset'>('request');
  const [email, setEmail] = useState('');
  const [tempPassword, setTempPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleRequest = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      await api.forgotPassword(email.trim());
      setNotice('If that email is registered, a temporary password has been sent.');
      setStep('reset');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Request failed. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  const handleReset = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (newPassword.trim().length === 0) {
      setError('New password is required.');
      return;
    }
    setLoading(true);
    try {
      await api.resetPassword(email.trim(), tempPassword.trim(), newPassword);
      navigate('/login', { replace: true, state: { passwordReset: true } });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Reset failed. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  if (step === 'request') {
    return (
      <div className="auth-page">
        <h1 className="auth-page__title">Forgot password</h1>
        <form className="auth-form" onSubmit={handleRequest}>
          {error && <p className="auth-form__error">{error}</p>}
          <p className="auth-form__hint">
            Enter your email and we’ll send you a temporary password. It expires shortly after it’s
            sent, and you’ll use it here to choose a new password.
          </p>
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
          <button type="submit" className="auth-form__submit primary" disabled={loading}>
            {loading ? 'Sending…' : 'Send temporary password'}
          </button>
        </form>
        <p className="auth-page__footer">
          Remembered it? <Link to="/login">Log in</Link>
        </p>
      </div>
    );
  }

  return (
    <div className="auth-page">
      <h1 className="auth-page__title">Reset password</h1>
      <form className="auth-form" onSubmit={handleReset}>
        {notice && <p className="auth-form__notice">{notice}</p>}
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
          Temporary password
          <input
            type="text"
            className="auth-form__input"
            value={tempPassword}
            onChange={(e) => setTempPassword(e.target.value)}
            required
            autoComplete="one-time-code"
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
          />
        </label>
        <label className="auth-form__label">
          New password
          <input
            type="password"
            className="auth-form__input"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            required
            autoComplete="new-password"
          />
        </label>
        <button type="submit" className="auth-form__submit primary" disabled={loading}>
          {loading ? 'Resetting…' : 'Reset password'}
        </button>
      </form>
      <p className="auth-page__footer">
        Need a new temporary password?{' '}
        <a
          href="#resend"
          onClick={(e) => {
            e.preventDefault();
            setStep('request');
            setTempPassword('');
            setNewPassword('');
            setNotice(null);
            setError(null);
          }}
        >
          Send another
        </a>
      </p>
    </div>
  );
}

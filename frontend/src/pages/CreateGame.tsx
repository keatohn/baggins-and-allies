import React, { useState, useEffect, useRef } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { api, type SetupInfo } from '../services/api';
import ScenarioTimeline from '../components/ScenarioTimeline';
import './CreateGame.css';

function RuleLabel({ name, description }: { name: string; description?: string }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);
  const hovering = useRef(false);

  useEffect(() => {
    if (!open) return;
    const close = (e: PointerEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('pointerdown', close);
    return () => document.removeEventListener('pointerdown', close);
  }, [open]);

  return (
    <span className="create-game-form__field-label create-game-form__rule-label" ref={ref}>
      {name}
      {description && (
        <>
          <button
            type="button"
            className="create-game-form__rule-info"
            aria-label={`What is ${name}?`}
            aria-expanded={open}
            onClick={() => setOpen((prev) => hovering.current || !prev)}
            onPointerEnter={(e) => {
              if (e.pointerType !== 'mouse') return;
              hovering.current = true;
              setOpen(true);
            }}
            onPointerLeave={(e) => {
              if (e.pointerType !== 'mouse') return;
              hovering.current = false;
              setOpen(false);
            }}
          >
            ?
          </button>
          {open && (
            <span className="create-game-form__rule-tip" role="tooltip">{description}</span>
          )}
        </>
      )}
    </span>
  );
}

export default function CreateGame() {
  const navigate = useNavigate();
  const [name, setName] = useState('');
  const [isMultiplayer, setIsMultiplayer] = useState(false);
  const [heroesEnabled, setHeroesEnabled] = useState(true);
  const [shadowOfWar, setShadowOfWar] = useState(false);
  const [optionalRules, setOptionalRules] = useState<Record<string, boolean>>({});
  const [setups, setSetups] = useState<SetupInfo[]>([]);
  const [ruleDescriptions, setRuleDescriptions] = useState<Record<string, string>>({});
  const [selectedSetupId, setSelectedSetupId] = useState<string | null>(null);
  const [loadingSetups, setLoadingSetups] = useState(true);
  const [setupsError, setSetupsError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [step, setStep] = useState<'scenario' | 'settings'>('scenario');

  // Backend returns only is_active + context scenarios; frontend filter matches that contract
  const scenariosWithContext = setups.filter(
    (s) => s.context && typeof s.context === 'object' && Object.keys(s.context).length > 0
  );

  useEffect(() => {
    let cancelled = false;
    setSetupsError(null);
    api.getSetups().then(({ setups: list, rule_descriptions: descriptions }) => {
      if (!cancelled) {
        setSetups(Array.isArray(list) ? list : []);
        setRuleDescriptions(descriptions ?? {});
        const withContext = (Array.isArray(list) ? list : []).filter(
          (s) => s.context && typeof s.context === 'object' && Object.keys(s.context).length > 0
        );
        if (withContext.length > 0 && selectedSetupId === null) setSelectedSetupId(withContext[0].id);
      }
    }).catch((err) => {
      if (!cancelled) {
        setSetups([]);
        setSetupsError(err instanceof Error ? err.message : 'Could not load scenarios. Is the backend running?');
      }
    }).finally(() => {
      if (!cancelled) setLoadingSetups(false);
    });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (scenariosWithContext.length > 0 && (selectedSetupId === null || !scenariosWithContext.some((s) => s.id === selectedSetupId)))
      setSelectedSetupId(scenariosWithContext[0].id);
  }, [scenariosWithContext, selectedSetupId]);

  useEffect(() => {
    const scenario = setups.find((s) => s.id === selectedSetupId);
    const next: Record<string, boolean> = {};
    for (const rule of scenario?.optional_rules ?? []) next[rule.type] = true;
    setOptionalRules(next);
    if (scenario?.rings_of_power) setHeroesEnabled(true);
  }, [selectedSetupId, setups]);

  const selectedScenario = scenariosWithContext.find((s) => s.id === selectedSetupId) ?? null;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const res = await api.createGame(
        name.trim() || 'My game',
        isMultiplayer,
        selectedSetupId ?? undefined,
        heroesEnabled,
        shadowOfWar,
        Boolean(selectedScenario?.rings_of_power) || optionalRules.rings_of_power === true,
        optionalRules,
      );
      const initialState = res.state != null
        ? { ...res.state, turn_order: res.turn_order ?? res.state.turn_order }
        : undefined;
      navigate(`/game/${res.game_id}`, {
        replace: true,
        state: initialState != null ? { initialState, gameId: res.game_id } : undefined,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create game');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="create-game-page">
      <h1 className="create-game-page__title">Create game</h1>
      {step === 'scenario' ? (
        <div className="create-game-form">
          <div className="create-game-form__field create-game-form__field--scenario">
            <span className="create-game-form__field-label">Scenario</span>
            {loadingSetups ? (
              <p className="create-game-form__hint">Loading scenarios…</p>
            ) : setupsError ? (
              <p className="create-game-form__error create-game-form__hint" role="alert">
                {setupsError}
              </p>
            ) : scenariosWithContext.length === 0 ? (
              <p className="create-game-form__hint">No scenarios available.</p>
            ) : (
              <ScenarioTimeline
                scenarios={scenariosWithContext}
                selectedId={selectedSetupId}
                onSelect={setSelectedSetupId}
              />
            )}
          </div>
          {scenariosWithContext.length > 0 && (
            <button
              type="button"
              className="create-game-form__submit primary create-game-form__continue"
              disabled={loadingSetups || selectedSetupId == null}
              onClick={() => setStep('settings')}
            >
              Continue
            </button>
          )}
        </div>
      ) : (
        <form className="create-game-form create-game-form--settings" onSubmit={handleSubmit}>
          <button type="button" className="page-menu-btn" onClick={() => setStep('scenario')}>
            Back
          </button>
          {selectedScenario && (
            <p className="create-game-form__chosen">
              {selectedScenario.display_name}
              {selectedScenario.context?.year ? ` · ${selectedScenario.context.year}` : ''}
            </p>
          )}
          {error && <p className="create-game-form__error">{error}</p>}
          <label className="create-game-form__label">
            Name
            <input
              type="text"
              className="create-game-form__input"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="My game"
            />
          </label>
          <div className="create-game-form__field">
            <span className="create-game-form__field-label">Mode</span>
            <div className="create-game-form__picker" role="group" aria-label="Single or multiplayer">
              <button
                type="button"
                className={`create-game-form__picker-option ${!isMultiplayer ? 'create-game-form__picker-option--active' : ''}`}
                onClick={() => setIsMultiplayer(false)}
              >
                Single Player
              </button>
              <button
                type="button"
                className={`create-game-form__picker-option ${isMultiplayer ? 'create-game-form__picker-option--active' : ''}`}
                onClick={() => setIsMultiplayer(true)}
              >
                Multiplayer
              </button>
            </div>
          </div>
          <div className="create-game-form__field">
            <span className="create-game-form__field-label">Heroes</span>
            <div className="create-game-form__picker" role="group" aria-label="Heroes on or off">
              <button
                type="button"
                className={`create-game-form__picker-option ${heroesEnabled ? 'create-game-form__picker-option--active' : ''}`}
                onClick={() => setHeroesEnabled(true)}
              >
                On
              </button>
              <button
                type="button"
                className={`create-game-form__picker-option ${!heroesEnabled ? 'create-game-form__picker-option--active' : ''}`}
                onClick={() => {
                  if (selectedScenario?.rings_of_power) return;
                  setHeroesEnabled(false);
                  setOptionalRules((prev) => (
                    prev.rings_of_power === undefined ? prev : { ...prev, rings_of_power: false }
                  ));
                }}
              >
                Off
              </button>
            </div>
          </div>
          {(selectedScenario?.optional_rules ?? []).map((rule) => {
            const enabled = optionalRules[rule.type] !== false;
            return (
              <div key={rule.type} className="create-game-form__field">
                <RuleLabel name={rule.name} description={rule.description ?? ruleDescriptions[rule.type]} />
                <div className="create-game-form__picker" role="group" aria-label={`${rule.name} on or off`}>
                  <button
                    type="button"
                    className={`create-game-form__picker-option ${enabled ? 'create-game-form__picker-option--active' : ''}`}
                    onClick={() => {
                      setOptionalRules((prev) => ({ ...prev, [rule.type]: true }));
                      if (rule.type === 'rings_of_power') setHeroesEnabled(true);
                    }}
                  >
                    On
                  </button>
                  <button
                    type="button"
                    className={`create-game-form__picker-option ${!enabled ? 'create-game-form__picker-option--active' : ''}`}
                    onClick={() => setOptionalRules((prev) => ({ ...prev, [rule.type]: false }))}
                  >
                    Off
                  </button>
                </div>
              </div>
            );
          })}
          <div className="create-game-form__field">
            <RuleLabel name="Shadow of War" description={ruleDescriptions.shadow_of_war} />
            <div className="create-game-form__picker" role="group" aria-label="Shadow of War on or off">
              <button
                type="button"
                className={`create-game-form__picker-option ${shadowOfWar ? 'create-game-form__picker-option--active' : ''}`}
                onClick={() => setShadowOfWar(true)}
              >
                On
              </button>
              <button
                type="button"
                className={`create-game-form__picker-option ${!shadowOfWar ? 'create-game-form__picker-option--active' : ''}`}
                onClick={() => setShadowOfWar(false)}
              >
                Off
              </button>
            </div>
          </div>
          <button type="submit" className="create-game-form__submit primary" disabled={loading || loadingSetups || selectedSetupId == null}>
            {loading ? 'Creating…' : 'Create game'}
          </button>
        </form>
      )}
      <Link to="/" className="page-menu-btn create-game-page__menu-anchor">Menu</Link>
    </div>
  );
}

import { useState } from 'react';
import { api, type AdminSetupListItem, type CatalogSpecial, type FormulaWeights, type UnitFormulas } from '../../services/api';
import { BASE_FEATURES, SPECIAL_PREFIX, formatWeight, formulaText } from './unitFormulas';

/** Keeps the typed text (e.g. "-" or "0.") until it parses, so weights are easy to edit. */
function WeightInput({
  value,
  onChange,
  ariaLabel,
}: {
  value: number;
  onChange: (n: number) => void;
  ariaLabel: string;
}) {
  const [text, setText] = useState(value ? String(value) : '');
  const [seen, setSeen] = useState(value);
  if (seen !== value) {
    setSeen(value);
    if (Number(text || 0) !== value) setText(value ? String(value) : '');
  }
  return (
    <input
      type="text"
      inputMode="decimal"
      className="admin-form__input admin-formulas__weight"
      placeholder="0"
      aria-label={ariaLabel}
      value={text}
      onChange={(e) => {
        const next = e.target.value.replace(/[^0-9.-]/g, '');
        setText(next);
        const n = Number(next);
        if (next === '' || next === '-') onChange(0);
        else if (Number.isFinite(n)) onChange(n);
      }}
    />
  );
}

function FeatureChecks({
  keys,
  labelFor,
  checked,
  onToggle,
}: {
  keys: string[];
  labelFor: (key: string) => string;
  checked: (key: string) => boolean;
  onToggle: (key: string, on: boolean) => void;
}) {
  return (
    <div className="admin-formulas__checks">
      {keys.map((key) => (
        <label key={key} className={`admin-formulas__check${checked(key) ? ' admin-formulas__check--on' : ''}`}>
          <input type="checkbox" checked={checked(key)} onChange={(e) => onToggle(key, e.target.checked)} />
          {labelFor(key)}
        </label>
      ))}
    </div>
  );
}

function fmt(n: number | null | undefined, digits = 2): string {
  return n == null ? '–' : n.toFixed(digits);
}

export function FormulasPanel({
  formulas,
  onChange,
  setups,
  specials,
}: {
  formulas: UnitFormulas;
  onChange: (next: UnitFormulas) => void;
  setups: AdminSetupListItem[];
  specials: CatalogSpecial[];
}) {
  const [training, setTraining] = useState(false);
  const [trainError, setTrainError] = useState<string | null>(null);

  const specialKeys = specials.map((s) => SPECIAL_PREFIX + s.id);
  const allKeys = [...BASE_FEATURES.map((f) => f.key), ...specialKeys];
  const specialName = (key: string) => {
    const sid = key.slice(SPECIAL_PREFIX.length);
    return specials.find((s) => s.id === sid)?.name || sid;
  };
  const longLabel = (key: string) => {
    const base = BASE_FEATURES.find((f) => f.key === key);
    return base ? `${base.label} (${base.short})` : specialName(key);
  };

  const features = new Set(formulas.features);
  const setFeature = (key: string, on: boolean) => {
    const next = new Set(features);
    if (on) next.add(key);
    else next.delete(key);
    onChange({ ...formulas, features: allKeys.filter((k) => next.has(k)) });
  };
  const allSpecialsOn = specialKeys.length > 0 && specialKeys.every((k) => features.has(k));
  const setAllSpecials = (on: boolean) => {
    const next = new Set(features);
    for (const k of specialKeys) {
      if (on) next.add(k);
      else next.delete(k);
    }
    onChange({ ...formulas, features: allKeys.filter((k) => next.has(k)) });
  };

  const reg = formulas.regression;
  const custom = formulas.custom;
  const setReg = (patch: Partial<UnitFormulas['regression']>) => onChange({ ...formulas, regression: { ...reg, ...patch } });
  const setCustom = (patch: Partial<UnitFormulas['custom']>) => onChange({ ...formulas, custom: { ...custom, ...patch } });
  const setTrainingOpts = (patch: Partial<UnitFormulas['training']>) =>
    onChange({ ...formulas, training: { ...formulas.training, ...patch } });

  const setupIds = new Set(formulas.training.setup_ids);
  const toggleSetup = (id: string, on: boolean) => {
    const next = new Set(setupIds);
    if (on) next.add(id);
    else next.delete(id);
    setTrainingOpts({ setup_ids: setups.map((s) => s.id).filter((sid) => next.has(sid)) });
  };

  const canTrain = setupIds.size > 0 && features.size > 0 && !training;
  const train = async () => {
    setTraining(true);
    setTrainError(null);
    try {
      const fit = await api.adminTrainFormula({
        setup_ids: formulas.training.setup_ids,
        features: formulas.features,
        include_heroes: formulas.training.include_heroes,
        ridge: formulas.training.ridge,
      });
      setReg({ intercept: fit.intercept, weights: fit.weights, stats: fit.stats });
    } catch (e) {
      setTrainError(e instanceof Error ? e.message : 'Training failed');
    } finally {
      setTraining(false);
    }
  };

  const regWeights = Object.entries(reg.weights).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]));
  const trained = reg.stats.n > 0;
  const setCustomWeight = (key: string, w: number) => {
    const next: FormulaWeights = { ...custom.weights };
    if (w) next[key] = w;
    else delete next[key];
    setCustom({ weights: next });
  };

  return (
    <div className="admin-formulas">
      <p className="admin-form__micro admin-formulas__intro">
        Two ways to estimate a unit&apos;s power cost from its stats. Tick <strong>Show in Unit Stats</strong> to add a
        formula as a blue column in the admin Unit Stats table. The last column adds up predicted − actual cost over the
        shown formulas: green means the unit looks underpriced, red means overpriced.
      </p>

      <section className="admin-formulas__card">
        <h3>Features</h3>
        <p className="admin-form__micro">What the regression may use to explain cost. The custom formula can weight any of them.</p>
        <FeatureChecks
          keys={BASE_FEATURES.map((f) => f.key)}
          labelFor={longLabel}
          checked={(k) => features.has(k)}
          onToggle={setFeature}
        />
        <div className="admin-formulas__subhead">
          <label className="admin-formulas__check admin-formulas__check--master">
            <input
              type="checkbox"
              checked={allSpecialsOn}
              ref={(el) => {
                if (el) el.indeterminate = !allSpecialsOn && specialKeys.some((k) => features.has(k));
              }}
              onChange={(e) => setAllSpecials(e.target.checked)}
            />
            Specials (one-hot: 1 if the unit has it)
          </label>
        </div>
        <FeatureChecks keys={specialKeys} labelFor={specialName} checked={(k) => features.has(k)} onToggle={setFeature} />
      </section>

      <section className="admin-formulas__card">
        <div className="admin-formulas__card-head">
          <h3>Regression</h3>
          <label className="admin-formulas__show">
            <input type="checkbox" checked={reg.show} onChange={(e) => setReg({ show: e.target.checked })} />
            Show in Unit Stats
          </label>
          <label className="admin-formulas__name">
            Column
            <input
              className="admin-form__input"
              maxLength={24}
              value={reg.label}
              onChange={(e) => setReg({ label: e.target.value })}
            />
          </label>
        </div>
        <p className="admin-form__micro">
          Fits the ticked features to the power cost of every purchasable unit that costs more than 0 in the setups you
          pick. A unit that appears unchanged in several setups counts once.
        </p>
        <div className="admin-formulas__row">
          <span className="admin-formulas__row-label">Train on</span>
          <div className="admin-formulas__checks">
            {setups.map((s) => (
              <label key={s.id} className={`admin-formulas__check${setupIds.has(s.id) ? ' admin-formulas__check--on' : ''}`}>
                <input type="checkbox" checked={setupIds.has(s.id)} onChange={(e) => toggleSetup(s.id, e.target.checked)} />
                {s.display_name}
              </label>
            ))}
          </div>
        </div>
        <div className="admin-formulas__row">
          <span className="admin-formulas__row-label">Heroes</span>
          <label className="admin-formulas__check">
            <input
              type="checkbox"
              checked={formulas.training.include_heroes}
              onChange={(e) => setTrainingOpts({ include_heroes: e.target.checked })}
            />
            Include purchasable heroes
          </label>
        </div>
        <div className="admin-formulas__row">
          <span className="admin-formulas__row-label">Ridge</span>
          <input
            type="range"
            min={0}
            max={10}
            step={0.1}
            className="admin-formulas__slider"
            value={formulas.training.ridge}
            onChange={(e) => setTrainingOpts({ ridge: Number(e.target.value) })}
            aria-label="Ridge strength"
          />
          <span className="admin-formulas__slider-value">{formulas.training.ridge.toFixed(1)}</span>
          <span className="admin-form__micro">0 = plain least squares. Higher pulls weights toward 0, which steadies rare specials.</span>
        </div>
        <div className="admin-formulas__train">
          <button type="button" className="admin-page__btn admin-page__btn--primary" disabled={!canTrain} onClick={() => void train()}>
            {training ? 'Training…' : 'Train'}
          </button>
          {setupIds.size === 0 ? <span className="admin-form__micro">Tick at least one setup.</span> : null}
          {features.size === 0 ? <span className="admin-form__micro">Tick at least one feature.</span> : null}
          {trainError ? <span className="admin-page__error admin-formulas__error">{trainError}</span> : null}
        </div>
        {trained ? (
          <div className="admin-formulas__result">
            <div className="admin-formulas__stats">
              <span>
                <strong>{reg.stats.n}</strong> units
              </span>
              <span title="Share of cost differences the formula explains (1 = perfect)">
                R² <strong>{fmt(reg.stats.r2)}</strong>
              </span>
              <span title="Average |predicted − actual| on the training units">
                Avg miss <strong>{fmt(reg.stats.mae)}</strong>
              </span>
              <span title="Root mean squared error on the training units">
                RMSE <strong>{fmt(reg.stats.rmse)}</strong>
              </span>
              {reg.stats.trained_at ? (
                <span className="admin-form__micro">Trained {new Date(reg.stats.trained_at).toLocaleString()}</span>
              ) : null}
            </div>
            <code className="admin-formulas__formula">cost ≈ {formulaText(reg, specials)}</code>
            <table className="admin-formulas__weights">
              <tbody>
                <tr>
                  <td>Constant</td>
                  <td>{formatWeight(reg.intercept)}</td>
                </tr>
                {regWeights.map(([key, w]) => (
                  <tr key={key}>
                    <td>{longLabel(key)}</td>
                    <td className={w < 0 ? 'admin-formulas__neg' : undefined}>{formatWeight(w)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <button
              type="button"
              className="admin-page__btn"
              onClick={() => setCustom({ intercept: Math.round(reg.intercept * 100) / 100, weights: Object.fromEntries(regWeights.map(([k, w]) => [k, Math.round(w * 100) / 100])) })}
            >
              Copy into custom formula
            </button>
          </div>
        ) : (
          <p className="admin-form__micro">Not trained yet.</p>
        )}
      </section>

      <section className="admin-formulas__card">
        <div className="admin-formulas__card-head">
          <h3>Custom formula</h3>
          <label className="admin-formulas__show">
            <input type="checkbox" checked={custom.show} onChange={(e) => setCustom({ show: e.target.checked })} />
            Show in Unit Stats
          </label>
          <label className="admin-formulas__name">
            Column
            <input
              className="admin-form__input"
              maxLength={24}
              value={custom.label}
              onChange={(e) => setCustom({ label: e.target.value })}
            />
          </label>
        </div>
        <p className="admin-form__micro">Cost = constant + Σ weight × feature. Leave a weight blank to skip that feature.</p>
        <code className="admin-formulas__formula">cost = {formulaText(custom, specials)}</code>
        <div className="admin-formulas__grid">
          <label className="admin-formulas__cell admin-formulas__cell--constant">
            <span>Constant</span>
            <WeightInput value={custom.intercept} ariaLabel="Constant" onChange={(n) => setCustom({ intercept: n })} />
          </label>
          {allKeys.map((key) => (
            <label key={key} className="admin-formulas__cell">
              <span>{longLabel(key)}</span>
              <WeightInput value={custom.weights[key] ?? 0} ariaLabel={`${longLabel(key)} weight`} onChange={(n) => setCustomWeight(key, n)} />
            </label>
          ))}
        </div>
        {Object.keys(custom.weights).length || custom.intercept ? (
          <button type="button" className="admin-page__btn" onClick={() => setCustom({ intercept: 0, weights: {} })}>
            Clear weights
          </button>
        ) : null}
      </section>

      <section className="admin-formulas__card">
        <h3>Difference column</h3>
        <p className="admin-form__micro">
          Last column in Unit Stats: the sum of (predicted − actual cost) over the formulas shown above. Green (positive)
          means the formulas think the unit should cost more; red (negative) means less. Hidden when neither formula is
          shown.
        </p>
        <label className="admin-formulas__name">
          Column
          <input
            className="admin-form__input"
            maxLength={24}
            value={formulas.diff_label}
            onChange={(e) => onChange({ ...formulas, diff_label: e.target.value })}
          />
        </label>
      </section>
    </div>
  );
}

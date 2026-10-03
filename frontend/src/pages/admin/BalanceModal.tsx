import { Fragment, useEffect, useState } from 'react';
import { api } from '../../services/api';
import type { AdminSetupBundle, AdminSetupSavePayload, BalanceSide, StartingStrengthReport } from '../../services/api';
import type { StatsFactionData } from '../../components/StatsModals';

function score(value: number): string {
  return value.toLocaleString(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}

function whole(value: number): string {
  return value.toLocaleString();
}

function place(ratio: number | null): string {
  if (ratio == null) return '—';
  return `${Math.round(ratio * 100)}%`;
}

function shareLabel(row: BalanceSide): string {
  if (row.starting_power_share == null) return '—';
  return `${(row.starting_power_share * 100).toFixed(1)}%`;
}

export function BalanceModal({
  bundle,
  factionData,
  onClose,
}: {
  bundle: AdminSetupBundle;
  factionData: StatsFactionData;
  onClose: () => void;
}) {
  const [report, setReport] = useState<StartingStrengthReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const setupName =
    typeof bundle.manifest.display_name === 'string' && bundle.manifest.display_name.trim()
      ? bundle.manifest.display_name.trim()
      : bundle.id;

  useEffect(() => {
    let cancelled = false;
    setReport(null);
    setError(null);
    const payload: AdminSetupSavePayload = {
      manifest: bundle.manifest,
      units: bundle.units,
      territories: bundle.territories,
      factions: bundle.factions,
      camps: bundle.camps,
      ports: bundle.ports,
      starting_setup: bundle.starting_setup,
      specials: bundle.specials,
    };
    api
      .adminBalance(payload)
      .then((next) => {
        if (!cancelled) setReport(next);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Could not calculate balance');
      });
    return () => {
      cancelled = true;
    };
  }, [bundle]);

  const alliances = report?.alliances ?? [];
  const good = alliances.find((row) => row.id === 'good');
  const evil = alliances.find((row) => row.id === 'evil');
  const goodShare = (good?.starting_power_share ?? 0) * 100;
  const evilShare = (evil?.starting_power_share ?? 0) * 100;

  return (
    <div className="admin-modal-overlay" role="presentation" onClick={onClose}>
      <div
        className="admin-modal admin-modal--balance"
        role="dialog"
        aria-modal="true"
        aria-labelledby="admin-balance-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="balance-modal__header">
          <div>
            <h2 id="admin-balance-title" className="admin-modal__title">
              Starting balance
            </h2>
            <p className="admin-form__micro">{setupName}</p>
          </div>
          <button type="button" className="admin-page__btn" onClick={onClose}>
            Close
          </button>
        </div>

        {error ? <p className="admin-page__error">{error}</p> : null}
        {!report && !error ? <p className="admin-form__micro">Calculating…</p> : null}

        {report ? (
          <>
            {good && evil ? (
              <div className="balance-share-block">
                <div className="balance-share-legend">
                  <span>Good {shareLabel(good)}</span>
                  <span>Evil {shareLabel(evil)}</span>
                </div>
                <div className="balance-share" aria-hidden>
                  <div className="balance-share__good" style={{ width: `${goodShare}%` }} />
                  <div className="balance-share__evil" style={{ width: `${evilShare}%` }} />
                </div>
                <p className="admin-form__micro">Share of modeled starting power (effective units + discounted production).</p>
              </div>
            ) : null}

            {report.readings.length > 0 ? (
              <ul className="balance-readings">
                {report.readings.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
            ) : null}

            <div className="balance-table-scroll">
              <table className="balance-table">
                <thead>
                  <tr>
                    <th>Faction</th>
                    <th title="Unit power: IPC cost of starting units">UP</th>
                    <th title="Share of unit power that is near an important fight">Place</th>
                    <th title="Effective unit power">EUP</th>
                    <th title="Attack half of effective unit power">Atk</th>
                    <th title="Defense half of effective unit power">Def</th>
                    <th title="Power production per turn">PP</th>
                    <th title="Discounted production over the next few rounds">Econ</th>
                    <th title="Starting power score: effective unit power plus economic power">Score</th>
                    <th title="Strongholds owned">S</th>
                    <th title="Strongholds this alliance still needs. Shown on the alliance row.">Need</th>
                    <th title="Territories owned. Listed only; not part of the score.">T</th>
                  </tr>
                </thead>
                <tbody>
                  {alliances.map((alliance) => {
                    const members = report.factions.filter((row) => row.alliance === alliance.id);
                    return (
                      <Fragment key={alliance.id}>
                        <tr className="balance-table__alliance">
                          <th scope="row">{alliance.display_name}</th>
                          <td>{whole(alliance.unit_power)}</td>
                          <td>{place(alliance.position_ratio)}</td>
                          <td>{score(alliance.effective_unit_power)}</td>
                          <td>{score(alliance.effective_unit_power_attack)}</td>
                          <td>{score(alliance.effective_unit_power_defense)}</td>
                          <td>{whole(alliance.power_production)}</td>
                          <td>{score(alliance.economic_power)}</td>
                          <td>{score(alliance.starting_power_score)}</td>
                          <td>{whole(alliance.strongholds)}</td>
                          <td>{alliance.strongholds_to_win == null ? '—' : whole(alliance.strongholds_to_win)}</td>
                          <td>{whole(alliance.territories)}</td>
                        </tr>
                        {members.map((row) => {
                          const fd = factionData[row.id];
                          return (
                            <tr key={row.id}>
                              <th scope="row">
                                <span className="balance-faction">
                                  {fd?.icon ? <img src={fd.icon} alt="" /> : null}
                                  <span>{row.display_name}</span>
                                </span>
                              </th>
                              <td>{whole(row.unit_power)}</td>
                              <td>{place(row.position_ratio)}</td>
                              <td>{score(row.effective_unit_power)}</td>
                              <td>{score(row.effective_unit_power_attack)}</td>
                              <td>{score(row.effective_unit_power_defense)}</td>
                              <td>{whole(row.power_production)}</td>
                              <td>{score(row.economic_power)}</td>
                              <td>{score(row.starting_power_score)}</td>
                              <td>{whole(row.strongholds)}</td>
                              <td>—</td>
                              <td>{whole(row.territories)}</td>
                            </tr>
                          );
                        })}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {report.neutral.strongholds > 0 || report.neutral.unit_power > 0 ? (
              <p className="admin-form__micro">
                {[
                  report.neutral.strongholds > 0 ? `Unowned strongholds: ${report.neutral.strongholds}.` : '',
                  report.neutral.unit_power > 0 ? `Neutral unit power: ${report.neutral.unit_power}.` : '',
                  'Left out of the alliance split.',
                ]
                  .filter(Boolean)
                  .join(' ')}
              </p>
            ) : null}

            <h3 className="balance-subtitle">Largest position discounts</h3>
            {report.largest_discounts.length === 0 ? (
              <p className="admin-form__micro">No starting stack is discounted for position.</p>
            ) : (
              <div className="balance-table-scroll">
                <table className="balance-table balance-table--discounts">
                  <thead>
                    <tr>
                      <th>Faction</th>
                      <th>Where</th>
                      <th>Unit</th>
                      <th title="Unit power of this stack">UP</th>
                      <th title="Turns until this stack can attack an important target">Attack</th>
                      <th title="Turns until this stack is defending an important friendly position">Defense</th>
                      <th title="Unit power lost to the availability discount">Lost</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.largest_discounts.map((row, index) => (
                      <tr key={`${row.faction_id}-${row.territory_id}-${row.unit_id}-${index}`}>
                        <td>{row.faction_name}</td>
                        <td>{row.territory_name}</td>
                        <td>
                          {row.count}× {row.unit_name}
                        </td>
                        <td>{whole(row.unit_power)}</td>
                        <td>{row.attack_turn_label}</td>
                        <td>{row.defense_turn_label}</td>
                        <td>{score(row.power_discounted)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            <details className="balance-formula">
              <summary>How this is calculated</summary>
              <p>{report.parameters.summary}</p>
            </details>
          </>
        ) : null}
      </div>
    </div>
  );
}

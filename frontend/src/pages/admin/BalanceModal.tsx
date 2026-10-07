import { Fragment, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
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

function needTitle(row: BalanceSide): string | undefined {
  if (row.strongholds_to_win == null || row.strongholds_on_map == null) return undefined;
  const target = row.stronghold_target == null ? '' : ` Target is ${whole(row.stronghold_target)}.`;
  const unreachable =
    row.stronghold_target != null && row.stronghold_target > row.strongholds_on_map
      ? ' That target is higher than the number of strongholds on the map.'
      : '';
  return `Still needs ${whole(row.strongholds_to_win)}. The map has ${whole(row.strongholds_on_map)} strongholds.${target}${unreachable}`;
}

function shareLabel(row: BalanceSide): string {
  if (row.starting_power_share == null) return '—';
  return `${(row.starting_power_share * 100).toFixed(1)}%`;
}

export function BalanceModal({
  bundle,
  factionData,
  withRings,
  ringsToggle,
  onClose,
}: {
  bundle: AdminSetupBundle;
  factionData: StatsFactionData;
  withRings: boolean;
  /** Shown when the Rings of Power rule is optional. */
  ringsToggle?: ReactNode;
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
    };
    api
      .adminBalance(payload, withRings)
      .then((next) => {
        if (!cancelled) setReport(next);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Could not calculate balance');
      });
    return () => {
      cancelled = true;
    };
  }, [bundle, withRings]);

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
          <div className="balance-modal__actions">
            {ringsToggle}
            <button type="button" className="admin-page__btn" onClick={onClose}>
              Close
            </button>
          </div>
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
                <p className="admin-form__micro">
                  Share of modeled starting power. An alliance that already holds enough strongholds takes the whole share.
                  An alliance whose target is higher than the number of strongholds on the map cannot win and scores 0
                  while another alliance still can. Otherwise each side keeps less of its units and economy as more
                  strongholds remain.
                </p>
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
                    <th title="Victory adjustment. Each alliance keeps less of its units and economy as more strongholds remain. The score is 0 when another alliance has already won, or when this target is higher than the number of strongholds on the map and the other alliance can still win.">
                      VP
                    </th>
                    <th title="Alliance rows include the victory adjustment. Faction rows are units plus economy only.">Score</th>
                    <th title="Strongholds owned">S</th>
                    <th title="Strongholds this alliance still needs, over how many strongholds are on the map.">Need</th>
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
                          <td>{score(alliance.victory_adjustment ?? 0)}</td>
                          <td>{score(alliance.starting_power_score)}</td>
                          <td>{whole(alliance.strongholds)}</td>
                          <td title={needTitle(alliance)}>
                            {alliance.strongholds_to_win == null || alliance.strongholds_on_map == null ? (
                              '—'
                            ) : (
                              <>
                                {whole(alliance.strongholds_to_win)}
                                <span className="balance-need__map"> / {whole(alliance.strongholds_on_map)}</span>
                              </>
                            )}
                          </td>
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
                              <td>—</td>
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
              <summary>Column definitions</summary>
              <dl className="balance-defs">
                <dt>UP</dt>
                <dd>IPC cost of the starting units.</dd>
                <dt>Place</dt>
                <dd>Share of that unit power already near an important fight. Effective unit power divided by unit power.</dd>
                <dt>Atk</dt>
                <dd>
                  Attack half of effective unit power. A unit counts less the longer it takes to reach an enemy or unowned
                  stronghold, capital, or territory producing {report.parameters.high_production} or more. Land armies do not
                  board ships, so a stack with no land route uses the floor.
                </dd>
                <dt>Def</dt>
                <dd>
                  Defense half. A unit counts less the longer it takes to stand on a threatened friendly stronghold, capital,
                  or territory producing {report.parameters.high_production} or more. A unit already standing on one counts in
                  full.
                </dd>
                <dt>EUP</dt>
                <dd>
                  Effective unit power. The average of Atk and Def.
                  <ul className="balance-defs__ladder">
                    {report.parameters.availability.map((row) => (
                      <li key={row.turns}>
                        {row.turns === '1' ? '1 turn' : `${row.turns} turns`}: {Math.round(row.factor * 100)}%
                      </li>
                    ))}
                  </ul>
                </dd>
                <dt>PP</dt>
                <dd>
                  Power produced per turn from owned land
                  {report.rings_of_power?.on ? ', plus Rings of Power held at the start' : ''}.
                </dd>
                <dt>Econ</dt>
                <dd>
                  That production over the next {report.parameters.horizon_rounds} rounds, discounted by {report.parameters.discount}{' '}
                  each round. A flat economy counts as {report.parameters.economic_coefficient.toFixed(2)} times current
                  production. Evolving territories follow their step until they stop.
                </dd>
                <dt>VP</dt>
                <dd>
                  How much of the unit and economy score this alliance keeps. Alliance row only. Faction rows leave this
                  blank.
                  <ul className="balance-defs__ladder">
                    {report.parameters.victory_closeness.map((row) => (
                      <li key={row.needed}>
                        {row.needed} still needed: keep {Math.round(row.kept * 100)}%
                      </li>
                    ))}
                  </ul>
                </dd>
                <dt>Score</dt>
                <dd>Alliance row is EUP + Econ + VP. Faction row is EUP + Econ only.</dd>
                <dt>S</dt>
                <dd>Strongholds owned.</dd>
                <dt>Need</dt>
                <dd>
                  Strongholds this alliance still needs, shown over how many strongholds are on the map. 3 / 13 means 3
                  still needed on a map of 13. Alliance row only.
                </dd>
                <dt>T</dt>
                <dd>Territories owned. Shown only, and left out of the score.</dd>
                <dt>Attack</dt>
                <dd>In the discount table, turns until that stack can attack an important target.</dd>
                <dt>Defense</dt>
                <dd>In the discount table, turns until that stack is defending an important friendly position.</dd>
                <dt>Lost</dt>
                <dd>In the discount table, unit power removed by the availability discount.</dd>
              </dl>
              <h4>When the share leaves the column math</h4>
              <ul className="balance-defs__notes">
                <li>An alliance that already holds enough strongholds takes the whole share, and the other scores 0.</li>
                <li>
                  An alliance whose target is higher than the number of strongholds on the map scores 0 while another alliance
                  can still win.
                </li>
                <li>Neutral units and unowned strongholds stay out of the alliance split.</li>
              </ul>
            </details>
          </>
        ) : null}
      </div>
    </div>
  );
}

import { useEffect, useMemo, useState } from 'react';
import { api } from '../../services/api';
import type { AdminSetupListItem, CatalogSpecial } from '../../services/api';
import { previewStatsFromBundle } from './previewStats';

type Props = {
  setups: AdminSetupListItem[];
  specials: CatalogSpecial[];
  onClose: () => void;
};

type UnitRow = {
  icon: string;
  setupId: string;
  scenario: string;
  unitId: string;
  cost: number;
  purchasable: boolean;
  attack: number;
  defense: number;
  dice: number;
  movement: number;
  health: number;
  specials: string;
  iconSpan: number;
  scenarioSpan: number;
};

function isListedScenario(setup: AdminSetupListItem): boolean {
  return setup.is_active === true || setup.is_active === 'admin';
}

function withSpans(rows: Omit<UnitRow, 'iconSpan' | 'scenarioSpan'>[]): UnitRow[] {
  return rows.map((row, index) => {
    const iconStart = index === 0 || rows[index - 1].icon !== row.icon;
    let iconSpan = 0;
    if (iconStart) {
      iconSpan = 1;
      while (index + iconSpan < rows.length && rows[index + iconSpan].icon === row.icon) iconSpan += 1;
    }
    const scenarioStart = iconStart || rows[index - 1].setupId !== row.setupId;
    let scenarioSpan = 0;
    if (scenarioStart) {
      scenarioSpan = 1;
      while (
        index + scenarioSpan < rows.length
        && rows[index + scenarioSpan].icon === row.icon
        && rows[index + scenarioSpan].setupId === row.setupId
      ) {
        scenarioSpan += 1;
      }
    }
    return { ...row, iconSpan, scenarioSpan };
  });
}

export function UnitsReportModal({ setups, specials, onClose }: Props) {
  const listed = useMemo(() => setups.filter(isListedScenario), [setups]);
  const [rows, setRows] = useState<UnitRow[]>([]);
  const [loading, setLoading] = useState(listed.length > 0);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (listed.length === 0) return;
    let cancelled = false;
    Promise.all(listed.map((setup) => api.adminGetSetup(setup.id).then((bundle) => ({ setup, bundle }))))
      .then((loaded) => {
        if (cancelled) return;
        const flat: Omit<UnitRow, 'iconSpan' | 'scenarioSpan'>[] = [];
        for (const { setup, bundle } of loaded) {
          const preview = previewStatsFromBundle(bundle, { specials });
          if (!preview) continue;
          const scenario = setup.display_name?.trim() || setup.id;
          for (const units of Object.values(preview.unitsByFaction)) {
            for (const unit of units) {
              flat.push({
                icon: unit.icon,
                setupId: setup.id,
                scenario,
                unitId: unit.id,
                cost: unit.cost,
                purchasable: unit.purchasable,
                attack: unit.attack,
                defense: unit.defense,
                dice: unit.dice,
                movement: unit.movement,
                health: unit.health,
                specials: unit.specials.length ? unit.specials.join(', ') : '',
              });
            }
          }
        }
        flat.sort((a, b) => (
          a.icon.localeCompare(b.icon)
          || a.scenario.localeCompare(b.scenario)
          || a.setupId.localeCompare(b.setupId)
          || a.unitId.localeCompare(b.unitId)
        ));
        setRows(withSpans(flat));
        setError(null);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Could not load units');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [listed, specials]);

  return (
    <div className="admin-modal-overlay" role="presentation" onClick={onClose}>
      <div
        className="admin-modal admin-modal--units"
        role="dialog"
        aria-modal="true"
        aria-labelledby="admin-units-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="games-report__header">
          <div>
            <h2 id="admin-units-title" className="admin-modal__title">
              Units
            </h2>
            <p className="admin-form__micro">
              Units from scenarios set to Yes or Admin only, grouped by icon and then scenario.
            </p>
          </div>
          <button type="button" className="admin-page__btn" onClick={onClose}>
            Close
          </button>
        </div>
        {error ? <p className="admin-page__error games-report__error">{error}</p> : null}
        <div className="games-report__table-scroll unit-stats-modal units-across">
          {loading ? <p className="admin-form__micro">Loading units…</p> : null}
          {!loading && rows.length === 0 ? <p className="admin-form__micro">No units in active scenarios.</p> : null}
          {!loading && rows.length > 0 ? (
            <table className="header-stats-table header-stats-table--units units-across__table">
              <colgroup>
                <col className="units-across__col-icon" />
                <col className="units-across__col-scenario" />
                <col className="units-across__col-id" />
                {[1, 2, 3, 4, 5, 6].map((i) => (
                  <col key={i} className="stats-units-col-num" />
                ))}
                <col className="stats-units-col-specials" />
              </colgroup>
              <thead>
                <tr>
                  <th className="units-across__icon">Icon</th>
                  <th className="units-across__scenario">Scenario</th>
                  <th className="units-across__id">Unit id</th>
                  <th className="stats-col-num">P</th>
                  <th className="stats-col-num">A</th>
                  <th className="stats-col-num">D</th>
                  <th className="stats-col-num">R</th>
                  <th className="stats-col-num">M</th>
                  <th className="stats-col-num">HP</th>
                  <th className="stats-col-num stats-col-specials">SP</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={`${row.icon}|${row.setupId}|${row.unitId}`} className="unit-stats-unit-row">
                    {row.iconSpan > 0 ? (
                      <td className="units-across__icon" rowSpan={row.iconSpan}>
                        <img src={row.icon} alt="" className="unit-stats-unit-icon" aria-hidden />
                      </td>
                    ) : null}
                    {row.scenarioSpan > 0 ? (
                      <td className="units-across__scenario" rowSpan={row.scenarioSpan}>
                        {row.scenario}
                      </td>
                    ) : null}
                    <td className="units-across__id">{row.unitId}</td>
                    <td className="stats-col-num">{row.purchasable ? row.cost : '–'}</td>
                    <td className="stats-col-num">{row.attack}</td>
                    <td className="stats-col-num">{row.defense}</td>
                    <td className="stats-col-num">{row.dice}</td>
                    <td className="stats-col-num">{row.movement}</td>
                    <td className="stats-col-num">{row.health}</td>
                    <td className="stats-col-num stats-col-specials">{row.specials}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
        </div>
        <p className="unit-stats-modal-key">
          P = Power cost | A = Attack | D = Defense | R = Dice rolls | M = Moves | HP = Hit Points | SP = Specials
        </p>
      </div>
    </div>
  );
}

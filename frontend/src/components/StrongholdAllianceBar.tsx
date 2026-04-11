import type { ApiFactionStats } from '../services/api';
import './StrongholdAllianceBar.css';

export interface StrongholdAllianceBarProps {
  factionStats: ApiFactionStats;
  /** Layout preset: matches previous header vs game list sizing. */
  variant: 'header' | 'gameList';
}

/**
 * Good (white) | neutral (gray) | evil (black) stronghold counts, with optional
 * vertical lines at victory thresholds (setup victory_criteria.strongholds):
 * - Dark line at (total - evil_need) / total — evil win threshold measured from the left.
 * - Light line at good_need / total — good win threshold.
 */
export default function StrongholdAllianceBar({ factionStats, variant }: StrongholdAllianceBarProps) {
  const good = factionStats.alliances?.['good']?.strongholds ?? 0;
  const evil = factionStats.alliances?.['evil']?.strongholds ?? 0;
  const neutral = factionStats.neutral_strongholds ?? 0;
  const total = good + neutral + evil || 1;
  const goodPct = (good / total) * 100;
  const neutralPct = (neutral / total) * 100;
  const evilPct = (evil / total) * 100;

  const vc = factionStats.stronghold_victory;
  const goodNeed = vc?.good != null && vc.good > 0 ? vc.good : null;
  const evilNeed = vc?.evil != null && vc.evil > 0 ? vc.evil : null;

  const evilThresholdPct =
    evilNeed != null
      ? Math.min(100, Math.max(0, ((total - evilNeed) / total) * 100))
      : null;
  const goodThresholdPct =
    goodNeed != null ? Math.min(100, Math.max(0, (goodNeed / total) * 100)) : null;

  const rootClass =
    variant === 'header' ? 'stronghold-alliance-bar stronghold-alliance-bar--header' : 'stronghold-alliance-bar stronghold-alliance-bar--game-list';

  const goodCountLabel = goodNeed != null ? `${good}/${goodNeed}` : String(good);
  const evilCountLabel = evilNeed != null ? `${evil}/${evilNeed}` : String(evil);

  return (
    <div className={rootClass}>
      <div className="stronghold-alliance-bar__track">
        <div className="stronghold-alliance-bar__segments">
          <div className="stronghold-alliance-bar__good" style={{ width: `${goodPct}%` }} />
          <div className="stronghold-alliance-bar__neutral" style={{ width: `${neutralPct}%` }} />
          <div className="stronghold-alliance-bar__evil" style={{ width: `${evilPct}%` }} />
        </div>
        {evilThresholdPct != null && (
          <div
            className="stronghold-alliance-bar__marker stronghold-alliance-bar__marker--evil-threshold"
            style={{ left: `${evilThresholdPct}%` }}
            title={`Evil wins at ${evilNeed} strongholds`}
            aria-hidden
          />
        )}
        {goodThresholdPct != null && (
          <div
            className="stronghold-alliance-bar__marker stronghold-alliance-bar__marker--good-threshold"
            style={{ left: `${goodThresholdPct}%` }}
            title={`Good wins at ${goodNeed} strongholds`}
            aria-hidden
          />
        )}
      </div>
      <span className="stronghold-alliance-bar__label">
        Good {goodCountLabel} · Evil {evilCountLabel}
      </span>
    </div>
  );
}

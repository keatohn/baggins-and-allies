import type { CSSProperties } from 'react';
import { contrastTextOn } from '../utils/colorContrast';
import './PhaseStepper.css';

const PHASE_ORDER: string[] = ['purchase', 'combat_move', 'combat', 'non_combat_move', 'mobilization'];

/** Short labels for the current-phase pill (full names are tooltips on the others). */
const PHASE_SHORT: Record<string, string> = {
  purchase: 'Purchase',
  combat_move: 'Combat Move',
  combat: 'Combat',
  non_combat_move: 'Non-Combat',
  mobilization: 'Mobilize',
};

const PHASE_FULL: Record<string, string> = {
  purchase: 'Purchase',
  combat_move: 'Combat Move',
  combat: 'Combat',
  non_combat_move: 'Non-Combat Move',
  mobilization: 'Mobilization',
};

function phaseKey(phase: string): string {
  return phase === 'mobilize' ? 'mobilization' : phase;
}

interface PhaseStepperProps {
  /** Current game phase (backend id; "mobilize" and "mobilization" both accepted). */
  phase: string;
  /** Current turn's faction color — fills the current pill; text contrast is computed from it. */
  factionColor?: string;
}

/** 5-phase stepper shown in the right sidebar above Actions. */
function PhaseStepper({ phase, factionColor }: PhaseStepperProps) {
  const activeIndex = Math.max(0, PHASE_ORDER.indexOf(phaseKey(phase)));
  const currentColor = factionColor || '#6b5b4b';
  return (
    <ol className="phase-stepper" aria-label="Turn phases">
      {PHASE_ORDER.map((key, i) => {
        const isCurrent = i === activeIndex;
        const state = i < activeIndex ? 'past' : isCurrent ? 'current' : 'future';
        const fullName = PHASE_FULL[key] ?? key;
        const style = isCurrent
          ? ({
              ['--phase-faction-color' as string]: currentColor,
              color: contrastTextOn(currentColor),
            } as CSSProperties)
          : undefined;
        return (
          <li
            key={key}
            className={`phase-step phase-step--${state}`}
            style={style}
            aria-current={isCurrent ? 'step' : undefined}
            aria-label={`${i + 1} of ${PHASE_ORDER.length}: ${fullName}`}
            title={fullName}
          >
            <span className="phase-step-num" aria-hidden="true">{i + 1}</span>
            <span className="phase-step-name">{PHASE_SHORT[key] ?? fullName}</span>
          </li>
        );
      })}
    </ol>
  );
}

export default PhaseStepper;

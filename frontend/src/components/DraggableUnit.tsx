import type { CSSProperties } from 'react';
import { useDraggable } from '@dnd-kit/core';
import { CSS } from '@dnd-kit/utilities';
import './UnitToken.css';

interface DraggableUnitProps {
  id: string;
  unitId: string;
  territoryId: string;
  count: number;
  unitDef?: { name: string; icon: string };
  isSelected: boolean;
  disabled?: boolean;
  factionColor?: string; // Color from faction definition
  /** Aerial in enemy territory: must move to friendly before ending non-combat move phase */
  showAerialMustMove?: boolean;
  /** Boat in sea zone that loaded this combat move: must attack (naval combat or sea raid) before ending phase */
  showNavalMustAttack?: boolean;
  /** Naval unit: use larger token on map (1.5×) */
  isNaval?: boolean;
  /** Passenger count to show on boat token (sea transport). */
  passengerCount?: number;
  /** When set (e.g. boat + passengers), use these instance IDs for the move. */
  instanceIds?: string[];
}

function DraggableUnit({
  id,
  unitId,
  territoryId,
  count,
  unitDef,
  isSelected,
  disabled = false,
  factionColor,
  showAerialMustMove = false,
  showNavalMustAttack = false,
  isNaval = false,
  passengerCount = 0,
  instanceIds,
}: DraggableUnitProps) {
  const { attributes, listeners, setNodeRef, transform, isDragging } = useDraggable({
    id,
    data: {
      unitId,
      territoryId,
      count,
      unitDef,
      factionColor,
      instanceIds,
      passengerCount,
    },
    disabled,
  });

  const style: CSSProperties = {
    transform: CSS.Translate.toString(transform),
    opacity: isDragging ? 0.5 : 1,
    zIndex: isDragging ? 1000 : undefined,
    borderColor: factionColor || undefined,
  };

  if (!unitDef) return null;

  const title = showAerialMustMove
    ? `${unitDef.name} ×${count} — Must move to friendly territory before ending phase`
    : showNavalMustAttack
      ? `${unitDef.name} ×${count} — Must attack (naval combat or sea raid) before ending phase`
      : passengerCount > 0
        ? `${unitDef.name} (${passengerCount} aboard)`
        : `${unitDef.name} ×${count}`;

  return (
    <div
      ref={setNodeRef}
      className={`unit-token ${isSelected ? 'selected' : ''} ${isDragging ? 'dragging' : ''} ${disabled ? 'disabled' : ''} ${showAerialMustMove ? 'aerial-must-move' : ''} ${showNavalMustAttack ? 'naval-must-attack' : ''} ${isNaval ? 'unit-token--naval' : ''}`}
      style={style}
      title={title}
      {...listeners}
      {...attributes}
    >
      <img src={unitDef.icon} alt={unitDef.name} draggable={false} />
      <span className={`count ${count === 1 && passengerCount === 0 ? 'single' : ''}`}>{count}</span>
      {passengerCount > 0 && (
        <span className="unit-token-passenger-badge" title={`${passengerCount} unit(s) aboard`}>{passengerCount}</span>
      )}
      {showAerialMustMove && (
        <span className="unit-token-caution" title="Must move to friendly territory" aria-hidden>
          ⚠️
        </span>
      )}
      {showNavalMustAttack && !showAerialMustMove && (
        <span className="unit-token-caution" title="Must attack (naval combat or sea raid) before ending phase" aria-hidden>
          ⚠️
        </span>
      )}
    </div>
  );
}

export default DraggableUnit;

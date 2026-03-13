import type { CSSProperties } from 'react';
import { DragOverlay as DndDragOverlay } from '@dnd-kit/core';
import './UnitToken.css';
import './MobilizationTray.css';

interface DragOverlayProps {
  activeUnit: {
    unitId: string;
    count: number;
    unitDef?: { name: string; icon: string };
    factionColor?: string;
    isNaval?: boolean;
    passengerCount?: number;
  } | null;
  activeMobilizationItem?: {
    unitId: string;
    name: string;
    icon: string;
    count: number;
    factionColor?: string;
  } | null;
  activeCampDrag?: { campIndex: number } | null;
  factionColor?: string;
}

function DragOverlay({ activeUnit, activeMobilizationItem, activeCampDrag, factionColor }: DragOverlayProps) {
  if (activeCampDrag) {
    const style: CSSProperties = factionColor
      ? { borderColor: factionColor, cursor: 'grabbing' }
      : { cursor: 'grabbing' };
    return (
      <DndDragOverlay dropAnimation={null}>
        <div className="purchase-stack camp-item purchase-stack-overlay" style={style}>
          <span className="purchase-icon camp-icon" aria-hidden>⛺</span>
          <span className="purchase-name">Camp</span>
        </div>
      </DndDragOverlay>
    );
  }

  if (activeMobilizationItem) {
    const style: CSSProperties = activeMobilizationItem.factionColor
      ? { borderColor: activeMobilizationItem.factionColor, cursor: 'grabbing' }
      : { cursor: 'grabbing' };
    return (
      <DndDragOverlay dropAnimation={null}>
        <div className="purchase-stack purchase-stack-overlay" style={style}>
          <img src={activeMobilizationItem.icon} alt={activeMobilizationItem.name} className="purchase-icon" draggable={false} />
          <span className="purchase-count">{activeMobilizationItem.count}</span>
          <span className="purchase-name">{activeMobilizationItem.name}</span>
        </div>
      </DndDragOverlay>
    );
  }

  if (!activeUnit || !activeUnit.unitDef) return null;

  const style: CSSProperties = activeUnit.factionColor
    ? { borderColor: activeUnit.factionColor }
    : {};

  const passengerCount = activeUnit.passengerCount ?? 0;

  return (
    <DndDragOverlay dropAnimation={null}>
      <div className={`unit-token dragging-overlay${activeUnit.isNaval ? ' unit-token--naval' : ''}`} style={style}>
        <img src={activeUnit.unitDef.icon} alt={activeUnit.unitDef.name} draggable={false} />
        <span className={`count ${activeUnit.count === 1 && passengerCount === 0 ? 'single' : ''}`}>
          {activeUnit.count}
        </span>
        {passengerCount > 0 && (
          <span className="unit-token-passenger-badge" title={`${passengerCount} aboard`}>{passengerCount}</span>
        )}
      </div>
    </DndDragOverlay>
  );
}

export default DragOverlay;

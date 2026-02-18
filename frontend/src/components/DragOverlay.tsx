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
  } | null;
  activeMobilizationItem?: {
    unitId: string;
    name: string;
    icon: string;
    count: number;
    factionColor?: string;
  } | null;
}

function DragOverlay({ activeUnit, activeMobilizationItem }: DragOverlayProps) {
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

  return (
    <DndDragOverlay dropAnimation={null}>
      <div className="unit-token dragging-overlay" style={style}>
        <img src={activeUnit.unitDef.icon} alt={activeUnit.unitDef.name} draggable={false} />
        <span className={`count ${activeUnit.count === 1 ? 'single' : ''}`}>
          {activeUnit.count}
        </span>
      </div>
    </DndDragOverlay>
  );
}

export default DragOverlay;

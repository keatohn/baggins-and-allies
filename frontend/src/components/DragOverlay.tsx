import type { CSSProperties } from 'react';
import { DragOverlay as DndDragOverlay } from '@dnd-kit/core';
import './UnitToken.css';
import './MobilizationTray.css';
import './GameMap.css';

export interface BulkDragOverlayStack {
  unitId: string;
  count: number;
  unitDef: { name: string; icon: string };
  factionColor?: string;
  isNaval: boolean;
  passengerCount: number;
}

interface DragOverlayProps {
  bulkDragOverlay?: { stacks: BulkDragOverlayStack[] } | null;
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

function DragOverlay({
  bulkDragOverlay,
  activeUnit,
  activeMobilizationItem,
  activeCampDrag,
  factionColor,
}: DragOverlayProps) {
  if (activeCampDrag) {
    const style: CSSProperties = factionColor
      ? { borderColor: factionColor, cursor: 'grabbing' }
      : { cursor: 'grabbing' };
    return (
      <DndDragOverlay dropAnimation={null} style={{ pointerEvents: 'none' }}>
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
      <DndDragOverlay dropAnimation={null} style={{ pointerEvents: 'none' }}>
        <div className="purchase-stack purchase-stack-overlay" style={style}>
          <img src={activeMobilizationItem.icon} alt={activeMobilizationItem.name} className="purchase-icon" draggable={false} />
          <span className="purchase-count">{activeMobilizationItem.count}</span>
          <span className="purchase-name">{activeMobilizationItem.name}</span>
        </div>
      </DndDragOverlay>
    );
  }

  const bulkStacks = bulkDragOverlay?.stacks;
  if (bulkStacks && bulkStacks.length > 0) {
    return (
      <DndDragOverlay dropAnimation={null} style={{ pointerEvents: 'none' }}>
        <div className="bulk-drag-overlay-stacks" aria-hidden>
          {bulkStacks.map((s) => {
            const style: CSSProperties = s.factionColor ? { borderColor: s.factionColor } : {};
            const passengerCount = s.passengerCount ?? 0;
            return (
              <div
                key={s.unitId}
                className={`unit-token dragging-overlay${s.isNaval ? ' unit-token--naval' : ''}`}
                style={style}
              >
                <img src={s.unitDef.icon} alt={s.unitDef.name} draggable={false} />
                <span className={`count ${s.count === 1 && passengerCount === 0 ? 'single' : ''}`}>{s.count}</span>
                {passengerCount > 0 && (
                  <span className="unit-token-passenger-badge" title={`${passengerCount} aboard`}>
                    {passengerCount}
                  </span>
                )}
              </div>
            );
          })}
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
    <DndDragOverlay dropAnimation={null} style={{ pointerEvents: 'none' }}>
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

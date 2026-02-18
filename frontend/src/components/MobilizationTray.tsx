import type { CSSProperties } from 'react';
import type { FactionId } from '../types/game';
import { useDraggable } from '@dnd-kit/core';
import { CSS } from '@dnd-kit/utilities';
import './MobilizationTray.css';

interface UnitPurchase {
  unitId: string;
  name: string;
  icon: string;
  count: number;
}

interface MobilizationTrayProps {
  isOpen: boolean;
  purchases: UnitPurchase[];
  faction: FactionId;
  factionColor: string;
  selectedUnitId: string | null;
  onSelectUnit: (unitId: string | null) => void;
  activeDragId?: string | null;
}

function DraggablePurchaseStack({
  purchase,
  isSelected,
  onSelect,
  factionColor,
  activeDragId,
}: {
  purchase: UnitPurchase;
  isSelected: boolean;
  onSelect: () => void;
  factionColor: string;
  activeDragId?: string | null;
}) {
  const dragId = `mobilize-${purchase.unitId}`;
  const isActiveDrag = activeDragId === dragId;
  const { attributes, listeners, setNodeRef, transform } = useDraggable({
    id: dragId,
    data: {
      type: 'mobilization-unit',
      unitId: purchase.unitId,
      unitName: purchase.name,
      icon: purchase.icon,
      count: purchase.count,
    },
  });
  const style: CSSProperties = {
    transform: isActiveDrag ? undefined : CSS.Translate.toString(transform),
    opacity: isActiveDrag ? 0 : 1,
  };
  return (
    <div
      ref={setNodeRef}
      className={`purchase-stack ${isSelected ? 'selected' : ''} ${isActiveDrag ? 'dragging-source' : ''}`}
      style={{ ...style, borderColor: factionColor }}
      onClick={onSelect}
      {...attributes}
      {...listeners}
    >
      <img src={purchase.icon} alt={purchase.name} className="purchase-icon" />
      <span className="purchase-count">{purchase.count}</span>
      <span className="purchase-name">{purchase.name}</span>
    </div>
  );
}

function MobilizationTray({
  isOpen,
  purchases,
  faction: _faction,
  factionColor,
  selectedUnitId,
  onSelectUnit,
  activeDragId = null,
}: MobilizationTrayProps) {
  if (!isOpen) return null;

  return (
    <div className="mobilization-tray" style={{ borderColor: factionColor }}>
      {(purchases.length === 0) && (
        <div className="tray-header">
          <span>No more units to mobilize.</span>
        </div>
      )}
      <div className="tray-units">
        {purchases.length > 0 && purchases.map(purchase => (
          <DraggablePurchaseStack
            key={purchase.unitId}
            purchase={purchase}
            isSelected={selectedUnitId === purchase.unitId}
            onSelect={() => onSelectUnit(selectedUnitId === purchase.unitId ? null : purchase.unitId)}
            factionColor={factionColor}
            activeDragId={activeDragId}
          />
        ))}
      </div>
    </div>
  );
}

export default MobilizationTray;

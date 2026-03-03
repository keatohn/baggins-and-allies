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

interface PendingCamp {
  campIndex: number;
  options?: string[];
}

interface MobilizationTrayProps {
  isOpen: boolean;
  purchases: UnitPurchase[];
  pendingCamps: PendingCamp[];
  faction: FactionId;
  factionColor: string;
  selectedUnitId: string | null;
  selectedCampIndex: number | null;
  onSelectUnit: (unitId: string | null) => void;
  onSelectCamp: (campIndex: number | null) => void;
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

function DraggableCampItem({
  campIndex,
  isSelected,
  onSelect,
  factionColor,
  activeDragId,
}: {
  campIndex: number;
  isSelected: boolean;
  onSelect: () => void;
  factionColor: string;
  activeDragId?: string | null;
}) {
  const dragId = `mobilize-camp-${campIndex}`;
  const isActiveDrag = activeDragId === dragId;
  const { attributes, listeners, setNodeRef, transform } = useDraggable({
    id: dragId,
    data: {
      type: 'mobilization-camp',
      campIndex,
    },
  });
  const style: CSSProperties = {
    transform: isActiveDrag ? undefined : CSS.Translate.toString(transform),
    opacity: isActiveDrag ? 0 : 1,
  };
  return (
    <div
      ref={setNodeRef}
      className={`purchase-stack camp-item ${isSelected ? 'selected' : ''} ${isActiveDrag ? 'dragging-source' : ''}`}
      style={{ ...style, borderColor: factionColor }}
      onClick={onSelect}
      {...attributes}
      {...listeners}
    >
      <span className="purchase-icon camp-icon" aria-hidden>⛺</span>
      <span className="purchase-name">Camp</span>
    </div>
  );
}

function MobilizationTray({
  isOpen,
  purchases,
  pendingCamps = [],
  faction: _faction,
  factionColor,
  selectedUnitId,
  selectedCampIndex,
  onSelectUnit,
  onSelectCamp,
  activeDragId = null,
}: MobilizationTrayProps) {
  if (!isOpen) return null;

  const hasItems = purchases.length > 0 || pendingCamps.length > 0;

  return (
    <div className="mobilization-tray" style={{ borderColor: factionColor }}>
      {!hasItems && (
        <div className="tray-header">
          <span>No more units to mobilize.</span>
        </div>
      )}
      <div className="tray-units">
        {purchases.map(purchase => (
          <DraggablePurchaseStack
            key={purchase.unitId}
            purchase={purchase}
            isSelected={selectedUnitId === purchase.unitId}
            onSelect={() => {
              onSelectCamp(null);
              onSelectUnit(selectedUnitId === purchase.unitId ? null : purchase.unitId);
            }}
            factionColor={factionColor}
            activeDragId={activeDragId}
          />
        ))}
        {pendingCamps.map(({ campIndex }) => (
          <DraggableCampItem
            key={`camp-${campIndex}`}
            campIndex={campIndex}
            isSelected={selectedCampIndex === campIndex}
            onSelect={() => {
              onSelectUnit(null);
              onSelectCamp(selectedCampIndex === campIndex ? null : campIndex);
            }}
            factionColor={factionColor}
            activeDragId={activeDragId}
          />
        ))}
      </div>
    </div>
  );
}

export default MobilizationTray;

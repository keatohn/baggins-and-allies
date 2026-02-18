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
}: DraggableUnitProps) {
  const { attributes, listeners, setNodeRef, transform, isDragging } = useDraggable({
    id,
    data: {
      unitId,
      territoryId,
      count,
      unitDef,
      factionColor,
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

  return (
    <div
      ref={setNodeRef}
      className={`unit-token ${isSelected ? 'selected' : ''} ${isDragging ? 'dragging' : ''} ${disabled ? 'disabled' : ''}`}
      style={style}
      title={`${unitDef.name} ×${count}`}
      {...listeners}
      {...attributes}
    >
      <img src={unitDef.icon} alt={unitDef.name} draggable={false} />
      <span className={`count ${count === 1 ? 'single' : ''}`}>{count}</span>
    </div>
  );
}

export default DraggableUnit;

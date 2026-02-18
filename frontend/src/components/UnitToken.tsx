import type { CSSProperties } from 'react';
import './UnitToken.css';

interface UnitTokenProps {
  unitId: string;
  count: number;
  unitDef?: { name: string; icon: string };
  isSelected: boolean;
  onClick: () => void;
  factionColor?: string; // Color from faction definition
}

function UnitToken({ unitId: _unitId, count, unitDef, isSelected, onClick, factionColor }: UnitTokenProps) {
  if (!unitDef) return null;

  const style: CSSProperties = factionColor ? { borderColor: factionColor } : {};

  return (
    <div
      className={`unit-token ${isSelected ? 'selected' : ''}`}
      style={style}
      title={`${unitDef.name} ×${count}`}
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
    >
      <img src={unitDef.icon} alt={unitDef.name} />
      <span className="count">{count}</span>
    </div>
  );
}

export default UnitToken;

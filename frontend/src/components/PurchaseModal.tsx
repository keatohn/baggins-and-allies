import { useState, useMemo, useEffect } from 'react';
import './PurchaseModal.css';

interface UnitPurchaseInfo {
  id: string;
  name: string;
  icon: string;
  cost: Record<string, number>;
  attack: number;
  defense: number;
  movement: number;
  health: number;
  dice: number;
}

interface PurchaseModalProps {
  isOpen: boolean;
  availableResources: Record<string, number>;
  availableUnits: UnitPurchaseInfo[];
  currentPurchases: Record<string, number>;
  /** Max units that can be mobilized this turn (from backend). Total purchased cannot exceed this. */
  mobilizationCapacity?: number;
  /** Units already purchased this turn (from backend). */
  purchasedUnitsCount?: number;
  onPurchase: (purchases: Record<string, number>) => void;
  onClose: () => void;
}

// Helper to format cost display (e.g., "2P | 1F")
function formatCost(cost: Record<string, number>): string {
  return Object.entries(cost)
    .filter(([, amount]) => amount > 0)
    .map(([resource, amount]) => `${amount}${resource[0].toUpperCase()}`)
    .join(' | ') || '0';
}

function PurchaseModal({
  isOpen,
  availableResources,
  availableUnits,
  currentPurchases,
  mobilizationCapacity,
  purchasedUnitsCount: _purchasedUnitsCount = 0,
  onPurchase,
  onClose,
}: PurchaseModalProps) {
  const [quantities, setQuantities] = useState<Record<string, number>>({});

  // Sync quantities with currentPurchases when modal opens
  useEffect(() => {
    if (isOpen) {
      setQuantities(currentPurchases);
    }
  }, [isOpen, currentPurchases]);

  // Calculate total cost per resource
  const totalCosts = useMemo(() => {
    const costs: Record<string, number> = {};
    for (const [unitId, qty] of Object.entries(quantities)) {
      const unit = availableUnits.find(u => u.id === unitId);
      if (unit) {
        for (const [resource, amount] of Object.entries(unit.cost)) {
          costs[resource] = (costs[resource] || 0) + amount * qty;
        }
      }
    }
    return costs;
  }, [quantities, availableUnits]);

  // Calculate remaining resources
  const remainingResources = useMemo(() => {
    const remaining: Record<string, number> = { ...availableResources };
    for (const [resource, spent] of Object.entries(totalCosts)) {
      remaining[resource] = (remaining[resource] || 0) - spent;
    }
    return remaining;
  }, [availableResources, totalCosts]);

  // Check if player can afford a unit
  const canAfford = (unit: UnitPurchaseInfo) => {
    for (const [resource, amount] of Object.entries(unit.cost)) {
      if ((remainingResources[resource] || 0) < amount) {
        return false;
      }
    }
    return true;
  };

  const handleQuantityChange = (unitId: string, delta: number) => {
    setQuantities(prev => {
      const current = prev[unitId] || 0;
      const newQty = Math.max(0, current + delta);
      const totalInCart = Object.values(prev).reduce((s, q) => s + q, 0) - current + newQty;

      const unit = availableUnits.find(u => u.id === unitId);
      if (!unit) return prev;

      if (delta > 0) {
        if (!canAfford(unit)) return prev;
        if (mobilizationCapacity != null && totalInCart > mobilizationCapacity) return prev;
      }

      if (newQty === 0) {
        const { [unitId]: _, ...rest } = prev;
        return rest;
      }

      return { ...prev, [unitId]: newQty };
    });
  };

  const handleConfirm = () => {
    if (mobilizationCapacity != null && totalUnits > mobilizationCapacity) return;
    onPurchase(quantities);
    onClose();
  };

  const handleCancel = () => {
    setQuantities(currentPurchases);
    onClose();
  };

  if (!isOpen) return null;

  const totalUnits = Object.values(quantities).reduce((sum, qty) => sum + qty, 0);
  const atMobilizationCap = mobilizationCapacity != null && totalUnits >= mobilizationCapacity;

  return (
    <div className="modal-overlay" onClick={handleCancel}>
      <div className="modal purchase-modal" onClick={e => e.stopPropagation()}>
        <header className="modal-header">
          <h2>Purchase Units</h2>
          <button className="close-btn" onClick={handleCancel}>×</button>
        </header>

        <div className="resources-display">
          {Object.entries(availableResources).map(([resource, amount]) => {
            const resourceLabel = resource.charAt(0).toUpperCase() + resource.slice(1);
            return (
              <div key={resource} className="resource-row">
                <span className="resource-label">{resourceLabel}:</span>
                <span className="resource-value">{amount}</span>
              </div>
            );
          })}
        </div>

        {mobilizationCapacity != null && (
          <p className="mobilization-capacity">
            Mobilization Capacity: <strong>{totalUnits}/{mobilizationCapacity}</strong>
          </p>
        )}
        
        <div className="unit-list">
          {availableUnits.map(unit => {
            const qty = quantities[unit.id] || 0;
            const affordable = canAfford(unit);
            
            return (
              <div key={unit.id} className="unit-row">
                <div className="unit-info">
                  <img src={unit.icon} alt={unit.name} className="unit-icon" />
                  <div className="unit-details">
                    <span className="unit-name">{unit.name}</span>
                    <span className="unit-stats">
                      {unit.attack}A | {unit.defense}D | {unit.dice}R | {unit.movement}M | {unit.health}HP
                    </span>
                  </div>
                </div>
                
                <div className="unit-cost">
                  <span className="cost-value">{formatCost(unit.cost)}</span>
                </div>
                
                <div className="quantity-controls">
                  <button 
                    onClick={() => handleQuantityChange(unit.id, -1)}
                    disabled={qty === 0}
                  >
                    −
                  </button>
                  <span className="quantity">{qty}</span>
                  <button 
                    onClick={() => handleQuantityChange(unit.id, 1)}
                    disabled={!affordable || atMobilizationCap}
                  >
                    +
                  </button>
                </div>
              </div>
            );
          })}
        </div>

        <p className="unit-stats-key">
          A = Attack | D = Defense | R = Dice rolls | M = Moves | HP = Hit Points
        </p>
        
        <footer className="modal-footer">
          <div className="purchase-summary">
            <span>{totalUnits} units for {formatCost(totalCosts)}</span>
          </div>
          <div className="modal-actions">
            <button onClick={handleCancel}>Cancel</button>
            <button 
              className="primary" 
              onClick={handleConfirm}
            >
              Confirm
            </button>
          </div>
        </footer>
      </div>
    </div>
  );
}

export default PurchaseModal;

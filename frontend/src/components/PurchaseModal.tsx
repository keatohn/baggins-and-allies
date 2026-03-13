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
  /** True if this unit is naval (mobilizes to sea zone). Used to show in Sea tab. */
  isNaval?: boolean;
  /** Number of specials (len of specials list). Shown as SP in purchase modal. */
  specialsCount?: number;
  /** Home territory count for this unit type (adds to land mobilization display denominator when in cart). */
  homeTerritoryCount?: number;
}

interface PurchaseModalProps {
  isOpen: boolean;
  /** Faction color for unit icon borders (e.g. from factionData[faction].color). */
  factionColor?: string;
  availableResources: Record<string, number>;
  availableUnits: UnitPurchaseInfo[];
  /** If true, show Sea tab when there are naval units to purchase. Hide if no port or no naval units (e.g. Isengard with a conquered port). */
  hasPort?: boolean;
  currentPurchases: Record<string, number>;
  /** Number of camps in cart (bought in purchase phase, placed in mobilization). */
  currentCamps?: number;
  /** Max camps that can be purchased (number of owned territories without a camp). */
  maxCamps?: number;
  /** Max units that can be mobilized this turn (from backend). Total units purchased cannot exceed this; camps do not count. */
  mobilizationCapacity?: number;
  /** Land mobilization capacity (camps + home slots). Land units in cart cannot exceed this. */
  mobilizationLandCapacity?: number;
  /** Land capacity from camps only (excl. home). When set, display denominator = this + home slots from cart. */
  mobilizationCampLandCapacity?: number;
  /** Sea mobilization capacity (port sea zones). Naval units in cart cannot exceed this. */
  mobilizationSeaCapacity?: number;
  /** Units already purchased this turn (from backend). */
  purchasedUnitsCount?: number;
  /** Power cost per camp (0 or undefined = camps not purchasable). */
  campCost?: number;
  onPurchase: (purchases: Record<string, number>, campsCount: number) => void;
  onClose: () => void;
}

// Helper to format cost display (e.g., "2P | 1F")
function formatCost(cost: Record<string, number>): string {
  return Object.entries(cost)
    .filter(([, amount]) => amount > 0)
    .map(([resource, amount]) => `${amount}${resource[0].toUpperCase()}`)
    .join(' | ') || '0';
}

type PurchaseTab = 'land' | 'sea' | 'other';

function PurchaseModal({
  isOpen,
  factionColor,
  availableResources,
  availableUnits,
  hasPort = false,
  currentPurchases,
  currentCamps = 0,
  maxCamps = 0,
  mobilizationCapacity,
  mobilizationLandCapacity,
  mobilizationCampLandCapacity,
  mobilizationSeaCapacity,
  purchasedUnitsCount: _purchasedUnitsCount = 0,
  campCost = 10,
  onPurchase,
  onClose,
}: PurchaseModalProps) {
  const landUnits = useMemo(() => availableUnits.filter(u => !u.isNaval), [availableUnits]);
  const seaUnits = useMemo(() => availableUnits.filter(u => u.isNaval), [availableUnits]);

  const [activeTab, setActiveTab] = useState<PurchaseTab>('land');
  const [quantities, setQuantities] = useState<Record<string, number>>({});
  const [campQuantity, setCampQuantity] = useState(0);

  const landInCart = useMemo(
    () => landUnits.reduce((s, u) => s + (quantities[u.id] || 0), 0),
    [landUnits, quantities]
  );
  const seaInCart = useMemo(
    () => seaUnits.reduce((s, u) => s + (quantities[u.id] || 0), 0),
    [seaUnits, quantities]
  );

  // Display land denominator: camp capacity + home slots from unit types currently in cart (1 per home territory per type), capped by backend total
  const displayLandDenominator = useMemo(() => {
    const campCap = mobilizationCampLandCapacity ?? mobilizationLandCapacity ?? 0;
    const homeSlots = landUnits
      .filter(u => (quantities[u.id] || 0) > 0 && (u.homeTerritoryCount ?? 0) > 0)
      .reduce((s, u) => s + (u.homeTerritoryCount ?? 0), 0);
    const withHome = campCap + homeSlots;
    return mobilizationLandCapacity != null ? Math.min(withHome, mobilizationLandCapacity) : withHome;
  }, [mobilizationCampLandCapacity, mobilizationLandCapacity, landUnits, quantities]);

  // Sync quantities and camps when modal opens; clamp camps to maxCamps; always open on Land tab
  useEffect(() => {
    if (isOpen) {
      setActiveTab('land');
      setQuantities(currentPurchases);
      const clamped = maxCamps != null && maxCamps > 0 ? Math.min(currentCamps, maxCamps) : currentCamps;
      setCampQuantity(clamped);
    }
  }, [isOpen, currentPurchases, currentCamps, maxCamps]);

  // Total cost for units only
  const totalUnitCosts = useMemo(() => {
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

  // Total cost including camps (power only)
  const totalCosts = useMemo(() => {
    const costs = { ...totalUnitCosts };
    if (campCost > 0 && campQuantity > 0) {
      costs.power = (costs.power || 0) + campQuantity * campCost;
    }
    return costs;
  }, [totalUnitCosts, campQuantity, campCost]);

  // Remaining resources after units + camps
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
        if (unit.isNaval && mobilizationSeaCapacity != null) {
          const newSeaInCart = seaInCart - (prev[unitId] || 0) + newQty;
          if (newSeaInCart > mobilizationSeaCapacity) return prev;
        }
        if (!unit.isNaval && (mobilizationLandCapacity != null || mobilizationCampLandCapacity != null)) {
          const newQuantities = { ...prev, [unitId]: newQty };
          const newLandInCart = landUnits.reduce((s, u) => s + (newQuantities[u.id] || 0), 0);
          const campCap = mobilizationCampLandCapacity ?? mobilizationLandCapacity ?? 0;
          const homeSlots = landUnits
            .filter(u => (newQuantities[u.id] || 0) > 0 && (u.homeTerritoryCount ?? 0) > 0)
            .reduce((s, u) => s + (u.homeTerritoryCount ?? 0), 0);
          const newLandCap = Math.min(campCap + homeSlots, mobilizationLandCapacity ?? Infinity);
          if (newLandInCart > newLandCap) return prev;
        }
      }

      if (newQty === 0) {
        const { [unitId]: _, ...rest } = prev;
        return rest;
      }

      return { ...prev, [unitId]: newQty };
    });
  };

  const totalUnits = Object.values(quantities).reduce((sum, qty) => sum + qty, 0);
  const atMobilizationCap =
    mobilizationCapacity != null && totalUnits >= mobilizationCapacity;
  const canAffordOneMoreCamp = campCost > 0 && (remainingResources.power ?? 0) >= campCost;
  const atCampCap = maxCamps !== undefined && maxCamps > 0 && campQuantity >= maxCamps;

  const handleCampChange = (delta: number) => {
    setCampQuantity(prev => {
      const next = prev + delta;
      if (maxCamps != null && maxCamps > 0 && next > maxCamps) return maxCamps;
      return Math.max(0, next);
    });
  };

  const handleConfirm = () => {
    if (mobilizationCapacity != null && totalUnits > mobilizationCapacity) return;
    if (landInCart > displayLandDenominator) return;
    if (mobilizationSeaCapacity != null && seaInCart > mobilizationSeaCapacity) return;
    const campsToSubmit = maxCamps != null && maxCamps > 0 ? Math.min(campQuantity, maxCamps) : campQuantity;
    onPurchase(quantities, campsToSubmit);
    onClose();
  };

  const handleCancel = () => {
    setQuantities(currentPurchases);
    setCampQuantity(currentCamps);
    onClose();
  };

  if (!isOpen) return null;

  return (
    <div className="modal-overlay" onClick={handleCancel}>
      <div className="modal purchase-modal" onClick={e => e.stopPropagation()}>
        <header className="modal-header">
          <h2>Purchase</h2>
          <button className="close-btn" onClick={handleCancel}>×</button>
        </header>

        <div className="purchase-modal-tabs">
          <button
            type="button"
            className={`purchase-tab ${activeTab === 'land' ? 'active' : ''}`}
            onClick={() => setActiveTab('land')}
          >
            Land
          </button>
          {hasPort && seaUnits.length > 0 && (
            <button
              type="button"
              className={`purchase-tab ${activeTab === 'sea' ? 'active' : ''}`}
              onClick={() => setActiveTab('sea')}
            >
              Sea
            </button>
          )}
          <button
            type="button"
            className={`purchase-tab ${activeTab === 'other' ? 'active' : ''}`}
            onClick={() => setActiveTab('other')}
          >
            Other
          </button>
        </div>

        <div className="resources-display">
          {Object.entries(availableResources).map(([resource]) => {
            const resourceLabel = resource.charAt(0).toUpperCase() + resource.slice(1);
            const remaining = remainingResources[resource] ?? 0;
            return (
              <div key={resource} className="resource-row">
                <span className="resource-label">{resourceLabel}:</span>
                <span className="resource-value">{remaining}</span>
              </div>
            );
          })}
        </div>

        {(activeTab === 'land' || activeTab === 'sea') && (mobilizationCapacity != null || mobilizationLandCapacity != null || mobilizationCampLandCapacity != null || mobilizationSeaCapacity != null) && (
          <p className="mobilization-capacity">
            {(mobilizationLandCapacity != null || mobilizationCampLandCapacity != null) && (mobilizationSeaCapacity == null || seaUnits.length === 0) ? (
              <>Land: <strong>{landInCart}/{displayLandDenominator}</strong></>
            ) : (mobilizationLandCapacity != null || mobilizationCampLandCapacity != null) && mobilizationSeaCapacity != null && seaUnits.length > 0 ? (
              <>Land: <strong>{landInCart}/{displayLandDenominator}</strong> | Sea: <strong>{seaInCart}/{mobilizationSeaCapacity}</strong></>
            ) : (
              <>Mobilization Capacity: <strong>{totalUnits}/{mobilizationCapacity ?? 0}</strong></>
            )}
          </p>
        )}

        {activeTab === 'other' && campCost > 0 && maxCamps != null && maxCamps > 0 && (
          <p className="mobilization-capacity">
            Camp Capacity: <strong>{campQuantity}/{maxCamps}</strong>
          </p>
        )}

        {activeTab === 'land' && (
          <>
            <div className="unit-list">
              {landUnits.map(unit => {
                const qty = quantities[unit.id] || 0;
                const affordable = canAfford(unit);
                return (
                    <div key={unit.id} className="unit-row">
                    <div className="unit-info">
                      <span
                        className="unit-icon-wrap"
                        style={factionColor ? { ['--faction-border' as string]: factionColor } : undefined}
                      >
                        <img src={unit.icon} alt={unit.name} className="unit-icon" />
                      </span>
                      <div className="unit-details">
                        <span className="unit-name">{unit.name}</span>
                        <span className="unit-stats">
                          {unit.attack}A | {unit.defense}D | {unit.dice}R | {unit.movement}M | {unit.health}HP | {(unit.specialsCount ?? 0)}SP
                        </span>
                      </div>
                    </div>
                    <div className="unit-cost">
                      <span className="cost-value">{formatCost(unit.cost)}</span>
                    </div>
                    <div className="quantity-controls">
                      <button onClick={() => handleQuantityChange(unit.id, -1)} disabled={qty === 0}>−</button>
                      <span className="quantity">{qty}</span>
                      <button onClick={() => handleQuantityChange(unit.id, 1)} disabled={!affordable || atMobilizationCap}>+</button>
                    </div>
                  </div>
                );
              })}
            </div>
            <p className="unit-stats-key">
              A = Attack | D = Defense | R = Dice rolls | M = Moves | HP = Hit Points | SP = Specials
            </p>
          </>
        )}

        {activeTab === 'sea' && (
          <>
            <div className="unit-list">
              {seaUnits.map(unit => {
                const qty = quantities[unit.id] || 0;
                const affordable = canAfford(unit);
                return (
                    <div key={unit.id} className="unit-row">
                    <div className="unit-info">
                      <span
                        className="unit-icon-wrap"
                        style={factionColor ? { ['--faction-border' as string]: factionColor } : undefined}
                      >
                        <img src={unit.icon} alt={unit.name} className="unit-icon" />
                      </span>
                      <div className="unit-details">
                        <span className="unit-name">{unit.name}</span>
                        <span className="unit-stats">
                          {unit.attack}A | {unit.defense}D | {unit.dice}R | {unit.movement}M | {unit.health}HP | {(unit.specialsCount ?? 0)}SP
                        </span>
                      </div>
                    </div>
                    <div className="unit-cost">
                      <span className="cost-value">{formatCost(unit.cost)}</span>
                    </div>
                    <div className="quantity-controls">
                      <button onClick={() => handleQuantityChange(unit.id, -1)} disabled={qty === 0}>−</button>
                      <span className="quantity">{qty}</span>
                      <button onClick={() => handleQuantityChange(unit.id, 1)} disabled={!affordable || atMobilizationCap}>+</button>
                    </div>
                  </div>
                );
              })}
            </div>
            <p className="unit-stats-key">
              A = Attack | D = Defense | R = Dice rolls | M = Moves | HP = Hit Points | SP = Specials
            </p>
          </>
        )}

        {activeTab === 'other' && (
          <div className="unit-list">
            {campCost > 0 ? (
              <div className="unit-row">
                <div className="unit-info">
                  <span className="purchase-modal-camp-icon" aria-hidden>⛺</span>
                  <div className="unit-details">
                    <span className="unit-name">Camp</span>
                    <span className="unit-stats">Mobilize units to a camp up to the territory's power production</span>
                  </div>
                </div>
                <div className="unit-cost">
                  <span className="cost-value">{campCost}P</span>
                </div>
                <div className="quantity-controls">
                  <button onClick={() => handleCampChange(-1)} disabled={campQuantity === 0}>−</button>
                  <span className="quantity">{campQuantity}</span>
                  <button onClick={() => handleCampChange(1)} disabled={!canAffordOneMoreCamp || atCampCap}>+</button>
                </div>
              </div>
            ) : (
              <p className="purchase-other-empty">No other purchases available.</p>
            )}
          </div>
        )}

        <footer className="modal-footer">
          <div className="purchase-summary">
            <span>
              {totalUnits} units
              {campQuantity > 0 && `, ${campQuantity} camp${campQuantity !== 1 ? 's' : ''}`}
              {' for '}{formatCost(totalCosts)}
            </span>
          </div>
          <div className="modal-actions">
            <button onClick={handleCancel}>Cancel</button>
            <button className="primary" onClick={handleConfirm}>Confirm</button>
          </div>
        </footer>
      </div>
    </div>
  );
}

export default PurchaseModal;

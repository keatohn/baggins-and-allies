import { useState, useMemo, useEffect, useRef, useCallback } from 'react';
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
  /** True if siegework archetype; shown in Siege tab (after Sea). Excludes naval. */
  isSiegework?: boolean;
  /** Human-readable special names (from setup specials defs), shown below numeric stats. */
  specialLabels?: string[];
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
  /** Power cost per HP to repair a stronghold (0 or undefined = repair not available). */
  strongholdRepairCost?: number;
  /** Strongholds owned by current faction that have currentHp < baseHp (only these show in Other tab). */
  repairableStrongholds?: { territoryId: string; name: string; currentHp: number; baseHp: number }[];
  /** Pending repairs (from cart) so we can init target HP when modal opens. */
  currentRepairs?: { territory_id: string; hp_to_add: number }[];
  onPurchase: (purchases: Record<string, number>, campsCount: number, repairs?: { territory_id: string; hp_to_add: number }[]) => void;
  onClose: () => void;
}

// Helper to format cost display (e.g., "2P | 1F")
function formatCost(cost: Record<string, number>): string {
  return Object.entries(cost)
    .filter(([, amount]) => amount > 0)
    .map(([resource, amount]) => `${amount}${resource[0].toUpperCase()}`)
    .join(' | ') || '0';
}

type PurchaseTab = 'land' | 'sea' | 'siege' | 'other';

function unitPowerCost(cost: Record<string, number>): number {
  return Object.values(cost || {}).reduce((sum, v) => sum + (Number.isFinite(v) ? v : 0), 0);
}

function compareUnitsForPurchase(a: UnitPurchaseInfo, b: UnitPurchaseInfo): number {
  return (
    unitPowerCost(a.cost) - unitPowerCost(b.cost)
    || a.dice - b.dice
    || a.attack - b.attack
    || (a.specialLabels?.length ?? 0) - (b.specialLabels?.length ?? 0)
    || a.name.localeCompare(b.name)
  );
}

/** Cart costs from unit quantities + camp + repair (must match totalCosts / remainingResources logic). */
function buildPurchaseCostTotals(
  quantities: Record<string, number>,
  units: UnitPurchaseInfo[],
  campQty: number,
  campCostPower: number,
  repairPowerTotal: number,
): Record<string, number> {
  const costs: Record<string, number> = {};
  for (const [id, qty] of Object.entries(quantities)) {
    if (qty <= 0) continue;
    const u = units.find(x => x.id === id);
    if (!u) continue;
    for (const [r, amt] of Object.entries(u.cost)) {
      costs[r] = (costs[r] || 0) + amt * qty;
    }
  }
  if (campCostPower > 0 && campQty > 0) {
    costs.power = (costs.power || 0) + campQty * campCostPower;
  }
  if (repairPowerTotal > 0) {
    costs.power = (costs.power || 0) + repairPowerTotal;
  }
  return costs;
}

function canAffordCostTotals(available: Record<string, number>, costs: Record<string, number>): boolean {
  for (const [r, spent] of Object.entries(costs)) {
    if ((available[r] || 0) < spent) return false;
  }
  return true;
}

function UnitStatBlock({ unit }: { unit: UnitPurchaseInfo }) {
  const labels = unit.specialLabels?.filter(Boolean) ?? [];
  return (
    <div className="unit-stat-stack">
      <span className="unit-stats unit-stats--inline">
        {unit.attack}A | {unit.defense}D | {unit.dice}R | {unit.movement}M | {unit.health}HP
      </span>
      <div className="unit-stats-specials-row">
        {labels.length > 0 ? (
          <span className="unit-stats-specials" title={labels.join(', ')}>
            {labels.join(' · ')}
          </span>
        ) : null}
      </div>
    </div>
  );
}

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
  strongholdRepairCost = 0,
  repairableStrongholds = [],
  currentRepairs = [],
  onPurchase,
  onClose,
}: PurchaseModalProps) {
  const nonNavalUnits = useMemo(() => availableUnits.filter(u => !u.isNaval), [availableUnits]);
  const landTabUnits = useMemo(
    () => availableUnits
      .filter(u => !u.isNaval && !u.isSiegework)
      .slice()
      .sort(compareUnitsForPurchase),
    [availableUnits]
  );
  const siegeUnits = useMemo(
    () => availableUnits
      .filter(u => !u.isNaval && u.isSiegework)
      .slice()
      .sort(compareUnitsForPurchase),
    [availableUnits]
  );
  const seaUnits = useMemo(
    () => availableUnits
      .filter(u => u.isNaval)
      .slice()
      .sort(compareUnitsForPurchase),
    [availableUnits]
  );

  const [activeTab, setActiveTab] = useState<PurchaseTab>('land');
  const [quantities, setQuantities] = useState<Record<string, number>>({});
  const [campQuantity, setCampQuantity] = useState(0);
  /** Target HP per repairable stronghold (territoryId -> HP to have after repair). */
  const [repairTargetHp, setRepairTargetHp] = useState<Record<string, number>>({});
  /** Avoid resetting in-progress picks when parent re-renders (e.g. multiplayer poll updates props). */
  const wasOpenRef = useRef(false);

  const landInCart = useMemo(
    () => nonNavalUnits.reduce((s, u) => s + (quantities[u.id] || 0), 0),
    [nonNavalUnits, quantities]
  );
  const seaInCart = useMemo(
    () => seaUnits.reduce((s, u) => s + (quantities[u.id] || 0), 0),
    [seaUnits, quantities]
  );

  // Display land denominator: camp capacity + home slots from unit types currently in cart (1 per home territory per type), capped by backend total
  const displayLandDenominator = useMemo(() => {
    const campCap = mobilizationCampLandCapacity ?? mobilizationLandCapacity ?? 0;
    const homeSlots = nonNavalUnits
      .filter(u => (quantities[u.id] || 0) > 0 && (u.homeTerritoryCount ?? 0) > 0)
      .reduce((s, u) => s + (u.homeTerritoryCount ?? 0), 0);
    const withHome = campCap + homeSlots;
    return mobilizationLandCapacity != null ? Math.min(withHome, mobilizationLandCapacity) : withHome;
  }, [mobilizationCampLandCapacity, mobilizationLandCapacity, nonNavalUnits, quantities]);

  // Sync from props only on open — not when isOpen stays true and poll refreshes repairableStrongholds / etc.
  useEffect(() => {
    if (!isOpen) {
      wasOpenRef.current = false;
      return;
    }
    if (wasOpenRef.current) return;
    wasOpenRef.current = true;
    setActiveTab('land');
    setQuantities({ ...currentPurchases });
    const clamped = maxCamps != null && maxCamps > 0 ? Math.min(currentCamps, maxCamps) : currentCamps;
    setCampQuantity(clamped);
    const targets: Record<string, number> = {};
    for (const s of repairableStrongholds) {
      const added = currentRepairs.find(r => r.territory_id === s.territoryId)?.hp_to_add ?? 0;
      targets[s.territoryId] = Math.min(s.baseHp, s.currentHp + added);
    }
    setRepairTargetHp(targets);
  }, [
    isOpen,
    currentPurchases,
    currentCamps,
    maxCamps,
    repairableStrongholds,
    currentRepairs,
  ]);

  useEffect(() => {
    if (activeTab === 'siege' && siegeUnits.length === 0) setActiveTab('land');
  }, [activeTab, siegeUnits.length]);

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

  // Total cost including camps and stronghold repairs (power only)
  const repairCostTotal = useMemo(() => {
    if (strongholdRepairCost <= 0) return 0;
    let hp = 0;
    for (const s of repairableStrongholds) {
      const target = repairTargetHp[s.territoryId] ?? s.currentHp;
      hp += Math.max(0, Math.min(target, s.baseHp) - s.currentHp);
    }
    return hp * strongholdRepairCost;
  }, [strongholdRepairCost, repairableStrongholds, repairTargetHp]);

  const totalCosts = useMemo(() => {
    const costs = { ...totalUnitCosts };
    if (campCost > 0 && campQuantity > 0) {
      costs.power = (costs.power || 0) + campQuantity * campCost;
    }
    if (repairCostTotal > 0) {
      costs.power = (costs.power || 0) + repairCostTotal;
    }
    return costs;
  }, [totalUnitCosts, campQuantity, campCost, repairCostTotal]);

  // Remaining resources after units + camps
  const remainingResources = useMemo(() => {
    const remaining: Record<string, number> = { ...availableResources };
    for (const [resource, spent] of Object.entries(totalCosts)) {
      remaining[resource] = (remaining[resource] || 0) - spent;
    }
    return remaining;
  }, [availableResources, totalCosts]);

  // Check if player can afford one more of this unit (for + button enablement; uses current render state)
  const canAfford = (unit: UnitPurchaseInfo) => {
    for (const [resource, amount] of Object.entries(unit.cost)) {
      if ((remainingResources[resource] || 0) < amount) {
        return false;
      }
    }
    return true;
  };

  const handleQuantityChange = useCallback(
    (unitId: string, delta: number) => {
      setQuantities(prev => {
        const current = prev[unitId] || 0;
        const newQty = Math.max(0, current + delta);
        const unit = availableUnits.find(u => u.id === unitId);
        if (!unit) return prev;

        if (delta > 0) {
          const tryState = { ...prev, [unitId]: newQty };
          const tryCosts = buildPurchaseCostTotals(
            tryState,
            availableUnits,
            campQuantity,
            campCost,
            repairCostTotal,
          );
          if (!canAffordCostTotals(availableResources, tryCosts)) return prev;

          const totalInCart = Object.values(tryState).reduce((s, q) => s + q, 0);
          if (mobilizationCapacity != null && totalInCart > mobilizationCapacity) return prev;

          if (unit.isNaval && mobilizationSeaCapacity != null) {
            const newSeaInCart = seaUnits.reduce((s, u) => s + (tryState[u.id] || 0), 0);
            if (newSeaInCart > mobilizationSeaCapacity) return prev;
          }
          if (!unit.isNaval && (mobilizationLandCapacity != null || mobilizationCampLandCapacity != null)) {
            const newLandInCart = nonNavalUnits.reduce((s, u) => s + (tryState[u.id] || 0), 0);
            const campCap = mobilizationCampLandCapacity ?? mobilizationLandCapacity ?? 0;
            const homeSlots = nonNavalUnits
              .filter(u => (tryState[u.id] || 0) > 0 && (u.homeTerritoryCount ?? 0) > 0)
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
    },
    [
      availableUnits,
      availableResources,
      campQuantity,
      campCost,
      repairCostTotal,
      mobilizationCapacity,
      mobilizationSeaCapacity,
      mobilizationLandCapacity,
      mobilizationCampLandCapacity,
      nonNavalUnits,
      seaUnits,
    ],
  );

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
    const repairs: { territory_id: string; hp_to_add: number }[] = [];
    for (const s of repairableStrongholds) {
      const target = repairTargetHp[s.territoryId] ?? s.currentHp;
      const hpToAdd = Math.max(0, Math.min(target, s.baseHp) - s.currentHp);
      if (hpToAdd > 0) repairs.push({ territory_id: s.territoryId, hp_to_add: hpToAdd });
    }
    onPurchase(quantities, campsToSubmit, repairs);
    onClose();
  };

  const handleCancel = () => {
    setQuantities(currentPurchases);
    setCampQuantity(currentCamps);
    setRepairTargetHp({});
    onClose();
  };

  const handleRepairTargetChange = (territoryId: string, delta: number) => {
    const s = repairableStrongholds.find(r => r.territoryId === territoryId);
    if (!s) return;
    setRepairTargetHp(prev => {
      const current = prev[territoryId] ?? s.currentHp;
      const next = Math.max(s.currentHp, Math.min(s.baseHp, current + delta));
      return { ...prev, [territoryId]: next };
    });
  };

  if (!isOpen) return null;

  return (
    <div className="modal-overlay" onClick={handleCancel}>
      <div className="modal purchase-modal" onClick={e => e.stopPropagation()}>
        <header className="modal-header">
          <h2>Purchase</h2>
          <button type="button" className="close-btn" onClick={handleCancel}>×</button>
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
          {siegeUnits.length > 0 && (
            <button
              type="button"
              className={`purchase-tab ${activeTab === 'siege' ? 'active' : ''}`}
              onClick={() => setActiveTab('siege')}
            >
              Siege
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

        {(activeTab === 'land' || activeTab === 'sea' || activeTab === 'siege') && (mobilizationCapacity != null || mobilizationLandCapacity != null || mobilizationCampLandCapacity != null || mobilizationSeaCapacity != null) && (
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
              {landTabUnits.map(unit => {
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
                        <UnitStatBlock unit={unit} />
                      </div>
                    </div>
                    <div className="unit-cost">
                      <span className="cost-value">{formatCost(unit.cost)}</span>
                    </div>
                    <div className="quantity-controls">
                      <button type="button" onClick={() => handleQuantityChange(unit.id, -1)} disabled={qty === 0}>−</button>
                      <span className="quantity">{qty}</span>
                      <button type="button" onClick={() => handleQuantityChange(unit.id, 1)} disabled={!affordable || atMobilizationCap}>+</button>
                    </div>
                  </div>
                );
              })}
            </div>
            <p className="unit-stats-key">
              A = Attack | D = Defense | R = Dice rolls | M = Moves | HP = Hit Points
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
                        <UnitStatBlock unit={unit} />
                      </div>
                    </div>
                    <div className="unit-cost">
                      <span className="cost-value">{formatCost(unit.cost)}</span>
                    </div>
                    <div className="quantity-controls">
                      <button type="button" onClick={() => handleQuantityChange(unit.id, -1)} disabled={qty === 0}>−</button>
                      <span className="quantity">{qty}</span>
                      <button type="button" onClick={() => handleQuantityChange(unit.id, 1)} disabled={!affordable || atMobilizationCap}>+</button>
                    </div>
                  </div>
                );
              })}
            </div>
            <p className="unit-stats-key">
              A = Attack | D = Defense | R = Dice rolls | M = Moves | HP = Hit Points
            </p>
          </>
        )}

        {activeTab === 'siege' && (
          <>
            <div className="unit-list">
              {siegeUnits.map(unit => {
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
                        <UnitStatBlock unit={unit} />
                      </div>
                    </div>
                    <div className="unit-cost">
                      <span className="cost-value">{formatCost(unit.cost)}</span>
                    </div>
                    <div className="quantity-controls">
                      <button type="button" onClick={() => handleQuantityChange(unit.id, -1)} disabled={qty === 0}>−</button>
                      <span className="quantity">{qty}</span>
                      <button type="button" onClick={() => handleQuantityChange(unit.id, 1)} disabled={!affordable || atMobilizationCap}>+</button>
                    </div>
                  </div>
                );
              })}
            </div>
            <p className="unit-stats-key">
              A = Attack | D = Defense | R = Dice rolls | M = Moves | HP = Hit Points
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
                  <button type="button" onClick={() => handleCampChange(-1)} disabled={campQuantity === 0}>−</button>
                  <span className="quantity">{campQuantity}</span>
                  <button type="button" onClick={() => handleCampChange(1)} disabled={!canAffordOneMoreCamp || atCampCap}>+</button>
                </div>
              </div>
            ) : null}
            {strongholdRepairCost > 0 && repairableStrongholds.length > 0
              ? repairableStrongholds.map(s => {
                  const target = repairTargetHp[s.territoryId] ?? s.currentHp;
                  const canAffordMore = (remainingResources.power ?? 0) >= strongholdRepairCost;
                  const atCap = target >= s.baseHp;
                  return (
                    <div key={s.territoryId} className="unit-row">
                      <div className="unit-info">
                        <span className="purchase-modal-camp-icon" aria-hidden title="Stronghold">🏰</span>
                        <div className="unit-details">
                          <span className="unit-name">Repair {s.name}</span>
                          <span className="unit-stats">HP: {target}/{s.baseHp}</span>
                        </div>
                      </div>
                      <div className="unit-cost">
                        <span className="cost-value">{strongholdRepairCost}P</span>
                      </div>
                      <div className="quantity-controls">
                        <button type="button" onClick={() => handleRepairTargetChange(s.territoryId, -1)} disabled={target <= s.currentHp}>−</button>
                        <span className="quantity">{target}</span>
                        <button type="button" onClick={() => handleRepairTargetChange(s.territoryId, 1)} disabled={!canAffordMore || atCap}>+</button>
                      </div>
                    </div>
                  );
                })
              : null}
            {campCost <= 0 && (!strongholdRepairCost || repairableStrongholds.length === 0) && (
              <p className="purchase-other-empty">No other purchases available.</p>
            )}
          </div>
        )}

        <footer className="modal-footer">
          <div className="purchase-summary">
            <span>
              {totalUnits} units
              {campQuantity > 0 && `, ${campQuantity} camp${campQuantity !== 1 ? 's' : ''}`}
              {repairCostTotal > 0 && `, stronghold repairs`}
              {' for '}{formatCost(totalCosts)}
            </span>
          </div>
          <div className="modal-actions">
            <button type="button" onClick={handleCancel}>Cancel</button>
            <button type="button" className="primary" onClick={handleConfirm}>Confirm</button>
          </div>
        </footer>
      </div>
    </div>
  );
}

export default PurchaseModal;

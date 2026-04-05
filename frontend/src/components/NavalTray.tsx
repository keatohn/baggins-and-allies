import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import {
  DndContext,
  useDraggable,
  useDroppable,
  PointerSensor,
  useSensor,
  useSensors,
} from '@dnd-kit/core';
import type { DragEndEvent, DragStartEvent } from '@dnd-kit/core';
import { CSS } from '@dnd-kit/utilities';
import type { CSSProperties, KeyboardEvent, MouseEvent, PointerEvent } from 'react';
import './NavalTray.css';

export interface BoatPassenger {
  unitId: string;
  name: string;
  icon: string;
  /** Present for confirmed passengers (from state); absent for pending loads. */
  instanceId?: string;
}

export interface BoatInTray {
  boatInstanceId: string;
  unitId: string;
  name: string;
  icon: string;
  passengers: BoatPassenger[];
  /** Max passengers this boat can carry (from unit definition). */
  transportCapacity: number;
}

export interface PendingLoadPassenger {
  instanceId: string;
  unitId: string;
  name: string;
  icon: string;
}

interface NavalTrayProps {
  isOpen: boolean;
  seaZoneId: string;
  seaZoneName: string;
  boats: BoatInTray[];
  factionColor?: string;
  onClose: () => void;
  /** When loading and multiple boats (different makeups), user picks boat from tray. Each option is [boatInstanceId, ...passengerInstanceIds]. */
  pendingLoadBoatOptions?: string[][];
  onChooseBoatForLoad?: (instanceIds: string[]) => void;
  /** Pending load: passengers being allocated (for drag between boats in tray). */
  pendingLoadPassengers?: PendingLoadPassenger[];
  /** Which boat each pending passenger is assigned to (boatInstanceId -> instanceIds[]). */
  loadAllocation?: Record<string, string[]>;
  onLoadAllocationChange?: (allocation: Record<string, string[]>) => void;
}

const TRAY_PASSENGER_PREFIX = 'naval-tray-passenger-';
const TRAY_BOAT_PREFIX = 'naval-tray-boat-';
const TAP_MOVE_THRESH_SQ = 100; // 10px, match GameMap tap-to-move (touch / coarse pointer only)

function useCoarsePointer(): boolean {
  return useSyncExternalStore(
    (onStoreChange) => {
      const mq = window.matchMedia('(pointer: coarse)');
      mq.addEventListener('change', onStoreChange);
      return () => mq.removeEventListener('change', onStoreChange);
    },
    () => window.matchMedia('(pointer: coarse)').matches,
    () => false,
  );
}

/** Fine pointer: same as pre–mobile-pass implementation (img + dnd-kit only). */
function DraggablePassengerIconFine({
  instanceId,
  name,
  icon,
}: {
  instanceId: string;
  name: string;
  icon: string;
}) {
  const { attributes, listeners, setNodeRef, transform, isDragging } = useDraggable({
    id: `${TRAY_PASSENGER_PREFIX}${instanceId}`,
    data: { type: 'naval-tray-passenger' as const, instanceId },
  });
  const style: CSSProperties = {
    transform: CSS.Translate.toString(transform),
    opacity: isDragging ? 0.6 : 1,
    cursor: isDragging ? 'grabbing' : 'grab',
  };
  return (
    <img
      ref={setNodeRef}
      src={icon}
      alt={name}
      className="naval-tray-passenger-icon naval-tray-passenger-icon--draggable"
      title={name}
      style={style}
      {...listeners}
      {...attributes}
      draggable={false}
    />
  );
}

/** Coarse pointer: tap-to-select + drag between boats. */
function DraggablePassengerIconCoarse({
  instanceId,
  name,
  icon,
  isTapSelected,
  onTapToggle,
}: {
  instanceId: string;
  name: string;
  icon: string;
  isTapSelected: boolean;
  onTapToggle: () => void;
}) {
  const tapStartRef = useRef<{ x: number; y: number } | null>(null);
  const { attributes, listeners, setNodeRef, transform, isDragging } = useDraggable({
    id: `${TRAY_PASSENGER_PREFIX}${instanceId}`,
    data: { type: 'naval-tray-passenger' as const, instanceId },
  });
  const style: CSSProperties = {
    transform: CSS.Translate.toString(transform),
    opacity: isDragging ? 0.6 : 1,
    cursor: isDragging ? 'grabbing' : 'grab',
    touchAction: 'none',
  };

  const rawListeners = listeners as Record<string, ((e: PointerEvent) => void) | undefined> | undefined;
  const mergedListeners = useMemo(
    () => ({
      ...rawListeners,
      onPointerDown: (e: PointerEvent) => {
        tapStartRef.current = { x: e.clientX, y: e.clientY };
        rawListeners?.onPointerDown?.(e);
      },
      onPointerUp: (e: PointerEvent) => {
        rawListeners?.onPointerUp?.(e);
        const s = tapStartRef.current;
        tapStartRef.current = null;
        if (!s) return;
        const dx = e.clientX - s.x;
        const dy = e.clientY - s.y;
        if (dx * dx + dy * dy >= TAP_MOVE_THRESH_SQ) return;
        onTapToggle();
      },
    }),
    [rawListeners, onTapToggle],
  );

  return (
    <div
      ref={setNodeRef}
      className={`naval-tray-passenger-draggable naval-tray-passenger-icon-wrap ${isTapSelected ? 'naval-tray-passenger-icon-wrap--selected' : ''}`}
      style={style}
      {...mergedListeners}
      {...attributes}
      title={name}
      role="button"
      tabIndex={0}
      onKeyDown={(e: KeyboardEvent) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onTapToggle();
        }
      }}
    >
      <img
        src={icon}
        alt={name}
        className="naval-tray-passenger-icon naval-tray-passenger-icon--draggable"
        draggable={false}
      />
    </div>
  );
}

function DraggablePassengerIcon(props: {
  instanceId: string;
  name: string;
  icon: string;
  coarsePointer: boolean;
  isTapSelected: boolean;
  onTapToggle: () => void;
}) {
  if (!props.coarsePointer) {
    return (
      <DraggablePassengerIconFine instanceId={props.instanceId} name={props.name} icon={props.icon} />
    );
  }
  return (
    <DraggablePassengerIconCoarse
      instanceId={props.instanceId}
      name={props.name}
      icon={props.icon}
      isTapSelected={props.isTapSelected}
      onTapToggle={props.onTapToggle}
    />
  );
}

function DroppableBoatCard({
  boat,
  factionColor,
  allocatedPassengers,
  isAllocationMode,
  coarsePointer,
  allocationTapPassengerId,
  onAssignPassengerToBoat,
  onPassengerTapToggle,
}: {
  boat: BoatInTray;
  factionColor: string;
  allocatedPassengers: PendingLoadPassenger[];
  isAllocationMode: boolean;
  coarsePointer: boolean;
  allocationTapPassengerId: string | null;
  onAssignPassengerToBoat: (targetBoatId: string) => void;
  onPassengerTapToggle: (instanceId: string) => void;
}) {
  const confirmedPassengers = boat.passengers.filter((p): p is BoatPassenger & { instanceId: string } => !!p.instanceId);

  const { setNodeRef, isOver } = useDroppable({
    id: `${TRAY_BOAT_PREFIX}${boat.boatInstanceId}`,
    data: { type: 'naval-tray-boat' as const, boatInstanceId: boat.boatInstanceId },
  });

  const boatCardContent = (
    <>
      <div className="naval-tray-passengers">
        {confirmedPassengers.map((p, i) => (
          <img
            key={`${boat.boatInstanceId}-confirmed-${p.instanceId}-${i}`}
            src={p.icon}
            alt={p.name}
            className="naval-tray-passenger-icon"
            title={p.name}
            draggable={false}
          />
        ))}
        {allocatedPassengers.map((p) => (
          <DraggablePassengerIcon
            key={p.instanceId}
            instanceId={p.instanceId}
            name={p.name}
            icon={p.icon}
            coarsePointer={coarsePointer}
            isTapSelected={coarsePointer && allocationTapPassengerId === p.instanceId}
            onTapToggle={() => onPassengerTapToggle(p.instanceId)}
          />
        ))}
      </div>
      <img
        src={boat.icon}
        alt={boat.name}
        className="naval-tray-boat-icon"
        title={boat.name}
        draggable={false}
      />
      <span className="naval-tray-boat-name">{boat.name}</span>
    </>
  );

  const className = [
    'naval-tray-boat-card',
    isAllocationMode && isOver ? 'naval-tray-boat-card--drop-over' : '',
    coarsePointer && isAllocationMode && allocationTapPassengerId ? 'naval-tray-boat-card--awaiting-tap' : '',
  ]
    .filter(Boolean)
    .join(' ');

  const handleBoatCardClick = (e: MouseEvent) => {
    if (!coarsePointer || !isAllocationMode || !allocationTapPassengerId) return;
    if ((e.target as HTMLElement).closest('.naval-tray-passenger-draggable')) return;
    e.preventDefault();
    e.stopPropagation();
    onAssignPassengerToBoat(boat.boatInstanceId);
  };

  const tapAssignActive = coarsePointer && isAllocationMode && allocationTapPassengerId;

  return (
    <div
      ref={setNodeRef}
      className={className}
      style={{ borderColor: factionColor }}
      onClick={handleBoatCardClick}
      role={tapAssignActive ? 'button' : undefined}
      tabIndex={tapAssignActive ? 0 : undefined}
      onKeyDown={
        tapAssignActive
          ? (e: KeyboardEvent) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                onAssignPassengerToBoat(boat.boatInstanceId);
              }
            }
          : undefined
      }
    >
      <div className="naval-tray-boat-card-inner naval-tray-boat-card-inner--no-drag">{boatCardContent}</div>
    </div>
  );
}

function DraggableBoatCard({
  boat,
  seaZoneId,
  factionColor,
  loadOption,
  onLoadIntoThisBoat,
}: {
  boat: BoatInTray;
  seaZoneId: string;
  factionColor: string;
  loadOption?: string[];
  onLoadIntoThisBoat?: () => void;
}) {
  const confirmedPassengers = boat.passengers.filter((p): p is BoatPassenger & { instanceId: string } => !!p.instanceId);
  const instanceIds = [boat.boatInstanceId, ...confirmedPassengers.map((p) => p.instanceId)];
  const passengerCount = confirmedPassengers.length;

  const { attributes, listeners, setNodeRef, transform, isDragging } = useDraggable({
    id: `naval-tray-${seaZoneId}-${boat.boatInstanceId}`,
    data: {
      unitId: boat.unitId,
      territoryId: seaZoneId,
      count: 1,
      unitDef: { name: boat.name, icon: boat.icon },
      instanceIds,
      passengerCount,
      factionColor,
    },
  });

  const style: CSSProperties = {
    transform: CSS.Translate.toString(transform),
    opacity: isDragging ? 0.5 : 1,
    cursor: isDragging ? 'grabbing' : 'grab',
  };

  return (
    <div
      ref={setNodeRef}
      className="naval-tray-boat-card"
      style={{ borderColor: factionColor, ...style }}
    >
      <div className="naval-tray-boat-card-inner" {...listeners} {...attributes}>
        <div className="naval-tray-passengers">
          {boat.passengers.map((p, i) => (
            <img
              key={`${boat.boatInstanceId}-${p.unitId}-${i}`}
              src={p.icon}
              alt={p.name}
              className="naval-tray-passenger-icon"
              title={p.name}
              draggable={false}
            />
          ))}
        </div>
        <img
          src={boat.icon}
          alt={boat.name}
          className="naval-tray-boat-icon"
          title={boat.name}
          draggable={false}
        />
        <span className="naval-tray-boat-name">{boat.name}</span>
      </div>
      {loadOption != null && onLoadIntoThisBoat && (
        <button
          type="button"
          className="naval-tray-load-btn"
          onClick={(e) => {
            e.stopPropagation();
            onLoadIntoThisBoat();
          }}
          title="Load into this boat"
        >
          Load into this boat
        </button>
      )}
    </div>
  );
}

function NavalTray({
  isOpen,
  seaZoneId,
  seaZoneName,
  boats,
  factionColor = '#1a4d8c',
  onClose,
  pendingLoadBoatOptions,
  onChooseBoatForLoad,
  pendingLoadPassengers = [],
  loadAllocation,
  onLoadAllocationChange,
}: NavalTrayProps) {
  const coarsePointer = useCoarsePointer();

  const isAllocationMode =
    Boolean(loadAllocation && Object.keys(loadAllocation).length > 0 && pendingLoadPassengers.length > 0) &&
    Boolean(onLoadAllocationChange);

  const [allocationTapPassengerId, setAllocationTapPassengerId] = useState<string | null>(null);

  useEffect(() => {
    if (!isOpen) setAllocationTapPassengerId(null);
  }, [isOpen]);

  useEffect(() => {
    if (!coarsePointer) setAllocationTapPassengerId(null);
  }, [coarsePointer]);

  const reassignPassengerToBoat = useCallback(
    (instanceId: string, targetBoatId: string) => {
      if (!loadAllocation || !onLoadAllocationChange) return;
      const currentBoatId = Object.keys(loadAllocation).find((bid) =>
        (loadAllocation[bid] ?? []).includes(instanceId),
      );
      if (currentBoatId === targetBoatId) return;

      const next: Record<string, string[]> = {};
      for (const [bid, ids] of Object.entries(loadAllocation)) {
        if (bid === currentBoatId) {
          const filtered = (ids ?? []).filter((id) => id !== instanceId);
          if (filtered.length > 0) next[bid] = filtered;
        } else if (bid === targetBoatId) {
          next[bid] = [...(ids ?? []), instanceId];
        } else {
          if ((ids ?? []).length > 0) next[bid] = ids;
        }
      }
      if (!next[targetBoatId]) next[targetBoatId] = [instanceId];
      for (const boat of boats) {
        const cap = boat.transportCapacity ?? 0;
        const confirmed = boat.passengers.filter((p): p is BoatPassenger & { instanceId: string } => !!p.instanceId).length;
        const alloc = (next[boat.boatInstanceId] ?? []).length;
        if (confirmed + alloc > cap) return;
      }
      onLoadAllocationChange(next);
    },
    [loadAllocation, onLoadAllocationChange, boats],
  );

  const handleDragEnd = useCallback(
    (event: DragEndEvent) => {
      const { active, over } = event;
      if (!over || !loadAllocation || !onLoadAllocationChange) return;
      const activeId = active.id as string;
      if (!activeId.startsWith(TRAY_PASSENGER_PREFIX)) return;
      const instanceId = activeId.slice(TRAY_PASSENGER_PREFIX.length);
      const overId = over.id as string;
      if (!overId.startsWith(TRAY_BOAT_PREFIX)) return;
      const targetBoatId = overId.slice(TRAY_BOAT_PREFIX.length);
      reassignPassengerToBoat(instanceId, targetBoatId);
    },
    [loadAllocation, onLoadAllocationChange, reassignPassengerToBoat],
  );

  const handleAllocationDragStart = useCallback((_event: DragStartEvent) => {
    setAllocationTapPassengerId(null);
  }, []);

  const onPassengerTapToggle = useCallback((instanceId: string) => {
    setAllocationTapPassengerId((prev) => (prev === instanceId ? null : instanceId));
  }, []);

  const onAssignPassengerToBoat = useCallback(
    (targetBoatId: string) => {
      if (!allocationTapPassengerId) return;
      reassignPassengerToBoat(allocationTapPassengerId, targetBoatId);
      setAllocationTapPassengerId(null);
    },
    [allocationTapPassengerId, reassignPassengerToBoat],
  );

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: coarsePointer ? 10 : 8 } }),
  );

  if (!isOpen) return null;

  const boatList = (
    <div className="naval-tray-boats">
      {boats.length === 0 ? (
        <div className="naval-tray-empty">No boats in this sea zone.</div>
      ) : isAllocationMode ? (
        boats.map((boat) => {
          const allocatedIds = loadAllocation?.[boat.boatInstanceId] ?? [];
          const allocatedPassengers = allocatedIds
            .map((id) => pendingLoadPassengers.find((p) => p.instanceId === id))
            .filter((p): p is PendingLoadPassenger => p != null);
          return (
            <DroppableBoatCard
              key={boat.boatInstanceId}
              boat={boat}
              factionColor={factionColor}
              allocatedPassengers={allocatedPassengers}
              isAllocationMode={true}
              coarsePointer={coarsePointer}
              allocationTapPassengerId={allocationTapPassengerId}
              onAssignPassengerToBoat={onAssignPassengerToBoat}
              onPassengerTapToggle={onPassengerTapToggle}
            />
          );
        })
      ) : (
        boats.map((boat) => {
          const loadOption = pendingLoadBoatOptions?.find((op) => op[0] === boat.boatInstanceId);
          return (
            <DraggableBoatCard
              key={boat.boatInstanceId}
              boat={boat}
              seaZoneId={seaZoneId}
              factionColor={factionColor}
              loadOption={loadOption}
              onLoadIntoThisBoat={
                loadOption != null && onChooseBoatForLoad ? () => onChooseBoatForLoad(loadOption) : undefined
              }
            />
          );
        })
      )}
    </div>
  );

  return (
    <div className="naval-tray" style={{ borderColor: factionColor }}>
      <div className="naval-tray-header">
        <span className="naval-tray-title">{seaZoneName}</span>
        <button
          type="button"
          className="naval-tray-close"
          onClick={onClose}
          title="Close naval tray"
          aria-label="Close"
        >
          ×
        </button>
      </div>
      {isAllocationMode && coarsePointer && (
        <p className="naval-tray-allocation-hint">
          Tap a unit, then tap a boat to assign. Or drag a unit onto a boat.
        </p>
      )}
      {isAllocationMode ? (
        <DndContext onDragStart={handleAllocationDragStart} onDragEnd={handleDragEnd} sensors={sensors}>
          {boatList}
        </DndContext>
      ) : (
        boatList
      )}
    </div>
  );
}

export default NavalTray;

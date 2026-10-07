import { useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import type { SetupInfo } from '../services/api';
import {
  AGES,
  assignLanes,
  buildTimeline,
  momentHeading,
  scaleTickFractions,
  type TimelineEvent,
} from '../scenarioTimeline';
import './ScenarioTimeline.css';

const TICKS = scaleTickFractions();

function ScenarioMetaLine({ scenario, showYear }: { scenario: SetupInfo; showYear: boolean }) {
  const ctx = scenario.context;
  const count = ctx?.faction_count ?? ctx?.factions?.length;
  const parts: ReactNode[] = [];
  const add = (key: string, node: ReactNode) => {
    if (parts.length > 0) parts.push(<span key={`${key}-sep`}> · </span>);
    parts.push(<span key={key}>{node}</span>);
  };
  if (showYear && ctx?.year) add('year', ctx.year);
  if (count != null) {
    add('count', `${count} ${count === 1 ? 'faction' : 'factions'}`);
  }
  if (ctx?.good_count != null && ctx?.evil_count != null) {
    add('sides', `${ctx.good_count}vs${ctx.evil_count}`);
  }
  if (ctx?.map) add('map', ctx.map);
  if (parts.length === 0) return null;
  return <span className="chronicle__option-meta">{parts}</span>;
}

function Frame({ image, age }: { image: string | null; age: string }) {
  const [failed, setFailed] = useState(false);
  if (image && !failed) {
    return (
      <span className="chronicle__frame">
        <img
          src={`/assets/scenarios/${encodeURIComponent(image)}`}
          alt=""
          onError={() => setFailed(true)}
        />
      </span>
    );
  }
  return (
    <span className="chronicle__frame">
      <span className="chronicle__monogram">{age}</span>
    </span>
  );
}

export default function ScenarioTimeline({
  scenarios,
  selectedId,
  onSelect,
}: {
  scenarios: SetupInfo[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const { events, undated } = useMemo(() => buildTimeline(scenarios), [scenarios]);
  const trackRef = useRef<HTMLDivElement>(null);
  const eventsRef = useRef(events);
  eventsRef.current = events;
  const [lanes, setLanes] = useState<number[]>(() => events.map(() => 0));
  const eventKey = events.map((event) => event.key).join('|');

  useLayoutEffect(() => {
    const el = trackRef.current;
    if (!el) return;
    const measure = () => {
      const markerWidth = parseFloat(getComputedStyle(el).getPropertyValue('--marker-w')) || 112;
      const next = assignLanes(
        eventsRef.current.map((event) => event.fraction),
        el.clientWidth,
        markerWidth,
      );
      setLanes((prev) => (prev.length === next.length && prev.every((lane, i) => lane === next[i]) ? prev : next));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [eventKey]);

  const maxLane = lanes.reduce((max, lane) => Math.max(max, lane), 0);
  const selectedEvent = events.find((event) => event.scenarios.some((scenario) => scenario.id === selectedId)) ?? null;

  const chooseMoment = (event: TimelineEvent) => {
    if (!event.scenarios.some((scenario) => scenario.id === selectedId) && event.scenarios[0]) {
      onSelect(event.scenarios[0].id);
    }
  };

  return (
    <div className="chronicle">
      <div className="chronicle__scale">
        <div className="chronicle__bands" aria-hidden="true">
          {AGES.map((age) => (
            <div
              key={age.id}
              className={`chronicle__band chronicle__band--${age.id}`}
              style={{ flexGrow: age.years, flexBasis: 0 }}
            />
          ))}
        </div>
        <div className="chronicle__head">
          {AGES.map((age) => (
            <div
              key={age.id}
              className="chronicle__age"
              style={{ flexGrow: age.years, flexBasis: 0 }}
            >
              <span className="chronicle__age-full">{age.name}</span>
              <span className="chronicle__age-short">{age.id}</span>
            </div>
          ))}
        </div>
        <div
          ref={trackRef}
          className="chronicle__track"
          style={{ ['--lanes' as string]: String(maxLane) }}
          role="radiogroup"
          aria-label="Scenario timeline"
        >
          <div className="chronicle__line" aria-hidden="true" />
          {TICKS.map((fraction) => (
            <span
              key={fraction}
              className="chronicle__tick"
              style={{ left: `${fraction * 100}%` }}
              aria-hidden="true"
            />
          ))}
          {events.map((event, index) => {
            const selected = event.scenarios.some((scenario) => scenario.id === selectedId);
            const count = event.scenarios.length;
            const names = event.scenarios.map((scenario) => scenario.display_name).join(', ');
            return (
              <div
                key={event.key}
                className={`chronicle__pin${selected ? ' chronicle__pin--selected' : ''}`}
                style={{
                  left: `${event.fraction * 100}%`,
                  ['--lane' as string]: String(lanes[index] ?? 0),
                }}
              >
                <button
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  className="chronicle__marker"
                  aria-label={count === 1 ? `${names}, ${event.label}` : `${event.label}, ${count} scenarios: ${names}`}
                  onClick={() => chooseMoment(event)}
                >
                  <span className="chronicle__frame-wrap">
                    <Frame key={event.image ?? event.age} image={event.image} age={event.age} />
                    {count > 1 && <span className="chronicle__count">{count}</span>}
                  </span>
                  <span className="chronicle__pin-year">{event.label}</span>
                </button>
                <span className="chronicle__stem" aria-hidden="true" />
                <span className="chronicle__dot" aria-hidden="true" />
              </div>
            );
          })}
        </div>
      </div>
      {selectedEvent && (
        <MomentChoices event={selectedEvent} selectedId={selectedId} onSelect={onSelect} />
      )}
      {undated.length > 0 && (
        <div className="chronicle__undated">
          <p className="chronicle__choose">Not placed on the timeline</p>
          <div className="chronicle__options" role="radiogroup" aria-label="Scenarios without a year">
            {undated.map((scenario) => (
              <ScenarioChoice
                key={scenario.id}
                scenario={scenario}
                selected={scenario.id === selectedId}
                showYear
                onSelect={onSelect}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function MomentChoices({
  event,
  selectedId,
  onSelect,
}: {
  event: TimelineEvent;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const years = new Set(event.scenarios.map((scenario) => scenario.context?.year?.trim()).filter(Boolean));
  const mixedYears = years.size > 1;
  return (
    <div className="chronicle__moment" id="scenario-timeline-choices">
      <p className="chronicle__moment-kicker">{momentHeading(event)}</p>
      {event.scenarios.length > 1 && <p className="chronicle__choose">Choose a scenario</p>}
      <div className="chronicle__options" role="radiogroup" aria-label={momentHeading(event)}>
        {event.scenarios.map((scenario) => (
          <ScenarioChoice
            key={scenario.id}
            scenario={scenario}
            selected={scenario.id === selectedId}
            showYear={mixedYears}
            onSelect={onSelect}
          />
        ))}
      </div>
    </div>
  );
}

function ScenarioChoice({
  scenario,
  selected,
  showYear,
  onSelect,
}: {
  scenario: SetupInfo;
  selected: boolean;
  showYear: boolean;
  onSelect: (id: string) => void;
}) {
  const factions = scenario.context?.factions;
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      className={`chronicle__option${selected ? ' chronicle__option--active' : ''}`}
      onClick={() => onSelect(scenario.id)}
    >
      <span className="chronicle__option-name">{scenario.display_name}</span>
      <ScenarioMetaLine scenario={scenario} showYear={showYear} />
      {Array.isArray(factions) && factions.length > 0 && (
        <span className="chronicle__option-factions">{factions.join(', ')}</span>
      )}
      {scenario.optional_rules?.map((rule) => (
        <span key={rule.type} className="chronicle__option-mode">Special Mode: {rule.name}</span>
      ))}
    </button>
  );
}

import type { SetupInfo } from './services/api';

export const AGES = [
  { id: 'FA', name: 'First Age', years: 590 },
  { id: 'SA', name: 'Second Age', years: 3441 },
  { id: 'TA', name: 'Third Age', years: 3021 },
] as const;

export type AgeId = (typeof AGES)[number]['id'];

export const TOTAL_YEARS = AGES.reduce((sum, age) => sum + age.years, 0);

export type ParsedYear = {
  age: AgeId;
  start: number;
  end: number;
};

const YEAR_RE =
  /^(?:c\.?\s*|circa\s+)?(F\.?\s*A\.?|S\.?\s*A\.?|T\.?\s*A\.?)\s*(\d+)\s*(?:[-–—]\s*(\d+))?\s*$/i;

export function parseScenarioYear(raw: string | undefined | null): ParsedYear | null {
  if (!raw || typeof raw !== 'string') return null;
  const match = raw.trim().replace(/\s+/g, ' ').match(YEAR_RE);
  if (!match) return null;
  const ageToken = match[1].replace(/[\s.]/g, '').toUpperCase();
  if (ageToken !== 'FA' && ageToken !== 'SA' && ageToken !== 'TA') return null;
  const start = Number(match[2]);
  const end = match[3] != null ? Number(match[3]) : start;
  if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end < 0) return null;
  return { age: ageToken, start, end };
}

export function yearFraction(age: AgeId, year: number): number {
  let before = 0;
  for (const span of AGES) {
    if (span.id === age) {
      const clamped = Math.min(span.years, Math.max(0, year));
      return (before + clamped) / TOTAL_YEARS;
    }
    before += span.years;
  }
  return 0;
}

export function scenarioImageFile(raw: string | undefined | null): string | null {
  if (!raw || typeof raw !== 'string') return null;
  const name = raw.trim();
  if (!name || /[\\/]/.test(name) || name === '.' || name === '..') return null;
  if (!/\.(png|jpe?g|webp|gif)$/i.test(name)) return null;
  return name;
}

export function ageName(id: AgeId): string {
  return AGES.find((age) => age.id === id)?.name ?? id;
}

export function canonicalYearLabel(age: AgeId, start: number, end: number): string {
  return start === end ? `${age} ${start}` : `${age} ${start}–${end}`;
}

export type TimelineEvent = {
  key: string;
  fraction: number;
  age: AgeId;
  start: number;
  end: number;
  label: string;
  image: string | null;
  scenarios: SetupInfo[];
};

function yearText(scenario: SetupInfo): string {
  const year = scenario.context?.year;
  return typeof year === 'string' ? year.trim().replace(/\s+/g, ' ') : '';
}

export function buildTimeline(scenarios: SetupInfo[]): { events: TimelineEvent[]; undated: SetupInfo[] } {
  const undated: SetupInfo[] = [];
  const order: string[] = [];
  const buckets = new Map<string, TimelineEvent>();

  for (const scenario of scenarios) {
    const parsed = parseScenarioYear(scenario.context?.year);
    if (!parsed) {
      undated.push(scenario);
      continue;
    }
    const image = scenarioImageFile(scenario.timeline_image);
    const key = `${parsed.age}:${parsed.start}:${parsed.end}:${image ? image.toLowerCase() : ''}`;
    let event = buckets.get(key);
    if (!event) {
      event = {
        key,
        fraction: yearFraction(parsed.age, parsed.start),
        age: parsed.age,
        start: parsed.start,
        end: parsed.end,
        label: canonicalYearLabel(parsed.age, parsed.start, parsed.end),
        image,
        scenarios: [],
      };
      buckets.set(key, event);
      order.push(key);
    }
    event.scenarios.push(scenario);
  }

  for (const event of buckets.values()) {
    const labels = new Set(event.scenarios.map(yearText).filter(Boolean));
    event.label = labels.size === 1 ? [...labels][0] : canonicalYearLabel(event.age, event.start, event.end);
  }

  const events = order.map((key) => buckets.get(key)!);
  events.sort((a, b) => a.fraction - b.fraction || order.indexOf(a.key) - order.indexOf(b.key));
  return { events, undated };
}

export function momentHeading(event: TimelineEvent): string {
  const rest = event.label.replace(
    /^(?:c\.?\s*|circa\s+)?(?:F\.?\s*A\.?|S\.?\s*A\.?|T\.?\s*A\.?)\s*/i,
    '',
  );
  return `${ageName(event.age)} · ${rest || event.label}`;
}

export function scaleTickFractions(): number[] {
  const boundaries: number[] = [0];
  let at = 0;
  for (const age of AGES) {
    at += age.years;
    boundaries.push(at);
  }
  const ticks: number[] = [];
  for (let year = 500; year < TOTAL_YEARS; year += 500) {
    if (boundaries.some((boundary) => Math.abs(year - boundary) < 120)) continue;
    ticks.push(year / TOTAL_YEARS);
  }
  return ticks;
}

export function assignLanes(fractions: number[], width: number, markerWidth: number): number[] {
  const lanes = fractions.map(() => 0);
  if (width <= 0 || markerWidth <= 0) return lanes;
  const items = fractions
    .map((fraction, index) => ({ index, x: fraction * width }))
    .sort((a, b) => a.x - b.x || a.index - b.index);
  const laneRight: number[] = [];
  const gap = 12;
  for (const item of items) {
    const left = item.x - markerWidth / 2;
    let lane = 0;
    while (lane < laneRight.length && laneRight[lane] + gap > left) lane += 1;
    lanes[item.index] = lane;
    laneRight[lane] = item.x + markerWidth / 2;
  }
  return lanes;
}

/**
 * Parse territories_test.csv into a graph { nodes, links } for topology visualization.
 * Handles quoted CSV fields; deduplicates links (A-B and B-A become one).
 */

export interface GraphNode {
  id: string;
  name: string;
}

export interface GraphLink {
  source: string;
  target: string;
}

export interface ForceGraphData {
  nodes: GraphNode[];
  links: GraphLink[];
}

/**
 * Parse a single CSV line respecting double-quoted fields (which may contain commas).
 */
function parseCsvLine(line: string): string[] {
  const fields: string[] = [];
  let i = 0;
  while (i < line.length) {
    if (line[i] === '"') {
      i += 1;
      let end = i;
      while (end < line.length && line[end] !== '"') end++;
      fields.push(line.slice(i, end).trim());
      i = end + 1;
      if (line[i] === ',') i += 1;
      continue;
    }
    let end = i;
    while (end < line.length && line[end] !== ',') end++;
    fields.push(line.slice(i, end).trim());
    i = end + 1;
  }
  return fields;
}

/**
 * Parse comma-separated list of territory ids (from adjacent_territory column).
 * Handles spaces after commas.
 */
function parseAdjacentList(text: string): string[] {
  if (!text || !text.trim()) return [];
  return text.split(',').map(s => s.trim()).filter(Boolean);
}

/**
 * Normalize link so (a, b) and (b, a) compare equal for deduping.
 */
function linkKey(a: string, b: string): string {
  return a < b ? `${a}|${b}` : `${b}|${a}`;
}

/**
 * Parse raw CSV text into { nodes, links } for react-force-graph-2d.
 * - Nodes: one per unique territory id, name = id with underscores → spaces
 * - Links: unique undirected edges (no duplicate A-B / B-A)
 */
export function parseTerritoriesCsvToGraph(csvText: string): ForceGraphData {
  const lines = csvText.split(/\r?\n/).map(l => l.trim()).filter(l => l && !l.startsWith('territory,'));
  const nodeIds = new Set<string>();
  const linkKeys = new Set<string>();
  const links: GraphLink[] = [];

  for (const line of lines) {
    const fields = parseCsvLine(line);
    const territory = fields[0]?.trim();
    const adjacentStr = fields[1] ?? '';
    if (!territory) continue;

    nodeIds.add(territory);
    const adjacents = parseAdjacentList(adjacentStr);
    for (const adj of adjacents) {
      nodeIds.add(adj);
      const key = linkKey(territory, adj);
      if (!linkKeys.has(key)) {
        linkKeys.add(key);
        links.push({ source: territory, target: adj });
      }
    }
  }

  const nodes: GraphNode[] = Array.from(nodeIds).map(id => ({
    id,
    name: id.replace(/_/g, ' '),
  }));

  return { nodes, links };
}

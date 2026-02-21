import { useState, useEffect, useRef, useCallback } from 'react';
import ForceGraph2D from 'react-force-graph-2d';
import {
  parseTerritoriesCsvToGraph,
  type ForceGraphData,
  type GraphNode,
  type GraphLink,
} from './parseTerritoriesCsv';

const CSV_URL = '/territories_test.csv';
const BACKGROUND = '#e8e0d5';
const LINK_COLOR = '#5c4033';

export interface MapGraphDebugProps {
  /** CSV URL (default: /territories_test.csv — put file in frontend/public). */
  csvUrl?: string;
  /** Optional: pass CSV text directly instead of fetching. */
  csvText?: string;
}

export default function MapGraphDebug({ csvUrl = CSV_URL, csvText }: MapGraphDebugProps) {
  const [data, setData] = useState<ForceGraphData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [frozen, setFrozen] = useState(false);
  const graphRef = useRef<ReturnType<typeof ForceGraph2D> | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    if (csvText !== undefined && csvText !== '') {
      try {
        const parsed = parseTerritoriesCsvToGraph(csvText);
        if (!cancelled) setData(parsed);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to parse CSV');
      }
      if (!cancelled) setLoading(false);
      return;
    }

    fetch(csvUrl)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}: ${r.statusText}`);
        return r.text();
      })
      .then((text) => {
        if (cancelled) return;
        const parsed = parseTerritoriesCsvToGraph(text);
        setData(parsed);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load CSV');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [csvUrl, csvText]);

  const handleEngineStop = useCallback(() => {
    setFrozen(true);
  }, []);

  if (loading) {
    return (
      <div style={{ padding: 24, background: BACKGROUND, minHeight: 400, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        Loading CSV…
      </div>
    );
  }
  if (error) {
    return (
      <div style={{ padding: 24, background: BACKGROUND, minHeight: 400 }}>
        <p style={{ color: '#c00' }}>{error}</p>
        <p style={{ fontSize: 14, color: '#666' }}>
          Put <code>territories_test.csv</code> in <code>frontend/public/</code> or pass <code>csvText</code>.
        </p>
      </div>
    );
  }
  if (!data || data.nodes.length === 0) {
    return (
      <div style={{ padding: 24, background: BACKGROUND, minHeight: 400 }}>
        No graph data (empty or invalid CSV).
      </div>
    );
  }

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%', minHeight: 600, background: BACKGROUND }}>
      <ForceGraph2D
        ref={graphRef}
        graphData={data}
        nodeLabel={(node: GraphNode) => node.name}
        linkColor={() => LINK_COLOR}
        onEngineStop={handleEngineStop}
        nodeCanvasObject={(node, ctx, globalScale) => {
          const x = node.x ?? 0;
          const y = node.y ?? 0;
          const label = (node as GraphNode).name;
          // Node circle
          ctx.beginPath();
          ctx.arc(x, y, 4, 0, 2 * Math.PI);
          ctx.fillStyle = '#5c4033';
          ctx.fill();
          ctx.strokeStyle = '#2c1810';
          ctx.lineWidth = 1 / globalScale;
          ctx.stroke();
          // Label
          const fontSize = Math.max(10, 12 / globalScale);
          ctx.font = `${fontSize}px Sans-Serif`;
          ctx.textAlign = 'center';
          ctx.textBaseline = 'middle';
          ctx.fillStyle = '#2c1810';
          ctx.fillText(label, x, y + 14);
        }}
      />
      {frozen && (
        <div style={{ position: 'absolute', top: 8, left: 8, fontSize: 12, color: '#666' }}>
          Layout frozen
        </div>
      )}
    </div>
  );
}

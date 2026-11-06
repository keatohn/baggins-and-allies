import { useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { api } from '../../services/api';
import {
  getMapTextureOpacity,
  setMapTextureOpacity,
  subscribeMapTextureOpacity,
} from '../../mapTexture';

/** Global parchment-grain strength. Dragging updates the map immediately and saves. */
export function MapTextureSlider() {
  const opacity = useSyncExternalStore(subscribeMapTextureOpacity, getMapTextureOpacity, getMapTextureOpacity);
  const [error, setError] = useState<string | null>(null);
  const touched = useRef(false);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    void api.getMapTexture().then((r) => {
      if (!cancelled && !touched.current) setMapTextureOpacity(r.opacity);
    }).catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => () => {
    if (timer.current != null) window.clearTimeout(timer.current);
  }, []);

  const commit = (next: number) => {
    touched.current = true;
    setError(null);
    setMapTextureOpacity(next);
    if (timer.current != null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      timer.current = null;
      void api.adminPutMapTexture(next).catch(() => setError('Could not save'));
    }, 250);
  };

  return (
    <label className="admin-graph__prop" title="Parchment grain over land on every map">
      Parchment
      <input
        type="range"
        min={0}
        max={100}
        step={1}
        className="admin-graph__slider"
        value={Math.round(opacity * 100)}
        onChange={(e) => commit(Number(e.target.value) / 100)}
        aria-label="Parchment filter strength"
      />
      <span className="admin-formulas__slider-value">{Math.round(opacity * 100)}%</span>
      {error ? <span className="admin-page__error">{error}</span> : null}
    </label>
  );
}

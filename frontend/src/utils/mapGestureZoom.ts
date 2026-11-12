/** Safari uses native gesture events for trackpad pinch. Keep them within the map. */
export function listenForMapPinch(element: HTMLElement, zoom: (factor: number, x: number, y: number) => void) {
  let previousScale = 1;
  const handle = (event: Event) => {
    event.preventDefault();
    event.stopPropagation();
    const gesture = event as Event & { scale?: number; clientX?: number; clientY?: number };
    if (event.type !== 'gesturechange') { previousScale = 1; return; }
    const scale = gesture.scale;
    if (!scale || !Number.isFinite(scale) || scale < 0) return;
    const rect = element.getBoundingClientRect();
    zoom(scale / previousScale, gesture.clientX ?? rect.left + rect.width / 2, gesture.clientY ?? rect.top + rect.height / 2);
    previousScale = scale;
  };
  const events = ['gesturestart', 'gesturechange', 'gestureend'];
  for (const type of events) element.addEventListener(type, handle, { passive: false });
  return () => { for (const type of events) element.removeEventListener(type, handle); };
}

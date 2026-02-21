import { createRoot } from 'react-dom/client';
import MapGraphDebug from './MapGraphDebug';
import csvText from './territories_test.csv?raw';

const root = document.getElementById('root');
if (root) {
  createRoot(root).render(
    <div style={{ width: '100vw', height: '100vh' }}>
      <MapGraphDebug csvText={csvText} />
    </div>,
  );
}

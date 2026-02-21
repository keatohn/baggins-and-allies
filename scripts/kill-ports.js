#!/usr/bin/env node
/**
 * Kill any process listening on backend (8000) and frontend (5173) so you can restart clean.
 * Usage: node scripts/kill-ports.js   or  npm run kill-ports
 */
const { execSync } = require('child_process');
const ports = [8000, 5173];

function killPort(port) {
  try {
    // macOS/Linux: lsof -ti :PORT
    const pids = execSync(`lsof -ti :${port}`, { encoding: 'utf8' }).trim();
    if (pids) {
      pids.split(/\s+/).forEach((pid) => {
        try {
          process.kill(parseInt(pid, 10), 'SIGKILL');
          console.log(`Killed process ${pid} on port ${port}`);
        } catch (_) {}
      });
    }
  } catch (_) {
    // lsof exits 1 when nothing found
  }
}

ports.forEach(killPort);
console.log('Ports 8000 and 5173 cleared.');

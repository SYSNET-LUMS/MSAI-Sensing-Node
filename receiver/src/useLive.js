import { useEffect, useState } from 'react';

// Serial polling avoids overlapping requests; a failed request never leaves
// stale measurements falsely labelled live. Cleanup also aborts in-flight work.
export function useLive(url, intervalMs = 1000) {
  const [state, setState] = useState({ nodes: {}, connected: false, error: '', updatedAt: null });
  useEffect(() => {
    let stopped = false;
    let timer;
    let controller;
    const tick = async () => {
      controller = new AbortController();
      const deadline = setTimeout(() => controller.abort(), 3500);
      try {
        const response = await fetch(url, { cache: 'no-store', signal: controller.signal });
        if (!response.ok) throw new Error(`Bridge HTTP ${response.status}`);
        const body = await response.json();
        if (!body.nodes || typeof body.nodes !== 'object') throw new Error('Unexpected bridge response');
        if (!stopped) setState({ nodes: body.nodes, receiver: body.receiver, connected: true, error: '', updatedAt: new Date() });
      } catch (error) {
        if (!stopped) setState(previous => ({
          ...previous, connected: false,
          error: error.name === 'AbortError' ? 'Bridge request timed out' : error.message,
          nodes: Object.fromEntries(Object.entries(previous.nodes).map(([id, node]) =>
            [id, { ...node, online: false, connected: false, readings: {} }])),
        }));
      } finally {
        clearTimeout(deadline);
        if (!stopped) timer = setTimeout(tick, intervalMs);
      }
    };
    tick();
    return () => { stopped = true; clearTimeout(timer); controller?.abort(); };
  }, [url, intervalMs]);
  return state;
}

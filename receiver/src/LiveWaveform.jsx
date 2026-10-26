import { useEffect, useRef, useState } from 'react';

const COLORS = ['#2F44F0', '#D46A00', '#10815E', '#B72F79', '#2F44F0', '#D46A00', '#10815E', '#67728C'];

export default function LiveWaveform({ nodeId, channels, windowMs, scale, center, enabled, baseUrl, paused = false, resetKey = '' }) {
  const canvas = useRef(null);
  const [frame, setFrame] = useState(null);
  const [error, setError] = useState('');
  const [size, setSize] = useState({ width: 320, height: 120 });

  useEffect(() => {
    if (!enabled) return undefined;
    let stopped = false;
    let timer;
    let controller;
    const tick = async () => {
      controller = new AbortController();
      const deadline = setTimeout(() => controller.abort(), 2000);
      try {
        const query = new URLSearchParams({ channels, window_ms: String(windowMs), max_points: '700', center: String(center) });
        const response = await fetch(`${baseUrl}/${encodeURIComponent(nodeId)}?${query}`, { cache: 'no-store', signal: controller.signal });
        if (!response.ok) throw new Error(`Waveform HTTP ${response.status}`);
        const body = await response.json();
        if (!body.channels || typeof body.channels !== 'object') throw new Error('Invalid waveform response');
        if (!stopped) { setFrame(body); setError(''); }
      } catch (reason) {
        if (!stopped) { setFrame(null); setError(reason.name === 'AbortError' ? 'Waveform request timed out' : reason.message); }
      } finally {
        clearTimeout(deadline);
        if (!stopped) timer = setTimeout(tick, 200);
      }
    };
    tick();
    return () => { stopped = true; clearTimeout(timer); controller?.abort(); };
  }, [nodeId, channels, windowMs, center, enabled, baseUrl, resetKey]);

  useEffect(() => {
    const element = canvas.current;
    if (!element) return undefined;
    const observer = new ResizeObserver(entries => {
      const box = entries[0].contentRect;
      setSize({ width: box.width, height: box.height });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const element = canvas.current;
    if (!element) return;
    const ratio = window.devicePixelRatio || 1;
    const width = Math.max(1, size.width), height = Math.max(1, size.height);
    element.width = Math.round(width * ratio); element.height = Math.round(height * ratio);
    const ctx = element.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    if (!enabled || !frame?.online) return;
    const series = Object.entries(frame.channels).filter(([, values]) => values.length);
    if (!series.length) return;
    const mixedUnits = new Set(series.map(([ch]) => frame.units[ch])).size > 1;
    if (mixedUnits) return;
    const padding = { left: 52, right: 9, top: 22, bottom: 23 };
    const plotW = Math.max(1, width - padding.left - padding.right);
    const plotH = Math.max(1, height - padding.top - padding.bottom);
    let limit = Number(scale) || 0;
    if (!limit) {
      for (const [, values] of series) for (const [, y] of values) limit = Math.max(limit, Math.abs(y));
      limit = Math.max(1e-9, limit * 1.08);
    }
    ctx.font = '10px system-ui'; ctx.fillStyle = '#59647D';
    const unit = mixedUnits ? 'mixed units' : frame.units[series[0][0]];
    const format = value => Math.abs(value) >= 10000 || (Math.abs(value) < 0.01 && value !== 0) ? value.toExponential(1) : value.toFixed(2);
    ctx.fillText(`±${format(limit)} ${unit}`, 7, 12);
    ctx.fillText('0', 7, padding.top + plotH / 2 + 3);
    ctx.fillText(`−${windowMs} ms`, padding.left, height - 6);
    ctx.fillText('now', Math.max(padding.left, width - 30), height - 6);
    ctx.strokeStyle = '#CAD0E1'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(padding.left, padding.top + plotH / 2); ctx.lineTo(width - padding.right, padding.top + plotH / 2); ctx.stroke();
    ctx.save(); ctx.beginPath(); ctx.rect(padding.left, padding.top, plotW, plotH); ctx.clip();
    for (const [ch, values] of series) {
      ctx.strokeStyle = COLORS[Number(ch)]; ctx.lineWidth = 1.4; ctx.beginPath();
      values.forEach(([x, y], index) => {
        const px = padding.left + (x + windowMs) / windowMs * plotW;
        const py = padding.top + plotH / 2 - y / limit * plotH / 2;
        if (index === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      });
      ctx.stroke();
    }
    ctx.restore();
  }, [frame, size, windowMs, scale, enabled]);

  const mixedUnits = new Set(Object.values(frame?.units || {})).size > 1;
  const hasSamples = !mixedUnits && enabled && frame?.online && Object.values(frame.channels || {}).some(values => values.length);
  return <>
    <div className="plot live-plot">
      <canvas ref={canvas} aria-label={`Measured waveform for ${nodeId}, channels ${channels}`} role="img" data-samples={hasSamples ? frame.source_samples : 0} />
      {!hasSamples && <div className="plot-empty"><div className="empty-wave">∿</div><strong>{paused ? 'Display paused' : mixedUnits ? 'Channels have different units' : error || 'Waiting for data'}</strong><span>{paused ? 'Raw collection continues' : mixedUnits ? 'Select one channel or calibrate all selected channels' : enabled ? 'Receiving waveform preview' : 'No fresh node samples'}</span></div>}
    </div>
    <div className="wave-legend">
      {channels.split(',').map(ch => <span key={ch}><i style={{ background: COLORS[Number(ch)] }} />CH{ch}{frame?.units?.[ch] ? ` · ${frame.units[ch]}` : ''}</span>)}
      <span>{hasSamples ? `${frame.source_samples.toLocaleString()} samples · preview` : 'No samples'}</span>
    </div>
  </>;
}

import { useEffect, useRef, useState } from 'react';

function Spectrum({ nodeId, channel, windowMs, enabled, range, paused }) {
  const canvas = useRef(null);
  const [frame, setFrame] = useState(null);
  const [error, setError] = useState('');
  const [size, setSize] = useState({ width: 600, height: 220 });
  useEffect(() => {
    if (!enabled) return undefined;
    let stopped = false, timer, controller;
    const tick = async () => {
      const started = performance.now();
      let failed = false;
      controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 1500);
      try {
        const response = await fetch(`/api/fft/${encodeURIComponent(nodeId)}?channel=${channel}&window_ms=${windowMs}`, { cache: 'no-store', signal: controller.signal });
        if (!response.ok) throw new Error(`FFT HTTP ${response.status}`);
        const body = await response.json();
        if (!Array.isArray(body.amplitudes) || !Array.isArray(body.frequencies)) throw new Error('Invalid FFT response');
        if (!stopped) { setFrame(body); setError(''); }
      } catch (reason) {
        failed = true;
        if (!stopped) { setFrame(null); setError(reason.name === 'AbortError' ? 'FFT request timed out' : reason.message); }
      } finally {
        clearTimeout(timeout);
        // Serial requests, at most 20 updates/s; never accumulate a request backlog.
        if (!stopped) timer = setTimeout(tick, Math.max(0, (failed ? 250 : 50) - (performance.now() - started)));
      }
    };
    tick();
    return () => { stopped = true; clearTimeout(timer); controller?.abort(); };
  }, [nodeId, channel, windowMs, enabled]);
  useEffect(() => {
    const observer = new ResizeObserver(([entry]) => setSize({ width: entry.contentRect.width, height: entry.contentRect.height }));
    observer.observe(canvas.current);
    return () => observer.disconnect();
  }, []);
  const ready = enabled && frame?.online && frame.amplitudes.length > 0;
  const hasReference = ready && Number.isFinite(frame.peak_amplitude) && frame.peak_amplitude > 0;
  useEffect(() => {
    const el = canvas.current, ctx = el.getContext('2d');
    if (!ctx) return;
    const ratio = window.devicePixelRatio || 1, { width, height } = size;
    el.width = Math.max(1, Math.round(width * ratio)); el.height = Math.max(1, Math.round(height * ratio));
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, width, height);
    if (!hasReference) return;
    const maxHz = Math.min(Number(range) || frame.rate / 2, frame.rate / 2);
    const left = 48, top = 24, bottom = height - 50, plotW = Math.max(1, width - left - 16), plotH = Math.max(1, bottom - top);
    const count = frame.frequencies.filter(f => f <= maxHz).length;
    // Amplitude ratios use 20 log10. Keep the full-spectrum reference when
    // zooming, so changing the visible frequency range never changes dBc.
    const floorDb = -100;
    const toDbc = amplitude => amplitude > 0
      ? Math.max(floorDb, Math.min(0, 20 * Math.log10(amplitude / frame.peak_amplitude)))
      : floorDb;
    ctx.font = '11px system-ui'; ctx.fillStyle = '#59647D';
    ctx.fillText('dBc', 8, 13);
    for (let i = 0; i <= 5; i++) {
      const y = top + plotH * i / 5;
      ctx.fillText(`${-20 * i}`, 8, y + 3);
      ctx.strokeStyle = '#E1E5EF'; ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(left + plotW, y); ctx.stroke();
    }
    for (let i = 0; i <= 4; i++) {
      const x = left + plotW * i / 4;
      ctx.textAlign = i === 0 ? 'left' : i === 4 ? 'right' : 'center'; ctx.fillText(`${Math.round(maxHz * i / 4)} Hz`, x, bottom + 16); ctx.textAlign = 'left';
    }
    ctx.strokeStyle = '#2F44F0'; ctx.lineWidth = 1.5; ctx.beginPath();
    let started = false;
    for (let i = 0; i < count; i++) {
      if (frame.frequencies[i] <= 0) continue;
      const x = left + frame.frequencies[i] / maxHz * plotW;
      const y = top + toDbc(frame.amplitudes[i]) / floorDb * plotH;
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    }
    ctx.stroke();
    if (frame.peak_hz <= maxHz) {
      const x = left + frame.peak_hz / maxHz * plotW;
      ctx.strokeStyle = '#A66A00'; ctx.lineWidth = 1; ctx.setLineDash([4, 3]);
      ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom + 21); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = '#A66A00'; ctx.beginPath(); ctx.arc(x, top, 3, 0, Math.PI * 2); ctx.fill();
      const label = `Main ${frame.peak_hz.toFixed(1)} Hz`, labelWidth = ctx.measureText(label).width + 12;
      const labelX = Math.max(left, Math.min(x - labelWidth / 2, left + plotW - labelWidth));
      ctx.fillStyle = '#FFF1CF'; ctx.fillRect(labelX, bottom + 25, labelWidth, 20);
      ctx.fillStyle = '#704500'; ctx.fillText(label, labelX + 6, bottom + 39);
    }
  }, [frame, hasReference, size, range]);
  return <>
    <div className="plot">
      <canvas ref={canvas} role="img" aria-label={`Live FFT in dBc versus frequency in Hz for ${nodeId}, channel ${channel}${hasReference ? `; main frequency ${frame.peak_hz.toFixed(1)} Hz at 0 dBc` : ''}`} data-samples={ready ? frame.source_samples : 0} />
      {!ready && <div className="plot-empty"><strong>{paused ? 'Display paused' : error || (enabled ? 'Filling FFT window…' : 'No fresh node samples')}</strong><span>{paused ? 'Raw collection continues' : `${windowMs} ms contiguous samples required`}</span></div>}
      {ready && !hasReference && <div className="plot-empty"><strong>No non-DC signal reference</strong><span>dBc requires a nonzero reference peak</span></div>}
    </div>
    <div className="fft-detail">
      <span>{ready ? `${frame.source_samples} samples · ${frame.window_ms} ms · ${frame.bin_hz.toFixed(1)} Hz/bin` : 'Waiting for a complete live window'}</span>
      {hasReference && <span>Main frequency: {frame.peak_hz.toFixed(1)} Hz · 0 dBc · {frame.peak_amplitude.toPrecision(4)} {frame.unit} peak{Number(range) > 0 && frame.peak_hz > Number(range) ? ' · outside selected range' : ''}</span>}
    </div>
  </>;
}

export default function LiveFFT({ nodeId, enabled, paused = false, resetKey = '' }) {
  const [channel, setChannel] = useState('0');
  const [windowMs, setWindowMs] = useState(100);
  const [range, setRange] = useState('500');
  return <section className="fft-card" aria-label={`Live FFT ${nodeId}`}>
    <h2 className="fft-heading">FFT <span>{Number(channel)<4?'Current':Number(channel)<7?'Voltage':'Auxiliary'}</span></h2>
    <div className="fft-content" id={`fft-${nodeId}`}>
      <div className="fft-controls">
        <label>Channel<select value={channel} onChange={e => setChannel(e.target.value)}>
          {Array.from({ length: 8 }, (_, ch) => <option key={ch} value={ch}>CH{ch} · {ch < 4 ? 'Current' : ch < 7 ? 'Voltage' : 'Auxiliary'}</option>)}
        </select></label>
        <label>Window<select value={windowMs} onChange={e => setWindowMs(Number(e.target.value))}>
          {[50, 100, 200, 500, 1000].map(ms => <option key={ms} value={ms}>{ms} ms · {1000 / ms} Hz/bin</option>)}
        </select></label>
        <label>Frequency range<select value={range} onChange={e => setRange(e.target.value)}>
          <option value="200">0–200 Hz</option><option value="500">0–500 Hz</option><option value="1000">0–1,000 Hz</option><option value="0">Full bandwidth</option>
        </select></label>
      </div>
    <Spectrum key={`${nodeId}:${channel}:${windowMs}:${enabled}:${resetKey}`} {...{ nodeId, channel, windowMs, enabled, range, paused }} />
    </div>
  </section>;
}

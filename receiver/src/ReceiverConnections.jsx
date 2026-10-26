import { useEffect, useRef, useState } from 'react';

const baseState = base => ({
  opening: 'Opening USB port', waiting: 'Waiting for the base station',
  scanning: 'Scanning for a node', connecting: 'Connecting to a node',
  receiving: 'Receiving data', streaming: 'Receiving data',
  disconnected: 'Disconnected', error: 'Connection error',
})[String(base.state || '').toLowerCase()] || base.state || 'Waiting for status';

export default function ReceiverConnections({ receiver, available }) {
  const [mode, setMode] = useState('wifi');
  const [ports, setPorts] = useState([]);
  const [port, setPort] = useState('');
  const [refresh, setRefresh] = useState(0);
  const [completedScan, setCompletedScan] = useState('');
  const [portsError, setPortsError] = useState('');
  const [serialAvailable, setSerialAvailable] = useState(true);
  const [pending, setPending] = useState('');
  const [actionError, setActionError] = useState('');
  const [message, setMessage] = useState('');
  const requests = useRef(new Set());
  const bases = receiver?.ble_bases || [];
  const attached = bases.some(base => base.port === port);
  const scan = `${refresh}:${available}`;
  const loading = mode === 'ble' && available && completedScan !== scan;

  useEffect(() => {
    const activeRequests = requests.current;
    return () => {
      for (const controller of activeRequests) controller.abort();
      activeRequests.clear();
    };
  }, []);

  useEffect(() => {
    if (mode !== 'ble' || !available) return;
    const controller = new AbortController();
    const deadline = setTimeout(() => controller.abort(), 10000);
    let active = true;
    (async () => {
      try {
        const response = await fetch('/api/serial-ports', { cache: 'no-store', signal: controller.signal });
        const body = await response.json();
        if (!response.ok) throw new Error(body.detail || body.error || `Port discovery failed (${response.status})`);
        if (!Array.isArray(body.ports)) throw new Error('The receiver returned an invalid port list.');
        if (!active) return;
        setSerialAvailable(body.available !== false);
        setPorts(body.ports);
        setPort(current => body.ports.some(item => item.port === current) ? current : '');
        setPortsError(body.error || (body.available === false ? 'USB serial support is unavailable on the receiver.' : ''));
      } catch (error) {
        if (active) {
          setPorts([]);
          setPort('');
          setPortsError(error.name === 'AbortError' ? 'Port discovery timed out. Try refreshing.' : error.message);
        }
      } finally {
        clearTimeout(deadline);
        if (active) setCompletedScan(scan);
      }
    })();
    return () => { active = false; clearTimeout(deadline); controller.abort(); };
  }, [mode, scan, available]);

  async function updateBase(target, disconnect = false) {
    if (pending || !available) return;
    const controller = new AbortController();
    requests.current.add(controller);
    const deadline = setTimeout(() => controller.abort(), 15000);
    setPending(target);
    setActionError('');
    setMessage('');
    try {
      const response = await fetch(disconnect ? `/api/ble-bases/${encodeURIComponent(target)}` : '/api/ble-bases', {
        method: disconnect ? 'DELETE' : 'POST',
        headers: disconnect ? undefined : { 'Content-Type': 'application/json' },
        body: disconnect ? undefined : JSON.stringify({ port: target }),
        signal: controller.signal,
      });
      const body = response.status === 204 ? {} : await response.json();
      if (!requests.current.has(controller)) return;
      if (!response.ok) throw new Error(body.detail || body.error || `Connection request failed (${response.status})`);
      setMessage(disconnect ? `${target} disconnected.` : `${target} added. The base station will connect to a compatible BLE node.`);
      if (!disconnect) setPort('');
    } catch (error) {
      if (requests.current.has(controller)) setActionError(error.name === 'AbortError' ? 'The request timed out. Check the base-station status before retrying.' : error.message);
    } finally {
      clearTimeout(deadline);
      if (requests.current.has(controller)) setPending('');
      requests.current.delete(controller);
    }
  }

  return <details className="panel receiver-connections" open>
    <summary className="panel-title">Receiver connections <span>{receiver?.tcp_enabled !== false ? `TCP ${receiver?.tcp_port || 5443}` : 'TCP disabled'} · {bases.length} BLE {bases.length === 1 ? 'base' : 'bases'}</span></summary>
    <div className="receiver-connection-body">
      <fieldset className="connection-modes">
        <legend>Add a connection</legend>
        {[['wifi', 'Wi-Fi'], ['ethernet', 'Ethernet'], ['ble', 'BLE']].map(([value, label]) => <label key={value} className={mode === value ? 'selected' : ''}>
          <input type="radio" name="receiver-mode" value={value} checked={mode === value} onChange={() => { setMode(value); if (value === 'ble') setRefresh(current => current + 1); setActionError(''); setMessage(''); }}/>{label}
        </label>)}
      </fieldset>
      {mode !== 'ble' ? <p className="connection-help">{receiver?.tcp_enabled === false ? 'The TCP listener is disabled. Enable it in the receiver configuration to accept Wi-Fi and Ethernet nodes.' : `On the node setup page, choose ${mode === 'wifi' ? 'Wi-Fi' : 'Ethernet'} and enter this receiver computer’s LAN IP address and TCP port ${receiver?.tcp_port || 5443}. Nodes connect automatically.`}</p> : <>
        <p className="connection-help">Connect the BLE base station’s USB cable to this receiver computer, then select its serial port. Windows Bluetooth pairing is not required.</p>
        <div className="serial-port-controls">
          <label htmlFor="ble-base-port">Base-station COM port<select id="ble-base-port" value={port} disabled={!available || loading || !serialAvailable || Boolean(pending)} onChange={event => { setPort(event.target.value); setActionError(''); setMessage(''); }}>
            <option value="">{loading ? 'Finding serial ports…' : 'Select a serial port'}</option>
            {ports.map(item => <option key={item.port} value={item.port} disabled={bases.some(base => base.port === item.port)}>{item.port}{item.description ? ` · ${item.description}` : ''}{bases.some(base => base.port === item.port) ? ' (connected)' : ''}</option>)}
          </select></label>
          <button className="connection-secondary" disabled={!available || loading || Boolean(pending)} onClick={() => setRefresh(value => value + 1)}>{loading ? 'Refreshing…' : 'Refresh ports'}</button>
          <button className="primary" disabled={!available || !port || attached || loading || !serialAvailable || Boolean(pending)} onClick={() => updateBase(port)}>{pending === port && port ? 'Connecting…' : 'Connect base station'}</button>
        </div>
        <p className="connection-help">After connecting the base station, open the sensing node’s setup page: choose BLE, scan and select this base station, confirm, then Save and start.</p>
        {!loading && !portsError && !ports.length && available && <p className="connection-help" role="status">No serial ports found. Connect the base station with a USB data cable, then refresh.</p>}
        {!loading && portsError && <p className="notice error" role="alert">{portsError}</p>}
      </>}
      {message && <p className="connection-result" role="status">{message}</p>}
      {actionError && <p className="notice error" role="alert">{actionError}</p>}
      {bases.length > 0 && <ul className="ble-base-list" aria-label="Attached BLE base stations">
        {bases.map(base => <li key={base.port}>
          <div><strong>{base.port}</strong><span className={`state ${available && base.connected ? 'live' : ''}`}>{available ? baseState(base) : 'Receiver unavailable'}</span>{base.device && <span className="base-device">Node {base.device}</span>}
            {base.error && <p className="connection-base-error" role="alert">{base.error}</p>}
          </div>
          <button className="connection-secondary" aria-label={`Disconnect base station ${base.port}`} disabled={!available || Boolean(pending)} onClick={() => updateBase(base.port, true)}>{pending === base.port ? 'Please wait…' : 'Disconnect'}</button>
        </li>)}
      </ul>}
      <p className="connection-help connection-footnote">Wi-Fi, Ethernet and attached BLE base stations can receive together. Each current BLE base station receives one node; connect another base station for another BLE node.</p>
    </div>
  </details>;
}

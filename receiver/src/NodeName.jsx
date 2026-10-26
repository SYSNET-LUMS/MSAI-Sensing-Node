import { useState } from 'react';

export default function NodeName({ nodeId, name }) {
  const [draft, setDraft] = useState(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  async function save(event) {
    event.preventDefault();
    if (busy) return;
    setBusy(true); setMessage('');
    try {
      const response = await fetch(`/api/nodes/${encodeURIComponent(nodeId)}/name`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: (draft ?? name).trim() }), signal: AbortSignal.timeout(5000),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'Could not save name');
      setDraft(body.name); setMessage('Name saved on receiver');
    } catch (error) { setMessage(error.message); }
    finally { setBusy(false); }
  }
  return <form className="node-name-form" onSubmit={save}>
    <label htmlFor={`name-${nodeId}`}>Room / equipment name
      <input id={`name-${nodeId}`} value={draft ?? name} maxLength={80} required disabled={busy}
        placeholder="Pump room / Motor 01" onChange={e => { setDraft(e.target.value); setMessage(''); }} />
    </label>
    <button disabled={busy || !(draft ?? name).trim()}>{busy ? 'Saving…' : 'Save name'}</button>
    <small>Example: Pump room / Motor 01 · identity stays linked to {nodeId}</small>
    <span role="status">{message}</span>
  </form>;
}

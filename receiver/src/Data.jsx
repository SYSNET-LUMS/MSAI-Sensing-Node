import { useState } from 'react';
import { useLive } from './useLive';
import LiveWaveform from './LiveWaveform';
import LiveFFT from './LiveFFT';
import NodeName from './NodeName';
import ReceiverConnections from './ReceiverConnections';
import './dashboard.css';

const pins=['29/30','32/31','1/2','4/3','5/6','8/7','9/10'];
const channels = Array.from({ length: 7 }, (_, id) => ({ id, pins:pins[id], kind: id < 4 ? 'Current' : 'Voltage' }));
const format = value => typeof value !== 'number' || !Number.isFinite(value) ? '—' : value !== 0 && Math.abs(value) < .001 ? value.toExponential(2) : value.toFixed(Math.abs(value) < 1 ? 4 : 2);
const quantity = value => typeof value === 'number' ? value.toLocaleString() : '—';
const age = value => value == null ? 'No samples' : value < 1000 ? `${Math.round(value)} ms ago` : `${(value / 1000).toFixed(1)} s ago`;
const stateLabel = info => info.online ? 'Receiving data' : info.connected ? 'Connected · waiting for samples' : 'Disconnected';
const selectorLabel = value => ({0:'CR · 30 A',1:'SCT · 30 A',2:'CR · 75 A',3:'Invalid selector'})[value] || 'Waiting for selection';

function Waveform({ id, info, selected, setSelected, paused, setPaused }) {
  const [open,setOpen]=useState(true);
  const [windowMs, setWindowMs] = useState(200);
  const [scale, setScale] = useState('auto');
  const [center, setCenter] = useState(true);
  const channelIds = selected === 'ct' ? '0,1,2,3' : selected === 'voltage' ? '4,5,6' : selected;
  return <section className="wave-card panel" aria-label={`Waveform ${id}`}>
    <h2><button className="panel-title yellow collapse-heading" aria-expanded={open} aria-controls={`wave-${id}`} onClick={()=>setOpen(!open)}>Waveform <span className={`chevron ${open?'':'closed'}`} aria-hidden="true"/></button></h2>
    {open&&<div id={`wave-${id}`}>
    <div className="wave-tools">
      <label>Channel<select value={selected} onChange={e => setSelected(e.target.value)}>
        {Array.from({length:8},(_,ch)=><option key={ch} value={ch}>CH{ch} · {ch<4?'Current':ch<7?'Voltage':'Auxiliary'}</option>)}
        <option value="ct">All current channels</option><option value="voltage">All voltage channels</option>
      </select></label>
      <label>Time window<select value={windowMs} onChange={e=>setWindowMs(Number(e.target.value))}>
        {[100,200,500,1000].map(ms=><option key={ms} value={ms}>{ms} ms</option>)}
      </select></label>
      <label>Scale<select value={scale} onChange={e=>setScale(e.target.value)}>
        <option value="auto">Auto</option>{[1,10,100,1000].map(n=><option key={n} value={n}>±{n} channel units</option>)}
      </select></label>
      <label className="check"><input type="checkbox" checked={center} onChange={e=>setCenter(e.target.checked)}/>Remove DC offset</label>
      <button className="primary pause-button" onClick={()=>setPaused(!paused)}>{paused?'Resume plots':'Pause plots'}</button><small>Raw recording continues</small>
    </div>
    <LiveWaveform key={`${id}:${channelIds}:${windowMs}:${center}:${info.session_id}:${info.window_version}:${info.online}:${paused}`}
      nodeId={id} channels={channelIds} windowMs={windowMs} scale={scale} center={center}
      enabled={Boolean(info.online&&!paused)} paused={paused} baseUrl="/api/waveform" />
    </div>}
  </section>;
}

function Diagnostics({ info }) {
  const s=info.status||{};
  return <details className="diagnostics"><summary>Connection & acquisition diagnostics</summary>
    <div className="diagnostic-grid">
      <span>Last samples <b>{age(info.waveform_age_ms)}</b></span>
      <span>Relative stream delay <b>{quantity(info.stream_delay_ms)} ms</b></span>
      <span>Node queue <b>{quantity(s.queued_blocks)} blocks · {quantity(s.queued_ms)} ms</b></span>
      <span>Longest arrival gap <b>{quantity(info.max_arrival_gap_ms)} ms</b></span>
      <span>Wi-Fi disconnects <b>{quantity(s.wifi_disconnects)}</b></span>
      <span>Last Wi-Fi reason <b>{quantity(s.wifi_disconnect_reason)}</b></span>
      <span>Send stalls ≥100 ms <b>{quantity(s.send_stalls)}</b></span>
      <span>Transport pauses <b>{quantity(s.transport_pauses)}{s.transport_paused?' · recovering':''}</b></span>
      <span>Firmware <b>{s.firmware??'Not reported'}</b></span>
      <span>Max send <b>{s.send_us_max==null?'—':(s.send_us_max/1000).toFixed(1)} ms</b></span>
      <span>Receiver processing max <b>{quantity(info.processing_ms_max)} ms</b></span>
      <span>Discontinuities <b>{quantity(info.discontinuities)}</b></span>
      <span>Receiver errors <b>{quantity(info.receiver_errors)}</b></span>
      <span>ADC missed / CRC <b>{quantity(s.missed_adc)} / {quantity(s.adc_crc)}</b></span>
      <span>Overflows <b>{quantity(s.overflows)}</b></span>
      <span>Fault <b>{s.fault??'Not reported'}{info.status_fresh===false?' (stale)':''}</b></span>
    </div>
    <p>Relative delay measures growth since the fastest arrival in this acquisition epoch; it is not absolute network latency.</p>
  </details>;
}

function Jobs({ nodes }) {
  const result=useLive('/api/recordings',3000);
  const entries=Object.entries(result.nodes);
  return <section className="panel jobs-panel"><h1 className="panel-title">Collection Jobs</h1><div className="panel-body">
    <p>Each connected node records automatically. Completed parts remain available after the node disconnects.</p>
    {result.error&&<p className="notice error" role="alert">{result.error}</p>}
    <div className="table-scroll"><table><thead><tr><th>Node</th><th>Recording part</th><th>Size</th><th>Status</th><th>Download</th></tr></thead><tbody>
      {entries.map(([file,item])=><tr key={file}><td>{nodes[item.device]?.name||item.name||item.device}<small>{item.device}</small></td><td>{file}</td><td>{(item.bytes/1048576).toFixed(2)} MB</td><td>{item.active?'Recording':'Closed part'}</td><td>{item.active?'Available when closed':<><a href={`/api/recordings/${encodeURIComponent(file)}`}>Raw data</a>{item.metadata&&<> · <a href={`/api/recordings/${encodeURIComponent(item.metadata)}`}>Metadata</a></>}</>}</td></tr>)}
      {!entries.length&&<tr><td colSpan="5">No recordings yet.</td></tr>}
    </tbody></table></div><small>Showing the 100 most recent parts. A part can end at a disconnect; check its sequence and CRC when exporting.</small>
  </div></section>;
}

function Relays({ nodes }) {
  return <section className="panel"><h1 className="panel-title">Relay Status</h1><div className="panel-body">
    <p>Relays follow the board’s sensor selector and acquisition interlocks. Remote manual switching is not exposed by this firmware.</p>
    {Object.entries(nodes).map(([id,info])=><div className="relay-node" key={id}><strong>{info.name} · {id}</strong><p>{selectorLabel(info.selectors)} · {info.status_fresh?'Current status':'Status unavailable or stale'}</p><div className="relay-grid">
      {[0,1,2,3].map(bit=><div key={bit}><span>Relay {bit+1}</span><strong>{!info.status_fresh||info.status?.feedback==null?'Unknown':(info.status.feedback&(1<<bit))?'Energized':'Released'}</strong></div>)}
    </div></div>)}
    {!Object.keys(nodes).length&&<p>No nodes connected yet.</p>}
  </div></section>;
}


const emptyNode={name:'No node selected',online:false,readings:{},units:{}};
const nodeLabel=(id,info)=>info.name&&!info.name.startsWith('MSAI-')?info.name:`Node #${id.slice(-4).toLowerCase()}`;
const duration=seconds=>seconds==null?'—':`${Math.floor(seconds/3600)}h ${Math.floor(seconds%3600/60)}m ${Math.floor(seconds%60)}s`;
const bandwidth=bytes=>bytes==null?'Measuring…':`${(bytes/1000).toLocaleString(undefined,{maximumFractionDigits:1})} kB/s`;

function NodePicker({nodes,ids,active,setPicked,receiver}) {
  return <section className="panel connected-panel"><h1 className="panel-title">Connected Nodes</h1>
    <div className="node-tabs" aria-label="Select node">
      {ids.map(id=><button key={id} aria-pressed={active===id} onClick={()=>setPicked(id)} title={`${nodes[id].name} · ${id} · ${stateLabel(nodes[id])}`}>
        {nodeLabel(id,nodes[id])}{!nodes[id].online&&<small>{nodes[id].connected?'Waiting':'Offline'}</small>}
      </button>)}
      {ids.length>1&&<button className="all-nodes" aria-pressed={active==='all'} onClick={()=>setPicked('all')}>All nodes</button>}
      {receiver?.ble_name&&<p className="no-nodes" role="status">BLE receiver: {receiver.ble} · {receiver.ble_name}</p>}
      {!ids.length&&<p className="no-nodes">{receiver?.ble_bases?.length?'Waiting for nodes. On the node setup page, select BLE and confirm the attached base station. Wi-Fi and Ethernet nodes can also connect using the receiver settings above.':receiver?.ble_name?'Keep the node powered on with BLE saved in its setup page. Connection is automatic; Windows pairing is not required.':receiver?.tcp_enabled===false?'Attach a BLE base station using Receiver connections above.':`Waiting for nodes. Connect to this PC’s LAN IP on TCP port ${receiver?.tcp_port||5443}, or attach a BLE base station above.`}</p>}
      {receiver?.last_error&&<p className="notice error" role="alert">Last receiver error: {receiver.last_error}</p>}
    </div>
  </section>;
}
function NodeInfo({info,nodes,active}) {
  const all=active==='all';
  const start=info?.boot_time_estimate?new Date(info.boot_time_estimate*1000):null;
  return <section className="panel node-info"><h2 className="panel-title">Node Info</h2><div className="panel-body">
    {all?<><p>{Object.values(nodes).filter(n=>n.online).length} nodes receiving data</p><p>Total bandwidth: {bandwidth(Object.values(nodes).reduce((sum,n)=>sum+(n.bandwidth_bps||0),0))}</p></>:<>
      <p title="Uptime from the latest packet. Start time is estimated from arrival time; network delay can affect it.">Uptime: {duration(info?.uptime_seconds)}{start&&<span className="boot-time"> (since ≈{start.toLocaleString(undefined,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})})</span>}</p>
      <p>Current Bandwidth: {info?bandwidth(info.bandwidth_bps):'—'}</p>
    </>}
    <div className="node-info-status"><span className={`state ${info?.online||all?'live':''}`}>{all?'Multiple nodes':info?stateLabel(info):'Waiting for connection'}</span>{info&&<code>{info.deviceId}</code>}</div>
  </div></section>;
}
function NodePanel({id,info,paused,setPaused,showName=false}) {
  const [selected,setSelected]=useState('0');
  const status=info.status||{};
  const synthetic=String(info.calibration_metadata?.source||'').startsWith('SYNTHETIC');
  return <article className="node-section" aria-label={info.name||'Measurements'}>
    {showName&&<h2 className="node-heading">{nodeLabel(id,info)} <small>{id}</small></h2>}
    {synthetic&&<p className="notice synthetic">SYNTHETIC TEST DATA — NOT REAL MOTOR MEASUREMENTS</p>}
    {info.error&&<p className="notice error" role="alert">Receiver: {info.error}</p>}
    {status.fault>0&&<p className="notice error" role="alert">Acquisition stopped · fault {status.fault}. Check the node console.</p>}
    {info.status_fresh&&status.transport_paused&&<p className="notice" role="status">Connection recovering. Buffered samples are being delivered; acquisition resumes automatically in a new segment.</p>}
    {info.connected&&!info.online&&<p className="notice" role="status">Waiting for fresh samples. Connection diagnostics are available in Node Settings.</p>}
    {info.online&&info.stream_delay_ms>500&&<p className="notice" role="status">Stream catching up · approximately {Math.round(info.stream_delay_ms)} ms of additional delay.</p>}
    {paused&&<p className="notice" role="status">Plots paused. Raw data collection and recording continue.</p>}
    <div className="measurement-grid"><div className="channel-grid">
      {channels.map(ch=><button key={ch.id} className="ch-card" title={`CH${ch.id} · pins ${ch.pins} · ${info.conversions?.[ch.id]?.reason||'Select waveform channel'}`} aria-pressed={selected===String(ch.id)} onClick={()=>setSelected(String(ch.id))}>
        <span className="ch-head"><strong>CH{ch.id}</strong><span>{ch.kind}</span></span>
        <span className="ch-body"><strong>{format(info.readings?.[ch.id])}</strong><span>{info.units?.[ch.id]||(id==='waiting'?(ch.id<4?'A':'V'):'counts')} {ch.id<4?'RMS':'AC RMS'}</span>
          {id!=='waiting'&&<small>{info.units?.[ch.id]==='counts'||!info.units?.[ch.id]?'Unscaled':info.calibrated?.[ch.id]?'Reference calibrated':'Nominal scale'}</small>}
        </span>
      </button>)}
    </div><LiveFFT nodeId={id} enabled={Boolean(info.online&&!paused)} paused={paused} resetKey={`${info.session_id}:${info.window_version}`}/></div>
    <Waveform {...{id,info,selected,setSelected,paused,setPaused}}/>
  </article>;
}
function Settings({nodes,shown}) {
  return <div className="settings-view">
    {!shown.length&&<section className="panel"><h2 className="panel-title">Node Settings</h2><div className="panel-body">Connect a node to assign a room or equipment name and view its diagnostics.</div></section>}
    {shown.map(id=>{const info=nodes[id];return <section className="panel settings-node" key={id}>
      <h2 className="panel-title">{nodeLabel(id,info)} <span>{id}</span></h2>
      <div className="settings-grid"><div><h3>Room / equipment name</h3><NodeName nodeId={id} name={info.name||id}/><p className="muted">Names stay associated with the hardware ID across reconnects.</p></div>
        <div><h3>Recording & connection</h3><p>{info.recording_active?'Recording automatically':'Recording inactive'} · {quantity(info.samples)} samples/channel</p><p>{quantity(info.rate)} samples/s/channel · {selectorLabel(info.selectors)}</p><p>{info.status?.link||info.transport||'TCP'}{info.status?.wifi_rssi_dbm!=null&&info.status.wifi_rssi_dbm!==-127?` · Wi-Fi ${info.status.wifi_rssi_dbm} dBm · channel ${info.status.wifi_channel}`:''}</p><p className="recording-file">{info.recording||'Waiting for a recording'}</p><p className="muted">RMS window: {Math.round(info.rms_window_ms||0)} ms · DC removed. CH7 is available in both plots.</p></div>
      </div><Diagnostics info={info}/>
    </section>})}
    <Jobs nodes={nodes}/><Relays nodes={nodes}/>
  </div>;
}
export default function Data() {
  const connection=useLive('/api/live');
  const nodes=connection.nodes, ids=Object.keys(nodes).sort();
  const [picked,setPicked]=useState(null),[tab,setTab]=useState('data'),[paused,setPaused]=useState(false);
  const active=picked==='all'&&ids.length>1?'all':ids.includes(picked)?picked:ids[0];
  const shown=active==='all'?ids:active?[active]:[];
  const info=active&&active!=='all'?{...nodes[active],deviceId:active}:null;
  return <div className="dashboard">
    <header className="app-header"><div className="header-content"><img src="/node-logo.png" alt="MultiSensor AI · EdgeAI Innovation Lab · LUMS"/><nav aria-label="Main navigation">
      {[['data','Dashboard'],['settings','Node Settings']].map(([key,label])=><button key={key} aria-current={tab===key?'page':undefined} onClick={()=>setTab(key)}>{label}</button>)}
    </nav></div></header>
    <main className="dashboard-main">
      {!connection.connected&&<p className="notice error" role="alert">{connection.error?`Receiver unavailable: ${connection.error}`:'Connecting to receiver…'}</p>}
      <ReceiverConnections receiver={connection.receiver} available={connection.connected}/>
      <div className="setup-grid"><NodePicker {...{nodes,ids,active,setPicked}} receiver={connection.connected?connection.receiver:null}/><NodeInfo {...{info,nodes,active}}/></div>
      {tab==='data'&&(shown.length?shown:['waiting']).map(id=><NodePanel key={id} id={id} info={nodes[id]||emptyNode} paused={paused} setPaused={setPaused} showName={active==='all'}/>)}
      {tab==='settings'&&<Settings {...{nodes,shown}}/>}
    </main>
  </div>;
}

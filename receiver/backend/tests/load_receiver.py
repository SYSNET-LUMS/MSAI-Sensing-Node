"""Paced four-node TCP ingestion under simultaneous FFT/waveform/live HTTP load."""
import asyncio
import hashlib
import json
import math
from pathlib import Path
import struct
import time
import uuid
import zlib

from fastapi.testclient import TestClient
from backend.server import create_app
from backend.simulate_node import waveform_packet, SYNTHETIC_CALIBRATION
from backend.tests.test_bridge import settings
from backend.vendor.protocol import HEADER


def run():
    output=Path('test-evidence')/('load-0.1.8-'+uuid.uuid4().hex)
    output.mkdir(parents=True)
    app=create_app(settings(output))
    ids=[f'DE000000000{i}' for i in range(1,5)]
    templates=[]
    for block in range(25):
        first=block*128
        rows=[[round(4000*math.sin(2*math.pi*50*(first+i)/10000+ch)) for ch in range(8)] for i in range(128)]
        templates.append(waveform_packet(rows,first))
    wires=[]
    for block in range(625):
        original=templates[block%25];fields=list(HEADER.unpack_from(original));fields[8]=block*128;fields[9]=block*12800
        raw=HEADER.pack(*fields)+original[HEADER.size:-4]
        wires.append(raw+struct.pack('<I',zlib.crc32(raw)))
    expected=hashlib.sha256(b''.join(wires)).hexdigest()
    metrics={'nodes':4,'sample_rate':10000,'samples_per_node':80000,'http_requests':0,'max_http_ms':0,'http_errors':0}
    with TestClient(app) as client:
        async def scenario():
            stop=asyncio.Event()
            async def send(device):
                reader,writer=await asyncio.open_connection('127.0.0.1',app.state.bridge.settings.tcp_port)
                writer.write(json.dumps({'protocol':'MSAC1','device':device,'rate':10000,'calibration':SYNTHETIC_CALIBRATION}).encode()+b'\n');await writer.drain()
                assert await reader.readline()==b'OK\n'
                start=asyncio.get_running_loop().time()
                for block,wire in enumerate(wires):
                    writer.write(wire);await writer.drain()
                    await asyncio.sleep(max(0,start+(block+1)*.0128-asyncio.get_running_loop().time()))
                # Keep the socket open until receiver has drained every last frame.
                for _ in range(500):
                    if app.state.store.nodes[device].samples==80000:break
                    await asyncio.sleep(.01)
                assert app.state.store.nodes[device].samples==80000
                writer.close();await writer.wait_closed()
            async def poll(device,path,interval):
                while not stop.is_set():
                    start=time.perf_counter()
                    response=await asyncio.to_thread(client.get,path)
                    metrics['max_http_ms']=max(metrics['max_http_ms'],(time.perf_counter()-start)*1000)
                    metrics['http_requests']+=1
                    if response.status_code not in (200,404):metrics['http_errors']+=1
                    await asyncio.sleep(interval)
            jobs=[asyncio.create_task(poll(device,f'/api/fft/{device}?window_ms=100',.05)) for device in ids]
            jobs += [asyncio.create_task(poll(device,f'/api/waveform/{device}?channels=0,1,2,3&window_ms=1000&max_points=700',.2)) for device in ids]
            jobs += [asyncio.create_task(poll('', '/api/live',.3))]
            start=time.perf_counter()
            try:await asyncio.gather(*(send(device) for device in ids))
            finally:
                stop.set();await asyncio.gather(*jobs)
            metrics['elapsed_seconds']=round(time.perf_counter()-start,3)
        asyncio.run(scenario())
        snapshot=client.get('/api/live').json()['nodes']
        metrics['results']={device:{key:node[key] for key in ['samples','discontinuities','receiver_errors','max_arrival_gap_ms','processing_ms_max','stream_delay_ms']} for device,node in snapshot.items()}
        for info in snapshot.values():
            assert info['samples']==80000 and info['discontinuities']==0 and info['receiver_errors']==0
    recordings=list(app.state.bridge.settings.recordings.glob('*.msac'))
    assert len(recordings)==4
    assert all(hashlib.sha256(path.read_bytes()).hexdigest()==expected for path in recordings)
    metrics['exact_recordings']=4;metrics['max_http_ms']=round(metrics['max_http_ms'],2)
    assert metrics['http_errors']==0
    (output/'results.json').write_text(json.dumps(metrics,indent=2))
    print(json.dumps(metrics,indent=2));print('Evidence:',output)

if __name__=='__main__':run()

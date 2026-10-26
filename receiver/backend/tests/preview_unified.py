"""Explicit local UI fixture: synthetic TCP + COM streams, never physical ports.

Run from v1_receiver: .venv/Scripts/python -m backend.tests.preview_unified
"""
import asyncio
from contextlib import asynccontextmanager, suppress
import json
from pathlib import Path
import threading
import time
import uuid

import uvicorn

from backend import serial_base
from backend.server import Settings, create_app
from backend.serial_wire import pack
from backend.simulate_node import simulate, waveform_packet, status_packet, SYNTHETIC_CALIBRATION
from backend.tests.test_serial_base import FakeSerial


class StreamingSerial(FakeSerial):
    def write(self, raw):
        result = super().write(raw)
        if raw == b'HELLO\n':
            threading.Thread(target=self.produce, daemon=True).start()
        return result

    def produce(self):
        device = 'DE0000000091' if self.port == 'COM91' else 'DE0000000092'
        session = 91 if self.port == 'COM91' else 92
        metadata = dict(protocol='MSAC1', transport='ble-plain-v1', device=device,
                        calibration=SYNTHETIC_CALIBRATION, rate=10000)
        self.feed(pack(2, json.dumps(metadata).encode(), 0, session, version=2))
        sequence = 1
        first = 0
        started = time.monotonic()
        while not self.closed:
            if len(self.buffer) > 16384:
                time.sleep(.01)
                continue
            rows = [[(first+i)%200-100+ch for ch in range(8)] for i in range(128)]
            self.feed(pack(1, waveform_packet(rows, first), sequence, session, version=2))
            sequence += 1
            if first % 12800 == 0:
                self.feed(pack(1, status_packet(first, status={'fault':0,'running':True,'link':'ble'}), sequence, session, version=2))
                sequence += 1
                self.feed(pack(3, b'{"state":"streaming","firmware":"SIMULATED","usb_retries":0}', sequence, session, version=2))
                sequence += 1
            first += 128
            time.sleep(max(0, started+first/10000-time.monotonic()))


real_base = serial_base.SerialBase
serial_base.SerialBase = lambda bridge, port: real_base(bridge, port, serial_factory=StreamingSerial)
serial_base.list_serial_ports = lambda: {'available':True,'error':None,'ports':[
    {'port':'COM91','description':'SIMULATED BLE base A','hwid':'FIXTURE'},
    {'port':'COM92','description':'SIMULATED BLE base B','hwid':'FIXTURE'}]}

folder = Path('evidence') / ('unified-preview-'+uuid.uuid4().hex[:8])
folder.mkdir(parents=True)
calibration = folder/'calibration.json'
calibration.write_text(json.dumps({'devices':{
    'DE0000000090':{'name':'SIMULATED TCP node'},
    'DE0000000091':{'name':'SIMULATED BLE node A'},
    'DE0000000092':{'name':'SIMULATED BLE node B'}}}))
app = create_app(Settings(tcp_bind='127.0.0.1',tcp_port=0,calibration=calibration,
                          recordings=folder/'recordings',minimum_free_bytes=0))
original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def lifespan(application):
    async with original_lifespan(application):
        task = asyncio.create_task(simulate('127.0.0.1', app.state.bridge.settings.tcp_port,
                                            3600,10000,'DE0000000090'))
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


app.router.lifespan_context = lifespan
if __name__ == '__main__':
    uvicorn.run(app,host='127.0.0.1',port=8002,access_log=False)

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import socket
import threading
import time

import numpy as np
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from backend.analysis import AnalysisWorker
from backend.server import create_app, Recording
from backend.tests.test_bridge import DEVICE, HELLO, accept, rows, settings
from backend.simulate_node import waveform_packet
from backend.store import LiveStore


def peer(app, device=DEVICE):
    sock=socket.create_connection(('127.0.0.1',app.state.bridge.settings.tcp_port),timeout=3)
    sock.sendall(json.dumps({**HELLO,'device':device}).encode()+b'\n')
    assert sock.recv(3)==b'OK\n'
    return sock


def wait_samples(client, device, count):
    end=time.monotonic()+2
    while time.monotonic()<end:
        info=client.get('/api/live').json()['nodes'][device]
        if info['samples']>=count:return info
        time.sleep(.01)
    raise AssertionError(f'Collection did not reach {count} while other work was blocked')


def test_fft_thread_can_block_without_stopping_collection_or_display(tmp_path,monkeypatch):
    app=create_app(settings(tmp_path));entered=threading.Event();release=threading.Event();threads=[]
    original=np.fft.rfft
    def slow_fft(*args,**kwargs):
        threads.append(threading.current_thread().name);entered.set()
        assert release.wait(5),'FFT test gate timed out'
        return original(*args,**kwargs)
    with TestClient(app) as client, peer(app) as sock, ThreadPoolExecutor(1) as callers:
        for first in range(0,1024,128):sock.sendall(waveform_packet(rows(first),first))
        wait_samples(client,DEVICE,1024)
        monkeypatch.setattr(np.fft,'rfft',slow_fft)
        request=callers.submit(client.get,f'/api/fft/{DEVICE}')
        try:
            assert entered.wait(2)
            for first in range(1024,2048,128):sock.sendall(waveform_packet(rows(first),first))
            info=wait_samples(client,DEVICE,2048)
            assert not request.done()
            assert info['discontinuities']==0
            assert client.get('/api/health').json()['fft_worker']['pending']==1
            assert client.get(f'/api/waveform/{DEVICE}?channels=0').json()['source_samples']>0
            assert threads[0].startswith('msai-fft')
        finally:release.set()
        assert request.result(2).status_code==200


def test_slow_disk_on_one_node_does_not_block_other_node_or_http(tmp_path,monkeypatch):
    entered=threading.Event();release=threading.Event();thread_names=[];original=Recording.write
    second='DE0000000002';app=create_app(settings(tmp_path))
    def slow_write(self,wire):
        thread_names.append(threading.current_thread().name)
        if self.node.device==DEVICE:
            entered.set();assert release.wait(5)
        return original(self,wire)
    monkeypatch.setattr(Recording,'write',slow_write)
    with TestClient(app) as client, peer(app) as a, peer(app,second) as b:
        try:
            a.sendall(waveform_packet(rows()))
            assert entered.wait(2)
            b.sendall(waveform_packet(rows()))
            assert wait_samples(client,second,128)['online']
            assert client.get('/api/live').json()['nodes'][DEVICE]['samples']==0
            assert client.get('/api/health').status_code==200
            assert all(name.startswith('msai-collect') for name in thread_names)
        finally:release.set()
        wait_samples(client,DEVICE,128)


def test_cancelled_browser_request_keeps_bounded_worker_slot():
    async def scenario():
        worker=AnalysisWorker('test-fft',capacity=1);entered=threading.Event();release=threading.Event()
        def blocked():entered.set();release.wait(3)
        task=asyncio.create_task(worker.run(blocked))
        try:
            assert await asyncio.to_thread(entered.wait,1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):await task
            assert worker.status()['pending']==1
            with pytest.raises(HTTPException) as err:await worker.run(lambda:1)
            assert err.value.status_code==503
        finally:
            release.set();await worker.close()
        assert worker.status()['pending']==0
    asyncio.run(scenario())


def test_worker_does_not_publish_fft_after_disconnect(tmp_path,monkeypatch):
    entered=threading.Event();release=threading.Event();original=np.fft.rfft
    def block(*args):entered.set();release.wait(3);return original(*args)
    app=create_app(settings(tmp_path))
    with TestClient(app) as client, peer(app) as sock, ThreadPoolExecutor(1) as callers:
        for first in range(0,1024,128):sock.sendall(waveform_packet(rows(first),first))
        wait_samples(client,DEVICE,1024);monkeypatch.setattr(np.fft,'rfft',block)
        request=callers.submit(client.get,f'/api/fft/{DEVICE}')
        try:
            assert entered.wait(2);sock.shutdown(socket.SHUT_RDWR)
            for _ in range(100):
                if not client.get('/api/live').json()['nodes'][DEVICE]['recording_active']:break
                time.sleep(.01)
        finally:release.set()
        frame=request.result(2).json()
        assert not frame['online'] and not frame['amplitudes']


def test_frozen_view_is_independent_and_bounded():
    store=LiveStore();node=store.connect(HELLO,'TCP','one')
    for first in range(0,1024,128):accept(node,first)
    view=store.view(DEVICE,50);values=view.nodes[DEVICE].recent()
    assert len(values)==500
    for first in range(1024,4096,128):accept(node,first)
    np.testing.assert_array_equal(values,view.nodes[DEVICE].recent())
    assert view.nodes[DEVICE].next_index==1024 and node.next_index==4096


def test_recording_history_download_and_active_file_guard(tmp_path):
    app=create_app(settings(tmp_path));wire=waveform_packet(rows())
    with TestClient(app) as client:
        with peer(app) as sock:
            sock.sendall(wire);wait_samples(client,DEVICE,128)
            listing=client.get('/api/recordings').json()['nodes'];name=next(iter(listing))
            assert listing[name]['active']
            assert client.get('/api/recordings/'+name).status_code==409
        for _ in range(100):
            if not client.get('/api/live').json()['nodes'][DEVICE]['recording_active']:break
            time.sleep(.01)
        assert client.get('/api/recordings/'+name).content==wire
        assert client.get('/api/recordings/calibration.json').status_code==404
        assert client.get('/api/recordings/..%2Fcalibration.json').status_code==404


def test_relative_stream_delay_identifies_backlog(monkeypatch):
    clock=[100.0];monkeypatch.setattr('backend.store.time.monotonic',lambda:clock[0])
    store=LiveStore();node=store.connect(HELLO,'TCP','one');accept(node)
    clock[0]+=1.0128;accept(node,128)
    assert node.stream_delay_ms==pytest.approx(1000)
    assert node.max_arrival_gap_ms==pytest.approx(1012.8)
    clock[0]+=.01;accept(node,0,epoch=2)
    assert node.stream_delay_ms==0

def test_partial_tcp_frame_survives_six_second_stall(tmp_path):
    app=create_app(settings(tmp_path));wire=waveform_packet(rows());second='DE0000000002'
    with TestClient(app) as client, peer(app) as a, peer(app,second) as b:
        a.sendall(wire[:50])
        for i in range(6):
            b.sendall(waveform_packet(rows(i*128),i*128))
            assert wait_samples(client,second,(i+1)*128)['online']
            time.sleep(1)
        a.sendall(wire[50:])
        info=wait_samples(client,DEVICE,128)
        assert info['discontinuities']==0 and info['receiver_errors']==0


def test_cancelled_handshake_closes_recording_created_by_worker(tmp_path,monkeypatch):
    from backend.server import Bridge
    entered=threading.Event();release=threading.Event();opened=[];original=Recording.__init__
    def delayed(self,*args):
        entered.set();release.wait(3);original(self,*args);opened.append(self)
    monkeypatch.setattr(Recording,'__init__',delayed)
    async def scenario():
        bridge=Bridge(settings(tmp_path),LiveStore());await bridge.start()
        reader,writer=await asyncio.open_connection('127.0.0.1',bridge.settings.tcp_port)
        writer.write(json.dumps(HELLO).encode()+b'\n');await writer.drain()
        assert await asyncio.to_thread(entered.wait,1)
        stop=asyncio.create_task(bridge.stop());await asyncio.sleep(.05);release.set();await stop
        writer.close();await writer.wait_closed()
        assert opened and opened[0].stream is None
        assert not bridge.store.nodes[DEVICE].connected
        assert not bridge.store.nodes[DEVICE].recording_active
    asyncio.run(scenario())

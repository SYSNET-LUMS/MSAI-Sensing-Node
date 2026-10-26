"""Regression: a half-open socket must not block the same boot's reconnect."""
import asyncio
import json
import threading

import pytest

from backend.server import Bridge, Settings, validate_hello
from backend.store import LiveStore
from backend.simulate_node import waveform_packet

DEVICE = '001122334455'
HELLO = dict(protocol='MSAC1', device=DEVICE, rate=10000,
             boot_id='0123456789abcdef', calibration={})


@pytest.mark.parametrize('boot', ['', 'f' * 15, 'g' * 16, 123, []])
def test_invalid_boot_id(boot):
    with pytest.raises(ValueError, match='boot session'):
        validate_hello({**HELLO, 'boot_id': boot})


def test_same_boot_replaces_half_open_connection_and_flushes_recording(tmp_path):
    async def scenario():
        settings = Settings(tcp_bind='127.0.0.1', tcp_port=0,
                            recordings=tmp_path, minimum_free_bytes=0)
        store = LiveStore()
        bridge = Bridge(settings, store)
        await bridge.start()
        peers = []

        async def connect(hello):
            reader, writer = await asyncio.open_connection('127.0.0.1', settings.tcp_port)
            peers.append(writer)
            writer.write(json.dumps(hello).encode() + b'\n')
            await writer.drain()
            return reader, writer, await asyncio.wait_for(reader.readline(), 2)

        try:
            first_reader, first_writer, ack = await connect(HELLO)
            assert ack == b'OK\n'
            old = store.nodes[DEVICE]
            wire = waveform_packet([[i + c for c in range(8)] for i in range(128)])
            first_writer.write(wire)
            await first_writer.drain()
            for _ in range(100):
                if old.samples == 128:
                    break
                await asyncio.sleep(.01)
            assert old.samples == 128

            # Neither a different boot nor a legacy duplicate can evict this node.
            for hello in ({**HELLO, 'boot_id': 'f' * 16},
                          {k: v for k, v in HELLO.items() if k != 'boot_id'}):
                _, _, ack = await connect(hello)
                assert ack == b''
                assert store.nodes[DEVICE] is old and old.connected

            # Leave old socket open: the node has timed out locally, but Windows
            # has not observed its close. Same-boot replacement must be immediate.
            _, second_writer, ack = await connect(HELLO)
            assert ack == b'OK\n'
            assert await asyncio.wait_for(first_reader.read(), 2) == b''
            assert not old.connected and not old.recording_active
            assert (tmp_path / old.recording).read_bytes() == wire
            new = store.nodes[DEVICE]
            assert new is not old and new.connected
            second_writer.write(wire)
            await second_writer.drain()
            for _ in range(100):
                if new.samples == 128:
                    break
                await asyncio.sleep(.01)
            assert new.samples == 128 and new.receiver_errors == 0
        finally:
            for peer in peers:
                peer.close()
                await peer.wait_closed()
            await bridge.stop()
        assert not bridge.tcp_sessions
        assert len(list(tmp_path.glob('*.msac'))) == 2

    asyncio.run(scenario())


def test_reconnect_finishes_inflight_recording_write(tmp_path):
    async def scenario():
        settings = Settings(tcp_bind='127.0.0.1', tcp_port=0,
                            recordings=tmp_path, minimum_free_bytes=0)
        store = LiveStore()
        bridge = Bridge(settings, store)
        entered, release = threading.Event(), threading.Event()
        original = bridge.packet

        def delayed_packet(*args):
            entered.set()
            assert release.wait(3)
            return original(*args)

        bridge.packet = delayed_packet
        await bridge.start()
        peers = []
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', settings.tcp_port)
            peers.append(writer)
            writer.write(json.dumps(HELLO).encode() + b'\n')
            await writer.drain()
            assert await asyncio.wait_for(reader.readline(), 2) == b'OK\n'
            old = store.nodes[DEVICE]
            wire = waveform_packet([[i + c for c in range(8)] for i in range(128)])
            writer.write(wire)
            await writer.drain()
            assert await asyncio.to_thread(entered.wait, 1)
            reader2, writer2 = await asyncio.open_connection('127.0.0.1', settings.tcp_port)
            peers.append(writer2)
            writer2.write(json.dumps(HELLO).encode() + b'\n')
            await writer2.drain()
            ack = asyncio.create_task(reader2.readline())
            await asyncio.sleep(.05)
            assert not ack.done(), 'Replacement must wait for the in-flight disk write'
            release.set()
            assert await asyncio.wait_for(ack, 2) == b'OK\n'
            assert old.samples == 128 and not old.recording_active
            assert (tmp_path / old.recording).read_bytes() == wire
            assert store.nodes[DEVICE] is not old and store.nodes[DEVICE].connected
        finally:
            release.set()
            for peer in peers:
                peer.close()
                await peer.wait_closed()
            await bridge.stop()

    asyncio.run(scenario())

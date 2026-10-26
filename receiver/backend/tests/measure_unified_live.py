"""Measure an already-running unified receiver; never open hardware ports.

Example:
  python -m backend.tests.measure_unified_live --device 38F5F090A994 \
      --duration 120 --output evidence/unified-hardware-10k.json

This checks sustained delivery and diagnostics. Verify the resulting .msac
recording separately to prove packet CRC/decode and sample continuity.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import urllib.error
import urllib.request


NODE_COUNTERS = (
    'overflows', 'adc_crc', 'missed_adc', 'adc_recoveries', 'transport_pauses',
)
BASE_COUNTERS = ('errors', 'usb_errors', 'disconnects')


def summarize(rows, device, expected_rate, requested_duration):
    errors, warnings = [], []

    def fail(message):
        if message not in errors:
            errors.append(message)

    good = []
    for row in rows:
        if 'error' in row:
            fail(f"HTTP polling failed: {row['error']}")
            continue
        node = row['snapshot'].get('nodes', {}).get(device)
        if node is None:
            fail(f'Node {device} is missing from /api/live')
            continue
        bases = [
            base for base in row['snapshot'].get('receiver', {}).get('ble_bases', [])
            if base.get('device') == device
        ]
        if str(node.get('transport', '')).startswith('BLE/USB') and len(bases) != 1:
            fail('BLE node does not have exactly one matching attached base station')
        good.append((row, node, bases[0] if len(bases) == 1 else None))
        if not node.get('online') or not node.get('connected'):
            fail('Node was offline or disconnected during measurement')
        if not node.get('status_fresh'):
            fail('Node diagnostic status became stale')
        if node.get('rate') != expected_rate:
            fail(f'Configured sample rate was not {expected_rate}')
        if node.get('error'):
            fail(f"Node error: {node['error']}")
        if not node.get('recording_active'):
            fail('Recording was inactive during measurement')
        status = node.get('status', {})
        if status.get('running') is not True:
            fail('Node acquisition was not running')
        if status.get('fault') != 0:
            fail(f"Node reported fault {status.get('fault')}")
        if status.get('transport_paused'):
            fail('Node acquisition paused for transport')
        if bases and (bases[0].get('error') or not bases[0].get('connected')):
            fail(f"Base error/disconnect: {bases[0].get('error')}")

    result = {
        'device': device, 'expected_rate_per_channel': expected_rate,
        'requested_duration_s': requested_duration, 'poll_count': len(rows),
        'valid_poll_count': len(good), 'errors': errors, 'warnings': warnings,
    }
    if len(good) < 2:
        fail('At least two valid live snapshots are required')
        result['continuous_rate_verified'] = False
        return result

    first_row, first, first_base = good[0]
    last_row, last, last_base = good[-1]
    duration = last_row['elapsed_s'] - first_row['elapsed_s']
    samples = last.get('samples', 0) - first.get('samples', 0)
    byte_count = last.get('received_bytes', 0) - first.get('received_bytes', 0)
    result.update(
        duration_s=duration,
        received_samples_per_channel=samples,
        delivered_sps_per_channel=samples / duration if duration > 0 else None,
        validated_packet_bytes_per_s=byte_count / duration if duration > 0 else None,
        session_id=last.get('session_id'), epoch=last.get('epoch'),
        transport=last.get('transport'), recording=last.get('recording'),
        node_firmware=last.get('status', {}).get('firmware'),
        maximum_stream_delay_ms=max(node.get('stream_delay_ms', 0) for _, node, _ in good),
        maximum_arrival_gap_ms=max(node.get('arrival_gap_ms', 0) for _, node, _ in good),
    )
    if duration < requested_duration * .95:
        fail('Measured interval was shorter than requested')
    if duration <= 0 or not expected_rate * .99 <= samples / duration <= expected_rate * 1.01:
        fail('Delivered sample rate is outside 1% of the configured rate')
    if byte_count <= 0:
        fail('No validated packet bytes were received')
    if len({node.get('session_id') for _, node, _ in good}) != 1:
        fail('Receiver session changed')
    if len({node.get('epoch') for _, node, _ in good}) != 1:
        fail('Acquisition epoch changed')

    def counters(values, keys, label, fatal=True):
        deltas = {}
        for key in keys:
            series = [value.get(key) for value in values]
            if any(type(value) is not int for value in series):
                fail(f'Missing or invalid {label}.{key} counter')
                continue
            deltas[key] = series[-1] - series[0]
            if any(after < before for before, after in zip(series, series[1:])):
                fail(f'{label}.{key} counter reset')
            if fatal and deltas[key]:
                fail(f'{label}.{key} increased by {deltas[key]}')
        return deltas

    result['receiver_counter_deltas'] = counters(
        [node for _, node, _ in good], ('discontinuities', 'receiver_errors'), 'receiver',
    )
    result['node_counter_deltas'] = counters(
        [node.get('status', {}) for _, node, _ in good], NODE_COUNTERS, 'node',
    )
    queues = [node.get('status', {}).get('queued_blocks') for _, node, _ in good]
    if any(type(queue) is not int for queue in queues):
        fail('Missing node backlog diagnostics')
    else:
        result.update(
            maximum_queued_blocks=max(queues), initial_queued_blocks=queues[0],
            final_queued_blocks=queues[-1], observed_empty_backlog=0 in queues,
        )
        if queues[-1] > max(32, queues[0]):
            fail('Node backlog remained elevated at the end')
        if 0 not in queues:
            warnings.append('No empty backlog was observed at the polling instants')

    bases = [base for _, _, base in good if base is not None]
    if bases:
        if len(bases) != len(good) or first_base is None or last_base is None:
            fail('Base station diagnostics disappeared during measurement')
        if len({base.get('port') for base in bases}) != 1:
            fail('Base station port changed')
        result['serial_counter_deltas'] = counters(
            bases, ('packet_errors',), 'serial',
        )
        result['recovered_serial_counter_deltas'] = counters(
            bases, ('serial_crc_errors', 'discarded_bytes'), 'serial', fatal=False,
        )
        base_statuses = [base.get('base', {}) for base in bases]
        result['base_counter_deltas'] = counters(base_statuses, BASE_COUNTERS, 'base')
        result['base_retry_delta'] = counters(
            base_statuses, ('usb_retries',), 'base', fatal=False,
        ).get('usb_retries')
        latest = base_statuses[-1]
        result['base_link'] = {
            key: latest.get(key) for key in (
                'firmware', 'mtu', 'interval_ms', 'rx_phy', 'tx_phy', 'rx_octets',
                'rssi', 'serial_baud', 'queue', 'last_error',
            )
        }
        if any(result['recovered_serial_counter_deltas'].values()) or result['base_retry_delta']:
            warnings.append(
                'USB retries/outer-CRC recovery occurred; validate the recording for loss or duplicates'
            )
    result['continuous_rate_verified'] = not errors
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8000')
    parser.add_argument('--device', required=True)
    parser.add_argument('--duration', type=float, default=120)
    parser.add_argument('--interval', type=float, default=2)
    parser.add_argument('--expected-rate', type=int, default=10000)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.duration < 2 or args.interval <= 0 or args.expected_rate <= 0:
        parser.error('duration must be at least 2 seconds; interval and expected-rate must be positive')
    device = args.device.upper()
    if len(device) != 12 or any(character not in '0123456789ABCDEF' for character in device):
        parser.error('device must be a 12-character hexadecimal node identity')
    if args.output.exists():
        parser.error(f'Refusing to overwrite existing evidence: {args.output}')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    evidence = {
        'started_utc': datetime.now(timezone.utc).isoformat(),
        'url': args.url.rstrip('/'), 'device': device, 'rows': [],
    }

    def save():
        temporary = args.output.with_suffix(args.output.suffix + '.tmp')
        temporary.write_text(json.dumps(evidence, indent=2), encoding='utf-8')
        temporary.replace(args.output)

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    started = time.monotonic()
    try:
        while True:
            before = time.monotonic()
            row = {'elapsed_s': before - started}
            try:
                with opener.open(evidence['url'] + '/api/live', timeout=5) as response:
                    snapshot = json.load(response)
                if not isinstance(snapshot, dict):
                    raise ValueError('Live response is not a JSON object')
                row.update(
                    elapsed_s=(before + time.monotonic()) / 2 - started,
                    snapshot=snapshot,
                )
                node = snapshot.get('nodes', {}).get(device, {})
                print(json.dumps({
                    'elapsed_s': round(row['elapsed_s'], 2),
                    'samples': node.get('samples'), 'rate': node.get('rate'),
                    'online': node.get('online'), 'epoch': node.get('epoch'),
                    'queued_blocks': node.get('status', {}).get('queued_blocks'),
                    'discontinuities': node.get('discontinuities'),
                    'receiver_errors': node.get('receiver_errors'),
                }), flush=True)
            except (OSError, urllib.error.URLError, ValueError) as error:
                row['error'] = str(error)
                print(json.dumps({'elapsed_s': row['elapsed_s'], 'error': str(error)}), flush=True)
            evidence['rows'].append(row)
            save()
            remaining = started + args.duration - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(args.interval, remaining))
    except KeyboardInterrupt:
        evidence['interrupted'] = True
    evidence['summary'] = summarize(evidence['rows'], device, args.expected_rate, args.duration)
    if evidence.get('interrupted'):
        evidence['summary']['errors'].append('Measurement interrupted')
        evidence['summary']['continuous_rate_verified'] = False
    evidence['finished_utc'] = datetime.now(timezone.utc).isoformat()
    save()
    print(json.dumps(evidence['summary'], indent=2), flush=True)
    return 0 if evidence['summary']['continuous_rate_verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

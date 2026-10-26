"""Check for an existing receiver or occupied ports before starting Uvicorn."""
import json
import os
import socket
import urllib.error
import urllib.request


def main():
    url = 'http://127.0.0.1:8000'
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url + '/api/health', timeout=3) as response:
            health = json.load(response)
        if (isinstance(health, dict) and health.get('ok') is True
                and 'tcp_port' in health and 'ble' in health
                and 'active_devices' in health):
            print('The MSAI receiver is already running. No second copy was started.')
            print('Open ' + url)
            print(f"Active devices: {health['active_devices']}")
            if 'ble_bases' not in health:
                print('Restart this receiver to load the new USB BLE base-station controls.')
            else:
                print(f"USB BLE bases: {len(health['ble_bases'])}; add a COM port in Receiver connections.")
            if os.getenv('MSAI_BLE_NAME') and health['ble'] == 'disabled':
                print('Direct laptop BLE is disabled in the running receiver. Restart it to apply MSAI_BLE_NAME.')
            else:
                print('The running receiver keeps its current settings.')
            return 2
    except (OSError, urllib.error.URLError, ValueError):
        pass

    ports = [('127.0.0.1', 8000)]
    if os.getenv('MSAI_TCP_ENABLED', '1') != '0':
        try:
            port = int(os.getenv('MSAI_TCP_PORT', '5443'))
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            print('MSAI_TCP_PORT must be a number from 1 to 65535.')
            return 1
        ports.append((os.getenv('MSAI_TCP_BIND', '0.0.0.0'), port))
    for host, port in ports:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind((host, port))
        except OSError:
            print(f'Cannot start the receiver: {host}:{port} is unavailable.')
            print('Close the other receiver using this port, then try again.')
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

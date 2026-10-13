"""Decode a compressed recording to exact integer sample counts in CSV."""
import argparse
import csv
from pathlib import Path
from protocol import HEADER, MAX_PAYLOAD, unpack

def export(source, destination):
    total = 0
    with source.open('rb') as src, destination.open('x', newline='', encoding='utf-8') as dst:
        out = csv.writer(dst)
        out.writerow(['epoch', 'sample_index', 'node_uptime_us', 'selectors']+[f'ch{c}_counts' for c in range(8)])
        while header := src.read(HEADER.size):
            if len(header) != HEADER.size:
                raise ValueError('Truncated header')
            size = HEADER.unpack(header)[-1]
            if size > MAX_PAYLOAD:
                raise ValueError('Oversized frame')
            meta, samples = unpack(header+src.read(size+4))
            if isinstance(samples, dict):
                continue
            for i, values in enumerate(samples):
                out.writerow([meta['epoch'], meta['first']+i, meta['time_us']+i*1000000//meta['rate'], meta['selectors']]+values)
                total += 1
    print(f'Exported {total} eight-channel samples to {destination}')

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('recording', type=Path)
    p.add_argument('csv', type=Path, help='New output file; existing files are never overwritten')
    args = p.parse_args()
    export(args.recording, args.csv)

#!/usr/bin/env python3
"""Repeat the same measured GPU workload after passive cooling."""
from datetime import datetime, timezone
import json
from pathlib import Path
import signal
import subprocess
import sys

BATCH = Path(__file__).resolve().parent
ROOT = BATCH.parents[2]
sys.path.insert(0, str(ROOT))
from quarch_capture.common import termination_guard, write_json_atomic

def now():
    return datetime.now(timezone.utc).isoformat()

@termination_guard()
def main():
    metadata = json.loads((BATCH / 'batch.json').read_text())
    if metadata['status'] != 'prepared':
        raise RuntimeError('Refusing to restart an existing batch')
    child = None
    metadata.update(status='running', started_at=now(), completed_runs=[])
    write_json_atomic(BATCH / 'batch.json', metadata)
    try:
        for index in range(1, 4):
            output = BATCH / f'r{index:02d}'
            output.mkdir()
            print(f'=== Repetition {index}/3 ===', flush=True)
            metadata['active_run'] = output.name
            write_json_atomic(BATCH / 'batch.json', metadata)
            child = subprocess.Popen([sys.executable, '-u', str(BATCH / 'run_one.py'), str(output)], cwd=ROOT)
            if child.wait() != 0:
                raise RuntimeError(f'Repetition {index} failed; inspect its logs')
            run = json.loads((output / 'run.json').read_text())
            if run['status'] != 'captured' or run.get('cleanup_errors') or run.get('telemetry_errors'):
                raise RuntimeError(f'Repetition {index} did not finish cleanly')
            metadata['completed_runs'].append(output.name)
            write_json_atomic(BATCH / 'batch.json', metadata)
        metadata.update(status='captured', active_run=None)
    except BaseException as exc:
        metadata.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        if child is not None and child.poll() is None:
            child.send_signal(signal.SIGTERM)
            try:
                child.wait(timeout=45)
            except subprocess.TimeoutExpired:
                metadata['cleanup_error'] = 'Run process did not exit after SIGTERM; inspect owned children.'
        metadata['finished_at'] = now()
        write_json_atomic(BATCH / 'batch.json', metadata)
    print('All three captures finished.', flush=True)

if __name__ == '__main__':
    main()

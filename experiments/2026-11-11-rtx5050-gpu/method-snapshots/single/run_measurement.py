#!/usr/bin/env python3
"""Run a verified FP32 GPU load and record 30 seconds of external power."""
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
sys.path.insert(0, str(ROOT))
from quarch_capture.common import termination_guard, write_json_atomic

FIELDS = ['pstate', 'utilization.gpu', 'utilization.memory', 'temperature.gpu',
          'power.draw', 'memory.used', 'clocks.current.graphics', 'clocks.current.memory']
QUERY = ['nvidia-smi', '--query-gpu=' + ','.join(FIELDS), '--format=csv,noheader,nounits']

def now():
    return datetime.now(timezone.utc).isoformat()

def snapshot():
    raw = subprocess.check_output(QUERY, text=True, timeout=10)
    rows = list(csv.reader(io.StringIO(raw)))
    if len(rows) != 1:
        raise RuntimeError('This run expects exactly one NVIDIA GPU')
    return {'at': now(), **dict(zip(FIELDS, (v.strip() for v in rows[0])))}

def finish_process(process, signum, timeout):
    if process is not None and process.poll() is None:
        process.send_signal(signum)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
            raise RuntimeError('Owned child did not shut down gracefully')

@termination_guard()
def main():
    if (OUT / 'run.json').exists():
        raise RuntimeError('Refusing to overwrite this experiment')
    existing = subprocess.check_output(['nvidia-smi', '--query-compute-apps=pid,process_name',
                                       '--format=csv,noheader'], text=True).strip()
    if existing:
        raise RuntimeError('Another GPU compute process is already running: ' + existing)
    metadata = {'status': 'preparing', 'created_at': now(), 'matrix_size': 4096,
                'operation': 'C = A * B repeated using cuBLAS SGEMM', 'dtype': 'FP32',
                'math_mode': 'CUBLAS_PEDANTIC_MATH', 'warmup_s': 5,
                'requested_duration_s': 30, 'output_interval_s': .1, 'expected_samples': 300,
                'device': 'USB::QTL2312-01-578', 'channel': 'Tot', 'before': snapshot(),
                'workload_source_sha256': hashlib.sha256((OUT / 'matmul_load.cu').read_bytes()).hexdigest(),
                'energy_scope': 'Total GPU slot and external supply energy during the 30-second load window; warmup excluded; no idle baseline subtraction; not energy of a single matrix multiply.',
                'telemetry_note': 'NVIDIA telemetry sampled about once per second for utilization/temperature checks. Quarch CSV supplies the power and energy results.'}
    write_json_atomic(OUT / 'run.json', metadata)
    worker = capture = monitor = None
    stop_monitor = threading.Event()
    telemetry_errors = []
    def telemetry():
        with (OUT / 'nvidia-telemetry.csv').open('x', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=['at', *FIELDS])
            writer.writeheader()
            while not stop_monitor.is_set():
                started = time.monotonic()
                try:
                    writer.writerow(snapshot())
                    handle.flush()
                except Exception as exc:
                    telemetry_errors.append(str(exc))
                stop_monitor.wait(max(.01, 1 - (time.monotonic() - started)))
    try:
        with (OUT / 'workload.log').open('x') as load_log:
            worker = subprocess.Popen([str(OUT / 'matmul_load'), '120', str(OUT / 'ready.json'),
                                       str(OUT / 'workload-result.json')], stdout=load_log, stderr=subprocess.STDOUT)
            metadata['workload_pid'] = worker.pid
            deadline = time.monotonic() + 30
            while True:
                if worker.poll() is not None:
                    raise RuntimeError('GPU workload exited during initialization; see workload.log')
                try:
                    ready = json.loads((OUT / 'ready.json').read_text())
                    if ready.get('ready'):
                        break
                except (FileNotFoundError, json.JSONDecodeError):
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError('GPU workload did not become ready')
                time.sleep(.1)
            metadata.update(status='warming_up', workload_ready=ready, workload_ready_at=now())
            write_json_atomic(OUT / 'run.json', metadata)
            print('Verified 4096 x 4096 FP32 matrix product; warming up for 5 seconds.', flush=True)
            monitor = threading.Thread(target=telemetry, daemon=True)
            monitor.start()
            time.sleep(5)
            metadata['load_before_capture'] = snapshot()
            if worker.poll() is not None or float(metadata['load_before_capture']['utilization.gpu']) < 50:
                raise RuntimeError('GPU load was not sustained before capture')
            command = [sys.executable, '-u', '-m', 'quarch_capture', '--device', metadata['device'],
                       '--channel', 'Tot', '--seconds', '30', '--resample', '100ms',
                       '--output', str(OUT / 'gpu-matmul.csv')]
            metadata.update(status='capturing', command=command, capture_command_started_at=now())
            write_json_atomic(OUT / 'run.json', metadata)
            print('GPU load active; starting 30-second Quarch capture at 100 ms.', flush=True)
            with (OUT / 'capture.log').open('x') as capture_log:
                capture = subprocess.Popen(command, cwd=ROOT, stdout=capture_log, stderr=subprocess.STDOUT)
                deadline = time.monotonic() + 90
                while capture.poll() is None:
                    if worker.poll() is not None:
                        raise RuntimeError('GPU workload exited before capture completed')
                    if time.monotonic() > deadline:
                        raise TimeoutError('Quarch capture exceeded its expected duration')
                    time.sleep(.25)
                if capture.returncode:
                    raise RuntimeError('Quarch capture failed; see capture.log')
            metadata.update(capture_command_finished_at=now(), load_after_capture=snapshot())
            if worker.poll() is not None:
                raise RuntimeError('GPU load did not cover the complete recording')
            finish_process(worker, signal.SIGTERM, 15)
            if worker.returncode:
                raise RuntimeError('GPU workload failed its final validation')
            metadata['workload_result'] = json.loads((OUT / 'workload-result.json').read_text())
            if not metadata['workload_result']['passed']:
                raise RuntimeError('Matrix calculation did not pass validation')
            metadata['status'] = 'captured'
    except BaseException as exc:
        metadata.update(status='failed', error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        for process, signum in [(capture, signal.SIGINT), (worker, signal.SIGTERM)]:
            try:
                finish_process(process, signum, 15)
            except Exception as exc:
                metadata.setdefault('cleanup_errors', []).append(str(exc))
                metadata['status'] = 'failed'
        stop_monitor.set()
        if monitor:
            monitor.join(timeout=12)
        metadata.update(finished_at=now(), telemetry_errors=telemetry_errors,
                        workload_exit_code=worker.returncode if worker else None,
                        capture_exit_code=capture.returncode if capture else None)
        write_json_atomic(OUT / 'run.json', metadata)
    print('Measurement and GPU workload finished:', OUT, flush=True)

if __name__ == '__main__':
    main()

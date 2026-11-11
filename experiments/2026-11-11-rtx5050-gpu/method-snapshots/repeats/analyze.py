#!/usr/bin/env python3
"""Validate three captures and compare their measured power and temperatures."""
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys

BATCH = Path(__file__).resolve().parent
ROOT = BATCH.parents[2]
sys.path.insert(0, str(ROOT))
from quarch_capture.common import write_json_atomic
from quarch_capture.energy import energy_from_csv

def stats(values):
    mean = statistics.mean(values)
    sd = statistics.stdev(values) if len(values) > 1 else 0
    return {'n': len(values), 'mean': mean, 'sample_stddev': sd,
            'cv_percent': 100 * sd / mean if mean else None,
            'min': min(values), 'max': max(values),
            'range_percent_of_mean': 100 * (max(values)-min(values)) / mean if mean else None}

def write_csv(path, rows):
    with path.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

def main():
    batch = json.loads((BATCH / 'batch.json').read_text())
    assert batch['completed_runs'] == ['r01', 'r02', 'r03'], batch
    assert batch['status'] in ('captured', 'complete'), batch
    source_hash = hashlib.sha256((BATCH / 'matmul_load.cu').read_bytes()).hexdigest()
    binary_hash = hashlib.sha256((BATCH / 'matmul_load').read_bytes()).hexdigest()
    assert source_hash == batch['source_sha256']
    assert binary_hash == batch['binary_sha256']
    rail_names = ['+12V', '+3.3V', '+3.3Vaux', '+12Vaux']
    rows, reports, traces = [], [], []
    for name in batch['completed_runs']:
        folder = BATCH / name
        run = json.loads((folder / 'run.json').read_text())
        assert run['status'] in ('captured', 'complete')
        assert run['workload_source_sha256'] == source_hash
        assert run['workload_binary_sha256'] == binary_hash
        assert run['matrix_size'] == 4096 and run['dtype'] == 'FP32'
        assert run['math_mode'] == 'CUBLAS_PEDANTIC_MATH' and run['warmup_s'] == 5
        assert run['capture_exit_code'] == 0 and run['workload_exit_code'] == 0
        assert not run.get('cleanup_errors') and not run['telemetry_errors']
        assert run['workload_result']['passed'] and run['workload_result']['max_abs_error'] == 0
        assert run['before']['pstate'] == 'P8' and float(run['before']['utilization.gpu']) == 0
        start_temp = float(run['before']['temperature.gpu'])
        assert abs(start_temp-batch['temperature_target_C']) <= batch['temperature_tolerance_C']
        raw_path = folder / 'gpu-matmul.all-channels.csv'
        with raw_path.open() as f:
            raw = list(csv.DictReader(f))
        channels = {}
        for rail in [*rail_names, 'Tot']:
            report = energy_from_csv(raw_path, channel=rail, expected_duration_s=30)
            assert report['samples'] == 300 and report['interval_s'] == .1
            assert report['requested_duration_matches']
            assert report['start_time_s'] == 0 and report['end_time_exclusive_s'] == 30
            channels[rail] = report[rail]
        values = {rail: [float(row[rail+' power uW'])/1e6 for row in raw]
                  for rail in [*rail_names, 'Tot']}
        traces.append(values['Tot'])
        residual = max(abs(float(row['Tot power uW']) -
                           sum(float(row[rail+' power uW']) for rail in rail_names)) for row in raw)
        assert residual <= 2, residual
        slot = [sum(values[rail][i] for rail in rail_names[:3]) for i in range(300)]
        channels['PCIe_slot_total'] = {'mean_power_W': statistics.mean(slot),
                                     'energy_J': math.fsum(slot)*.1}
        saved_energy = json.loads((folder/'gpu-matmul.energy.json').read_text())
        assert saved_energy['hardware_averaging_samples'] == 1
        assert saved_energy['analog_base_rates_Hz'] == {'0': 250000}
        selected = energy_from_csv(folder/'gpu-matmul.csv', channel='Tot', expected_duration_s=30)
        assert selected['samples'] == 300 and selected['requested_duration_matches']
        assert math.isclose(selected['Tot']['energy_J'], channels['Tot']['energy_J'], abs_tol=1e-8)
        assert math.isclose(saved_energy['Tot']['energy_J'], channels['Tot']['energy_J'], abs_tol=1e-8)
        with (folder/'nvidia-telemetry.csv').open() as f:
            telemetry_all = list(csv.DictReader(f))
        telemetry = [row for row in telemetry_all if
                     run['capture_command_started_at'] <= row['at'] <= run['capture_command_finished_at']]
        assert len(telemetry) >= 30
        def tele(field):
            return [float(row[field]) for row in telemetry]
        during = {'samples': len(telemetry), 'pstates': sorted({r['pstate'] for r in telemetry}),
                  'utilization_percent': stats(tele('utilization.gpu')),
                  'temperature_C': stats(tele('temperature.gpu')),
                  'nvidia_power_draw_W': stats(tele('power.draw')),
                  'graphics_clock_MHz': stats(tele('clocks.current.graphics')),
                  'memory_clock_MHz': stats(tele('clocks.current.memory')),
                  'fan_percent': stats(tele('fan.speed')),
                  'power_limit_W': sorted(set(tele('power.limit'))),
                  'scope': 'Approximately 1 Hz telemetry during the capture command, including QIS startup/shutdown around the exact 30 s hardware recording; not precisely time aligned to Quarch samples.'}
        assert during['utilization_percent']['min'] >= 95
        total = channels['Tot']
        end_temp = float(run['load_after_capture']['temperature.gpu'])
        record = {
            'run': name,
            'start_temperature_C': start_temp,
            'after_5s_warmup_temperature_C': float(run['load_before_capture']['temperature.gpu']),
            'end_under_load_temperature_C': end_temp,
            'temperature_rise_C': end_temp-start_temp,
            'max_observed_load_temperature_C': max(end_temp, float(run['load_before_capture']['temperature.gpu']),
                                                   max(float(t['temperature.gpu']) for t in telemetry_all)),
            'mean_power_W': total['mean_power_W'],
            'energy_J': total['energy_J'], 'energy_Wh': total['energy_Wh'],
            'min_100ms_power_W': total['min_interval_mean_W'],
            'max_100ms_power_W': total['max_interval_mean_W'],
            'within_run_power_stddev_W': statistics.stdev(values['Tot']),
            'PCIe_slot_mean_W': channels['PCIe_slot_total']['mean_power_W'],
            'AUX_mean_W': channels['+12Vaux']['mean_power_W'],
            'NVIDIA_power_draw_mean_W': during['nvidia_power_draw_W']['mean'],
            'NVIDIA_minus_Quarch_mean_W': during['nvidia_power_draw_W']['mean']-total['mean_power_W'],
            'GPU_utilization_mean_percent': during['utilization_percent']['mean'],
            'GPU_utilization_min_percent': during['utilization_percent']['min'],
            'graphics_clock_mean_MHz': during['graphics_clock_MHz']['mean'],
            'fan_start_percent': float(run['before']['fan.speed']),
            'fan_end_percent': float(run['load_after_capture']['fan.speed']),
            'temperature_wait_s': run['temperature_wait_s'],
            'samples': 300, 'interval_s': .1, 'duration_s': 30,
        }
        rows.append(record)
        result = {'complete': True, 'run': name, 'measurement': record, 'channels': channels,
                  'total_minus_rails_max_abs_uW': residual, 'gpu_during_capture_command': during,
                  'workload_validation': run['workload_result'],
                  'fixture_identity': saved_energy['fixture_identity'],
                  'hardware_averaging_samples': saved_energy['hardware_averaging_samples'],
                  'analog_base_rates_Hz': saved_energy['analog_base_rates_Hz'],
                  'energy_scope': run['energy_scope']}
        reports.append(result)
        write_json_atomic(folder/'summary.json', result)
        write_csv(folder/'summary.csv', [record])
        run['status'] = 'complete'
        write_json_atomic(folder/'run.json', run)
    write_csv(BATCH/'comparison.csv', rows)
    write_csv(BATCH/'power-traces.csv', [
        {'interval_start_s': i/10, 'r01_total_W': traces[0][i],
         'r02_total_W': traces[1][i], 'r03_total_W': traces[2][i]}
        for i in range(300)])
    result = {
        'complete': True, 'validated_at': datetime.now(timezone.utc).isoformat(),
        'runs': rows, 'total_samples': sum(r['samples'] for r in rows),
        'mean_power_W_between_runs': stats([r['mean_power_W'] for r in rows]),
        'energy_J_between_runs': stats([r['energy_J'] for r in rows]),
        'temperature_rise_C_between_runs': stats([r['temperature_rise_C'] for r in rows]),
        'temperature_start_range_C': [min(r['start_temperature_C'] for r in rows), max(r['start_temperature_C'] for r in rows)],
        'temperature_end_range_C': [min(r['end_under_load_temperature_C'] for r in rows), max(r['end_under_load_temperature_C'] for r in rows)],
        'source_sha256': source_hash, 'binary_sha256': binary_hash,
        'method': 'Three new runs of the same deterministic 4096x4096 FP32 cuBLAS SGEMM binary, 5 s warmup, 30 s Quarch Tot capture, 100 ms interval means, native 250 kHz analog acquisition, full-rate hardware averaging (1 sample). Energy = sum(power * 0.1 s).',
        'energy_scope': reports[0]['energy_scope'],
        'statistics_scope': 'Sample standard deviation and CV are calculated across the three independent run means, not across all 900 power points. Equal durations mean energy CV equals mean-power CV. Three runs describe observed short-term repeatability, not measurement accuracy or long-term thermal steady state.',
        'temperature_scope': 'Start = immediately before launching GPU workload. End = under load after capture command finishes. 5 s warmup and QIS startup/shutdown are outside the exact 30 s integration window. NVIDIA telemetry approximately 1 Hz. GPU temperature/fan reported by the driver; ambient and heatsink temperatures not measured.',
        'fan_policy': 'Default automatic fan control unchanged. Start/end fan percentages are preserved for each run; automatic fan hysteresis can produce different fan speeds at similar GPU temperatures.',
        'measurement_discrepancy': 'NVIDIA power.draw and Quarch measured rail sum differ substantially. Preserve both without correction. Stable repeats do not resolve physical power-path coverage or absolute calibration.',
        'reference_experiment_not_in_statistics': batch['reference_experiment'],
    }
    write_json_atomic(BATCH/'summary.json', result)
    batch.update(status='complete', analyzed_at=result['validated_at'])
    write_json_atomic(BATCH/'batch.json', batch)
    print(json.dumps(result, indent=2, ensure_ascii=False))

if __name__ == '__main__':
    main()

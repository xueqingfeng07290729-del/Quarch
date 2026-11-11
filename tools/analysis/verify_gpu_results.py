#!/usr/bin/env python3
"""Verify the published GPU captures offline, without CUDA, QIS or hardware."""

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from quarch_capture.energy import energy_from_csv
from tools.analysis.verify_published_results import close, local_path, read_csv, read_json, require

RAILS = ['+12V', '+3.3V', '+3.3Vaux', '+12Vaux']


def archive_reference(archive, parent, relative):
    return local_path(archive, parent.relative_to(archive) / relative)


def verify_inventory(folder):
    listed = set()
    for line in (folder / 'SHA256SUMS').read_text().splitlines():
        digest, relative = line.split('  ', 1)
        require(relative not in listed, f'Duplicate checksum: {relative}')
        path = local_path(folder, relative)
        require(hashlib.sha256(path.read_bytes()).hexdigest() == digest, f'Checksum mismatch: {relative}')
        listed.add(relative)
    actual = {str(p.relative_to(folder)) for p in folder.rglob('*')
              if p.is_file() and p.name != 'SHA256SUMS'}
    require(listed == actual, 'Archive checksum inventory mismatch')
    manifest = read_json(folder / 'manifest.json')
    source_paths = set()
    for item in manifest['files']:
        require(item['path'] not in source_paths, 'Duplicate manifest entry')
        source_paths.add(item['path'])
        digest = hashlib.sha256(local_path(folder, item['path']).read_bytes()).hexdigest()
        require(digest == item['published_sha256'], f'Manifest mismatch: {item["path"]}')
        if Path(item['path']).suffix in ('.csv', '.xml', '.cu', '.py'):
            require(digest == item['source_sha256'], f'Raw evidence changed: {item["path"]}')
    require(listed - source_paths == {'README.md', 'manifest.json', 'overview.csv'},
            'Unexpected generated or untracked archive files')
    return manifest, len(listed)


def verify_fixture(path):
    tree = ET.fromstring(path.read_text())
    def params(node):
        return {p.findtext('Name'): p.findtext('Value') for p in node.findall('Param')}
    require(params(tree.find('General'))['FixtureStatus'] == 'OK', 'Fixture not OK')
    groups = {p['Number']: p for p in (params(n) for n in tree.findall('./Groups/Group'))}
    require(groups['0']['SampleAveraging'] == '0', 'Analog hardware averaging was not restored')


def verify_capture(archive, capture):
    folder = local_path(archive, capture['folder'])
    stem = capture['stem']
    saved = read_json(folder / f'{stem}.energy.json')
    selected_path = folder / f'{stem}.csv'
    full_path = folder / f'{stem}.all-channels.csv'
    require(local_path(folder, saved['source_csv']) == selected_path, 'Broken selected CSV reference')
    require(local_path(folder, saved['all_channels_csv']) == full_path, 'Broken full CSV reference')
    require(saved['selected_channel'] == 'Tot' and saved['requested_duration_matches'], 'Incomplete capture')
    require(saved['hardware_averaging_samples'] == 1 and saved['analog_base_rates_Hz'] == {'0': 250000},
            'Unexpected analog sampling configuration')
    integrated = {}
    for channel in [*RAILS, 'Tot']:
        data = energy_from_csv(full_path, channel=channel, expected_duration_s=capture['duration_s'])
        require(data['samples'] == capture['samples'] and close(data['interval_s'], capture['interval_s']),
                f'Sampling mismatch: {folder}')
        require(data['requested_duration_matches'] and close(data['start_time_s'], 0), 'Incomplete time span')
        integrated[channel] = data[channel]
    selected = energy_from_csv(selected_path, channel='Tot', expected_duration_s=capture['duration_s'])
    require(selected['samples'] == capture['samples'] and selected['requested_duration_matches'], 'Selected CSV incomplete')
    require(saved['samples'] == capture['samples'] and close(saved['duration_s'], capture['duration_s'])
            and close(saved['interval_s'], capture['interval_s']), 'Saved energy sampling mismatch')
    for key in integrated['Tot']:
        require(close(integrated['Tot'][key], saved['Tot'][key]), f'Saved Tot mismatch: {key}')
        require(close(integrated['Tot'][key], selected['Tot'][key]), f'Selected Tot mismatch: {key}')
    raw = read_csv(full_path)
    residual = max(abs(float(r['Tot power uW']) - sum(float(r[c+' power uW']) for c in RAILS)) for r in raw)
    require(residual <= 2, 'Rail sum does not match Tot within integer-uW rounding')
    power = [float(r['Tot power uW'])/1e6 for r in raw]
    if capture['folder'] == 'hardware-check':
        summary = read_json(folder / 'channel-summary.json')
        channels = summary['rails']
        require(summary['gpu_driver_bound'] is False and summary['record_state_after'] == 'OFF', 'Hardware-check state mismatch')
    else:
        summary = read_json(folder / 'summary.json')
        require(summary['complete'], 'Incomplete summary')
        channels = summary['channels']
        if isinstance(channels, list):
            channels = {c['channel']: c for c in channels}
        run = read_json(folder / 'run.json')
        require(run['status'] == 'complete' and run['expected_samples'] == capture['samples'], 'Incomplete run')
        require(close(run['output_interval_s'], capture['interval_s']), 'Run interval mismatch')
        require(close(run['requested_duration_s'], capture['duration_s']), 'Run duration mismatch')
    for channel in [*RAILS, 'Tot']:
        for key, value in integrated[channel].items():
            require(close(value, channels[channel][key]), f'Channel summary mismatch: {folder}/{channel}/{key}')
    slot_mean = sum(integrated[c]['mean_power_W'] for c in RAILS[:3])
    if capture['folder'] == 'hardware-check':
        require(close(slot_mean, summary['pcie_slot_mean_power_W']), 'Hardware slot sum mismatch')
    else:
        require(close(slot_mean, channels['PCIe_slot_total']['mean_power_W']), 'Slot mean mismatch')
        require(close(slot_mean*capture['duration_s'], channels['PCIe_slot_total']['energy_J']), 'Slot energy mismatch')
    require(close(summary['total_minus_rails_max_abs_uW'], residual), 'Rail rounding summary mismatch')
    if (folder / 'fixture-after.xml').exists():
        verify_fixture(folder / 'fixture-after.xml')
    if capture['folder'] in ('idle', 'matmul-single'):
        for row in read_csv(folder / 'summary.csv'):
            for field in ('mean_power_W', 'energy_J'):
                require(close(row[field], channels[row['channel']][field]), 'Channel CSV/JSON mismatch')
        require(summary['record_state_after'] == 'OFF', 'Recording left active')
    return {'energy': integrated, 'power': power, 'summary': summary}


def verify_workload(folder, source_hash):
    run = read_json(folder / 'run.json')
    require(run['workload_source_sha256'] == source_hash, 'Matrix source hash mismatch')
    require(run['matrix_size'] == 4096 and run['dtype'] == 'FP32'
            and run['math_mode'] == 'CUBLAS_PEDANTIC_MATH' and run['warmup_s'] == 5, 'Workload configuration mismatch')
    require(run['capture_exit_code'] == 0 and run['workload_exit_code'] == 0, 'Failed workload/capture')
    require(not run['telemetry_errors'] and not run.get('cleanup_errors'), 'Run errors')
    result = read_json(folder / 'workload-result.json')
    ready = read_json(folder / 'ready.json')
    require(result == run['workload_result'] and ready == run['workload_ready'], 'Workload record mismatch')
    require(result['passed'] and result['verification_entries_each'] == 64 and result['max_abs_error'] == 0,
            'Matrix validation failed')
    require(ready['ready'] and ready['verified_entries'] == 64 and ready['max_abs_error'] == 0, 'Initial validation failed')
    telemetry = [r for r in read_csv(folder / 'nvidia-telemetry.csv') if
                 run['capture_command_started_at'] <= r['at'] <= run['capture_command_finished_at']]
    require(len(telemetry) >= 30, 'Missing telemetry')
    require(min(float(r['utilization.gpu']) for r in telemetry) >= 99, 'Low GPU utilization')
    return run, telemetry


def verify(archive):
    manifest, files = verify_inventory(archive)
    captures = manifest['capture_sets']
    expected = {'hardware-check': (500, .01, 5), 'idle': (120, .5, 60),
                'matmul-single': (300, .1, 30),
                **{f'matmul-repeats/r{i:02d}': (300, .1, 30) for i in range(1, 4)}}
    require(len(captures) == manifest['capture_count'] == 6, 'Capture count mismatch')
    require({c['folder'] for c in captures} == set(expected), 'Capture set mismatch')
    for c in captures:
        require((c['samples'], c['interval_s'], c['duration_s']) == expected[c['folder']], 'Unexpected capture settings')
    reports = {c['folder']: verify_capture(archive, c) for c in captures}
    require(sum(c['samples'] for c in captures) == manifest['samples_total'] == 1820, 'Total sample count mismatch')
    overview = read_csv(archive / 'overview.csv')
    require(len(overview) == 6 and {r['capture'] for r in overview} == set(expected), 'Overview capture mismatch')
    for row in overview:
        total = reports[row['capture']]['energy']['Tot']
        for field in ('mean_power_W', 'energy_J'):
            require(close(row[field], total[field]), 'Overview power/energy mismatch')
        n, interval, duration = expected[row['capture']]
        require(int(row['samples']) == n and close(row['interval_s'], interval)
                and close(row['duration_s'], duration), 'Overview sampling mismatch')

    source_hash = hashlib.sha256((archive / 'workload/matmul_load.cu').read_bytes()).hexdigest()
    repeat = archive / 'matmul-repeats'
    batch = read_json(repeat / 'batch.json')
    summary = read_json(repeat / 'summary.json')
    require(batch['status'] == 'complete' and summary['complete'], 'Incomplete repeats')
    require(batch['completed_runs'] == ['r01', 'r02', 'r03'] and batch['runs_requested'] == 3, 'Missing repetitions')
    require(summary['total_samples'] == 900 and len(summary['runs']) == 3, 'Repeat summary count mismatch')
    require(batch['source_sha256'] == summary['source_sha256'] == source_hash, 'Batch source mismatch')
    require(archive_reference(archive, repeat, batch['reference_experiment']) == archive / 'matmul-single', 'Broken reference experiment')
    require(archive_reference(archive, repeat, summary['reference_experiment_not_in_statistics']) == archive / 'matmul-single', 'Broken reference summary')
    single = read_json(archive / 'matmul-single/summary.json')
    require(archive_reference(archive, archive / 'matmul-single', single['idle_comparison']['source']) == archive / 'idle/summary.json', 'Broken idle reference')
    comparison = read_csv(repeat / 'comparison.csv')
    require([r['run'] for r in comparison] == batch['completed_runs'], 'Comparison run order mismatch')
    for index, name in enumerate(batch['completed_runs']):
        folder = repeat / name
        run, telemetry = verify_workload(folder, source_hash)
        row = summary['runs'][index]
        one = reports['matmul-repeats/'+name]
        require(row == one['summary']['measurement'], 'Per-run/aggregate summary mismatch')
        require(row['run'] == name, 'Summary run order mismatch')
        require(run['workload_binary_sha256'] == batch['binary_sha256'] == summary['binary_sha256'], 'Binary identity mismatch')
        require(30 <= float(run['before']['temperature.gpu']) <= 32
                and run['before']['pstate'] == 'P8' and float(run['before']['utilization.gpu']) == 0, 'Invalid initial GPU condition')
        cooldown = read_csv(folder / 'cooldown.csv')
        require(cooldown[-1] == run['before'], 'Cooldown/start snapshot mismatch')
        expected_fields = {
            'start_temperature_C': float(run['before']['temperature.gpu']),
            'after_5s_warmup_temperature_C': float(run['load_before_capture']['temperature.gpu']),
            'end_under_load_temperature_C': float(run['load_after_capture']['temperature.gpu']),
            'mean_power_W': one['energy']['Tot']['mean_power_W'], 'energy_J': one['energy']['Tot']['energy_J'],
            'within_run_power_stddev_W': statistics.stdev(one['power']),
            'NVIDIA_power_draw_mean_W': statistics.mean(float(t['power.draw']) for t in telemetry),
            'GPU_utilization_mean_percent': statistics.mean(float(t['utilization.gpu']) for t in telemetry),
            'GPU_utilization_min_percent': min(float(t['utilization.gpu']) for t in telemetry),
            'graphics_clock_mean_MHz': statistics.mean(float(t['clocks.current.graphics']) for t in telemetry),
            'fan_start_percent': float(run['before']['fan.speed']),
            'fan_end_percent': float(run['load_after_capture']['fan.speed']),
            'AUX_mean_W': one['energy']['+12Vaux']['mean_power_W'],
            'PCIe_slot_mean_W': sum(one['energy'][r]['mean_power_W'] for r in RAILS[:3]),
        }
        expected_fields['temperature_rise_C'] = expected_fields['end_under_load_temperature_C']-expected_fields['start_temperature_C']
        expected_fields['NVIDIA_minus_Quarch_mean_W'] = expected_fields['NVIDIA_power_draw_mean_W']-expected_fields['mean_power_W']
        for field, value in expected_fields.items():
            require(close(row[field], value), f'Repeat statistic mismatch: {name}/{field}')
        per_run_csv = read_csv(folder / 'summary.csv')
        require(len(per_run_csv) == 1, 'Per-run CSV count mismatch')
        for saved in (per_run_csv[0], comparison[index]):
            require(set(saved) == set(row), 'Summary CSV columns mismatch')
            for key, value in row.items():
                require(saved[key] == value if isinstance(value, str) else close(saved[key], value), f'Summary CSV mismatch: {key}')
    for field, key in [('mean_power_W', 'mean_power_W_between_runs'), ('energy_J', 'energy_J_between_runs'),
                       ('temperature_rise_C', 'temperature_rise_C_between_runs')]:
        values = [row[field] for row in summary['runs']]
        mean, sd = statistics.mean(values), statistics.stdev(values)
        expected_stats = {'n': 3, 'mean': mean, 'sample_stddev': sd, 'cv_percent': 100*sd/mean,
                          'min': min(values), 'max': max(values), 'range_percent_of_mean': 100*(max(values)-min(values))/mean}
        for name, value in expected_stats.items():
            require(close(summary[key][name], value), f'Batch statistic mismatch: {key}/{name}')
    traces = read_csv(repeat / 'power-traces.csv')
    require(len(traces) == 300, 'Trace comparison count mismatch')
    for index, row in enumerate(traces):
        require(close(row['interval_start_s'], index*.1), 'Trace timestamp mismatch')
        for name in batch['completed_runs']:
            require(close(row[name+'_total_W'], reports['matmul-repeats/'+name]['power'][index]), 'Trace power mismatch')
    single_run, single_telemetry = verify_workload(archive / 'matmul-single', source_hash)
    require(close(single['power_source_comparison']['nvidia_power_draw_mean_W'],
                  statistics.mean(float(t['power.draw']) for t in single_telemetry)), 'Single-run NVIDIA mean mismatch')
    for row in overview:
        if row['capture'].startswith('matmul'):
            run = read_json(archive / row['capture'] / 'run.json')
            require(close(row['start_temperature_C'], run['before']['temperature.gpu'])
                    and close(row['end_temperature_C'], run['load_after_capture']['temperature.gpu']), 'Overview temperature mismatch')
    idle = read_json(archive / 'idle/run.json')
    for boundary in ('before', 'after'):
        require(not idle[boundary]['compute_processes'] and idle[boundary]['gpus'][0]['pstate'] == 'P8'
                and float(idle[boundary]['gpus'][0]['utilization.gpu']) == 0, 'Idle endpoint mismatch')
    postcheck = read_json(repeat / 'postcheck.json')
    require(summary['postcheck'] == postcheck and postcheck['record_state'] == 'OFF'
            and not postcheck['compute_processes'], 'Postcheck mismatch')
    verify_fixture(repeat / 'fixture-after.xml')
    return {'checksums_verified': files, 'complete_captures': 6, 'raw_samples': 1820,
            'matrix_calculation_checks_passed': 4, 'repeat_samples': 900,
            'repeat_mean_power_W': summary['mean_power_W_between_runs']['mean'],
            'repeat_mean_energy_J': summary['energy_J_between_runs']['mean'],
            'repeat_cv_percent': summary['mean_power_W_between_runs']['cv_percent'],
            'scope': 'Offline data integrity and consistency; no hardware access or calibration claim.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path, nargs='?', default=ROOT / 'experiments/2026-11-11-rtx5050-gpu')
    args = parser.parse_args()
    print(json.dumps(verify(args.archive.resolve()), indent=2))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, IndexError, ET.ParseError) as exc:
        print(f'Verification failed: {exc}', file=sys.stderr)
        raise SystemExit(1)

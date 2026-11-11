#!/usr/bin/env python3
"""Read-only post-run instrument checks and archive integrity manifest."""
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

BATCH = Path(__file__).resolve().parent
ROOT = BATCH.parents[2]
sys.path.insert(0, str(ROOT))
from quarch_capture.common import device_lock, write_json_atomic
from quarchpy.device import quarchDevice

def params(node):
    return {p.findtext('Name'): p.findtext('Value') for p in node.findall('Param')}

def main():
    summary = json.loads((BATCH/'summary.json').read_text())
    assert summary['complete'] and summary['total_samples'] == 900
    with device_lock('USB::QTL2312-01-578'):
        device = quarchDevice('USB:QTL2312-01-578')
        try:
            state = device.send_command('rec?').strip()
            fixture = device.send_command('fix:identity?').strip()
            xml = device.send_command('fix:chan:xml?')
        finally:
            device.close_connection()
    (BATCH/'fixture-after.xml').write_text(xml)
    tree = ET.fromstring(xml)
    general = params(tree.find('General'))
    groups = {p['Number']: p for p in (params(n) for n in tree.findall('./Groups/Group'))}
    assert state == 'OFF', state
    assert general['FixtureStatus'] == 'OK', general
    assert groups['0']['SampleAveraging'] == '0', groups
    compute = subprocess.check_output(['nvidia-smi','--query-compute-apps=pid,process_name',
                                     '--format=csv,noheader'],text=True,timeout=10).strip()
    assert not compute, compute
    fields = ['name','pstate','utilization.gpu','temperature.gpu','fan.speed','power.draw']
    raw = subprocess.check_output(['nvidia-smi','--query-gpu='+','.join(fields),
                                   '--format=csv,noheader,nounits'],text=True,timeout=10)
    gpu = dict(zip(fields,(v.strip() for v in next(csv.reader(io.StringIO(raw))))))
    check = {'checked_at': datetime.now(timezone.utc).isoformat(), 'record_state': state,
             'fixture_identity': fixture, 'fixture_status': general,
             'analog_group': groups['0'], 'compute_processes': compute, 'gpu': gpu}
    write_json_atomic(BATCH/'postcheck.json', check)
    summary['postcheck'] = check
    write_json_atomic(BATCH/'summary.json', summary)
    files = sorted(p for p in BATCH.rglob('*') if p.is_file() and
                   p.name != 'SHA256SUMS' and '__pycache__' not in p.parts)
    manifest = ''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+str(p.relative_to(BATCH))+'\n'
                       for p in files)
    (BATCH/'SHA256SUMS').write_text(manifest)
    print(json.dumps({'postcheck':check,'files_in_manifest':len(files)}, indent=2))

if __name__ == '__main__':
    main()

"""Versioned resource admission around an unchanged, pinned scientific entrypoint."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time

GUI_CAPS = {'/usr/bin/nautilus':64, '/opt/google/chrome/chrome':256}
LAYER_FILES = {'v8_resource_layer.py', 'supervisor.py', 'desktop-policy.json'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def configuration(path, source, home, expected_sha256=None):
    path, source, home = Path(path).resolve(), Path(source).resolve(), Path(home).resolve()
    if not path.is_relative_to(home / 'runtime-control'):
        raise ValueError('Resource layer escaped the owned runtime')
    if expected_sha256 is not None and sha(path) != expected_sha256:
        raise ValueError('Approved resource configuration hash mismatch')
    value = json.loads(path.read_text(encoding='utf8'))
    if value.get('schema_version') != 'v8-resource-admission-v1' or set(value.get('files', {})) != LAYER_FILES:
        raise ValueError('Unsupported resource layer contract')
    if value.get('scientific_source_manifest_sha256') != sha(source / 'source-manifest.json'):
        raise ValueError('Resource layer scientific source mismatch')
    for name, digest in value['files'].items():
        p = (path.parent / name).resolve()
        if not p.is_relative_to(path.parent) or sha(p) != digest:
            raise ValueError('Resource layer file mismatch: ' + name)
    policy = json.loads((path.parent / 'desktop-policy.json').read_text(encoding='utf8'))
    if type(policy.get('user_id')) is not int or policy['user_id'] < 1:
        raise ValueError('An explicit unprivileged desktop user is required')
    total = policy.get('maximum_total_desktop_mib')
    if type(total) not in (int, float) or not 0 < total <= 512:
        raise ValueError('Desktop aggregate memory budget must be at most 512MiB')
    if 'cached_models_only' in policy and type(policy['cached_models_only']) is not bool:
        raise ValueError('Cached model policy must be an explicit boolean')
    for entry in policy.get('additional_desktops', []):
        name, maximum = entry.get('executable'), entry.get('maximum_mib')
        if (name not in GUI_CAPS or type(maximum) not in (int, float)
                or not 0 < maximum <= GUI_CAPS[name]):
            raise ValueError('Unknown desktop executable or excessive memory allowance')
    return value, policy


def process_identity(pid):
    import psutil
    try:
        p = psutil.Process(pid)
        with p.oneshot():
            uids = p.uids()
            return {'executable':p.exe(), 'real_uid':uids.real, 'effective_uid':uids.effective,
                    'arguments':p.cmdline()}
    except psutil.Error as error:
        raise OSError('GPU process identity unavailable') from error


def configure_model_resolution(policy):
    """Set library cache mode before importing the frozen pipeline or HF libraries."""
    if policy.get('cached_models_only') is True:
        os.environ['HF_HUB_OFFLINE'] = '1'
        os.environ['TRANSFORMERS_OFFLINE'] = '1'


def additional_desktop_allowed(process, selected_uuid, policy, lookup=None, resolve=None):
    if process.get('gpu_uuid') != selected_uuid:
        return False
    lookup, resolve = lookup or process_identity, resolve or os.path.realpath
    try:
        memory = float(process['used_mib'])
        if not math.isfinite(memory) or memory < 0:
            return False
        info = lookup(int(process['pid']))
        if info['real_uid'] != policy['user_id'] or info['effective_uid'] != policy['user_id']:
            return False
        for entry in policy.get('additional_desktops', []):
            executable, maximum = entry.get('executable'), entry.get('maximum_mib')
            if (executable not in GUI_CAPS or type(maximum) not in (int, float)
                    or not 0 < maximum <= GUI_CAPS[executable] or memory > maximum):
                continue
            if resolve(info['executable']) != resolve(executable):
                continue
            if executable == '/opt/google/chrome/chrome' and '--type=gpu-process' not in info['arguments']:
                continue
            return True
    except (OSError, ValueError, TypeError, KeyError):
        return False
    return False


def install(common, policy, audit, execution_sha256):
    """Change only the resource predicate; preserve all scientific callables."""
    original = common.gpu_process_allowed
    def admitted(process, selected_uuid, resource_policy, current_pid=None,
                 executable_lookup=None, resolve_path=None):
        if original(process, selected_uuid, resource_policy, current_pid, executable_lookup, resolve_path):
            return True
        allowed = additional_desktop_allowed(process, selected_uuid, policy)
        if allowed:
            try:
                allowed = desktop_budget_allowed(nvidia_processes(), selected_uuid, policy, original,
                                                 resource_policy, current_pid)
            except (OSError, ValueError, subprocess.SubprocessError):
                allowed = False
        if allowed:
            event = {'observed_at':time.time(), 'pid':int(process['pid']), 'gpu_uuid':selected_uuid,
                     'used_mib':float(process['used_mib']), 'rule':'same_user_small_verified_desktop',
                     'execution_sha256':execution_sha256}
            with audit.open('a', encoding='utf8') as stream:
                stream.write(json.dumps(event, sort_keys=True) + '\n')
        return allowed
    common.gpu_process_allowed = admitted
    return original


def nvidia_processes():
    result = subprocess.run(['nvidia-smi','--query-compute-apps=pid,gpu_uuid,used_memory',
                             '--format=csv,noheader,nounits'],check=True,capture_output=True,text=True,timeout=5)
    rows = []
    for line in result.stdout.splitlines():
        fields = [v.strip() for v in line.split(',')]
        if len(fields) != 3:
            raise ValueError('GPU process inventory cannot be verified')
        rows.append(dict(zip(('pid','gpu_uuid','used_mib'), fields)))
    return rows


def desktop_budget_allowed(rows, selected_uuid, policy, original, resource_policy, current_pid=None, lookup=None):
    total = 0.
    for row in rows:
        if row['gpu_uuid'] != selected_uuid or int(row['pid']) == (current_pid or os.getpid()):
            continue
        if not original(row, selected_uuid, resource_policy, current_pid) and not additional_desktop_allowed(
                row, selected_uuid, policy, lookup=lookup):
            return False
        memory = float(row['used_mib'])
        if not math.isfinite(memory) or memory < 0:
            return False
        total += memory
    return total <= policy['maximum_total_desktop_mib']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--configuration', type=Path, required=True)
    parser.add_argument('--configuration-sha256', required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--stage', required=True)
    args = parser.parse_args()
    allowed_stages = {'probe','audit','smoke-ner','smoke-id','prepare','train-ner','train-id',
                      'train-relations','freeze','predict','verify-export'}
    if args.stage not in allowed_stages:
        raise ValueError('Unsupported resource-wrapped stage')
    _, policy = configuration(args.configuration, args.source, args.home, args.configuration_sha256)
    if policy['user_id'] != os.getuid():
        raise ValueError('Resource policy belongs to another user')
    configure_model_resolution(policy)
    os.environ['EVIDENCE_V8_HOME'] = str(args.home.resolve())
    sys.path.insert(0, str(args.source.resolve()))
    from nutri_literature.evidence_v8 import common
    audit = args.home / 'resource-admission-audit'
    audit.mkdir(exist_ok=True)
    original = install(common, policy, audit / (str(os.getpid()) + '.jsonl'), sha(args.configuration))
    entry = args.source / 'scripts/evidence_v8.py'
    sys.argv = [str(entry), args.stage, '--home', str(args.home)]
    try:
        runpy.run_path(str(entry), run_name='__main__')
    finally:
        common.gpu_process_allowed = original


if __name__ == '__main__':
    main()

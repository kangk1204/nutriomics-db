"""Read-only progress/working-set snapshot, without auth data or command lines."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    import psutil
    from nutri_literature.chatgpt_repair_io import HOME, AttemptBudget
    partitions = {phase: len(list((HOME / phase).glob('**/responses/*.json')))
                  for phase in ('reference', 'correction/train', 'correction/development')}
    workers = []
    for process in psutil.process_iter(['pid', 'cmdline', 'memory_info', 'cpu_times']):
        try:
            args = ' '.join(process.info['cmdline'] or []).replace('\\', '/')
            matches = [name for name in ('nutri_literature.chatgpt_repair_baseline', 'scripts/run_chatgpt_repair_v2.py',
                                        'scripts/continue_chatgpt_repair_v2.py', 'scripts/finish_chatgpt_repair_delivery.py') if name in args]
            if matches and process.info['memory_info']:
                workers.append({'pid': process.pid, 'task': matches[0],
                                'resident_mib': round(process.info['memory_info'].rss/1024**2, 1),
                                'cpu_seconds': round(sum(process.info['cpu_times'][:2]), 1)})
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    packaged = (HOME / 'delivery-manifest.json').exists()
    metric = json.loads((HOME / 'comparison.json').read_bytes())['arms']['combined']['metrics'] if packaged else None
    print(json.dumps({'budget': AttemptBudget().status(), 'completed_partitions': partitions,
                      'reference_scored': (HOME / 'reference/comparison.json').exists(),
                      'selection_frozen': (HOME / 'selection.json').exists(),
                      'development_scored': (HOME / 'comparison.json').exists(),
                      'completion_audit': (HOME / 'completion-audit.json').exists(),
                      'delivery_packaged': packaged, 'final_metrics': metric, 'workers': workers}, ensure_ascii=False))


if __name__ == '__main__':
    main()

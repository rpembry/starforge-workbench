"""Explicit operator CLI: one owned installed-model call, synthetic data only.

Run from the checkout with PYTHONPATH=src and provide existing runner, model,
and the designated durable private inference-slot directory. No model download,
live VM transport, service changes or outbound content release occurs.
"""
import argparse
import json
from pathlib import Path
import re
import tempfile
import time

from coordinator.supervisor import Supervisor
from starforge_workbench.diagnostic_batch import DiagnosticWorker
from starforge_workbench.diagnostic_vm_bridge import SupervisorDiagnosticFence
from starforge_workbench.local_qwen_synthetic import LocalQwen8BAdapter


class SyntheticAllocation:
    """Supervisor lifecycle fixture; creates no guest/process/container."""
    def validate(self, plan):
        if plan['profile_ref'] != 'synthetic' or plan['workspace_ref'] != 'synthetic':
            raise ValueError('Invalid synthetic allocation')

    def launch(self, plan):
        return 'synthetic-allocation'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runner', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--inference-slot', required=True, type=Path)
    args = parser.parse_args()
    adapter = LocalQwen8BAdapter(args.runner, args.model, inference_slot=args.inference_slot)
    with tempfile.TemporaryDirectory(prefix='sf-qwen-batch-') as temporary:
        root = Path(temporary)
        for name in ('supervisor', 'attempt'):
            (root/name).mkdir(mode=0o700)
        supervisor = Supervisor(root/'supervisor', SyntheticAllocation())
        generation = supervisor.acquire('synthetic-controller', lease_seconds=60)['generation']
        plan = {'job_id': 'synthetic-job', 'attempt_id': 'synthetic-attempt',
                'incarnation': 'synthetic-incarnation', 'worker_type': 'command',
                'profile_ref': 'synthetic', 'workspace_ref': 'synthetic',
                'payload': {'argv': ['true']}, 'deadline_seconds': 60,
                'orphan_policy': {'mode': 'strict', 'grace_seconds': 3, 'max_orphan_seconds': 0}}
        supervisor.launch(plan, controller='synthetic-controller', generation=generation,
                          operation_id='synthetic-allocation')
        fence = SupervisorDiagnosticFence(supervisor, job_id=plan['job_id'],
                  attempt_id=plan['attempt_id'], incarnation=plan['incarnation'],
                  controller='synthetic-controller', generation=generation)
        worker = DiagnosticWorker(root/'attempt', plan['job_id'], plan['attempt_id'], fence.ownership)
        request = {'protocol': 'diagnostic.batch.v1', 'job_id': plan['job_id'],
                   'attempt_id': plan['attempt_id'], 'operation_id': 'synthetic-diagnostic',
                   'diagnostic_type': 'capacity.v1', 'target_id': 'synthetic-1',
                   'parameters': {}, 'deadline': time.time()+60,
                   'budget': {'steps': 1, 'max_output_bytes': 4096}}
        # Long inference is outside the global supervisor lock; the worker probes
        # canonical ownership before and after, and release would require its fence.
        status = worker.run(request, adapter)
        evidence = {'status': status, 'runtime': adapter.evidence,
                    'model_invocations_requested': 1, 'outbound_content_releases': 0,
                    'vm_transport': 'mock_allocation_only'}
        if status['status'] == 'report_ready':
            report = json.loads(worker.local_report())
            evidence['known_finding_correct'] = any(
                re.search(r'\bdeficit\s*[:=]?\s*2\b', item, re.I) for item in report['findings'])
        # Detailed model report remains local; stdout contains public-safe aggregates.
        print(json.dumps(evidence, sort_keys=True))


if __name__ == '__main__':
    main()

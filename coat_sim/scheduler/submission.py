"""Trusted release policy and thin submission adapters shared by every surface."""
from copy import deepcopy
import hashlib
from coat_sim.core.api import canonical_compute_parameters
from coat_sim.core.compute_tasks import ComputeTaskStore
from coat_sim.core.compute_estimation import input_file_fingerprints
from . import SchedulerClient, freeze_spec

def submit_confirmed(service, kind, parameters, client_request_id=None, *, estimate_id=None):
    parameters = canonical_compute_parameters(service, kind, parameters)
    store = ComputeTaskStore(service.root)
    if estimate_id is None:
        receipt = service.estimate_compute_task(kind, parameters)
        estimate_id = store.bind_request(client_request_id, receipt['estimate_id'])
        receipt = store.public_estimate(store.read_estimate(estimate_id))
        if receipt.get('job_id'): return SchedulerClient(service.root).status(receipt['job_id'])
        return {**receipt, 'state': 'prepared', 'needs_release_request': True,
                'needs_user_confirmation': False, 'release_policy': 'unbound',
                'next_action': 'request_compute_confirmation'}
    estimate_id = store.bind_request(client_request_id, estimate_id)
    receipt = store.read_estimate(estimate_id)
    client = SchedulerClient(service.root)
    if receipt.get('job_id'): return client.status(receipt['job_id'])
    receipt = store.authorize(estimate_id, kind, parameters, input_file_fingerprints(service, kind, parameters))
    freeze_service, freeze_parameters = service, parameters
    if kind == 'film_optimization':
        from coat_sim.core.film_optimization_job import deserialize_request
        from .result_capability import film_optimization_service
        freeze_service = film_optimization_service(service)
        freeze_parameters = {'request': deserialize_request(parameters['request'])}
    spec = freeze_spec(freeze_service, kind, freeze_parameters)
    store.authorize(estimate_id, kind, parameters, input_file_fingerprints(service, kind, parameters))
    job_id = hashlib.sha256(('confirmation:' + estimate_id).encode('utf-8')).hexdigest()[:32]
    metadata = {key: receipt[key] for key in ('summary','estimated_s','origin','estimate_id',
                'memory_estimate_label','memory_warning') if key in receipt}
    return client.submit(spec, job_id=job_id, metadata=metadata)

def confirm_and_submit(service, estimate_id, accepted, *, automatic=False):
    store = ComputeTaskStore(service.root)
    decision = store.decide(estimate_id, accepted, automatic=automatic)
    SchedulerClient(service.root).refresh_estimates()
    if not accepted: return decision
    receipt = store.read_estimate(estimate_id)
    return submit_confirmed(service, receipt['kind'], deepcopy(receipt['parameters']), estimate_id=estimate_id)

"""Local lost-ACK reconciliation uses the original frozen identity, never POSTs twice."""
import threading

import pytest

from coat_sim.scheduler.desktop import RemoteCancelEvent, submit_local_with_receipt
from coat_sim.scheduler.jobs import JobSpec
from coat_sim.scheduler.remote_client import RemoteSubmissionUnknown


@pytest.mark.parametrize("receipt", ["matching", "mismatched", "missing"])
def test_local_lost_ack_preserves_exact_id_and_pending_cancel(receipt):
    spec = JobSpec("probe", {}, {}).to_dict()
    fingerprint = JobSpec.parse_frozen(spec).fingerprint
    cancelled = threading.Event()

    class Client:
        transport = None
        submissions = []
        cancels = []
        def submit(self, value, *, job_id, metadata):
            self.submissions.append(job_id)
            raise OSError("accepted then response lost")
        def status(self, job_id):
            if receipt == "missing":
                raise OSError("cannot confirm receipt")
            return {"job_id": job_id, "input_digest": fingerprint if receipt == "matching" else "bad"}
        def cancel(self, job_id):
            self.cancels.append(job_id)
            cancelled.set()
            return {"cancel_requested": True}

    client = Client()
    event = RemoteCancelEvent(client, "frozen-job", pending_admission=True)
    event.set()
    assert not client.cancels
    if receipt == "matching":
        submit_local_with_receipt(client, spec, job_id="frozen-job", metadata={})
        event.mark_admitted()
        assert cancelled.wait(1)
        assert client.cancels == ["frozen-job"]
    else:
        with pytest.raises(RemoteSubmissionUnknown) as error:
            submit_local_with_receipt(client, spec, job_id="frozen-job", metadata={})
        assert error.value.job_id == "frozen-job"
        assert not client.cancels
    assert client.submissions == ["frozen-job"]

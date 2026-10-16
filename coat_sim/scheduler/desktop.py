"""Qt-free observers; their lifetime has no effect on submitted work."""
import threading
import time
import re


class RemoteCancelEvent(threading.Event):
    def __init__(self, client, job_id):
        super().__init__()
        self.client, self.job_id = client, job_id
        self._remote = bool(getattr(getattr(client, "transport", None),
                                    "remote_http", False))
        self._lock = threading.Lock()
        self._admitted = not self._remote
        self._cancel_started = False
        self.cancel_acknowledged = False
        self.cancel_error_code = None

    def mark_admitted(self):
        """Release a pre-admission cancel only after this exact ID is accepted."""
        if not self._remote:
            return
        with self._lock:
            self._admitted = True
            launch = self.is_set() and not self._cancel_started
            if launch:
                self._cancel_started = True
        if launch:
            self._launch_cancel()

    def replace_job_id_before_admission(self, job_id):
        """Reconcile a saved lost-ACK ID without creating a second job."""
        with self._lock:
            if self._admitted or self._cancel_started:
                raise RuntimeError("job_id_already_admitted")
            self.job_id = job_id

    def set(self):
        if not self._remote:
            self.client.cancel(self.job_id)
            super().set()
            return
        # A remote cancel may take an HTTP timeout. Record intent immediately;
        # never run that request on the Qt thread or ahead of POST admission.
        super().set()
        with self._lock:
            launch = self._admitted and not self._cancel_started
            if launch:
                self._cancel_started = True
        if launch:
            self._launch_cancel()

    def _launch_cancel(self):
        try:
            threading.Thread(target=self._cancel_remote, name="remote-job-cancel",
                             daemon=True).start()
        except Exception:
            # Worker allocation failure must not turn an already-admitted job
            # into a spurious submission error. A repeated click can retry.
            with self._lock:
                self._cancel_started = False
                self.cancel_error_code = "cancel_thread_start_failed"

    def _cancel_remote(self):
        # Cancel is idempotent by job ID. A bounded retry covers a brief
        # disconnect, but neither an acknowledgement nor a timeout is a
        # terminal cancelled state; the observer remains its only authority.
        for attempt in range(3):
            try:
                reply = self.client.cancel(self.job_id)
                self.cancel_acknowledged = bool(reply.get("cancel_requested")
                                                or reply.get("state") == "cancelled")
                self.cancel_error_code = None
                return
            except Exception as exc:
                code = getattr(exc, "code", None)
                self.cancel_error_code = code if isinstance(code, str) else "cancel_request_unknown"
                if attempt < 2:
                    time.sleep(.2 * (2 ** attempt))
        with self._lock:
            # A later user click may retry the same idempotent ID. The intent
            # stays set; only a terminal service record confirms cancellation.
            self._cancel_started = False


def resolve_unknown_submission(client, error, *, timeout_s=90, should_stop=None):
    """Recover only the original remote ID with its immutable input identity.

    The POST may still be in flight, so a temporary 404 is not a rejection and
    cannot authorize a new ID. This helper is called only on a background
    worker; no endpoint, token, or arbitrary HTTP message reaches the UI.
    """
    from coat_sim.scheduler.remote_client import RemoteServiceError, RemoteSubmissionUnknown
    if not isinstance(error, RemoteSubmissionUnknown):
        raise TypeError("expected_remote_submission_unknown")
    digest = error.expected_spec_sha256
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise RemoteServiceError("submission_identity_unavailable")
    deadline = time.monotonic() + timeout_s
    while True:
        if should_stop and should_stop():
            raise error
        try:
            status = client.status(error.job_id)
        except Exception:
            status = None
        if isinstance(status, dict):
            summary = status.get("input_summary")
            if (status.get("job_id") != error.job_id
                    or not isinstance(summary, dict)
                    or summary.get("external_spec_sha256") != digest
                    or summary.get("device_id") != error.device_id):
                raise RemoteServiceError("submission_receipt_mismatch")
            return status
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise error
        time.sleep(min(.5, remaining))

def observe_job(client, job_id, progress=None, *, disconnected=None, wait_s=1.0):
    previous = None
    def publish(job):
        nonlocal previous
        value = job.get('progress')
        if progress and value and value != previous:
            progress(value); previous = value

    if disconnected and disconnected():
        raise RuntimeError("observer_disconnected: admitted job continues")
    wait = getattr(client, 'wait_for_terminal', None)
    if callable(wait):
        job = wait(job_id, on_update=publish, should_stop=disconnected, wait_s=wait_s)
    else:
        # Preserve compatibility with small third-party/test clients.
        import time
        while True:
            if disconnected and disconnected():
                raise RuntimeError("observer_disconnected: admitted job continues")
            job = client.status(job_id)
            publish(job)
            if job.get('state') in ('succeeded', 'failed', 'cancelled'): break
            time.sleep(.2)
    if disconnected and disconnected():
        raise RuntimeError("observer_disconnected: admitted job continues")
    if job['state'] == 'succeeded':
        return job['result'] if 'result' in job else client.result(job_id)
    if job['state'] == 'cancelled': raise InterruptedError(job.get('error') or '计算已取消')
    if job['state'] == 'failed': raise RuntimeError(f"{job.get('error_code', 'failed')}: {job.get('error')}")
    raise RuntimeError("observer_stopped: admitted job continues")

"""Qt-free observers; their lifetime has no effect on submitted work."""
import threading
import time
import re
from typing import Callable


class RemoteCancelEvent(threading.Event):
    def __init__(self, client, job_id, *, pending_admission=False):
        super().__init__()
        self.client, self.job_id = client, job_id
        # Historical name: this flag selects asynchronous transport/lifetime
        # handling for remote jobs and opt-in pending local admission alike.
        self._remote = pending_admission or bool(getattr(getattr(client, "transport", None),
                                    "remote_http", False))
        self._lock = threading.Lock()
        self._admitted = not self._remote
        self._cancel_started = False
        self.cancel_acknowledged = False
        self.cancel_error_code = None
        self.on_cancel_status: Callable[[str, str], None] | None = None

    def _notify_cancel_status(self):
        callback = self.on_cancel_status
        if callback is not None:
            try:
                callback(self.job_id, self.cancel_error_code or "")
            except Exception:
                pass  # The observer may already have been destroyed.

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
            self._notify_cancel_status()

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
                self._notify_cancel_status()
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
        self._notify_cancel_status()


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

def observe_job(client, job_id, progress=None, *, disconnected=None, wait_s=1.0, display=None):
    previous = None
    previous_display = None
    identity = [None, None]
    def publish(job):
        nonlocal previous, previous_display
        value = job.get('progress')
        if progress and value and value != previous:
            progress(value); previous = value
        if display is not None:
            if not all(identity):
                identity[:] = [job.get('input_digest'), (job.get('film_display_config') or {}).get('generation')]
            if not identity[0] or not identity[1]:
                return
            from .film_display import validate_live_display_packet
            packet = validate_live_display_packet(job.get('film_display'), job_id=job_id,
                input_digest=identity[0], generation=identity[1])
            marker = None if packet is None else (packet['generation'], packet['control_sequence'], packet['display']['sequence'])
            if packet is not None and marker != previous_display:
                previous_display = marker
                try:
                    display(packet)
                except Exception:
                    pass  # A display observer has no ownership of admitted work.

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


def submit_local_with_receipt(client, spec, *, job_id, metadata):
    """Submit once; reconcile an ambiguous local ACK against frozen identity.

    Explicit scheduler ValueError rejections remain failures. Transport/startup
    errors may follow a committed POST, so only a matching persisted receipt
    authorizes admission and release of a pending cancellation. Never repost.
    """
    import json
    from .jobs import JobSpec
    from .remote_client import RemoteSubmissionUnknown

    try:
        return client.submit(spec, job_id=job_id, metadata=metadata)
    except Exception as exc:
        if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError):
            raise
        unknown = RemoteSubmissionUnknown(job_id)
        try:
            expected = JobSpec.parse_frozen(spec).fingerprint
            receipt = client.status(job_id)
            if (isinstance(receipt, dict) and receipt.get("job_id") == job_id
                    and receipt.get("input_digest") == expected):
                return receipt
        except Exception:
            pass
        raise unknown from None

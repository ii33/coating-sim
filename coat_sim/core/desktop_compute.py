"""Explicit desktop jobs share the persistent FIFO and cancellation protocol."""
import threading
import time

class QueueCancelEvent(threading.Event):
    def __init__(self, root, job_id):
        super().__init__()
        self.marker = root / 'artifacts/runtime/jobs' / (job_id + '.cancel')
        self.next_check = 0.0

    def is_set(self):
        if super().is_set():
            return True
        now = time.monotonic()
        if now >= self.next_check:
            self.next_check = now + .2
            if self.marker.exists():
                self.set()
        return super().is_set()

class execution_slot:
    """Shared FIFO lease without rewriting frozen service exceptions.

    contextlib's generator __exit__ assigns exception.__traceback__, which is
    incompatible with frozen ServiceError dataclasses and hides the real error.
    """
    def __init__(self, store, job_id, event):
        self.store, self.job_id, self.event = store, job_id, event
        self.acquired = False
        self.stop = threading.Event()
        self.keeper = None

    def __enter__(self):
        try:
            while not self.acquired:
                if self.event.is_set():
                    raise InterruptedError('排队任务已取消')
                self.acquired = self.store.acquire_execution(self.job_id)
                if not self.acquired:
                    self.event.wait(.2)
            if self.event.is_set():
                raise InterruptedError('排队任务已取消')
            def renew():
                while not self.stop.wait(5):
                    try:
                        if not self.store.acquire_execution(self.job_id):
                            self.event.set()
                            return
                    except Exception:
                        self.event.set()
                        return
            self.keeper = threading.Thread(target=renew, name='desktop-compute-lease', daemon=True)
            self.keeper.start()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type, exc, traceback):
        self.stop.set()
        if self.keeper:
            self.keeper.join(timeout=1)
        if self.acquired:
            self.store.release_execution(self.job_id)
            self.acquired = False
        return False

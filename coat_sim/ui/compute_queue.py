"""Read/cancel the shared queue from a standalone simulation window."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3
from typing import TYPE_CHECKING
from PySide6.QtCore import QObject, Property, QTimer, QUrl, Signal, Slot
from PySide6.QtQml import QQmlComponent, QQmlContext
from PySide6.QtQuick import QQuickWindow
from coat_sim.core.compute_tasks import queue_job_is_recent
from coat_sim.core.api import CoatService
from coat_sim.core.geometric_raytrace_result import (
    geometric_raytrace_summary_matches_result,
    validate_geometric_raytrace_result,
)
from coat_sim.scheduler import SchedulerClient
from coat_sim.scheduler.result_capability import (
    is_best_candidate_result, is_film_optimization_result,
    is_geometric_raytrace_result, is_synthesis_result, is_sensitivity_result,
    validate_film_optimization_result,
)
from coat_sim.ui.icons import window_icon

if TYPE_CHECKING:
    from coat_sim.ui.film_sensitivity_quick_controller import FilmSensitivityQuickController

_MAX_RAYTRACE_RESULT_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class _RayTraceQueueResult:
    artifact_path: str

_MISSING_RESULT_REASON = "队列记录缺少可验证的 result 结构，无法确认结果包。"


def _require_result_payload(job):
    if not isinstance(job, dict) or "result" not in job:
        raise ValueError(_MISSING_RESULT_REASON)
    result = job["result"]
    if not isinstance(result, dict):
        raise ValueError("队列 result 字段不是有效对象，无法确认结果包。")
    return result


def _read_queue_result(service, store, job_id, asset_path=None):
    """Dispatch a queue result by its typed product kind."""
    job = store.read_job(job_id)
    result = _require_result_payload(job)
    if is_sensitivity_result(result):
        if job.get('kind') != 'film_sensitivity':
            raise ValueError('Typed sensitivity result does not belong to a film_sensitivity queue job.')
        from coat_sim.core.film_sensitivity import validate_film_sensitivity_result_payload
        from coat_sim.core.film_sensitivity_jobs import (
            deserialize_job_payload, result_matches_job, terminal_reply,
        )
        from coat_sim.scheduler.jobs import JobSpec
        if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', job_id):
            raise ValueError('Invalid sensitivity queue identity.')
        root = Path(store.root).resolve()
        directory = (root / 'artifacts/runtime/scheduler/jobs' / job_id).resolve()
        spec_path = directory / 'spec.json'
        if (not directory.is_relative_to(root) or spec_path.is_symlink()
                or not spec_path.resolve().is_relative_to(directory)):
            raise ValueError('Sensitivity queue input identity escaped its job directory.')
        try:
            spec = JobSpec.parse_frozen_json(spec_path.read_text(encoding='utf-8'))
        except (OSError, TypeError, ValueError) as exc:
            raise ValueError('Sensitivity queue input identity is unavailable or invalid.') from exc
        if (spec.executor != 'film_sensitivity' or spec.fingerprint != job.get('input_digest')
                or not result_matches_job(result, spec.parameters.get('payload'))):
            raise ValueError('Sensitivity result is not bound to this frozen queue input identity.')
        if job.get('state') in ('failed', 'cancelled'):
            result = terminal_reply(result, job['state'])
        return validate_film_sensitivity_result_payload(result,
            expected_request=deserialize_job_payload(spec.parameters['payload']), replay=True), ''
    if is_synthesis_result(result):
        if job.get('kind') != 'film_synthesis':
            raise ValueError('Typed synthesis result does not belong to a film_synthesis queue job.')
        return deepcopy(result), ''
    if (job.get('state') in ('failed', 'cancelled')
            and not is_best_candidate_result(result)):
        raise ValueError(job.get('result_unavailable_reason')
                         or 'No verified candidate is available for this job.')
    if is_film_optimization_result(result):
        return validate_film_optimization_result(service, result), ''
    if (result.get('kind') == 'geometric_raytrace'
            or result.get('result_schema') == 'coat_sim.geometric_raytrace_result'):
        if not is_geometric_raytrace_result(result):
            raise ValueError('The geometric ray-trace result summary is invalid.')
        if job.get('state') != 'succeeded':
            raise ValueError('A geometric ray-trace result is available only for a succeeded job.')
        result = store.result(job_id)
        if not is_geometric_raytrace_result(result):
            raise ValueError('The geometric ray-trace result manifest is invalid.')
        artifact = next(item for item in result['result_artifacts']
                        if isinstance(item, dict) and item.get('role') == 'result')
        if type(artifact.get('bytes')) is not int or not 0 <= artifact['bytes'] <= _MAX_RAYTRACE_RESULT_BYTES:
            raise ValueError('The geometric ray-trace result artifact exceeds its byte limit.')
        path = store.materialize_artifact(artifact['artifact_id'], artifact)
        if path.stat().st_size != artifact['bytes']:
            raise ValueError('The downloaded geometric ray-trace artifact size does not match its manifest.')
        try:
            downloaded = json.loads(path.read_text(encoding='utf-8'))
            validate_geometric_raytrace_result(downloaded)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(f'The downloaded geometric ray-trace result is invalid: {exc}') from exc
        if not geometric_raytrace_summary_matches_result(result, downloaded):
            raise ValueError('The downloaded geometric ray-trace result does not match its queue summary.')
        return _RayTraceQueueResult(str(path)), str(path)
    path = Path(asset_path) if asset_path else store.result_asset_path(job_id)
    expected_run = (result.get('asset') or {}).get('run_id') or result.get('run_id')
    if not isinstance(expected_run, str) or not re.fullmatch(r'[0-9a-f]{32}', expected_run):
        raise ValueError('Queue result is missing a valid run_id.')
    loaded = service.load_simulation_asset(path)
    if loaded.get('run_id') != expected_run:
        raise ValueError('Queue asset identity does not match its result record.')
    return loaded, str(path)


def _recent_expiry_at(row):
    if row.get('state') not in ('succeeded', 'failed', 'cancelled'):
        return None
    finished = row.get('finished_at')
    if not isinstance(finished, str):
        return None
    try:
        timestamp = datetime.fromisoformat(finished)
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    # The cutoff includes the exact 24-hour boundary; expire just after it.
    return timestamp.astimezone(timezone.utc) + timedelta(hours=24, microseconds=1)


def _next_recent_expiry(rows):
    expiries = [expiry for row in rows if (expiry := _recent_expiry_at(row)) is not None]
    return min(expiries, default=None)


def _visible_queue_rows(bucket, rows, now=None):
    if bucket != 'recent':
        return rows
    now = now or datetime.now(timezone.utc)
    return [row for row in rows if queue_job_is_recent(row, now)]


def _annotate_result_availability(root, rows):
    """Mark terminal rows whose successful result or best candidate can be opened."""
    root = Path(root).resolve()
    run_ids = set()
    for row in rows:
        if row.get('state') not in ('succeeded', 'failed', 'cancelled'):
            continue
        try:
            result = _require_result_payload(row)
        except ValueError as exc:
            row['resultAvailable'] = False
            row['resultUnavailableReason'] = row.get('result_unavailable_reason') or str(exc)
            continue
        if is_film_optimization_result(result):
            row['resultAvailable'] = True
            row['resultUnavailableReason'] = ''
            continue
        if is_sensitivity_result(result):
            row['resultAvailable'] = row.get('kind') == 'film_sensitivity'
            row['resultUnavailableReason'] = ('' if row['resultAvailable'] else
                'Typed sensitivity result does not belong to this job kind.')
            continue
        if is_synthesis_result(result):
            if row.get('kind') != 'film_synthesis':
                row['resultAvailable'] = False
                row['resultUnavailableReason'] = 'Typed synthesis result does not belong to this job kind.'
                continue
            row['resultAvailable'] = True
            row['resultUnavailableReason'] = ''
            continue
        if is_geometric_raytrace_result(result):
            row['resultAvailable'] = True
            row['resultUnavailableReason'] = ''
            continue
        if (result.get('kind') == 'geometric_raytrace'
                or result.get('result_schema') == 'coat_sim.geometric_raytrace_result'):
            row['resultAvailable'] = False
            row['resultUnavailableReason'] = 'The geometric ray-trace result summary is invalid.'
            continue
        if row.get('state') in ('failed', 'cancelled') and not is_best_candidate_result(result):
            row['resultAvailable'] = False
            row['resultUnavailableReason'] = (
                row.get('result_unavailable_reason')
                or '此任务没有可验证的最佳候选，无法打开结果。')
            continue
        asset = result.get('asset') or {}
        run_id = asset.get('run_id') or result.get('run_id')
        if isinstance(run_id, str) and re.fullmatch(r'[0-9a-f]{32}', run_id):
            run_ids.add(run_id)

    saved_paths = {}
    database = root / 'data' / 'simulation_history.sqlite3'
    if run_ids and database.is_file():
        connection = None
        try:
            connection = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
            connection.execute('PRAGMA query_only=ON')
            marks = ','.join('?' for _ in run_ids)
            saved_paths = dict(connection.execute(
                f'SELECT run_id,asset_path FROM simulations WHERE run_id IN ({marks})',
                tuple(run_ids)))
        except sqlite3.Error:
            saved_paths = {}
        finally:
            if connection is not None:
                connection.close()

    for row in rows:
        if row.get('state') not in ('succeeded', 'failed', 'cancelled'):
            continue
        try:
            result = _require_result_payload(row)
        except ValueError as exc:
            row['resultAvailable'] = False
            row['resultUnavailableReason'] = row.get('result_unavailable_reason') or str(exc)
            continue
        if is_film_optimization_result(result):
            row['resultAvailable'] = True
            row['resultUnavailableReason'] = ''
            continue
        if is_sensitivity_result(result):
            row['resultAvailable'] = row.get('kind') == 'film_sensitivity'
            row['resultUnavailableReason'] = ('' if row['resultAvailable'] else
                'Typed sensitivity result does not belong to this job kind.')
            continue
        if is_synthesis_result(result):
            if row.get('kind') != 'film_synthesis':
                row['resultAvailable'] = False
                row['resultUnavailableReason'] = 'Typed synthesis result does not belong to this job kind.'
                continue
            row['resultAvailable'] = True
            row['resultUnavailableReason'] = ''
            continue
        if is_geometric_raytrace_result(result):
            row['resultAvailable'] = True
            row['resultUnavailableReason'] = ''
            continue
        if (result.get('kind') == 'geometric_raytrace'
                or result.get('result_schema') == 'coat_sim.geometric_raytrace_result'):
            row['resultAvailable'] = False
            row['resultUnavailableReason'] = 'The geometric ray-trace result summary is invalid.'
            continue
        if row.get('state') in ('failed', 'cancelled') and not is_best_candidate_result(result):
            row['resultAvailable'] = False
            row['resultUnavailableReason'] = (
                row.get('result_unavailable_reason')
                or '此任务没有可验证的最佳候选，无法打开结果。')
            continue
        asset = result.get('asset') or {}
        artifacts = result.get('result_artifacts') or []
        if any(item.get('role') == 'bundle' and item.get('artifact_id')
               for item in artifacts if isinstance(item, dict)):
            row['resultAvailable'] = True
            row['resultUnavailableReason'] = ''
            continue

        run_id = asset.get('run_id') or result.get('run_id')
        candidates = [asset.get('temporary_bundle_path')]
        if isinstance(run_id, str) and re.fullmatch(r'[0-9a-f]{32}', run_id):
            candidates.append(root / 'data' / 'tmp' / 'simulations' / run_id / 'result.coatasset')
            candidates.append(saved_paths.get(run_id))
        local_path = None
        for raw in candidates:
            if not raw:
                continue
            try:
                path = Path(raw).resolve()
                recorded_copy = isinstance(run_id, str) and raw == saved_paths.get(run_id)
                if path.is_file() and (path.is_relative_to(root) or recorded_copy):
                    local_path = path
                    break
            except (OSError, TypeError, ValueError):
                continue
        row['resultAvailable'] = local_path is not None
        row['resultUnavailableReason'] = ('' if local_path is not None else
            '结果资产包已不存在；任务历史仍保留，不能仅凭摘要恢复计算结果。')
        if local_path is not None:
            row['resultAssetPath'] = str(local_path)


class ComputeQueueView(QObject):
    PAGE_SIZE = 12
    changed = Signal()
    received = Signal(object, str)
    cancelReceived = Signal(str, object, str)
    filmOptimizationResultReady = Signal(str, object)
    filmSynthesisResultReady = Signal(str, object)
    rayTraceResultReady = Signal(str, str)
    def __init__(self, engine, root, *, project_context=None, navigation=None):
        super().__init__(engine)
        self.project_context = project_context
        self._project_compute = None
        self._project_operation = None
        if project_context is not None:
            from coat_sim.core.project_compute import ProjectComputeWorkspace
            self._project_compute = ProjectComputeWorkspace.for_project(
                project_context.host, project_context.binding)
            if Path(root).resolve() != self._project_compute.root:
                raise ValueError("Queue root differs from its bound project")
            self._project_operation = self._project_compute.capture("queue-view")
        from coat_sim.scheduler import SchedulerClient
        self.store = SchedulerClient(root)
        self._jobs_by_bucket = {'recent': [], 'history': []}
        self._cursors = {'recent': None, 'history': None}
        self._has_more = {'recent': False, 'history': False}
        self._pages_loaded = {'recent': 0, 'history': 0}
        self._next_recent_expiry = None
        self._active_bucket = 'recent'
        self._status = ''
        self._closed, self._fetching = False, False
        self._loading = False
        self._pending_cancels = set()
        self._confirmed_cancels = set()
        self._pending_request = None
        self._result_viewer = None
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='simulation-queue-view')
        self.received.connect(self._receive)
        self.cancelReceived.connect(self._cancel_received)
        self.context = QQmlContext(engine.rootContext())
        self.context.setContextProperty('computeQueue', self)
        self.component = QQmlComponent(engine, QUrl.fromLocalFile(str(Path(__file__).parent / 'qml/ComputeQueueWindow.qml')))
        self.window = self.component.createWithInitialProperties(
            {"projectNavigation": navigation}, self.context)
        if self.window is None:
            raise RuntimeError('\n'.join(str(error) for error in self.component.errors()))
        self.window.setIcon(window_icon('main'))
        self.timer = QTimer(self); self.timer.setInterval(500)
        self.timer.timeout.connect(self._poll_queue)
    def _project_current(self):
        return (self._project_compute is None or
                (self._project_operation is not None
                 and self._project_compute.accepts(self._project_operation)))

    def _owns_job(self, row):
        if self.project_context is None:
            return True
        origin = row.get("origin") if isinstance(row, dict) else None
        return (isinstance(origin, dict)
                and origin.get("project_id") == self.project_context.binding.project_id
                and origin.get("project_root") == str(self.project_context.binding.root))

    def _require_owned_job(self, job_id):
        if self.project_context is None:
            return None
        if not self._project_current():
            raise ValueError("来源项目已关闭或重新打开")
        row = self.store.read_job(job_id)
        if not self._owns_job(row):
            raise ValueError("此任务不属于当前项目")
        return row

    @Property('QVariantList', notify=changed)
    def jobs(self):
        rows = self._jobs_by_bucket[self._active_bucket]
        # Never render a stale cached terminal row as recent after its cutoff.
        visible = _visible_queue_rows(self._active_bucket, rows)
        return visible[:self.PAGE_SIZE] if self._active_bucket == 'recent' else visible
    @Property(str, notify=changed)
    def status(self): return self._status
    @Property(bool, notify=changed)
    def hasMoreJobs(self):
        # Recent is a bounded summary; only older history is paginated.
        return self._active_bucket == 'history' and self._has_more['history']
    @Property(bool, notify=changed)
    def loading(self): return self._loading or bool(self._pending_cancels)
    @Property('QVariantList', notify=changed)
    def pendingCancelJobIds(self):
        return sorted(self._pending_cancels | self._confirmed_cancels)
    @Property(str, notify=changed)
    def activeBucket(self): return self._active_bucket
    def show(self):
        self.window.showNormal(); self.window.raise_(); self.window.requestActivate()
        self.timer.start(); self.refreshQueue()
    @Slot()
    def refreshQueue(self):
        if self._closed or self._fetching:
            return
        # Explicit refresh starts a new page range. Polling keeps pages the
        # user has loaded and never changes the selected tab.
        self._pages_loaded[self._active_bucket] = 0
        self._poll_queue()
    def _poll_queue(self):
        if self._closed:
            return
        if not self.window.isVisible():
            self.timer.stop(); return
        self._expire_recent_cache()
        self._fetch_page(self._active_bucket, before=None, append=False)

    def _expire_recent_cache(self, now=None):
        now = now or datetime.now(timezone.utc)
        if self._next_recent_expiry is None or now < self._next_recent_expiry:
            return False
        recent, expired = [], []
        for row in self._jobs_by_bucket['recent']:
            if row['state'] in ('succeeded', 'failed', 'cancelled') and not self._is_recent_job(row, now):
                expired.append(row)
            else:
                recent.append(row)
        if not expired:
            self._next_recent_expiry = _next_recent_expiry(recent)
            return False
        self._jobs_by_bucket['recent'] = recent
        history = self._jobs_by_bucket['history']
        history.extend(expired)
        history.sort(key=lambda row: (
            row['state'] not in ('queued', 'running'),
            row.get('queue_order', 0) if row['state'] in ('queued', 'running')
            else -row.get('queue_order', 0)))
        self._next_recent_expiry = _next_recent_expiry(recent)
        self.changed.emit()
        return True
    @Slot(int)
    def selectQueueTab(self, index):
        bucket = 'recent' if int(index) == 0 else 'history'
        if bucket == self._active_bucket:
            return
        self._active_bucket = bucket
        self.changed.emit()
        # A row can move here when an active task completes or ages out of the
        # recent window. That cached row does not mean this bucket's persistent
        # first page (and cursor) has been loaded yet.
        if self._pages_loaded[bucket] == 0:
            self._fetch_page(bucket, before=None, append=False)
    @Slot()
    def loadMoreQueueJobs(self):
        bucket = self._active_bucket
        if self._has_more[bucket] and self._cursors[bucket] is not None:
            self._fetch_page(bucket, before=self._cursors[bucket], append=True)
    def _fetch_page(self, bucket, *, before, append):
        if self._closed or self._fetching or not self.window.isVisible():
            return
        self._fetching = self._loading = True
        self._pending_request = (bucket, append)
        self.changed.emit()
        active_ids = [row['job_id'] for row in self._jobs_by_bucket['recent']
                      if row['state'] in ('queued', 'running')]
        future = self.pool.submit(self._read_page, bucket, before, active_ids)
        def finished(item):
            try: value, error = item.result(), ''
            except Exception as exc: value, error = {}, str(exc)
            if not self._closed: self.received.emit(value, error)
        future.add_done_callback(finished)
    def _read_page(self, bucket, before, active_ids=()):
        page = self.store.queue_jobs_page(bucket, before=before, limit=self.PAGE_SIZE)
        # Preserve every active task in the backing model even though the recent
        # tab displays a bounded 12-row summary. The active snapshot refreshes
        # statuses without changing the durable queue records.
        rows = {job['job_id']: job for job in page['jobs'] + page.pop('active_jobs', [])}
        needs_active = bucket == 'recent' or bool(active_ids)
        snapshot = self.store.snapshot(limit=1) if needs_active else {'jobs': []}
        active = {job['job_id']: job for job in snapshot['jobs']
                  if job['state'] in ('queued', 'running')}
        for job in rows.values():
            if job['state'] in ('queued', 'running') and job['job_id'] in active:
                job.update(active[job['job_id']])
            elif job['state'] in ('succeeded', 'failed', 'cancelled'):
                job['expected_finish_at'] = job.get('finished_at')
        rows = {identity: row for identity, row in rows.items() if self._owns_job(row)}
        active = {identity: row for identity, row in active.items() if self._owns_job(row)}
        page['jobs'] = list(rows.values())
        refresh_jobs = []
        tracked_ids = set(active_ids) | {job['job_id'] for job in rows.values()
                                        if job['state'] in ('queued', 'running')}
        if bucket == 'recent':
            tracked_ids.update(active)
        for job_id in tracked_ids:
            if job_id in active:
                refresh_jobs.append(active[job_id])
                continue
            try:
                current = self.store.read_job(job_id)
            except ValueError:
                continue
            if not self._owns_job(current):
                continue
            current = {key: value for key, value in current.items()
                       if not key.startswith('_')}
            current['expected_finish_at'] = current.get('finished_at')
            refresh_jobs.append(current)
        page['refresh_jobs'] = refresh_jobs
        _annotate_result_availability(self.store.root, page['jobs'] + refresh_jobs)
        return page
    _is_recent_job = staticmethod(queue_job_is_recent)
    @Slot(object, str)
    def _receive(self, value, error):
        bucket, append = self._pending_request or (self._active_bucket, False)
        self._pending_request = None
        self._fetching = self._loading = False
        if self._closed or not self._project_current():
            return
        from coat_sim.ui.assistant_desktop import duration_label, finish_label
        names = {'queued':'等待开始', 'running':'计算中', 'succeeded':'已完成',
                 'failed':'失败', 'cancelled':'已取消'}
        if error:
            self._status = error
        else:
            finish_prefixes = {
                'succeeded': '完成时间：', 'failed': '失败时间：',
                'cancelled': '取消时间：',
            }
            incoming = [dict(row) for row in
                        (value.get('jobs', []) + value.get('refresh_jobs', []))]
            by_id = {row['job_id']: row for name in ('recent', 'history')
                     for row in self._jobs_by_bucket[name]
                     if name != bucket or append or self._pages_loaded[bucket] > 1
                     or row['state'] in ('queued', 'running')}
            by_id.update((row['job_id'], {**by_id.get(row['job_id'], {}), **row})
                         for row in incoming)
            # Rebuild display fields for every row after merging cache and page
            # data. A row restored from another bucket or an older daemon may
            # lack the presentation keys expected by QML.
            for row in by_id.values():
                prefix = finish_prefixes.get(row['state'], '预计完成：')
                label = finish_label(row.get('expected_finish_at') or row.get('finished_at'))
                row.update(
                    stateLabel=names.get(row['state'], row['state']),
                    durationLabel=duration_label(row.get('estimated_s')),
                    finishLabel=label,
                    finishLabelPrefix=prefix,
                    finishTimeText=prefix + label,
                )
            for job_id in tuple(self._confirmed_cancels):
                row = by_id.get(job_id)
                if row and (row.get('cancel_requested') or
                            row.get('state') in ('succeeded', 'failed', 'cancelled')):
                    self._confirmed_cancels.discard(job_id)
            self._jobs_by_bucket = {'recent': [], 'history': []}
            now = datetime.fromisoformat(value['as_of']) if value.get('as_of') else datetime.now(timezone.utc)
            for row in by_id.values():
                target = 'recent' if self._is_recent_job(row, now) else 'history'
                self._jobs_by_bucket[target].append(row)
            for rows in self._jobs_by_bucket.values():
                rows.sort(key=lambda row: (
                    row['state'] not in ('queued', 'running'),
                    row.get('queue_order', 0) if row['state'] in ('queued', 'running')
                    else -row.get('queue_order', 0)))
            self._next_recent_expiry = _next_recent_expiry(self._jobs_by_bucket['recent'])
            if not append:
                if self._pages_loaded[bucket] <= 1:
                    self._cursors[bucket] = value.get('next_cursor')
                    self._has_more[bucket] = bool(value.get('has_more'))
                self._pages_loaded[bucket] = max(1, self._pages_loaded[bucket])
            else:
                self._cursors[bucket] = value.get('next_cursor')
                self._has_more[bucket] = bool(value.get('has_more'))
                self._pages_loaded[bucket] += 1
        self.changed.emit()
        if (self._pages_loaded[self._active_bucket] == 0 and
                self._active_bucket != bucket):
            self._fetch_page(self._active_bucket, before=None, append=False)
    @Slot(str)
    def cancelQueueJob(self, job_id):
        if self._closed or job_id in self._pending_cancels:
            return
        self._pending_cancels.add(job_id)
        self._status = '正在请求取消…'
        self.changed.emit()
        def cancel_owned():
            self._require_owned_job(job_id)
            return self.store.request_cancel(job_id)
        future = self.pool.submit(cancel_owned)
        def finished(item):
            try: result, error = item.result(), ''
            except Exception as exc: result, error = None, str(exc)
            if not self._closed:
                self.cancelReceived.emit(job_id, result, error)
        future.add_done_callback(finished)

    @Slot(str, object, str)
    def _cancel_received(self, job_id, result, error):
        self._pending_cancels.discard(job_id)
        if self._closed:
            return
        if not error and result:
            self._confirmed_cancels.add(job_id)
        else:
            self._confirmed_cancels.discard(job_id)
        self._status = error or ('后台已确认取消请求。' if result else '任务已结束。')
        self.changed.emit()
        self.refreshQueue()
    @Slot(str)
    def openQueueResult(self, job_id):
        try:
            self._require_owned_job(job_id)
            row = next((item for bucket in self._jobs_by_bucket.values() for item in bucket
                        if item.get('job_id') == job_id), None)
            if row is not None and row.get('resultAvailable') is False:
                self._status = row.get('resultUnavailableReason') or '结果资产包不可用。'
                self.changed.emit()
                return
            if self._result_viewer is None:
                self._result_viewer = QueueResultViewer(
                    self, self.store.root, project_context=self.project_context)
                self._result_viewer.statusChanged.connect(self._result_status)
                self._result_viewer.availabilityChanged.connect(self._result_availability_changed)
                self._result_viewer.filmOptimizationResultReady.connect(
                    self.filmOptimizationResultReady.emit)
                self._result_viewer.filmSynthesisResultReady.connect(
                    self.filmSynthesisResultReady.emit)
                self._result_viewer.rayTraceResultReady.connect(
                    self.rayTraceResultReady.emit)
            self._result_viewer.open(job_id, row.get('resultAssetPath') if row else None)
        except Exception as exc:
            self._result_status('无法打开结果：' + str(exc))

    @Slot(str)
    def _result_status(self, message):
        self._status = message
        self.changed.emit()

    @Slot(str, bool, str)
    def _result_availability_changed(self, job_id, available, reason):
        for rows in self._jobs_by_bucket.values():
            for row in rows:
                if row.get('job_id') == job_id:
                    row['resultAvailable'] = bool(available)
                    row['resultUnavailableReason'] = reason
        self.changed.emit()

    def shutdown(self):
        self._closed = True; self.timer.stop(); self.pool.shutdown(wait=False, cancel_futures=True)
        if self._result_viewer is not None:
            self._result_viewer.shutdown()
        self.window.close()


class QueueResultViewer(QObject):
    """Load a persisted queue asset off the UI thread into an independent view.

    An independent engine avoids replacing the running simulation's image provider
    or frozen job state. Repeated clicks reuse the same viewer and discard stale
    loads; the sole input is the persisted run identity, never current settings.
    """
    loaded = Signal(int, object, str, str)
    statusChanged = Signal(str)
    availabilityChanged = Signal(str, bool, str)
    filmOptimizationResultReady = Signal(str, object)
    filmSynthesisResultReady = Signal(str, object)
    rayTraceResultReady = Signal(str, str)

    def __init__(self, owner, root, *, project_context=None):
        super().__init__(owner)
        from PySide6.QtQml import QQmlApplicationEngine
        from coat_sim.ui.main import MainBridge
        self.store = SchedulerClient(root)
        self.project_context = project_context
        self.service: CoatService
        self._project_compute = None
        self._project_operation = None
        if project_context is not None:
            from coat_sim.core.project_compute import ProjectComputeWorkspace
            self._project_compute = ProjectComputeWorkspace.for_project(
                project_context.host, project_context.binding)
            self.service = self._project_compute.service
            self._project_operation = self._project_compute.capture("queue-result-view")
        else:
            self.service = CoatService(root, restrict_to_root=False)
        self.bridge = MainBridge(self.service, project_context=project_context, result_view_only=True)
        self.bridge.setParent(self)
        self.bridge._result_view_only = True
        self.engine = QQmlApplicationEngine(self)
        self.engine.addImageProvider('thickness', self.bridge.image_provider)
        self.engine.rootContext().setContextProperty('bridge', self.bridge)
        from coat_sim.ui.theme_service import ThemeService

        ThemeService.instance().attach_engine(self.engine)
        self.engine.load(QUrl.fromLocalFile(str(Path(__file__).parent / 'qml/MainWindow.qml')))
        if not self.engine.rootObjects():
            raise RuntimeError('无法创建结果查看窗口')
        self.window = self.engine.rootObjects()[0]
        self.window.setProperty('titleBase', '计算队列 · 已完成结果')
        if project_context is not None and owner.window is not None:
            # Reuse the queue's live project-name signal without exposing the
            # result viewer to the project's module-navigation behavior.
            navigation = owner.window.property('projectNavigation')
            if navigation is not None:
                self.window.setProperty('titleProjectNameSource', navigation)
        self.window.setIcon(window_icon('main'))
        self.window.hide()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='queue-result-reader')
        self._closed, self._generation, self._loading_job, self._shown_job = False, 0, '', ''
        self._shown_kind, self._shown_film_result = '', None
        self._shown_raytrace_path = ''
        self._sensitivity_window: QQuickWindow | None = None
        self._sensitivity_controller: "FilmSensitivityQuickController | None" = None
        self._sensitivity_component: QQmlComponent | None = None
        self._future = None
        self.loaded.connect(self._receive)
        self.bridge.showQueueRequested.connect(owner.showQueue if hasattr(owner, 'showQueue') else owner.show)

    def open(self, job_id, asset_path=None):
        if self._closed or self._loading_job == job_id:
            return
        # Every new selection invalidates earlier work, even when returning to
        # the currently displayed asset while another asset is still loading.
        self._generation += 1
        generation = self._generation
        self._loading_job = ''
        if self._future is not None:
            self._future.cancel()
        if self._shown_job == job_id:
            if self._shown_kind == 'film_sensitivity' and self._shown_film_result is not None:
                self._show_sensitivity_result(job_id, self._shown_film_result)
                return
            if self._shown_kind == 'film_optimization' and self._shown_film_result is not None:
                self.filmOptimizationResultReady.emit(job_id, deepcopy(self._shown_film_result))
                return
            if self._shown_kind == 'film_synthesis' and self._shown_film_result is not None:
                self.filmSynthesisResultReady.emit(job_id, deepcopy(self._shown_film_result))
                return
            if self._shown_kind == 'geometric_raytrace' and self._shown_raytrace_path:
                self.rayTraceResultReady.emit(job_id, self._shown_raytrace_path)
                return
            if self._shown_kind == 'simulation' and self.bridge.hasResult:
                self.window.showNormal(); self.window.raise_(); self.window.requestActivate()
                return
        self._loading_job = job_id
        self.statusChanged.emit('正在读取已完成结果资产…')
        def read():
            if self._project_compute is not None:
                if (self._project_operation is None
                        or not self._project_compute.accepts(self._project_operation)):
                    raise ValueError("来源项目已关闭或重新打开")
                row = self.store.read_job(job_id)
                origin = row.get("origin") if isinstance(row, dict) else None
                if (not isinstance(origin, dict)
                        or origin.get("project_id") != self.project_context.binding.project_id
                        or origin.get("project_root") != str(self.project_context.binding.root)):
                    raise ValueError("此任务不属于当前项目")
            return _read_queue_result(self.service, self.store, job_id, asset_path)
        self._future = self.pool.submit(read)
        def finished(item):
            if item.cancelled():
                return
            try:
                value, path = item.result(); error = ''
            except Exception as exc:
                value, path, error = None, '', str(exc)
            if not self._closed:
                self.loaded.emit(generation, value, path, error)
        self._future.add_done_callback(finished)

    @Slot(int, object, str, str)
    def _receive(self, generation, loaded, path, error):
        if (self._closed or generation != self._generation
                or (self._project_compute is not None
                    and (self._project_operation is None
                         or not self._project_compute.accepts(self._project_operation)))):
            return
        job_id, self._loading_job = self._loading_job, ''
        if error:
            self.statusChanged.emit('无法打开结果：' + error)
            self.availabilityChanged.emit(
                job_id, False,
                '结果资产包不可用；队列历史仍保留，不能仅凭摘要恢复计算结果。')
            return
        if is_film_optimization_result(loaded):
            self._shown_job = job_id
            self._shown_kind = 'film_optimization'
            self._shown_film_result = deepcopy(loaded)
            self.filmOptimizationResultReady.emit(job_id, deepcopy(loaded))
            self.availabilityChanged.emit(job_id, True, '')
            self.statusChanged.emit('Film optimization candidate is ready for its typed result view.')
            return
        if is_synthesis_result(loaded):
            self._shown_job = job_id
            self._shown_kind = 'film_synthesis'
            self._shown_film_result = deepcopy(loaded)
            self.filmSynthesisResultReady.emit(job_id, deepcopy(loaded))
            self.availabilityChanged.emit(job_id, True, '')
            self.statusChanged.emit('Film synthesis candidate is ready for its read-only typed result view.')
            return
        if is_sensitivity_result(loaded):
            self._shown_job, self._shown_kind = job_id, 'film_sensitivity'
            self._shown_film_result = deepcopy(loaded)
            self._show_sensitivity_result(job_id, loaded)
            self.availabilityChanged.emit(job_id, True, '')
            self.statusChanged.emit('Opened a replay-validated sensitivity report; no source design is bound or modified.')
            return
        if isinstance(loaded, _RayTraceQueueResult):
            self._shown_job = job_id
            self._shown_kind = 'geometric_raytrace'
            self._shown_raytrace_path = loaded.artifact_path
            self.rayTraceResultReady.emit(job_id, loaded.artifact_path)
            self.availabilityChanged.emit(job_id, True, '')
            self.statusChanged.emit('Geometric ray-trace result is opening in its dedicated window.')
            return
        try:
            if not loaded.get('has_results', True):
                raise ValueError('资产包不含计算结果；不会重新计算')
            if self.bridge._apply_loaded_asset(loaded, path, queue_result_copy=True) is False:
                self._shown_job = ''
                raise ValueError(self.bridge.status)
            self._shown_job = job_id
            self._shown_kind = 'simulation'
            self._shown_film_result = None
            self._shown_raytrace_path = ''
            self.window.showNormal(); self.window.raise_(); self.window.requestActivate()
            self.statusChanged.emit('已打开持久化结果；没有重新计算')
        except Exception as exc:
            self.statusChanged.emit('无法显示结果：' + str(exc))

    def _show_sensitivity_result(self, job_id, payload):
        from coat_sim.ui.film_sensitivity_quick_controller import FilmSensitivityQuickController
        if self._sensitivity_window is None:
            if self._sensitivity_controller is None:
                self._sensitivity_controller = FilmSensitivityQuickController(self)
            self._sensitivity_component = QQmlComponent(self.engine, QUrl.fromLocalFile(
                str(Path(__file__).parent / 'qml/FilmSensitivityResultWindow.qml')))
            created = self._sensitivity_component.createWithInitialProperties(
                {'controller': self._sensitivity_controller})
            if not isinstance(created, QQuickWindow):
                if created is not None:
                    created.deleteLater()
                message = '\n'.join(error.toString() for error in self._sensitivity_component.errors())
                raise ValueError(message or 'The sensitivity report did not create a Qt Quick window.')
            self._sensitivity_window = created
            self._sensitivity_window.setIcon(window_icon('film_optimization'))
        assert self._sensitivity_controller is not None
        self._sensitivity_controller.load_verified_history(payload, job_id)
        self._sensitivity_window.showNormal()
        self._sensitivity_window.raise_()
        self._sensitivity_window.requestActivate()

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self._generation += 1
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.bridge.detach_observers()
        self.window.close()
        # Tear down QML bindings while their bridge is still alive.
        import shiboken6
        if self._sensitivity_controller is not None:
            self._sensitivity_controller.shutdown()
        if self._sensitivity_window is not None:
            self._sensitivity_window.close()
            shiboken6.delete(self._sensitivity_window)
        shiboken6.delete(self.window)
        shiboken6.delete(self.engine)


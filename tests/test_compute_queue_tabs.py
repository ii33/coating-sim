"""Actual Qt/QML queue pages backed by a temporary shared SQLite queue."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QT_QUICK_BACKEND', 'software')
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import time
import threading
import unittest
from unittest.mock import patch

from PySide6.QtCore import QObject, QMetaObject, QCoreApplication, QEvent
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtWidgets import QApplication
from PySide6.QtQuick import QQuickWindow
from coat_sim.ui.compute_queue import ComputeQueueView
from coat_sim.core.compute_tasks import ComputeTaskStore
from tests.scheduler_queue_support import SchedulerHarness

APP = QApplication.instance() or QApplication([])


class ComputeQueueTabsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = QQmlApplicationEngine()
        self.view = ComputeQueueView(self.engine, Path(self.tmp.name))
        self.assertIsInstance(self.view.window, QQuickWindow)
        self.view.timer.stop()
        # This window normally fetches only while visible. Show the actual QML
        # page without starting its periodic timer; each test controls refresh.
        self.view.window.show()
        self.tabs = self.view.window.findChild(QObject, 'queueTabs')
        self.now = datetime.now(timezone.utc)
        self.queue = ComputeTaskStore(Path(self.tmp.name))
        self.harness = None

    def tearDown(self):
        self.view.shutdown()
        self.view.pool.shutdown(wait=True)
        if self.harness is not None:
            self.harness.close()
        APP.processEvents()
        self.view.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        APP.processEvents()
        self.view.context.setContextProperty('computeQueue', None)
        self.view.component.deleteLater()
        self.view.context.deleteLater()
        self.view.deleteLater()
        self.engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        APP.processEvents()
        self.tmp.cleanup()

    def spin(self, predicate):
        deadline = time.monotonic() + 8
        while not predicate() and time.monotonic() < deadline:
            APP.processEvents()
            time.sleep(.01)
        APP.processEvents()
        self.assertTrue(predicate(), self.view.status)

    def visual_items(self, name):
        def walk(item):
            if item.objectName() == name:
                yield item
            for child in item.childItems():
                yield from walk(child)
        return list(walk(self.view.window.contentItem()))

    def seed(self):
        with self.queue.connection() as connection:
            for index, (job_id, state, finished) in enumerate(
                [('old-running', 'running', None), ('old-queued', 'queued', None)]
                + [(f'recent-{n}', 'succeeded', self.now.isoformat()) for n in range(80)]
                + [(f'history-{n}', 'succeeded', (self.now - timedelta(days=3)).isoformat()) for n in range(137)]
            ):
                value = {'job_id': job_id, 'state': state, 'summary': job_id,
                         'kind': 'process', 'created_at': (self.now - timedelta(days=10)).isoformat(),
                         'finished_at': finished, 'estimated_s': 10,
                         'progress': {'fraction': .25}, 'result': {'run_id': job_id}}
                connection.execute('INSERT INTO queue_jobs VALUES(?,?,?,?)',
                    (job_id, json.dumps(value), state, value['created_at']))
        # The UI talks to the scheduler protocol. Use the isolated in-process
        # peer after seeding so its initial snapshot includes these fixtures.
        self.harness = SchedulerHarness(Path(self.tmp.name))
        self.view.store = self.harness.client()

    def transition(self, job_id, **changes):
        return self.harness.scheduler._commit('transition', job_id, **changes)

    def refresh(self):
        if self.harness is None:
            # Empty-state coverage also uses the current scheduler protocol,
            # while keeping the fixture isolated from a background daemon.
            self.harness = SchedulerHarness(Path(self.tmp.name))
            self.view.store = self.harness.client()
        self.view.refreshQueue()
        self.spin(lambda: not self.view.loading)

    def select(self, index):
        self.tabs.setProperty('currentIndex', index)
        self.spin(lambda: not self.view.loading)
        self.assertEqual(self.view.activeBucket, 'recent' if index == 0 else 'history')

    def test_empty_states_and_render_both_tabs(self):
        self.refresh()
        empty = self.view.window.findChild(QObject, 'queueEmptyState')
        self.assertEqual(empty.property('text'), '暂无最近任务')
        self.select(1)
        self.assertEqual(empty.property('text'), '暂无历史任务')
        self.assertTrue(empty.property('visible'))
        self.assertFalse(self.view.hasMoreJobs)

    def test_all_old_active_jobs_are_visible_and_actions_are_preserved(self):
        self.seed(); self.refresh()
        self.assertEqual([job['job_id'] for job in self.view.jobs[:2]], ['old-running', 'old-queued'])
        self.assertEqual(len(self.view.jobs), 12)
        self.assertFalse(self.view.hasMoreJobs)  # Recent is a bounded summary.
        self.assertEqual(self.view.jobs[0]['stateLabel'], '计算中')
        cancels = self.visual_items('cancelQueueJob')
        results = self.visual_items('viewQueueResult')
        self.assertEqual(sum(bool(button.property('visible')) for button in cancels), 2)
        self.assertEqual(sum(bool(button.property('visible')) for button in results), 10)
        with patch.object(self.view.store, 'request_cancel', wraps=self.view.store.request_cancel) as cancel:
            self.select(1)
            self.assertFalse(cancel.called)
            self.assertFalse(any(button.property('visible') for button in
                                 self.visual_items('cancelQueueJob')))
        self.select(0)
        self.view.cancelQueueJob('old-queued')
        self.spin(lambda: not self.view.loading)
        self.assertEqual(self.view.store.read_job('old-queued')['state'], 'cancelled')

    def test_pagination_progress_poll_and_completion_keep_selected_tab(self):
        self.seed(); self.refresh(); self.select(1)
        self.assertEqual(len(self.view.jobs), 12)
        button = self.view.window.findChild(QObject, 'loadMoreQueueJobs')
        self.assertTrue(QMetaObject.invokeMethod(button, 'clicked'))
        self.spin(lambda: not self.view.loading)
        self.assertEqual(len(self.view.jobs), 24)
        job = self.view.store.read_job('old-running')
        self.transition('old-running', progress={'fraction': .75})
        self.view._poll_queue(); self.spin(lambda: not self.view.loading)
        self.assertEqual(self.tabs.property('currentIndex'), 1)
        self.assertEqual(len(self.view.jobs), 24)
        self.assertEqual(next(row for row in self.view._jobs_by_bucket['recent']
                              if row['job_id'] == 'old-running')['progress']['fraction'], .75)
        old_finish = (self.now - timedelta(days=2)).isoformat()
        with patch('coat_sim.scheduler.store._now', return_value=old_finish):
            self.transition('old-running', state='succeeded', progress={'fraction': 1})
        self.view._poll_queue(); self.spin(lambda: not self.view.loading)
        while self.view.hasMoreJobs:
            self.view.loadMoreQueueJobs(); self.spin(lambda: not self.view.loading)
        ids = [row['job_id'] for row in self.view.jobs]
        self.assertEqual(set(ids), {f'history-{n}' for n in range(137)} | {'old-running'})
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(self.tabs.property('currentIndex'), 1)
        self.assertFalse(set(ids) & {row['job_id'] for row in self.view._jobs_by_bucket['recent']})
        self.refresh()
        self.assertEqual(len(self.view.jobs), 12)
        self.assertTrue(self.view.hasMoreJobs)

    def test_delayed_previous_tab_response_does_not_take_selection(self):
        self.seed()
        response = self.view._read_page('recent', None)
        self.view._pending_request = ('recent', False)
        self.view._fetching = self.view._loading = True
        with patch.object(self.view.store, 'request_cancel') as cancel:
            self.tabs.setProperty('currentIndex', 1)
            self.view._receive(response, '')
            self.spin(lambda: not self.view.loading)
            self.assertEqual(self.tabs.property('currentIndex'), 1)
            self.assertEqual(self.view.activeBucket, 'history')
            self.assertEqual(len(self.view.jobs), 12)
            self.assertFalse(cancel.called)

    def test_loading_error_and_real_cancel_actions(self):
        self.seed(); self.refresh()
        button = self.view.window.findChild(QObject, 'loadMoreQueueJobs')
        self.select(1)
        release = threading.Event()
        original = self.view._read_page
        def delayed(*args):
            if not release.wait(5):
                raise RuntimeError('bounded wait expired')
            return original(*args)
        with patch.object(self.view, '_read_page', side_effect=delayed):
            self.view.loadMoreQueueJobs()
            self.assertTrue(self.view.loading)
            self.assertFalse(button.property('enabled'))
            release.set()
            self.spin(lambda: not self.view.loading)
        self.assertEqual(len(self.view.jobs), 24)
        before = [(job['job_id'], job['state']) for job in self.view.jobs]
        with patch.object(self.view, '_read_page', side_effect=RuntimeError('test queue read failure')):
            self.refresh()
            self.assertEqual(self.view.status, 'test queue read failure')
            self.assertEqual(before, [(job['job_id'], job['state']) for job in self.view.jobs])
        self.refresh()
        self.assertEqual(len(self.view.jobs), 12)
        self.select(0)
        active = [item for item in self.visual_items('cancelQueueJob') if item.property('visible')]
        with patch.object(self.view.store, 'request_cancel', wraps=self.view.store.request_cancel) as cancel:
            self.assertTrue(QMetaObject.invokeMethod(active[1], 'clicked'))
            self.spin(lambda: not self.view.loading)
            cancel.assert_called_once_with('old-queued')
        self.assertEqual(self.view.store.read_job('old-running')['state'], 'running')
        self.assertFalse((self.view.store.root / 'artifacts/runtime/jobs/old-running.cancel').exists())

    def test_real_qml_screenshots_for_review(self):
        self.seed(); self.refresh()
        directory = os.environ.get('COAT_QUEUE_QA_DIR')
        if not directory:
            directory = self.tmp.name
        destination = Path(directory)
        destination.mkdir(parents=True, exist_ok=True)
        for index, bucket in enumerate(('recent', 'history')):
            self.select(index)
            self.view.window.requestUpdate()
            for _ in range(10):
                APP.processEvents(); time.sleep(.01)
            image = self.view.window.grabWindow()
            self.assertFalse(image.isNull())
            self.assertTrue(image.save(str(destination / f'queue-{bucket}.png')))


if __name__ == '__main__':
    unittest.main()

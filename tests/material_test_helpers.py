"""Main-thread lifetime and async event-loop helpers for material UI tests."""
import time
import unittest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication
from coat_sim.ui.source_substrate import SourceBridge


class MaterialQtCase(unittest.TestCase):
    def make_source(self, *args, **kwargs):
        source = SourceBridge(*args, **kwargs)
        if not hasattr(self, '_material_sources'):
            self._material_sources = []
        self._material_sources.append(source)
        self.addCleanup(self._release_source, source)
        self.settle_materials()
        return source

    def _release_source(self, source):
        # A material bridge can still have catalog, import, or optical-curve
        # work running after its visible loading state clears. Join those
        # workers before Qt destroys their bridge and before the next test
        # pumps the event loop for a fresh source.
        source.materialLibrary.waitForBackgroundTasks()
        source.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        QApplication.instance().processEvents()
        if source in self._material_sources:
            self._material_sources.remove(source)

    def settle_materials(self):
        deadline = time.monotonic() + 10
        while True:
            QApplication.instance().processEvents()
            pending = any(source.materialLibrary.loading for source in getattr(self, '_material_sources', []))
            if not pending:
                break
            if time.monotonic() >= deadline:
                self.fail('Material library worker timeout')
            time.sleep(.005)
        QApplication.instance().processEvents()

"""Launcher-facing global-only resource library facade."""
from PySide6.QtCore import Slot

from .resource_library import ResourceLibraryBridge


class GlobalResourcesBridge(ResourceLibraryBridge):
    @Slot(result=bool)
    def browseGlobal(self):
        return self.browseLibrary()

    @Slot("QVariantList", str, result=bool)
    def previewGlobalTransfer(self, selected_ids, destination):
        return self.previewLibraryTransfer(selected_ids, destination)

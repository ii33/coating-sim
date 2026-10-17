"""Qt desktop tray, queue confirmation and small assistant notifications.

The queue window deliberately shows estimates and states only. It does not poll
simulation progress; queue records are shared across processes by SQLite.
"""

from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
import uuid
import time
import warnings

from PySide6.QtCore import QObject, Property, QMetaObject, QTimer, QUrl, Signal, Slot, QPoint, Qt
from PySide6.QtGui import QCursor, QGuiApplication, QWindow

from coat_sim.core.atomic_io import replace_with_retry
try:
    from PySide6.QtMultimedia import QSoundEffect
except ModuleNotFoundError as exc:
    if exc.name != "PySide6.QtMultimedia":
        raise
    QSoundEffect = None
from PySide6.QtQml import QQmlContext
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from coat_sim.ui.assistant import AssistantController
from coat_sim.ui.icons import window_icon
from coat_sim.ui.settings import (
    DEFAULT_ASSISTANT_NOTIFICATION_VOLUME_PERCENT,
    load_ui_settings,
    save_ui_settings,
)

ROOT = Path(__file__).resolve().parents[2]
CURSOR_PATH = ROOT / "artifacts" / "runtime" / "desktop-compute-cursor.json"
NOTIFICATION_SOUND_PATH = Path(__file__).resolve().parent / "assets" / "bubble_pop.wav"


def _notification_sound_source(assets=None) -> QUrl:
    if assets is None:
        from coat_sim.ui.theme_service import ThemeService

        assets = ThemeService.instance().current_assets
    sounds = assets.get("sounds", {}) if isinstance(assets, dict) else {}
    value = sounds.get("assistant.notification", "") if isinstance(sounds, dict) else ""
    if isinstance(value, str) and value:
        url = QUrl(value)
        if url.isLocalFile():
            themed_path = Path(url.toLocalFile())
            if themed_path.is_file():
                return QUrl.fromLocalFile(str(themed_path))
    if NOTIFICATION_SOUND_PATH.is_file():
        return QUrl.fromLocalFile(str(NOTIFICATION_SOUND_PATH))
    return QUrl()


def preview_notification_sound(
    volume_percent: int = DEFAULT_ASSISTANT_NOTIFICATION_VOLUME_PERCENT,
) -> bool:
    """Play the configured theme sound through the existing QSoundEffect path."""
    app = QGuiApplication.instance()
    if not isinstance(app, QGuiApplication):
        return False
    desktop = getattr(app, "_coat_assistant_desktop_bridge", None)
    if desktop is not None:
        desktop.previewNotificationSound(volume_percent)
        return True
    if QSoundEffect is None:
        QApplication.beep()
        return True
    player = getattr(app, "_coat_assistant_preview_sound", None)
    if player is None:
        player = QSoundEffect(app)
        app._coat_assistant_preview_sound = player
    player.setSource(_notification_sound_source())
    player.setVolume(max(0, min(100, int(volume_percent))) / 100.0)
    player.play()
    return True


def duration_label(value) -> str:
    if not isinstance(value, (int, float)) or value < 0:
        return "暂无耗时记录"
    if value < 60:
        return f"约 {max(1, round(value))} 秒"
    if value < 3600:
        return f"约 {value / 60:.1f} 分钟"
    return f"约 {value / 3600:.1f} 小时"


def finish_label(value) -> str:
    try:
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M:%S")
        if not isinstance(value, (int, float)) or value <= 0:
            return "待定"
        return datetime.fromtimestamp(value).astimezone().strftime("%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return "待定"


class DesktopAssistantBridge(QObject):
    changed = Signal()
    notificationChanged = Signal()
    snapshotReady = Signal(object, str)
    submissionReady = Signal(str, str)
    decisionChanged = Signal()
    filmOptimizationResultReady = Signal(str, object)
    rayTraceResultReady = Signal(str, str)

    def __init__(self, engine):
        super().__init__(engine)
        self.engine = engine
        self._result_viewer = None
        self._application_exit_callback = None
        self._application_exit_cancel_callback = None
        self._application_exit_waiting = False
        self._external_window_refresh = None
        self._external_window_action = None
        self._external_windows = []
        self.controller = None
        self._hidden = False
        settings = load_ui_settings()
        self._sound = bool(settings.get("assistant_notification_sound", True))
        self._sound_volume_percent = int(settings.get(
            "assistant_notification_volume_percent", DEFAULT_ASSISTANT_NOTIFICATION_VOLUME_PERCENT))
        self._notification_sound = QSoundEffect(self) if QSoundEffect is not None else None
        if self._notification_sound is not None:
            self._notification_sound.setVolume(self._sound_volume_percent / 100.0)
        else:
            warnings.warn("QtMultimedia 不可用：气泡提示音降级为系统提示音；安装匹配的 PySide6-Addons 可恢复。",
                          RuntimeWarning, stacklevel=2)
        from coat_sim.ui.theme_service import ThemeService

        self._theme_service = ThemeService.instance()
        self._theme_service.snapshotReplaced.connect(self._theme_replaced)
        if self._notification_sound is not None:
            self._notification_sound.setSource(
                _notification_sound_source(self._theme_service.current_assets))
        app = QGuiApplication.instance()
        if isinstance(app, QGuiApplication):
            app._coat_assistant_desktop_bridge = self
        self._jobs = []
        self._confirmations = []
        self._notification = ""
        self._notification_action = "queue"
        self._decisions = {}
        self._decision_id = ""
        self._deferred_decisions = set()
        self._decision_context = None
        self._decision_component = None
        self._decision_window = None
        self._decision_chat = None
        self._release_inflight = set()
        self._failed_auto_releases = set()
        self._held_countdowns = set()
        self._decision_supplements = {}
        self._notice_queue = deque()
        self._status = ""
        self._busy_confirmation = set()
        self._seen_confirmations = set()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="compute-queue-ui")
        self._fetching = False
        self._refresh_estimates_pending = False
        self._queue_authority_epoch = 0
        self._assistant_mode_seen: str | None = None
        self._closed = False
        self._open_windows = []
        self._window_refs = {}
        self._tracked_window_ids = set()
        self._window_list_timer = QTimer(self)
        self._window_list_timer.setInterval(300)
        self._window_list_timer.timeout.connect(self.refreshWindows)
        self._queue_context = None
        self._queue_component = None
        self._queue_window = None
        self._notice_component = None
        self._notice_window = None
        self._tray = None
        self._tray_menu_component = None
        self._tray_menu_window = None
        try:
            self._cursor = int(json.loads(CURSOR_PATH.read_text(encoding="utf-8"))["cursor"])
            self._bootstrap = False
        except (OSError, ValueError, KeyError, TypeError):
            self._cursor = 0
            self._bootstrap = True
        self.snapshotReady.connect(self._receive_snapshot)
        self.submissionReady.connect(self._submission_complete)
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self.refreshQueue)
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.setInterval(6800)
        self._notice_timer.timeout.connect(self._next_notice)
        self._decision_timer = QTimer(self)
        self._decision_timer.setInterval(500)
        self._decision_timer.timeout.connect(self._tick_decisions)

    def attach(self, controller):
        self.controller = controller
        self._assistant_mode_seen = str(controller.bridge.taskMode)
        controller.bridge.decisionRequested.connect(self._receive_decision)
        controller.bridge.decisionsChanged.connect(self._sync_agent_decisions)
        controller.bridge.taskSettled.connect(self._task_settled)
        controller.bridge.configurationChanged.connect(self._assistant_mode_changed)
        self.bind_chat()
        self._create_tray()
        self._timer.start()
        self._decision_timer.start()
        self.refreshQueue()

    def _assistant_mode_changed(self):
        # Stop displaying a stale default immediately; the persistent service
        # independently checks UI authority again at approval and final enqueue.
        self._queue_authority_epoch = getattr(self, "_queue_authority_epoch", 0) + 1
        self._refresh_estimates_pending = True
        current_mode = str(self.controller.bridge.taskMode) if self.controller is not None else ""
        previous_mode = getattr(self, "_assistant_mode_seen", None)
        self._assistant_mode_seen = current_mode
        if current_mode == "chat" or previous_mode is not None and previous_mode != current_mode:
            reason = ("讨论模式只读；请切换操作模式并重新明确请求计算。" if current_mode == "chat"
                      else "任务模式已改变；请在当前模式重新明确请求计算。")
            for row in self._decisions.values():
                if row.get("source") == "compute" and row.get("origin", {}).get("session_id"):
                    row.update(optional=False, deadline_ms=None, release_blocked=True,
                               error=reason,
                               options=[{"label": "保留待定记录", "description": "关闭提示；不会批准或提交计算。"},
                                        {"label": "关闭本次请求", "description": "撤销待定请求，保留估算历史。"}])
            self.decisionChanged.emit()
        self.refreshQueue()

    def bind_chat(self):
        chat = self.controller._chat if self.controller else None
        if chat is not None and chat is not self._decision_chat:
            chat.activeChanged.connect(self._chat_active_changed)
            self._decision_chat = chat

    @Property("QVariantMap", notify=decisionChanged)
    def decision(self):
        row = self._decisions.get(self._decision_id)
        if row is None:
            return {}
        value = dict(row)
        if self._decision_id in self._held_countdowns:
            value.update(optional=False, deadline_ms=None, countdown_s=0)
        if row.get("deadline_ms"):
            value["remaining_s"] = max(0, int((row["deadline_ms"] - time.time() * 1000 + 999) // 1000))
        return value

    def _chat_active(self):
        return bool(self.controller and self.controller._chat is not None and self.controller._chat.isActive())

    def _chat_active_changed(self):
        if self._chat_active():
            self._present_next_decision()

    @Slot(object)
    def _receive_decision(self, payload):
        identity = str(payload.get("decision_id") or "")
        if not identity or identity in self._decisions:
            return
        row = dict(payload)
        row["source"] = "agent"
        if row.get("optional") and int(row.get("countdown_s") or 0) > 0:
            row.setdefault("deadline_ms", time.time() * 1000 + int(row["countdown_s"]) * 1000)
        self._decisions[identity] = row
        self._new_decision_notice()

    def _new_decision_notice(self):
        if self._sound:
            self._play_notification_sound()
        if self._chat_active() and not self._hidden:
            self.showPendingDecision()
        else:
            self.notify("有一项待决事项，点击查看选项。", sound=False, action="decision")

    def _sync_agent_decisions(self):
        pending = self.controller.bridge.pendingDecisions
        live = {str(item.get("decision_id") or "") for item in pending}
        for payload in pending:
            identity = str(payload.get("decision_id") or "")
            if identity in self._decisions:
                self._decisions[identity].update(payload)
            else:
                self._receive_decision(payload)
        for identity, row in list(self._decisions.items()):
            if row.get("source") == "agent" and identity not in live:
                self._remove_decision(identity)
        self.decisionChanged.emit()

    @Slot(str, str)
    def _task_settled(self, state, text):
        if self._chat_active():
            return
        heading = {"completed": "本轮任务已完成", "failed": "本轮任务失败", "error": "本轮任务失败",
                   "cancelled": "本轮任务已停止"}.get(state, "本轮任务已结束")
        self.notify(heading, action="chat")

    def _remove_decision(self, identity):
        self._decisions.pop(identity, None)
        self._deferred_decisions.discard(identity)
        self._held_countdowns.discard(identity)
        if self._decision_id == identity:
            was_active = self._chat_active() or bool(self._decision_window and self._decision_window.isActive())
            self._decision_id = ""
            self.decisionChanged.emit()
            if self._decision_window is not None:
                self._decision_window.hide()
            if was_active:
                self._present_next_decision()

    @Slot()
    def showPendingDecision(self):
        self._deferred_decisions.clear()
        self._present_next_decision()

    def _present_next_decision(self):
        if not self.controller or self._closed or not self.controller.bridge.enabled:
            return
        if self._decision_id not in self._decisions:
            self._decision_id = next((identity for identity in self._decisions
                                      if identity not in self._deferred_decisions), "")
        if not self._decision_id or self._decision_id in self._deferred_decisions:
            return
        try:
            if self._decision_window is None:
                self._decision_context = QQmlContext(self.engine.rootContext())
                self._decision_context.setContextProperty("decisionUi", self)
                self._decision_component, self._decision_window = self.controller._create_window(
                    "AssistantDecisionWindow.qml", self._decision_context)
                self._decision_window.setIcon(window_icon("launcher"))
            self.decisionChanged.emit()
            window = self._decision_window
            screen = (self.controller._chat.screen() if self.controller._chat is not None else None) or QGuiApplication.primaryScreen()
            if screen is not None:
                area = screen.availableGeometry()
                window.resize(min(600, area.width() - 24), min(540, area.height() - 24))
                window.setPosition(QPoint(area.left() + (area.width() - window.width()) // 2,
                                         area.top() + (area.height() - window.height()) // 2))
            window.show()
            window.raise_()
            window.requestActivate()
        except RuntimeError as exc:
            self._status = f"待决窗口无法打开：{exc}"
            self.notify("有待决事项，但选项窗口未能打开。", sound=False, action="chat")
            self.changed.emit()

    @Slot(str)
    def deferDecision(self, identity):
        self._deferred_decisions.add(identity)
        self._decision_id = ""
        self.decisionChanged.emit()
        if self._decision_window is not None:
            self._decision_window.hide()

    @Slot(str)
    def pauseDecisionCountdown(self, identity):
        if identity in self._decisions:
            self._held_countdowns.add(identity)
            self.decisionChanged.emit()

    @Slot(str, int, str)
    def submitDecision(self, identity, index, text):
        row = self._decisions.get(identity)
        if not row:
            return
        options = row.get("options", [])
        if index < -1 or index >= len(options) or (index == -1 and not text.strip()):
            return
        if row.get("source") == "supplement":
            if index == 1:
                self._remove_decision(identity)
            elif self.controller.bridge.sendDecisionSupplement(text.strip() or row.get("supplement", "")):
                self._remove_decision(identity)
            return
        if row.get("source") == "compute":
            if row.get("release_blocked") and not text.strip():
                if index == 0:
                    self.deferDecision(identity)
                else:
                    self.confirm(row["estimate_id"], False)
                return
            # Supplementary requirements may change the frozen parameters.
            # Revoke this estimate and send the actual text for re-estimation.
            if text.strip():
                self._decision_supplements[row["estimate_id"]] = text.strip()
            row["submitting"] = True
            self.decisionChanged.emit()
            self.confirm(row["estimate_id"], index == 0 and not text.strip())
        else:
            # Keep the question until the worker acknowledges its real answer.
            # A rejected or lost submission remains visible and can be retried.
            self.controller.bridge.answerDecision(identity, index, text)

    def _tick_decisions(self):
        now = time.time() * 1000
        for identity, row in list(self._decisions.items()):
            deadline = row.get("deadline_ms")
            if (identity in self._held_countdowns or row.get("submitting")
                    or not row.get("optional") or not deadline or now < deadline):
                continue
            if row.get("source") == "compute":
                row["submitting"] = True
                self._release_due(row["estimate_id"])
            else:
                self.controller.bridge.applyDecisionDefault(identity)
        if self._decision_id:
            self.decisionChanged.emit()

    def _release_due(self, estimate_id):
        if (estimate_id in self._release_inflight or estimate_id in self._busy_confirmation
                or estimate_id in self._failed_auto_releases or "compute:" + estimate_id in self._held_countdowns
                or self._closed):
            return
        if DesktopAssistantBridge._compute_release_blocked(self, estimate_id):
            return
        self._release_inflight.add(estimate_id)
        def submit():
            from coat_sim.core.api import CoatService
            return CoatService(ROOT).release_due_compute_estimate(estimate_id)
        future = self._executor.submit(submit)
        def done(item):
            try:
                item.result()
                error = ""
            except Exception as exc:
                error = str(exc)
            if not self._closed:
                self.submissionReady.emit(estimate_id, error)
        future.add_done_callback(done)

    def _compute_release_blocked(self, estimate_id):
        row: dict = next((item for item in self._confirmations if item.get("estimate_id") == estimate_id), {})
        if row.get("release_policy") == "blocked":
            return True
        controller = getattr(self, "controller", None)
        return bool(row.get("origin", {}).get("session_id") and controller is not None
                    and controller.bridge.taskMode == "chat")

    @Property("QVariantList", notify=changed)
    def jobs(self):
        return self._jobs

    @Property("QVariantList", notify=changed)
    def confirmations(self):
        return self._confirmations

    @Property(bool, notify=changed)
    def hidden(self):
        return self._hidden

    @Property(bool, notify=changed)
    def soundEnabled(self):
        return self._sound

    @Property(int, notify=changed)
    def notificationSoundVolumePercent(self):
        return self._sound_volume_percent

    @Property(str, notify=changed)
    def status(self):
        return self._status

    @Property(str, notify=notificationChanged)
    def notificationText(self):
        return self._notification

    @Property("QVariantList", notify=changed)
    def openWindows(self):
        return self._open_windows

    def _window_list_identity(self, window):
        identity = str(window.property("_coat_sim_window_instance_id")
                       or window.property("_coat_sim_window_list_id") or "")
        if not identity:
            identity = uuid.uuid4().hex
        window.setProperty("_coat_sim_window_list_id", identity)
        return identity

    @Slot()
    def refreshWindows(self):
        """Publish local and authenticated app-owned remote windows."""
        if callable(self._external_window_refresh) and self._window_list_timer.isActive():
            self._external_window_refresh()
        app = QGuiApplication.instance()
        windows = []
        refs = {}
        ignored_types = (Qt.ToolTip, Qt.Popup, Qt.SplashScreen, Qt.SubWindow)
        if app is not None:
            for window in app.allWindows():
                try:
                    if (not window.isVisible()
                            or window is getattr(self.controller, "_bubble", None)
                            or window.objectName() == "assistantTrayMenuHost"):
                        continue
                    if window.type() in ignored_types:
                        continue
                    identity = self._window_list_identity(window)
                    title = (window.title() or "").strip() or (window.objectName() or "").strip()
                    if not title:
                        title = "未命名窗口"
                    windows.append({"id": identity, "title": title})
                    refs[identity] = window
                    if identity not in self._tracked_window_ids:
                        self._tracked_window_ids.add(identity)
                        window.visibleChanged.connect(self.refreshWindows)
                        for signal_name in ("titleChanged", "windowTitleChanged"):
                            title_changed = getattr(window, signal_name, None)
                            if title_changed is not None and hasattr(title_changed, "connect"):
                                title_changed.connect(self.refreshWindows)
                        window.destroyed.connect(
                            lambda _obj=None, window_id=identity: self._forget_window(window_id))
                except RuntimeError:
                    # A window may be destroyed while the application snapshot is read.
                    continue
        windows.extend(dict(row) for row in self._external_windows)
        changed = windows != self._open_windows
        self._window_refs = refs
        if changed:
            self._open_windows = windows
            self.changed.emit()

    def _forget_window(self, identity):
        self._tracked_window_ids.discard(identity)
        self._window_refs.pop(identity, None)
        updated = [item for item in self._open_windows if item["id"] != identity]
        if updated != self._open_windows:
            self._open_windows = updated
            self.changed.emit()

    @Slot()
    def startWindowListTracking(self):
        if callable(self._external_window_refresh):
            self._external_window_refresh()
        self.refreshWindows()
        self._window_list_timer.start()

    @Slot()
    def stopWindowListTracking(self):
        self._window_list_timer.stop()
        self.refreshWindows()

    @Slot(str, result=bool)
    def activateWindow(self, identity):
        if any(row.get("id") == identity for row in self._external_windows) and callable(self._external_window_action):
            return self._external_window_action(identity, "open_window")
        window = self._window_refs.get(identity)
        try:
            if window is None or not window.isVisible():
                self.refreshWindows()
                return False
            if window.visibility() == QWindow.Visibility.Minimized:
                window.showNormal()
            window.raise_()
            window.requestActivate()
        except RuntimeError:
            self.refreshWindows()
            return False
        return True

    @Slot(str, result=bool)
    def closeWindow(self, identity):
        if any(row.get("id") == identity for row in self._external_windows) and callable(self._external_window_action):
            return self._external_window_action(identity, "close_window")
        window = self._window_refs.get(identity)
        try:
            if window is None or not window.isVisible():
                self.refreshWindows()
                return False
            # QML onClosing handlers can reject this close while a dirty-state
            # confirmation is pending or the user cancels it.
            if window.visibility() == QWindow.Visibility.Minimized:
                window.showNormal()
            accepted = bool(window.close())
        except RuntimeError:
            accepted = True
        self.refreshWindows()
        return accepted

    def _create_tray(self):
        self._tray = QSystemTrayIcon(window_icon("launcher"), self)
        self._tray.setToolTip("看板娘 · 行星盘沉积仿真")
        if self.controller is not None:
            self._tray_menu_component, self._tray_menu_window = self.controller._create_window(
                "ShellMenu.qml", self.engine.rootContext())
            self._tray_menu_window.setIcon(window_icon("launcher"))
        self._tray.activated.connect(self._tray_activated)
        self._tray.messageClicked.connect(self.openNotification)
        self.syncVisibility()

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.controller.toggle_chat()
        elif reason == QSystemTrayIcon.ActivationReason.Context:
            # Let the native tray-button release complete before the QML popup
            # window is opened; this prevents the popup losing its initial grab.
            QTimer.singleShot(0, self._open_tray_menu)

    def _open_tray_menu(self):
        window = self._tray_menu_window
        if window is None:
            return
        method = "closeMenu" if window.isVisible() else "openAtCursor"
        QMetaObject.invokeMethod(window, method, Qt.ConnectionType.QueuedConnection)

    def syncVisibility(self):
        if self._tray is None or self.controller is None:
            return
        self._tray.setVisible((bool(self.controller.bridge.enabled) and self._hidden) or self._application_exit_waiting)
        settings = load_ui_settings()
        sound = bool(settings.get("assistant_notification_sound", True))
        volume = int(settings.get(
            "assistant_notification_volume_percent", DEFAULT_ASSISTANT_NOTIFICATION_VOLUME_PERCENT))
        if sound != self._sound or volume != self._sound_volume_percent:
            self._sound = sound
            self._set_notification_sound_volume(volume)
            self.changed.emit()

    @Slot(bool)
    def setSoundEnabled(self, enabled):
        self._sound = bool(enabled)
        config = load_ui_settings()
        config["assistant_notification_sound"] = self._sound
        config["assistant_notification_volume_percent"] = self._sound_volume_percent
        try:
            save_ui_settings(config)
        except OSError as exc:
            self._status = f"提示音设置无法保存：{exc}"
        self.changed.emit()

    def _set_notification_sound_volume(self, value):
        self._sound_volume_percent = max(0, min(100, int(value)))
        if self._notification_sound is not None:
            self._notification_sound.setVolume(self._sound_volume_percent / 100.0)

    @Slot(int)
    def setNotificationSoundVolumePercent(self, value):
        self._set_notification_sound_volume(value)
        config = load_ui_settings()
        config["assistant_notification_sound"] = self._sound
        config["assistant_notification_volume_percent"] = self._sound_volume_percent
        try:
            save_ui_settings(config)
        except OSError as exc:
            self._status = f"提示音设置无法保存：{exc}"
        self.changed.emit()

    @Slot(int)
    def previewNotificationSound(
        self, volume_percent=DEFAULT_ASSISTANT_NOTIFICATION_VOLUME_PERCENT
    ):
        self._set_notification_sound_volume(volume_percent)
        self._play_notification_sound()

    @Slot(object, int, str, str, str, str, object)
    def _theme_replaced(self, snapshot, revision, mode, preference, theme_id, theme_name, assets):
        if self._notification_sound is not None:
            self._notification_sound.setSource(_notification_sound_source(assets))
        if self._tray is not None:
            self._tray.setIcon(window_icon("launcher"))

    def _play_notification_sound(self):
        if self._notification_sound is not None:
            self._notification_sound.play()
        else:
            QApplication.beep()

    @Slot(int, int, result="QVariantMap")
    def trayMenuPosition(self, menu_width, menu_height):
        cursor = QCursor.pos()
        screen = QGuiApplication.screenAt(cursor) or QGuiApplication.primaryScreen()
        if screen is None:
            return {"x": cursor.x(), "y": cursor.y()}
        bounds = screen.availableGeometry()
        left, top = bounds.x(), bounds.y()
        right, bottom = left + bounds.width(), top + bounds.height()
        gap = 8
        popup_x = cursor.x() + gap
        popup_y = cursor.y() + gap
        if popup_x + menu_width > right:
            popup_x = cursor.x() - menu_width - gap
        if popup_y + menu_height > bottom:
            popup_y = cursor.y() - menu_height - gap
        popup_x = max(left, min(popup_x, right - menu_width))
        popup_y = max(top, min(popup_y, bottom - menu_height))
        return {"x": popup_x, "y": popup_y}

    @Slot(int, int, int, int, int, int, result="QVariantMap")
    def menuPosition(self, x, y, width, height, menu_width, menu_height):
        screen = QGuiApplication.screenAt(QPoint(x + width // 2, y + height // 2)) or QGuiApplication.primaryScreen()
        gap = 6
        if screen is None:
            return {"x": 0, "y": height + gap}
        bounds = screen.availableGeometry()
        right, bottom = bounds.x() + bounds.width(), bounds.y() + bounds.height()
        popup_x = max(bounds.x(), min(x, right - menu_width))
        if y + height + gap + menu_height <= bottom:
            popup_y = y + height + gap
        elif y - gap - menu_height >= bounds.y():
            popup_y = y - gap - menu_height
        else:
            popup_y = max(bounds.y(), min(y, bottom - menu_height))
            if x + width + gap + menu_width <= right:
                popup_x = x + width + gap
            elif x - gap - menu_width >= bounds.x():
                popup_x = x - gap - menu_width
        return {"x": popup_x - x, "y": popup_y - y}

    def set_external_window_callbacks(self, refresh, action):
        if not callable(refresh) or not callable(action):
            raise TypeError("External window callbacks must be callable")
        self._external_window_refresh, self._external_window_action = refresh, action

    def set_external_windows(self, rows):
        self._external_windows = [
            {**dict(row), "title": str(row.get("title") or "").strip() or "未命名窗口"}
            for row in rows
        ]
        self.refreshWindows()

    def set_application_exit_cancel_callback(self, callback):
        self._application_exit_cancel_callback = callback

    def set_application_exit_waiting(self, waiting):
        self._application_exit_waiting = bool(waiting)
        self.syncVisibility()
        self.changed.emit()

    @Property(bool, notify=changed)
    def applicationExitWaiting(self):
        return self._application_exit_waiting

    @Slot()
    def cancelExitWaiting(self):
        if self._application_exit_waiting and callable(self._application_exit_cancel_callback):
            self._application_exit_cancel_callback()

    def set_application_exit_callback(self, callback):
        """Bind the application coordinator; this bridge never kills processes."""
        if not callable(callback):
            raise TypeError("Application exit callback must be callable")
        self._application_exit_callback = callback
        self.changed.emit()

    @Property(bool, notify=changed)
    def applicationExitAvailable(self):
        return callable(self._application_exit_callback)

    @Slot()
    def exitApplication(self):
        if callable(self._application_exit_callback):
            self._application_exit_callback()
        else:
            self.notify("应用退出协调器尚未就绪。", sound=False)

    @Slot()
    def showSettings(self):
        if self.controller is not None:
            self.controller.show_settings()

    @Slot()
    def toggleVisibility(self):
        if self._hidden:
            self.showAssistant()
        else:
            self.hideAssistant()

    @Slot()
    def hideAssistant(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self._status = "当前桌面没有可用的系统托盘，已保留浮动入口。"
            self.notify(self._status, sound=False)
            self.changed.emit()
            return
        self._hidden = True
        self.controller.hide_windows()
        if self._notice_window is not None:
            self._notice_window.hide()
        if self._decision_window is not None:
            self._decision_window.hide()
        self.syncVisibility()
        self.changed.emit()

    @Slot()
    def showAssistant(self):
        if not self.controller.bridge.enabled:
            return
        self._hidden = False
        self.controller._ensure_windows()
        self.controller._bubble.show()
        self.syncVisibility()
        self.changed.emit()

    @Slot(str)
    def openQueueResult(self, job_id):
        from coat_sim.ui.compute_queue import QueueResultViewer
        try:
            if self._result_viewer is None:
                self._result_viewer = QueueResultViewer(self, ROOT)
                self._result_viewer.statusChanged.connect(self._queue_result_status)
                self._result_viewer.filmOptimizationResultReady.connect(
                    self.filmOptimizationResultReady.emit)
                self._result_viewer.rayTraceResultReady.connect(
                    self.rayTraceResultReady.emit)
            self._result_viewer.open(job_id)
        except Exception as exc:
            self._queue_result_status("无法打开结果：" + str(exc))

    @Slot(str)
    def _queue_result_status(self, message):
        self._status = message
        self.changed.emit()

    @Slot(str)
    def cancelQueueJob(self, job_id):
        from coat_sim.scheduler import SchedulerClient
        try:
            accepted = SchedulerClient(ROOT).request_cancel(job_id)
            self._status = "已请求取消，等待计算检查点。" if accepted else "任务已结束。"
        except Exception as exc:
            self._status = f"无法取消：{exc}"
        self.changed.emit()
        self.refreshQueue()

    @Slot()
    def refreshQueue(self):
        if self._closed or self._fetching:
            return
        self._fetching = True
        refresh_estimates = getattr(self, "_refresh_estimates_pending", False)
        self._refresh_estimates_pending = False
        cursor = self._cursor
        authority_epoch = getattr(self, "_queue_authority_epoch", 0)
        def fetch():
            from coat_sim.scheduler import SchedulerClient
            client = SchedulerClient(ROOT)
            if refresh_estimates:
                client.refresh_estimates()
            return client.snapshot(after_event=cursor, limit=100)
        future = self._executor.submit(fetch)
        def done(item):
            try:
                result, error = item.result(), ""
            except Exception as exc:
                result, error = {}, str(exc)
            if not self._closed:
                # This tag is owned by the UI request, never the daemon cache.
                self.snapshotReady.emit({**result, "_assistant_mode_epoch": authority_epoch,
                    "_assistant_refresh_failed": bool(refresh_estimates and error)}, error)
        future.add_done_callback(done)

    @Slot(object, str)
    def _receive_snapshot(self, snapshot, error):
        self._fetching = False
        if self._closed:
            return
        if snapshot.get("_assistant_mode_epoch", 0) != getattr(self, "_queue_authority_epoch", 0):
            # A mode change may already have returned to the previous label.
            # Reject the old request by its epoch, not by the current label.
            self._refresh_estimates_pending = True
            self.refreshQueue()
            return
        if error:
            if snapshot.get("_assistant_refresh_failed"):
                # Keep the need for authoritative revalidation across transient
                # RPC failure. The normal timer retries; never recurse on error.
                self._refresh_estimates_pending = True
            self._status = f"计算队列暂不可用：{error}"
            self.changed.emit()
            return
        state_names = {"queued": "等待开始", "running": "计算中", "completed": "已完成", "succeeded": "已完成", "failed": "失败", "cancelled": "已取消"}
        self._jobs = [{**row, "durationLabel": duration_label(row.get("estimated_s")),
                       "finishLabel": finish_label(row.get("expected_finish_at", row.get("expected_completion_at"))),
                       "stateLabel": state_names.get(row.get("state"), str(row.get("state", "")))}
                      for row in snapshot.get("jobs", [])]
        self._confirmations = [{**row, "durationLabel": duration_label(row.get("estimated_s")),
                                "submitting": row.get("estimate_id") in self._busy_confirmation}
                               for row in snapshot.get("confirmations", []) if row.get("state") == "pending"]
        controller = getattr(self, "controller", None)
        if controller is not None and controller.bridge.taskMode == "chat":
            # A request already in flight may return the daemon's pre-switch
            # cache. Never restore its old countdown while refreshing authority.
            for row in self._confirmations:
                if row.get("origin", {}).get("session_id"):
                    row.update(release_policy="blocked", release_at=None, requires_new_request=True,
                               release_error=row.get("release_error") or
                                   "compute_discussion_read_only: 讨论模式只读；请切换操作模式并重新明确请求计算。")
        cursor = int(snapshot.get("cursor", self._cursor))
        if self._bootstrap:
            cursor = int(snapshot.get("latest_event_id", cursor))
        for event in snapshot.get("events", []):
            if not self._bootstrap and int(event.get("event_id", 0)) > self._cursor:
                label = str(event.get("summary", "计算任务"))[:180]
                if event.get("type") == "enqueued":
                    self.notify(f"新计算任务已加入队列\n{label}")
                elif event.get("type") == "completed":
                    state = event.get("state")
                    prefix = "计算已完成" if state in ("completed", "succeeded") else "计算已结束"
                    self.notify(f"{prefix}\n{label}")
                    callback = getattr(self.controller.bridge, "backgroundJobCompleted", None)
                    if callable(callback):
                        callback(dict(event))
        previous_cursor = self._cursor
        first_snapshot = self._bootstrap
        self._cursor = max(self._cursor, cursor)
        self._bootstrap = False
        try:
            if first_snapshot or previous_cursor != self._cursor:
                CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
                temporary = CURSOR_PATH.with_name(f".{CURSOR_PATH.name}.{uuid.uuid4().hex}.tmp")
                temporary.write_text(json.dumps({"cursor": self._cursor}) + "\n", encoding="utf-8")
                replace_with_retry(temporary, CURSOR_PATH)
        except OSError:
            pass
        pending = {str(row.get("estimate_id", "")) for row in self._confirmations}
        for identity, row in list(self._decisions.items()):
            if row.get("source") == "compute" and row.get("estimate_id") not in pending:
                self._remove_decision(identity)
        for row in self._confirmations:
            estimate_id = str(row.get("estimate_id", ""))
            seen = estimate_id in self._seen_confirmations
            self._seen_confirmations.add(estimate_id)
            policy = row.get("release_policy", "manual")
            if policy == "immediate":
                continue
            deadline = None
            if policy == "countdown" and row.get("release_at"):
                try:
                    deadline = datetime.fromisoformat(row["release_at"].replace("Z", "+00:00")).timestamp() * 1000
                except (ValueError, TypeError):
                    pass
            identity = "compute:" + estimate_id
            payload = {"decision_id": identity, "source": "compute", "estimate_id": estimate_id,
                "origin": row.get("origin", {}), "release_blocked": policy == "blocked",
                "question": "计算资源确认：" + (row.get("summary") or "计算任务") + "\n预计用时：" + row["durationLabel"],
                "options": [{"label": "开始计算", "description": "放行此计算的资源使用；请求不会重复提交，会话可以继续。"},
                            {"label": "暂不计算", "description": "取消本次计算请求，保留设计文件。"}],
                "optional": deadline is not None, "deadline_ms": deadline,
                "countdown_s": 10 if deadline is not None else 0, "default_index": 0}
            if policy == "blocked":
                payload.update(question=payload["question"] + "\n" + str(row.get("release_error") or row.get("release_reason") or
                    "讨论模式只读；请切换操作模式并重新明确请求计算。"),
                    optional=False, deadline_ms=None, countdown_s=0,
                    options=[{"label": "保留待定记录", "description": "关闭提示；不会批准或提交计算。"},
                             {"label": "关闭本次请求", "description": "撤销待定请求，保留估算历史。"}])
            if identity in self._decisions:
                self._decisions[identity].update(payload)
            elif not seen:
                self._decisions[identity] = payload
                self._new_decision_notice()
        self.decisionChanged.emit()
        self.changed.emit()
        for row in self._confirmations:
            if row.get("release_policy") == "countdown" and row.get("release_at"):
                try:
                    if datetime.fromisoformat(row["release_at"].replace("Z", "+00:00")).timestamp() <= time.time():
                        self._release_due(row["estimate_id"])
                except (ValueError, TypeError):
                    pass
        if getattr(self, "_refresh_estimates_pending", False):
            self.refreshQueue()

    @Slot(str, bool)
    def confirm(self, estimate_id, accepted):
        if estimate_id in self._busy_confirmation or self._closed:
            return
        if accepted and DesktopAssistantBridge._compute_release_blocked(self, estimate_id):
            self._status = "讨论模式只读；请切换操作模式并重新明确请求计算。"
            self.changed.emit()
            self.refreshQueue()
            return
        self._busy_confirmation.add(estimate_id)
        for row in self._confirmations:
            if row.get("estimate_id") == estimate_id:
                row["submitting"] = True
        self.changed.emit()
        def submit():
            from coat_sim.core.api import CoatService
            return CoatService(ROOT).confirm_compute_estimate(estimate_id, accepted)
        future = self._executor.submit(submit)
        def done(item):
            try:
                item.result()
                error = ""
            except Exception as exc:
                error = str(exc)
            if not self._closed:
                self.submissionReady.emit(estimate_id, error)
        future.add_done_callback(done)

    @Slot(str, str)
    def _submission_complete(self, estimate_id, error):
        automatic = estimate_id in self._release_inflight
        self._busy_confirmation.discard(estimate_id)
        self._release_inflight.discard(estimate_id)
        self._status = f"计算任务提交失败：{error}" if error else ""
        identity = "compute:" + estimate_id
        if error:
            if automatic:
                self._failed_auto_releases.add(estimate_id)
            row = self._decisions.get(identity)
            if row:
                row.update(submitting=False, error=error)
                # An operation countdown failure must not retry every 500ms.
                # Keep a visible manual retry instead.
                row.update(optional=False, deadline_ms=None)
            self.notify("计算任务未能提交，点击查看。", action="decision")
        else:
            self._failed_auto_releases.discard(estimate_id)
            supplement = self._decision_supplements.pop(estimate_id, "")
            if supplement and not self.controller.bridge.sendDecisionSupplement(supplement):
                row = self._decisions.get(identity)
                if row:
                    row.update(source="supplement", submitting=False, optional=False, deadline_ms=None,
                        supplement=supplement, error="补充内容未能送达；原计算估算已取消，可重试发送。",
                        options=[{"label": "重新发送补充", "description": "向当前会话发送要求，重新估算。"},
                                 {"label": "取消补充", "description": "不再发送这段补充内容。"}])
                self.notify("补充内容暂未发送，点击重试。", action="decision")
            else:
                self._remove_decision(identity)
        self.decisionChanged.emit()
        self.changed.emit()
        self.refreshQueue()

    @Slot()
    def showQueue(self):
        if self.controller is None or self._closed:
            return
        if self._queue_window is None:
            self._queue_context = QQmlContext(self.engine.rootContext())
            self._queue_context.setContextProperty("computeQueue", self)
            self._queue_component, self._queue_window = self.controller._create_window("ComputeQueueWindow.qml", self._queue_context)
            self._queue_window.setIcon(window_icon("launcher"))
        window = self._queue_window
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            width, height = min(900, max(360, area.width() - 32)), min(650, max(300, area.height() - 32))
            window.resize(width, height)
            window.setPosition(QPoint(area.left() + (area.width() - width) // 2, area.top() + (area.height() - height) // 2))
        window.showNormal()
        window.raise_()
        window.requestActivate()
        self.refreshQueue()

    def notify(self, text, *, sound=True, action="queue"):
        self._notice_queue.append((text, sound, action))
        if not self._notice_timer.isActive():
            self._next_notice()

    def _next_notice(self):
        if not self._notice_queue or self._closed:
            return
        text, sound, self._notification_action = self._notice_queue.popleft()
        self._notice_timer.start()
        self._notification = text
        self.notificationChanged.emit()
        if self.controller is None or not self.controller.bridge.enabled:
            return
        if self._sound and sound:
            self._play_notification_sound()
        if self._hidden and self._tray is not None:
            self._tray.showMessage("看板娘", text, QSystemTrayIcon.MessageIcon.Information, 6500)
            return
        try:
            if self._notice_window is None:
                self._notice_component, self._notice_window = self.controller._create_window("AssistantNotification.qml", self.engine.rootContext())
                self._notice_window.setIcon(window_icon("launcher"))
            bubble = self.controller._bubble
            screen = bubble.screen() or QGuiApplication.primaryScreen()
            if screen is not None:
                area = screen.availableGeometry()
                width, height = self._notice_window.width(), self._notice_window.height()
                x = max(area.left() + 8, min(bubble.x() - width - 12, area.right() - width - 8))
                y = max(area.top() + 8, min(bubble.y() - height + bubble.height(), area.bottom() - height - 8))
                self._notice_window.setPosition(QPoint(x, y))
            self._notice_window.setProperty("notificationSerial", self._notice_window.property("notificationSerial") + 1)
            self._notice_window.show()
        except RuntimeError as exc:
            self._status = f"气泡提示暂不可用：{exc}"
            self.changed.emit()

    @Slot()
    def openNotification(self):
        if self._notification_action == "decision":
            self.showPendingDecision()
        elif self._notification_action == "chat":
            self.controller.show_chat()
        else:
            self.showQueue()

    def shutdown(self):
        app = QGuiApplication.instance()
        if (isinstance(app, QGuiApplication)
                and getattr(app, "_coat_assistant_desktop_bridge", None) is self):
            delattr(app, "_coat_assistant_desktop_bridge")
        if self._result_viewer is not None:
            self._result_viewer.shutdown()
        self._closed = True
        self._timer.stop()
        self._decision_timer.stop()
        self._notice_timer.stop()
        self._notice_queue.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._window_list_timer.stop()
        self._window_refs.clear()
        if self._tray is not None:
            self._tray.hide()
        for window in (self._queue_window, self._notice_window, self._decision_window):
            if window is not None:
                window.close()
                window.deleteLater()
        if self._tray_menu_window is not None:
            self._tray_menu_window.hide()
            self._tray_menu_window.deleteLater()


class DesktopAssistantController(AssistantController):
    def __init__(self, engine, open_settings_callback=None):
        # The base class creates AssistantBubble during its constructor. Bind the
        # independent desktop bridge before that QML component is instantiated.
        desktop = DesktopAssistantBridge(engine)
        engine.rootContext().setContextProperty("desktop", desktop)
        super().__init__(engine, open_settings_callback=open_settings_callback)
        self.desktop = desktop
        desktop.attach(self)

    def _ensure_windows(self):
        super()._ensure_windows()
        desktop = getattr(self, "desktop", None)
        if desktop is not None:
            desktop.bind_chat()

    def reload_configuration(self):
        super().reload_configuration()
        self.desktop.bind_chat()
        if self.desktop.hidden and self._bubble is not None:
            self._bubble.hide()
        self.desktop.syncVisibility()

    def shutdown(self):
        self.desktop.shutdown()
        super().shutdown()

from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Signal, Slot


class TaskReceiver(QObject):
    """A GUI-thread receiver; plain Python lambdas have no Qt thread affinity."""

    def __init__(self, parent, result, failed, stopped):
        super().__init__(parent)
        self.result_callback = result
        self.failed_callback = failed
        self.stopped_callback = stopped

    @Slot(object)
    def on_result(self, value):
        self.result_callback(value)

    @Slot(str)
    def on_failed(self, message):
        self.failed_callback(message)

    @Slot()
    def on_stopped(self):
        self.stopped_callback()
        self.deleteLater()


class FunctionThread(QThread):
    result = Signal(object)
    failed = Signal(str)

    def __init__(self, parent, function):
        super().__init__(parent)
        self.function = function

    def run(self):
        try:
            self.result.emit(self.function())
        except Exception as exc:
            self.failed.emit(str(exc))

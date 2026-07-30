from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from .ui import MainWindow, configure_application


def main() -> int:
    application = QApplication(sys.argv)
    configure_application(application)
    window = MainWindow()
    window.show()
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())

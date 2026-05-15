import os
import sys
from pathlib import Path

os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"
os.environ["QT_SCALE_FACTOR_ROUNDING_POLICY"] = "PassThrough"

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from ui.main_app import MainApp

BASE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))


def main():
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    icon_path = Path(BASE_DIR) / "translator.png"
    if icon_path.exists():
        icon = QIcon(str(icon_path))
        QApplication.setWindowIcon(icon)

    window = MainApp()
    window.resize(1920, 1080)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

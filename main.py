from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QIcon, QMovie, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStyle,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DB_PATH = DATA_DIR / "media_notes.db"

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
GIF_EXTENSIONS = {".gif"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v"}


class Database:
    def __init__(self, path: Path):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._create_schema()
        self._ensure_default_category()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE
            );

            CREATE TABLE IF NOT EXISTS media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                media_type TEXT NOT NULL,
                category_id INTEGER NOT NULL,
                caption TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (category_id) REFERENCES categories(id)
            );

            CREATE INDEX IF NOT EXISTS idx_media_category
            ON media(category_id);
            """
        )
        self.conn.commit()

    def _ensure_default_category(self) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO categories(name) VALUES (?)",
            ("Nezařazené",),
        )
        self.conn.commit()

    def default_category_id(self) -> int:
        row = self.conn.execute(
            "SELECT id FROM categories WHERE name = ?",
            ("Nezařazené",),
        ).fetchone()
        return int(row["id"])

    def categories(self):
        return self.conn.execute(
            "SELECT id, name FROM categories ORDER BY name COLLATE NOCASE"
        ).fetchall()

    def add_category(self, name: str) -> None:
        self.conn.execute("INSERT INTO categories(name) VALUES (?)", (name,))
        self.conn.commit()

    def delete_category(self, category_id: int) -> None:
        default_id = self.default_category_id()
        if category_id == default_id:
            raise ValueError("Výchozí kategorii nelze smazat.")

        self.conn.execute(
            "UPDATE media SET category_id = ? WHERE category_id = ?",
            (default_id, category_id),
        )
        self.conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        self.conn.commit()

    def add_media(self, path: str, media_type: str, category_id: int) -> None:
        self.conn.execute(
            """
            INSERT INTO media(path, media_type, category_id)
            VALUES (?, ?, ?)
            """,
            (path, media_type, category_id),
        )
        self.conn.commit()

    def media_items(self, category_id: int | None = None):
        if category_id is None:
            return self.conn.execute(
                """
                SELECT m.*, c.name AS category_name
                FROM media m
                JOIN categories c ON c.id = m.category_id
                ORDER BY m.id DESC
                """
            ).fetchall()

        return self.conn.execute(
            """
            SELECT m.*, c.name AS category_name
            FROM media m
            JOIN categories c ON c.id = m.category_id
            WHERE m.category_id = ?
            ORDER BY m.id DESC
            """,
            (category_id,),
        ).fetchall()

    def media_by_id(self, media_id: int):
        return self.conn.execute(
            """
            SELECT m.*, c.name AS category_name
            FROM media m
            JOIN categories c ON c.id = m.category_id
            WHERE m.id = ?
            """,
            (media_id,),
        ).fetchone()

    def update_media(self, media_id: int, caption: str, notes: str, category_id: int) -> None:
        self.conn.execute(
            """
            UPDATE media
            SET caption = ?, notes = ?, category_id = ?
            WHERE id = ?
            """,
            (caption, notes, category_id, media_id),
        )
        self.conn.commit()

    def delete_media(self, media_id: int) -> None:
        self.conn.execute("DELETE FROM media WHERE id = ?", (media_id,))
        self.conn.commit()


def detect_media_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in GIF_EXTENSIONS:
        return "gif"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    return "other"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.db = Database(DB_PATH)
        self.current_media_id: int | None = None
        self.current_movie: QMovie | None = None

        self.setWindowTitle("Media Notes")
        self.resize(1200, 760)

        self._build_ui()
        self.reload_categories()
        self.reload_media()

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)

        top_bar = QHBoxLayout()
        self.add_media_btn = QPushButton("Přidat média")
        self.add_category_btn = QPushButton("Nová kategorie")
        self.delete_category_btn = QPushButton("Smazat kategorii")
        top_bar.addWidget(self.add_media_btn)
        top_bar.addStretch()
        top_bar.addWidget(self.add_category_btn)
        top_bar.addWidget(self.delete_category_btn)
        root_layout.addLayout(top_bar)

        splitter = QSplitter(Qt.Horizontal)
        root_layout.addWidget(splitter, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("Kategorie"))
        self.category_list = QListWidget()
        left_layout.addWidget(self.category_list)
        splitter.addWidget(left)

        right_splitter = QSplitter(Qt.Vertical)

        media_panel = QWidget()
        media_layout = QVBoxLayout(media_panel)
        media_layout.addWidget(QLabel("Média"))
        self.media_list = QListWidget()
        self.media_list.setViewMode(QListWidget.IconMode)
        self.media_list.setResizeMode(QListWidget.Adjust)
        self.media_list.setMovement(QListWidget.Static)
        self.media_list.setIconSize(QSize(170, 115))
        self.media_list.setGridSize(QSize(200, 155))
        self.media_list.setSpacing(8)
        media_layout.addWidget(self.media_list)
        right_splitter.addWidget(media_panel)

        detail = QWidget()
        detail_layout = QHBoxLayout(detail)

        preview_side = QVBoxLayout()
        self.preview = QLabel("Vyber médium")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(420, 260)
        self.preview.setStyleSheet(
            "QLabel { background: #202124; color: #ddd; border-radius: 6px; }"
        )
        preview_side.addWidget(self.preview, 1)

        self.open_btn = QPushButton("Otevřít původní soubor")
        self.open_btn.setEnabled(False)
        preview_side.addWidget(self.open_btn)

        detail_layout.addLayout(preview_side, 3)

        form_side = QVBoxLayout()
        form_side.addWidget(QLabel("Krátký popisek"))
        self.caption_edit = QLineEdit()
        self.caption_edit.setPlaceholderText("Např. nejlepší moment, vtipný GIF...")
        form_side.addWidget(self.caption_edit)

        form_side.addWidget(QLabel("Kategorie"))
        self.category_combo = QComboBox()
        form_side.addWidget(self.category_combo)

        form_side.addWidget(QLabel("Poznámky"))
        self.notes_edit = QTextEdit()
        self.notes_edit.setPlaceholderText(
            "Sem můžeš napsat delší poznámku, kontext nebo cokoliv dalšího."
        )
        form_side.addWidget(self.notes_edit, 1)

        buttons = QHBoxLayout()
        self.save_btn = QPushButton("Uložit")
        self.delete_media_btn = QPushButton("Smazat z databáze")
        buttons.addWidget(self.save_btn)
        buttons.addWidget(self.delete_media_btn)
        form_side.addLayout(buttons)

        detail_layout.addLayout(form_side, 2)
        right_splitter.addWidget(detail)

        right_splitter.setSizes([420, 320])
        splitter.addWidget(right_splitter)
        splitter.setSizes([220, 980])

        self.add_media_btn.clicked.connect(self.add_media)
        self.add_category_btn.clicked.connect(self.add_category)
        self.delete_category_btn.clicked.connect(self.delete_category)
        self.category_list.currentItemChanged.connect(self.on_category_changed)
        self.media_list.currentItemChanged.connect(self.on_media_changed)
        self.save_btn.clicked.connect(self.save_current)
        self.delete_media_btn.clicked.connect(self.delete_current)
        self.open_btn.clicked.connect(self.open_current)

    def reload_categories(self, keep_category_id: int | None = None) -> None:
        self.category_list.blockSignals(True)
        self.category_combo.blockSignals(True)
        self.category_list.clear()
        self.category_combo.clear()

        all_item = QListWidgetItem("Vše")
        all_item.setData(Qt.UserRole, None)
        self.category_list.addItem(all_item)

        selected_row = 0
        for row_index, row in enumerate(self.db.categories(), start=1):
            item = QListWidgetItem(row["name"])
            item.setData(Qt.UserRole, int(row["id"]))
            self.category_list.addItem(item)
            self.category_combo.addItem(row["name"], int(row["id"]))
            if keep_category_id == int(row["id"]):
                selected_row = row_index

        self.category_list.setCurrentRow(selected_row)
        self.category_list.blockSignals(False)
        self.category_combo.blockSignals(False)

    def selected_category_id(self) -> int | None:
        item = self.category_list.currentItem()
        return None if item is None else item.data(Qt.UserRole)

    def reload_media(self) -> None:
        rows = self.db.media_items(self.selected_category_id())
        self.media_list.blockSignals(True)
        self.media_list.clear()

        for row in rows:
            path = Path(row["path"])
            title = row["caption"].strip() or path.name
            item = QListWidgetItem(title)
            item.setData(Qt.UserRole, int(row["id"]))
            item.setToolTip(str(path))
            item.setIcon(self.make_icon(path, row["media_type"]))
            self.media_list.addItem(item)

        self.media_list.blockSignals(False)
        if self.media_list.count() > 0:
            self.media_list.setCurrentRow(0)
            self.on_media_changed(self.media_list.currentItem(), None)
        else:
            self.clear_detail()

    def make_icon(self, path: Path, media_type: str) -> QIcon:
        if path.exists() and media_type in {"image", "gif"}:
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                return QIcon(pixmap.scaled(160, 110, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        if media_type == "video":
            return self.style().standardIcon(QStyle.SP_MediaPlay)
        return self.style().standardIcon(QStyle.SP_FileIcon)

    def add_media(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Vyber obrázky, GIFy nebo videa",
            str(Path.home()),
            "Média (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.mp4 *.mkv *.webm *.avi *.mov *.m4v);;Všechny soubory (*)",
        )
        if not files:
            return
        category_id = self.selected_category_id()
        if category_id is None:
            category_id = self.db.default_category_id()
        for file_name in files:
            path = Path(file_name)
            self.db.add_media(str(path), detect_media_type(path), int(category_id))
        self.reload_media()

    def add_category(self) -> None:
        name, ok = QInputDialog.getText(self, "Nová kategorie", "Název:")
        name = name.strip()
        if not ok or not name:
            return
        try:
            self.db.add_category(name)
        except sqlite3.IntegrityError:
            QMessageBox.information(self, "Kategorie", "Tato kategorie už existuje.")
            return
        self.reload_categories()

    def delete_category(self) -> None:
        item = self.category_list.currentItem()
        if item is None:
            return
        category_id = item.data(Qt.UserRole)
        if category_id is None:
            QMessageBox.information(self, "Kategorie", "Položku „Vše“ nelze smazat.")
            return
        if QMessageBox.question(
            self,
            "Smazat kategorii",
            "Média z této kategorie se přesunou do „Nezařazené“. Pokračovat?",
        ) != QMessageBox.Yes:
            return
        try:
            self.db.delete_category(int(category_id))
        except ValueError as exc:
            QMessageBox.information(self, "Kategorie", str(exc))
            return
        self.reload_categories()
        self.reload_media()

    def on_category_changed(self, current, previous) -> None:
        self.reload_media()

    def on_media_changed(self, current, previous) -> None:
        if current is None:
            self.clear_detail()
            return
        row = self.db.media_by_id(int(current.data(Qt.UserRole)))
        if row is None:
            self.clear_detail()
            return
        self.current_media_id = int(row["id"])
        self.caption_edit.setText(row["caption"])
        self.notes_edit.setPlainText(row["notes"])
        combo_index = self.category_combo.findData(int(row["category_id"]))
        if combo_index >= 0:
            self.category_combo.setCurrentIndex(combo_index)
        self.show_preview(Path(row["path"]), row["media_type"])
        self.open_btn.setEnabled(True)

    def show_preview(self, path: Path, media_type: str) -> None:
        if self.current_movie is not None:
            self.current_movie.stop()
            self.current_movie = None
            self.preview.setMovie(None)

        self.preview.clear()
        if not path.exists():
            self.preview.setText(f"Soubor nenalezen\n{path}")
            return

        if media_type == "gif":
            movie = QMovie(str(path))
            if movie.isValid():
                movie.setScaledSize(self.preview.size())
                self.preview.setMovie(movie)
                movie.start()
                self.current_movie = movie
                return

        if media_type == "image":
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                self.preview.setPixmap(
                    pixmap.scaled(self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )
                return

        if media_type == "video":
            self.preview.setText(f"VIDEO\n\n{path.name}\n\nV této první verzi se video otevírá externě.")
            return

        self.preview.setText(path.name)

    def save_current(self) -> None:
        if self.current_media_id is None:
            return
        category_id = self.category_combo.currentData()
        if category_id is None:
            return
        self.db.update_media(
            self.current_media_id,
            self.caption_edit.text().strip(),
            self.notes_edit.toPlainText().strip(),
            int(category_id),
        )
        keep_category = self.selected_category_id()
        self.reload_categories(keep_category)
        self.reload_media()

    def delete_current(self) -> None:
        if self.current_media_id is None:
            return
        if QMessageBox.question(
            self,
            "Smazat z databáze",
            "Smazat tuto položku? Původní soubor v počítači zůstane nedotčený.",
        ) != QMessageBox.Yes:
            return
        self.db.delete_media(self.current_media_id)
        self.reload_media()

    def open_current(self) -> None:
        if self.current_media_id is None:
            return
        row = self.db.media_by_id(self.current_media_id)
        if row is None:
            return
        path = Path(row["path"])
        if not path.exists():
            QMessageBox.warning(self, "Soubor", "Původní soubor už na této cestě není.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def clear_detail(self) -> None:
        self.current_media_id = None
        if self.current_movie is not None:
            self.current_movie.stop()
            self.current_movie = None
        self.preview.clear()
        self.preview.setText("Vyber médium")
        self.caption_edit.clear()
        self.notes_edit.clear()
        self.open_btn.setEnabled(False)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Media Notes")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

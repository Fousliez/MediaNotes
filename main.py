from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QIcon,
    QKeySequence,
    QMovie,
    QPainter,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
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
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | GIF_EXTENSIONS | VIDEO_EXTENSIONS


class Database:
    def __init__(self, path: Path):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._create_schema()
        self._migrate_schema()
        self._ensure_default_category()
        self._normalize_category_order()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                sort_order INTEGER NOT NULL DEFAULT 0
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

    def _migrate_schema(self) -> None:
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(categories)").fetchall()
        }
        if "sort_order" not in columns:
            self.conn.execute(
                "ALTER TABLE categories ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"
            )
            self.conn.commit()

    def _ensure_default_category(self) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO categories(name, sort_order)
            VALUES (?, COALESCE((SELECT MAX(sort_order) + 1 FROM categories), 0))
            """,
            ("Nezařazené",),
        )
        self.conn.commit()

    def _normalize_category_order(self) -> None:
        rows = self.conn.execute(
            "SELECT id FROM categories ORDER BY sort_order, id"
        ).fetchall()
        for index, row in enumerate(rows):
            self.conn.execute(
                "UPDATE categories SET sort_order = ? WHERE id = ?",
                (index, int(row["id"])),
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
            """
            SELECT
                c.id,
                c.name,
                c.sort_order,
                COUNT(m.id) AS media_count
            FROM categories c
            LEFT JOIN media m ON m.category_id = c.id
            GROUP BY c.id, c.name, c.sort_order
            ORDER BY c.sort_order, c.id
            """
        ).fetchall()

    def total_media_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM media").fetchone()
        return int(row["n"])

    def add_category(self, name: str) -> int:
        next_order = self.conn.execute(
            "SELECT COALESCE(MAX(sort_order) + 1, 0) AS n FROM categories"
        ).fetchone()["n"]
        cursor = self.conn.execute(
            "INSERT INTO categories(name, sort_order) VALUES (?, ?)",
            (name, int(next_order)),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def rename_category(self, category_id: int, name: str) -> None:
        if category_id == self.default_category_id():
            raise ValueError("Kategorie „Nezařazené“ má pevný název.")
        self.conn.execute(
            "UPDATE categories SET name = ? WHERE id = ?",
            (name, category_id),
        )
        self.conn.commit()

    def move_category(self, category_id: int, direction: int) -> bool:
        rows = self.conn.execute(
            "SELECT id, sort_order FROM categories ORDER BY sort_order, id"
        ).fetchall()
        ids = [int(row["id"]) for row in rows]

        if category_id not in ids:
            return False

        index = ids.index(category_id)
        target_index = index + direction
        if target_index < 0 or target_index >= len(ids):
            return False

        current = rows[index]
        target = rows[target_index]

        self.conn.execute(
            "UPDATE categories SET sort_order = ? WHERE id = ?",
            (int(target["sort_order"]), int(current["id"])),
        )
        self.conn.execute(
            "UPDATE categories SET sort_order = ? WHERE id = ?",
            (int(current["sort_order"]), int(target["id"])),
        )
        self.conn.commit()
        return True

    def delete_category(self, category_id: int) -> None:
        default_id = self.default_category_id()
        if category_id == default_id:
            raise ValueError("Výchozí kategorii „Nezařazené“ nelze smazat.")

        self.conn.execute(
            "UPDATE media SET category_id = ? WHERE category_id = ?",
            (default_id, category_id),
        )
        self.conn.execute("DELETE FROM categories WHERE id = ?", (category_id,))
        self.conn.commit()
        self._normalize_category_order()

    def add_media(self, path: str, media_type: str, category_id: int) -> bool:
        normalized = str(Path(path).expanduser().resolve())
        exists = self.conn.execute(
            "SELECT id FROM media WHERE path = ? LIMIT 1",
            (normalized,),
        ).fetchone()
        if exists is not None:
            return False

        self.conn.execute(
            """
            INSERT INTO media(path, media_type, category_id)
            VALUES (?, ?, ?)
            """,
            (normalized, media_type, category_id),
        )
        self.conn.commit()
        return True

    def media_items(self, category_id: int | None = None, search: str = ""):
        query = """
            SELECT m.*, c.name AS category_name
            FROM media m
            JOIN categories c ON c.id = m.category_id
            WHERE 1 = 1
        """
        params: list[object] = []

        if category_id is not None:
            query += " AND m.category_id = ?"
            params.append(category_id)

        if search.strip():
            needle = f"%{search.strip()}%"
            query += """
                AND (
                    m.caption LIKE ?
                    OR m.notes LIKE ?
                    OR m.path LIKE ?
                    OR c.name LIKE ?
                )
            """
            params.extend([needle, needle, needle, needle])

        query += " ORDER BY m.id DESC"
        return self.conn.execute(query, params).fetchall()

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

    def update_media(
        self,
        media_id: int,
        caption: str,
        notes: str,
        category_id: int,
    ) -> None:
        self.conn.execute(
            """
            UPDATE media
            SET caption = ?, notes = ?, category_id = ?
            WHERE id = ?
            """,
            (caption, notes, category_id, media_id),
        )
        self.conn.commit()

    def move_media(self, media_ids: list[int], category_id: int) -> None:
        if not media_ids:
            return
        placeholders = ",".join("?" for _ in media_ids)
        self.conn.execute(
            f"UPDATE media SET category_id = ? WHERE id IN ({placeholders})",
            [category_id, *media_ids],
        )
        self.conn.commit()

    def delete_media_ids(self, media_ids: list[int]) -> None:
        if not media_ids:
            return
        placeholders = ",".join("?" for _ in media_ids)
        self.conn.execute(
            f"DELETE FROM media WHERE id IN ({placeholders})",
            media_ids,
        )
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


class MediaListWidget(QListWidget):
    filesDropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("mediaList")
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setViewMode(QListWidget.IconMode)
        self.setResizeMode(QListWidget.Adjust)
        self.setMovement(QListWidget.Static)
        self.setIconSize(QSize(180, 125))
        self.setGridSize(QSize(215, 170))
        self.setSpacing(8)

    def _has_local_files(self, event) -> bool:
        mime = event.mimeData()
        return mime.hasUrls() and any(url.isLocalFile() for url in mime.urls())

    def _set_drag_active(self, active: bool) -> None:
        self.setProperty("dragActive", active)
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event) -> None:
        if self._has_local_files(event):
            event.acceptProposedAction()
            self._set_drag_active(True)
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if self._has_local_files(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._set_drag_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        self._set_drag_active(False)
        if not self._has_local_files(event):
            event.ignore()
            return

        paths = [
            url.toLocalFile()
            for url in event.mimeData().urls()
            if url.isLocalFile()
        ]
        self.filesDropped.emit(paths)
        event.acceptProposedAction()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.count() != 0:
            return

        painter = QPainter(self.viewport())
        painter.setPen(QColor("#7f8792"))
        painter.drawText(
            self.viewport().rect(),
            Qt.AlignCenter,
            "Přetáhni sem obrázky, GIFy nebo videa\n"
            "nebo klikni nahoře na „Přidat média“",
        )


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.db = Database(DB_PATH)
        self.current_media_id: int | None = None
        self.current_movie: QMovie | None = None
        self.current_preview_path: Path | None = None
        self.current_preview_type: str | None = None

        self.setWindowTitle("MediaNotes")
        self.resize(1280, 820)
        self.setMinimumSize(950, 620)

        self._build_ui()
        self._apply_style()
        self._create_shortcuts()

        self.reload_categories()
        self.reload_media()
        self.statusBar().showMessage("Připraveno", 2500)

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(16, 14, 16, 12)
        root_layout.setSpacing(12)

        top_bar = QHBoxLayout()
        top_bar.setSpacing(10)

        title = QLabel("MediaNotes")
        title.setObjectName("appTitle")
        top_bar.addWidget(title)

        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("searchBox")
        self.search_edit.setPlaceholderText(
            "Hledat v popiscích, poznámkách, kategoriích nebo názvech souborů…"
        )
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setMaximumWidth(520)
        top_bar.addWidget(self.search_edit, 1)

        self.add_media_btn = QPushButton("＋  Přidat média")
        self.add_media_btn.setObjectName("primaryButton")
        top_bar.addWidget(self.add_media_btn)
        root_layout.addLayout(top_bar)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        root_layout.addWidget(splitter, 1)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(12, 14, 12, 12)
        sidebar_layout.setSpacing(8)

        sidebar_header = QHBoxLayout()
        category_heading = QLabel("Kategorie")
        category_heading.setObjectName("sectionTitle")
        sidebar_header.addWidget(category_heading)
        sidebar_header.addStretch()

        self.add_category_btn = QPushButton("＋")
        self.add_category_btn.setObjectName("miniButton")
        self.add_category_btn.setToolTip("Nová kategorie")
        self.add_category_btn.setFixedWidth(34)
        sidebar_header.addWidget(self.add_category_btn)
        sidebar_layout.addLayout(sidebar_header)

        self.category_list = QListWidget()
        self.category_list.setObjectName("categoryList")
        self.category_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.category_list.setSpacing(2)
        sidebar_layout.addWidget(self.category_list, 1)

        category_buttons = QHBoxLayout()
        category_buttons.setSpacing(6)

        self.rename_category_btn = QPushButton("Přejmenovat")
        self.rename_category_btn.setToolTip("Přejmenovat vybranou kategorii")
        category_buttons.addWidget(self.rename_category_btn)

        self.category_up_btn = QPushButton("↑")
        self.category_up_btn.setObjectName("miniButton")
        self.category_up_btn.setToolTip("Posunout kategorii nahoru")
        self.category_up_btn.setFixedWidth(34)
        category_buttons.addWidget(self.category_up_btn)

        self.category_down_btn = QPushButton("↓")
        self.category_down_btn.setObjectName("miniButton")
        self.category_down_btn.setToolTip("Posunout kategorii dolů")
        self.category_down_btn.setFixedWidth(34)
        category_buttons.addWidget(self.category_down_btn)

        self.delete_category_btn = QPushButton("×")
        self.delete_category_btn.setObjectName("dangerMiniButton")
        self.delete_category_btn.setToolTip("Smazat kategorii")
        self.delete_category_btn.setFixedWidth(34)
        category_buttons.addWidget(self.delete_category_btn)

        sidebar_layout.addLayout(category_buttons)
        splitter.addWidget(sidebar)

        right_splitter = QSplitter(Qt.Vertical)
        right_splitter.setChildrenCollapsible(False)

        media_panel = QFrame()
        media_panel.setObjectName("panel")
        media_layout = QVBoxLayout(media_panel)
        media_layout.setContentsMargins(14, 14, 14, 14)
        media_layout.setSpacing(8)

        media_header = QHBoxLayout()
        self.category_title = QLabel("Vše")
        self.category_title.setObjectName("sectionTitle")
        media_header.addWidget(self.category_title)

        self.media_count_label = QLabel("")
        self.media_count_label.setObjectName("mutedLabel")
        media_header.addWidget(self.media_count_label)
        media_header.addStretch()

        drop_hint = QLabel("Tip: soubory můžeš sem rovnou přetáhnout")
        drop_hint.setObjectName("mutedLabel")
        media_header.addWidget(drop_hint)

        media_layout.addLayout(media_header)

        self.media_list = MediaListWidget()
        self.media_list.setContextMenuPolicy(Qt.CustomContextMenu)
        media_layout.addWidget(self.media_list, 1)
        right_splitter.addWidget(media_panel)

        detail = QFrame()
        detail.setObjectName("detailCard")
        detail_layout = QHBoxLayout(detail)
        detail_layout.setContentsMargins(14, 14, 14, 14)
        detail_layout.setSpacing(16)

        preview_side = QVBoxLayout()
        preview_side.setSpacing(8)

        self.preview = QLabel("Vyber médium")
        self.preview.setObjectName("preview")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(430, 260)
        preview_side.addWidget(self.preview, 1)

        self.path_label = QLabel("")
        self.path_label.setObjectName("pathLabel")
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.path_label.setWordWrap(False)
        preview_side.addWidget(self.path_label)

        preview_actions = QHBoxLayout()
        self.open_btn = QPushButton("Otevřít soubor")
        self.open_btn.setEnabled(False)
        preview_actions.addWidget(self.open_btn)
        preview_actions.addStretch()
        preview_side.addLayout(preview_actions)

        detail_layout.addLayout(preview_side, 3)

        form_side = QVBoxLayout()
        form_side.setSpacing(7)

        caption_label = QLabel("Krátký popisek")
        caption_label.setObjectName("fieldLabel")
        form_side.addWidget(caption_label)

        self.caption_edit = QLineEdit()
        self.caption_edit.setPlaceholderText(
            "Např. nejlepší moment, reakce, nápad, připomínka…"
        )
        form_side.addWidget(self.caption_edit)

        category_label = QLabel("Kategorie")
        category_label.setObjectName("fieldLabel")
        form_side.addWidget(category_label)

        self.category_combo = QComboBox()
        form_side.addWidget(self.category_combo)

        notes_label = QLabel("Delší poznámka")
        notes_label.setObjectName("fieldLabel")
        form_side.addWidget(notes_label)

        self.notes_edit = QTextEdit()
        self.notes_edit.setPlaceholderText(
            "Kontext, vysvětlení, proč sis to uložil, nápad na později…"
        )
        form_side.addWidget(self.notes_edit, 1)

        buttons = QHBoxLayout()
        self.save_btn = QPushButton("Uložit změny")
        self.save_btn.setObjectName("primaryButton")
        buttons.addWidget(self.save_btn)

        self.delete_media_btn = QPushButton("Smazat z databáze")
        self.delete_media_btn.setObjectName("dangerButton")
        buttons.addWidget(self.delete_media_btn)

        form_side.addLayout(buttons)
        detail_layout.addLayout(form_side, 2)

        right_splitter.addWidget(detail)
        right_splitter.setSizes([470, 330])

        splitter.addWidget(right_splitter)
        splitter.setSizes([245, 1035])

        self.add_media_btn.clicked.connect(self.add_media_dialog)
        self.add_category_btn.clicked.connect(self.add_category)
        self.rename_category_btn.clicked.connect(self.rename_category)
        self.delete_category_btn.clicked.connect(self.delete_category)
        self.category_up_btn.clicked.connect(lambda: self.move_category(-1))
        self.category_down_btn.clicked.connect(lambda: self.move_category(1))

        self.category_list.currentItemChanged.connect(self.on_category_changed)
        self.category_list.itemDoubleClicked.connect(
            lambda _item: self.rename_category()
        )
        self.category_list.customContextMenuRequested.connect(
            self.show_category_context_menu
        )

        self.media_list.currentItemChanged.connect(self.on_media_changed)
        self.media_list.itemDoubleClicked.connect(lambda _item: self.open_current())
        self.media_list.customContextMenuRequested.connect(
            self.show_media_context_menu
        )
        self.media_list.filesDropped.connect(self.add_media_paths)

        self.search_edit.textChanged.connect(lambda _text: self.reload_media())
        self.save_btn.clicked.connect(self.save_current)
        self.delete_media_btn.clicked.connect(self.delete_selected_media)
        self.open_btn.clicked.connect(self.open_current)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QWidget {
                font-size: 13px;
                color: #e9edf2;
            }

            QWidget#root, QMainWindow {
                background: #11151a;
            }

            QFrame#sidebar,
            QFrame#panel,
            QFrame#detailCard {
                background: #181d23;
                border: 1px solid #29313a;
                border-radius: 10px;
            }

            QLabel#appTitle {
                font-size: 22px;
                font-weight: 700;
                padding-right: 10px;
            }

            QLabel#sectionTitle {
                font-size: 16px;
                font-weight: 700;
            }

            QLabel#fieldLabel {
                font-weight: 600;
                margin-top: 3px;
            }

            QLabel#mutedLabel,
            QLabel#pathLabel {
                color: #8e98a5;
                font-size: 12px;
            }

            QLabel#preview {
                background: #0d1014;
                color: #8e98a5;
                border: 1px solid #2b333d;
                border-radius: 8px;
                padding: 8px;
            }

            QLineEdit,
            QTextEdit,
            QComboBox,
            QListWidget {
                background: #11161c;
                color: #edf1f5;
                border: 1px solid #303945;
                border-radius: 7px;
                padding: 7px;
                selection-background-color: #2d6cdf;
            }

            QLineEdit:focus,
            QTextEdit:focus,
            QComboBox:focus,
            QListWidget:focus {
                border: 1px solid #4d86e8;
            }

            QLineEdit#searchBox {
                padding: 9px 11px;
            }

            QListWidget#categoryList {
                padding: 5px;
            }

            QListWidget#categoryList::item {
                padding: 8px 7px;
                border-radius: 6px;
            }

            QListWidget#categoryList::item:selected {
                background: #274e83;
            }

            QListWidget#categoryList::item:hover:!selected {
                background: #222a33;
            }

            QListWidget#mediaList {
                padding: 10px;
                background: #0f1419;
            }

            QListWidget#mediaList[dragActive="true"] {
                border: 2px dashed #5d95f4;
                background: #111d2c;
            }

            QListWidget#mediaList::item {
                border: 1px solid transparent;
                border-radius: 8px;
                padding: 5px;
            }

            QListWidget#mediaList::item:selected {
                background: #243f63;
                border: 1px solid #4d86e8;
            }

            QListWidget#mediaList::item:hover:!selected {
                background: #1c252f;
            }

            QPushButton {
                background: #252c34;
                color: #eef2f6;
                border: 1px solid #35404b;
                border-radius: 7px;
                padding: 8px 11px;
            }

            QPushButton:hover {
                background: #303944;
            }

            QPushButton:pressed {
                background: #20262d;
            }

            QPushButton:disabled {
                color: #69727c;
                background: #1c2127;
                border-color: #272e36;
            }

            QPushButton#primaryButton {
                background: #2d6cdf;
                border-color: #3979e8;
                font-weight: 600;
            }

            QPushButton#primaryButton:hover {
                background: #3778ea;
            }

            QPushButton#dangerButton,
            QPushButton#dangerMiniButton {
                color: #ffb6b6;
            }

            QPushButton#miniButton,
            QPushButton#dangerMiniButton {
                padding: 6px;
                font-size: 16px;
            }

            QMenu {
                background: #1b2128;
                border: 1px solid #36404b;
                padding: 5px;
            }

            QMenu::item {
                padding: 7px 28px 7px 10px;
                border-radius: 5px;
            }

            QMenu::item:selected {
                background: #2d6cdf;
            }

            QStatusBar {
                background: #11151a;
                color: #8e98a5;
            }

            QSplitter::handle {
                background: transparent;
            }
            """
        )

    def _create_shortcuts(self) -> None:
        self.shortcut_open = QShortcut(QKeySequence("Ctrl+O"), self)
        self.shortcut_open.activated.connect(self.add_media_dialog)

        self.shortcut_save = QShortcut(QKeySequence("Ctrl+S"), self)
        self.shortcut_save.activated.connect(self.save_current)

        self.shortcut_search = QShortcut(QKeySequence("Ctrl+F"), self)
        self.shortcut_search.activated.connect(self.search_edit.setFocus)

        self.shortcut_delete = QShortcut(QKeySequence("Delete"), self)
        self.shortcut_delete.activated.connect(self.delete_selected_media)

    def reload_categories(self, keep_category_id: int | None = None) -> None:
        if keep_category_id is None:
            keep_category_id = self.selected_category_id()

        self.category_list.blockSignals(True)
        self.category_combo.blockSignals(True)
        self.category_list.clear()
        self.category_combo.clear()

        total = self.db.total_media_count()
        all_item = QListWidgetItem(f"Vše  ·  {total}")
        all_item.setData(Qt.UserRole, None)
        all_item.setData(Qt.UserRole + 1, "Vše")
        self.category_list.addItem(all_item)

        selected_row = 0
        for row_index, row in enumerate(self.db.categories(), start=1):
            category_id = int(row["id"])
            name = str(row["name"])
            count = int(row["media_count"])

            item = QListWidgetItem(f"{name}  ·  {count}")
            item.setData(Qt.UserRole, category_id)
            item.setData(Qt.UserRole + 1, name)
            self.category_list.addItem(item)

            self.category_combo.addItem(name, category_id)

            if keep_category_id == category_id:
                selected_row = row_index

        self.category_list.setCurrentRow(selected_row)
        self.category_list.blockSignals(False)
        self.category_combo.blockSignals(False)
        self.update_category_controls()

    def selected_category_id(self) -> int | None:
        item = self.category_list.currentItem()
        return None if item is None else item.data(Qt.UserRole)

    def selected_category_name(self) -> str:
        item = self.category_list.currentItem()
        if item is None:
            return "Vše"
        return str(item.data(Qt.UserRole + 1) or "Vše")

    def update_category_controls(self) -> None:
        category_id = self.selected_category_id()
        enabled = category_id is not None
        is_default = (
            enabled and category_id == self.db.default_category_id()
        )

        self.rename_category_btn.setEnabled(enabled and not is_default)
        self.delete_category_btn.setEnabled(enabled and not is_default)
        self.category_up_btn.setEnabled(enabled)
        self.category_down_btn.setEnabled(enabled)

    def reload_media(self, select_media_id: int | None = None) -> None:
        if select_media_id is None:
            select_media_id = self.current_media_id

        rows = self.db.media_items(
            self.selected_category_id(),
            self.search_edit.text(),
        )

        self.media_list.blockSignals(True)
        self.media_list.clear()

        selected_row = -1
        for index, row in enumerate(rows):
            path = Path(row["path"])
            caption = row["caption"].strip()
            title = caption or path.name

            item = QListWidgetItem(title)
            item.setData(Qt.UserRole, int(row["id"]))
            item.setToolTip(
                f"{path}\nKategorie: {row['category_name']}"
            )
            item.setIcon(self.make_icon(path, row["media_type"]))
            item.setTextAlignment(Qt.AlignHCenter | Qt.AlignTop)
            self.media_list.addItem(item)

            if select_media_id == int(row["id"]):
                selected_row = index

        self.media_list.blockSignals(False)

        self.category_title.setText(self.selected_category_name())
        self.media_count_label.setText(f"{len(rows)} položek")

        if selected_row >= 0:
            self.media_list.setCurrentRow(selected_row)
            self.on_media_changed(self.media_list.currentItem(), None)
        elif self.media_list.count() > 0:
            self.media_list.setCurrentRow(0)
            self.on_media_changed(self.media_list.currentItem(), None)
        else:
            self.clear_detail()

    def make_icon(self, path: Path, media_type: str) -> QIcon:
        if path.exists() and media_type in {"image", "gif"}:
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                canvas = QPixmap(180, 125)
                canvas.fill(QColor("#0d1014"))
                scaled = pixmap.scaled(
                    176,
                    121,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
                painter = QPainter(canvas)
                x = (canvas.width() - scaled.width()) // 2
                y = (canvas.height() - scaled.height()) // 2
                painter.drawPixmap(x, y, scaled)
                painter.end()
                return QIcon(canvas)

        if media_type == "video":
            return self.style().standardIcon(QStyle.SP_MediaPlay)

        return self.style().standardIcon(QStyle.SP_FileIcon)

    def add_media_dialog(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Vyber obrázky, GIFy nebo videa",
            str(Path.home()),
            (
                "Média (*.png *.jpg *.jpeg *.webp *.bmp *.gif "
                "*.mp4 *.mkv *.webm *.avi *.mov *.m4v);;"
                "Obrázky a GIFy (*.png *.jpg *.jpeg *.webp *.bmp *.gif);;"
                "Videa (*.mp4 *.mkv *.webm *.avi *.mov *.m4v);;"
                "Všechny soubory (*)"
            ),
        )
        if files:
            self.add_media_paths(files)

    def add_media_paths(self, paths: list[str]) -> None:
        category_id = self.selected_category_id()
        if category_id is None:
            category_id = self.db.default_category_id()

        added = 0
        duplicates = 0
        unsupported = 0

        for raw_path in paths:
            path = Path(raw_path).expanduser()
            if not path.is_file():
                unsupported += 1
                continue

            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                unsupported += 1
                continue

            if self.db.add_media(
                str(path),
                detect_media_type(path),
                int(category_id),
            ):
                added += 1
            else:
                duplicates += 1

        self.reload_categories(category_id)
        self.reload_media()

        parts = []
        if added:
            parts.append(f"přidáno {added}")
        if duplicates:
            parts.append(f"duplicit {duplicates}")
        if unsupported:
            parts.append(f"nepodporovaných {unsupported}")

        message = "Média: " + ", ".join(parts) if parts else "Nebyl přidán žádný soubor."
        self.statusBar().showMessage(message, 5000)

    def add_category(self) -> None:
        name, ok = QInputDialog.getText(
            self,
            "Nová kategorie",
            "Název kategorie:",
        )
        name = name.strip()
        if not ok or not name:
            return

        try:
            category_id = self.db.add_category(name)
        except sqlite3.IntegrityError:
            QMessageBox.information(
                self,
                "Kategorie",
                "Kategorie s tímto názvem už existuje.",
            )
            return

        self.reload_categories(category_id)
        self.reload_media()
        self.statusBar().showMessage(f"Vytvořena kategorie „{name}“.", 3500)

    def rename_category(self) -> None:
        item = self.category_list.currentItem()
        if item is None:
            return

        category_id = item.data(Qt.UserRole)
        if category_id is None:
            return

        current_name = str(item.data(Qt.UserRole + 1) or "")
        if int(category_id) == self.db.default_category_id():
            QMessageBox.information(
                self,
                "Kategorie",
                "Kategorie „Nezařazené“ má pevný název.",
            )
            return

        name, ok = QInputDialog.getText(
            self,
            "Přejmenovat kategorii",
            "Nový název:",
            text=current_name,
        )
        name = name.strip()
        if not ok or not name or name == current_name:
            return

        try:
            self.db.rename_category(int(category_id), name)
        except sqlite3.IntegrityError:
            QMessageBox.information(
                self,
                "Kategorie",
                "Kategorie s tímto názvem už existuje.",
            )
            return
        except ValueError as exc:
            QMessageBox.information(self, "Kategorie", str(exc))
            return

        self.reload_categories(int(category_id))
        self.reload_media()
        self.statusBar().showMessage(
            f"Kategorie přejmenována na „{name}“.",
            3500,
        )

    def move_category(self, direction: int) -> None:
        category_id = self.selected_category_id()
        if category_id is None:
            return

        if self.db.move_category(int(category_id), direction):
            self.reload_categories(int(category_id))
            self.reload_media()

    def delete_category(self) -> None:
        item = self.category_list.currentItem()
        if item is None:
            return

        category_id = item.data(Qt.UserRole)
        if category_id is None:
            return

        name = str(item.data(Qt.UserRole + 1) or "kategorii")

        if int(category_id) == self.db.default_category_id():
            QMessageBox.information(
                self,
                "Kategorie",
                "Výchozí kategorii „Nezařazené“ nelze smazat.",
            )
            return

        answer = QMessageBox.question(
            self,
            "Smazat kategorii",
            f"Smazat kategorii „{name}“?\n\n"
            "Média z ní se přesunou do „Nezařazené“. "
            "Původní soubory v počítači zůstanou beze změny.",
        )
        if answer != QMessageBox.Yes:
            return

        try:
            self.db.delete_category(int(category_id))
        except ValueError as exc:
            QMessageBox.information(self, "Kategorie", str(exc))
            return

        default_id = self.db.default_category_id()
        self.reload_categories(default_id)
        self.reload_media()
        self.statusBar().showMessage(f"Kategorie „{name}“ smazána.", 3500)

    def show_category_context_menu(self, pos) -> None:
        menu = QMenu(self)

        new_action = menu.addAction("Nová kategorie")
        rename_action = menu.addAction("Přejmenovat")
        menu.addSeparator()
        up_action = menu.addAction("Posunout nahoru")
        down_action = menu.addAction("Posunout dolů")
        menu.addSeparator()
        delete_action = menu.addAction("Smazat kategorii")

        category_id = self.selected_category_id()
        is_real = category_id is not None
        is_default = (
            is_real and int(category_id) == self.db.default_category_id()
        )

        rename_action.setEnabled(is_real and not is_default)
        up_action.setEnabled(is_real)
        down_action.setEnabled(is_real)
        delete_action.setEnabled(is_real and not is_default)

        chosen = menu.exec(self.category_list.mapToGlobal(pos))
        if chosen == new_action:
            self.add_category()
        elif chosen == rename_action:
            self.rename_category()
        elif chosen == up_action:
            self.move_category(-1)
        elif chosen == down_action:
            self.move_category(1)
        elif chosen == delete_action:
            self.delete_category()

    def on_category_changed(self, current, previous) -> None:
        self.update_category_controls()
        self.reload_media()

    def selected_media_ids(self) -> list[int]:
        ids = []
        for item in self.media_list.selectedItems():
            media_id = item.data(Qt.UserRole)
            if media_id is not None:
                ids.append(int(media_id))
        return ids

    def on_media_changed(self, current, previous) -> None:
        if current is None:
            self.clear_detail()
            return

        media_id = current.data(Qt.UserRole)
        row = self.db.media_by_id(int(media_id))
        if row is None:
            self.clear_detail()
            return

        self.current_media_id = int(row["id"])
        self.caption_edit.setText(row["caption"])
        self.notes_edit.setPlainText(row["notes"])

        combo_index = self.category_combo.findData(int(row["category_id"]))
        if combo_index >= 0:
            self.category_combo.setCurrentIndex(combo_index)

        path = Path(row["path"])
        self.path_label.setText(str(path))
        self.path_label.setToolTip(str(path))
        self.show_preview(path, row["media_type"])
        self.open_btn.setEnabled(True)

    def show_preview(self, path: Path, media_type: str) -> None:
        self.current_preview_path = path
        self.current_preview_type = media_type

        if self.current_movie is not None:
            self.current_movie.stop()
            self.current_movie = None
            self.preview.setMovie(None)

        self.preview.clear()

        if not path.exists():
            self.preview.setText(f"Soubor nenalezen\n\n{path}")
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
            self._refresh_static_preview()
            return

        if media_type == "video":
            self.preview.setPixmap(
                self.style()
                .standardIcon(QStyle.SP_MediaPlay)
                .pixmap(QSize(96, 96))
            )
            self.preview.setToolTip(
                "Video se zatím otevírá v systémovém přehrávači."
            )
            return

        self.preview.setText(path.name)

    def _refresh_static_preview(self) -> None:
        if (
            self.current_preview_path is None
            or self.current_preview_type != "image"
            or not self.current_preview_path.exists()
        ):
            return

        pixmap = QPixmap(str(self.current_preview_path))
        if pixmap.isNull():
            return

        target = QSize(
            max(100, self.preview.width() - 20),
            max(100, self.preview.height() - 20),
        )
        self.preview.setPixmap(
            pixmap.scaled(
                target,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.current_movie is not None:
            self.current_movie.setScaledSize(
                QSize(
                    max(100, self.preview.width() - 20),
                    max(100, self.preview.height() - 20),
                )
            )
        elif self.current_preview_type == "image":
            self._refresh_static_preview()

    def save_current(self) -> None:
        if self.current_media_id is None:
            return

        category_id = self.category_combo.currentData()
        if category_id is None:
            return

        media_id = self.current_media_id
        selected_category = self.selected_category_id()

        self.db.update_media(
            media_id,
            self.caption_edit.text().strip(),
            self.notes_edit.toPlainText().strip(),
            int(category_id),
        )

        self.reload_categories(selected_category)
        self.reload_media(select_media_id=media_id)
        self.statusBar().showMessage("Změny uloženy.", 3000)

    def move_selected_media_to(self, category_id: int) -> None:
        media_ids = self.selected_media_ids()
        if not media_ids and self.current_media_id is not None:
            media_ids = [self.current_media_id]
        if not media_ids:
            return

        self.db.move_media(media_ids, category_id)
        self.reload_categories(self.selected_category_id())
        self.reload_media()
        self.statusBar().showMessage(
            f"Přesunuto {len(media_ids)} položek.",
            3500,
        )

    def delete_selected_media(self) -> None:
        media_ids = self.selected_media_ids()
        if not media_ids and self.current_media_id is not None:
            media_ids = [self.current_media_id]
        if not media_ids:
            return

        count = len(media_ids)
        text = (
            "Smazat vybranou položku z databáze?"
            if count == 1
            else f"Smazat {count} vybraných položek z databáze?"
        )

        answer = QMessageBox.question(
            self,
            "Smazat z databáze",
            text
            + "\n\nPůvodní soubory v počítači zůstanou nedotčené.",
        )
        if answer != QMessageBox.Yes:
            return

        self.db.delete_media_ids(media_ids)
        self.current_media_id = None
        self.reload_categories(self.selected_category_id())
        self.reload_media()
        self.statusBar().showMessage(
            f"Smazáno {count} položek z databáze.",
            3500,
        )

    def show_media_context_menu(self, pos) -> None:
        item = self.media_list.itemAt(pos)
        if item is None:
            return

        if not item.isSelected():
            self.media_list.clearSelection()
            item.setSelected(True)
            self.media_list.setCurrentItem(item)

        menu = QMenu(self)
        open_action = menu.addAction("Otevřít původní soubor")
        move_menu = menu.addMenu("Přesunout do kategorie")

        for row in self.db.categories():
            category_id = int(row["id"])
            action = move_menu.addAction(str(row["name"]))
            action.setData(category_id)

        menu.addSeparator()
        delete_action = menu.addAction("Smazat z databáze")

        chosen = menu.exec(self.media_list.mapToGlobal(pos))
        if chosen == open_action:
            self.open_current()
        elif chosen == delete_action:
            self.delete_selected_media()
        elif chosen is not None and chosen.parent() == move_menu:
            category_id = chosen.data()
            if category_id is not None:
                self.move_selected_media_to(int(category_id))

    def open_current(self) -> None:
        if self.current_media_id is None:
            return

        row = self.db.media_by_id(self.current_media_id)
        if row is None:
            return

        path = Path(row["path"])
        if not path.exists():
            QMessageBox.warning(
                self,
                "Soubor",
                "Původní soubor už na této cestě není.",
            )
            return

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def clear_detail(self) -> None:
        self.current_media_id = None
        self.current_preview_path = None
        self.current_preview_type = None

        if self.current_movie is not None:
            self.current_movie.stop()
            self.current_movie = None

        self.preview.clear()
        self.preview.setText("Vyber médium")
        self.preview.setToolTip("")
        self.path_label.clear()
        self.caption_edit.clear()
        self.notes_edit.clear()
        self.open_btn.setEnabled(False)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("MediaNotes")
    app.setStyle("Fusion")

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

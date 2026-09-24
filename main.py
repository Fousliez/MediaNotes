from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, QTimer, QUrl, Signal
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
    QDialog,
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
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PySide6.QtMultimediaWidgets import QVideoWidget

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

APP_VERSION = "0.9.6"

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DB_PATH = DATA_DIR / "media_notes.db"
THUMB_DIR = DATA_DIR / "thumbnails"

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
GIF_EXTENSIONS = {".gif"}
VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".webm",
    ".avi",
    ".mov",
    ".m4v",
    ".mpeg",
    ".mpg",
    ".wmv",
    ".flv",
    ".ogv",
    ".ts",
    ".mts",
    ".m2ts",
    ".3gp",
    ".3g2",
    ".vob",
    ".asf",
}
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | GIF_EXTENSIONS | VIDEO_EXTENSIONS
FINGERPRINT_CHUNK = 256 * 1024


def fingerprint_file(path: Path) -> str | None:
    try:
        size = path.stat().st_size
        digest = hashlib.sha256()
        digest.update(str(size).encode("ascii"))
        with path.open("rb") as handle:
            digest.update(handle.read(FINGERPRINT_CHUNK))
            if size > FINGERPRINT_CHUNK:
                handle.seek(max(0, size - FINGERPRINT_CHUNK))
                digest.update(handle.read(FINGERPRINT_CHUNK))
        return digest.hexdigest()
    except (OSError, PermissionError):
        return None


def file_identity(path: Path) -> tuple[int | None, int | None, int | None, str | None]:
    try:
        stat = path.stat()
    except (OSError, PermissionError):
        return None, None, None, None
    return int(stat.st_dev), int(stat.st_ino), int(stat.st_size), fingerprint_file(path)


def normalized_path(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve(strict=False))


def extract_drop_paths(event) -> list[str]:
    mime = event.mimeData()
    paths: list[str] = []

    if mime.hasUrls():
        for url in mime.urls():
            if url.isLocalFile():
                local = url.toLocalFile()
                if local:
                    paths.append(local)

    # Fallback pro některé linuxové správce souborů, které předají
    # text/uri-list trochu jinak než Qt očekává.
    if not paths and mime.hasText():
        for line in mime.text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            url = QUrl(line)
            if url.isLocalFile():
                local = url.toLocalFile()
                if local:
                    paths.append(local)
            elif Path(line).expanduser().exists():
                paths.append(line)

    # Zachovej pořadí a odstraň duplicity.
    return list(dict.fromkeys(paths))


def video_thumbnail_path(media_id: int) -> Path:
    return THUMB_DIR / f"{media_id}.jpg"


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
                rating INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                file_device INTEGER,
                file_inode INTEGER,
                file_size INTEGER,
                fingerprint TEXT,
                FOREIGN KEY (category_id) REFERENCES categories(id)
            );

            CREATE INDEX IF NOT EXISTS idx_media_category
            ON media(category_id);

            CREATE TABLE IF NOT EXISTS notebook (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                content TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        self.conn.commit()

    def _migrate_schema(self) -> None:
        category_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(categories)").fetchall()
        }
        if "sort_order" not in category_columns:
            self.conn.execute(
                "ALTER TABLE categories ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"
            )

        media_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(media)").fetchall()
        }
        if "rating" not in media_columns:
            self.conn.execute(
                "ALTER TABLE media ADD COLUMN rating INTEGER NOT NULL DEFAULT 0"
            )

        identity_columns = {
            "file_device": "INTEGER",
            "file_inode": "INTEGER",
            "file_size": "INTEGER",
            "fingerprint": "TEXT",
        }
        for name, sql_type in identity_columns.items():
            if name not in media_columns:
                self.conn.execute(
                    f"ALTER TABLE media ADD COLUMN {name} {sql_type}"
                )

        self.conn.commit()

    def _backfill_media_identity(self) -> None:
        rows = self.conn.execute(
            """
            SELECT id, path
            FROM media
            WHERE file_device IS NULL
               OR file_inode IS NULL
               OR file_size IS NULL
               OR fingerprint IS NULL
            """
        ).fetchall()

        changed = False
        for row in rows:
            path = Path(row["path"])
            if not path.is_file():
                continue
            device, inode, size, fingerprint = file_identity(path)
            self.conn.execute(
                """
                UPDATE media
                SET file_device = ?, file_inode = ?, file_size = ?, fingerprint = ?
                WHERE id = ?
                """,
                (device, inode, size, fingerprint, int(row["id"])),
            )
            changed = True

        if changed:
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
        normalized = normalized_path(path)
        exists = self.conn.execute(
            "SELECT id FROM media WHERE path = ? LIMIT 1",
            (normalized,),
        ).fetchone()
        if exists is not None:
            return False

        file_path = Path(normalized)
        device, inode, size, fingerprint = file_identity(file_path)
        self.conn.execute(
            """
            INSERT INTO media(
                path,
                media_type,
                category_id,
                file_device,
                file_inode,
                file_size,
                fingerprint
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized,
                media_type,
                category_id,
                device,
                inode,
                size,
                fingerprint,
            ),
        )
        self.conn.commit()
        return True

    def tracked_media(self):
        return self.conn.execute(
            """
            SELECT id, path, file_device, file_inode, file_size, fingerprint
            FROM media
            ORDER BY id
            """
        ).fetchall()

    def missing_media(self):
        return [
            row
            for row in self.tracked_media()
            if not Path(row["path"]).is_file()
        ]

    def media_id_for_path(self, path: str | Path) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM media WHERE path = ? LIMIT 1",
            (normalized_path(path),),
        ).fetchone()
        return None if row is None else int(row["id"])

    def update_media_path(self, media_id: int, new_path: str | Path) -> None:
        normalized = normalized_path(new_path)
        path = Path(normalized)
        device, inode, size, fingerprint = file_identity(path)
        self.conn.execute(
            """
            UPDATE media
            SET path = ?,
                media_type = ?,
                file_device = ?,
                file_inode = ?,
                file_size = ?,
                fingerprint = ?
            WHERE id = ?
            """,
            (
                normalized,
                detect_media_type(path),
                device,
                inode,
                size,
                fingerprint,
                media_id,
            ),
        )
        self.conn.commit()

    def update_moved_path(self, old_path: str | Path, new_path: str | Path) -> int | None:
        media_id = self.media_id_for_path(old_path)
        if media_id is None:
            return None
        self.update_media_path(media_id, new_path)
        return media_id

    def update_moved_directory(
        self,
        old_dir: str | Path,
        new_dir: str | Path,
    ) -> list[int]:
        old_prefix = normalized_path(old_dir).rstrip(os.sep) + os.sep
        new_prefix = normalized_path(new_dir).rstrip(os.sep) + os.sep
        rows = self.conn.execute(
            "SELECT id, path FROM media WHERE path LIKE ?",
            (old_prefix + "%",),
        ).fetchall()

        updated: list[int] = []
        for row in rows:
            old_path = str(row["path"])
            suffix = old_path[len(old_prefix):]
            new_path = new_prefix + suffix
            self.update_media_path(int(row["id"]), new_path)
            updated.append(int(row["id"]))
        return updated

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
        rating: int,
    ) -> None:
        rating = max(0, min(3, int(rating)))
        self.conn.execute(
            """
            UPDATE media
            SET caption = ?, notes = ?, category_id = ?, rating = ?
            WHERE id = ?
            """,
            (caption, notes, category_id, rating, media_id),
        )
        self.conn.commit()

    def update_media_rating(self, media_id: int, rating: int) -> None:
        rating = max(0, min(3, int(rating)))
        self.conn.execute(
            "UPDATE media SET rating = ? WHERE id = ?",
            (rating, media_id),
        )
        self.conn.commit()

    def update_media_ratings(self, media_ids: list[int], rating: int) -> None:
        if not media_ids:
            return
        rating = max(0, min(3, int(rating)))
        placeholders = ",".join("?" for _ in media_ids)
        self.conn.execute(
            f"UPDATE media SET rating = ? WHERE id IN ({placeholders})",
            [rating, *media_ids],
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

    def notebook_content(self) -> str:
        row = self.conn.execute(
            "SELECT content FROM notebook WHERE id = 1"
        ).fetchone()
        return "" if row is None else str(row["content"])

    def save_notebook(self, content: str) -> None:
        self.conn.execute(
            """
            INSERT INTO notebook(id, content, updated_at)
            VALUES (1, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                content = excluded.content,
                updated_at = CURRENT_TIMESTAMP
            """,
            (content,),
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
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self.setDefaultDropAction(Qt.CopyAction)
        self.setDropIndicatorShown(False)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setViewMode(QListWidget.IconMode)
        self.setResizeMode(QListWidget.Adjust)
        self.setMovement(QListWidget.Static)
        self.setIconSize(QSize(160, 106))
        self.setGridSize(QSize(185, 140))
        self.setSpacing(3)

    def _set_drag_active(self, active: bool) -> None:
        self.setProperty("dragActive", active)
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.RightButton:
            item = self.itemAt(event.position().toPoint())
            if item is not None and item.isSelected():
                event.accept()
                return
        super().mousePressEvent(event)

    def dragEnterEvent(self, event) -> None:
        paths = extract_drop_paths(event)
        if paths:
            event.setDropAction(Qt.CopyAction)
            event.accept()
            self._set_drag_active(True)
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if extract_drop_paths(event):
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._set_drag_active(False)
        event.accept()

    def dropEvent(self, event) -> None:
        self._set_drag_active(False)
        paths = extract_drop_paths(event)
        if not paths:
            event.ignore()
            return

        self.filesDropped.emit(paths)
        event.setDropAction(Qt.CopyAction)
        event.accept()

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


class VideoThumbnailer(QObject):
    thumbnailReady = Signal(int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        THUMB_DIR.mkdir(parents=True, exist_ok=True)

        self.queue: list[tuple[int, Path]] = []
        self.queued_ids: set[int] = set()
        self.current_id: int | None = None
        self.current_path: Path | None = None

        self.audio = QAudioOutput(self)
        self.audio.setMuted(True)

        self.sink = QVideoSink(self)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.sink)

        self.sink.videoFrameChanged.connect(self._on_frame)
        self.player.mediaStatusChanged.connect(self._on_status)

    def request(self, media_id: int, path: Path) -> None:
        if not path.is_file():
            return

        thumb = video_thumbnail_path(media_id)
        try:
            if thumb.is_file() and thumb.stat().st_mtime >= path.stat().st_mtime:
                return
        except OSError:
            pass

        if self.current_id == media_id or media_id in self.queued_ids:
            return

        self.queue.append((media_id, path))
        self.queued_ids.add(media_id)

        if self.current_id is None:
            QTimer.singleShot(0, self._next)

    def _next(self) -> None:
        if self.current_id is not None or not self.queue:
            return

        media_id, path = self.queue.pop(0)
        self.queued_ids.discard(media_id)

        if not path.is_file():
            QTimer.singleShot(0, self._next)
            return

        self.current_id = media_id
        self.current_path = path
        self.player.stop()
        self.player.setSource(QUrl.fromLocalFile(str(path)))

    def _on_status(self, status) -> None:
        if self.current_id is None:
            return

        if status == QMediaPlayer.MediaStatus.LoadedMedia:
            duration = self.player.duration()
            position = 700
            if duration > 0:
                position = min(1200, max(100, duration // 10))
            self.player.setPosition(position)
            self.player.play()
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._finish_current(None)

    def _on_frame(self, frame) -> None:
        if self.current_id is None or not frame.isValid():
            return

        image = frame.toImage()
        if image.isNull():
            return

        target = video_thumbnail_path(self.current_id)
        THUMB_DIR.mkdir(parents=True, exist_ok=True)

        scaled = image.scaled(
            360,
            220,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )

        if scaled.save(str(target), "JPG", 88):
            self._finish_current(target)

    def _finish_current(self, target: Path | None) -> None:
        media_id = self.current_id
        self.player.stop()
        self.player.setSource(QUrl())
        self.current_id = None
        self.current_path = None

        if media_id is not None and target is not None:
            self.thumbnailReady.emit(media_id, str(target))

        QTimer.singleShot(0, self._next)


class TrackerBridge(QObject):
    moved = Signal(str, str, bool)
    created = Signal(str)
    deleted = Signal(str, bool)
    recovered = Signal(int, str)
    recoveryFinished = Signal(int)


class TrackerEventHandler(FileSystemEventHandler):
    def __init__(self, bridge: TrackerBridge):
        super().__init__()
        self.bridge = bridge

    def on_moved(self, event) -> None:
        self.bridge.moved.emit(
            str(event.src_path),
            str(event.dest_path),
            bool(event.is_directory),
        )

    def on_created(self, event) -> None:
        if not event.is_directory:
            self.bridge.created.emit(str(event.src_path))

    def on_deleted(self, event) -> None:
        self.bridge.deleted.emit(str(event.src_path), bool(event.is_directory))


def live_watch_dirs(paths: list[str]) -> list[Path]:
    """Sleduj jen konkrétní složky, kde média skutečně leží."""
    roots: list[Path] = []
    seen: set[str] = set()

    for raw in paths:
        parent = Path(raw).expanduser().parent
        try:
            resolved = parent.resolve()
        except OSError:
            continue
        if not resolved.is_dir():
            continue

        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        roots.append(resolved)

    return roots


def recovery_roots(paths: list[str]) -> list[Path]:
    """Širší hledání běží jen ve worker threadu, nikdy v GUI vlákně."""
    candidates: list[Path] = [Path.home()]

    media_root = Path("/media") / Path.home().name
    if media_root.exists():
        candidates.append(media_root)

    if Path("/mnt").exists():
        candidates.append(Path("/mnt"))

    for raw in paths:
        parent = Path(raw).expanduser().parent
        if parent.exists():
            candidates.append(parent)

    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not resolved.is_dir():
            continue

        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        roots.append(resolved)

    return roots


def recover_missing_files(
    bridge: TrackerBridge,
    missing_rows: list[dict],
    roots: list[Path],
) -> None:
    if not missing_rows:
        bridge.recoveryFinished.emit(0)
        return

    inode_targets: dict[tuple[int, int], int] = {}
    fingerprint_targets: dict[tuple[int, str], int] = {}
    unresolved: set[int] = set()

    for row in missing_rows:
        original_path = Path(str(row["path"]))
        if original_path.is_file():
            continue

        media_id = int(row["id"])
        unresolved.add(media_id)
        device = row.get("file_device")
        inode = row.get("file_inode")
        size = row.get("file_size")
        fingerprint = row.get("fingerprint")

        if device is not None and inode is not None:
            inode_targets[(int(device), int(inode))] = media_id
        if size is not None and fingerprint:
            fingerprint_targets[(int(size), str(fingerprint))] = media_id

    if not unresolved:
        bridge.recoveryFinished.emit(0)
        return

    skip_dirs = {
        ".cache",
        ".git",
        ".venv",
        "__pycache__",
        "node_modules",
        "Trash",
    }
    recovered = 0

    for root in roots:
        if not unresolved:
            break

        try:
            walker = os.walk(root, topdown=True, followlinks=False)
            for dirpath, dirnames, filenames in walker:
                dirnames[:] = [
                    name for name in dirnames
                    if name not in skip_dirs
                ]

                for filename in filenames:
                    if not unresolved:
                        break

                    path = Path(dirpath) / filename
                    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                        continue

                    try:
                        stat = path.stat()
                    except (OSError, PermissionError):
                        continue

                    media_id = inode_targets.get(
                        (int(stat.st_dev), int(stat.st_ino))
                    )

                    if media_id is None and int(stat.st_size) > 0:
                        possible = [
                            (key, target_id)
                            for key, target_id in fingerprint_targets.items()
                            if key[0] == int(stat.st_size)
                            and target_id in unresolved
                        ]
                        if possible:
                            fingerprint = fingerprint_file(path)
                            if fingerprint is not None:
                                media_id = fingerprint_targets.get(
                                    (int(stat.st_size), fingerprint)
                                )

                    if media_id is None or media_id not in unresolved:
                        continue

                    unresolved.remove(media_id)
                    recovered += 1
                    bridge.recovered.emit(media_id, str(path))
        except (OSError, PermissionError):
            continue

    bridge.recoveryFinished.emit(recovered)


class NotebookDialog(QDialog):
    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.saved_content = self.db.notebook_content()
        self.saved_feedback_active = False
        self.editing = False
        self.closing_after_save = False

        self.setWindowTitle("Sešit")
        self.resize(760, 560)
        self.setMinimumSize(520, 360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(8)

        heading = QLabel("Sešit")
        heading.setObjectName("notebookTitle")
        layout.addWidget(heading)

        hint = QLabel("Volný dokument pro poznámky, nápady a texty.")
        hint.setObjectName("mutedLabel")
        layout.addWidget(hint)

        self.editor = QTextEdit()
        self.editor.setObjectName("notebookEditor")
        self.editor.setPlaceholderText("Začni psát…")
        self.editor.setPlainText(self.saved_content)
        self.editor.setReadOnly(True)
        self.editor.setProperty("editing", False)
        layout.addWidget(self.editor, 1)

        footer = QHBoxLayout()
        self.status_label = QLabel("")
        self.status_label.setObjectName("mutedLabel")
        footer.addWidget(self.status_label)
        footer.addStretch()

        self.edit_btn = QPushButton("Editovat")
        self.edit_btn.setObjectName("notebookEditButton")
        self.edit_btn.setFixedSize(96, 30)
        footer.addWidget(self.edit_btn)

        self.save_btn = QPushButton("Uložit")
        self.save_btn.setObjectName("notebookSaveButton")
        self.save_btn.setFixedSize(96, 30)
        self.save_btn.setEnabled(True)
        self.save_btn.setProperty("dirty", False)
        self.save_btn.setProperty("saved", False)
        footer.addWidget(self.save_btn)
        layout.addLayout(footer)

        self.editor.textChanged.connect(self._on_changed)
        self.edit_btn.clicked.connect(self.toggle_editing)
        self.save_btn.clicked.connect(self.save)

        self.shortcut_save = QShortcut(QKeySequence("Ctrl+S"), self)
        self.shortcut_save.activated.connect(self.save)

        self.shortcut_enter_save = QShortcut(QKeySequence(Qt.Key_Return), self)
        self.shortcut_enter_save.activated.connect(self._save_with_enter)

        self.shortcut_numpad_enter_save = QShortcut(
            QKeySequence(Qt.Key_Enter),
            self,
        )
        self.shortcut_numpad_enter_save.activated.connect(self._save_with_enter)

    def _save_with_enter(self) -> None:
        if QApplication.focusWidget() is self.editor and not self.editor.isReadOnly():
            return
        self.save()

    def _set_editing(self, editing: bool) -> None:
        self.editing = bool(editing)
        self.editor.setReadOnly(not self.editing)
        self.edit_btn.setText("Hotovo" if self.editing else "Editovat")
        self.editor.setProperty("editing", self.editing)
        self.editor.style().unpolish(self.editor)
        self.editor.style().polish(self.editor)
        self.editor.update()

        if self.editing:
            self.editor.setFocus()
            self.status_label.setText(
                "Neuložené změny" if self._is_dirty() else "Režim úprav"
            )
        elif not self._is_dirty():
            self.status_label.setText("")

    def toggle_editing(self) -> None:
        if not self.editing:
            self._set_editing(True)
            return

        if not self._is_dirty():
            self._set_editing(False)
            return

        answer = QMessageBox.warning(
            self,
            "Neuložené změny",
            "V sešitu jsou neuložené změny.",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save,
        )

        if answer == QMessageBox.Save:
            self.save()
        elif answer == QMessageBox.Discard:
            self.editor.blockSignals(True)
            self.editor.setPlainText(self.saved_content)
            self.editor.blockSignals(False)
            self._on_changed()
            self._set_editing(False)

    def _repolish_button(self) -> None:
        self.save_btn.style().unpolish(self.save_btn)
        self.save_btn.style().polish(self.save_btn)
        self.save_btn.update()

    def _is_dirty(self) -> bool:
        return self.editor.toPlainText() != self.saved_content

    def _on_changed(self) -> None:
        self.saved_feedback_active = False
        dirty = self._is_dirty()
        self.save_btn.setProperty("saved", False)
        self.save_btn.setProperty("dirty", dirty)
        self.save_btn.setText("Uložit")
        self.save_btn.setEnabled(True)
        self.status_label.setText("Neuložené změny" if dirty else "")
        self._repolish_button()

    def save(self) -> None:
        if self.closing_after_save:
            return

        if self._is_dirty():
            content = self.editor.toPlainText()
            self.db.save_notebook(content)
            self.saved_content = content

        self.saved_feedback_active = False
        self.closing_after_save = True
        self.save_btn.setProperty("dirty", False)
        self.save_btn.setProperty("saved", False)
        self.save_btn.setText("Uložit")
        self.save_btn.setEnabled(True)
        self._repolish_button()
        self._set_editing(False)

        self.save_btn.setDown(True)
        QTimer.singleShot(250, self._finish_save_and_close)

    def _finish_save_and_close(self) -> None:
        self.save_btn.setDown(False)
        self.closing_after_save = False
        self.accept()

    def _finish_saved_feedback(self) -> None:
        if not self.saved_feedback_active:
            return
        self.saved_feedback_active = False
        self.save_btn.setProperty("saved", False)
        self.save_btn.setText("Uložit")
        self.save_btn.setEnabled(True)
        self.status_label.setText("")
        self._repolish_button()

    def closeEvent(self, event) -> None:
        if not self._is_dirty():
            event.accept()
            return

        answer = QMessageBox.warning(
            self,
            "Neuložené změny",
            "V sešitu jsou neuložené změny.",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save,
        )

        if answer == QMessageBox.Save:
            self.save()
            event.ignore()
        elif answer == QMessageBox.Discard:
            self.editor.blockSignals(True)
            self.editor.setPlainText(self.saved_content)
            self.editor.blockSignals(False)
            self._on_changed()
            event.accept()
        else:
            event.ignore()


class GuardedNoteEdit(QTextEdit):
    editRequested = Signal()

    def mouseDoubleClickEvent(self, event) -> None:
        if self.isReadOnly():
            self.editRequested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class CategoryDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index) -> None:
        super().paint(painter, option, index)

        if index.row() == 0:
            painter.save()
            painter.setPen(QColor("#cfd5dc"))
            y = option.rect.bottom() + 1
            painter.drawLine(
                option.rect.left() + 6,
                y,
                option.rect.right() - 6,
                y,
            )
            painter.restore()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.db = Database(DB_PATH)
        self.current_media_id: int | None = None
        self.current_movie: QMovie | None = None
        self.current_preview_path: Path | None = None
        self.current_preview_type: str | None = None
        self.current_rating = 0
        self.loaded_detail_state: tuple[str, str, int, int] | None = None
        self.loading_detail = False
        self.thumbnail_mode = "medium"
        self.sort_descending = True
        self.save_feedback_active = False
        self.notebook_dialog: NotebookDialog | None = None

        self.audio_output = QAudioOutput(self)
        self.audio_output.setMuted(False)
        self.media_player = QMediaPlayer(self)
        self.media_player.setAudioOutput(self.audio_output)

        self.thumbnailer = VideoThumbnailer(self)
        self.thumbnailer.thumbnailReady.connect(self._on_thumbnail_ready)

        self.tracker_bridge = TrackerBridge()
        self.tracker_observer: Observer | None = None
        self.recovery_thread: threading.Thread | None = None
        self.tracker_signals_connected = False

        self.setWindowTitle(f"Zobrazovač v{APP_VERSION}")
        self.setAcceptDrops(True)
        self.resize(1180, 740)
        self.setMinimumSize(850, 560)

        self._build_ui()
        self._apply_style()
        self._create_shortcuts()

        self.reload_categories()
        self.reload_media()
        self.statusBar().showMessage("Připraveno", 2500)

        # Okno se zobrazí hned. Sledovač a hledání přesunutých souborů
        # se spouští až potom, aby neblokovaly start aplikace.
        QTimer.singleShot(1200, self._start_background_services)

    def _start_background_services(self) -> None:
        self._start_file_tracker()
        self._start_missing_recovery()

    def _start_file_tracker(self) -> None:
        if not self.tracker_signals_connected:
            self.tracker_bridge.moved.connect(self._on_tracked_move)
            self.tracker_bridge.created.connect(self._on_tracked_created)
            self.tracker_bridge.deleted.connect(self._on_tracked_deleted)
            self.tracker_bridge.recovered.connect(self._on_file_recovered)
            self.tracker_bridge.recoveryFinished.connect(
                self._on_recovery_finished
            )
            self.tracker_signals_connected = True

        paths = [str(row["path"]) for row in self.db.tracked_media()]
        if not paths:
            return

        observer = Observer()
        handler = TrackerEventHandler(self.tracker_bridge)
        scheduled = 0

        # Zásadně nerekurzivně. Dřívější verze sledovala celý domovský
        # adresář a na větším stromu dokázala desktop prakticky zmrazit.
        for root in live_watch_dirs(paths):
            try:
                observer.schedule(handler, str(root), recursive=False)
                scheduled += 1
            except (OSError, PermissionError):
                continue

        if scheduled:
            observer.start()
            self.tracker_observer = observer

    def _start_missing_recovery(self, only_media_id: int | None = None) -> None:
        if self.recovery_thread is not None and self.recovery_thread.is_alive():
            return

        # Na GUI vlákně jen rychle načteme řádky ze SQLite.
        # Kontrola existence i případné procházení disku běží až ve workeru.
        rows = list(self.db.tracked_media())
        if only_media_id is not None:
            rows = [
                row for row in rows
                if int(row["id"]) == int(only_media_id)
            ]
        if not rows:
            return

        payload = [dict(row) for row in rows]
        roots = recovery_roots([str(row["path"]) for row in rows])

        self.recovery_thread = threading.Thread(
            target=recover_missing_files,
            args=(self.tracker_bridge, payload, roots),
            daemon=True,
            name="MediaNotesRecovery",
        )
        self.recovery_thread.start()

    def _on_tracked_move(
        self,
        old_path: str,
        new_path: str,
        is_directory: bool,
    ) -> None:
        updated: list[int] = []
        if is_directory:
            updated = self.db.update_moved_directory(old_path, new_path)
        else:
            media_id = self.db.update_moved_path(old_path, new_path)
            if media_id is not None:
                updated = [media_id]

        if not updated:
            return

        current = self.current_media_id
        self.reload_categories(self.selected_category_id())
        self.reload_media(select_media_id=current)
        self.statusBar().showMessage(
            "Sledovač aktualizoval cestu k médiu.",
            4000,
        )

    def _on_tracked_created(self, path: str) -> None:
        file_path = Path(path)
        if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            return

    def _on_tracked_deleted(self, path: str, is_directory: bool) -> None:
        if is_directory:
            affected = [
                row
                for row in self.db.tracked_media()
                if normalized_path(row["path"]).startswith(
                    normalized_path(path).rstrip(os.sep) + os.sep
                )
            ]
            if affected:
                self._start_missing_recovery()
            return

        media_id = self.db.media_id_for_path(path)
        if media_id is not None:
            self._start_missing_recovery(media_id)

    def _on_file_recovered(self, media_id: int, new_path: str) -> None:
        self.db.update_media_path(media_id, new_path)
        current = self.current_media_id
        self.reload_categories(self.selected_category_id())
        self.reload_media(select_media_id=current)
        self.statusBar().showMessage(
            f"Sledovač našel přesunutý soubor: {Path(new_path).name}",
            5000,
        )

    def _on_recovery_finished(self, recovered: int) -> None:
        if recovered:
            self.statusBar().showMessage(
                f"Sledovač opravil {recovered} přesunutých souborů.",
                5000,
            )

    def dragEnterEvent(self, event) -> None:
        paths = extract_drop_paths(event)
        if paths:
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if extract_drop_paths(event):
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        paths = extract_drop_paths(event)
        if not paths:
            event.ignore()
            return

        self.add_media_paths(paths)
        event.setDropAction(Qt.CopyAction)
        event.accept()

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(7, 6, 7, 5)
        root_layout.setSpacing(4)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(1)
        root_layout.addWidget(splitter, 1)

        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(3, 5, 5, 4)
        sidebar_layout.setSpacing(3)

        title = QLabel("Zobrazovač")
        title.setObjectName("appTitle")
        sidebar_layout.addWidget(title)

        self.notebook_btn = QPushButton("✎  Sešit")
        self.notebook_btn.setObjectName("notebookButton")
        self.notebook_btn.setFixedHeight(30)
        self.notebook_btn.setToolTip("Otevřít samostatný sešit poznámek")
        sidebar_layout.addWidget(self.notebook_btn)

        notebook_separator = QFrame()
        notebook_separator.setObjectName("sidebarSeparator")
        notebook_separator.setFrameShape(QFrame.HLine)
        notebook_separator.setFrameShadow(QFrame.Plain)
        notebook_separator.setFixedHeight(1)
        sidebar_layout.addWidget(notebook_separator)

        sidebar_header = QHBoxLayout()
        category_heading = QLabel("Kategorie")
        category_heading.setObjectName("sectionTitle")
        sidebar_header.addWidget(category_heading)
        sidebar_header.addStretch()

        self.add_category_btn = QPushButton("＋")
        self.add_category_btn.setObjectName("miniButton")
        self.add_category_btn.setToolTip("Nová kategorie")
        self.add_category_btn.setFixedSize(30, 30)
        sidebar_header.addWidget(self.add_category_btn)
        sidebar_layout.addLayout(sidebar_header)

        self.category_list = QListWidget()
        self.category_list.setObjectName("categoryList")
        self.category_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.category_list.setSpacing(2)
        self.category_list.setItemDelegate(CategoryDelegate(self.category_list))
        sidebar_layout.addWidget(self.category_list, 1)

        category_buttons = QHBoxLayout()
        category_buttons.setSpacing(4)

        self.rename_category_btn = QPushButton("Přejmenovat")
        self.rename_category_btn.setFixedHeight(30)
        self.rename_category_btn.setToolTip("Přejmenovat vybranou kategorii")
        category_buttons.addWidget(self.rename_category_btn)

        self.category_up_btn = QPushButton("↑")
        self.category_up_btn.setObjectName("miniButton")
        self.category_up_btn.setToolTip("Posunout kategorii nahoru")
        self.category_up_btn.setFixedSize(30, 30)
        category_buttons.addWidget(self.category_up_btn)

        self.category_down_btn = QPushButton("↓")
        self.category_down_btn.setObjectName("miniButton")
        self.category_down_btn.setToolTip("Posunout kategorii dolů")
        self.category_down_btn.setFixedSize(30, 30)
        category_buttons.addWidget(self.category_down_btn)

        self.delete_category_btn = QPushButton("×")
        self.delete_category_btn.setObjectName("dangerMiniButton")
        self.delete_category_btn.setToolTip("Smazat kategorii")
        self.delete_category_btn.setFixedSize(30, 30)
        category_buttons.addWidget(self.delete_category_btn)

        sidebar_layout.addLayout(category_buttons)
        splitter.addWidget(sidebar)

        media_panel = QFrame()
        media_panel.setObjectName("panel")
        media_layout = QVBoxLayout(media_panel)
        media_layout.setContentsMargins(6, 5, 4, 4)
        media_layout.setSpacing(3)

        media_header = QHBoxLayout()
        self.category_title = QLabel("Vše")
        self.category_title.setObjectName("sectionTitle")
        media_header.addWidget(self.category_title)

        self.media_count_label = QLabel("")
        self.media_count_label.setObjectName("mutedLabel")
        media_header.addWidget(self.media_count_label)

        media_header.addStretch(1)

        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("searchBox")
        self.search_edit.setPlaceholderText("Hledat…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setFixedWidth(290)
        media_header.addWidget(self.search_edit)
        media_header.addStretch(1)

        self.type_filter_combo = QComboBox()
        self.type_filter_combo.setObjectName("filterCombo")
        self.type_filter_combo.setToolTip("Filtrovat podle typu média")
        self.type_filter_combo.addItem("Všechny typy", None)
        self.type_filter_combo.addItem("Obrázky", "image")
        self.type_filter_combo.addItem("GIFy", "gif")
        self.type_filter_combo.addItem("Videa", "video")
        self.type_filter_combo.setCurrentIndex(0)
        self.type_filter_combo.setFixedWidth(112)
        media_header.addWidget(self.type_filter_combo)

        self.sort_combo = QComboBox()
        self.sort_combo.setObjectName("filterCombo")
        self.sort_combo.setToolTip("Řazení galerie")
        self.sort_combo.addItem("Datum", "date")
        self.sort_combo.addItem("Název", "name")
        self.sort_combo.addItem("Hodnocení", "rating")
        self.sort_combo.setCurrentIndex(0)
        self.sort_combo.setFixedWidth(145)
        self.sort_combo.setStyleSheet(
            "QComboBox::drop-down { width: 0px; border: none; }"
            "QComboBox::down-arrow { image: none; width: 0px; height: 0px; }"
        )

        self.sort_direction_btn = QPushButton("↓", self.sort_combo)
        self.sort_direction_btn.setObjectName("sortDirectionButton")
        self.sort_direction_btn.setToolTip("Otočit směr řazení")
        self.sort_direction_btn.setFixedSize(32, 28)
        self.sort_direction_btn.move(113, 1)
        self.sort_direction_btn.setFocusPolicy(Qt.NoFocus)

        self.sort_direction_separator = QFrame(self.sort_direction_btn)
        self.sort_direction_separator.setObjectName("sortDirectionSeparator")
        self.sort_direction_separator.setFixedSize(1, 28)
        self.sort_direction_separator.move(0, 0)
        self.sort_direction_separator.setAttribute(
            Qt.WA_TransparentForMouseEvents,
            True,
        )

        self.sort_direction_btn.raise_()
        self.sort_direction_separator.raise_()

        media_header.addWidget(self.sort_combo)

        self.thumbnail_combo = QComboBox()
        self.thumbnail_combo.setObjectName("filterCombo")
        self.thumbnail_combo.setToolTip("Velikost náhledů")
        self.thumbnail_combo.addItem("Malé náhledy", "small")
        self.thumbnail_combo.addItem("Střední náhledy", "medium")
        self.thumbnail_combo.addItem("Velké náhledy", "large")
        self.thumbnail_combo.addItem("Extra velké náhledy", "xlarge")
        self.thumbnail_combo.addItem("Obří náhledy", "huge")
        self.thumbnail_combo.setCurrentIndex(1)
        self.thumbnail_combo.setFixedWidth(170)
        media_header.addWidget(self.thumbnail_combo)

        media_layout.addLayout(media_header)

        media_header_separator = QFrame()
        media_header_separator.setObjectName("mediaHeaderSeparator")
        media_header_separator.setFrameShape(QFrame.HLine)
        media_header_separator.setFrameShadow(QFrame.Plain)
        media_header_separator.setFixedHeight(1)
        media_layout.addWidget(media_header_separator)

        self.content_splitter = QSplitter(Qt.Horizontal)
        self.content_splitter.setChildrenCollapsible(False)
        self.content_splitter.setHandleWidth(1)
        media_layout.addWidget(self.content_splitter, 1)

        self.media_list = MediaListWidget()
        self.media_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.content_splitter.addWidget(self.media_list)

        detail = QFrame()
        detail.setObjectName("detailCard")
        detail.setMinimumWidth(330)
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(8, 6, 6, 6)
        detail_layout.setSpacing(5)

        rating_row = QHBoxLayout()
        rating_row.setSpacing(3)
        rating_row.addStretch()

        rating_label = QLabel("Hodnocení")
        rating_label.setObjectName("fieldLabel")
        rating_row.addWidget(rating_label)

        self.rating_buttons: list[QPushButton] = []
        for value in (1, 2, 3):
            button = QPushButton("★")
            button.setObjectName("ratingButton")
            button.setProperty("active", False)
            button.setFixedSize(29, 27)
            button.setToolTip(f"{value} hvězda" if value == 1 else f"{value} hvězdy")
            button.clicked.connect(
                lambda _checked=False, rating=value: self.set_rating(rating)
            )
            self.rating_buttons.append(button)
            rating_row.addWidget(button)

        rating_row.addStretch()
        detail_layout.addLayout(rating_row)

        rating_separator = QFrame()
        rating_separator.setObjectName("detailSeparator")
        rating_separator.setFrameShape(QFrame.HLine)
        rating_separator.setFrameShadow(QFrame.Plain)
        rating_separator.setFixedHeight(1)
        detail_layout.addWidget(rating_separator)

        self.preview_stack = QStackedWidget()

        self.preview = QLabel("Vyber médium")
        self.preview.setObjectName("preview")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(320, 190)

        self.video_widget = QVideoWidget()
        self.video_widget.setMinimumSize(320, 190)
        self.video_widget.setStyleSheet("background: #000000;")

        self.preview_stack.addWidget(self.preview)
        self.preview_stack.addWidget(self.video_widget)
        self.preview_stack.setCurrentWidget(self.preview)
        detail_layout.addWidget(self.preview_stack, 1)

        self.media_player.setVideoOutput(self.video_widget)

        self.notes_edit = GuardedNoteEdit()
        self.notes_edit.setObjectName("previewNote")
        self.notes_edit.setPlaceholderText("Poznámka k tomuto médiu…")
        self.notes_edit.setMinimumHeight(72)
        self.notes_edit.setMaximumHeight(110)
        self.notes_edit.setReadOnly(True)
        self.notes_edit.setToolTip("Dvojklikem upravit poznámku")
        detail_layout.addWidget(self.notes_edit)

        preview_actions = QHBoxLayout()
        preview_actions.setSpacing(4)

        self.play_pause_btn = QPushButton("▶ Přehrát")
        self.play_pause_btn.setFixedSize(112, 30)
        self.play_pause_btn.setEnabled(False)
        self.play_pause_btn.setVisible(False)
        preview_actions.addWidget(self.play_pause_btn)

        self.mute_btn = QPushButton("Ztlumit")
        self.mute_btn.setFixedSize(112, 30)
        self.mute_btn.setEnabled(False)
        self.mute_btn.setVisible(False)
        preview_actions.addWidget(self.mute_btn)

        self.open_btn = QPushButton("Otevřít")
        self.open_btn.setFixedSize(112, 30)
        self.open_btn.setEnabled(False)

        self.open_menu = QMenu(self.open_btn)
        self.open_file_action = self.open_menu.addAction("Soubor")
        self.open_path_action = self.open_menu.addAction("Cestu")
        self.open_btn.setMenu(self.open_menu)

        preview_actions.addWidget(self.open_btn)

        preview_actions.addStretch()
        detail_layout.addLayout(preview_actions)

        detail_separator = QFrame()
        detail_separator.setObjectName("detailSeparator")
        detail_separator.setFrameShape(QFrame.HLine)
        detail_separator.setFrameShadow(QFrame.Plain)
        detail_separator.setFixedHeight(1)
        detail_layout.addWidget(detail_separator)

        caption_label = QLabel("Krátký popisek")
        caption_label.setObjectName("fieldLabel")
        detail_layout.addWidget(caption_label)

        self.caption_edit = QLineEdit()
        self.caption_edit.setPlaceholderText(
            "Např. nejlepší moment, reakce, nápad, připomínka…"
        )
        detail_layout.addWidget(self.caption_edit)

        category_label = QLabel("Kategorie")
        category_label.setObjectName("fieldLabel")
        detail_layout.addWidget(category_label)

        self.category_combo = QComboBox()
        detail_layout.addWidget(self.category_combo)

        buttons = QHBoxLayout()
        self.save_btn = QPushButton("Uložit změny")
        self.save_btn.setObjectName("saveButton")
        self.save_btn.setEnabled(False)
        self.save_btn.setProperty("dirty", False)
        self.save_btn.setProperty("saved", False)
        self.save_btn.setFixedSize(145, 30)
        buttons.addWidget(self.save_btn)

        self.delete_media_btn = QPushButton("Smazat z databáze")
        self.delete_media_btn.setObjectName("dangerButton")
        self.delete_media_btn.setFixedSize(145, 30)
        buttons.addWidget(self.delete_media_btn)

        detail_layout.addLayout(buttons)

        self.content_splitter.addWidget(detail)
        self.content_splitter.setSizes([700, 380])
        self.content_splitter.splitterMoved.connect(
            lambda _pos, _index: QTimer.singleShot(
                0, self._refresh_preview_size
            )
        )

        splitter.addWidget(media_panel)
        splitter.setSizes([195, 1085])

        self.notebook_btn.clicked.connect(self.open_notebook)
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
        self.type_filter_combo.currentIndexChanged.connect(
            lambda _index: self.reload_media()
        )
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        self.sort_direction_btn.clicked.connect(self.toggle_sort_direction)
        self.thumbnail_combo.currentIndexChanged.connect(
            self._on_thumbnail_mode_changed
        )

        self.caption_edit.textChanged.connect(self._on_detail_edited)
        self.notes_edit.textChanged.connect(self._on_detail_edited)
        self.notes_edit.editRequested.connect(self._enable_note_editing)
        self.category_combo.currentIndexChanged.connect(self._on_detail_edited)

        self.save_btn.clicked.connect(self.save_current)
        self.delete_media_btn.clicked.connect(self.delete_selected_media)
        self.open_file_action.triggered.connect(self.open_current)
        self.open_path_action.triggered.connect(self.open_current_path)
        self.play_pause_btn.clicked.connect(self.toggle_video_playback)
        self.mute_btn.clicked.connect(self.toggle_video_mute)
        self.media_player.playbackStateChanged.connect(
            self._on_video_playback_state_changed
        )
        self.media_player.mediaStatusChanged.connect(
            self._on_video_media_status_changed
        )

    def open_notebook(self) -> None:
        if self.notebook_dialog is None:
            self.notebook_dialog = NotebookDialog(self.db, self)

        self.notebook_dialog.show()
        self.notebook_dialog.raise_()
        self.notebook_dialog.activateWindow()
        self.notebook_dialog.editor.setFocus()

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QWidget {
                font-size: 13px;
                color: #20242a;
            }

            QWidget#root, QMainWindow {
                background: #ffffff;
            }

            QFrame#sidebar {
                background: #f4f6f8;
                border: none;
                border-radius: 0;
            }

            QFrame#panel {
                background: #ffffff;
                border: none;
                border-radius: 0;
            }

            QFrame#detailCard {
                background: #f8f9fb;
                border: none;
                border-radius: 0;
            }

            QFrame#mediaHeaderSeparator,
            QFrame#sidebarSeparator,
            QFrame#detailSeparator {
                background: #d9dee5;
                border: none;
            }

            QLabel#appTitle {
                font-size: 20px;
                font-weight: 700;
                color: #1d232b;
                padding-right: 4px;
            }

            QLabel#sectionTitle {
                font-size: 15px;
                font-weight: 700;
                color: #252b33;
            }

            QLabel#fieldLabel {
                font-weight: 600;
                color: #333a43;
                margin-top: 1px;
            }

            QLabel#mutedLabel {
                color: #78828e;
                font-size: 12px;
            }

            QLabel#preview {
                background: #f6f7f9;
                color: #78828e;
                border: none;
                border-radius: 4px;
                padding: 4px;
            }

            QTextEdit#previewNote {
                background: #f6f7f9;
                color: #20242a;
                border: none;
                border-radius: 4px;
                padding: 5px;
            }

            QTextEdit#previewNote:focus {
                border: none;
            }

            QLineEdit,
            QTextEdit,
            QComboBox,
            QListWidget {
                background: #ffffff;
                color: #20242a;
                border: 1px solid #cfd5dc;
                border-radius: 6px;
                padding: 3px;
                selection-background-color: #cfe0ff;
                selection-color: #172033;
            }

            QLineEdit:focus,
            QTextEdit:focus,
            QComboBox:focus,
            QListWidget:focus {
                border: 1px solid #6d9ee8;
            }

            QLineEdit#searchBox {
                background: #f3f5f7;
                border: 1px solid #d9dee5;
                border-radius: 10px;
                padding: 5px 9px;
                min-height: 20px;
            }

            QLineEdit#searchBox:hover {
                background: #eceff3;
                border-color: #cbd2da;
            }

            QLineEdit#searchBox:focus {
                background: #ffffff;
                border: 1px solid #8aabe0;
            }

            QComboBox#filterCombo {
                background: #f3f5f7;
                color: #2b3138;
                border: 1px solid #d9dee5;
                border-radius: 10px;
                padding: 5px 26px 5px 10px;
                min-height: 20px;
            }

            QComboBox#filterCombo:hover {
                background: #eceff3;
                border-color: #cbd2da;
            }

            QPushButton#sortDirectionButton {
                background: transparent;
                color: #59636f;
                border: none;
                border-radius: 0 10px 10px 0;
                padding: 0;
                text-align: center;
                font-size: 16px;
                font-weight: 700;
            }

            QPushButton#sortDirectionButton:hover {
                background: rgba(220,225,231,90);
                color: #252b33;
            }

            QPushButton#sortDirectionButton:pressed {
                background: rgba(205,212,220,130);
            }

            QFrame#sortDirectionSeparator {
                background: rgba(145,154,164,150);
                border: none;
            }

            QComboBox#filterCombo:focus,
            QComboBox#filterCombo:on {
                background: #ffffff;
                border: 1px solid #8aabe0;
            }

            QComboBox#filterCombo::drop-down {
                subcontrol-origin: padding;
                subcontrol-position: top right;
                width: 24px;
                border: none;
                background: transparent;
            }

            QComboBox#filterCombo QAbstractItemView {
                background: #ffffff;
                color: #20242a;
                border: 1px solid #d3d9e0;
                border-radius: 7px;
                padding: 4px;
                selection-background-color: #dbe8fb;
                selection-color: #172033;
                outline: none;
            }

            QListWidget#categoryList {
                padding: 2px;
                border: none;
                background: transparent;
            }

            QListWidget#categoryList::item {
                padding: 5px 6px;
                border-radius: 6px;
            }

            QListWidget#categoryList::item:selected {
                background: #d6e4f7;
                color: #172033;
                font-weight: 600;
            }

            QListWidget#categoryList::item:hover:!selected {
                background: #e9edf2;
            }

            QListWidget#mediaList {
                padding: 4px;
                background: #ffffff;
                border: none;
            }

            QListWidget#categoryList:focus,
            QListWidget#mediaList:focus {
                border: none;
            }

            QListWidget#mediaList[dragActive="true"] {
                border: 2px dashed #6d9ee8;
                background: #eef5ff;
            }

            QListWidget#mediaList::item {
                border: 1px solid transparent;
                border-radius: 6px;
                padding: 3px;
            }

            QListWidget#mediaList::item:selected {
                background: #dbe8fb;
                color: #172033;
                border: none;
            }

            QListWidget#mediaList::item:hover:!selected {
                background: #f1f4f7;
            }

            QPushButton {
                background: #ffffff;
                color: #252b33;
                border: 1px solid #cbd2da;
                border-radius: 6px;
                padding: 4px 8px;
                min-height: 20px;
            }

            QPushButton:hover {
                background: #f1f4f7;
            }

            QPushButton:pressed {
                background: #e6eaee;
            }

            QPushButton:disabled {
                color: #a0a7af;
                background: #f2f4f6;
                border-color: #dfe3e7;
            }

            QPushButton#primaryButton {
                background: #2d6cdf;
                color: #ffffff;
                border-color: #2d6cdf;
                font-weight: 600;
            }

            QPushButton#primaryButton:hover {
                background: #3978e5;
            }

            QPushButton#notebookButton {
                background: #eef1f4;
                color: #27313c;
                border: 1px solid #d9dee5;
                border-radius: 7px;
                padding: 6px 9px;
                text-align: left;
                font-weight: 600;
            }

            QPushButton#notebookButton:hover {
                background: #e5eaf0;
            }

            QLabel#notebookTitle {
                font-size: 18px;
                font-weight: 700;
                color: #20242a;
            }

            QTextEdit#notebookEditor {
                background: #ffffff;
                color: #20242a;
                border: 1px solid #d7dce2;
                border-radius: 8px;
                padding: 10px;
                font-size: 14px;
            }

            QTextEdit#notebookEditor:focus {
                border: 1px solid #8aabe0;
            }

            QPushButton#notebookEditButton {
                background: #ffffff;
                color: #2b3138;
                border: 1px solid #cbd2da;
                font-weight: 600;
                min-width: 92px;
            }

            QPushButton#notebookEditButton:hover {
                background: #eef1f4;
            }

            QTextEdit#notebookEditor[editing="false"] {
                background: #f6f7f9;
                color: #3f4852;
            }

            QTextEdit#notebookEditor[editing="true"] {
                background: #ffffff;
            }

            QPushButton#notebookSaveButton {
                background: #2e9b55;
                color: #ffffff;
                border-color: #2e9b55;
                font-weight: 600;
                min-width: 92px;
            }

            QPushButton#notebookSaveButton:hover {
                background: #27874a;
                border-color: #27874a;
            }

            QPushButton#notebookSaveButton:pressed {
                background: #1f743d;
                border-color: #1f743d;
                padding-top: 6px;
                padding-bottom: 4px;
            }

            QPushButton#notebookSaveButton[dirty="true"] {
                background: #238746;
                color: #ffffff;
                border-color: #238746;
            }

            QPushButton#notebookSaveButton[saved="true"] {
                background: #2e9b55;
                color: #ffffff;
                border-color: #2e9b55;
            }

            QPushButton#saveButton {
                background: #f2f4f6;
                color: #9aa2ab;
                border-color: #dfe3e7;
                font-weight: 600;
            }

            QPushButton#saveButton[dirty="true"] {
                background: #2d6cdf;
                color: #ffffff;
                border-color: #2d6cdf;
            }

            QPushButton#saveButton[dirty="true"]:hover {
                background: #3978e5;
            }

            QPushButton#saveButton[saved="true"] {
                background: #2e9b55;
                color: #ffffff;
                border-color: #2e9b55;
            }

            QPushButton#dangerButton,
            QPushButton#dangerMiniButton {
                color: #b42318;
            }

            QPushButton#ratingButton {
                color: #a8adb4;
                background: #ffffff;
                border: 1px solid #d8dde3;
                padding: 1px;
                font-size: 17px;
            }

            QPushButton#ratingButton[active="true"] {
                color: #c98b00;
                background: #fff7df;
                border-color: #e1bd61;
            }

            QPushButton#miniButton,
            QPushButton#dangerMiniButton {
                padding: 3px;
                font-size: 14px;
            }

            QMenu {
                background: #ffffff;
                color: #20242a;
                border: 1px solid #cfd5dc;
                padding: 5px;
            }

            QMenu::item {
                padding: 5px 22px 5px 8px;
                border-radius: 5px;
            }

            QMenu::item:selected {
                background: #dbe8fb;
                color: #172033;
            }

            QStatusBar {
                background: #ffffff;
                color: #78828e;
                border-top: 1px solid #e3e7eb;
            }

            QSplitter::handle {
                background: #d7dce2;
            }

            QSplitter::handle:horizontal {
                width: 1px;
            }

            QSplitter::handle:vertical {
                height: 1px;
            }
            """
        )

    def _create_shortcuts(self) -> None:
        self.shortcut_open = QShortcut(QKeySequence("Ctrl+O"), self)
        self.shortcut_open.activated.connect(self.add_media_dialog)

        self.shortcut_save = QShortcut(QKeySequence("Ctrl+S"), self)
        self.shortcut_save.activated.connect(self.save_current)

        self.shortcut_enter_save = QShortcut(QKeySequence(Qt.Key_Return), self)
        self.shortcut_enter_save.activated.connect(self._save_current_with_enter)

        self.shortcut_numpad_enter_save = QShortcut(
            QKeySequence(Qt.Key_Enter),
            self,
        )
        self.shortcut_numpad_enter_save.activated.connect(
            self._save_current_with_enter
        )

        self.shortcut_search = QShortcut(QKeySequence("Ctrl+F"), self)
        self.shortcut_search.activated.connect(self.search_edit.setFocus)

        self.shortcut_delete = QShortcut(QKeySequence("Delete"), self)
        self.shortcut_delete.activated.connect(self.delete_selected_media)

    def _save_current_with_enter(self) -> None:
        if (
            QApplication.focusWidget() is self.notes_edit
            and not self.notes_edit.isReadOnly()
        ):
            return
        self.save_current()

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
        all_font = all_item.font()
        all_font.setBold(True)
        all_item.setFont(all_font)
        all_item.setSizeHint(QSize(all_item.sizeHint().width(), 34))
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

        rows = list(
            self.db.media_items(
                self.selected_category_id(),
                self.search_edit.text(),
            )
        )

        media_type = self.type_filter_combo.currentData()
        if media_type in {"image", "gif", "video"}:
            rows = [
                row for row in rows
                if str(row["media_type"]) == str(media_type)
            ]

        sort_mode = self.sort_combo.currentData() or "date"
        reverse = bool(self.sort_descending)

        if sort_mode == "name":
            rows.sort(
                key=lambda row: (
                    str(row["caption"]).strip() or Path(row["path"]).name
                ).casefold(),
                reverse=reverse,
            )
        elif sort_mode == "rating":
            rows.sort(
                key=lambda row: (
                    int(row["rating"] or 0),
                    int(row["id"]),
                ),
                reverse=reverse,
            )
        else:
            rows.sort(
                key=lambda row: int(row["id"]),
                reverse=reverse,
            )

        self.media_list.blockSignals(True)
        self.media_list.clear()

        selected_row = -1
        for index, row in enumerate(rows):
            path = Path(row["path"])
            caption = row["caption"].strip()
            rating = int(row["rating"] or 0)
            display_title = caption

            item = QListWidgetItem(display_title)
            item.setData(Qt.UserRole, int(row["id"]))
            rating_text = "★" * rating if rating else "bez hodnocení"
            item.setToolTip(
                f"Kategorie: {row['category_name']}\nHodnocení: {rating_text}"
            )
            item.setIcon(
                self.make_icon(
                    path,
                    row["media_type"],
                    int(row["id"]),
                )
            )
            item.setTextAlignment(Qt.AlignHCenter | Qt.AlignTop)
            self.media_list.addItem(item)

            if select_media_id == int(row["id"]):
                selected_row = index

        self.media_list.blockSignals(False)

        self.category_title.setText(self.selected_category_name())
        self.media_count_label.setText(f"{len(rows)} položek")
        if not rows and self.db.total_media_count() > 0:
            self.statusBar().showMessage(
                "V databázi média jsou. Zkontroluj filtr nebo hledání.",
                4000,
            )

        if selected_row >= 0:
            self.media_list.setCurrentRow(selected_row)
            self.on_media_changed(self.media_list.currentItem(), None)
        elif self.media_list.count() > 0:
            self.media_list.setCurrentRow(0)
            self.on_media_changed(self.media_list.currentItem(), None)
        else:
            self.clear_detail()

    def _update_sort_direction_button(self) -> None:
        self.sort_direction_btn.setText(
            "↓" if self.sort_descending else "↑"
        )
        self.sort_direction_btn.setToolTip(
            "Směr řazení: "
            + ("sestupně" if self.sort_descending else "vzestupně")
        )

    def _on_sort_changed(self, _index: int) -> None:
        self._update_sort_direction_button()
        self.reload_media(select_media_id=self.current_media_id)

    def toggle_sort_direction(self) -> None:
        self.sort_descending = not self.sort_descending
        self._update_sort_direction_button()
        self.reload_media(select_media_id=self.current_media_id)

    def _thumbnail_dimensions(self) -> tuple[int, int, int, int]:
        sizes = {
            "small": (120, 80, 145, 110),
            "medium": (160, 106, 185, 140),
            "large": (220, 146, 248, 182),
            "xlarge": (300, 200, 330, 240),
            "huge": (380, 250, 420, 300),
        }
        return sizes.get(self.thumbnail_mode, sizes["medium"])

    def _thumbnail_icon(self, pixmap: QPixmap) -> QIcon:
        width, height, _grid_w, _grid_h = self._thumbnail_dimensions()
        canvas = QPixmap(width, height)
        canvas.fill(QColor("#ffffff"))
        scaled = pixmap.scaled(
            max(1, width - 4),
            max(1, height - 4),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        painter = QPainter(canvas)
        x = (canvas.width() - scaled.width()) // 2
        y = (canvas.height() - scaled.height()) // 2
        painter.drawPixmap(x, y, scaled)
        painter.end()
        return QIcon(canvas)

    def _on_thumbnail_mode_changed(self, _index: int) -> None:
        mode = self.thumbnail_combo.currentData()
        if mode not in {"small", "medium", "large", "xlarge", "huge"}:
            mode = "medium"

        self.thumbnail_mode = str(mode)
        width, height, grid_w, grid_h = self._thumbnail_dimensions()
        self.media_list.setIconSize(QSize(width, height))
        self.media_list.setGridSize(QSize(grid_w, grid_h))
        self.reload_media(select_media_id=self.current_media_id)

    def make_icon(
        self,
        path: Path,
        media_type: str,
        media_id: int | None = None,
    ) -> QIcon:
        if path.exists() and media_type in {"image", "gif"}:
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                return self._thumbnail_icon(pixmap)

        if media_type == "video":
            if media_id is not None:
                thumb = video_thumbnail_path(media_id)
                if thumb.is_file():
                    pixmap = QPixmap(str(thumb))
                    if not pixmap.isNull():
                        try:
                            if thumb.stat().st_mtime >= path.stat().st_mtime:
                                return self._thumbnail_icon(pixmap)
                        except OSError:
                            pass

                self.thumbnailer.request(media_id, path)

            return self.style().standardIcon(QStyle.SP_MediaPlay)

        return self.style().standardIcon(QStyle.SP_FileIcon)

    def _on_thumbnail_ready(self, media_id: int, thumb_path: str) -> None:
        pixmap = QPixmap(thumb_path)
        if pixmap.isNull():
            return

        icon = self._thumbnail_icon(pixmap)
        for index in range(self.media_list.count()):
            item = self.media_list.item(index)
            if int(item.data(Qt.UserRole)) == media_id:
                item.setIcon(icon)
                break

        if (
            self.current_media_id == media_id
            and self.current_preview_type == "video"
            and self.media_player.playbackState()
            != QMediaPlayer.PlaybackState.PlayingState
        ):
            self._refresh_video_poster()

    def add_media_dialog(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Vyber obrázky, GIFy nebo videa",
            str(Path.home()),
            (
                "Média (*.png *.jpg *.jpeg *.webp *.bmp *.gif "
                "*.mp4 *.mkv *.webm *.avi *.mov *.m4v *.mpeg *.mpg "
                "*.wmv *.flv *.ogv *.ts *.mts *.m2ts *.3gp *.3g2 *.vob *.asf);;"
                "Obrázky a GIFy (*.png *.jpg *.jpeg *.webp *.bmp *.gif);;"
                "Videa (*.mp4 *.mkv *.webm *.avi *.mov *.m4v *.mpeg *.mpg "
                "*.wmv *.flv *.ogv *.ts *.mts *.m2ts *.3gp *.3g2 *.vob *.asf);;"
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
        self._restart_file_tracker()

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

        self.loading_detail = True
        try:
            self.current_media_id = int(row["id"])
            self.caption_edit.setText(row["caption"])
            self.notes_edit.setReadOnly(True)
            self.notes_edit.setPlainText(row["notes"])

            combo_index = self.category_combo.findData(int(row["category_id"]))
            if combo_index >= 0:
                self.category_combo.setCurrentIndex(combo_index)

            self.current_rating = max(0, min(3, int(row["rating"] or 0)))
            for button in self.rating_buttons:
                button.setEnabled(True)
            self._refresh_rating_buttons()

            self.loaded_detail_state = (
                str(row["caption"]),
                str(row["notes"]),
                int(row["category_id"]),
                self.current_rating,
            )

            path = Path(row["path"])
            self.show_preview(path, row["media_type"])
            file_exists = path.exists()
            folder_exists = path.parent.exists()
            self.open_file_action.setEnabled(file_exists)
            self.open_path_action.setEnabled(folder_exists)
            self.open_btn.setEnabled(file_exists or folder_exists)
        finally:
            self.loading_detail = False

        self._update_save_button_state()

    def _current_detail_state(self) -> tuple[str, str, int, int] | None:
        if self.current_media_id is None:
            return None

        category_id = self.category_combo.currentData()
        if category_id is None:
            return None

        return (
            self.caption_edit.text(),
            self.notes_edit.toPlainText(),
            int(category_id),
            int(self.current_rating),
        )

    def _enable_note_editing(self) -> None:
        if self.current_media_id is None:
            return
        self.notes_edit.setReadOnly(False)
        self.notes_edit.setToolTip("Poznámku právě upravuješ")
        self.notes_edit.setFocus()

    def set_rating(self, rating: int) -> None:
        if self.current_media_id is None:
            return

        rating = max(1, min(3, int(rating)))
        selected_ids = self.selected_media_ids()

        if len(selected_ids) > 1:
            new_rating = rating
            self.db.update_media_ratings(selected_ids, new_rating)
            self.current_rating = new_rating

            if self.loaded_detail_state is not None:
                caption, notes, category_id, _old_rating = self.loaded_detail_state
                self.loaded_detail_state = (
                    caption,
                    notes,
                    category_id,
                    self.current_rating,
                )

            self._refresh_rating_buttons()
            self.save_feedback_active = False
            self._update_save_button_state()
            self.statusBar().showMessage(
                f"Hodnocení {'★' * new_rating} nastaveno pro "
                f"{len(selected_ids)} položek.",
                2000,
            )
            return

        self.current_rating = 0 if self.current_rating == rating else rating
        self.db.update_media_rating(self.current_media_id, self.current_rating)
        self._refresh_rating_buttons()

        if self.loaded_detail_state is not None:
            caption, notes, category_id, _old_rating = self.loaded_detail_state
            self.loaded_detail_state = (
                caption,
                notes,
                category_id,
                self.current_rating,
            )

        item = self.media_list.currentItem()
        if item is not None:
            row = self.db.media_by_id(self.current_media_id)
            if row is not None:
                caption = str(row["caption"]).strip()
                item.setText(caption)

                rating_text = (
                    "★" * self.current_rating
                    if self.current_rating
                    else "bez hodnocení"
                )
                item.setToolTip(
                    f"Kategorie: {row['category_name']}\n"
                    f"Hodnocení: {rating_text}"
                )

        self.save_feedback_active = False
        self._update_save_button_state()
        self.statusBar().showMessage("Hodnocení uloženo.", 1500)

    def _refresh_rating_buttons(self) -> None:
        for index, button in enumerate(self.rating_buttons, start=1):
            button.setProperty("active", index <= self.current_rating)
            button.style().unpolish(button)
            button.style().polish(button)
            button.update()

    def _on_detail_edited(self, *args) -> None:
        if self.loading_detail:
            return
        self.save_feedback_active = False
        self._update_save_button_state()

    def _repolish_save_button(self) -> None:
        self.save_btn.style().unpolish(self.save_btn)
        self.save_btn.style().polish(self.save_btn)
        self.save_btn.update()

    def _update_save_button_state(self) -> None:
        current = self._current_detail_state()
        dirty = (
            current is not None
            and self.loaded_detail_state is not None
            and current != self.loaded_detail_state
        )

        self.save_btn.setProperty("saved", False)
        self.save_btn.setProperty("dirty", dirty)
        self.save_btn.setText("Uložit změny")
        self.save_btn.setEnabled(dirty)
        self._repolish_save_button()

    def _show_saved_feedback(self) -> None:
        self.save_feedback_active = True
        self.save_btn.setProperty("dirty", False)
        self.save_btn.setProperty("saved", True)
        self.save_btn.setText("✓ Uloženo")
        self.save_btn.setEnabled(True)
        self._repolish_save_button()
        QTimer.singleShot(1100, self._finish_saved_feedback)

    def _finish_saved_feedback(self) -> None:
        if not self.save_feedback_active:
            return
        self.save_feedback_active = False
        self._update_save_button_state()

    def show_preview(self, path: Path, media_type: str) -> None:
        self.current_preview_path = path
        self.current_preview_type = media_type

        self.media_player.stop()
        self.media_player.setSource(QUrl())
        self.preview_stack.setCurrentWidget(self.preview)
        self.play_pause_btn.setVisible(False)
        self.play_pause_btn.setEnabled(False)
        self.mute_btn.setVisible(False)
        self.mute_btn.setEnabled(False)

        if self.current_movie is not None:
            self.current_movie.stop()
            self.current_movie = None
            self.preview.setMovie(None)

        self.preview.clear()
        self.preview.setToolTip("")

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
            self.media_player.setSource(QUrl.fromLocalFile(str(path)))
            self.play_pause_btn.setVisible(True)
            self.play_pause_btn.setEnabled(True)
            self.mute_btn.setVisible(True)
            self.mute_btn.setEnabled(True)
            self._update_mute_button()
            self._refresh_video_poster()

            if self.current_media_id is not None:
                self.thumbnailer.request(self.current_media_id, path)
            return

        self.preview.setText("Náhled není k dispozici")

    def _refresh_video_poster(self) -> None:
        if (
            self.current_preview_type != "video"
            or self.current_media_id is None
        ):
            return

        thumb = video_thumbnail_path(self.current_media_id)
        if thumb.is_file():
            pixmap = QPixmap(str(thumb))
            if not pixmap.isNull():
                target = QSize(
                    max(100, self.preview.width() - 12),
                    max(100, self.preview.height() - 12),
                )
                self.preview.setPixmap(
                    pixmap.scaled(
                        target,
                        Qt.KeepAspectRatio,
                        Qt.SmoothTransformation,
                    )
                )
                return

        self.preview.setPixmap(
            self.style()
            .standardIcon(QStyle.SP_MediaPlay)
            .pixmap(QSize(72, 72))
        )

    def toggle_video_playback(self) -> None:
        if (
            self.current_preview_type != "video"
            or self.current_preview_path is None
            or not self.current_preview_path.is_file()
        ):
            return

        if (
            self.media_player.playbackState()
            == QMediaPlayer.PlaybackState.PlayingState
        ):
            self.media_player.pause()
            return

        if self.media_player.source().isEmpty():
            self.media_player.setSource(
                QUrl.fromLocalFile(str(self.current_preview_path))
            )

        self.preview_stack.setCurrentWidget(self.video_widget)
        self.media_player.play()

    def toggle_video_mute(self) -> None:
        self.audio_output.setMuted(not self.audio_output.isMuted())
        self._update_mute_button()

    def _update_mute_button(self) -> None:
        self.mute_btn.setText(
            "Zapnout zvuk" if self.audio_output.isMuted() else "Ztlumit"
        )

    def _on_video_playback_state_changed(self, state) -> None:
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.play_pause_btn.setText("⏸ Pauza")
        else:
            self.play_pause_btn.setText("▶ Přehrát")

    def _on_video_media_status_changed(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.media_player.setPosition(0)
            self.play_pause_btn.setText("▶ Přehrát")

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

    def _refresh_preview_size(self) -> None:
        if self.current_movie is not None:
            target = QSize(
                max(100, self.preview.width() - 20),
                max(100, self.preview.height() - 20),
            )
            current = self.current_movie.currentPixmap()
            if not current.isNull():
                target = current.size().scaled(target, Qt.KeepAspectRatio)
            self.current_movie.setScaledSize(target)
        elif self.current_preview_type == "image":
            self._refresh_static_preview()
        elif (
            self.current_preview_type == "video"
            and self.preview_stack.currentWidget() is self.preview
        ):
            self._refresh_video_poster()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_preview_size()

    def save_current(self) -> None:
        if (
            self.current_media_id is None
            or not bool(self.save_btn.property("dirty"))
        ):
            return

        category_id = self.category_combo.currentData()
        if category_id is None:
            return

        media_id = self.current_media_id
        selected_category = self.selected_category_id()
        caption = self.caption_edit.text()
        notes = self.notes_edit.toPlainText()

        self.db.update_media(
            media_id,
            caption.strip(),
            notes.strip(),
            int(category_id),
            int(self.current_rating),
        )

        self.reload_categories(selected_category)
        self.reload_media(select_media_id=media_id)

        row = self.db.media_by_id(media_id)
        if row is not None:
            self.current_rating = max(0, min(3, int(row["rating"] or 0)))
            self._refresh_rating_buttons()
            self.notes_edit.setReadOnly(True)
            self.notes_edit.setToolTip("Dvojklikem upravit poznámku")
            self.loaded_detail_state = (
                str(row["caption"]),
                str(row["notes"]),
                int(row["category_id"]),
                self.current_rating,
            )

        self._show_saved_feedback()
        self.statusBar().showMessage("Změny uloženy.", 3000)

    def rate_media_ids(self, media_ids: list[int], rating: int) -> None:
        if not media_ids:
            return

        rating = max(0, min(3, int(rating)))
        current = self.current_media_id
        self.db.update_media_ratings(media_ids, rating)
        self.reload_media(select_media_id=current)

        label = "bez hodnocení" if rating == 0 else "★" * rating
        self.statusBar().showMessage(
            f"Hodnocení {label} nastaveno pro {len(media_ids)} položek.",
            2500,
        )

    def rate_selected_media(self, rating: int) -> None:
        media_ids = self.selected_media_ids()
        if not media_ids and self.current_media_id is not None:
            media_ids = [self.current_media_id]
        self.rate_media_ids(media_ids, rating)

    def move_media_ids_to(
        self,
        media_ids: list[int],
        category_id: int,
    ) -> None:
        if not media_ids:
            return

        source_category_id = self.selected_category_id()
        self.db.move_media(media_ids, category_id)
        self.reload_categories(source_category_id)
        self.reload_media(select_media_id=None)
        self.statusBar().showMessage(
            f"Přesunuto {len(media_ids)} položek.",
            3500,
        )

    def move_selected_media_to(self, category_id: int) -> None:
        media_ids = self.selected_media_ids()
        if not media_ids and self.current_media_id is not None:
            media_ids = [self.current_media_id]
        self.move_media_ids_to(media_ids, category_id)

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

        selected_ids = self.selected_media_ids()
        if not selected_ids:
            media_id = item.data(Qt.UserRole)
            if media_id is not None:
                selected_ids = [int(media_id)]

        menu = QMenu(self)
        open_action = menu.addAction("Otevřít původní soubor")

        rating_title = (
            f"Hodnocení ({len(selected_ids)} položek)"
            if len(selected_ids) > 1
            else "Hodnocení"
        )
        rating_menu = menu.addMenu(rating_title)
        frozen_ids = tuple(selected_ids)
        for label, value in (
            ("Bez hodnocení", 0),
            ("★", 1),
            ("★★", 2),
            ("★★★", 3),
        ):
            action = rating_menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, rating=value, ids=frozen_ids:
                    self.rate_media_ids(list(ids), rating)
            )

        move_menu = menu.addMenu("Přesunout do kategorie")
        frozen_move_ids = tuple(selected_ids)

        for row in self.db.categories():
            category_id = int(row["id"])
            action = move_menu.addAction(str(row["name"]))
            action.triggered.connect(
                lambda _checked=False, target_id=category_id, ids=frozen_move_ids:
                    self.move_media_ids_to(list(ids), target_id)
            )

        menu.addSeparator()
        delete_action = menu.addAction("Smazat z databáze")

        chosen = menu.exec(self.media_list.mapToGlobal(pos))
        if chosen == open_action:
            self.open_current()
        elif chosen == delete_action:
            self.delete_selected_media()

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

    def open_current_path(self) -> None:
        if self.current_media_id is None:
            return

        row = self.db.media_by_id(self.current_media_id)
        if row is None:
            return

        path = Path(row["path"])
        folder = path.parent
        if not folder.exists():
            QMessageBox.warning(
                self,
                "Cesta",
                "Složka, ve které byl soubor uložený, už neexistuje.",
            )
            return

        if path.exists():
            uri = QUrl.fromLocalFile(str(path)).toString()

            gdbus = shutil.which("gdbus")
            if gdbus:
                try:
                    result = subprocess.run(
                        [
                            gdbus,
                            "call",
                            "--session",
                            "--dest",
                            "org.freedesktop.FileManager1",
                            "--object-path",
                            "/org/freedesktop/FileManager1",
                            "--method",
                            "org.freedesktop.FileManager1.ShowItems",
                            f"['{uri}']",
                            "",
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=2,
                        check=False,
                    )
                    if result.returncode == 0:
                        return
                except (OSError, subprocess.SubprocessError):
                    pass

            for executable, args in (
                ("thunar", ["--select", str(path)]),
                ("dolphin", ["--select", str(path)]),
                ("nautilus", ["--select", str(path)]),
            ):
                program = shutil.which(executable)
                if program:
                    try:
                        subprocess.Popen(
                            [program, *args],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        return
                    except OSError:
                        continue

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _restart_file_tracker(self) -> None:
        if self.tracker_observer is not None:
            self.tracker_observer.stop()
            self.tracker_observer.join(timeout=2)
            self.tracker_observer = None
        self._start_file_tracker()

    def closeEvent(self, event) -> None:
        if self.tracker_observer is not None:
            self.tracker_observer.stop()
            self.tracker_observer.join(timeout=2)
            self.tracker_observer = None
        super().closeEvent(event)

    def clear_detail(self) -> None:
        self.media_player.stop()
        self.media_player.setSource(QUrl())
        self.preview_stack.setCurrentWidget(self.preview)
        self.play_pause_btn.setVisible(False)
        self.play_pause_btn.setEnabled(False)
        self.mute_btn.setVisible(False)
        self.mute_btn.setEnabled(False)

        self.current_media_id = None
        self.current_preview_path = None
        self.current_preview_type = None
        self.current_rating = 0
        self.loaded_detail_state = None
        self.save_feedback_active = False

        if self.current_movie is not None:
            self.current_movie.stop()
            self.current_movie = None

        self.preview.clear()
        self.preview.setText("Vyber médium")
        self.preview.setToolTip("")
        self.loading_detail = True
        try:
            self.caption_edit.clear()
            self.notes_edit.setReadOnly(True)
            self.notes_edit.setToolTip("Dvojklikem upravit poznámku")
            self.notes_edit.clear()
        finally:
            self.loading_detail = False

        self._refresh_rating_buttons()
        for button in self.rating_buttons:
            button.setEnabled(False)

        self.open_file_action.setEnabled(False)
        self.open_path_action.setEnabled(False)
        self.open_btn.setEnabled(False)
        self.save_btn.setProperty("dirty", False)
        self.save_btn.setProperty("saved", False)
        self.save_btn.setText("Uložit změny")
        self.save_btn.setEnabled(False)
        self._repolish_save_button()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Zobrazovač")
    app.setStyle("Fusion")

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

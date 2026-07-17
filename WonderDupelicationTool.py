import sys, os, io, json, shutil, traceback, subprocess, struct, re
from datetime import datetime
from PyQt5.QtWidgets import *
from PyQt5.QtCore import Qt, QRegExp, QTimer
from PyQt5.QtGui import (
    QFont, QRegExpValidator, QStandardItemModel, QStandardItem,
    QKeySequence, QDragEnterEvent, QDropEvent, QTextCursor, QColor
)
import zstandard as zstd, sarc, oead

try:
    import byml as byml_legacy
    HAVE_LEGACY_BYML = True
except ImportError:
    HAVE_LEGACY_BYML = False

# Pfade für Einstellungen, Sprachdatei und externes Werkzeug
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "actor_duplication_settings.json")
LANGUAGE_FILE: str   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "language_stuff.json")
BFRES_RENAMER_EXE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "BfresRenamer.exe")

# Standardkonfiguration (wird beim ersten Start in die JSON geschrieben)
DEFAULT_SETTINGS = {
    "language": "en",
    "theme": "dark",
    "font_family": "Segoe UI",
    "font_size": 11,
    "window_geometry": "1100x750",
    "last_source": "",
    "last_dest": "",
    "actorinfo_path": "",
    "gameactorinfo_path": "",
    "rstb_exe_path": "",
    "rstbl_path": "",                     #irgendwann gemacht hoffentlich
    "tab_order": ["clone", "editor", "settings"],
    "editor_splitter": [300, 500],
    "clone_splitter": [300, 200],
    "main_splitter": [400, 100],
    "clone_checkboxes": {
        "adjust_actor_engine": True,
        "adjust_modelinfo_refs": True,
        "adjust_rsdb_entries": True,
        "adjust_rstbl": False             #auch
    },
    "enable_clone_log": False,
    "batch_mode": False,
    "open_editor_tabs": [],
    "high_dpi": False
}


# EINstellungen laden und speichern
def load_settings():
    """Lädt die Benutzereinstellungen aus der JSON-Datei. Fehlt sie, wird sie mit Standardwerten erstellt."""
    if not os.path.exists(SETTINGS_FILE):
        save_settings(DEFAULT_SETTINGS)
        return DEFAULT_SETTINGS.copy()
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        for k, v in DEFAULT_SETTINGS.items():
            if k not in loaded:
                loaded[k] = v
        if "clone_checkboxes" in loaded:
            for ck, cv in DEFAULT_SETTINGS["clone_checkboxes"].items():
                loaded["clone_checkboxes"].setdefault(ck, cv)
        if loaded.get("tab_order") == ["editor", "clone", "settings"]:
            loaded["tab_order"] = DEFAULT_SETTINGS["tab_order"]
        return loaded
    except:
        return DEFAULT_SETTINGS.copy()

def save_settings(settings):
    """Schreibt die Einstellungen in die JSON-Datei."""
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)

def load_texts(language_code):
    """Lädt die Übersetzungstexte für die gewählte Sprache."""
    try:
        with open(LANGUAGE_FILE, "r", encoding="utf-8") as f:
            all_texts = json.load(f)
        return all_texts.get(language_code, all_texts.get("de", {}))
    except:
        return {}


#Byml Funktion
def is_byml_container(obj):
    return isinstance(obj, (byml_legacy.SortedDict, list))

def byml_set_field_computed(obj, field, compute_fn):
    """Durchläuft rekursiv alle Werte und ersetzt das Feld 'field' durch den Rückgabewert von compute_fn."""
    if isinstance(obj, dict):
        if field in obj and isinstance(obj[field], str):
            obj[field] = compute_fn(obj[field])
        for v in obj.values():
            byml_set_field_computed(v, field, compute_fn)
    elif isinstance(obj, list):
        for v in obj:
            byml_set_field_computed(v, field, compute_fn)

def byml_copy(obj):
    if isinstance(obj, dict):
        new_dict = type(obj)() if hasattr(type(obj), '__call__') else {}
        for k, v in obj.items():
            new_dict[k] = byml_copy(v)
        return new_dict
    if isinstance(obj, list):
        return [byml_copy(v) for v in obj]
    return obj

def byml_set_field(obj, field, new_value):
    """Setzt rekursiv den Wert eines Feldes."""
    if isinstance(obj, dict):
        if field in obj:
            obj[field] = new_value
        for v in obj.values():
            byml_set_field(v, field, new_value)
    elif isinstance(obj, list):
        for v in obj:
            byml_set_field(v, field, new_value)

def byml_find_field(obj, field):
    """Sucht rekursiv nach einem Feld und gibt dessen Wert zurück."""
    if isinstance(obj, dict):
        if field in obj:
            return obj[field]
        for v in obj.values():
            result = byml_find_field(v, field)
            if result is not None:
                return result
    elif isinstance(obj, list):
        for v in obj:
            result = byml_find_field(v, field)
            if result is not None:
                return result
    return None

def parse_byml(data: bytes):
    if not HAVE_LEGACY_BYML:
        raise RuntimeError("byml ist nicht installiert.")
    return byml_legacy.Byml(data).parse()

def dump_byml(obj) -> bytes:
    if not HAVE_LEGACY_BYML:
        raise RuntimeError("byml ist nicht installiert.")
    writer = byml_legacy.Writer(obj)
    return writer.get_bytes()

def decompress_zs(path: str) -> bytes:
    with open(path, "rb") as f:
        return zstd.ZstdDecompressor().decompress(f.read())

def compress_and_write_zs(path: str, data: bytes, level=19):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(zstd.ZstdCompressor(level=level).compress(data))


#Actor und ModelInfo Dinger
def adjust_actor_param_modelinfo_ref(data: bytes, new_name: str) -> bytes:
    """Ersetzt den ModelInfoRef-Pfad im ActorParam durch den neuen Namen."""
    obj = parse_byml(data)
    def compute_new_ref(old_value: str) -> str:
        if "/" in old_value:
            directory = old_value.rsplit("/", 1)[0]
            return f"{directory}/{new_name}.engine__component__ModelInfo.bgyml"
        return f"{new_name}.engine__component__ModelInfo.bgyml"
    byml_set_field_computed(obj, "ModelInfoRef", compute_new_ref)
    return dump_byml(obj)

def adjust_model_info(data: bytes, change_fmdb: bool, change_modelproject: bool, new_name: str) -> bytes:
    """Benennt die Felder FmdbName und/oder ModelProjectName in der ModelInfo um."""
    obj = parse_byml(data)
    if change_fmdb:
        byml_set_field(obj, "FmdbName", new_name)
    if change_modelproject:
        byml_set_field(obj, "ModelProjectName", new_name)
    return dump_byml(obj)

def get_model_info_from_pack(pack_path, actor_name):
    """Liest aus dem Actor-Pack die ModelInfo und gibt (ModelProjectName, FmdbName) zurück."""
    mi_path = f"Component/ModelInfo/{actor_name}.engine__component__ModelInfo.bgyml"
    data = decompress_zs(pack_path)
    archive = sarc.SARC(data=data)
    if mi_path not in archive.list_files():
        return None
    content = bytes(archive.get_file_data(mi_path))
    obj = parse_byml(content)
    model_project_name = byml_find_field(obj, "ModelProjectName")
    fmdb_name = byml_find_field(obj, "FmdbName")
    if not model_project_name or not fmdb_name:
        return None
    return model_project_name, fmdb_name

def rsdb_clone_entry(data: bytes, id_field: str, old_id: str, new_id: str,
                      change_fmdb=False, change_modelproject=False, new_name=""):
    """Kopiert einen Eintrag in einer RSDB-Liste (ActorInfo/GameActorInfo) und fügt ihn mit neuer ID an."""
    root = parse_byml(data)
    if not isinstance(root, list):
        raise ValueError("RSDB-Root ist keine Liste")
    for entry in root:
        if isinstance(entry, dict) and entry.get(id_field) == new_id:
            return None, "exists"
    target = None
    for entry in root:
        if isinstance(entry, dict) and entry.get(id_field) == old_id:
            target = entry
            break
    if target is None:
        return None, "not_found"
    new_entry = byml_copy(target)
    new_entry[id_field] = new_id
    if change_fmdb:
        byml_set_field(new_entry, "FmdbName", new_name)
    if change_modelproject:
        byml_set_field(new_entry, "ModelProjectName", new_name)
    new_root = root + [new_entry]
    return dump_byml(new_root), "ok"

def clone_actorinfo_entry(path, old_name, new_name, change_fmdb=False, change_modelproject=False):
    if not os.path.isfile(path): return {"status": "file_missing"}
    data = decompress_zs(path)
    new_data, status = rsdb_clone_entry(data, "__RowId", old_name, new_name,
                                        change_fmdb, change_modelproject, new_name)
    if status == "ok":
        compress_and_write_zs(path, new_data)
    return {"status": status}

def clone_gameactorinfo_entry(path, old_name, new_name):
    if not os.path.isfile(path): return {"status": "file_missing"}
    data = decompress_zs(path)
    old_rowid = f"Work/Actor/{old_name}.engine__actor__ActorParam.gyml"
    new_rowid = f"Work/Actor/{new_name}.engine__actor__ActorParam.gyml"
    new_data, status = rsdb_clone_entry(data, "__RowId", old_rowid, new_rowid)
    if status == "ok":
        compress_and_write_zs(path, new_data)
    return {"status": status}

def remove_actorinfo_entry(path, row_id):
    if not os.path.isfile(path): return False
    data = decompress_zs(path)
    root = parse_byml(data)
    if not isinstance(root, list): return False
    new_root = [e for e in root if not (isinstance(e, dict) and e.get("__RowId") == row_id)]
    if len(new_root) == len(root):
        return False
    compress_and_write_zs(path, dump_byml(new_root))
    return True

def remove_gameactorinfo_entry(path, row_id):
    if not os.path.isfile(path): return False
    data = decompress_zs(path)
    root = parse_byml(data)
    if not isinstance(root, list): return False
    new_root = [e for e in root if not (isinstance(e, dict) and e.get("__RowId") == row_id)]
    if len(new_root) == len(root):
        return False
    compress_and_write_zs(path, dump_byml(new_root))
    return True


#BFRES Extraxtions Function 
def try_extract_bfres_embeds(raw):
    """Extrahiert eingebettete Dateien aus einer BFRES-Datei (für den Editor)."""
    if raw[:4] != b"FRES" or len(raw) < 0xEC+2: return {}
    bom = raw[0x0C:0x0E]; endian = ">" if bom == b"\xfe\xff" else "<"
    embed_count = struct.unpack_from(endian + "H", raw, 0xEC)[0]
    if embed_count == 0: return {}
    array_rel = struct.unpack_from(endian + "q", raw, 0xB8)[0]
    dict_rel = struct.unpack_from(endian + "q", raw, 0xC0)[0]
    if array_rel == 0 or dict_rel == 0: return {}
    array_addr = 0xB8 + array_rel; dict_addr = 0xC0 + dict_rel
    num_nodes = struct.unpack_from(endian + "I", raw, dict_addr)[0]
    root_idx = struct.unpack_from(endian + "I", raw, dict_addr + 4)[0]
    if num_nodes == 0: return {}
    NODE_SIZE = 0x18; base = dict_addr + 8
    items = []; visited = set(); stack = [root_idx]
    while stack:
        idx = stack.pop()
        if idx < 0 or idx >= num_nodes or idx in visited: continue
        visited.add(idx); node = base + idx * NODE_SIZE
        if node + NODE_SIZE > len(raw): continue
        left = struct.unpack_from(endian + "i", raw, node)[0]
        right = struct.unpack_from(endian + "i", raw, node + 4)[0]
        key_rel = struct.unpack_from(endian + "q", raw, node + 8)[0]
        value = struct.unpack_from(endian + "Q", raw, node + 16)[0]
        name = ""
        if key_rel != 0:
            key_addr = node + 8 + key_rel
            s = raw.find(b"\x00", key_addr)
            if s != -1 and s - key_addr <= 200:
                name = raw[key_addr:s].decode("utf-8", errors="ignore")
        if name: items.append((name, value))
        if left >= 0: stack.append(left)
        if right >= 0: stack.append(right)
    ENTRY_SIZE = 0x10; embeds = {}
    for name, index in items:
        entry = array_addr + index * ENTRY_SIZE
        if entry + ENTRY_SIZE > len(raw): continue
        file_size = struct.unpack_from(endian + "I", raw, entry)[0]
        data_offset = struct.unpack_from(endian + "q", raw, entry + 8)[0]
        if data_offset < 0 or data_offset + file_size > len(raw): continue
        embeds[name] = raw[data_offset:data_offset + file_size]
    return embeds


#Loging Sachen
CLONE_LOG_FILE = "clone_history.json"

def load_clone_history(dest_dir):
    path = os.path.join(dest_dir, CLONE_LOG_FILE)
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_clone_history(dest_dir, entries):
    path = os.path.join(dest_dir, CLONE_LOG_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)

def append_clone_entry(dest_dir, entry):
    history = load_clone_history(dest_dir)
    history.append(entry)
    save_clone_history(dest_dir, history)


#Undo SChinanigangs
class UndoDialog(QDialog):
    """Dialog zum Auswählen und Löschen von vorherigen Klonen (Dateien + RSDB-Einträge)."""
    def __init__(self, history, dest_dir, settings, texts, parent=None):
        super().__init__(parent)
        self.dest_dir = dest_dir
        self.history = history
        self.settings = settings
        self.texts = texts
        self.setWindowTitle(texts.get("undo_dialog_title", "Undo Clones"))
        self.resize(600, 400)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(texts.get("undo_select", "Select clones to undo:")))

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.MultiSelection)
        for entry in history:
            label = f"{entry['original_actor']} -> {entry['new_actor']}  ({entry['timestamp']})"
            self.list.addItem(label)
        layout.addWidget(self.list)

        self.select_all_cb = QCheckBox(texts.get("select_all", "Select All"))
        self.select_all_cb.toggled.connect(self.toggle_select_all)
        layout.addWidget(self.select_all_cb)

        self.remove_rsdb_cb = QCheckBox(texts.get("undo_remove_rsdb", "Remove RSDB entries"))
        self.remove_rsdb_cb.setToolTip(texts.get("undo_remove_rsdb_tooltip", "Also delete the corresponding entries from ActorInfo/GameActorInfo"))
        layout.addWidget(self.remove_rsdb_cb)

        btn_layout = QHBoxLayout()
        del_btn = QPushButton(texts.get("undo_delete_btn", "Delete Selected"))
        del_btn.clicked.connect(self.delete_selected)
        cancel_btn = QPushButton(texts.get("cancel", "Abbrechen"))
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(del_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

    def toggle_select_all(self, checked):
        if checked:
            self.list.selectAll()
        else:
            self.list.clearSelection()

    def delete_selected(self):
        selected = [i.row() for i in self.list.selectedIndexes()]
        if not selected:
            QMessageBox.information(self, self.texts.get("info_dialog_title", "Info"),
                                    self.texts.get("undo_info_none", "No entries selected."))
            return

        for idx in sorted(selected, reverse=True):
            entry = self.history[idx]
            # Dateien löschen
            pack_full = os.path.join(self.dest_dir, entry["pack_file"])
            model_full = os.path.join(self.dest_dir, entry["model_file"])
            for p in (pack_full, model_full):
                if os.path.isfile(p):
                    try:
                        os.remove(p)
                    except Exception as e:
                        QMessageBox.warning(self, self.texts.get("error_dialog_title", "Fehler"),
                                            self.texts.get("msg_delete_error", "Konnte {0} nicht löschen: {1}").format(p, e))

            # RSDB-Einträge entfernen (optional)
            if self.remove_rsdb_cb.isChecked():
                if entry.get("actorinfo_rowid"):
                    remove_actorinfo_entry(self.settings.get("actorinfo_path"), entry["actorinfo_rowid"])
                if entry.get("gameactorinfo_rowid"):
                    remove_gameactorinfo_entry(self.settings.get("gameactorinfo_path"), entry["gameactorinfo_rowid"])

            del self.history[idx]

        save_clone_history(self.dest_dir, self.history)
        QMessageBox.information(self, self.texts.get("info_dialog_title", "Info"),
                                self.texts.get("undo_success", "Selected clones have been deleted."))
        self.accept()


#Editor Tab
class SingleFileEditor(QWidget):
    """Editor für eine einzelne Pack-Datei (SARC, BYML, BFRES)."""
    def __init__(self, texts, path=None, parent=None):
        super().__init__(parent)
        self.texts = texts
        self.files = {}
        self.file_sizes = {}
        self.file_meta = {}
        self.original_path = None
        self.mode = None
        self.current_edit_path = None
        self.modified = False
        self.init_ui()
        if path:
            self.load_file(path)

    def tr(self, key, *args):
        return self.texts.get(key, key).format(*args)

    def init_ui(self):
        main_layout = QVBoxLayout(self)

        # Symbolleiste
        toolbar = QHBoxLayout()
        self.save_btn = QPushButton(self.tr("editor_save"))
        self.save_btn.clicked.connect(self.save)
        self.save_btn.setShortcut(QKeySequence.Save)
        toolbar.addWidget(self.save_btn)

        self.save_as_btn = QPushButton(self.tr("editor_save_as"))
        self.save_as_btn.clicked.connect(self.save_as)
        self.save_as_btn.setShortcut(QKeySequence("Ctrl+Shift+S"))
        toolbar.addWidget(self.save_as_btn)
        toolbar.addStretch()

        toolbar.addWidget(QLabel(self.tr("editor_search")))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(self.tr("search_placeholder"))
        self.search_edit.textChanged.connect(self.populate_tree)
        toolbar.addWidget(self.search_edit)

        self.search_content_cb = QCheckBox(self.tr("console_search_content"))
        self.search_content_cb.setChecked(True)
        self.search_content_cb.toggled.connect(self.populate_tree)
        toolbar.addWidget(self.search_content_cb)
        main_layout.addLayout(toolbar)

        # Splitter: Baum (links) / Editor (rechts)
        self.splitter = QSplitter(Qt.Horizontal)

        self.tree = QTreeView()
        self.model = QStandardItemModel()
        self.model.setHorizontalHeaderLabels([self.tr("editor_name_col"), self.tr("editor_size_col")])
        self.tree.setModel(self.model)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.on_context_menu)
        self.tree.doubleClicked.connect(self.on_double_click)
        self.splitter.addWidget(self.tree)

        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0,0,0,0)

        edit_toolbar = QHBoxLayout()
        self.edit_path_label = QLabel("")
        edit_toolbar.addWidget(self.edit_path_label)
        edit_toolbar.addStretch()
        self.save_current_btn = QPushButton(self.tr("editor_save_current"))
        self.save_current_btn.clicked.connect(self.save_current_file)
        self.save_current_btn.setEnabled(False)
        self.save_current_btn.setShortcut(QKeySequence("Ctrl+Return"))
        edit_toolbar.addWidget(self.save_current_btn)
        right_layout.addLayout(edit_toolbar)

        # Suche im geöffneten BYML/JSON
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel(self.tr("search_byml")))
        self.byml_search_edit = QLineEdit()
        self.byml_search_edit.setPlaceholderText(self.tr("search_byml"))
        self.byml_search_edit.textChanged.connect(self.highlight_byml)
        search_row.addWidget(self.byml_search_edit)
        right_layout.addLayout(search_row)

        self.text_edit = QPlainTextEdit()
        self.text_edit.setObjectName("monoEditor")
        self.text_edit.setFont(QFont("Consolas", 10))
        self.text_edit.setReadOnly(True)
        right_layout.addWidget(self.text_edit)

        self.splitter.addWidget(right_widget)
        main_layout.addWidget(self.splitter)

        self.restore_splitter()
        self.splitter.splitterMoved.connect(self.save_splitter)

    def restore_splitter(self):
        main_win = self.window()
        if hasattr(main_win, 'settings'):
            self.splitter.setSizes(main_win.settings.get("editor_splitter", [300, 500]))

    def save_splitter(self):
        main_win = self.window()
        if hasattr(main_win, 'settings'):
            main_win.settings["editor_splitter"] = self.splitter.sizes()
            save_settings(main_win.settings)

    def load_file(self, path):
        """Lädt eine Datei (.pack.zs, .byml.zs, .bfres.zs) und zeigt ihren Inhalt im Baum an."""
        if not os.path.isfile(path): return
        try:
            data = decompress_zs(path)
        except Exception as e:
            QMessageBox.critical(self, self.tr("error_dialog_title"), str(e))
            return
        fn = os.path.basename(path).lower()
        self.close_inline_editor()
        if fn.endswith(".bfres.zs") or data[:4] == b"FRES":
            self.mode = "bfres_raw"
            self.files.clear()
            self.file_sizes.clear()
            self.original_path = path
            embeds = try_extract_bfres_embeds(data)
            if embeds:
                for name, d in embeds.items():
                    full = f"Embedded/{name}"
                    self.files[full] = d
                    self.file_sizes[full] = len(d)
            else:
                name = os.path.basename(path)
                if name.lower().endswith(".zs"):
                    name = name[:-3]
                self.files[name] = data
                self.file_sizes[name] = len(data)
            self.populate_tree()
            return
        if fn.endswith((".byml.zs", ".bgyml.zs")):
            self.mode = "byml_single"
            entry = os.path.basename(path)[:-3]
            self.files = {entry: data}
            self.file_sizes = {entry: len(data)}
            self.original_path = path
            self.populate_tree()
            return
        if data[:4] == b"SARC":
            try:
                archive = sarc.SARC(data=data)
                names = archive.list_files()
                self.mode = "sarc"
                self.files = {n: bytes(archive.get_file_data(n)) for n in names}
                self.file_sizes = {p: len(d) for p, d in self.files.items()}
                self.original_path = path
                self.populate_tree()
            except Exception as e:
                QMessageBox.critical(self, self.tr("error_dialog_title"), str(e))
            return

    def populate_tree(self):
        """Baut den Dateibaum neu auf, berücksichtigt Suchfilter."""
        self.model.clear()
        self.model.setHorizontalHeaderLabels([self.tr("editor_name_col"), self.tr("editor_size_col")])
        filter_text = self.search_edit.text().strip().lower()
        search_content = self.search_content_cb.isChecked()
        for path in sorted(self.files):
            name_match = filter_text in path.lower() if filter_text else True
            content_match = False
            if search_content and filter_text:
                try:
                    content_match = filter_text in self.files[path].decode("utf-8", errors="ignore").lower()
                except:
                    pass
            if not name_match and not content_match:
                continue
            parts = path.split("/")
            parent = self.model.invisibleRootItem()
            for part in parts[:-1]:
                found = None
                for i in range(parent.rowCount()):
                    if parent.child(i).text() == part:
                        found = parent.child(i)
                        break
                if found is None:
                    found = QStandardItem(part)
                    found.setEditable(False)
                    parent.appendRow([found, QStandardItem("")])
                parent = found
            item = QStandardItem(parts[-1])
            item.setEditable(False)
            if content_match and not name_match:
                item.setForeground(QColor(255, 165, 0))
                item.setToolTip(self.tr("content_match_tooltip", "Contains search term"))
            size_item = QStandardItem(f"{self.file_sizes[path]:,} Bytes")
            size_item.setEditable(False)
            parent.appendRow([item, size_item])

    def on_double_click(self, index):
        item = self.model.itemFromIndex(index)
        if not item or item.hasChildren():
            return
        full_path = self._get_full_path(index)
        search_term = self.search_edit.text().strip() if self.search_content_cb.isChecked() else ""
        self.open_inline_editor(full_path, search_term)

    def open_inline_editor(self, full_path, auto_search=""):
        """Öffnet eine Datei aus dem Baum im Texteditor (BYML als JSON, sonst Hex/Text)."""
        data = self.files.get(full_path)
        if data is None:
            return
        self.current_edit_path = full_path
        filename = os.path.basename(full_path)
        self.edit_path_label.setText(f"Bearbeite: {filename}")
        self.save_current_btn.setEnabled(True)
        try:
            if filename.lower().endswith((".byml", ".bgyml")):
                obj = parse_byml(data)
                native = self._byml_to_native(obj)
                self.file_meta[full_path] = {"native_obj": native}
                self.text_edit.setPlainText(json.dumps(native, indent=2, ensure_ascii=False, default=str))
                self.text_edit.setReadOnly(False)
                if auto_search:
                    self.byml_search_edit.setText(auto_search)
            else:
                try:
                    decoded = data.decode("utf-8")
                    if all(32 <= ord(c) < 127 or c in "\n\r\t" for c in decoded[:500]):
                        self.text_edit.setPlainText(decoded[:50000])
                        self.text_edit.setReadOnly(False)
                    else:
                        raise ValueError("binary")
                except:
                    # Hex-Dump für Binärdaten
                    lines = []
                    for i in range(0, min(len(data), 4096), 16):
                        chunk = data[i:i+16]
                        hex_str = " ".join(f"{b:02x}" for b in chunk)
                        ascii_str = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                        lines.append(f"{i:08x}  {hex_str:<48}  {ascii_str}")
                    self.text_edit.setPlainText("\n".join(lines))
                    self.text_edit.setReadOnly(True)
        except Exception as e:
            QMessageBox.warning(self, self.tr("warning_dialog_title"), str(e))
            self.close_inline_editor()

    def _byml_to_native(self, obj):
        if isinstance(obj, dict):
            return {k: self._byml_to_native(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._byml_to_native(v) for v in obj]
        if isinstance(obj, bytes):
            return obj.hex()
        return obj

    def save_current_file(self):
        """Speichert die aktuell im Editor geöffnete Datei (als BYML oder Text)."""
        if not self.current_edit_path:
            return
        full_path = self.current_edit_path
        filename = os.path.basename(full_path)
        try:
            if filename.lower().endswith((".byml", ".bgyml")):
                new_native = json.loads(self.text_edit.toPlainText())
                new_obj = self._native_to_byml(new_native)
                new_data = dump_byml(new_obj)
            else:
                new_data = self.text_edit.toPlainText().encode("utf-8")
            self.files[full_path] = new_data
            self.file_sizes[full_path] = len(new_data)
            self.modified = True
            self.populate_tree()
        except Exception as e:
            QMessageBox.critical(self, self.tr("error_dialog_title"), str(e))

    def _native_to_byml(self, obj):
        if isinstance(obj, dict):
            return {k: self._native_to_byml(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._native_to_byml(v) for v in obj]
        if isinstance(obj, str):
            return obj
        if isinstance(obj, bool):
            return obj
        if isinstance(obj, int):
            return byml_legacy.Int(obj)
        if isinstance(obj, float):
            return byml_legacy.Float(obj)
        return obj

    def close_inline_editor(self):
        self.current_edit_path = None
        self.edit_path_label.setText("")
        self.save_current_btn.setEnabled(False)
        self.text_edit.clear()
        self.text_edit.setReadOnly(True)

    def on_context_menu(self, pos):
        index = self.tree.indexAt(pos)
        if not index.isValid():
            return
        item = self.model.itemFromIndex(index)
        menu = QMenu(self)
        if not item.hasChildren():
            menu.addAction(self.tr("editor_edit"), lambda: self.on_double_click(index))
        if self.mode != "bfres_raw":
            menu.addAction(self.tr("editor_rename"), lambda: self.rename_item(index))
            menu.addAction(self.tr("editor_delete"), lambda: self.delete_item(index))
        menu.addAction(self.tr("editor_export"), lambda: self.export_item(index))
        menu.exec_(self.tree.viewport().mapToGlobal(pos))

    def _get_full_path(self, index):
        parts = []
        while index.isValid():
            parts.append(self.model.itemFromIndex(index).text())
            index = index.parent()
        return "/".join(reversed(parts))

    def rename_item(self, index):
        old_path = self._get_full_path(index)
        item = self.model.itemFromIndex(index)
        is_folder = item.hasChildren()
        new_name, ok = QInputDialog.getText(self,
                                            self.tr("rename_dialog_title"),
                                            self.tr("rename_dialog_label"),
                                            text=item.text())
        if not ok or new_name == item.text() or "/" in new_name or "\\" in new_name:
            return
        if is_folder:
            prefix = old_path + "/"
            new_prefix = os.path.dirname(old_path) + "/" + new_name
            if new_prefix == "/":
                new_prefix = new_name
            new_files = {}
            for p, d in self.files.items():
                new_p = p.replace(prefix, new_prefix, 1) if p.startswith(prefix) else p
                if new_p != p and new_p in self.files:
                    QMessageBox.critical(self, self.tr("error_dialog_title"),
                                         self.tr("msg_path_exists").format(new_p))
                    return
                new_files[new_p] = d
            self.files = new_files
            self.file_sizes = {p: len(d) for p, d in self.files.items()}
        else:
            dirname = os.path.dirname(old_path)
            new_path = (dirname + "/" if dirname else "") + new_name
            if new_path in self.files:
                QMessageBox.critical(self, self.tr("error_dialog_title"),
                                     self.tr("msg_file_exists").format(new_name))
                return
            data = self.files.pop(old_path)
            self.files[new_path] = data
            self.file_sizes[new_path] = len(data)
            del self.file_sizes[old_path]
        self.modified = True
        self.populate_tree()

    def delete_item(self, index):
        path = self._get_full_path(index)
        item = self.model.itemFromIndex(index)
        if item.hasChildren():
            prefix = path + "/"
            to_delete = [p for p in self.files if p.startswith(prefix)]
            if QMessageBox.question(self, self.tr("delete_dialog_title"),
                                    self.tr("delete_confirm_folder").format(path, len(to_delete)),
                                    QMessageBox.Yes|QMessageBox.No) != QMessageBox.Yes:
                return
            for p in to_delete:
                del self.files[p], self.file_sizes[p]
        else:
            if QMessageBox.question(self, self.tr("delete_dialog_title"),
                                    self.tr("delete_confirm_file").format(path),
                                    QMessageBox.Yes|QMessageBox.No) != QMessageBox.Yes:
                return
            del self.files[path], self.file_sizes[path]
        self.modified = True
        self.populate_tree()

    def export_item(self, index):
        path = self._get_full_path(index)
        if self.model.itemFromIndex(index).hasChildren():
            return
        data = self.files.get(path)
        if data:
            save_path, _ = QFileDialog.getSaveFileName(self, self.tr("export_dialog_title"), os.path.basename(path))
            if save_path:
                with open(save_path, "wb") as f:
                    f.write(data)

    def build_save_bytes(self):
        if self.mode == "sarc":
            writer = sarc.SARCWriter(be=False)
            for name, data in self.files.items():
                writer.add_file(name, data)
            out = io.BytesIO()
            writer.write(out)
            return zstd.ZstdCompressor(level=19).compress(out.getvalue())
        elif self.mode == "byml_single":
            return zstd.ZstdCompressor(level=19).compress(list(self.files.values())[0])
        raise ValueError("Speichern nicht unterstützt")

    def save(self):
        if self.mode == "bfres_raw":
            QMessageBox.information(self, self.tr("info_dialog_title"),
                                    self.tr("msg_save_bfres_blocked"))
            return
        if not self.original_path:
            self.save_as()
            return
        try:
            with open(self.original_path, "wb") as f:
                f.write(self.build_save_bytes())
            self.modified = False
            self.window().statusBar().showMessage(self.tr("msg_saved"), 3000)
        except Exception as e:
            QMessageBox.critical(self, self.tr("error_dialog_title"), str(e))

    def save_as(self):
        if self.mode == "bfres_raw":
            QMessageBox.information(self, self.tr("info_dialog_title"),
                                    self.tr("msg_save_bfres_blocked"))
            return
        path, _ = QFileDialog.getSaveFileName(self, self.tr("save_as_dialog_title"), "",
                                              self.tr("file_filter_pack"))
        if path:
            try:
                with open(path, "wb") as f:
                    f.write(self.build_save_bytes())
                self.original_path = path
                self.modified = False
                self.window().statusBar().showMessage(self.tr("msg_saved"), 3000)
            except Exception as e:
                QMessageBox.critical(self, self.tr("error_dialog_title"), str(e))

    def highlight_byml(self):
        search_text = self.byml_search_edit.text()
        if not search_text:
            self.text_edit.setExtraSelections([])
            return
        selections = []
        doc = self.text_edit.document()
        cursor = QTextCursor(doc)
        while True:
            cursor = doc.find(search_text, cursor)
            if cursor.isNull():
                break
            extra = QTextEdit.ExtraSelection()
            extra.format.setBackground(Qt.yellow)
            extra.cursor = cursor
            selections.append(extra)
        self.text_edit.setExtraSelections(selections)

    def refresh_texts(self, texts):
        self.texts = texts
        self.save_btn.setText(self.tr("editor_save"))
        self.save_as_btn.setText(self.tr("editor_save_as"))
        self.search_edit.setPlaceholderText(self.tr("search_placeholder"))
        self.save_current_btn.setText(self.tr("editor_save_current"))
        self.populate_tree()


#Editor für mehrere Dateien in Tabs
class PackEditorWidget(QWidget):
    """Verwaltet mehrere SingleFileEditor-Instanzen in einem Tab-Widget."""
    def __init__(self, texts, parent=None):
        super().__init__(parent)
        self.texts = texts
        self.open_docs = {}
        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        self.open_btn = QPushButton(texts.get("editor_open", "Open"))
        self.open_btn.clicked.connect(self.open_file)
        self.open_btn.setShortcut(QKeySequence.Open)
        toolbar.addWidget(self.open_btn)
        toolbar.addStretch()
        layout.addLayout(toolbar)
        self.inner_tabs = QTabWidget()
        self.inner_tabs.setTabsClosable(True)
        self.inner_tabs.tabCloseRequested.connect(self.close_tab)
        layout.addWidget(self.inner_tabs)

    def tr(self, key, *args):
        return self.texts.get(key, key).format(*args)

    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(self, self.tr("open_file_dialog_title"), "",
                                              self.tr("file_filter_all_supported"))
        if path:
            self.load_file(path)

    def load_file(self, path):
        norm = os.path.normcase(os.path.abspath(path))
        if norm in self.open_docs:
            self.inner_tabs.setCurrentWidget(self.open_docs[norm])
            return
        editor = SingleFileEditor(self.texts, path, self)
        self.open_docs[norm] = editor
        self.inner_tabs.addTab(editor, os.path.basename(path))
        self.inner_tabs.setCurrentWidget(editor)

        # Titelaktualisierung bei Änderung
        editor.text_edit.modificationChanged.connect(
            lambda changed: self.update_tab_title(editor, changed)
        )
        editor.save_btn.clicked.connect(lambda: self.update_tab_title(editor, editor.modified))

    def update_tab_title(self, editor, modified):
        for i in range(self.inner_tabs.count()):
            if self.inner_tabs.widget(i) is editor:
                title = os.path.basename(editor.original_path) if editor.original_path else "Untitled"
                if modified:
                    self.inner_tabs.setTabText(i, title + " *")
                else:
                    self.inner_tabs.setTabText(i, title)
                break

    def close_tab(self, index):
        widget = self.inner_tabs.widget(index)
        if hasattr(widget, 'modified') and widget.modified:
            res = QMessageBox.question(self, self.tr("unsaved_changes_title"),
                                       self.tr("unsaved_changes_text"),
                                       QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
            if res == QMessageBox.Cancel:
                return
            if res == QMessageBox.Save:
                widget.save()
        for k, v in list(self.open_docs.items()):
            if v is widget:
                del self.open_docs[k]
                break
        self.inner_tabs.removeTab(index)
        widget.deleteLater()

    def refresh_texts(self, texts):
        self.texts = texts
        self.open_btn.setText(texts.get("editor_open", "Open"))
        for i in range(self.inner_tabs.count()):
            self.inner_tabs.widget(i).refresh_texts(texts)

    def get_open_tabs(self):
        return [editor.original_path for editor in self.open_docs.values() if editor.original_path]

    def restore_tabs(self, paths):
        for p in paths:
            if os.path.isfile(p):
                self.load_file(p)


#KLONEN (hier geht es ab)
class CloneTab(QWidget):
    """Tab zum Klonen eines Actors (Quell- → Ziel-RomFS)."""
    def __init__(self, settings, texts, log_func, progress_callback, editor_tab=None, main_window=None):
        super().__init__()
        self.settings = settings
        self.texts = texts
        self.log = log_func
        self.progress = progress_callback
        self.editor_tab = editor_tab
        self.main_window = main_window
        self.actors = []

        main_layout = QVBoxLayout(self)
        self.splitter = QSplitter(Qt.Vertical)

        # Oberer Bereich: Actor-Auswahl
        actor_widget = QWidget()
        actor_layout = QVBoxLayout(actor_widget)
        actor_layout.setContentsMargins(0,0,0,0)

        source_row = QHBoxLayout()
        source_row.addWidget(QLabel(self.tr("source_switch") + ":"))
        self.source_combo = QComboBox()
        self.source_combo.addItem("Original")
        self.source_combo.addItem("Mod")
        self.source_combo.setToolTip(self.tr("switch_source_tooltip"))
        self.source_combo.currentIndexChanged.connect(self.on_source_changed)
        source_row.addWidget(self.source_combo)
        actor_layout.addLayout(source_row)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel(self.tr("search_placeholder")))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(self.tr("search_placeholder"))
        self.search_edit.textChanged.connect(self.filter_actors)
        search_row.addWidget(self.search_edit)
        actor_layout.addLayout(search_row)

        self.actor_list = QListWidget()
        self.actor_list.currentItemChanged.connect(self.on_select)
        actor_layout.addWidget(self.actor_list)
        self.splitter.addWidget(actor_widget)

        # Unterer Bereich: Name, Optionen, Buttons
        lower_widget = QWidget()
        lower_layout = QVBoxLayout(lower_widget)
        lower_layout.setContentsMargins(0,0,0,0)

        self.name_stacked = QStackedWidget()
        self.single_name_edit = QLineEdit()
        self.single_name_edit.setPlaceholderText(self.tr("new_name_label"))
        self.single_name_edit.textChanged.connect(self.on_name_changed)
        self.name_stacked.addWidget(self.single_name_edit)

        self.batch_edit = QPlainTextEdit()
        self.batch_edit.setPlaceholderText(self.tr("clone_batch_placeholder"))
        self.batch_edit.textChanged.connect(self.on_batch_name_changed)
        self.name_stacked.addWidget(self.batch_edit)
        lower_layout.addWidget(QLabel(self.tr("new_name_label")))
        lower_layout.addWidget(self.name_stacked)

        # Checkboxen für Anpassungen
        saved_boxes = settings.get("clone_checkboxes", DEFAULT_SETTINGS["clone_checkboxes"])
        self.cb_actor_engine = QCheckBox(self.tr("check_actor_engine"))
        self.cb_actor_engine.setChecked(saved_boxes.get("adjust_actor_engine", True))
        self.cb_actor_engine.setToolTip(self.tr("tooltip_actor_engine"))
        self.cb_actor_engine.toggled.connect(lambda: self.save_checkbox("adjust_actor_engine", self.cb_actor_engine.isChecked()))
        lower_layout.addWidget(self.cb_actor_engine)

        self.cb_modelinfo_refs = QCheckBox(self.tr("check_modelinfo_refs"))
        self.cb_modelinfo_refs.setChecked(saved_boxes.get("adjust_modelinfo_refs", True))
        self.cb_modelinfo_refs.setToolTip(self.tr("tooltip_modelinfo_refs"))
        self.cb_modelinfo_refs.toggled.connect(lambda: self.save_checkbox("adjust_modelinfo_refs", self.cb_modelinfo_refs.isChecked()))
        lower_layout.addWidget(self.cb_modelinfo_refs)

        self.cb_rsdb_entries = QCheckBox(self.tr("check_rsdb_entries"))
        self.cb_rsdb_entries.setChecked(saved_boxes.get("adjust_rsdb_entries", True))
        self.cb_rsdb_entries.setToolTip(self.tr("tooltip_rsdb_entries"))
        self.cb_rsdb_entries.toggled.connect(lambda: self.save_checkbox("adjust_rsdb_entries", self.cb_rsdb_entries.isChecked()))
        lower_layout.addWidget(self.cb_rsdb_entries)

        self.cb_rstbl = QCheckBox(self.tr("check_rstbl"))
        self.cb_rstbl.setChecked(saved_boxes.get("adjust_rstbl", False))
        self.cb_rstbl.setToolTip(self.tr("tooltip_rstbl"))
        self.cb_rstbl.toggled.connect(lambda: self.save_checkbox("adjust_rstbl", self.cb_rstbl.isChecked()))
        lower_layout.addWidget(self.cb_rstbl)

        # Buttons
        btn_row = QHBoxLayout()
        self.clone_btn = QPushButton(self.tr("clone_btn"))
        self.clone_btn.setEnabled(False)
        self.clone_btn.clicked.connect(self.clone_actor)
        btn_row.addWidget(self.clone_btn)

        self.rstb_btn = QPushButton(self.tr("rstb_now"))
        self.rstb_btn.clicked.connect(self.generate_rstb)
        self.update_rstb_button_state()
        btn_row.addWidget(self.rstb_btn)

        self.undo_btn = QPushButton(self.tr("undo_btn"))
        self.undo_btn.clicked.connect(self.show_undo_dialog)
        self.undo_btn.setVisible(self.settings.get("enable_clone_log", False))
        btn_row.addWidget(self.undo_btn)
        lower_layout.addLayout(btn_row)

        self.splitter.addWidget(lower_widget)
        main_layout.addWidget(self.splitter)

        self.restore_splitter()
        self.splitter.splitterMoved.connect(self.save_splitter)

        self.on_source_changed()

    def tr(self, key, *args):
        return self.texts.get(key, key).format(*args)

    def save_checkbox(self, key, value):
        boxes = self.settings.setdefault("clone_checkboxes", {})
        boxes[key] = value
        save_settings(self.settings)

    def restore_splitter(self):
        if hasattr(self.window(), 'settings'):
            self.splitter.setSizes(self.window().settings.get("clone_splitter", [300, 200]))

    def save_splitter(self):
        if hasattr(self.window(), 'settings'):
            self.window().settings["clone_splitter"] = self.splitter.sizes()
            save_settings(self.window().settings)

    def on_source_changed(self):
        """Wechselt zwischen Original- und Mod-Quelle."""
        idx = self.source_combo.currentIndex()
        if idx == 0:
            src = self.settings.get("last_source", "")
        else:
            src = self.settings.get("last_dest", "")
        pack_dir = os.path.join(src, "Pack", "Actor") if src else ""
        valid = bool(src) and os.path.isdir(pack_dir)

        if not valid:
            self.actor_list.clear()
            self.actor_list.setEnabled(False)
            self.search_edit.setEnabled(False)
            self.single_name_edit.setEnabled(False)
            self.batch_edit.setEnabled(False)
            self.clone_btn.setEnabled(False)
            self.source_combo.setToolTip(self.texts.get("switch_source_tooltip", "") + "\n(Kein gültiger Ordner)")
            return

        self.actor_list.setEnabled(True)
        self.search_edit.setEnabled(True)
        self.single_name_edit.setEnabled(True)
        self.batch_edit.setEnabled(True)
        self.source_combo.setToolTip(self.texts.get("switch_source_tooltip", ""))
        self.load_actors(src)

    def load_actors(self, directory=None):
        if directory is None:
            idx = self.source_combo.currentIndex()
            if idx == 0:
                directory = self.settings.get("last_source", "")
            else:
                directory = self.settings.get("last_dest", "")
        if not directory:
            self.actors = []
            self.filter_actors()
            return
        pack_dir = os.path.join(directory, "Pack", "Actor")
        if os.path.isdir(pack_dir):
            self.actors = sorted([f[:-8] for f in os.listdir(pack_dir) if f.endswith(".pack.zs")])
        else:
            self.actors = []
        self.filter_actors()

    def filter_actors(self):
        s = self.search_edit.text().lower()
        self.actor_list.clear()
        for a in self.actors:
            if s in a.lower():
                self.actor_list.addItem(a)

    def on_select(self, curr, prev):
        if curr:
            if self.settings.get("batch_mode"):
                pass
            else:
                self.single_name_edit.setText(curr.text())
            self.clone_btn.setEnabled(True)
            self.update_clone_btn_tooltip()
        else:
            self.clone_btn.setEnabled(False)

    def on_name_changed(self):
        name = self.single_name_edit.text().strip()
        self.validate_name(name)
        self.update_clone_btn_tooltip()

    def on_batch_name_changed(self):
        names = self.get_batch_names()
        all_valid = all(self.is_name_valid(n) for n in names) if names else False
        if names and all_valid:
            self.single_name_edit.setStyleSheet("")
            self.clone_btn.setEnabled(True)
        else:
            self.single_name_edit.setStyleSheet("background-color: #b71c1c;")
            self.clone_btn.setEnabled(False)
        self.update_clone_btn_tooltip()

    def get_batch_names(self):
        text = self.batch_edit.toPlainText().strip()
        if not text:
            return []
        parts = re.split(r'[;\n]', text)
        return [p.strip() for p in parts if p.strip()]

    def is_name_valid(self, name):
        if not name:
            return False
        dest = self.settings.get("last_dest", "")
        if not dest:
            return False
        pack_path = os.path.join(dest, "Pack", "Actor", f"{name}.pack.zs")
        return not os.path.exists(pack_path)

    def validate_name(self, name):
        valid = self.is_name_valid(name)
        if valid:
            self.single_name_edit.setStyleSheet("")
        else:
            self.single_name_edit.setStyleSheet("background-color: #b71c1c;")
        self.clone_btn.setEnabled(valid)
        self.update_clone_btn_tooltip()

    def update_clone_btn_tooltip(self):
        if self.clone_btn.isEnabled():
            self.clone_btn.setToolTip("")
        else:
            reasons = []
            if not self.actor_list.currentItem():
                reasons.append("Kein Actor ausgewählt")
            else:
                name = self.single_name_edit.text().strip() if not self.settings.get("batch_mode") else self.get_batch_names()
                if not name:
                    reasons.append("Kein Name eingegeben")
                elif isinstance(name, str) and not self.is_name_valid(name):
                    reasons.append("Name existiert bereits oder ist ungültig")
                elif isinstance(name, list) and not all(self.is_name_valid(n) for n in name):
                    reasons.append("Mindestens ein Name existiert bereits")
                if not self.settings.get("last_dest"):
                    reasons.append("Zielordner fehlt")
            self.clone_btn.setToolTip("; ".join(reasons) if reasons else "Klonen möglich")

    def update_rstb_button_state(self):
        exe = self.settings.get("rstb_exe_path", "")
        ok = bool(exe and os.path.isfile(exe))
        self.rstb_btn.setEnabled(ok)
        if ok:
            self.rstb_btn.setToolTip(self.texts.get("rstb_exe_tooltip", ""))
        else:
            self.rstb_btn.setToolTip(self.texts.get("rstb_missing_tooltip", "RSTB Exe Pfad in Einstellungen angeben"))

    def generate_rstb(self):
        exe = self.settings.get("rstb_exe_path", "")
        if not exe or not os.path.isfile(exe):
            self.log(self.tr("error_rstb_exe_missing"))
            return
        dest = self.settings.get("last_dest", "")
        if not dest:
            self.log(self.tr("msg_no_dest_folder"))
            return
        self.log(self.tr("log_rstb_start"))
        try:
            subprocess.Popen([exe], cwd=dest)
            self.log(self.tr("log_rstb_done"))
        except Exception as e:
            self.log(f"RSTB Fehler: {e}")

    def update_batch_mode(self):
        batch = self.settings.get("batch_mode", False)
        self.name_stacked.setCurrentIndex(1 if batch else 0)
        if batch:
            self.single_name_edit.clear()
            self.clone_btn.setEnabled(False)
        else:
            self.batch_edit.clear()
            self.on_name_changed()

    def adjust_bfres_model(self, model_path, old_names, new_name):
        """Ruft BfresRenamer.exe auf, um interne Modellnamen zu ändern."""
        if not os.path.isfile(BFRES_RENAMER_EXE):
            return {"status": "error", "message": f"BfresRenamer.exe nicht gefunden unter: {BFRES_RENAMER_EXE}"}
        if isinstance(old_names, (list, tuple, set)):
            names = [n for n in old_names if n]
        else:
            names = [old_names] if old_names else []
        seen = set()
        unique_names = []
        for n in names:
            if n not in seen:
                seen.add(n)
                unique_names.append(n)
        old_names_arg = ",".join(unique_names)
        try:
            result = subprocess.run(
                [BFRES_RENAMER_EXE, model_path, old_names_arg, new_name],
                capture_output=True, text=True, encoding="utf-8-sig", timeout=30
            )
            stdout = result.stdout.strip()
            if not stdout:
                stderr = (result.stderr or "").strip()
                return {"status": "error", "message": f"Keine Ausgabe. Exit-Code {result.returncode}. Stderr: {stderr or '(leer)'}"}
            json_line = None
            for line in reversed(stdout.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    json_line = line
                    break
            if json_line is None:
                return {"status": "error", "message": f"Keine JSON-Zeile gefunden: {stdout!r}"}
            return json.loads(json_line)
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def clone_actor(self):
        """Startet den Klonvorgang (Einzel- oder Batch-Modus)."""
        if self.settings.get("batch_mode"):
            names = self.get_batch_names()
            if not names:
                self.log(self.tr("msg_no_valid_batch_names"))
                return
            self.log(self.tr("log_batch_start", len(names)))
            self.progress(0)
            for i, new_name in enumerate(names):
                if not self.is_name_valid(new_name):
                    self.log(self.tr("msg_skipping_existing").format(new_name))
                    continue
                self.log(self.tr("log_batch_progress", i+1, len(names), self.actor_list.currentItem().text(), new_name))
                self._clone_single(new_name)
                self.progress(int((i+1)/len(names)*100))
            self.progress(100)
            self.log(self.tr("msg_batch_done"))
        else:
            new_name = self.single_name_edit.text().strip()
            if not new_name or not self.is_name_valid(new_name):
                self.log(self.tr("msg_invalid_name"))
                return
            self._clone_single(new_name)

    def _clone_single(self, new_name):
        """Führt das Klonen für einen einzelnen Actor durch."""
        src_type = self.source_combo.currentIndex()
        if src_type == 0:
            src = self.settings.get("last_source", "")
        else:
            src = self.settings.get("last_dest", "")
        dest = self.settings.get("last_dest", "")
        if not src or not dest:
            self.log(self.tr("error_no_source"))
            return

        old_name = self.actor_list.currentItem().text()
        src_pack = os.path.join(src, "Pack", "Actor", f"{old_name}.pack.zs")
        if not os.path.isfile(src_pack):
            self.log(self.tr("msg_source_pack_missing"))
            return

        old_fmdb_name = None
        old_model_project_name = None
        if self.cb_modelinfo_refs.isChecked():
            try:
                names = get_model_info_from_pack(src_pack, old_name)
            except Exception as e:
                names = None
                self.log(f"Fehler beim Lesen der ModelInfo: {e}")
            if not names:
                self.log(self.tr("error_modelinfo_unreadable"))
                return
            old_model_project_name, old_fmdb_name = names
            src_model = os.path.join(src, "Model", f"{old_model_project_name}.bfres.zs")
        else:
            src_model = os.path.join(src, "Model", f"{old_name}.bfres.zs")

        if not os.path.isfile(src_model):
            self.log("Quell-Model fehlt: " + src_model)
            return

        dest_pack = os.path.join(dest, "Pack", "Actor", f"{new_name}.pack.zs")
        dest_model = os.path.join(dest, "Model", f"{new_name}.bfres.zs")
        os.makedirs(os.path.dirname(dest_pack), exist_ok=True)
        os.makedirs(os.path.dirname(dest_model), exist_ok=True)

        if os.path.exists(dest_pack) or os.path.exists(dest_model):
            self.log(self.tr("msg_dest_file_exists"))
            return

        self.progress(10)
        self.log(self.tr("log_clone_start", old_name, new_name))
        shutil.copy2(src_pack, dest_pack)
        shutil.copy2(src_model, dest_model)
        self.log(self.tr("log_copy_pack", old_name, new_name))
        self.log(self.tr("log_copy_model", src_model, new_name))
        self.progress(30)

        self.adjust_internal_pack(dest_pack, old_name, new_name, old_fmdb_name, old_model_project_name)
        self.progress(60)

        if self.cb_modelinfo_refs.isChecked():
            bfres_result = self.adjust_bfres_model(dest_model, [old_model_project_name, old_fmdb_name], new_name)
            if bfres_result:
                if bfres_result.get("status") == "ok":
                    for r in bfres_result.get("renamed", []):
                        self.log(self.tr("log_bfres_renamed", r.get("old", "?"), r.get("new", new_name)))
                else:
                    self.log(self.tr("log_bfres_error", bfres_result.get("message")))
        self.progress(80)

        if self.cb_rsdb_entries.isChecked():
            rsdb_msg = self.update_rsdb(old_name, new_name)
            if rsdb_msg:
                self.log(rsdb_msg)

        # Optional: TagDatabase (rstbl) aktualisieren
        if self.cb_rstbl.isChecked():
            rstbl_path = self.settings.get("rstbl_path", "")
            if rstbl_path and os.path.isfile(rstbl_path):
                self.update_rstbl(rstbl_path, new_name)
            else:
                self.log(self.tr("error_rstbl_missing"))

        self.progress(100)

        if self.settings.get("enable_clone_log"):
            entry = {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "original_actor": old_name,
                "new_actor": new_name,
                "pack_file": f"Pack/Actor/{new_name}.pack.zs",
                "model_file": f"Model/{new_name}.bfres.zs",
                "actorinfo_rowid": new_name if self.cb_rsdb_entries.isChecked() else None,
                "gameactorinfo_rowid": f"Work/Actor/{new_name}.engine__actor__ActorParam.gyml" if self.cb_rsdb_entries.isChecked() else None
            }
            append_clone_entry(dest, entry)

        self.log(self.tr("log_clone_success", new_name))
        if not self.settings.get("batch_mode"):
            self.single_name_edit.clear()
        if self.source_combo.currentIndex() == 1:
            self.load_actors()

    def adjust_internal_pack(self, pack_path, old_name, new_name, old_fmdb_name=None, old_model_project_name=None):
        """Passt die Dateien im kopierten Pack an (ActorParam, ModelInfo)."""
        do_actor_rename = self.cb_actor_engine.isChecked()
        do_model_refs = self.cb_modelinfo_refs.isChecked()
        if not do_actor_rename and not do_model_refs:
            return
        data = decompress_zs(pack_path)
        archive = sarc.SARC(data=data)
        files = {n: bytes(archive.get_file_data(n)) for n in archive.list_files()}
        old_ap = f"Actor/{old_name}.engine__actor__ActorParam.bgyml"
        new_ap = f"Actor/{new_name}.engine__actor__ActorParam.bgyml"
        if old_ap in files:
            content = files.pop(old_ap)
            if do_model_refs:
                content = adjust_actor_param_modelinfo_ref(content, new_name)
            files[new_ap if do_actor_rename else old_ap] = content
        old_mi = f"Component/ModelInfo/{old_name}.engine__component__ModelInfo.bgyml"
        new_mi = f"Component/ModelInfo/{new_name}.engine__component__ModelInfo.bgyml"
        if old_mi in files and do_model_refs:
            content = files.pop(old_mi)
            content = adjust_model_info(content, change_fmdb=True, change_modelproject=True, new_name=new_name)
            files[new_mi] = content
        writer = sarc.SARCWriter(be=False)
        for n, d in files.items():
            writer.add_file(n, d)
        out = io.BytesIO()
        writer.write(out)
        compress_and_write_zs(pack_path, out.getvalue())

    def update_rsdb(self, old_name, new_name):
        """Fügt Einträge in ActorInfo und GameActorInfo hinzu."""
        msgs = []
        change_model = self.cb_modelinfo_refs.isChecked()
        actorinfo_path = self.settings.get("actorinfo_path")
        if actorinfo_path:
            res = clone_actorinfo_entry(actorinfo_path, old_name, new_name,
                                        change_fmdb=change_model, change_modelproject=change_model)
            if res['status'] == 'ok':
                msgs.append(self.tr("log_rsdb_actorinfo_ok"))
            elif res['status'] == 'exists':
                msgs.append(self.tr("log_rsdb_exists").format("ActorInfo"))
            elif res['status'] == 'not_found':
                msgs.append(self.tr("log_rsdb_not_found").format("ActorInfo"))
            else:
                msgs.append(self.tr("log_rsdb_error").format("ActorInfo", res.get('message', '')))
        gameactorinfo_path = self.settings.get("gameactorinfo_path")
        if gameactorinfo_path:
            res = clone_gameactorinfo_entry(gameactorinfo_path, old_name, new_name)
            if res['status'] == 'ok':
                msgs.append(self.tr("log_rsdb_gameactorinfo_ok"))
            elif res['status'] == 'exists':
                msgs.append(self.tr("log_rsdb_exists").format("GameActorInfo"))
            elif res['status'] == 'not_found':
                msgs.append(self.tr("log_rsdb_not_found").format("GameActorInfo"))
            else:
                msgs.append(self.tr("log_rsdb_error").format("GameActorInfo", res.get('message', '')))
        return "\n".join(msgs) if msgs else None

    def update_rstbl(self, rstbl_path, new_name):
        """Fügt einen neuen Eintrag in die TagDatabase (rstbl.byml.zs) ein."""
        # TODO: Implementierung der RSTBL-Modifikation
        self.log(self.tr("rstbl_update_not_implemented").format(new_name))

    def show_undo_dialog(self):
        dest = self.settings.get("last_dest")
        if not dest:
            return
        history = load_clone_history(dest)
        if not history:
            QMessageBox.information(self, self.tr("info_dialog_title"),
                                    self.tr("undo_no_history"))
            return
        dlg = UndoDialog(history, dest, self.settings, self.texts, self)
        dlg.exec_()
        if self.source_combo.currentIndex() == 1:
            self.load_actors()

    def refresh_texts(self, texts):
        self.texts = texts
        self.single_name_edit.setPlaceholderText(self.tr("new_name_label"))
        self.batch_edit.setPlaceholderText(self.tr("clone_batch_placeholder"))
        self.clone_btn.setText(self.tr("clone_btn"))
        self.rstb_btn.setText(self.tr("rstb_now"))
        self.undo_btn.setText(self.tr("undo_btn"))
        self.cb_actor_engine.setText(self.tr("check_actor_engine"))
        self.cb_modelinfo_refs.setText(self.tr("check_modelinfo_refs"))
        self.cb_rsdb_entries.setText(self.tr("check_rsdb_entries"))
        self.cb_rstbl.setText(self.tr("check_rstbl"))
        self.source_combo.setToolTip(self.tr("switch_source_tooltip"))
        self.search_edit.setPlaceholderText(self.tr("search_placeholder"))
        self.update_clone_btn_tooltip()
        self.update_rstb_button_state()


#Einstellungen
class SettingsTab(QWidget):
    """Tab für alle Benutzereinstellungen (Pfade, Sprache, Theme, etc.)."""
    def __init__(self, settings, texts, main_window):
        super().__init__()
        self.settings = settings
        self.texts = texts
        self.main = main_window

        layout = QVBoxLayout(self)
        form = QFormLayout()

        # Quell-RomFS
        self.src_edit = QLineEdit(settings.get("last_source", ""))
        self.src_edit.textChanged.connect(lambda t: self.update_path("last_source", t, self.src_edit, "source"))
        self.src_edit.setToolTip(texts.get("source_dir_tooltip", ""))
        src_row = QHBoxLayout()
        src_row.addWidget(self.src_edit)
        src_row.addWidget(QPushButton(texts.get("browse_btn", "..."), clicked=lambda: self.browse_folder("last_source", self.src_edit)))
        form.addRow(texts.get("source_label", "Source RomFS:"), src_row)

        # Mod-RomFS
        self.dst_edit = QLineEdit(settings.get("last_dest", ""))
        self.dst_edit.textChanged.connect(lambda t: self.update_path("last_dest", t, self.dst_edit, "dest"))
        self.dst_edit.setToolTip(texts.get("dest_dir_tooltip", ""))
        dst_row = QHBoxLayout()
        dst_row.addWidget(self.dst_edit)
        dst_row.addWidget(QPushButton(texts.get("browse_btn", "..."), clicked=lambda: self.browse_folder("last_dest", self.dst_edit)))
        form.addRow(texts.get("dest_label", "Mod RomFS:"), dst_row)

        # ActorInfo
        self.actorinfo_edit = QLineEdit(settings.get("actorinfo_path", ""))
        self.actorinfo_edit.textChanged.connect(lambda t: self.update_path("actorinfo_path", t, self.actorinfo_edit, "actorinfo"))
        self.actorinfo_edit.setToolTip(texts.get("actorinfo_tooltip", ""))
        row = QHBoxLayout()
        row.addWidget(self.actorinfo_edit)
        row.addWidget(QPushButton(texts.get("browse_btn", "..."), clicked=lambda: self.browse_rsdb("actorinfo")))
        self.actorinfo_editor_btn = QPushButton(texts.get("open_editor_btn", "Editor"))
        self.actorinfo_editor_btn.clicked.connect(lambda: self.main.open_path_in_editor(self.actorinfo_edit.text()))
        row.addWidget(self.actorinfo_editor_btn)
        form.addRow(texts.get("actorinfo_label", "ActorInfo...:"), row)

        # GameActorInfo
        self.gameactorinfo_edit = QLineEdit(settings.get("gameactorinfo_path", ""))
        self.gameactorinfo_edit.textChanged.connect(lambda t: self.update_path("gameactorinfo_path", t, self.gameactorinfo_edit, "gameactorinfo"))
        self.gameactorinfo_edit.setToolTip(texts.get("gameactorinfo_tooltip", ""))
        row = QHBoxLayout()
        row.addWidget(self.gameactorinfo_edit)
        row.addWidget(QPushButton(texts.get("browse_btn", "..."), clicked=lambda: self.browse_rsdb("gameactorinfo")))
        self.gameactorinfo_editor_btn = QPushButton(texts.get("open_editor_btn", "Editor"))
        self.gameactorinfo_editor_btn.clicked.connect(lambda: self.main.open_path_in_editor(self.gameactorinfo_edit.text()))
        row.addWidget(self.gameactorinfo_editor_btn)
        form.addRow(texts.get("gameactorinfo_label", "GameActorInfo...:"), row)

        # RSTB Exe
        self.rstb_edit = QLineEdit(settings.get("rstb_exe_path", ""))
        self.rstb_edit.textChanged.connect(lambda t: self.update_path("rstb_exe_path", t, self.rstb_edit, "rstb"))
        self.rstb_edit.setToolTip(texts.get("rstb_exe_tooltip", ""))
        row = QHBoxLayout()
        row.addWidget(self.rstb_edit)
        row.addWidget(QPushButton(texts.get("browse_btn", "..."), clicked=self.browse_rstb))
        form.addRow(texts.get("rstb_exe_label", "Wonder RSTB Gen.exe:"), row)

        # RSTBL (TagDatabase)
        self.rstbl_edit = QLineEdit(settings.get("rstbl_path", ""))
        self.rstbl_edit.textChanged.connect(lambda t: self.update_path("rstbl_path", t, self.rstbl_edit, "rstbl"))
        self.rstbl_edit.setToolTip(texts.get("rstbl_tooltip", ""))
        row = QHBoxLayout()
        row.addWidget(self.rstbl_edit)
        row.addWidget(QPushButton(texts.get("browse_btn", "..."), clicked=lambda: self.browse_rstbl()))
        self.rstbl_editor_btn = QPushButton(texts.get("open_editor_btn", "Editor"))
        self.rstbl_editor_btn.clicked.connect(lambda: self.main.open_path_in_editor(self.rstbl_edit.text()))
        row.addWidget(self.rstbl_editor_btn)
        form.addRow(texts.get("rstbl_label", "RSTBL...:"), row)

        layout.addLayout(form)

        # Allgemeine Einstellungen
        group = QGroupBox(texts.get("settings_group", "General"))
        gen_form = QFormLayout()

        self.lang_combo = QComboBox()
        self.lang_combo.addItems(["Deutsch", "English"])
        self.lang_combo.setCurrentText("Deutsch" if settings["language"]=="de" else "English")
        self.lang_combo.currentIndexChanged.connect(self.on_language_changed)
        gen_form.addRow(texts.get("language", "Language"), self.lang_combo)

        self.theme_combo = QComboBox()
        self.theme_combo.addItems([texts.get("dark", "Dark"), texts.get("light", "Light")])
        self.theme_combo.setCurrentText(texts.get(settings["theme"], "Dark"))
        self.theme_combo.currentIndexChanged.connect(self.on_theme_changed)
        gen_form.addRow(texts.get("theme", "Theme"), self.theme_combo)

        self.font_combo = QComboBox()
        self.font_combo.setEditable(True)
        self.font_combo.addItems(["Segoe UI","Arial","Courier New","Times New Roman","Verdana"])
        self.font_combo.setCurrentText(settings["font_family"])
        self.font_combo.currentTextChanged.connect(self.on_font_changed)
        gen_form.addRow(texts.get("font", "Font"), self.font_combo)

        self.size_spin = QSpinBox()
        self.size_spin.setRange(8,30)
        self.size_spin.setValue(settings["font_size"])
        self.size_spin.valueChanged.connect(self.on_font_size_changed)
        gen_form.addRow(texts.get("font_size", "Font Size"), self.size_spin)

        self.high_dpi_cb = QCheckBox(texts.get("high_dpi", "High DPI (restart required)"))
        self.high_dpi_cb.setChecked(settings.get("high_dpi", False))
        self.high_dpi_cb.toggled.connect(self.on_high_dpi_toggled)
        self.high_dpi_cb.setToolTip(texts.get("high_dpi_tooltip", ""))
        gen_form.addRow(self.high_dpi_cb)

        self.batch_cb = QCheckBox(texts.get("batch_mode", "Batch Mode"))
        self.batch_cb.setChecked(settings.get("batch_mode", False))
        self.batch_cb.toggled.connect(self.on_batch_toggled)
        self.batch_cb.setToolTip(texts.get("batch_mode_tooltip", ""))
        gen_form.addRow(self.batch_cb)

        self.log_cb = QCheckBox(texts.get("enable_clone_log", "Enable Clone Log"))
        self.log_cb.setChecked(settings.get("enable_clone_log", False))
        self.log_cb.toggled.connect(self.on_log_toggled)
        self.log_cb.setToolTip(texts.get("enable_clone_log_tooltip", ""))
        gen_form.addRow(self.log_cb)

        group.setLayout(gen_form)
        layout.addWidget(group)

        self.validate_all()
        self.update_editor_buttons_state()

    def tr(self, key, *args):
        return self.texts.get(key, key).format(*args)

    def update_editor_buttons_state(self):
        for edit, btn in [(self.actorinfo_edit, self.actorinfo_editor_btn),
                          (self.gameactorinfo_edit, self.gameactorinfo_editor_btn),
                          (self.rstbl_edit, self.rstbl_editor_btn)]:
            path = edit.text().strip()
            ok = os.path.isfile(path)
            btn.setEnabled(ok)
            btn.setToolTip(self.texts.get("open_editor_btn", "Im Editor öffnen") if ok else self.texts.get("editor_btn_missing", "Pfad nicht gültig"))

    def browse_folder(self, key, edit):
        folder = QFileDialog.getExistingDirectory(self, self.tr("select_folder_dialog_title"))
        if folder:
            edit.setText(folder)

    def browse_rsdb(self, which):
        path, _ = QFileDialog.getOpenFileName(self, self.tr("select_rsdb_dialog_title"), "",
                                              self.tr("file_filter_byml"))
        if path:
            if which == "actorinfo":
                self.actorinfo_edit.setText(path)
            else:
                self.gameactorinfo_edit.setText(path)

    def browse_rstb(self):
        path, _ = QFileDialog.getOpenFileName(self, self.tr("select_rstb_exe_dialog_title"), "",
                                              self.tr("file_filter_exe"))
        if path:
            self.rstb_edit.setText(path)

    def browse_rstbl(self):
        path, _ = QFileDialog.getOpenFileName(self, self.tr("select_rstbl_dialog_title"), "",
                                              self.tr("file_filter_byml"))
        if path:
            self.rstbl_edit.setText(path)

    def update_path(self, key, value, edit, context):
        self.settings[key] = value
        save_settings(self.settings)
        self.validate_path(edit, context)
        if key == "last_source":
            self.main.clone_tab.on_source_changed()
        if key == "last_dest":
            self.main.clone_tab.on_name_changed()
            self.main.clone_tab.on_source_changed()
        if key == "rstb_exe_path":
            self.main.clone_tab.update_rstb_button_state()
        if key in ("actorinfo_path", "gameactorinfo_path", "rstbl_path"):
            self.update_editor_buttons_state()

    def validate_path(self, edit, context):
        path = edit.text().strip()
        if not path:
            edit.setStyleSheet("")
            return
        if context == "source":
            valid = os.path.isdir(path) and os.path.isdir(os.path.join(path, "Pack", "Actor"))
        elif context == "dest":
            valid = os.path.isdir(path)
        elif context in ("actorinfo", "gameactorinfo", "rstbl"):
            valid = os.path.isfile(path) and path.lower().endswith(".byml.zs")
        elif context == "rstb":
            valid = os.path.isfile(path) and path.lower().endswith(".exe")
        else:
            valid = False
        color = "#2e7d32" if valid else "#b71c1c"
        edit.setStyleSheet(f"QLineEdit {{ background-color: {color}; }}")

    def validate_all(self):
        self.validate_path(self.src_edit, "source")
        self.validate_path(self.dst_edit, "dest")
        self.validate_path(self.actorinfo_edit, "actorinfo")
        self.validate_path(self.gameactorinfo_edit, "gameactorinfo")
        self.validate_path(self.rstb_edit, "rstb")
        self.validate_path(self.rstbl_edit, "rstbl")
        self.update_editor_buttons_state()

    def on_language_changed(self, idx):
        lang = "de" if self.lang_combo.currentText() == "Deutsch" else "en"
        self.settings["language"] = lang
        save_settings(self.settings)
        self.main.apply_theme_and_language()

    def on_theme_changed(self, idx):
        if idx < 0:
            return
        theme = "dark" if idx == 0 else "light"
        self.settings["theme"] = theme
        save_settings(self.settings)
        self.main.apply_theme_and_language()

    def on_font_changed(self, family):
        self.settings["font_family"] = family
        save_settings(self.settings)
        self.main.apply_font()

    def on_font_size_changed(self, size):
        self.settings["font_size"] = size
        save_settings(self.settings)
        self.main.apply_font()

    def on_high_dpi_toggled(self, checked):
        self.settings["high_dpi"] = checked
        save_settings(self.settings)
        QMessageBox.information(self, self.tr("info_dialog_title"),
                                self.texts.get("high_dpi_restart", "High DPI wird nach einem Neustart wirksam."))

    def on_batch_toggled(self, checked):
        self.settings["batch_mode"] = checked
        save_settings(self.settings)
        self.main.clone_tab.update_batch_mode()

    def on_log_toggled(self, checked):
        self.settings["enable_clone_log"] = checked
        save_settings(self.settings)
        self.main.clone_tab.undo_btn.setVisible(checked)

    def refresh_texts(self, texts):
        self.texts = texts

        self.theme_combo.blockSignals(True)
        self.theme_combo.clear()
        self.theme_combo.addItems([texts.get("dark", "Dark"), texts.get("light", "Light")])
        self.theme_combo.setCurrentText(texts.get(self.settings["theme"], "Dark"))
        self.theme_combo.blockSignals(False)

        self.batch_cb.setText(texts.get("batch_mode", "Batch Mode"))
        self.log_cb.setText(texts.get("enable_clone_log", "Enable Clone Log"))
        self.high_dpi_cb.setText(texts.get("high_dpi", "High DPI (restart required)"))
        self.high_dpi_cb.setToolTip(texts.get("high_dpi_tooltip", ""))
        self.update_editor_buttons_state()


#Main Window
class MainWindow(QMainWindow):
    """Das Hauptfenster der Anwendung mit Tabs, Konsole und Statusleiste."""
    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.lang = self.settings["language"]
        self.texts = load_texts(self.lang)

        self.setWindowTitle(self.texts.get("title", "Mario Wonder Combi Tool"))
        geo = self.settings["window_geometry"]
        if "x" in geo:
            w, h = map(int, geo.split("x"))
        else:
            w, h = 1100, 750
        self.resize(w, h)

        self.setAcceptDrops(True)

        # Haupt-Splitter: Tabs oben, Konsole unten
        self.main_splitter = QSplitter(Qt.Vertical)
        self.setCentralWidget(self.main_splitter)

        self.tabs = QTabWidget()
        self.tabs.setMovable(True)
        self.tabs.tabBar().tabMoved.connect(self.on_tab_moved)
        self.main_splitter.addWidget(self.tabs)

        # Konsolenbereich
        console_container = QWidget()
        console_layout = QVBoxLayout(console_container)
        console_layout.setContentsMargins(0,0,0,0)
        self.console = QTextEdit()
        self.console.setReadOnly(True)
        self.console.setPlaceholderText(self.texts.get("console_placeholder", "Log..."))
        console_layout.addWidget(self.console)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        console_layout.addWidget(self.progress_bar)

        self.main_splitter.addWidget(console_container)

        if "main_splitter" in self.settings:
            self.main_splitter.setSizes(self.settings["main_splitter"])
        self.main_splitter.splitterMoved.connect(self.save_main_splitter)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage(self.texts.get("status_ready", "Bereit"))

        # Tabs initialisieren
        self.editor_tab = PackEditorWidget(self.texts)
        self.clone_tab = CloneTab(self.settings, self.texts, self.log, self.set_progress, self.editor_tab, self)
        self.settings_tab = SettingsTab(self.settings, self.texts, self)

        self.tab_widgets = {
            "editor": self.editor_tab,
            "clone": self.clone_tab,
            "settings": self.settings_tab
        }
        self.tab_names = {
            "editor": self.texts.get("tab_editor", "Pack Editor"),
            "clone": self.texts.get("tab_clone", "Clone Actor"),
            "settings": self.texts.get("tab_settings", "Settings")
        }
        order = self.settings.get("tab_order", ["clone", "editor", "settings"])
        for key in order:
            if key in self.tab_widgets:
                self.tabs.addTab(self.tab_widgets[key], self.tab_names[key])

        self.apply_theme()
        self.apply_font()

        # Letzte Sitzung: Editor-Tabs wiederherstellen
        for p in self.settings.get("open_editor_tabs", []):
            self.editor_tab.load_file(p)

        # Shortcuts
        QShortcut(QKeySequence("Ctrl+O"), self, self.editor_tab.open_file)
        QShortcut(QKeySequence("Ctrl+S"), self, self.save_current_editor)
        QShortcut(QKeySequence("Ctrl+Shift+S"), self, self.save_current_editor_as)

        # Fenstergröße speichern
        self.resize_timer = QTimer()
        self.resize_timer.setSingleShot(True)
        self.resize_timer.timeout.connect(self.save_window_geometry)
        self.resizing = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resizing = True
        self.resize_timer.start(500)

    def save_window_geometry(self):
        if self.resizing:
            w = self.width()
            h = self.height()
            self.settings["window_geometry"] = f"{w}x{h}"
            save_settings(self.settings)
            self.resizing = False

    def log(self, msg):
        self.console.append(msg)

    def set_progress(self, value):
        self.progress_bar.setValue(value)

    def save_current_editor(self):
        current_widget = self.editor_tab.inner_tabs.currentWidget()
        if isinstance(current_widget, SingleFileEditor):
            current_widget.save()

    def save_current_editor_as(self):
        current_widget = self.editor_tab.inner_tabs.currentWidget()
        if isinstance(current_widget, SingleFileEditor):
            current_widget.save_as()

    def save_main_splitter(self):
        self.settings["main_splitter"] = self.main_splitter.sizes()
        save_settings(self.settings)

    def open_path_in_editor(self, path):
        if os.path.isfile(path):
            self.editor_tab.load_file(path)
            self.tabs.setCurrentWidget(self.editor_tab)

    def apply_font(self):
        family = self.settings["font_family"]
        size = self.settings["font_size"]
        app_font = QFont(family, size)
        QApplication.setFont(app_font)
        for widget in QApplication.allWidgets():
            if not widget.objectName() == "monoEditor":
                widget.setFont(app_font)
        for widget in QApplication.allWidgets():
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    def apply_theme(self):
        if self.settings["theme"] == "dark":
            self.setStyleSheet("""
                QWidget { background-color: #2e2e2e; color: white; }
                QLineEdit, QTextEdit, QPlainTextEdit, QListWidget, QTreeView, QComboBox {
                    background-color: #3c3c3c; color: white; border: 1px solid #5a5a5a;
                }
                QGroupBox { color: white; border: 1px solid #5a5a5a; margin-top: 1em; }
                QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px; }
                QPushButton { background-color: #424242; color: white; border: 1px solid #5a5a5a; padding: 5px; }
                QPushButton:hover { background-color: #505050; }
                QPushButton:disabled { background-color: #555; color: #aaa; }
                QHeaderView::section { background-color: #3c3c3c; color: white; border: 1px solid #5a5a5a; padding: 4px; }
                QCheckBox { color: white; }
                QListWidget::item:selected { background-color: #388e3c; color: white; }
                QTabWidget::pane { border: 1px solid #5a5a5a; }
                QTabBar::tab { background-color: #3c3c3c; color: white; padding: 6px; }
                QTabBar::tab:selected { background-color: #505050; }
                QToolTip { background-color: #424242; color: white; border: 1px solid #5a5a5a; }
            """)
        else:
            self.setStyleSheet("")

    def apply_theme_and_language(self):
        self.lang = self.settings["language"]
        self.texts = load_texts(self.lang)
        self.setWindowTitle(self.texts.get("title", "Mario Wonder Combi Tool"))

        self.tab_names = {
            "editor": self.texts.get("tab_editor", "Pack Editor"),
            "clone": self.texts.get("tab_clone", "Clone Actor"),
            "settings": self.texts.get("tab_settings", "Settings")
        }
        for i in range(self.tabs.count()):
            widget = self.tabs.widget(i)
            for key, w in self.tab_widgets.items():
                if widget is w:
                    self.tabs.setTabText(i, self.tab_names[key])
                    break

        self.console.setPlaceholderText(self.texts.get("console_placeholder", "Log..."))
        self.editor_tab.refresh_texts(self.texts)
        self.clone_tab.refresh_texts(self.texts)
        self.settings_tab.refresh_texts(self.texts)

        self.apply_theme()
        self.apply_font()

    def on_tab_moved(self, from_idx, to_idx):
        order = []
        for i in range(self.tabs.count()):
            widget = self.tabs.widget(i)
            for key, w in self.tab_widgets.items():
                if widget is w:
                    order.append(key)
                    break
        self.settings["tab_order"] = order
        save_settings(self.settings)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if os.path.isfile(path):
                self.editor_tab.load_file(path)
                self.tabs.setCurrentWidget(self.editor_tab)

    def closeEvent(self, event):
        # Prüfen auf ungespeicherte Änderungen
        for editor in self.editor_tab.open_docs.values():
            if hasattr(editor, 'modified') and editor.modified:
                res = QMessageBox.question(self, self.tr("unsaved_changes_title"),
                                           self.tr("unsaved_changes_text"),
                                           QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
                if res == QMessageBox.Cancel:
                    event.ignore()
                    return
                elif res == QMessageBox.Save:
                    for ed in self.editor_tab.open_docs.values():
                        if ed.modified:
                            ed.save()
                break

        open_paths = self.editor_tab.get_open_tabs()
        self.settings["open_editor_tabs"] = open_paths
        save_settings(self.settings)
        event.accept()


if __name__ == "__main__":
    settings = load_settings()

    if settings.get("high_dpi", False):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)

    app = QApplication(sys.argv)

    window = MainWindow(settings)
    window.show()
    sys.exit(app.exec_())

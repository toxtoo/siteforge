#!/usr/bin/env python3

import zipfile
from datetime import datetime
import json
import os
import html
import subprocess
import sys
import re
import shlex
import shutil
import stat
import tempfile
from pathlib import Path
from urllib.parse import quote, unquote

# SiteForge configuration paths
CONFIG_DIR = Path.home() / ".config" / "SiteForge"
STATE_FILE = CONFIG_DIR / "state.json"
SHORTCUTS_FILE = CONFIG_DIR / "shortcuts.json"
BROWSERS_FILE = CONFIG_DIR / "browsers.json"
EXTERNAL_TOOLS_FILE = CONFIG_DIR / "external_tools.json"
USER_DICTIONARY_FILE = CONFIG_DIR / "user_dictionary.txt"
TEMPLATES_DIR = CONFIG_DIR / "templates"
SNIPPETS_DIR = CONFIG_DIR / "snippets"


from html.parser import HTMLParser
from PyQt6.QtCore import Qt, QUrl, QDir, QSize, QTimer
from PyQt6.QtGui import QDesktopServices, QTextCursor, QShortcut, QAction, QFont, QFileSystemModel, QColor, QIcon, QKeySequence, QPixmap, QPalette
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QFileDialog,
    QMessageBox, QTreeView, QDockWidget,
    QToolBar, QStatusBar, QDialog, QVBoxLayout, QTableWidget,
    QTableWidgetItem, QHeaderView, QDialogButtonBox,
    QPushButton, QSplitter, QWidget, QLabel, QHBoxLayout,
    QToolButton, QColorDialog, QFontDialog, QInputDialog,
    QLineEdit, QKeySequenceEdit, QTreeWidget, QTreeWidgetItem,
    QFileIconProvider, QStyledItemDelegate, QListWidget, QListWidgetItem,
    QTextEdit, QStyleOptionViewItem, QRadioButton, QComboBox, QFormLayout,
    QSpinBox, QMenu
)
from PyQt6.Qsci import (
    QsciScintilla, QsciLexerHTML,
    QsciLexerCSS, QsciLexerJavaScript
)


def apply_saved_font(font, saved):
    """Applies a font description saved by QFont.toString().

    A description saved by a newer Qt can have extra fields that an older
    Qt rejects with 'QFont::fromString: Invalid description'. Only the
    first 16 fields matter, so trim to those, and if that still fails
    fall back to just the font family and size."""
    if not saved or not isinstance(saved, str):
        return font

    parts = saved.split(",")

    # Try the description trimmed to the 16 fields every Qt understands
    if font.fromString(",".join(parts[:16])):
        return font

    # Still rejected: keep just the family and size
    try:
        font.setFamily(parts[0])
        font.setPointSizeF(float(parts[1]))
    except (IndexError, ValueError):
        pass

    return font


# Spell checking ---------------------------------------------------------

# Parts of an HTML file that are not text a reader sees: comments, scripts,
# styles, tags (with their attributes) and character entities.
SPELL_MASK = re.compile(
    r"<!--.*?-->|<script\b.*?</script\s*>|<style\b.*?</style\s*>"
    r"|<[^>]*>|&#?\w+;",
    re.DOTALL | re.IGNORECASE
)

# A word: letters only, with apostrophes allowed inside (don't, Stuart's)
SPELL_WORD = re.compile(r"(?<!\w)[^\W\d_]+(?:['\u2019][^\W\d_]+)*(?!\w)")

SPELL_INSTALL_HELP = (
    "Spell checking needs the program aspell or hunspell, with a "
    "dictionary for the language '{language}'.\n\n"
    "In a terminal, install them with:\n"
    "    sudo apt install aspell aspell-en\n"
    "or:\n"
    "    sudo apt install hunspell hunspell-en-gb\n\n"
    "Details: {details}"
)


# Ways of marking a misspelled word: name -> Scintilla indicator style number
SPELL_STYLES = {
    "Thick straight underline": 14,   # INDIC_COMPOSITIONTHICK
    "Wavy line (thin)": 1,            # INDIC_SQUIGGLE
    "Pale red box": 8,                # INDIC_STRAIGHTBOX
    "Red text": 17,                   # INDIC_TEXTFORE
}
DEFAULT_SPELL_STYLE = "Thick straight underline"


class SpellChecker:
    """Spell checking through the aspell or hunspell command-line programs.

    One shared copy (SPELLER) is used by every editor tab."""

    BACKENDS = ("aspell", "hunspell")

    def __init__(self):
        self.enabled = True
        self.language = "en_GB"
        self.style = DEFAULT_SPELL_STYLE
        self.cache = {}            # word -> True if the dictionary rejects it
        self.ignored = set()       # words ignored for this session (lower case)
        self.user_words = set()    # personal dictionary (lower case)
        self.working_backend = None
        self.failed = False
        self.on_error = None
        self.load_settings()
        self.load_user_words()

    def load_settings(self):
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return

        self.enabled = bool(data.get("spell_check_enabled", True))
        language = str(data.get("spell_language", "en_GB")).strip()

        if re.fullmatch(r"[A-Za-z_]{2,20}", language):
            self.language = language

        style = data.get("spell_style", DEFAULT_SPELL_STYLE)
        self.style = style if style in SPELL_STYLES else DEFAULT_SPELL_STYLE

    def save_settings(self):
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}

        data["spell_check_enabled"] = self.enabled
        data["spell_language"] = self.language
        data["spell_style"] = self.style

        try:
            STATE_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
        except OSError:
            pass

    def load_user_words(self):
        try:
            lines = USER_DICTIONARY_FILE.read_text(encoding="utf-8").splitlines()
        except OSError:
            return

        self.user_words = {line.strip().lower() for line in lines if line.strip()}

    def set_language(self, language):
        self.language = language
        self.cache.clear()
        self.failed = False
        self.working_backend = None
        self.save_settings()

    def add_word(self, word):
        word = word.strip()
        if not word:
            return

        self.user_words.add(word.lower())

        try:
            USER_DICTIONARY_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(USER_DICTIONARY_FILE, "a", encoding="utf-8") as file:
                file.write(word + "\n")
        except OSError:
            pass

    def ignore_word(self, word):
        self.ignored.add(word.lower())

    def backends_to_try(self):
        found = [name for name in self.BACKENDS if shutil.which(name)]

        if self.working_backend in found:
            found.remove(self.working_backend)
            found.insert(0, self.working_backend)

        return found

    def available(self):
        return bool(self.backends_to_try())

    def command(self, backend, mode):
        if backend == "aspell":
            base = ["aspell", "--lang=" + self.language, "--encoding=utf-8"]
            return base + (["list"] if mode == "list" else ["-a"])

        base = ["hunspell", "-d", self.language, "-i", "utf-8"]
        return base + (["-l"] if mode == "list" else ["-a"])

    def run(self, mode, text):
        """Runs the checker. Returns its output, or None if it failed."""
        details = "no spell checker program found"

        for backend in self.backends_to_try():
            try:
                result = subprocess.run(
                    self.command(backend, mode),
                    input=text,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=20
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                details = f"{backend}: {error}"
                continue

            if result.returncode == 0:
                self.working_backend = backend
                return result.stdout

            details = f"{backend}: " + (
                (result.stderr or result.stdout).strip() or "failed"
            )

        self.failed = True

        if self.on_error:
            self.on_error(
                SPELL_INSTALL_HELP.format(
                    language=self.language, details=details
                )
            )

        return None

    def misspelled(self, words):
        """Given a set of words, returns the ones that are misspelled."""
        if self.failed:
            return set()

        unknown = [word for word in words if word not in self.cache]

        if unknown:
            output = self.run("list", "\n".join(unknown) + "\n")

            if output is None:
                return set()

            rejected = {
                line.strip() for line in output.splitlines() if line.strip()
            }

            for word in unknown:
                self.cache[word] = word in rejected

        return {
            word for word in words
            if self.cache.get(word)
            and word.lower() not in self.user_words
            and word.lower() not in self.ignored
        }

    def suggest(self, word):
        """Returns up to 8 suggested spellings for a word."""
        output = self.run("suggest", "^" + word + "\n")

        if not output:
            return []

        for line in output.splitlines():
            if line.startswith("&"):
                _, _, rest = line.partition(":")
                return [item.strip() for item in rest.split(",") if item.strip()][:8]

        return []


SPELLER = SpellChecker()


class ImageSourceParser(HTMLParser):

    def __init__(self):
        super().__init__()
        self.sources = []
        self.stylesheets = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        attributes = {
            name.lower(): value
            for name, value in attrs
        }

        if tag.lower() == "img":
            source = attributes.get("src")
            if source:
                self.sources.append(source.strip())

        elif tag.lower() == "link":
            rel = attributes.get("rel", "")
            href = attributes.get("href")

            if href and "stylesheet" in rel.lower().split():
                self.stylesheets.append(href.strip())

        elif tag.lower() == "script":
            source = attributes.get("src")
            if source:
                self.scripts.append(source.strip())


class CodeEditor(QsciScintilla):

    def __init__(self, path=None):
        super().__init__()
        self.setAcceptDrops(True)

        self.path = Path(path).resolve() if path else None
        self.setUtf8(True)

        editor_font = QFont("Monospace", 10)
        settings = STATE_FILE

        try:
            data = json.loads(settings.read_text())
            saved_font = data.get("editor_font")
            apply_saved_font(editor_font, saved_font)
        except (OSError, ValueError):
            pass

        self.setFont(editor_font)

        # Line numbers / Bookmark margin
        self.setMarginType(1, QsciScintilla.MarginType.SymbolMargin)
        self.setMarginWidth(1, 12)
        self.setMarginSensitivity(1, True)

        self.bookmark_marker = self.markerDefine(
            QsciScintilla.MarkerSymbol.Circle
        )

        bookmark_pixmap = QPixmap(
            str(Path(__file__).resolve().parent / "icons" / "bookmark-triangle.png")
        )

        self.SendScintilla(
            QsciScintilla.SCI_MARKERDEFINEPIXMAP,
            self.bookmark_marker,
            bookmark_pixmap
        )
        
        try:
            data = json.loads(settings.read_text())
            saved_color = data.get("bookmark_color", "#ff0000")
        except (OSError, ValueError):
            saved_color = "#ff0000"

        self.bookmark_color = QColor(saved_color)
        self.setMarkerBackgroundColor(
            self.bookmark_color, self.bookmark_marker
        )

        self.setMarginMarkerMask(1, 1 << self.bookmark_marker)
        self.marginClicked.connect(self.toggle_bookmark)
        self.setMarginType(0, QsciScintilla.MarginType.NumberMargin)
        self.setMarginWidth(0, "0000")

        # Indentation
        try:
            indent_data = json.loads(STATE_FILE.read_text())
        except (OSError, ValueError):
            indent_data = {}

        use_tabs = bool(indent_data.get("indent_use_tabs", False))
        indent_width = indent_data.get("indent_width", 4)

        try:
            indent_width = int(indent_width)
        except (TypeError, ValueError):
            indent_width = 4

        indent_width = max(1, min(8, indent_width))

        self.setIndentationsUseTabs(use_tabs)
        self.setTabWidth(indent_width)
        self.setIndentationWidth(indent_width)
        self.setAutoIndent(True)

        # Highlight the current line
        self.setCaretLineVisible(True)

        try:
            line_data = json.loads(STATE_FILE.read_text())
            line_color_name = line_data.get("current_line_color", "#f7f7c6")
        except (OSError, ValueError):
            line_color_name = "#f7f7c6"

        line_color = QColor(line_color_name)
        if not line_color.isValid():
            line_color = QColor("#f7f7c6")

        self.current_line_color = line_color
        self.setCaretLineBackgroundColor(self.current_line_color)

        self.set_language()
        
        # Tag matching configuration & indicator setup
        self.tag_indicator = self.indicatorDefine(
            QsciScintilla.IndicatorStyle.RoundBoxIndicator
        )

        try:
            tag_data = json.loads(STATE_FILE.read_text())
            tag_color_name = tag_data.get("tag_pair_color", "#308cc6")
        except (OSError, ValueError):
            tag_color_name = "#308cc6"

        tag_color = QColor(tag_color_name)
        if not tag_color.isValid():
            tag_color = QColor("#308cc6")

        self.tag_pair_color = tag_color
        self.setIndicatorForegroundColor(
            self.tag_pair_color, self.tag_indicator
        )
        self.setIndicatorDrawUnder(True, self.tag_indicator)
        
        # Feed the swapped hex code so Scintilla flips it back to blue
        sel_bg = QColor("#c68c30")
        sel_fg = QColor("#ffffff")
        self.setSelectionForegroundColor(sel_fg)
        self.setSelectionBackgroundColor(sel_bg)
        self.SendScintilla(QsciScintilla.SCI_SETSELFORE, True, sel_fg.rgb())
        self.SendScintilla(QsciScintilla.SCI_SETSELBACK, True, sel_bg.rgb())

        self.cursorPositionChanged.connect(self.update_tag_matching)

        # Tag completion setting
        try:
            cfg = json.loads(STATE_FILE.read_text())
            self.tag_completion_enabled = bool(cfg.get("tag_completion_enabled", True))
        except (OSError, ValueError):
            self.tag_completion_enabled = True

        self.setAutoCompletionSource(QsciScintilla.AutoCompletionSource.AcsAll)
        self.setAutoCompletionThreshold(1)
        self.setAutoCompletionCaseSensitivity(False)
        self.setAutoCompletionReplaceWord(False)

        # Spell checking: red squiggle under misspelled words
        self.spell_ranges = []
        self.spell_indicator = self.indicatorDefine(
            QsciScintilla.IndicatorStyle.SquiggleIndicator
        )
        self.apply_spell_style()

        self.spell_timer = QTimer(self)
        self.spell_timer.setSingleShot(True)
        self.spell_timer.setInterval(700)
        self.spell_timer.timeout.connect(self.run_spell_check)
        self.textChanged.connect(lambda: self.spell_timer.start())

        if self.path:
            self.load_file(self.path)

    def has_active_selection(self):
        start = self.SendScintilla(QsciScintilla.SCI_GETSELECTIONSTART)
        end = self.SendScintilla(QsciScintilla.SCI_GETSELECTIONEND)
        return start != end

    def clear_tag_highlights(self):
        encoded_text = self.text().encode("utf-8")
        self.SendScintilla(QsciScintilla.SCI_SETINDICATORCURRENT, self.tag_indicator)
        self.SendScintilla(QsciScintilla.SCI_INDICATORCLEARRANGE, 0, len(encoded_text))

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if self.has_active_selection():
            self.clear_tag_highlights()

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        if self.has_active_selection():
            self.clear_tag_highlights()

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if self.has_active_selection():
            self.clear_tag_highlights()

    def keyPressEvent(self, event):
        super().keyPressEvent(event)
        
        if self.has_active_selection():
            self.clear_tag_highlights()

        if not getattr(self, "tag_completion_enabled", True):
            return

        if event.text() == '>':
            line, index = self.getCursorPosition()

            # Look back over the current line and up to 20 lines before it
            first_line = max(0, line - 20)
            text_up_to_pos = "".join(
                self.text(n) for n in range(first_line, line)
            ) + self.text(line)[:index]

            match = re.search(
                r'<([A-Za-z][\w:.-]*)(?:\s[^<>]*)?>$',
                text_up_to_pos
            )
            if match and not text_up_to_pos.endswith("/>"):
                tag_name = match.group(1).lower()
                void_tags = {
                    "area", "base", "br", "col", "embed", "hr", "img",
                    "input", "link", "meta", "param", "source", "track", "wbr"
                }
                if (
                    tag_name not in void_tags
                    and not self.tag_already_closed(tag_name)
                ):
                    self.insertAt(f"</{tag_name}>", line, index)
                    self.setCursorPosition(line, index)

    def tag_already_closed(self, tag_name):
        """True if the page already has as many closing </tag> as opening
        <tag>, so a tag being re-typed does not need another closing tag.

        This is what stops a closing tag being added again when you delete
        the > of an opening tag and type it back."""
        cleaned = re.sub(
            r"<!--.*?-->|<script\b.*?</script\s*>|<style\b.*?</style\s*>",
            "",
            self.text(),
            flags=re.DOTALL | re.IGNORECASE
        )

        name = re.escape(tag_name)

        # Opening tags, not counting self-closing ones such as <div/>
        opening = len(re.findall(
            r"<\s*" + name + r"(?=[\s>/])[^>]*(?<!/)>",
            cleaned,
            flags=re.IGNORECASE
        ))

        closing = len(re.findall(
            r"<\s*/\s*" + name + r"\s*>",
            cleaned,
            flags=re.IGNORECASE
        ))

        return closing >= opening

    def apply_spell_style(self):
        """Sets how misspelled words are marked (see SPELL_STYLES)."""
        style = SPELL_STYLES.get(SPELLER.style, SPELL_STYLES[DEFAULT_SPELL_STYLE])

        self.SendScintilla(
            QsciScintilla.SCI_INDICSETSTYLE, self.spell_indicator, style
        )
        self.setIndicatorForegroundColor(
            QColor("#e00000"), self.spell_indicator
        )

        # Transparency is only used by the pale box style
        self.SendScintilla(
            QsciScintilla.SCI_INDICSETALPHA, self.spell_indicator, 60
        )
        self.SendScintilla(
            QsciScintilla.SCI_INDICSETOUTLINEALPHA, self.spell_indicator, 140
        )

    def spell_applicable(self):
        """Spell check page text, not code files."""
        if not self.path:
            return True

        return self.path.suffix.lower() in (
            ".html", ".htm", ".xhtml", ".txt", ".md"
        )

    def run_spell_check(self):
        """Underlines misspelled words in the text of the page."""
        self.spell_ranges = []

        self.SendScintilla(
            QsciScintilla.SCI_SETINDICATORCURRENT, self.spell_indicator
        )
        self.SendScintilla(
            QsciScintilla.SCI_INDICATORCLEARRANGE,
            0,
            self.SendScintilla(QsciScintilla.SCI_GETLENGTH)
        )

        if not SPELLER.enabled or not self.spell_applicable():
            return

        if not SPELLER.available():
            return

        text = self.text()
        masked = SPELL_MASK.sub(lambda match: " " * len(match.group(0)), text)

        found = []
        for match in SPELL_WORD.finditer(masked):
            word = match.group(0).replace("\u2019", "'")

            # Skip single letters and ACRONYMS
            if len(word) < 2 or word.isupper():
                continue

            found.append((match.start(), match.end(), word))

        if not found:
            return

        bad = SPELLER.misspelled({word for _, _, word in found})

        if not bad:
            return

        ranges = []
        last_char = 0
        byte_position = 0

        for start, end, word in found:
            if word not in bad:
                continue

            byte_position += len(text[last_char:start].encode("utf-8"))
            length = len(text[start:end].encode("utf-8"))
            ranges.append((byte_position, byte_position + length, word))
            byte_position += length
            last_char = end

            if len(ranges) >= 2000:
                break

        self.SendScintilla(
            QsciScintilla.SCI_SETINDICATORCURRENT, self.spell_indicator
        )

        for start, end, word in ranges:
            self.SendScintilla(
                QsciScintilla.SCI_INDICATORFILLRANGE, start, end - start
            )

        self.spell_ranges = ranges

    def replace_byte_range(self, start, end, new_text):
        start_line, start_index = self.lineIndexFromPosition(start)
        end_line, end_index = self.lineIndexFromPosition(end)
        self.setSelection(start_line, start_index, end_line, end_index)
        self.replaceSelectedText(new_text)

    def recheck_spelling_everywhere(self):
        window = self.window()

        if hasattr(window, "recheck_all_spelling"):
            window.recheck_all_spelling()
        else:
            self.run_spell_check()

    def contextMenuEvent(self, event):
        # Build the whole right-click menu here rather than relying on
        # QScintilla's own menu, so the spelling items are always included
        menu = QMenu(self)
        menu.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        position = self.SendScintilla(
            QsciScintilla.SCI_POSITIONFROMPOINT,
            event.pos().x(),
            event.pos().y()
        )

        hit = None

        if position >= 0 and SPELLER.enabled and self.spell_applicable():
            self.run_spell_check()

            for start, end, word in self.spell_ranges:
                if start <= position <= end:
                    hit = (start, end, word)
                    break

        if hit:
            start, end, word = hit
            suggestions = SPELLER.suggest(word)

            if suggestions:
                for suggestion in suggestions:
                    action = menu.addAction(suggestion)
                    bold = action.font()
                    bold.setBold(True)
                    action.setFont(bold)
                    action.triggered.connect(
                        lambda checked=False, s=suggestion, a=start, b=end:
                            self.replace_byte_range(a, b, s)
                    )
            else:
                action = menu.addAction("(No suggestions)")
                action.setEnabled(False)

            menu.addSeparator()

            ignore_action = menu.addAction(f'Ignore "{word}"')
            ignore_action.triggered.connect(
                lambda checked=False, w=word: (
                    SPELLER.ignore_word(w),
                    self.recheck_spelling_everywhere()
                )
            )

            add_action = menu.addAction(f'Add "{word}" to dictionary')
            add_action.triggered.connect(
                lambda checked=False, w=word: (
                    SPELLER.add_word(w),
                    self.recheck_spelling_everywhere()
                )
            )

            menu.addSeparator()

        # The usual editing items
        undo_action = menu.addAction("Undo")
        undo_action.setEnabled(self.isUndoAvailable())
        undo_action.triggered.connect(lambda checked=False: self.undo())

        redo_action = menu.addAction("Redo")
        redo_action.setEnabled(self.isRedoAvailable())
        redo_action.triggered.connect(lambda checked=False: self.redo())

        menu.addSeparator()

        cut_action = menu.addAction("Cut")
        cut_action.setEnabled(self.hasSelectedText())
        cut_action.triggered.connect(lambda checked=False: self.cut())

        copy_action = menu.addAction("Copy")
        copy_action.setEnabled(self.hasSelectedText())
        copy_action.triggered.connect(lambda checked=False: self.copy())

        paste_action = menu.addAction("Paste")
        paste_action.triggered.connect(lambda checked=False: self.paste())

        menu.addSeparator()

        select_all_action = menu.addAction("Select All")
        select_all_action.triggered.connect(
            lambda checked=False: self.selectAll()
        )

        menu.popup(event.globalPos())

    def toggle_bookmark(self, margin, line, modifiers): 
        if margin != 1:
            return

        markers = self.markerFindNext(line, 1 << self.bookmark_marker)

        if markers == line:
            self.markerDelete(line, self.bookmark_marker)
        else:
            self.markerAdd(line, self.bookmark_marker)

    def set_bookmark_color(self, color):
        self.bookmark_color = QColor(color)
        self.setMarkerBackgroundColor(
            self.bookmark_color, self.bookmark_marker
        )

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile() and Path(url.toLocalFile()).is_file():
                    event.acceptProposedAction()
                    return

        event.ignore()

    def dropEvent(self, event):
        image_extensions = {
            ".jpg", ".jpeg", ".png", ".gif",
            ".webp", ".svg", ".avif", ".bmp"
        }

        dropped_path = None

        for url in event.mimeData().urls():
            if url.isLocalFile():
                candidate = Path(url.toLocalFile())
                if candidate.is_file():
                    dropped_path = candidate.resolve()
                    break

        if dropped_path is None:
            event.ignore()
            return

        if not self.path or not self.path.exists():
            QMessageBox.warning(
                self,
                "Save file first",
                "Please save this file before inserting a reference.\n\n"
                "The relative path depends on the file's location."
            )
            event.ignore()
            return

        editor_extension = self.path.suffix.lower()

        if editor_extension not in (".html", ".htm", ".xhtml", ".css"):
            event.ignore()
            return

        relative_path = os.path.relpath(dropped_path, self.path.parent)
        relative_path = Path(relative_path).as_posix()

        if editor_extension == ".css":
            relative_path = relative_path.replace("\\", "\\\\").replace('"', '\\"')

            if dropped_path.suffix.lower() == ".css":
                tag = f'@import url("{relative_path}");'
            else:
                tag = f'url("{relative_path}")'

        elif dropped_path.suffix.lower() in image_extensions:
            image_src = html.escape(relative_path, quote=True)
            pixmap = QPixmap(str(dropped_path))

            if pixmap.isNull():
                tag = f'<img src="{image_src}">'
            else:
                width = pixmap.width()
                height = pixmap.height()
                tag = (
                    f'<img src="{image_src}" '
                    f'width="{width}" height="{height}" alt="">'
                )

        else:
            link_href = html.escape(relative_path, quote=True)
            link_text = html.escape(dropped_path.name)
            tag = f'<a href="{link_href}">{link_text}</a>'

        point = event.position().toPoint()
        position = self.SendScintilla(
            QsciScintilla.SCI_POSITIONFROMPOINT,
            point.x(),
            point.y()
        )

        if position < 0:
            position = self.SendScintilla(QsciScintilla.SCI_GETCURRENTPOS)

        line, index = self.lineIndexFromPosition(position)
        self.insertAt(tag, line, index)
        event.acceptProposedAction()

    def set_language(self):
        suffix = self.path.suffix.lower() if self.path else ".html"

        if suffix in (".html", ".htm", ".xhtml", ".xml", ".svg"):
            lexer = QsciLexerHTML(self)
        elif suffix == ".css":
            lexer = QsciLexerCSS(self)
        elif suffix in (".js", ".mjs"):
            lexer = QsciLexerJavaScript(self)
        else:
            self.setLexer(None)
            return

        try:
            data = json.loads(STATE_FILE.read_text())
            saved_font = data.get("editor_font")
        except (OSError, ValueError):
            saved_font = None

        font = QFont("Monospace", 10)
        apply_saved_font(font, saved_font)

        lexer.setFont(font, -1)
        self.setLexer(lexer)
        
        lexer.setFont(font, -1)
        self.setLexer(lexer)

        # Keep selection nice and blue
        self.setSelectionForegroundColor(QColor("#ffffff"))
        self.setSelectionBackgroundColor(QColor("#308cc6"))

    def update_tag_matching(self, line, index):
        if self.has_active_selection():
            self.clear_tag_highlights()
            return

        text = self.text()
        encoded_text = text.encode("utf-8")

        cursor_byte = self.SendScintilla(QsciScintilla.SCI_GETCURRENTPOS)

        self.clear_tag_highlights()

        pattern = re.compile(
            r"<!--.*?-->|"
            r"<\s*(/?)\s*([A-Za-z][\w:.-]*)\b[^>]*>",
            re.DOTALL,
        )

        void_tags = {
            "area", "base", "br", "col", "embed", "hr", "img",
            "input", "link", "meta", "param", "source", "track",
            "wbr",
        }

        tags = []

        for match in pattern.finditer(text):
            token = match.group(0)

            if token.startswith("<!--"):
                continue

            name = match.group(2).lower()
            closing = bool(match.group(1))
            self_closing = (
                token.rstrip().endswith("/>")
                or name in void_tags
            )

            tags.append({
                "start_byte": len(text[:match.start()].encode("utf-8")),
                "end_byte": len(text[:match.end()].encode("utf-8")),
                "line": text.count("\n", 0, match.start()) + 1,
                "name": name,
                "closing": closing,
                "self_closing": self_closing,
            })

        current = None

        for i, tag in enumerate(tags):
            if tag["start_byte"] <= cursor_byte <= tag["end_byte"]:
                current = i
                break

        if current is None:
            return

        tag = tags[current]
        name = tag["name"]

        if tag["self_closing"]:
            return

        partner = None
        depth = 0

        if tag["closing"]:
            for i in range(current - 1, -1, -1):
                candidate = tags[i]
                if candidate["name"] != name or candidate["self_closing"]:
                    continue
                if candidate["closing"]:
                    depth += 1
                elif depth:
                    depth -= 1
                else:
                    partner = i
                    break
        else:
            for i in range(current + 1, len(tags)):
                candidate = tags[i]
                if candidate["name"] != name or candidate["self_closing"]:
                    continue
                if not candidate["closing"]:
                    depth += 1
                elif depth:
                    depth -= 1
                else:
                    partner = i
                    break

        if partner is None:
            return

        self.fill_tag(tag)
        self.fill_tag(tags[partner])

    def fill_tag(self, tag):
        if self.has_active_selection():
            return

        self.SendScintilla(
            QsciScintilla.SCI_SETINDICATORCURRENT,
            self.tag_indicator,
        )
        self.SendScintilla(
            QsciScintilla.SCI_INDICATORFILLRANGE,
            tag["start_byte"],
            tag["end_byte"] - tag["start_byte"],
        )

    def load_file(self, path):
        self.path = Path(path).resolve()
        self.set_language()

        content = self.path.read_text(encoding="utf-8", errors="replace")

        self.setText(content)
        self.setModified(False)

    def save_file(self, path=None):
        if path:
            self.path = Path(path).resolve()

        if not self.path:
            return False

        self.path.write_text(self.text(), encoding="utf-8")
        self.setModified(False)
        return True


class ClickableOutput(QTextEdit):
    """Read-only output box. Clicking a line that mentions
    'line N column M' (as HTML Tidy writes it) jumps to that spot."""

    def __init__(self, parent, jump_callback):
        super().__init__(parent)
        self.jump_callback = jump_callback
        self.viewport().setCursor(Qt.CursorShape.IBeamCursor)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)

        if event.button() != Qt.MouseButton.LeftButton:
            return

        # Do nothing if the user was selecting text to copy
        if self.textCursor().hasSelection():
            return

        cursor = self.cursorForPosition(event.position().toPoint())
        line_text = cursor.block().text()

        match = re.search(
            r"line\s+(\d+)(?:\s+column\s+(\d+))?",
            line_text,
            re.IGNORECASE
        )

        if match:
            line_number = int(match.group(1))
            column = int(match.group(2)) if match.group(2) else 1
            self.jump_callback(line_number, column)


class FindReplaceDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.last_search = ""
        self.last_editor = None
        self.last_direction = None

        self.setWindowTitle("Find and Replace")
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)

        find_row = QHBoxLayout()
        find_row.addWidget(QLabel("Find what:", self))
        self.find_input = QLineEdit(self)
        find_row.addWidget(self.find_input)
        layout.addLayout(find_row)

        replace_row = QHBoxLayout()
        replace_row.addWidget(QLabel("Replace with:", self))
        self.replace_input = QLineEdit(self)
        replace_row.addWidget(self.replace_input)
        layout.addLayout(replace_row)

        self.match_count_label = QLabel("Matches: 0", self)
        layout.addWidget(self.match_count_label)

        button_row = QHBoxLayout()

        self.find_next_button = QPushButton("Find Next", self)
        self.replace_next_button = QPushButton("Replace Next", self)
        self.replace_all_button = QPushButton("Replace All", self)
        self.close_button = QPushButton("Close", self)

        button_row.addWidget(self.find_next_button)
        button_row.addWidget(self.replace_next_button)
        button_row.addWidget(self.replace_all_button)
        button_row.addWidget(self.close_button)

        layout.addLayout(button_row)

        self.find_input.textChanged.connect(self.search_text_changed)
        self.find_next_button.clicked.connect(lambda: self.find_next())
        self.replace_next_button.clicked.connect(lambda: self.replace_next())
        self.replace_all_button.clicked.connect(lambda: self.replace_all())
        self.close_button.clicked.connect(self.close)

    def count_matches(self, search_text):
        editor = self.owner.editor()
        if not editor or not search_text:
            return 0

        return len(re.findall(re.escape(search_text), editor.text(), re.IGNORECASE))

    def update_match_count(self):
        search_text = self.find_input.text()
        count = self.count_matches(search_text)
        self.match_count_label.setText(f"Matches: {count}")

    def search_text_changed(self, *_):
        self.last_search = ""
        self.update_match_count()

    def find_in_direction(self, forward):
        search_text = self.find_input.text()
        editor = self.owner.editor()

        if not editor or not search_text:
            return False

        continuing = (
            editor is self.last_editor
            and search_text == self.last_search
        )

        if (
            continuing
            and editor.hasSelectedText()
            and editor.selectedText().casefold() == search_text.casefold()
        ):
            line_from, index_from, line_to, index_to = editor.getSelection()
            if forward:
                start_line, start_index = line_to, index_to
            else:
                start_line, start_index = line_from, index_from
        else:
            start_line, start_index = editor.getCursorPosition()

        found = editor.findFirst(
            search_text, False, False, False, True, forward, start_line, start_index
        )

        self.last_editor = editor
        self.last_search = search_text
        self.last_direction = "forward" if forward else "backward"

        return found

    def find_next_match(self):
        return self.find_in_direction(True)

    def find_next(self):
        search_text = self.find_input.text()
        if not search_text:
            return

        if not self.find_next_match():
            QMessageBox.information(
                self, "Find Next", f"No match found for '{search_text}'."
            )

    def find_previous(self):
        search_text = self.find_input.text()
        if not search_text:
            return

        if not self.find_in_direction(False):
            QMessageBox.information(
                self, "Find Previous", f"No previous match found for '{search_text}'."
            )

    def replace_next(self):
        search_text = self.find_input.text()
        editor = self.owner.editor()

        if not editor or not search_text:
            return

        if editor.selectedText().casefold() != search_text.casefold():
            if not self.find_next_match():
                QMessageBox.information(
                    self, "Replace Next", f"No match found for '{search_text}'."
                )
                return

        editor.replace(self.replace_input.text())
        self.last_editor = editor
        self.last_search = search_text
        self.update_match_count()

        if not editor.findFirst(search_text, False, False, False, True, True):
            self.last_search = ""

    def replace_all(self):
        search_text = self.find_input.text()
        editor = self.owner.editor()

        if not editor or not search_text:
            return

        count_before = self.count_matches(search_text)

        if count_before == 0:
            QMessageBox.information(self, "Replace All", "No matches found.")
            return

        replacement_text = self.replace_input.text()
        count = 0

        editor.beginUndoAction()
        try:
            editor.setCursorPosition(0, 0)
            found = editor.findFirst(search_text, False, False, False, False, True)

            while found:
                editor.replace(replacement_text)
                count += 1
                found = editor.findNext()
        finally:
            editor.endUndoAction()

        self.last_editor = editor
        self.last_search = search_text
        self.update_match_count()

        QMessageBox.information(
            self, "Replace All", f"Replaced {count} occurrence(s)."
        )


class SiteForgeIconProvider(QFileIconProvider):

    def icon(self, info):
        if info.isDir():
            icon_path = (
                Path(__file__).resolve().parent
                / "mime"
                / "homesite_mime_folder_closed.svg"
            )
            if icon_path.is_file():
                return QIcon(str(icon_path))
            return super().icon(info)

        suffix = info.suffix().lower()
        filename = info.fileName().lower()

        special_files = {
            ".htaccess": "htaccess",
            "robots.txt": "robots",
        }

        mime_icons = {
            "html": "html", "htm": "html", "xhtml": "html", "css": "css",
            "js": "js", "mjs": "js", "json": "json", "jpg": "image-file",
            "jpeg": "image-file", "png": "image-file", "gif": "image-file",
            "bmp": "image-file", "webp": "image-file", "svg": "svg",
            "ico": "image-file", "txt": "text", "md": "markdown", "php": "php",
            "asp": "binary", "aspx": "binary", "cfm": "binary", "jsp": "binary",
            "pl": "shell", "rb": "binary", "sql": "config", "py": "python",
            "vbs": "shell", "swf": "binary", "zip": "zip", "mp3": "audio",
            "wav": "audio", "ogg": "audio", "mp4": "video", "avi": "video",
            "mkv": "video", "xml": "xml", "sh": "shell", "exe": "exe",
            "pdf": "pdf", "ttf": "font", "otf": "font", "conf": "config",
            "ini": "config",
        }

        icon_name = special_files.get(filename)
        if icon_name is None:
            icon_name = mime_icons.get(suffix, "file-generic")

        icon_path = (
            Path(__file__).resolve().parent / "icons" / "files" / f"{icon_name}.png"
        )

        if icon_path.is_file():
            return QIcon(str(icon_path))

        return super().icon(info)


class FolderStateIconDelegate(QStyledItemDelegate):
    """Show open and closed icons for folders in the folder tree."""

    def __init__(self, view):
        super().__init__(view)
        self.view = view

        icon_directory = Path(__file__).resolve().parent / "icons" / "files"

        self.closed_icon = QIcon(str(icon_directory / "folder.png"))
        self.open_icon = QIcon(str(icon_directory / "folder-open.png"))

    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        model = index.model()

        if index.column() == 0 and model.isDir(index):
            if self.view.isExpanded(index):
                option.icon = self.open_icon
            else:
                option.icon = self.closed_icon


class DraggableFileSystemModel(QFileSystemModel):
    """Custom QFileSystemModel that explicitly enables dragging for files."""
    def flags(self, index):
        default_flags = super().flags(index)
        if index.isValid() and not self.isDir(index):
            return default_flags | Qt.ItemFlag.ItemIsDragEnabled
        return default_flags


class SiteForge(QMainWindow):

    def __init__(self):
        super().__init__()
        
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
        SNIPPETS_DIR.mkdir(parents=True, exist_ok=True)

        self.setWindowTitle("SiteForge")
        icon_path = os.path.join(os.path.dirname(__file__), "icons", "siteforge-icon.png")
        self.setWindowIcon(QIcon(icon_path))   
        window_settings = STATE_FILE

        try:
            window_data = json.loads(window_settings.read_text())
        except (OSError, ValueError):
            window_data = {}

        geometry = window_data.get("window_geometry", {})

        self.resize(
            geometry.get("width", 1100),
            geometry.get("height", 750)
        )

        if "x" in geometry and "y" in geometry:
            self.move(geometry["x"], geometry["y"])

        self.restore_maximized = window_data.get("window_maximized", False)
        self.saved_explorer_width = window_data.get("explorer_width", 250)
        
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.currentChanged.connect(self.update_title)
        self.tabs.currentChanged.connect(self.update_cursor_position)
        self.tabs.tabCloseRequested.connect(self.close_tab)

        self.setCentralWidget(self.tabs)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.position_label = QLabel("Line 1, Column 1", self)
        self.status.addPermanentWidget(self.position_label)        
        self.last_directory = self.load_last_directory()
        self.recent_files = self.load_recent_files()        
        self.create_file_panel()
        self.create_actions()
        self.setup_tidy_tool()
        self.external_tool_entries = self.load_external_tools()
        self.create_output_panel()
        self.create_menus()
        self.create_toolbar()

        self.spell_error_shown = False
        SPELLER.on_error = self.spell_error
        self.register_snippet_shortcuts()
        self.restore_session()

        if SPELLER.enabled and not SPELLER.available():
            self.status.showMessage(
                "Spell checking is on, but aspell or hunspell is not "
                "installed (sudo apt install aspell aspell-en).", 10000
            )

    def create_file_panel(self):
        root = str(Path.home())
        self.explorer_tabs = QTabWidget()

        # Folder navigation button
        self.up_folder_button = QPushButton("↑ Up")
        self.up_folder_button.setToolTip("Navigate to the parent folder")
        self.up_folder_button.clicked.connect(self.navigate_up_folder)

        # Single unified tree view for both folders and files with draggable support
        self.file_model = DraggableFileSystemModel(self)
        self.file_model.setFilter(
            QDir.Filter.AllDirs | QDir.Filter.Files | QDir.Filter.NoDotAndDotDot
        )
        self.file_model.setRootPath(root)
        self.file_model.setIconProvider(SiteForgeIconProvider())

        self.explorer_panel = QWidget()
        self.explorer_panel_layout = QVBoxLayout(self.explorer_panel)
        self.explorer_panel_layout.setContentsMargins(0, 0, 0, 0)
        self.explorer_panel_layout.setSpacing(4)
        self.explorer_panel_layout.addWidget(self.up_folder_button)

        self.file_tree = QTreeView()
        self.file_tree.setModel(self.file_model)
        self.file_tree.setItemDelegate(
            FolderStateIconDelegate(self.file_tree)
        )
        self.file_tree.setStyleSheet("QTreeView { font-size: 11pt; }")
        self.file_tree.setRootIndex(self.file_model.index(self.last_directory))
        
        self.file_tree.setHeaderHidden(True)
        self.file_tree.setIconSize(QSize(24, 24))
        self.file_tree.setDragEnabled(True)
        self.file_tree.setDragDropMode(QTreeView.DragDropMode.DragOnly)

        for column in range(1, 4):
            self.file_tree.hideColumn(column)

        # Double-clicking a file opens it
        self.file_tree.doubleClicked.connect(self.open_tree_file)

        self.explorer_panel_layout.addWidget(self.file_tree)

        self.file_dock = QDockWidget("Explorer", self)
        dock = self.file_dock
        dock.setObjectName("FilesDock")
        dock.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)

        title_bar = QWidget(dock)
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(6, 2, 4, 2)

        title_label = QLabel("Explorer", title_bar)
        title_layout.addWidget(title_label)
        title_layout.addStretch()

        collapse_button = QToolButton(title_bar)
        collapse_button.setArrowType(Qt.ArrowType.LeftArrow)
        collapse_button.setToolTip("Hide Explorer")
        collapse_button.setAutoRaise(True)
        collapse_button.clicked.connect(self.toggle_explorer_panel)
        title_layout.addWidget(collapse_button)

        dock.setTitleBarWidget(title_bar)
        self.explorer_tabs.addTab(self.explorer_panel, "Explorer")

        # Snippets panel with manage button and QTreeWidget matching Bluefish structure
        self.snippets_panel = QWidget()
        snippets_layout = QVBoxLayout(self.snippets_panel)
        snippets_layout.setContentsMargins(0, 0, 0, 0)
        snippets_layout.setSpacing(4)

        manage_snippets_btn = QPushButton("Manage Snippets...")
        manage_snippets_btn.clicked.connect(self.manage_snippets)
        snippets_layout.addWidget(manage_snippets_btn)

        self.snippet_list = QTreeWidget()
        self.snippet_list.setHeaderHidden(True)
        self.snippet_list.itemDoubleClicked.connect(self.insert_selected_snippet)
        snippets_layout.addWidget(self.snippet_list)

        self.explorer_tabs.addTab(self.snippets_panel, "Snippets")

        dock.setWidget(self.explorer_tabs)
        self.refresh_snippet_list()
        
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        self.resizeDocks([dock], [self.saved_explorer_width], Qt.Orientation.Horizontal)

        # Create the expand button toolbar on the left edge (hidden by default)
        self.expand_toolbar = QToolBar("Expand Explorer", self)
        self.expand_toolbar.setMovable(False)
        self.expand_explorer_btn = QToolButton(self)
        self.expand_explorer_btn.setArrowType(Qt.ArrowType.RightArrow)
        self.expand_explorer_btn.setToolTip("Expand Explorer")
        self.expand_explorer_btn.setAutoRaise(True)
        self.expand_explorer_btn.clicked.connect(self.toggle_explorer_panel)
        self.expand_toolbar.addWidget(self.expand_explorer_btn)
        self.addToolBar(Qt.ToolBarArea.LeftToolBarArea, self.expand_toolbar)
        self.expand_toolbar.hide()

        dock.visibilityChanged.connect(self.on_dock_visibility_changed)

    def toggle_explorer_panel(self):
        """Toggles the Explorer panel open and closed."""
        if self.file_dock.isVisible():
            self.file_dock.hide()
            self.expand_toolbar.show()
        else:
            self.file_dock.show()
            self.expand_toolbar.hide()

    def on_dock_visibility_changed(self, visible):
        """Ensures the expand button stays synchronized if visibility changes."""
        if visible:
            self.expand_toolbar.hide()
        else:
            self.expand_toolbar.show()

    def navigate_up_folder(self):
        current_path = Path(
            self.file_model.filePath(
                self.file_tree.rootIndex()
            )
        ).resolve()

        parent = current_path.parent
        if parent == current_path:
            return

        parent_path = str(parent)
        parent_index = self.file_model.index(parent_path)

        if not parent_index.isValid():
            return

        self.file_tree.setRootIndex(parent_index)
        self.file_tree.setCurrentIndex(parent_index)
        self.file_tree.expand(parent_index)

        self.remember_directory(parent_path)

    def load_last_directory(self):
        settings = STATE_FILE

        try:
            data = json.loads(settings.read_text())
            directory = Path(data["last_directory"])

            if directory.is_dir():
                return str(directory)

        except (OSError, ValueError, KeyError, TypeError):
            pass

        default = Path.home() / "_WEBDEV"

        if default.is_dir():
            return str(default)

        return str(Path.home())

    def remember_directory(self, directory):
        directory = Path(directory)

        if directory.is_file():
            directory = directory.parent

        if not directory.is_dir():
            return

        self.last_directory = str(directory)
        settings = STATE_FILE

        try:
            data = json.loads(settings.read_text())
        except (OSError, ValueError):
            data = {}

        data["last_directory"] = self.last_directory

        try:
            settings.write_text(json.dumps(data, indent=4))
        except OSError:
            pass

    def load_recent_files(self):
        settings = STATE_FILE

        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

        recent = data.get("recent_files", [])

        if not isinstance(recent, list):
            return []

        return [
            str(Path(path).resolve())
            for path in recent
            if isinstance(path, str)
            and path
            and Path(path).is_file()
        ][:10]

    def save_recent_files(self):
        settings = STATE_FILE

        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}

        data["recent_files"] = self.recent_files

        try:
            settings.write_text(
                json.dumps(data, indent=4),
                encoding="utf-8"
            )
        except OSError:
            pass

    def add_recent_file(self, path):
        target = str(Path(path).resolve())

        self.recent_files = [
            target
        ] + [
            item for item in self.recent_files
            if item != target and Path(item).is_file()
        ]

        self.recent_files = self.recent_files[:10]
        self.save_recent_files()
        self.update_recent_files_menu()

    def update_recent_files_menu(self):
        if not hasattr(self, "recent_files_menu"):
            return

        self.recent_files_menu.clear()

        if not self.recent_files:
            action = self.recent_files_menu.addAction(
                "(No recent files)"
            )
            action.setEnabled(False)
            return

        for path in self.recent_files:
            action = self.recent_files_menu.addAction(path)
            action.triggered.connect(
                lambda checked=False, filename=path:
                    self.open_recent_file(filename)
            )

    def open_recent_file(self, path):
        if not Path(path).is_file():
            self.recent_files = [
                item for item in self.recent_files
                if item != path
            ]
            self.save_recent_files()
            self.update_recent_files_menu()
            QMessageBox.information(
                self,
                "Recent Files",
                "That file no longer exists."
            )
            return

        self.open_path(path)

    def save_session(self):
        settings = STATE_FILE

        try:
            data = json.loads(settings.read_text())
        except (OSError, ValueError):
            data = {}

        open_files = []
        cursor_positions = {}
        bookmarks = {}
        active_index = self.tabs.currentIndex()

        for index in range(self.tabs.count()):
            editor = self.tabs.widget(index)

            if editor.path:
                file_path = str(editor.path.resolve())
                open_files.append(file_path)
                line, column = editor.getCursorPosition()
                cursor_positions[file_path] = [line, column]

                bookmark_lines = []
                line = editor.markerFindNext(
                    0, 1 << editor.bookmark_marker
                )

                while line != -1:
                    bookmark_lines.append(line)
                    line = editor.markerFindNext(
                        line + 1, 1 << editor.bookmark_marker
                    )

                bookmarks[file_path] = bookmark_lines

            else:
                open_files.append(None)

        data["open_files"] = open_files
        data["cursor_positions"] = cursor_positions
        data["active_tab"] = active_index
        data["bookmarks"] = bookmarks
        data["word_wrap"] = self.actions["wordwrap"].isChecked()
        data["tag_completion_enabled"] = self.actions["tag_completion"].isChecked()
        data["spell_check_enabled"] = self.actions["spellcheck"].isChecked()
        data["spell_language"] = SPELLER.language
        data["spell_style"] = SPELLER.style
        
        geometry = (
            self.normalGeometry()
            if self.isMaximized()
            else self.geometry()
        )

        data["window_geometry"] = {
            "x": geometry.x(),
            "y": geometry.y(),
            "width": geometry.width(),
            "height": geometry.height(),
        }
        data["window_maximized"] = self.isMaximized()
        data["explorer_width"] = self.file_dock.width()
        
        try:
            settings.write_text(json.dumps(data, indent=4))
        except OSError:
            pass

    def restore_session(self):
        data = {}
        settings = STATE_FILE

        try:
            data = json.loads(settings.read_text())
            open_files = data.get("open_files", [])
            cursor_positions = data.get("cursor_positions", {})
            bookmarks = data.get("bookmarks", {})
            active_index = data.get("active_tab", 0)
        except (OSError, ValueError):
            open_files = []
            cursor_positions = {}
            bookmarks = {}
            active_index = 0

        word_wrap = data.get("word_wrap", False)
        self.actions["wordwrap"].setChecked(word_wrap)

        tag_comp = data.get("tag_completion_enabled", True)
        self.actions["tag_completion"].setChecked(tag_comp)

        restored = False

        for path in open_files:
            if path and Path(path).is_file():
                self.open_path(path, record_recent=False)
                target = Path(path).resolve()

                for tab_index in range(self.tabs.count()):
                    editor = self.tabs.widget(tab_index)

                    if editor.path == target:
                        position = cursor_positions.get(str(target))

                        if position and len(position) == 2:
                            line = max(
                                0, min(int(position[0]), editor.lines() - 1)
                            )
                            column = max(0, int(position[1]))
                            editor.setCursorPosition(line, column)
                            editor.ensureLineVisible(line)
                            editor.setFocus()

                        for bookmark_line in bookmarks.get(
                            str(target), []
                        ):
                            bookmark_line = int(bookmark_line)

                            if 0 <= bookmark_line < editor.lines():
                                editor.markerAdd(
                                    bookmark_line,
                                    editor.bookmark_marker
                                )

                        break

                restored = True

        if restored:
            self.tabs.setCurrentIndex(
                min(active_index, self.tabs.count() - 1)
            )
        else:
            self.new_file()

    def create_actions(self):
        self.actions = {}
        self.shortcut_settings_file = SHORTCUTS_FILE
        
        icon_directory = Path(__file__).resolve().parent / "icons" / "menu"

        icon_files = {
            "new": "new.png",
            "open": "open.png",
            "save": "save.png",
            "saveall": "saveall.png",
            "saveas": "saveas.png",
            "close": "close.png",
            "close_all": "close-all.png",
            "undo": "undo.png",
            "redo": "redo.png",
            "find": "find.png",
            "findnext": "find.png",
            "findprevious": "find.png",
            "replaceall": "replace.png",
            "insert_paragraph": "paragraph.png",
            "insert_h1": "heading.png",
            "insert_h2": "heading.png",
            "insert_bold": "bold.png",
            "insert_italic": "italic.png",
            "insert_underline": "underline.png",
            "insert_div": "tag.png",
            "insert_ul": "ul.png",
            "insert_ol": "ol.png",
            "insert_hr": "hr.png",
            "insert_link": "link.png",
            "insert_image": "image.png",
        }

        def add(name, label, shortcut, function):
            action = QAction(label, self)
            action.setShortcut(shortcut)

            icon_name = icon_files.get(name)
            if icon_name:
                icon_path = icon_directory / icon_name
                if icon_path.is_file():
                    icon = QIcon(str(icon_path))
                    if not icon.isNull():
                        action.setIcon(icon)

            action.triggered.connect(function)
            self.actions[name] = action

        add("new", "New", "Ctrl+N", self.new_file)
        add("open", "Open...", "Ctrl+O", self.open_file)
        add("save", "Save", "Ctrl+S", self.save_file)
        add("saveall", "Save All", "Ctrl+Alt+S", self.save_all)        
        add("saveas", "Save As...", "Ctrl+Shift+S", self.save_as)
        add("close", "Close Tab", "Ctrl+W",
            lambda: self.close_tab(self.tabs.currentIndex()))

        add("undo", "Undo", "Ctrl+Z", lambda: self.editor_call("undo"))
        add("redo", "Redo", "Ctrl+Y", lambda: self.editor_call("redo"))
        add("find", "Find and Replace...", "Ctrl+F", self.show_find_replace)
        add("findnext", "Find Next", "F3", self.find_next_in_dialog)
        add("findprevious", "Find Previous", "Shift+F3", self.find_previous_in_dialog)
        add("gotoline", "Go to Line...", "Ctrl+G", self.go_to_line)        
        add("replaceall", "Replace...", "Ctrl+H", self.show_replace_dialog)        
        add("quit", "Quit", "Ctrl+Q", self.close)
        add("close_all", "Close All", "", self.close_all_tabs)
        add("help", "SiteForge Help", "F1", self.show_help)
        add("defaultbrowser", "Open in Default Browser", "",
            self.launch_default_browser)
        self.actions["defaultbrowser"].setShortcut(QKeySequence("F12"))
        self.actions["defaultbrowser"].setShortcutContext(
            Qt.ShortcutContext.ApplicationShortcut
        )
        self.addAction(self.actions["defaultbrowser"])

        # HTML insertion actions
        add("insert_paragraph", "Paragraph", "",
            lambda checked=False: self.insert_tag_pair("<p>", "</p>"))
        add("insert_h1", "Heading 1", "",
            lambda checked=False: self.insert_tag_pair("<h1>", "</h1>"))
        add("insert_h2", "Heading 2", "",
            lambda checked=False: self.insert_tag_pair("<h2>", "</h2>"))
        add("insert_bold", "Bold", "",
            lambda checked=False: self.insert_tag_pair("<b>", "</b>"))
        add("insert_italic", "Italic", "",
            lambda checked=False: self.insert_tag_pair("<i>", "</i>"))
        add("insert_underline", "Underline", "",
            lambda checked=False: self.insert_tag_pair("<u>", "</u>"))
        add("insert_div", "Division (div)", "",
            lambda checked=False: self.insert_tag_pair("<div>", "</div>"))
        add("insert_ul", "Unordered List", "", self.insert_unordered_list)
        add("insert_ol", "Ordered List", "", self.insert_ordered_list)
        add("insert_hr", "Horizontal Rule", "", self.insert_horizontal_rule)
        add("insert_link", "Link...", "", self.insert_link)
        add("insert_image", "Image...", "", self.insert_image)
     
        panel_action = QAction("Explorer", self)
        panel_action.setCheckable(True)
        panel_action.setChecked(True)
        panel_action.triggered.connect(self.file_dock.setVisible)
        self.file_dock.visibilityChanged.connect(panel_action.setChecked)
        self.actions["filepanel"] = panel_action
        
        wrap_action = QAction("Word wrap", self)
        wrap_action.setCheckable(True)
        wrap_action.setChecked(False)
        wrap_action.triggered.connect(self.toggle_word_wrap)
        self.actions["wordwrap"] = wrap_action

        tag_comp_action = QAction("Tag completion", self)
        tag_comp_action.setCheckable(True)
        tag_comp_action.setChecked(True)
        tag_comp_action.triggered.connect(self.toggle_tag_completion)
        self.actions["tag_completion"] = tag_comp_action

        spell_action = QAction("Spell checking", self)
        spell_action.setCheckable(True)
        spell_action.setChecked(SPELLER.enabled)
        spell_action.triggered.connect(self.toggle_spell_check)
        self.actions["spellcheck"] = spell_action

        font_action = QAction("Editor font...", self)
        font_action.triggered.connect(self.choose_editor_font)
        self.actions["editorfont"] = font_action

        # Indentation commands
        add("indent_line", "Indent Line", "Ctrl+]", self.indent_line)
        add("unindent_line", "Unindent Line", "Ctrl+[", self.unindent_line)
        add("indent_block", "Indent Block", "Ctrl+Shift+]", self.indent_block)
        add("unindent_block", "Unindent Block", "Ctrl+Shift+[", self.unindent_block)        
        
        self.default_shortcuts = {
            name: action.shortcut().toString()
            for name, action in self.actions.items()
        }

        self.load_shortcuts()

    def browser_config_path(self):
        return BROWSERS_FILE

    def load_browser_entries(self):
        defaults = [
            {"name": "Firefox", "command": "/usr/bin/firefox", "icon": "firefox.png"},
            {"name": "Brave", "command": "/usr/bin/brave-browser", "icon": "brave.png"},
            {"name": "Firefox 4", "command": "/opt/firefox-4/firefox", "icon": "firefox.png"},
        ]

        config = self.browser_config_path()

        if config.exists():
            try:
                entries = json.loads(config.read_text(encoding="utf-8"))
                for entry in entries:
                    if not entry.get("icon"):
                        entry["icon"] = "browser.png"
                return (entries + [{"name": "", "command": "", "icon": "browser.png"}] * 10)[:10]
            except (OSError, json.JSONDecodeError):
                pass

        return defaults + [{"name": "", "command": "", "icon": "browser.png"}] * 7

    def rebuild_browser_menu(self):
        self.browsers_menu.clear()
        icon_dir = Path(__file__).resolve().parent / "icons" / "browsers"

        for browser_index, entry in enumerate(self.browser_entries):
            name = entry.get("name", "").strip()
            command = entry.get("command", "").strip()

            if name and command:
                icon_name = entry.get("icon", "browser.png")
                icon_path = icon_dir / icon_name

                if not icon_path.is_file():
                    icon_path = icon_dir / "browser.png"

                action = self.browsers_menu.addAction(
                    QIcon(str(icon_path)),
                    name,
                    lambda checked=False, cmd=command: self.launch_browser(cmd)
                )

                if browser_index < 3:
                    action.setShortcut(
                        ["Ctrl+F2", "Ctrl+F3", "Ctrl+F4"][browser_index]
                    )

        self.browsers_menu.addSeparator()
        self.browsers_menu.addAction(
            "Configure browsers...",
            self.configure_browsers
        )

    def browse_browser_executable(self, table, row):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select browser executable",
            "/usr/bin",
            "All files (*)"
        )

        if path:
            table.setItem(row, 1, QTableWidgetItem(path))

    def configure_browsers(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Configure browsers")
        dialog.resize(950, 420)
        layout = QVBoxLayout(dialog)

        table = QTableWidget(10, 5, dialog)
        table.setHorizontalHeaderLabels(
            ["Browser name", "Executable path", "Browse", "Icon", "Default"]
        )

        table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )

        browser_icon_dir = Path(__file__).resolve().parent / "icons" / "browsers"
        icon_files = sorted(
            path.name for path in browser_icon_dir.glob("*.png")
        )

        if "browser.png" in icon_files:
            icon_files.remove("browser.png")
            icon_files.insert(0, "browser.png")

        default_buttons = []
        icon_boxes = []

        for row, entry in enumerate(self.browser_entries):
            table.setItem(row, 0, QTableWidgetItem(entry.get("name", "")))
            table.setItem(row, 1, QTableWidgetItem(entry.get("command", "")))

            browse_button = QPushButton("Browse...")
            browse_button.clicked.connect(
                lambda checked=False, r=row: self.browse_browser_executable(table, r)
            )
            table.setCellWidget(row, 2, browse_button)

            icon_box = QComboBox()
            for filename in icon_files:
                icon_path = browser_icon_dir / filename
                icon_box.addItem(
                    QIcon(str(icon_path)),
                    filename.removesuffix(".png").replace("_", " ").title(),
                    filename
                )

            selected_icon = entry.get("icon", "browser.png")
            icon_index = icon_box.findData(selected_icon)
            if icon_index >= 0:
                icon_box.setCurrentIndex(icon_index)
            else:
                fallback_index = icon_box.findData("browser.png")
                if fallback_index >= 0:
                    icon_box.setCurrentIndex(fallback_index)

            icon_boxes.append(icon_box)
            table.setCellWidget(row, 3, icon_box)

            default_button = QRadioButton()
            default_buttons.append(default_button)
            table.setCellWidget(row, 4, default_button)

            def select_default(checked, current=row):
                if checked:
                    for index, button in enumerate(default_buttons):
                        if index != current:
                            button.setChecked(False)

            default_button.toggled.connect(select_default)

        selected_default = None

        for row, entry in enumerate(self.browser_entries):
            if (
                entry.get("is_default", False)
                and entry.get("name", "").strip()
                and entry.get("command", "").strip()
            ):
                selected_default = row
                break

        if selected_default is None:
            for row, entry in enumerate(self.browser_entries):
                if (
                    entry.get("name", "").strip()
                    and entry.get("command", "").strip()
                ):
                    selected_default = row
                    break

        if selected_default is not None:
            default_buttons[selected_default].setChecked(True)

        layout.addWidget(table)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        entries = []
        selected_row = next(
            (
                row for row, button in enumerate(default_buttons)
                if button.isChecked()
            ),
            None
        )

        if selected_row is not None:
            name_item = table.item(selected_row, 0)
            command_item = table.item(selected_row, 1)

            if not (
                name_item and name_item.text().strip()
                and command_item and command_item.text().strip()
            ):
                QMessageBox.information(
                    self, "SiteForge",
                    "Please select a row with a browser name and executable path."
                )
                return

        for row in range(10):
            name_item = table.item(row, 0)
            command_item = table.item(row, 1)

            name = name_item.text().strip() if name_item else ""
            command = command_item.text().strip() if command_item else ""

            entries.append({
                "name": name,
                "command": command,
                "icon": icon_boxes[row].currentData() or "browser.png",
                "is_default": row == selected_row
            })

        config = self.browser_config_path()

        try:
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(
                json.dumps(entries, indent=4),
                encoding="utf-8"
            )
        except OSError as error:
            QMessageBox.critical(
                self, "SiteForge",
                f"Could not save browser settings:\n{error}"
            )
            return

        self.browser_entries = entries
        self.rebuild_browser_menu()
        self.refresh_browser_toolbar()

    def launch_browser(self, command):
        editor = self.editor()

        if not editor:
            return

        if not editor.path or not editor.path.exists():
            QMessageBox.information(
                self, "SiteForge",
                "Save this file before opening it in a browser."
            )
            return

        try:
            subprocess.Popen([command, str(editor.path)])
        except Exception as error:
            QMessageBox.critical(
                self, "SiteForge",
                f"Could not launch browser:\n{error}"
            )

    def load_shortcuts(self):
        try:
            data = json.loads(
                self.shortcut_settings_file.read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, ValueError):
            return

        for name, shortcut in data.items():
            if name in self.actions:
                self.actions[name].setShortcut(shortcut)

    def show_keyboard_shortcuts(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Keyboard Shortcuts")
        dialog.resize(560, 600)

        layout = QVBoxLayout(dialog)

        tree = QTreeWidget(dialog)
        tree.setColumnCount(2)
        tree.setHeaderLabels(["Command", "Shortcut"])
        tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        tree.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        tree.setRootIsDecorated(True)

        standard_names = {
            "new", "open", "save", "saveall", "saveas",
            "close", "undo", "redo", "find", "findnext",
            "findprevious", "gotoline", "replaceall", "quit",
        }

        sections = {
            "insert": QTreeWidgetItem(tree, ["Insert commands"]),
            "standard": QTreeWidgetItem(tree, ["Standard shortcuts"]),
            "other": QTreeWidgetItem(tree, ["Other commands"]),
        }

        shortcut_editors = {}

        for name, action in self.actions.items():
            label = action.text().replace("&", "")

            if name.startswith("insert_"):
                section = sections["insert"]
            elif name in standard_names:
                section = sections["standard"]
            else:
                section = sections["other"]

            item = QTreeWidgetItem(section, [label, ""])
            editor = QKeySequenceEdit(action.shortcut(), tree)
            tree.setItemWidget(item, 1, editor)
            shortcut_editors[name] = editor

        sections["insert"].setExpanded(True)
        sections["standard"].setExpanded(False)
        sections["other"].setExpanded(False)

        layout.addWidget(tree)

        restore_button = QPushButton("Restore Defaults", dialog)
        button_row = QHBoxLayout()
        button_row.addWidget(restore_button)
        button_row.addStretch()
        layout.addLayout(button_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel,
            parent=dialog
        )
        layout.addWidget(buttons)

        def restore_defaults():
            for name, editor in shortcut_editors.items():
                editor.setKeySequence(
                    QKeySequence(self.default_shortcuts[name])
                )

        def save_shortcuts():
            shortcuts = {}
            owners = {}

            for name, editor in shortcut_editors.items():
                shortcut = editor.keySequence().toString()
                shortcuts[name] = shortcut

                if shortcut:
                    if shortcut in owners:
                        QMessageBox.warning(
                            dialog,
                            "Shortcut Conflict",
                            f'"{shortcut}" is assigned to more '
                            f'than one command:\n\n'
                            f'{owners[shortcut]}\n'
                            f'{self.actions[name].text()}'
                        )
                        return

                    owners[shortcut] = self.actions[name].text()

            try:
                self.shortcut_settings_file.write_text(
                    json.dumps(shortcuts, indent=2),
                    encoding="utf-8"
                )
            except OSError as error:
                QMessageBox.warning(
                    dialog,
                    "Save Error",
                    f"Could not save keyboard shortcuts:\n{error}"
                )
                return

            for name, shortcut in shortcuts.items():
                self.actions[name].setShortcut(shortcut)

            dialog.accept()

        restore_button.clicked.connect(restore_defaults)
        buttons.accepted.connect(save_shortcuts)
        buttons.rejected.connect(dialog.reject)

        dialog.exec()

    def show_document_weight(self):
        editor = self.editor()
        if not editor:
            return

        if not editor.path:
            QMessageBox.warning(
                self, "Document Weight",
                "Please save the document first."
            )
            return

        if editor.isModified():
            QMessageBox.warning(
                self, "Document Weight",
                "Please save your changes first."
            )
            return

        try:
            html_path = editor.path.resolve()
            html_size = html_path.stat().st_size
            html_text = html_path.read_text(encoding="utf-8")

            parser = ImageSourceParser()
            parser.feed(html_text)

            def collect_files(sources):
                found = set()
                missing = set()
                external = set()

                for source in sources:
                    source = source.strip()
                    if not source:
                        continue

                    if source.startswith(
                        ("http://", "https://", "//", "data:")
                    ):
                        external.add(source)
                        continue

                    if source.startswith("file:"):
                        continue

                    try:
                        path = (
                            html_path.parent
                            / source.split("#")[0].split("?")[0]
                        ).resolve()

                        if path.is_file():
                            found.add(path)
                        else:
                            missing.add(source)
                    except (OSError, ValueError):
                        missing.add(source)

                return found, missing, external

            image_paths, missing_images, external_images = (
                collect_files(parser.sources)
            )

            css_paths, missing_css, external_css = (
                collect_files(parser.stylesheets)
            )

            js_paths, missing_js, external_js = (
                collect_files(parser.scripts)
            )

            def total_size(paths):
                size = 0
                for path in paths:
                    try:
                        size += path.stat().st_size
                    except OSError:
                        pass
                return size

            used_paths = {html_path}

            image_paths -= used_paths
            image_size = total_size(image_paths)
            used_paths.update(image_paths)

            css_paths -= used_paths
            css_size = total_size(css_paths)
            used_paths.update(css_paths)

            js_paths -= used_paths
            js_size = total_size(js_paths)

        except (OSError, UnicodeError, ValueError) as error:
            QMessageBox.warning(
                self, "Document Weight",
                f"Could not measure the document:\n{error}"
            )
            return

        total = html_size + image_size + css_size + js_size

        QMessageBox.information(
            self,
            "Document Weight",
            f"HTML: {html_size / 1024:.2f} KiB\n"
            f"Images: {image_size / 1024:.2f} KiB\n"
            f"Stylesheets: {css_size / 1024:.2f} KiB\n"
            f"JavaScript: {js_size / 1024:.2f} KiB\n"
            f"--------------------\n"
            f"Total: {total / 1024:.2f} KiB\n"
            f"Total: {total / 1_000_000:.4f} MB\n\n"
            f"Local images counted: {len(image_paths)}\n"
            f"Missing images: {len(missing_images)}\n"
            f"External images excluded: {len(external_images)}\n"
            f"Local stylesheets counted: {len(css_paths)}\n"
            f"Missing stylesheets: {len(missing_css)}\n"
            f"External stylesheets excluded: {len(external_css)}\n"
            f"Local scripts counted: {len(js_paths)}\n"
            f"Missing scripts: {len(missing_js)}\n"
            f"External scripts excluded: {len(external_js)}"
        )

    def backup_configuration(self):
        self.save_session()

        default_name = (
            Path.home()
            / f"SiteForge-configuration-{datetime.now():%Y%m%d-%H%M%S}.zip"
        )

        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Backup SiteForge Configuration",
            str(default_name),
            "ZIP archives (*.zip)"
        )

        if not filename:
            return

        archive_path = Path(filename)

        if archive_path.suffix.lower() != ".zip":
            archive_path = archive_path.with_suffix(".zip")

        try:
            with zipfile.ZipFile(
                archive_path, "w", zipfile.ZIP_DEFLATED
            ) as archive:
                for file_path in CONFIG_DIR.rglob("*"):
                    if file_path.is_file():
                        if file_path.resolve() == archive_path.resolve():
                            continue

                        archive.write(
                            file_path,
                            file_path.relative_to(CONFIG_DIR).as_posix()
                        )

            QMessageBox.information(
                self,
                "Backup Complete",
                f"Configuration backed up to:\n{archive_path}"
            )

        except Exception as exc:
            QMessageBox.critical(
                self,
                "Backup Failed",
                f"Could not create the configuration backup:\n{exc}"
            )

    def create_menus(self):
        bar = self.menuBar()

        file_menu = bar.addMenu("&File")

        for name in ("new", "open", "save", "saveall", "saveas", "close"):
            file_menu.addAction(self.actions[name])

        file_menu.addAction(self.actions["close_all"])

        self.recent_files_menu = file_menu.addMenu("Recent Files")
        self.update_recent_files_menu()

        file_menu.addSeparator()
        file_menu.addAction("New from Template...", self.new_from_template)
        file_menu.addAction("Save as Template...", self.save_as_template)

        file_menu.addSeparator()
        file_menu.addAction("Backup Configuration...", self.backup_configuration)

        file_menu.addSeparator()
        file_menu.addAction(self.actions["quit"])

        edit_menu = bar.addMenu("&Edit")
        edit_menu.addAction(self.actions["undo"])
        edit_menu.addAction(self.actions["redo"])
        edit_menu.addSeparator()

        menu_icon_dir = Path(__file__).resolve().parent / "icons" / "menu"

        self.actions["indent_line"].setIcon(QIcon(str(menu_icon_dir / "indent.png")))
        self.actions["indent_block"].setIcon(QIcon(str(menu_icon_dir / "indent.png")))
        self.actions["unindent_line"].setIcon(QIcon(str(menu_icon_dir / "outdent.png")))
        self.actions["unindent_block"].setIcon(QIcon(str(menu_icon_dir / "outdent.png")))

        edit_menu.addAction(self.actions["indent_line"])
        edit_menu.addAction(self.actions["unindent_line"])
        edit_menu.addAction(self.actions["indent_block"])
        edit_menu.addAction(self.actions["unindent_block"])

        search_menu = bar.addMenu("&Search")
        search_menu.addAction(self.actions["find"])
        search_menu.addAction(self.actions["findnext"])
        search_menu.addAction(self.actions["findprevious"])
        search_menu.addAction(self.actions["replaceall"]) 
        search_menu.addAction(self.actions["gotoline"])                      

        view_menu = bar.addMenu("&View")
        view_menu.addAction(self.actions["filepanel"])
        view_menu.addSeparator()
        view_menu.addAction(self.actions["wordwrap"])
        view_menu.addAction(self.actions["editorfont"])

        insert_menu = bar.addMenu("&Insert")

        for name in ("insert_paragraph", "insert_h1", "insert_h2"):
            insert_menu.addAction(self.actions[name])

        insert_menu.addSeparator()

        for name in ("insert_bold", "insert_italic", "insert_underline"):
            insert_menu.addAction(self.actions[name])

        insert_menu.addSeparator()

        insert_menu.addAction(self.actions["insert_div"])
        insert_menu.addAction(self.actions["insert_ul"])
        insert_menu.addAction(self.actions["insert_ol"])
        insert_menu.addAction(self.actions["insert_hr"])

        insert_menu.addSeparator()

        insert_menu.addAction(self.actions["insert_link"])
        insert_menu.addAction(self.actions["insert_image"])

        tools_menu = bar.addMenu("&Tools")

        tools_menu.addAction(
            QIcon(str(menu_icon_dir / "bgcolor.png")),
            "Tag-pair highlight colour...",
             self.choose_tag_pair_color
        )
        tools_menu.addAction(
             QIcon(str(menu_icon_dir / "bgcolor.png")),
             "Current-line highlight colour...",
             self.choose_current_line_color
        )
        tools_menu.addAction(
            QIcon(str(menu_icon_dir / "weight.png")),
            "Document Weight",
             self.show_document_weight
        )
        tools_menu.addAction(self.tidy_action)

        # External programs: separator, the user's tools, then Configure
        self.tools_menu = tools_menu
        tools_menu.addSeparator()
        self.external_tool_menu_actions = []
        self.configure_tools_action = tools_menu.addAction(
            "Configure external tools...",
            self.configure_external_tools
        )
        self.rebuild_external_tools_menu()

        self.browser_entries = self.load_browser_entries()
        self.browsers_menu = bar.addMenu("&Browsers")
        self.rebuild_browser_menu()


        settings_menu = bar.addMenu("&Settings")
        settings_menu.addAction("Default Template...", self.choose_default_template)
        settings_menu.addAction("Indentation Settings...", self.choose_indentation_settings)
        settings_menu.addAction("Keyboard Shortcuts...", self.show_keyboard_shortcuts)
        settings_menu.addSeparator()
        settings_menu.addAction(self.actions["tag_completion"])
        settings_menu.addAction(self.actions["spellcheck"])
        settings_menu.addAction("Spelling language...", self.choose_spelling_language)
        settings_menu.addAction("Spelling highlight style...", self.choose_spell_style)

        help_menu = bar.addMenu("&Help")
        help_menu.addAction(self.actions["help"])

    def launch_default_browser(self):
        for entry in self.browser_entries:
            if entry.get("is_default", False):
                command = entry.get("command", "").strip()
                if command:
                    self.launch_browser(command)
                    return

        QMessageBox.information(
            self, "SiteForge",
            "Please select a default browser in Configure browsers."
        )

    def create_toolbar(self):
        self.main_toolbar_tabs = QTabWidget(self)
        self.main_toolbar_tabs.setMovable(False)
        self.main_toolbar_tabs.setDocumentMode(True)

        tab_definitions = [
            ("Standard", (
                "filepanel", "new", "open", "save", "close_all", "undo", "redo", 
            )),
            ("HTML", (
                "insert_paragraph", "insert_h1", "insert_h2",
                "insert_link", "insert_image", "insert_hr",
            )),
            ("Formatting", (
                "insert_bold", "insert_italic", "insert_underline",
                "indent_line", "unindent_line",
                "indent_block", "unindent_block",
            )),
            ("Lists", (
                "insert_ul", "insert_ol",
            )),
            ("Tools", ()),
        ]

        for tab_name, action_names in tab_definitions:
            page = QWidget()
            layout = QHBoxLayout(page)
            layout.setContentsMargins(4, 3, 4, 3)
            layout.setSpacing(3)

            # Special handling for the Tools tab so it can be dynamic
            if "tool" in tab_name.lower():
                # Save a reference to this layout so we can refresh it anytime
                self.tools_tab_layout = layout
                self.refresh_tools_toolbar()
            else:
                # Standard tabs
                for name in action_names:
                    action = self.actions.get(name)
                    if action:
                        button = QToolButton(page)
                        button.setDefaultAction(action)
                        
                        # Give tidy text beside its icon; keep others icon-only
                        if name == "tidy":
                            button.setToolButtonStyle(
                                Qt.ToolButtonStyle.ToolButtonTextBesideIcon
                            )
                        else:
                            button.setToolButtonStyle(
                                Qt.ToolButtonStyle.ToolButtonIconOnly
                            )
                            
                        button.setToolTip(action.text())
                        layout.addWidget(button)
                layout.addStretch()

            self.main_toolbar_tabs.addTab(page, tab_name)

        browser_page = QWidget()
        self.browser_tab_layout = QHBoxLayout(browser_page)
        self.browser_tab_layout.setContentsMargins(4, 3, 4, 3)
        self.browser_tab_layout.setSpacing(6)
        self.browser_tab_layout.addStretch()
        self.main_toolbar_tabs.addTab(browser_page, "Browsers")

        toolbar = QToolBar("Main toolbar", self)
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.addWidget(self.main_toolbar_tabs)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)

        self.refresh_browser_toolbar()

    def refresh_browser_toolbar(self):
        if not hasattr(self, "browser_tab_layout"):
            return

        while self.browser_tab_layout.count():
            item = self.browser_tab_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        icon_dir = Path(__file__).resolve().parent / "icons" / "browsers"

        for entry in self.browser_entries:
            name = entry.get("name", "").strip()
            command = entry.get("command", "").strip()

            if not name or not command:
                continue

            icon_name = entry.get("icon", "browser.png")
            icon_path = icon_dir / icon_name

            if not icon_path.is_file():
                icon_path = icon_dir / "browser.png"

            button = QToolButton()
            button.setText(name)
            button.setIcon(QIcon(str(icon_path)))
            button.setToolButtonStyle(
                Qt.ToolButtonStyle.ToolButtonTextBesideIcon
            )
            button.setToolTip(f"Open current document in {name}")
            button.clicked.connect(
                lambda checked=False, cmd=command: self.launch_browser(cmd)
            )
            self.browser_tab_layout.addWidget(button)

        self.browser_tab_layout.addStretch()

    def refresh_tools_toolbar(self):
        """Refreshes the Tools tab with any custom external tools."""
        if not hasattr(self, "tools_tab_layout"):
            return

        # Clear existing buttons from the layout
        while self.tools_tab_layout.count():
            item = self.tools_tab_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        # Add any custom external tools from your list
        for entry in getattr(self, "external_tool_entries", []):
            name = entry.get("name", "").strip()
            command = entry.get("command", "").strip()

            if not name or not command:
                continue

            button = QToolButton()
            button.setText(name)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setToolTip(f"Run external tool: {name}")
            
            # Connect click to launch the external command
            button.clicked.connect(
                lambda checked=False, e=entry: self.launch_external_tool(e)
            )
            self.tools_tab_layout.addWidget(button)

        self.tools_tab_layout.addStretch()

    def load_external_tools(self):
        """Reads the list of external programs saved in external_tools.json."""
        try:
            data = json.loads(EXTERNAL_TOOLS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

        if not isinstance(data, list):
            return []

        entries = []
        for item in data:
            if not isinstance(item, dict):
                continue

            name = str(item.get("name", "")).strip()
            command = str(item.get("command", "")).strip()

            if name and command:
                entries.append({
                    "name": name,
                    "command": command,
                    "send_file": bool(item.get("send_file", False)),
                })

        return entries[:10]

    def rebuild_external_tools_menu(self):
        """Rebuilds the external program entries in the Tools menu."""
        for action in self.external_tool_menu_actions:
            self.tools_menu.removeAction(action)
            action.deleteLater()

        self.external_tool_menu_actions = []

        for entry in self.external_tool_entries:
            action = QAction(entry["name"], self)
            action.triggered.connect(
                lambda checked=False, e=entry: self.launch_external_tool(e)
            )
            self.tools_menu.insertAction(self.configure_tools_action, action)
            self.external_tool_menu_actions.append(action)

    def configure_external_tools(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Configure external tools")
        dialog.resize(850, 420)
        layout = QVBoxLayout(dialog)

        layout.addWidget(QLabel(
            "Add programs to the Tools menu (and the Tools tab of the "
            "toolbar). Tick 'Open current file' to give the program the "
            "file you are editing when it starts.",
            dialog
        ))

        table = QTableWidget(10, 4, dialog)
        table.setHorizontalHeaderLabels(
            ["Name in menu", "Program", "Browse", "Open current file"]
        )
        table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )

        def browse_program(row):
            path, _ = QFileDialog.getOpenFileName(
                dialog,
                "Select program",
                "/usr/bin",
                "All files (*)"
            )

            if not path:
                return

            table.setItem(row, 1, QTableWidgetItem(path))

            name_item = table.item(row, 0)
            if not name_item or not name_item.text().strip():
                table.setItem(
                    row, 0, QTableWidgetItem(Path(path).name.title())
                )

        for row in range(10):
            entry = (
                self.external_tool_entries[row]
                if row < len(self.external_tool_entries)
                else {}
            )

            table.setItem(row, 0, QTableWidgetItem(entry.get("name", "")))
            table.setItem(row, 1, QTableWidgetItem(entry.get("command", "")))

            browse_button = QPushButton("Browse...")
            browse_button.clicked.connect(
                lambda checked=False, r=row: browse_program(r)
            )
            table.setCellWidget(row, 2, browse_button)

            check_item = QTableWidgetItem()
            check_item.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
            )
            check_item.setCheckState(
                Qt.CheckState.Checked
                if entry.get("send_file", False)
                else Qt.CheckState.Unchecked
            )
            table.setItem(row, 3, check_item)

        layout.addWidget(table)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        entries = []

        for row in range(10):
            name_item = table.item(row, 0)
            command_item = table.item(row, 1)
            check_item = table.item(row, 3)

            name = name_item.text().strip() if name_item else ""
            command = command_item.text().strip() if command_item else ""

            if not command:
                continue

            if not name:
                try:
                    name = Path(shlex.split(command)[0]).name.title()
                except (ValueError, IndexError):
                    name = command

            entries.append({
                "name": name,
                "command": command,
                "send_file": (
                    check_item is not None
                    and check_item.checkState() == Qt.CheckState.Checked
                ),
            })

        try:
            EXTERNAL_TOOLS_FILE.parent.mkdir(parents=True, exist_ok=True)
            EXTERNAL_TOOLS_FILE.write_text(
                json.dumps(entries, indent=4),
                encoding="utf-8"
            )
        except OSError as error:
            QMessageBox.critical(
                self, "SiteForge",
                f"Could not save external tools:\n{error}"
            )
            return

        self.external_tool_entries = entries
        self.rebuild_external_tools_menu()
        self.refresh_tools_toolbar()
        self.status.showMessage("External tools saved", 2500)

    def launch_external_tool(self, entry):
        """Starts an external program from the Tools menu or toolbar."""
        command = entry.get("command", "").strip()
        name = entry.get("name", command)

        try:
            arguments = shlex.split(command)
        except ValueError as error:
            QMessageBox.warning(
                self, "SiteForge",
                f"Could not understand the command for {name}:\n{error}"
            )
            return

        if not arguments:
            return

        if entry.get("send_file", False):
            editor = self.editor()

            if not editor or not editor.path or not editor.path.exists():
                QMessageBox.information(
                    self, "SiteForge",
                    f"Save this file before opening it in {name}."
                )
                return

            arguments.append(str(editor.path))

        try:
            subprocess.Popen(arguments, start_new_session=True)
            self.status.showMessage(f"Started {name}", 2500)
        except FileNotFoundError:
            QMessageBox.warning(
                self, "SiteForge",
                f"Could not find the program for {name}:\n{arguments[0]}"
            )
        except Exception as error:
            QMessageBox.critical(
                self, "SiteForge",
                f"Could not start {name}:\n{error}"
            )

    def editor(self):
        return self.tabs.currentWidget()
        
    def editor_call(self, method):
        editor = self.editor()
        if editor:
            getattr(editor, method)()

    def insert_tag_pair(self, opening, closing):
        editor = self.editor()
        if not editor:
            return

        if editor.hasSelectedText():
            selected = editor.selectedText()
            editor.replaceSelectedText(
                f"{opening}{selected}{closing}"
            )
        else:
            line, index = editor.getCursorPosition()
            editor.insertAt(opening + closing, line, index)
            editor.setCursorPosition(
                line, index + len(opening)
            )

        editor.setFocus()

    def insert_list(self, list_tag):
        editor = self.editor()
        if not editor:
            return

        if editor.hasSelectedText():
            selected = editor.selectedText()
            items = selected.splitlines() or [""]
            list_items = "\n".join(
                f"    <li>{item}</li>" for item in items
            )
            replacement = (
                f"<{list_tag}>\n"
                f"{list_items}\n"
                f"</{list_tag}>"
            )
            editor.replaceSelectedText(replacement)
        else:
            line, index = editor.getCursorPosition()
            replacement = (
                f"<{list_tag}>\n"
                "    <li></li>\n"
                f"</{list_tag}>"
            )
            editor.insertAt(replacement, line, index)
            editor.setCursorPosition(line + 1, 8)

        editor.setFocus()

    def insert_unordered_list(self):
        self.insert_list("ul")

    def insert_ordered_list(self):
        self.insert_list("ol")

    def insert_horizontal_rule(self):
        self.insert_self_closing_tag("<hr>")

    def insert_self_closing_tag(self, tag):
        editor = self.editor()
        if not editor:
            return

        if editor.hasSelectedText():
            editor.replaceSelectedText(tag)
        else:
            line, index = editor.getCursorPosition()
            editor.insertAt(tag, line, index)
            editor.setCursorPosition(
                line, index + len(tag)
            )

        editor.setFocus()

    def insert_link(self):
        editor = self.editor()
        if not editor:
            return

        if editor.path:
            base_dir = editor.path.resolve().parent
        else:
            base_dir = (
                Path(self.last_directory).resolve()
                if self.last_directory
                else Path.home()
            )

        dialog = QDialog(self)
        dialog.setWindowTitle("Insert Link")
        dialog.setModal(True)

        layout = QVBoxLayout(dialog)
        row = QHBoxLayout()

        url_input = QLineEdit(dialog)
        url_input.setPlaceholderText(
            "Enter a URL or browse for a local file..."
        )

        browse_button = QPushButton("Browse...", dialog)
        row.addWidget(url_input, 1)
        row.addWidget(browse_button)
        layout.addLayout(row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=dialog
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def browse_target(_checked=False):
            filename, _ = QFileDialog.getOpenFileName(
                dialog,
                "Select Link Target",
                str(base_dir),
                "Web files (*.html *.htm *.xhtml *.xml "
                "*.css *.js *.pdf *.jpg *.jpeg *.png "
                "*.gif *.webp *.svg);;All files (*)"
            )

            if not filename:
                return

            target = Path(filename).resolve()
            relative = os.path.relpath(
                str(target), str(base_dir)
            ).replace(os.sep, "/")

            url_input.setText(quote(relative, safe="/._~-"))

        browse_button.clicked.connect(browse_target)
        url_input.setFocus()

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        url = url_input.text().strip()
        if not url:
            return

        url = html.escape(url, quote=True)
        opening = f'<a href="{url}">'

        if editor.hasSelectedText():
            selected = editor.selectedText()
            editor.replaceSelectedText(
                f"{opening}{selected}</a>"
            )
        else:
            line, index = editor.getCursorPosition()
            editor.insertAt(opening + "</a>", line, index)
            editor.setCursorPosition(
                line, index + len(opening)
            )

        editor.setFocus()

    def insert_image(self):
        editor = self.editor()
        if not editor:
            return

        if editor.path:
            base_dir = editor.path.resolve().parent
        else:
            base_dir = (
                Path(self.last_directory).resolve()
                if self.last_directory
                else Path.home()
            )

        had_selection = editor.hasSelectedText()
        selected = editor.selectedText() if had_selection else ""
        selection = editor.getSelection() if had_selection else None
        cursor = editor.getCursorPosition()

        dialog = QDialog(self)
        dialog.setWindowTitle("Insert Image")
        dialog.setModal(True)

        layout = QVBoxLayout(dialog)

        source_row = QHBoxLayout()
        source_input = QLineEdit(dialog)
        source_input.setPlaceholderText(
            "Enter an image URL or browse for a file..."
        )
        browse_button = QPushButton("Browse...", dialog)

        source_row.addWidget(source_input, 1)
        source_row.addWidget(browse_button)
        layout.addLayout(source_row)

        preview_label = QLabel("Image preview", dialog)
        preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview_label.setMinimumSize(240, 150)
        preview_label.setMaximumHeight(220)
        preview_label.setStyleSheet("border: 1px solid #999; padding: 4px;")
        layout.addWidget(preview_label)

        dimensions_label = QLabel("", dialog)
        dimensions_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(dimensions_label)

        def update_image_preview():
            source = source_input.text().strip()

            if not source or source.startswith(
                ("http://", "https://", "data:")
            ):
                preview_label.setPixmap(QPixmap())
                preview_label.setText(
                    "Preview unavailable"
                    if source else "Image preview"
                )
                return

            image_path = Path(unquote(source))

            if not image_path.is_absolute():
                image_path = base_dir / image_path

            pixmap = QPixmap(str(image_path))

            if pixmap.isNull():
                preview_label.setPixmap(QPixmap())
                preview_label.setText("Preview unavailable")
                dimensions_label.setText("")
                return

            width = pixmap.width()
            height = pixmap.height()
            dimensions_label.setText(
                f"Image dimensions: {width} × {height} pixels"
            )
            preview_label.setPixmap(
                pixmap.scaled(
                    preview_label.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation
                )
            )

        layout.addWidget(QLabel("Alternative text:"))
        alt_input = QLineEdit(dialog)
        alt_input.setPlaceholderText("Describe the image")
        alt_input.setText(selected)
        layout.addWidget(alt_input)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=dialog
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def browse_image(_checked=False):
            filename, _ = QFileDialog.getOpenFileName(
                dialog,
                "Select Image",
                str(base_dir),
                "Images (*.png *.jpg *.jpeg *.gif *.webp "
                "*.svg *.avif *.bmp *.tif *.tiff);;All files (*)"
            )

            if not filename:
                return

            target = Path(filename).resolve()
            relative = os.path.relpath(
                str(target), str(base_dir)
            ).replace(os.sep, "/")

            source_input.setText(
                quote(relative, safe="/._~-")
            )

        browse_button.clicked.connect(browse_image)
        source_input.textChanged.connect(update_image_preview)

        image_width = 0
        image_height = 0

        source_input.setFocus()

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        source = source_input.text().strip()
        if not source:
            return

        source = html.escape(source, quote=True)
        alt = html.escape(alt_input.text().strip(), quote=True)

        image_path = Path(unquote(source_input.text().strip()))
        if not image_path.is_absolute():
            image_path = base_dir / image_path

        pixmap = QPixmap(str(image_path))
        if not pixmap.isNull():
            image_width = pixmap.width()
            image_height = pixmap.height()

        if image_width and image_height:
            tag = (
                f'<img src="{source}" '
                f'width="{image_width}" height="{image_height}" '
                f'alt="{alt}">'
            )
        else:
            tag = f'<img src="{source}" alt="{alt}">'

        if had_selection and selection:
            editor.setSelection(*selection)
            editor.replaceSelectedText(tag)
        else:
            line, index = cursor
            editor.setCursorPosition(line, index)
            editor.insertAt(tag, line, index)
            editor.setCursorPosition(
                line, index + len(tag)
            )

        editor.setFocus()

    def go_to_line(self):
        editor = self.editor()

        if not editor:
            return

        line_number, accepted = QInputDialog.getInt(
            self,
            "Go to Line",
            "Line number:",
            1,
            1,
            max(1, editor.lines()),
            1
        )

        if not accepted:
            return

        editor.setCursorPosition(line_number - 1, 0)
        editor.ensureLineVisible(line_number - 1)
        editor.setFocus()            

    def update_cursor_position(self, *_):
        editor = self.editor()

        if not editor:
            self.position_label.setText("Line 1, Column 1")
            return

        line, column = editor.getCursorPosition()
        self.position_label.setText(
            f"Line {line + 1}, Column {column + 1}"
        )           

    def show_find_replace(self):
        editor = self.editor()

        if not editor:
            return

        if not hasattr(self, "find_replace_dialog"):
            self.find_replace_dialog = FindReplaceDialog(self)

        if editor.hasSelectedText():
            self.find_replace_dialog.find_input.setText(
                editor.selectedText()
            )

        self.find_replace_dialog.update_match_count()
        self.find_replace_dialog.show()
        self.find_replace_dialog.raise_()
        self.find_replace_dialog.activateWindow()

    def show_replace_dialog(self):
        self.show_find_replace()
        self.find_replace_dialog.replace_input.setFocus()
        
    def find_next_in_dialog(self):
        if not hasattr(self, "find_replace_dialog"):
            self.show_find_replace()
            return

        if not self.find_replace_dialog.isVisible():
            self.find_replace_dialog.show()

        self.find_replace_dialog.find_next()
        
    def find_previous_in_dialog(self):
        if not hasattr(self, "find_replace_dialog"):
            self.show_find_replace()
            return

        if not self.find_replace_dialog.isVisible():
            self.find_replace_dialog.show()

        self.find_replace_dialog.find_previous()                

    def toggle_word_wrap(self, checked):
        mode = (
            QsciScintilla.WrapMode.WrapWord
            if checked
            else QsciScintilla.WrapMode.WrapNone
        )

        for index in range(self.tabs.count()):
            self.tabs.widget(index).setWrapMode(mode)

    def show_help(self):
        """Opens the help file (help/help.html) in the default browser."""
        help_file = Path(__file__).resolve().parent / "help" / "help.html"

        if not help_file.is_file():
            QMessageBox.information(
                self,
                "SiteForge Help",
                "The help file was not found:\n"
                f"{help_file}\n\n"
                "Put help.html in a folder called 'help' next to siteforge.py."
            )
            return

        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(help_file))):
            QMessageBox.warning(
                self,
                "SiteForge Help",
                f"Could not open the help file:\n{help_file}"
            )

    def spell_error(self, message):
        """Tells the user, once per session, that spell checking can't run."""
        if self.spell_error_shown:
            return

        self.spell_error_shown = True
        QMessageBox.warning(self, "Spell checking", message)

    def recheck_all_spelling(self):
        for index in range(self.tabs.count()):
            editor = self.tabs.widget(index)

            if editor:
                editor.run_spell_check()

    def toggle_spell_check(self, checked):
        SPELLER.enabled = checked
        SPELLER.failed = False
        SPELLER.save_settings()

        if checked and not SPELLER.available():
            self.spell_error_shown = True
            QMessageBox.warning(
                self, "Spell checking",
                SPELL_INSTALL_HELP.format(
                    language=SPELLER.language,
                    details="no spell checker program found"
                )
            )

        self.recheck_all_spelling()

    def choose_spell_style(self):
        names = list(SPELL_STYLES)
        current = names.index(SPELLER.style) if SPELLER.style in names else 0

        style, accepted = QInputDialog.getItem(
            self,
            "Spelling highlight style",
            "How should misspelled words be marked?",
            names,
            current,
            False
        )

        if not accepted:
            return

        SPELLER.style = style
        SPELLER.save_settings()

        for index in range(self.tabs.count()):
            editor = self.tabs.widget(index)

            if editor:
                editor.apply_spell_style()
                editor.viewport().update()

    def choose_spelling_language(self):
        language, accepted = QInputDialog.getText(
            self,
            "Spelling language",
            "Dictionary code (for example en_GB or en_US):",
            text=SPELLER.language
        )

        language = language.strip()

        if not accepted or not language:
            return

        if not re.fullmatch(r"[A-Za-z_]{2,20}", language):
            QMessageBox.warning(
                self, "Spell checking",
                "Please enter a dictionary code such as en_GB or en_US."
            )
            return

        SPELLER.set_language(language)
        self.spell_error_shown = False
        self.recheck_all_spelling()
        self.status.showMessage(f"Spelling language set to {language}", 3000)

    def toggle_tag_completion(self, checked):
        for index in range(self.tabs.count()):
            self.tabs.widget(index).tag_completion_enabled = checked

    def indent_line(self):
        editor = self.editor()
        if editor:
            line, _ = editor.getCursorPosition()
            editor.indent(line)

    def unindent_line(self):
        editor = self.editor()
        if editor:
            line, _ = editor.getCursorPosition()
            editor.unindent(line)

    def indent_block(self):
        editor = self.editor()
        if not editor:
            return

        line_from, index_from, line_to, index_to = editor.getSelection()
        if line_from < 0:
            return

        editor.beginUndoAction()
        try:
            for line in range(line_from, line_to + 1):
                editor.indent(line)
        finally:
            editor.endUndoAction()

    def unindent_block(self):
        editor = self.editor()
        if not editor:
            return

        line_from, index_from, line_to, index_to = editor.getSelection()
        if line_from < 0:
            return

        editor.beginUndoAction()
        try:
            for line in range(line_from, line_to + 1):
                editor.unindent(line)
        finally:
            editor.endUndoAction()
            
    def choose_editor_font(self):
        editor = self.editor()
        if not editor:
            return

        chosen_font, accepted = QFontDialog.getFont(
            editor.font(), self, "Choose editor font"
        )

        if not accepted:
            return

        for index in range(self.tabs.count()):
            editor = self.tabs.widget(index)
            editor.setFont(chosen_font)

            lexer = editor.lexer()
            if lexer:
                lexer.setDefaultFont(chosen_font)
                for style in range(128):
                    lexer.setFont(chosen_font, style)

        settings = STATE_FILE

        try:
            data = json.loads(settings.read_text())
        except (OSError, ValueError):
            data = {}

        data["editor_font"] = chosen_font.toString()

        try:
            settings.write_text(json.dumps(data, indent=4))
        except OSError as error:
            QMessageBox.warning(
                self, "SiteForge",
                f"Could not save editor font:\n{error}"
            )

        self.status.showMessage("Editor font saved", 2500)

    def choose_bookmark_color(self):
        editor = self.editor()
        if not editor:
            return

        color = QColorDialog.getColor(
            editor.bookmark_color,
            self,
            "Choose bookmark colour"
        )

        if not color.isValid():
            return

        color_name = color.name()

        for index in range(self.tabs.count()):
            self.tabs.widget(index).set_bookmark_color(color)

        settings = STATE_FILE

        try:
            data = json.loads(settings.read_text())
        except (OSError, ValueError):
            data = {}

        data["bookmark_color"] = color_name

        try:
            settings.write_text(json.dumps(data, indent=4))
        except OSError as error:
            QMessageBox.warning(
                self, "SiteForge",
                f"Could not save bookmark colour:\n{error}"
            )

        self.status.showMessage("Bookmark colour saved", 2500)

    def choose_tag_pair_color(self):
        editor = self.editor()
        if not editor:
            return

        color = QColorDialog.getColor(
            editor.tag_pair_color,
            self,
            "Choose tag-pair highlight colour"
        )

        if not color.isValid():
            return

        for index in range(self.tabs.count()):
            tab = self.tabs.widget(index)
            tab.tag_pair_color = QColor(color)
            tab.setIndicatorForegroundColor(color, tab.tag_indicator)

        settings = STATE_FILE

        try:
            data = json.loads(settings.read_text())
        except (OSError, ValueError):
            data = {}

        data["tag_pair_color"] = color.name()

        try:
            settings.write_text(json.dumps(data, indent=4))
        except OSError as error:
            QMessageBox.warning(
                self,
                "SiteForge",
                f"Could not save tag-pair colour:\n{error}"
            )

        self.status.showMessage(
            "Tag-pair highlight colour saved", 2500
        )

    def choose_current_line_color(self):
        editor = self.editor()
        if not editor:
            return

        color = QColorDialog.getColor(
            editor.current_line_color,
            self,
            "Choose current-line highlight colour"
        )

        if not color.isValid():
            return

        for index in range(self.tabs.count()):
            tab = self.tabs.widget(index)
            tab.current_line_color = QColor(color)
            tab.setCaretLineBackgroundColor(color)

        settings = STATE_FILE

        try:
            data = json.loads(settings.read_text())
        except (OSError, ValueError):
            data = {}

        data["current_line_color"] = color.name()

        try:
            settings.write_text(json.dumps(data, indent=4))
        except OSError as error:
            QMessageBox.warning(
                self,
                "SiteForge",
                f"Could not save current-line colour:\n{error}"
            )

        self.status.showMessage(
            "Current-line highlight colour saved", 2500
        )

    def new_file(self):
        template_directory = TEMPLATES_DIR
        template_directory.mkdir(parents=True, exist_ok=True)

        default_template = template_directory / "HTML5 Standard.html"

        boilerplate = (
            '<!DOCTYPE html>\n'
            '<html lang="en">\n'
            '<head>\n'
            '<meta charset="UTF-8">\n'
            '\n'
            '<title>Page Title</title>\n'
            '\n'
            '\t<!-- Responsive and mobile friendly stuff -->\n'
            '\t<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '\t<meta name="description" content="Description of website.">\n'
            '\t<meta name="keywords" content="List, of, keywords">\n'
            '\t<meta name="HandheldFriendly" content="True">\n'
            '\t<meta name="MobileOptimized" content="320">\n'
            '\t\n'
            '\t<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">\n'
            '\t<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32x32.png">\n'
            '\t<link rel="icon" type="image/png" sizes="16x16" href="/favicon-16x16.png">\n'
            '\t<link rel="manifest" href="/site.webmanifest">\n'
            '\n'
            '\t<link rel="stylesheet" href="css/styles.css">\n'
            '\n'
            '</head>\n'
            '<body>\n'
            '\n'
            '\n'
            '\n'
            '</body>\n'
            '</html>'
        )

        if not default_template.exists():
            default_template.write_text(boilerplate, encoding="utf-8")

        settings = STATE_FILE

        try:
            data = json.loads(settings.read_text())
        except (OSError, ValueError):
            data = {}

        selected_template = Path(
            data.get("default_template", str(default_template))
        )

        if not selected_template.is_file():
            selected_template = default_template

        try:
            template_text = selected_template.read_text(encoding="utf-8")
        except OSError as error:
            QMessageBox.warning(
                self,
                "SiteForge",
                f"Could not load the default template:\n{error}"
            )
            template_text = ""

        editor = CodeEditor()
        editor.cursorPositionChanged.connect(self.update_cursor_position)

        if self.actions["wordwrap"].isChecked():
            editor.setWrapMode(QsciScintilla.WrapMode.WrapWord)
        
        editor.tag_completion_enabled = self.actions["tag_completion"].isChecked()

        editor.modificationChanged.connect(
            lambda modified, e=editor: self.update_tab_label(e)
        )

        index = self.tabs.addTab(editor, "Untitled")
        self.tabs.setCurrentIndex(index)
        editor.setText(template_text)
        editor.setModified(False)
        editor.setFocus()

    def choose_indentation_settings(self):
        settings = STATE_FILE

        try:
            data = json.loads(settings.read_text())
        except (OSError, ValueError):
            data = {}

        current_tabs = data.get("indent_use_tabs", False)
        current_width = data.get("indent_width", 4)

        dialog = QDialog(self)
        dialog.setWindowTitle("Indentation Settings")

        layout = QFormLayout(dialog)

        mode_box = QComboBox()
        mode_box.addItem("Spaces", False)
        mode_box.addItem("Tabs", True)
        mode_box.setCurrentIndex(
            mode_box.findData(bool(current_tabs))
        )

        width_box = QSpinBox()
        width_box.setRange(1, 8)
        width_box.setValue(
            max(1, min(8, int(current_width)))
        )

        layout.addRow("Indent using:", mode_box)
        layout.addRow("Indent width:", width_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        data["indent_use_tabs"] = mode_box.currentData()
        data["indent_width"] = width_box.value()

        try:
            settings.write_text(json.dumps(data, indent=4))
        except OSError as error:
            QMessageBox.warning(
                self,
                "SiteForge",
                f"Could not save indentation settings:\n{error}"
            )
            return

        use_tabs = bool(data["indent_use_tabs"])
        indent_width = data["indent_width"]

        for index in range(self.tabs.count()):
            editor = self.tabs.widget(index)
            editor.setIndentationsUseTabs(use_tabs)
            editor.setTabWidth(indent_width)
            editor.setIndentationWidth(indent_width)

        self.status.showMessage(
            "Indentation settings saved and applied.",
            4000
        )

    def choose_default_template(self):
        template_directory = TEMPLATES_DIR
        template_directory.mkdir(parents=True, exist_ok=True)

        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose Default Template",
            str(template_directory),
            "HTML Templates (*.html);;All files (*)"
        )

        if not path:
            return

        settings = STATE_FILE

        try:
            data = json.loads(settings.read_text())
        except (OSError, ValueError):
            data = {}

        data["default_template"] = str(Path(path).resolve())

        try:
            settings.write_text(json.dumps(data, indent=4))
        except OSError as error:
            QMessageBox.warning(
                self,
                "SiteForge",
                f"Could not save default template:\n{error}"
            )
            return

        self.status.showMessage(
            f"Default template set to {Path(path).name}", 3000
        )

    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open source file", self.last_directory,
            "Web files (*.html *.htm *.xhtml *.xml *.svg "
            "*.css *.js *.mjs *.txt);;All files (*)"
        )

        if path:
            self.open_path(path)

    def open_path(self, path, record_recent=True):
        target = Path(path).resolve()
        self.remember_directory(target.parent)
        
        for index in range(self.tabs.count()):
            editor = self.tabs.widget(index)

            if editor.path == target:
                self.tabs.setCurrentIndex(index)

                if record_recent:
                    self.add_recent_file(target)

                return

        try:
            editor = CodeEditor(target)
            editor.cursorPositionChanged.connect(self.update_cursor_position)
            if self.actions["wordwrap"].isChecked():
                editor.setWrapMode(QsciScintilla.WrapMode.WrapWord)
            
            editor.tag_completion_enabled = self.actions["tag_completion"].isChecked()
                
            editor.modificationChanged.connect(
                lambda modified, e=editor: self.update_tab_label(e)
            )

            index = self.tabs.addTab(editor, target.name)
            self.tabs.setCurrentIndex(index)
            if record_recent:
                self.add_recent_file(target)

        except Exception as error:
            QMessageBox.critical(
                self, "SiteForge", f"Could not open file:\n{error}"
            )

    def open_tree_file(self, index):
        path = self.file_model.filePath(index)

        if Path(path).is_file():
            self.open_path(path)

    def save_file(self):
        editor = self.editor()
        if not editor:
            return

        if not editor.path:
            self.save_as()
            return

        try:
            editor.save_file()
            self.update_tab_label(editor)
            self.status.showMessage("File saved", 2500)

        except Exception as error:
            QMessageBox.critical(
                self, "SiteForge", f"Could not save file:\n{error}"
            )

    def save_all(self):
        saved_count = 0
        failed = False

        for index in range(self.tabs.count()):
            editor = self.tabs.widget(index)

            if not editor or not editor.isModified():
                continue

            if not editor.path:
                self.tabs.setCurrentIndex(index)
                self.save_as()

                if not editor.path or editor.isModified():
                    failed = True
                else:
                    saved_count += 1

                continue

            try:
                if editor.save_file():
                    self.update_tab_label(editor)
                    saved_count += 1
                else:
                    failed = True

            except Exception as error:
                QMessageBox.critical(
                    self,
                    "SiteForge",
                    f"Could not save {editor.path}:\n{error}"
                )
                failed = True

        if failed:
            self.status.showMessage(
                "Save All finished with unsaved file(s)", 4000
            )
        else:
            self.status.showMessage(
                f"Saved {saved_count} changed file(s)", 2500
            )

    def save_as(self):
        editor = self.editor()
        if not editor:
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Save source file", self.last_directory,
            "HTML (*.html);;CSS (*.css);;JavaScript (*.js);;All files (*)"
        )

        if path:
            try:
                editor.save_file(path)
                self.remember_directory(Path(path).parent)
                self.update_tab_label(editor)

            except Exception as error:
                QMessageBox.critical(
                    self, "SiteForge",
                    f"Could not save file:\n{error}"
                )

    def save_as_template(self):
        editor = self.editor()
        if not editor:
            return

        template_directory = TEMPLATES_DIR
        template_directory.mkdir(parents=True, exist_ok=True)

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save as Template",
            str(template_directory / "my-template.html"),
            "HTML Templates (*.html);;All files (*)"
        )

        if not path:
            return

        try:
            Path(path).write_text(editor.text(), encoding="utf-8")
            self.status.showMessage("Template saved", 2500)

        except OSError as error:
            QMessageBox.warning(
                self,
                "SiteForge",
                f"Could not save template:\n{error}"
            )

    def new_from_template(self):
        template_directory = TEMPLATES_DIR

        path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose Template",
            str(template_directory),
            "HTML Templates (*.html);;All files (*)"
        )

        if not path:
            return

        try:
            template_text = Path(path).read_text(encoding="utf-8")
        except OSError as error:
            QMessageBox.warning(
                self,
                "SiteForge",
                f"Could not open template:\n{error}"
            )
            return

        self.new_file()
        editor = self.editor()
        editor.setText(template_text)
        editor.setModified(False)
        editor.setFocus()

    def update_tab_label(self, editor):
        index = self.tabs.indexOf(editor)

        if index != -1:
            name = editor.path.name if editor.path else "Untitled"

            if editor.isModified():
                name = "*" + name
                self.tabs.tabBar().setTabTextColor(index, QColor("red"))
            else:
                self.tabs.tabBar().setTabTextColor(index, QColor())

            self.tabs.setTabText(index, name)

        self.update_title()

    def update_title(self, *_):
        editor = self.editor()

        if not editor:
            return

        name = str(editor.path) if editor.path else "Untitled"
        if editor.isModified():
            name = "*" + name

        self.setWindowTitle(f"SiteForge — {name}")

        if not editor.path or not hasattr(self, "file_tree"):
            return

        folder = editor.path.parent.resolve()
        home = Path.home().resolve()

        try:
            folder.relative_to(home)
            tree_root = home
        except ValueError:
            tree_root = folder.parent

        self.file_model.setRootPath(str(tree_root))
        self.file_tree.setRootIndex(
            self.file_model.index(str(tree_root))
        )

        folder_index = self.file_model.index(str(folder))
        
        if folder_index.isValid():
            tree_root_index = self.file_tree.rootIndex()

            if folder_index != tree_root_index:
                parent_index = folder_index.parent()

                while parent_index.isValid():
                    self.file_tree.expand(parent_index)

                    if parent_index == tree_root_index:
                        break

                    parent_index = parent_index.parent()

            self.file_tree.setCurrentIndex(folder_index)
            self.file_tree.scrollTo(folder_index)

        self.remember_directory(str(folder))

    def closeEvent(self, event):
        for index in range(self.tabs.count()):
            editor = self.tabs.widget(index)

            if not editor or not editor.isModified():
                continue

            self.tabs.setCurrentIndex(index)

            answer = QMessageBox.question(
                self,
                "Unsaved Changes",
                f"Save changes to {editor.path or 'untitled document'} before closing?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel
            )

            if answer == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return

            if answer == QMessageBox.StandardButton.Save:
                self.save_file()

                if editor.isModified():
                    event.ignore()
                    return
        self.save_session()
        event.accept()

    def close_tab(self, index):
        editor = self.tabs.widget(index)

        if not editor:
            return

        if editor.isModified():
            answer = QMessageBox.question(
                self, "SiteForge",
                "Save changes before closing?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel
            )

            if answer == QMessageBox.StandardButton.Cancel:
                return

            if answer == QMessageBox.StandardButton.Save:
                self.tabs.setCurrentIndex(index)
                self.save_file()

                if editor.isModified():
                    return
        self.tabs.removeTab(index)
        editor.deleteLater()

    def close_all_tabs(self):
        index = self.tabs.count() - 1

        while index >= 0:
            count_before = self.tabs.count()
            self.tabs.setCurrentIndex(index)
            self.close_tab(index)

            if self.tabs.count() == count_before:
                return

            index -= 1

    def snippets_file(self):
        directory = os.path.join(
            os.path.expanduser("~"),
            ".config",
            "SiteForge",
            "snippets"
        )
        os.makedirs(directory, exist_ok=True)
        return os.path.join(directory, "snippets.json")

    def load_snippets(self):
        path = self.snippets_file()

        if not os.path.isfile(path):
            return []

        try:
            with open(path, "r", encoding="utf-8") as file:
                snippets = json.load(file)
        except (OSError, json.JSONDecodeError):
            return []

        return snippets if isinstance(snippets, list) else []

    def save_snippets(self, snippets):
        path = self.snippets_file()

        try:
            with open(path, "w", encoding="utf-8") as file:
                json.dump(
                    snippets,
                    file,
                    indent=4,
                    ensure_ascii=False
                )
        except OSError as exc:
            QMessageBox.warning(
                self,
                "Snippets",
                f"Could not save snippets:\n{exc}"
            )

    def refresh_snippet_list(self):
        if not hasattr(self, "snippet_list"):
            return

        self.snippet_list.clear()
        snippets = self.load_snippets()
        branches = {}

        for snippet in snippets:
            branch = snippet.get("branch", "").strip() or "General"
            branches.setdefault(branch, []).append(snippet)

        for branch_name, branch_snippets in sorted(branches.items()):
            branch_item = QTreeWidgetItem(self.snippet_list, [branch_name])
            branch_item.setExpanded(True)
            for snippet in branch_snippets:
                item = QTreeWidgetItem(branch_item, [snippet["name"]])
                item.setData(0, Qt.ItemDataRole.UserRole, snippet)

        self.register_snippet_shortcuts()

    def register_snippet_shortcuts(self):
        """Makes each snippet's Shortcut key insert that snippet."""
        for old in getattr(self, "snippet_shortcuts", []):
            old.setEnabled(False)
            old.setParent(None)
            old.deleteLater()

        self.snippet_shortcuts = []

        # Keys already used by menu commands win; a snippet can't take them
        # (self.actions is only SiteForge's own command list once it has been
        # created; before that the name belongs to a built-in Qt function)
        commands = getattr(self, "actions", None)

        if not isinstance(commands, dict):
            commands = {}

        taken = {
            action.shortcut().toString()
            for action in commands.values()
            if not action.shortcut().isEmpty()
        }

        for snippet in self.load_snippets():
            text = str(snippet.get("shortcut", "")).strip()

            if not text:
                continue

            keys = QKeySequence(text)

            if keys.isEmpty() or keys.toString() in taken:
                continue

            taken.add(keys.toString())

            shortcut = QShortcut(keys, self)
            shortcut.activated.connect(
                lambda s=snippet: self.insert_snippet_content(s)
            )
            self.snippet_shortcuts.append(shortcut)

    def insert_selected_snippet(self, item, column=0):
        snippet = item.data(0, Qt.ItemDataRole.UserRole)
        if snippet:
            self.insert_snippet_content(snippet)

    def insert_snippet_content(self, snippet):
        editor = self.editor()
        if not editor:
            QMessageBox.information(
                self,
                "Snippets",
                "Open a document before inserting a snippet."
            )
            return

        before = snippet.get("before", "")
        after = snippet.get("after", "")

        if editor.hasSelectedText():
            selected = editor.selectedText()
            editor.replaceSelectedText(
                before + selected + after
            )
        else:
            line, column = editor.getCursorPosition()
            editor.insertAt(
                before + after, line, column
            )

            new_line = line + before.count("\n")
            if "\n" in before:
                new_column = len(before.rsplit("\n", 1)[-1])
            else:
                new_column = column + len(before)

            editor.setCursorPosition(new_line, new_column)

        editor.setFocus()

    def manage_snippets(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Manage Snippets")
        dialog.resize(550, 400)

        snippets = self.load_snippets()

        for snippet in snippets:
            if "before" not in snippet:
                snippet["before"] = snippet.pop("code", "")
            snippet.setdefault("after", "")
            snippet.setdefault("branch", "")

        layout = QVBoxLayout(dialog)
        snippet_list = QTreeWidget()
        snippet_list.setHeaderHidden(True)
        layout.addWidget(snippet_list)

        def refresh_list(select_index=None):
            snippet_list.clear()
            branches = {}
            for index, snippet in enumerate(snippets):
                branch = snippet.get("branch", "").strip() or "General"
                branches.setdefault(branch, []).append((index, snippet))

            for branch_name, branch_snippets in sorted(branches.items()):
                branch_item = QTreeWidgetItem(snippet_list, [branch_name])
                branch_item.setExpanded(True)
                for index, snippet in branch_snippets:
                    item = QTreeWidgetItem(branch_item, [snippet["name"]])
                    item.setData(0, Qt.ItemDataRole.UserRole, index)

                    if index == select_index:
                        snippet_list.setCurrentItem(item)

        def snippet_dialog(title, snippet=None):
            edit_dialog = QDialog(dialog)
            edit_dialog.setWindowTitle(title)
            edit_dialog.resize(500, 500)

            form = QVBoxLayout(edit_dialog)

            form.addWidget(QLabel("Branch / Category"))
            branch_edit = QLineEdit()
            form.addWidget(branch_edit)

            form.addWidget(QLabel("Name"))
            name_edit = QLineEdit()
            form.addWidget(name_edit)

            form.addWidget(QLabel("Shortcut"))
            shortcut_edit = QKeySequenceEdit()
            form.addWidget(shortcut_edit)

            form.addWidget(QLabel("Before text"))
            before_edit = QTextEdit()
            before_edit.setMinimumHeight(80)
            form.addWidget(before_edit)

            form.addWidget(QLabel("After text"))
            after_edit = QTextEdit()
            after_edit.setMinimumHeight(80)
            form.addWidget(after_edit)

            if snippet:
                branch_edit.setText(snippet.get("branch", ""))
                name_edit.setText(snippet["name"])
                shortcut_edit.setKeySequence(
                    QKeySequence(snippet.get("shortcut", ""))
                )
                before_edit.setPlainText(snippet.get("before", ""))
                after_edit.setPlainText(snippet.get("after", ""))

            buttons = QHBoxLayout()
            save_button = QPushButton("Save")
            cancel_button = QPushButton("Cancel")

            def save():
                if not name_edit.text().strip():
                    QMessageBox.warning(
                        edit_dialog,
                        "Snippets",
                        "Please enter a snippet name."
                    )
                    return

                new_key = shortcut_edit.keySequence().toString()

                if new_key:
                    for action in self.actions.values():
                        if action.shortcut().toString() == new_key:
                            QMessageBox.warning(
                                edit_dialog,
                                "Snippets",
                                f'"{new_key}" is already used by the command '
                                f'"{action.text().replace("&", "")}". '
                                'Please choose a different shortcut.'
                            )
                            return

                    for other in snippets:
                        if (
                            other is not snippet
                            and other.get("shortcut", "") == new_key
                        ):
                            QMessageBox.warning(
                                edit_dialog,
                                "Snippets",
                                f'"{new_key}" is already used by the snippet '
                                f'"{other.get("name", "")}". '
                                'Please choose a different shortcut.'
                            )
                            return

                edit_dialog.accept()

            save_button.clicked.connect(save)
            cancel_button.clicked.connect(edit_dialog.reject)

            buttons.addWidget(save_button)
            buttons.addWidget(cancel_button)
            form.addLayout(buttons)

            if edit_dialog.exec() != QDialog.DialogCode.Accepted:
                return None

            return {
                "branch": branch_edit.text().strip(),
                "name": name_edit.text().strip(),
                "shortcut": shortcut_edit.keySequence().toString(),
                "before": before_edit.toPlainText(),
                "after": after_edit.toPlainText()
            }

        def add_snippet():
            snippet = snippet_dialog("Add Snippet")
            if snippet is None:
                return

            snippets.append(snippet)
            self.save_snippets(snippets)
            self.refresh_snippet_list()            
            refresh_list()

        def edit_snippet():
            item = snippet_list.currentItem()
            if not item:
                return
            index = item.data(0, Qt.ItemDataRole.UserRole)
            if index is None:
                return

            snippet = snippet_dialog(
                "Edit Snippet", snippets[index]
            )
            if snippet is None:
                return

            snippets[index] = snippet
            self.save_snippets(snippets)
            self.refresh_snippet_list()
            refresh_list()

        def delete_snippet():
            item = snippet_list.currentItem()
            if not item:
                return
            index = item.data(0, Qt.ItemDataRole.UserRole)
            if index is None:
                return

            answer = QMessageBox.question(
                dialog,
                "Delete Snippet",
                f"Delete '{snippets[index]['name']}'?"
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

            snippets.pop(index)
            self.save_snippets(snippets)
            self.refresh_snippet_list()
            refresh_list()

        def insert_snippet():
            item = snippet_list.currentItem()
            if not item:
                return
            index = item.data(0, Qt.ItemDataRole.UserRole)
            if index is None:
                return

            editor = self.editor()
            if not editor:
                QMessageBox.information(
                    dialog,
                    "Snippets",
                    "Open a document before inserting a snippet."
                )
                return

            snippet = snippets[index]
            self.insert_snippet_content(snippet)
            dialog.accept()

        def branch_key(snippet):
            return snippet.get("branch", "").strip() or "General"

        def move_snippet(direction):
            """Moves the selected snippet up (-1) or down (+1) within its branch."""
            item = snippet_list.currentItem()
            if not item:
                return
            index = item.data(0, Qt.ItemDataRole.UserRole)
            if index is None:
                return

            same_branch = [
                i for i, other in enumerate(snippets)
                if branch_key(other) == branch_key(snippets[index])
            ]
            position = same_branch.index(index) + direction

            if position < 0 or position >= len(same_branch):
                return

            target = same_branch[position]
            snippets[index], snippets[target] = snippets[target], snippets[index]

            self.save_snippets(snippets)
            self.refresh_snippet_list()
            refresh_list(select_index=target)

        def rename_branch(*_):
            item = snippet_list.currentItem()
            if not item:
                QMessageBox.information(
                    dialog, "Snippets",
                    "Click a branch (or a snippet in it) first."
                )
                return

            # A snippet is selected: use the branch it sits in
            branch_item = item.parent() if item.parent() else item
            old_name = branch_item.text(0)

            new_name, accepted = QInputDialog.getText(
                dialog,
                "Rename Branch",
                "Branch name:",
                text=old_name
            )

            new_name = new_name.strip()

            if not accepted or not new_name or new_name == old_name:
                return

            existing = {branch_key(snippet) for snippet in snippets}

            if new_name in existing:
                answer = QMessageBox.question(
                    dialog,
                    "Rename Branch",
                    f"A branch called '{new_name}' already exists.\n\n"
                    "Move these snippets into it?"
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return

            for snippet in snippets:
                if branch_key(snippet) == old_name:
                    snippet["branch"] = new_name

            self.save_snippets(snippets)
            self.refresh_snippet_list()
            refresh_list()

        def branch_double_clicked(item, column):
            # Double-clicking a branch heading renames it
            if item.parent() is None:
                rename_branch()

        snippet_list.itemDoubleClicked.connect(branch_double_clicked)

        buttons = QHBoxLayout()

        for label, callback in (
            ("Add", add_snippet),
            ("Edit", edit_snippet),
            ("Delete", delete_snippet),
            ("Insert", insert_snippet),
            ("Close", dialog.reject),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            buttons.addWidget(button)

        layout.addLayout(buttons)

        order_buttons = QHBoxLayout()

        for label, callback in (
            ("Move Up", lambda: move_snippet(-1)),
            ("Move Down", lambda: move_snippet(1)),
            ("Rename Branch...", rename_branch),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            order_buttons.addWidget(button)

        layout.addLayout(order_buttons)

        refresh_list()
        dialog.exec()

    def setup_tidy_tool(self):
        """Sets up the Check HTML with Tidy action and button icon."""
        self.tidy_action = QAction("Run HTML Tidy", self)
        
        from pathlib import Path
        base_dir = Path(__file__).resolve().parent
        
        # Check a few common places where the icon might live
        possible_paths = [
            base_dir / "menu" / "tidy.png",
            base_dir / "icons" / "tidy.png",
            base_dir / "icons" / "menu" / "tidy.png"
        ]
        
        icon_found = False
        for icon_path in possible_paths:
            if icon_path.is_file():
                self.tidy_action.setIcon(QIcon(str(icon_path)))
                icon_found = True
                break
                
        if not icon_found:
            print("Notice: Tidy icon image not found, using text/default fallback.")
        
        self.tidy_action.triggered.connect(self.run_html_tidy)
        
        self.actions = getattr(self, 'actions', {})
        self.actions["tidy"] = self.tidy_action

        # Tidy is created after the other actions, so it has to be given
        # its default shortcut entry and its saved shortcut here
        self.default_shortcuts["tidy"] = ""
        self.tidy_action.setShortcutContext(
            Qt.ShortcutContext.ApplicationShortcut
        )
        self.addAction(self.tidy_action)

        try:
            saved = json.loads(
                self.shortcut_settings_file.read_text(encoding="utf-8")
            )
            if "tidy" in saved:
                self.tidy_action.setShortcut(saved["tidy"])
        except (OSError, ValueError):
            pass

    def run_html_tidy(self):
        """Executes the HTML check on the currently active editor tab using HTML Tidy."""

        current_editor = self.editor() if hasattr(self, 'editor') else None
        
        if current_editor is None:
            self.log_message("Active editor not found for Tidy check.")
            return

        html_content = current_editor.text()
        
        if not html_content.strip():
            self.log_message("The editor is empty. Nothing to tidy.")
            return

        self.log_message("--- Running HTML Tidy check ---")
        
        try:
            import subprocess
            # Run html tidy, passing the editor content via stdin
            process = subprocess.run(
                ["tidy", "-q", "-errors"],
                input=html_content,
                text=True,
                capture_output=True
            )
            
            # Tidy reports errors and warnings on stderr
            errors = process.stderr.strip()
            output = process.stdout.strip()
            
            if errors:
                self.log_message("Tidy Report:\n" + errors)
            elif output:
                self.log_message("Tidy Output:\n" + output)
            else:
                self.log_message("HTML Tidy check completed: No errors or warnings found! ✨")
                
        except FileNotFoundError:
            self.log_message("Error: 'tidy' command-line tool is not installed or not found in system PATH.")
            self.log_message("Tip: You can install it via your terminal using 'sudo apt install tidy'.")
        except Exception as e:
            self.log_message(f"An unexpected error occurred while running Tidy: {e}")

    def create_output_panel(self):
        """Creates the bottom output/message dock panel.

        The panel has its own title bar with the title on the left and the
        close button (close-2.png) in the top-right corner, in the same way
        as the Explorer panel.
        """
        base_dir = Path(__file__).resolve().parent

        self.output_dock = QDockWidget("Output / Messages", self)
        self.output_dock.setAllowedAreas(
            Qt.DockWidgetArea.BottomDockWidgetArea |
            Qt.DockWidgetArea.TopDockWidgetArea
        )

        # --- Custom title bar: title on the left, close button in the corner ---
        title_bar = QWidget(self.output_dock)
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(6, 2, 4, 2)

        title_label = QLabel("Output / Messages", title_bar)
        title_layout.addWidget(title_label)
        title_layout.addStretch()

        # Look for close-2.png in the usual icon folders
        close_icon = None
        for folder in (
            base_dir / "icons" / "menu",
            base_dir / "icons",
            base_dir / "menu",
        ):
            candidate = folder / "close-2.png"
            if candidate.is_file():
                icon = QIcon(str(candidate))
                if not icon.isNull():
                    close_icon = icon
                    break

        output_close_btn = QToolButton(title_bar)
        if close_icon is not None:
            output_close_btn.setIcon(close_icon)
            output_close_btn.setIconSize(QSize(20, 20))
        else:
            # Never leave an invisible button if the icon can't be found
            output_close_btn.setText("✕")
            print("Notice: close-2.png not found, using text fallback.")
        output_close_btn.setAutoRaise(True)
        output_close_btn.setToolTip("Close output window")
        output_close_btn.clicked.connect(self.output_dock.hide)
        title_layout.addWidget(output_close_btn)

        self.output_dock.setTitleBarWidget(title_bar)
        # -----------------------------------------------------------------------

        # Container widget to hold the action bar and the text box
        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Action bar at the top of the output panel
        top_bar_layout = QHBoxLayout()

        run_tidy_btn = QPushButton("Run HTML Tidy", self)
        if hasattr(self, 'actions') and "tidy" in self.actions:
            run_tidy_btn.setIcon(self.actions["tidy"].icon())
        run_tidy_btn.clicked.connect(self.run_html_tidy)

        top_bar_layout.addWidget(run_tidy_btn)
        top_bar_layout.addStretch()

        layout.addLayout(top_bar_layout)

        # Text area for logs and error messages
        self.output_text = ClickableOutput(self, self.go_to_line_column)
        self.output_text.setReadOnly(True)
        font = QFont("Monospace", 10)
        self.output_text.setFont(font)

        layout.addWidget(self.output_text)
        self.output_dock.setWidget(container)

        # Add it to the bottom of the main window by default
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.output_dock)
        self.output_dock.hide()
        
    def go_to_line_column(self, line_number, column):
        """Moves the cursor in the current editor to a 1-based line and column."""
        editor = self.editor()
        if not editor:
            return

        line = max(0, min(line_number - 1, editor.lines() - 1))
        index = max(0, min(column - 1, len(editor.text(line).rstrip("\r\n"))))

        editor.setCursorPosition(line, index)
        editor.ensureLineVisible(line)
        editor.setFocus()

    def log_message(self, message):
        """Appends a message to the bottom output panel."""
        if hasattr(self, 'output_text'):
            # Insert as plain text so tags such as <div> in Tidy's messages
            # are shown literally and every report line is its own line
            self.output_text.moveCursor(QTextCursor.MoveOperation.End)
            self.output_text.insertPlainText(message + "\n")
            self.output_text.moveCursor(QTextCursor.MoveOperation.End)
            # Ensure the dock is visible when messages arrive
            if hasattr(self, 'output_dock'):
                self.output_dock.show()

app = QApplication(sys.argv)
app.setApplicationName("SiteForge")
app_icon_file = Path(__file__).resolve().parent / "icons" / "siteforge-icon.png"
app.setWindowIcon(QIcon(str(app_icon_file)))
palette = app.palette()
palette.setColor(QPalette.ColorRole.Window, QColor("#efefef"))
palette.setColor(QPalette.ColorRole.Button, QColor("#efefef"))
app.setPalette(palette)

window = SiteForge()

if window.restore_maximized:
    window.showMaximized()
else:
    window.show()

sys.exit(app.exec())

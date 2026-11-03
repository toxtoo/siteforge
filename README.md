[README.md](https://github.com/user-attachments/files/34310701/README.md)
Editor for Linux

# <img width="292" height="62" alt="SiteForge" src="https://github.com/user-attachments/assets/de38ff87-67be-4044-a1ff-87f15511a4b0" />

is a fast, no-nonsense HTML editor for Linux, written in Python with PyQt6 and QScintilla. It is built for people who write web pages by hand and want a tidy, familiar tool: syntax colouring, tag helpers, an HTML checker, spell checking and one-click previews in all of your browsers.

Press **F1** inside SiteForge at any time to open the built-in help.

---




## Features

**Editing**
- Syntax colouring for HTML, XHTML, XML, SVG, CSS and JavaScript
- Tabbed editing, with your open files, cursor positions, bookmarks and window layout restored on the next start
- Tag-pair highlighting: put the cursor in a tag and its matching opening or closing tag is highlighted
- Tag completion: typing `<div>` adds `</div>` and leaves the cursor in between (switch it on or off in **Settings**; tags such as `<br>` and `<img>` are skipped)
- Find, Replace, Replace All, Find Next / Previous and Go to Line
- Indent and unindent for single lines and selected blocks, with your choice of spaces or tabs and indent width
- Bookmarks in the margin, word wrap, and your choice of editor font and highlight colours
- Word suggestions from the text already in your document as you type

**Inserting HTML**
- Insert menu and toolbar for paragraphs, headings, bold, italic, underline, div, lists, horizontal rule, links and images
- Link and image dialogs that work out the correct relative path to your file, and fill in the image's `width` and `height` for you
- Drag a file from the Explorer panel into the editor to insert an `<img>`, a link, an `@import` or a `url(...)` with the right relative path
- Snippets: reusable pieces of text and code, grouped by category, that can also wrap around selected text
- Templates: start new pages from a template of your own, with a configurable default

**Checking**
- **HTML Tidy** integration: runs on the page you are editing (even unsaved), shows the report in an output panel, and **clicking a message jumps to that line and column**
- **Spell checking** for the visible text of your page: tags, attributes, scripts and styles are ignored. Right-click a word for suggestions, Ignore, or Add to dictionary. Choose the language and how misspelled words are marked
- **Document Weight**: adds up the size of a page plus its local images, stylesheets and scripts

**Previewing and tools**
- Open the current page in up to ten browsers, with shortcuts for the first three and `F12` for your default
- Add your own programs (a graphics editor such as GIMP, for example) to the **Tools** menu and toolbar, optionally passing them the file you are editing
- Explorer panel with file-type icons, a Snippets panel, and an output panel
- Customisable keyboard shortcuts for every command
- One-click **Backup Configuration** to a ZIP file

---

## Installation

### Option 1: standalone download (easiest)

1. Download `siteforge-linux-vX.Y.tar.gz` from the [Releases](../../releases) page.
2. Extract it, open a terminal in the extracted folder and run:

   ```
   ./install.sh
   ```

3. SiteForge appears in your applications menu. To remove it later, run `./install.sh --uninstall`.

The standalone build is for 64-bit Linux and includes everything it needs, so you do not need to install Python or Qt. It was built on Linux Mint (Debian-based); it may not run on much older distributions.

### Option 2: run from source

You need Python 3 and these packages:

```
pip install PyQt6 PyQt6-QScintilla
```

On Debian-based systems you may prefer to do this inside a virtual environment:

```
sudo apt install python3-venv
python3 -m venv venv
./venv/bin/pip install PyQt6 PyQt6-QScintilla
./venv/bin/python3 siteforge.py
```

### Optional programs

SiteForge works without these, but two features rely on them:

| Feature | Needs | Install on Debian / Ubuntu / Mint |
|---|---|---|
| HTML Tidy | `tidy` | `sudo apt install tidy` |
| Spell checking | `aspell` or `hunspell`, with a dictionary | `sudo apt install aspell aspell-en` or `sudo apt install hunspell hunspell-en-gb` |

---

## Getting started

1. Start SiteForge. It opens a new page from your default template, or the files you had open last time.
2. Write some HTML. Use the **Insert** menu or toolbar for common tags, and the Explorer panel to find your files.
3. Save the page, then press `F12` to see it in your default browser.
4. Run **Tools > Run HTML Tidy** to check your markup, and click any message to jump straight to the problem.
5. Press `F1` for the full help.

### Default keyboard shortcuts

| Keys | Command |
|---|---|
| `Ctrl+N` / `Ctrl+O` / `Ctrl+S` | New / Open / Save |
| `Ctrl+Shift+S` / `Ctrl+Alt+S` | Save As / Save All |
| `Ctrl+W` / `Ctrl+Q` | Close tab / Quit |
| `Ctrl+Z` / `Ctrl+Y` | Undo / Redo |
| `Ctrl+F` / `F3` / `Shift+F3` | Find and Replace / Find Next / Find Previous |
| `Ctrl+H` / `Ctrl+G` | Replace / Go to Line |
| `Ctrl+]` / `Ctrl+[` | Indent / Unindent line |
| `Ctrl+Shift+]` / `Ctrl+Shift+[` | Indent / Unindent block |
| `F12` | Open in default browser |
| `Ctrl+F2` `Ctrl+F3` `Ctrl+F4` | Open in the first, second, third browser |
| `F1` | Help |

Every shortcut can be changed in **Settings > Keyboard Shortcuts**.

---

## Configuration

SiteForge stores all of its settings in `~/.config/SiteForge/`:

| File or folder | Contents |
|---|---|
| `state.json` | Open files, window layout, font, colours, indentation, tag completion and spell check settings |
| `shortcuts.json` | Your keyboard shortcuts |
| `browsers.json` | Your browser list |
| `external_tools.json` | Your external tools |
| `user_dictionary.txt` | Words you added to the spelling dictionary |
| `templates/` | Page templates |
| `snippets/` | Snippets |

Use **File > Backup Configuration...** to save all of this into a single ZIP file. To reset SiteForge, close it and rename the `~/.config/SiteForge` folder.

---

## Building the standalone program

From a folder containing the script, `icons` and `help` folders, and a virtual environment with PyInstaller installed:

```
./venv/bin/pip install pyinstaller
./build.sh
```

`build.sh` runs PyInstaller with `--onefile`, bundling the `icons` and `help` folders. To package the result as a release download with the installer script:

```
./make-release.sh 0.1
```

This creates `release/siteforge-linux-v0.1.tar.gz`.

---

## Project layout

```
siteforge.py        the application
icons/              toolbar, menu, file-type and browser icons
help/               the built-in help (help.html and its logo)
build.sh            builds the standalone program with PyInstaller
make-release.sh     packs the standalone program for download
install.sh          installer included in the download
```

---

## Known limitations

- Linux only. Some default paths (such as the browser locations) assume a typical Linux layout.
- Document Weight does not count images that are referenced only from inside a CSS file.
- The snippet editor has a Shortcut box, but snippet shortcuts do not trigger anything yet. Insert snippets by double-clicking them in the Snippets panel.
- Spell checking only covers HTML and text files, not CSS or JavaScript.

---

## Troubleshooting

| Problem | What to try |
|---|---|
| Help (F1) does not open | Check that `help/help.html` is next to the program. The standalone build includes it. |
| No spelling underlines | Tick **Settings > Spell checking**, install `aspell` or `hunspell`, and check the language in **Settings > Spelling language** |
| Tidy reports it is not installed | `sudo apt install tidy` |
| A browser button does nothing | Check the program path in **Browsers > Configure browsers...**, and save the page first |
| The menu entry is missing after installing | Log out and back in, or run `./install.sh` again |

---

## Contributing

Bug reports and suggestions are welcome. Please open an issue and include what you were doing, what you expected, and what happened, plus any message shown in the terminal.

---

## Licence

SiteForge is released under the **GNU General Public License v3.0**, because it is built on PyQt6 and QScintilla, which are GPL-licensed. See the `LICENSE` file for the full text.

HTML Tidy, aspell and hunspell are separate programs, used if installed, and are not part of SiteForge.

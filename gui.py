"""Simple graphical front end: pick a game .exe, pick a folder, press the button.

Standard library only (tkinter).  Start with ``python -m ccdec.gui [game.exe]`` or by
double-clicking ``ccdec-gui.pyw``.  The recovery runs in a worker thread; the window talks
to it through a queue, so it never freezes.  Russian and English (switch in the window).
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import traceback
from typing import Optional

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:                      # some Linux Pythons ship without Tk
    tk = None                            # type: ignore[assignment]

from . import __version__
from . import theme
from .i18n import LANGUAGE_NAMES, LANGUAGES, T, get_language, load_settings, save_language, save_setting, set_language
from .pipeline import Cancelled, Options, RecoveryError, Result, default_output_dir, level_name, path_problem, run


def encodings() -> list[tuple[str, Optional[str]]]:
    return [(T("Определить автоматически", "Detect automatically"), None),
            (T("Кириллица (cp1251)", "Cyrillic (cp1251)"), "cp1251"),
            (T("Западная (cp1252)", "Western (cp1252)"), "cp1252"),
            (T("Центральная Европа (cp1250)", "Central European (cp1250)"), "cp1250"),
            (T("Греческая (cp1253)", "Greek (cp1253)"), "cp1253"),
            (T("Турецкая (cp1254)", "Turkish (cp1254)"), "cp1254")]


def theme_names() -> list[tuple[str, str]]:
    return [("system", T("Как в системе", "System")), ("light", T("Светлая", "Light")), ("dark", T("Тёмная", "Dark"))]


def level_text(level: int) -> str:
    return {1: T("Только картинки и файлы (звуки, музыка)", "Images and files only (sounds, music)"),
            2: T("Картинки, файлы и описание проекта (уровни, объекты, события)",
                 "Images, files and the project description (layouts, objects, events)"),
            3: T("Всё, включая проект для Construct Classic (.cap)",
                 "Everything, including a Construct Classic project (.cap)")}[level]


class QueueLogHandler(logging.Handler):
    """Send library warnings (corrupt block, unknown plugin…) to the log box."""

    def __init__(self, q: "queue.Queue") -> None:
        super().__init__(level=logging.WARNING)
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        self.q.put(("log", f"  ! {record.getMessage()}"))


def open_path(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)               # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


class App:
    POLL_MS = 80

    def __init__(self, root: "tk.Tk", exe: Optional[str] = None) -> None:
        self.root = root
        self.queue: queue.Queue = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.cancel_event = threading.Event()
        self.result: Optional[Result] = None
        self.finished = threading.Event()          # set after every run (used by tests)
        self.out_edited = False
        self.busy = False
        self.encoding_code: Optional[str] = None
        saved_theme = load_settings().get("theme")
        self.theme_mode = saved_theme if saved_theme in theme.MODES else "system"
        self.palette = theme.resolve(self.theme_mode)
        theme.apply(root, self.palette)

        root.minsize(660, 580)
        self.exe_var = tk.StringVar()
        self.out_var = tk.StringVar()
        self.level_var = tk.IntVar(value=3)
        self.enc_var = tk.StringVar()
        self.masks_var = tk.BooleanVar(value=False)
        self.lang_var = tk.StringVar(value=LANGUAGE_NAMES[get_language()])
        self.theme_var = tk.StringVar()
        self.status_var = tk.StringVar()
        self.warn_var = tk.StringVar()
        self.main: Optional["ttk.Frame"] = None
        self._build()
        self.exe_var.trace_add("write", lambda *_: self._exe_changed())
        self.out_var.trace_add("write", lambda *_: self._path_check())
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._after = root.after(self.POLL_MS, self._poll)
        root.bind("<Destroy>", self._on_destroy)
        if exe:
            self.exe_var.set(exe)

    # -- layout --------------------------------------------------------------------
    def _build(self) -> None:
        """Create all widgets in the current language (called again when it changes)."""
        self.root.title(T(f"ccdec {__version__} — восстановление проектов Construct Classic",
                          f"ccdec {__version__} — Construct Classic project recovery"))
        pad = {"padx": 12, "pady": 6}
        main = self.main = ttk.Frame(self.root, padding=4)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(5, weight=1)

        head = ttk.Frame(main)
        head.grid(row=0, column=0, sticky="ew", **pad)
        head.columnconfigure(0, weight=1)
        ttk.Label(head, text=T("Восстановление проекта из игры Construct Classic",
                               "Recover a project from a Construct Classic game"),
                  font=("Segoe UI", 13, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(head, text=T("Тема:", "Theme:")).grid(row=0, column=1, sticky="e", padx=(8, 4))
        names = theme_names()
        self.theme_var.set(dict(names)[self.theme_mode])
        self.theme_box = ttk.Combobox(head, textvariable=self.theme_var, state="readonly", width=13,
                                      values=[n for _, n in names])
        self.theme_box.grid(row=0, column=2, sticky="e")
        self.theme_box.bind("<<ComboboxSelected>>", lambda e: self._change_theme())
        self.lang_box = ttk.Combobox(head, textvariable=self.lang_var, state="readonly", width=10,
                                     values=[LANGUAGE_NAMES[x] for x in LANGUAGES])
        self.lang_box.grid(row=0, column=3, sticky="e", padx=(8, 0))
        self.lang_box.bind("<<ComboboxSelected>>", lambda e: self._change_language())

        files = ttk.Frame(main)
        files.grid(row=1, column=0, sticky="ew", **pad)
        files.columnconfigure(1, weight=1)
        ttk.Label(files, text=T("Игра (.exe):", "Game (.exe):")).grid(row=0, column=0, sticky="w")
        self.exe_entry = ttk.Entry(files, textvariable=self.exe_var)
        self.exe_entry.grid(row=0, column=1, sticky="ew", padx=8, pady=3)
        self.exe_btn = ttk.Button(files, text=T("Выбрать…", "Browse…"), command=self._choose_exe)
        self.exe_btn.grid(row=0, column=2)
        ttk.Label(files, text=T("Сохранить в папку:", "Save to folder:")).grid(row=1, column=0, sticky="w")
        self.out_entry = ttk.Entry(files, textvariable=self.out_var)
        self.out_entry.grid(row=1, column=1, sticky="ew", padx=8, pady=3)
        self.out_entry.bind("<Key>", lambda e: setattr(self, "out_edited", True))
        self.out_btn = ttk.Button(files, text=T("Выбрать…", "Browse…"), command=self._choose_out)
        self.out_btn.grid(row=1, column=2)

        what = ttk.LabelFrame(main, text=T("Что восстановить", "What to recover"))
        what.grid(row=2, column=0, sticky="ew", **pad)
        self.level_buttons = []
        for level in (3, 2, 1):
            b = ttk.Radiobutton(what, text=level_text(level), value=level, variable=self.level_var)
            b.pack(anchor="w", padx=10, pady=2)
            self.level_buttons.append(b)

        adv = ttk.LabelFrame(main, text=T("Дополнительно", "Advanced"))
        adv.grid(row=3, column=0, sticky="ew", **pad)
        ttk.Label(adv, text=T("Кодировка текста:", "Text encoding:")).grid(row=0, column=0, sticky="w",
                                                                         padx=10, pady=3)
        names = encodings()
        self.enc_var.set(next(n for n, code in names if code == self.encoding_code))
        self.enc_box = ttk.Combobox(adv, textvariable=self.enc_var, state="readonly", width=32,
                                    values=[n for n, _ in names])
        self.enc_box.grid(row=0, column=1, sticky="w", padx=4)
        self.enc_box.bind("<<ComboboxSelected>>",
                          lambda e: setattr(self, "encoding_code", dict(encodings()).get(self.enc_var.get())))
        self.masks_check = ttk.Checkbutton(adv, text=T("Разбирать маски столкновений (медленно)",
                                                       "Analyse collision masks (slow)"),
                                           variable=self.masks_var)
        self.masks_check.grid(row=1, column=0, columnspan=2, sticky="w", padx=10, pady=3)

        actions = ttk.Frame(main)
        actions.grid(row=4, column=0, sticky="ew", **pad)
        self.start_btn = ttk.Button(actions, text=T("Восстановить", "Recover"), command=self.start)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(actions, text=T("Отмена", "Cancel"), command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=8)
        self.report_btn = ttk.Button(actions, text=T("Открыть отчёт", "Open report"), command=self._open_report,
                                     state="normal" if self.result and self.result.report_path else "disabled")
        self.report_btn.pack(side="right")
        self.folder_btn = ttk.Button(actions, text=T("Открыть папку", "Open folder"), command=self._open_folder,
                                     state="normal" if self.result else "disabled")
        self.folder_btn.pack(side="right", padx=8)

        out = ttk.Frame(main)
        out.grid(row=5, column=0, sticky="nsew", **pad)
        out.columnconfigure(0, weight=1)
        out.rowconfigure(2, weight=1)
        self.progress = ttk.Progressbar(out, maximum=1000)
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(out, textvariable=self.status_var, wraplength=620, justify="left").grid(row=1, column=0,
                                                                                         sticky="w", pady=4)
        box = ttk.Frame(out)
        box.grid(row=2, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.log = tk.Text(box, height=10, wrap="word", state="disabled", relief="solid", borderwidth=1,
                           font=("Segoe UI", 9) if sys.platform == "win32" else None)
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(box, command=self.log.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log["yscrollcommand"] = scroll.set
        theme.style_text(self.log, self.palette)

        self.warn_label = ttk.Label(main, textvariable=self.warn_var, foreground=self.palette.warn, wraplength=620,
                                    justify="left")
        self.warn_label.grid(row=6, column=0, sticky="w", **pad)
        self.status_var.set(T("Готово. Результат сохранён в папку.", "Done. The result is in the folder.")
                            if self.result else
                            T("Выберите файл игры (.exe) и нажмите «Восстановить».",
                              "Choose the game file (.exe) and press Recover."))
        self._path_check()

    def _change_theme(self) -> None:
        mode = next(code for code, name in theme_names() if name == self.theme_var.get())
        if mode == self.theme_mode:
            return
        self.theme_mode = mode
        save_setting("theme", mode)
        self.palette = theme.resolve(mode)
        theme.apply(self.root, self.palette)
        theme.style_text(self.log, self.palette)
        self.warn_label.configure(foreground=self.palette.warn)
        # Windows repaints a changed title bar only on the next frame update: nudge the size.
        self.root.update_idletasks()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        if w > 1 and h > 1:
            self.root.geometry(f"{w + 1}x{h}")
            self.root.update_idletasks()
            self.root.geometry(f"{w}x{h}")

    def _change_language(self) -> None:
        lang = next(code for code, name in LANGUAGE_NAMES.items() if name == self.lang_var.get())
        if lang == get_language() or self.busy:
            return
        set_language(lang)
        save_language(lang)
        log_text = self.log.get("1.0", "end-1c")
        progress = self.progress["value"]
        self.main.destroy()
        self._build()
        self.progress["value"] = progress
        if log_text:
            self._write(log_text)

    # -- helpers -------------------------------------------------------------------
    def _write(self, text: str, tag: Optional[str] = None) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for w in (self.exe_entry, self.out_entry, self.exe_btn, self.out_btn, self.masks_check, *self.level_buttons):
            w.configure(state=state)
        for box in (self.enc_box, self.lang_box):
            box.configure(state="disabled" if busy else "readonly")
        self.start_btn.configure(state="disabled" if busy else "normal")
        self.cancel_btn.configure(state="normal" if busy else "disabled")

    def _exe_changed(self) -> None:
        exe = self.exe_var.get().strip().strip('"')
        if exe and not self.out_edited:
            self.out_var.set(default_output_dir(exe))

    def _path_check(self) -> None:
        out = self.out_var.get().strip()
        problem = path_problem(out) if out else None
        self.warn_var.set(T(f"Внимание: Construct Classic может не открыть файлы из этой папки — {problem}. "
                            "Лучше выбрать папку с латинским путём, например C:\\cc.",
                            f"Note: Construct Classic may fail to open files from this folder - {problem}. "
                            "Better choose a folder with a plain Latin path, e.g. C:\\cc.") if problem else "")

    def _choose_exe(self) -> None:
        path = filedialog.askopenfilename(title=T("Выберите игру, экспортированную из Construct Classic",
                                                  "Choose a game exported from Construct Classic"),
                                          filetypes=[(T("Программы Windows", "Windows programs"), "*.exe"),
                                                     (T("Все файлы", "All files"), "*.*")])
        if path:
            self.exe_var.set(os.path.normpath(path))

    def _choose_out(self) -> None:
        path = filedialog.askdirectory(title=T("Куда сохранить результат", "Where to save the result"),
                                       mustexist=False)
        if path:
            self.out_edited = True
            self.out_var.set(os.path.normpath(path))

    # -- running -------------------------------------------------------------------
    def start(self, confirm: bool = True) -> bool:
        """Validate the form and start the worker.  Returns False if it did not start."""
        if self.worker is not None and self.worker.is_alive():
            return False
        exe = self.exe_var.get().strip().strip('"')
        out = self.out_var.get().strip().strip('"')
        if not exe or not os.path.isfile(exe):
            if confirm:
                messagebox.showerror(T("Нет файла игры", "No game file"),
                                     T("Выберите существующий файл игры (.exe).", "Choose an existing game file (.exe)."))
            self.status_var.set(T("Файл игры не выбран или не найден.", "The game file is not chosen or not found."))
            return False
        if not out:
            if confirm:
                messagebox.showerror(T("Нет папки", "No folder"),
                                     T("Укажите, куда сохранить результат.", "Choose where to save the result."))
            self.status_var.set(T("Не указана папка для результата.", "No output folder given."))
            return False
        if confirm and os.path.isdir(out) and os.listdir(out):
            if not messagebox.askyesno(T("Папка не пуста", "Folder is not empty"),
                                       T(f"В папке уже есть файлы:\n{out}\n\nФайлы с теми же именами будут заменены. "
                                         "Продолжить?",
                                         f"The folder already has files:\n{out}\n\nFiles with the same names will be "
                                         "replaced. Continue?")):
                return False
        options = Options(level=self.level_var.get(), encoding=self.encoding_code, masks=self.masks_var.get())
        self.result = None
        self.finished.clear()
        self.cancel_event.clear()
        self.progress["value"] = 0
        self.report_btn.configure(state="disabled")
        self.folder_btn.configure(state="disabled")
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self._set_busy(True)
        self.status_var.set(T("Работаю…", "Working…"))
        self._write(T(f"Файл: {exe}", f"File: {exe}"))
        self._write(T(f"Режим: {level_name(options.level)}", f"Mode: {level_name(options.level)}"))
        self.worker = threading.Thread(target=self._work, args=(exe, out, options), daemon=True)
        self.worker.start()
        return True

    def _work(self, exe: str, out: str, options: Options) -> None:
        handler = QueueLogHandler(self.queue)
        logging.getLogger("ccdec").addHandler(handler)
        try:
            result = run(exe, out, options,
                         log=lambda s: self.queue.put(("log", s)),
                         progress=lambda f, s: self.queue.put(("progress", (f, s))),
                         cancelled=self.cancel_event.is_set)
            self.queue.put(("done", result))
        except Cancelled:
            self.queue.put(("cancelled", None))
        except RecoveryError as e:
            self.queue.put(("failed", str(e)))
        except MemoryError:
            self.queue.put(("failed", T("Не хватило памяти для обработки этой игры.",
                                        "Not enough memory to process this game.")))
        except PermissionError as e:
            self.queue.put(("failed", T(f"Нет доступа к файлу или папке: {e.filename or e}. Выберите другую папку.",
                                        f"Access denied: {e.filename or e}. Choose another folder.")))
        except OSError as e:
            self.queue.put(("failed", T(f"Ошибка при работе с файлами: {e}", f"File error: {e}")))
        except Exception:
            self.queue.put(("crashed", traceback.format_exc()))
        finally:
            logging.getLogger("ccdec").removeHandler(handler)

    def _cancel(self) -> None:
        self.cancel_event.set()
        self.cancel_btn.configure(state="disabled")
        self.status_var.set(T("Останавливаю после текущего шага…", "Stopping after the current step…"))

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                getattr(self, f"_on_{kind}")(payload)
        except queue.Empty:
            pass
        self._after = self.root.after(self.POLL_MS, self._poll)

    def _on_destroy(self, event: "tk.Event") -> None:
        if event.widget is self.root and self._after:
            self.root.after_cancel(self._after)
            self._after = None

    # -- messages from the worker --------------------------------------------------
    def _on_log(self, text: str) -> None:
        self._write(text, "warn" if text.lstrip().startswith("!") else None)

    def _on_progress(self, payload: tuple) -> None:
        fraction, text = payload
        self.progress["value"] = fraction * 1000
        self.status_var.set(text)

    def _on_done(self, result: Result) -> None:
        self.result = result
        self._set_busy(False)
        self.progress["value"] = 1000
        self.status_var.set(T("Готово. Результат сохранён в папку.", "Done. The result is in the folder."))
        self._write("")
        for line in result.summary:
            self._write(line, "ok")
        if result.warnings:
            self._write("")
            self._write(T("Что стоит знать:", "Worth knowing:"), "warn")
            for w in result.warnings:
                self._write(f"• {w}", "warn")
        self._write("")
        self._write(T(f"Папка с результатом: {result.out_dir}", f"Result folder: {result.out_dir}"))
        self._write(T("Подробности о том, что восстановлено и что потеряно, — в отчёте report.md.",
                      "What was recovered and what is lost is explained in report.md."))
        self.report_btn.configure(state="normal" if result.report_path else "disabled")
        self.folder_btn.configure(state="normal")
        self.finished.set()

    def _on_cancelled(self, _: object) -> None:
        self._set_busy(False)
        self.progress["value"] = 0
        self.status_var.set(T("Остановлено. Уже записанные файлы остались в папке.",
                              "Stopped. Files written so far remain in the folder."))
        self._write(T("Остановлено пользователем.", "Stopped by the user."), "warn")
        self.folder_btn.configure(state="normal")
        self.finished.set()

    def _on_failed(self, message: str) -> None:
        self._set_busy(False)
        self.progress["value"] = 0
        self.status_var.set(T("Не получилось.", "Failed."))
        self._write(message, "error")
        self.finished.set()
        messagebox.showerror(T("Не получилось", "Failed"), message)

    def _on_crashed(self, trace: str) -> None:
        self._set_busy(False)
        self.progress["value"] = 0
        self.status_var.set(T("Внутренняя ошибка программы.", "Internal program error."))
        self._write(T("Внутренняя ошибка. Подробности для разработчика:", "Internal error. Details for the developer:"),
                    "error")
        self._write(trace, "error")
        self.finished.set()
        messagebox.showerror(T("Внутренняя ошибка", "Internal error"),
                             T("Что-то пошло не так. Технические подробности выведены в окне программы — "
                               "скопируйте их и покажите разработчику вместе с файлом игры, если можно.",
                               "Something went wrong. The technical details are shown in the window - please "
                               "copy them and report them, with the game file if possible."))

    # -- results -------------------------------------------------------------------
    def _open_folder(self) -> None:
        if self.result:
            open_path(self.result.out_dir)

    def _open_report(self) -> None:
        if self.result and self.result.report_path:
            open_path(self.result.report_path)

    def _on_close(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            if not messagebox.askyesno(T("Идёт восстановление", "Recovery in progress"),
                                       T("Прервать работу и закрыть программу?", "Stop and close the program?")):
                return
            self.cancel_event.set()
        self.root.destroy()


def main(argv: Optional[list[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if tk is None:
        print(T("ccdec: в этом Python нет tkinter. Установите его (например, пакет python3-tk) "
                "или используйте командную строку: python -m ccdec --help",
                "ccdec: this Python has no tkinter. Install it (e.g. the python3-tk package) "
                "or use the command line: python -m ccdec --help"), file=sys.stderr)
        return 1
    if sys.platform == "win32":                       # crisp text on high-DPI screens
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    App(root, argv[0] if argv else None)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

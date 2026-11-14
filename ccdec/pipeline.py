"""One-call recovery workflow used by the GUI: exe in, folder with results out.

Same steps and the same library functions as ``ccdec.cli`` (container -> blocks -> index ->
levels 1-3 -> report), but with progress and cancellation hooks and no printing, so a
front end can drive it from a worker thread.  User-facing texts are bilingual (ccdec.i18n).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Callable, Optional

from .cap_format import read_cap, write_cap
from .cap_writer import build_cap
from .container import ContainerError, load_container
from .export_assets import export_assets
from .export_json import Dumper, export_json
from .i18n import T, plural_ru
from .index import build_index
from .reader import iter_strings, parse_project
from .report import build_report


def level_name(level: int) -> str:
    return {1: T("картинки и файлы", "images and files"),
            2: T("картинки, файлы и описание проекта", "images, files and project description"),
            3: T("всё, включая проект .cap", "everything, including the .cap project")}[level]


class Cancelled(Exception):
    """The user asked to stop."""


class RecoveryError(Exception):
    """A problem with the input the user can understand (message is shown as is)."""


@dataclass
class Options:
    level: int = 3                     # 1 = assets, 2 = + project.json, 3 = + .cap
    encoding: Optional[str] = None     # None: guess cp1251/cp1252
    masks: bool = False                # analyse collision masks (slow)


@dataclass
class Result:
    out_dir: str
    cap_path: Optional[str] = None
    report_path: Optional[str] = None
    summary: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def system_codepage() -> Optional[int]:
    """The Windows ANSI code page (what non-Unicode programs such as Construct Classic use)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        return int(ctypes.windll.kernel32.GetACP())
    except Exception:
        return None


def path_problem(path: str, acp: Optional[int] = None) -> Optional[str]:
    """Why Construct Classic could not open a file at ``path``, or None if nothing is wrong.

    The editor is a non-Unicode program: on Windows a path is usable only if every character
    exists in the system ANSI code page.
    """
    acp = system_codepage() if acp is None else acp
    if acp is None:
        return None
    if acp == 65001:
        return T("включена бета-функция UTF-8 в региональных настройках Windows",
                 "the beta UTF-8 option is enabled in the Windows region settings")
    try:
        path.encode(f"cp{acp}")
        return None
    except (UnicodeEncodeError, LookupError):
        bad = " ".join(sorted({ch for ch in path if ord(ch) > 127 and not _encodable(ch, acp)})[:8])
        return T(f"в пути есть символы, которых нет в системной кодировке Windows (cp{acp}): {bad}",
                 f"the path has characters that the Windows system code page (cp{acp}) lacks: {bad}")


def _encodable(ch: str, acp: int) -> bool:
    try:
        ch.encode(f"cp{acp}")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def text_problem(strings_encoding: str, has_non_ascii: bool, acp: Optional[int] = None) -> Optional[str]:
    """Warn when the project's text would look garbled in the editor on this computer."""
    acp = system_codepage() if acp is None else acp
    if not has_non_ascii or acp is None:
        return None
    if f"cp{acp}" == strings_encoding.lower():
        return None
    system = "UTF-8" if acp == 65001 else f"cp{acp}"
    return T(f"В проекте есть текст в кодировке {strings_encoding}, а системная кодировка Windows — {system}. "
             "В Construct Classic и в готовой игре на этом компьютере этот текст может выглядеть кракозябрами. "
             "Решение: Панель управления → Region → Administrative → Change system locale → язык проекта "
             "(для русского — Russian), затем перезагрузка.",
             f"The project's text uses the {strings_encoding} code page, but the Windows system code page is "
             f"{system}. In Construct Classic and in the game, that text may look garbled on this computer. "
             "Fix: Control Panel → Region → Administrative → Change system locale → the project's language "
             "(for Cyrillic text: Russian), then restart.")


def default_output_dir(exe: str) -> str:
    base = os.path.splitext(os.path.basename(exe))[0] or "project"
    return os.path.join(os.path.dirname(os.path.abspath(exe)), base + "_recovered")


def run(exe: str, out_dir: str, options: Options = Options(), *,
        log: Callable[[str], None] = lambda s: None,
        progress: Callable[[float, str], None] = lambda f, s: None,
        cancelled: Callable[[], bool] = lambda: False) -> Result:
    """Recover ``exe`` into ``out_dir``.

    Raises :class:`RecoveryError` (readable message), :class:`Cancelled`, or lets unexpected
    exceptions through.  ``progress(fraction, text)`` is in [0, 1].
    """
    exe, out_dir = os.path.abspath(exe), os.path.abspath(out_dir)
    if os.path.exists(out_dir) and not os.path.isdir(out_dir):
        raise RecoveryError(T("Выбранный путь для результата — это файл, а не папка.",
                              "The output path is a file, not a folder."))
    steps = [("open", 5), ("parse", 10), ("index", 5)]
    if options.level >= 1:
        steps.append(("assets", 40))
    if options.level >= 2:
        steps.append(("json", 15))
    if options.level >= 3:
        steps.append(("cap", 20))
    steps.append(("report", 5))
    total = sum(w for _, w in steps)
    done = 0

    def check() -> None:
        if cancelled():
            raise Cancelled()

    def begin(name: str, text: str) -> None:
        nonlocal done
        check()
        done = sum(w for n, w in steps[:[n for n, _ in steps].index(name)])
        progress(done / total, text)
        log(text)

    result = Result(out_dir)
    os.makedirs(out_dir, exist_ok=True)

    begin("open", T("Читаю файл игры…", "Reading the game file…"))
    try:
        c = load_container(exe)
    except ContainerError as e:
        raise RecoveryError(T(f"Не удалось прочитать файл как программу Windows: {e}",
                              f"The file could not be read as a Windows program: {e}")) from None
    if not c.is_construct_export:
        raise RecoveryError(T("Это не игра, экспортированная из Construct Classic: в файле нет данных проекта.",
                              "This is not a game exported from Construct Classic: the file has no project data."))
    if c.is_preview:
        result.warnings.append(T("Это файл превью, а не готовая игра: плагины в нём не встроены, результат неполный.",
                                 "This is a preview build, not an exported game: plugins are not embedded, "
                                 "so the result is incomplete."))
    result.warnings += [w for w in c.warnings if "preview build" not in w]
    log(T(f"  найдено блоков данных: {len(c.blocks)}, плагинов: {len(c.plugins)}, файлов-ресурсов: {len(c.files)}",
          f"  data blocks: {len(c.blocks)}, plugins: {len(c.plugins)}, file resources: {len(c.files)}"))

    begin("parse", T("Разбираю структуру проекта…", "Parsing the project structure…"))
    p = parse_project(c, options.encoding)
    if p.raw:
        blocks = ", ".join(sorted(p.raw))
        result.warnings.append(T(f"Не удалось разобрать блоки: {blocks}. Они сохранены в сыром виде в project.json, "
                                 "часть данных будет утрачена.",
                                 f"These blocks could not be parsed: {blocks}. They are kept raw in project.json; "
                                 "part of the data will be missing."))
    result.warnings += [i for i in p.issues if "kept as raw bytes" not in i]
    if p.app is None or p.level is None:
        raise RecoveryError(T("Не удалось разобрать основные данные проекта (настройки или уровни). "
                              "Возможно, игра сделана неподдерживаемой версией Construct.",
                              "The main project data (settings or layouts) could not be parsed. "
                              "The game may have been made with an unsupported Construct version."))
    how = T("задана вручную", "set manually") if options.encoding else T("определена автоматически", "detected")
    log(T(f"  кодировка текста: {p.encoding} ({how})", f"  text encoding: {p.encoding} ({how})"))
    has_text = any(any(ord(ch) > 127 for ch in s) for obj in (p.app, p.level, p.events) if obj
                   for s in iter_strings(obj))
    problem = text_problem(p.encoding, has_text)
    if problem:
        result.warnings.append(problem)

    begin("index", T("Связываю объекты, анимации и картинки…", "Linking objects, animations and images…"))
    ix = build_index(c, p)
    lv = p.level
    events = sum(1 for lst in p.events.layout_events for _ in _walk_events(lst)) if p.events else 0
    nl, nt = len(lv.layouts), len(lv.object_types)
    result.summary.append(T(f"Игра «{p.app.name}»: {nl} {plural_ru(nl, 'уровень', 'уровня', 'уровней')}, "
                            f"{nt} {plural_ru(nt, 'тип', 'типа', 'типов')} объектов, "
                            f"{events} {plural_ru(events, 'событие', 'события', 'событий')}.",
                            f"Game \"{p.app.name}\": {len(lv.layouts)} layouts, {len(lv.object_types)} object types, "
                            f"{events} events."))

    assets = dumper = rebuilt = None
    if options.level >= 1:
        begin("assets", T("Сохраняю картинки и файлы…", "Saving images and files…"))
        base, weight = done, dict(steps)["assets"]

        def image_progress(k: int, n: int) -> None:
            check()
            progress((base + weight * k / max(1, n)) / total,
                     T(f"Сохраняю картинки: {k} из {n}", f"Saving images: {k} of {n}"))

        assets = export_assets(ix, out_dir, analyse_masks=options.masks, progress=image_progress)
        result.summary.append(T(f"Картинок: {len(assets['images'])}, звуков и других файлов: {len(assets['files'])}.",
                                f"Images: {len(assets['images'])}, sounds and other files: {len(assets['files'])}."))
        log(T(f"  картинок: {len(assets['images'])}, файлов: {len(assets['files'])}, эффектов: {len(assets['effects'])}",
              f"  images: {len(assets['images'])}, files: {len(assets['files'])}, effects: {len(assets['effects'])}"))

    if options.level >= 2:
        begin("json", T("Записываю описание проекта (project.json)…", "Writing the project description (project.json)…"))
        _, dumper = export_json(ix, out_dir)
        if dumper.property_problems:
            listing = "; ".join(sorted(dumper.property_problems)[:3])
            result.warnings.append(T(f"Свойства некоторых объектов не расшифрованы: {listing}",
                                     f"Some object properties could not be decoded: {listing}"))

    if options.level >= 3:
        begin("cap", T("Собираю проект для Construct Classic (.cap)…", "Building the Construct Classic project (.cap)…"))
        try:
            rebuilt = build_cap(ix, os.path.join(out_dir, "files"))
        except ValueError as e:
            raise RecoveryError(T(f"Не удалось собрать .cap: {e}", f"The .cap could not be built: {e}")) from None
        data = write_cap(rebuilt.app, rebuilt.encoding)
        if write_cap(read_cap(data, rebuilt.encoding), rebuilt.encoding) != data:
            raise RecoveryError(T("Внутренняя ошибка: собранный .cap не прошёл самопроверку. Файл не записан.",
                                  "Internal error: the rebuilt .cap failed its self-check. The file was not written."))
        check()
        result.cap_path = os.path.join(out_dir, (os.path.splitext(os.path.basename(exe))[0] or "project") + ".cap")
        with open(result.cap_path, "wb") as f:
            f.write(data)
        name = os.path.basename(result.cap_path)
        result.summary.append(T(f"Проект: {name} ({len(data) // 1024} КБ).", f"Project: {name} ({len(data) // 1024} KB)."))
        log(T(f"  записан {result.cap_path}", f"  written {result.cap_path}"))
        unknown = rebuilt.stats.get("aces_without_metadata", 0)
        if unknown:
            result.warnings.append(T(f"{unknown} условий и действий относятся к плагинам без описания: после открытия "
                                     "проекта в редакторе их параметры стоит проверить (подробности в отчёте).",
                                     f"{unknown} conditions and actions belong to plugins ccdec has no description "
                                     "for: check their parameters after opening the project (see the report)."))
        bad_path = path_problem(result.cap_path)
        if bad_path:
            result.warnings.append(T(f"Construct Classic может не открыть этот файл: {bad_path}. Положите его в папку "
                                     "с латинским путём, например C:\\cc.",
                                     f"Construct Classic may fail to open this file: {bad_path}. Move it to a folder "
                                     "with a plain Latin path, e.g. C:\\cc."))
        if c.files:
            result.warnings.append(T("Звуки и другие файлы подключены к .cap путями к папке «files» — не переносите "
                                     "её, иначе укажите файлы заново в списке Files редактора.",
                                     "Sounds and other files are linked to the .cap by their paths in the 'files' "
                                     "folder - do not move it, or relink them in the editor's Files list."))

    begin("report", T("Пишу отчёт о восстановлении…", "Writing the recovery report…"))
    if dumper is None:
        dumper = Dumper(ix)
        dumper.project()
    text = build_report(ix, assets, dumper, options.level, rebuilt)
    result.report_path = os.path.join(out_dir, "report.md")
    with open(result.report_path, "w", encoding="utf-8", errors="backslashreplace") as f:
        f.write(text)
    progress(1.0, T("Готово", "Done"))
    log(T("Готово.", "Done."))
    return result


def _walk_events(lines: list):
    for e in lines:
        if hasattr(e, "conditions"):
            yield e
        yield from _walk_events(e.children)

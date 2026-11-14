"""Recovery report (``--report``, ``report.md``): what was recovered, what is lost, and why.

Bilingual (ccdec.i18n); facts are cross-referenced to FORMAT.md sections.
"""

from __future__ import annotations

from typing import Optional

from .export_json import Dumper
from .i18n import T
from .index import ProjectIndex
from .model import Event


def _count(lines: list) -> tuple[int, int, int, int]:
    e = g = c = a = 0
    for x in lines:
        if isinstance(x, Event):
            e, c, a = e + 1, c + len(x.conditions), a + len(x.actions)
        else:
            g += 1
        se, sg, sc, sa = _count(x.children)
        e, g, c, a = e + se, g + sg, c + sc, a + sa
    return e, g, c, a


def build_report(ix: ProjectIndex, assets: Optional[dict], dumper: Optional[Dumper], level: int,
                 rebuilt=None) -> str:
    c, p = ix.container, ix.project
    lv = p.level
    L: list[str] = [T("# Отчёт ccdec", "# ccdec report"), "",
                    T(f"Файл: `{c.path}` ({c.size} байт), рантайм собран: PE timestamp {c.pe_timestamp}. "
                      f"Кодировка строк: {p.encoding}. Уровень: {level}.",
                      f"File: `{c.path}` ({c.size} bytes), runtime built: PE timestamp {c.pe_timestamp}. "
                      f"String encoding: {p.encoding}. Level: {level}."), ""]

    L += [T("## Восстановлено", "## Recovered"), ""]
    if p.app:
        a = p.app
        L.append(T(f"- Настройки приложения «{a.name}», окно {a.window_width}×{a.window_height}, "
                   f"{len(a.global_variables)} глобальных переменных, {len(a.controls)} элементов управления.",
                   f"- Application settings \"{a.name}\", window {a.window_width}x{a.window_height}, "
                   f"{len(a.global_variables)} global variables, {len(a.controls)} controls."))
        L.append(T(f"- Путь исходного .cap у автора: `{a.cap_path}`.",
                   f"- Path of the original .cap on the author's computer: `{a.cap_path}`."))
    if lv:
        inst = sum(len(y.instances) for l in lv.layouts for y in l.layers)
        layers = sum(len(l.layers) for l in lv.layouts)
        L.append(T(f"- {len(lv.object_types)} типов объектов, {len(lv.behaviors)} behaviors, {len(lv.families)} "
                   f"families, {len(lv.traits)} атрибутов, {len(lv.containers)} контейнеров.",
                   f"- {len(lv.object_types)} object types, {len(lv.behaviors)} behaviors, {len(lv.families)} "
                   f"families, {len(lv.traits)} attributes, {len(lv.containers)} containers."))
        L.append(T(f"- {len(lv.layouts)} layouts, {layers} слоёв, {inst} экземпляров.",
                   f"- {len(lv.layouts)} layouts, {layers} layers, {inst} instances."))
    if p.events:
        e, g, cn, ac = map(sum, zip(*(_count(x) for x in p.events.layout_events))) if p.events.layout_events \
            else (0,) * 4
        L.append(T(f"- События: {len(p.events.sheet_names)} листов, {e} событий, {g} групп, {cn} условий, "
                   f"{ac} действий.",
                   f"- Events: {len(p.events.sheet_names)} sheets, {e} events, {g} groups, {cn} conditions, "
                   f"{ac} actions."))
    if assets is not None:
        imgs = assets["images"]
        unref = sum(1 for i in imgs if i["file"].startswith("images/_unreferenced"))
        orphan = sum(1 for i in imgs if i["file"].startswith("images/_orphaned"))
        L.append(T(f"- Изображения: {len(imgs)} (байт в байт как в .exe), из них привязаны к объектам "
                   f"{len(imgs) - unref - orphan}, в анимациях удалённых объектов {orphan}, без ссылок {unref}.",
                   f"- Images: {len(imgs)} (byte-identical to the .exe); attached to objects "
                   f"{len(imgs) - unref - orphan}, in animations of deleted objects {orphan}, unreferenced {unref}."))
        if assets["files"]:
            kinds: dict[str, int] = {}
            for f in assets["files"]:
                kinds[f["type"]] = kinds.get(f["type"], 0) + 1
            listing = ", ".join(f"{k}: {v}" for k, v in sorted(kinds.items()))
            L.append(T(f"- Файлы-ресурсы: {len(assets['files'])} ({listing}).",
                       f"- File resources: {len(assets['files'])} ({listing})."))
        if assets["effects"]:
            L.append(T(f"- Эффекты: {len(assets['effects'])} (скомпилированный байткод).",
                       f"- Effects: {len(assets['effects'])} (compiled bytecode)."))
        if assets["icon"]:
            L.append(T("- Иконка приложения.", "- Application icon."))
        if assets["python"]:
            L.append(T("- Библиотеки Python (ZIP).", "- Python libraries (ZIP)."))
    if dumper is not None:
        dec = sum(1 for v in ix.property_cache.values() if v[0] is not None)
        L.append(T(f"- Свойства объектов: расшифровано {dec} различных блоков (Sprite, Text, Tiled Background, "
                   "Panel, Platform); остальные сохранены как `raw_hex`.",
                   f"- Object properties: {dec} distinct blocks decoded (Sprite, Text, Tiled Background, "
                   "Panel, Platform); the others are kept as `raw_hex`."))

    L += ["", T("## Плагины", "## Plugins"), "",
          T("| # | Файл | Сверка с исходниками | Неподтверждённых ACE |",
            "| # | File | Matched source | Unverified ACEs |"), "|---|---|---|---|"]
    for pa in ix.plugin_aces:
        match = f"{pa.source} ({pa.match_ratio:.0%})" if pa.source else \
            T("нет исходников — только имена из .exe", "no source - names from the .exe only")
        L.append(f"| {pa.plugin_index} | {pa.file_name or '?'} | {match} | {len(pa.mismatches)} |")

    L += ["", T("## Потеряно при экспорте (невосстановимо из .exe)", "## Lost at export (not recoverable from an .exe)"),
          ""]
    L += [
        T("- Разбиение событий на листы и include: в .exe один плоский список на layout (FORMAT.md §5.1).",
          "- The split of events into sheets and includes: the .exe has one flat list per layout (FORMAT.md 5.1)."),
        T("- Отключённые события, условия, действия и комментарии — не экспортируются.",
          "- Disabled events, conditions, actions and comments are not exported."),
        T("- Исходный текст выражений: хранятся токены. Пробелы и синонимы (`floor`→`int`, `and`→`&`, "
          "`Self`→имя объекта) утрачены; смысл выражения сохраняется.",
          "- The original text of expressions: only tokens are stored. Spacing and synonyms (`floor`->`int`, "
          "`and`->`&`, `Self`->the object name) are lost; the meaning is preserved."),
        T("- События с триггерами внутри под-событий и Wait — переписаны экспортёром; в JSON они в изменённом виде.",
          "- Events with triggers inside sub-events, and Wait, are rewritten by the exporter; JSON shows the "
          "rewritten form."),
        T("- Имена изображений и файлов-ресурсов; имена восстановлены из объектов и анимаций.",
          "- Names of images and file resources; names are rebuilt from objects and animations."),
        T("- Исходники эффектов (.fx) — только байткод и имя файла.",
          "- Effect sources (.fx): only the bytecode and the file name remain."),
        T("- Звуки и музыка из списков «Sounds»/«Music» редактора (в .exe попадает только список «Files»).",
          "- Sounds and music from the editor's Sounds/Music lists (only the Files list is exported)."),
        T("- Всё, что относится только к редактору: папки объектов, сетка, блокировки, порядок вкладок, "
          "отдельная картинка коллизий (остаётся только маска).",
          "- Editor-only data: object folders, grid, locks, tab order, the separate collision image "
          "(only the mask remains)."),
    ]

    problems: list[str] = list(p.issues) + list(ix.issues)
    if dumper is not None:
        problems += [T(f"не удалось расшифровать свойства — {k} (первый объект: {v})",
                       f"properties not decoded - {k} (first object: {v})")
                     for k, v in dumper.property_problems.items()]
        if dumper.unknown_aces:
            problems.append(T("ACE без метаданных: ", "ACEs without metadata: ")
                            + ", ".join(sorted(dumper.unknown_aces)[:30]))
    problems += c.warnings
    L += ["", T("## Предупреждения", "## Warnings"), ""]
    L += [f"- {x}" for x in problems] or [T("- нет", "- none")]
    if assets is not None:
        notes = []
        if assets["images"] and "kind" not in assets["images"][0]["collision_mask"]:
            notes.append(T("Маски коллизий не анализировались (ключ `--masks`). Маска, отличная от альфа-канала "
                           "картинки, означает, что в редакторе была задана отдельная картинка коллизий.",
                           "Collision masks were not analysed (`--masks`). A mask that differs from the image's "
                           "alpha channel means a separate collision image was set in the editor."))
        else:
            custom = sum(1 for i in assets["images"] if i["collision_mask"].get("kind") == "custom")
            notes.append(T(f"Маски коллизий: собственных {custom}, остальные совпадают с альфа-каналом.",
                           f"Collision masks: {custom} custom, the rest match the alpha channel."))
        if assets["files"]:
            notes.append(T("Файлы-ресурсы в .exe без имён: они пронумерованы в порядке списка Files редактора; "
                           "по этим же номерам на них ссылаются события.",
                           "File resources have no names in the .exe: they are numbered in the order of the "
                           "editor's Files list, which is also how events refer to them."))
        if assets["effects"]:
            notes.append(T("Эффекты — скомпилированный байткод D3DX (.fxo). Стандартные .fx лежат в папке "
                           "Effects самого Construct под теми же именами.",
                           "Effects are compiled D3DX bytecode (.fxo). The stock .fx files are in Construct's own "
                           "Effects folder under the same names."))
        if any(i["file"].startswith("images/_orphaned") for i in assets["images"]):
            notes.append(T("`images/_orphaned_animation_*` — анимации, которые редактор хранил, но ни один объект "
                           "не использует (обычно остаются от удалённых объектов).",
                           "`images/_orphaned_animation_*` are animations the editor kept but no object uses "
                           "(usually left over from deleted objects)."))
        L += ["", T("## Примечания", "## Notes"), ""] + [f"- {n}" for n in notes]
    if rebuilt is not None and level >= 3:
        s = rebuilt.stats
        L += ["", T("## Level 3 — пересобранный .cap", "## Level 3 - rebuilt .cap"), "",
              T(f"- Записано: {s.get('events', 0)} событий, {s.get('groups', 0)} групп, {s.get('params', 0)} "
                "параметров; формат CApplication v18 (Construct Classic 1.x / r2).",
                f"- Written: {s.get('events', 0)} events, {s.get('groups', 0)} groups, {s.get('params', 0)} "
                "parameters; CApplication v18 format (Construct Classic 1.x / r2)."),
              T("- Файл проверен собственным читателем: читается до конца и записывается обратно байт в байт. "
                "Сам ccdec не может проверить, откроет ли его Construct Classic, — проверьте вручную.",
                "- The file was checked with ccdec's own reader: it parses completely and writes back byte for "
                "byte. ccdec cannot check whether Construct Classic opens it - please try."),
              "", T("Что было решено за вас при пересборке:", "Decisions made for you while rebuilding:"), ""]
        L += [f"- {n}" for n in rebuilt.notes]
        L += ["", T("Как проверить: открыть .cap в Construct Classic r2, экспортировать в .exe и выполнить",
                    "To verify: open the .cap in Construct Classic r2, export it to an .exe and run"),
              T("`python -m ccdec исходный.exe --compare-exe новый.exe` — команда покажет расхождения.",
                "`python -m ccdec original.exe --compare-exe new.exe` - it lists the differences.")]
    return "\n".join(L) + "\n"

#!/usr/bin/env python3
"""Канарейка: в достижимых текстах бота не осталось слов мёртвой воронки «Манифест».

Мандат Кая 28.08.2026: старая воронка мертва целиком. 29.08 Кай получил в живом
боте «Карту перепутья» и воркбук «Манифест 7» — значит слова остались на путях,
до которых человек доходит. Этот прибор ловит их обратно.

Что считается достижимым: модули, которые bot.py реально подключает
(dp.include_router / scheduler.add_job / create_task), плюс всё, что они
импортируют внутри репозитория. Файл, лежащий в репозитории, но никем не
подключённый, прибор НЕ смотрит — мёртвый код никому ничего не шлёт.

Что считается текстом: строковый литерал, где есть пробел И кириллица. Так
человеческая фраза отделяется от служебного имени: код продукта manifest_club
и SQL «SELECT * FROM manifest7_guide» — не текст и мимо не проходят как дефект.

Прибор обязан уметь краснеть: перед вердиктом он прогоняет матчер по образцу
с заведомым дефектом и падает, если тот не нашёлся (иначе зелёный ничего не значит).

    python3 proverka_voronki.py      # 0 — чисто, 1 — нашёл или не смог проверить

Отсутствие данных = падение: нет bot.py, не разобрался с роутерами, пустой
список модулей — это красный, а не «проверять нечего».
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Слова мёртвой воронки. Ловим по смыслу, а не по букве: «поворот» сам по себе
# живёт в текстах «6 секунд» (поворот-к-другу по Готтману) — берём только
# нумерованные карточки старой колоды.
FORBIDDEN = [
    ("перепут", re.compile(r"перепут")),
    ("манифест", re.compile(r"манифест")),
    ("воркбук", re.compile(r"воркбук")),
    ("колода", re.compile(r"колод[аыуеой]")),
    ("990 ₽", re.compile(r"\b1?990\b")),
    ("поворот N", re.compile(r"поворот[а-я]*\s*[1-5]\b|(?<![:\d])[1-5]\s+поворот[а-я]*")),
]

# Оговорка ставится на строку комментарием: «# voronka: ok — причина».
# Молчаливых исключений у прибора нет: каждая оговорка печатается в вердикте.
WAIVER = "voronka: ok"

# Образец с заведомым дефектом — канарейка самого прибора.
CANARY_SAMPLE = (
    "Клуб «Манифест» — 990 ₽/мес. Воркбук «Манифест 7» бонусом, "
    "карта перепутья по 5 поворотам, обложка колоды."
)


def hits(text: str) -> list[str]:
    """Какие запрещённые слова есть в тексте. Чистая функция — тестируется без файлов."""
    low = text.lower().replace("ё", "е")  # ё → е
    return [name for name, rx in FORBIDDEN if rx.search(low)]


def is_human_text(s: str) -> bool:
    """Строка похожа на фразу человеку: есть пробел и кириллица."""
    return " " in s and any("а" <= c.lower() <= "я" or c in "ёЁ" for c in s)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _local_module(name: str) -> str | None:
    """Имя модуля → само имя, если это .py рядом с bot.py. Иначе None (внешняя либа)."""
    head = name.split(".")[0]
    return head if (ROOT / f"{head}.py").exists() else None


def imported_modules(tree: ast.Module) -> dict[str, str]:
    """{локальное имя → модуль репозитория}. Покрывает import X, from X import a, b."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mod = _local_module(a.name)
                if mod:
                    out[a.asname or a.name.split(".")[0]] = mod
        elif isinstance(node, ast.ImportFrom) and node.module:
            mod = _local_module(node.module)
            if mod:
                for a in node.names:
                    out[a.asname or a.name] = mod
    return out


def live_modules() -> set[str]:
    """Модули, которые bot.py реально включает в работу.

    Берём имена из dp.include_router(X), scheduler.add_job(F, ...) и
    create_task(F(...)) — и дотягиваем до модулей по импортам bot.py.
    Дальше — транзитивное замыкание по локальным импортам.
    """
    bot_py = ROOT / "bot.py"
    if not bot_py.exists():
        sys.exit("КРАСНЫЙ: нет bot.py — проверять нечего, значит проверка не состоялась.")
    tree = _parse(bot_py)
    imports = imported_modules(tree)

    used: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        fname = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if fname not in ("include_router", "add_job", "create_task"):
            continue
        for arg in node.args[:1]:
            target = arg.func if isinstance(arg, ast.Call) else arg
            name = getattr(target, "id", None) or getattr(target, "attr", None)
            if name:
                used.add(name)

    seeds = {imports[n] for n in used if n in imports}
    if not seeds:
        sys.exit("КРАСНЫЙ: в bot.py не нашлось ни одного подключённого модуля — "
                 "прибор не понимает разметку, вердикт недействителен.")
    seeds.add("bot")
    seeds.add("webhooks")  # вебхуки шлют сообщения людям мимо роутеров

    seen: set[str] = set()
    queue = list(seeds)
    while queue:
        mod = queue.pop()
        if mod in seen:
            continue
        seen.add(mod)
        path = ROOT / f"{mod}.py"
        if not path.exists():
            sys.exit(f"КРАСНЫЙ: модуль {mod} подключён, но файла {path.name} нет.")
        queue.extend(set(imported_modules(_parse(path)).values()) - seen)
    return seen


def _docstrings(tree: ast.Module) -> set[int]:
    """id() узлов-докстрингов: пояснение разработчику человеку не уходит."""
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None)
        if body and isinstance(body[0], ast.Expr) and \
                isinstance(body[0].value, ast.Constant) and \
                isinstance(body[0].value.value, str):
            out.add(id(body[0].value))
    return out


def scan(mod: str) -> list[tuple[int, str, str]]:
    """(строка, слово, фрагмент) по каждому человеческому литералу модуля."""
    found = []
    src = (ROOT / f"{mod}.py").read_text(encoding="utf-8").split("\n")
    waived = {i + 1 for i, line in enumerate(src) if WAIVER in line}
    tree = _parse(ROOT / f"{mod}.py")
    skip = _docstrings(tree)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in skip:
            continue
        s = node.value
        if not is_human_text(s):
            continue
        if node.lineno in waived:
            continue
        for word in hits(s):
            found.append((node.lineno, word, " ".join(s.split())[:90]))
    return found


def waivers() -> list[str]:
    """Все оговорки в репозитории — с причиной, чтобы их было видно в отчёте."""
    out = []
    for path in sorted(ROOT.glob("*.py")):
        if path.name == Path(__file__).name:
            continue  # сам прибор: его определение WAIVER — не оговорка
        for i, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
            if WAIVER in line:
                out.append(f"{path.name}:{i} — {line.split(WAIVER, 1)[1].strip()}")
    return out


def _str_args(node: ast.Call) -> list[str]:
    return [a.value for a in node.args
            if isinstance(a, ast.Constant) and isinstance(a.value, str)]


def buttons_and_handlers(mods: list[str]) -> tuple[dict[str, str], set[str], set[str]]:
    """Что кнопки шлют и что бот умеет ловить.

    Возвращает: {callback_data → где объявлена}, точные фильтры F.data == "x",
    префиксы F.data.startswith("y"). Кнопка без обработчика — дефект того же
    рода, что и старый текст: человек жмёт, и не происходит ничего.
    """
    buttons: dict[str, str] = {}
    exact: set[str] = set()
    prefixes: set[str] = set()
    for mod in mods:
        tree = _parse(ROOT / f"{mod}.py")
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "callback_data" and \
                    isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                buttons.setdefault(node.value.value, f"{mod}.py:{node.value.lineno}")
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            # F.data.startswith("x")
            if isinstance(fn, ast.Attribute) and fn.attr == "startswith":
                prefixes.update(_str_args(node))
            # F.data == "x" внутри Compare
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare) and isinstance(node.ops[0], ast.Eq) and \
                    isinstance(node.left, ast.Attribute) and node.left.attr == "data":
                exact.update(c.value for c in node.comparators
                             if isinstance(c, ast.Constant) and isinstance(c.value, str))
    return buttons, exact, prefixes


def dangling_buttons(mods: list[str]) -> list[str]:
    """Кнопки, чей callback никто из достижимых модулей не ловит."""
    buttons, exact, prefixes = buttons_and_handlers(mods)
    out = []
    for data, where in sorted(buttons.items()):
        if data in exact or any(data.startswith(pref) for pref in prefixes):
            continue
        out.append(f"{where}  callback_data={data!r} — обработчика нет")
    return out


def main() -> int:
    # Канарейка прибора: на образце с дефектом матчер обязан покраснеть.
    missed = {n for n, _ in FORBIDDEN} - set(hits(CANARY_SAMPLE))
    if missed:
        print(f"КРАСНЫЙ: прибор не ловит собственный образец — {sorted(missed)}")
        return 1

    mods = sorted(live_modules())
    bad = [(m, *h) for m in mods for h in scan(m)]

    print(f"Достижимых модулей: {len(mods)} — {', '.join(mods)}")
    for w in waivers():
        print(f"  оговорка: {w}")
    rc = 0
    if bad:
        print(f"\nКРАСНЫЙ: слова мёртвой воронки в достижимых текстах — {len(bad)} шт.\n")
        for mod, line, word, frag in bad:
            print(f"  {mod}.py:{line}  [{word}]  {frag}")
        rc = 1
    else:
        print("\nЗЕЛЁНЫЙ: слов мёртвой воронки в достижимых текстах нет.")

    # Убранный текст оставляет за собой кнопку: пустая кнопка хуже старого текста.
    hanging = dangling_buttons(mods)
    if hanging:
        print(f"\nКРАСНЫЙ: кнопок без обработчика — {len(hanging)}\n")
        for h in hanging:
            print(f"  {h}")
        rc = 1
    else:
        print("ЗЕЛЁНЫЙ: у каждой кнопки достижимого экрана есть обработчик.")
    return rc


if __name__ == "__main__":
    sys.exit(main())

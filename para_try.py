#!/usr/bin/env python3
"""Сухой прогон лидмагнита в терминале — без Telegram, без подписки, без базы.

Печатает ровно те экраны и тексты, которые бот отдаёт человеку.

    python3 para_try.py                  # прогон с примером ответов
    python3 para_try.py АБВГДЕ 432104 ВБДАГ   # свои ответы
    python3 para_try.py --texts          # все 6 динамик и все 5 ролей целиком

Ответы: 6 букв (тест 1, ситуационные) · 6 цифр 0–4 (тест 1, шкала) ·
5 букв (тест 2, роль). Буква — позиция НА ЭКРАНЕ, как её видит человек.
"""
import sys

import quiz_para as q
import quiz_para_data as d

LETTERS = "АБВГДЕ"
RULE = "─" * 64


def _dump_texts():
    for key, name in d.DYNAMIC_NAMES.items():
        print(f"{RULE}\nДИНАМИКА {name}\n{RULE}\n{d.RESULTS[key]}\n")
    for key, name in d.STRATEGY_NAMES.items():
        print(f"{RULE}\nРОЛЬ: {name}\n{RULE}\n{d.STRATEGY_TEXTS[key]}\n")
        print(f"{d.PRACTICE_HEADER}.\n\n{d.PRACTICES[key]}\n")


def _pick(shuffle_key: str, screen_char: str) -> int:
    """Буква на экране → исходный индекс варианта (так же делает обработчик)."""
    pos = LETTERS.index(screen_char.upper())
    return shuffle_key[pos]


def run(t1_letters: str, t1_scale: str, t2_letters: str):
    scores = {k: 0 for k in q.SCALES}

    print(f"{RULE}\n{d.T1_INTRO}\n\n[{d.T1_BTN_GO}]\n")
    for idx in range(len(d.T1_SITUATIONAL) + len(d.T1_SCALED)):
        text, _ = q._t1_screen(idx)
        print(text)
        if idx < len(d.T1_SITUATIONAL):
            qid = d.T1_SITUATIONAL[idx]["id"]
            orig = _pick(q._SHUFFLE_T1[qid], t1_letters[idx])
            print(f"\n→ ты жмёшь: {t1_letters[idx].upper()}\n")
        else:
            qid = d.T1_SCALED[idx - len(d.T1_SITUATIONAL)]["id"]
            orig = int(t1_scale[idx - len(d.T1_SITUATIONAL)])
            print("\n".join(f"   • {lbl}" for lbl in d.T1_SCALE_LABELS))
            print(f"\n→ ты жмёшь: {d.T1_SCALE_LABELS[orig]}\n")
        scale, pts = q.score_answer(qid, orig)
        scores[scale] += pts

    main, second = q.pick_dynamics(scores)
    result = d.RESULTS[main]
    if second:
        result += "\n\n" + d.SECONDARY_PREFIX + d.SECONDARY_NOTES[second]
    print(f"{RULE}\nРЕЗУЛЬТАТ ТЕСТА 1   (шкалы: {scores})\n{RULE}")
    print(result + "\n\n" + d.DISCLAIMER + "\n")
    img = q._dynamic_image(main)
    print(f"[картинка динамики: {img if img else 'НЕТ ФАЙЛА — экран пропускается'}]\n")
    print(f"{d.HOOK_TEXT}\n\n[{d.HOOK_BTN}]\n")

    print(f"{RULE}\nГЕЙТ ПОДПИСКИ (в боте здесь проверяется членство в канале)\n{RULE}")
    print(f"{d.GATE_TEXT}\n\n[{d.GATE_BTN_CHANNEL}] [{d.GATE_BTN_DONE}]\n")

    counts = {}
    print(f"{d.T2_INTRO}\n")
    for idx, question in enumerate(d.T2_QUESTIONS):
        text, _ = q._t2_screen(idx)
        print(text)
        orig = _pick(q._SHUFFLE_T2[question["id"]], t2_letters[idx])
        strat = q.STRATEGIES[orig]
        counts[strat] = counts.get(strat, 0) + 1
        print(f"\n→ ты жмёшь: {t2_letters[idx].upper()}\n")

    strat = q.pick_strategy(counts)
    print(f"{RULE}\nРЕЗУЛЬТАТ ТЕСТА 2   (голоса: {counts})\n{RULE}")
    print(d.STRATEGY_TEXTS[strat] + "\n")
    print(f"{d.PRACTICE_HEADER}.\n\n{d.PRACTICES[strat]}\n")
    print(f"{d.BRIDGE_TEXT}\n\n[{d.BRIDGE_BTN}]")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--texts":
        _dump_texts()
    else:
        t1l, t1s, t2l = (args + ["АААААА", "222222", "ААААА"][len(args):])[:3]
        assert len(t1l) == 6 and len(t1s) == 6 and len(t2l) == 5, __doc__
        run(t1l, t1s, t2l)

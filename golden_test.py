#!/usr/bin/env python3
"""Golden-set регресс-тест фильтра и заголовков (29.10.2026).

Зачем: логика should_forward/build_alert_title держится на порядке веток и на
строковом протоколе match_reason — правка в одном месте молча ломает другое
(см. CLAUDE.md, раздел 5). Тест прогоняет РЕАЛЬНЫЕ сообщения канала, накопленные
в логах, и показывает, что изменилось по сравнению с зафиксированным эталоном.

Корпус собирается из alert_monitor.log*:
  - строки «СОВПАДЕНИЕ (...)» содержат ПОЛНЫЙ текст пересланного сообщения
    (многострочные — склеиваются до следующей метки времени);
  - строки «Пропущено:» обрезаны до 60 символов, поэтому берём только те,
    что короче 60 — они заведомо целые.
Корпус из логов ОБЪЕДИНЯЕТСЯ с уже сохранённым в golden.json, поэтому ротация
логов не уменьшает набор.

Запуск (только через venv — нужен telethon):
    ./venv/bin/python3 golden_test.py              # проверка, код возврата 0/1
    ./venv/bin/python3 golden_test.py --generate   # переснять эталон
"""
import os
import re
import sys
import glob
import json
import logging

BASE = os.path.dirname(os.path.abspath(__file__))
# main.py читает config.json и пишет alert_monitor.log по относительным путям,
# поэтому переходим в папку бота ДО импорта
os.chdir(BASE)
sys.path.insert(0, BASE)

import main  # noqa: E402

# глушим логгер бота, чтобы он не мешался в выводе теста и не писал в боевой лог
main.logger.setLevel(logging.CRITICAL)

GOLDEN_PATH = os.path.join(BASE, 'golden.json')
LOG_GLOB = os.path.join(BASE, 'alert_monitor.log*')
SOURCE_URL = 'https://t.me/kpszsu/0'  # фиксированная ссылка, чтобы тело было стабильным

TS_RE = re.compile(r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} \[')
MATCH_RE = re.compile(r'\[poll\] СОВПАДЕНИЕ \([^)]*\): ')
SKIP_RE = re.compile(r'\[poll\] Пропущено: ')
SKIP_TRUNCATE_LEN = 60  # msg.message[:60] в main.py


def extract_from_logs():
    """Уникальные тексты сообщений из всех логов."""
    texts = set()

    def take(record):
        if not record:
            return
        blob = "\n".join(record)
        m = MATCH_RE.search(blob)
        if m:
            t = blob[m.end():].strip()
            if t:
                texts.add(t)
            return
        m = SKIP_RE.search(blob)
        if m:
            t = blob[m.end():]
            if len(t) < SKIP_TRUNCATE_LEN:  # не обрезано — значит целое
                t = t.strip()
                if t:
                    texts.add(t)

    for path in sorted(glob.glob(LOG_GLOB)):
        record = []
        with open(path, encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.rstrip('\n')
                if TS_RE.match(line):
                    take(record)
                    record = [line]
                elif record:
                    record.append(line)
        take(record)
    return texts


def evaluate(text, config):
    """Что бот сделает с этим текстом сейчас."""
    send, reason = main.should_forward(text, config)
    if not send:
        return {"send": False, "reason": reason}
    title, reason_ru = main.build_alert_title(text, reason)
    return {
        "send": True,
        "reason": reason,
        "kind": main.classify_message_kind(text) or "бойове",
        "title": title,
        "reason_ru": reason_ru,
        "message": main.build_alert_message(
            title, main.translate_to_ru(text), reason_ru, SOURCE_URL, reason),
    }


def load_golden():
    if not os.path.exists(GOLDEN_PATH):
        print(f"Эталон {GOLDEN_PATH} не найден. Сначала: "
              f"./venv/bin/python3 golden_test.py --generate")
        sys.exit(2)
    with open(GOLDEN_PATH, encoding='utf-8') as f:
        return json.load(f)


def short(value, limit=110):
    """Однострочное представление значения для вывода расхождений."""
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    s = s.replace("\n", " ⏎ ")
    return s if len(s) <= limit else s[:limit] + "…"


def cmd_generate(config):
    from_logs = extract_from_logs()
    old = {}
    if os.path.exists(GOLDEN_PATH):
        with open(GOLDEN_PATH, encoding='utf-8') as f:
            old = json.load(f)
    corpus = sorted(from_logs | set(old))
    golden = {t: evaluate(t, config) for t in corpus}
    with open(GOLDEN_PATH, 'w', encoding='utf-8') as f:
        json.dump(golden, f, ensure_ascii=False, indent=1, sort_keys=True)
    added = len(set(corpus) - set(old))
    changed = sum(1 for t in corpus if t in old and old[t] != golden[t])
    forwarded = sum(1 for v in golden.values() if v["send"])
    print(f"Эталон записан: {GOLDEN_PATH}")
    print(f"  сообщений в корпусе: {len(golden)} "
          f"(из логов {len(from_logs)}, новых к прошлому эталону {added})")
    print(f"  из них пересылается: {forwarded}, отсекается: {len(golden) - forwarded}")
    if changed:
        print(f"  ⚠️  результат изменился у {changed} сообщений — "
              f"убедись, что это ожидаемые правки")
    return 0


def cmd_check(config):
    golden = load_golden()
    diffs = 0
    for text in sorted(golden):
        was, now = golden[text], evaluate(text, config)
        if was == now:
            continue
        diffs += 1
        print(f"\n─── {short(text, 90)}")
        for key in sorted(set(was) | set(now)):
            a, b = was.get(key, "—"), now.get(key, "—")
            if a != b:
                print(f"    {key}:")
                print(f"      было:  {short(a)}")
                print(f"      стало: {short(b)}")
    print(f"\n{len(golden)} сообщений, {diffs} расхождений")
    return 1 if diffs else 0


if __name__ == '__main__':
    cfg = main.load_config()
    if len(sys.argv) > 1 and sys.argv[1] == '--generate':
        sys.exit(cmd_generate(cfg))
    elif len(sys.argv) > 1 and sys.argv[1] not in ('--check',):
        print(__doc__)
        sys.exit(2)
    sys.exit(cmd_check(cfg))

#!/usr/bin/env python3
import os
import re
import sys
import json
import logging
from logging.handlers import RotatingFileHandler
import asyncio
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import JoinChannelRequest

# Настройка логирования
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
_fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
_fh = RotatingFileHandler('alert_monitor.log', maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')
_fh.setFormatter(_fmt)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
logger.addHandler(_fh)
logger.addHandler(_sh)

CONFIG_PATH = 'config.json'

def load_config():
    if not os.path.exists(CONFIG_PATH):
        logger.error(f"Файл конфигурации {CONFIG_PATH} не найден!")
        sys.exit(1)
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Ошибка при чтении {CONFIG_PATH}: {e}")
        sys.exit(1)

SUBSCRIBERS_PATH = 'subscribers.json'
LAST_ID_PATH = '/opt/alert_monitor/last_id.txt'
subscribers = set()

def load_subscribers(config):
    global subscribers
    # Добавляем дефолтный chat_id из конфига, чтобы владелец гарантированно получал уведомления
    default_chat_id = config.get("chat_id")
    if default_chat_id:
        subscribers.add(int(default_chat_id))
        
    if os.path.exists(SUBSCRIBERS_PATH):
        try:
            with open(SUBSCRIBERS_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    for cid in data:
                        subscribers.add(int(cid))
            logger.info(f"Загружено подписчиков: {len(subscribers)} (список: {list(subscribers)})")
        except Exception as e:
            logger.error(f"Ошибка загрузки подписчиков: {e}")
    else:
        save_subscribers()

def save_subscribers():
    try:
        with open(SUBSCRIBERS_PATH, 'w', encoding='utf-8') as f:
            json.dump(list(subscribers), f, indent=4)
    except Exception as e:
        logger.error(f"Ошибка сохранения подписчиков: {e}")

def send_telegram_bot_message_to_all(text, config):
    bot_token = config.get("bot_token")
    if not bot_token:
        logger.error("Токен бота не задан!")
        return False
        
    to_remove = []
    current_subscribers = list(subscribers)
    success_count = 0
    
    for cid in current_subscribers:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": cid,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        try:
            data = json.dumps(payload).encode('utf-8')
            req = urllib.request.Request(
                url,
                data=data,
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status == 200:
                    success_count += 1
                else:
                    logger.error(f"Не удалось отправить пользователю {cid}: {response.status}")
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8')
            logger.error(f"Ошибка отправки пользователю {cid} (код {e.code}): {error_body}")
            # Если пользователь заблокировал бота или чат не найден, удаляем его
            if e.code in (403, 400):
                logger.info(f"Удаление неактивного пользователя {cid} из подписок.")
                to_remove.append(cid)
        except Exception as e:
            logger.error(f"Ошибка отправки пользователю {cid}: {e}")
            
    if to_remove:
        for cid in to_remove:
            subscribers.discard(cid)
        save_subscribers()
        
    logger.info(f"Рассылка завершена. Успешно отправлено: {success_count} из {len(current_subscribers)}")
    return success_count > 0

def check_message_match(text, keywords):
    # Оставлено для совместимости (старая логика)
    text_lower = text.lower()
    for kw in keywords:
        if kw.lower() in text_lower:
            return kw
    return None

def should_forward(text, config):
    """Фильтр Путь Б: города всегда, балистика/ракеты кроме далёкого региона/направления,
    дроны только с городом, відбій только по своему городу."""
    t = text.lower()
    cities      = config.get('cities', [])
    fast        = config.get('fast_threats', [])
    slow        = config.get('slow_threats', [])
    national    = config.get('national', [])
    all_clear   = config.get('all_clear', [])
    far_regions = config.get('far_regions', [])
    far_dirs    = config.get('far_directions', [])

    city_hit = [k for k in cities if k in t]
    far_hit  = [k for k in far_regions if k in t]
    dir_hit  = [k for k in far_dirs if k in t]
    # фильтр дронов 2026-08-28: fast_hit/slow_hit нужны раньше, до city_hit
    fast_hit = [k for k in fast if k in t]
    slow_hit = [k for k in slow if k in t]

    # ВІДБІЙ — только если мой город упомянут
    if any(k in t for k in all_clear):
        if city_hit:
            return True, f"відбій ({','.join(city_hit)})"
        return False, "відбій не мій регіон"

    # --- фильтр дронов 2026-08-28: дрон без ракетных слов шлём тільки якщо в
    # тексті є саме місто Київ (word-boundary regex), а не Київщина/пригороди
    # (cities містить підрядки 'київщ','бровар','борисп' і т.п., які інакше
    # перехоплюють дрон на кроці "1. Мій город" нижче) ---
    if slow_hit and not fast_hit:
        is_kyiv_city = bool(re.search(r'\bки[їіє]в(а|у|і|ом)?\b', t))
        if is_kyiv_city:
            return True, f"дрон, місто Київ: {','.join(slow_hit)}"
        return False, f"дрон не в місті Київ: {','.join(slow_hit)}"
    # --- конец фильтра дронов 2026-08-28 ---

    # 1. Мой город → всегда (приоритет над далёкими регионами)
    if city_hit:
        return True, f"моє місто: {','.join(city_hit)}"
    # 2. Общенациональное → всегда
    nat_hit = [k for k in national if k in t]
    if nat_hit:
        return True, "загальнонаціональна"
    # 3. Быстрые угрозы (балистика/ракеты) — всегда, кроме далёкого региона/направления
    # было до фильтра дронов 2026-08-28: fast_hit = [k for k in fast if k in t]
    if fast_hit:
        if far_hit:
            return False, f"швидка, далекий регіон ({','.join(far_hit)})"
        if dir_hit:
            return False, f"швидка, далекий напрямок ({','.join(dir_hit)})"
        return True, f"швидка загроза: {','.join(fast_hit)}"
    # 4. Дроны — только если мой город (уже проверен выше, города нет → пропуск)
    # было до фильтра дронов 2026-08-28: slow_hit = [k for k in slow if k in t]
    # (сюда попадаем, только если slow_hit есть вместе с fast_hit — уже не
    #  «чистый» дрон, обрабатывается как раньше)
    if slow_hit:
        return False, f"дрон далеко: {','.join(slow_hit)}"
    return False, "не в фокусі"

def test_bot_connection(config):
    logger.info("Запуск теста подключения Telegram-бота к подписчикам...")
    test_msg = "🔔 <b>Тестовое сообщение от монитора ракетных угроз (Режим рассылки)</b>\nБот настроен корректно и готов к работе."
    load_subscribers(config)
    success = send_telegram_bot_message_to_all(test_msg, config)
    if success:
        logger.info("Тестовое сообщение успешно отправлено всем активным подписчикам!")
    else:
        logger.error("Ошибка при отправке тестового сообщения. Проверьте настройки бота и список подписчиков.")

def test_filter(text, config):
    keywords = config.get("keywords", [])
    matched_kw = check_message_match(text, keywords)
    if matched_kw:
        print(f"✅ Совпадение найдено! Ключевое слово: '{matched_kw}'")
    else:
        print("❌ Совпадений не найдено.")

def translate_to_ru(text):
    try:
        params = urllib.parse.urlencode({
            'client': 'gtx',
            'sl': 'uk',
            'tl': 'ru',
            'dt': 't',
            'q': text,
        })
        url = f"https://translate.googleapis.com/translate_a/single?{params}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        translated = ''.join(seg[0] for seg in data[0] if seg[0])
        if not translated.strip():
            raise ValueError("пустой ответ")
        return translated
    except Exception as e:
        logger.warning(f"[translate] fallback на оригинал: {e}")
        return text


async def poll_bot_updates(config):
    bot_token = config.get("bot_token")
    if not bot_token:
        logger.error("Опрос бота не запущен: токен не найден в конфигурации.")
        return
        
    last_update_id = 0
    
    # Инициализация offset
    try:
        url = f"https://api.telegram.org/bot{bot_token}/getUpdates?limit=1&offset=-1"
        req = urllib.request.Request(url, method='GET')
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
            if data.get("ok") and data.get("result"):
                last_update_id = data["result"][0]["update_id"]
                logger.info(f"Начальный offset обновлений бота: {last_update_id}")
    except Exception as e:
        logger.warning(f"Не удалось получить начальный offset бота: {e}")
        
    logger.info("Запущен фоновый опрос обновлений бота...")
    
    while True:
        try:
            url = f"https://api.telegram.org/bot{bot_token}/getUpdates?offset={last_update_id + 1}&timeout=5"
            req = urllib.request.Request(url, method='GET')
            
            loop = asyncio.get_running_loop()
            
            def do_request():
                try:
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        return json.loads(resp.read().decode('utf-8'))
                except Exception as ex:
                    logger.debug(f"Ошибка при запросе getUpdates: {ex}")
                    return None
                    
            res = await loop.run_in_executor(None, do_request)
            
            if res and res.get("ok") and res.get("result"):
                for update in res["result"]:
                    last_update_id = update["update_id"]
                    message = update.get("message")
                    if not message:
                        continue
                        
                    chat_id = message["chat"]["id"]
                    text = message.get("text", "").strip()
                    
                    if text == "/start":
                        if chat_id not in subscribers:
                            subscribers.add(chat_id)
                            save_subscribers()
                            logger.info(f"Добавлен новый подписчик: {chat_id}")
                            welcome_msg = (
                                "🚨 <b>Ви успішно підписалися на сповіщення про повітряні загрози!</b>\n\n"
                                "Бот надсилатиме вам інформацію про прольоти ракет та безпілотників в сторону Києва та області в реальному часі.\n\n"
                                "Щоб відписатися, відправте команду /stop."
                            )
                            url_send = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                            payload = {
                                "chat_id": chat_id,
                                "text": welcome_msg,
                                "parse_mode": "HTML"
                            }
                            data_send = json.dumps(payload).encode('utf-8')
                            req_send = urllib.request.Request(
                                url_send, data=data_send,
                                headers={'Content-Type': 'application/json'},
                                method='POST'
                            )
                            await loop.run_in_executor(None, lambda: urllib.request.urlopen(req_send, timeout=5).read())
                    elif text == "/stop":
                        if chat_id in subscribers:
                            subscribers.discard(chat_id)
                            save_subscribers()
                            logger.info(f"Подписчик удален: {chat_id}")
                            goodbye_msg = "🔕 <b>Ви відписалися від сповіщень.</b>"
                            url_send = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                            payload = {
                                "chat_id": chat_id,
                                "text": goodbye_msg,
                                "parse_mode": "HTML"
                            }
                            data_send = json.dumps(payload).encode('utf-8')
                            req_send = urllib.request.Request(
                                url_send, data=data_send,
                                headers={'Content-Type': 'application/json'},
                                method='POST'
                            )
                            await loop.run_in_executor(None, lambda: urllib.request.urlopen(req_send, timeout=5).read())
                    elif text.startswith("/test"):
                        sample = text[5:].strip()
                        if not sample:
                            reply_text = "Використання: /test &lt;текст повідомлення&gt;\nПриклад: /test Ракети на Київ"
                        else:
                            fwd, reason = should_forward(sample, config)
                            status = "✅ ПЕРЕШЛЕ" if fwd else "❌ ПРОПУСТИТЬ"
                            reply_text = f"{status}\nПричина: {reason}"
                        url_send = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                        payload = {
                            "chat_id": chat_id,
                            "text": reply_text,
                            "parse_mode": "HTML"
                        }
                        data_send = json.dumps(payload).encode('utf-8')
                        req_send = urllib.request.Request(
                            url_send, data=data_send,
                            headers={'Content-Type': 'application/json'},
                            method='POST'
                        )
                        await loop.run_in_executor(None, lambda: urllib.request.urlopen(req_send, timeout=5).read())

        except Exception as e:
            logger.error(f"Ошибка в цикле фонового опроса бота: {e}")
            
        await asyncio.sleep(3)

async def poll_channel(client, channel_entity, channel_username, config):
    # Загружаем или инициализируем last_id
    if os.path.exists(LAST_ID_PATH):
        try:
            with open(LAST_ID_PATH) as f:
                last_id = int(f.read().strip())
            logger.info(f"[poll] Старт с last_id={last_id}")
        except Exception as e:
            logger.warning(f"[poll] Не удалось прочитать last_id, начинаем с 0: {e}")
            last_id = 0
    else:
        # Первый запуск — берём текущий последний id, бэклог не шлём
        try:
            msgs = await client.get_messages(channel_entity, limit=1)
            last_id = msgs[0].id if msgs else 0
        except Exception as e:
            logger.warning(f"[poll] Не удалось получить начальный id: {e}")
            last_id = 0
        try:
            with open(LAST_ID_PATH, 'w') as f:
                f.write(str(last_id))
        except Exception as e:
            logger.warning(f"[poll] Не удалось сохранить начальный last_id: {e}")
        logger.info(f"[poll] Первый запуск, стартовый last_id={last_id}, бэклог не отправляем")

    logger.info(f"[poll] Опрос канала @{channel_username} каждые 7 сек запущен")

    while True:
        try:
            try:
                msgs = await client.get_messages(channel_entity, min_id=last_id, limit=50)
            except FloodWaitError as fw:
                logger.warning(f"[poll] FloodWait: Telegram просит подождать {fw.seconds} сек")
                await asyncio.sleep(fw.seconds + 1)
                continue
            if msgs:
                for msg in reversed(msgs):  # хронологический порядок (старые → новые)
                    if not msg.message:
                        last_id = max(last_id, msg.id)
                        continue

                    pub = msg.date.astimezone()
                    now = datetime.now(pub.tzinfo)
                    lag = (now - pub).total_seconds()
                    logger.info(f"⏱ [poll] Публикация: {pub:%H:%M:%S} | Получено: {now:%H:%M:%S} | Задержка: {lag:.1f} сек")

                    should_send, match_reason = should_forward(msg.message, config)
                    if should_send:
                        logger.warning(f"[poll] СОВПАДЕНИЕ ({match_reason}): {msg.message}")

                        if "відбій" in match_reason:
                            title     = "🟢 <b>Отбой — Киев/область</b>"
                            reason_ru = "Отбой для Киева/области"
                        elif "моє місто" in match_reason:
                            txt = msg.message.lower()
                            city_explicit = re.search(r'\bки[їіє]в(а|у|і|ом)?\b', txt)
                            rocket_markers = ("балістичн", "ракетн", "ракета", "ракети",
                                              "крилат", "аеробаліст")
                            is_rocket = any(m in txt for m in rocket_markers)
                            oblast_markers = ("київщ", "київська обл", "київської обл",
                                              "бровар", "борисп", "васильк", "ірпін",
                                              "обухів", "вишгород", "фастів", "буча", "гостомел")
                            has_oblast = any(m in txt for m in oblast_markers)
                            if city_explicit and is_rocket:
                                title     = "🚨 🚀 <b>РАКЕТА НА КИЕВ — В УКРЫТИЕ!</b> ‼️"
                                reason_ru = "Ракета на Киев"
                            elif city_explicit:
                                title     = "🚨 <b>Угроза в Киеве</b> 🚨"
                                reason_ru = "Упоминается Киев"
                            elif has_oblast:
                                title     = "🚨 <b>Угроза: Киевская область</b> 🚨"
                                reason_ru = "Упоминается Киевская область"
                            elif re.search(r'ки[їіє]в', txt):
                                title     = "🚨 <b>Угроза в Киеве</b> 🚨"
                                reason_ru = "Упоминается Киев"
                            else:
                                title     = "🚨 <b>Угроза: Киев и область</b> 🚨"
                                reason_ru = "Упоминается Киев/область"
                        elif "загальнонаціональна" in match_reason:
                            title     = "🚨 <b>Тревога по всей Украине</b> 🚨"
                            reason_ru = "Угроза по всей Украине"
                        elif "швидка загроза" in match_reason:
                            title     = "⚠️ <b>Возможная угроза — пуски ракет</b>"
                            reason_ru = "Быстрая угроза — баллистика или ракеты"
                        else:
                            title     = "⚠️ <b>Внимание</b>"
                            reason_ru = match_reason

                        loop = asyncio.get_running_loop()
                        translated = await loop.run_in_executor(None, translate_to_ru, msg.message)

                        alert_text = (
                            f"{title}\n\n"
                            f"{translated}\n\n"
                            f"📍 Причина: {reason_ru}\n"
                            f"🔗 <a href='https://t.me/{channel_username}/{msg.id}'>Источник</a>"
                        )
                        await loop.run_in_executor(None, send_telegram_bot_message_to_all, alert_text, config)
                    else:
                        logger.info(f"[poll] Пропущено: {msg.message[:60]}")

                    last_id = max(last_id, msg.id)

                # Сохраняем last_id после обработки пачки
                try:
                    with open(LAST_ID_PATH, 'w') as f:
                        f.write(str(last_id))
                except Exception as e:
                    logger.warning(f"[poll] Не удалось сохранить last_id: {e}")

        except Exception as e:
            logger.error(f"[poll] Ошибка опроса канала: {e}")

        await asyncio.sleep(7)


async def main():
    # Проверка аргументов командной строки для тестирования
    config = load_config()
    
    if len(sys.argv) > 1:
        if sys.argv[1] == '--test-bot':
            test_bot_connection(config)
            return
        elif sys.argv[1] == '--test-filter' and len(sys.argv) > 2:
            test_filter(sys.argv[2], config)
            return
        else:
            print("Использование:")
            print("  python main.py                  - запуск мониторинга")
            print("  python main.py --test-bot       - отправить тестовое сообщение через бота")
            print("  python main.py --test-filter \"текст\" - проверить работу фильтра")
            return

    api_id = config.get("telegram_api_id")
    api_hash = config.get("telegram_api_hash")
    channel_username = config.get("monitored_channel", "kpszsu")
    keywords = config.get("keywords", [])

    if not api_id or not api_hash or api_hash == "YOUR_API_HASH_HERE":
        logger.error("Пожалуйста, укажите валидные telegram_api_id и telegram_api_hash в config.json!")
        sys.exit(1)

    logger.info("Загрузка списка подписчиков...")
    load_subscribers(config)

    logger.info("Инициализация клиента Telegram...")
    
    # Создаем клиента Telethon. Сессия будет сохранена в файл alert_monitor.session
    client = TelegramClient('alert_monitor', api_id, api_hash)

    logger.info("Подключение к Telegram...")
    # client.start() автоматически запросит телефон/код в консоли при первом запуске
    await client.start()

    # Запуск фонового опроса обновлений бота
    asyncio.create_task(poll_bot_updates(config))

    logger.info("Загрузка списка диалогов для кэширования...")
    try:
        await client.get_dialogs(limit=None)
        logger.info("Диалоги успешно загружены.")
    except Exception as e:
        logger.warning(f"Не удалось загрузить список диалогов: {e}")

    logger.info(f"Разрешение сущности канала @{channel_username}...")
    try:
        channel_entity = await client.get_entity(channel_username)
        logger.info(f"Сущность канала успешно разрешена: ID={channel_entity.id}, Title={channel_entity.title}")
        
        # Автоподписка на канал
        logger.info(f"Подписка на канал @{channel_username}...")
        try:
            await client(JoinChannelRequest(channel_entity))
            logger.info(f"Успешно подписались/проверили подписку на канал @{channel_username}.")
        except Exception as e:
            logger.warning(f"Не удалось автоматически подписаться на канал: {e}")
            
    except Exception as e:
        logger.error(f"Не удалось получить сущность канала @{channel_username}: {e}")
        logger.warning("Используем текстовое имя канала для регистрации обработчика.")
        channel_entity = channel_username

    @client.on(events.NewMessage(chats=channel_entity))
    async def new_message_handler(event):
        message_text = event.message.message
        if not message_text:
            return

        pub = event.message.date.astimezone()
        now = datetime.now(pub.tzinfo)
        lag = (now - pub).total_seconds()
        logger.info(f"⏱ Публикация: {pub:%H:%M:%S} | Получено: {now:%H:%M:%S} | Задержка: {lag:.1f} сек")
        if lag < 0 or lag > 300:
            logger.warning(f"⚠️ Аномальная задержка {lag:.1f} сек — проверить часы/сеть")
        logger.info(f"Новое сообщение в канале {channel_username}: {message_text[:60]}...")

        # push-обработчик оставлен только для диагностики задержки;
        # отправка перенесена в poll_channel
        should_send, match_reason = should_forward(message_text, config)
        if should_send:
            logger.info(f"[push-диагностика] совпадение ({match_reason}) — отправка через poll_channel")
        else:
            logger.info("[push-диагностика] сообщение не в фокусе")

    # Активный опрос канала каждые 20 сек — источник истины для отправки уведомлений
    asyncio.create_task(poll_channel(client, channel_entity, channel_username, config))

    logger.info(f"Успешно подключено! Мониторинг канала @{channel_username} запущен.")
    logger.info(f"Список отслеживаемых ключевых слов: {keywords}")
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, send_telegram_bot_message_to_all, "🟢 <b>Бот запущен</b>\nМониторинг тревог активен.", config)
        logger.info("Стартовое уведомление отправлено подписчикам.")
    except Exception as e:
        logger.error(f"Не вдалося надіслати стартове повідомлення: {e}")
    
    # Ожидание новых сообщений
    await client.run_until_disconnected()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Работа монитора остановлена пользователем.")

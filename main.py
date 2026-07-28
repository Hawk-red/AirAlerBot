#!/usr/bin/env python3
import os
import sys
import json
import logging
import asyncio
import urllib.request
import urllib.parse
import urllib.error
from telethon import TelegramClient, events
from telethon.tl.functions.channels import JoinChannelRequest

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('alert_monitor.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

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
    text_lower = text.lower()
    for kw in keywords:
        if kw.lower() in text_lower:
            return kw
    return None

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
                            
        except Exception as e:
            logger.error(f"Ошибка в цикле фонового опроса бота: {e}")
            
        await asyncio.sleep(3)

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
            
        logger.info(f"Новое сообщение в канале {channel_username}: {message_text[:60]}...")
        
        matched_keyword = check_message_match(message_text, keywords)
        if matched_keyword:
            logger.warning(f"ОБНАРУЖЕНО СОВПАДЕНИЕ (ключ: {matched_keyword}): {message_text}")
            
            # Формируем красивое сообщение
            alert_text = (
                f"🚨 <b>КИЇВ / ОБЛАСТЬ (Увага!)</b> 🚨\n\n"
                f"{message_text}\n\n"
                f"🔗 <a href='https://t.me/{channel_username}/{event.message.id}'>Оригінал повідомлення</a>"
            )
            
            # Отправляем через бота всем подписчикам
            send_telegram_bot_message_to_all(alert_text, config)
        else:
            logger.info("Сообщение проигнорировано (нет ключевых слов для Киева/области).")

    logger.info(f"Успешно подключено! Мониторинг канала @{channel_username} запущен.")
    logger.info(f"Список отслеживаемых ключевых слов: {keywords}")
    
    # Ожидание новых сообщений
    await client.run_until_disconnected()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Работа монитора остановлена пользователем.")

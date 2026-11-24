import os
import sys
import time
import json
import logging
import threading
import telebot
from telebot import types

from data_manager import staff_mgr, load_config, db_set_link, db_get_linked_nick

# Logging configuration
logging.basicConfig(
    format="%(asctime)s (%(filename)s:%(lineno)d %(threadName)s) %(levelname)s - %(name)s: \"%(message)s\"",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO
)
logger = logging.getLogger("AQUA_STAFF_BOT")

cfg = load_config()
BOT_TOKEN = cfg.get("bot_token")
if not BOT_TOKEN:
    logger.error("Критическая ошибка: bot_token не указан в config.json или .env!")
    sys.exit(1)

ADMIN_IDS = set(cfg.get("admin_ids", [1399524277, 5594041416]))
CACHE_TTL = cfg.get("cache_ttl_minutes", 5) * 60

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# User state storage for FSM (in-memory)
user_states = {} # user_id -> str state

def is_senior_admin(user_id, adm_info=None):
    if user_id in ADMIN_IDS:
        return True
    if adm_info:
        role = adm_info.get("role", "").lower()
        if any(w in role for w in ["главный", "заместитель главного", "куратор"]):
            return True
        try:
            lvl = float(adm_info.get("lvl", "0"))
            if lvl >= 4:
                return True
        except Exception:
            pass
    return False

def _safe_send_message(chat_id, text, reply_markup=None, disable_preview=True):
    try:
        return bot.send_message(
            chat_id, 
            text, 
            parse_mode="HTML", 
            reply_markup=reply_markup, 
            disable_web_page_preview=disable_preview
        )
    except Exception as e:
        err_str = str(e)
        logger.warning(f"Ошибка отправки HTML сообщения ({chat_id}): {err_str}. Пробуем без HTML...")
        try:
            import re
            clean_text = re.sub(r'<[^>]+>', '', text)
            return bot.send_message(
                chat_id, 
                clean_text, 
                parse_mode=None, 
                reply_markup=reply_markup, 
                disable_web_page_preview=disable_preview
            )
        except Exception as e2:
            logger.error(f"Не удалось отправить сообщение ({chat_id}): {e2}")
            return None

def _safe_edit_message(chat_id, message_id, text, reply_markup=None):
    try:
        return bot.edit_message_text(
            text, 
            chat_id=chat_id, 
            message_id=message_id, 
            parse_mode="HTML", 
            reply_markup=reply_markup, 
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.warning(f"Ошибка редактирования HTML сообщения: {e}. Пробуем без HTML...")
        try:
            import re
            clean_text = re.sub(r'<[^>]+>', '', text)
            return bot.edit_message_text(
                clean_text, 
                chat_id=chat_id, 
                message_id=message_id, 
                parse_mode=None, 
                reply_markup=reply_markup, 
                disable_web_page_preview=True
            )
        except Exception as e2:
            logger.error(f"Не удалось отредактировать сообщение: {e2}")
            return None

def get_main_keyboard(user_id, adm_info=None):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn_my = types.KeyboardButton("📊 Моя статистика")
    
    if is_senior_admin(user_id, adm_info):
        markup.row(btn_my)
        btn_find = types.KeyboardButton("🔍 Найти админа")
        btn_refresh = types.KeyboardButton("🔄 Обновить данные")
        markup.row(btn_find, btn_refresh)
    else:
        markup.row(btn_my)
        
    return markup

def get_card_inline_keyboard():
    markup = types.InlineKeyboardMarkup()
    btn_reload = types.InlineKeyboardButton("🔄 Обновить карточку", callback_data="refresh_my_card")
    markup.add(btn_reload)
    return markup

def _deny_access(user_id):
    """Отказ в доступе и удаление клавиатуры, если Telegram ID не найден в таблице."""
    user_states.pop(user_id, None)
    text = (
        "⛔ <b>Доступ запрещён.</b>\n\n"
        f"Ваш Telegram ID (<code>{user_id}</code>) не найден в таблице администрации."
    )
    _safe_send_message(user_id, text, reply_markup=types.ReplyKeyboardRemove())

# --- Handlers ---

@bot.message_handler(commands=['start', 'help'])
def cmd_start(message):
    user_id = message.from_user.id
    username = message.from_user.username
    user_states.pop(user_id, None)

    adm = staff_mgr.find_admin_for_telegram_user(user_id, username)
    if not adm:
        _deny_access(user_id)
        return

    kb = get_main_keyboard(user_id, adm)
    text = (
        f"👋 Привет, <b>{adm['nick']}</b>!\n"
        f"Твой профиль успешно распознан в таблице администрации.\n\n"
        f"Нажми кнопку <b>[📊 Моя статистика]</b> ниже, чтобы просмотреть актуальную информацию о себе."
    )
    _safe_send_message(user_id, text, reply_markup=kb)

@bot.message_handler(func=lambda msg: msg.text in ["📊 Моя статистика", "/my", "/stats", "/stat", "/me"])
def cmd_my_stats(message):
    user_id = message.from_user.id
    username = message.from_user.username
    user_states.pop(user_id, None)

    adm = staff_mgr.find_admin_for_telegram_user(user_id, username)
    if not adm:
        _deny_access(user_id)
        return

    card = staff_mgr.format_card(adm)
    card += f"\n\n🕒 <i>Последнее обновление таблицы: {staff_mgr.last_update_str}</i>"
    inline_kb = get_card_inline_keyboard()
    _safe_send_message(user_id, card, reply_markup=inline_kb)

@bot.message_handler(func=lambda msg: msg.text == "🔍 Найти админа" or (msg.text and (msg.text.startswith("/find") or msg.text.startswith("/check"))))
def cmd_find_admin(message):
    user_id = message.from_user.id
    username = message.from_user.username

    adm_caller = staff_mgr.find_admin_for_telegram_user(user_id, username)
    if not adm_caller:
        _deny_access(user_id)
        return
    if not is_senior_admin(user_id, adm_caller):
        _safe_send_message(user_id, "⛔ Данная функция доступна только руководству администрации.")
        return
    
    # Check if arguments provided in command
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1 and parts[0] in ["/find", "/check"]:
        target_query = parts[1].strip()
        adm = staff_mgr.search_by_nick(target_query)
        if adm:
            card = staff_mgr.format_card(adm)
            card += f"\n\n🕒 <i>Последнее обновление таблицы: {staff_mgr.last_update_str}</i>"
            _safe_send_message(user_id, card)
        else:
            _safe_send_message(user_id, f"❌ Администратор с ником <code>{target_query}</code> не найден в таблице.")
        return

    user_states[user_id] = "WAITING_SEARCH"
    _safe_send_message(user_id, "🔎 Введи ник администратора для поиска (например: <code>Ivan_Ivanov</code>):")

@bot.message_handler(func=lambda msg: msg.text == "🔄 Обновить данные" or msg.text == "/refresh")
def cmd_refresh_data(message):
    user_id = message.from_user.id
    adm = staff_mgr.find_admin_for_telegram_user(user_id, message.from_user.username)
    if not adm:
        _deny_access(user_id)
        return
    if not is_senior_admin(user_id, adm):
        _safe_send_message(user_id, "⛔ Данная функция доступна только руководству администрации.")
        return

    msg = _safe_send_message(user_id, "⏳ <i>Запрашиваю актуальные данные из Google Таблицы...</i>")
    ok, res = staff_mgr.fetch_and_parse()
    if ok:
        text = (
            f"✅ <b>Таблица успешно обновлена!</b>\n\n"
            f"👥 Загружено администраторов: <b>{len(staff_mgr.admins_by_nick)}</b>\n"
            f"🕒 Время обновления: <b>{staff_mgr.last_update_str}</b>"
        )
    else:
        text = f"❌ <b>Ошибка обновления:</b> {res}"
        
    if msg:
        _safe_edit_message(user_id, msg.message_id, text)
    else:
        _safe_send_message(user_id, text)

@bot.callback_query_handler(func=lambda call: call.data == "refresh_my_card")
def cb_refresh_my_card(call):
    user_id = call.from_user.id
    username = call.from_user.username
    
    # Smart throttle: if last update was more than 30 seconds ago, fetch fresh data
    if time.time() - staff_mgr.last_update_ts > 30:
        staff_mgr.fetch_and_parse()
        
    adm = staff_mgr.find_admin_for_telegram_user(user_id, username)
    if not adm:
        bot.answer_callback_query(call.id, "⛔ Доступ запрещён. Ваш Telegram ID отсутствует в таблице.", show_alert=True)
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass
        return
        
    card = staff_mgr.format_card(adm)
    card += f"\n\n🕒 <i>Последнее обновление таблицы: {staff_mgr.last_update_str}</i>"
    inline_kb = get_card_inline_keyboard()
    
    try:
        _safe_edit_message(call.message.chat.id, call.message.message_id, card, reply_markup=inline_kb)
        bot.answer_callback_query(call.id, "Данные обновлены!")
    except Exception:
        bot.answer_callback_query(call.id, "Карточка актуальна")

@bot.message_handler(func=lambda msg: True, content_types=['text'])
def handle_text(message):
    user_id = message.from_user.id
    username = message.from_user.username
    
    adm_caller = staff_mgr.find_admin_for_telegram_user(user_id, username)
    if not adm_caller:
        _deny_access(user_id)
        return

    state = user_states.get(user_id)
    text = message.text.strip()

    if state == "WAITING_SEARCH":
        user_states.pop(user_id, None)
        if not is_senior_admin(user_id, adm_caller):
            return
        adm = staff_mgr.search_by_nick(text)
        if adm:
            card = staff_mgr.format_card(adm)
            card += f"\n\n🕒 <i>Последнее обновление таблицы: {staff_mgr.last_update_str}</i>"
            _safe_send_message(user_id, card)
        else:
            _safe_send_message(user_id, f"❌ Администратор с ником <code>{text}</code> не найден в таблице.")
        return

    # Default fallback for authorized user
    kb = get_main_keyboard(user_id, adm_caller)
    _safe_send_message(user_id, "Выбери действие на клавиатуре ниже 👇", reply_markup=kb)

@bot.message_handler(func=lambda msg: True)
def handle_other_content(message):
    user_id = message.from_user.id
    adm = staff_mgr.find_admin_for_telegram_user(user_id, message.from_user.username)
    if not adm:
        _deny_access(user_id)
        return
    kb = get_main_keyboard(user_id, adm)
    _safe_send_message(user_id, "Используй кнопку ниже 👇", reply_markup=kb)

# Background auto-refresher
def background_sync_worker():
    logger.info("Фоновый поток авто-синхронизации таблицы запущен.")
    while True:
        try:
            time.sleep(CACHE_TTL)
            ok, res = staff_mgr.fetch_and_parse()
            if ok:
                logger.info(f"Фоновая авто-синхронизация успешна: {res}")
            else:
                logger.warning(f"Фоновая синхронизация завершилась с ошибкой: {res}")
        except Exception as e:
            logger.error(f"Исключение в потоке синхронизации: {e}")
            time.sleep(60)

def main():
    print("=" * 60)
    print("🚀 Запуск AQUA STAFF BOT (@aqua18admin_bot)...")
    print("=" * 60)
    
    # Initial data load
    logger.info("Загрузка данных из таблицы при старте...")
    ok, res = staff_mgr.fetch_and_parse()
    if ok:
        logger.info(f"Начальная загрузка успешна: {res}")
    else:
        logger.error(f"Не удалось загрузить начальные данные: {res}")

    # Start background daemon
    t = threading.Thread(target=background_sync_worker, daemon=True)
    t.start()

    logger.info("Бот готов к приёму сообщений (infinity_polling)...")
    bot.infinity_polling(timeout=20, long_polling_timeout=20)

if __name__ == "__main__":
    main()

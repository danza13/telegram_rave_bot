import os
import io
import json
import sys
import logging
import datetime
import re
import gspread
from flask import Flask, request

from telegram import (
    Update,
    Bot,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Dispatcher,
    CommandHandler,
    CallbackContext,
    MessageHandler,
    Filters,
    CallbackQueryHandler,
    ConversationHandler,
)

# === Налаштування логування ===
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# === Якщо credentials.json не існує, створюємо його зі змінної середовища ===
if not os.path.exists("credentials.json"):
    creds = os.environ.get("GOOGLE_CREDENTIALS")
    if creds:
        try:
            json.loads(creds)  # Перевірка валідності JSON
            with open("credentials.json", "w", encoding="utf-8") as f:
                f.write(creds)
            logger.info("Файл credentials.json створено з Render Secrets.")
        except json.JSONDecodeError as e:
            logger.error("Невірний формат JSON у змінній GOOGLE_CREDENTIALS: %s", e)
            sys.exit(1)
    else:
        logger.error("Змінна середовища GOOGLE_CREDENTIALS не знайдена!")
        sys.exit(1)

# =======================
# Налаштування бота та імена файлів
# =======================
TOKEN = os.environ.get("TELEGRAM_TOKEN")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
ADMIN_IDS = [1124775269, 382701754]  # ID адміністраторів

# Використання внутрішнього сховища процесу на Render
DATA_DIR = os.getenv("DATA_DIR", "/data")
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR, exist_ok=True)

SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
USERS_FILE = os.path.join(DATA_DIR, "users.txt")

# Дефолтні налаштування заходу
default_settings = {
    "event_date": "18.02",
    "event_time": "20:00",
    "event_location": "Club XYZ"
}

# === Функції для роботи з файлами у внутрішньому сховищі ===
def ensure_local_file(file_path, default_content):
    """Якщо файл не існує, створити його з дефолтним вмістом."""
    if not os.path.exists(file_path):
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(default_content)
            logger.info("Файл %s створено локально.", file_path)
        except Exception as e:
            logger.error("Помилка створення файлу %s: %s", file_path, e)

# === Завантаження та збереження налаштувань із внутрішнього сховища ===
def load_settings():
    global event_date, event_time, event_location
    default_settings_content = json.dumps(default_settings, ensure_ascii=False, indent=4)
    ensure_local_file(SETTINGS_FILE, default_settings_content)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            settings = json.load(f)
            event_date = settings.get("event_date", default_settings["event_date"])
            event_time = settings.get("event_time", default_settings["event_time"])
            event_location = settings.get("event_location", default_settings["event_location"])
            logger.info("Налаштування завантажено: Дата: %s, Час: %s, Локація: %s",
                        event_date, event_time, event_location)
    except Exception as e:
        logger.error("Помилка завантаження налаштувань: %s", e)

def save_settings():
    settings = {
        "event_date": event_date,
        "event_time": event_time,
        "event_location": event_location,
    }
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=4)
        logger.info("Налаштування збережено локально.")
    except Exception as e:
        logger.error("Помилка збереження налаштувань: %s", e)

# === Завантаження списку користувачів із внутрішнього сховища ===
def load_users():
    ensure_local_file(USERS_FILE, "")  # Порожній файл за замовчуванням
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            users = f.read().splitlines()
        logger.info("Завантажено %d користувачів із %s", len(users), USERS_FILE)
        return users
    except Exception as e:
        logger.error("Помилка при зчитуванні %s: %s", USERS_FILE, e)
        return []

# === Функції для роботи з користувачами ===
def add_user(user_id: int):
    try:
        users = load_users()
        if str(user_id) not in users:
            with open(USERS_FILE, "a", encoding="utf-8") as f:
                f.write(str(user_id) + "\n")
            logger.info("Користувача %d додано у %s", user_id, USERS_FILE)
    except Exception as e:
        logger.error("Помилка додавання користувача: %s", e)

def store_registration(user_data: dict):
    try:
        gc = gspread.service_account(filename="credentials.json")
        sh = gc.open_by_key(SPREADSHEET_ID)
        sheet_name = event_date
        try:
            worksheet = sh.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            worksheet = sh.add_worksheet(title=sheet_name, rows="100", cols="20")
            worksheet.append_row(["Ім'я", "Телефон", "Telegram", "Джерело"])
        worksheet.append_row([
            user_data.get("name"),
            user_data.get("phone"),
            user_data.get("username"),
            user_data.get("source"),
        ])
    except Exception as e:
        logger.error("Помилка запису в Google Sheets: %s", e)

# === Допоміжні функції ===
def get_weekday(date_str: str) -> str:
    try:
        day, month = map(int, date_str.split('.'))
    except Exception as e:
        logger.error("Невірний формат дати: %s. Помилка: %s", date_str, e)
        return "невідомий день"
    now = datetime.datetime.now()
    year = now.year
    try:
        event_date_obj = datetime.datetime(year, month, day)
    except ValueError:
        return "невідомий день"
    if event_date_obj < now:
        try:
            event_date_obj = datetime.datetime(year + 1, month, day)
        except ValueError:
            pass
    weekdays = {
        0: "Понеділок",
        1: "Вівторок",
        2: "Середа",
        3: "Четвер",
        4: "П’ятниця",
        5: "Субота",
        6: "Неділя",
    }
    return weekdays.get(event_date_obj.weekday(), "невідомий день")

def get_invitation_message() -> str:
    weekday = get_weekday(event_date)
    message = (
        f"Привіт! Запрошую тебе на вечірку в {weekday}, {event_date}, початок о {event_time}\n"
        f"{event_location}\n\n"
        "Чи будеш ти з нами?"
    )
    return message

# === Визначення констант для станів розмови ===
NAME, PHONE, USERNAME, SOURCE = range(4)
ADMIN_DATE, ADMIN_TIME, ADMIN_LOCATION, ADMIN_BROADCAST = range(4, 8)

# === Хендлери для користувача ===
def start_command(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    add_user(chat_id)
    update.message.reply_text(
        "Вітаю! Щоб отримати безкоштовне запрошення на вечірку, натисни команду /starts в меню."
    )

def starts(update: Update, context: CallbackContext):
    add_user(update.effective_chat.id)
    msg = update.message.reply_text("\u2063", reply_markup=ReplyKeyboardRemove())
    context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg.message_id)
    text = get_invitation_message()
    keyboard = [
        [
            InlineKeyboardButton("Так", callback_data="yes"),
            InlineKeyboardButton("Ні", callback_data="no")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(text, reply_markup=reply_markup)

def invitation_response(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    try:
        query.message.delete()
    except Exception as e:
        logger.error("Помилка при видаленні повідомлення запрошення: %s", e)
    chat_id = update.effective_chat.id
    if query.data == "yes":
        context.bot.send_message(chat_id=chat_id, text="Введіть ваше ім'я:")
        return NAME
    elif query.data == "no":
        keyboard = [[InlineKeyboardButton("Назад", callback_data="back")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        context.bot.send_message(
            chat_id=chat_id,
            text="Зрозуміло! Тоді чекаємо тебе наступного разу, або ж передумай та приходь!",
            reply_markup=reply_markup
        )
        return ConversationHandler.END

def back_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    text = get_invitation_message()
    keyboard = [
        [
            InlineKeyboardButton("Так", callback_data="yes"),
            InlineKeyboardButton("Ні", callback_data="no")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    query.edit_message_text(text=text, reply_markup=reply_markup)

def get_name(update: Update, context: CallbackContext):
    user_text = update.message.text
    if user_text.strip().lower() == "відміна":
        return cancel(update, context)
    context.user_data["name"] = user_text
    contact_button = KeyboardButton("Поділитись контактом", request_contact=True)
    reply_markup = ReplyKeyboardMarkup(
        [[contact_button], ["Відміна"]],
        one_time_keyboard=False,
        resize_keyboard=True
    )
    update.message.reply_text("Введіть номер телефону або поділіться контактом:", reply_markup=reply_markup)
    return PHONE

def get_phone(update: Update, context: CallbackContext):
    if update.message.contact:
        phone = update.message.contact.phone_number
    else:
        phone = update.message.text.strip()
        if phone.lower() == "відміна":
            return cancel(update, context)
    phone = re.sub(r"[^\d+]", "", phone)
    if not phone.startswith("+"):
        phone = "+" + phone
    logger.info("Очищений номер телефону: %s", phone)
    if not (phone.startswith("+380") and len(phone) == 13 and phone[1:].isdigit()):
        update.message.reply_text("Будь ласка, введіть коректний номер телефону у форматі +380XXXXXXXXX.")
        return PHONE
    context.user_data["phone"] = phone
    reply_markup = ReplyKeyboardMarkup([["Відміна"]], one_time_keyboard=False, resize_keyboard=True)
    update.message.reply_text("Введіть ваш Telegram нік (через @):", reply_markup=reply_markup)
    return USERNAME

def get_username(update: Update, context: CallbackContext):
    username = update.message.text.strip()
    if username.lower() == "відміна":
        return cancel(update, context)
    if not username.startswith("@"):
        update.message.reply_text("Будь ласка, введіть ваш Telegram нік, який починається з @.")
        return USERNAME
    context.user_data["username"] = username
    reply_markup = ReplyKeyboardMarkup([["Відміна"]], one_time_keyboard=False, resize_keyboard=True)
    update.message.reply_text(
        "Де ви побачили інформацію про вечірку?\n(наприклад: інстаграм реклама, інстаграм сторінка, телеграм канал Холі, інший телеграм канал)",
        reply_markup=reply_markup
    )
    return SOURCE

def get_source(update: Update, context: CallbackContext):
    source_text = update.message.text.strip()
    if source_text.lower() == "відміна":
        return cancel(update, context)
    context.user_data["source"] = source_text
    store_registration(context.user_data)
    update.message.reply_text("Дякуємо за реєстрацію, чекаємо вас на вході для перевірки інформації 🫶🏻",
                                reply_markup=ReplyKeyboardRemove())
    social_text = (
        "Залишайся з ботом до самої вечірки, адже через нього тобі будуть надходити важливі повідомлення щодо деталей заходу!\n\n"
        "Підпишись на наші соціальні мережі та будь в курсі новин 👇🏻"
    )
    keyboard = [
        [InlineKeyboardButton("Телеграм канал з додатковою інформацією", url="https://t.me/holytusa")],
        [InlineKeyboardButton("Чат для спілкування та знайомств", url="https://t.me/+yOxlMtK2JDZlNWUy")],
        [InlineKeyboardButton("Instagram", url="https://www.instagram.com/holy.tusa")]
    ]
    reply_markup_inline = InlineKeyboardMarkup(keyboard)
    update.message.reply_text(social_text, reply_markup=reply_markup_inline)
    return ConversationHandler.END

def cancel(update: Update, context: CallbackContext):
    update.message.reply_text("Реєстрацію скасовано.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# === Хендлери для адміністратора ===
def admin(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        update.message.reply_text("Ви не маєте доступу до цієї команди.")
        return
    keyboard = [
        [InlineKeyboardButton("Змінити інформацію про захід", callback_data="admin_change")],
        [InlineKeyboardButton("Розсилка", callback_data="admin_broadcast")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    update.message.reply_text("Адмін панель:", reply_markup=reply_markup)

def admin_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    if query.data == "admin_change":
        query.message.reply_text("Введіть нову дату заходу (формат дд.мм):")
        return ADMIN_DATE
    elif query.data == "admin_broadcast":
        query.message.reply_text("Введіть текст повідомлення для розсилки:")
        return ADMIN_BROADCAST

def admin_set_date(update: Update, context: CallbackContext):
    global event_date
    event_date = update.message.text
    update.message.reply_text("Введіть новий час заходу (формат гг:хх):")
    return ADMIN_TIME

def admin_set_time(update: Update, context: CallbackContext):
    global event_time
    event_time = update.message.text
    update.message.reply_text("Введіть нову локацію:")
    return ADMIN_LOCATION

def admin_set_location(update: Update, context: CallbackContext):
    global event_location
    event_location = update.message.text
    save_settings()
    update.message.reply_text("Інформація про захід оновлена!")
    return ConversationHandler.END

def admin_broadcast_message(update: Update, context: CallbackContext):
    message_text = update.message.text
    users = load_users()
    if users:
        count = 0
        for uid in users:
            try:
                context.bot.send_message(chat_id=int(uid), text=message_text)
                count += 1
            except Exception as e:
                logger.error("Помилка відправки повідомлення користувачу %s: %s", uid, e)
        update.message.reply_text(f"Розсилка завершена. Повідомлення відправлено {count} користувачам.")
    else:
        update.message.reply_text("Немає користувачів для розсилки.")
    return ConversationHandler.END

def admin_cancel(update: Update, context: CallbackContext):
    update.message.reply_text("Адмін операцію скасовано.")
    return ConversationHandler.END

def error_handler(update: object, context: CallbackContext):
    logger.error("Виникла помилка: ", exc_info=context.error)

# === Налаштування диспетчера ===
bot = Bot(TOKEN)
dispatcher = Dispatcher(bot, None, workers=0, use_context=True)

reg_conv_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(invitation_response, pattern="^(yes|no)$")],
    states={
        NAME: [
            MessageHandler(Filters.text & ~Filters.command, get_name),
            MessageHandler(Filters.regex("^Відміна$"), cancel)
        ],
        PHONE: [
            MessageHandler(Filters.contact | (Filters.text & ~Filters.command), get_phone),
            MessageHandler(Filters.regex("^Відміна$"), cancel)
        ],
        USERNAME: [
            MessageHandler(Filters.text & ~Filters.command, get_username),
            MessageHandler(Filters.regex("^Відміна$"), cancel)
        ],
        SOURCE: [
            MessageHandler(Filters.text & ~Filters.command, get_source),
            MessageHandler(Filters.regex("^Відміна$"), cancel)
        ],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)

admin_conv_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(admin_callback, pattern="^(admin_change|admin_broadcast)$")],
    states={
        ADMIN_DATE: [MessageHandler(Filters.text & ~Filters.command, admin_set_date)],
        ADMIN_TIME: [MessageHandler(Filters.text & ~Filters.command, admin_set_time)],
        ADMIN_LOCATION: [MessageHandler(Filters.text & ~Filters.command, admin_set_location)],
        ADMIN_BROADCAST: [MessageHandler(Filters.text & ~Filters.command, admin_broadcast_message)],
    },
    fallbacks=[CommandHandler("cancel", admin_cancel)],
)

dispatcher.add_handler(CommandHandler("start", start_command))
dispatcher.add_handler(CommandHandler("starts", starts))
dispatcher.add_handler(reg_conv_handler)
dispatcher.add_handler(CommandHandler("admin", admin))
dispatcher.add_handler(admin_conv_handler)
dispatcher.add_handler(CallbackQueryHandler(back_handler, pattern="^back$"))
dispatcher.add_error_handler(error_handler)

# === Flask-додаток для вебхуку та health check ===
app = Flask(__name__)

@app.route("/", methods=["GET"])
def index():
    return "ok", 200

@app.route("/" + TOKEN, methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    dispatcher.process_update(update)
    return "ok", 200

# === Основна функція ===
def main():
    load_settings()   # Завантаження налаштувань із внутрішнього сховища
    load_users()      # Завантаження списку користувачів із внутрішнього сховища
    
    port = int(os.environ.get("PORT", "8443"))
    WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # Наприклад, "https://your-app.onrender.com/"
    if not WEBHOOK_URL.endswith("/"):
        WEBHOOK_URL += "/"
    
    bot.delete_webhook()
    bot.set_webhook(WEBHOOK_URL + TOKEN)
    logger.info("Бот запущено через вебхук!")
    
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    main()

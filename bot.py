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

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

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
# Налаштування бота та файли
# =======================
TOKEN = os.environ.get("TELEGRAM_TOKEN")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
ADMIN_IDS = [1124775269, 382701754]  # ID адміністраторів

SETTINGS_FILE = "settings.json"  # Локальний файл налаштувань
USERS_FILE = "users.txt"         # Локальний файл з ID користувачів

# Глобальні змінні заходу (за замовчуванням)
event_date = "18.02"      # формат: дд.мм
event_time = "20:00"      # формат: гг:хх
event_location = "Club XYZ"

# ==================================
# Налаштування Google Drive API
# ==================================
DRIVE_SCOPES = ['https://www.googleapis.com/auth/drive.file']
SERVICE_ACCOUNT_FILE = 'credentials.json'

try:
    drive_creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=DRIVE_SCOPES
    )
    drive_service = build('drive', 'v3', credentials=drive_creds)
    logger.info("Google Drive service ініціалізовано.")
except Exception as e:
    logger.error(f"Помилка ініціалізації Google Drive service: {e}")

# Функція завантаження файлу з Google Drive
def download_file_from_drive(file_name, local_path):
    try:
        results = drive_service.files().list(
            q=f"name='{file_name}'",
            spaces='drive',
            fields='files(id, name)'
        ).execute()
        files = results.get('files', [])
        if files:
            file_id = files[0]['id']
            request_drive = drive_service.files().get_media(fileId=file_id)
            fh = io.FileIO(local_path, 'wb')
            downloader = MediaIoBaseDownload(fh, request_drive)
            done = False
            while not done:
                status, done = downloader.next_chunk()
            fh.close()
            logger.info("Файл %s завантажено з Google Drive.", file_name)
            return True
    except Exception as e:
        logger.error(f"Помилка завантаження {file_name} з Google Drive: {e}")
    return False

# Функція завантаження (оновлення) файлу на Google Drive
def upload_file_to_drive(local_path, file_name, drive_folder_id=None):
    file_metadata = {'name': file_name}
    if drive_folder_id:
        file_metadata['parents'] = [drive_folder_id]
    media = MediaFileUpload(local_path, mimetype='text/plain')
    try:
        results = drive_service.files().list(
            q=f"name='{file_name}'", spaces='drive', fields='files(id, name)'
        ).execute()
        files = results.get('files', [])
        if files:
            file_id = files[0]['id']
            updated_file = drive_service.files().update(
                fileId=file_id, media_body=media
            ).execute()
            logger.info("Файл %s оновлено на Google Drive.", file_name)
            return updated_file.get('id')
        else:
            file = drive_service.files().create(
                body=file_metadata, media_body=media, fields='id'
            ).execute()
            logger.info("Файл %s створено на Google Drive.", file_name)
            return file.get('id')
    except Exception as e:
        logger.error(f"Помилка завантаження файлу {file_name} на Google Drive: {e}")
        return None

# === Завантаження та збереження налаштувань ===
def load_settings():
    global event_date, event_time, event_location
    # Якщо локально відсутній файл, спробуємо завантажити його з Drive
    if not os.path.exists(SETTINGS_FILE):
        download_file_from_drive("settings.json", SETTINGS_FILE)
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                settings = json.load(f)
                event_date = settings.get("event_date", event_date)
                event_time = settings.get("event_time", event_time)
                event_location = settings.get("event_location", event_location)
                logger.info("Налаштування завантажено з файлу. (Дата: %s, Час: %s, Локація: %s)",
                            event_date, event_time, event_location)
        except Exception as e:
            logger.error(f"Помилка завантаження налаштувань: {e}")

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
        upload_file_to_drive(SETTINGS_FILE, "settings.json")
    except Exception as e:
        logger.error(f"Помилка збереження налаштувань: {e}")

# === Завантаження списку користувачів із файлу users.txt ===
def load_users():
    # Якщо файлу немає, спробуємо завантажити його з Drive
    if not os.path.exists(USERS_FILE):
        download_file_from_drive("users.txt", USERS_FILE)
    users = []
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r") as f:
                users = f.read().splitlines()
            logger.info("Завантажено %d користувачів з %s", len(users), USERS_FILE)
        except Exception as e:
            logger.error("Помилка при зчитуванні %s: %s", USERS_FILE, e)
    else:
        logger.info("Файл %s не знайдено.", USERS_FILE)
    return users

# === Функції для роботи з користувачами ===
def add_user(user_id: int):
    try:
        load_users()  # Завантаження локального файлу, якщо ще не завантажено
        users = []
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE, "r") as f:
                users = f.read().splitlines()
        if str(user_id) not in users:
            with open(USERS_FILE, "a") as f:
                f.write(str(user_id) + "\n")
            logger.info("Користувача %d додано у %s", user_id, USERS_FILE)
            upload_file_to_drive(USERS_FILE, "users.txt")
    except Exception as e:
        logger.error(f"Помилка додавання користувача: {e}")

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
        logger.error(f"Помилка запису в Google Sheets: {e}")

# === Допоміжні функції ===
def get_weekday(date_str: str) -> str:
    try:
        day, month = map(int, date_str.split('.'))
    except Exception as e:
        logger.error(f"Невірний формат дати: {date_str}. Помилка: {e}")
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

# === Хендлери для користувача ===
def start(update: Update, context: CallbackContext):
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
    update.message.reply_text("Дякуємо за реєстрацію, чекаємо вас на вході для перевірки інформації 🫶🏻", reply_markup=ReplyKeyboardRemove())
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
    users = load_users()  # Зчитування користувачів із users.txt
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

dispatcher.add_handler(CommandHandler("start", start))
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
    load_settings()
    # Завантажуємо список користувачів і логування кількості
    load_users()
    
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

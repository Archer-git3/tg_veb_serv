import os
import json
import asyncio

from datetime import datetime
from telethon import TelegramClient, events, errors, types
from telethon.sessions import StringSession
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import time
import pickle  # Додано для роботи з бінарними файлами

# Конфігурація
API_ID = 29148113
API_HASH = "0fba92868b9d99d1e63583a8fb751fb4"
BOT_TOKEN = "7603687034:AAG9102_4yFSuHrwE17FgO-Fc8nnfL1Z4-8"
ACCOUNTS_FILE = "telegram_accounts.json"  # Змінимо розширення на pkl
NOTIFICATION_CHATS_FILE = "notification_chats.json"  # Змінимо розширення на pkl

SESSION_TIMEOUT = 60
ACCOUNTS_CHECK_INTERVAL = 30

# Список спеціальних користувачів (user_id)
SPECIAL_USERS = ["fgtaaaqd", "іншийкористувач"]


# Глобальні змінні
clients = {}
notification_chats = {}
admins = set()
message_queue = asyncio.Queue()
last_accounts_check = 0
last_accounts_mtime = 0


class AccountClient:
    def __init__(self, account_data):
        self.account_data = account_data
        self.client = None
        self.is_running = False
        self.me = None
        self.is_special = account_data.get('is_special', False)
        self.last_updated = account_data.get('last_updated')
        if isinstance(self.last_updated, str):
            try:
                self.last_updated = datetime.fromisoformat(self.last_updated)
            except ValueError:
                self.last_updated = datetime.now()

    async def start(self):
        if self.client and self.client.is_connected():
            return True

        try:
            self.client = TelegramClient(
                StringSession(self.account_data['session_string']),
                API_ID,
                API_HASH,
                timeout=SESSION_TIMEOUT
            )
            await self.client.connect()

            if not await self.client.is_user_authorized():
                return False

            self.me = await self.client.get_me()
            self.is_running = True
            return True
        except Exception as e:
            logger.error(f"Помилка запуску клієнта {self.account_data['name']}: {e}")
            return False

    async def stop(self):
        if self.client and self.client.is_connected():
            await self.client.disconnect()
        self.is_running = False


async def load_accounts():
    global last_accounts_mtime, admins

    try:
        # Перевіряємо, чи файл існує
        if not os.path.exists(ACCOUNTS_FILE):
            logger.warning("Файл акаунтів не знайдено")
            return False

        # Отримуємо час модифікації файлу
        current_mtime = os.path.getmtime(ACCOUNTS_FILE)

        # Перевіряємо, чи файл змінився
        if current_mtime <= last_accounts_mtime:
            return False

        last_accounts_mtime = current_mtime

        # Читаємо бінарний файл за допомогою pickle
        with open(ACCOUNTS_FILE, 'rb') as f:
            data = pickle.load(f)

        # Отримуємо акаунти з нової структури даних
        accounts = data.get("accounts", [])
        if not accounts:
            logger.warning("Файл акаунтів не містить даних")
            return False

        # Зупиняємо старі клієнти
        for client in list(clients.values()):
            await client.stop()
        clients.clear()

        # Очищаємо адмінів
        admins.clear()

        # Завантажуємо нові акаунти
        for account in accounts:
            if account.get('skip_check', False):
                continue

            client = AccountClient(account)
            if await client.start():
                clients[account['phone']] = client
                logger.info(f"Акаунт {account['name']} успішно завантажено")

                # Якщо акаунт є адміном, додаємо його user_id
                if account.get('is_admin', False) and client.me:
                    admins.add(client.me.id)
                    logger.info(f"Додано адміністратора: {client.me.id}")

        return True
    except Exception as e:
        logger.error(f"Помилка завантаження акаунтів: {e}")
        return False


async def load_notification_chats():
    global notification_chats
    try:
        if os.path.exists(NOTIFICATION_CHATS_FILE):
            with open(NOTIFICATION_CHATS_FILE, 'rb') as f:
                notification_chats = pickle.load(f)

                # Оновлюємо статус спеціальних користувачів
                for chat_id, settings in notification_chats.items():
                    username = settings.get('username')
                    if username:
                        settings['is_special'] = username.lower() in [u.lower() for u in SPECIAL_USERS]
    except Exception as e:
        logger.error(f"Помилка завантаження чатів: {e}")
        notification_chats = {}


async def save_notification_chats():
    try:
        with open(NOTIFICATION_CHATS_FILE, 'wb') as f:
            pickle.dump(notification_chats, f)
    except Exception as e:
        logger.error(f"Помилка збереження чатів: {e}")


async def message_listener(client: AccountClient):
    @client.client.on(events.NewMessage(incoming=True))
    async def handler(event):
        try:
            # Ігноруємо власні повідомлення
            if event.message.sender_id == client.me.id:
                return

            # Ігноруємо повідомлення від ботів
            sender = await event.get_sender()
            if isinstance(sender, types.User) and sender.bot:
                return

            # Перевіряємо, що це повідомлення від користувача
            if not isinstance(event.message.peer_id, types.PeerUser):
                return

            # Отримуємо ім'я користувача
            sender_name = "Невідомий"
            if sender:
                sender_name = sender.username or f"{sender.first_name or ''} {sender.last_name or ''}".strip()
                if not sender_name:
                    sender_name = f"user_{sender.id}"

            # Перевіряємо, чи це перше повідомлення в діалозі
            is_first_message = await is_first_in_dialog(client, event.message.peer_id.user_id)

            # Обробляємо текст повідомлення
            message_text = ""
            if event.message.text:
                message_text = event.message.text[:1000] + '...' if len(
                    event.message.text) > 1000 else event.message.text
            elif event.message.media:
                message_text = "📷 Медіа-повідомлення"

            message_info = {
                'account': client.account_data['name'],
                'sender': sender_name,
                'text': message_text,
                'date': event.message.date.isoformat(),
                'phone': client.account_data['phone'],
                'group': client.account_data['group'],
                'sender_id': sender.id if sender else 0,
                'is_first': is_first_message,
                'is_special': client.is_special
            }
            await message_queue.put(message_info)
        except Exception as e:
            logger.error(f"Помилка обробки повідомлення: {e}")


async def is_first_in_dialog(client, user_id):
    """Перевіряє, чи є повідомлення першим у діалозі"""
    try:
        # Отримуємо історію діалогу
        messages = await client.client.get_messages(
            user_id,
            limit=4,  # Беремо 2 повідомлення, щоб перевірити чи є попередні
            reverse=True
        )

        # Якщо повідомлень менше 2, значить це перше повідомлення
        return len(messages) < 2
    except Exception as e:
        logger.error(f"Помилка перевірки історії діалогу: {e}")
        return False


async def process_message_queue(bot: Bot):
    while True:
        message = await message_queue.get()
        for chat_id_str, settings in notification_chats.items():
            try:
                # Для спеціальних акаунтів - ігноруємо перевірку груп
                #if not message.get('is_special', False):
                # Для звичайних акаунтів - перевіряємо групи
                if 'groups' not in settings or message['group'] not in settings['groups']:
                    continue

                # Додаємо інформацію про перше повідомлення
                first_msg_info = "🌟 **Перше повідомлення!**\n" if message['is_first'] else ""

                # Додаємо індикатор спеціального акаунта
                special_indicator = "⭐ СПЕЦІАЛЬНИЙ АКАУНТ ⭐\n" if message.get('is_special', False) else ""

                text = (
                    f"🔔 **Нове повідомлення!**\n"
                    f"{special_indicator}"
                    f"{first_msg_info}"
                    f"👤 Акаунт: `{message['account']}`\n"
                    f"👤 Відправник: `{message['sender']}`\n"
                    f"📅 Дата: `{message['date']}`\n"
                    f"🏷️ Група: `{message['group']}`\n"
                    f"\n{message['text']}"
                )
                await bot.send_message(
                    chat_id=int(chat_id_str),
                    text=text,
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.error(f"Помилка відправки сповіщення: {e}")
        message_queue.task_done()


def is_admin(user_id):
    """Перевіряє, чи є користувач адміністратором"""
    return user_id in admins

def has_admin_rights(user_id):
    """Перевіряє, чи користувач має права адміністратора"""
    return user_id in admins or user_id in SPECIAL_USERS


async def group_selection_required( update: Update, context: ContextTypes.DEFAULT_TYPE, handler):
    """Декоратор для перевірки прав на вибір груп"""
    user_id = update.effective_user.id
    query = update.callback_query
    chat_id = query.message.chat_id
    chat_id_str = str(chat_id)
    settings = notification_chats.get(chat_id_str, {})
    username = settings.get('username')
    is_special = username and username.lower() in [u.lower() for u in SPECIAL_USERS]
    # Тільки спеціальні користувачі можуть вибирати групи


    # Викликаємо обробник
    return await handler(update, context)

async def admin_required(update: Update, context: ContextTypes.DEFAULT_TYPE, handler):
    """Декоратор для перевірки прав адміністратора"""
    user_id = update.effective_user.id

    # Базова перевірка на адміністратора
    if not is_admin(user_id) and user_id not in SPECIAL_USERS:
        if update.callback_query:
            await update.callback_query.answer("❌ Ви не маєте прав адміністратора!", show_alert=True)
        elif update.message:
            await update.message.reply_text("❌ Ви не маєте прав адміністратора!")
        return None

    # Викликаємо обробник
    return await handler(update, context)


async def show_accessible_groups(query, context: ContextTypes.DEFAULT_TYPE):
    user_id = query.from_user.id
    username = query.from_user.username
    is_special = username and username.lower() in [u.lower() for u in SPECIAL_USERS]
    chat_id_str = str(query.message.chat_id)

    # Отримуємо налаштування чату
    settings = notification_chats.get(chat_id_str, {})
    user_groups = settings.get('groups', [])

    # Шукаємо групу адміністратора з його акаунта
    user_group = None
    if not is_special:
        # Шукаємо акаунт, який відповідає адміністратору
        for client in clients.values():
            if client.me and client.me.id == user_id:
                user_group = client.account_data.get('group', '')
                break

        # Якщо не знайшли, використовуємо ім'я користувача
        if not user_group:
            user_group = username if username else f"ID: {user_id}"

    # Визначаємо доступні групи
    if is_special:
        # Для спеціальних користувачів - всі обрані групи
        groups_text = "🏷️ Ваші доступні групи:\n\n" + "\n".join(f"• `{group}`" for group in user_groups)
        if not user_groups:
            groups_text = "ℹ️ Ви ще не обрали жодної групи. Використайте '➕ Обрати групу'."
    else:
        # Для звичайних адміністраторів - тільки їх група
        groups_text = f"🏷️ Ваша група: `{user_group}`"

    await query.edit_message_text(
        groups_text,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]
        ])
    )


# Функція для показу вибору груп (для спеціальних користувачів)
async def show_group_selection(query, context: ContextTypes.DEFAULT_TYPE):
    # Отримуємо унікальні групи з усіх акаунтів
    all_groups = set()
    for client in clients.values():
        all_groups.add(client.account_data['group'])

    if not all_groups:
        await query.edit_message_text("ℹ️ Немає доступних груп для вибору.")
        return

    # Створюємо клавіатуру з кнопками груп
    keyboard = []
    current_row = []

    for group in sorted(all_groups):
        # Перевіряємо, чи група вже вибрана
        is_selected = False
        chat_id_str = str(query.message.chat_id)
        if chat_id_str in notification_chats and group in notification_chats[chat_id_str].get('groups', []):
            is_selected = True

        btn_text = f"✅ {group}" if is_selected else group
        current_row.append(InlineKeyboardButton(btn_text, callback_data=f"toggle_group:{group}"))

        if len(current_row) == 2:
            keyboard.append(current_row)
            current_row = []

    if current_row:
        keyboard.append(current_row)

    # Кнопки для збереження та скидання
    keyboard.append([
        InlineKeyboardButton("💾 Зберегти вибір", callback_data="save_groups"),
        InlineKeyboardButton("🧹 Скинути всі", callback_data="reset_groups"),
        InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    selected_count = 0
    chat_id_str = str(query.message.chat_id)
    if chat_id_str in notification_chats:
        selected_count = len(notification_chats[chat_id_str].get('groups', []))

    await query.edit_message_text(
        f"🏷️ Виберіть групи для моніторингу (вибрано: {selected_count}):\n\n"
        "ℹ️ Натисніть на групу, щоб додати або видалити її зі списку",
        reply_markup=reply_markup
    )


# Обробник для кнопок груп
async def group_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "select_group":
        await show_group_selection(query, context)
    elif query.data.startswith("toggle_group:"):
        await toggle_group_handler(update, context)
    elif query.data == "save_groups":
        await save_groups_handler(update, context)
    elif query.data == "reset_groups":
        await reset_groups_handler(update, context)
    elif query.data == "back_to_main":
        await start(update, context)


async def toggle_group_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    chat_id_str = str(chat_id)
    _, group = query.data.split(':')

    # Ініціалізуємо налаштування для чату, якщо потрібно
    if chat_id_str not in notification_chats:
        notification_chats[chat_id_str] = {
            'user_id': query.from_user.id,
            'groups': []
        }

    settings = notification_chats[chat_id_str]

    # Додаємо або видаляємо групу
    if group in settings['groups']:
        settings['groups'].remove(group)
    else:
        settings['groups'].append(group)

    await save_notification_chats()
    await show_group_selection(query, context)


async def save_groups_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    chat_id_str = str(chat_id)
    selected_count = 0

    if chat_id_str in notification_chats:
        selected_count = len(notification_chats[chat_id_str].get('groups', []))

    await query.edit_message_text(
        f"✅ Вибрано груп: {selected_count}\n\n"
        "Тепер ви будете отримувати сповіщення лише для обраних груп."
    )


async def reset_groups_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    chat_id_str = str(chat_id)

    if chat_id_str in notification_chats:
        notification_chats[chat_id_str]['groups'] = []
        await save_notification_chats()

    await query.edit_message_text("🧹 Всі групи скинуті! Ви не отримуватимете сповіщень.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Визначаємо тип виклику (повідомлення чи callback)
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        user = query.from_user
        message = query.message
    else:
        user = update.effective_user
        message = update.message

    user_id = user.id
    username = user.username
    chat_id = message.chat_id
    chat_id_str = str(chat_id)
    is_special = username and username.lower() in [u.lower() for u in SPECIAL_USERS]

    # Ініціалізація чату
    if chat_id_str not in notification_chats:
        notification_chats[chat_id_str] = {
            'user_id': user_id,
            'username': username,
            'groups': [],
            'is_special': is_special
        }
    else:
        # Оновлюємо дані користувача
        notification_chats[chat_id_str]['user_id'] = user_id
        notification_chats[chat_id_str]['username'] = username
        notification_chats[chat_id_str]['is_special'] = is_special

    await save_notification_chats()

    # Визначаємо статус користувача
    if is_special:
        admin_status = "⭐ Ви спеціальний користувач (повний доступ)"
    elif is_admin(user_id):
        admin_status = "✅ Ви адміністратор"
    else:
        admin_status = "❌ Ви не маєте прав адміністратора"

    # Створюємо клавіатуру в залежності від типу користувача
    keyboard = []

    # Кнопки для спеціальних користувачів
    if is_special:
        keyboard.append([
            InlineKeyboardButton("➕ Обрати групу", callback_data="select_group"),
        ])
        keyboard.append([
            InlineKeyboardButton("🔔 Перевірити сповіщення", callback_data="check_notifications"),
            InlineKeyboardButton("👁️ Доступні групи", callback_data="view_groups")
        ])

    # Кнопки для звичайних адміністраторів
    elif is_admin(user_id):
        keyboard.append([
            InlineKeyboardButton("🔔 Перевірити сповіщення", callback_data="check_notifications"),
            InlineKeyboardButton("👁️ Моя група", callback_data="view_groups")
        ])

    # Кнопки для всіх користувачів
    keyboard.append([
        InlineKeyboardButton("🔄 Оновити", callback_data="refresh"),
        InlineKeyboardButton("❌ Закрити", callback_data="close")
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    text = f"🔔 Бот активований! {admin_status}\n\nОберіть дію з меню:"

    # Відповідаємо в залежності від типу запиту
    if update.callback_query:
        await query.edit_message_text(text, reply_markup=reply_markup)
    else:
        await message.reply_text(text, reply_markup=reply_markup)


async def account_group_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, phone = query.data.split(':')
    client = clients.get(phone)

    if client:
        group = client.account_data['group']
        status = "⭐ Спеціальний акаунт" if client.is_special else "🛟 Звичайний акаунт"

        await query.edit_message_text(
            f"ℹ️ Інформація про акаунт:\n\n"
            f"📱 Телефон: {phone}\n"
            f"👤 Ім'я: {client.account_data['name']}\n"
            f"🏷️ Група: {group}\n"
            f"{status}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Назад", callback_data="view_account_group")]
            ])
        )


async def view_account_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = []
    for client in clients.values():
        account = client.account_data
        btn_text = f"{account['name']} ({account['phone']})"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"account_group:{account['phone']}")])

    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")])

    await query.edit_message_text(
        "👤 Оберіть акаунт для перегляду його групи:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def set_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Якщо це виклик з кнопки, використовуємо query
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        update.message = query.message
        update.effective_chat = query.message.chat

    return await group_selection_required(update, context, _set_groups)


async def _set_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Визначаємо тип виклику
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        chat = query.message.chat
        user = query.from_user
    else:
        chat = update.effective_chat
        user = update.effective_user

    username = user.username
    is_special = username and username.lower() in [u.lower() for u in SPECIAL_USERS]

    if not is_special:
        if update.callback_query:
            await query.edit_message_text("❌ Тільки спеціальні користувачі можуть вибирати групи!")
        else:
            await update.message.reply_text("❌ Тільки спеціальні користувачі можуть вибирати групи!")
        return

    chat_id = update.effective_chat.id
    chat_id_str = str(chat_id)
    user_id = update.effective_user.id

    # Отримуємо унікальні групи
    all_groups = set()
    special_groups = set()

    for client in clients.values():
        group = client.account_data['group']
        all_groups.add(group)
        if client.is_special:
            special_groups.add(group)

    # Для звичайних адміністраторів показуємо тільки неспеціальні групи
    if user_id not in SPECIAL_USERS:
        all_groups = all_groups - special_groups
        if not all_groups:
            await update.message.reply_text("ℹ️ Немає доступних груп для вашого рівня доступу.")
            return

    # Створюємо клавіатуру з кнопками груп
    keyboard = []
    current_row = []

    for group in sorted(all_groups):
        # Перевіряємо, чи група вже вибрана
        is_selected = False
        if chat_id_str in notification_chats and group in notification_chats[chat_id_str].get('groups', []):
            is_selected = True

        # Позначаємо спеціальні групи
        group_display = group
        if group in special_groups:
            group_display = f"⭐ {group}"

        btn_text = f"✅ {group_display}" if is_selected else group_display
        current_row.append(InlineKeyboardButton(btn_text, callback_data=f"toggle_group:{group}"))

        if len(current_row) == 2:
            keyboard.append(current_row)
            current_row = []

    if current_row:
        keyboard.append(current_row)

    # Кнопки для збереження та скидання
    keyboard.append([
        InlineKeyboardButton("💾 Зберегти вибір", callback_data="save_groups"),
        InlineKeyboardButton("🧹 Скинути всі", callback_data="reset_groups")
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    selected_count = 0
    if chat_id_str in notification_chats:
        selected_count = len(notification_chats[chat_id_str].get('groups', []))

    # Додаємо пояснення про спеціальні групи
    explanation = "\n\n⭐ - Спеціальні групи (доступні всім акаунтам)" if special_groups else ""

    await update.message.reply_text(
        f"🏷️ Виберіть групи для моніторингу (вибрано: {selected_count}):{explanation}\n\n"
        "ℹ️ Натисніть на групу, щоб додати або видалити її зі списку",
        reply_markup=reply_markup
    )


async def my_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await admin_required(update, context, _my_groups)


async def _my_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        chat_id = query.message.chat_id
        chat_id_str = str(chat_id)
    else:
        chat_id = update.effective_chat.id
        chat_id_str = str(chat_id)

    if chat_id_str not in notification_chats or not notification_chats[chat_id_str].get('groups'):
        # Використовуємо правильний спосіб відповіді для callback
        if query:
            await query.edit_message_text("ℹ️ Ви ще не вибрали жодної групи для моніторингу.")
        else:
            await update.message.reply_text("ℹ️ Ви ще не вибрали жодної групи для моніторингу.")
        return

    groups = notification_chats[chat_id_str]['groups']
    response = "🏷️ Ваші вибрані групи:\n\n" + "\n".join(f"• `{group}`" for group in groups)

    # Додаємо попередження для неспеціальних користувачів
    user_id = update.effective_user.id
    if user_id not in SPECIAL_USERS:
        response += "\n\nℹ️ Зверніть увагу: ви не можете змінювати цей список, оскільки не є спеціальним користувачем"

    # Використовуємо правильний спосіб відповіді
    if query:
        await query.edit_message_text(response, parse_mode='Markdown')
    else:
        await update.message.reply_text(response, parse_mode='Markdown')


#async def toggle_group_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
#    query = update.callback_query
#    await query.answer()
#
#    chat_id = query.message.chat_id
#    chat_id_str = str(chat_id)
#    action, group = query.data.split(':')
#
#    # Ініціалізуємо налаштування для чату, якщо потрібно
#    if chat_id_str not in notification_chats:
#        notification_chats[chat_id_str] = {
#            'user_id': query.from_user.id,
#            'groups': []
#        }
#
#    settings = notification_chats[chat_id_str]
#
#    # Додаємо або видаляємо групу
#    if group in settings['groups']:
#        settings['groups'].remove(group)
#    else:
#        settings['groups'].append(group)
#
#    await save_notification_chats()
#    await show_group_selection(query, context)
#

async def _toggle_group_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    chat_id_str = str(chat_id)
    action, group = query.data.split(':')

    # Ініціалізуємо налаштування для чату, якщо потрібно
    if chat_id_str not in notification_chats:
        notification_chats[chat_id_str] = {
            'user_id': query.from_user.id,
            'groups': []
        }

    settings = notification_chats[chat_id_str]

    # Додаємо або видаляємо групу
    if group in settings['groups']:
        settings['groups'].remove(group)
    else:
        settings['groups'].append(group)

    await save_notification_chats()

    # Оновлюємо повідомлення з новим станом кнопок
    await update_group_buttons(query)




async def _save_groups_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    chat_id_str = str(chat_id)
    selected_count = 0

    if chat_id_str in notification_chats:
        selected_count = len(notification_chats[chat_id_str].get('groups', []))

    await query.edit_message_text(
        f"✅ Вибрано груп: {selected_count}\n\n"
        "Тепер ви будете отримувати сповіщення лише для обраних груп."
    )





async def _reset_groups_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    chat_id_str = str(chat_id)

    if chat_id_str in notification_chats:
        notification_chats[chat_id_str]['groups'] = []
        await save_notification_chats()

    await query.edit_message_text("🧹 Всі групи скинуті! Ви не отримуватимете сповіщень.")


async def update_group_buttons(query):
    chat_id = query.message.chat_id
    chat_id_str = str(chat_id)
    user_id = query.from_user.id

    # Отримуємо групи
    all_groups = set()
    special_groups = set()
    for client in clients.values():
        group = client.account_data['group']
        all_groups.add(group)
        if client.is_special:
            special_groups.add(group)

    # Для звичайних адміністраторів показуємо тільки неспеціальні групи
    if user_id not in SPECIAL_USERS:
        all_groups = all_groups - special_groups


    # Створюємо нову клавіатуру з оновленими станами
    keyboard = []
    current_row = []

    for group in sorted(all_groups):
        # Перевіряємо, чи група вибрана
        is_selected = False
        if chat_id_str in notification_chats and group in notification_chats[chat_id_str].get('groups', []):
            is_selected = True

        # Позначаємо спеціальні групи
        group_display = group
        if group in special_groups:
            group_display = f"⭐ {group}"

        btn_text = f"✅ {group_display}" if is_selected else group_display
        current_row.append(InlineKeyboardButton(btn_text, callback_data=f"toggle_group:{group}"))

        if len(current_row) == 2:
            keyboard.append(current_row)
            current_row = []

    if current_row:
        keyboard.append(current_row)

    # Кнопки для збереження та скидання
    keyboard.append([
        InlineKeyboardButton("💾 Зберегти вибір", callback_data="save_groups"),
        InlineKeyboardButton("🧹 Скинути всі", callback_data="reset_groups")
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    selected_count = 0
    if chat_id_str in notification_chats:
        selected_count = len(notification_chats[chat_id_str].get('groups', []))

    await query.edit_message_text(
        f"🏷️ Виберіть групи для моніторингу (вибрано: {selected_count}):\n\n"
        "ℹ️ Натисніть на групу, щоб додати або видалити її зі списку",
        reply_markup=reply_markup
    )


async def check_unread(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await admin_required(update, context, _check_unread)


async def _check_unread(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        chat_id = query.message.chat_id
        chat_id_str = str(chat_id)
        message = query.message
    else:
        chat_id = update.effective_chat.id
        chat_id_str = str(chat_id)
        message = update.message

    # Отримуємо налаштування чату
    settings = notification_chats.get(chat_id_str, {})
    username = settings.get('username')
    is_special = username and username.lower() in [u.lower() for u in SPECIAL_USERS]
    user_groups = settings.get('groups', [])

    # Визначаємо групу адміністратора З АКАУНТІВ
    admin_group = None
    user_id = update.effective_user.id

    # Шукаємо акаунт, який відповідає адміністратору
    for client in clients.values():
        if client.me and client.me.id == user_id:
            admin_group = client.account_data.get('group', '')
            break

    # Якщо не знайшли, використовуємо ім'я користувача як запасний варіант
    if not admin_group:
        admin_group = username if username else str(user_id)

    # Створюємо клавіатуру
    keyboard = [
        [InlineKeyboardButton("🔄 Перевірити зараз", callback_data="check_now")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Визначаємо доступні групи
    if is_special:
        groups_text = "обраних групах" if user_groups else "всіх групах"
    else:
        groups_text = f"групі `{admin_group}`"

    message_text = f"Натисніть кнопку для перевірки непрочитаних повідомлень у {groups_text}:"

    # Використовуємо правильний спосіб відповіді
    if query:
        await query.edit_message_text(message_text, reply_markup=reply_markup)
    else:
        await message.reply_text(message_text, reply_markup=reply_markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    await query.answer()
    user_id = query.from_user.id
    username = query.from_user.username
    is_special = username and username.lower() in [u.lower() for u in SPECIAL_USERS]
    chat_id_str = str(query.message.chat_id)

    if query.data == "select_group":
        if is_special:
            await show_group_selection(query, context)
        else:
            await query.edit_message_text("❌ Ця дія доступна тільки для спеціальних користувачів!")

    elif query.data == "check_notifications":
        if is_admin(user_id) or is_special:
            await check_unread(update, context)
        else:
            await query.edit_message_text("❌ Ви не маєте прав для цієї дії!")

    elif query.data == "view_groups":
        if is_admin(user_id) or is_special:
            await show_accessible_groups(query, context)
        else:
            await query.edit_message_text("❌ Ви не маєте прав для цієї дії!")
    elif query.data == "check_now":
        await handle_unread_messages(query, context)
    elif query.data == "refresh":
        await start(update, context)

    elif query.data == "close":
        await query.delete_message()

        # ADD HANDLER FOR BACK BUTTONS
    elif query.data == "back_to_main":
        await start(update, context)
    elif query.data == "view_account_group":
        await view_account_group(update, context)


async def handle_unread_messages(query, context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot
    chat_id = query.message.chat_id
    chat_id_str = str(chat_id)
    await query.edit_message_text("🔍 Перевіряю непрочитані повідомлення...")

    # Отримуємо налаштування чату
    settings = notification_chats.get(chat_id_str, {})
    username = settings.get('username')
    is_special = username and username.lower() in [u.lower() for u in SPECIAL_USERS]
    user_groups = settings.get('groups', [])

    # Отримуємо user_id користувача
    user_id = query.from_user.id

    # Визначаємо групи для перевірки
    if is_special:
        # Для спеціальних користувачів - всі обрані групи
        groups_to_check = user_groups
    else:
        # Для звичайних адміністраторів - шукаємо групу з акаунта
        groups_to_check = []

        # Шукаємо акаунт, який відповідає адміністратору
        for client in clients.values():
            if client.me and client.me.id == user_id:
                group = client.account_data.get('group', '')
                if group:
                    groups_to_check = [group]
                    break

        # Якщо не знайшли, використовуємо ім'я користувача як запасний варіант
        if not groups_to_check:
            groups_to_check = [username] if username else [str(user_id)]

    messages = []
    accounts_in_group = 0

    for phone, client in list(clients.items()):
        try:
            # Отримуємо групу клієнта
            client_group = client.account_data.get('group', '')

            # Перевіряємо чи група клієнта входить у список для перевірки
            if groups_to_check and client_group not in groups_to_check:
                continue

            accounts_in_group += 1  # Лічильник акаунтів у групі

            if not client.is_running:
                await client.start()

            unread_dialogs = []

            async for dialog in client.client.iter_dialogs():
                # Ігноруємо ботів, групи і канали
                if not isinstance(dialog.entity, types.User) or dialog.entity.bot:
                    continue

                # Фільтруємо лише діалоги з непрочитаними
                if dialog.unread_count > 0:
                    # Отримуємо ім'я користувача
                    user = dialog.entity
                    username = user.username or f"{user.first_name or ''} {user.last_name or ''}".strip()
                    if not username:
                        username = f"user_{user.id}"

                    unread_dialogs.append({
                        'username': username,
                        'count': dialog.unread_count,
                        'user_id': user.id
                    })

            if not unread_dialogs:
                messages.append({
                    'account': client.account_data['name'],
                    'status': "✅ Немає непрочитаних повідомлень",
                    'group': client_group
                })
            else:
                messages.append({
                    'account': client.account_data['name'],
                    'dialogs': unread_dialogs,
                    'total': sum(d['count'] for d in unread_dialogs),
                    'group': client_group
                })

        except Exception as e:
            logger.error(f"Помилка перевірки акаунта {phone}: {e}")
            messages.append({
                'account': client.account_data['name'],
                'status': f"❌ Помилка: {str(e)[:100]}",
                'group': client.account_data.get('group', '')
            })

    # Формуємо відповідь частинами
    if accounts_in_group == 0:
        group_text = ", ".join(groups_to_check) if groups_to_check else "групах"

        # Отримуємо список усіх груп з акаунтів
        all_groups = set()
        for client in clients.values():
            group = client.account_data.get('group', '')
            if group:
                all_groups.add(group)

        await query.edit_message_text(
            f"ℹ️ Не знайдено акаунтів у групах: {group_text}\n\n"
            f"Доступні групи: {', '.join(all_groups) if all_groups else 'немає груп в акаунтах'}"
        )
        return

    response_parts = ["📬 **Непрочитані повідомлення:**\n\n"]

    for msg in messages:
        account_line = f"👤 **{msg['account']}**\n🏷️ Група: `{msg['group']}`\n"

        if 'dialogs' in msg:
            dialogs_text = f"🔢 Всього непрочитаних: {msg['total']}\n"
            for dialog in msg['dialogs']:
                dialogs_text += f"👤 `{dialog['username']}`: {dialog['count']} непрочитаних\n"

            # Додаємо до останньої частини або створюємо нову
            if len(response_parts[-1]) + len(account_line) + len(dialogs_text) > 3800:
                response_parts.append("")

            response_parts[-1] += account_line + dialogs_text + "\n"
        else:
            if len(response_parts[-1]) + len(account_line) + len(msg['status']) > 3800:
                response_parts.append("")

            response_parts[-1] += account_line + msg['status'] + "\n\n"

    # Відправляємо частини повідомлення
    for i, part in enumerate(response_parts):
        if i == 0:
            await query.edit_message_text(
                text=part,
                parse_mode='Markdown'
            )
        else:
            await bot.send_message(
                chat_id=query.message.chat_id,
                text=part,
                parse_mode='Markdown'
            )


async def manage_special(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Дозволяємо тільки спеціальним користувачам
    user_id = update.effective_user.id
    if user_id not in SPECIAL_USERS:
        await update.message.reply_text("❌ Ця команда доступна тільки для спеціальних користувачів!")
        return


async def _manage_special(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Створюємо список акаунтів з можливістю зміни статусу
    keyboard = []
    for phone, client in clients.items():
        account = client.account_data
        status = "✅" if client.is_special else "❌"
        btn_text = f"{status} {account['name']} ({account['phone']})"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"toggle_special:{phone}")])

    keyboard.append([InlineKeyboardButton("💾 Зберегти зміни", callback_data="save_special")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "⭐ Керування спеціальними акаунтами:\n\n"
        "Оберіть акаунт, щоб змінити його статус:",
        reply_markup=reply_markup
    )


async def toggle_special_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await admin_required(update, context, _toggle_special_handler)


async def _toggle_special_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, phone = query.data.split(':')
    client = clients.get(phone)

    if client:
        # Змінюємо статус
        client.is_special = not client.is_special
        # Оновлюємо дані акаунта
        client.account_data['is_special'] = client.is_special

        # Оновлюємо кнопки
        await update_special_buttons(query)


async def update_special_buttons(query):
    keyboard = []
    for phone, client in clients.items():
        account = client.account_data
        status = "✅" if client.is_special else "❌"
        btn_text = f"{status} {account['name']} ({account['phone']})"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"toggle_special:{phone}")])

    keyboard.append([InlineKeyboardButton("💾 Зберегти зміни", callback_data="save_special")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "⭐ Керування спеціальними акаунтами:\n\n"
        "Оберіть акаунт, щоб змінити його статус:",
        reply_markup=reply_markup
    )


async def save_special_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    # Зберігаємо зміни у файл
    try:
        # Завантажуємо поточні дані
        if os.path.exists(ACCOUNTS_FILE):
            with open(ACCOUNTS_FILE, 'rb') as f:
                data = pickle.load(f)
        else:
            data = {"accounts": [], "groups": []}

        # Оновлюємо статуси спеціальних акаунтів
        for i, account in enumerate(data['accounts']):
            for client in clients.values():
                if account['phone'] == client.account_data['phone']:
                    data['accounts'][i]['is_special'] = client.is_special

        # Зберігаємо зміни
        with open(ACCOUNTS_FILE, 'wb') as f:
            pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)

        await query.edit_message_text("✅ Зміни успішно збережено!")
    except Exception as e:
        logger.error(f"Помилка збереження спеціальних акаунтів: {e}")
        await query.edit_message_text(f"❌ Помилка збереження: {str(e)}")




async def check_accounts_updates():
    """Періодично перевіряє оновлення файлу акаунтів"""
    global last_accounts_check

    while True:
        try:
            # Перевіряємо кожні 30 секунд
            await asyncio.sleep(30)

            # Оновлюємо акаунти, якщо файл змінився
            if await load_accounts():
                logger.info("Оновлено акаунти з файлу")

                # Перезапускаємо слухачі повідомлень
                for client in clients.values():
                    if client.is_running:
                        asyncio.create_task(message_listener(client))
        except Exception as e:
            logger.error(f"Помилка перевірки оновлень акаунтів: {e}")


async def main():
    # Ініціалізація бота
    application = Application.builder().token(BOT_TOKEN).build()

    # Завантаження даних
    await load_notification_chats()
    await load_accounts()

    # Запуск обробників
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("set_groups", set_groups))
    application.add_handler(CommandHandler("my_groups", my_groups))
    application.add_handler(CommandHandler("check_unread", check_unread))
    application.add_handler(CommandHandler("manage_special", manage_special))
    # У функції main:
    application.add_handler(CommandHandler("set_groups",
                                           lambda update, context: group_selection_required(update, context,
                                                                                            set_groups)))

    application.add_handler(CallbackQueryHandler(
        lambda update, context: group_selection_required(update, context, toggle_group_handler),
        pattern="^toggle_group:"))

    application.add_handler(CallbackQueryHandler(
        lambda update, context: group_selection_required(update, context, save_groups_handler),
        pattern="^save_groups$"))

    application.add_handler(CallbackQueryHandler(
        lambda update, context: group_selection_required(update, context, reset_groups_handler),
        pattern="^reset_groups$"))
    application.add_handler(CallbackQueryHandler(button_handler, pattern="^check_now$"))
    application.add_handler(CallbackQueryHandler(toggle_group_handler, pattern="^toggle_group:"))
    application.add_handler(CallbackQueryHandler(save_groups_handler, pattern="^save_groups$"))
    application.add_handler(CallbackQueryHandler(reset_groups_handler, pattern="^reset_groups$"))
    application.add_handler(CallbackQueryHandler(toggle_special_handler, pattern="^toggle_special:"))
    application.add_handler(CallbackQueryHandler(save_special_handler, pattern="^save_special$"))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(CallbackQueryHandler(account_group_handler, pattern="^account_group:"))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(CallbackQueryHandler(
        lambda update, context: group_selection_required(update, context, group_button_handler),
        pattern="^(toggle_group|save_groups|reset_groups|back_to_main)"
    ))
    # Запуск слухачів повідомлень
    for client in clients.values():
        if client.is_running:
            asyncio.create_task(message_listener(client))

    # Запуск обробки черги повідомлень
    asyncio.create_task(process_message_queue(application.bot))

    # Запуск перевірки оновлень акаунтів
    asyncio.create_task(check_accounts_updates())

    # Запуск бота
    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    # Запуск основного циклу
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        # Коректне закриття
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        for client in clients.values():
            await client.stop()


if __name__ == "__main__":
    asyncio.run(main())

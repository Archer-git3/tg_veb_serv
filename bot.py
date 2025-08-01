import json
import asyncio
import os
from datetime import datetime
from telethon import TelegramClient, events, errors, types
from telethon.sessions import StringSession
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import time
import pickle
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import pytz


# Конфігурація
API_ID = 29148113
API_HASH = "0fba92868b9d99d1e63583a8fb751fb4"
BOT_TOKEN = "7603687034:AAG9102_4yFSuHrwE17FgO-Fc8nnfL1Z4-8"
ACCOUNTS_FILE = "telegram_accounts.json"
NOTIFICATION_CHATS_FILE = "notification_chats.json"
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
        except Exception:
            return False

    async def stop(self):
        if self.client and self.client.is_connected():
            await self.client.disconnect()
        self.is_running = False


async def load_accounts():
    global last_accounts_mtime, admins

    try:
        if not os.path.exists(ACCOUNTS_FILE):
            return False

        current_mtime = os.path.getmtime(ACCOUNTS_FILE)
        if current_mtime <= last_accounts_mtime:
            return False

        last_accounts_mtime = current_mtime

        with open(ACCOUNTS_FILE, 'rb') as f:
            data = pickle.load(f)

        accounts = data.get("accounts", [])
        if not accounts:
            return False

        # Зупиняємо старі клієнти
        for client in list(clients.values()):
            await client.stop()
        clients.clear()
        admins.clear()

        for account in accounts:
            if account.get('skip_check', False):
                continue

            client = AccountClient(account)
            if await client.start():
                clients[account['phone']] = client

                if account.get('is_admin', False) and client.me:
                    admins.add(client.me.id)

        return True
    except Exception:
        return False


def format_datetime(raw_dt: str) -> str:
    # Парсимо дату з ISO-формату
    dt_utc = datetime.fromisoformat(raw_dt)
    # Переводимо в Київський час
    kyiv_tz = pytz.timezone("Europe/Kyiv")
    dt_kyiv = dt_utc.astimezone(kyiv_tz)
    # Форматуємо дату в зручному вигляді
    return dt_kyiv.strftime("%d.%m.%Y %H:%M")

async def send_notification(bot: Bot, chat_id: int, message: dict):
    try:
        first_msg_info = "🌟 **Перше повідомлення!**\n" if message['is_first'] else ""
        special_indicator = "⭐ СПЕЦІАЛЬНИЙ АКАУНТ ⭐\n" if message.get('is_special', False) else ""
        formatted_date = format_datetime(message['date'])
        text = (
            f"🔔 **Нове повідомлення!**\n"
            f"{special_indicator}"
            f"{first_msg_info}"
            f"👤 Акаунт: `{message['account']}`\n"
            f"👤 Відправник: `{message['sender']}`\n"
            f"📅 Дата: `{formatted_date}`\n"
            f"🏷️ Група: `{message['group']}`\n"
            f"\n{message['text']}"
        )

        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode='Markdown'
        )


        if message['is_first']:
            scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
            creds = ServiceAccountCredentials.from_json_keyfile_name("serviceaccount121-b4e897371ed8.json", scope)
            client = gspread.authorize(creds)
            spreadsheet = client.open_by_key("1JI5CfxlZItxTCaIy41Ja_ZMRMwaLzC7WXApiFOhESBg")
            sheet = spreadsheet.sheet1

            sheet.append_row([
                message['account'],
                message['sender'],
                formatted_date,
                message['group'],
                message['text']
            ])

    except Exception:
        pass


def get_group_admins(group_name):
    admins_list = []
    for client in clients.values():
        if (client.account_data.get('group') == group_name and
                client.account_data.get('is_admin', False) and
                client.me):
            admins_list.append(client.me.id)
    return admins_list


async def load_notification_chats():
    global notification_chats
    try:
        if os.path.exists(NOTIFICATION_CHATS_FILE):
            with open(NOTIFICATION_CHATS_FILE, 'rb') as f:
                notification_chats = pickle.load(f)
                for chat_id, settings in notification_chats.items():
                    username = settings.get('username')
                    if username:
                        settings['is_special'] = username.lower() in [u.lower() for u in SPECIAL_USERS]
    except Exception:
        notification_chats = {}


async def save_notification_chats():
    try:
        with open(NOTIFICATION_CHATS_FILE, 'wb') as f:
            pickle.dump(notification_chats, f)
    except Exception:
        pass


async def message_listener(client: AccountClient):
    @client.client.on(events.NewMessage(incoming=True))
    async def handler(event):
        try:
            if event.message.sender_id == client.me.id:
                return

            if client.account_data.get('skip_check', False):
                return

            sender = await event.get_sender()
            if isinstance(sender, types.User) and sender.bot:
                return

            if not isinstance(event.message.peer_id, types.PeerUser):
                return

            sender_name = "Невідомий"
            if sender:
                sender_name = sender.username or f"{sender.first_name or ''} {sender.last_name or ''}".strip()
                if not sender_name:
                    sender_name = f"user_{sender.id}"

            is_first_message = await is_first_in_dialog(client, event.message.peer_id.user_id)

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

        except Exception:
            pass


async def is_first_in_dialog(wrapper_client, user_id):
    try:
        # Дістаємо внутрішній TelegramClient
        client = wrapper_client.client

        me = await client.get_me()
        my_id = me.id

        messages = await client.get_messages(user_id, limit=10)

        # Фільтруємо повідомлення не від нас
        other_user_messages = [msg for msg in messages if msg.sender_id != my_id]

        return len(other_user_messages) <= 1

    except Exception as e:
        print("Помилка при перевірці першого повідомлення:", e)
        return False



async def process_message_queue(bot: Bot):
    while True:
        message = await message_queue.get()
        sent_to_admins = set()

        for chat_id_str, settings in notification_chats.items():
            try:
                chat_id = int(chat_id_str)
                user_id = settings['user_id']
                if not (is_admin(user_id) or settings.get('is_special', False)):
                    continue

                if message.get('is_special', False):
                    if settings.get('is_special', False):
                        await send_notification(bot, chat_id, message)
                        sent_to_admins.add(chat_id)
                else:
                    if 'groups' in settings and message['group'] in settings['groups']:
                        await send_notification(bot, chat_id, message)
                        sent_to_admins.add(chat_id)
            except Exception:
                pass

        if not message.get('is_special', False):
            group_admins = get_group_admins(message['group'])
            for admin_id in group_admins:
                if admin_id not in sent_to_admins:
                    try:
                        for cid, sett in notification_chats.items():
                            if sett['user_id'] == admin_id:
                                await send_notification(bot, int(cid), message)
                                break
                    except Exception:
                        pass

        message_queue.task_done()


def is_admin(user_id):
    return user_id in admins


def has_admin_rights(user_id):
    return user_id in admins or user_id in SPECIAL_USERS


async def group_selection_required(update: Update, context: ContextTypes.DEFAULT_TYPE, handler):
    return await handler(update, context)


async def admin_required(update: Update, context: ContextTypes.DEFAULT_TYPE, handler):
    user_id = update.effective_user.id

    if not is_admin(user_id) and user_id not in SPECIAL_USERS:
        if update.callback_query:
            await update.callback_query.answer("❌ Ви не маєте прав адміністратора!", show_alert=True)
        elif update.message:
            await update.message.reply_text("❌ Ви не маєте прав адміністратора!")
        return None

    return await handler(update, context)


async def show_accessible_groups(query, context: ContextTypes.DEFAULT_TYPE):
    user_id = query.from_user.id
    username = query.from_user.username
    is_special = username and username.lower() in [u.lower() for u in SPECIAL_USERS]
    chat_id_str = str(query.message.chat_id)

    settings = notification_chats.get(chat_id_str, {})
    user_groups = settings.get('groups', [])

    user_group = None
    if not is_special:
        for client in clients.values():
            if client.me and client.me.id == user_id:
                user_group = client.account_data.get('group', '')
                break
        if not user_group:
            user_group = username if username else f"ID: {user_id}"

    if is_special:
        groups_text = "🏷️ Ваші доступні групи:\n\n" + "\n".join(f"• `{group}`" for group in user_groups)
        if not user_groups:
            groups_text = "ℹ️ Ви ще не обрали жодної групи. Використайте '➕ Обрати групу'."
    else:
        groups_text = f"🏷️ Ваша група: `{user_group}`"

    await query.edit_message_text(
        groups_text,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]
        ])
    )


async def show_group_selection(query, context: ContextTypes.DEFAULT_TYPE):
    all_groups = set()
    for client in clients.values():
        all_groups.add(client.account_data['group'])

    if not all_groups:
        await query.edit_message_text("ℹ️ Немає доступних груп для вибору.")
        return

    keyboard = []
    current_row = []

    for group in sorted(all_groups):
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

    if chat_id_str not in notification_chats:
        notification_chats[chat_id_str] = {
            'user_id': query.from_user.id,
            'groups': []
        }

    settings = notification_chats[chat_id_str]

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


def get_admin_group(user_id):
    for client in clients.values():
        if client.me and client.me.id == user_id:
            return client.account_data.get('group', '')
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    if not (is_admin(user_id) or is_special):
        text = "❌ Ви не маєте доступу до цього бота. Зверніться до адміністратора."
        if update.callback_query:
            await query.edit_message_text(text)
        else:
            await message.reply_text(text)
        return

    if chat_id_str not in notification_chats:
        notification_chats[chat_id_str] = {
            'user_id': user_id,
            'username': username,
            'groups': [],
            'is_special': is_special
        }

        if not is_special and is_admin(user_id):
            admin_group = get_admin_group(user_id)
            if admin_group:
                notification_chats[chat_id_str]['groups'] = [admin_group]
                await save_notification_chats()
    else:
        notification_chats[chat_id_str]['user_id'] = user_id
        notification_chats[chat_id_str]['username'] = username
        notification_chats[chat_id_str]['is_special'] = is_special

    settings = notification_chats[chat_id_str]
    if not settings['is_special'] and is_admin(user_id) and not settings.get('groups'):
        admin_group = get_admin_group(user_id)
        if admin_group:
            settings['groups'] = [admin_group]
            await save_notification_chats()

    if is_special:
        admin_status = "⭐ Ви спеціальний користувач (повний доступ)"
    elif is_admin(user_id):
        admin_status = "✅ Ви адміністратор"
    else:
        admin_status = "❌ Ви не маєте прав адміністратора"

    keyboard = []

    if is_special:
        keyboard.append([
            InlineKeyboardButton("➕ Обрати групу", callback_data="select_group"),
        ])
        keyboard.append([
            InlineKeyboardButton("🔔 Перевірити сповіщення", callback_data="check_notifications"),
            InlineKeyboardButton("👁️ Доступні групи", callback_data="view_groups")
        ])

    elif is_admin(user_id):
        keyboard.append([
            InlineKeyboardButton("🔔 Перевірити сповіщення", callback_data="check_notifications"),
            InlineKeyboardButton("👁️ Моя група", callback_data="view_groups")
        ])

    keyboard.append([
        InlineKeyboardButton("🔄 Оновити", callback_data="refresh"),
        InlineKeyboardButton("❌ Закрити", callback_data="close")
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    text = f"🔔 Бот активований! {admin_status}\n\nОберіть дію з меню:"

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
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        update.message = query.message
        update.effective_chat = query.message.chat

    return await group_selection_required(update, context, _set_groups)


async def _set_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    all_groups = set()
    special_groups = set()

    for client in clients.values():
        group = client.account_data['group']
        all_groups.add(group)
        if client.is_special:
            special_groups.add(group)

    if user_id not in SPECIAL_USERS:
        all_groups = all_groups - special_groups
        if not all_groups:
            await update.message.reply_text("ℹ️ Немає доступних груп для вашого рівня доступу.")
            return

    keyboard = []
    current_row = []

    for group in sorted(all_groups):
        is_selected = False
        if chat_id_str in notification_chats and group in notification_chats[chat_id_str].get('groups', []):
            is_selected = True

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

    keyboard.append([
        InlineKeyboardButton("💾 Зберегти вибір", callback_data="save_groups"),
        InlineKeyboardButton("🧹 Скинути всі", callback_data="reset_groups")
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    selected_count = 0
    if chat_id_str in notification_chats:
        selected_count = len(notification_chats[chat_id_str].get('groups', []))

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
        if query:
            await query.edit_message_text("ℹ️ Ви ще не вибрали жодної групи для моніторингу.")
        else:
            await update.message.reply_text("ℹ️ Ви ще не вибрали жодної групи для моніторингу.")
        return

    groups = notification_chats[chat_id_str]['groups']
    response = "🏷️ Ваші вибрані групи:\n\n" + "\n".join(f"• `{group}`" for group in groups)

    user_id = update.effective_user.id
    if user_id not in SPECIAL_USERS:
        response += "\n\nℹ️ Зверніть увагу: ви не можете змінювати цей список, оскільки не є спеціальним користувачем"

    if query:
        await query.edit_message_text(response, parse_mode='Markdown')
    else:
        await update.message.reply_text(response, parse_mode='Markdown')


async def _toggle_group_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    chat_id_str = str(chat_id)
    action, group = query.data.split(':')

    if chat_id_str not in notification_chats:
        notification_chats[chat_id_str] = {
            'user_id': query.from_user.id,
            'groups': []
        }

    settings = notification_chats[chat_id_str]

    if group in settings['groups']:
        settings['groups'].remove(group)
    else:
        settings['groups'].append(group)

    await save_notification_chats()
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

    all_groups = set()
    special_groups = set()
    for client in clients.values():
        group = client.account_data['group']
        all_groups.add(group)
        if client.is_special:
            special_groups.add(group)

    if user_id not in SPECIAL_USERS:
        all_groups = all_groups - special_groups

    keyboard = []
    current_row = []

    for group in sorted(all_groups):
        is_selected = False
        if chat_id_str in notification_chats and group in notification_chats[chat_id_str].get('groups', []):
            is_selected = True

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

    settings = notification_chats.get(chat_id_str, {})
    username = settings.get('username')
    is_special = username and username.lower() in [u.lower() for u in SPECIAL_USERS]
    user_groups = settings.get('groups', [])

    admin_group = None
    user_id = update.effective_user.id

    for client in clients.values():
        if client.me and client.me.id == user_id:
            admin_group = client.account_data.get('group', '')
            break

    if not admin_group:
        admin_group = username if username else str(user_id)

    keyboard = [
        [InlineKeyboardButton("🔄 Перевірити зараз", callback_data="check_now")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if is_special:
        groups_text = "обраних групах" if user_groups else "всіх групах"
    else:
        groups_text = f"групі `{admin_group}`"

    message_text = f"Натисніть кнопку для перевірки непрочитаних повідомлень у {groups_text}:"

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

    elif query.data == "back_to_main":
        await start(update, context)
    elif query.data == "view_account_group":
        await view_account_group(update, context)


async def handle_unread_messages(query, context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot
    chat_id = query.message.chat_id
    chat_id_str = str(chat_id)
    await query.edit_message_text("🔍 Перевіряю непрочитані повідомлення...")

    settings = notification_chats.get(chat_id_str, {})
    username = settings.get('username')
    is_special = username and username.lower() in [u.lower() for u in SPECIAL_USERS]
    user_groups = settings.get('groups', [])

    user_id = query.from_user.id

    if is_special:
        groups_to_check = user_groups
    else:
        groups_to_check = []
        for client in clients.values():
            if client.me and client.me.id == user_id:
                group = client.account_data.get('group', '')
                if group:
                    groups_to_check = [group]
                    break
        if not groups_to_check:
            groups_to_check = [username] if username else [str(user_id)]

    messages = []
    accounts_in_group = 0

    for phone, client in list(clients.items()):
        try:
            client_group = client.account_data.get('group', '')
            if groups_to_check and client_group not in groups_to_check:
                continue

            accounts_in_group += 1
            if not client.is_running:
                await client.start()

            unread_dialogs = []
            async for dialog in client.client.iter_dialogs():
                if not isinstance(dialog.entity, types.User) or dialog.entity.bot:
                    continue

                if dialog.unread_count > 0:
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

        except Exception:
            messages.append({
                'account': client.account_data['name'],
                'status': "❌ Помилка перевірки",
                'group': client.account_data.get('group', '')
            })

    if accounts_in_group == 0:
        group_text = ", ".join(groups_to_check) if groups_to_check else "групах"
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

            if len(response_parts[-1]) + len(account_line) + len(dialogs_text) > 3800:
                response_parts.append("")

            response_parts[-1] += account_line + dialogs_text + "\n"
        else:
            if len(response_parts[-1]) + len(account_line) + len(msg['status']) > 3800:
                response_parts.append("")

            response_parts[-1] += account_line + msg['status'] + "\n\n"

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
    user_id = update.effective_user.id
    if user_id not in SPECIAL_USERS:
        await update.message.reply_text("❌ Ця команда доступна тільки для спеціальних користувачів!")
        return


async def _manage_special(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        client.is_special = not client.is_special
        client.account_data['is_special'] = client.is_special
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

    try:
        if os.path.exists(ACCOUNTS_FILE):
            with open(ACCOUNTS_FILE, 'rb') as f:
                data = pickle.load(f)
        else:
            data = {"accounts": [], "groups": []}

        for i, account in enumerate(data['accounts']):
            for client in clients.values():
                if account['phone'] == client.account_data['phone']:
                    data['accounts'][i]['is_special'] = client.is_special

        with open(ACCOUNTS_FILE, 'wb') as f:
            pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)

        await query.edit_message_text("✅ Зміни успішно збережено!")
    except Exception:
        await query.edit_message_text("❌ Помилка збереження")


async def check_accounts_updates():
    global last_accounts_check

    while True:
        try:
            await asyncio.sleep(30)
            if await load_accounts():
                for client in clients.values():
                    if client.is_running:
                        asyncio.create_task(message_listener(client))
        except Exception:
            pass


async def main():
    await load_notification_chats()
    application = Application.builder().token(BOT_TOKEN).build()
    await load_accounts()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("set_groups", set_groups))
    application.add_handler(CommandHandler("my_groups", my_groups))
    application.add_handler(CommandHandler("check_unread", check_unread))
    application.add_handler(CommandHandler("manage_special", manage_special))

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

    for client in clients.values():
        if client.is_running:
            asyncio.create_task(message_listener(client))

    asyncio.create_task(process_message_queue(application.bot))
    asyncio.create_task(check_accounts_updates())

    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        for client in clients.values():
            await client.stop()


if __name__ == "__main__":
    asyncio.run(main())


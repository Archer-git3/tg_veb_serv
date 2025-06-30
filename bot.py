import os
import json
import asyncio
import logging
from datetime import datetime
from telethon import TelegramClient, events, errors, types
from telethon.sessions import StringSession
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import time

# Конфігурація
API_ID = 29148113
API_HASH = "0fba92868b9d99d1e63583a8fb751fb4"
BOT_TOKEN = "7603687034:AAG9102_4yFSuHrwE17FgO-Fc8nnfL1Z4-8"
ACCOUNTS_FILE = "telegram_accounts.json"
NOTIFICATION_CHATS_FILE = "notification_chats.json"
ADMINS_FILE = "admins.json"
LOG_FILE = "bot.log"
SESSION_TIMEOUT = 60
ACCOUNTS_CHECK_INTERVAL = 30

# Налаштування логування
if os.path.exists(LOG_FILE):
    os.remove(LOG_FILE)

logging.basicConfig(
    filename=LOG_FILE,
    encoding='utf-8',
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)
logger.info("=== Бот запущено ===")

# Глобальні змінні
clients = {}
notification_chats = {}
admins = set()
message_queue = asyncio.Queue()
user_groups = {}
last_accounts_check = 0
last_accounts_mtime = 0


class AccountClient:
    def __init__(self, account_data):
        self.account_data = account_data
        self.client = None
        self.is_running = False
        self.me = None

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
                logger.error(f"Клієнт {self.account_data['name']} не авторизований!")
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
        # Отримуємо час модифікації файлу
        current_mtime = os.path.getmtime(ACCOUNTS_FILE)

        # Перевіряємо, чи файл змінився
        if current_mtime <= last_accounts_mtime:
            return False

        last_accounts_mtime = current_mtime

        with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
            accounts = json.load(f)

        # Зупиняємо старі клієнти
        for client in list(clients.values()):
            await client.stop()
        clients.clear()

        # Очищаємо адмінів та завантажуємо нових
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

        # Зберігаємо адмінів у файл
        await save_admins()

        return True
    except Exception as e:
        logger.error(f"Помилка завантаження акаунтів: {e}")
        return False


async def load_admins():
    global admins
    try:
        if os.path.exists(ADMINS_FILE):
            with open(ADMINS_FILE, 'r', encoding='utf-8') as f:
                admins = set(json.load(f))
                logger.info(f"Завантажено адміністраторів: {admins}")
    except Exception as e:
        logger.error(f"Помилка завантаження адміністраторів: {e}")
        admins = set()


async def save_admins():
    try:
        with open(ADMINS_FILE, 'w', encoding='utf-8') as f:
            json.dump(list(admins), f)
    except Exception as e:
        logger.error(f"Помилка збереження адміністраторів: {e}")


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


async def load_notification_chats():
    global notification_chats
    try:
        if os.path.exists(NOTIFICATION_CHATS_FILE):
            with open(NOTIFICATION_CHATS_FILE, 'r', encoding='utf-8') as f:
                # Конвертуємо старий формат у новий
                old_data = json.load(f)
                notification_chats = {}

                for key, value in old_data.items():
                    # Якщо це старий формат (chat_id: int)
                    if isinstance(value, int):
                        notification_chats[key] = {
                            'user_id': int(key),
                            'groups': []
                        }
                    else:
                        notification_chats[key] = value
    except Exception as e:
        logger.error(f"Помилка завантаження чатів: {e}")
        notification_chats = {}


async def save_notification_chats():
    try:
        with open(NOTIFICATION_CHATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(notification_chats, f, indent=2)
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
                'sender_id': sender.id if sender else 0
            }
            await message_queue.put(message_info)
        except Exception as e:
            logger.error(f"Помилка обробки повідомлення: {e}")


async def process_message_queue(bot: Bot):
    while True:
        message = await message_queue.get()
        for chat_id_str, settings in notification_chats.items():
            try:
                # Перевіряємо, чи група повідомлення входить у вибрані групи
                if 'groups' not in settings or message['group'] not in settings['groups']:
                    continue

                text = (
                    f"🔔 **Нове повідомлення!**\n"
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


async def admin_required(update: Update, context: ContextTypes.DEFAULT_TYPE, handler):
    """Декоратор для перевірки прав адміністратора"""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        if update.callback_query:
            await update.callback_query.answer("❌ Ви не маєте прав адміністратора!", show_alert=True)
        elif update.message:
            await update.message.reply_text("❌ Ви не маєте прав адміністратора!")
        return None
    return await handler(update, context)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    chat_id_str = str(chat_id)

    # Ініціалізуємо налаштування для чату
    if chat_id_str not in notification_chats:
        notification_chats[chat_id_str] = {
            'user_id': user_id,
            'groups': []
        }
    else:
        # Оновлюємо user_id для існуючого чату
        notification_chats[chat_id_str]['user_id'] = user_id

    await save_notification_chats()

    # Визначаємо, чи є користувач адміністратором
    admin_status = "✅ Ви адміністратор" if is_admin(user_id) else "❌ Ви не адміністратор"

    await update.message.reply_text(
        f"🔔 Бот активований! {admin_status}\n\n"
        "Доступні команди:\n"
        "/start - Активація сповіщень\n"
        "/set_groups - Вибрати групи для моніторингу\n"
        "/check_unread - Перевірка непрочитаних\n"
        "/my_groups - Показати вибрані групи"
    )


async def set_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await admin_required(update, context, _set_groups)


async def _set_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_id_str = str(chat_id)

    # Отримуємо унікальні групи з усіх акаунтів
    all_groups = set()
    for client in clients.values():
        all_groups.add(client.account_data['group'])

    if not all_groups:
        await update.message.reply_text("ℹ️ Немає доступних груп для вибору.")
        return

    # Створюємо клавіатуру з кнопками груп
    keyboard = []
    current_row = []

    for group in sorted(all_groups):
        # Перевіряємо, чи група вже вибрана
        is_selected = False
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
        InlineKeyboardButton("🧹 Скинути всі", callback_data="reset_groups")
    ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    selected_count = 0
    if chat_id_str in notification_chats:
        selected_count = len(notification_chats[chat_id_str].get('groups', []))

    await update.message.reply_text(
        f"🏷️ Виберіть групи для моніторингу (вибрано: {selected_count}):\n\n"
        "ℹ️ Натисніть на групу, щоб додати або видалити її зі списку",
        reply_markup=reply_markup
    )


async def my_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await admin_required(update, context, _my_groups)


async def _my_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_id_str = str(chat_id)

    if chat_id_str not in notification_chats or not notification_chats[chat_id_str].get('groups'):
        await update.message.reply_text("ℹ️ Ви ще не вибрали жодної групи для моніторингу.")
        return

    groups = notification_chats[chat_id_str]['groups']
    response = "🏷️ Ваші вибрані групи:\n\n" + "\n".join(f"• `{group}`" for group in groups)

    await update.message.reply_text(response, parse_mode='Markdown')


async def toggle_group_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await admin_required(update, context, _toggle_group_handler)


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


async def save_groups_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await admin_required(update, context, _save_groups_handler)


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


async def reset_groups_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await admin_required(update, context, _reset_groups_handler)


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

    # Отримуємо унікальні групи з усіх акаунтів
    all_groups = set()
    for client in clients.values():
        all_groups.add(client.account_data['group'])

    # Створюємо нову клавіатуру з оновленими станами
    keyboard = []
    current_row = []

    for group in sorted(all_groups):
        # Перевіряємо, чи група вибрана
        is_selected = False
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
    chat_id = update.effective_chat.id
    chat_id_str = str(chat_id)

    # Перевіряємо, чи користувач вибрав групи
    if chat_id_str not in notification_chats or not notification_chats[chat_id_str].get('groups'):
        await update.message.reply_text(
            "ℹ️ Ви ще не вибрали групи для моніторингу.\n"
            "Використайте /set_groups щоб вибрати групи."
        )
        return

    keyboard = [
        [InlineKeyboardButton("🔄 Перевірити зараз", callback_data="check_now")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "Натисніть кнопку для перевірки непрочитаних повідомлень:",
        reply_markup=reply_markup
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await admin_required(update, context, _button_handler)


async def _button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "check_now":
        await handle_unread_messages(query, context.bot)


async def handle_unread_messages(query, bot):
    chat_id = query.message.chat_id
    chat_id_str = str(chat_id)
    await query.edit_message_text("🔍 Перевіряю непрочитані повідомлення...")

    # Отримуємо вибрані групи
    selected_groups = []
    if chat_id_str in notification_chats:
        selected_groups = notification_chats[chat_id_str].get('groups', [])

    messages = []
    for phone, client in list(clients.items()):
        try:
            # Пропускаємо акаунти, які не належать до вибраних груп
            if selected_groups and client.account_data['group'] not in selected_groups:
                continue

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
                    'group': client.account_data['group']
                })
            else:
                messages.append({
                    'account': client.account_data['name'],
                    'dialogs': unread_dialogs,
                    'total': sum(d['count'] for d in unread_dialogs),
                    'group': client.account_data['group']
                })

        except Exception as e:
            logger.error(f"Помилка перевірки акаунта {phone}: {e}")
            messages.append({
                'account': client.account_data['name'],
                'status': f"❌ Помилка: {str(e)[:100]}",
                'group': client.account_data['group']
            })

    # Формуємо відповідь частинами
    if not messages:
        await query.edit_message_text("ℹ️ Немає акаунтів для перевірки або всі акаунти мають помилки")
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


async def main():
    # Ініціалізація бота
    application = Application.builder().token(BOT_TOKEN).build()

    # Завантаження даних
    await load_admins()
    await load_notification_chats()
    await load_accounts()

    # Запуск обробників
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("set_groups", set_groups))
    application.add_handler(CommandHandler("my_groups", my_groups))
    application.add_handler(CommandHandler("check_unread", check_unread))

    application.add_handler(CallbackQueryHandler(button_handler, pattern="^check_now$"))
    application.add_handler(CallbackQueryHandler(toggle_group_handler, pattern="^toggle_group:"))
    application.add_handler(CallbackQueryHandler(save_groups_handler, pattern="^save_groups$"))
    application.add_handler(CallbackQueryHandler(reset_groups_handler, pattern="^reset_groups$"))

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
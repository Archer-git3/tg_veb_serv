import asyncio
from telethon import TelegramClient, types
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from datetime import datetime, timezone
import streamlit as st
import pandas as pd
import time
import json
import os

# Конфігурація
API_ID = 29148113
API_HASH = "0fba92868b9d99d1e63583a8fb751fb4"
ACCOUNTS_FILE = "telegram_accounts.json"

# Глобальний цикл подій
if not hasattr(st.session_state, 'loop'):
    st.session_state.loop = asyncio.new_event_loop()
asyncio.set_event_loop(st.session_state.loop)


def save_accounts_to_file():
    """Зберігає акаунти у JSON файл"""
    try:
        # Зберігаємо тільки необхідні дані
        accounts_to_save = []
        for account in st.session_state.accounts:
            accounts_to_save.append({
                'group': account['group'],
                'name': account['name'],
                'phone': account['phone'],
                'session_string': account['session_string'],
                'last_updated': account['last_updated'].isoformat() if account['last_updated'] else None,
                'is_admin': account.get('is_admin', False),
                'skip_check': account.get('skip_check', False)
            })

        with open(ACCOUNTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(accounts_to_save, f, ensure_ascii=False, indent=2)

    except Exception as e:
        st.error(f"Помилка збереження акаунтів: {str(e)}")


def load_accounts_from_file():
    """Завантажує акаунти з JSON файлу"""
    try:
        if not os.path.exists(ACCOUNTS_FILE):
            return []

        with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
            saved_accounts = json.load(f)

        # Конвертуємо дату останнього оновлення
        accounts = []
        for account in saved_accounts:
            accounts.append({
                'group': account['group'],
                'name': account['name'],
                'phone': account['phone'],
                'session_string': account['session_string'],
                'unread_count': 0,
                'oldest_unread': None,
                'status': '?',
                'last_updated': datetime.fromisoformat(account['last_updated']) if account['last_updated'] else None,
                'is_admin': account.get('is_admin', False),
                'skip_check': account.get('skip_check', False)
            })

        return accounts

    except Exception as e:
        st.error(f"Помилка завантаження акаунтів: {str(e)}")
        return []


# Ініціалізація стану сесії
def init_session_state():
    required_states = {
        'current_account': None,
        'login_stage': 'start',
        'phone_code_hash': None,
        'phone': None,
        'group_name': '',
        'stats_updated': 0,
        'editing_account_index': None
    }

    for key, default in required_states.items():
        if key not in st.session_state:
            st.session_state[key] = default

    # Завантажуємо акаунти з файлу при першому запуску
    if 'accounts' not in st.session_state:
        st.session_state.accounts = load_accounts_from_file()


async def create_client(session_string=None):
    """Створення нового клієнта Telegram"""
    client = TelegramClient(
        StringSession(session_string) if session_string else StringSession(),
        API_ID,
        API_HASH,
        loop=st.session_state.loop
    )
    client.flood_sleep_threshold = 0
    await client.connect()
    return client


async def login():
    """Функція для авторизації користувача"""
    st.subheader("Додати новий акаунт")

    # Введення назви групи
    group_name = st.text_input("Назва групи (наприклад, 'Марина' або 'Олександр'):")

    # Перевірка збереженої сесії
    if st.session_state.login_stage == 'start':
        phone = st.text_input("Введіть номер телефону (у міжнародному форматі):")

        if st.button("Надіслати код"):
            try:
                client = await create_client()
                sent_code = await client.send_code_request(phone)
                st.session_state.phone_code_hash = sent_code.phone_code_hash
                st.session_state.phone = phone
                st.session_state.group_name = group_name
                st.session_state.client = client
                st.session_state.login_stage = 'phone_sent'
                st.rerun()
            except FloodWaitError as fwe:
                st.error(f"Занадто багато спроб. Спробуйте через {fwe.seconds} секунд.")
            except Exception as e:
                st.error(f"Помилка: {str(e)}")

    elif st.session_state.login_stage == 'phone_sent':
        code = st.text_input("Введіть отриманий код:")

        if st.button("Увійти"):
            try:
                await st.session_state.client.sign_in(
                    st.session_state.phone,
                    code,
                    phone_code_hash=st.session_state.phone_code_hash
                )
            except SessionPasswordNeededError:
                st.session_state.login_stage = '2fa'
                st.rerun()
                return
            except Exception as e:
                st.error(f"Помилка авторизації: {str(e)}")
                await st.session_state.client.disconnect()
                st.session_state.client = None
                st.session_state.login_stage = 'start'
                return

            # Збереження сесії
            session_string = st.session_state.client.session.save()
            me = await st.session_state.client.get_me()

            # Додавання нового акаунта
            new_account = {
                'group': st.session_state.group_name,
                'name': f"{me.first_name or ''} {me.last_name or ''}".strip() or me.username or me.phone,
                'phone': me.phone,
                'session_string': session_string,
                'unread_count': 0,
                'oldest_unread': None,
                'status': '✓',
                'last_updated': datetime.now(),
                'is_admin': False,
                'skip_check': False
            }

            st.session_state.accounts.append(new_account)
            save_accounts_to_file()  # Зберігаємо зміни у файл

            st.session_state.login_stage = 'start'
            await st.session_state.client.disconnect()
            st.session_state.client = None
            st.success(f"Акаунт {new_account['name']} успішно додано до групи '{st.session_state.group_name}'!")
            st.session_state.stats_updated += 1
            st.rerun()

    elif st.session_state.login_stage == '2fa':
        password = st.text_input("Введіть пароль двофакторної аутентифікації:", type="password")

        if st.button("Підтвердити"):
            try:
                await st.session_state.client.sign_in(password=password)
                session_string = st.session_state.client.session.save()
                me = await st.session_state.client.get_me()

                # Додавання нового акаунта
                new_account = {
                    'group': st.session_state.group_name,
                    'name': f"{me.first_name or ''} {me.last_name or ''}".strip() or me.username or me.phone,
                    'phone': me.phone,
                    'session_string': session_string,
                    'unread_count': 0,
                    'oldest_unread': None,
                    'status': '✓',
                    'last_updated': datetime.now(),
                    'is_admin': False,
                    'skip_check': False
                }

                st.session_state.accounts.append(new_account)
                save_accounts_to_file()  # Зберігаємо зміни у файл

                st.session_state.login_stage = 'start'
                await st.session_state.client.disconnect()
                st.session_state.client = None
                st.success(f"Акаунт {new_account['name']} успішно додано до групи '{st.session_state.group_name}'!")
                st.session_state.stats_updated += 1
                st.rerun()
            except Exception as e:
                st.error(f"Помилка: {str(e)}")
                await st.session_state.client.disconnect()
                st.session_state.client = None
                st.session_state.login_stage = 'start'


async def get_unread_stats_for_account(account):
    """Отримання статистики для конкретного акаунта"""
    if account.get('skip_check', False):
        account['status'] = '⏭️ Пропущено'
        return

    client = None
    try:
        client = await create_client(account['session_string'])

        if not await client.is_user_authorized():
            account['status'] = "Не авторизовано"
            await client.disconnect()
            return

        me = await client.get_me()
        unread_chats_count = 0
        oldest_unread_date = None

        # Отримуємо всі діалоги
        dialogs = []
        async for dialog in client.iter_dialogs():
            # Фільтруємо тільки приватні чати з користувачами
            if not isinstance(dialog.entity, types.User):
                continue

            if getattr(dialog.entity, 'bot', False) or dialog.entity.id == me.id:
                continue

            dialogs.append(dialog)

        # Рахуємо тільки діалоги з непрочитаними повідомленнями
        for dialog in dialogs:
            if dialog.unread_count > 0:
                unread_chats_count += 1

                # Використовуємо дату останнього повідомлення як приблизний показник
                last_message_date = dialog.message.date
                if oldest_unread_date is None or last_message_date < oldest_unread_date:
                    oldest_unread_date = last_message_date

        account['unread_count'] = unread_chats_count
        account['oldest_unread'] = oldest_unread_date
        account['status'] = '✓'
        account['last_updated'] = datetime.now()

    except FloodWaitError as fwe:
        wait_time = fwe.seconds
        st.warning(f"Необхідно зачекати {wait_time} секунд для акаунта {account['name']}")
        await asyncio.sleep(wait_time + 1)
        # Повторюємо спробу після очікування
        await get_unread_stats_for_account(account)

    except Exception as e:
        account['status'] = f"Помилка: {str(e)}"
    finally:
        if client:
            try:
                await client.disconnect()
            except:
                pass


def format_time_diff(oldest_unread_date):
    """Форматує різницю часу у відносний формат"""
    if oldest_unread_date is None:
        return "-"

    now = datetime.now(timezone.utc)
    time_diff = now - oldest_unread_date

    days = time_diff.days
    hours = time_diff.seconds // 3600
    minutes = (time_diff.seconds % 3600) // 60

    if days > 0:
        return f"{days} дн. тому"
    elif hours > 0:
        return f"{hours} год. тому"
    elif minutes > 0:
        return f"{minutes} хв. тому"
    else:
        return "щойно"


def format_last_updated(last_updated):
    """Форматує час останнього оновлення"""
    if not last_updated:
        return "ніколи"

    now = datetime.now()
    time_diff = now - last_updated

    minutes = int(time_diff.total_seconds() // 60)
    if minutes < 1:
        return "щойно"
    elif minutes < 60:
        return f"{minutes} хв. тому"
    else:
        return last_updated.strftime("%d.%m.%Y %H:%M")


async def update_all_accounts():
    """Оновлення всіх акаунтів"""
    if not st.session_state.accounts:
        return

    progress_bar = st.progress(0)
    status_text = st.empty()

    accounts_to_update = [acc for acc in st.session_state.accounts if not acc.get('skip_check', False)]

    if not accounts_to_update:
        st.info("Немає акаунтів для оновлення (всі позначені як 'не перевіряти')")
        return

    for i, account in enumerate(accounts_to_update):
        status_text.text(f"Оновлення {i + 1}/{len(accounts_to_update)}: {account['name']}")
        await get_unread_stats_for_account(account)
        progress_bar.progress((i + 1) / len(accounts_to_update))
        time.sleep(0.5)  # Щоб уникнути занадто швидких запитів

    progress_bar.empty()
    status_text.empty()
    st.session_state.stats_updated += 1
    save_accounts_to_file()  # Зберігаємо оновлені дані
    st.success(f"Успішно оновлено {len(accounts_to_update)} акаунтів!")


def display_accounts_table():
    """Відображення таблиці з акаунтами"""
    if not st.session_state.accounts:
        st.info("Додайте акаунт, щоб почати моніторинг")
        return

    # Створюємо DataFrame для зручного відображення
    data = []
    for account in st.session_state.accounts:
        # Додаємо спеціальні позначки для адмінів та акаунтів, які не перевіряються
        group = account['group']
        if account.get('is_admin', False):
            group = f"👑 {group}"
        if account.get('skip_check', False):
            group = f"⏭️ {group}"

        data.append({
            "Група": group,
            "Акаунт": account['name'],
            "Повідомлення": account['unread_count'],
            "Час": format_time_diff(account['oldest_unread']),
            "Дані": account['status'],
            "Оновлено": format_last_updated(account['last_updated'])
        })

    df = pd.DataFrame(data)

    # Відображаємо таблицю
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Група": st.column_config.TextColumn(width="medium"),
            "Акаунт": st.column_config.TextColumn(width="medium"),
            "Повідомлення": st.column_config.NumberColumn(width="small"),
            "Час": st.column_config.TextColumn(width="medium"),
            "Дані": st.column_config.TextColumn(width="small"),
            "Оновлено": st.column_config.TextColumn(width="medium")
        }
    )


async def edit_account_form(account_index):
    """Форма для редагування акаунта з адміністративними налаштуваннями"""
    account = st.session_state.accounts[account_index]

    with st.form(key=f'edit_form_{account_index}'):
        st.subheader(f"Редагування акаунта: {account['name']}")

        # Основні налаштування
        col1, col2 = st.columns(2)
        with col1:
            new_group = st.text_input("Назва групи:", value=account['group'])

        # Адміністративні налаштування
        with col2:
            st.write("Адміністративні налаштування:")
            is_admin = st.checkbox("Адміністративний акаунт", value=account.get('is_admin', False))
            skip_check = st.checkbox("Не перевіряти цей акаунт", value=account.get('skip_check', False))

        update_stats = st.checkbox("Оновити статистику після змін", value=True)

        # Кнопки
        col_save, col_cancel = st.columns(2)
        with col_save:
            save_button = st.form_submit_button("💾 Зберегти зміни")
        with col_cancel:
            cancel_button = st.form_submit_button("❌ Скасувати")

        if save_button:
            # Оновлюємо дані
            st.session_state.accounts[account_index]['group'] = new_group
            st.session_state.accounts[account_index]['is_admin'] = is_admin
            st.session_state.accounts[account_index]['skip_check'] = skip_check

            # Оновлюємо статистику, якщо користувач вказав
            if update_stats:
                with st.spinner("Оновлення статистики..."):
                    await get_unread_stats_for_account(st.session_state.accounts[account_index])

            st.session_state.stats_updated += 1
            save_accounts_to_file()  # Зберігаємо зміни у файл
            st.session_state.editing_account_index = None
            st.success("Акаунт успішно оновлено!")
            st.rerun()

        if cancel_button:
            st.session_state.editing_account_index = None
            st.rerun()


async def main_ui():
    """Головний інтерфейс програми"""
    st.title("📊 Моніторинг повідомлень Telegram")
    init_session_state()

    # Додавання нового акаунта
    if st.session_state.login_stage != 'start':
        await login()
    else:
        with st.expander("Додати новий акаунт", expanded=False):
            await login()

    # Керування акаунтами
    st.subheader("Ваші акаунти")

    # Кнопки керування
    col1, col2 = st.columns(2)  # Змінено на 2 колонки
    with col1:
        if st.button("🔄 Оновити всі акаунти", use_container_width=True):
            await update_all_accounts()
            st.rerun()

    with col2:
        if st.button("🧹 Очистити всі акаунти", use_container_width=True):
            st.session_state.accounts = []
            save_accounts_to_file()  # Очищаємо файл
            st.session_state.stats_updated += 1
            st.success("Всі акаунти видалено!")
            st.rerun()

    # Відображення таблиці
    display_accounts_table()

    # Керування окремими акаунтами
    if st.session_state.accounts:
        st.subheader("Керування акаунтами")

        # Вибір акаунта для керування
        account_names = [f"{acc['group']} - {acc['name']}" for acc in st.session_state.accounts]
        selected_account = st.selectbox("Оберіть акаунт для керування:", account_names)

        if selected_account:
            acc_index = account_names.index(selected_account)
            account = st.session_state.accounts[acc_index]

            # Якщо ми вже редагуємо цей акаунт
            if st.session_state.editing_account_index == acc_index:
                await edit_account_form(acc_index)
                return

            # Видалено кнопку оновлення конкретного акаунта
            # Залишено лише 2 кнопки у 2 колонках
            col1, col2 = st.columns(2)  # Змінено на 2 колонки
            with col1:
                if st.button("✏️ Редагувати", use_container_width=True, key=f"edit_{acc_index}"):
                    st.session_state.editing_account_index = acc_index
                    st.rerun()

            with col2:
                if st.button("🗑️ Видалити", use_container_width=True, key=f"del_{acc_index}"):
                    account_name = account['name']
                    del st.session_state.accounts[acc_index]
                    save_accounts_to_file()  # Зберігаємо зміни
                    st.session_state.stats_updated += 1
                    st.success(f"Акаунт {account_name} видалено!")
                    st.rerun()
# Запуск додатка
if __name__ == '__main__':
    # Перевірка чи цикл подій вже запущений
    if not st.session_state.loop.is_running():
        st.session_state.loop.run_until_complete(main_ui())
    else:
        # Якщо цикл вже запущений, просто додаємо задачу
        asyncio.run_coroutine_threadsafe(main_ui(), st.session_state.loop)
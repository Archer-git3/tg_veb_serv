from telethon import TelegramClient, types
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from datetime import datetime, timezone
import streamlit as st
import pandas as pd
import time
import json
import os
import random
import asyncio
import pickle  # Для більш ефективного збереження даних
from db import async_session,init_db
from models import NotificationChat, TelegramAccount
from sqlalchemy import select



# Конфігурація
API_ID = 29148113
API_HASH = "0fba92868b9d99d1e63583a8fb751fb4"
ACCOUNTS_FILE = "telegram_accounts.json"

# Глобальний цикл подій
if not hasattr(st.session_state, 'loop') or st.session_state.loop.is_closed():
    st.session_state.loop = asyncio.new_event_loop()
asyncio.set_event_loop(st.session_state.loop)


def save_accounts_to_file():
    """Зберігає акаунти та групи у файл (новий бінарний формат)"""
    try:
        accounts_to_save = []
        for account in st.session_state.accounts:
            # Відбираємо тільки необхідні дані (без клієнта)
            accounts_to_save.append({
                'group': account['group'],
                'name': account['name'],
                'phone': account['phone'],
                'session_string': account['session_string'],
                'last_updated': account['last_updated'],
                'is_admin': account.get('is_admin', False),
                'skip_check': account.get('skip_check', False)
            })

        data = {
            "accounts": accounts_to_save,
            "groups": st.session_state.groups,
            "last_saved": datetime.now()
        }

        with open(ACCOUNTS_FILE, 'wb') as f:
            pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)

    except Exception as e:
        st.error(f"Помилка збереження даних: {str(e)}")


async def load_accounts_from_db():
    async with async_session() as session:
        result = await session.execute(select(TelegramAccount))
        return result.scalars().all()

def load_accounts_from_file():
    """Завантажує акаунти та групи з файлу, конвертує з JSON при необхідності"""
    # Спроба завантажити з нового формату
    if os.path.exists(ACCOUNTS_FILE):
        try:
            with open(ACCOUNTS_FILE, 'rb') as f:
                data = pickle.load(f)

            accounts_data = data.get("accounts", [])
            groups = data.get("groups", [])
            last_saved = data.get('last_saved', datetime.now())

            # Переконуємось, що всі обов'язкові поля ініціалізовані
            accounts = []
            for account in accounts_data:
                # Додаємо обов'язкові поля, якщо вони відсутні
                acc = {
                    'group': account.get('group', ''),
                    'name': account.get('name', ''),
                    'phone': account.get('phone', ''),
                    'session_string': account.get('session_string', ''),
                    'last_updated': account.get('last_updated', None),
                    'is_admin': account.get('is_admin', False),
                    'skip_check': account.get('skip_check', False),
                    'client': None,
                    'unread_count': account.get('unread_count', 0),  # Додано значення за замовчуванням
                    'oldest_unread': account.get('oldest_unread', None),  # Додано значення за замовчуванням
                    'status': account.get('status', '?')  # Додано значення за замовчуванням
                }
                accounts.append(acc)

            return accounts, groups, last_saved
        except (pickle.UnpicklingError, KeyError, EOFError, AttributeError) as e:
            # Обробка помилок формату
            st.error(f"Помилка завантаження нового формату: {str(e)}. Спробуємо конвертувати з JSON.")
            pass

    # Спроба завантажити зі старого JSON формату
    old_json_file = "telegram_accounts.json"
    if os.path.exists(old_json_file):
        try:
            with open(old_json_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            # Обробка старого формату
            if isinstance(data, dict) and "accounts" in data and "groups" in data:
                accounts_data = data["accounts"]
                groups = data["groups"]
            else:
                accounts_data = data
                groups = sorted(set(account.get('group', '') for account in accounts_data))

            accounts = []
            for account in accounts_data:
                # Конвертація дати
                last_updated = account.get('last_updated')
                if last_updated and isinstance(last_updated, str):
                    try:
                        last_updated = datetime.fromisoformat(last_updated)
                    except:
                        last_updated = None
                else:
                    last_updated = None

                acc = {
                    'group': account.get('group', ''),
                    'name': account.get('name', ''),
                    'phone': account.get('phone', ''),
                    'session_string': account.get('session_string', ''),
                    'last_updated': last_updated,
                    'is_admin': account.get('is_admin', False),
                    'skip_check': account.get('skip_check', False),
                    'client': None,
                    'unread_count': account.get('unread_count', 0),  # Додано значення за замовчуванням
                    'oldest_unread': account.get('oldest_unread', None),  # Додано значення за замовчуванням
                    'status': account.get('status', '?')  # Додано значення за замовчуванням
                }
                accounts.append(acc)

            # Зберігаємо у новому форматі
            data_to_save = {
                "accounts": accounts,
                "groups": groups,
                "last_saved": datetime.now()
            }
            with open(ACCOUNTS_FILE, 'wb') as f:
                pickle.dump(data_to_save, f, protocol=pickle.HIGHEST_PROTOCOL)

            # Перейменовуємо старий файл
            os.rename(old_json_file, old_json_file + ".old")
            return accounts, groups, datetime.now()
        except Exception as e:
            st.error(f"Помилка конвертації з JSON: {str(e)}")
            return [], [], None
    else:
        # Немає файлів
        return [], [], None


async def init_session_state():
    required_states = {
        'current_account': None,
        'login_stage': 'start',
        'phone_code_hash': None,
        'phone': None,
        'group_name': '',
        'stats_updated': 0,
        'editing_account_index': None,
        'active_form': None,
        'editing_group': None,
        'group_to_delete': None,
        'last_full_update': datetime.min,
        'db_initialized': False  # Додано прапорець ініціалізації БД
    }

    for key, default in required_states.items():
        if key not in st.session_state:
            st.session_state[key] = default

    # Ініціалізація БД виконується лише один раз
    if not st.session_state.db_initialized:
        await init_db()
        st.session_state.db_initialized = True

    # Завантаження акаунтів лише при першому запуску
    if 'accounts' not in st.session_state or 'groups' not in st.session_state:
        accounts_raw = await load_accounts_from_db()
        accounts = [a.to_dict() for a in accounts_raw]
        groups = sorted({acc["group"] for acc in accounts})
        st.session_state.accounts = accounts
        st.session_state.groups = groups

    # Ініціалізація полів акаунтів
    for account in st.session_state.accounts:
        account.setdefault('unread_count', 0)
        account.setdefault('oldest_unread', None)
        account.setdefault('status', '?')
        account.setdefault('last_updated', None)
        account.setdefault('is_admin', False)
        account.setdefault('skip_check', False)
        account.setdefault('client', None)


async def create_client(session_string=None):
    """Створення нового клієнта Telegram з кешуванням"""
    # Перевіряємо чи вже маємо активного клієнта
    if session_string:
        for account in st.session_state.accounts:
            if account.get('session_string') == session_string and account.get('client'):
                try:
                    if await account['client'].is_connected():
                        return account['client']
                except:
                    pass

    # Створюємо нового клієнта
    client = TelegramClient(
        StringSession(session_string) if session_string else StringSession(),
        API_ID,
        API_HASH,
        loop=st.session_state.loop
    )
    client.flood_sleep_threshold = 0
    await client.connect()

    # Кешуємо клієнт для майбутнього використання
    if session_string:
        for account in st.session_state.accounts:
            if account.get('session_string') == session_string:
                account['client'] = client

    return client


async def login():
    """Функція для авторизації користувача"""
    st.subheader("Додати новий акаунт")

    # Отримуємо список унікальних груп
    groups = st.session_state.groups

    # Вибір групи з випадаючого списку
    selected_group = st.selectbox(
        "Оберіть групу:",
        groups,
        index=0 if groups else None,
        key="login_group_select"  # Унікальний ключ
    )

    # Перевірка збереженої сесії
    if st.session_state.login_stage == 'start':
        phone = st.text_input("Введіть номер телефону (у міжнародному форматі):", key="login_phone_input")

        if st.button("Надіслати код", key="login_send_code_btn"):
            try:
                client = await create_client()
                sent_code = await client.send_code_request(phone)
                st.session_state.phone_code_hash = sent_code.phone_code_hash
                st.session_state.phone = phone
                st.session_state.group_name = selected_group
                st.session_state.client = client
                st.session_state.login_stage = 'phone_sent'
                st.rerun()
            except FloodWaitError as fwe:
                st.error(f"Занадто багато спроб. Спробуйте через {fwe.seconds} секунд.")
            except Exception as e:
                st.error(f"Помилка: {str(e)}")

    elif st.session_state.login_stage == 'phone_sent':
        code = st.text_input("Введіть отриманий код:", key="login_code_input")

        if st.button("Увійти", key="login_sign_in_btn"):
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
                'unread_count': 0,  # Додано
                'oldest_unread': None,  # Додано
                'status': '✓',  # Додано
                'last_updated': datetime.now(),
                'is_admin': False,
                'skip_check': False,
                'client': None  # Додано
            }

            st.session_state.accounts.append(new_account)

            # Оновлення списку груп
            if st.session_state.group_name not in st.session_state.groups:
                st.session_state.groups.append(st.session_state.group_name)
                st.session_state.groups.sort()

            save_accounts_to_file()  # Зберігаємо зміни у файл

            st.session_state.login_stage = 'start'
            await st.session_state.client.disconnect()
            st.session_state.client = None
            st.session_state.active_form = None  # Закриваємо форму після успішного додавання
            st.success(f"Акаунт {new_account['name']} успішно додано до групи '{st.session_state.group_name}'!")
            st.session_state.stats_updated += 1
            st.rerun()

    elif st.session_state.login_stage == '2fa':
        password = st.text_input("Введіть пароль двофакторної аутентифікації:", type="password", key="login_2fa_input")

        if st.button("Підтвердити", key="login_confirm_2fa_btn"):
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

                # Оновлення списку груп
                if st.session_state.group_name not in st.session_state.groups:
                    st.session_state.groups.append(st.session_state.group_name)
                    st.session_state.groups.sort()

                save_accounts_to_file()  # Зберігаємо зміни у файл

                st.session_state.login_stage = 'start'
                await st.session_state.client.disconnect()
                st.session_state.client = None
                st.session_state.active_form = None  # Закриваємо форму після успішного додавання
                st.success(f"Акаунт {new_account['name']} успішно додано до групи '{st.session_state.group_name}'!")
                st.session_state.stats_updated += 1
                st.rerun()
            except Exception as e:
                st.error(f"Помилка: {str(e)}")
                await st.session_state.client.disconnect()
                st.session_state.client = None
                st.session_state.login_stage = 'start'


async def get_unread_stats_for_account(account):
    """Оптимізоване отримання статистики для акаунта"""
    if account.get('skip_check', False):
        account['status'] = '⏭️ Пропущено'
        return

    # Ініціалізація лічильника спроб
    if 'attempts' not in account:
        account['attempts'] = 0

    # Максимальна кількість спроб
    MAX_ATTEMPTS = 2

    client = None
    try:
        client = await create_client(account['session_string'])

        if not await client.is_user_authorized():
            account['status'] = "Не авторизовано"
            return

        me = await client.get_me()
        unread_chats_count = 0
        oldest_unread_date = None

        # Отримуємо тільки необхідні діалоги з обмеженням
        dialogs = await client.get_dialogs(
            limit=150,  # Зменшена кількість для прискорення
            ignore_migrated=True,
            archived=False
        )

        # Швидка фільтрація діалогів
        for dialog in dialogs:
            # Швидка перевірка типу
            if not hasattr(dialog.entity, 'id') or dialog.entity.id == me.id:
                continue

            # Пропускаємо ботів і не-користувачів
            if getattr(dialog.entity, 'bot', False) or not isinstance(dialog.entity, types.User):
                continue

            # Перевіряємо непрочитані повідомлення
            if dialog.unread_count > 0:
                unread_chats_count += 1

                # Оновлюємо найстаріше повідомлення
                if oldest_unread_date is None or dialog.message.date < oldest_unread_date:
                    oldest_unread_date = dialog.message.date

        account['unread_count'] = unread_chats_count
        account['oldest_unread'] = oldest_unread_date
        account['status'] = '✓'
        account['last_updated'] = datetime.now()
        account['attempts'] = 0

    except FloodWaitError as fwe:
        account['attempts'] += 1

        if account['attempts'] > MAX_ATTEMPTS:
            account['status'] = f"❗ FloodWait {fwe.seconds}s"
            return

        wait_time = min(fwe.seconds + random.uniform(2, 5), 120)  # Обмежуємо очікування
        account['status'] = f"⏳ Чекаємо {wait_time:.1f}с"
        await asyncio.sleep(wait_time)
        await get_unread_stats_for_account(account)  # Рекурсивний повтор

    except Exception as e:
        account['status'] = f"⚠️ {str(e)[:20]}"
    finally:
        # Не закриваємо клієнта - залишаємо для кешу
        pass

async def update_all_accounts():
    """Паралельне оновлення акаунтів з обмеженням потоків"""
    if not st.session_state.accounts:
        return

    # Перевірка часу останнього оновлення
    if (datetime.now() - st.session_state.last_full_update).total_seconds() < 1800:  # 30 хв
        st.info("Дані ще актуальні. Повне оновлення доступне раз на 30 хвилин.")
        return

    accounts_to_update = [acc for acc in st.session_state.accounts if not acc.get('skip_check', False)]
    if not accounts_to_update:
        st.info("Немає акаунтів для оновлення")
        return

    progress_bar = st.progress(0)
    status_text = st.empty()
    progress_counter = 0

    # Оновлюємо прогрес
    def update_progress():
        nonlocal progress_counter
        progress_counter += 1
        progress_bar.progress(progress_counter / len(accounts_to_update))
        status_text.text(f"Оновлено {progress_counter}/{len(accounts_to_update)} акаунтів")

    # Використовуємо семафор для обмеження паралельних запитів
    MAX_CONCURRENT = 4
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async def safe_update(account):
        async with semaphore:
            await get_unread_stats_for_account(account)
            update_progress()
            await asyncio.sleep(random.uniform(1, 3))  # Невелика пауза

    # Створюємо та виконуємо задачі
    tasks = [safe_update(account) for account in accounts_to_update]
    await asyncio.gather(*tasks)

    progress_bar.empty()
    status_text.empty()
    st.session_state.last_full_update = datetime.now()
    save_accounts_to_file()
    st.success(f"Оновлення завершено для {len(accounts_to_update)} акаунтів!")


def format_time_diff(oldest_unread_date):
    """Оптимізоване форматування різниці часу"""
    if not oldest_unread_date:
        return "-"

    now = datetime.now(timezone.utc)
    time_diff = now - oldest_unread_date

    total_minutes = int(time_diff.total_seconds() // 60)

    if total_minutes < 1:
        return "<1 хв"
    elif total_minutes < 60:
        return f"{total_minutes} хв"

    hours = total_minutes // 60
    if hours < 24:
        return f"{hours} год"

    days = hours // 24
    return f"{days} дн"


def format_last_updated(last_updated):
    """Ефективне форматування часу останнього оновлення"""
    if not last_updated:
        return "ніколи"

    now = datetime.now()
    time_diff = now - last_updated
    minutes = int(time_diff.total_seconds() // 60)

    if minutes < 2:
        return "щойно"
    elif minutes < 60:
        return f"{minutes} хв"

    return last_updated.strftime("%d.%m %H:%M")


def display_accounts_table():
    if not st.session_state.accounts:
        st.info("Додайте акаунт, щоб почати моніторинг")
        return

    data = []
    for account in st.session_state.accounts:
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



def edit_account_form(account_index):
    """Форма для редагування акаунта з виправленою обробкою оновлення статистики"""
    account = st.session_state.accounts[account_index]

    with st.form(key=f'edit_form_{account_index}'):
        st.subheader(f"Редагування акаунта: {account['name']}")

        # Отримуємо список груп
        groups = st.session_state.groups

        # Вибір групи з випадаючого списку
        current_group = account['group']
        group_name = st.selectbox(
            "Оберіть групу:",
            groups,
            index=groups.index(current_group) if current_group in groups else 0,
            key=f"edit_group_select_{account_index}"  # Унікальний ключ
        )

        # Адміністративні налаштування
        col1, col2 = st.columns(2)
        with col1:
            is_admin = st.checkbox("Адміністративний акаунт", value=account.get('is_admin', False))
        with col2:
            skip_check = st.checkbox("Не перевіряти цей акаунт", value=account.get('skip_check', False))



        # Кнопки
        col_save, col_cancel = st.columns(2)
        with col_save:
            save_button = st.form_submit_button("💾 Зберегти зміни")
        with col_cancel:
            cancel_button = st.form_submit_button("❌ Скасувати")

        if save_button:
            # Оновлюємо дані
            if group_name:
                st.session_state.accounts[account_index]['group'] = group_name

            st.session_state.accounts[account_index]['is_admin'] = is_admin
            st.session_state.accounts[account_index]['skip_check'] = skip_check

            # Оновлюємо статистику, якщо користувач вказав


            st.session_state.stats_updated += 1
            save_accounts_to_file()  # Зберігаємо зміни у файл
            st.session_state.active_form = None  # Закриваємо форму
            st.session_state.editing_account_index = None
            st.success("Акаунт успішно оновлено!")
            st.rerun()

        if cancel_button:
            st.session_state.active_form = None  # Закриваємо форму
            st.session_state.editing_account_index = None
            st.rerun()



def create_new_group_form():
    """Форма для створення нової групи"""
    with st.form(key='new_group_form'):
        st.subheader("Створення нової групи")
        new_group_name = st.text_input("Назва нової групи:")

        col1, col2 = st.columns(2)
        with col1:
            create_button = st.form_submit_button("✅ Створити групу")
        with col2:
            cancel_button = st.form_submit_button("❌ Скасувати")

        if create_button:
            if not new_group_name:
                st.error("Будь ласка, введіть назву групи")
                return

            if new_group_name in st.session_state.groups:
                st.error("Група з такою назвою вже існує")
                return

            st.session_state.groups.append(new_group_name)
            st.session_state.groups.sort()
            save_accounts_to_file()
            st.session_state.active_form = None  # Закриваємо форму
            st.success(f"Група '{new_group_name}' успішно створена!")
            st.rerun()

        if cancel_button:
            st.session_state.active_form = None  # Закриваємо форму
            st.rerun()


def manage_groups_form():
    """Форма управління групами"""
    st.subheader("Керування групами")

    if not st.session_state.groups:
        st.info("Немає створених груп")
        return

    # Вибір групи для керування
    selected_group = st.selectbox(
        "Оберіть групу:",
        st.session_state.groups,
        key="group_management_select"  # Унікальний ключ
    )

    # Перевірка чи група використовується
    group_in_use = any(acc['group'] == selected_group for acc in st.session_state.accounts)

    col1, col2 = st.columns(2)
    with col1:
        if st.button("✏️ Перейменувати групу", use_container_width=True, key="rename_group_btn"):
            st.session_state.editing_group = selected_group
            st.rerun()

    with col2:
        # Кнопка видалення групи завжди активна
        if st.button("🗑️ Видалити групу", use_container_width=True, key="delete_group_btn"):
            st.session_state.group_to_delete = selected_group
            st.rerun()

    # Підтвердження видалення групи
    if st.session_state.group_to_delete == selected_group:
        st.warning(f"Ви впевнені, що хочете видалити групу '{selected_group}'?")
        st.warning("Ця дія видалить усі акаунти, що належать до цієї групи!")

        accounts_in_group = [acc for acc in st.session_state.accounts if acc['group'] == selected_group]
        if accounts_in_group:
            st.error(f"Увага: ця група містить {len(accounts_in_group)} акаунт(ів), які будуть видалені!")

        col1, col2 = st.columns(2)
        with col1:
            if st.button("✅ Так, видалити групу", key="confirm_delete_group", type="primary"):
                # Видаляємо всі акаунти групи
                st.session_state.accounts = [acc for acc in st.session_state.accounts if acc['group'] != selected_group]

                # Видаляємо групу зі списку груп
                st.session_state.groups.remove(selected_group)
                st.session_state.groups.sort()

                save_accounts_to_file()
                st.session_state.group_to_delete = None
                st.session_state.active_form = None
                st.success(f"Група '{selected_group}' та всі її акаунти успішно видалені!")
                st.rerun()
        with col2:
            if st.button("❌ Скасувати видалення", key="cancel_delete_group"):
                st.session_state.group_to_delete = None
                st.rerun()

    # Форма для перейменування групи
    if st.session_state.get('editing_group') == selected_group:
        with st.form(key='rename_group_form'):
            st.subheader(f"Перейменування групи: {st.session_state.editing_group}")
            new_name = st.text_input("Нова назва групи:", value=st.session_state.editing_group)

            col1, col2 = st.columns(2)
            with col1:
                rename_button = st.form_submit_button("💾 Зберегти зміни")
            with col2:
                cancel_button = st.form_submit_button("❌ Скасувати")

            if rename_button:
                if not new_name:
                    st.error("Будь ласка, введіть нову назву групи")
                    return

                if new_name in st.session_state.groups:
                    st.error("Група з такою назвою вже існує")
                    return

                # Оновлюємо назву групи у всіх акаунтах
                for account in st.session_state.accounts:
                    if account['group'] == st.session_state.editing_group:
                        account['group'] = new_name

                # Оновлюємо список груп
                st.session_state.groups.remove(st.session_state.editing_group)
                st.session_state.groups.append(new_name)
                st.session_state.groups.sort()

                save_accounts_to_file()
                st.session_state.editing_group = None
                st.success(f"Група успішно перейменована на '{new_name}'!")
                st.rerun()

            if cancel_button:
                st.session_state.editing_group = None
                st.rerun()


async def main_ui():
    """Головний інтерфейс програми"""
    st.title("📊 Моніторинг повідомлень Telegram")
    await init_db()
    await init_session_state()

    # Кнопки додавання нового акаунта та нової групи
    col1, col2 = st.columns(2)
    with col1:
        if st.button("➕ Додати новий акаунт", use_container_width=True, key="add_account_btn"):
            # Закриваємо всі інші форми
            st.session_state.active_form = 'add_account'
            st.session_state.login_stage = 'start'
            st.session_state.editing_account_index = None
            st.rerun()

    with col2:
        if st.button("🏗️ Додати нову групу", use_container_width=True, key="add_group_btn"):
            # Закриваємо всі інші форми
            st.session_state.active_form = 'add_group'
            st.session_state.login_stage = 'start'
            st.session_state.editing_account_index = None
            st.rerun()

    # Кнопка керування групами
    if st.button("👥 Керування групами", use_container_width=True, key="manage_groups_btn"):
        # Перемикаємо стан форми керування групами
        if st.session_state.get("active_form") == 'manage_groups':
            st.session_state.active_form = None
        else:
            st.session_state.active_form = 'manage_groups'
        st.rerun()

    # Відображення активної форми
    if st.session_state.get("active_form") == 'add_account':
        with st.expander("Додати новий акаунт", expanded=True):
            await login()

    elif st.session_state.get("active_form") == 'add_group':
        with st.expander("Створення нової групи", expanded=True):
            create_new_group_form()

    elif st.session_state.get("active_form") == 'manage_groups':
        with st.expander("Керування групами", expanded=True):
            manage_groups_form()

    elif st.session_state.editing_account_index is not None:
        with st.expander(f"Редагування акаунта", expanded=True):
            edit_account_form(st.session_state.editing_account_index)

    # Керування акаунтами
    st.subheader("Ваші акаунти")

    # Кнопка оновлення
    if st.button("🔄 Оновити всі акаунти", use_container_width=True, key="update_accounts_btn"):
        await update_all_accounts()
        st.rerun()

    # Відображення таблиці
    display_accounts_table()

    # Керування окремими акаунтами
    if st.session_state.accounts:
        st.subheader("Керування акаунтами")

        # Вибір акаунта для керування
        account_names = [f"{acc['group']} - {acc['name']}" for acc in st.session_state.accounts]
        selected_account = st.selectbox(
            "Оберіть акаунт для керування:",
            account_names,
            key="account_management_select"  # Унікальний ключ
        )

        if selected_account:
            acc_index = account_names.index(selected_account)
            account = st.session_state.accounts[acc_index]

            # Дві кнопки для керування
            col1, col2 = st.columns(2)
            with col1:
                if st.button("✏️ Редагувати", use_container_width=True, key=f"edit_btn_{acc_index}"):
                    # Закриваємо всі інші форми
                    st.session_state.active_form = None
                    st.session_state.editing_account_index = acc_index
                    st.rerun()
            with col2:
                if st.button("🗑️ Видалити", use_container_width=True, key=f"delete_btn_{acc_index}"):
                    account_name = account['name']
                    del st.session_state.accounts[acc_index]

                    # Оновлюємо список груп
                    groups = set(acc['group'] for acc in st.session_state.accounts)
                    st.session_state.groups = sorted(groups)

                    save_accounts_to_file()  # Зберігаємо зміни
                    st.session_state.stats_updated += 1
                    st.success(f"Акаунт {account_name} видалено!")
                    st.rerun()


async def main_async():
    await main_ui()

    
# Запуск додатка
if __name__ == "__main__":
    asyncio.run(main_async())

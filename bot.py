import os
import math
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from pymongo import MongoClient, DESCENDING, ASCENDING

load_dotenv()

# ================= НАСТРОЙКИ БАЗЫ ДАННЫХ MONGODB И БОТА =================

MONGO_URL = os.getenv("MONGO_URL")
if not MONGO_URL:
    raise ValueError("Ошибка: MONGO_URL не найден в файле .env или Variables Railway")

# Подключение к MongoDB Atlas с таймаутом для Railway
mongo_client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)

try:
    mongo_client.admin.command('ping')
    print("Успешное подключение к MongoDB Atlas!")
except Exception as e:
    print(f"Ошибка подключения к MongoDB: {e}")

db = mongo_client["discord_tickets_db"]

# Коллекции: тикеты, счётчики автоинкремента, удалённые тикеты и настройки бота
tickets_col = db["tickets"]
counters_col = db["counters"]
deleted_tickets_col = db["deleted_tickets"]
settings_col = db["settings"]

# Индексы для предотвращения дублирования транскриптов
tickets_col.create_index("transcript_url", unique=True, sparse=True)
deleted_tickets_col.create_index("transcript_url", unique=True, sparse=True)


def get_next_sequence_value(sequence_name: str) -> int:
    """Генерирует автоинкрементный ID (аналог AUTOINCREMENT в SQLite)."""
    seq = counters_col.find_one_and_update(
        {"_id": sequence_name},
        {"$inc": {"sequence_value": 1}},
        upsert=True,
        return_document=True
    )
    return seq["sequence_value"]


# ================= ВЛАДЕЛЕЦ БОТА =================
# Укажи сюда свой Discord ID. Команда .config / /config доступна только этому пользователю.
OWNER_ID = 000000000000000000  # TODO: укажите ваш Discord ID

# ================= ТАБЛИЦА ЛОГИРУЕМЫХ КОМАНД (значения по умолчанию) =================
# True  — действие этой команды будет отправляться в канал логов
# False — действие этой команды логироваться не будет
# Значения по умолчанию используются только при первом запуске бота — далее всё
# хранится в базе данных и меняется через .config / /config.
LOGGABLE_COMMANDS_DEFAULT = {
    "addticket": True,      # Добавление тикета/транскрипта в базу
    "deleteticket": True,   # Удаление тикета (канала) + запись в базу
    "deletelog": True,      # Удаление конкретного лога из базы
    "resetlogs": True,      # Полный сброс логов пользователя
    "ticketlogs": False,    # Просмотр логов модератора
    "ticketstats": False,   # Просмотр статистики
    "leaderboard": False,   # Просмотр лидерборда
    "help": False,          # Использование меню помощи
    "config": True,         # Изменения в настройках бота
}

# ================= НАСТРОЙКИ БОТА, ХРАНИМЫЕ В БД (цвет, footer, канал логов, тумблеры) =================

CONFIG = {}


def apply_config_globals():
    """Обновляет глобальные переменные EMBED_COLOR / FOOTER_TEXT из текущего CONFIG."""
    global EMBED_COLOR, FOOTER_TEXT
    EMBED_COLOR = discord.Color(CONFIG["embed_color"])
    FOOTER_TEXT = CONFIG["footer_text"]


def load_config():
    """Загружает настройки из БД при старте бота, создавая их при первом запуске."""
    global CONFIG
    defaults = {
        "_id": "config",
        "embed_color": 0x212121,
        "footer_text": "ТУСОВКА ДОРИСТА",
        "log_channel_id": None,
        "log_toggles": dict(LOGGABLE_COMMANDS_DEFAULT),
    }

    doc = settings_col.find_one({"_id": "config"})
    if doc is None:
        settings_col.insert_one(defaults)
        doc = defaults
    else:
        updated = False
        toggles = doc.get("log_toggles", {})
        for cmd, default_value in LOGGABLE_COMMANDS_DEFAULT.items():
            if cmd not in toggles:
                toggles[cmd] = default_value
                updated = True
        doc["log_toggles"] = toggles
        for key, value in defaults.items():
            if key not in doc:
                doc[key] = value
                updated = True
        if updated:
            settings_col.update_one({"_id": "config"}, {"$set": doc}, upsert=True)

    CONFIG = doc
    apply_config_globals()


def update_config(patch: dict):
    """Обновляет настройки бота и в БД, и в кэше памяти."""
    global CONFIG
    settings_col.update_one({"_id": "config"}, {"$set": patch}, upsert=True)
    CONFIG.update(patch)
    apply_config_globals()


load_config()

FOOTER_TEXT_DEFAULT_NAME = "ТУСОВКА ДОРИСТА"  # оставлено для справки, реальный текст берётся из CONFIG

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=".", intents=intents, help_command=None)

# Количество логов на одной странице пагинации
LOGS_PER_PAGE = 3

# ================= НАСТРОЙКА РОЛЕЙ И КАНАЛОВ =================

ALLOWED_CHANNEL_IDS = [1322968592202993746, 1537220150267220018]

SUPPORT_ROLE_IDS = [1501507449860001853, 1322962344040464424]
TRANSCRIPT_ROLE_IDS = [1542601770461569044, 1323348388762226759]
ADMIN_ROLE_IDS = [1322962317885046844, 1502684875868737796]

VALID_CATEGORIES = ["Помощь по серверу", "Получение призов", "Получение роли", "Покупка рекламы"]

# ================= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И ПРОВЕРКИ =================

def has_role_access(user: discord.Member | discord.User, role_ids: list[int]) -> bool:
    if not isinstance(user, discord.Member):
        return False
    if user.guild_permissions.administrator:
        return True
    return any(role.id in role_ids for role in user.roles)

def is_admin_user(user: discord.Member | discord.User) -> bool:
    return has_role_access(user, ADMIN_ROLE_IDS)

def is_owner_user(user: discord.Member | discord.User) -> bool:
    return user.id == OWNER_ID

def check_access(user: discord.Member | discord.User, channel_id: int, role_ids: list[int]) -> tuple[bool, str]:
    if not isinstance(user, discord.Member):
        return False, "Команды работают только на сервере."

    if user.guild_permissions.administrator:
        return True, ""

    if not has_role_access(user, role_ids):
        return False, "У вас недостаточно ролей для использования этой команды."

    if ALLOWED_CHANNEL_IDS and channel_id not in ALLOWED_CHANNEL_IDS:
        channels_mention = ", ".join([f"<#{cid}>" for cid in ALLOWED_CHANNEL_IDS])
        return False, f"Эта команда доступна только в каналах: {channels_mention}"

    return True, ""

def get_user_group_name(user: discord.Member | discord.User, channel_id: int) -> str:
    if is_owner_user(user):
        return "Владелец"
    if is_admin_user(user):
        return "Администрация"
    if has_role_access(user, TRANSCRIPT_ROLE_IDS):
        return "Транскрипты"
    if has_role_access(user, SUPPORT_ROLE_IDS):
        return "Поддержка"
    return "Пользователь"

def check_access_decorator(role_ids: list[int], is_slash: bool = False):
    async def predicate(target):
        user = target.user if is_slash else target.author
        channel_id = target.channel_id if is_slash else target.channel.id
        ok, msg = check_access(user, channel_id, role_ids)
        if not ok:
            raise app_commands.AppCommandError(msg) if is_slash else commands.CheckFailure(msg)
        return True
    return app_commands.check(predicate) if is_slash else commands.check(predicate)

def check_support_prefix(): return check_access_decorator(SUPPORT_ROLE_IDS)
def check_transcript_prefix(): return check_access_decorator(TRANSCRIPT_ROLE_IDS)
def check_admin_prefix(): return check_access_decorator(ADMIN_ROLE_IDS)

def check_support_slash(): return check_access_decorator(SUPPORT_ROLE_IDS, is_slash=True)
def check_transcript_slash(): return check_access_decorator(TRANSCRIPT_ROLE_IDS, is_slash=True)
def check_admin_slash(): return check_access_decorator(ADMIN_ROLE_IDS, is_slash=True)

def is_valid_addticket(transcript_url: str, category: str) -> bool:
    return "https://discord.com/" in transcript_url and category in VALID_CATEGORIES

def is_valid_transcript_link(transcript_url: str) -> bool:
    return "https://discord.com/" in transcript_url

def is_transcript_exists(transcript_url: str) -> bool:
    return tickets_col.find_one({"transcript_url": transcript_url}) is not None

def is_deleted_transcript_exists(transcript_url: str) -> bool:
    return deleted_tickets_col.find_one({"transcript_url": transcript_url}) is not None

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Bot logged in as {bot.user}")

# ================= ЛОГИРОВАНИЕ ДЕЙСТВИЙ В КАНАЛ ЛОГОВ =================

def build_log_embed(title: str, lines: list[str]) -> discord.Embed:
    embed = discord.Embed(title=title, description="\n".join(lines), color=EMBED_COLOR, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=FOOTER_TEXT)
    return embed

async def send_log(command_name: str, embed: discord.Embed):
    """Отправляет embed в канал логов, если для этой команды логирование включено."""
    if not CONFIG.get("log_toggles", {}).get(command_name, False):
        return
    channel_id = CONFIG.get("log_channel_id")
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            return
    try:
        await channel.send(embed=embed)
    except Exception:
        pass

# ================= ОБРАБОТКА ОШИБОК =================

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.send(f"<:bruh:1521904409582375174> {error}")
        return
    raise error

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandNotFound):
        return
    msg = f"<:bruh:1521904409582375174> {error}" if str(error) else "<:bruh:1521904409582375174> Произошла ошибка при выполнении команды."
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)

# ================= БИЗНЕС-ЛОГИКА ТИКЕТОВ =================

def get_monthly_tickets(staff_id: int) -> int:
    date_30_days = datetime.now(timezone.utc) - timedelta(days=30)
    return tickets_col.count_documents({
        "staff_id": staff_id,
        "created_at": {"$gte": date_30_days}
    })

def process_add_ticket(author_user: discord.User, staff_user: discord.User, transcript_url: str, category: str):
    ticket_id = get_next_sequence_value("ticket_id")
    now = datetime.now(timezone.utc)

    tickets_col.insert_one({
        "_id": ticket_id,
        "staff_id": staff_user.id,
        "author_id": author_user.id,
        "transcript_url": transcript_url,
        "category": category,
        "created_at": now
    })

    monthly_count = get_monthly_tickets(staff_user.id)
    discord_timestamp = f"<t:{int(now.timestamp())}:F>"

    embed = discord.Embed(title=f"<:logs:1522340749998428160> Лог тикета — {staff_user.display_name}", color=EMBED_COLOR)
    embed.add_field(name="Дата транскрипта", value=discord_timestamp, inline=False)
    embed.add_field(name="Номер лога", value=f"№{ticket_id}", inline=False)
    embed.add_field(name="Ссылка на транскрипт", value=transcript_url, inline=False)
    embed.add_field(name="Кто вёл тикет", value=str(staff_user.id), inline=False)
    embed.add_field(name="Внёс в базу", value=author_user.mention, inline=False)
    embed.add_field(name="Тикетов за последний месяц", value=str(monthly_count), inline=False)
    embed.add_field(name="Категория", value=category, inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed, ticket_id

def process_ticket_logs(target_user: discord.User, page: int = 1) -> tuple[discord.Embed, int]:
    logs = list(tickets_col.find({"staff_id": target_user.id}).sort("_id", ASCENDING))

    if not logs:
        embed = discord.Embed(
            title=f"Тикеты — {target_user.name}",
            description="<:bruh:1521904409582375174> У этого модератора нет ни одного тикета.",
            color=EMBED_COLOR,
        )
        embed.set_footer(text=f"Страница 1/1 (0 логов) • {FOOTER_TEXT}")
        return embed, 1

    total_logs = len(logs)
    total_pages = math.ceil(total_logs / LOGS_PER_PAGE)
    current_page = max(1, min(page, total_pages))

    start_idx = (current_page - 1) * LOGS_PER_PAGE
    current_logs = logs[start_idx:start_idx + LOGS_PER_PAGE]

    embed = discord.Embed(title=f"<:ticket:1522343287816716379> Тикеты — {target_user.name}", color=EMBED_COLOR)
    embed.description = f"{target_user.id}\n" + "—" * 28

    lines = []
    for doc in current_logs:
        log_id = doc["_id"]
        transcript_url = doc["transcript_url"]
        category = doc["category"]
        created_at = doc["created_at"]

        if isinstance(created_at, datetime):
            formatted_date = f"<t:{int(created_at.timestamp())}:F>"
        else:
            formatted_date = str(created_at)

        lines.append(
            f"**Тикет №{log_id}**\n"
            f"**Модератор:** {target_user.name} ({target_user.mention})\n"
            f"**Категория:** {category}\n"
            f"**Транскрипт:** {transcript_url}\n"
            f"{formatted_date}"
        )

    embed.description += "\n\n" + "\n\n".join(lines)
    embed.set_footer(text=f"Страница {current_page}/{total_pages} ({total_logs} логов) • {FOOTER_TEXT}")
    return embed, total_pages

# ================= КОМПОНЕНТ НАВИГАЦИИ (КНОПКИ СТРАНИЦ) =================

class TicketLogsPaginationView(discord.ui.View):
    def __init__(self, author_id: int, target_user: discord.User, total_pages: int, initial_page: int = 1):
        super().__init__(timeout=120)  # Таймаут активности кнопок — 2 минуты
        self.author_id = author_id
        self.target_user = target_user
        self.total_pages = total_pages
        self.current_page = initial_page
        self.update_buttons()

    def update_buttons(self):
        self.prev_button.disabled = (self.current_page <= 1)
        self.next_button.disabled = (self.current_page >= self.total_pages)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "<:bruh:1521904409582375174> Вы не можете переключать страницы в чужом меню.",
                ephemeral=True
            )
            return False
        return True

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 1:
            self.current_page -= 1
            embed, _ = process_ticket_logs(self.target_user, self.current_page)
            self.update_buttons()
            await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < self.total_pages:
            self.current_page += 1
            embed, _ = process_ticket_logs(self.target_user, self.current_page)
            self.update_buttons()
            await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        try:
            if hasattr(self, 'message') and self.message:
                await self.message.edit(view=self)
        except Exception:
            pass

def process_ticket_stats(target_user: discord.User):
    now = datetime.now(timezone.utc)
    d7 = now - timedelta(days=7)
    d30 = now - timedelta(days=30)

    c7_s = tickets_col.count_documents({"staff_id": target_user.id, "created_at": {"$gte": d7}})
    c30_s = tickets_col.count_documents({"staff_id": target_user.id, "created_at": {"$gte": d30}})
    call_s = tickets_col.count_documents({"staff_id": target_user.id})

    c7_a = tickets_col.count_documents({"author_id": target_user.id, "created_at": {"$gte": d7}})
    c30_a = tickets_col.count_documents({"author_id": target_user.id, "created_at": {"$gte": d30}})
    call_a = tickets_col.count_documents({"author_id": target_user.id})

    c7_d = deleted_tickets_col.count_documents({"staff_id": target_user.id, "created_at": {"$gte": d7}})
    c30_d = deleted_tickets_col.count_documents({"staff_id": target_user.id, "created_at": {"$gte": d30}})
    call_d = deleted_tickets_col.count_documents({"staff_id": target_user.id})

    l_s_doc = tickets_col.find_one({"staff_id": target_user.id}, sort=[("_id", DESCENDING)])
    l_a_doc = tickets_col.find_one({"author_id": target_user.id}, sort=[("_id", DESCENDING)])

    def fmt_last(doc):
        if not doc:
            return "—"
        dt = doc.get("created_at")
        if isinstance(dt, datetime):
            return f"<t:{int(dt.timestamp())}:R>"
        return str(dt)

    embed = discord.Embed(color=EMBED_COLOR)
    embed.set_author(name=target_user.name, icon_url=target_user.display_avatar.url)
    embed.title = "<:ticket:1522343287816716379> Статистика тикетов и транскриптов"
    embed.add_field(name="За последние 7 дней:", value=f"• Тикетов: **{c7_s}**\n• Транскриптов: **{c7_a}**\n• Удалено тикетов: **{c7_d}**", inline=True)
    embed.add_field(name="За последние 30 дней:", value=f"• Тикетов: **{c30_s}**\n• Транскриптов: **{c30_a}**\n• Удалено тикетов: **{c30_d}**", inline=True)
    embed.add_field(name="За всё время:", value=f"• Тикетов: **{call_s}**\n• Транскриптов: **{call_a}**\n• Удалено тикетов: **{call_d}**", inline=True)
    embed.add_field(
        name="<:lighting:1522337543360872489> Активность:",
        value=f"• **Последний проведённый тикет:** {fmt_last(l_s_doc)}\n• **Последний внесённый транскрипт:** {fmt_last(l_a_doc)}",
        inline=False
    )
    embed.set_footer(text=f"ID: {target_user.id} • Сегодня в {now.strftime('%H:%M')} • {FOOTER_TEXT}")
    return embed

def process_leaderboard():
    now = datetime.now(timezone.utc)
    d7 = now - timedelta(days=7)
    d30 = now - timedelta(days=30)

    def get_top(collection, field: str, min_date=None, exclude_zero=False):
        match_stage = {}
        if min_date:
            match_stage["created_at"] = {"$gte": min_date}
        if exclude_zero:
            match_stage[field] = {"$ne": 0}

        pipeline = []
        if match_stage:
            pipeline.append({"$match": match_stage})

        pipeline.extend([
            {"$group": {"_id": f"${field}", "cnt": {"$sum": 1}}},
            {"$sort": {"cnt": -1}},
            {"$limit": 5}
        ])

        results = list(collection.aggregate(pipeline))
        return [(doc["_id"], doc["cnt"]) for doc in results]

    def format_top(top_list, unit_label="тикетов"):
        if not top_list:
            return "— *Нет данных*"
        return "\n".join([f"`{idx}.` <@{u_id}> — **{count}** {unit_label}" for idx, (u_id, count) in enumerate(top_list, 1)])

    embed = discord.Embed(title="<:sparkles:1522342290494849034> Лидерборд тикетов и транскриптов", color=EMBED_COLOR)

    embed.add_field(name="<:ticket:1522343287816716379> Тикетов (7 дн.)", value=format_top(get_top(tickets_col, "staff_id", d7)), inline=True)
    embed.add_field(name="<:logs:1522340749998428160> Транскриптов (7 дн.)", value=format_top(get_top(tickets_col, "author_id", d7, True), "транскриптов"), inline=True)
    embed.add_field(name="🗑️ Удалено тикетов (7 дн.)", value=format_top(get_top(deleted_tickets_col, "staff_id", d7), "удалений"), inline=True)

    embed.add_field(name="<:ticket:1522343287816716379> Тикетов (30 дн.)", value=format_top(get_top(tickets_col, "staff_id", d30)), inline=True)
    embed.add_field(name="<:logs:1522340749998428160> Транскриптов (30 дн.)", value=format_top(get_top(tickets_col, "author_id", d30, True), "транскриптов"), inline=True)
    embed.add_field(name="🗑️ Удалено тикетов (30 дн.)", value=format_top(get_top(deleted_tickets_col, "staff_id", d30), "удалений"), inline=True)

    embed.add_field(name="<:ticket:1522343287816716379> Тикетов (Все время)", value=format_top(get_top(tickets_col, "staff_id")), inline=True)
    embed.add_field(name="<:logs:1522340749998428160> Транскриптов (Все время)", value=format_top(get_top(tickets_col, "author_id", exclude_zero=True), "транскриптов"), inline=True)
    embed.add_field(name="🗑️ Удалено тикетов (Все время)", value=format_top(get_top(deleted_tickets_col, "staff_id"), "удалений"), inline=True)

    embed.set_footer(text=f"Сегодня в {now.strftime('%H:%M')} • {FOOTER_TEXT}")
    return embed

def delete_ticket_log(log_id: int):
    """Удаляет одну запись лога тикета из базы (команда .deletelog)."""
    return tickets_col.find_one_and_delete({"_id": log_id})

def reset_tickets(staff_id: int) -> int:
    """Полностью очищает все логи (тикеты, транскрипты, удаления) конкретного пользователя."""
    res1 = tickets_col.delete_many({"$or": [{"staff_id": staff_id}, {"author_id": staff_id}]})
    res2 = deleted_tickets_col.delete_many({"$or": [{"staff_id": staff_id}, {"deleted_by": staff_id}]})
    return res1.deleted_count + res2.deleted_count

def process_delete_ticket_channel(author_user: discord.User, log_id: int, transcript_url: str):
    """
    Записывает факт удаления тикета (канала) в базу и добавляет +1 к счётчику
    удалённых тикетов того модератора, который изначально вёл тикет №log_id.
    Возвращает (embed, error_code). error_code: None | "not_found" | "duplicate".
    """
    original = tickets_col.find_one({"_id": log_id})
    if not original:
        return None, "not_found"

    if is_deleted_transcript_exists(transcript_url):
        return None, "duplicate"

    staff_id = original["staff_id"]
    deleted_id = get_next_sequence_value("deleted_ticket_id")
    now = datetime.now(timezone.utc)

    deleted_tickets_col.insert_one({
        "_id": deleted_id,
        "original_log_id": log_id,
        "staff_id": staff_id,
        "deleted_by": author_user.id,
        "transcript_url": transcript_url,
        "created_at": now,
    })

    monthly_count = deleted_tickets_col.count_documents({
        "staff_id": staff_id,
        "created_at": {"$gte": datetime.now(timezone.utc) - timedelta(days=30)}
    })
    discord_timestamp = f"<t:{int(now.timestamp())}:F>"

    embed = discord.Embed(title="🗑️ Удалён тикет", color=EMBED_COLOR)
    embed.add_field(name="Дата удаления", value=discord_timestamp, inline=False)
    embed.add_field(name="Номер лога тикета", value=f"№{log_id}", inline=False)
    embed.add_field(name="Ссылка на транскрипт удаления", value=transcript_url, inline=False)
    embed.add_field(name="Ответственный модератор", value=f"<@{staff_id}>", inline=False)
    embed.add_field(name="Внёс в базу", value=author_user.mention, inline=False)
    embed.add_field(name="Удалённых тикетов за последний месяц", value=str(monthly_count), inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed, None

# ================= EMBEDS ПОМОЩИ =================

def get_help_categories_embed(user: discord.Member | discord.User, channel_id: int):
    group_name = get_user_group_name(user, channel_id)
    embed = discord.Embed(
        title="<:staff:1522338131339251823> Меню команд бота",
        description="Выберите категорию команд ниже.\nВ будущем здесь появится больше категорий.",
        color=EMBED_COLOR,
    )
    embed.add_field(name="🎫 Тикеты", value="Команды для работы с тикетами и транскриптами.", inline=False)
    embed.set_footer(text=f"Ваша текущая группа: {group_name} • {FOOTER_TEXT}")
    return embed

def get_help_embed(user: discord.Member | discord.User, channel_id: int):
    group_name = get_user_group_name(user, channel_id)

    embed = discord.Embed(
        title="<:ticket:1522343287816716379> Категория: Тикеты",
        description="Используй префикс `.` или слэш-команды `/`",
        color=EMBED_COLOR,
    )

    if has_role_access(user, SUPPORT_ROLE_IDS):
        embed.add_field(
            name="<:ticket:1522343287816716379> Команды Support",
            value=(
                "`.help` — Показать меню команд.\n\n"
                "`.ticketstats [ID / упоминание]`\n> *Посмотреть статистику тикетов, транскриптов и удалений.*\n\n"
                "`.leaderboard`\n> *Посмотреть топ модераторов по тикетам, транскриптам и удалениям.*"
            ),
            inline=False,
        )

    if has_role_access(user, TRANSCRIPT_ROLE_IDS):
        embed.add_field(
            name="<:logs:1522340749998428160> Команды Transcript",
            value=(
                "`.addticket [ID модератора] [ссылка] [категория]`\n> *Записать новый обработанный тикет в базу данных.*\n\n"
                "`.ticketlogs [ID / упоминание]`\n> *Посмотреть логи тикетов модератора (с кнопками листания).*\n\n"
                "`.deleteticket [номер лога] [ссылка на транскрипт]`\n> *Записать удаление тикета (канала) и добавить +1 удалённый тикет модератору.*"
            ),
            inline=False,
        )

    if is_admin_user(user):
        embed.add_field(
            name="<:mod:1522343179205087363> Команды Администрации",
            value=(
                "`.deletelog [ID лога]`\n> *Удалить конкретный лог тикета по ID.*\n\n"
                "`.resetlogs [ID / упоминание]`\n> *Очистить абсолютно все логи модератора (тикеты, транскрипты, удаления).*"
            ),
            inline=False,
        )

    if is_owner_user(user):
        embed.add_field(
            name="⚙️ Команды Владельца",
            value="`.config`\n> *Открыть меню настроек бота (цвет, footer, канал логов, логируемые команды).*",
            inline=False,
        )

    embed.set_footer(text=f"Ваша текущая группа: {group_name} • {FOOTER_TEXT}")
    return embed

def get_addticket_usage_embed():
    categories_str = ", ".join([f"`{c}`" for c in VALID_CATEGORIES])
    embed = discord.Embed(color=EMBED_COLOR, title="Команда: addticket", description="Добавить новый лог об обработанном тикете")
    embed.add_field(name="Кулдаун:", value="3 секунды (Для Администрации отсутствует)", inline=False)
    embed.add_field(
        name="Правила аргументов:",
        value=(
            "1. Ссылка должна содержать: `https://discord.com/`\n"
            "2. Вы **не можете** указать свой собственный ID\n"
            "3. Один и тот же транскрипт нельзя вносить дважды\n"
            f"4. Допустимые категории: {categories_str}"
        ),
        inline=False,
    )
    embed.add_field(name="Использование:", value="`.addticket [ID модератора] [ссылка на транскрипт] [категория]`", inline=False)
    embed.add_field(name="Пример:", value="`.addticket 851443344718430210 https://discord.com/channels/... Получение призов`", inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed

def get_ticketstats_usage_embed():
    embed = discord.Embed(color=EMBED_COLOR, title="Команда: ticketstats", description="Посмотреть статистику тикетов, транскриптов и удалений модератора")
    embed.add_field(name="Использование:", value="`.ticketstats [упоминание / ID модератора]`", inline=False)
    embed.add_field(name="Примеры:", value="`.ts` — показать свою статистику\n`.ticketstats [ID модератора]` — показать статистику выбранного модератора", inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed

def get_ticketlogs_usage_embed():
    embed = discord.Embed(color=EMBED_COLOR, title="Команда: ticketlogs", description="Посмотреть список обработанных тикетов модератора")
    embed.add_field(name="Использование:", value="`.ticketlogs [упоминание / ID модератора]`", inline=False)
    embed.add_field(name="Примеры:", value="`.tl` — показать свои логи\n`.ticketlogs [ID]` — показать логи выбранного модератора", inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed

def get_deleteticket_usage_embed():
    embed = discord.Embed(color=EMBED_COLOR, title="Команда: deleteticket", description="Записать удаление тикета (канала) в базу данных")
    embed.add_field(
        name="Правила аргументов:",
        value=(
            "1. Номер лога — это номер, который бот показал при `.addticket` (поле «Номер лога»)\n"
            "2. Ссылка должна содержать: `https://discord.com/`\n"
            "3. Один и тот же транскрипт удаления нельзя внести дважды\n"
            "4. Модератору, который изначально вёл этот тикет, добавляется +1 удалённый тикет"
        ),
        inline=False,
    )
    embed.add_field(name="Использование:", value="`.deleteticket [номер лога] [ссылка на транскрипт]`", inline=False)
    embed.add_field(name="Пример:", value="`.deleteticket 42 https://discord.com/channels/...`", inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed

def get_deletelog_usage_embed():
    embed = discord.Embed(color=EMBED_COLOR, title="Команда: deletelog 🔒", description="Удалить один лог тикета из базы данных (Только Администрация)")
    embed.add_field(name="Использование:", value="`.deletelog [номер лога]`", inline=False)
    embed.add_field(name="Пример:", value="`.del 15` — удалить лог под номером 15", inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed

def get_resetlogs_usage_embed():
    embed = discord.Embed(color=EMBED_COLOR, title="Команда: resetlogs (алиасы: .rt) 🔒", description="Полностью очистить логи пользователя (Только Администрация)")
    embed.add_field(name="Использование:", value="`.resetlogs [упоминание / ID пользователя]`", inline=False)
    embed.add_field(name="Пример:", value="`.resetlogs [ID модератора]` — сбросить все логи указанного модератора", inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed

# ================= МЕНЮ ПОМОЩИ С КАТЕГОРИЯМИ (КНОПКИ) =================

class TicketHelpView(discord.ui.View):
    """Показывается после нажатия на категорию «Тикеты». Содержит кнопку «Назад»."""
    def __init__(self, author_id: int, channel_id: int):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.channel_id = channel_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "<:bruh:1521904409582375174> Это меню вызвали не вы.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = get_help_categories_embed(interaction.user, self.channel_id)
        view = HelpCategoriesView(self.author_id, self.channel_id)
        await interaction.response.edit_message(embed=embed, view=view)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

class HelpCategoriesView(discord.ui.View):
    """Стартовое меню помощи со списком категорий. Пока только «Тикеты» — новые
    категории добавляются простым добавлением новой @discord.ui.button ниже."""
    def __init__(self, author_id: int, channel_id: int):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.channel_id = channel_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "<:bruh:1521904409582375174> Это меню вызвали не вы.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Тикеты", emoji="🎫", style=discord.ButtonStyle.primary)
    async def tickets_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = get_help_embed(interaction.user, self.channel_id)
        view = TicketHelpView(self.author_id, self.channel_id)
        await interaction.response.edit_message(embed=embed, view=view)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

# ================= МЕНЮ НАСТРОЕК БОТА (.config) =================

def get_config_embed():
    channel_id = CONFIG.get("log_channel_id")
    channel_mention = f"<#{channel_id}>" if channel_id else "Не установлен"

    toggles_lines = []
    for cmd in LOGGABLE_COMMANDS_DEFAULT:
        state = "✅" if CONFIG.get("log_toggles", {}).get(cmd, False) else "❌"
        toggles_lines.append(f"{state} `{cmd}`")

    embed = discord.Embed(
        title="⚙️ Настройки бота",
        description="Меню настроек доступно только владельцу бота. Выберите пункт в списке ниже.",
        color=EMBED_COLOR,
    )
    embed.add_field(name="Цвет embed", value=f"`#{CONFIG['embed_color']:06X}`", inline=True)
    embed.add_field(name="Footer текст", value=CONFIG["footer_text"], inline=True)
    embed.add_field(name="Канал логов", value=channel_mention, inline=True)
    embed.add_field(name="Команды для логов", value="\n".join(toggles_lines), inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed

class ColorModal(discord.ui.Modal, title="Изменить цвет embed"):
    color_input = discord.ui.TextInput(label="HEX цвет без #, например 212121", placeholder="212121", min_length=6, max_length=6)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.color_input.value.strip().lstrip("#")
        try:
            value = int(raw, 16)
            if not (0 <= value <= 0xFFFFFF):
                raise ValueError
        except ValueError:
            await interaction.response.send_message(f"<:bruh:1521904409582375174> Некорректный HEX цвет: `{raw}`", ephemeral=True)
            return
        update_config({"embed_color": value})
        await interaction.response.send_message(f"<a:gif_verify:1522328481956888686> Цвет embed изменён на `#{raw.upper()}`.", ephemeral=True)
        await send_log("config", build_log_embed("⚙️ Изменена настройка бота", [
            f"**Кто изменил:** {interaction.user.mention}",
            "**Что изменено:** Цвет embed",
            f"**Новое значение:** `#{raw.upper()}`",
        ]))

class FooterModal(discord.ui.Modal, title="Изменить footer текст"):
    footer_input = discord.ui.TextInput(label="Новый текст footer", placeholder="ТУСОВКА ДОРИСТА", max_length=100)

    async def on_submit(self, interaction: discord.Interaction):
        update_config({"footer_text": self.footer_input.value})
        await interaction.response.send_message(f"<a:gif_verify:1522328481956888686> Footer текст изменён на: `{self.footer_input.value}`.", ephemeral=True)
        await send_log("config", build_log_embed("⚙️ Изменена настройка бота", [
            f"**Кто изменил:** {interaction.user.mention}",
            "**Что изменено:** Footer текст",
            f"**Новое значение:** {self.footer_input.value}",
        ]))

class LogChannelView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=180)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("<:bruh:1521904409582375174> Эта команда доступна только владельцу бота.", ephemeral=True)
            return False
        return True

    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="Выберите канал для логов...", channel_types=[discord.ChannelType.text])
    async def channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        channel = select.values[0]
        update_config({"log_channel_id": channel.id})
        embed = get_config_embed()
        await interaction.response.edit_message(embed=embed, view=self)
        await send_log("config", build_log_embed("⚙️ Изменена настройка бота", [
            f"**Кто изменил:** {interaction.user.mention}",
            "**Что изменено:** Канал логов",
            f"**Новое значение:** {channel.mention}",
        ]))

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = get_config_embed()
        view = ConfigMainView(self.owner_id)
        await interaction.response.edit_message(embed=embed, view=view)

class LogTogglesSelect(discord.ui.Select):
    def __init__(self):
        options = []
        for cmd, default_value in LOGGABLE_COMMANDS_DEFAULT.items():
            enabled = CONFIG.get("log_toggles", {}).get(cmd, default_value)
            options.append(discord.SelectOption(label=cmd, value=cmd, default=enabled))
        super().__init__(
            placeholder="Отметьте команды, которые нужно логировать...",
            min_values=0,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        enabled_set = set(self.values)
        new_toggles = {cmd: (cmd in enabled_set) for cmd in LOGGABLE_COMMANDS_DEFAULT}
        update_config({"log_toggles": new_toggles})
        embed = get_config_embed()
        view = LogTogglesView(interaction.user.id)
        await interaction.response.edit_message(embed=embed, view=view)
        await send_log("config", build_log_embed("⚙️ Изменена настройка бота", [
            f"**Кто изменил:** {interaction.user.mention}",
            "**Что изменено:** Список логируемых команд",
        ]))

class LogTogglesView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.add_item(LogTogglesSelect())
        back = discord.ui.Button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
        back.callback = self.back_callback
        self.add_item(back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("<:bruh:1521904409582375174> Эта команда доступна только владельцу бота.", ephemeral=True)
            return False
        return True

    async def back_callback(self, interaction: discord.Interaction):
        embed = get_config_embed()
        view = ConfigMainView(self.owner_id)
        await interaction.response.edit_message(embed=embed, view=view)

class ConfigMainView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=180)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("<:bruh:1521904409582375174> Эта команда доступна только владельцу бота.", ephemeral=True)
            return False
        return True

    @discord.ui.select(
        placeholder="Выберите, что настроить...",
        options=[
            discord.SelectOption(label="Цвет embed", value="color", emoji="🎨", description="Изменить цвет всех embed-сообщений бота"),
            discord.SelectOption(label="Footer текст", value="footer", emoji="📝", description="Изменить текст в footer всех embed-сообщений"),
            discord.SelectOption(label="Канал логов", value="log_channel", emoji="📨", description="Куда бот будет отправлять логи действий"),
            discord.SelectOption(label="Команды для логов", value="log_toggles", emoji="🧾", description="Выбрать, какие команды логировать"),
        ],
    )
    async def select_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        value = select.values[0]
        if value == "color":
            await interaction.response.send_modal(ColorModal())
        elif value == "footer":
            await interaction.response.send_modal(FooterModal())
        elif value == "log_channel":
            embed = get_config_embed()
            view = LogChannelView(self.owner_id)
            await interaction.response.edit_message(embed=embed, view=view)
        elif value == "log_toggles":
            embed = get_config_embed()
            view = LogTogglesView(self.owner_id)
            await interaction.response.edit_message(embed=embed, view=view)

# ================= СЛЭШ КОМАНДЫ =================

@bot.tree.command(name="help", description="Показать полный список команд бота")
@check_support_slash()
async def slash_help(interaction: discord.Interaction):
    embed = get_help_categories_embed(interaction.user, interaction.channel_id)
    view = HelpCategoriesView(interaction.user.id, interaction.channel_id)
    await interaction.response.send_message(embed=embed, view=view)
    await send_log("help", build_log_embed("📖 Использовано меню помощи", [f"**Кто:** {interaction.user.mention}"]))

@bot.tree.command(name="addticket", description="Записать новый обработанный тикет")
@app_commands.describe(staff="ID участника персонала", transcript="Ссылка на транскрипт", category="Категория тикета")
@app_commands.choices(category=[app_commands.Choice(name=cat, value=cat) for cat in VALID_CATEGORIES])
@check_transcript_slash()
@app_commands.checks.cooldown(1, 3.0, key=lambda i: i.user.id)
async def slash_add_ticket(interaction: discord.Interaction, staff: str, transcript: str, category: str):
    if not is_valid_addticket(transcript, category):
        await interaction.response.send_message(embed=get_addticket_usage_embed(), ephemeral=True)
        return

    try:
        staff_id = int(staff.strip("<@!>"))
    except ValueError:
        await interaction.response.send_message(f"<:bruh:1521904409582375174> Некорректный ID: `{staff}`.", ephemeral=True)
        return

    if staff_id == interaction.user.id:
        await interaction.response.send_message("<:bruh:1521904409582375174> Вы не можете занести тикет, который провели сами!", ephemeral=True)
        return

    if is_transcript_exists(transcript):
        await interaction.response.send_message("<:bruh:1521904409582375174> Этот транскрипт уже внесен", ephemeral=True)
        return

    try:
        staff_user = await bot.fetch_user(staff_id)
    except (discord.NotFound, discord.HTTPException):
        await interaction.response.send_message(f"<:bruh:1521904409582375174> Не удалось найти пользователя по ID `{staff}`.", ephemeral=True)
        return

    embed, ticket_id = process_add_ticket(interaction.user, staff_user, transcript, category)
    await interaction.response.send_message(embed=embed)
    await send_log("addticket", build_log_embed("📥 Добавлен тикет", [
        f"**Номер лога:** №{ticket_id}",
        f"**Внёс:** {interaction.user.mention}",
        f"**Модератор:** <@{staff_id}>",
        f"**Категория:** {category}",
        f"**Транскрипт:** {transcript}",
    ]))

@slash_add_ticket.error
async def slash_add_ticket_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        if is_admin_user(interaction.user):
            return
        await interaction.response.send_message(
            f"<:zzz:1522341702852022412> Подождите ещё {error.retry_after:.1f} сек.", ephemeral=True
        )
        return

@bot.tree.command(name="deleteticket", description="Записать удаление тикета (канала) в базу данных")
@app_commands.describe(log_id="Номер лога тикета (из .addticket)", transcript="Ссылка на транскрипт удаления")
@check_transcript_slash()
@app_commands.checks.cooldown(1, 3.0, key=lambda i: i.user.id)
async def slash_delete_ticket_channel(interaction: discord.Interaction, log_id: int, transcript: str):
    if not is_valid_transcript_link(transcript):
        await interaction.response.send_message(embed=get_deleteticket_usage_embed(), ephemeral=True)
        return

    embed, error_code = process_delete_ticket_channel(interaction.user, log_id, transcript)
    if error_code == "not_found":
        await interaction.response.send_message(f"<:bruh:1521904409582375174> Тикет с номером лога `{log_id}` не найден.", ephemeral=True)
        return
    if error_code == "duplicate":
        await interaction.response.send_message("<:bruh:1521904409582375174> Этот транскрипт удаления уже внесён.", ephemeral=True)
        return

    await interaction.response.send_message(embed=embed)
    await send_log("deleteticket", build_log_embed("🗑️ Удалён тикет (канал)", [
        f"**Номер лога:** №{log_id}",
        f"**Внёс:** {interaction.user.mention}",
        f"**Транскрипт удаления:** {transcript}",
    ]))

@slash_delete_ticket_channel.error
async def slash_delete_ticket_channel_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        if is_admin_user(interaction.user):
            return
        await interaction.response.send_message(
            f"<:zzz:1522341702852022412> Подождите ещё {error.retry_after:.1f} сек.", ephemeral=True
        )
        return

@bot.tree.command(name="ticketstats", description="Посмотреть статистику тикетов")
@app_commands.describe(staff="Участник персонала")
@check_support_slash()
async def slash_ticket_stats(interaction: discord.Interaction, staff: discord.User = None):
    target = staff or interaction.user
    embed = process_ticket_stats(target)
    await interaction.response.send_message(embed=embed)
    await send_log("ticketstats", build_log_embed("📊 Просмотр статистики", [
        f"**Кто смотрел:** {interaction.user.mention}",
        f"**Чья статистика:** <@{target.id}>",
    ]))

@bot.tree.command(name="ticketlogs", description="Посмотреть тикеты пользователя")
@app_commands.describe(staff="Участник персонала")
@check_transcript_slash()
async def slash_ticket_logs(interaction: discord.Interaction, staff: discord.User = None):
    target = staff or interaction.user
    embed, total_pages = process_ticket_logs(target, 1)

    if total_pages > 1:
        view = TicketLogsPaginationView(interaction.user.id, target, total_pages, 1)
        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()
    else:
        await interaction.response.send_message(embed=embed)
    await send_log("ticketlogs", build_log_embed("📖 Просмотр логов тикетов", [
        f"**Кто смотрел:** {interaction.user.mention}",
        f"**Чьи логи:** <@{target.id}>",
    ]))

@bot.tree.command(name="leaderboard", description="Посмотреть топ модераторов по тикетам и транскриптам")
@check_support_slash()
async def slash_leaderboard(interaction: discord.Interaction):
    embed = process_leaderboard()
    await interaction.response.send_message(embed=embed)
    await send_log("leaderboard", build_log_embed("🏆 Просмотр лидерборда", [f"**Кто:** {interaction.user.mention}"]))

@bot.tree.command(name="deletelog", description="Удалить лог тикета по номеру (Только для Админов)")
@app_commands.describe(log_id="Номер лога")
@check_admin_slash()
async def slash_delete_log(interaction: discord.Interaction, log_id: int):
    ticket = delete_ticket_log(log_id)
    if not ticket:
        await interaction.response.send_message(f"<:bruh:1521904409582375174> Лог с номером `{log_id}` не найден.", ephemeral=True)
        return
    await interaction.response.send_message(f"<a:gif_verify:1522328481956888686> Лог `{log_id}` удалён.")
    await send_log("deletelog", build_log_embed("❌ Удалён лог тикета", [
        f"**Номер лога:** №{log_id}",
        f"**Кто удалил:** {interaction.user.mention}",
    ]))

@bot.tree.command(name="resetlogs", description="Удалить все логи пользователя (Только для Админов)")
@app_commands.describe(staff="Модератор")
@check_admin_slash()
async def slash_reset_logs(interaction: discord.Interaction, staff: discord.User):
    count = reset_tickets(staff.id)
    await interaction.response.send_message(f"<a:gif_verify:1522328481956888686> Удалено логов модератора **{staff.name}**: `{count}`.")
    await send_log("resetlogs", build_log_embed("♻️ Сброшены логи пользователя", [
        f"**Пользователь:** <@{staff.id}>",
        f"**Кто сбросил:** {interaction.user.mention}",
        f"**Удалено записей:** {count}",
    ]))

@bot.tree.command(name="config", description="Настройки бота (только для владельца)")
async def slash_config(interaction: discord.Interaction):
    if not is_owner_user(interaction.user):
        await interaction.response.send_message("<:bruh:1521904409582375174> Эта команда доступна только владельцу бота.", ephemeral=True)
        return
    embed = get_config_embed()
    view = ConfigMainView(interaction.user.id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    await send_log("config", build_log_embed("⚙️ Открыто меню настроек", [f"**Кто:** {interaction.user.mention}"]))

# ================= ПРЕФИКСНЫЕ КОМАНДЫ =================

@bot.command(name="help")
@check_support_prefix()
async def prefix_help(ctx: commands.Context):
    embed = get_help_categories_embed(ctx.author, ctx.channel.id)
    view = HelpCategoriesView(ctx.author.id, ctx.channel.id)
    await ctx.send(embed=embed, view=view)
    await send_log("help", build_log_embed("📖 Использовано меню помощи", [f"**Кто:** {ctx.author.mention}"]))

@bot.command(name="addticket", aliases=["t", "ticket"])
@check_transcript_prefix()
@commands.cooldown(1, 3.0, commands.BucketType.user)
async def prefix_add_ticket(ctx: commands.Context, *, args: str = None):
    if not args:
        await ctx.send(embed=get_addticket_usage_embed())
        return

    parts = args.split(maxsplit=2)
    if len(parts) < 3:
        await ctx.send(embed=get_addticket_usage_embed())
        return

    staff_raw, transcript, category = parts[0], parts[1], parts[2]

    if not is_valid_addticket(transcript, category):
        await ctx.send(embed=get_addticket_usage_embed())
        return

    try:
        staff_id = int(staff_raw.strip("<@!>"))
    except ValueError:
        await ctx.send("<:bruh:1521904409582375174> Укажите корректный numeric ID модератора.")
        return

    if staff_id == ctx.author.id:
        await ctx.send("<:bruh:1521904409582375174> Вы не можете занести тикет, который провели сами!")
        return

    if is_transcript_exists(transcript):
        await ctx.send("<:bruh:1521904409582375174> Этот транскрипт уже внесен")
        return

    try:
        staff_user = await bot.fetch_user(staff_id)
    except (discord.NotFound, discord.HTTPException):
        await ctx.send(f"<:bruh:1521904409582375174> Не удалось найти пользователя по ID `{staff_raw}`.")
        return

    embed, ticket_id = process_add_ticket(ctx.author, staff_user, transcript, category)
    await ctx.send(embed=embed)
    await send_log("addticket", build_log_embed("📥 Добавлен тикет", [
        f"**Номер лога:** №{ticket_id}",
        f"**Внёс:** {ctx.author.mention}",
        f"**Модератор:** <@{staff_id}>",
        f"**Категория:** {category}",
        f"**Транскрипт:** {transcript}",
    ]))

@prefix_add_ticket.error
async def prefix_add_ticket_error(ctx: commands.Context, error):
    if isinstance(error, commands.CommandOnCooldown):
        if is_admin_user(ctx.author):
            ctx.command.reset_cooldown(ctx)
            await ctx.reinvoke()
            return
        await ctx.send(f"<:zzz:1522341702852022412> Подождите ещё {error.retry_after:.1f} сек.")
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.send(f"<:bruh:1521904409582375174> {error}")
        return
    await ctx.send(embed=get_addticket_usage_embed())

@bot.command(name="deleteticket", aliases=["dt"])
@check_transcript_prefix()
@commands.cooldown(1, 3.0, commands.BucketType.user)
async def prefix_delete_ticket_channel(ctx: commands.Context, *, args: str = None):
    if not args:
        await ctx.send(embed=get_deleteticket_usage_embed())
        return

    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        await ctx.send(embed=get_deleteticket_usage_embed())
        return

    log_id_raw, transcript = parts[0], parts[1]

    try:
        log_id = int(log_id_raw)
    except ValueError:
        await ctx.send(f"<:bruh:1521904409582375174> Номер лога должен быть числом: `{log_id_raw}`.")
        return

    if not is_valid_transcript_link(transcript):
        await ctx.send(embed=get_deleteticket_usage_embed())
        return

    embed, error_code = process_delete_ticket_channel(ctx.author, log_id, transcript)
    if error_code == "not_found":
        await ctx.send(f"<:bruh:1521904409582375174> Тикет с номером лога `{log_id}` не найден.")
        return
    if error_code == "duplicate":
        await ctx.send("<:bruh:1521904409582375174> Этот транскрипт удаления уже внесён.")
        return

    await ctx.send(embed=embed)
    await send_log("deleteticket", build_log_embed("🗑️ Удалён тикет (канал)", [
        f"**Номер лога:** №{log_id}",
        f"**Внёс:** {ctx.author.mention}",
        f"**Транскрипт удаления:** {transcript}",
    ]))

@prefix_delete_ticket_channel.error
async def prefix_delete_ticket_channel_error(ctx: commands.Context, error):
    if isinstance(error, commands.CommandOnCooldown):
        if is_admin_user(ctx.author):
            ctx.command.reset_cooldown(ctx)
            await ctx.reinvoke()
            return
        await ctx.send(f"<:zzz:1522341702852022412> Подождите ещё {error.retry_after:.1f} сек.")
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.send(f"<:bruh:1521904409582375174> {error}")
        return
    await ctx.send(embed=get_deleteticket_usage_embed())

@bot.command(name="ticketstats", aliases=["ts"])
@check_support_prefix()
async def prefix_ticket_stats(ctx: commands.Context, staff: discord.User = None):
    target = staff or ctx.author
    embed = process_ticket_stats(target)
    await ctx.send(embed=embed)
    await send_log("ticketstats", build_log_embed("📊 Просмотр статистики", [
        f"**Кто смотрел:** {ctx.author.mention}",
        f"**Чья статистика:** <@{target.id}>",
    ]))

@prefix_ticket_stats.error
async def prefix_ticket_stats_error(ctx: commands.Context, error):
    if isinstance(error, commands.BadArgument):
        await ctx.send(embed=get_ticketstats_usage_embed())

@bot.command(name="ticketlogs", aliases=["tl", "tlogs"])
@check_transcript_prefix()
async def prefix_ticket_logs(ctx: commands.Context, staff: discord.User = None):
    target = staff or ctx.author
    embed, total_pages = process_ticket_logs(target, 1)

    if total_pages > 1:
        view = TicketLogsPaginationView(ctx.author.id, target, total_pages, 1)
        msg = await ctx.send(embed=embed, view=view)
        view.message = msg
    else:
        await ctx.send(embed=embed)
    await send_log("ticketlogs", build_log_embed("📖 Просмотр логов тикетов", [
        f"**Кто смотрел:** {ctx.author.mention}",
        f"**Чьи логи:** <@{target.id}>",
    ]))

@prefix_ticket_logs.error
async def prefix_ticket_logs_error(ctx: commands.Context, error):
    if isinstance(error, commands.BadArgument):
        await ctx.send(embed=get_ticketlogs_usage_embed())

@bot.command(name="leaderboard", aliases=["lb", "top", "leaderstats"])
@check_support_prefix()
async def prefix_leaderboard(ctx: commands.Context):
    embed = process_leaderboard()
    await ctx.send(embed=embed)
    await send_log("leaderboard", build_log_embed("🏆 Просмотр лидерборда", [f"**Кто:** {ctx.author.mention}"]))

@bot.command(name="deletelog", aliases=["del"])
@check_admin_prefix()
async def prefix_delete_log(ctx: commands.Context, log_id: int = None):
    if log_id is None:
        await ctx.send(embed=get_deletelog_usage_embed())
        return
    ticket = delete_ticket_log(log_id)
    if not ticket:
        await ctx.send(f"<:bruh:1521904409582375174> Лог под номером `{log_id}` не найден.")
        return
    await ctx.send(f"<a:gif_verify:1522328481956888686> Лог `{log_id}` удалён.")
    await send_log("deletelog", build_log_embed("❌ Удалён лог тикета", [
        f"**Номер лога:** №{log_id}",
        f"**Кто удалил:** {ctx.author.mention}",
    ]))

@prefix_delete_log.error
async def prefix_delete_log_error(ctx: commands.Context, error):
    if isinstance(error, commands.BadArgument):
        await ctx.send(embed=get_deletelog_usage_embed())

@bot.command(name="resetlogs", aliases=["rt"])
@check_admin_prefix()
async def prefix_reset_logs(ctx: commands.Context, staff: discord.User = None):
    if staff is None:
        await ctx.send(embed=get_resetlogs_usage_embed())
        return
    count = reset_tickets(staff.id)
    await ctx.send(f"<a:gif_verify:1522328481956888686> Удалено логов модератора **{staff.name}**: `{count}`.")
    await send_log("resetlogs", build_log_embed("♻️ Сброшены логи пользователя", [
        f"**Пользователь:** <@{staff.id}>",
        f"**Кто сбросил:** {ctx.author.mention}",
        f"**Удалено записей:** {count}",
    ]))

@prefix_reset_logs.error
async def prefix_reset_logs_error(ctx: commands.Context, error):
    if isinstance(error, commands.BadArgument):
        await ctx.send(embed=get_resetlogs_usage_embed())

@bot.command(name="config")
async def prefix_config(ctx: commands.Context):
    if not is_owner_user(ctx.author):
        await ctx.send("<:bruh:1521904409582375174> Эта команда доступна только владельцу бота.")
        return
    embed = get_config_embed()
    view = ConfigMainView(ctx.author.id)
    await ctx.send(embed=embed, view=view)
    await send_log("config", build_log_embed("⚙️ Открыто меню настроек", [f"**Кто:** {ctx.author.mention}"]))

# ================= ЗАПУСК БОТА =================

TOKEN = os.getenv("DISCORD_TOKEN")
if TOKEN:
    bot.run(TOKEN)
else:
    print("Ошибка: DISCORD_TOKEN не найден в файле .env")

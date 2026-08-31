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
message_stats_col = db["message_stats"]
bump_stats_col = db["bump_stats"]

# Индексы для предотвращения дублирования транскриптов
tickets_col.create_index("transcript_url", unique=True, sparse=True)
deleted_tickets_col.create_index("transcript_url", unique=True, sparse=True)
message_stats_col.create_index([("channel_id", 1), ("day", 1), ("user_id", 1)])
bump_stats_col.create_index([("channel_id", 1), ("day", 1), ("user_id", 1)])


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
OWNER_ID = 851443344718430210 

# ================= КОНФИГ, ХРАНИМЫЙ В MONGODB =================

# Меняется только в коде: сюда вставляются прямые URL header-картинок.
# Если оставить "", дополнительный header-embed для этого раздела не отправляется.
HEADER_IMAGES = {
    "help": "",
    "config": "",
    "leaderboard": "",
    "tickets": "",
    "results": "",
}

# Меняются через .config -> Эмбеды.
EMOJI_DEFAULTS = {
    "success": "<a:gif_verify:1522328481956888686>",
    "warning": "<:zzz:1522341702852022412>",
    "error": "<:bruh:1521904409582375174>",
    "permission": "<:imcrine:1543711667647418381>",
    "info": "ℹ️",
    "delete": "🗑️",
}

LOGGABLE_COMMANDS_DEFAULT = {
    "addticket": True,
    "deleteticket": True,
    "deletelog": True,
    "resetlogs": True,
    "ticketlogs": False,
    "ticketstats": False,
    "leaderboard": False,
    "count": False,
    "help": False,
    "config": True,
}

# Группы доступа. Роли и команды редактируются через .config -> Доступ.
PERMISSION_GROUPS_DEFAULT = {
    "support": {
        "name": "Поддержка",
        "emoji": "🛟",
        "roles": [1501507449860001853, 1322962344040464424],
        "commands": ["help", "ticketstats", "leaderboard", "count"],
    },
    "transcript": {
        "name": "Транскрипты",
        "emoji": "🧾",
        "roles": [1542601770461569044, 1323348388762226759],
        "commands": ["addticket", "deleteticket", "ticketlogs"],
    },
    "admin": {
        "name": "Администрация",
        "emoji": "🛡️",
        "roles": [1322962317885046844, 1502684875868737796],
        "commands": ["deletelog", "resetlogs"],
    },
}

COMMAND_LABELS = {
    "help": "help",
    "addticket": "addticket",
    "deleteticket": "deleteticket",
    "ticketstats": "ticketstats",
    "ticketlogs": "ticketlogs",
    "leaderboard": "leaderboard",
    "deletelog": "deletelog",
    "resetlogs": "resetlogs",
    "count": "подсчет / count",
}

CONFIG = {}

def apply_config_globals():
    global EMBED_COLOR, FOOTER_TEXT
    EMBED_COLOR = discord.Color(CONFIG["embed_color"])
    FOOTER_TEXT = CONFIG["footer_text"]

def load_config():
    global CONFIG
    defaults = {
        "_id": "config",
        "embed_color": 0x212121,
        "footer_text": "ТУСОВКА ДОРИСТА",
        "log_channel_id": 1543903998677876836,
        "log_toggles": dict(LOGGABLE_COMMANDS_DEFAULT),
        "leaderboard_layout": "horizontal",
        "counting_channel_id": None,
        "bump_channel_id": None,
        "permission_groups": {k: dict(v) for k, v in PERMISSION_GROUPS_DEFAULT.items()},
        "emojis": dict(EMOJI_DEFAULTS),
    }

    doc = settings_col.find_one({"_id": "config"})
    if doc is None:
        settings_col.insert_one(defaults)
        doc = defaults
    else:
        updated = False
        for key, value in defaults.items():
            if key not in doc:
                doc[key] = value
                updated = True

        toggles = doc.get("log_toggles", {})
        for cmd, default_value in LOGGABLE_COMMANDS_DEFAULT.items():
            if cmd not in toggles:
                toggles[cmd] = default_value
                updated = True
        doc["log_toggles"] = toggles

        groups = doc.get("permission_groups", {})
        for group_key, group_default in PERMISSION_GROUPS_DEFAULT.items():
            if group_key not in groups:
                groups[group_key] = dict(group_default)
                updated = True
            else:
                for key, value in group_default.items():
                    if key not in groups[group_key]:
                        groups[group_key][key] = value
                        updated = True
        doc["permission_groups"] = groups

        emojis = doc.get("emojis", {})
        for key, value in EMOJI_DEFAULTS.items():
            if key not in emojis:
                emojis[key] = value
                updated = True
        doc["emojis"] = emojis

        if doc.get("leaderboard_layout") not in ("horizontal", "vertical"):
            doc["leaderboard_layout"] = "horizontal"
            updated = True

        if updated:
            settings_col.update_one({"_id": "config"}, {"$set": doc}, upsert=True)

    CONFIG = doc
    apply_config_globals()

def update_config(patch: dict):
    global CONFIG
    settings_col.update_one({"_id": "config"}, {"$set": patch}, upsert=True)
    for key, value in patch.items():
        if "." not in key:
            CONFIG[key] = value
            continue
        target = CONFIG
        parts = key.split(".")
        for part in parts[:-1]:
            if not isinstance(target.get(part), dict):
                target[part] = {}
            target = target[part]
        target[parts[-1]] = value
    apply_config_globals()

def get_emoji(name: str) -> str:
    return CONFIG.get("emojis", {}).get(name, EMOJI_DEFAULTS.get(name, ""))

def make_status_embed(title: str, message: str, kind: str = "info") -> discord.Embed:
    emoji = get_emoji(kind)
    embed = discord.Embed(
        title=title,
        description=f"{emoji} {message}" if emoji else message,
        color=EMBED_COLOR,
    )
    embed.set_footer(text=FOOTER_TEXT)
    return embed

def make_header_embed(header_key: str):
    url = HEADER_IMAGES.get(header_key, "")
    if not url:
        return None
    embed = discord.Embed(color=EMBED_COLOR)
    embed.set_image(url=url)
    return embed

async def send_embed_with_header(destination, embed: discord.Embed, header_key: str | None = None, **kwargs):
    header = make_header_embed(header_key) if header_key else None
    if isinstance(destination, discord.Interaction):
        if header:
            await destination.response.send_message(embed=header, **kwargs)
            return await destination.followup.send(embed=embed, wait=True, **kwargs)
        await destination.response.send_message(embed=embed, **kwargs)
        return await destination.original_response()
    if header:
        await destination.send(embed=header)
    return await destination.send(embed=embed, **kwargs)

load_config()

FOOTER_TEXT_DEFAULT_NAME = "ТУСОВКА ДОРИСТА"  # оставлено для справки, реальный текст берётся из CONFIG

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=".", intents=intents, help_command=None)

# Количество логов на одной странице пагинации
LOGS_PER_PAGE = 3

# ================= НАСТРОЙКА РОЛЕЙ И КАНАЛОВ =================

ALLOWED_CHANNEL_IDS = [1322968592202993746, 1537220150267220018]
VALID_CATEGORIES = ["Помощь по серверу", "Получение призов", "Получение роли", "Покупка рекламы"]

def get_group(group_key: str) -> dict:
    return CONFIG.get("permission_groups", {}).get(group_key, {})

def get_group_role_ids(group_key: str) -> list[int]:
    return [int(x) for x in get_group(group_key).get("roles", [])]

def is_owner_user(user: discord.Member | discord.User) -> bool:
    return user.id == OWNER_ID

def user_in_group(user: discord.Member | discord.User, group_key: str) -> bool:
    if not isinstance(user, discord.Member):
        return False
    if is_owner_user(user) or user.guild_permissions.administrator:
        return True
    return any(role.id in get_group_role_ids(group_key) for role in user.roles)

def has_role_access(user: discord.Member | discord.User, role_ids: list[int]) -> bool:
    if not isinstance(user, discord.Member):
        return False
    if is_owner_user(user) or user.guild_permissions.administrator:
        return True
    return any(role.id in role_ids for role in user.roles)

def is_admin_user(user: discord.Member | discord.User) -> bool:
    return user_in_group(user, "admin")

def get_user_group_name(user: discord.Member | discord.User, channel_id: int) -> str:
    if is_owner_user(user):
        return "Владелец"
    for key in ("admin", "transcript", "support"):
        if user_in_group(user, key):
            return get_group(key).get("name", key)
    return "Пользователь"

def check_access(user: discord.Member | discord.User, channel_id: int, command_name: str) -> tuple[bool, str]:
    if not isinstance(user, discord.Member):
        return False, "Команды работают только на сервере."
    if is_owner_user(user) or user.guild_permissions.administrator:
        return True, ""
    if ALLOWED_CHANNEL_IDS and channel_id not in ALLOWED_CHANNEL_IDS:
        channels_mention = ", ".join(f"<#{cid}>" for cid in ALLOWED_CHANNEL_IDS)
        return False, f"Эта команда доступна только в каналах: {channels_mention}"

    matched_group = False
    for group in CONFIG.get("permission_groups", {}).values():
        role_ids = [int(x) for x in group.get("roles", [])]
        if any(role.id in role_ids for role in user.roles):
            matched_group = True
            if command_name in group.get("commands", []):
                return True, ""
    if not matched_group:
        return False, "У вас недостаточно ролей для использования этой команды."
    return False, "Ваша группа не имеет доступа к этой команде. Доступ можно изменить в конфигурации бота."

def check_access_decorator(role_ids=None, is_slash: bool = False, command_name: str | None = None):
    configured_command_name = command_name
    async def predicate(target):
        user = target.user if is_slash else target.author
        channel_id = target.channel_id if is_slash else target.channel.id
        command = getattr(target, "command", None)
        current_command_name = configured_command_name or getattr(command, "name", "") or ""
        ok, msg = check_access(user, channel_id, current_command_name)
        if not ok:
            if is_slash:
                raise app_commands.AppCommandError(msg)
            raise commands.CheckFailure(msg)
        return True
    return app_commands.check(predicate) if is_slash else commands.check(predicate)

# Имена оставлены, чтобы не пришлось менять декораторы существующих команд.
def check_support_prefix(command_name=None): return check_access_decorator(command_name=command_name)
def check_transcript_prefix(command_name=None): return check_access_decorator(command_name=command_name)
def check_admin_prefix(command_name=None): return check_access_decorator(command_name=command_name)
def check_support_slash(command_name=None): return check_access_decorator(is_slash=True, command_name=command_name)
def check_transcript_slash(command_name=None): return check_access_decorator(is_slash=True, command_name=command_name)
def check_admin_slash(command_name=None): return check_access_decorator(is_slash=True, command_name=command_name)

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

async def send_error_ctx(ctx: commands.Context, message: str, title: str = "Ошибка"):
    await ctx.send(embed=make_status_embed(title, message, "error"))

async def send_error_interaction(interaction: discord.Interaction, message: str, title: str = "Ошибка", ephemeral: bool = True):
    embed = make_status_embed(title, message, "error")
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=ephemeral)

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CheckFailure):
        await send_error_ctx(ctx, str(error) or "У вас нет доступа к этой команде.", "Недостаточно прав")
        return
    await send_error_ctx(ctx, str(error) or "Произошла ошибка при выполнении команды.")

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandNotFound):
        return
    if isinstance(error, app_commands.CommandOnCooldown):
        await send_error_interaction(interaction, f"Подождите ещё {error.retry_after:.1f} сек.", "Команда на кулдауне")
        return
    await send_error_interaction(interaction, str(error) or "Произошла ошибка при выполнении команды.")

# ================= СТАТИСТИКА СООБЩЕНИЙ И BUMP =================

def utc_day(dt=None):
    dt = dt or datetime.now(timezone.utc)
    return dt.date().isoformat()

def record_message_stat(message: discord.Message):
    channel_id = CONFIG.get("counting_channel_id")
    if not channel_id or message.channel.id != channel_id or message.author.bot:
        return
    message_stats_col.update_one(
        {"channel_id": channel_id, "user_id": message.author.id, "day": utc_day()},
        {"$inc": {"count": 1}},
        upsert=True,
    )

def record_bump_stat(interaction: discord.Interaction):
    channel_id = CONFIG.get("bump_channel_id")
    if not channel_id or interaction.channel_id != channel_id or interaction.user.bot:
        return
    if interaction.type != discord.InteractionType.application_command:
        return
    command_name = (interaction.data or {}).get("name")
    if not command_name:
        return
    bump_stats_col.update_one(
        {"channel_id": channel_id, "user_id": interaction.user.id, "day": utc_day()},
        {"$inc": {"count": 1}, "$set": {"last_command": command_name}},
        upsert=True,
    )

def process_results():
    now = datetime.now(timezone.utc)
    d7 = (now - timedelta(days=7)).date().isoformat()
    d30 = (now - timedelta(days=30)).date().isoformat()

    def top_stats(collection, start_day, channel_id):
        if not channel_id:
            return []
        pipeline = [
            {"$match": {"channel_id": channel_id, "day": {"$gte": start_day}}},
            {"$group": {"_id": "$user_id", "count": {"$sum": "$count"}}},
            {"$sort": {"count": -1}},
            {"$limit": 10},
        ]
        return [(doc["_id"], doc["count"]) for doc in collection.aggregate(pipeline)]

    def fmt(rows):
        if not rows:
            return "— *Нет данных*"
        return "\n".join(f"`{i}.` <@{uid}> — **{count}**" for i, (uid, count) in enumerate(rows, 1))

    embed = discord.Embed(title=f"{get_emoji('info')} Итоги", color=EMBED_COLOR)
    embed.add_field(name="💬 Сообщения — 7 дней", value=fmt(top_stats(message_stats_col, d7, CONFIG.get("counting_channel_id"))), inline=True)
    embed.add_field(name="💬 Сообщения — 30 дней", value=fmt(top_stats(message_stats_col, d30, CONFIG.get("counting_channel_id"))), inline=True)
    embed.add_field(name="🚀 Bump — 7 дней", value=fmt(top_stats(bump_stats_col, d7, CONFIG.get("bump_channel_id"))), inline=True)
    embed.add_field(name="🚀 Bump — 30 дней", value=fmt(top_stats(bump_stats_col, d30, CONFIG.get("bump_channel_id"))), inline=True)
    count_channel = CONFIG.get("counting_channel_id")
    bump_channel = CONFIG.get("bump_channel_id")
    embed.add_field(name="Канал считалки", value=f"<#{count_channel}>" if count_channel else "Не установлен", inline=True)
    embed.add_field(name="Канал bump", value=f"<#{bump_channel}>" if bump_channel else "Не установлен", inline=True)
    embed.set_footer(text=FOOTER_TEXT)
    return embed

@bot.event
async def on_message(message: discord.Message):
    record_message_stat(message)
    await bot.process_commands(message)

@bot.event
async def on_interaction(interaction: discord.Interaction):
    record_bump_stat(interaction)

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
            description=f"{get_emoji('error')} У этого модератора нет ни одного тикета.",
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
            await send_error_interaction(interaction, "Вы не можете переключать страницы в чужом меню.", "Доступ запрещён")
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
    layout_inline = CONFIG.get("leaderboard_layout", "horizontal") == "horizontal"

    embed.add_field(name="<:ticket:1522343287816716379> Тикетов (7 дн.)", value=format_top(get_top(tickets_col, "staff_id", d7)), inline=layout_inline)
    embed.add_field(name="<:logs:1522340749998428160> Транскриптов (7 дн.)", value=format_top(get_top(tickets_col, "author_id", d7, True), "транскриптов"), inline=layout_inline)
    embed.add_field(name="🗑️ Удалено тикетов (7 дн.)", value=format_top(get_top(deleted_tickets_col, "staff_id", d7), "удалений"), inline=layout_inline)

    embed.add_field(name="<:ticket:1522343287816716379> Тикетов (30 дн.)", value=format_top(get_top(tickets_col, "staff_id", d30)), inline=layout_inline)
    embed.add_field(name="<:logs:1522340749998428160> Транскриптов (30 дн.)", value=format_top(get_top(tickets_col, "author_id", d30, True), "транскриптов"), inline=layout_inline)
    embed.add_field(name="🗑️ Удалено тикетов (30 дн.)", value=format_top(get_top(deleted_tickets_col, "staff_id", d30), "удалений"), inline=layout_inline)

    embed.add_field(name="<:ticket:1522343287816716379> Тикетов (Все время)", value=format_top(get_top(tickets_col, "staff_id")), inline=layout_inline)
    embed.add_field(name="<:logs:1522340749998428160> Транскриптов (Все время)", value=format_top(get_top(tickets_col, "author_id", exclude_zero=True), "транскриптов"), inline=layout_inline)
    embed.add_field(name="🗑️ Удалено тикетов (Все время)", value=format_top(get_top(deleted_tickets_col, "staff_id"), "удалений"), inline=layout_inline)

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
    embed = discord.Embed(
        title="<:staff:1522338131339251823> Меню команд бота",
        description="Выберите категорию команд ниже.",
        color=EMBED_COLOR,
    )
    embed.add_field(name="🎫 Тикеты", value="Команды для работы с тикетами и транскриптами.", inline=False)
    embed.add_field(name="🏆 Итоги", value="Статистика сообщений в считалке и использования slash-команд в bump-канале.", inline=False)
    if is_owner_user(user):
        embed.add_field(name="⚙️ Конфиг", value="Настройки бота, прав, логов, каналов и оформления.", inline=False)
    embed.set_footer(text=f"Ваша текущая группа: {get_user_group_name(user, channel_id)} • {FOOTER_TEXT}")
    return embed

def get_help_embed(user: discord.Member | discord.User, channel_id: int):
    embed = discord.Embed(
        title="<:ticket:1522343287816716379> Категория: Тикеты",
        description="Используй префикс `.` или слэш-команды `/`.",
        color=EMBED_COLOR,
    )
    commands_help = {
        "help": "`.help` / `/help` — меню команд.",
        "ticketstats": "`.ticketstats [ID / упоминание]` — статистика тикетов.",
        "leaderboard": "`.leaderboard` — лидерборд тикетов, транскриптов и удалений.",
        "addticket": "`.addticket [ID] [ссылка] [категория]` — добавить тикет.",
        "ticketlogs": "`.ticketlogs [ID / упоминание]` — логи тикетов.",
        "deleteticket": "`.deleteticket [номер] [ссылка]` — записать удаление тикета.",
        "deletelog": "`.deletelog [ID лога]` — удалить конкретный лог.",
        "resetlogs": "`.resetlogs [ID / упоминание]` — сбросить логи пользователя.",
        "count": "`.подсчет` / `/count` — показать итоги.",
    }
    for group_key, group in CONFIG.get("permission_groups", {}).items():
        visible = [commands_help[c] for c in group.get("commands", []) if c in commands_help and user_in_group(user, group_key)]
        if visible:
            embed.add_field(
                name=f"{group.get('emoji', '•')} {group.get('name', group_key)}",
                value="\n\n".join(visible),
                inline=False,
            )
    if is_owner_user(user):
        embed.add_field(name="⚙️ Владелец", value="`.config` — открыть настройки бота.", inline=False)
    embed.set_footer(text=f"Ваша текущая группа: {get_user_group_name(user, channel_id)} • {FOOTER_TEXT}")
    return embed

def get_results_usage_embed():
    embed = discord.Embed(title="Команда: подсчет", description="Показать статистику считалки и bump за 7 и 30 дней.", color=EMBED_COLOR)
    embed.add_field(name="Использование", value="`.подсчет` / `.count` / `/count`", inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed

def get_addticket_usage_embed():
    categories_str = ", ".join([f"`{c}`" for c in VALID_CATEGORIES])
    embed = discord.Embed(color=EMBED_COLOR, title="Команда: addticket", description="Добавить новый лог об обработанном тикете")
    embed.add_field(name="Кулдаун:", value="3 секунды (Для Администрации отсутствует)", inline=False)
    embed.add_field(name="Правила аргументов:", value=("1. Ссылка должна содержать: `https://discord.com/`\n2. Вы **не можете** указать свой собственный ID\n3. Один и тот же транскрипт нельзя вносить дважды\n4. Допустимые категории: " + categories_str), inline=False)
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
    embed.add_field(name="Правила аргументов:", value=("1. Номер лога — номер из `.addticket`\n2. Ссылка должна содержать: `https://discord.com/`\n3. Один и тот же транскрипт удаления нельзя внести дважды\n4. Ответственному модератору добавляется +1 удалённый тикет"), inline=False)
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

# ================= МЕНЮ ПОМОЩИ =================

class HelpBaseView(discord.ui.View):
    def __init__(self, author_id: int, channel_id: int):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.channel_id = channel_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await send_error_interaction(interaction, "Это меню вызвали не вы.", "Доступ запрещён")
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True

class TicketHelpView(HelpBaseView):
    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=get_help_categories_embed(interaction.user, self.channel_id), view=HelpCategoriesView(self.author_id, self.channel_id))

class ResultsHelpView(HelpBaseView):
    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=get_help_categories_embed(interaction.user, self.channel_id), view=HelpCategoriesView(self.author_id, self.channel_id))

class HelpCategoriesView(HelpBaseView):
    @discord.ui.button(label="Тикеты", emoji="🎫", style=discord.ButtonStyle.primary)
    async def tickets_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=get_help_embed(interaction.user, self.channel_id), view=TicketHelpView(self.author_id, self.channel_id))

    @discord.ui.button(label="Итоги", emoji="🏆", style=discord.ButtonStyle.primary)
    async def results_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=process_results(), view=ResultsHelpView(self.author_id, self.channel_id))

    @discord.ui.button(label="Конфиг", emoji="⚙️", style=discord.ButtonStyle.secondary)
    async def config_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner_user(interaction.user):
            await send_error_interaction(interaction, "Эта категория доступна только владельцу бота.", "Доступ запрещён")
            return
        await interaction.response.edit_message(embed=get_config_home_embed(), view=ConfigMainView(self.author_id))

# ================= МЕНЮ НАСТРОЕК БОТА (.config) =================

CONFIG_SECTIONS = {
    "general": ("⚙️", "General", "Цвет и footer"),
    "tickets": ("🎫", "Тикеты", "Канал логов и логируемые команды"),
    "permissions": ("🛡️", "Доступ", "Группы, роли и команды"),
    "leaderboard": ("🏆", "Лидерборды", "Горизонтально или вертикально"),
    "results": ("📊", "Итоги", "Каналы считалки и bump"),
    "embeds": ("🎨", "Эмбеды", "Статусные эмодзи"),
}

def get_config_home_embed():
    embed = discord.Embed(title="⚙️ Настройки бота", description="Выберите категорию настроек ниже.", color=EMBED_COLOR)
    for key, (emoji, name, desc) in CONFIG_SECTIONS.items():
        embed.add_field(name=f"{emoji} {name}", value=desc, inline=True)
    embed.add_field(name="🖼️ Header-изображения", value="Ссылки задаются только в переменной `HEADER_IMAGES` в коде.", inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed

def get_config_section_embed(section: str):
    emoji, name, desc = CONFIG_SECTIONS[section]
    embed = discord.Embed(title=f"{emoji} Конфиг → {name}", description=desc, color=EMBED_COLOR)
    if section == "general":
        embed.add_field(name="Цвет embed", value=f"`#{CONFIG['embed_color']:06X}`", inline=True)
        embed.add_field(name="Footer", value=CONFIG["footer_text"], inline=True)
    elif section == "tickets":
        ch = CONFIG.get("log_channel_id")
        enabled = [cmd for cmd, state in CONFIG.get("log_toggles", {}).items() if state]
        embed.add_field(name="Канал логов", value=f"<#{ch}>" if ch else "Не установлен", inline=False)
        embed.add_field(name="Логируемые команды", value="\n".join(f"`{x}`" for x in enabled) or "—", inline=False)
    elif section == "permissions":
        for key, group in CONFIG.get("permission_groups", {}).items():
            roles = ", ".join(f"<@&{r}>" for r in group.get("roles", [])) or "—"
            commands = ", ".join(f"`{c}`" for c in group.get("commands", [])) or "—"
            embed.add_field(name=f"{group.get('emoji','')} {group.get('name', key)}", value=f"**Роли:** {roles}\n**Команды:** {commands}", inline=False)
    elif section == "leaderboard":
        embed.add_field(name="Расположение категорий", value="Горизонтально" if CONFIG.get("leaderboard_layout") == "horizontal" else "Вертикально", inline=False)
    elif section == "results":
        count = CONFIG.get("counting_channel_id")
        bump = CONFIG.get("bump_channel_id")
        embed.add_field(name="Канал считалки", value=f"<#{count}>" if count else "Не установлен", inline=True)
        embed.add_field(name="Канал bump", value=f"<#{bump}>" if bump else "Не установлен", inline=True)
    elif section == "embeds":
        embed.add_field(name="Статусные эмодзи", value="\n".join(f"{get_emoji(k)} `{k}`" for k in EMOJI_DEFAULTS), inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed

class ConfigBaseView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=180)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await send_error_interaction(interaction, "Эта команда доступна только владельцу бота.", "Доступ запрещён")
            return False
        return True

class ConfigMainView(ConfigBaseView):
    @discord.ui.select(
        placeholder="Выберите категорию настроек...",
        options=[discord.SelectOption(label=name, value=key, emoji=emoji, description=desc) for key, (emoji, name, desc) in CONFIG_SECTIONS.items()],
    )
    async def section_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        section = select.values[0]
        views = {
            "general": GeneralConfigView,
            "tickets": TicketConfigView,
            "permissions": PermissionConfigView,
            "leaderboard": LeaderboardConfigView,
            "results": ResultsConfigView,
            "embeds": EmbedConfigView,
        }
        await interaction.response.edit_message(embed=get_config_section_embed(section), view=views[section](self.owner_id))

class GeneralConfigView(ConfigBaseView):
    @discord.ui.button(label="Цвет embed", emoji="🎨", style=discord.ButtonStyle.primary)
    async def color_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ColorModal())

    @discord.ui.button(label="Footer", emoji="📝", style=discord.ButtonStyle.primary)
    async def footer_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FooterModal())

    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=get_config_home_embed(), view=ConfigMainView(self.owner_id))

class ColorModal(discord.ui.Modal, title="Изменить цвет embed"):
    color_input = discord.ui.TextInput(label="HEX цвет без #", placeholder="212121", min_length=6, max_length=6)
    async def on_submit(self, interaction: discord.Interaction):
        raw = self.color_input.value.strip().lstrip("#")
        try:
            value = int(raw, 16)
            if not 0 <= value <= 0xFFFFFF:
                raise ValueError
        except ValueError:
            await send_error_interaction(interaction, f"Некорректный HEX цвет: `{raw}`.")
            return
        update_config({"embed_color": value})
        await interaction.response.send_message(embed=make_status_embed("Настройка изменена", f"Цвет embed изменён на `#{raw.upper()}`.", "success"), ephemeral=True)

class FooterModal(discord.ui.Modal, title="Изменить footer текст"):
    footer_input = discord.ui.TextInput(label="Новый текст footer", placeholder="ТУСОВКА ДОРИСТА", max_length=100)
    async def on_submit(self, interaction: discord.Interaction):
        update_config({"footer_text": self.footer_input.value})
        await interaction.response.send_message(embed=make_status_embed("Настройка изменена", f"Footer изменён на `{self.footer_input.value}`.", "success"), ephemeral=True)

class LogChannelView(ConfigBaseView):
    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="Выберите канал для логов...", channel_types=[discord.ChannelType.text])
    async def channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        channel = select.values[0]
        update_config({"log_channel_id": channel.id})
        await interaction.response.edit_message(embed=get_config_section_embed("tickets"), view=TicketConfigView(self.owner_id))

class LogTogglesSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=cmd, value=cmd, default=CONFIG.get("log_toggles", {}).get(cmd, default)) for cmd, default in LOGGABLE_COMMANDS_DEFAULT.items()]
        super().__init__(placeholder="Выберите команды для логирования...", min_values=0, max_values=len(options), options=options)
    async def callback(self, interaction: discord.Interaction):
        enabled = set(self.values)
        update_config({"log_toggles": {cmd: cmd in enabled for cmd in LOGGABLE_COMMANDS_DEFAULT}})
        await interaction.response.edit_message(embed=get_config_section_embed("tickets"), view=TicketConfigView(self.view.owner_id))

class LogTogglesView(ConfigBaseView):
    def __init__(self, owner_id: int):
        super().__init__(owner_id)
        self.add_item(LogTogglesSelect())

class TicketConfigView(ConfigBaseView):
    @discord.ui.button(label="Канал логов", emoji="📨", style=discord.ButtonStyle.primary)
    async def log_channel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=get_config_section_embed("tickets"), view=LogChannelView(self.owner_id))
    @discord.ui.button(label="Команды для логов", emoji="🧾", style=discord.ButtonStyle.primary)
    async def log_toggles_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=get_config_section_embed("tickets"), view=LogTogglesView(self.owner_id))
    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=get_config_home_embed(), view=ConfigMainView(self.owner_id))

class PermissionGroupSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=g.get("name", k), value=k, emoji=g.get("emoji", "🛡️")) for k, g in CONFIG.get("permission_groups", {}).items()]
        super().__init__(placeholder="Выберите группу...", options=options)
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=get_config_section_embed("permissions"), view=PermissionEditorView(self.view.owner_id, self.values[0]))

class PermissionConfigView(ConfigBaseView):
    def __init__(self, owner_id: int):
        super().__init__(owner_id)
        self.add_item(PermissionGroupSelect())
        back = discord.ui.Button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary)
        back.callback = self.back_callback
        self.add_item(back)
    async def back_callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=get_config_home_embed(), view=ConfigMainView(self.owner_id))

class PermissionRoleSelect(discord.ui.RoleSelect):
    def __init__(self, group_key: str):
        self.group_key = group_key
        super().__init__(placeholder="Выберите роли группы (заменяет список)...", min_values=0, max_values=25)
    async def callback(self, interaction: discord.Interaction):
        update_config({f"permission_groups.{self.group_key}.roles": [r.id for r in self.values]})
        await interaction.response.edit_message(embed=get_config_section_embed("permissions"), view=PermissionEditorView(self.view.owner_id, self.group_key))

class PermissionCommandSelect(discord.ui.Select):
    def __init__(self, group_key: str):
        self.group_key = group_key
        current = set(get_group(group_key).get("commands", []))
        options = [discord.SelectOption(label=label, value=cmd, default=cmd in current) for cmd, label in COMMAND_LABELS.items()]
        super().__init__(placeholder="Выберите команды группы...", min_values=0, max_values=len(options), options=options)
    async def callback(self, interaction: discord.Interaction):
        update_config({f"permission_groups.{self.group_key}.commands": list(self.values)})
        await interaction.response.edit_message(embed=get_config_section_embed("permissions"), view=PermissionEditorView(self.view.owner_id, self.group_key))

class PermissionEditorView(ConfigBaseView):
    def __init__(self, owner_id: int, group_key: str):
        super().__init__(owner_id)
        self.group_key = group_key
        self.add_item(PermissionRoleSelect(group_key))
        self.add_item(PermissionCommandSelect(group_key))
        back = discord.ui.Button(label="Назад к группам", emoji="◀️", style=discord.ButtonStyle.secondary, row=2)
        back.callback = self.back_callback
        self.add_item(back)
    async def back_callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=get_config_section_embed("permissions"), view=PermissionConfigView(self.owner_id))

class LeaderboardConfigView(ConfigBaseView):
    @discord.ui.select(placeholder="Выберите расположение...", options=[
        discord.SelectOption(label="Горизонтально", value="horizontal", emoji="↔️"),
        discord.SelectOption(label="Вертикально", value="vertical", emoji="↕️"),
    ])
    async def layout_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        update_config({"leaderboard_layout": select.values[0]})
        await interaction.response.edit_message(embed=get_config_section_embed("leaderboard"), view=self)
    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=get_config_home_embed(), view=ConfigMainView(self.owner_id))

class ResultChannelView(ConfigBaseView):
    def __init__(self, owner_id: int, setting_key: str):
        super().__init__(owner_id)
        self.setting_key = setting_key
    @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder="Выберите текстовый канал...", channel_types=[discord.ChannelType.text])
    async def channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        update_config({self.setting_key: select.values[0].id})
        await interaction.response.edit_message(embed=get_config_section_embed("results"), view=ResultsConfigView(self.owner_id))

class ResultsConfigView(ConfigBaseView):
    @discord.ui.button(label="Канал считалки", emoji="💬", style=discord.ButtonStyle.primary)
    async def count_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=get_config_section_embed("results"), view=ResultChannelView(self.owner_id, "counting_channel_id"))
    @discord.ui.button(label="Канал bump", emoji="🚀", style=discord.ButtonStyle.primary)
    async def bump_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=get_config_section_embed("results"), view=ResultChannelView(self.owner_id, "bump_channel_id"))
    @discord.ui.button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=get_config_home_embed(), view=ConfigMainView(self.owner_id))

class EmojiModal(discord.ui.Modal, title="Изменить эмодзи"):
    emoji_input = discord.ui.TextInput(label="Эмодзи", placeholder="Вставьте эмодзи или текст", max_length=100)
    def __init__(self, emoji_key: str):
        super().__init__()
        self.emoji_key = emoji_key
        self.emoji_input.default = get_emoji(emoji_key)
    async def on_submit(self, interaction: discord.Interaction):
        emojis = dict(CONFIG.get("emojis", {}))
        emojis[self.emoji_key] = self.emoji_input.value.strip()
        update_config({"emojis": emojis})
        await interaction.response.send_message(embed=make_status_embed("Настройка изменена", f"Эмодзи `{self.emoji_key}` обновлён.", "success"), ephemeral=True)

class EmojiSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=k, value=k, description=f"Текущее: {get_emoji(k)[:90]}") for k in EMOJI_DEFAULTS]
        super().__init__(placeholder="Выберите статусный эмодзи...", options=options)
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(EmojiModal(self.values[0]))

class EmbedConfigView(ConfigBaseView):
    def __init__(self, owner_id: int):
        super().__init__(owner_id)
        self.add_item(EmojiSelect())
        back = discord.ui.Button(label="Назад", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
        back.callback = self.back_callback
        self.add_item(back)
    async def back_callback(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=get_config_home_embed(), view=ConfigMainView(self.owner_id))

# ================= СЛЭШ КОМАНДЫ =================

@bot.tree.command(name="help", description="Показать полный список команд бота")
@check_support_slash("help")
async def slash_help(interaction: discord.Interaction):
    embed = get_help_categories_embed(interaction.user, interaction.channel_id)
    view = HelpCategoriesView(interaction.user.id, interaction.channel_id)
    await send_embed_with_header(interaction, embed, "help", view=view)
    await send_log("help", build_log_embed("📖 Использовано меню помощи", [f"**Кто:** {interaction.user.mention}"]))

@bot.tree.command(name="addticket", description="Записать новый обработанный тикет")
@app_commands.describe(staff="ID участника персонала", transcript="Ссылка на транскрипт", category="Категория тикета")
@app_commands.choices(category=[app_commands.Choice(name=cat, value=cat) for cat in VALID_CATEGORIES])
@check_transcript_slash("addticket")
@app_commands.checks.cooldown(1, 3.0, key=lambda i: i.user.id)
async def slash_add_ticket(interaction: discord.Interaction, staff: str, transcript: str, category: str):
    if not is_valid_addticket(transcript, category):
        await interaction.response.send_message(embed=get_addticket_usage_embed(), ephemeral=True)
        return

    try:
        staff_id = int(staff.strip("<@!>"))
    except ValueError:
        await send_error_interaction(interaction, f"Некорректный ID: `{staff}`.")
        return

    if staff_id == interaction.user.id:
        await send_error_interaction(interaction, "Вы не можете занести тикет, который провели сами!")
        return

    if is_transcript_exists(transcript):
        await send_error_interaction(interaction, "Этот транскрипт уже внесён.")
        return

    try:
        staff_user = await bot.fetch_user(staff_id)
    except (discord.NotFound, discord.HTTPException):
        await send_error_interaction(interaction, f"Не удалось найти пользователя по ID `{staff}`.")
        return

    embed, ticket_id = process_add_ticket(interaction.user, staff_user, transcript, category)
    await send_embed_with_header(interaction, embed, "tickets")
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
        await send_error_interaction(interaction, f"Подождите ещё {error.retry_after:.1f} сек.", "Команда на кулдауне")
        return

@bot.tree.command(name="deleteticket", description="Записать удаление тикета (канала) в базу данных")
@app_commands.describe(log_id="Номер лога тикета (из .addticket)", transcript="Ссылка на транскрипт удаления")
@check_transcript_slash("deleteticket")
@app_commands.checks.cooldown(1, 3.0, key=lambda i: i.user.id)
async def slash_delete_ticket_channel(interaction: discord.Interaction, log_id: int, transcript: str):
    if not is_valid_transcript_link(transcript):
        await interaction.response.send_message(embed=get_deleteticket_usage_embed(), ephemeral=True)
        return

    embed, error_code = process_delete_ticket_channel(interaction.user, log_id, transcript)
    if error_code == "not_found":
        await send_error_interaction(interaction, f"Тикет с номером лога `{log_id}` не найден.")
        return
    if error_code == "duplicate":
        await send_error_interaction(interaction, "Этот транскрипт удаления уже внесён.")
        return

    await send_embed_with_header(interaction, embed, "tickets")
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
        await send_error_interaction(interaction, f"Подождите ещё {error.retry_after:.1f} сек.", "Команда на кулдауне")
        return

@bot.tree.command(name="ticketstats", description="Посмотреть статистику тикетов")
@app_commands.describe(staff="Участник персонала")
@check_support_slash("ticketstats")
async def slash_ticket_stats(interaction: discord.Interaction, staff: discord.User = None):
    target = staff or interaction.user
    embed = process_ticket_stats(target)
    await send_embed_with_header(interaction, embed, "tickets")
    await send_log("ticketstats", build_log_embed("📊 Просмотр статистики", [
        f"**Кто смотрел:** {interaction.user.mention}",
        f"**Чья статистика:** <@{target.id}>",
    ]))

@bot.tree.command(name="ticketlogs", description="Посмотреть тикеты пользователя")
@app_commands.describe(staff="Участник персонала")
@check_transcript_slash("ticketlogs")
async def slash_ticket_logs(interaction: discord.Interaction, staff: discord.User = None):
    target = staff or interaction.user
    embed, total_pages = process_ticket_logs(target, 1)

    if total_pages > 1:
        view = TicketLogsPaginationView(interaction.user.id, target, total_pages, 1)
        view.message = await send_embed_with_header(interaction, embed, "tickets", view=view)
    else:
        await send_embed_with_header(interaction, embed, "tickets")
    await send_log("ticketlogs", build_log_embed("📖 Просмотр логов тикетов", [
        f"**Кто смотрел:** {interaction.user.mention}",
        f"**Чьи логи:** <@{target.id}>",
    ]))

@bot.tree.command(name="leaderboard", description="Посмотреть топ модераторов по тикетам и транскриптам")
@check_support_slash("leaderboard")
async def slash_leaderboard(interaction: discord.Interaction):
    embed = process_leaderboard()
    await send_embed_with_header(interaction, embed, "leaderboard")
    await send_log("leaderboard", build_log_embed("🏆 Просмотр лидерборда", [f"**Кто:** {interaction.user.mention}"]))

@bot.tree.command(name="deletelog", description="Удалить лог тикета по номеру (Только для Админов)")
@app_commands.describe(log_id="Номер лога")
@check_admin_slash("deletelog")
async def slash_delete_log(interaction: discord.Interaction, log_id: int):
    ticket = delete_ticket_log(log_id)
    if not ticket:
        await send_error_interaction(interaction, f"Лог с номером `{log_id}` не найден.")
        return
    await interaction.response.send_message(embed=make_status_embed("Лог удалён", f"Лог `{log_id}` удалён.", "success"))
    await send_log("deletelog", build_log_embed("❌ Удалён лог тикета", [
        f"**Номер лога:** №{log_id}",
        f"**Кто удалил:** {interaction.user.mention}",
    ]))

@bot.tree.command(name="resetlogs", description="Удалить все логи пользователя (Только для Админов)")
@app_commands.describe(staff="Модератор")
@check_admin_slash("resetlogs")
async def slash_reset_logs(interaction: discord.Interaction, staff: discord.User):
    count = reset_tickets(staff.id)
    await interaction.response.send_message(embed=make_status_embed("Логи сброшены", f"Удалено логов модератора **{staff.name}**: `{count}`.", "success"))
    await send_log("resetlogs", build_log_embed("♻️ Сброшены логи пользователя", [
        f"**Пользователь:** <@{staff.id}>",
        f"**Кто сбросил:** {interaction.user.mention}",
        f"**Удалено записей:** {count}",
    ]))

@bot.tree.command(name="config", description="Настройки бота (только для владельца)")
async def slash_config(interaction: discord.Interaction):
    if not is_owner_user(interaction.user):
        await send_error_interaction(interaction, "Эта команда доступна только владельцу бота.", "Доступ запрещён")
        return
    embed = get_config_home_embed()
    view = ConfigMainView(interaction.user.id)
    await send_embed_with_header(interaction, embed, "config", view=view, ephemeral=True)
    await send_log("config", build_log_embed("⚙️ Открыто меню настроек", [f"**Кто:** {interaction.user.mention}"]))

# ================= ИТОГИ / СЧИТАЛКА =================

@bot.tree.command(name="count", description="Показать итоги считалки и bump")
@check_support_slash("count")
async def slash_count(interaction: discord.Interaction):
    embed = process_results()
    await send_embed_with_header(interaction, embed, "results")
    await send_log("count", build_log_embed("📊 Просмотр итогов", [f"**Кто:** {interaction.user.mention}"]))

# ================= ПРЕФИКСНЫЕ КОМАНДЫ =================

@bot.command(name="help")
@check_support_prefix("help")
async def prefix_help(ctx: commands.Context):
    embed = get_help_categories_embed(ctx.author, ctx.channel.id)
    view = HelpCategoriesView(ctx.author.id, ctx.channel.id)
    await send_embed_with_header(ctx, embed, "help", view=view)
    await send_log("help", build_log_embed("📖 Использовано меню помощи", [f"**Кто:** {ctx.author.mention}"]))

@bot.command(name="addticket", aliases=["t", "ticket"])
@check_transcript_prefix("addticket")
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
        await send_error_ctx(ctx, "Укажите корректный numeric ID модератора.")
        return

    if staff_id == ctx.author.id:
        await send_error_ctx(ctx, "Вы не можете занести тикет, который провели сами!")
        return

    if is_transcript_exists(transcript):
        await send_error_ctx(ctx, "Этот транскрипт уже внесён.")
        return

    try:
        staff_user = await bot.fetch_user(staff_id)
    except (discord.NotFound, discord.HTTPException):
        await send_error_ctx(ctx, f"Не удалось найти пользователя по ID `{staff_raw}`.")
        return

    embed, ticket_id = process_add_ticket(ctx.author, staff_user, transcript, category)
    await send_embed_with_header(ctx, embed, "tickets")
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
        await send_error_ctx(ctx, f"Подождите ещё {error.retry_after:.1f} сек.", "Команда на кулдауне")
        return
    if isinstance(error, commands.CheckFailure):
        await send_error_ctx(ctx, str(error) or "Недостаточно прав.", "Недостаточно прав")
        return
    await ctx.send(embed=get_addticket_usage_embed())

@bot.command(name="deleteticket", aliases=["dt"])
@check_transcript_prefix("deleteticket")
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
        await send_error_ctx(ctx, f"Номер лога должен быть числом: `{log_id_raw}`.")
        return

    if not is_valid_transcript_link(transcript):
        await ctx.send(embed=get_deleteticket_usage_embed())
        return

    embed, error_code = process_delete_ticket_channel(ctx.author, log_id, transcript)
    if error_code == "not_found":
        await send_error_ctx(ctx, f"Тикет с номером лога `{log_id}` не найден.")
        return
    if error_code == "duplicate":
        await send_error_ctx(ctx, "Этот транскрипт удаления уже внесён.")
        return

    await send_embed_with_header(ctx, embed, "tickets")
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
        await send_error_ctx(ctx, f"Подождите ещё {error.retry_after:.1f} сек.", "Команда на кулдауне")
        return
    if isinstance(error, commands.CheckFailure):
        await send_error_ctx(ctx, str(error) or "Недостаточно прав.", "Недостаточно прав")
        return
    await ctx.send(embed=get_deleteticket_usage_embed())

@bot.command(name="ticketstats", aliases=["ts"])
@check_support_prefix("ticketstats")
async def prefix_ticket_stats(ctx: commands.Context, staff: discord.User = None):
    target = staff or ctx.author
    embed = process_ticket_stats(target)
    await send_embed_with_header(ctx, embed, "tickets")
    await send_log("ticketstats", build_log_embed("📊 Просмотр статистики", [
        f"**Кто смотрел:** {ctx.author.mention}",
        f"**Чья статистика:** <@{target.id}>",
    ]))

@prefix_ticket_stats.error
async def prefix_ticket_stats_error(ctx: commands.Context, error):
    if isinstance(error, commands.BadArgument):
        await ctx.send(embed=get_ticketstats_usage_embed())

@bot.command(name="ticketlogs", aliases=["tl", "tlogs"])
@check_transcript_prefix("ticketlogs")
async def prefix_ticket_logs(ctx: commands.Context, staff: discord.User = None):
    target = staff or ctx.author
    embed, total_pages = process_ticket_logs(target, 1)

    if total_pages > 1:
        view = TicketLogsPaginationView(ctx.author.id, target, total_pages, 1)
        msg = await send_embed_with_header(ctx, embed, "tickets", view=view)
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
@check_support_prefix("leaderboard")
async def prefix_leaderboard(ctx: commands.Context):
    embed = process_leaderboard()
    await send_embed_with_header(ctx, embed, "leaderboard")
    await send_log("leaderboard", build_log_embed("🏆 Просмотр лидерборда", [f"**Кто:** {ctx.author.mention}"]))

@bot.command(name="deletelog", aliases=["del"])
@check_admin_prefix("deletelog")
async def prefix_delete_log(ctx: commands.Context, log_id: int = None):
    if log_id is None:
        await ctx.send(embed=get_deletelog_usage_embed())
        return
    ticket = delete_ticket_log(log_id)
    if not ticket:
        await send_error_ctx(ctx, f"Лог под номером `{log_id}` не найден.")
        return
    await ctx.send(embed=make_status_embed("Лог удалён", f"Лог `{log_id}` удалён.", "success"))
    await send_log("deletelog", build_log_embed("❌ Удалён лог тикета", [
        f"**Номер лога:** №{log_id}",
        f"**Кто удалил:** {ctx.author.mention}",
    ]))

@prefix_delete_log.error
async def prefix_delete_log_error(ctx: commands.Context, error):
    if isinstance(error, commands.BadArgument):
        await ctx.send(embed=get_deletelog_usage_embed())

@bot.command(name="resetlogs", aliases=["rt"])
@check_admin_prefix("resetlogs")
async def prefix_reset_logs(ctx: commands.Context, staff: discord.User = None):
    if staff is None:
        await ctx.send(embed=get_resetlogs_usage_embed())
        return
    count = reset_tickets(staff.id)
    await ctx.send(embed=make_status_embed("Логи сброшены", f"Удалено логов модератора **{staff.name}**: `{count}`.", "success"))
    await send_log("resetlogs", build_log_embed("♻️ Сброшены логи пользователя", [
        f"**Пользователь:** <@{staff.id}>",
        f"**Кто сбросил:** {ctx.author.mention}",
        f"**Удалено записей:** {count}",
    ]))

@prefix_reset_logs.error
async def prefix_reset_logs_error(ctx: commands.Context, error):
    if isinstance(error, commands.BadArgument):
        await ctx.send(embed=get_resetlogs_usage_embed())

@bot.command(name="подсчет", aliases=["count", "stats"])
@check_support_prefix("count")
async def prefix_count(ctx: commands.Context):
    embed = process_results()
    await send_embed_with_header(ctx, embed, "results")
    await send_log("count", build_log_embed("📊 Просмотр итогов", [f"**Кто:** {ctx.author.mention}"]))


@bot.command(name="config")
async def prefix_config(ctx: commands.Context):
    if not is_owner_user(ctx.author):
        await send_error_ctx(ctx, "Эта команда доступна только владельцу бота.", "Доступ запрещён")
        return
    embed = get_config_home_embed()
    view = ConfigMainView(ctx.author.id)
    await send_embed_with_header(ctx, embed, "config", view=view)
    await send_log("config", build_log_embed("⚙️ Открыто меню настроек", [f"**Кто:** {ctx.author.mention}"]))

# ================= ЗАПУСК БОТА =================

TOKEN = os.getenv("DISCORD_TOKEN")
if TOKEN:
    bot.run(TOKEN)
else:
    print("Ошибка: DISCORD_TOKEN не найден в файле .env")

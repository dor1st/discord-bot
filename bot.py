import os
import math
import asyncio
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import motor.motor_asyncio
from pymongo import ReturnDocument, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
MONGO_URL = os.getenv("MONGO_URL")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN not found in environment variables.")

if not MONGO_URL:
    raise RuntimeError("MONGO_URL not found. Please add MONGO_URL to your environment variables.")

# ВАЖНО: используем motor (async-клиент), а не pymongo.MongoClient.
# Синхронный pymongo внутри discord.py блокирует весь event loop бота на
# время каждого запроса к базе — именно это и было главной причиной
# зависаний/задержек и "бот не отвечает".
cluster = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URL)
db = cluster["dorysta_bot"]
tickets_col = db["tickets"]
counters_col = db["counters"]

_indexes_ready = False


async def ensure_indexes():
    global _indexes_ready
    if _indexes_ready:
        return
    await asyncio.gather(
        tickets_col.create_index("ticket_id", unique=True),
        tickets_col.create_index("transcript_url", unique=True),
        tickets_col.create_index([("staff_id", ASCENDING), ("created_at", DESCENDING)]),
        tickets_col.create_index([("author_id", ASCENDING), ("created_at", DESCENDING)]),
        tickets_col.create_index("staff_id"),
        tickets_col.create_index("author_id"),
        tickets_col.create_index("created_at"),
    )
    _indexes_ready = True


async def get_next_ticket_id() -> int:
    counter = await counters_col.find_one_and_update(
        {"_id": "ticket_id"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return counter["seq"]


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=".", intents=intents, help_command=None)

EMBED_COLOR = discord.Color(0x212121)
FOOTER_TEXT = "ТУСОВКА ДОРИСТА"

ALLOWED_CHANNEL_IDS = [1466886479396737024]
UPDATE_ID_CHANNEL = int(os.getenv("UPDATE_ID_CHANNEL", 1543654684462424194))

SUPPORT_ROLE_IDS = [1501507449860001853, 1322962344040464424]
TRANSCRIPT_ROLE_IDS = [1542601770461569044, 1323348388762226759]
ADMIN_ROLE_IDS = [1322962317885046844, 1502684875868737796]

VALID_CATEGORIES = [
    "Помощь по серверу",
    "Получение призов",
    "Получение ролей",
    "Покупка рекламы"
]

_user_cache: dict[int, tuple[discord.User, float]] = {}
USER_CACHE_TTL = 300  # 5 минут


def has_role_access(user: discord.Member | discord.User, role_ids: list[int]) -> bool:
    if not isinstance(user, discord.Member):
        return False
    if user.guild_permissions.administrator:
        return True
    user_role_ids = [role.id for role in user.roles]
    return any(r_id in user_role_ids for r_id in role_ids)


def is_admin_user(user: discord.Member | discord.User) -> bool:
    return has_role_access(user, ADMIN_ROLE_IDS)


def get_user_group_name(user: discord.Member | discord.User, channel_id: int) -> str:
    can_admin, _ = check_access(user, channel_id, ADMIN_ROLE_IDS, check_channels=False)
    if can_admin:
        return "Администрация"
    can_transcript, _ = check_access(user, channel_id, TRANSCRIPT_ROLE_IDS)
    if can_transcript:
        return "Transcript"
    can_support, _ = check_access(user, channel_id, SUPPORT_ROLE_IDS)
    if can_support:
        return "Support"
    return "Пользователь"


def check_access(
    user: discord.Member | discord.User,
    channel_id: int,
    role_ids: list[int],
    check_channels: bool = True,
) -> tuple[bool, str]:
    if not isinstance(user, discord.Member):
        return False, "Команды работают только на сервере."
    if user.guild_permissions.administrator or has_role_access(user, ADMIN_ROLE_IDS):
        return True, ""
    if not has_role_access(user, role_ids):
        return False, "У вас недостаточно ролей для использования этой команды."
    if check_channels and ALLOWED_CHANNEL_IDS and channel_id not in ALLOWED_CHANNEL_IDS:
        channels_mention = ", ".join([f"<#{cid}>" for cid in ALLOWED_CHANNEL_IDS])
        return False, f"Эта команда доступна только в каналах: {channels_mention}"
    return True, ""


def check_support_prefix():
    async def predicate(ctx: commands.Context):
        ok, msg = check_access(ctx.author, ctx.channel.id, SUPPORT_ROLE_IDS)
        if not ok:
            raise commands.CheckFailure(msg)
        return True
    return commands.check(predicate)


def check_transcript_prefix():
    async def predicate(ctx: commands.Context):
        ok, msg = check_access(ctx.author, ctx.channel.id, TRANSCRIPT_ROLE_IDS)
        if not ok:
            raise commands.CheckFailure(msg)
        return True
    return commands.check(predicate)


def check_admin_prefix():
    async def predicate(ctx: commands.Context):
        ok, msg = check_access(ctx.author, ctx.channel.id, ADMIN_ROLE_IDS, check_channels=False)
        if not ok:
            raise commands.CheckFailure(msg)
        return True
    return commands.check(predicate)


def check_support_slash():
    async def predicate(interaction: discord.Interaction):
        ok, msg = check_access(interaction.user, interaction.channel_id, SUPPORT_ROLE_IDS)
        if not ok:
            raise app_commands.AppCommandError(msg)
        return True
    return app_commands.check(predicate)


def check_transcript_slash():
    async def predicate(interaction: discord.Interaction):
        ok, msg = check_access(interaction.user, interaction.channel_id, TRANSCRIPT_ROLE_IDS)
        if not ok:
            raise app_commands.AppCommandError(msg)
        return True
    return app_commands.check(predicate)


def check_admin_slash():
    async def predicate(interaction: discord.Interaction):
        ok, msg = check_access(interaction.user, interaction.channel_id, ADMIN_ROLE_IDS, check_channels=False)
        if not ok:
            raise app_commands.AppCommandError(msg)
        return True
    return app_commands.check(predicate)


def validate_addticket_args(transcript_url: str, category: str) -> tuple[bool, str]:
    if "https://discord.com/" not in transcript_url:
        return False, f"Ссылка `{transcript_url}` некорректна. Ссылка должна содержать `https://discord.com/`"
    if category not in VALID_CATEGORIES:
        cats_formatted = ", ".join([f"`{c}`" for c in VALID_CATEGORIES])
        return False, f"Категория `{category}` не найдена.\nДопустимые категории: {cats_formatted}"
    return True, ""


async def is_transcript_exists(transcript_url: str) -> bool:
    return (await tickets_col.find_one({"transcript_url": transcript_url}, {"_id": 1})) is not None


async def get_user_fast(user_id: int) -> discord.User | None:
    now = datetime.utcnow().timestamp()
    cached = _user_cache.get(user_id)
    if cached and now - cached[1] < USER_CACHE_TTL:
        return cached[0]

    user = bot.get_user(user_id)
    if not user:
        try:
            user = await bot.fetch_user(user_id)
        except (discord.NotFound, discord.HTTPException):
            return None

    _user_cache[user_id] = (user, now)
    return user


_startup_notified = False


async def send_restart_embed():
    """Отправляет embed о перезапуске бота в канал UPDATE_ID_CHANNEL.
    on_ready может срабатывать не только при старте процесса, но и при
    каждом переподключении к Discord gateway (обрыв сети и т.п.) — флаг
    ниже гарантирует, что embed уйдёт один раз за время жизни процесса,
    а не при каждом таком reconnect."""
    global _startup_notified
    if _startup_notified:
        return

    channel = bot.get_channel(UPDATE_ID_CHANNEL)
    if channel is None:
        print(f"send_restart_embed: канал {UPDATE_ID_CHANNEL} не найден (бот не в гильдии или неверный ID).")
        return

    timestamp = int(datetime.utcnow().timestamp())
    embed = discord.Embed(
        title="<a:gif_verify:1522328481956888686> Бот запущен",
        description=(
            f"Бот успешно перезапущен и готов к работе.\n"
            f"**Время запуска:** <t:{timestamp}:F> (<t:{timestamp}:R>)"
        ),
        color=EMBED_COLOR,
    )
    embed.set_footer(text=FOOTER_TEXT)

    try:
        await channel.send(embed=embed)
        _startup_notified = True
    except discord.HTTPException as e:
        print(f"send_restart_embed: не удалось отправить embed: {e!r}")


@bot.event
async def on_ready():
    await ensure_indexes()
    await bot.tree.sync()
    print(f"Bot logged in as {bot.user} with Motor connected!")
    await send_restart_embed()


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
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.NotFound:
        print(f"on_app_command_error: interaction {interaction.id} уже недоступен (404), не смог отправить: {msg!r}")
    except discord.HTTPException as e:
        print(f"on_app_command_error: не удалось отправить сообщение об ошибке: {e!r}")


async def get_monthly_tickets(staff_id: int) -> int:
    date_30_days = datetime.utcnow() - timedelta(days=30)
    return await tickets_col.count_documents({
        "staff_id": staff_id,
        "created_at": {"$gte": date_30_days}
    })


async def process_add_ticket(author_user: discord.User, staff_user: discord.User, transcript_url: str, category: str):
    ticket_id = await get_next_ticket_id()
    now = datetime.utcnow()

    try:
        await tickets_col.insert_one({
            "ticket_id": ticket_id,
            "staff_id": staff_user.id,
            "author_id": author_user.id,
            "transcript_url": transcript_url,
            "category": category,
            "created_at": now
        })
    except DuplicateKeyError:
        raise ValueError("этот транскрипт уже внесен")

    monthly_count = await get_monthly_tickets(staff_user.id)
    timestamp = int(now.timestamp())
    discord_timestamp = f"<t:{timestamp}:F>"

    embed = discord.Embed(
        title=f"<:logs:1522340749998428160> Лог тикета — {staff_user.display_name}", color=EMBED_COLOR
    )
    embed.add_field(name="Дата транскрипта", value=discord_timestamp, inline=False)
    embed.add_field(name="Ссылка на транскрипт", value=transcript_url, inline=False)
    embed.add_field(name="Кто вёл тикет", value=str(staff_user.id), inline=False)
    embed.add_field(name="Внёс в базу", value=author_user.mention, inline=False)
    embed.add_field(name="Тикетов за последний месяц", value=str(monthly_count), inline=False)
    embed.add_field(name="Категория", value=category, inline=False)
    embed.set_footer(text=FOOTER_TEXT)

    return embed


async def process_ticket_logs(target_user: discord.User, page: int = 1):
    cursor = tickets_col.find({"staff_id": target_user.id}).sort("ticket_id", ASCENDING)
    logs = await cursor.to_list(length=None)

    if not logs:
        embed = discord.Embed(
            title=f"Тикеты — {target_user.name}",
            description="<:bruh:1521904409582375174> У этого модератора нет ни одного тикета.",
            color=EMBED_COLOR,
        )
        embed.set_footer(text=f"Страница 1/1 (0 логов) • {FOOTER_TEXT}")
        return embed

    items_per_page = 5
    total_logs = len(logs)
    total_pages = math.ceil(total_logs / items_per_page)
    current_page = max(1, min(page, total_pages))

    start_idx = (current_page - 1) * items_per_page
    end_idx = start_idx + items_per_page
    current_logs = logs[start_idx:end_idx]

    embed = discord.Embed(
        title=f"<:ticket:1522343287816716379> Тикеты — {target_user.name}", color=EMBED_COLOR
    )
    embed.description = f"{target_user.id}\n" + "—" * 28

    lines = []
    for doc in current_logs:
        log_id = doc.get("ticket_id", "—")
        transcript_url = doc.get("transcript_url", "")
        category = doc.get("category", "")
        created_at = doc.get("created_at")

        if isinstance(created_at, datetime):
            formatted_date = f"<t:{int(created_at.timestamp())}:F>"
        else:
            formatted_date = str(created_at)

        entry = (
            f"**Тикет №{log_id}**\n"
            f"**Модератор:** {target_user.name} ({target_user.mention})\n"
            f"**Категория:** {category}\n"
            f"**Транскрипт:** {transcript_url}\n"
            f"{formatted_date}"
        )
        lines.append(entry)

    embed.description += "\n\n" + "\n\n".join(lines)
    embed.set_footer(text=f"Страница {current_page}/{total_pages} ({total_logs} логов) • {FOOTER_TEXT}")

    return embed


async def process_ticket_stats(target_user: discord.User):
    now = datetime.utcnow()
    date_7_days = now - timedelta(days=7)
    date_30_days = now - timedelta(days=30)

    (
        count_7_staff,
        count_30_staff,
        count_all_staff,
        count_7_author,
        count_30_author,
        count_all_author,
        last_staff_doc,
        last_author_doc,
    ) = await asyncio.gather(
        tickets_col.count_documents({"staff_id": target_user.id, "created_at": {"$gte": date_7_days}}),
        tickets_col.count_documents({"staff_id": target_user.id, "created_at": {"$gte": date_30_days}}),
        tickets_col.count_documents({"staff_id": target_user.id}),
        tickets_col.count_documents({"author_id": target_user.id, "created_at": {"$gte": date_7_days}}),
        tickets_col.count_documents({"author_id": target_user.id, "created_at": {"$gte": date_30_days}}),
        tickets_col.count_documents({"author_id": target_user.id}),
        tickets_col.find_one({"staff_id": target_user.id}, sort=[("ticket_id", DESCENDING)]),
        tickets_col.find_one({"author_id": target_user.id}, sort=[("ticket_id", DESCENDING)]),
    )

    if last_staff_doc and isinstance(last_staff_doc.get("created_at"), datetime):
        last_staff_str = f"<t:{int(last_staff_doc['created_at'].timestamp())}:R>"
    else:
        last_staff_str = "—"

    if last_author_doc and isinstance(last_author_doc.get("created_at"), datetime):
        last_author_str = f"<t:{int(last_author_doc['created_at'].timestamp())}:R>"
    else:
        last_author_str = "—"

    embed = discord.Embed(color=EMBED_COLOR)
    embed.set_author(name=target_user.name, icon_url=target_user.display_avatar.url)
    embed.title = "<:ticket:1522343287816716379> Статистика тикетов и транскриптов"

    embed.add_field(
        name="За последние 7 дней:",
        value=f"• Тикетов: **{count_7_staff}**\n• Транскриптов: **{count_7_author}**",
        inline=True,
    )
    embed.add_field(
        name="За последние 30 дней:",
        value=f"• Тикетов: **{count_30_staff}**\n• Транскриптов: **{count_30_author}**",
        inline=True,
    )
    embed.add_field(
        name="За всё время:",
        value=f"• Тикетов: **{count_all_staff}**\n• Транскриптов: **{count_all_author}**",
        inline=True,
    )
    embed.add_field(
        name="<:lighting:1522337543360872489> Активность:",
        value=(
            f"• **Последний проведённый тикет:** {last_staff_str}\n"
            f"• **Последний внесённый транскрипт:** {last_author_str}"
        ),
        inline=False,
    )
    embed.set_footer(text=f"ID: {target_user.id} • Сегодня в {now.strftime('%H:%M')} • {FOOTER_TEXT}")

    return embed


async def process_leaderboard():
    now = datetime.utcnow()
    date_7_days = now - timedelta(days=7)
    date_30_days = now - timedelta(days=30)

    async def get_top_users(field: str, min_date: datetime = None):
        match_stage = {"$match": {field: {"$ne": 0}}}
        if min_date:
            match_stage["$match"]["created_at"] = {"$gte": min_date}

        pipeline = [
            match_stage,
            {"$group": {"_id": f"${field}", "cnt": {"$sum": 1}}},
            {"$sort": {"cnt": -1}},
            {"$limit": 5}
        ]
        cursor = tickets_col.aggregate(pipeline)
        return await cursor.to_list(length=5)

    (
        top_7_staff,
        top_30_staff,
        top_all_staff,
        top_7_author,
        top_30_author,
        top_all_author,
    ) = await asyncio.gather(
        get_top_users("staff_id", date_7_days),
        get_top_users("staff_id", date_30_days),
        get_top_users("staff_id"),
        get_top_users("author_id", date_7_days),
        get_top_users("author_id", date_30_days),
        get_top_users("author_id"),
    )

    def format_top(top_list, unit_label="тикетов"):
        if not top_list:
            return "— *Нет данных*"
        res = []
        for idx, item in enumerate(top_list, 1):
            res.append(f"`{idx}.` <@{item['_id']}> — **{item['cnt']}** {unit_label}")
        return "\n".join(res)

    embed = discord.Embed(
        title="<:ticket:1522343287816716379> Лидерборд тикетов и транскриптов",
        color=EMBED_COLOR,
    )

    embed.add_field(
        name="<:ticket:1522343287816716379>🎟️ Проведено тикетов (Топ за 7 дней)",
        value=format_top(top_7_staff, "тикетов"),
        inline=True,
    )
    embed.add_field(
        name="<:logs:1522340749998428160> Внесёно транскриптов (Топ за 7 дней)",
        value=format_top(top_7_author, "транскриптов"),
        inline=True,
    )
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    embed.add_field(
        name="<:ticket:1522343287816716379> Проведено тикетов (Топ за 30 дней)",
        value=format_top(top_30_staff, "тикетов"),
        inline=True,
    )
    embed.add_field(
        name="<:logs:1522340749998428160> Внесёно транскриптов (Топ за 30 дней)",
        value=format_top(top_30_author, "транскриптов"),
        inline=True,
    )
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    embed.add_field(
        name="<:ticket:1522343287816716379> Проведено тикетов (За всё время)",
        value=format_top(top_all_staff, "тикетов"),
        inline=True,
    )
    embed.add_field(
        name="<:logs:1522340749998428160> Внесёно транскриптов (За всё время)",
        value=format_top(top_all_author, "транскриптов"),
        inline=True,
    )
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    embed.set_footer(text=f"Сегодня в {now.strftime('%H:%M')} • {FOOTER_TEXT}")
    return embed


async def delete_ticket(log_id: int):
    return await tickets_col.find_one_and_delete({"ticket_id": log_id})


async def reset_tickets(staff_id: int) -> int:
    result = await tickets_col.delete_many({
        "$or": [{"staff_id": staff_id}, {"author_id": staff_id}]
    })
    return result.deleted_count


def get_help_embed(user: discord.Member | discord.User, channel_id: int):
    can_support, _ = check_access(user, channel_id, SUPPORT_ROLE_IDS)
    can_transcript, _ = check_access(user, channel_id, TRANSCRIPT_ROLE_IDS)
    can_admin, _ = check_access(user, channel_id, ADMIN_ROLE_IDS, check_channels=False)

    group_name = get_user_group_name(user, channel_id)

    embed = discord.Embed(
        title="<:staff:1522338131339251823> Список команд бота",
        description="Используй префикс `.` или слэш-команды `/`",
        color=EMBED_COLOR,
    )

    if can_support:
        embed.add_field(
            name="<:ticket:1522343287816716379> Команды Support",
            value=(
                "`.help` — Показать это меню с командами.\n\n"
                "`.ticketstats [ID / упоминание]`\n"
                "> *Посмотреть статистику тикетов и транскриптов.*\n\n"
                "`.leaderboard`\n"
                "> *Посмотреть топ модераторов по тикетам и транскриптам.*"
            ),
            inline=False,
        )

    if can_transcript:
        embed.add_field(
            name="<:logs:1522340749998428160> Команды Transcript",
            value=(
                "`.addticket [ID модератора] [ссылка] [категория]`\n"
                "> *Записать новый обработанный тикет в базу данных.*\n\n"
                "`.ticketlogs [ID / упоминание] [страница]`\n"
                "> *Посмотреть логи тикетов модератора.*"
            ),
            inline=False,
        )

    if can_admin:
        embed.add_field(
            name="<:mod:1522343179205087363> Команды Администрации",
            value=(
                "`.deleteticket [ID лога]`\n"
                "> *Удалить конкретный тикет по ID лога.*\n\n"
                "`.resettickets [ID / упоминание]`\n"
                "> *Очистить абсолютно все логи модератора.*"
            ),
            inline=False,
        )

    embed.set_footer(text=f"Ваша текущая группа: {group_name} • {FOOTER_TEXT}")
    return embed


def get_addticket_usage_embed():
    categories_str = ", ".join([f"`{c}`" for c in VALID_CATEGORIES])
    embed = discord.Embed(color=EMBED_COLOR)
    embed.title = "Команда: addticket"
    embed.description = "Добавить новый лог об обработанном тикете"
    embed.add_field(name="Кулдаун:", value="3 секунды (Для Администрации отсутствует)", inline=False)
    embed.add_field(
        name="Правила аргументов:",
        value=(
            "1. Ссылка должна содержать: `https://discord.com/`\n"
            "2. Вы **не можете** указать свой собственный ID (нельзя внести свой тикет)\n"
            "3. Один и тот же транскрипт нельзя вносить дважды\n"
            f"4. Допустимые категории: {categories_str}"
        ),
        inline=False,
    )
    embed.add_field(
        name="Использование:",
        value="`.addticket [ID модератора] [ссылка на транскрипт] [категория]`",
        inline=False,
    )
    embed.add_field(
        name="Пример:",
        value="`.addticket 851443344718430210 https://discord.com/channels/... Получение призов`",
        inline=False,
    )
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def get_ticketstats_usage_embed():
    embed = discord.Embed(color=EMBED_COLOR)
    embed.title = "Команда: ticketstats"
    embed.description = "Посмотреть статистику тикетов и транскриптов модератора"
    embed.add_field(name="Использование:", value="`.ticketstats [упоминание / ID модератора]`", inline=False)
    embed.add_field(
        name="Примеры:",
        value="`.ts` — показать свою статистику\n`.ticketstats [ID модератора]` — показать статистику выбранного модератора",
        inline=False,
    )
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def get_ticketlogs_usage_embed():
    embed = discord.Embed(color=EMBED_COLOR)
    embed.title = "Команда: ticketlogs"
    embed.description = "Посмотреть список обработанных тикетов модератора с постраничной навигацией"
    embed.add_field(name="Использование:", value="`.ticketlogs [упоминание / ID модератора] [номер страницы]`", inline=False)
    embed.add_field(
        name="Примеры:",
        value=(
            "`.tl` — показать 1-ю страницу своих логов\n"
            "`.tl 2` — показать 2-ю страницу своих логов\n"
            "`.ticketlogs 851443344718430210 1` — показать 1-ю страницу логов модератора"
        ),
        inline=False,
    )
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def get_deleteticket_usage_embed():
    embed = discord.Embed(color=EMBED_COLOR)
    embed.title = "Команда: deleteticket 🔒"
    embed.description = "Удалить один тикет из базы данных по его уникальному ID лога (Доступно только Администрации)"
    embed.add_field(name="Использование:", value="`.deleteticket [номер лога]`", inline=False)
    embed.add_field(name="Пример:", value="`.dt 15` — удалить лог под номером 15", inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def get_resettickets_usage_embed():
    embed = discord.Embed(color=EMBED_COLOR)
    embed.title = "Команда: resettickets (алиасы: .rt) 🔒"
    embed.description = "Полностью очистить статистику и логи пользователя (Доступно только Администрации)"
    embed.add_field(name="Использование (Аргумент обязателен!):", value="`.resettickets [упоминание / ID пользователя]`", inline=False)
    embed.add_field(name="Пример:", value="`.resettickets [ID модератора]` — сбросить тикеты указанного модератора", inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


async def safe_defer(interaction: discord.Interaction) -> bool:
    """Аккуратный defer(): если interaction уже протух (истекли 3 сек. ack-окна —
    например из-за сетевой задержки или гонки при редеплое, когда старый и новый
    инстансы бота какое-то время висят в gateway одновременно), не роняем хендлер
    необработанным исключением, а логируем и возвращаем False."""
    try:
        if not interaction.response.is_done():
            await interaction.response.defer()
        return True
    except discord.NotFound:
        print(f"safe_defer: interaction {interaction.id} истёк до ack (Unknown interaction / 404).")
        return False
    except discord.HTTPException as e:
        print(f"safe_defer: ошибка при defer(): {e!r}")
        return False


async def safe_followup(interaction: discord.Interaction, **kwargs):
    """Аккуратный followup.send(): если вебхук взаимодействия уже невалиден
    (404 Unknown Webhook), пробуем fallback — отправить то же самое прямо в
    канал, чтобы пользователь не остался без ответа."""
    try:
        await interaction.followup.send(**kwargs)
    except discord.NotFound:
        print(f"safe_followup: webhook для interaction {interaction.id} не найден (404). Пробую fallback в канал.")
        channel = interaction.channel
        if channel is not None:
            try:
                await channel.send(**kwargs)
            except discord.HTTPException as e:
                print(f"safe_followup: fallback в канал тоже не удался: {e!r}")
    except discord.HTTPException as e:
        print(f"safe_followup: ошибка при followup.send(): {e!r}")


@bot.tree.command(name="help", description="Показать полный список команд бота")
@check_support_slash()
async def slash_help(interaction: discord.Interaction):
    embed = get_help_embed(interaction.user, interaction.channel_id)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="addticket", description="Записать новый обработанный тикет")
@app_commands.describe(
    staff="ID участника персонала",
    transcript="Ссылка на транскрипт (должна содержать https://discord.com/)",
    category="Категория тикета",
)
@app_commands.choices(category=[app_commands.Choice(name=cat, value=cat) for cat in VALID_CATEGORIES])
@check_transcript_slash()
@app_commands.checks.cooldown(1, 3.0, key=lambda i: i.user.id)
async def slash_add_ticket(interaction: discord.Interaction, staff: str, transcript: str, category: str):
    valid, err_msg = validate_addticket_args(transcript, category)
    if not valid:
        await interaction.response.send_message(f"<:bruh:1521904409582375174> {err_msg}", ephemeral=True)
        return

    try:
        staff_id = int(staff.strip("<@!>"))
    except ValueError:
        await interaction.response.send_message(f"<:bruh:1521904409582375174> Некорректный ID: `{staff}`.", ephemeral=True)
        return

    if staff_id == interaction.user.id:
        await interaction.response.send_message(
            "<:bruh:1521904409582375174> Вы не можете занести тикет, который провели сами!", ephemeral=True
        )
        return

    # Дальше только запросы к базе/API Discord — сразу ack, чтобы не словить
    # "The application did not respond", если Mongo/Discord ответят не мгновенно.
    if not await safe_defer(interaction):
        return

    transcript_exists, staff_user = await asyncio.gather(
        is_transcript_exists(transcript),
        get_user_fast(staff_id),
    )

    if transcript_exists:
        await safe_followup(interaction, content="<:bruh:1521904409582375174> этот транскрипт уже внесен")
        return

    if not staff_user:
        await safe_followup(
            interaction,
            content=f"<:bruh:1521904409582375174> Не удалось найти пользователя по ID `{staff}`."
        )
        return

    try:
        embed = await process_add_ticket(interaction.user, staff_user, transcript, category)
    except ValueError as e:
        await safe_followup(interaction, content=f"<:bruh:1521904409582375174> {e}")
        return

    await safe_followup(interaction, embed=embed)


@slash_add_ticket.error
async def slash_add_ticket_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        if is_admin_user(interaction.user):
            return
        await interaction.response.send_message(
            f"<:zzz:1522341702852022412> Подождите ещё {error.retry_after:.1f} сек. перед повторным использованием этой команды.",
            ephemeral=True,
        )
        return
    print(f"addticket (slash) error: {error!r}")


@bot.tree.command(name="ticketstats", description="Посмотреть статистику тикетов")
@app_commands.describe(staff="Участник персонала, чью статистику нужно проверить")
@check_support_slash()
async def slash_ticket_stats(interaction: discord.Interaction, staff: discord.User = None):
    if not await safe_defer(interaction):
        return
    target_user = staff or interaction.user
    embed = await process_ticket_stats(target_user)
    await safe_followup(interaction, embed=embed)


@bot.tree.command(name="ticketlogs", description="Посмотреть тикеты пользователя")
@app_commands.describe(staff="Участник персонала", page="Номер страницы (по умолчанию 1)")
@check_transcript_slash()
async def slash_ticket_logs(interaction: discord.Interaction, staff: discord.User = None, page: int = 1):
    if not await safe_defer(interaction):
        return
    target_user = staff or interaction.user
    embed = await process_ticket_logs(target_user, page)
    await safe_followup(interaction, embed=embed)


@bot.tree.command(name="leaderboard", description="Посмотреть топ модераторов по тикетам и транскриптам")
@check_support_slash()
async def slash_leaderboard(interaction: discord.Interaction):
    if not await safe_defer(interaction):
        return
    embed = await process_leaderboard()
    await safe_followup(interaction, embed=embed)


@bot.tree.command(name="deleteticket", description="Удалить тикет по номеру лога (Только для Админов)")
@app_commands.describe(log_id="Номер лога, который нужно удалить")
@check_admin_slash()
async def slash_delete_ticket(interaction: discord.Interaction, log_id: int):
    if not await safe_defer(interaction):
        return
    ticket = await delete_ticket(log_id)
    if not ticket:
        await safe_followup(
            interaction,
            content=f"<:bruh:1521904409582375174> Лог с номером `{log_id}` не найден."
        )
        return
    await safe_followup(interaction, content=f"<a:gif_verify:1522328481956888686> Лог `{log_id}` удалён.")


@bot.tree.command(name="resettickets", description="Удалить все логи модератора (Только для Админов)")
@app_commands.describe(staff="Модератор, чьи логи удалить (Укажите пользователя)")
@check_admin_slash()
async def slash_reset_tickets(interaction: discord.Interaction, staff: discord.User):
    if not await safe_defer(interaction):
        return
    count = await reset_tickets(staff.id)
    await safe_followup(
        interaction,
        content=f"<a:gif_verify:1522328481956888686> Удалено логов модератора **{staff.name}**: `{count}`."
    )


@bot.command(name="help")
@check_support_prefix()
async def prefix_help(ctx: commands.Context):
    embed = get_help_embed(ctx.author, ctx.channel.id)
    await ctx.send(embed=embed)


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

    valid, err_msg = validate_addticket_args(transcript, category)
    if not valid:
        await ctx.send(f"<:bruh:1521904409582375174> {err_msg}")
        return

    try:
        staff_id = int(staff_raw.strip("<@!>"))
    except ValueError:
        await ctx.send("<:bruh:1521904409582375174> Укажите корректный numeric ID модератора.")
        return

    if staff_id == ctx.author.id:
        await ctx.send("<:bruh:1521904409582375174> Вы не можете занести тикет, который провели сами!")
        return

    transcript_exists, staff_user = await asyncio.gather(
        is_transcript_exists(transcript),
        get_user_fast(staff_id),
    )

    if transcript_exists:
        await ctx.send("<:bruh:1521904409582375174> этот транскрипт уже внесен")
        return

    if not staff_user:
        await ctx.send(f"<:bruh:1521904409582375174> Не удалось найти пользователя по ID `{staff_raw}`.")
        return

    try:
        embed = await process_add_ticket(ctx.author, staff_user, transcript, category)
    except ValueError as e:
        await ctx.send(f"<:bruh:1521904409582375174> {e}")
        return

    await ctx.send(embed=embed)


@prefix_add_ticket.error
async def prefix_add_ticket_error(ctx: commands.Context, error):
    if isinstance(error, commands.CommandOnCooldown):
        if is_admin_user(ctx.author):
            ctx.command.reset_cooldown(ctx)
            await ctx.reinvoke()
            return
        await ctx.send(
            f"<:zzz:1522341702852022412> Подождите ещё {error.retry_after:.1f} сек. перед повторным использованием этой команды."
        )
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.send(f"<:bruh:1521904409582375174> {error}")
        return
    print(f"addticket prefix error: {error!r}")
    await ctx.send(embed=get_addticket_usage_embed())


@bot.command(name="ticketstats", aliases=["ts"])
@check_support_prefix()
async def prefix_ticket_stats(ctx: commands.Context, staff: discord.User = None):
    target_user = staff or ctx.author
    embed = await process_ticket_stats(target_user)
    await ctx.send(embed=embed)


@bot.command(name="ticketlogs", aliases=["tl"])
@check_transcript_prefix()
async def prefix_ticket_logs(ctx: commands.Context, staff: discord.User = None, page: int = 1):
    target_user = staff or ctx.author
    embed = await process_ticket_logs(target_user, page)
    await ctx.send(embed=embed)


@bot.command(name="leaderboard", aliases=["lb"])
@check_support_prefix()
async def prefix_leaderboard(ctx: commands.Context):
    embed = await process_leaderboard()
    await ctx.send(embed=embed)


@bot.command(name="deleteticket", aliases=["dt"])
@check_admin_prefix()
async def prefix_delete_ticket(ctx: commands.Context, log_id: int = None):
    if log_id is None:
        await ctx.send(embed=get_deleteticket_usage_embed())
        return

    ticket = await delete_ticket(log_id)
    if not ticket:
        await ctx.send(f"<:bruh:1521904409582375174> Лог с номером `{log_id}` не найден.")
        return

    await ctx.send(f"<a:gif_verify:1522328481956888686> Лог `{log_id}` удалён.")


@prefix_delete_ticket.error
async def prefix_delete_ticket_error(ctx: commands.Context, error):
    if isinstance(error, commands.BadArgument):
        await ctx.send(embed=get_deleteticket_usage_embed())
        return


@bot.command(name="resettickets", aliases=["rt"])
@check_admin_prefix()
async def prefix_reset_tickets(ctx: commands.Context, staff: discord.User = None):
    if staff is None:
        await ctx.send(embed=get_resettickets_usage_embed())
        return

    count = await reset_tickets(staff.id)
    await ctx.send(
        f"<a:gif_verify:1522328481956888686> Удалено логов модератора **{staff.name}**: `{count}`."
    )


@prefix_reset_tickets.error
async def prefix_reset_tickets_error(ctx: commands.Context, error):
    if isinstance(error, commands.BadArgument):
        await ctx.send(embed=get_resettickets_usage_embed())
        return


if __name__ == "__main__":
    bot.run(TOKEN)

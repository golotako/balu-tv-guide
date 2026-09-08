import json
import os
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# ============================================================
# CONFIG
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

EPG_URL = "https://www.open-epg.com/files/israel.xml"

EPG_CACHE_HOURS = 48
TELEGRAM_MESSAGE_LIMIT = 4000

PORT = int(os.environ.get("PORT", "10000"))

WEBHOOK_PATH = "/telegram"

RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")

ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


# ============================================================
# CHANNEL ALIASES
# ============================================================

with open("channels.json", "r", encoding="utf-8") as file:
    CHANNEL_ALIASES = json.load(file)


# ============================================================
# EPG CACHE
# ============================================================

EPG_ROOT = None
EPG_LAST_UPDATE = None

CHANNELS_CACHE = []
CHANNEL_BY_ID = {}

ALIAS_TO_CHANNEL_IDS = {}

PROGRAMS_BY_CHANNEL = {}


# ============================================================
# USER STATE
# ============================================================

def get_user_state(context):
    return context.user_data.setdefault(
        "state",
        {
            "searching_channel": False,
            "searching_program": False,
            "selected_channel_id": None,
        },
    )


def reset_state(context):
    state = get_user_state(context)

    state["searching_channel"] = False
    state["searching_program"] = False


# ============================================================
# TEXT HELPERS
# ============================================================

def normalize_text(text):
    """
    Normalize Hebrew / English channel names for searching.
    """

    if not text:
        return ""

    text = unicodedata.normalize("NFKC", str(text))
    text = text.lower().strip()

    text = re.sub(r"[-_/_.]+", " ", text)

    # Keep Hebrew, English, numbers and +
    text = re.sub(r"[^\w\u0590-\u05FF+ ]+", " ", text)

    text = re.sub(r"\s+", " ", text)

    return text.strip()


def extract_numbers(text):
    return set(re.findall(r"\d+", normalize_text(text)))


# ============================================================
# DATE HELPERS
# ============================================================

def parse_epg_datetime(value):
    """
    Parse common XMLTV datetime formats.
    """

    if not value:
        return None

    value = value.strip()

    formats = [
        "%Y%m%d%H%M%S %z",
        "%Y%m%d%H%M%S%z",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass

    return None


def to_israel_time(dt):
    if dt is None:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(ISRAEL_TZ)


# ============================================================
# EPG INDEX
# ============================================================

def build_epg_index(root):
    """
    Build all searchable indexes once after downloading EPG.
    """

    global CHANNELS_CACHE
    global CHANNEL_BY_ID
    global ALIAS_TO_CHANNEL_IDS
    global PROGRAMS_BY_CHANNEL

    CHANNELS_CACHE = []
    CHANNEL_BY_ID = {}
    ALIAS_TO_CHANNEL_IDS = {}
    PROGRAMS_BY_CHANNEL = {}

    # --------------------------------------------------------
    # Channels
    # --------------------------------------------------------

    for channel in root.findall("channel"):
        channel_id = channel.get("id")

        if not channel_id:
            continue

        display_name_element = channel.find("display-name")

        if display_name_element is not None:
            display_name = display_name_element.text or channel_id
        else:
            display_name = channel_id

        normalized_name = normalize_text(display_name)

        channel_data = {
            "id": channel_id,
            "name": display_name,
            "normalized_name": normalized_name,
            "numbers": extract_numbers(display_name),
        }

        CHANNELS_CACHE.append(channel_data)
        CHANNEL_BY_ID[channel_id] = channel_data

    # --------------------------------------------------------
    # Aliases
    # --------------------------------------------------------

    for channel_id, aliases in CHANNEL_ALIASES.items():

        if channel_id not in CHANNEL_BY_ID:
            continue

        for alias in aliases:
            normalized_alias = normalize_text(alias)

            if not normalized_alias:
                continue

            ALIAS_TO_CHANNEL_IDS.setdefault(
                normalized_alias,
                []
            ).append(channel_id)

    # --------------------------------------------------------
    # Programs
    # --------------------------------------------------------

    for programme in root.findall("programme"):

        channel_id = programme.get("channel")

        if not channel_id:
            continue

        start = parse_epg_datetime(programme.get("start"))
        stop = parse_epg_datetime(programme.get("stop"))

        title_element = programme.find("title")
        desc_element = programme.find("desc")

        title = (
            title_element.text.strip()
            if title_element is not None and title_element.text
            else "ללא שם"
        )

        description = (
            desc_element.text.strip()
            if desc_element is not None and desc_element.text
            else ""
        )

        program_data = {
            "title": title,
            "description": description,
            "start": start,
            "stop": stop,
        }

        PROGRAMS_BY_CHANNEL.setdefault(
            channel_id,
            []
        ).append(program_data)

    # --------------------------------------------------------
    # Sort programs
    # --------------------------------------------------------

    for channel_id in PROGRAMS_BY_CHANNEL:
        PROGRAMS_BY_CHANNEL[channel_id].sort(
            key=lambda program: program["start"] or datetime.min.replace(
                tzinfo=timezone.utc
            )
        )

    print(
        f"EPG indexed: "
        f"{len(CHANNELS_CACHE)} channels, "
        f"{sum(len(v) for v in PROGRAMS_BY_CHANNEL.values())} programs, "
        f"{len(ALIAS_TO_CHANNEL_IDS)} aliases"
    )


# ============================================================
# EPG LOADER
# ============================================================

def load_epg(force=False):
    """
    Download EPG only when cache is empty or older than 48 hours.
    """

    global EPG_ROOT
    global EPG_LAST_UPDATE

    now = datetime.now(timezone.utc)

    if (
        not force
        and EPG_ROOT is not None
        and EPG_LAST_UPDATE is not None
        and now - EPG_LAST_UPDATE < timedelta(hours=EPG_CACHE_HOURS)
    ):
        return EPG_ROOT

    print("Downloading EPG...")

    response = requests.get(
        EPG_URL,
        timeout=60,
    )

    response.raise_for_status()

    root = ET.fromstring(response.content)

    build_epg_index(root)

    EPG_ROOT = root
    EPG_LAST_UPDATE = now

    print("EPG loaded successfully")

    return EPG_ROOT


# ============================================================
# CHANNEL SEARCH
# ============================================================

def find_channels(search_text):
    """
    Search channels using aliases, exact names,
    partial names and fuzzy matching.
    """

    query = normalize_text(search_text)

    if not query:
        return []

    # --------------------------------------------------------
    # Exact alias
    # --------------------------------------------------------

    exact_ids = ALIAS_TO_CHANNEL_IDS.get(query)

    if exact_ids:
        return [
            CHANNEL_BY_ID[channel_id]
            for channel_id in exact_ids
            if channel_id in CHANNEL_BY_ID
        ]

    # --------------------------------------------------------
    # Search
    # --------------------------------------------------------

    query_numbers = extract_numbers(query)

    results = []

    for channel in CHANNELS_CACHE:

        name = channel["normalized_name"]

        score = 0

        # Exact name
        if query == name:
            score = 100

        # Query contained in name
        elif query in name:
            score = 90

        # Name contained in query
        elif name in query:
            score = 85

        else:
            query_words = set(query.split())
            name_words = set(name.split())

            # Word overlap
            if query_words and query_words.intersection(name_words):
                score = max(score, 75)

            # Number match
            if query_numbers and query_numbers.intersection(
                channel["numbers"]
            ):
                score = max(score, 80)

            # Fuzzy match
            fuzzy_score = SequenceMatcher(
                None,
                query,
                name,
            ).ratio()

            if fuzzy_score >= 0.55:
                score = max(score, int(fuzzy_score * 70))

        if score > 0:
            results.append(
                (
                    score,
                    channel,
                )
            )

    results.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    return [
        channel
        for _, channel in results[:5]
    ]


# ============================================================
# PROGRAM SEARCH
# ============================================================

def search_programs(channel_id, search_text):
    query = normalize_text(search_text)

    if not query:
        return []

    programs = PROGRAMS_BY_CHANNEL.get(
        channel_id,
        []
    )

    results = []

    for program in programs:

        title = normalize_text(
            program["title"]
        )

        if not title:
            continue

        score = 0

        if query == title:
            score = 100

        elif query in title:
            score = 90

        else:
            fuzzy_score = SequenceMatcher(
                None,
                query,
                title,
            ).ratio()

            if fuzzy_score >= 0.45:
                score = int(fuzzy_score * 80)

        if score > 0:
            results.append(
                (
                    score,
                    program,
                )
            )

    results.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    return [
        program
        for _, program in results[:10]
    ]


# ============================================================
# PROGRAM FILTERS
# ============================================================

def get_last_48_hours(channel_id):
    now = datetime.now(timezone.utc)

    start_time = now - timedelta(hours=48)

    programs = PROGRAMS_BY_CHANNEL.get(
        channel_id,
        []
    )

    return [
        program
        for program in programs
        if (
            program["stop"] is not None
            and program["stop"] >= start_time
            and program["start"] is not None
            and program["start"] <= now
        )
    ]


def get_current_program(channel_id):
    now = datetime.now(timezone.utc)

    programs = PROGRAMS_BY_CHANNEL.get(
        channel_id,
        []
    )

    for program in programs:

        start = program["start"]
        stop = program["stop"]

        if not start:
            continue

        if start <= now and (
            stop is None or now < stop
        ):
            return program

    return None


# ============================================================
# FORMATTING
# ============================================================

def format_program(program):
    start = to_israel_time(program["start"])
    stop = to_israel_time(program["stop"])

    if start:
        start_text = start.strftime("%d/%m %H:%M")
    else:
        start_text = "?"

    if stop:
        stop_text = stop.strftime("%H:%M")
    else:
        stop_text = "?"

    text = f"📺 {program['title']}\n"
    text += f"🕐 {start_text} - {stop_text}"

    if program["description"]:
        text += f"\n{program['description']}"

    return text


# ============================================================
# KEYBOARDS
# ============================================================

def channel_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "▶️ עכשיו",
                    callback_data="current",
                ),
                InlineKeyboardButton(
                    "🕐 48 שעות",
                    callback_data="48hours",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔎 חיפוש תוכנית",
                    callback_data="search_program",
                ),
                InlineKeyboardButton(
                    "📺 ערוץ אחר",
                    callback_data="search_channel",
                ),
            ],
        ]
    )


def program_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔎 חיפוש תוכנית אחרת",
                    callback_data="search_program",
                )
            ],
            [
                InlineKeyboardButton(
                    "📺 ערוץ אחר",
                    callback_data="search_channel",
                )
            ],
        ]
    )


# ============================================================
# COMMANDS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    reset_state(context)

    state = get_user_state(context)
    state["searching_channel"] = True

    await update.message.reply_text(
        "📺 ברוכים הבאים!\n\n"
        "שלח לי שם או מספר של ערוץ.\n\n"
        "לדוגמה:\n"
        "• 12\n"
        "• קשת\n"
        "• כאן 11\n"
        "• בית+\n"
        "• ערוץ ההומור\n"
        "• Sport 5"
    )


# ============================================================
# CHANNEL SELECTION
# ============================================================

async def handle_channel_search(
    update,
    context,
    text,
):
    load_epg()

    matches = find_channels(text)

    if not matches:
        await update.message.reply_text(
            "❌ לא מצאתי ערוץ מתאים.\n\n"
            "נסה שם אחר או מספר ערוץ."
        )
        return

    if len(matches) == 1:

        channel = matches[0]

        state = get_user_state(context)

        state["searching_channel"] = False
        state["searching_program"] = False
        state["selected_channel_id"] = channel["id"]

        await update.message.reply_text(
            f"📺 {channel['name']}\n\n"
            "מה תרצה לראות?",
            reply_markup=channel_keyboard(),
        )

        return

    keyboard = []

    for channel in matches:

        keyboard.append(
            [
                InlineKeyboardButton(
                    channel["name"],
                    callback_data=f"channel:{channel['id']}",
                )
            ]
        )

    await update.message.reply_text(
        "מצאתי כמה ערוצים. בחר:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ============================================================
# PROGRAM SEARCH
# ============================================================

async def handle_program_search(
    update,
    context,
    text,
):
    state = get_user_state(context)

    channel_id = state.get(
        "selected_channel_id"
    )

    if not channel_id:
        state["searching_program"] = False
        state["searching_channel"] = True

        await update.message.reply_text(
            "📺 קודם בחר ערוץ."
        )
        return

    load_epg()

    results = search_programs(
        channel_id,
        text,
    )

    if not results:
        await update.message.reply_text(
            "❌ לא מצאתי תוכנית מתאימה בערוץ הזה.\n\n"
            "נסה לחפש לפי שם התוכנית."
        )
        return

    state["searching_program"] = False

    channel = CHANNEL_BY_ID.get(
        channel_id
    )

    header = f"📺 {channel['name']}\n\n"

    text_parts = [header]

    for program in results:
        text_parts.append(
            format_program(program)
        )

    message = "\n\n".join(text_parts)

    if len(message) > TELEGRAM_MESSAGE_LIMIT:
        message = message[
            :TELEGRAM_MESSAGE_LIMIT
        ]

    await update.message.reply_text(
        message,
        reply_markup=program_keyboard(),
    )


# ============================================================
# MESSAGE HANDLER
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = update.message.text.strip()

    if not text:
        return

    state = get_user_state(context)

    # --------------------------------------------------------
    # Program search has priority
    # --------------------------------------------------------

    if state.get("searching_program"):
        await handle_program_search(
            update,
            context,
            text,
        )
        return

    # --------------------------------------------------------
    # Channel search
    # --------------------------------------------------------

    await handle_channel_search(
        update,
        context,
        text,
    )


# ============================================================
# CALLBACKS
# ============================================================

async def handle_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    state = get_user_state(context)

    # --------------------------------------------------------
    # Search another channel
    # --------------------------------------------------------

    if query.data == "search_channel":

        reset_state(context)

        state["searching_channel"] = True

        await query.message.reply_text(
            "📺 שלח שם או מספר של ערוץ."
        )

        return

    # --------------------------------------------------------
    # Search program
    # --------------------------------------------------------

    if query.data == "search_program":

        state["searching_channel"] = False
        state["searching_program"] = True

        await query.message.reply_text(
            "🔎 שלח את שם התוכנית שאתה מחפש."
        )

        return

    # --------------------------------------------------------
    # Select channel
    # --------------------------------------------------------

    if query.data.startswith("channel:"):

        channel_id = query.data.split(
            ":",
            1,
        )[1]

        load_epg()

        channel = CHANNEL_BY_ID.get(
            channel_id
        )

        if not channel:

            await query.message.reply_text(
                "❌ הערוץ לא נמצא."
            )

            return

        state["selected_channel_id"] = channel_id
        state["searching_channel"] = False
        state["searching_program"] = False

        await query.message.reply_text(
            f"📺 {channel['name']}\n\n"
            "מה תרצה לראות?",
            reply_markup=channel_keyboard(),
        )

        return

    # --------------------------------------------------------
    # Current program
    # --------------------------------------------------------

    if query.data == "current":

        channel_id = state.get(
            "selected_channel_id"
        )

        if not channel_id:
            await query.message.reply_text(
                "❌ לא נבחר ערוץ."
            )
            return

        load_epg()

        channel = CHANNEL_BY_ID.get(
            channel_id
        )

        program = get_current_program(
            channel_id
        )

        if not program:

            await query.message.reply_text(
                f"📺 {channel['name']}\n\n"
                "❌ לא מצאתי כרגע תוכנית משודרת."
            )

            return

        await query.message.reply_text(
            f"📺 {channel['name']}\n\n"
            f"{format_program(program)}",
            reply_markup=channel_keyboard(),
        )

        return

    # --------------------------------------------------------
    # Last 48 hours
    # --------------------------------------------------------

    if query.data == "48hours":

        channel_id = state.get(
            "selected_channel_id"
        )

        if not channel_id:
            await query.message.reply_text(
                "❌ לא נבחר ערוץ."
            )
            return

        load_epg()

        channel = CHANNEL_BY_ID.get(
            channel_id
        )

        programs = get_last_48_hours(
            channel_id
        )

        if not programs:

            await query.message.reply_text(
                f"📺 {channel['name']}\n\n"
                "❌ אין מידע זמין ל־48 השעות האחרונות."
            )

            return

        parts = [
            f"📺 {channel['name']}",
            "🕐 48 השעות האחרונות",
            "",
        ]

        for program in programs:
            parts.append(
                format_program(program)
            )
            parts.append("")

        message = "\n".join(parts)

        if len(message) > TELEGRAM_MESSAGE_LIMIT:
            message = message[
                :TELEGRAM_MESSAGE_LIMIT
            ]

            message += "\n\n...ההודעה קוצרה"

        await query.message.reply_text(
            message,
            reply_markup=channel_keyboard(),
        )

        return


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context,
):

    print(
        "Telegram error:",
        context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            handle_callback,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    application.add_error_handler(
        error_handler
    )

    if RENDER_EXTERNAL_URL:

        webhook_url = (
            f"{RENDER_EXTERNAL_URL}"
            f"{WEBHOOK_PATH}"
        )

        print(
            f"Starting webhook: {webhook_url}"
        )

        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=webhook_url,
            url_path=WEBHOOK_PATH.lstrip("/"),
        )

    else:

        print(
            "Starting polling mode..."
        )

        application.run_polling()


if __name__ == "__main__":
    main()
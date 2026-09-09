import html
import json
import os
import re
import unicodedata
import xml.etree.ElementTree as ET

from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

import requests

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)

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

# Internal cache only.
# The user does NOT see a "48 hours" feature.
EPG_CACHE_HOURS = 48

TELEGRAM_MESSAGE_LIMIT = 4000

PORT = int(
    os.environ.get(
        "PORT",
        "10000"
    )
)

WEBHOOK_PATH = "/telegram"

RENDER_EXTERNAL_URL = os.environ.get(
    "RENDER_EXTERNAL_URL",
    ""
).rstrip("/")

ISRAEL_TZ = ZoneInfo(
    "Asia/Jerusalem"
)


# ============================================================
# LOAD CHANNEL ALIASES
# ============================================================

with open(
    "channels.json",
    "r",
    encoding="utf-8"
) as file:
    CHANNEL_ALIASES = json.load(file)


# ============================================================
# GLOBAL EPG CACHE
# ============================================================

EPG_ROOT = None
EPG_LAST_UPDATE = None

CHANNELS_CACHE = []
CHANNEL_BY_ID = {}

ALIAS_TO_CHANNEL_IDS = {}

PROGRAMS_BY_CHANNEL = {}


# ============================================================
# TEXT NORMALIZATION
# ============================================================

def normalize_text(text):

    if not text:
        return ""

    text = str(text)

    text = unicodedata.normalize(
        "NFKC",
        text
    )

    text = text.lower()

    text = re.sub(
        r"[^\w\u0590-\u05FF]+",
        " ",
        text,
        flags=re.UNICODE,
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    ).strip()

    return text


def extract_numbers(text):

    return re.findall(
        r"\d+",
        text or ""
    )


# ============================================================
# EPG DATE PARSING
# ============================================================

def parse_epg_datetime(value):

    if not value:
        return None

    value = value.strip()

    match = re.match(
        r"^(\d{14})\s*([+-]\d{4})?$",
        value,
    )

    if not match:
        return None

    date_part = match.group(1)
    offset_part = match.group(2)

    try:

        dt = datetime.strptime(
            date_part,
            "%Y%m%d%H%M%S"
        )

    except ValueError:

        return None

    # EPG contains explicit timezone
    if offset_part:

        from datetime import timedelta

        sign = (
            1
            if offset_part[0] == "+"
            else -1
        )

        hours = int(
            offset_part[1:3]
        )

        minutes = int(
            offset_part[3:5]
        )

        offset = (
            timedelta(
                hours=hours,
                minutes=minutes
            )
            * sign
        )

        dt = dt.replace(
            tzinfo=timezone(offset)
        )

        return dt.astimezone(
            timezone.utc
        )

    # No timezone in EPG:
    # assume Israel local time.
    dt = dt.replace(
        tzinfo=ISRAEL_TZ
    )

    return dt.astimezone(
        timezone.utc
    )


def to_israel_time(dt):

    if dt is None:
        return None

    return dt.astimezone(
        ISRAEL_TZ
    )


# ============================================================
# EPG INDEX
# ============================================================

def build_epg_index(root):

    global CHANNELS_CACHE
    global CHANNEL_BY_ID
    global ALIAS_TO_CHANNEL_IDS
    global PROGRAMS_BY_CHANNEL

    CHANNELS_CACHE = []
    CHANNEL_BY_ID = {}
    ALIAS_TO_CHANNEL_IDS = {}
    PROGRAMS_BY_CHANNEL = {}

    # ========================================================
    # CHANNELS
    # ========================================================

    for channel in root.findall(
        "channel"
    ):

        channel_id = channel.attrib.get(
            "id"
        )

        if not channel_id:
            continue

        display_names = []

        for name in channel.findall(
            "display-name"
        ):

            if name.text:

                display_names.append(
                    name.text.strip()
                )

        channel_data = {
            "id": channel_id,
            "names": display_names,
        }

        CHANNELS_CACHE.append(
            channel_data
        )

        CHANNEL_BY_ID[
            channel_id
        ] = channel_data

    # ========================================================
    # ALIASES FROM channels.json
    # ========================================================

    for channel_entry in CHANNEL_ALIASES:

        channel_id = channel_entry.get(
            "id"
        )

        aliases = channel_entry.get(
            "names",
            []
        )

        if not channel_id:
            continue

        if channel_id not in CHANNEL_BY_ID:
            continue

        for alias in aliases:

            normalized_alias = normalize_text(
                alias
            )

            if not normalized_alias:
                continue

            ALIAS_TO_CHANNEL_IDS.setdefault(
                normalized_alias,
                []
            ).append(
                channel_id
            )

    # ========================================================
    # EPG DISPLAY NAMES
    # ========================================================

    for channel in CHANNELS_CACHE:

        channel_id = channel["id"]

        for name in channel["names"]:

            normalized_name = normalize_text(
                name
            )

            if not normalized_name:
                continue

            ALIAS_TO_CHANNEL_IDS.setdefault(
                normalized_name,
                []
            ).append(
                channel_id
            )

    # Remove duplicates
    for alias, ids in ALIAS_TO_CHANNEL_IDS.items():

        ALIAS_TO_CHANNEL_IDS[alias] = list(
            dict.fromkeys(ids)
        )

    # ========================================================
    # PROGRAMS
    # ========================================================

    for program in root.findall(
        "programme"
    ):

        channel_id = program.attrib.get(
            "channel"
        )

        if not channel_id:
            continue

        if channel_id not in CHANNEL_BY_ID:
            continue

        start = parse_epg_datetime(
            program.attrib.get(
                "start"
            )
        )

        stop = parse_epg_datetime(
            program.attrib.get(
                "stop"
            )
        )

        title_element = program.find(
            "title"
        )

        desc_element = program.find(
            "desc"
        )

        title = ""

        if title_element is not None:

            title = (
                title_element.text or ""
            ).strip()

        description = ""

        if desc_element is not None:

            description = (
                desc_element.text or ""
            ).strip()

        if not title:
            title = "ללא שם"

        program_data = {
            "channel_id": channel_id,
            "title": title,
            "description": description,
            "start": start,
            "stop": stop,
        }

        PROGRAMS_BY_CHANNEL.setdefault(
            channel_id,
            []
        ).append(
            program_data
        )

    # ========================================================
    # SORT PROGRAMS
    # ========================================================

    for channel_id in PROGRAMS_BY_CHANNEL:

        PROGRAMS_BY_CHANNEL[
            channel_id
        ].sort(
            key=lambda p: (
                p["start"]
                or datetime.min.replace(
                    tzinfo=timezone.utc
                )
            )
        )

    total_programs = sum(
        len(programs)
        for programs in PROGRAMS_BY_CHANNEL.values()
    )

    print(
        f"EPG indexed: "
        f"{len(CHANNELS_CACHE)} channels, "
        f"{total_programs} programs"
    )


# ============================================================
# LOAD EPG
# ============================================================

def load_epg(force=False):

    global EPG_ROOT
    global EPG_LAST_UPDATE

    now = datetime.now(
        timezone.utc
    )

    cache_valid = (
        EPG_ROOT is not None
        and EPG_LAST_UPDATE is not None
        and (
            now - EPG_LAST_UPDATE
        ).total_seconds()
        < EPG_CACHE_HOURS * 3600
    )

    if cache_valid and not force:

        return True

    print(
        "Downloading EPG..."
    )

    try:

        response = requests.get(
            EPG_URL,
            timeout=60
        )

        response.raise_for_status()

        root = ET.fromstring(
            response.content
        )

        build_epg_index(
            root
        )

        EPG_ROOT = root
        EPG_LAST_UPDATE = now

        print(
            "EPG loaded successfully."
        )

        return True

    except Exception as error:

        print(
            f"EPG loading error: {error}"
        )

        return False


# ============================================================
# CHANNEL SEARCH
# ============================================================

def find_channels(search_text):

    if not load_epg():
        return []

    query = normalize_text(
        search_text
    )

    if not query:
        return []

    results = []
    seen = set()

    # ========================================================
    # EXACT MATCH
    # ========================================================

    exact_matches = (
        ALIAS_TO_CHANNEL_IDS.get(
            query,
            []
        )
    )

    for channel_id in exact_matches:

        if channel_id in seen:
            continue

        channel = CHANNEL_BY_ID.get(
            channel_id
        )

        if not channel:
            continue

        results.append(
            channel
        )

        seen.add(
            channel_id
        )

    if results:

        return results[:5]

    # ========================================================
    # FUZZY / PARTIAL SEARCH
    # ========================================================

    query_numbers = extract_numbers(
        query
    )

    scored = []

    for channel in CHANNELS_CACHE:

        channel_id = channel["id"]

        names = list(
            channel.get(
                "names",
                []
            )
        )

        # Add aliases
        for alias_entry in CHANNEL_ALIASES:

            if (
                alias_entry.get("id")
                == channel_id
            ):

                names.extend(
                    alias_entry.get(
                        "names",
                        []
                    )
                )

        best_score = 0

        for name in names:

            normalized_name = normalize_text(
                name
            )

            if not normalized_name:
                continue

            if query == normalized_name:

                score = 100

            elif normalized_name.startswith(
                query
            ):

                score = 90

            elif query in normalized_name:

                score = 80

            elif any(
                query == word
                for word in normalized_name.split()
            ):

                score = 85

            else:

                ratio = SequenceMatcher(
                    None,
                    query,
                    normalized_name
                ).ratio()

                score = ratio * 70

            best_score = max(
                best_score,
                score
            )

        # Number search
        if query_numbers:

            channel_numbers = set()

            for name in names:

                channel_numbers.update(
                    extract_numbers(name)
                )

            if any(
                number in channel_numbers
                for number in query_numbers
            ):

                best_score = max(
                    best_score,
                    95
                )

        if best_score >= 45:

            scored.append(
                (
                    best_score,
                    channel
                )
            )

    scored.sort(
        key=lambda item: item[0],
        reverse=True
    )

    for _, channel in scored:

        channel_id = channel["id"]

        if channel_id in seen:
            continue

        results.append(
            channel
        )

        seen.add(
            channel_id
        )

        if len(results) >= 5:
            break

    return results


# ============================================================
# PROGRAM SEARCH
# ============================================================

def search_programs(
    channel_id,
    search_text
):

    if not load_epg():
        return []

    query = normalize_text(
        search_text
    )

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

        description = normalize_text(
            program.get(
                "description",
                ""
            )
        )

        if (
            query in title
            or query in description
        ):

            results.append(
                program
            )

    return results[:20]


# ============================================================
# CURRENT / PREVIOUS / NEXT
# ============================================================

def get_current_program(
    channel_id
):

    now = datetime.now(
        timezone.utc
    )

    programs = PROGRAMS_BY_CHANNEL.get(
        channel_id,
        []
    )

    for program in programs:

        start = program["start"]
        stop = program["stop"]

        if start is None:
            continue

        if stop is None:

            if start <= now:
                return program

        elif start <= now < stop:

            return program

    return None


def get_previous_program(
    channel_id
):

    now = datetime.now(
        timezone.utc
    )

    programs = PROGRAMS_BY_CHANNEL.get(
        channel_id,
        []
    )

    previous = None

    for program in programs:

        start = program["start"]
        stop = program["stop"]

        if start is None:
            continue

        if (
            stop is not None
            and stop <= now
        ):

            previous = program

        elif start > now:

            break

    return previous


def get_next_program(
    channel_id
):

    now = datetime.now(
        timezone.utc
    )

    programs = PROGRAMS_BY_CHANNEL.get(
        channel_id,
        []
    )

    for program in programs:

        start = program["start"]

        if start is None:
            continue

        if start > now:

            return program

    return None


# ============================================================
# TODAY'S PROGRAMS
# ============================================================

def get_today_programs(
    channel_id
):

    """
    Returns all programs that started
    between today's 00:00 and now,
    according to Israel time.

    The currently playing program is included.
    Future programs are excluded.
    """

    now_utc = datetime.now(
        timezone.utc
    )

    now_israel = now_utc.astimezone(
        ISRAEL_TZ
    )

    # Today's midnight in Israel
    today_start_israel = (
        now_israel.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0
        )
    )

    today_start_utc = (
        today_start_israel.astimezone(
            timezone.utc
        )
    )

    programs = PROGRAMS_BY_CHANNEL.get(
        channel_id,
        []
    )

    today_programs = []

    for program in programs:

        start = program["start"]
        stop = program["stop"]

        if start is None:
            continue

        # A program that started before midnight
        # but is still running after midnight.
        if (
            start < today_start_utc
            and stop is not None
            and stop > today_start_utc
            and start <= now_utc
        ):

            today_programs.append(
                program
            )

            continue

        # Program started today and has already begun.
        if (
            start >= today_start_utc
            and start <= now_utc
        ):

            today_programs.append(
                program
            )

    # Remove duplicates
    unique_programs = []
    seen = set()

    for program in today_programs:

        key = (
            program["start"],
            program["stop"],
            program["title"],
        )

        if key in seen:
            continue

        seen.add(
            key
        )

        unique_programs.append(
            program
        )

    unique_programs.sort(
        key=lambda p: (
            p["start"]
            or datetime.min.replace(
                tzinfo=timezone.utc
            )
        )
    )

    return unique_programs


# ============================================================
# TIME FORMAT
# ============================================================

def format_time(dt):

    if dt is None:
        return "--:--"

    local_time = to_israel_time(
        dt
    )

    return local_time.strftime(
        "%H:%M"
    )


# ============================================================
# CHANNEL NAME
# ============================================================

def get_channel_display_name(
    channel
):

    names = channel.get(
        "names",
        []
    )

    if names:

        return names[0]

    return channel.get(
        "id",
        "ערוץ"
    )


# ============================================================
# PROGRAM CARD
# ============================================================

def format_program_card(
    program,
    emoji
):

    if not program:
        return (
            f"{emoji} <b>אין מידע</b>"
        )

    title = html.escape(program["title"])

    start = format_time(
        program["start"]
    )

    stop = format_time(
        program["stop"]
    )

    return (
        f"{emoji} <b>{start}–{stop}</b>\n"
        f"{title}"
    )


# ============================================================
# CHANNEL SCREEN
# ============================================================

def build_channel_screen(
    channel_id
):

    channel = CHANNEL_BY_ID.get(
        channel_id
    )

    if not channel:
        return (
            "❌ הערוץ לא נמצא."
        )

    previous = get_previous_program(
        channel_id
    )

    current = get_current_program(
        channel_id
    )

    next_program = get_next_program(
        channel_id
    )

    channel_name = html.escape(
        get_channel_display_name(channel)
    )

    text = (
        f"📺 <b>{channel_name}</b>\n\n"
    )

    # Previous
    if previous:
        text += format_program_card(
            previous,
            "⏮️"
        )
    else:
        text += "⏮️ <b>אין מידע</b>"

    text += (
        "\n\n"
        "━━━━━━━━━━━━━━"
        "\n\n"
    )

    # Current
    if current:
        text += format_program_card(
            current,
            "🔴"
        )
    else:
        text += (
            "⏸️ <b>אין שידור כרגע</b>"
        )

    text += (
        "\n\n"
        "━━━━━━━━━━━━━━"
        "\n\n"
    )

    # Next
    if next_program:
        text += format_program_card(
            next_program,
            "⏭️"
        )
    else:
        text += "⏭️ <b>אין מידע</b>"

    return text


# ============================================================
# TODAY SCREEN
# ============================================================

def build_today_screen(
    channel_id
):

    channel = CHANNEL_BY_ID.get(
        channel_id
    )

    if not channel:
        return (
            "❌ הערוץ לא נמצא."
        )

    programs = get_today_programs(
        channel_id
    )

    current = get_current_program(
        channel_id
    )

    channel_name = html.escape(
        get_channel_display_name(channel)
    )

    now_israel = datetime.now(
        timezone.utc
    ).astimezone(
        ISRAEL_TZ
    )

    date_text = now_israel.strftime(
        "%d/%m/%Y"
    )

    text = (
        f"📺 <b>{channel_name}</b>\n"
        f"📅 <b>מה שודר היום</b>\n"
        f"{date_text}\n\n"
    )

    if not programs:
        text += (
            "אין מידע על תוכניות ששודרו היום."
        )
        return text

    for program in programs:

        start_time = format_time(
            program["start"]
        )

        stop_time = format_time(
            program["stop"]
        )

        title = html.escape(
            program["title"]
        )

        if current is program:
            text += (
                f"🔴 <b>{start_time}–{stop_time}</b>  "
                f"<b>{title}</b>\n"
            )
        else:
            text += (
                f"• {start_time}–{stop_time}  "
                f"{title}\n"
            )

    # Keep the complete header and schedule within Telegram's limit.
    if len(text) > TELEGRAM_MESSAGE_LIMIT:

        header = (
            f"📺 <b>{channel_name}</b>\n"
            f"📅 <b>מה שודר היום</b>\n"
            f"{date_text}\n\n"
        )

        schedule = ""

        for program in programs:

            start_time = format_time(
                program["start"]
            )

            stop_time = format_time(
                program["stop"]
            )

            title = html.escape(
                program["title"]
            )

            current_marker = (
                "🔴 "
                if current is program
                else "• "
            )

            line = (
                f"{current_marker}"
                f"{start_time}–{stop_time}  "
                f"{title}\n"
            )

            if (
                len(header)
                + len(schedule)
                + len(line)
                + 20
                > TELEGRAM_MESSAGE_LIMIT
            ):
                break

            schedule += line

        text = (
            header
            + schedule
            + "..."
        )

    return text


# ============================================================
# PRIME TIME
# ============================================================

def get_prime_time_programs(
    channel_id
):

    now_utc = datetime.now(
        timezone.utc
    )

    now_israel = now_utc.astimezone(
        ISRAEL_TZ
    )

    prime_start_israel = now_israel.replace(
        hour=20,
        minute=0,
        second=0,
        microsecond=0
    )

    prime_end_israel = (
        prime_start_israel
        + timedelta(days=1)
    )

    prime_start_utc = (
        prime_start_israel.astimezone(
            timezone.utc
        )
    )

    prime_end_utc = (
        prime_end_israel.astimezone(
            timezone.utc
        )
    )

    programs = PROGRAMS_BY_CHANNEL.get(
        channel_id,
        []
    )

    prime_time_programs = []

    for program in programs:

        start = program["start"]
        stop = program["stop"]

        if start is None:
            continue

        # Include every program that overlaps 20:00–00:00.
        if (
            start < prime_end_utc
            and (
                stop is None
                or stop > prime_start_utc
            )
        ):
            prime_time_programs.append(
                program
            )

    return prime_time_programs


def build_prime_time_screen(
    channel_id
):

    channel = CHANNEL_BY_ID.get(
        channel_id
    )

    if not channel:
        return (
            "❌ הערוץ לא נמצא."
        )

    programs = get_prime_time_programs(
        channel_id
    )

    current = get_current_program(
        channel_id
    )

    channel_name = html.escape(
        get_channel_display_name(channel)
    )

    now_israel = datetime.now(
        timezone.utc
    ).astimezone(
        ISRAEL_TZ
    )

    date_text = now_israel.strftime(
        "%d/%m/%Y"
    )

    text = (
        f"📺 <b>{channel_name}</b>\n"
        f"🌙 <b>פריים טיים</b>\n"
        f"{date_text}\n"
        f"20:00–00:00\n\n"
    )

    if not programs:
        text += (
            "אין מידע על תוכניות בפריים טיים."
        )
        return text

    for program in programs:

        start_time = format_time(
            program["start"]
        )

        stop_time = format_time(
            program["stop"]
        )

        title = html.escape(
            program["title"]
        )

        if current is program:
            text += (
                f"🔴 <b>{start_time}–{stop_time}</b>  "
                f"<b>{title}</b>\n"
            )
        else:
            text += (
                f"• {start_time}–{stop_time}  "
                f"{title}\n"
            )

    if len(text) > TELEGRAM_MESSAGE_LIMIT:

        header = (
            f"📺 <b>{channel_name}</b>\n"
            f"🌙 <b>פריים טיים</b>\n"
            f"{date_text}\n"
            f"20:00–00:00\n\n"
        )

        schedule = ""

        for program in programs:

            start_time = format_time(
                program["start"]
            )

            stop_time = format_time(
                program["stop"]
            )

            title = html.escape(
                program["title"]
            )

            marker = (
                "🔴 "
                if current is program
                else "• "
            )

            line = (
                f"{marker}"
                f"{start_time}–{stop_time}  "
                f"{title}\n"
            )

            if (
                len(header)
                + len(schedule)
                + len(line)
                + 20
                > TELEGRAM_MESSAGE_LIMIT
            ):
                break

            schedule += line

        text = (
            header
            + schedule
            + "..."
        )

    return text


# ============================================================
# CHANNEL KEYBOARD
# ============================================================

def channel_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📅 מה שודר היום",
                    callback_data="today"
                ),
                InlineKeyboardButton(
                    "🌙 פריים טיים",
                    callback_data="prime_time"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔎 חיפוש תוכנית",
                    callback_data="search_program"
                )
            ],
            [
                InlineKeyboardButton(
                    "📺 ערוץ אחר",
                    callback_data="search_channel"
                )
            ],
        ]
    )


# ============================================================
# TODAY KEYBOARD
# ============================================================

def today_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⬅️ חזרה לערוץ",
                    callback_data="back_channel"
                ),
                InlineKeyboardButton(
                    "🌙 פריים טיים",
                    callback_data="prime_time"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔎 חיפוש תוכנית",
                    callback_data="search_program"
                )
            ],
            [
                InlineKeyboardButton(
                    "📺 ערוץ אחר",
                    callback_data="search_channel"
                )
            ],
        ]
    )


# ============================================================
# PRIME TIME KEYBOARD
# ============================================================

def prime_time_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⬅️ חזרה לערוץ",
                    callback_data="back_channel"
                ),
                InlineKeyboardButton(
                    "📅 מה שודר היום",
                    callback_data="today"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔎 חיפוש תוכנית",
                    callback_data="search_program"
                )
            ],
            [
                InlineKeyboardButton(
                    "📺 ערוץ אחר",
                    callback_data="search_channel"
                )
            ],
        ]
    )


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    context.user_data["state"] = (
        "searching_channel"
    )

    context.user_data.pop(
        "selected_channel_id",
        None
    )

    await update.message.reply_text(
        "📺 <b>מדריך הטלוויזיה</b>\n\n"
        "חפש ערוץ לפי שם או מספר.\n\n"
        "לדוגמה:\n"
        "• 12\n"
        "• קשת\n"
        "• ערוץ 13\n"
        "• ספורט 5\n"
        "• בית+\n\n"
        "🔎 כתוב את שם הערוץ:",
        parse_mode="HTML"
    )


# ============================================================
# CHANNEL SEARCH
# ============================================================

async def handle_channel_search(
    update,
    context
):

    text = (
        update.message.text or ""
    ).strip()

    results = find_channels(
        text
    )

    if not results:

        await update.message.reply_text(
            "❌ לא מצאתי ערוץ מתאים.\n\n"
            "נסה שם אחר או מספר ערוץ."
        )

        return

    buttons = []

    for channel in results:

        channel_id = channel["id"]

        display_name = (
            get_channel_display_name(
                channel
            )
        )

        buttons.append(
            [
                InlineKeyboardButton(
                    f"📺 {display_name}",
                    callback_data=(
                        f"channel:{channel_id}"
                    )
                )
            ]
        )

    await update.message.reply_text(
        "📺 <b>מצאתי את הערוצים הבאים:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            buttons
        )
    )


# ============================================================
# PROGRAM SEARCH
# ============================================================

async def handle_program_search(
    update,
    context
):

    channel_id = context.user_data.get(
        "selected_channel_id"
    )

    if not channel_id:

        context.user_data["state"] = (
            "searching_channel"
        )

        await update.message.reply_text(
            "📺 קודם בחר ערוץ."
        )

        return

    query = (
        update.message.text or ""
    ).strip()

    programs = search_programs(
        channel_id,
        query
    )

    if not programs:

        await update.message.reply_text(
            "❌ לא מצאתי תוכנית כזאת בערוץ.\n\n"
            "נסה לחפש בשם אחר."
        )

        return

    context.user_data[
        "program_search_results"
    ] = programs

    buttons = []

    for index, program in enumerate(
        programs
    ):

        start = format_time(
            program["start"]
        )

        title = program["title"]

        label = (
            f"{start} • {title}"
        )

        if len(label) > 55:

            label = (
                label[:52]
                + "..."
            )

        buttons.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=(
                        f"program_result:{index}"
                    )
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ חזרה לערוץ",
                callback_data="back_channel"
            )
        ]
    )

    await update.message.reply_text(
        "🔎 <b>תוצאות החיפוש:</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            buttons
        )
    )

    context.user_data["state"] = (
        "searching_program"
    )


# ============================================================
# MESSAGE HANDLER
# ============================================================

async def handle_message(
    update,
    context
):

    if not update.message:
        return

    print(
        "MESSAGE RECEIVED:",
        repr(update.message.text)
    )

    state = context.user_data.get(
        "state"
    )

    # Program search
    if state == "searching_program":

        await handle_program_search(
            update,
            context
        )

        return

    # Channel search
    await handle_channel_search(
        update,
        context
    )


# ============================================================
# CALLBACK HANDLER
# ============================================================

async def handle_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    data = query.data

    # ========================================================
    # SEARCH CHANNEL
    # ========================================================

    if data == "search_channel":

        context.user_data["state"] = (
            "searching_channel"
        )

        await query.edit_message_text(
            "📺 <b>חיפוש ערוץ</b>\n\n"
            "כתוב את שם הערוץ או המספר שלו:",
            parse_mode="HTML"
        )

        return

    # ========================================================
    # SEARCH PROGRAM
    # ========================================================

    if data == "search_program":

        channel_id = context.user_data.get(
            "selected_channel_id"
        )

        if not channel_id:

            context.user_data["state"] = (
                "searching_channel"
            )

            await query.edit_message_text(
                "📺 קודם בחר ערוץ."
            )

            return

        context.user_data["state"] = (
            "searching_program"
        )

        await query.edit_message_text(
            "🔎 <b>חיפוש תוכנית</b>\n\n"
            "כתוב את שם התוכנית שאתה מחפש:",
            parse_mode="HTML"
        )

        return

    # ========================================================
    # BACK TO CHANNEL
    # ========================================================

    if data == "back_channel":

        channel_id = context.user_data.get(
            "selected_channel_id"
        )

        if not channel_id:
            return

        context.user_data["state"] = (
            "channel_selected"
        )

        text = build_channel_screen(
            channel_id
        )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=channel_keyboard()
        )

        return

    # ========================================================
    # CHANNEL SELECTION
    # ========================================================

    if data.startswith(
        "channel:"
    ):

        channel_id = data.split(
            ":",
            1
        )[1]

        if channel_id not in CHANNEL_BY_ID:

            await query.edit_message_text(
                "❌ הערוץ לא נמצא."
            )

            return

        context.user_data[
            "selected_channel_id"
        ] = channel_id

        context.user_data["state"] = (
            "channel_selected"
        )

        text = build_channel_screen(
            channel_id
        )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=channel_keyboard()
        )

        return

    # ========================================================
    # TODAY
    # ========================================================

    if data == "today":

        channel_id = context.user_data.get(
            "selected_channel_id"
        )

        if not channel_id:
            return

        # Uses cached EPG unless
        # the 48-hour cache expired.
        load_epg()

        text = build_today_screen(
            channel_id
        )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=today_keyboard()
        )

        return

    # ========================================================
    # PRIME TIME
    # ========================================================

    if data == "prime_time":

        channel_id = context.user_data.get(
            "selected_channel_id"
        )

        if not channel_id:
            return

        load_epg()

        text = build_prime_time_screen(
            channel_id
        )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=prime_time_keyboard()
        )

        return

    # ========================================================
    # PROGRAM SEARCH RESULT
    # ========================================================

    if data.startswith(
        "program_result:"
    ):

        try:

            index = int(
                data.split(
                    ":",
                    1
                )[1]
            )

        except ValueError:

            return

        programs = context.user_data.get(
            "program_search_results",
            []
        )

        if (
            index < 0
            or index >= len(programs)
        ):

            return

        program = programs[index]

        title = program["title"]

        start = format_time(
            program["start"]
        )

        stop = format_time(
            program["stop"]
        )

        text = (
            f"📺 <b>תוכנית</b>\n\n"
            f"🎬 <b>{title}</b>\n\n"
            f"🕐 {start} - {stop}"
        )

        description = (
            program.get(
                "description",
                ""
            ).strip()
        )

        if description:

            if len(description) > 500:

                description = (
                    description[:497]
                    + "..."
                )

            text += (
                f"\n\n"
                f"📝 {description}"
            )

        buttons = [
            [
                InlineKeyboardButton(
                    "⬅️ חזרה לתוצאות",
                    callback_data=(
                        "back_program_results"
                    )
                )
            ],
            [
                InlineKeyboardButton(
                    "📺 חזרה לערוץ",
                    callback_data=(
                        "back_channel"
                    )
                )
            ],
        ]

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                buttons
            )
        )

        return

    # ========================================================
    # BACK TO PROGRAM RESULTS
    # ========================================================

    if data == "back_program_results":

        programs = context.user_data.get(
            "program_search_results",
            []
        )

        if not programs:
            return

        buttons = []

        for index, program in enumerate(
            programs[:20]
        ):

            start = format_time(
                program["start"]
            )

            title = program["title"]

            label = (
                f"{start} • {title}"
            )

            if len(label) > 55:

                label = (
                    label[:52]
                    + "..."
                )

            buttons.append(
                [
                    InlineKeyboardButton(
                        label,
                        callback_data=(
                            f"program_result:{index}"
                        )
                    )
                ]
            )

        buttons.append(
            [
                InlineKeyboardButton(
                    "⬅️ חזרה לערוץ",
                    callback_data=(
                        "back_channel"
                    )
                )
            ]
        )

        await query.edit_message_text(
            "🔎 <b>תוצאות החיפוש:</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                buttons
            )
        )

        return


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context
):

    print(
        "Telegram error:",
        context.error
    )


# ============================================================
# MAIN
# ============================================================

def main():

    application = (
        Application.builder()
        .token(
            TELEGRAM_BOT_TOKEN
        )
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            handle_callback
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            handle_message
        )
    )

    application.add_error_handler(
        error_handler
    )

    # ========================================================
    # RENDER WEBHOOK
    # ========================================================

    if RENDER_EXTERNAL_URL:

        webhook_url = (
            f"{RENDER_EXTERNAL_URL}"
            f"{WEBHOOK_PATH}"
        )

        print(
            f"Starting webhook: "
            f"{webhook_url}"
        )

        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=webhook_url,
            url_path=WEBHOOK_PATH.lstrip("/")
        )

    # ========================================================
    # LOCAL POLLING
    # ========================================================

    else:

        print(
            "Starting polling mode..."
        )

        application.run_polling()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()

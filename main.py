from fastapi import FastAPI, Form
from fastapi.responses import PlainTextResponse, RedirectResponse, HTMLResponse, JSONResponse
import anthropic
import os
import json
import base64
import httpx
from datetime import datetime, timedelta
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
from tavily import TavilyClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
import pytz

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
import spotipy
from spotipy.oauth2 import SpotifyOAuth

app = FastAPI()
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
twilio_client = TwilioClient(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
scheduler = AsyncIOScheduler()

TWILIO_FROM = "whatsapp:+14155238886"
ISRAEL_TZ = pytz.timezone("Asia/Jerusalem")
SCOPES = ["https://www.googleapis.com/auth/calendar"]
REDIRECT_URI = "https://web-production-d9ba5e.up.railway.app/auth/callback"
TOKEN_FILE = "token.json"

conversations = {}
user_tasks = {}
_oauth_flow = None
pending_spotify = {}  # user_phone -> list of {name, artist, uri}

DATA_DIR = "/data" if os.path.isdir("/data") else "/tmp"
SCHEDULE_FILE = f"{DATA_DIR}/schedule.json"
MY_WHATSAPP = os.environ.get("MY_WHATSAPP", "")
DAY_NAMES = {0: "שני", 1: "שלישי", 2: "רביעי", 3: "חמישי", 4: "שישי", 5: "שבת", 6: "ראשון"}
os.makedirs(DATA_DIR, exist_ok=True)


def load_schedules():
    if os.path.exists(SCHEDULE_FILE):
        with open(SCHEDULE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"מערכת_שעות": "", "סידור_עבודה": ""}


def save_schedules(data):
    with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_owner_phone() -> str:
    """Return the stored owner phone (whatsapp:+972...) or fallback to MY_WHATSAPP env."""
    schedules = load_schedules()
    stored = schedules.get("owner_phone", "")
    if stored:
        return stored
    # fallback: env variable, add prefix if missing
    env = MY_WHATSAPP
    if env and not env.startswith("whatsapp:"):
        env = f"whatsapp:{env}"
    return env


async def send_morning_brief():
    target = get_owner_phone()
    if not target:
        return

    schedules = load_schedules()
    today = datetime.now(ISRAEL_TZ)
    day_name = DAY_NAMES[today.weekday()]
    date_str = today.strftime("%d/%m/%Y")

    cal_summary = ""
    try:
        service, _ = get_calendar_service()
        if service:
            time_min = today.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            time_max = today.replace(hour=23, minute=59, second=59).isoformat()
            result = service.events().list(
                calendarId="primary",
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=20,
            ).execute()
            events = result.get("items", [])
            if events:
                lines = []
                for e in events:
                    start = e["start"].get("dateTime", e["start"].get("date", ""))
                    if "T" in start:
                        dt = datetime.fromisoformat(start).astimezone(ISRAEL_TZ)
                        time_str = dt.strftime("%H:%M")
                    else:
                        time_str = "כל היום"
                    lines.append(f"• {time_str} — {e.get('summary', 'ללא כותרת')}")
                cal_summary = "אירועי יומן גוגל היום:\n" + "\n".join(lines)
    except Exception:
        pass

    timetable = schedules.get("מערכת_שעות") or "לא הוגדרה"
    work_schedule = schedules.get("סידור_עבודה") or "לא הוגדר"

    prompt = (
        f"היום יום {day_name}, {date_str}.\n\n"
        f"מערכת השעות הקבועה שלי (לימודים/קורסים שחוזרים כל שבוע):\n{timetable}\n\n"
        f"סידור עבודה לשבוע הנוכחי:\n{work_schedule}\n\n"
        f"{cal_summary}\n\n"
        "צור הודעת בוקר מסודרת וכרונולוגית של מה שיש לי היום בלבד.\n"
        "- שעות מדויקות בסדר עולה\n"
        "- הפרד בין סוגי אירועים (עבודה / לימודים / אחר)\n"
        "- אם אין כלום היום כתוב את זה\n"
        "- סיכום קצר בסוף\n"
        "ענה בעברית בלבד, ללא מבוא מיותר."
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system="אתה עוזר אישי שמכין תקציר יומי מסודר.",
        messages=[{"role": "user", "content": prompt}]
    )

    body = f"בוקר טוב! הנה היום שלך:\n\n{response.content[0].text}"
    twilio_client.messages.create(from_=TWILIO_FROM, to=target, body=body)


# ── Google Calendar helpers ──────────────────────────────────────────────────

def get_google_creds():
    """Load credentials. Returns (creds, error_str)."""
    refresh_token = None

    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            data = json.load(f)
            refresh_token = data.get("refresh_token")

    if not refresh_token:
        refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN")

    if not refresh_token:
        return None, "GOOGLE_REFRESH_TOKEN חסר"

    try:
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.environ["GOOGLE_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
            scopes=SCOPES,
        )
        creds.refresh(Request())
        return creds, None
    except Exception as e:
        return None, str(e)


def _save_creds(creds: Credentials):
    data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
    }
    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f)


def get_calendar_service():
    creds, err = get_google_creds()
    if not creds:
        return None, err
    return build("calendar", "v3", credentials=creds), None


# ── Spotify helpers ──────────────────────────────────────────────────────────

SPOTIFY_REDIRECT_URI = "https://web-production-d9ba5e.up.railway.app/auth/spotify/callback"
SPOTIFY_SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing"


def get_spotify():
    """Return an authenticated Spotify client or None."""
    refresh_token = os.environ.get("SPOTIFY_REFRESH_TOKEN")
    if not refresh_token:
        return None, "SPOTIFY_REFRESH_TOKEN חסר"
    try:
        auth = SpotifyOAuth(
            client_id=os.environ["SPOTIFY_CLIENT_ID"],
            client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
            redirect_uri=SPOTIFY_REDIRECT_URI,
            scope=SPOTIFY_SCOPES,
        )
        token_info = auth.refresh_access_token(refresh_token)
        return spotipy.Spotify(auth=token_info["access_token"]), None
    except Exception as e:
        return None, str(e)


def spotify_currently_playing_fn() -> str:
    sp, err = get_spotify()
    if not sp:
        return f"ספוטיפיי לא מחובר ({err}). בקר ב: https://web-production-d9ba5e.up.railway.app/auth/spotify"
    try:
        current = sp.current_playback()
        if not current or not current.get("is_playing"):
            return "לא מתנגן כלום כרגע בספוטיפיי."
        item = current["item"]
        artists = ", ".join(a["name"] for a in item["artists"])
        track = item["name"]
        return f"מתנגן עכשיו: {track} — {artists}"
    except Exception as e:
        return f"שגיאה: {str(e)}"


def spotify_control_fn(action: str) -> str:
    sp, err = get_spotify()
    if not sp:
        return f"ספוטיפיי לא מחובר ({err}). בקר ב: https://web-production-d9ba5e.up.railway.app/auth/spotify"
    try:
        if action == "pause":
            sp.pause_playback()
            return "מושהה ⏸"
        elif action == "play":
            sp.start_playback()
            return "מתנגן ▶️"
        elif action == "next":
            sp.next_track()
            return "דילגתי לשיר הבא ⏭"
        elif action == "previous":
            sp.previous_track()
            return "חזרתי לשיר הקודם ⏮"
        else:
            return f"פעולה לא מוכרת: {action}"
    except Exception as e:
        return f"שגיאה: {str(e)}"


def spotify_search_and_play_fn(query: str, user_phone: str, type: str = "track") -> str:
    sp, err = get_spotify()
    if not sp:
        return f"ספוטיפיי לא מחובר ({err}). בקר ב: https://web-production-d9ba5e.up.railway.app/auth/spotify"
    try:
        results = sp.search(q=query, type=type, limit=5)
        items = results[f"{type}s"]["items"]
        if not items:
            return f"לא מצאתי '{query}' בספוטיפיי."

        if type != "track":
            # For albums/playlists just play the first result
            sp.start_playback(context_uri=items[0]["uri"])
            return f"מנגן: {items[0]['name']} ▶️"

        # Check for multiple distinct artists
        seen_artists = []
        unique_tracks = []
        for t in items:
            artist = t["artists"][0]["name"]
            if artist not in seen_artists:
                seen_artists.append(artist)
                unique_tracks.append(t)

        if len(unique_tracks) == 1:
            sp.start_playback(uris=[unique_tracks[0]["uri"]])
            return f"מנגן: {unique_tracks[0]['name']} — {unique_tracks[0]['artists'][0]['name']} ▶️"

        # Multiple artists — ask user to choose
        pending_spotify[user_phone] = [
            {"name": t["name"], "artist": t["artists"][0]["name"], "uri": t["uri"]}
            for t in unique_tracks[:5]
        ]
        lines = ["מצאתי כמה אמנים לשיר הזה, בחר מספר:"]
        for i, t in enumerate(pending_spotify[user_phone], 1):
            lines.append(f"{i}. {t['artist']} — {t['name']}")
        return "\n".join(lines)

    except Exception as e:
        return f"שגיאה: {str(e)}"


# ── OAuth endpoints ──────────────────────────────────────────────────────────

@app.get("/auth/google")
async def auth_google():
    global _oauth_flow
    _oauth_flow = Flow.from_client_config(
        {
            "web": {
                "client_id": os.environ["GOOGLE_CLIENT_ID"],
                "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                "redirect_uris": [REDIRECT_URI],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )
    auth_url, _ = _oauth_flow.authorization_url(access_type="offline", prompt="consent")
    return RedirectResponse(auth_url)


@app.get("/auth/callback")
async def auth_callback(code: str):
    global _oauth_flow
    if not _oauth_flow:
        return HTMLResponse("שגיאה: התחל מחדש מ- /auth/google")
    _oauth_flow.fetch_token(code=code)
    _save_creds(_oauth_flow.credentials)

    refresh_token = _oauth_flow.credentials.refresh_token

    return HTMLResponse(f"""
    <h2>✅ יומן גוגל חובר בהצלחה!</h2>
    <p>כדי שהחיבור יישמר גם אחרי deploy, הוסף ב-Railway את ה-env var הבא:</p>
    <p><b>שם:</b> GOOGLE_REFRESH_TOKEN</p>
    <p><b>ערך:</b></p>
    <pre style="background:#f0f0f0;padding:10px;word-break:break-all">{refresh_token}</pre>
    <p>העתק את הערך הזה ושמור אותו.</p>
    """)


# ── Spotify OAuth endpoints ──────────────────────────────────────────────────

@app.get("/auth/spotify")
async def auth_spotify():
    auth = SpotifyOAuth(
        client_id=os.environ["SPOTIFY_CLIENT_ID"],
        client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope=SPOTIFY_SCOPES,
    )
    return RedirectResponse(auth.get_authorize_url())


@app.get("/auth/spotify/callback")
async def auth_spotify_callback(code: str):
    auth = SpotifyOAuth(
        client_id=os.environ["SPOTIFY_CLIENT_ID"],
        client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope=SPOTIFY_SCOPES,
    )
    token_info = auth.get_access_token(code, as_dict=True)
    refresh_token = token_info["refresh_token"]
    return HTMLResponse(f"""
    <h2>✅ ספוטיפיי חובר בהצלחה!</h2>
    <p>הוסף ב-Railway את ה-env var הבא:</p>
    <p><b>שם:</b> SPOTIFY_REFRESH_TOKEN</p>
    <p><b>ערך:</b></p>
    <pre style="background:#f0f0f0;padding:10px;word-break:break-all">{refresh_token}</pre>
    """)


# ── Calendar tools ───────────────────────────────────────────────────────────

def list_calendar_events(days: int = 7) -> str:
    service, err = get_calendar_service()
    if not service:
        return f"יומן גוגל לא מחובר ({err}). בקר ב: https://web-production-d9ba5e.up.railway.app/auth/google"

    now = datetime.now(ISRAEL_TZ)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=days)).isoformat()

    result = service.events().list(
        calendarId="primary",
        timeMin=time_min,
        timeMax=time_max,
        singleEvents=True,
        orderBy="startTime",
        maxResults=10,
    ).execute()

    events = result.get("items", [])
    if not events:
        return f"אין אירועים ב-{days} הימים הקרובים."

    lines = []
    for e in events:
        start = e["start"].get("dateTime", e["start"].get("date", ""))
        if "T" in start:
            dt = datetime.fromisoformat(start).astimezone(ISRAEL_TZ)
            time_str = dt.strftime("%d/%m %H:%M")
        else:
            time_str = start
        lines.append(f"• {time_str} — {e.get('summary', 'ללא כותרת')}")

    return "\n".join(lines)


def create_calendar_event(summary: str, start_datetime: str, end_datetime: str, description: str = "") -> str:
    service, err = get_calendar_service()
    if not service:
        return f"יומן גוגל לא מחובר ({err}). בקר ב: https://web-production-d9ba5e.up.railway.app/auth/google"

    def parse_dt(s):
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = ISRAEL_TZ.localize(dt)
        return dt.isoformat()

    event = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": parse_dt(start_datetime), "timeZone": "Asia/Jerusalem"},
        "end": {"dateTime": parse_dt(end_datetime), "timeZone": "Asia/Jerusalem"},
    }

    created = service.events().insert(calendarId="primary", body=event).execute()
    return f"נוצר אירוע: {created.get('summary')} ✅"


# ── Tools definition ─────────────────────────────────────────────────────────

tools = [
    {
        "name": "search_web",
        "description": "חיפוש מידע עדכני באינטרנט. השתמש כאשר המשתמש שואל על חדשות, עובדות, מחירים, או כל דבר שדורש מידע עדכני.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "שאילתת החיפוש"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "set_reminder",
        "description": "קביעת תזכורת שתישלח כהודעת וואטסאפ בשעה מסוימת.",
        "input_schema": {
            "type": "object",
            "properties": {
                "datetime_str": {
                    "type": "string",
                    "description": "תאריך ושעה בפורמט ISO (YYYY-MM-DDTHH:MM:SS) בשעון ישראל"
                },
                "message": {"type": "string", "description": "תוכן התזכורת"}
            },
            "required": ["datetime_str", "message"]
        }
    },
    {
        "name": "manage_tasks",
        "description": "ניהול רשימת משימות — הוספה, צפייה, סימון כהושלם, מחיקה.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "complete", "delete"],
                    "description": "הפעולה: add/list/complete/delete"
                },
                "task": {"type": "string", "description": "תיאור המשימה (לפעולת add)"},
                "task_id": {"type": "integer", "description": "מזהה המשימה (לפעולות complete/delete)"}
            },
            "required": ["action"]
        }
    },
    {
        "name": "list_calendar_events",
        "description": "הצגת אירועים קרובים מיומן גוגל של המשתמש.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "כמה ימים קדימה להציג (ברירת מחדל: 7)"
                }
            },
            "required": []
        }
    },
    {
        "name": "create_calendar_event",
        "description": "יצירת אירוע חדש ביומן גוגל.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "כותרת האירוע"},
                "start_datetime": {
                    "type": "string",
                    "description": "תאריך ושעת התחלה בפורמט ISO (YYYY-MM-DDTHH:MM:SS) בשעון ישראל"
                },
                "end_datetime": {
                    "type": "string",
                    "description": "תאריך ושעת סיום בפורמט ISO (YYYY-MM-DDTHH:MM:SS) בשעון ישראל"
                },
                "description": {"type": "string", "description": "תיאור האירוע (אופציונלי)"}
            },
            "required": ["summary", "start_datetime", "end_datetime"]
        }
    },
    {
        "name": "spotify_currently_playing",
        "description": "מה מתנגן עכשיו בספוטיפיי.",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "spotify_control",
        "description": "שליטה על ניגון ספוטיפיי: השהייה, המשך, דילוג קדימה/אחורה.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["play", "pause", "next", "previous"],
                    "description": "הפעולה"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "spotify_search_and_play",
        "description": "חיפוש שיר, אמן, או פלייליסט בספוטיפיי והשמעתו.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "שם השיר / האמן / הפלייליסט"},
                "type": {
                    "type": "string",
                    "enum": ["track", "artist", "playlist", "album"],
                    "description": "סוג החיפוש (ברירת מחדל: track)"
                }
            },
            "required": ["query"]
        }
    }
]


# ── Tool runner ──────────────────────────────────────────────────────────────

def search_web(query: str) -> str:
    try:
        tavily = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
        response = tavily.search(query=query, max_results=5)
        results = response.get("results", [])
        if not results:
            return "לא נמצאו תוצאות."
        parts = []
        for r in results[:4]:
            parts.append(f"• {r['title']}\n{r['content'][:250]}")
        return "\n\n".join(parts)
    except Exception as e:
        return f"שגיאה בחיפוש: {str(e)}"


def manage_tasks_fn(user_phone: str, action: str, task: str = None, task_id: int = None) -> str:
    if user_phone not in user_tasks:
        user_tasks[user_phone] = []

    if action == "add":
        new_id = (max((t["id"] for t in user_tasks[user_phone]), default=0)) + 1
        user_tasks[user_phone].append({"id": new_id, "task": task, "done": False})
        return f"נוסף ✅: {task}"

    elif action == "list":
        if not user_tasks[user_phone]:
            return "אין משימות."
        lines = []
        for t in user_tasks[user_phone]:
            mark = "✅" if t["done"] else "⬜"
            lines.append(f"{mark} {t['id']}. {t['task']}")
        return "\n".join(lines)

    elif action == "complete":
        for t in user_tasks[user_phone]:
            if t["id"] == task_id:
                t["done"] = True
                return f"סומנה: {t['task']} ✅"
        return "משימה לא נמצאה."

    elif action == "delete":
        before = len(user_tasks[user_phone])
        user_tasks[user_phone] = [t for t in user_tasks[user_phone] if t["id"] != task_id]
        return "נמחקה." if len(user_tasks[user_phone]) < before else "לא נמצאה."


async def send_reminder_msg(user_phone: str, message: str):
    twilio_client.messages.create(
        from_=TWILIO_FROM,
        to=f"whatsapp:{user_phone}",
        body=f"⏰ תזכורת: {message}"
    )


def set_reminder_fn(user_phone: str, datetime_str: str, message: str) -> str:
    try:
        dt = datetime.fromisoformat(datetime_str)
        if dt.tzinfo is None:
            dt = ISRAEL_TZ.localize(dt)
        scheduler.add_job(
            send_reminder_msg,
            DateTrigger(run_date=dt),
            args=[user_phone, message]
        )
        return f"תזכורת נקבעה ל-{dt.strftime('%d/%m/%Y %H:%M')} ✅"
    except Exception as e:
        return f"שגיאה: {str(e)}"


def run_tool(name: str, inp: dict, user_phone: str) -> str:
    if name == "search_web":
        return search_web(inp["query"])
    elif name == "set_reminder":
        return set_reminder_fn(user_phone, inp["datetime_str"], inp["message"])
    elif name == "manage_tasks":
        return manage_tasks_fn(user_phone, inp["action"], inp.get("task"), inp.get("task_id"))
    elif name == "list_calendar_events":
        return list_calendar_events(inp.get("days", 7))
    elif name == "create_calendar_event":
        return create_calendar_event(
            inp["summary"], inp["start_datetime"], inp["end_datetime"], inp.get("description", "")
        )
    elif name == "spotify_currently_playing":
        return spotify_currently_playing_fn()
    elif name == "spotify_control":
        return spotify_control_fn(inp["action"])
    elif name == "spotify_search_and_play":
        return spotify_search_and_play_fn(inp["query"], user_phone, inp.get("type", "track"))
    return "כלי לא מוכר"


# ── App lifecycle ────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    scheduler.start()
    scheduler.add_job(send_morning_brief, "cron", hour=6, minute=0, timezone=ISRAEL_TZ)


# ── Webhook ──────────────────────────────────────────────────────────────────

async def fetch_media_as_base64(url: str) -> tuple[str, str]:
    """Download Twilio media and return (base64_data, media_type)."""
    auth = (os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
    async with httpx.AsyncClient() as http:
        resp = await http.get(url, auth=auth, follow_redirects=True)
        resp.raise_for_status()
        media_type = resp.headers.get("content-type", "image/jpeg").split(";")[0]
        return base64.standard_b64encode(resp.content).decode(), media_type


@app.post("/webhook")
async def webhook(
    From: str = Form(...),
    Body: str = Form(""),
    NumMedia: int = Form(0),
    MediaUrl0: str = Form(None),
    MediaContentType0: str = Form(None),
):
    user_phone = From.replace("whatsapp:", "")
    body_stripped = Body.strip()

    # ── Save owner phone on first contact ────────────────────────────────────
    _sched_data = load_schedules()
    if not _sched_data.get("owner_phone"):
        _sched_data["owner_phone"] = From
        save_schedules(_sched_data)
    # ─────────────────────────────────────────────────────────────────────────

    # ── Schedule commands ─────────────────────────────────────────────────────
    if body_stripped.startswith("מערכת שעות:"):
        content = body_stripped[len("מערכת שעות:"):].strip()
        data = load_schedules()
        data["מערכת_שעות"] = content
        save_schedules(data)
        resp = MessagingResponse()
        resp.message("מערכת השעות נשמרה. כל יום ב-06:00 אשלח לך סיכום יומי.")
        return PlainTextResponse(str(resp), media_type="application/xml")

    if body_stripped.startswith("סידור עבודה:"):
        content = body_stripped[len("סידור עבודה:"):].strip()
        data = load_schedules()
        data["סידור_עבודה"] = content
        save_schedules(data)
        resp = MessagingResponse()
        resp.message("סידור העבודה נשמר.")
        return PlainTextResponse(str(resp), media_type="application/xml")
    # ─────────────────────────────────────────────────────────────────────────

    # ── Spotify artist selection ──────────────────────────────────────────────
    if user_phone in pending_spotify and Body.strip().isdigit():
        idx = int(Body.strip()) - 1
        options = pending_spotify[user_phone]
        if 0 <= idx < len(options):
            selected = options[idx]
            del pending_spotify[user_phone]
            sp, err = get_spotify()
            if sp:
                try:
                    sp.start_playback(uris=[selected["uri"]])
                    reply = f"מנגן: {selected['name']} — {selected['artist']} ▶️"
                except Exception as e:
                    reply = f"שגיאה בהפעלה: {str(e)}"
            else:
                reply = f"ספוטיפיי לא מחובר: {err}"
            resp = MessagingResponse()
            resp.message(reply)
            return PlainTextResponse(str(resp), media_type="application/xml")
    # ─────────────────────────────────────────────────────────────────────────

    if From not in conversations:
        conversations[From] = []

    now = datetime.now(ISRAEL_TZ).strftime("%d/%m/%Y %H:%M")
    schedules = load_schedules()
    schedule_context = ""
    if schedules.get("מערכת_שעות") or schedules.get("סידור_עבודה"):
        schedule_context = (
            f"\nמערכת השעות הקבועה של אלרואי:\n{schedules.get('מערכת_שעות', 'לא הוגדרה')}"
            f"\nסידור עבודה שבועי:\n{schedules.get('סידור_עבודה', 'לא הוגדר')}"
        )
    system_prompt = (
        f"אתה סוכן אישי של אלרואי מאיר. ענה תמיד בעברית, בקצרה ולעניין. "
        f"יש לך כלים: חיפוש אינטרנט, תזכורות, ניהול משימות, יומן גוגל (צפייה ויצירת אירועים). "
        f"אתה יכול לראות תמונות ולקרוא קבצי PDF. "
        f"אתה יכול לשלוט על ספוטיפיי: לנגן, להשהות, לדלג, ולחפש שירים. "
        f"השעה עכשיו: {now} (ישראל).{schedule_context}"
    )

    # Build user message content (text + optional media)
    if NumMedia > 0 and MediaUrl0:
        try:
            file_data, file_type = await fetch_media_as_base64(MediaUrl0)
            user_text = Body if Body else None

            if file_type.startswith("image/"):
                media_block = {
                    "type": "image",
                    "source": {"type": "base64", "media_type": file_type, "data": file_data},
                }
                default_text = "מה יש בתמונה?"
            elif file_type == "application/pdf":
                media_block = {
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": file_data},
                }
                default_text = "תסכם את תוכן הקובץ."
            else:
                media_block = None
                user_text = user_text or f"שלחת קובץ מסוג {file_type} — אני לא יכול לקרוא סוג קובץ זה כרגע."

            if media_block:
                user_content = [
                    media_block,
                    {"type": "text", "text": user_text or default_text},
                ]
            else:
                user_content = user_text

        except Exception as e:
            user_content = f"שגיאה בטעינת הקובץ: {str(e)}"
    else:
        user_content = Body or "שלום"  # fallback for empty body

    # Store only text in history (images/PDFs are too large to keep)
    if isinstance(user_content, list):
        text_for_history = next((b["text"] for b in user_content if b.get("type") == "text"), "[מדיה]")
        conversations[From].append({"role": "user", "content": text_for_history})
    else:
        conversations[From].append({"role": "user", "content": user_content})

    if len(conversations[From]) > 20:
        conversations[From] = conversations[From][-20:]

    # Build messages — history (text only) + current message (may include media)
    history = [{"role": m["role"], "content": m["content"]}
               for m in conversations[From][:-1]
               if isinstance(m.get("content"), str) and m.get("content")]

    # Ensure alternating roles (API requirement): drop leading assistant messages
    while history and history[0]["role"] == "assistant":
        history.pop(0)

    messages = history + [{"role": "user", "content": user_content}]

    import sys
    print(f"[WEBHOOK] user={user_phone} body={repr(Body[:50])} media={NumMedia}", file=sys.stderr, flush=True)
    print(f"[WEBHOOK] messages count={len(messages)}", file=sys.stderr, flush=True)

    reply = ""
    max_iterations = 4
    iterations = 0
    try:
        while iterations < max_iterations:
            iterations += 1
            print(f"[CLAUDE] iteration {iterations}, messages={len(messages)}", file=sys.stderr, flush=True)
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=system_prompt,
                tools=tools,
                messages=messages
            )
            print(f"[CLAUDE] stop_reason={response.stop_reason}", file=sys.stderr, flush=True)

            if response.stop_reason == "end_turn":
                reply = next((b.text for b in response.content if hasattr(b, "text")), "")
                break

            if response.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": response.content})
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        print(f"[TOOL] calling {block.name}", file=sys.stderr, flush=True)
                        result = run_tool(block.name, block.input, user_phone)
                        print(f"[TOOL] result={repr(result[:80])}", file=sys.stderr, flush=True)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result
                        })
                messages.append({"role": "user", "content": tool_results})
            else:
                break
    except Exception as e:
        import traceback
        print(f"[ERROR] {traceback.format_exc()}", file=sys.stderr, flush=True)
        reply = f"שגיאה: {str(e)}"

    print(f"[REPLY] {repr(reply[:100])}", file=sys.stderr, flush=True)
    if not reply:
        reply = "מצטער, משהו השתבש. נסה שוב."

    conversations[From].append({"role": "assistant", "content": reply})

    if len(reply) > 1500:
        reply = reply[:1497] + "..."

    resp = MessagingResponse()
    resp.message(reply)
    return PlainTextResponse(str(resp), media_type="application/xml")


@app.get("/morning-test")
async def morning_test():
    import traceback
    target = get_owner_phone()
    if not target:
        return JSONResponse(
            {"error": "no phone stored yet — send any WhatsApp message to the bot first"},
            status_code=400
        )
    try:
        await send_morning_brief()
        return JSONResponse({"status": "sent", "to": target})
    except Exception as e:
        return JSONResponse({"error": str(e), "trace": traceback.format_exc()}, status_code=500)


@app.get("/")
async def health():
    return {"status": "ok"}

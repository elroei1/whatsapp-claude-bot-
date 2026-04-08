from fastapi import FastAPI, Form
from fastapi.responses import PlainTextResponse, RedirectResponse, HTMLResponse
import anthropic
import os
import json
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


# ── Google Calendar helpers ──────────────────────────────────────────────────

def get_google_creds() -> Credentials | None:
    """Load credentials from token.json or GOOGLE_TOKEN env var."""
    token_data = None

    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            token_data = json.load(f)
    elif os.environ.get("GOOGLE_TOKEN"):
        token_data = json.loads(os.environ["GOOGLE_TOKEN"])

    if not token_data:
        return None

    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        scopes=SCOPES,
    )

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_creds(creds)

    return creds


def _save_creds(creds: Credentials):
    data = {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
    }
    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f)


def get_calendar_service():
    creds = get_google_creds()
    if not creds:
        return None
    return build("calendar", "v3", credentials=creds)


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

    token_json = json.dumps({
        "token": _oauth_flow.credentials.token,
        "refresh_token": _oauth_flow.credentials.refresh_token,
    })

    return HTMLResponse(f"""
    <h2>✅ יומן גוגל חובר בהצלחה!</h2>
    <p>כדי שהחיבור יישמר גם אחרי deploy, הוסף ב-Railway את ה-env var הבא:</p>
    <p><b>GOOGLE_TOKEN</b></p>
    <pre style="background:#f0f0f0;padding:10px;word-break:break-all">{token_json}</pre>
    <p>העתק את הערך הזה ושמור אותו.</p>
    """)


# ── Calendar tools ───────────────────────────────────────────────────────────

def list_calendar_events(days: int = 7) -> str:
    service = get_calendar_service()
    if not service:
        return "יומן גוגל לא מחובר. בקר ב: https://web-production-d9ba5e.up.railway.app/auth/google"

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
    service = get_calendar_service()
    if not service:
        return "יומן גוגל לא מחובר. בקר ב: https://web-production-d9ba5e.up.railway.app/auth/google"

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
    return "כלי לא מוכר"


# ── App lifecycle ────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    scheduler.start()
    # If GOOGLE_TOKEN env var exists, write to file so get_google_creds() can load it
    if os.environ.get("GOOGLE_TOKEN") and not os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "w") as f:
            f.write(os.environ["GOOGLE_TOKEN"])


# ── Webhook ──────────────────────────────────────────────────────────────────

@app.post("/webhook")
async def webhook(From: str = Form(...), Body: str = Form(...)):
    user_phone = From.replace("whatsapp:", "")

    if From not in conversations:
        conversations[From] = []

    conversations[From].append({"role": "user", "content": Body})
    if len(conversations[From]) > 20:
        conversations[From] = conversations[From][-20:]

    now = datetime.now(ISRAEL_TZ).strftime("%d/%m/%Y %H:%M")
    system_prompt = (
        f"אתה סוכן אישי של אלרואי מאיר. ענה תמיד בעברית, בקצרה ולעניין. "
        f"יש לך כלים: חיפוש אינטרנט, תזכורות, ניהול משימות, יומן גוגל (צפייה ויצירת אירועים). "
        f"השעה עכשיו: {now} (ישראל)."
    )

    messages = [{"role": m["role"], "content": m["content"]}
                for m in conversations[From]
                if isinstance(m.get("content"), str)]

    reply = ""
    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system_prompt,
            tools=tools,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            reply = next((b.text for b in response.content if hasattr(b, "text")), "")
            break

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = run_tool(block.name, block.input, user_phone)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result
                    })
            messages.append({"role": "user", "content": tool_results})
        else:
            reply = "שגיאה לא צפויה"
            break

    conversations[From].append({"role": "assistant", "content": reply})

    if len(reply) > 1500:
        reply = reply[:1497] + "..."

    resp = MessagingResponse()
    resp.message(reply)
    return PlainTextResponse(str(resp), media_type="application/xml")


@app.get("/")
async def health():
    return {"status": "ok"}

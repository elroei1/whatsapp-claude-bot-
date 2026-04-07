from fastapi import FastAPI, Form
from fastapi.responses import PlainTextResponse
import anthropic
import os
from datetime import datetime
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
from tavily import TavilyClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
import pytz

app = FastAPI()
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
twilio_client = TwilioClient(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
scheduler = AsyncIOScheduler()

TWILIO_FROM = "whatsapp:+14155238886"
ISRAEL_TZ = pytz.timezone("Asia/Jerusalem")

conversations = {}
user_tasks = {}

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
    }
]


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
    return "כלי לא מוכר"


@app.on_event("startup")
async def startup():
    scheduler.start()


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
        f"יש לך כלים: חיפוש אינטרנט, תזכורות, ניהול משימות. "
        f"השעה עכשיו: {now} (ישראל)."
    )

    # Build clean messages list (text only for history)
    messages = [{"role": m["role"], "content": m["content"]}
                for m in conversations[From]
                if isinstance(m.get("content"), str)]

    reply = ""
    # Agentic loop
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

    # WhatsApp limit ~1600 chars
    if len(reply) > 1500:
        reply = reply[:1497] + "..."

    resp = MessagingResponse()
    resp.message(reply)
    return PlainTextResponse(str(resp), media_type="application/xml")


@app.get("/")
async def health():
    return {"status": "ok"}

import os
import requests
from flask import Flask, request, jsonify
from redis import Redis

# ======== קונפיג בסיסי ========

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN env var")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

SECRET = os.getenv("SECRET", "repost-secret-123")

REDIS_URL = os.getenv("REDIS_URL")
if not REDIS_URL:
    raise RuntimeError("Missing REDIS_URL env var")

redis_client = Redis.from_url(REDIS_URL, decode_responses=True)

app = Flask(__name__)

# סטייט זמני בזיכרון – לא קריטי אם נופל
PENDING_TARGET = {}       # user_id -> target_chat_id
USER_GROUP_CHOICES = {}   # user_id -> [chat_ids לפי סדר המספור]


# ======== פונקציות עזר ל-Telegram ========

def send_message(chat_id, text):
    try:
        requests.post(
            f"{API}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=5
        )
    except Exception as e:
        print("send_message error:", e)


def forward_message(target_chat_id, from_chat_id, message_id):
    try:
        requests.post(
            f"{API}/forwardMessage",
            json={
                "chat_id": target_chat_id,
                "from_chat_id": from_chat_id,
                "message_id": message_id,
            },
            timeout=5
        )
    except Exception as e:
        print("forward_message error:", e)


# ======== Redis – שמירת קבוצות והגדרות ========

def save_group(chat_id: int, title: str):
    """
    שומר/מעדכן קבוצה/ערוץ ברשימת הקבוצות שהבוט מכיר.
    """
    redis_client.hset("repost:known_groups", str(chat_id), title)


def get_all_groups():
    """
    מחזיר dict: {chat_id_str: title}
    """
    return redis_client.hgetall("repost:known_groups")


def add_source_to_target(target_chat_id: int, source_chat_id: int):
    """
    מוסיף קבוצת מקור ליעד.
    """
    redis_client.sadd(f"repost:target_sources:{target_chat_id}", str(source_chat_id))


def remove_source_from_target(target_chat_id: int, source_chat_id: int):
    """
    מסיר קבוצת מקור מיעד.
    """
    redis_client.srem(f"repost:target_sources:{target_chat_id}", str(source_chat_id))


def toggle_source(target_chat_id: int, source_chat_id: int) -> bool:
    """
    עושה toggle לקבוצת מקור:
    - אם לא היתה פעילה → מוסיף ומחזיר True
    - אם היתה פעילה → מסיר ומחזיר False
    """
    key = f"repost:target_sources:{target_chat_id}"
    source_str = str(source_chat_id)

    if redis_client.sismember(key, source_str):
        redis_client.srem(key, source_str)
        return False
    else:
        redis_client.sadd(key, source_str)
        return True


def get_sources_for_target(target_chat_id: int):
    """
    מחזיר set של chat_id_str של מקורות ליעד מסוים.
    """
    return redis_client.smembers(f"repost:target_sources:{target_chat_id}")


def get_all_targets():
    """
    מחזיר רשימת כל היעדים שיש להם מפתחות target_sources.
    """
    keys = redis_client.keys("repost:target_sources:*")
    targets = []
    for k in keys:
        try:
            tid = int(k.split(":")[-1])
            targets.append(tid)
        except ValueError:
            continue
    return targets


def find_targets_for_source(source_chat_id: int):
    """
    מחזיר רשימת כל היעדים שקבעו את source_chat_id כמקור.
    (המקור כאן הוא קבוצת הדיונים – לא הערוץ עצמו)
    """
    source_str = str(source_chat_id)
    targets = []
    for target_id in get_all_targets():
        key = f"repost:target_sources:{target_id}"
        if redis_client.sismember(key, source_str):
            targets.append(target_id)
    return targets


def build_sources_list_for_target(target_chat_id: int):
    """
    בונה רשימה מסודרת של כל הקבוצות שהבוט מכיר,
    יחד עם סימון האם הן פעילות כמקורות ליעד הנתון.

    מחזיר:
        items = [(chat_id_int, title_str, is_active_bool), ...]
    """
    all_groups = get_all_groups()  # {chat_id_str: title}
    active_sources = get_sources_for_target(target_chat_id)  # set של chat_id_str

    items = []
    for chat_id_str, title in all_groups.items():
        # לא להציג את קבוצת היעד עצמה כמקור
        if chat_id_str == str(target_chat_id):
            continue
        is_active = chat_id_str in active_sources
        try:
            cid_int = int(chat_id_str)
        except ValueError:
            continue
        items.append((cid_int, title, is_active))

    # סדר לפי שם קבוצה
    items.sort(key=lambda x: x[1])

    return items


# ======== Webhook ========

@app.route(f"/{SECRET}", methods=["POST"])
def webhook():
    # תומך גם ב-message וגם ב-channel_post (למקרה של ערוץ שהבוט בתוכו)
    update = request.get_json(force=True, silent=True)
    if not update:
        return jsonify(ok=True)

    message = update.get("message") or update.get("channel_post")
    if not message:
        return jsonify(ok=True)

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    from_user = message.get("from", {})
    user_id = from_user.get("id")
    text = message.get("text", "")

    # מידע על מי ששלח – יכול להיות ערוץ בתוך קבוצת דיונים
    sender_chat = message.get("sender_chat")
    is_from_channel_in_group = bool(sender_chat and sender_chat.get("type") == "channel")

    # ===== שמירת קבוצות שהבוט מכיר =====
    if chat_type in ("group", "supergroup", "channel"):
        title = chat.get("title", f"chat_{chat_id}")
        save_group(chat_id, title)

    # ===== אוטו forward מקבוצות דיון ליעדים =====
    #
    # כאן נכנס הטריק:
    # אנחנו מתייחסים רק להודעות שמגיעות מ-group/supergroup
    # ורק אם הן נשלחו "מטעם ערוץ" (sender_chat.type == "channel").
    #
    if chat_type in ("group", "supergroup") and "message_id" in message:
        # אלה קבוצות (כולל קבוצות דיון) שהוגדרו כמקורות
        targets = find_targets_for_source(chat_id)

        # אם אין יעדים – אין מה לעשות
        if targets:
            # אם זו לא הודעה של ערוץ בתוך קבוצת הדיון – לא מעבירים
            if not is_from_channel_in_group:
                return jsonify(ok=True)

            for target_chat_id in targets:
                if target_chat_id == chat_id:
                    continue
                forward_message(
                    target_chat_id=target_chat_id,
                    from_chat_id=chat_id,
                    message_id=message["message_id"],
                )

    # (אופציונלי) אם תרצה גם לתמוך בערוץ שהבוט אדמין בו ישירות:
    # if chat_type == "channel" and "message_id" in message:
    #     targets = find_targets_for_source(chat_id)
    #     for target_chat_id in targets:
    #         if target_chat_id == chat_id:
    #             continue
    #         forward_message(
    #             target_chat_id=target_chat_id,
    #             from_chat_id=chat_id,
    #             message_id=message["message_id"],
    #         )

    # ===== פקודת \repost / /repost בקבוצת יעד =====
    if chat_type in ("group", "supergroup") and user_id and text in ("\\repost", "/repost"):
        target_chat_id = chat_id
        target_title = chat.get("title", f"chat_{chat_id}")

        # קובע שהמשתמש הזה כרגע עורך את הקבוצה הזו כיעד
        PENDING_TARGET[user_id] = target_chat_id

        # בונה את הרשימה עבור היעד הזה
        items = build_sources_list_for_target(target_chat_id)

        if not items:
            send_message(
                user_id,
                "לא מצאתי קבוצות אחרות שהבוט מכיר.\n"
                "תצרף את הבוט לקבוצות נוספות (למשל קבוצות דיון של ערוצים), "
                "תכתוב שם הודעה אחת לפחות,\n"
                "ואז תחזור לכאן ותשלח שוב \\repost."
            )
            return jsonify(ok=True)

        # שומר את רשימת ה-chat_id לפי סדר המספור
        USER_GROUP_CHOICES[user_id] = [cid for (cid, _title, _active) in items]

        # בונה טקסט רשימה
        lines = [f"קבוצות מקור עבור: {target_title}", ""]
        for i, (cid, title, is_active) in enumerate(items, start=1):
            prefix = "✅" if is_active else "⬜️"
            lines.append(f"{i}. {prefix} {title}")

        lines.append("")
        lines.append("שלח מספר כדי להפעיל/לבטל קבוצה כמקור עבור הקבוצה הזאת (קבוצת היעד).")

        send_message(user_id, "\n".join(lines))

        return jsonify(ok=True)

    # ===== טיפול בהודעות פרטיות – בחירת מספר =====
    if chat_type == "private" and user_id in PENDING_TARGET and text:
        target_chat_id = PENDING_TARGET[user_id]
        all_groups = get_all_groups()
        target_title = all_groups.get(str(target_chat_id), f"chat_{target_chat_id}")

        choices = USER_GROUP_CHOICES.get(user_id)
        if not choices:
            # במקרה והסטייט בזיכרון נפל – נבנה מחדש מהרשימה
            items = build_sources_list_for_target(target_chat_id)
            if not items:
                send_message(user_id, "אין לי כרגע רשימת קבוצות לעבודה. תנסה שוב \\repost בקבוצת היעד.")
                return jsonify(ok=True)
            USER_GROUP_CHOICES[user_id] = [cid for (cid, _title, _active) in items]
            choices = USER_GROUP_CHOICES[user_id]

        txt = text.strip()

        # מקבלים רק מספר אחד בכל פעם
        if not txt.isdigit():
            send_message(user_id, "שלח רק מספר אחד מהרשימה (לדוגמה: 1 או 2).")
            return jsonify(ok=True)

        idx = int(txt)
        if idx < 1 or idx > len(choices):
            send_message(user_id, "מספר לא תקין, תנסה שוב.")
            return jsonify(ok=True)

        source_chat_id = choices[idx - 1]

        all_groups = get_all_groups()
        source_title = all_groups.get(str(source_chat_id), f"chat_{source_chat_id}")

        # toggle
        is_active_now = toggle_source(target_chat_id, source_chat_id)
        if is_active_now:
            send_message(
                user_id,
                f"הקבוצה '{source_title}' נוספה כמקור עבור '{target_title}'."
            )
        else:
            send_message(
                user_id,
                f"הקבוצה '{source_title}' הוסרה מרשימת המקורות של '{target_title}'."
            )

        # בונים מחדש רשימה מעודכנת ומחזירים לך
        items = build_sources_list_for_target(target_chat_id)
        USER_GROUP_CHOICES[user_id] = [cid for (cid, _title, _active) in items]

        lines = [f"קבוצות מקור עבור: {target_title}", ""]
        for i, (cid, title, is_active) in enumerate(items, start=1):
            prefix = "✅" if is_active else "⬜️"
            lines.append(f"{i}. {prefix} {title}")

        lines.append("")
        lines.append("שלח מספר נוסף כדי להפעיל/לבטל עוד קבוצה. אפשר פשוט להפסיק לענות מתי שבא לך 😊")

        send_message(user_id, "\n".join(lines))

        return jsonify(ok=True)

    return jsonify(ok=True)


if __name__ == "__main__":
    # להרצה לוקאלית – ברנדר משתמש ב-gunicorn
    app.run(host="0.0.0.0", port=8000)

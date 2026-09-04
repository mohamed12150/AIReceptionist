# scripts/sync_qhub_config.py
"""Generate config/businesses/qhub.yaml from the QHub ERP database.

Reads the incubator's live data (qualification services, membership plans,
meeting rooms, upcoming events, staff) and renders a receptionist business
config so the phone agent answers callers with real, current information.

Supports both QHub storage backends:
  - PostgreSQL (production server): set QHUB_DATABASE_URL
  - SQLite (local dev copy):        set QHUB_SQLITE_PATH

Personal/tenant values (notification email, WhatsApp number, tawatur IDs)
come from the environment too, so this script holds no private data and is
safe to publish. Required env vars (put them in .env):

    QHUB_DATABASE_URL   or  QHUB_SQLITE_PATH
    QHUB_NOTIFY_EMAIL           e.g. owner@example.com
    QHUB_WHATSAPP_PHONE         e.g. +9665XXXXXXXX
    TAWATUR_WORKSPACE_ID
    TAWATUR_WHATSAPP_ACCOUNT_ID

Re-run whenever QHub data changes:

    python scripts/sync_qhub_config.py

The DB is opened read-only; QHub can keep running while this syncs.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "config" / "businesses" / "qhub.yaml"

load_dotenv(REPO_ROOT / ".env.local")
load_dotenv(REPO_ROOT / ".env")


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"Missing required env var: {name} (set it in .env)", file=sys.stderr)
        raise SystemExit(1)
    return value


# ---------------------------------------------------------------------------
# DB access — one dict-rows interface over PostgreSQL or SQLite
# ---------------------------------------------------------------------------

def _connect():
    pg_url = os.environ.get("QHUB_DATABASE_URL", "").strip()
    if pg_url:
        import psycopg2
        import psycopg2.extras

        con = psycopg2.connect(pg_url)
        con.set_session(readonly=True)
        cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        return con, cur

    sqlite_path = os.environ.get("QHUB_SQLITE_PATH", "").strip()
    if not sqlite_path:
        print(
            "Set QHUB_DATABASE_URL (PostgreSQL) or QHUB_SQLITE_PATH (SQLite) in .env",
            file=sys.stderr,
        )
        raise SystemExit(1)
    import sqlite3

    con = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con, con.cursor()


def _rows(cur, sql: str) -> list[dict]:
    cur.execute(sql)
    return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Cross-backend value normalization
# ---------------------------------------------------------------------------

def _truthy(v) -> bool:
    """active flag: SQLite integer 0/1, Postgres boolean."""
    return bool(v)


def _iso(v) -> str:
    """date/datetime (Postgres) or ISO string (SQLite) -> ISO string."""
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v)


def _jlist(v) -> list:
    """JSON-array column: text (SQLite) or already-parsed list (Postgres jsonb)."""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    try:
        parsed = json.loads(v)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _fmt_price(v) -> str:
    if not v:
        return "بعرض سعر حسب الطلب"
    return f"{int(v):,} ريال".replace(",", "،")


# ---------------------------------------------------------------------------
# Content builders
# ---------------------------------------------------------------------------

def build_faqs(cur) -> list[dict]:
    faqs: list[dict] = []
    today = date.today().isoformat()

    # --- Supplier qualification services (the flagship offering) ---
    qual = [
        s for s in _rows(cur, "SELECT * FROM qual_services ORDER BY sort")
        if _truthy(s["active"])
    ]

    def _qual_price(s: dict) -> str:
        price = _fmt_price(s["price"])
        ends = _iso(s["discount_ends_at"])[:10]
        if s["discount_price"] and ends >= today:
            price = (
                f"{_fmt_price(s['discount_price'])} بدلاً من {_fmt_price(s['price'])} "
                f"(عرض حتى {ends})"
            )
        return price

    lines = [
        f"- {s['name']}: {_qual_price(s)} — المدة: {s['duration']}" for s in qual
    ]
    faqs.append({
        "question": "ما هي خدمات تأهيل الموردين وأسعارها؟",
        "answer": "خدمات التأهيل المتاحة حالياً:\n" + "\n".join(lines),
    })

    # One FAQ per qualification service with a compact detail card.
    for s in qual:
        desc = (s["description"] or "").split("،")[0][:180]
        faqs.append({
            "question": s["name"],
            "answer": (
                f"{desc}. الجهة: {s['entity']}. السعر: {_qual_price(s)}. "
                f"المدة المتوقعة: {s['duration']}. للتسجيل أو معرفة المتطلبات "
                "والمستندات كاملة، نأخذ بياناتك ويتواصل معك فريق التأهيل."
            ),
        })

    # --- Membership plans ---
    plans = _rows(cur, "SELECT * FROM plans ORDER BY price_monthly")
    plan_lines = [
        f"- {p['name']}: {_fmt_price(p['price_monthly'])} شهرياً — "
        f"تشمل: {'، '.join(str(f) for f in _jlist(p['features']))}"
        for p in plans
    ]
    faqs.append({
        "question": "ما هي باقات العضوية وأسعارها؟",
        "answer": "باقات العضوية في الحاضنة:\n" + "\n".join(plan_lines),
    })

    # --- Meeting rooms ---
    rooms = [r for r in _rows(cur, "SELECT * FROM rooms") if _truthy(r["active"])]
    room_lines = [
        f"- {r['name']} (تتسع {r['capacity']} شخصاً): "
        f"{_fmt_price(r['hourly_rate'])} للساعة — "
        f"التجهيزات: {'، '.join(str(a) for a in _jlist(r['amenities']))}"
        for r in rooms
    ]
    faqs.append({
        "question": "ما هي القاعات المتاحة للحجز وأسعارها؟",
        "answer": (
            "القاعات المتاحة:\n" + "\n".join(room_lines)
            + "\nالحجز يتم عبر منصة الحاضنة أو نأخذ بياناتك ويؤكد لك الفريق الحجز."
        ),
    })

    # --- Upcoming events ---
    now_iso = datetime.now(timezone.utc).isoformat()
    events = _rows(cur, "SELECT * FROM events ORDER BY starts_at")
    upcoming = [e for e in events if _iso(e["starts_at"]) >= now_iso[:len(_iso(e["starts_at"]))]]
    if upcoming:
        ev_lines = [
            f"- {e['title']}: {e['description']} — بتاريخ {_iso(e['starts_at'])[:10]} "
            f"في {e['location']}"
            for e in upcoming
        ]
        faqs.append({
            "question": "ما هي الفعاليات القادمة؟",
            "answer": "الفعاليات القادمة في الحاضنة:\n" + "\n".join(ev_lines)
                      + "\nللتسجيل نأخذ اسمك ورقمك ويصلك رابط التسجيل.",
        })

    # --- Other service categories (compact summary) ---
    services = [
        s for s in _rows(cur, "SELECT category FROM services")
        if s.get("category") and s["category"] != "تأهيل الموردين"
    ]
    if services:
        counts: dict[str, int] = {}
        for s in services:
            counts[s["category"]] = counts.get(s["category"], 0) + 1
        cat_line = "، ".join(f"{cat} ({n} خدمة)" for cat, n in counts.items())
        faqs.append({
            "question": "هل لديكم خدمات أخرى غير تأهيل الموردين؟",
            "answer": (
                f"نعم — لدينا كتالوج خدمات يشمل: {cat_line}. "
                "اذكر لي احتياجك وآخذ بياناتك ليتواصل معك الفريق بالتفاصيل والأسعار."
            ),
        })

    return faqs


def _normalize_saudi_phone(phone: str) -> str | None:
    """05XXXXXXXX -> +9665XXXXXXXX; passthrough for +… numbers."""
    p = (phone or "").strip().replace(" ", "")
    if p.startswith("+"):
        return p
    if p.startswith("05") and len(p) == 10:
        return "+966" + p[1:]
    if p.startswith("966"):
        return "+" + p
    return None


def build_routing(cur) -> list[dict]:
    """Incubator staff (users with no tenant) become transfer targets.

    Callers can then say e.g. "حولني للدعم الفني" and the transfer_call
    tool matches the routing entry by name. Numbers are managed inside
    QHub (users.phone) — update there, re-run this script.
    """
    role_desc = {
        "super_admin": "إدارة الحاضنة والقرارات العليا",
        "qhub_staff": "موظف الحاضنة — العضويات والقاعات والخدمات",
        "tech_support": "الدعم الفني للمنصة",
    }
    # Optional per-role phone overrides from the environment, e.g.
    #   QHUB_PHONE_OVERRIDES=tech_support=+966505886317;super_admin=+9665XXXXXXXX
    # Lets a deployment point a staff role at a real number without editing
    # QHub's own database (useful for demo/test data).
    overrides: dict[str, str] = {}
    for item in os.environ.get("QHUB_PHONE_OVERRIDES", "").split(";"):
        if "=" in item:
            role, phone = item.split("=", 1)
            overrides[role.strip()] = phone.strip()

    routing: list[dict] = []
    rows = _rows(cur, (
        "SELECT full_name, phone, role FROM users "
        "WHERE tenant_id IS NULL AND status = 'active'"
    ))
    for r in rows:
        raw_phone = overrides.get(r["role"]) or (r.get("phone") or "")
        number = _normalize_saudi_phone(raw_phone)
        if number is None:
            continue
        routing.append({
            "name": r["full_name"],
            "number": number,
            "description": role_desc.get(r["role"], "موظف الحاضنة"),
        })
    if not routing:
        routing.append({
            "name": "الإدارة",
            "number": "+15550000001",
            "description": "إدارة الحاضنة — placeholder، استبدله برقم حقيقي",
        })
    return routing


def build_config(cur) -> dict:
    notify_email = _require_env("QHUB_NOTIFY_EMAIL")
    whatsapp_phone = _require_env("QHUB_WHATSAPP_PHONE")
    tawatur_workspace = _require_env("TAWATUR_WORKSPACE_ID")
    tawatur_account = _require_env("TAWATUR_WHATSAPP_ACCOUNT_ID")

    return {
        "business": {
            "name": "حاضنة كيو هَب للأعمال",
            "type": "business incubator",
            "timezone": "Asia/Riyadh",
        },
        "voice": {
            "provider": "google",
            "voice_id": "Aoede",
            "model": "gemini-2.5-flash-native-audio-preview-12-2025",
        },
        "languages": {"primary": "ar", "allowed": ["ar", "en"]},
        "greeting": "حياك الله في كيو هَب، معك دانة، وش أقدر أخدمك؟",
        "personality": (
            "أنتِ دانة، موظفة استقبال حاضنة الأعمال كيو هَب في السعودية.\n"
            "\n"
            "اللهجة والأسلوب (مهم جداً):\n"
            "- تكلمي باللهجة السعودية البيضاء، مثل موظفة استقبال حقيقية: "
            "«حياك الله»، «أبشر»، «تم»، «وش تحتاج»، «على طول»، «لا يهمك»، "
            "«تأمر شي ثاني؟». لا تتكلمي بالفصحى الرسمية المتكلفة.\n"
            "- جمل قصيرة وعفوية. رد طبيعي متدفق، مو خطاب مكتوب.\n"
            "- لا تسردي قوائم طويلة أبداً في الكلام — اذكري نقطتين أو ثلاث "
            "الأنسب للمتصل، ثم اسألي: «تبغى أفصّل لك في شي منها؟».\n"
            "- الأرقام انطقيها طبيعي: «أحد عشر ألف وخمسمية ريال» مو "
            "«11500.00».\n"
            "- تفاعلي كإنسانة: «والله سؤال حلو»، «أكيد»، «ثواني أشوف لك». "
            "ويمنع نهائياً ذكر أنك ذكاء اصطناعي أو نموذج أو مساعد آلي — أنتِ "
            "دانة من فريق الاستقبال وبس.\n"
            "- نبرة دافئة هادئة وسرعة كلام طبيعية، لا متحمسة زيادة ولا "
            "روبوتية.\n"
            "\n"
            "تخصص الحاضنة: تأهيل المنشآت للتسجيل كموردين معتمدين لدى كبرى "
            "الجهات (أرامكو، سابك، نيوم، القدية، الكهرباء، الهيئة الملكية)، "
            "وشهادات ISO وسباهي، إضافة لعضويات مساحات العمل وحجز القاعات.\n"
            "أجيبي من المعلومات المذكورة في الأسئلة الشائعة فقط، ولا تختلقي "
            "أسعاراً أو مدداً أو معلومات غير مذكورة أبداً.\n"
            "أي طلب تسجيل في خدمة، أو حجز قاعة، أو عرض سعر، أو سؤال خارج "
            "معلوماتك: اطلبي اسم المتصل ورقم جواله وموضوعه، وسجليها بأداة "
            "take_message قبل إنهاء المكالمة.\n"
            "قاعدة صارمة لا استثناء فيها: يُمنع منعاً باتاً أن تقولي «تم "
            "تسجيل الرسالة» أو أي تأكيد مشابه قبل أن تستدعي فعلياً أداة "
            "take_message وتصلك نتيجتها بنجاح. الترتيب دائماً: اجمعي الاسم "
            "والرقم والطلب ← استدعي take_message ← ثم أكدي للمتصل."
        ),
        "hours": {
            "sunday": {"open": "09:00", "close": "17:00"},
            "monday": {"open": "09:00", "close": "17:00"},
            "tuesday": {"open": "09:00", "close": "17:00"},
            "wednesday": {"open": "09:00", "close": "17:00"},
            "thursday": {"open": "09:00", "close": "17:00"},
            "friday": "closed",
            "saturday": "closed",
        },
        "after_hours_message": (
            "دوام الحاضنة من الأحد إلى الخميس، من التاسعة صباحاً حتى الخامسة "
            "مساءً. أقدر آخذ رسالتك ويتواصل معك الفريق في أول يوم عمل."
        ),
        "routing": build_routing(cur),
        "faqs": build_faqs(cur),
        "messages": {
            "channels": [
                {"type": "file", "file_path": "./messages/qhub/"},
                {
                    "type": "email",
                    "to": [notify_email],
                    "include_transcript": True,
                    "include_recording_link": False,
                },
                {
                    "type": "whatsapp",
                    "provider": "tawatur",
                    "phone": whatsapp_phone,
                    "workspace_id": tawatur_workspace,
                    "whatsapp_account_id": tawatur_account,
                },
            ],
        },
        "transcripts": {
            "enabled": True,
            "storage": {"type": "local", "path": "./transcripts/qhub/"},
            "formats": ["json", "markdown"],
        },
        "email": {
            "from": "onboarding@resend.dev",
            "sender": {"type": "resend", "resend": {"api_key": "${RESEND_API_KEY}"}},
            "triggers": {"on_message": True, "on_call_end": True},
            "summary": {"enabled": False},
        },
    }


def main() -> int:
    con, cur = _connect()
    try:
        config = build_config(cur)
    finally:
        con.close()

    header = (
        "# config/businesses/qhub.yaml — GENERATED FILE, do not edit by hand.\n"
        f"# Generated by scripts/sync_qhub_config.py at {datetime.now().isoformat(timespec='seconds')}\n"
        "# Source: QHub ERP database. Re-run the script after data changes.\n"
    )
    body = yaml.safe_dump(
        config, allow_unicode=True, sort_keys=False, width=1000,
    )
    OUT_PATH.write_text(header + body, encoding="utf-8")
    print(f"Wrote {OUT_PATH} ({len(config['faqs'])} FAQs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

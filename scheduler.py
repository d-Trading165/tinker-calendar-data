"""Cloud reminder scheduler for Tinker Calendar — runs in GitHub Actions.

Reads data.json (the board, incl. settings), pre-schedules the next 36 hours
of reminders on ntfy using server-side delayed delivery, and records what it
scheduled in registry.json so re-runs never duplicate. Because delivery is
delayed server-side by ntfy, pushes arrive on the phone even though nothing
is running anywhere at fire time — no PC, no runner.

Runs twice a day on cron (36h horizon > 12h cadence → full coverage) and on
every push that changes data.json (so edits re-arm quickly).
"""

import json
import os
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

HORIZON = timedelta(hours=36)
MIN_DELAY = timedelta(seconds=30)   # ntfy minimum delay is 10s; stay clear of it
REGISTRY = "registry.json"


def fmt_time(t):
    if not t:
        return ""
    return t[1:] if t.startswith("0") else t


def post(server, topic, title, body, at_ts, click=None):
    payload = {"topic": topic, "title": title, "message": body,
               "tags": ["calendar"], "delay": str(at_ts)}
    if click:
        payload["click"] = click
    req = urllib.request.Request(
        server.rstrip("/") + "/",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print("  post failed:", e)
        return False


def targets(data, s, now, tz, lead):
    """Yield (sig, fire_at, title, body) for items + digests in the window."""
    lead_min = int(lead.total_seconds() // 60)
    days = {(now + timedelta(days=k)).date() for k in range(3)}

    for it in data.get("items", []):
        if it.get("status") == "Done" or not it.get("date") or not it.get("time"):
            continue
        try:
            due = datetime.strptime(f"{it['date']} {it['time']}",
                                    "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        except ValueError:
            continue
        if due.date() not in days:
            continue
        rng = fmt_time(it["time"])
        if it.get("end"):
            rng += "–" + fmt_time(it["end"])
        body = (f"Starts at {rng} — in {lead_min} min" if lead_min
                else f"Starting now ({rng})")
        tag = (it.get("tag") or "").strip()
        if tag:
            body += f" · {tag}"
        yield (f"{it['id']}|{it['date']}|{it['time']}|{lead_min}|item",
               due - lead, it.get("name", "Reminder"), body)

    dt_s = (s.get("digest_time") or "").strip()
    if not dt_s:
        return
    try:
        dh, dm = (int(x) for x in dt_s.split(":"))
    except ValueError:
        return
    for k in range(3):
        day = (now + timedelta(days=k)).date()
        day_iso = day.isoformat()
        day_items = [i for i in data.get("items", [])
                     if i.get("date") == day_iso and i.get("status") != "Done"]
        if not day_items:
            continue
        day_items.sort(key=lambda i: i.get("time") or "~")
        lines = []
        for i in day_items[:8]:
            t = fmt_time(i.get("time") or "")
            if t and i.get("end"):
                t += "–" + fmt_time(i["end"])
            tg = (i.get("tag") or "").strip()
            lines.append((t + "  " if t else "") + i.get("name", "")
                         + (f"  [{tg}]" if tg else ""))
        if len(day_items) > 8:
            lines.append(f"+{len(day_items) - 8} more")
        fire = datetime(day.year, day.month, day.day, dh, dm, tzinfo=tz)
        n = len(day_items)
        yield (f"digest|{day_iso}|{dt_s}|digest", fire,
               f"Today: {n} thing{'s' if n != 1 else ''} on the board",
               "\n".join(lines))


def main():
    with open("data.json", encoding="utf-8") as f:
        data = json.load(f)
    s = data.get("settings", {})
    # the topic is a secret (it receives AND accepts pushes) — it lives in the
    # repo's Actions secret NTFY_TOPIC, never in data.json
    topic = os.environ.get("NTFY_TOPIC", "").strip() or (s.get("ntfy_topic") or "")
    if not s.get("ntfy_enabled") or not topic:
        print("ntfy disabled or no NTFY_TOPIC secret — nothing to schedule")
        return
    tz = ZoneInfo(s.get("timezone") or "UTC")
    now = datetime.now(tz)
    lead = timedelta(minutes=int(s.get("reminder_lead_min") or 10))
    server = s.get("ntfy_server") or "https://ntfy.sh"
    # taps open the cloud phone app (works with the PC off); fall back to the
    # Tailscale companion when no cloud app is configured
    click = s.get("cloud_app_url") or (
        (s.get("web_url") or None) if s.get("web_enabled") else None)

    reg = {}
    if os.path.exists(REGISTRY):
        try:
            with open(REGISTRY, encoding="utf-8") as f:
                reg = json.load(f)
        except (OSError, json.JSONDecodeError):
            reg = {}

    scheduled = 0
    for sig, fire_at, title, body in targets(data, s, now, tz, lead):
        if sig in reg:
            continue
        if fire_at <= now + MIN_DELAY or fire_at > now + HORIZON:
            continue
        print(f"scheduling {sig} at {fire_at:%Y-%m-%d %H:%M %Z}")
        if post(server, topic, title, body, int(fire_at.timestamp()), click):
            reg[sig] = now.isoformat(timespec="seconds")
            scheduled += 1

    cutoff = (now - timedelta(days=3)).date().isoformat()
    reg = {k: v for k, v in reg.items()
           if len(k.split("|")) > 1 and k.split("|")[1] >= cutoff}
    with open(REGISTRY, "w", encoding="utf-8") as f:
        json.dump(reg, f, indent=1, sort_keys=True)
    print(f"scheduled {scheduled} push(es); registry holds {len(reg)}")


if __name__ == "__main__":
    main()

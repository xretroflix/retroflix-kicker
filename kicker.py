#!/usr/bin/env python3
"""
RetroFlix Tripwire ₹30 — Daily Expiry Kicker
─────────────────────────────────────────────────────────────────
Runs once a day on Railway as a cron job.

For every ₹30 user whose 30 days have passed:
  1. Removes them from the Telegram channel (silent ban + immediate unban
     so they can rejoin if they pay again — Telegram requires this dance)
  2. Marks them inactive in Supabase
  3. NEVER sends a DM. The user discovers their expiry only when they
     visit the index-30.html page and see the expiry banner.

ENVIRONMENT VARIABLES (set in Railway → Variables tab — NEVER in git):
  TELEGRAM_BOT_TOKEN     bot token from @BotFather
  TRIPWIRE_CHANNEL_ID    -100xxxxxxxxx for the ₹30 tripwire channel
  SUPABASE_URL           https://mrimhdcyurxeollkyver.supabase.co
  SUPABASE_SERVICE_KEY   service_role key (NOT anon key)

USAGE:
  Local test:    python tripwire_kicker.py --dry-run
  Production:    python tripwire_kicker.py     (Railway runs this on cron)
"""
import os
import sys
import argparse
import logging
from datetime import datetime, timezone

import requests
from supabase import create_client


# ─────────────────────────── Config ───────────────────────────
TELEGRAM_BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TRIPWIRE_CHANNEL_ID  = os.getenv("TRIPWIRE_CHANNEL_ID")
SUPABASE_URL         = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("tripwire-kicker")


# ─────────────────────────── Helpers ───────────────────────────
def telegram_api(method: str, **payload) -> dict:
    """Call any Telegram Bot API method. Never raises."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        r = requests.post(url, json=payload, timeout=15)
        return r.json()
    except Exception as e:
        log.error("telegram_api %s failed: %s", method, e)
        return {"ok": False, "error": str(e)}


def resolve_user_id(username: str) -> int | None:
    """
    Resolve @username to user_id by checking if they're a member of the channel.
    Returns None if user can't be resolved (already left, never joined, etc.)
    """
    if not username:
        return None
    handle = username if username.startswith("@") else f"@{username}"
    result = telegram_api("getChatMember", chat_id=TRIPWIRE_CHANNEL_ID, user_id=handle)
    if result.get("ok"):
        user = result.get("result", {}).get("user", {})
        return user.get("id")
    return None


def kick_silent(user_id: int, dry_run: bool = False) -> bool:
    """
    Remove user from channel via ban + immediate unban (no DM, no notification).
    The unban allows them to rejoin if they pay again.
    """
    if dry_run:
        log.info("[DRY] Would kick user_id=%s from channel", user_id)
        return True

    ban = telegram_api("banChatMember", chat_id=TRIPWIRE_CHANNEL_ID, user_id=user_id)
    if not ban.get("ok"):
        log.warning("Ban failed for user_id=%s: %s", user_id, ban)
        return False

    # Immediately unban so user can rejoin if they pay again
    telegram_api("unbanChatMember", chat_id=TRIPWIRE_CHANNEL_ID, user_id=user_id, only_if_banned=True)
    return True


# ─────────────────────────── Main flow ───────────────────────────
def main():
    parser = argparse.ArgumentParser(description="RetroFlix tripwire daily kicker")
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't actually kick or write to DB")
    args = parser.parse_args()

    # Validate env
    missing = [k for k in ("TELEGRAM_BOT_TOKEN", "TRIPWIRE_CHANNEL_ID",
                           "SUPABASE_URL", "SUPABASE_SERVICE_KEY")
               if not os.getenv(k)]
    if missing:
        log.error("Missing required env vars: %s", missing)
        sys.exit(1)

    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

    log.info("═══════════════════════════════════════════════════")
    log.info("RetroFlix Tripwire Kicker · %s", datetime.now(timezone.utc).isoformat())
    log.info("═══════════════════════════════════════════════════")
    if args.dry_run:
        log.info("⚠️  DRY RUN — no Telegram kicks, no DB writes")

    # Find expired tripwire users
    now_iso = datetime.now(timezone.utc).isoformat()
    res = (
        supabase.table("users")
        .select("telegram_username, expires_at")
        .eq("subscription_type", "tripwire_30")
        .eq("is_premium", True)
        .lt("expires_at", now_iso)
        .is_("kicked_at", "null")
        .execute()
    )
    expired = res.data or []
    log.info("Found %d expired tripwire users to remove", len(expired))

    if not expired:
        log.info("✅ Nothing to do today")
        return

    success_count = 0
    fail_count = 0

    for u in expired:
        username = (u.get("telegram_username") or "").lstrip("@")
        if not username:
            continue

        log.info("⏳ Processing @%s (expired %s)", username, u.get("expires_at"))

        # Try to resolve and kick
        user_id = resolve_user_id(username)
        if user_id:
            kicked_ok = kick_silent(user_id, dry_run=args.dry_run)
            if kicked_ok:
                success_count += 1
                log.info("✅ Removed @%s (user_id=%s)", username, user_id)
            else:
                fail_count += 1
        else:
            log.warning("⚠️  Could not resolve @%s (already left? not in channel?) — marking inactive anyway", username)

        # Always mark inactive in DB (even if Telegram resolution failed),
        # so we don't keep retrying the same user every day forever
        if not args.dry_run:
            supabase.table("users").update({
                "is_premium": False,
                "active": False,
                "kicked_at": datetime.now(timezone.utc).isoformat(),
            }).eq("telegram_username", username).execute()

    log.info("═══════════════════════════════════════════════════")
    log.info("✅ Done. Removed: %d  Failed: %d  Total: %d", success_count, fail_count, len(expired))


if __name__ == "__main__":
    main()

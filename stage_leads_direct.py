#!/usr/bin/env python3
"""
Direct-to-DB GeM Tender Lead Staging Controller (Manual / Zero-Vercel)
======================================================================
Note: Automated daily evening review (9:00 PM IST -> 9:00 AM IST next-morning delivery)
is now consolidated into `ka-keepalive` (`evening-lead-review`) so admin receives
at most ONE combined evening review email per day.

If invoked manually, this script:
1. Counts active (non-expired) GeM tenders directly in Firestore `fir-80-72586`
   (`gem_vendor_feeds` documents with `__category__:*` prefix) — NOT COMBLOPEZ commercial leads.
2. Preserves existing `vendor_exclusions` when upserting `__lead_staging_batch__` in COMBLOPEZ.
3. Schedules delivery for next morning 9:00 AM IST (unless `--minutes` is explicitly provided).

Usage:
  python stage_leads_direct.py [--minutes 30] --source gem_scraper_manual [--dry-run]
"""

import argparse
import email.utils
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Tuple

import requests

from firebase_sync import FirebaseSyncManager

STAGING_BATCH_KEY = "__lead_staging_batch__"
IST_TZ = timezone(timedelta(hours=5, minutes=30))


def load_local_env_fallback() -> None:
    """Loads local .env files for local testing without overriding CI env vars."""
    candidates = [
        Path.cwd() / ".env",
        Path.cwd() / ".env.local",
        Path(__file__).resolve().parent.parent / "digitalMarketing" / ".env.local",
    ]
    for env_path in candidates:
        if not env_path.exists():
            continue
        try:
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
        except Exception:
            pass


def get_env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def compute_next_morning_9am_ist(now_utc: datetime) -> datetime:
    """Computes the next 9:00 AM IST (03:30 UTC) strictly after `now_utc`."""
    now_ist = now_utc.astimezone(IST_TZ)
    candidate_ist = now_ist.replace(hour=9, minute=0, second=0, microsecond=0)
    if candidate_ist <= now_ist:
        candidate_ist = candidate_ist + timedelta(days=1)
    return candidate_ist.astimezone(timezone.utc)


def scan_gem_firestore_category_pools(db=None) -> Tuple[int, int]:
    """Counts active (non-expired) GeM tenders across `__category__:*` pools in Firestore `fir-80-72586`."""
    if db is None:
        sync_manager = FirebaseSyncManager()
        db = sync_manager.db

    if db is None:
        print("[StageLeadsDirect] Warning: Firestore client unavailable; cannot count GeM tenders.")
        return 0, 0

    now_utc = datetime.now(timezone.utc)
    total_tenders = 0
    categories_count = 0

    try:
        docs = db.collection("gem_vendor_feeds").stream()
        for doc in docs:
            if not str(doc.id).startswith("__category__:"):
                continue
            data = doc.to_dict() or {}
            inquiries = data.get("inquiries")
            if not isinstance(inquiries, dict):
                continue

            cat_active = 0
            for _, item in inquiries.items():
                if not isinstance(item, dict):
                    continue
                expiry_raw = str(item.get("expiry_date") or item.get("expiry") or "").strip()
                is_expired = False
                if expiry_raw:
                    try:
                        exp_dt = datetime.fromisoformat(expiry_raw.replace("Z", "+00:00"))
                        if exp_dt.tzinfo is None:
                            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                        if exp_dt < now_utc:
                            is_expired = True
                    except Exception:
                        pass
                if not is_expired:
                    cat_active += 1

            if cat_active > 0:
                categories_count += 1
                total_tenders += cat_active
    except Exception as exc:
        print(f"[StageLeadsDirect] Warning scanning GeM Firestore pools: {exc}")
        return 0, 0

    return total_tenders, categories_count


def fetch_existing_staging_metadata(supabase_url: str, service_key: str) -> dict:
    """Reads existing `__lead_staging_batch__` metadata so `vendor_exclusions` are preserved."""
    endpoint = f"{supabase_url.rstrip('/')}/rest/v1/scraped_leads?select=metadata&email=eq.{STAGING_BATCH_KEY}&limit=1"
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
    }
    try:
        resp = requests.get(endpoint, headers=headers, timeout=15)
        if resp.ok:
            rows = resp.json()
            if isinstance(rows, list) and rows and isinstance(rows[0].get("metadata"), dict):
                return dict(rows[0]["metadata"])
    except Exception as exc:
        print(f"[StageLeadsDirect] Warning reading existing staging metadata: {exc}")
    return {}


def build_email_html(
    staged_ist_str: str,
    scheduled_ist_str: str,
    tenders_count: int,
    categories_count: int,
    dashboard_url: str,
    sender_email: str,
    target_email: str,
) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>GeM Tender Staging Review</title>
</head>
<body style="margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #0b0f19; color: #f3f4f6;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color: #0b0f19; padding: 32px 16px;">
    <tr>
      <td align="center">
        <table width="100%" style="max-width: 600px; background-color: #111827; border-radius: 12px; border: 1px solid #374151; overflow: hidden;" cellpadding="0" cellspacing="0">
          <tr>
            <td style="padding: 28px 32px; background: linear-gradient(135deg, #1e1b4b 0%, #311042 100%); border-bottom: 1px solid #374151; text-align: center;">
              <span style="display: inline-block; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1.5px; color: #f59e0b; background-color: rgba(245, 158, 11, 0.15); border: 1px solid rgba(245, 158, 11, 0.3); padding: 4px 12px; border-radius: 9999px; margin-bottom: 12px;">
                🛡️ GeM Tender Review Staging
              </span>
              <h1 style="margin: 0; font-size: 24px; font-weight: 800; color: #ffffff; letter-spacing: -0.5px;">
                Zivic Nexus GeM Lead Engine
              </h1>
              <p style="margin: 6px 0 0; font-size: 13px; color: #d1d5db;">
                Staged at {staged_ist_str}
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding: 32px;">
              <div style="background: rgba(245, 158, 11, 0.08); border-left: 4px solid #f59e0b; padding: 16px; border-radius: 6px; margin-bottom: 24px;">
                <p style="margin: 0 0 6px; font-weight: 700; color: #fbbf24; font-size: 15px;">
                  ⏰ Scheduled Vendor Delivery: {scheduled_ist_str}
                </p>
                <p style="margin: 0; font-size: 13.5px; color: #d1d5db; line-height: 1.5;">
                  Active GeM government tenders from Firestore are staged for admin verification ahead of vendor delivery at
                  <strong>{scheduled_ist_str}</strong>.
                </p>
              </div>
              <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom: 24px;">
                <tr>
                  <td width="48%" style="background-color: #1f2937; border: 1px solid #374151; border-radius: 8px; padding: 16px; text-align: center;">
                    <div style="font-size: 28px; font-weight: 800; color: #10b981;">{tenders_count}</div>
                    <div style="font-size: 12px; text-transform: uppercase; color: #9ca3af; letter-spacing: 0.5px; margin-top: 4px;">Active GeM Tenders</div>
                  </td>
                  <td width="4%"></td>
                  <td width="48%" style="background-color: #1f2937; border: 1px solid #374151; border-radius: 8px; padding: 16px; text-align: center;">
                    <div style="font-size: 28px; font-weight: 800; color: #6366f1;">{categories_count}</div>
                    <div style="font-size: 12px; text-transform: uppercase; color: #9ca3af; letter-spacing: 0.5px; margin-top: 4px;">GeM Categories</div>
                  </td>
                </tr>
              </table>
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td align="center">
                    <a href="{dashboard_url}" target="_blank" style="display: inline-block; padding: 14px 32px; background: linear-gradient(135deg, #f97316 0%, #ea580c 100%); color: #ffffff; text-decoration: none; font-weight: 700; font-size: 15px; border-radius: 8px; text-align: center;">
                      Open Admin Review Dashboard &rarr;
                    </a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td style="padding: 20px 32px; background-color: #0f172a; border-top: 1px solid #1f2937; text-align: center; font-size: 11px; color: #6b7280;">
              Sent directly by Zivic Nexus GeM Staging Controller<br>
              Sender: <code>{sender_email}</code> &bull; Recipient: <code>{target_email}</code>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def send_admin_staging_email(
    staged_ist_str: str,
    scheduled_ist_str: str,
    tenders_count: int,
    categories_count: int,
    platform_base_url: str,
) -> Tuple[bool, str | None]:
    sender_email = get_env("AI_LEAD_SCRAPER_EMAIL_NOTFCN_URL", "comblopez@gmail.com")
    sender_pass = get_env("AI_LEAD_SCRAPER_EMAIL_NOTFCN_KEY")
    target_email = get_env("TARGET_ADMIN_EMAIL", "zivicnexus@gmail.com")

    if not sender_pass:
        print(
            "[StageLeadsDirect] Skipping admin email alert: AI_LEAD_SCRAPER_EMAIL_NOTFCN_KEY secret not set on this repo."
        )
        return False, None

    dashboard_url = f"{platform_base_url.rstrip('/')}/dashboard/admin"
    subject = f"🛡️ GeM Tender Review Staged ({tenders_count} Active GeM Tenders) — Delivery {scheduled_ist_str}"
    text_body = (
        f"Hello Admin,\n\n"
        f"GeM Tender staging ran at {staged_ist_str}.\n\n"
        f"Active GeM Tenders in Firestore: {tenders_count} across {categories_count} categories.\n"
        f"Scheduled vendor delivery: {scheduled_ist_str}.\n\n"
        f"Review, edit, drop leads per vendor, or dispatch early here: {dashboard_url}\n"
    )
    html_body = build_email_html(
        staged_ist_str=staged_ist_str,
        scheduled_ist_str=scheduled_ist_str,
        tenders_count=tenders_count,
        categories_count=categories_count,
        dashboard_url=dashboard_url,
        sender_email=sender_email,
        target_email=target_email,
    )

    msg_id = email.utils.make_msgid(domain="zivicnexus.com")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f'"Zivic Nexus Lead Engine" <{sender_email}>'
    msg["To"] = target_email
    msg["Message-ID"] = msg_id
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    smtp_host = "smtp.gmail.com" if sender_email.lower().endswith("@gmail.com") else get_env("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(get_env("SMTP_PORT", "587") or "587")

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(sender_email, sender_pass)
            server.sendmail(sender_email, [target_email], msg.as_string())
        print(f"[StageLeadsDirect] Admin alert email sent to {target_email} (MsgId: {msg_id})")
        return True, msg_id
    except Exception as exc:
        print(f"[StageLeadsDirect] Warning: failed to send admin staging email: {exc}")
        return False, None


def upsert_staging_batch(
    supabase_url: str,
    service_key: str,
    batch_metadata: dict,
) -> bool:
    endpoint = f"{supabase_url.rstrip('/')}/rest/v1/scraped_leads?on_conflict=email"
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }
    row = {
        "email": STAGING_BATCH_KEY,
        "metadata": batch_metadata,
        "status": "staged",
        "source": "lead_staging_engine",
        "original_post_text": "Lead Staging and Review Batch",
    }
    resp = requests.post(endpoint, headers=headers, json=row, timeout=20)
    if not resp.ok:
        print(f"[StageLeadsDirect] ERROR upserting staging batch ({resp.status_code}): {resp.text[:300]}", file=sys.stderr)
        return False
    return True


def main() -> int:
    load_local_env_fallback()

    parser = argparse.ArgumentParser(description="Direct-to-DB GeM Tender Staging Controller (Manual / Zero-Vercel)")
    parser.add_argument(
        "--minutes",
        type=int,
        default=None,
        help="Optional override duration in minutes (default: next morning 9:00 AM IST)",
    )
    parser.add_argument("--source", type=str, default="gem_scraper_manual", help="Trigger source identifier")
    parser.add_argument("--dry-run", action="store_true", help="Scan GeM Firestore read-only without writing or emailing")
    args = parser.parse_args()

    supabase_url = get_env("COMBLOPEZ_SUPABASE_PROJECT2_URL")
    service_key = get_env("COMBLOPEZ_SUPABASE_PROJECT2_SERVICE_ROLE_KEY")
    platform_base_url = get_env("PLATFORM_BASE_URL") or get_env("ZIVIC_BASE_URL") or get_env("ZIVIC_API_URL") or "https://zivicnexus.com"

    if not supabase_url or not service_key:
        print(
            "[StageLeadsDirect] ERROR: Missing COMBLOPEZ_SUPABASE_PROJECT2_URL or COMBLOPEZ_SUPABASE_PROJECT2_SERVICE_ROLE_KEY.",
            file=sys.stderr,
        )
        return 1

    now_utc = datetime.now(timezone.utc)
    if args.minutes is not None and args.minutes > 0:
        scheduled_utc = now_utc + timedelta(minutes=args.minutes)
    else:
        scheduled_utc = compute_next_morning_9am_ist(now_utc)

    duration_minutes = max(1, int((scheduled_utc - now_utc).total_seconds() // 60))
    staged_at_iso = now_utc.isoformat().replace("+00:00", "Z")
    scheduled_iso = scheduled_utc.isoformat().replace("+00:00", "Z")
    staged_ist_str = now_utc.astimezone(IST_TZ).strftime("%a, %d %b, %I:%M %p IST")
    scheduled_ist_str = scheduled_utc.astimezone(IST_TZ).strftime("%a, %d %b, %I:%M %p IST")
    batch_id = f"batch_{int(time.time() * 1000)}"

    tenders_count, categories_count = scan_gem_firestore_category_pools()
    print(
        f"[StageLeadsDirect] Scanned GeM Firestore: active_tenders={tenders_count}, categories={categories_count}, "
        f"scheduled={scheduled_ist_str}, source={args.source}"
    )

    if args.dry_run:
        print(f"[StageLeadsDirect] DRY-RUN complete (batch_id={batch_id}). No DB write or email sent.")
        return 0

    existing_meta = fetch_existing_staging_metadata(supabase_url, service_key)
    preserved_exclusions = existing_meta.get("vendor_exclusions") if isinstance(existing_meta.get("vendor_exclusions"), dict) else {}

    email_sent, msg_id = send_admin_staging_email(
        staged_ist_str=staged_ist_str,
        scheduled_ist_str=scheduled_ist_str,
        tenders_count=tenders_count,
        categories_count=categories_count,
        platform_base_url=platform_base_url,
    )

    batch_metadata = {
        **existing_meta,
        "batch_id": batch_id,
        "status": "staged",
        "staged_at": staged_at_iso,
        "scheduled_delivery_at": scheduled_iso,
        "duration_minutes": duration_minutes,
        "trigger_source": args.source,
        "leads_count": tenders_count,
        "gem_tenders_count": tenders_count,
        "categories_count": categories_count,
        "vendor_exclusions": preserved_exclusions,
        "dispatched_at": None,
        "dispatched_by": None,
    }
    if email_sent:
        batch_metadata["last_admin_alert_sent_at"] = staged_at_iso
    if msg_id:
        batch_metadata["last_admin_alert_message_id"] = msg_id

    ok = upsert_staging_batch(supabase_url, service_key, batch_metadata)
    if not ok:
        return 1

    print(f"[StageLeadsDirect] GeM staging batch {batch_id} saved directly to COMBLOPEZ (exclusions preserved).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

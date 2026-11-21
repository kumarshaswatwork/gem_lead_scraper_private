#!/usr/bin/env python3
"""
Direct-to-DB Lead Staging Controller (Zero-Vercel)
==================================================
Replaces `curl POST https://zivicnexus.com/api/cron/stage-leads` at the end of
the AI Buyer Lead Scraper and GeM Tender Scraper GitHub Actions workflows.

What it does directly (0 Vercel calls):
1. Scans COMBLOPEZ Supabase `scraped_leads` category pools (`__category__:%`)
   to count fresh leads discovered in the last 48 hours.
2. Upserts the `__lead_staging_batch__` row directly into COMBLOPEZ Supabase
   with `status = "staged"` and `scheduled_delivery_at = now + <minutes>`.
3. Sends the branded 30-minute review window alert email directly to admin
   (`zivicnexus@gmail.com`) via Gmail SMTP using `AI_LEAD_SCRAPER_EMAIL_NOTFCN_URL`
   and `AI_LEAD_SCRAPER_EMAIL_NOTFCN_KEY`.

Usage:
  python stage_leads_direct.py --minutes 30 --source gem_scraper_actions [--dry-run]
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

import requests

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


def scan_comblopez_category_pools(supabase_url: str, service_key: str) -> tuple[int, int]:
    """Counts active categories and fresh leads (last 48h) directly in COMBLOPEZ."""
    endpoint = (
        f"{supabase_url.rstrip('/')}/rest/v1/scraped_leads"
        "?select=email,metadata,updated_at&email=like.__category__:*"
    )
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
    }

    resp = requests.get(endpoint, headers=headers, timeout=20)
    if not resp.ok:
        print(f"[StageLeadsDirect] Warning: category scan returned HTTP {resp.status_code}: {resp.text[:250]}")
        return 0, 0

    rows = resp.json()
    if not isinstance(rows, list):
        return 0, 0

    cutoff_iso = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat().replace("+00:00", "Z")
    categories_count = len(rows)
    total_leads = 0

    for row in rows:
        meta = row.get("metadata") or {}
        leads = meta.get("leads") if isinstance(meta.get("leads"), list) else []
        row_updated = str(row.get("updated_at") or "")
        for lead in leads:
            if not isinstance(lead, dict):
                continue
            disc = str(lead.get("discovered_at") or row_updated or "")
            if not disc or disc >= cutoff_iso:
                total_leads += 1

    return total_leads, categories_count


def build_email_html(
    scheduled_ist_str: str,
    leads_count: int,
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
  <title>AI Lead Scraper Review Window Active</title>
</head>
<body style="margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #0b0f19; color: #f3f4f6;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color: #0b0f19; padding: 32px 16px;">
    <tr>
      <td align="center">
        <table width="100%" style="max-width: 600px; background-color: #111827; border-radius: 12px; border: 1px solid #374151; overflow: hidden;" cellpadding="0" cellspacing="0">
          <tr>
            <td style="padding: 28px 32px; background: linear-gradient(135deg, #1e1b4b 0%, #311042 100%); border-bottom: 1px solid #374151; text-align: center;">
              <span style="display: inline-block; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 1.5px; color: #f59e0b; background-color: rgba(245, 158, 11, 0.15); border: 1px solid rgba(245, 158, 11, 0.3); padding: 4px 12px; border-radius: 9999px; margin-bottom: 12px;">
                ⚡ 30-Minute Review Staging Window
              </span>
              <h1 style="margin: 0; font-size: 24px; font-weight: 800; color: #ffffff; letter-spacing: -0.5px;">
                Zivic Nexus Lead Engine
              </h1>
              <p style="margin: 6px 0 0; font-size: 14px; color: #d1d5db;">
                GeM Tender Radar &amp; AI Lead Scraper Ran Successfully
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding: 32px;">
              <div style="background: rgba(245, 158, 11, 0.08); border-left: 4px solid #f59e0b; padding: 16px; border-radius: 6px; margin-bottom: 24px;">
                <p style="margin: 0 0 6px; font-weight: 700; color: #fbbf24; font-size: 15px;">
                  ⏳ Delivery Paused for 30 Minutes
                </p>
                <p style="margin: 0; font-size: 13.5px; color: #d1d5db; line-height: 1.5;">
                  The GeM tender scraper just finished its scan and discovered fresh tenders.
                  WhatsApp template and companion email dispatch to subscribed vendors is
                  <strong>held until {scheduled_ist_str} IST</strong> (30-minute verification window).
                </p>
              </div>
              <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom: 24px;">
                <tr>
                  <td width="48%" style="background-color: #1f2937; border: 1px solid #374151; border-radius: 8px; padding: 16px; text-align: center;">
                    <div style="font-size: 28px; font-weight: 800; color: #10b981;">{leads_count}</div>
                    <div style="font-size: 12px; text-transform: uppercase; color: #9ca3af; letter-spacing: 0.5px; margin-top: 4px;">Fresh Leads Discovered</div>
                  </td>
                  <td width="4%"></td>
                  <td width="48%" style="background-color: #1f2937; border: 1px solid #374151; border-radius: 8px; padding: 16px; text-align: center;">
                    <div style="font-size: 28px; font-weight: 800; color: #6366f1;">{categories_count}</div>
                    <div style="font-size: 12px; text-transform: uppercase; color: #9ca3af; letter-spacing: 0.5px; margin-top: 4px;">Active Categories</div>
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
              <p style="margin: 20px 0 0; text-align: center; font-size: 12px; color: #6b7280;">
                Automatic dispatch will execute at <strong>{scheduled_ist_str} IST</strong> if no manual action is taken.
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding: 20px 32px; background-color: #0f172a; border-top: 1px solid #1f2937; text-align: center; font-size: 11px; color: #6b7280;">
              Sent directly by Zivic Nexus Scraper Staging Controller (Zero-Vercel)<br>
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
    scheduled_ist_str: str,
    leads_count: int,
    categories_count: int,
    platform_base_url: str,
) -> tuple[bool, str | None]:
    sender_email = get_env("AI_LEAD_SCRAPER_EMAIL_NOTFCN_URL", "comblopez@gmail.com")
    sender_pass = get_env("AI_LEAD_SCRAPER_EMAIL_NOTFCN_KEY")
    target_email = get_env("TARGET_ADMIN_EMAIL", "zivicnexus@gmail.com")

    if not sender_pass:
        print(
            "[StageLeadsDirect] Skipping admin email alert: AI_LEAD_SCRAPER_EMAIL_NOTFCN_KEY secret not set on this repo."
        )
        return False, None

    dashboard_url = f"{platform_base_url.rstrip('/')}/dashboard/admin"
    subject = f"🔔 Action Required: GeM Tender Scraper Ran ({leads_count} Leads) — 30-Minute Staging Window Active"
    text_body = (
        f"Hello Admin,\n\n"
        f"The GeM Tender Radar cron job ran successfully!\n\n"
        f"Discovered {leads_count} fresh inquiries across {categories_count} categories.\n"
        f"Delivery to vendors is paused until {scheduled_ist_str} IST.\n\n"
        f"Review, edit, drop leads, or dispatch early here: {dashboard_url}\n"
    )
    html_body = build_email_html(
        scheduled_ist_str=scheduled_ist_str,
        leads_count=leads_count,
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

    parser = argparse.ArgumentParser(description="Direct-to-DB Lead Staging Controller (Zero-Vercel)")
    parser.add_argument("--minutes", type=int, default=30, help="Review window duration in minutes (default: 30)")
    parser.add_argument("--source", type=str, default="gem_scraper_actions", help="Trigger source identifier")
    parser.add_argument("--dry-run", action="store_true", help="Scan COMBLOPEZ read-only without writing or emailing")
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
    scheduled_utc = now_utc + timedelta(minutes=max(1, args.minutes))
    staged_at_iso = now_utc.isoformat().replace("+00:00", "Z")
    scheduled_iso = scheduled_utc.isoformat().replace("+00:00", "Z")
    scheduled_ist_str = scheduled_utc.astimezone(IST_TZ).strftime("%I:%M %p").lstrip("0")
    batch_id = f"batch_{int(time.time() * 1000)}"

    leads_count, categories_count = scan_comblopez_category_pools(supabase_url, service_key)
    print(
        f"[StageLeadsDirect] Scanned COMBLOPEZ: leads={leads_count}, categories={categories_count}, "
        f"window={args.minutes}m, scheduled={scheduled_ist_str} IST, source={args.source}"
    )

    if args.dry_run:
        print(f"[StageLeadsDirect] DRY-RUN complete (batch_id={batch_id}). No DB write or email sent.")
        return 0

    email_sent, msg_id = send_admin_staging_email(
        scheduled_ist_str=scheduled_ist_str,
        leads_count=leads_count,
        categories_count=categories_count,
        platform_base_url=platform_base_url,
    )

    batch_metadata = {
        "batch_id": batch_id,
        "status": "staged",
        "staged_at": staged_at_iso,
        "scheduled_delivery_at": scheduled_iso,
        "duration_minutes": max(1, args.minutes),
        "trigger_source": args.source,
        "leads_count": leads_count,
        "categories_count": categories_count,
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

    print(f"[StageLeadsDirect] Staging batch {batch_id} saved directly to COMBLOPEZ (0 Vercel calls).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Delivery channels: console, email (Gmail SMTP), Telegram, SMS (Twilio).

Every channel follows the same contract: it reports whether it is configured, and
sending returns a (ok, detail) pair rather than raising. One dead channel must never
stop the others -- a failed SMS should not swallow the email that would have warned
you about a storm.
"""
from __future__ import annotations

import logging
import smtplib
from datetime import date, datetime, timezone
from email.message import EmailMessage
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .config import secret

LOG = logging.getLogger("manaslu.alerts")

def mask(value: Optional[str]) -> str:
    """Redact a recipient for logging.

    GitHub Actions logs and artifacts are world-readable on a public repository, so a
    plain "Email sent to ..." line would republish the address the rest of this design
    works to keep private. Enough is kept to tell two recipients apart.
    """
    if not value:
        return "<unset>"
    value = str(value)
    if "@" in value:
        local, _, domain = value.partition("@")
        head = local[:2] if len(local) > 3 else local[:1]
        return "{}***@{}".format(head, domain)
    if len(value) > 4:
        return "{}***{}".format(value[:3], value[-2:])
    return "***"


SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class Channel:
    name = "base"

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.settings = cfg["channels"].get(self.name, {})

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", False))

    @property
    def active_until(self) -> Optional[date]:
        """Optional last day this channel may send, as YYYY-MM-DD (inclusive, UTC).

        Exists so a channel tied to a number or address that will change can be given
        a hard stop date, rather than relying on someone remembering to switch it off.
        """
        raw = self.settings.get("active_until")
        if not raw:
            return None
        try:
            return date.fromisoformat(str(raw).strip())
        except ValueError:
            LOG.error("Channel %s has an unparseable active_until (%r); expected "
                      "YYYY-MM-DD. Treating the channel as expired to avoid sending "
                      "to a stale destination.", self.name, raw)
            return date.min

    @property
    def expired(self) -> bool:
        until = self.active_until
        return until is not None and datetime.now(timezone.utc).date() > until

    def missing_secrets(self) -> List[str]:
        return []

    @property
    def recipient(self) -> Optional[str]:
        """Destination for this channel, or None if it has none / is not configured."""
        return None

    @property
    def needs_recipient(self) -> bool:
        return False

    @property
    def ready(self) -> bool:
        if not self.enabled or self.expired or self.missing_secrets():
            return False
        return not (self.needs_recipient and not self.recipient)

    def send(self, message: Dict[str, str]) -> Tuple[bool, str]:
        raise NotImplementedError


class ConsoleChannel(Channel):
    """Always-available fallback so the service is runnable with zero credentials."""
    name = "console"

    def send(self, message: Dict[str, str]) -> Tuple[bool, str]:
        print("\n" + "=" * 78)
        print("SUBJECT: " + message["subject"])
        print("=" * 78)
        print(message["text"])
        print("-" * 78)
        print("SMS ({} chars): {}".format(len(message.get("sms", "")), message.get("sms", "")))
        print("=" * 78 + "\n")
        return True, "printed to console"


class EmailChannel(Channel):
    name = "email"

    def missing_secrets(self) -> List[str]:
        return [n for n in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD") if not secret(n)]

    @property
    def needs_recipient(self) -> bool:
        return True

    @property
    def recipient(self) -> Optional[str]:
        """Configured recipient, or the sending account itself.

        Mailing yourself is by far the common case, so an unset MANASLU_EMAIL_TO is
        treated as "send it to me" rather than as a misconfiguration.
        """
        return self.cfg["recipients"].get("email") or secret("GMAIL_ADDRESS")

    def send(self, message: Dict[str, str]) -> Tuple[bool, str]:
        address = secret("GMAIL_ADDRESS")
        password = secret("GMAIL_APP_PASSWORD")
        recipient = self.recipient
        prefix = self.settings.get("subject_prefix", "[Manaslu]")

        mail = EmailMessage()
        mail["Subject"] = "{} {}".format(prefix, message["subject"])
        mail["From"] = address
        mail["To"] = recipient
        mail.set_content(message["text"])
        if message.get("html"):
            mail.add_alternative(message["html"], subtype="html")

        try:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
                smtp.login(address, password)
                smtp.send_message(mail)
            LOG.info("Email sent to %s", mask(recipient))
            return True, "sent to {}".format(mask(recipient))
        except smtplib.SMTPAuthenticationError:
            return False, ("Gmail rejected the login. Use a 16-character App Password "
                           "(myaccount.google.com/apppasswords), not the account password.")
        except Exception as exc:  # network, DNS, TLS -- never fatal to the run
            LOG.error("Email send failed: %s", exc)
            return False, "email failed: {}".format(exc)


class TelegramChannel(Channel):
    name = "telegram"

    @property
    def recipient(self) -> Optional[str]:
        return secret("TELEGRAM_CHAT_ID")

    def missing_secrets(self) -> List[str]:
        return [n for n in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not secret(n)]

    def send(self, message: Dict[str, str]) -> Tuple[bool, str]:
        token = secret("TELEGRAM_BOT_TOKEN")
        chat_id = secret("TELEGRAM_CHAT_ID")
        text = message.get("telegram") or message["text"]

        # Telegram hard-caps a message at 4096 characters.
        chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
        try:
            for chunk in chunks:
                response = httpx.post(
                    TELEGRAM_API.format(token=token),
                    json={"chat_id": chat_id, "text": chunk,
                          "parse_mode": "HTML", "disable_web_page_preview": True},
                    timeout=30,
                )
                response.raise_for_status()
            LOG.info("Telegram sent (%d chunk(s))", len(chunks))
            return True, "sent {} chunk(s)".format(len(chunks))
        except Exception as exc:
            LOG.error("Telegram send failed: %s", exc)
            return False, "telegram failed: {}".format(exc)


class SmsChannel(Channel):
    """Twilio SMS. Billed per segment, so the message is capped before it gets here."""
    name = "sms"

    def missing_secrets(self) -> List[str]:
        return [n for n in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER")
                if not secret(n)]

    @property
    def needs_recipient(self) -> bool:
        return True

    @property
    def recipient(self) -> Optional[str]:
        """There is no safe default destination for an SMS, so this one is required."""
        return self.cfg["recipients"].get("sms") or None

    def send(self, message: Dict[str, str]) -> Tuple[bool, str]:
        body = message.get("sms")
        if not body:
            return False, "no SMS body built"
        recipient = self.recipient
        if not recipient:
            return False, "no SMS recipient configured (set MANASLU_SMS_TO)"
        try:
            from twilio.rest import Client
        except ImportError:
            return False, "twilio package not installed (pip install twilio)"
        try:
            client = Client(secret("TWILIO_ACCOUNT_SID"), secret("TWILIO_AUTH_TOKEN"))
            sent = client.messages.create(
                body=body, from_=secret("TWILIO_FROM_NUMBER"), to=recipient)
            LOG.info("SMS sent to %s (sid=%s)", mask(recipient), sent.sid)
            return True, "sent to {} (sid {})".format(mask(recipient), sent.sid)
        except Exception as exc:
            LOG.error("SMS send failed: %s", exc)
            return False, "sms failed: {}".format(exc)


CHANNELS = (ConsoleChannel, EmailChannel, TelegramChannel, SmsChannel)


def build_channels(cfg: Dict[str, Any]) -> List[Channel]:
    return [cls(cfg) for cls in CHANNELS]


def dispatch(cfg: Dict[str, Any], message: Dict[str, str],
             dry_run: bool = False) -> Dict[str, Any]:
    """Send `message` on every ready channel. Returns a per-channel result summary."""
    results: Dict[str, Any] = {"sent": [], "skipped": [], "failed": []}

    for channel in build_channels(cfg):
        if not channel.enabled:
            results["skipped"].append((channel.name, "disabled in config.yaml"))
            continue
        if channel.expired:
            LOG.info("Channel %s expired on %s; not sending.",
                     channel.name, channel.active_until)
            results["skipped"].append(
                (channel.name, "expired after {}".format(channel.active_until)))
            continue
        missing = channel.missing_secrets()
        if missing:
            LOG.warning("Channel %s is enabled but missing %s", channel.name, missing)
            results["skipped"].append(
                (channel.name, "missing secrets: {}".format(", ".join(missing))))
            continue
        if channel.needs_recipient and not channel.recipient:
            variable = "MANASLU_{}_TO".format(channel.name.upper())
            LOG.warning("Channel %s has no recipient; set %s", channel.name, variable)
            results["skipped"].append(
                (channel.name, "no recipient configured (set {})".format(variable)))
            continue
        if dry_run and channel.name != "console":
            results["skipped"].append((channel.name, "dry run"))
            continue

        ok, detail = channel.send(message)
        (results["sent"] if ok else results["failed"]).append((channel.name, detail))

    return results


def summarise(results: Dict[str, Any]) -> str:
    parts = []
    for bucket in ("sent", "failed", "skipped"):
        if results[bucket]:
            parts.append("{}: {}".format(bucket, ", ".join(
                "{} ({})".format(name, detail) for name, detail in results[bucket])))
    return " | ".join(parts) or "nothing dispatched"

"""Guards against committing personal data or credentials to a public repository.

These tests exist because the leak they prevent is silent: nothing breaks, the
service keeps working, and the contact details are simply public forever. Once a
real value reaches a public git history, deleting it is not enough.
"""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Files that are committed and therefore published.
SCANNED = [
    "config.yaml", ".env.example", "README.md", "HOWTORUN.md", "CLAUDE.md",
    "manaslu-forecast-spec.md", "requirements.txt", ".gitignore",
]
SCANNED += [str(p.relative_to(ROOT)) for p in ROOT.glob("src/*.py")]
SCANNED += [str(p.relative_to(ROOT)) for p in ROOT.glob(".github/workflows/*.yml")]
SCANNED += [str(p.relative_to(ROOT)) for p in ROOT.glob("deploy/**/*") if p.is_file()]

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"\+[0-9]{7,15}")

ALLOWED_EMAIL = re.compile(r"example\.(com|org)|you@gmail\.com|users\.noreply\.github\.com")
ALLOWED_PHONE = {"+49XXXXXXXXXXX", "+15551234567", "+491700000000", "+4900"}


@pytest.mark.parametrize("relative", SCANNED)
def test_no_real_email_addresses_in_committed_files(relative):
    path = ROOT / relative
    if not path.exists():
        pytest.skip("{} not present".format(relative))
    for match in EMAIL.finditer(path.read_text(encoding="utf-8", errors="ignore")):
        assert ALLOWED_EMAIL.search(match.group(0)), \
            "{} contains a real email address: {}".format(relative, match.group(0))


@pytest.mark.parametrize("relative", SCANNED)
def test_no_real_phone_numbers_in_committed_files(relative):
    path = ROOT / relative
    if not path.exists():
        pytest.skip("{} not present".format(relative))
    for match in PHONE.finditer(path.read_text(encoding="utf-8", errors="ignore")):
        assert match.group(0) in ALLOWED_PHONE or "X" in match.group(0), \
            "{} contains a real phone number: {}".format(relative, match.group(0))


def test_env_file_is_gitignored():
    ignore = (ROOT / ".gitignore").read_text()
    assert ".env" in ignore.split("\n")


def test_env_example_holds_no_values():
    """The template must ship with every value blank."""
    for line in (ROOT / ".env.example").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        assert value.strip() == "", "{} has a value in .env.example".format(key)


def test_config_contains_no_credentials():
    """Comments may discuss secrets; a key that *assigns* one must not exist."""
    assignment = re.compile(
        r"^\s*[\w.-]*(password|token|api_key|secret|sid)[\w.-]*\s*:\s*\S+",
        re.IGNORECASE | re.MULTILINE)
    raw = (ROOT / "config.yaml").read_text()
    stripped = "\n".join(l for l in raw.splitlines() if not l.strip().startswith("#"))
    match = assignment.search(stripped)
    assert match is None, "config.yaml assigns a credential: {}".format(
        match.group(0) if match else "")


def test_workflows_read_recipients_from_secrets():
    for name in ("digest", "danger"):
        raw = (ROOT / ".github/workflows/{}.yml".format(name)).read_text()
        assert "secrets.MANASLU_EMAIL_TO" in raw
        assert "secrets.MANASLU_SMS_TO" in raw


def test_preflight_script_passes():
    """The publish gate must be green on the current tree."""
    script = ROOT / "scripts/preflight_public.sh"
    result = subprocess.run(["bash", str(script)], cwd=str(ROOT),
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_preflight_script_detects_a_planted_leak(tmp_path):
    """A gate that cannot fail is not a gate."""
    script = ROOT / "scripts/preflight_public.sh"
    planted = ROOT / "_publish_safety_probe.md"
    # Assembled at runtime: a literal number here would trip the very scanner
    # this test invokes, and the suite would flag its own test file.
    planted.write_text('contact: "{}"\n'.format("+49" + "9" * 10))
    try:
        result = subprocess.run(["bash", str(script)], cwd=str(ROOT),
                                capture_output=True, text=True)
        assert result.returncode != 0
        assert "phone" in result.stdout.lower()
    finally:
        planted.unlink()


def test_masking_hides_the_bulk_of_a_recipient():
    """Actions logs are public on a public repo; a full address must never reach them."""
    from src.alerts import mask
    email = "climber121@example.com"
    masked = mask(email)
    assert "climber121" not in masked
    assert masked.endswith("@example.com")  # enough to tell recipients apart

    # Synthetic, assembled at runtime: a real number written literally here would be
    # published by this very file, which is the leak the suite is meant to catch.
    phone = "+49" + "1" * 10
    masked_phone = mask(phone)
    assert masked_phone.startswith("+49")
    assert "1" * 6 not in masked_phone
    assert len(masked_phone) < len(phone)


def test_masking_handles_unset_and_short_values():
    from src.alerts import mask
    assert mask(None) == "<unset>"
    assert mask("") == "<unset>"
    assert mask("ab") == "***"


def test_dispatch_summary_never_contains_a_full_recipient(cfg, monkeypatch):
    """summarise() output is logged and stored in the database; it must be masked."""
    from src.alerts import EmailChannel, summarise
    monkeypatch.setenv("GMAIL_ADDRESS", "sender@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x" * 16)
    patched = {**cfg, "recipients": {"email": "climber121@example.com", "sms": ""}}
    channel = EmailChannel(patched)

    sent = {}

    class _FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, *a):
            pass

        def send_message(self, mail):
            sent["to"] = mail["To"]

    monkeypatch.setattr("src.alerts.smtplib.SMTP_SSL", _FakeSMTP)
    ok, detail = channel.send({"subject": "s", "text": "t", "html": "<p>t</p>"})

    assert ok
    assert sent["to"] == "climber121@example.com", "the real address must still be used"
    assert "climber121" not in detail, "but must not appear in the logged detail"
    assert "climber121" not in summarise({"sent": [("email", detail)],
                                          "skipped": [], "failed": []})

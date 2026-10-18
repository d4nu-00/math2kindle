"""Tests for math2kindle.send against a real local SMTP server (aiosmtpd)."""

from __future__ import annotations

import email
import logging
import socket
import zipfile
from email import policy
from pathlib import Path

import pytest

aiosmtpd_controller = pytest.importorskip("aiosmtpd.controller")
from aiosmtpd.smtp import AuthResult, LoginPassword  # noqa: E402

import keyring  # noqa: E402
from keyring.backend import KeyringBackend  # noqa: E402

from math2kindle import send  # noqa: E402

PASSWORD = "s3cret-AppPassw0rd-XYZ"
USER = "me@example.com"
KINDLE = "yourname_abc@kindle.com"
UNICODE_NAME = "Élie–Cartan ζ-functions.epub"
ASCII_NAME = "Elie-Cartan zeta-functions.epub"   # what goes on the wire (transliterated)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


class FakeKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        self.store.pop((service, username), None)


@pytest.fixture
def fake_keyring():
    old = keyring.get_keyring()
    kr = FakeKeyring()
    keyring.set_keyring(kr)
    try:
        yield kr
    finally:
        keyring.set_keyring(old)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv(send.PASSWORD_ENV, raising=False)
    monkeypatch.delenv(send.CONFIG_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(send, "RETRY_DELAY", 0.0)


class Recorder:
    def __init__(self):
        self.envelopes = []

    async def handle_DATA(self, server, session, envelope):
        self.envelopes.append(envelope)
        return "250 OK"


def _authenticator(server, session, envelope, mechanism, auth_data):
    ok = (
        isinstance(auth_data, LoginPassword)
        and auth_data.login.decode() == USER
        and auth_data.password.decode() == PASSWORD
    )
    return AuthResult(success=ok, handled=False)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def smtpd():
    handler = Recorder()
    port = _free_port()
    ctl = aiosmtpd_controller.Controller(
        handler,
        hostname="127.0.0.1",
        port=port,
        authenticator=_authenticator,
        auth_require_tls=False,
        data_size_limit=80 * 1000 * 1000,
    )
    ctl.start()
    try:
        yield handler, port
    finally:
        ctl.stop()


def write_config(path: Path, port: int, **over) -> Path:
    vals = {
        "kindle_email": KINDLE,
        "from_email": USER,
        "smtp_host": "127.0.0.1",
        "smtp_port": port,
        "smtp_security": "none",
    }
    vals.update(over)
    lines = [f"{k} = {v!r}" if isinstance(v, int) else f'{k} = "{v}"' for k, v in vals.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def make_epub(path: Path, title: str = "On ζ-functions", extra: bytes = b"") -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
        )
        zf.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" '
            'version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<dc:title>{title}</dc:title></metadata></package>",
        )
        zf.writestr("OEBPS/blob.bin", extra + bytes(range(256)) * 40)
    return path


def received(handler: Recorder) -> email.message.EmailMessage:
    assert len(handler.envelopes) == 1
    return email.message_from_bytes(handler.envelopes[0].content, policy=policy.default)


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #


def test_send_delivers_intact_attachment_with_unicode_filename(smtpd, tmp_path, monkeypatch, capsys):
    handler, port = smtpd
    monkeypatch.setenv(send.PASSWORD_ENV, PASSWORD)
    cfg = write_config(tmp_path / "c.toml", port)
    book = make_epub(tmp_path / UNICODE_NAME)

    send.send_epub(book, config_path=cfg)

    env = handler.envelopes[0]
    assert env.mail_from == USER
    assert env.rcpt_tos == [KINDLE]
    msg = received(handler)
    assert msg["To"] == KINDLE
    assert msg["From"] == USER
    assert msg["Subject"] == "On ζ-functions"  # dc:title from the EPUB
    assert msg["Message-ID"] and msg["Message-ID"].startswith("<")
    assert msg["Date"]
    atts = list(msg.iter_attachments())
    assert len(atts) == 1
    att = atts[0]
    assert att.get_content_type() == "application/epub+zip"
    assert att.get_filename() == ASCII_NAME
    assert att.get_payload(decode=True) == book.read_bytes()
    # short plain-ASCII parameter on the wire: no RFC 2231 encoding/continuations
    raw = env.content
    unfolded = raw.replace(b"\r\n ", b" ").replace(b"\r\n\t", b" ")
    assert b'filename="Elie-Cartan zeta-functions.epub"' in unfolded
    assert b"filename*" not in raw
    head = raw.split(b"\r\n\r\n", 1)[0]
    head.decode("ascii")  # top-level headers are 7-bit clean (Subject is RFC 2047)
    assert all(b < 128 for b in raw)  # whole message is 7-bit; no SMTPUTF8 needed
    assert "Sent to Kindle" in capsys.readouterr().out


@pytest.mark.parametrize(
    "suffix, ctype",
    [
        (".pdf", "application/pdf"),
        (".txt", "text/plain"),
        (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ],
)
def test_other_accepted_types(smtpd, tmp_path, monkeypatch, suffix, ctype):
    handler, port = smtpd
    monkeypatch.setenv(send.PASSWORD_ENV, PASSWORD)
    cfg = write_config(tmp_path / "c.toml", port)
    f = tmp_path / f"Riemann Hypothesis notes{suffix}"
    f.write_bytes(b"%PDF-1.7\n\x00\xff binary-ish content\n" * 10)
    send.send_epub(f, config_path=cfg)
    att = next(received(handler).iter_attachments())
    assert att.get_content_type() == ctype
    assert att.get_filename() == f.name
    assert att.get_payload(decode=True) == f.read_bytes()
    assert received(handler)["Subject"] == "Riemann Hypothesis notes"


@pytest.mark.parametrize("suffix", [".azw3", ".mobi", ".AZW3"])
def test_rejects_kindle_native_formats(smtpd, tmp_path, monkeypatch, suffix):
    handler, port = smtpd
    monkeypatch.setenv(send.PASSWORD_ENV, PASSWORD)
    cfg = write_config(tmp_path / "c.toml", port)
    f = tmp_path / f"book{suffix}"
    f.write_bytes(b"x" * 100)
    with pytest.raises(send.SendError, match="no longer accepts"):
        send.send_epub(f, config_path=cfg)
    assert handler.envelopes == []


def test_rejects_unknown_extension(tmp_path):
    cfg = write_config(tmp_path / "c.toml", 2525)
    f = tmp_path / "book.xyz"
    f.write_bytes(b"x")
    with pytest.raises(send.SendError, match="unsupported file type"):
        send.send_epub(f, config_path=cfg, dry_run=True)


def test_size_limit(smtpd, tmp_path, monkeypatch):
    handler, port = smtpd
    monkeypatch.setenv(send.PASSWORD_ENV, PASSWORD)
    cfg = write_config(tmp_path / "c.toml", port)
    big = tmp_path / "big.epub"
    with big.open("wb") as fh:  # sparse file just over the limit
        fh.truncate(send.MAX_ATTACHMENT_BYTES + 1)
    with pytest.raises(send.SendError, match="at most 50 MB"):
        send.send_epub(big, config_path=cfg)
    assert handler.envelopes == []

    ok = tmp_path / "ok.epub"
    with ok.open("wb") as fh:
        fh.truncate(send.MAX_ATTACHMENT_BYTES)
    assert send._check_size(ok) == send.MAX_ATTACHMENT_BYTES


def test_dry_run_writes_eml_and_does_not_connect(tmp_path, monkeypatch, capsys):
    port = _free_port()  # nothing listens here
    cfg = write_config(tmp_path / "c.toml", port)
    book = make_epub(tmp_path / UNICODE_NAME)

    def boom(*a, **k):
        raise AssertionError("dry run must not connect")

    monkeypatch.setattr(send.smtplib, "SMTP", boom)
    monkeypatch.setattr(send.smtplib, "SMTP_SSL", boom)

    send.send_epub(book, config_path=cfg, dry_run=True)  # no password needed

    eml = tmp_path / "Élie–Cartan ζ-functions.eml"
    assert eml.is_file()
    msg = email.message_from_bytes(eml.read_bytes(), policy=policy.default)
    assert msg["To"] == KINDLE
    att = next(msg.iter_attachments())
    assert att.get_filename() == ASCII_NAME
    assert att.get_payload(decode=True) == book.read_bytes()
    out = capsys.readouterr().out
    assert KINDLE in out and USER in out and ASCII_NAME in out
    assert f"{book.stat().st_size:,} bytes" in out


# --------------------------------------------------------------------------- #
# Auth, errors, retries
# --------------------------------------------------------------------------- #


def test_bad_password_gives_app_password_hint(smtpd, tmp_path, monkeypatch):
    handler, port = smtpd
    monkeypatch.setenv(send.PASSWORD_ENV, "wrong-password")
    cfg = write_config(tmp_path / "c.toml", port)
    calls = []
    real = send._connect
    monkeypatch.setattr(send, "_connect", lambda c: calls.append(1) or real(c))
    with pytest.raises(send.SendError) as ei:
        send.send_epub(make_epub(tmp_path / "a.epub"), config_path=cfg)
    msg = str(ei.value)
    assert "App Password" in msg and "2-Step Verification" in msg
    assert "wrong-password" not in msg
    assert len(calls) == 1  # auth failures are not retried
    assert handler.envelopes == []


def test_one_retry_on_connection_refused(tmp_path, monkeypatch):
    monkeypatch.setenv(send.PASSWORD_ENV, PASSWORD)
    cfg = write_config(tmp_path / "c.toml", _free_port())
    calls = []
    real = send._connect
    monkeypatch.setattr(send, "_connect", lambda c: calls.append(1) or real(c))
    with pytest.raises(send.SendError, match="Could not talk to"):
        send.send_epub(make_epub(tmp_path / "a.epub"), config_path=cfg)
    assert len(calls) == 2


def test_retry_succeeds_after_transient_error(smtpd, tmp_path, monkeypatch):
    handler, port = smtpd
    monkeypatch.setenv(send.PASSWORD_ENV, PASSWORD)
    cfg = write_config(tmp_path / "c.toml", port)
    real = send._connect
    calls = []

    def flaky(c):
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionResetError("simulated")
        return real(c)

    monkeypatch.setattr(send, "_connect", flaky)
    send.send_epub(make_epub(tmp_path / "a.epub"), config_path=cfg)
    assert len(calls) == 2
    assert len(handler.envelopes) == 1


def test_check_logs_in_without_sending(smtpd, tmp_path, monkeypatch, capsys):
    handler, port = smtpd
    monkeypatch.setenv(send.PASSWORD_ENV, PASSWORD)
    cfg = write_config(tmp_path / "c.toml", port)
    assert send.main(["check", "--config", str(cfg)]) == 0
    assert "SMTP login OK" in capsys.readouterr().out
    assert handler.envelopes == []

    monkeypatch.setenv(send.PASSWORD_ENV, "nope")
    assert send.main(["check", "--config", str(cfg)]) == 1
    assert "App Password" in capsys.readouterr().err


def test_cli_send_and_dry_run(smtpd, tmp_path, monkeypatch):
    handler, port = smtpd
    monkeypatch.setenv(send.PASSWORD_ENV, PASSWORD)
    cfg = write_config(tmp_path / "c.toml", port)
    book = make_epub(tmp_path / "x.epub")
    assert send.main([str(book), "--config", str(cfg), "--dry-run"]) == 0
    assert (tmp_path / "x.eml").exists() and handler.envelopes == []
    assert send.main([str(book), "--config", str(cfg)]) == 0
    assert len(handler.envelopes) == 1
    assert send.main([str(tmp_path / "missing.epub"), "--config", str(cfg)]) == 1


# --------------------------------------------------------------------------- #
# Password precedence and secrecy
# --------------------------------------------------------------------------- #


def test_password_env_beats_keyring(tmp_path, monkeypatch, fake_keyring):
    cfg = send.load_config(write_config(tmp_path / "c.toml", 2525))
    fake_keyring.set_password("math2kindle", USER, "from-keyring")
    assert send.get_password(cfg) == "from-keyring"
    monkeypatch.setenv(send.PASSWORD_ENV, "from-env")
    assert send.get_password(cfg) == "from-env"


def test_keyring_used_with_smtp_user(smtpd, tmp_path, fake_keyring):
    handler, port = smtpd
    fake_keyring.set_password("math2kindle", USER, PASSWORD)
    cfg = write_config(tmp_path / "c.toml", port, from_email="alias@example.org", smtp_user=USER)
    send.send_epub(make_epub(tmp_path / "a.epub"), config_path=cfg)
    assert handler.envelopes[0].mail_from == "alias@example.org"


def test_missing_password_explains_both_options(tmp_path, fake_keyring):
    cfg = send.load_config(write_config(tmp_path / "c.toml", 2525))
    with pytest.raises(send.SendError) as ei:
        send.get_password(cfg)
    msg = str(ei.value)
    assert send.PASSWORD_ENV in msg and "set-password" in msg and "keyring" in msg


def test_missing_keyring_package(tmp_path, monkeypatch):
    cfg = send.load_config(write_config(tmp_path / "c.toml", 2525))
    monkeypatch.setattr(send, "_keyring", lambda: None)
    with pytest.raises(send.SendError, match="pip install keyring"):
        send.get_password(cfg)


def test_set_password_stores_in_keyring(tmp_path, monkeypatch, fake_keyring, capsys):
    cfg = write_config(tmp_path / "c.toml", 2525)
    monkeypatch.setattr(send.getpass, "getpass", lambda prompt="": PASSWORD)
    assert send.main(["set-password", "--config", str(cfg)]) == 0
    assert fake_keyring.store[("math2kindle", USER)] == PASSWORD
    out = capsys.readouterr()
    assert PASSWORD not in out.out + out.err


def test_password_in_config_is_refused(tmp_path):
    p = write_config(tmp_path / "c.toml", 2525)
    p.write_text(p.read_text() + 'smtp_password = "oops"\n')
    with pytest.raises(send.ConfigError, match="never read from disk"):
        send.load_config(p)


def test_password_never_logged_or_printed(smtpd, tmp_path, monkeypatch, caplog, capsys):
    handler, port = smtpd
    monkeypatch.setenv(send.PASSWORD_ENV, PASSWORD)
    cfg = write_config(tmp_path / "c.toml", port)
    caplog.set_level(logging.DEBUG)
    book = make_epub(tmp_path / "a.epub")
    send.send_epub(book, config_path=cfg, dry_run=True)
    send.send_epub(book, config_path=cfg)
    send.main(["check", "--config", str(cfg)])
    monkeypatch.setenv(send.PASSWORD_ENV, PASSWORD + "-bad")
    send.main(["check", "--config", str(cfg)])
    out = capsys.readouterr()
    everything = caplog.text + out.out + out.err + (tmp_path / "a.eml").read_text()
    assert "Logging in" in caplog.text  # logging really was captured
    assert PASSWORD not in everything
    assert PASSWORD.encode() not in handler.envelopes[0].content


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


def test_config_path_precedence(tmp_path, monkeypatch):
    home_cfg = write_config(tmp_path / "home" / ".config" / "math2kindle" / "config.toml", 1, kindle_email="home@kindle.com")
    env_cfg = write_config(tmp_path / "env.toml", 2, kindle_email="env@kindle.com")
    arg_cfg = write_config(tmp_path / "arg.toml", 3, kindle_email="arg@kindle.com")

    assert send.resolve_config_path() == home_cfg
    assert send.load_config().kindle_email == "home@kindle.com"
    monkeypatch.setenv(send.CONFIG_ENV, str(env_cfg))
    assert send.load_config().kindle_email == "env@kindle.com"
    assert send.load_config(arg_cfg).kindle_email == "arg@kindle.com"


def test_config_defaults_and_validation(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('kindle_email = "x@kindle.com"\nfrom_email = "me@gmail.com"\nsmtp_host = "smtp.gmail.com"\n')
    cfg = send.load_config(p)
    assert cfg.smtp_port == 587 and cfg.smtp_security == "starttls" and cfg.smtp_user == "me@gmail.com"

    p.write_text('kindle_email = "x@gmail.com"\nfrom_email = "me@gmail.com"\nsmtp_host = "h"\n')
    with pytest.raises(send.ConfigError, match="@kindle.com"):
        send.load_config(p)
    p.write_text('kindle_email = "x@kindle.com"\nfrom_email = "me@gmail.com"\nsmtp_host = "h"\nsmtp_security = "tls"\n')
    with pytest.raises(send.ConfigError, match="smtp_security"):
        send.load_config(p)
    with pytest.raises(send.ConfigError, match="not found"):
        send.load_config(tmp_path / "nope.toml")


def test_free_kindle_warns_wifi_only(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    send.load_config(write_config(tmp_path / "c.toml", 2525, kindle_email="me@free.kindle.com"))
    assert "Wi-Fi" in caplog.text


def test_example_config_parses(tmp_path, monkeypatch):
    example = Path(send.__file__).with_name("config_example.toml")
    cfg = send.load_config(example)
    assert cfg.smtp_host == "smtp.gmail.com" and cfg.smtp_port == 587
    assert cfg.smtp_security == "starttls"
    active = [ln for ln in example.read_text().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert not any("password" in ln.lower() for ln in active)


@pytest.mark.parametrize("name, expected", [
    ("Élie–Cartan ζ-functions.epub", "Elie-Cartan zeta-functions.epub"),
    ("Über Λ(n) — Größen.EPUB", None),
    ("A truncated Perron formula and short sums of Lambda(n) in arithmetic progressions.epub",
     "A truncated Perron formula and short sums of Lambda(n).epub"),
    ('../we"ird/name.pdf', None),
])
def test_attachment_filename_is_short_ascii(name, expected):
    f = send._safe_filename(name)
    assert f.isascii() and len(f) <= 60 and "/" not in f and '"' not in f
    assert f.lower().endswith((".epub", ".pdf"))
    if expected:
        assert f == expected

"""Email step: deliver a converted book to a Kindle via Amazon's Send to Kindle.

Public API
----------
``send_epub(epub_path, *, config_path=None, dry_run=False) -> None``

Standalone CLI
--------------
::

    python -m math2kindle.send FILE [--config PATH] [--dry-run]
    python -m math2kindle.send check [--config PATH]
    python -m math2kindle.send set-password [--config PATH] [--user USER]

Configuration is a TOML file (see ``config_example.toml``) looked up at
``--config``, then ``$MATH2KINDLE_CONFIG``, then
``~/.config/math2kindle/config.toml``.  The SMTP password is *never* read from
that file: it comes from ``$MATH2KINDLE_SMTP_PASSWORD`` or, failing that, the
OS keyring (service ``"math2kindle"``, username ``smtp_user``).
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import re
import smtplib
import socket
import ssl
import sys
import time
import tomllib
import unicodedata
import zipfile
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from email.utils import formatdate, make_msgid
from pathlib import Path
from xml.etree import ElementTree

__all__ = [
    "Config",
    "ConfigError",
    "SendError",
    "TEXT_FALLBACK_CLASS",
    "build_message",
    "check_login",
    "get_password",
    "load_config",
    "main",
    "resolve_config_path",
    "send_epub",
    "store_password",
]

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

KEYRING_SERVICE = "math2kindle"
PASSWORD_ENV = "MATH2KINDLE_SMTP_PASSWORD"
CONFIG_ENV = "MATH2KINDLE_CONFIG"

#: Amazon's Send to Kindle e-mail limit (total attachment size per e-mail).
MAX_ATTACHMENT_BYTES = 50 * 1000 * 1000
#: Gmail rejects messages over 25 MB *after* base64 encoding (~4/3 growth).
GMAIL_MAX_MESSAGE_BYTES = 25 * 1000 * 1000

DEFAULT_TIMEOUT = 60.0  # seconds; generous enough for a ~50 MB upload start
RETRY_DELAY = 3.0  # seconds before the single retry

MIME_TYPES: dict[str, tuple[str, str]] = {
    ".epub": ("application", "epub+zip"),
    ".pdf": ("application", "pdf"),
    ".docx": ("application", "vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ".txt": ("text", "plain"),
}

REJECTED_TYPES: dict[str, str] = {
    ".azw3": "AZW3",
    ".azw": "AZW",
    ".mobi": "MOBI",
    ".kfx": "KFX",
}

SECURITY_MODES = ("starttls", "ssl", "none")
KINDLE_DOMAINS = ("@kindle.com", "@free.kindle.com")
KNOWN_KEYS = {
    "kindle_email",
    "from_email",
    "smtp_host",
    "smtp_port",
    "smtp_security",
    "smtp_user",
    "smtp_timeout",
}
FORBIDDEN_KEYS = {"password", "smtp_password", "pass", "smtp_pass", "app_password"}

AUTH_HINT = (
    "SMTP login was rejected.\n"
    "  * Gmail: you must use a 16-character App Password, not your normal\n"
    "    password. App Passwords require 2-Step Verification to be switched on:\n"
    "    https://myaccount.google.com/apppasswords\n"
    "  * Check that smtp_user in your config is the full address of the account.\n"
    "  * Re-store the password with: python -m math2kindle.send set-password"
)
APPROVED_LIST_HINT = (
    "Amazon only accepts mail from addresses on your Approved Personal Document\n"
    "E-mail List (Manage Your Content and Devices -> Preferences -> Personal\n"
    "Document Settings). If the book never arrives, check that {sender} is listed\n"
    "and look in that inbox for an Amazon verification or rejection e-mail."
)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class SendError(RuntimeError):
    """Any failure in the e-mail step; the message is meant for end users."""


class ConfigError(SendError):
    """The configuration file is missing or invalid."""


# CSS class on the warning paragraph of an EPUB made by the plain-text PDF
# fallback (garbled maths).  Such books are never e-mailed.
TEXT_FALLBACK_CLASS = "m2k-text-fallback"


def is_text_fallback_epub(path: Path) -> bool:
    """True if ``path`` is an EPUB written by math2kindle's plain-text fallback."""
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.endswith((".xhtml", ".html")) and TEXT_FALLBACK_CLASS.encode() in zf.read(name):
                    return True
    except (zipfile.BadZipFile, OSError, KeyError):
        return False
    return False


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Config:
    kindle_email: str
    from_email: str
    smtp_host: str
    smtp_port: int = 587
    smtp_security: str = "starttls"
    smtp_user: str = ""
    smtp_timeout: float = DEFAULT_TIMEOUT
    source: Path | None = None

    @property
    def is_free_address(self) -> bool:
        return self.kindle_email.lower().endswith("@free.kindle.com")


def default_config_path() -> Path:
    return Path.home() / ".config" / "math2kindle" / "config.toml"


def resolve_config_path(config_path: Path | str | None = None) -> Path:
    """``config_path`` argument > ``$MATH2KINDLE_CONFIG`` > ``~/.config/...``."""
    if config_path is not None:
        return Path(config_path).expanduser()
    env = os.environ.get(CONFIG_ENV)
    if env:
        return Path(env).expanduser()
    return default_config_path()


def _email_like(value: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value))


def load_config(config_path: Path | str | None = None) -> Config:
    path = resolve_config_path(config_path)
    if not path.is_file():
        raise ConfigError(
            f"Config file not found: {path}\n"
            f"Copy math2kindle/config_example.toml there (or pass --config, or set "
            f"${CONFIG_ENV}) and fill in your addresses."
        )
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from None

    # Allow either top-level keys or a [send] / [email] table.
    for table in ("send", "email"):
        if isinstance(raw.get(table), dict):
            raw = {**{k: v for k, v in raw.items() if not isinstance(v, dict)}, **raw[table]}
            break

    bad = FORBIDDEN_KEYS & {k.lower() for k in raw}
    if bad:
        raise ConfigError(
            f"{path}: remove {', '.join(sorted(bad))!s} from the config file. "
            f"Passwords are never read from disk; use ${PASSWORD_ENV} or "
            f"`python -m math2kindle.send set-password` (OS keyring)."
        )
    unknown = set(raw) - KNOWN_KEYS
    if unknown:
        log.warning("Ignoring unknown config keys in %s: %s", path, ", ".join(sorted(unknown)))

    def req(key: str) -> str:
        val = raw.get(key)
        if not isinstance(val, str) or not val.strip():
            raise ConfigError(f"{path}: `{key}` is required and must be a string.")
        return val.strip()

    kindle_email = req("kindle_email")
    if not kindle_email.lower().endswith(KINDLE_DOMAINS) or not _email_like(kindle_email):
        raise ConfigError(
            f"{path}: kindle_email {kindle_email!r} must end with @kindle.com or "
            f"@free.kindle.com. Find it in Amazon: Manage Your Content and Devices -> "
            f"Preferences -> Personal Document Settings."
        )
    from_email = req("from_email")
    if not _email_like(from_email):
        raise ConfigError(f"{path}: from_email {from_email!r} is not an e-mail address.")
    smtp_host = req("smtp_host")

    security = str(raw.get("smtp_security", "starttls")).lower()
    if security not in SECURITY_MODES:
        raise ConfigError(f"{path}: smtp_security must be one of {', '.join(SECURITY_MODES)}.")

    port = raw.get("smtp_port", 465 if security == "ssl" else 587)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise ConfigError(f"{path}: smtp_port must be an integer 1-65535.")

    timeout = raw.get("smtp_timeout", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ConfigError(f"{path}: smtp_timeout must be a positive number of seconds.")

    smtp_user = raw.get("smtp_user") or from_email
    if not isinstance(smtp_user, str):
        raise ConfigError(f"{path}: smtp_user must be a string.")

    cfg = Config(
        kindle_email=kindle_email,
        from_email=from_email,
        smtp_host=smtp_host,
        smtp_port=port,
        smtp_security=security,
        smtp_user=smtp_user.strip(),
        smtp_timeout=float(timeout),
        source=path,
    )
    if cfg.is_free_address:
        log.warning(
            "kindle_email is a @free.kindle.com address: documents are delivered "
            "only over Wi-Fi (never over cellular). Use the @kindle.com address "
            "for normal delivery."
        )
    if security == "none" and smtp_host not in ("localhost", "127.0.0.1", "::1"):
        log.warning("smtp_security = 'none' sends your password unencrypted; use it for local testing only.")
    return cfg


# --------------------------------------------------------------------------- #
# Password (env var -> OS keyring). Never from the config file, never logged.
# --------------------------------------------------------------------------- #


def _keyring():
    """Return the ``keyring`` module, or ``None`` if it is not installed."""
    try:
        import keyring  # type: ignore[import-not-found]
    except ImportError:
        return None
    return keyring


def get_password(cfg: Config, *, required: bool = True) -> str | None:
    env = os.environ.get(PASSWORD_ENV)
    if env:
        log.debug("Using SMTP password from $%s", PASSWORD_ENV)
        return env

    kr = _keyring()
    keyring_problem = "the `keyring` package is not installed (pip install keyring)"
    if kr is not None:
        try:
            pw = kr.get_password(KEYRING_SERVICE, cfg.smtp_user)
        except Exception as exc:  # keyring.errors.KeyringError, no backend, D-Bus...
            keyring_problem = f"the OS keyring is unavailable ({type(exc).__name__})"
        else:
            if pw:
                log.debug("Using SMTP password from OS keyring for %s", cfg.smtp_user)
                return pw
            keyring_problem = (
                f"no keyring entry for service {KEYRING_SERVICE!r}, user {cfg.smtp_user!r}"
            )

    if not required:
        return None
    raise SendError(
        "No SMTP password available. Provide it in one of two ways:\n"
        f"  1. Environment variable: export {PASSWORD_ENV}='your-app-password'\n"
        "  2. OS keyring (recommended): python -m math2kindle.send set-password\n"
        f"     (stores it under service {KEYRING_SERVICE!r}, user {cfg.smtp_user!r};\n"
        "      needs `pip install keyring`)\n"
        f"Keyring status: {keyring_problem}.\n"
        "Passwords are never read from the config file."
    )


def store_password(cfg_or_user: Config | str, password: str) -> None:
    user = cfg_or_user.smtp_user if isinstance(cfg_or_user, Config) else cfg_or_user
    kr = _keyring()
    if kr is None:
        raise SendError(
            "The `keyring` package is not installed, so the password cannot be stored.\n"
            f"Run `pip install keyring`, or set ${PASSWORD_ENV} instead."
        )
    try:
        kr.set_password(KEYRING_SERVICE, user, password)
    except Exception as exc:
        raise SendError(
            f"Could not write to the OS keyring ({type(exc).__name__}). "
            f"Set ${PASSWORD_ENV} instead."
        ) from None


# --------------------------------------------------------------------------- #
# Message construction
# --------------------------------------------------------------------------- #


def _attachment_type(path: Path) -> tuple[str, str]:
    ext = path.suffix.lower()
    if ext in REJECTED_TYPES:
        raise SendError(
            f"{path.name}: {REJECTED_TYPES[ext]} files cannot be sent. Amazon's Send to "
            "Kindle e-mail service no longer accepts Kindle-native formats "
            "(.azw3/.azw/.mobi/.kfx); send the EPUB instead and Amazon converts it."
        )
    try:
        return MIME_TYPES[ext]
    except KeyError:
        raise SendError(
            f"{path.name}: unsupported file type {ext or '(none)'!r}. "
            f"Supported here: {', '.join(MIME_TYPES)}."
        ) from None


MAX_FILENAME = 60


def _translit(ch: str) -> str:
    """Greek letters -> their names (ζ -> zeta); other characters unchanged."""
    if "\u0370" <= ch <= "\u03ff":
        try:
            name = unicodedata.name(ch)
        except ValueError:
            return ch
        m = re.match(r"GREEK (CAPITAL|SMALL) LETTER (\w+)$", name)
        if m:
            w = m.group(2).lower().replace("lamda", "lambda")
            return w.capitalize() if m.group(1) == "CAPITAL" else w
    return ch


def _safe_filename(name: str) -> str:
    """Short, ASCII-only attachment name (<= 60 chars, accents transliterated).

    A plain ASCII name keeps the MIME header simple (no RFC 2231 encoding or
    long folded parameters, which some mail paths mangle).  Nothing is lost:
    Amazon titles the document from the EPUB's own metadata."""
    p = Path(name)
    ext = p.suffix.lower() if re.fullmatch(r"\.[A-Za-z0-9]{1,5}", p.suffix) else ""
    stem = name[: -len(p.suffix)] if ext else name
    stem = "".join(_translit(ch) for ch in unicodedata.normalize("NFC", stem))
    stem = re.sub(r"[\u2010-\u2015\u2212]", "-", stem)                     # dashes
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"[^A-Za-z0-9 ._()+,-]", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" .-_")
    room = MAX_FILENAME - len(ext)
    if len(stem) > room:
        cut = stem[:room]
        stem = (cut.rsplit(" ", 1)[0] if " " in cut[room // 2:] else cut).rstrip(" .,-_")
    return (stem or "book") + ext


def _epub_title(path: Path) -> str | None:
    """Read dc:title from the EPUB's OPF package document, if possible."""
    try:
        with zipfile.ZipFile(path) as zf:
            container = ElementTree.fromstring(zf.read("META-INF/container.xml"))
            rootfile = container.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
            if rootfile is None:
                return None
            opf = ElementTree.fromstring(zf.read(rootfile.attrib["full-path"]))
            el = opf.find(".//{http://purl.org/dc/elements/1.1/}title")
            if el is not None and el.text and el.text.strip():
                return " ".join(el.text.split())
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, OSError):
        pass
    return None


def book_title(path: Path) -> str:
    if path.suffix.lower() == ".epub":
        title = _epub_title(path)
        if title:
            return title
    return path.stem


def _check_size(path: Path) -> int:
    size = path.stat().st_size
    if size > MAX_ATTACHMENT_BYTES:
        raise SendError(
            f"{path.name} is {size / 1e6:.1f} MB; Send to Kindle by e-mail accepts at most "
            f"{MAX_ATTACHMENT_BYTES / 1e6:.0f} MB. Reduce image resolution, split the "
            "document, or upload it via https://www.amazon.com/sendtokindle instead."
        )
    if size == 0:
        raise SendError(f"{path.name} is empty.")
    return size


def build_message(path: Path, cfg: Config) -> EmailMessage:
    """Build the complete MIME message (one attachment) for ``path``."""
    path = Path(path)
    if not path.is_file():
        raise SendError(f"File not found: {path}")
    maintype, subtype = _attachment_type(path)
    _check_size(path)

    msg = EmailMessage(policy=SMTP_POLICY)
    msg["From"] = cfg.from_email
    msg["To"] = cfg.kindle_email
    msg["Subject"] = book_title(path)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(idstring="math2kindle", domain=cfg.from_email.rsplit("@", 1)[1])
    msg.set_content(
        "Sent by math2kindle.\n", subtype="plain", charset="utf-8"
    )

    filename = _safe_filename(path.name)
    data = path.read_bytes()
    # ASCII-only and <= 60 characters, so it is a plain quoted parameter.
    msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    part = next(msg.iter_attachments())
    part.set_param("name", filename)  # legacy Content-Type name, also RFC 2231
    return msg


# --------------------------------------------------------------------------- #
# SMTP
# --------------------------------------------------------------------------- #

_TRANSIENT = (
    smtplib.SMTPConnectError,
    smtplib.SMTPServerDisconnected,
    ConnectionError,  # refused / reset / aborted
    TimeoutError,  # includes socket.timeout
)


def _connect(cfg: Config) -> smtplib.SMTP:
    ctx = ssl.create_default_context()
    if cfg.smtp_security == "ssl":
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(
            cfg.smtp_host, cfg.smtp_port, timeout=cfg.smtp_timeout, context=ctx
        )
        smtp.ehlo()
    else:
        smtp = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=cfg.smtp_timeout)
        smtp.ehlo()
        if cfg.smtp_security == "starttls":
            if not smtp.has_extn("starttls"):
                smtp.close()
                raise SendError(
                    f"{cfg.smtp_host}:{cfg.smtp_port} does not offer STARTTLS. "
                    "Use smtp_security = 'ssl' with port 465, or check the port."
                )
            smtp.starttls(context=ctx)
            smtp.ehlo()
    return smtp


def _login(smtp: smtplib.SMTP, cfg: Config, password: str | None) -> None:
    if not smtp.has_extn("auth"):
        if cfg.smtp_security == "none":
            log.info("Server does not advertise AUTH; sending unauthenticated (test mode).")
            return
        smtp.close()
        raise SendError(f"{cfg.smtp_host} does not offer SMTP AUTH on port {cfg.smtp_port}.")
    if password is None:
        password = get_password(cfg)
    log.info("Logging in to %s:%s as %s", cfg.smtp_host, cfg.smtp_port, cfg.smtp_user)
    try:
        smtp.login(cfg.smtp_user, password)
    except smtplib.SMTPAuthenticationError as exc:
        smtp.close()
        raise SendError(f"{AUTH_HINT}\n(Server said: {exc.smtp_code})") from None


def _with_retry(cfg: Config, action):
    """Run ``action()``; retry once on transient connection errors only."""
    for attempt in (1, 2):
        try:
            return action()
        except _TRANSIENT as exc:
            if attempt == 2:
                raise SendError(
                    f"Could not talk to {cfg.smtp_host}:{cfg.smtp_port} "
                    f"({type(exc).__name__}: {exc}). Check smtp_host/smtp_port/"
                    "smtp_security and your network connection."
                ) from None
            log.warning(
                "Connection problem with %s:%s (%s); retrying once in %.0fs",
                cfg.smtp_host, cfg.smtp_port, type(exc).__name__, RETRY_DELAY,
            )
            time.sleep(RETRY_DELAY)
        except socket.gaierror:
            raise SendError(f"Cannot resolve SMTP host {cfg.smtp_host!r}.") from None
        except ssl.SSLError as exc:
            raise SendError(
                f"TLS error with {cfg.smtp_host}:{cfg.smtp_port} ({exc.reason}). "
                "Port 587 needs smtp_security='starttls'; port 465 needs 'ssl'."
            ) from None


def check_login(cfg: Config) -> None:
    """Connect, negotiate TLS and log in, without sending anything."""
    password = get_password(cfg, required=cfg.smtp_security != "none")

    def action() -> None:
        smtp = _connect(cfg)
        try:
            _login(smtp, cfg, password)
        finally:
            try:
                smtp.quit()
            except (smtplib.SMTPException, OSError):
                smtp.close()

    _with_retry(cfg, action)


def _deliver(msg: EmailMessage, cfg: Config, password: str | None) -> None:
    raw = msg.as_bytes()
    if cfg.smtp_host.lower() in ("smtp.gmail.com", "smtp.googlemail.com") and len(raw) > GMAIL_MAX_MESSAGE_BYTES:
        raise SendError(
            f"The encoded message is {len(raw) / 1e6:.1f} MB but Gmail rejects messages "
            f"over {GMAIL_MAX_MESSAGE_BYTES / 1e6:.0f} MB (attachments grow ~33% when "
            "encoded, so ~18 MB is the practical limit). Use another SMTP provider or "
            "upload via https://www.amazon.com/sendtokindle."
        )
    sent = False

    def action() -> None:
        nonlocal sent
        smtp = _connect(cfg)
        try:
            _login(smtp, cfg, password)
            limit = smtp.esmtp_features.get("size", "")
            if limit.isdigit() and 0 < int(limit) < len(raw):
                raise SendError(
                    f"The encoded message is {len(raw) / 1e6:.1f} MB but {cfg.smtp_host} "
                    f"accepts at most {int(limit) / 1e6:.1f} MB."
                )
            try:
                refused = smtp.send_message(msg, from_addr=cfg.from_email, to_addrs=[cfg.kindle_email])
            except smtplib.SMTPRecipientsRefused as exc:
                raise SendError(f"Server refused recipient {cfg.kindle_email}: {exc.recipients}") from None
            except smtplib.SMTPSenderRefused as exc:
                raise SendError(
                    f"Server refused sender {cfg.from_email} ({exc.smtp_code}). With Gmail, "
                    "from_email must be your account address or a verified alias."
                ) from None
            except smtplib.SMTPDataError as exc:
                raise SendError(f"Server rejected the message ({exc.smtp_code}): {exc.smtp_error!r}") from None
            sent = True
            if refused:
                raise SendError(f"Server refused recipient(s): {refused}")
        finally:
            try:
                smtp.quit()
            except (smtplib.SMTPException, OSError):
                smtp.close()

    def guarded() -> None:
        if not sent:  # never resend once the server has accepted the message
            action()

    _with_retry(cfg, guarded)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def _summary(msg: EmailMessage, path: Path) -> str:
    part = next(msg.iter_attachments())
    size = path.stat().st_size
    return (
        f"  To:         {msg['To']}\n"
        f"  From:       {msg['From']}\n"
        f"  Subject:    {msg['Subject']}\n"
        f"  Attachment: {part.get_filename()} ({part.get_content_type()}, "
        f"{size:,} bytes / {size / 1e6:.2f} MB)"
    )


def send_epub(
    epub_path: Path,
    *,
    config_path: Path | None = None,
    dry_run: bool = False,
) -> None:
    """E-mail ``epub_path`` to the Kindle address in the config.

    With ``dry_run=True`` the full message is written to ``<file>.eml`` next to
    the input and nothing is sent (no password needed, no network).
    Raises :class:`SendError` (or its subclass :class:`ConfigError`) on failure.
    """
    path = Path(epub_path).expanduser()
    if not dry_run and is_text_fallback_epub(path):
        raise SendError(f"{path.name} was made by the plain-text fallback (formulas are garbled), so it is "
                        "not e-mailed. Fix marker-pdf and convert the paper again.")
    cfg = load_config(config_path)
    msg = build_message(path, cfg)

    if dry_run:
        eml = path.with_suffix(".eml")
        eml.write_bytes(msg.as_bytes())
        print(f"Dry run: message NOT sent; written to {eml}\n{_summary(msg, path)}")
        return

    password = get_password(cfg, required=cfg.smtp_security != "none")
    log.info("Sending %s to %s via %s:%s", path.name, cfg.kindle_email, cfg.smtp_host, cfg.smtp_port)
    _deliver(msg, cfg, password)
    print(f"Sent to Kindle.\n{_summary(msg, path)}")
    print(APPROVED_LIST_HINT.format(sender=cfg.from_email), file=sys.stderr)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m math2kindle.send",
        description=(
            "E-mail a book to your Kindle via Send to Kindle.\n\n"
            "  python -m math2kindle.send FILE [--config PATH] [--dry-run]\n"
            "  python -m math2kindle.send check [--config PATH]\n"
            "  python -m math2kindle.send set-password [--config PATH] [--user USER]"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("send", help="send FILE (default when the first argument is a file)")
    s.add_argument("file", type=Path)
    s.add_argument("--config", type=Path)
    s.add_argument("--dry-run", action="store_true", help="write a .eml next to FILE; do not connect")
    c = sub.add_parser("check", help="validate config and test the SMTP login without sending")
    c.add_argument("--config", type=Path)
    sp = sub.add_parser("set-password", help="prompt for the SMTP password and store it in the OS keyring")
    sp.add_argument("--config", type=Path)
    sp.add_argument("--user", help="keyring username (default: smtp_user from the config)")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in ("send", "check", "set-password", "-h", "--help"):
        argv.insert(0, "send")
    args = _parser().parse_args(argv)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    try:
        if args.command == "send":
            send_epub(args.file, config_path=args.config, dry_run=args.dry_run)
        elif args.command == "check":
            cfg = load_config(args.config)
            print(f"Config OK: {cfg.source}")
            print(f"  kindle_email: {cfg.kindle_email}\n  from_email:   {cfg.from_email}")
            print(f"  smtp:         {cfg.smtp_user} @ {cfg.smtp_host}:{cfg.smtp_port} ({cfg.smtp_security})")
            check_login(cfg)
            print("SMTP login OK. Nothing was sent.")
            print(APPROVED_LIST_HINT.format(sender=cfg.from_email))
        elif args.command == "set-password":
            if args.user:
                user = args.user
            else:
                user = load_config(args.config).smtp_user
            pw = getpass.getpass(f"SMTP password (App Password) for {user}: ")
            if not pw:
                raise SendError("Empty password; nothing stored.")
            if getpass.getpass("Repeat to confirm: ") != pw:
                raise SendError("Passwords did not match; nothing stored.")
            store_password(user, pw.replace(" ", "") if len(pw.replace(" ", "")) == 16 else pw)
            print(f"Stored in OS keyring (service {KEYRING_SERVICE!r}, user {user!r}).")
    except SendError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())

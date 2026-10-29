"""Secret redaction for logs, SQLite audit rows, error strings and API output."""
import logging
import re

_PATTERNS = [
    (re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}"), "bot<redacted>"),
    (re.compile(r"gsk_[A-Za-z0-9]{10,}"), "gsk_<redacted>"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"), "<jwt-redacted>"),
    (re.compile(r"[A-Z0-9]{6,14}-\d{3}:[^\s\"',}]+"), "<fyers-auth-redacted>"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer <redacted>"),
    (re.compile(r"(?i)(access_token|auth_code|refresh_token|appIdHash|app_secret|api_key|secret|token|authorization|code|state)"
                r"([\"']?\s*[=:]\s*[\"']?)([^&\s\"',}]+)"), r"\1\2<redacted>"),
]
_SECRET_KEYS = re.compile(r"(?i)(token|secret|api_key|authorization|password|passphrase|access_key)")


def redact(text, extra_secrets=()):
    if text is None:
        return text
    s = str(text)
    for sec in extra_secrets:
        if sec and len(sec) >= 6:
            s = s.replace(sec, "<redacted>")
    for pat, rep in _PATTERNS:
        s = pat.sub(rep, s)
    return s


def scrub_obj(obj, extra_secrets=()):
    if isinstance(obj, dict):
        return {k: ("<redacted>" if _SECRET_KEYS.search(str(k)) and v else scrub_obj(v, extra_secrets))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub_obj(v, extra_secrets) for v in obj]
    if isinstance(obj, str):
        return redact(obj, extra_secrets)
    return obj


class RedactingFilter(logging.Filter):
    def __init__(self, secrets_fn=lambda: ()):
        super().__init__()
        self.secrets_fn = secrets_fn

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        record.msg = redact(msg, self.secrets_fn())
        record.args = ()
        return True


def install_log_redaction(secrets_fn=lambda: ()):
    """Attach redaction to root + werkzeug handlers; silence per-request access logs (URLs carry auth codes)."""
    flt = RedactingFilter(secrets_fn)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for h in root.handlers:
        h.addFilter(flt)
    root.addFilter(flt)
    wz = logging.getLogger("werkzeug")
    wz.setLevel(logging.WARNING)
    wz.addFilter(flt)
    for h in wz.handlers:
        h.addFilter(flt)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return flt

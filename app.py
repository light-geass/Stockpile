"""Flask entrypoint. Binds 127.0.0.1 only; debug OFF. Reach it from outside only through your own HTTPS tunnel (ngrok).
Security model:
  * Host header allow-list (loopback + the host in FYERS_REDIRECT_URI). Forwarded headers are NEVER trusted for host/scheme.
  * A request is "tunnel/public" if its Host is the tunnel host OR it carries X-Forwarded-* headers (ngrok adds them).
    Tunnel requests must unlock with DASHBOARD_ACCESS_KEY. Plain local requests are the machine owner.
  * /auth/fyers/callback is intentionally reachable publicly but is useless without a matching, single-use, 10-min OAuth state."""
import hmac
import logging
import os
import secrets
import threading
import time
from pathlib import Path

from flask import Flask, Response, abort, jsonify, redirect, request, session, url_for
from flask.sessions import SecureCookieSessionInterface
from markupsafe import escape

import config
from fyers_auth import AuthError, FyersAuth, TokenStore
from instrument_search import (CatalogueError, InstrumentCatalogue, MAX_QUERY as SEARCH_MAX_QUERY,
                               MIN_QUERY as SEARCH_MIN_QUERY)
from pipeline import Pipeline
from security import install_log_redaction, redact
from storage import Store
from watchlists import WatchlistError, WatchlistStore

log = logging.getLogger("app")
BASE = Path(__file__).resolve().parent
PUBLIC_ENDPOINTS = {"healthz", "fyers_callback", "unlock"}
FWD_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded")
# ?auth=<kind> on return from the OAuth callback, rendered by the dashboard through /api/auth-notice/<kind>.
AUTH_NOTICES = {
    "ok": ("good", "FYERS connected. Token stored locally on the server."),
    "denied": ("warn", "FYERS login was cancelled or denied."),
    "invalid_state": ("bad", "Login check failed (invalid or expired state). Start again from this page."),
    "failed": ("bad", "FYERS rejected the code exchange. Check the app ID, secret and redirect URL."),
    "unavailable": ("bad", "Could not reach FYERS. Try again shortly."),
    "missing": ("warn", "FYERS returned no authorization code."),
}


def _route_yfinance_proxy(s):
    """Yahoo blocks datacentre IPs (Render included) on its ``.info`` endpoint, which silently starved the
    Fundamentalist. yfinance honours these variables, so point it at a residential proxy when one is configured.
    Nothing is set unless the operator provides it: behaviour is unchanged without a proxy."""
    for k in ("YFINANCE_PROXY", "PROXY_URL"):
        v = os.environ.get(k, "").strip()
        if v:
            os.environ.setdefault("HTTP_PROXY", v)
            os.environ.setdefault("HTTPS_PROXY", v)
            logging.getLogger("app").info("Yahoo/yfinance requests routed through a proxy (%s)", k)
            return


class HostAwareSessionInterface(SecureCookieSessionInterface):
    """Secure cookie flag per request: True on the HTTPS tunnel host, False on plain loopback (browsers drop Secure cookies there)."""

    def get_cookie_secure(self, app):
        s = app.config["SETTINGS"]
        return bool(s.tunnel_host) and request.host.lower() == s.tunnel_host and s.fyers_redirect_uri.startswith("https://")


def create_app(settings=None, deps=None, store_path=None):
    s = settings or config.Settings.from_env()
    _route_yfinance_proxy(s)
    app = Flask(__name__)
    app.config.update(SETTINGS=s, SECRET_KEY=s.flask_secret_key, SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_NAME="sd_session",
                      PERMANENT_SESSION_LIFETIME=8 * 3600, MAX_CONTENT_LENGTH=64 * 1024)
    app.session_interface = HostAwareSessionInterface()
    app.debug = False
    app.json.sort_keys = False  # keep agent pipeline order in JSON
    install_log_redaction(s.secret_values)

    store = Store(store_path or (s.data_dir / "audit.sqlite3"), s.secret_values())
    auth = FyersAuth(s, TokenStore(config.LOCAL_DIR / "fyers_token.json"), (deps or {}).get("http", __import__("requests")))
    watchlists = (deps or {}).get("watchlists") or WatchlistStore(s.database_url, s.watchlist_max_items)
    pipe = Pipeline(s, auth, store, deps, watchlists)
    catalogue = (deps or {}).get("catalogue") or InstrumentCatalogue(s.instrument_catalogue_url,
                                                                     s.instrument_catalogue_ttl)
    app.extensions.update(auth=auth, pipeline=pipe, store=store, catalogue=catalogue, watchlists=watchlists)
    threading.Thread(target=catalogue.ensure_loaded, daemon=True, name="catalogue-warmup").start()
    unlock_attempts = []  # timestamps (global limiter: tunnel brute-force guard)
    search_attempts = []  # timestamps (per-session throttle for the symbol master download trigger)

    # ---------------- request classification ----------------
    def allowed_hosts():
        h = {f"127.0.0.1:{s.port}", f"localhost:{s.port}", f"[::1]:{s.port}", "127.0.0.1", "localhost"}
        if s.tunnel_host:
            h.add(s.tunnel_host)
        return h

    def is_tunnel():
        return (request.host.lower() == s.tunnel_host and bool(s.tunnel_host)) or any(request.headers.get(h) for h in FWD_HEADERS)

    @app.before_request
    def gate():
        if request.host.lower() not in allowed_hosts():
            abort(400, "Unrecognised Host header.")
        if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint == "static":
            return None
        if is_tunnel() and not session.get("unlocked"):
            if request.path == "/" and request.method == "GET":
                return redirect("/unlock")
            return jsonify(error="locked", message="Unlock the dashboard first."), 401
        if request.method in ("POST", "PATCH", "DELETE"):
            origin = request.headers.get("Origin")
            if origin and origin.split("://", 1)[-1].lower() != request.host.lower():
                abort(403, "Cross-origin request blocked.")
            tok = session.get("csrf")
            if not tok or not hmac.compare_digest(tok, request.headers.get("X-CSRF-Token", "")):
                abort(403, "Missing or invalid CSRF token.")
        return None

    @app.after_request
    def headers(resp):
        nonce = getattr(request, "_nonce", "")
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            f"default-src 'none'; script-src 'self' 'nonce-{nonce}'; style-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        if request.host.lower() == s.tunnel_host and s.fyers_redirect_uri.startswith("https://"):
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        return resp

    # ---------------- pages ----------------
    @app.get("/healthz")
    def healthz():
        return jsonify(ok=True)

    @app.get("/")
    def index():
        request._nonce = secrets.token_urlsafe(16)
        session.permanent = True
        session.setdefault("csrf", secrets.token_urlsafe(32))
        html = (BASE / "dashboard.html").read_text(encoding="utf-8")
        html = html.replace("__NONCE__", request._nonce).replace("__CSRF__", session["csrf"]).replace("__BRAND__", str(escape(s.brand)))
        return Response(html, mimetype="text/html")

    @app.route("/unlock", methods=["GET", "POST"])
    def unlock():
        request._nonce = ""
        msg = ""
        if request.method == "POST":
            now = time.time()
            unlock_attempts[:] = [t for t in unlock_attempts if now - t < 60]
            if len(unlock_attempts) >= 6:
                msg = "Too many attempts. Wait a minute."
            else:
                unlock_attempts.append(now)
                if hmac.compare_digest(request.form.get("key", "").encode(), s.dashboard_access_key.encode()):
                    session.permanent = True
                    session["unlocked"] = True
                    session["csrf"] = secrets.token_urlsafe(32)
                    return redirect("/")
                msg = "Incorrect key."
        body = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                f"<title>{escape(s.brand)}: unlock</title><style>body{{font-family:system-ui,sans-serif;background:#f6f5f1;color:#1d2433;"
                f"display:grid;place-items:center;min-height:100vh;margin:0}}form{{background:#fff;padding:32px;border-radius:16px;"
                f"box-shadow:0 8px 30px rgba(20,30,60,.08);width:min(360px,90vw)}}input,button{{width:100%;padding:12px;margin-top:12px;"
                f"border-radius:10px;border:1px solid #d9d6cc;font-size:15px;box-sizing:border-box}}button{{background:#1d2433;color:#fff;border:0;"
                f"cursor:pointer}}p{{color:#8a4b2a;margin:8px 0 0;font-size:14px}}small{{color:#6b7280}}</style>"
                f"<form method=post><h2 style='margin:0 0 4px'>{escape(s.brand)}</h2><small>This dashboard is reached through a public tunnel. "
                f"Enter the access key from <code>.local/dashboard_access_key</code> or your .env.</small>"
                f"<input type=password name=key placeholder='Access key' autofocus autocomplete=off><button>Unlock</button>"
                f"{'<p>' + str(escape(msg)) + '</p>' if msg else ''}</form>")
        return Response(body, mimetype="text/html")

    # ---------------- JSON API ----------------
    @app.get("/api/config")
    def api_config():
        notice = session.pop("auth_notice", None)
        return jsonify(brand=s.brand, fyers=auth.status(), tunnel=bool(s.tunnel_host), redirect_host=s.redirect_host,
                       on_tunnel_host=request.host.lower() == s.tunnel_host,
                       groq=s.groq_configured, model=s.groq_model if s.groq_configured else None,
                       telegram=s.telegram_ready, threshold=s.confidence_threshold, notice=notice)

    @app.get("/api/status")
    def api_status():
        snap = pipe.snapshot()
        snap["running"] = pipe.running
        st = catalogue.status()
        snap["search"] = {"ready": catalogue.ready(), "stale": st.get("stale"), "error": st.get("error"),
                          "min_query": SEARCH_MIN_QUERY}
        return jsonify(snap)

    @app.post("/api/start")
    def api_start():
        mode = (request.get_json(silent=True) or {}).get("mode", "live")
        if mode != "live":
            return jsonify(error="bad_mode", message="mode must be live"), 400
        wid = (request.get_json(silent=True) or {}).get("watchlist_id")
        if wid is not None and (not isinstance(wid, (str, int)) or not str(wid).strip() or len(str(wid)) > 64):
            return jsonify(error="bad_watchlist", message="Unknown watchlist."), 400
        wid = str(wid).strip() if wid is not None else None
        if not pipe.start("live", watchlist_id=wid):
            return jsonify(error="busy", message="A run is already in progress."), 409
        return jsonify(started=True, mode="live", scope="watchlist", watchlist_id=wid), 202

    # ---------------- watchlists (Supabase) ----------------
    _WL_ERRORS = {"unavailable": 503, "not_found": 404, "duplicate": 409, "too_many": 400,
                  "last_list": 409, "empty": 400}

    def wl_fail(e):
        return jsonify(error=e.kind, message=str(e)), _WL_ERRORS.get(e.kind, 400)

    def wl_name(body):
        name = body.get("name") if isinstance(body, dict) else None
        return " ".join(name.split()) if isinstance(name, str) and 0 < len(name.strip()) <= 60 else None

    def wl_payload(wl, code=200):
        """One response shape for every mutation: the edited list, the full list set and the add-stock flag."""
        wl = dict(wl, items=[{**i, "in_catalogue": catalogue.get_if_fresh(i["provider_symbol"]) is not None}
                             for i in wl["items"]])
        return jsonify(watchlist=wl, watchlists=watchlists.lists(), max_items=watchlists.max_items), code

    @app.get("/api/watchlists")
    def api_watchlists():
        try:
            return jsonify(watchlists=watchlists.lists(), max_items=watchlists.max_items)
        except WatchlistError as e:
            return wl_fail(e)

    @app.post("/api/watchlists")
    def api_watchlist_create():
        name = wl_name(request.get_json(silent=True))
        if not name:
            return jsonify(error="bad_name", message="Give the watchlist a name (up to 60 characters)."), 400
        try:
            return wl_payload(watchlists.create(name), 201)
        except WatchlistError as e:
            return wl_fail(e)

    @app.get("/api/watchlists/<wid>")
    def api_watchlist_get(wid):
        try:
            return wl_payload(watchlists.get(wid))
        except WatchlistError as e:
            return wl_fail(e)

    @app.patch("/api/watchlists/<wid>")
    def api_watchlist_rename(wid):
        name = wl_name(request.get_json(silent=True))
        if not name:
            return jsonify(error="bad_name", message="Give the watchlist a name (up to 60 characters)."), 400
        try:
            return wl_payload(watchlists.rename(wid, name))
        except WatchlistError as e:
            return wl_fail(e)

    @app.delete("/api/watchlists/<wid>")
    def api_watchlist_delete(wid):
        try:
            watchlists.delete(wid)
        except WatchlistError as e:
            return wl_fail(e)
        lists = watchlists.lists()
        return jsonify(deleted=True, watchlists=lists, default_id=lists[0]["id"])

    @app.post("/api/watchlists/<wid>/items")
    def api_watchlist_add(wid):
        """The browser names a provider symbol; the stored fields come from our catalogue, as in /api/analyze-selected."""
        body = request.get_json(silent=True)
        ref = body.get("provider_symbol") if isinstance(body, dict) else None
        if not isinstance(ref, str) or not ref or len(ref) > 60:
            return jsonify(error="bad_instrument", message="Select an instrument from the search list."), 400
        instrument = catalogue.get_if_fresh(ref)
        if instrument is None:
            return jsonify(error="unknown_instrument", message="That instrument is not in the current NSE symbol list."), 400
        try:
            return wl_payload(watchlists.add_item(wid, instrument), 201)
        except WatchlistError as e:
            return wl_fail(e)

    @app.delete("/api/watchlists/<wid>/items/<path:provider_symbol>")
    def api_watchlist_remove(wid, provider_symbol):
        try:
            return wl_payload(watchlists.remove_item(wid, provider_symbol))
        except WatchlistError as e:
            return wl_fail(e)

    # ---------------- search & select (focused analysis) ----------------
    @app.get("/api/instruments/search")
    def api_instruments_search():
        """Autocomplete over the cached FYERS NSE cash master. Never performs a provider call per keystroke."""
        q = (request.args.get("q") or "").strip()
        if len(q) < SEARCH_MIN_QUERY:
            return jsonify(results=[], min_query=SEARCH_MIN_QUERY)
        if len(q) > SEARCH_MAX_QUERY:
            return jsonify(error="query_too_long", message=f"Keep the query under {SEARCH_MAX_QUERY} characters."), 400
        now = time.time()
        search_attempts[:] = [t for t in search_attempts if now - t < 60]
        if len(search_attempts) >= 60:
            return jsonify(error="rate_limited", message="Too many searches. Wait a minute."), 429
        search_attempts.append(now)
        try:
            hits = catalogue.search(q)
        except CatalogueError as e:
            log.warning("instrument catalogue unavailable: %s", e.kind)
            return jsonify(error="catalogue_unavailable", results=[],
                           message="Symbol list is not available right now. Try again shortly."), 503
        fields = ("symbol", "name", "exchange", "asset_type", "provider", "provider_symbol")
        return jsonify(results=[{k: hit.get(k) for k in fields} for hit in hits],
                       stale=catalogue.status().get("stale"))

    @app.post("/api/analyze-selected")
    def api_analyze_selected():
        """Focused run. The browser may only name a symbol; every displayed field comes from our catalogue."""
        body = request.get_json(silent=True)
        ref = body.get("provider_symbol") if isinstance(body, dict) else None
        if not isinstance(ref, str) or not ref or len(ref) > 60:
            return jsonify(error="bad_instrument", message="Select an instrument from the search list."), 400
        instrument = catalogue.get_if_fresh(ref)
        if instrument is None:
            return jsonify(error="unknown_instrument", message="That instrument is not in the current NSE symbol list."), 400
        if not pipe.start("live", instrument):
            return jsonify(error="busy", message="A run is already in progress."), 409
        brief = {k: instrument.get(k) for k in ("symbol", "name", "exchange", "asset_type", "provider_symbol")}
        return jsonify(started=True, mode="live", scope="focused", target=brief), 202

    # ---------------- FYERS OAuth ----------------
    def err_page(title, detail, code=400):
        request._nonce = ""
        return Response(f"<!doctype html><meta charset=utf-8><title>{escape(title)}</title><body style='font-family:system-ui;max-width:560px;"
                        f"margin:12vh auto;padding:0 20px;color:#1d2433'><h2>{escape(title)}</h2><p>{detail}</p><p><a href='/'>Back to dashboard</a></p>",
                        status=code, mimetype="text/html")

    @app.get("/auth/fyers/login")
    def fyers_login():
        if not s.fyers_configured:
            return err_page("FYERS not configured", "Set <code>FYERS_APP_ID</code> and <code>FYERS_APP_SECRET</code> in <code>.env</code>, then restart.")
        if not s.fyers_redirect_uri:
            return err_page("Redirect URI missing", "Set <code>FYERS_REDIRECT_URI</code> in <code>.env</code>.")
        if request.host.lower() != s.redirect_host:
            target = f"{'https' if s.fyers_redirect_uri.startswith('https') else 'http'}://{s.redirect_host}/"
            return err_page("Start login from the redirect host",
                            f"Session cookies and OAuth state only match on the same host as your registered callback. "
                            f"Open <a href='{escape(target)}'>{escape(target)}</a> and click Connect FYERS there.")
        state = secrets.token_urlsafe(32)
        session["fyers_state"], session["fyers_state_ts"] = state, time.time()
        return redirect(auth.build_login_url(state))

    @app.get("/auth/fyers/callback")
    def fyers_callback():
        """Public by necessity (the browser lands here). All checks run BEFORE any code exchange."""
        expected, ts = session.pop("fyers_state", None), session.pop("fyers_state_ts", 0)
        got = request.args.get("state", "")
        if not expected or not got or not hmac.compare_digest(expected, got) or time.time() - ts > 600:
            return redirect("/?auth=invalid_state")
        if request.args.get("s") == "error" or request.args.get("error"):
            return redirect("/?auth=denied")
        code = request.args.get("auth_code")  # NB: FYERS' own `code` param is an HTTP-style status (200), NOT the auth code
        if not code:
            return redirect("/?auth=missing")
        try:
            auth.exchange_code(code)
        except AuthError as e:
            log.warning("FYERS code exchange failed: %s", e.kind)
            return redirect(f"/?auth={'unavailable' if e.kind == 'unavailable' else 'failed'}")
        return redirect("/?auth=ok")  # drops the code from the address bar

    @app.post("/auth/fyers/logout")
    def fyers_logout():
        auth.logout()
        return jsonify(ok=True)

    @app.get("/api/auth-notice/<kind>")
    def auth_notice(kind):
        level, text = AUTH_NOTICES.get(kind, ("warn", "Unknown status."))
        return jsonify(level=level, text=text)

    @app.errorhandler(400)
    @app.errorhandler(403)
    def err(e):
        return jsonify(error="request_rejected", message=str(getattr(e, "description", "Rejected"))), e.code

    return app


def main():
    config.load_env()
    s = config.Settings.from_env()
    app = create_app(s)
    logging.getLogger("app").info("Listening on http://%s:%s (loopback only). Access key file: .local/dashboard_access_key", s.host, s.port)
    if s.host not in ("127.0.0.1", "localhost"):
        logging.getLogger("app").warning("HOST is not loopback. Expose the app only via your HTTPS tunnel.")
    app.run(host=s.host, port=s.port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()

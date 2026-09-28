"""A small, anonymous pastebin. All retention and size rules live on the server."""

import hashlib
import ipaddress
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import click
from flask import Flask, Response, abort, g, jsonify, redirect, render_template, request, url_for
from markupsafe import Markup, escape
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from werkzeug.exceptions import HTTPException

MAX_TEXT_BYTES = 1024 * 1024
MAX_RETENTION = 7 * 24 * 60 * 60
EXPIRIES = {"10m": (600, "10 分钟"), "1h": (3600, "1 小时"), "1d": (86400, "1 天"), "7d": (604800, "7 天")}
SYNTAXES = {
    "text": "纯文本", "bash": "Bash / Shell", "python": "Python", "javascript": "JavaScript",
    "typescript": "TypeScript", "json": "JSON", "html": "HTML", "css": "CSS", "sql": "SQL",
    "yaml": "YAML", "markdown": "Markdown", "diff": "Diff", "go": "Go", "rust": "Rust",
    "c": "C", "cpp": "C++",
}
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{6,}\Z")
REQUEST_LIMIT = MAX_TEXT_BYTES * 6 + 16384  # JSON escapes and form encoding overhead.
ERROR_MESSAGES = {
    400: "提交内容有误，请检查后重试。", 404: "这条 Paste 不存在，或已到期删除。",
    405: "不支持此操作。", 413: "文本超过 1 MB，请缩短后再试。",
    415: "请使用 JSON 或网页表单提交文本。", 429: "提交过于频繁，请 10 分钟后再试。",
    503: "服务暂时繁忙，请稍后重试。",
}
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS pastes (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    author TEXT NOT NULL,
    syntax TEXT NOT NULL,
    content TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 1 AND {MAX_TEXT_BYTES}),
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL CHECK(expires_at > created_at AND expires_at <= created_at + {MAX_RETENTION}),
    CHECK(length(CAST(content AS BLOB)) = size_bytes)
);
CREATE INDEX IF NOT EXISTS pastes_expiry ON pastes(expires_at);
CREATE TABLE IF NOT EXISTS rate_limits (
    key TEXT PRIMARY KEY,
    count INTEGER NOT NULL,
    resets_at INTEGER NOT NULL
);
"""


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SITE_NAME=os.environ.get("PASTEBIN_SITE_NAME", "Pastebin"),
        DATABASE=os.environ.get("PASTEBIN_DATABASE", str(Path(__file__).parent / ".data" / "pastebin.sqlite3")),
        PUBLIC_ORIGIN=os.environ.get("PASTEBIN_PUBLIC_ORIGIN", "").rstrip("/"),
        TRUST_PROXY=os.environ.get("PASTEBIN_TRUST_PROXY", "0") == "1",
        MAX_CONTENT_LENGTH=REQUEST_LIMIT,
        MAX_FORM_MEMORY_SIZE=REQUEST_LIMIT,
        MAX_FORM_PARTS=8,
        MAX_STORAGE_BYTES=int(os.environ.get("PASTEBIN_MAX_STORAGE_BYTES", 512 * 1024 * 1024)),
        MAX_PASTES=int(os.environ.get("PASTEBIN_MAX_PASTES", 10000)),
        RATE_LIMIT=int(os.environ.get("PASTEBIN_RATE_LIMIT", 30)),
        RATE_WINDOW=600,
    )
    if test_config:
        app.config.update(test_config)
    if app.config["PUBLIC_ORIGIN"]:
        origin = urlsplit(app.config["PUBLIC_ORIGIN"])
        if origin.scheme not in ("http", "https") or not origin.hostname or origin.path or origin.query or origin.fragment:
            raise ValueError("PASTEBIN_PUBLIC_ORIGIN must be an HTTP(S) origin without a path")

    def connect():
        db = sqlite3.connect(app.config["DATABASE"], timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout = 10000")
        db.execute("PRAGMA secure_delete = ON")
        return db

    database = Path(app.config["DATABASE"])
    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = connect()
    try:
        db.execute("PRAGMA auto_vacuum = INCREMENTAL")
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(SCHEMA)
    finally:
        db.close()

    def get_db():
        if "db" not in g:
            g.db = connect()
        return g.db

    @app.teardown_appcontext
    def close_db(_error):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    @app.context_processor
    def template_defaults():
        return {"syntaxes": SYNTAXES, "expiries": EXPIRIES, "max_text_bytes": MAX_TEXT_BYTES,
                "site_name": app.config["SITE_NAME"]}

    @app.template_filter("timestamp")
    def timestamp(value):
        return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")

    @app.template_filter("filesize")
    def filesize(value):
        if value < 1024:
            return f"{value} B"
        if value < MAX_TEXT_BYTES:
            return f"{value / 1024:.1f} KB"
        return f"{value / MAX_TEXT_BYTES:.2f} MB"

    @app.before_request
    def check_origin():
        if request.method == "POST":
            expected = app.config["PUBLIC_ORIGIN"] or request.host_url.rstrip("/")
            origin = request.headers.get("Origin")
            if (origin and origin != expected) or request.headers.get("Sec-Fetch-Site") == "cross-site":
                abort(403, description="请在本站提交文本。")

    @app.after_request
    def response_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        # Preserve same-origin form POST origins while keeping paste URLs off other sites.
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "font-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

    def error_message(error):
        if error.description != type(error).description:
            return error.description
        return ERROR_MESSAGES.get(error.code, "请求未能完成，请稍后重试。")

    @app.errorhandler(HTTPException)
    def http_error(error):
        message = error_message(error)
        if request.path.startswith("/api/"):
            response = jsonify(error=message)
        else:
            response = app.make_response(render_template("error.html", status=error.code, message=message))
        response.status_code = error.code
        if error.code == 429:
            response.headers["Retry-After"] = str(app.config["RATE_WINDOW"])
        return response

    @app.errorhandler(sqlite3.Error)
    def database_error(error):
        app.logger.error("Database operation failed: %s", type(error).__name__)
        from werkzeug.exceptions import ServiceUnavailable
        return http_error(ServiceUnavailable())

    def client_key(now):
        address = request.remote_addr or "unknown"
        # Only trust a header overwritten by our loopback Caddy proxy.
        if app.config["TRUST_PROXY"] and address in ("127.0.0.1", "::1"):
            address = request.headers.get("X-Pastebin-Client-IP", address)
        try:
            ip = ipaddress.ip_address(address)
            if ip.version == 6:
                address = str(ipaddress.ip_network(f"{ip}/64", strict=False))
            else:
                address = str(ip)
        except ValueError:
            address = "unknown"
        bucket = now // app.config["RATE_WINDOW"]
        return hashlib.sha256(f"{bucket}:{address}".encode()).hexdigest()

    def validate_paste(data):
        if not isinstance(data, dict):
            abort(400)
        values = {key: data.get(key, default) for key, default in (
            ("content", ""), ("title", ""), ("author", ""), ("syntax", "text"), ("expiry", "1d")
        )}
        if any(not isinstance(value, str) for value in values.values()):
            abort(400, description="提交字段必须是文本。")
        try:
            size = len(values["content"].encode("utf-8"))
            values["title"].encode("utf-8")
            values["author"].encode("utf-8")
        except UnicodeEncodeError:
            abort(400, description="请使用有效的 UTF-8 文本。")
        if size > MAX_TEXT_BYTES:
            abort(413)
        if not values["content"].strip():
            abort(400, description="请先输入要分享的文本。")
        if "\x00" in values["content"]:
            abort(400, description="内容包含空字节，请提交纯文本。")
        for field, maximum in (("title", 120), ("author", 64)):
            values[field] = values[field].strip()
            if len(values[field]) > maximum or any(ord(char) < 32 for char in values[field]):
                abort(400, description="标题或署名过长，或含有不支持的控制字符。")
        if values["syntax"] not in SYNTAXES:
            abort(400, description="请选择支持的文本格式。")
        if values["expiry"] not in EXPIRIES:
            abort(400, description="保存时间只能是 10 分钟、1 小时、1 天或 7 天。")
        values["size_bytes"] = size
        return values

    def save_paste(data):
        values = validate_paste(data)
        now = int(time.time())
        paste = {**values, "created_at": now,
                 "expires_at": now + EXPIRIES[values["expiry"]][0]}
        db = get_db()
        with db:
            # Serialize quota checks and inserts across all workers and threads.
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM pastes WHERE expires_at <= ?", (now,))
            db.execute("DELETE FROM rate_limits WHERE resets_at <= ?", (now,))
            key = client_key(now)
            count = db.execute("SELECT count FROM rate_limits WHERE key = ?", (key,)).fetchone()
            if count and count[0] >= app.config["RATE_LIMIT"]:
                abort(429)
            usage = db.execute("SELECT count(*), coalesce(sum(size_bytes), 0) FROM pastes").fetchone()
            if usage[0] >= app.config["MAX_PASTES"] or usage[1] + paste["size_bytes"] > app.config["MAX_STORAGE_BYTES"]:
                abort(503, description="存储空间暂时已满，请稍后再试。")
            id_length = 6
            while True:
                # token_urlsafe takes a byte count; truncate to the desired character count.
                paste["id"] = secrets.token_urlsafe(id_length)[:id_length]
                inserted = db.execute(
                    "INSERT INTO pastes (id, title, author, syntax, content, size_bytes, created_at, expires_at) "
                    "VALUES (:id, :title, :author, :syntax, :content, :size_bytes, :created_at, :expires_at) "
                    "ON CONFLICT(id) DO NOTHING", paste
                )
                if inserted.rowcount == 1:
                    break
                id_length += 3
            db.execute(
                "INSERT INTO rate_limits (key, count, resets_at) VALUES (?, 1, ?) "
                "ON CONFLICT(key) DO UPDATE SET count = count + 1",
                (key, (now // app.config["RATE_WINDOW"] + 1) * app.config["RATE_WINDOW"]),
            )
        return paste

    def find_paste(paste_id):
        if not ID_PATTERN.fullmatch(paste_id):
            abort(404)
        row = get_db().execute("SELECT * FROM pastes WHERE id = ? AND expires_at > ?", (paste_id, int(time.time()))).fetchone()
        if row is None:
            abort(404)
        return dict(row)

    def paste_metadata(paste):
        origin = app.config["PUBLIC_ORIGIN"] or request.host_url.rstrip("/")
        result = {key: paste[key] for key in ("id", "title", "author", "syntax", "size_bytes", "created_at", "expires_at")}
        result.update(
            url=origin + url_for("view_paste", paste_id=paste["id"]),
            raw_url=origin + url_for("raw_paste", paste_id=paste["id"]),
            download_url=origin + url_for("download_paste", paste_id=paste["id"]),
        )
        return result

    @app.get("/")
    def index():
        return render_template("index.html", values={"expiry": "1d", "syntax": "text"})

    @app.post("/paste")
    def submit_paste():
        # Browsers serialize textarea line endings as CRLF; count what the editor shows.
        values = request.form.to_dict()
        values["content"] = values.get("content", "").replace("\r\n", "\n")
        try:
            paste = save_paste(values)
        except HTTPException as error:
            if error.code in (400, 413, 429, 503) and request.content_length and request.content_length <= REQUEST_LIMIT:
                response = app.make_response((render_template("index.html", values=values, error=error_message(error)), error.code))
                if error.code == 429:
                    response.headers["Retry-After"] = str(app.config["RATE_WINDOW"])
                return response
            raise
        return redirect(url_for("view_paste", paste_id=paste["id"]), code=303)

    @app.post("/api/pastes")
    def api_create():
        if not request.is_json:
            abort(415)
        paste = save_paste(request.get_json())
        response = jsonify(paste_metadata(paste))
        response.status_code = 201
        response.headers["Location"] = url_for("view_paste", paste_id=paste["id"])
        return response

    @app.get("/api/pastes/<paste_id>")
    def api_get(paste_id):
        paste = find_paste(paste_id)
        return jsonify(**paste_metadata(paste), content=paste["content"])

    @app.get("/p/<paste_id>/")
    def view_paste(paste_id):
        paste = find_paste(paste_id)
        content = paste["content"]
        line_count = content.count("\n") + (0 if content.endswith("\n") else 1)
        # Bound highlighting work. Large pastes remain readable and downloadable.
        use_highlight = paste["syntax"] != "text" and paste["size_bytes"] <= 65536 and line_count <= 3000
        if use_highlight:
            lexer = get_lexer_by_name(paste["syntax"], stripnl=False, ensurenl=False)
            rendered = Markup(highlight(content, lexer, HtmlFormatter(nowrap=True)))
        else:
            rendered = escape(content)
        return render_template(
            "paste.html", paste=paste, rendered=rendered, metadata=paste_metadata(paste),
            line_count=line_count, line_numbers="\n".join(str(i) for i in range(1, line_count + 1)) if line_count <= 3000 else "",
            highlight_skipped=paste["syntax"] != "text" and not use_highlight,
        )

    def text_response(paste_id, download=False):
        paste = find_paste(paste_id)
        response = Response(paste["content"].encode("utf-8"), content_type="text/plain; charset=utf-8")
        if download:
            response.headers["Content-Disposition"] = f'attachment; filename="paste-{paste_id}.txt"'
        return response

    @app.get("/p/<paste_id>/raw")
    def raw_paste(paste_id):
        return text_response(paste_id)

    @app.get("/p/<paste_id>/download")
    def download_paste(paste_id):
        return text_response(paste_id, download=True)

    @app.get("/healthz")
    def health():
        get_db().execute("SELECT 1 FROM pastes LIMIT 1")
        return jsonify(status="ok")

    @app.get("/robots.txt")
    def robots():
        return Response("User-agent: *\nDisallow: /\n", content_type="text/plain; charset=utf-8")

    @app.cli.command("cleanup")
    def cleanup():
        """Remove expired content, checkpoint the WAL, and reclaim free pages."""
        db = get_db()
        with db:
            removed = db.execute("DELETE FROM pastes WHERE expires_at <= ?", (int(time.time()),)).rowcount
            db.execute("DELETE FROM rate_limits WHERE resets_at <= ?", (int(time.time()),))
        db.execute("PRAGMA incremental_vacuum(2000)")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        click.echo(f"Removed {removed} expired paste(s).")

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=int(os.environ.get("PASTEBIN_PORT", "8000")))

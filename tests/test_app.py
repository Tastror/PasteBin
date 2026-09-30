import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app import EXPIRIES, MAX_TEXT_BYTES, REQUEST_LIMIT, create_app


class PastebinTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = str(Path(self.directory.name) / "pastes.sqlite3")
        self.config = {"TESTING": True, "DATABASE": self.database, "RATE_LIMIT": 1000}
        self.app = create_app(self.config)
        self.client = self.app.test_client()

    def tearDown(self):
        self.directory.cleanup()

    def post(self, content="Hello, 世界!\n", **kwargs):
        return self.client.post("/api/pastes", json={"content": content, **kwargs})

    def test_home_has_no_announced_limits_or_registration(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        for phrase in ("1 MB", "最长", "上限", "/register", "/login", "永久", "让分享简单一点", "关于你的 Paste", "<aside"):
            self.assertNotIn(phrase, text)
        self.assertIn('<option value="7d">7 天</option>', text)

    def test_create_read_raw_and_download_preserve_unicode_and_whitespace(self):
        content = "\n\t你好 🐧\r\nlast line  "
        response = self.post(content, title="示例", author="Tester", syntax="python")
        self.assertEqual(response.status_code, 201)
        record = response.json
        self.assertEqual(len(record["id"]), 6)
        self.assertEqual(record["url"], f'http://localhost/p/{record["id"]}')
        self.assertEqual(response.headers["Location"], f'/p/{record["id"]}')
        self.assertEqual(record["size_bytes"], len(content.encode("utf-8")))
        raw = self.client.get(f'/p/{record["id"]}/raw')
        self.assertEqual(raw.data, content.encode("utf-8"))
        self.assertEqual(raw.content_type, "text/plain; charset=utf-8")
        download = self.client.get(f'/p/{record["id"]}/download')
        self.assertEqual(download.data, raw.data)
        self.assertIn("attachment;", download.headers["Content-Disposition"])
        api = self.client.get(f'/api/pastes/{record["id"]}')
        self.assertEqual(api.json["content"], content)
        self.assertEqual(self.client.get(response.headers["Location"]).status_code, 200)

    def test_form_redirects_without_javascript(self):
        response = self.client.post("/paste", data={"content": "from form", "expiry": "7d"})
        self.assertEqual(response.status_code, 303)
        self.assertRegex(response.location, r"^/p/[A-Za-z0-9_-]{6}$")
        self.assertEqual(self.client.get(response.location).status_code, 200)

    def test_new_ids_reject_uppercase_i_and_lowercase_l_without_growing(self):
        with patch("app.secrets.token_urlsafe", side_effect=["Ibc123XY", "abc12lXY", "iL1_-0XY"]):
            response = self.post("unambiguous id")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["id"], "iL1_-0")
        self.assertEqual(self.client.get(response.headers["Location"]).status_code, 200)

    def test_collisions_extend_ids_by_three_without_changing_existing_pastes(self):
        self.app.config["RATE_LIMIT"] = 9
        records = []
        with patch("app.secrets.token_urlsafe", return_value="a" * 32):
            for length in (6, 9, 12, 15, 18, 21, 24):
                content = f"paste with {length} character id"
                response = self.post(content)
                self.assertEqual(response.status_code, 201)
                self.assertEqual(len(response.json["id"]), length)
                records.append((response.json, content))
        with patch("app.secrets.token_urlsafe", side_effect=["a" * 32] * 9 + ["b" * 32]):
            content = "retry at 24 characters until a free id is found"
            response = self.post(content)
            self.assertEqual(response.status_code, 201)
            self.assertEqual(len(response.json["id"]), 24)
            records.append((response.json, content))
        for record, content in records:
            self.assertEqual(self.client.get(f'/p/{record["id"]}/').status_code, 200)
            self.assertEqual(self.client.get(f'/api/pastes/{record["id"]}').json["content"], content)
            for suffix in ("raw", "download"):
                self.assertEqual(self.client.get(f'/p/{record["id"]}/{suffix}').data, content.encode())
        with patch("app.secrets.token_urlsafe", return_value="b" * 32):
            response = self.post("next paste starts at six characters again")
            self.assertEqual(response.status_code, 201)
            self.assertEqual(len(response.json["id"]), 6)
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM pastes").fetchone()[0], 9)
            self.assertEqual(db.execute("SELECT sum(count) FROM rate_limits").fetchone()[0], 9)
        self.assertEqual(self.post().status_code, 429)

    def test_collision_regenerates_the_entire_longer_id(self):
        with patch("app.secrets.token_urlsafe", return_value="abc123XY"):
            first = self.post("original paste").json
        with patch("app.secrets.token_urlsafe", side_effect=["abc123XY", "NewJd9876XYZ"]):
            response = self.post("new paste")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(first["id"], "abc123")
        self.assertEqual(response.json["id"], "NewJd9876")
        self.assertFalse(response.json["id"].startswith(first["id"]))
        self.assertEqual(self.client.get(f'/p/{first["id"]}/raw').data, b"original paste")
        self.assertEqual(self.client.get(f'/p/{response.json["id"]}/raw').data, b"new paste")

    def test_concurrent_id_collisions_preserve_every_paste(self):
        def write(index):
            content = f"concurrent paste {index}"
            with self.app.test_client() as client:
                response = client.post("/api/pastes", json={"content": content})
                return response.status_code, response.json, content
        with patch("app.secrets.token_urlsafe", return_value="c" * 32):
            with ThreadPoolExecutor(max_workers=4) as workers:
                results = list(workers.map(write, range(4)))
        self.assertEqual([status for status, _, _ in results], [201] * 4)
        self.assertEqual(sorted(len(record["id"]) for _, record, _ in results), [6, 9, 12, 15])
        for _, record, content in results:
            self.assertEqual(self.client.get(f'/p/{record["id"]}/raw').data, content.encode())

    def test_legacy_24_character_links_remain_readable(self):
        record = self.post("existing paste").json
        legacy_id = "Il_old-_" + "a" * 16
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE pastes SET id = ? WHERE id = ?", (legacy_id, record["id"]))
        for route in (f"/p/{legacy_id}", f"/p/{legacy_id}/"):
            response = self.client.get(route)
            self.assertEqual(response.status_code, 200)
            self.assertIn(f'id="share-url" value="http://localhost/p/{legacy_id}"', response.get_data(as_text=True))
        api = self.client.get(f"/api/pastes/{legacy_id}")
        self.assertEqual(api.status_code, 200)
        self.assertEqual(api.json["id"], legacy_id)
        self.assertEqual(api.json["url"], f"http://localhost/p/{legacy_id}")
        self.assertEqual(api.json["content"], "existing paste")
        for suffix in ("raw", "download"):
            self.assertEqual(self.client.get(f"/p/{legacy_id}/{suffix}").data, b"existing paste")

    def test_exact_byte_limit_and_one_byte_over(self):
        self.assertEqual(self.post("a" * MAX_TEXT_BYTES).status_code, 201)
        response = self.post("a" * (MAX_TEXT_BYTES + 1))
        self.assertEqual(response.status_code, 413)
        self.assertIn("1 MB", response.json["error"])

    def test_multibyte_limit_is_bytes_not_characters(self):
        exact = "中" * (MAX_TEXT_BYTES // 3) + "a"
        self.assertEqual(len(exact.encode()), MAX_TEXT_BYTES)
        self.assertEqual(self.post(exact).status_code, 201)
        self.assertEqual(self.post(exact + "b").status_code, 413)

    def test_form_supports_maximum_urlencoded_text(self):
        content = "中" * (MAX_TEXT_BYTES // 3) + "a"
        response = self.client.post("/paste", data={"content": content})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.client.get(response.location + "/raw").data.decode(), content)

    def test_browser_form_newlines_match_the_editor_size(self):
        editor_content = "x\n" * (MAX_TEXT_BYTES // 2)
        response = self.client.post("/paste", data={"content": editor_content.replace("\n", "\r\n")})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.client.get(response.location + "/raw").data.decode(), editor_content)

    def test_form_size_error_is_localized_and_preserves_input(self):
        response = self.client.post("/paste", data={"content": "x" * (MAX_TEXT_BYTES + 1), "title": "Keep my title"})
        self.assertEqual(response.status_code, 413)
        html = response.get_data(as_text=True)
        self.assertIn("文本超过 1 MB", html)
        self.assertIn("Keep my title", html)

    def test_json_escape_overhead_does_not_reduce_limit(self):
        content = "\t" * (MAX_TEXT_BYTES - 1) + "x"
        response = self.post(content)
        self.assertEqual(response.status_code, 201)

    def test_all_expiry_choices_and_default(self):
        for key, (seconds, _) in EXPIRIES.items():
            with self.subTest(key=key):
                record = self.post(expiry=key).json
                self.assertEqual(record["expires_at"] - record["created_at"], seconds)
                self.assertLessEqual(seconds, 604800)
        record = self.post().json
        self.assertEqual(record["expires_at"] - record["created_at"], 86400)

    def test_tampered_expiries_and_invalid_fields_are_rejected(self):
        for value in ("8d", "30d", "never", "-1", "604801", "", 604801, None):
            with self.subTest(expiry=value):
                self.assertEqual(self.post(expiry=value).status_code, 400)
        for data in (None, [], "text", {}, {"content": 123}, {"content": "hello", "syntax": "../python"},
                     {"content": "hello", "author": "x" * 65}, {"content": "hello", "title": "x" * 121}):
            response = self.client.post("/api/pastes", data=json.dumps(data), content_type="application/json")
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.post("\ud800").status_code, 400)
        self.assertEqual(self.post("a\x00b").status_code, 400)

    def test_empty_content_rejected(self):
        for content in ("", "  \t\n"):
            self.assertEqual(self.post(content).status_code, 400)

    def test_expired_paste_is_unreadable_everywhere_even_before_cleanup(self):
        with patch("app.time.time", return_value=1_800_000_000):
            record = self.post(expiry="10m").json
        routes = (f'/p/{record["id"]}', f'/p/{record["id"]}/', f'/p/{record["id"]}/raw', f'/p/{record["id"]}/download', f'/api/pastes/{record["id"]}')
        with patch("app.time.time", return_value=record["expires_at"] - 1):
            for route in routes:
                self.assertEqual(self.client.get(route).status_code, 200)
        with patch("app.time.time", return_value=record["expires_at"]):
            for route in routes:
                self.assertEqual(self.client.get(route).status_code, 404)

    def test_cleanup_removes_only_expired_rows_and_releases_wal(self):
        with patch("app.time.time", return_value=1_800_000_000):
            old = self.post(expiry="10m").json
            live = self.post(expiry="7d").json
        with patch("app.time.time", return_value=old["expires_at"]):
            result = self.app.test_cli_runner().invoke(args=["cleanup"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("Removed 1", result.output)
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute("SELECT id FROM pastes").fetchall(), [(live["id"],)])

    def test_database_survives_application_restart(self):
        record = self.post("persistent").json
        restarted = create_app(self.config).test_client()
        self.assertEqual(restarted.get(f'/p/{record["id"]}/raw').data, b"persistent")

    def test_user_html_cannot_execute_even_with_highlighting(self):
        attack = '<script>alert(1)</script><img src=x onerror="alert(2)">'
        for syntax in ("text", "html", "markdown", "javascript"):
            record = self.post(attack, syntax=syntax, title=attack, author='<img src=x onerror="alert(2)">').json
            response = self.client.get(f'/p/{record["id"]}/')
            html = response.get_data(as_text=True)
            self.assertNotIn("<script>alert", html)
            self.assertNotIn("<img src=x", html)
            self.assertNotIn("'unsafe-inline'", response.headers["Content-Security-Policy"])
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")

    def test_large_code_uses_fast_plain_text_fallback(self):
        record = self.post("print(1)\n" * 10000, syntax="python").json
        with patch("app.highlight", side_effect=AssertionError("must not highlight large inputs")):
            self.assertEqual(self.client.get(f'/p/{record["id"]}/').status_code, 200)

    def test_cross_origin_post_rejected_and_own_origin_allowed(self):
        response = self.client.post("/api/pastes", json={"content": "x"}, headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        response = self.client.post("/api/pastes", json={"content": "x"}, headers={"Origin": "http://localhost"})
        self.assertEqual(response.status_code, 201)

    def test_rate_limit_is_persistent_and_cannot_be_spoofed(self):
        self.app.config["RATE_LIMIT"] = 1
        self.assertEqual(self.post().status_code, 201)
        response = self.client.post("/api/pastes", json={"content": "x"}, headers={"X-Pastebin-Client-IP": "1.2.3.4"})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "600")
        restarted = create_app({**self.config, "RATE_LIMIT": 1}).test_client()
        self.assertEqual(restarted.post("/api/pastes", json={"content": "x"}).status_code, 429)

    def test_trusted_proxy_distinguishes_clients_and_rejects_direct_spoofing(self):
        self.app.config.update(RATE_LIMIT=1, TRUST_PROXY=True)
        for address in ("1.2.3.4", "1.2.3.5"):
            response = self.client.post("/api/pastes", json={"content": "x"}, headers={"X-Pastebin-Client-IP": address})
            self.assertEqual(response.status_code, 201)
        for index, address in enumerate(("1.2.3.4", "1.2.3.5")):
            response = self.client.post("/api/pastes", json={"content": "x"}, headers={"X-Pastebin-Client-IP": address}, environ_base={"REMOTE_ADDR": "10.0.0.2"})
            self.assertEqual(response.status_code, 201 if index == 0 else 429)

    def test_storage_quota_is_atomic_under_concurrent_writes(self):
        self.app.config["MAX_STORAGE_BYTES"] = 10
        def write(_):
            with self.app.test_client() as client:
                return client.post("/api/pastes", json={"content": "123456"}).status_code
        with ThreadPoolExecutor(max_workers=4) as workers:
            statuses = list(workers.map(write, range(4)))
        self.assertEqual(statuses.count(201), 1)
        self.assertEqual(statuses.count(503), 3)

    def test_expired_content_does_not_consume_quota(self):
        self.app.config["MAX_PASTES"] = 1
        with patch("app.time.time", return_value=1_800_000_000):
            first = self.post(expiry="10m").json
            self.assertEqual(self.post().status_code, 503)
        with patch("app.time.time", return_value=first["expires_at"]):
            self.assertEqual(self.post().status_code, 201)

    def test_bad_paths_and_unsupported_content_type(self):
        for route in ("/p/nope/", "/p/../../etc/passwd/raw", "/api/pastes", "/.data/pastebin.sqlite3"):
            self.assertIn(self.client.get(route).status_code, (404, 405))
        self.assertEqual(self.client.post("/api/pastes", data="hello").status_code, 415)
        self.assertEqual(self.client.post("/api/pastes", data="{broken", content_type="application/json").status_code, 400)

    def test_oversized_http_body_is_stopped(self):
        response = self.client.post("/api/pastes", data="x" * (REQUEST_LIMIT + 1), content_type="application/json")
        self.assertEqual(response.status_code, 413)

    def test_canonical_links_ignore_untrusted_host(self):
        self.app.config["PUBLIC_ORIGIN"] = "https://paste.example.com"
        response = self.client.post("/api/pastes", json={"content": "x"}, headers={"Host": "evil.example"})
        self.assertEqual(response.json["url"], f'https://paste.example.com/p/{response.json["id"]}')

    def test_site_name_comes_from_configuration_and_is_escaped(self):
        self.app.config["SITE_NAME"] = 'Community <script>alert(1)</script>'
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('Community &lt;script&gt;', html)
        self.assertNotIn('<script>alert(1)</script>', html)

    def test_environment_can_customize_site_without_source_edits(self):
        with patch.dict("os.environ", {"PASTEBIN_SITE_NAME": "Community Paste"}):
            customized = create_app(self.config).test_client()
            self.assertIn("Community Paste", customized.get("/").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()

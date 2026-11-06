"""Публичная страница: что видят посетители и чего они видеть не должны."""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from malinnik import auth, portfolio, sites  # noqa: E402
from malinnik.server import create_server  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
JPEG = b"\xff\xd8\xff" + b"\0" * 32
WEBP = b"RIFF\0\0\0\0WEBP" + b"\0" * 32


class HelpersTests(unittest.TestCase):
    def test_request_host(self):
        for header, expected in (("example.ru", "example.ru"), ("example.ru:443", "example.ru"),
                                 ("192.168.0.1:7698", "192.168.0.1"), (None, "localhost"), ("", "localhost"),
                                 ('evil"><script>', "localhost"), ("a b", "localhost"), ("[::1]:80", "localhost")):
            self.assertEqual(portfolio.request_host(header), expected, header)

    def test_cover_type_by_content_not_name(self):
        self.assertEqual(sites.cover_type(PNG), "image/png")
        self.assertEqual(sites.cover_type(JPEG), "image/jpeg")
        self.assertEqual(sites.cover_type(WEBP), "image/webp")
        for bad in (b"", b"<svg onload=alert(1)>", b"<html>", b"GIF89a", b"RIFF\0\0\0\0WAVE"):
            self.assertIsNone(sites.cover_type(bad), bad)

    def test_public_url_validation(self):
        self.assertEqual(sites._clean_public_url(""), "")
        self.assertEqual(sites._clean_public_url(" https://shop.example.ru/ "), "https://shop.example.ru/")
        for bad in ("javascript:alert(1)", "data:text/html,x", "ftp://x.ru", "//x.ru", "https://", "http://a b",
                    "https://x.ru/" + "a" * 300):
            with self.assertRaises(sites.SiteError, msg=bad):
                sites._clean_public_url(bad)

    def test_page_escapes_everything(self):
        evil = '<script>alert(1)</script>"\''
        page = portfolio.render_page({"title": evil, "subtitle": evil}, [{
            "id": "a", "name": evil, "description": evil, "kind": "static", "online": True,
            "url": 'http://x/"><script>', "cover": 0,
        }])
        self.assertNotIn("<script>", page)

    def test_offline_site_has_no_link(self):
        page = portfolio.render_page({"title": "T", "subtitle": ""}, [{
            "id": "a", "name": "Сайт", "description": "", "kind": None, "online": False,
            "url": "http://secret:8100/", "cover": 0,
        }])
        self.assertNotIn("secret", page)
        self.assertIn("Сейчас недоступен", page)

    def test_empty_page(self):
        self.assertIn("скоро появятся", portfolio.render_page({"title": "T", "subtitle": ""}, []))


class PublicServerTests(unittest.TestCase):
    """Настоящие серверы панели и портфолио и настоящий сайт."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="malinnik-portfolio-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        auth.set_password(self.tmp, "secret")
        self.admin, self.manager = create_server("127.0.0.1", 0, self.tmp)
        self.public = portfolio.create_public_server("127.0.0.1", 0, self.manager)
        self.admin_port = self.admin.server_address[1]
        self.public_port = self.public.server_address[1]
        self.manager.run()
        threading.Thread(target=self.admin.serve_forever, daemon=True).start()
        threading.Thread(target=self.public.serve_forever, daemon=True).start()
        self.addCleanup(self.close)
        self.cookie = None

    def close(self):
        self.public.shutdown()
        self.public.server_close()
        self.admin.shutdown()
        self.admin.server_close()
        self.manager.shutdown()

    def get(self, path, method="GET", body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.public_port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res, data

    def admin_call(self, method, path, body=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.admin_port, timeout=30)
        headers = {"X-Malinnik": "1"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        conn.request(method, path, body=payload, headers=headers)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res, data

    def deploy(self, name="Магазин"):
        res, _ = self.admin_call("POST", "/api/login", {"password": "secret"})
        self.cookie = res.getheader("Set-Cookie").split(";")[0]
        path = os.path.join(self.tmp, "site.zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("index.html", "<h1>сайт</h1>")
        site = self.manager.receive_upload(path, f"{name}.zip", None)
        deadline = time.time() + 30
        while time.time() < deadline and (site.deploy["active"] or site.state() != "running"):
            site.check_health()
            time.sleep(0.1)
        self.assertEqual(site.state(), "running")
        return site

    def page(self):
        res, data = self.get("/", headers={"Host": "agentnik.example.ru"})
        self.assertEqual(res.status, 200)
        return data.decode("utf-8")

    def test_deployed_site_appears_with_link_to_its_port(self):
        site = self.deploy()
        page = self.page()
        self.assertIn("Магазин", page)
        self.assertIn(f"http://agentnik.example.ru:{site.meta['port']}/", page)
        self.assertIn('rel="noopener noreferrer"', page)

    def test_admin_api_does_not_exist_here(self):
        self.deploy()
        for path in ("/api/state", "/api/login", "/api/upload", "/api/sites/magazin/logs", "/api/telegram",
                     "/app.js", "/style.css", "/index.html"):
            res, _ = self.get(path, headers={"X-Malinnik": "1"})
            self.assertEqual(res.status, 404, path)
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            res, _ = self.get("/api/login", method=method, body=b"{}", headers={"X-Malinnik": "1"})
            self.assertEqual(res.status, 405, method)

    def test_secrets_do_not_reach_visitors(self):
        site = self.deploy()
        self.manager.update_settings(site.id, {"env": "API_KEY=очень-секретно", "start_override": ""})
        self.manager.telegram.settings["token"] = "123456:" + "A" * 30
        page = self.page()
        for secret in ("очень-секретно", "API_KEY", "static_server", self.tmp, "AAAAAAAA"):
            self.assertNotIn(secret, page)
        public = self.manager.public_site(site, "x")
        self.assertEqual(set(public), {"id", "name", "description", "kind", "online", "url", "cover"})

    def test_hidden_site_and_its_cover_disappear(self):
        site = self.deploy()
        self.manager.set_cover(site.id, PNG)
        self.assertEqual(self.get(f"/covers/{site.id}")[0].status, 200)
        self.manager.update_settings(site.id, {"show_in_portfolio": False})
        self.assertNotIn("Магазин", self.page())
        self.assertEqual(self.get(f"/covers/{site.id}")[0].status, 404)

    def test_failed_site_never_shown(self):
        res, _ = self.admin_call("POST", "/api/login", {"password": "secret"})
        self.cookie = res.getheader("Set-Cookie").split(";")[0]
        self.manager._create("Черновик")
        self.assertNotIn("Черновик", self.page())

    def test_stopped_site_shown_without_link(self):
        site = self.deploy()
        self.manager.action(site.id, "stop")
        page = self.page()
        self.assertIn("Магазин", page)
        self.assertIn("Сейчас недоступен", page)
        self.assertNotIn(f":{site.meta['port']}/", page)

    def test_settings_and_description_shown(self):
        site = self.deploy()
        res, _ = self.admin_call("POST", "/api/portfolio", {"title": "Мои работы", "subtitle": "Сайты на заказ"})
        self.assertEqual(res.status, 200)
        self.manager.update_settings(site.id, {"description": "Интернет-магазин", "public_url": "https://shop.example.ru/"})
        page = self.page()
        for part in ("Мои работы", "Сайты на заказ", "Интернет-магазин", "https://shop.example.ru/"):
            self.assertIn(part, page)

    def test_settings_survive_restart(self):
        self.manager.update_portfolio({"title": "Новый"})
        again = sites.Manager(self.tmp)
        self.assertEqual(again.portfolio["title"], "Новый")

    def test_invalid_values_rejected(self):
        site = self.deploy()
        res, _ = self.admin_call("POST", "/api/portfolio", {"title": ""})
        self.assertEqual(res.status, 400)
        res, _ = self.admin_call("PATCH", f"/api/sites/{site.id}", {"public_url": "javascript:alert(1)"})
        self.assertEqual(res.status, 400)
        res, _ = self.admin_call("PATCH", f"/api/sites/{site.id}", {"description": "я" * 501})
        self.assertEqual(res.status, 400)

    def test_cover_flow_through_admin(self):
        site = self.deploy()
        path = f"/api/sites/{site.id}/cover"
        res, data = self.admin_call("POST", path, raw=b"<svg onload=alert(1)>")
        self.assertEqual(res.status, 400, data)
        res, _ = self.admin_call("POST", path, raw=b"x" * (5 * 1024 * 1024 + 1))
        self.assertEqual(res.status, 413)
        res, _ = self.admin_call("POST", path, raw=PNG)
        self.assertEqual(res.status, 200)

        res, data = self.get(f"/covers/{site.id}")
        self.assertEqual(res.status, 200)
        self.assertEqual(data, PNG)
        self.assertEqual(res.getheader("Content-Type"), "image/png")
        self.assertEqual(res.getheader("X-Content-Type-Options"), "nosniff")
        self.assertIn(f"/covers/{site.id}?v=", self.page())

        res, data = self.admin_call("GET", path)
        self.assertEqual(data, PNG)
        res, _ = self.admin_call("DELETE", path)
        self.assertEqual(res.status, 200)
        self.assertEqual(self.get(f"/covers/{site.id}")[0].status, 404)

    def test_cover_requires_login(self):
        site = self.deploy()
        self.cookie = None
        res, _ = self.admin_call("POST", f"/api/sites/{site.id}/cover", raw=PNG)
        self.assertEqual(res.status, 401)

    def test_cover_survives_update(self):
        site = self.deploy()
        self.manager.set_cover(site.id, PNG)
        path = os.path.join(self.tmp, "site2.zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("index.html", "новая версия")
        self.manager.receive_upload(path, "x.zip", site.id)
        deadline = time.time() + 30
        while time.time() < deadline and site.deploy["active"]:
            time.sleep(0.1)
        self.assertEqual(site.meta["current"], 2)
        self.assertEqual(self.get(f"/covers/{site.id}")[1], PNG)

    def test_security_headers(self):
        res, _ = self.get("/")
        csp = res.getheader("Content-Security-Policy")
        self.assertIn("default-src 'none'", csp)
        self.assertEqual(res.getheader("X-Frame-Options"), "DENY")

    def test_styles_load(self):
        res, data = self.get("/portfolio.css")
        self.assertEqual(res.status, 200)
        self.assertIn(b"--accent", data)

    def test_path_tricks_do_not_read_files(self):
        for path in ("/../config.json", "/covers/../../config.json", "/portfolio.css/../portfolio.py", "/covers/%2e%2e"):
            res, data = self.get(path)
            self.assertEqual(res.status, 404, path)
            self.assertNotIn(b"hash", data)


if __name__ == "__main__":
    unittest.main()

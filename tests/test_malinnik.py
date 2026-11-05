"""Запуск: python -m unittest discover -s tests"""
import http.client
import io
import json
import os
import shutil
import socket
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import zipfile
from unittest import mock
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from malinnik import archive, auth, detect, metrics, sites, telegram, tunnel  # noqa: E402
from malinnik.server import Handler, create_server  # noqa: E402

LIMIT = 10 ** 9


def make_zip(path, files):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="malinnik-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class ArchiveTests(TempDirCase):
    def extract(self, files):
        path = os.path.join(self.tmp, "a.zip")
        dest = os.path.join(self.tmp, "out")
        make_zip(path, files)
        archive.extract(path, dest, LIMIT)
        return dest

    def test_removes_wrapper_folders(self):
        dest = self.extract({"site/site/index.html": "x", "site/site/a.css": "y"})
        self.assertEqual(sorted(os.listdir(dest)), ["a.css", "index.html"])

    def test_wrapper_with_same_named_child(self):
        dest = self.extract({"site/site/index.html": "x", "site/other.txt": "y"})
        self.assertEqual(sorted(os.listdir(dest)), ["other.txt", "site"])

    def test_rejects_path_traversal(self):
        for bad in ("../evil.txt", "a/../../evil.txt", "..\\evil.txt", "C:\\evil.txt"):
            with self.subTest(bad):
                with self.assertRaises(archive.ArchiveError):
                    self.extract({bad: "x"})
                shutil.rmtree(os.path.join(self.tmp, "out"), ignore_errors=True)

    def test_backslash_paths_from_windows_tools(self):
        dest = self.extract({"css\\main.css": "x", "index.html": "y"})
        self.assertTrue(os.path.isfile(os.path.join(dest, "css", "main.css")))

    def test_russian_names_from_windows_explorer(self):
        path = os.path.join(self.tmp, "a.zip")
        dest = os.path.join(self.tmp, "out")
        info = zipfile.ZipInfo("x.html")
        make_zip(path, {"index.html": "y"})
        # Имя в cp866 без флага UTF-8 — так пишет Проводник на русской Windows.
        with mock.patch.object(zipfile.ZipInfo, "_encodeFilenameFlags",
                               lambda self: (self.filename.encode("cp437"), self.flag_bits)):
            with zipfile.ZipFile(path, "a") as zf:
                info.filename = "Сайт.html".encode("cp866").decode("cp437")
                info.flag_bits = 0
                zf.writestr(info, "z")
        archive.extract(path, dest, LIMIT)
        self.assertIn("Сайт.html", os.listdir(dest))

    def test_removes_windows_build_leftovers(self):
        dest = self.extract({
            "package.json": "{}",
            "node_modules/x/index.js": "x",
            ".venv/pyvenv.cfg": "x",
            ".venv/Scripts/python.exe": "x",
            "__MACOSX/junk": "x",
            "Thumbs.db": "x",
            "app/__pycache__/a.pyc": "x",
            "app/main.js": "x",
        })
        self.assertEqual(sorted(os.listdir(dest)), ["app", "package.json"])
        self.assertEqual(os.listdir(os.path.join(dest, "app")), ["main.js"])

    def test_keeps_node_modules_of_plain_static_site(self):
        dest = self.extract({"index.html": "x", "node_modules/lib/a.js": "x"})
        self.assertTrue(os.path.isfile(os.path.join(dest, "node_modules", "lib", "a.js")))

    def test_fixes_crlf_in_shell_scripts(self):
        dest = self.extract({"start.sh": "#!/bin/sh\r\necho hi\r\n", "a.txt": "x\r\n"})
        with open(os.path.join(dest, "start.sh"), "rb") as f:
            self.assertNotIn(b"\r", f.read())
        with open(os.path.join(dest, "a.txt"), "rb") as f:
            self.assertIn(b"\r", f.read())

    def test_tar_skips_symlinks_and_rejects_traversal(self):
        path = os.path.join(self.tmp, "a.tar.gz")
        with tarfile.open(path, "w:gz") as tf:
            data = b"hello"
            info = tarfile.TarInfo("index.html")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
            link = tarfile.TarInfo("evil")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            tf.addfile(link)
        dest = os.path.join(self.tmp, "out")
        archive.extract(path, dest, LIMIT)
        self.assertEqual(os.listdir(dest), ["index.html"])

    def test_size_limit(self):
        path = os.path.join(self.tmp, "a.zip")
        make_zip(path, {"big.bin": b"0" * 5000})
        with self.assertRaisesRegex(archive.ArchiveError, "не помещается"):
            archive.extract(path, os.path.join(self.tmp, "out"), 1000)

    def test_rejects_non_archive_and_empty(self):
        path = os.path.join(self.tmp, "a.zip")
        with open(path, "wb") as f:
            f.write(b"not an archive at all")
        with self.assertRaisesRegex(archive.ArchiveError, "не архив"):
            archive.extract(path, os.path.join(self.tmp, "out"), LIMIT)
        make_zip(path, {})
        with self.assertRaisesRegex(archive.ArchiveError, "пустой"):
            archive.extract(path, os.path.join(self.tmp, "out2"), LIMIT)

    def test_rejects_password_protected_zip(self):
        path = os.path.join(self.tmp, "a.zip")
        make_zip(path, {"index.html": "x"})
        with open(path, "r+b") as f:
            data = bytearray(f.read())
            data[6] |= 1  # флаг шифрования в локальном заголовке
            central = data.find(b"PK\x01\x02")
            data[central + 8] |= 1
            f.seek(0)
            f.write(data)
        with self.assertRaisesRegex(archive.ArchiveError, "парол"):
            archive.extract(path, os.path.join(self.tmp, "out"), LIMIT)


class DetectTests(TempDirCase):
    def write(self, files):
        for name, content in files.items():
            path = os.path.join(self.tmp, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)

    def plan(self, files):
        self.write(files)
        with mock.patch("shutil.which", return_value="/usr/bin/tool"):
            result = detect.detect(self.tmp)
            result.resolve()
        return result

    def test_static_index(self):
        plan = self.plan({"index.html": "x"})
        self.assertEqual(plan.kind, "static")
        self.assertIn("static_server.py", plan.start)

    def test_static_index_any_case(self):
        plan = self.plan({"Index.HTML": "x"})
        self.assertIn("Index.HTML", plan.start)

    def test_static_in_subfolder(self):
        plan = self.plan({"public/index.html": "x", "readme.md": "y"})
        self.assertEqual(plan.kind, "static")
        self.assertIn("public", plan.start)

    def test_single_page_with_other_name(self):
        plan = self.plan({"landing.html": "x"})
        self.assertIn("landing.html", plan.start)

    def test_node_server_with_start(self):
        plan = self.plan({"package.json": json.dumps({"scripts": {"start": "node s.js"}})})
        self.assertEqual((plan.kind, plan.start), ("node", "npm start"))
        self.assertEqual(plan.steps[0][0], "install")

    def test_vite_app_serves_build_output(self):
        pkg = {"scripts": {"dev": "vite", "build": "vite build"}, "devDependencies": {"vite": "1"}}
        plan = self.plan({"package.json": json.dumps(pkg), "index.html": "src", "dist/index.html": "built"})
        self.assertEqual(plan.kind, "static")
        self.assertIn("dist", plan.start)
        self.assertEqual([s[0] for s in plan.steps], ["install", "build"])

    def test_create_react_app_start_is_not_used(self):
        pkg = {"scripts": {"start": "react-scripts start", "build": "react-scripts build"}}
        plan = self.plan({"package.json": json.dumps(pkg), "build/index.html": "x"})
        self.assertEqual(plan.kind, "static")

    def test_express_without_start_script(self):
        pkg = {"dependencies": {"express": "4"}}
        plan = self.plan({"package.json": json.dumps(pkg), "server.js": "x", "public/index.html": "y"})
        self.assertEqual(plan.kind, "node")
        self.assertIn("server.js", plan.start)

    def test_package_json_with_bom_from_windows(self):
        self.write({})
        with open(os.path.join(self.tmp, "package.json"), "w", encoding="utf-8-sig") as f:
            json.dump({"scripts": {"start": "node s.js"}}, f)
        plan = self.plan({})
        self.assertEqual(plan.kind, "node")

    def test_flask_uses_waitress(self):
        plan = self.plan({"app.py": "from flask import Flask\nserver = Flask(__name__)\n", "requirements.txt": "flask\n"})
        self.assertEqual(plan.kind, "python")
        self.assertIn("waitress --listen=0.0.0.0:$PORT app:server", plan.start)
        self.assertTrue(any("-r requirements.txt" in s[1] for s in plan.steps))

    def test_fastapi_uses_uvicorn(self):
        plan = self.plan({"main.py": "from fastapi import FastAPI\napp = FastAPI()\n"})
        self.assertIn("uvicorn main:app", plan.start)

    def test_django(self):
        plan = self.plan({"manage.py": "x"})
        self.assertIn("runserver 0.0.0.0:$PORT", plan.start)
        self.assertTrue(any("migrate" in s[1] for s in plan.steps))

    def test_plain_python_script(self):
        plan = self.plan({"main.py": "print(1)"})
        self.assertTrue(plan.start.endswith("main.py") or plan.start.endswith('main.py"') or "main.py" in plan.start)

    def test_requirements_cleanup(self):
        path = os.path.join(self.tmp, "requirements.txt")
        with open(path, "wb") as f:
            f.write("flask==3.0\npywin32==306\n".encode("utf-16"))
        detect._clean_requirements(path)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("flask==3.0", text)
        if os.name == "posix":
            self.assertNotIn("pywin32", text)

    def test_php(self):
        self.assertIn("-t .", self.plan({"index.php": "x"}).start)

    def test_laravel_style_public(self):
        plan = self.plan({"public/index.php": "x", "package.json": "{}"})
        self.assertEqual(plan.kind, "php")
        self.assertIn("-t public", plan.start)

    def test_start_sh_wins(self):
        plan = self.plan({"start.sh": "x", "index.html": "y"})
        self.assertEqual((plan.kind, plan.start), ("custom", "bash start.sh"))

    def test_unknown_project_explains(self):
        self.write({"readme.md": "x"})
        with self.assertRaisesRegex(detect.DetectError, "readme.md"):
            detect.detect(self.tmp)

    def test_override_saves_unknown_project(self):
        self.write({"readme.md": "x"})
        plan = detect.detect(self.tmp, "./run --port $PORT")
        plan.resolve()
        self.assertEqual((plan.kind, plan.start), ("custom", "./run --port $PORT"))

    def test_missing_runtime_gives_install_hint(self):
        self.write({"package.json": "{}"})
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaisesRegex(detect.DetectError, "apt install nodejs"):
                detect.detect(self.tmp)


class HelpersTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(sites.site_name("C:\\x\\shop.tar.gz"), "shop")
        self.assertEqual(sites.site_name("Мой сайт.zip"), "Мой сайт")
        self.assertEqual(sites.slugify("Мой сайт"), "moi-sait")
        self.assertEqual(sites.slugify("!!!"), "site")

    def test_parse_env(self):
        self.assertEqual(sites.parse_env("# c\nA=1\n\nB = x=y\n"), {"A": "1", "B": "x=y"})
        with self.assertRaises(sites.SiteError):
            sites.parse_env("not valid")
        with self.assertRaises(sites.SiteError):
            sites.parse_env("1BAD=x")


class MetricsTests(TempDirCase):
    """На Windows /proc нет, поэтому разбор проверяем на подставных файлах."""

    def build_proc(self, utime=100, listen_inode="555"):
        proc = self.tmp
        pid_dir = os.path.join(proc, "42")
        os.makedirs(os.path.join(pid_dir, "fd"), exist_ok=True)
        stat = f"42 (my (odd) name) S 1 42 42 0 -1 0 0 0 0 0 {utime} 20 0 0 20 0 1 0 0 0 1000 0"
        with open(os.path.join(pid_dir, "stat"), "w") as f:
            f.write(stat)
        os.makedirs(os.path.join(proc, "net"), exist_ok=True)
        header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        with open(os.path.join(proc, "net", "tcp"), "w") as f:
            f.write(header)
            f.write(f"   0: 00000000:0BB8 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 {listen_inode} 1\n")
            f.write("   1: 00000000:1F90 0100007F:9999 01 00000000:00000000 00:00000000 00000000  1000        0 777 1\n")
        return proc

    def test_read_stat_with_odd_process_name(self):
        proc = self.build_proc()
        with mock.patch.object(metrics, "PROC", proc):
            self.assertEqual(metrics._read_stat("42"), (42, 120, 1000))

    def test_listening_inodes(self):
        proc = self.build_proc()
        with mock.patch.object(metrics, "PROC", proc):
            self.assertEqual(metrics._listening_inodes(), {"555": 3000})


class AuthTests(TempDirCase):
    def test_login_and_lockout(self):
        auth.set_password(self.tmp, "secret")
        a = auth.Auth(self.tmp)
        self.assertIsNone(a.login("ip", "wrong"))
        token = a.login("ip", "secret")
        self.assertTrue(a.check(token))
        a.logout(token)
        self.assertFalse(a.check(token))
        for _ in range(auth.MAX_FAILURES):
            a.login("ip", "wrong")
        self.assertGreater(a.locked_for("ip"), 0)
        self.assertEqual(a.locked_for("other"), 0)


class ApiTests(TempDirCase):
    """Панель целиком: настоящий сервер, настоящие процессы сайтов."""

    def setUp(self):
        super().setUp()
        auth.set_password(self.tmp, "secret")
        self.server, self.manager = create_server("127.0.0.1", 0, self.tmp)
        self.port = self.server.server_address[1]
        self.manager.run()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.stop_all)
        self.cookie = None

    def stop_all(self):
        self.server.shutdown()
        self.server.server_close()
        self.manager.shutdown()

    def call(self, method, path, body=None, raw=None, headers=None, port=None):
        conn = http.client.HTTPConnection("127.0.0.1", port or self.port, timeout=30)
        send = {"X-Malinnik": "1"}
        if self.cookie:
            send["Cookie"] = self.cookie
        send.update(headers or {})
        payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        conn.request(method, path, body=payload, headers=send)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res, data

    def login(self):
        res, _ = self.call("POST", "/api/login", {"password": "secret"})
        self.assertEqual(res.status, 200)
        self.cookie = res.getheader("Set-Cookie").split(";")[0]

    def upload(self, name, files, site=None):
        path = os.path.join(self.tmp, "upload.zip")
        make_zip(path, files)
        with open(path, "rb") as f:
            raw = f.read()
        query = f"/api/upload?filename={quote(name)}" + (f"&site={site}" if site else "")
        res, data = self.call("POST", query, raw=raw)
        self.assertEqual(res.status, 200, data)
        return json.loads(data)["id"]

    def wait_site(self, site_id, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            site = self.site(site_id)
            if not site["deploy"]["active"] and site["state"] in ("running", "failed", "crashed", "stopped"):
                return site
            time.sleep(0.2)
        self.fail(f"Сайт не дошёл до конечного состояния: {site}")

    def site(self, site_id):
        res, data = self.call("GET", "/api/state")
        return next(s for s in json.loads(data)["sites"] if s["id"] == site_id)

    def fetch(self, port, path="/"):
        deadline = time.time() + 10
        while True:
            try:
                res, data = self.call("GET", path, port=port)
                return res.status, data.decode("utf-8")
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.2)

    def test_requires_login(self):
        res, _ = self.call("GET", "/api/state")
        self.assertEqual(res.status, 401)
        res, _ = self.call("POST", "/api/upload?filename=a.zip", raw=b"x")
        self.assertEqual(res.status, 401)
        res, _ = self.call("POST", "/api/login", {"password": "nope"})
        self.assertEqual(res.status, 401)

    def test_page_is_public_but_api_needs_custom_header(self):
        res, data = self.call("GET", "/")
        self.assertEqual(res.status, 200)
        self.assertIn("Аггентник".encode(), data)
        self.login()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/logout", headers={"Cookie": self.cookie})
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()

    def test_full_lifecycle(self):
        self.login()
        site_id = self.upload("Мой-сайт.zip", {
            "wrap/index.html": "<h1>первая</h1>",
            "wrap/about.html": "о нас",
            "wrap/.env": "SECRET=1",
            "wrap/shop.db": "база",
        })
        site = self.wait_site(site_id)
        self.assertEqual(site["state"], "running", site)
        self.assertEqual(site["kind"], "static")
        self.assertEqual(site["release"], 1)
        port = site["port"]
        self.assertIn(port, sites.PORT_RANGE)

        self.assertIn("первая", self.fetch(port)[1])
        self.assertEqual(self.fetch(port, "/about")[1], "о нас")
        self.assertEqual(self.fetch(port, "/.env")[0], 404)
        self.assertIn("первая", self.fetch(port, "/some/spa/route")[1])

        # Обновление того же сайта: порт тот же, база не теряется.
        same_id = self.upload("Мой-сайт.zip", {"index.html": "<h1>вторая</h1>"})
        self.assertEqual(same_id, site_id)
        site = self.wait_site(site_id)
        self.assertEqual(site["release"], 2)
        self.assertEqual(site["port"], port)
        self.assertIn("вторая", self.fetch(port)[1])
        new_dir = os.path.join(self.tmp, "sites", site_id, "releases", "2")
        self.assertTrue(os.path.isfile(os.path.join(new_dir, "shop.db")))

        # Откат.
        res, _ = self.call("POST", f"/api/sites/{site_id}/rollback")
        self.assertEqual(res.status, 200)
        self.assertEqual(self.wait_site(site_id)["release"], 1)
        self.assertIn("первая", self.fetch(port)[1])

        # Остановка и запуск.
        self.call("POST", f"/api/sites/{site_id}/stop")
        self.assertEqual(self.site(site_id)["state"], "stopped")
        with socket.socket() as sock:
            sock.settimeout(1)
            self.assertNotEqual(sock.connect_ex(("127.0.0.1", port)), 0)
        self.call("POST", f"/api/sites/{site_id}/start")
        self.assertEqual(self.wait_site(site_id)["state"], "running")

        # Журнал и удаление.
        res, data = self.call("GET", f"/api/sites/{site_id}/logs?kind=deploy")
        self.assertIn("Готово", json.loads(data)["text"])
        res, _ = self.call("DELETE", f"/api/sites/{site_id}")
        self.assertEqual(res.status, 200)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "sites", site_id)))

    def test_broken_archive_reports_error_and_keeps_site(self):
        self.login()
        res, data = self.call("POST", "/api/upload?filename=bad.zip", raw=b"garbage")
        self.assertEqual(res.status, 200)
        site = self.wait_site(json.loads(data)["id"])
        self.assertEqual(site["state"], "failed")
        self.assertIn("не архив", site["error"])

    def test_failed_update_keeps_old_version_running(self):
        self.login()
        site_id = self.upload("shop.zip", {"index.html": "старая"})
        port = self.wait_site(site_id)["port"]
        self.upload("shop.zip", {"readme.txt": "тут нет сайта", "more.txt": "и тут"})
        site = self.wait_site(site_id)
        self.assertEqual(site["state"], "running")
        self.assertEqual(site["release"], 1)
        self.assertIn("Не понял", site["error"])
        self.assertIn("старая", self.fetch(port)[1])

    def test_crashing_update_rolls_back_automatically(self):
        self.login()
        site_id = self.upload("shop.zip", {"index.html": "старая"})
        port = self.wait_site(site_id)["port"]
        self.upload("shop.zip", {"main.py": "import sys\nprint('бум')\nsys.exit(3)\n"})
        site = self.wait_site(site_id)
        self.assertEqual(site["state"], "running", site)
        self.assertEqual(site["release"], 1)
        self.assertIn("Вернул прошлую версию", site["error"])
        self.assertIn("старая", self.fetch(port)[1])
        res, data = self.call("GET", f"/api/sites/{site_id}/logs?kind=deploy")
        self.assertIn("бум", json.loads(data)["text"])

    def test_crash_loop_gives_up(self):
        self.login()
        site_id = self.upload("loop.zip", {"index.html": "x"})
        self.wait_site(site_id)
        site = self.manager.get(site_id)
        with mock.patch.object(sites, "MAX_FAST_CRASHES", 1):
            with site.lock:
                site.crashes = 1
                sites._kill(site.proc)
                site.started_at = time.time()
            deadline = time.time() + 10
            while time.time() < deadline and not site.gave_up:
                time.sleep(0.1)
        self.assertTrue(site.gave_up)
        self.assertEqual(self.site(site_id)["state"], "crashed")

    def test_settings_override_and_env(self):
        self.login()
        site_id = self.upload("env.zip", {"index.html": "x"})
        self.wait_site(site_id)
        script = (
            "import http.server,os;"
            "H=type('H',(http.server.BaseHTTPRequestHandler,),{'do_GET':lambda s:"
            "(s.send_response(200),s.end_headers(),s.wfile.write(os.environ['GREETING'].encode()))});"
            "http.server.HTTPServer(('0.0.0.0',int(os.environ['PORT'])),H).serve_forever()"
        )
        res, _ = self.call("PATCH", f"/api/sites/{site_id}", {
            "env": "GREETING=hello-env", "start_override": f'"{sys.executable}" -c "{script}"',
        })
        self.assertEqual(res.status, 200)
        self.call("POST", f"/api/sites/{site_id}/restart")
        site = self.wait_site(site_id)
        self.assertEqual(self.fetch(site["port"])[1], "hello-env")

        res, data = self.call("PATCH", f"/api/sites/{site_id}", {"env": "no equals"})
        self.assertEqual(res.status, 400)

    def test_publish_flow(self):
        self.login()
        site_id = self.upload("pub.zip", {"index.html": "x", "shop.db": "база"})
        site = self.wait_site(site_id)
        self.assertEqual(self.fetch(site["port"], "/shop.db")[0], 404)

        with mock.patch.object(tunnel, "available", lambda: False):
            res, data = self.call("POST", f"/api/sites/{site_id}/publish")
        self.assertEqual(res.status, 409)
        self.assertIn("cloudflared", json.loads(data)["error"])
        self.assertFalse(self.site(site_id)["published"])

        script = os.path.join(self.tmp, "fake_cloudflared.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write("import time\nprint('https://demo-site.trycloudflare.com', flush=True)\ntime.sleep(60)\n")
        with mock.patch.object(tunnel, "available", lambda: True), \
                mock.patch.object(tunnel, "command", lambda port: f'"{sys.executable}" "{script}"'):
            res, _ = self.call("POST", f"/api/sites/{site_id}/publish")
            self.assertEqual(res.status, 200)
            deadline = time.time() + 10
            while time.time() < deadline and not self.site(site_id)["tunnel"]["url"]:
                time.sleep(0.1)
        current = self.site(site_id)
        self.assertTrue(current["published"])
        self.assertEqual(current["tunnel"], {"state": "online", "url": "https://demo-site.trycloudflare.com"})
        res, data = self.call("GET", f"/api/sites/{site_id}/logs?kind=tunnel")
        self.assertIn("demo-site", json.loads(data)["text"])

        res, _ = self.call("POST", f"/api/sites/{site_id}/unpublish")
        self.assertEqual(res.status, 200)
        current = self.site(site_id)
        self.assertFalse(current["published"])
        self.assertEqual(current["tunnel"]["state"], "off")

    def test_cookie_is_secure_only_behind_https(self):
        res, _ = self.call("POST", "/api/login", {"password": "secret"})
        self.assertNotIn("Secure", res.getheader("Set-Cookie"))
        res, _ = self.call("POST", "/api/login", {"password": "secret"}, headers={"X-Forwarded-Proto": "https"})
        cookie = res.getheader("Set-Cookie")
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

    def test_login_lockout_counts_each_visitor_behind_nginx(self):
        attacker = {"X-Real-IP": "203.0.113.5"}
        for _ in range(5):
            res, _ = self.call("POST", "/api/login", {"password": "нет"}, headers=attacker)
            self.assertEqual(res.status, 401)
        res, _ = self.call("POST", "/api/login", {"password": "secret"}, headers=attacker)
        self.assertEqual(res.status, 429)
        res, _ = self.call("POST", "/api/login", {"password": "secret"}, headers={"X-Real-IP": "203.0.113.6"})
        self.assertEqual(res.status, 200, "чужие ошибки не должны блокировать остальных")

    def test_real_ip_header_is_trusted_only_from_local_connection(self):
        fake = mock.Mock()
        fake.headers = {"X-Real-IP": "1.2.3.4"}
        fake.client_address = ("192.168.1.50", 5000)
        self.assertEqual(Handler._client_ip(fake), "192.168.1.50")
        fake.client_address = ("127.0.0.1", 5000)
        self.assertEqual(Handler._client_ip(fake), "1.2.3.4")
        fake.headers = {}
        self.assertEqual(Handler._client_ip(fake), "127.0.0.1")

    def test_rejected_upload_still_delivers_the_answer(self):
        # Сессия истекла, а браузер шлёт большой архив: он должен увидеть 401, а не обрыв связи.
        for _ in range(5):
            res, data = self.call("POST", "/api/upload?filename=a.zip", raw=b"x" * (3 * 1024 * 1024))
            self.assertEqual(res.status, 401)
            self.assertIn("войти", json.loads(data)["error"])
            self.assertEqual(res.getheader("Connection"), "close")

    def test_error_with_unread_body_does_not_poison_next_request(self):
        self.login()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"X-Malinnik": "1", "Cookie": self.cookie}
        conn.request("POST", "/api/nonexistent", body=b"GET /secret HTTP/1.1\r\n\r\n", headers=headers)
        res = conn.getresponse()
        res.read()
        self.assertEqual(res.status, 404)
        self.assertTrue(res.will_close)
        conn.close()

    def test_services_require_login_and_header(self):
        for method, path in (("POST", "/api/vpn/up"), ("POST", "/api/vpn/import"), ("DELETE", "/api/vpn"),
                             ("POST", "/api/telegram"), ("POST", "/api/telegram/test")):
            with self.subTest(path):
                res, _ = self.call(method, path, body={})
                self.assertEqual(res.status, 401)

    def test_vpn_unavailable_is_reported_clearly(self):
        self.login()
        with mock.patch.object(sites.vpn, "call", side_effect=sites.vpn.VpnError("Сервис VPN не запущен.")):
            res, data = self.call("POST", "/api/vpn/up")
        self.assertEqual(res.status, 409)
        self.assertIn("не запущен", json.loads(data)["error"])
        res, data = self.call("GET", "/api/state")
        self.assertIn("vpn", json.loads(data))

    def test_vpn_key_reaches_helper_and_is_not_kept_by_panel(self):
        self.login()
        secret = "vpn://секрет"
        answer = {"installed": True, "configured": True, "up": False}
        with mock.patch.object(sites.vpn, "call", return_value=answer) as call:
            res, _ = self.call("POST", "/api/vpn/import", {"text": secret})
        self.assertEqual(res.status, 200)
        call.assert_called_once_with("import", text=secret)
        _, data = self.call("GET", "/api/state")
        self.assertNotIn("секрет", data.decode("utf-8"))
        for root, _, names in os.walk(self.tmp):
            for name in names:
                with open(os.path.join(root, name), "rb") as f:
                    self.assertNotIn("секрет".encode(), f.read(), name)

    def test_telegram_token_is_not_returned_by_api(self):
        self.login()
        token = "123456:" + "A" * 30
        # После сохранения токена бот начнёт опрос: тест не должен ходить в настоящий Telegram.
        patch = mock.patch.object(telegram, "API", "http://127.0.0.1:1")
        patch.start()
        self.addCleanup(patch.stop)
        res, data = self.call("POST", "/api/telegram", {"token": token})
        self.assertEqual(res.status, 200)
        self.assertNotIn(token, data.decode())
        _, data = self.call("GET", "/api/state")
        self.assertNotIn(token, data.decode())
        self.assertTrue(json.loads(data)["telegram"]["token_set"])
        res, data = self.call("POST", "/api/telegram", {"token": "плохой"})
        self.assertEqual(res.status, 400)

    def test_does_not_assign_port_used_by_another_program(self):
        port = self.manager._free_port()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", port))
            listener.listen()
            self.assertNotEqual(self.manager._free_port(), port)

    def test_detects_port_hardcoded_in_site_code(self):
        self.login()
        site = self.manager.get(self.wait_site(self.upload("h.zip", {"index.html": "x"}))["id"])
        assigned = site.meta["port"]
        self.assertEqual(site.effective_port(), assigned)
        site.stats = {"listen": [3000]}
        self.assertEqual(site.effective_port(), 3000)
        site.stats = {"listen": [3000, assigned]}
        self.assertEqual(site.effective_port(), assigned)
        site.stats = {}

    def test_second_site_gets_own_port(self):
        self.login()
        a = self.upload("a.zip", {"index.html": "a"})
        b = self.upload("b.zip", {"index.html": "b"})
        site_a, site_b = self.wait_site(a), self.wait_site(b)
        self.assertNotEqual(site_a["port"], site_b["port"])
        self.assertEqual(self.fetch(site_a["port"])[1], "a")
        self.assertEqual(self.fetch(site_b["port"])[1], "b")

    def test_restart_of_panel_restores_sites(self):
        self.login()
        site_id = self.upload("keep.zip", {"index.html": "живой"})
        port = self.wait_site(site_id)["port"]
        self.manager.shutdown()
        second = sites.Manager(self.tmp)
        second.run()
        self.addCleanup(second.shutdown)
        self.assertIn("живой", self.fetch(port)[1])


if __name__ == "__main__":
    unittest.main()

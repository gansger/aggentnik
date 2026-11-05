"""Проверки файлов Docker без самого Docker: на машине разработки его может не быть."""
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8", newline="") as f:
        return f.read()


class DockerFilesTests(unittest.TestCase):
    def test_scripts_have_unix_line_endings(self):
        for name in ("install-docker.sh", "install.sh", "install-amneziawg.sh", "setup-https.sh",
                     "Dockerfile", "docker-compose.yml"):
            self.assertNotIn("\r", read(name), name)

    def test_image_never_receives_local_secrets(self):
        ignore = read(".dockerignore").splitlines()
        self.assertEqual(ignore[1], "*", "в образ по умолчанию не должно попадать ничего")
        self.assertIn("!malinnik", ignore)

    def test_git_ignores_local_data(self):
        ignored = read(".gitignore").splitlines()
        for entry in ("dev-data/", ".env", "Caddyfile"):
            self.assertIn(entry, ignored)

    def test_only_vpn_container_is_privileged(self):
        compose = read("docker-compose.yml")
        services = re.split(r"\n  (?=\w+:\n)", compose.split("services:", 1)[1].split("\nvolumes:", 1)[0])
        privileged = [s.split(":", 1)[0].strip() for s in services if "privileged: true" in s]
        self.assertEqual(privileged, ["vpn"])

    def test_panel_runs_as_regular_user(self):
        self.assertRegex(read("Dockerfile"), r"(?m)^USER malinnik$")
        self.assertNotIn("/var/run/docker.sock", read("docker-compose.yml"))

    def test_ports_and_paths_match_the_code(self):
        from malinnik.vpn_helper import CONF_DIR, SOCKET_PATH, STATE_PATH
        compose = read("docker-compose.yml")
        self.assertIn(f"{CONF_DIR}:{CONF_DIR}", compose)
        self.assertIn(f"{os.path.dirname(STATE_PATH)}:{os.path.dirname(STATE_PATH)}", compose)
        self.assertIn(f"vpn-socket:{os.path.dirname(SOCKET_PATH)}", compose)


def find_bash():
    """На Windows обычный bash — это заглушка WSL, поэтому берём тот, что идёт с Git."""
    if os.name != "nt":
        return shutil.which("bash")
    folder = os.path.dirname(shutil.which("git") or "")
    for _ in range(3):  # git.exe лежит в Git\cmd или в Git\mingw64\bin
        folder = os.path.dirname(folder)
        candidate = os.path.join(folder, "bin", "bash.exe")
        if os.path.exists(candidate):
            return candidate
    return None


BASH = find_bash()


@unittest.skipUnless(BASH, "нужен bash")
class InstallerTests(unittest.TestCase):
    def run_function(self, code):
        """Выполняет функции установщика отдельно, без его вопросов и действий с системой."""
        script = read("install-docker.sh")
        functions = re.search(r"^normalize_domain\(\) \{.*?^# --- вопросы ---", script, re.S | re.M).group(0)
        program = "INTERNAL_PORT=7699\n" + functions + "\n" + code
        result = subprocess.run([BASH, "-c", program], capture_output=True, encoding="utf-8")
        return result.returncode, result.stdout

    def test_syntax(self):
        for name in ("install-docker.sh", "install.sh", "install-amneziawg.sh", "setup-https.sh"):
            result = subprocess.run([BASH, "-n", os.path.join(ROOT, name).replace("\\", "/")], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_domain_validation(self):
        for good, expected in (("Example.RU", "example.ru"), ("sub.example.ru.", "sub.example.ru")):
            self.assertEqual(self.run_function(f"normalize_domain '{good}'")[1], expected)
        for bad in ("localhost", "exa mple.ru", "-bad.ru", "a.b/c", "example.ru;rm -rf /", "$(id).ru"):
            self.assertEqual(self.run_function(f"normalize_domain '{bad}'")[1], "", bad)

    def test_port_validation(self):
        for good in ("7700", "8443", "65535"):
            self.assertEqual(self.run_function(f"valid_port {good}")[0], 0, good)
        for bad in ("80", "443", "7699", "8100", "8150", "8199", "70000", "abc", "'7700;id'"):
            self.assertNotEqual(self.run_function(f"valid_port {bad}")[0], 0, bad)

    def test_caddyfile(self):
        _, text = self.run_function("caddyfile example.ru 7700 me@example.ru")
        self.assertIn("admin off", text)
        self.assertIn("https_port 7700", text)
        self.assertIn("example.ru {", text)
        self.assertIn("email me@example.ru", text)
        self.assertIn("disable_tlsalpn_challenge", text)
        self.assertIn("reverse_proxy 127.0.0.1:7699", text)
        self.assertIn("X-Real-IP {remote_host}", text)
        self.assertEqual(text.count("{"), text.count("}"))

    def test_caddyfile_without_email(self):
        _, text = self.run_function("caddyfile example.ru 8443 ''")
        self.assertNotIn("email", text)
        self.assertIn("https_port 8443", text)


if __name__ == "__main__":
    unittest.main()

"""Проверки файлов Docker и установщика без самого Docker: его может не быть на машине разработки."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8", newline="") as f:
        return f.read()


class DockerFilesTests(unittest.TestCase):
    def test_files_have_unix_line_endings(self):
        for name in ("install-docker.sh", "keep-inbound.sh", "Dockerfile", "docker-compose.yml"):
            self.assertNotIn("\r", read(name), name)

    def test_image_never_receives_local_secrets(self):
        ignore = read(".dockerignore").splitlines()
        self.assertEqual(ignore[1], "*", "в образ по умолчанию не должно попадать ничего")
        self.assertIn("!malinnik", ignore)

    def test_git_ignores_local_data(self):
        ignored = read(".gitignore").splitlines()
        for entry in ("dev-data/", ".env", "Caddyfile"):
            self.assertIn(entry, ignored)

    def test_no_container_has_extra_rights(self):
        compose = read("docker-compose.yml")
        for word in ("privileged", "cap_add", "/var/run/docker.sock", "user: root"):
            self.assertNotIn(word, compose)
        self.assertRegex(read("Dockerfile"), r"(?m)^USER malinnik$")

    def test_no_vpn_left_in_the_project(self):
        for name in ("Dockerfile", "docker-compose.yml", "malinnik/server.py", "malinnik/sites.py",
                     "malinnik/bot.py", "malinnik/alerts.py", "malinnik/web/app.js", "malinnik/web/index.html"):
            self.assertNotRegex(read(name).lower(), r"vpn|amnezia", name)

    def test_no_cloudflare_left_in_the_project(self):
        for name in ("Dockerfile", "docker-compose.yml", "install-docker.sh", "keep-inbound.sh", "README.md",
                     "malinnik/server.py", "malinnik/sites.py", "malinnik/bot.py", "malinnik/web/app.js",
                     "malinnik/web/index.html"):
            self.assertNotRegex(read(name).lower(), r"cloudflare|cloudflared|trycloudflare", name)
        self.assertFalse(os.path.exists(os.path.join(ROOT, "malinnik", "tunnel.py")))

    def test_keep_inbound_rules_only_touch_router_traffic(self):
        script = read("keep-inbound.sh")
        self.assertIn("lookup main priority 100", script)
        self.assertIn("ct state new ct mark set", script)
        self.assertIn('iifname "$dev"', script, "отмечать надо только то, что пришло через роутер")
        self.assertIn("meta l4proto tcp", script, "UDP самого VPN трогать нельзя")
        self.assertNotRegex(script, r"\b(ufw|iptables -F|nft flush)\b", "чужие правила трогать нельзя")

    def test_install_instructions_run_on_the_server_itself(self):
        readme = read("README.md")
        for block in re.findall(r"```bash\n(.*?)```", readme, re.S):
            self.assertNotRegex(block, r"\b(ssh|scp)\b", block)
        self.assertIn(re.search(r"^INSTALL_COMMAND='(.*)'$", read("install-docker.sh"), re.M).group(1), readme)


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
    def run_function(self, code, stdin=None, env=None):
        """Выполняет функции установщика отдельно, без его основной части."""
        script = read("install-docker.sh")
        header = script[:script.index("# --- функции ---")].split("set -euo pipefail\n", 1)[1]
        functions = re.search(r"# --- функции ---.*?# --- установка ---", script, re.S).group(0)
        program = "set -euo pipefail\n" + header + functions + "\n" + code + "\n"
        # Через файл: на Windows длинный текст с кавычками искажается при передаче в bash -c.
        folder = tempfile.mkdtemp(prefix="malinnik-installer-")
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        path = os.path.join(folder, "program.sh")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(program)
        result = subprocess.run(
            [BASH, path.replace("\\", "/")], capture_output=True, encoding="utf-8",
            input=stdin if stdin is not None else "",
            env={**os.environ, **(env or {})},
        )
        return result.returncode, result.stdout, result.stderr

    def test_syntax(self):
        for name in ("install-docker.sh", "keep-inbound.sh"):
            path = os.path.join(ROOT, name).replace("\\", "/")
            result = subprocess.run([BASH, "-n", path], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_domain_validation(self):
        for good, expected in (("Example.RU", "example.ru"), ("sub.example.ru.", "sub.example.ru")):
            self.assertEqual(self.run_function(f"normalize_domain '{good}'")[1], expected)
        for bad in ("localhost", "exa mple.ru", "-bad.ru", "a.b/c", "example.ru;rm -rf /", "$(id).ru"):
            self.assertEqual(self.run_function(f"normalize_domain '{bad}'")[1], "", bad)

    def test_port_validation(self):
        for good in ("7700", "8443", "65535"):
            self.assertEqual(self.run_function(f"valid_port {good}")[0], 0, good)
        for bad in ("80", "443", "7698", "7699", "8100", "8150", "8199", "70000", "abc", "'7700;id'"):
            self.assertNotEqual(self.run_function(f"valid_port {bad}")[0], 0, bad)

    def test_caddyfile(self):
        _, text, _ = self.run_function("caddyfile example.ru 7700 me@example.ru")
        for part in ("admin off", "email me@example.ru", "auto_https disable_redirects",
                     "X-Real-IP {remote_host}"):
            self.assertIn(part, text)
        self.assertEqual(text.count("{"), text.count("}"))
        self.assertNotIn("disable_tlsalpn_challenge", text, "порт 443 остаётся запасным способом проверки")

    def test_caddyfile_separates_public_page_and_admin(self):
        _, text, _ = self.run_function("caddyfile example.ru 7700 ''")
        blocks = dict(re.findall(r"^(https?://\S+) \{\n(.*?)^\}", text, re.S | re.M))
        self.assertIn("127.0.0.1:7698", blocks["https://example.ru"], "на 443 только портфолио")
        self.assertNotIn("7699", blocks["https://example.ru"])
        self.assertIn("127.0.0.1:7699", blocks["https://example.ru:7700"], "админка только на своём порту")
        self.assertIn("redir https://example.ru{uri}", blocks["http://example.ru"], "http ведёт на главную, а не в админку")

    def test_caddyfile_without_email(self):
        _, text, _ = self.run_function("caddyfile example.ru 8443 ''")
        self.assertNotIn("email", text)
        self.assertIn("https://example.ru:8443 {", text)

    # --- вопросы: именно здесь установка раньше молча пропускала домен ---

    def test_piped_run_stops_with_correct_command(self):
        code, _, err = self.run_function("require_terminal; echo продолжаю", stdin="ответ")
        self.assertNotEqual(code, 0)
        self.assertIn('sudo bash -c "$(curl', err)

    def test_ask_returns_answer_and_prompts_on_stderr(self):
        code, out, err = self.run_function('answer="$(ask "Домен" "")"; printf "<%s>" "$answer"', stdin="  panel.example.ru \n")
        self.assertEqual((code, out), (0, "<panel.example.ru>"))
        self.assertIn("Домен", err)

    def test_ask_uses_default_on_enter(self):
        _, out, _ = self.run_function('answer="$(ask "Порт" "7700")"; printf "%s" "$answer"', stdin="\n")
        self.assertEqual(out, "7700")

    def test_unreadable_answer_stops_instead_of_empty_domain(self):
        code, out, err = self.run_function('DOMAIN="$(ask "Домен" "")"; echo "продолжаю без домена"', stdin="")
        self.assertNotEqual(code, 0)
        self.assertNotIn("продолжаю", out)
        self.assertIn("не удалось прочитать", err)

    def test_confirm(self):
        for answer, default, expected in (("д\n", "n", 0), ("да\n", "n", 0), ("y\n", "n", 0), ("\n", "y", 0),
                                          ("н\n", "y", 1), ("\n", "n", 1), ("нет\n", "y", 1)):
            code, _, _ = self.run_function(f'confirm "Вопрос?" {default}', stdin=answer)
            self.assertEqual(code, expected, (answer, default))

    # --- индикатор ---

    def step_env(self):
        folder = tempfile.mkdtemp(prefix="malinnik-step-").replace("\\", "/")
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        return f"{folder}/install.log"

    def test_step_hides_command_output_and_keeps_it_in_log(self):
        log = self.step_env()
        code, out, _ = self.run_function(f'LOG_FILE="{log}"; step "Сборка" sh -c "echo шумный вывод; echo ещё >&2"')
        self.assertEqual(code, 0)
        self.assertIn("Сборка", out)
        self.assertNotIn("шумный", out)
        with open(log, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("шумный вывод", content)
        self.assertIn("ещё", content)

    def test_step_failure_shows_log_and_stops(self):
        log = self.step_env()
        code, out, err = self.run_function(f'LOG_FILE="{log}"; step "Сборка" sh -c "echo причина сбоя; exit 3"; echo дальше')
        self.assertNotEqual(code, 0)
        self.assertNotIn("дальше", out)
        self.assertIn("причина сбоя", err)
        self.assertIn(log, err)

    def test_try_step_returns_code_without_stopping(self):
        log = self.step_env()
        _, out, _ = self.run_function(f'LOG_FILE="{log}"; try_step "Шаг" sh -c "exit 4" || echo "код $?"')
        self.assertIn("код 4", out)

    def test_step_without_terminal_prints_no_control_characters(self):
        log = self.step_env()
        _, out, _ = self.run_function(f'LOG_FILE="{log}"; step "Сборка" sh -c "sleep 0.3"')
        self.assertNotIn("\r", out)
        self.assertNotIn("\x1b", out)

    # --- диагностика сертификата ---

    def fake_curl(self, result):
        return f"""
sleep() {{ :; }}
curl() {{
  case "$*" in
    *check-tcp*) echo '{{"ok":1,"request_id":"abc"}}' ;;
    *check-result/abc*) echo '{result}' ;;
    *) return 1 ;;
  esac
}}
"""

    def test_port_open_when_any_node_connects(self):
        result = '{"a":[{"time":0.1,"address":"1.2.3.4"}],"b":[{"error":"Connection timed out"}]}'
        _, out, _ = self.run_function(self.fake_curl(result) + "port_from_outside example.ru 80")
        self.assertEqual(out.strip(), "open")

    def test_port_closed_when_all_nodes_fail(self):
        result = '{"a":[{"error":"Connection timed out"}],"b":[{"error":"Connection refused"}]}'
        _, out, _ = self.run_function(self.fake_curl(result) + "port_from_outside example.ru 80")
        self.assertEqual(out.strip(), "closed")

    def test_port_unknown_when_service_unavailable(self):
        _, out, _ = self.run_function('sleep() { :; }; curl() { return 1; }; port_from_outside example.ru 80')
        self.assertEqual(out.strip(), "unknown")

    def test_port_unknown_when_results_never_arrive(self):
        _, out, _ = self.run_function(self.fake_curl('{"a":null,"b":null}') + "port_from_outside example.ru 80")
        self.assertEqual(out.strip(), "unknown")

    def test_vpn_device_detection(self):
        for name in ("wg0", "awg0", "tun0", "malinnik0", "amn0"):
            self.assertEqual(self.run_function(f"is_vpn_device {name}")[0], 0, name)
        for name in ("eth0", "wlan0", "enp3s0", "''"):
            self.assertNotEqual(self.run_function(f"is_vpn_device {name}")[0], 0, name)


if __name__ == "__main__":
    unittest.main()

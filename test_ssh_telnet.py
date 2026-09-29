"""
Test ONLY the SSH + telnet part of Code 33.
- Uses the same config.ini and the same saved passwords as the app.
- Logs in to SSH, then telnet, and stops. It does NOT run any CUDBSUE command
  unless you type one yourself at the end.
- Prints everything the server sends, so we can see the real prompts.

Run:  python test_ssh_telnet.py
"""
import configparser
import getpass
import os
import re
import sys
import time

import paramiko

try:
    import keyring
except ImportError:
    keyring = None

APP = "Code33"
HERE = os.path.dirname(os.path.abspath(__file__))
# terminal colour codes + invisible control characters (the server ends its prompt with \x0f)
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][AB012]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

cfg = configparser.ConfigParser(interpolation=None)
if not cfg.read(os.path.join(HERE, "config.ini"), encoding="utf-8"):
    sys.exit("config.ini not found next to this file")
s, t = cfg["ssh"], cfg["telnet"]


def password(key, label):
    if keyring:
        saved = keyring.get_password(APP, key)
        if saved:
            print(f"[i] using saved {label} password")
            return saved
    return getpass.getpass(f"{label} password: ")


class Shell:
    def __init__(self, chan):
        self.chan, self.buf = chan, ""

    def send(self, line, secret=False):
        print(f"\n>>> SEND: {'********' if secret else line}")
        self.chan.send(line + "\r")

    def expect(self, pattern, timeout, step):
        rx = re.compile(pattern)
        deadline = time.time() + timeout
        while True:
            m = rx.search(self.buf)
            if m:
                out, self.buf = self.buf[: m.end()], self.buf[m.end():]
                print(out, end="")
                print(f"\n[OK] {step}")
                return out
            if time.time() > deadline:
                print(self.buf)
                print(f"\n[FAIL] {step}: did not see {pattern!r} within {timeout}s")
                print("[i] Last 300 chars exactly as received (send me this line):")
                print(repr(self.buf[-300:]))
                raise SystemExit(1)
            if self.chan.recv_ready():
                self.buf += ANSI_RE.sub("", self.chan.recv(65535).decode("utf-8", "replace"))
            elif self.chan.closed:
                print(self.buf)
                raise SystemExit("\n[FAIL] connection was closed by the server")
            else:
                time.sleep(0.1)


ssh_pw = password(f"ssh:{s['user']}@{s['host']}", f"SSH {s['user']}@{s['host']}")
tel_pw = password(f"telnet:{t['user']}@{t['host']}", f"TELNET {t['user']}@{t['host']}")

print(f"\n=== 1. SSH to {s['user']}@{s['host']}:{s.get('port', '22')} ===")
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
try:
    client.connect(s["host"], int(s.get("port", "22")), s["user"], ssh_pw,
                   look_for_keys=False, allow_agent=False, timeout=20)
except paramiko.AuthenticationException:
    sys.exit("[FAIL] SSH password rejected (in the app: 'Reset saved passwords')")
except Exception as e:
    sys.exit(f"[FAIL] SSH connection failed: {e}")
print("[OK] SSH connected")

try:
    sh = Shell(client.invoke_shell(width=300, height=200))
    sh.expect(s["shell_prompt"], 20, "got the Linux shell prompt")

    print(f"\n=== 2. TELNET {t['host']} {t['port']} ===")
    sh.send(f"telnet {t['host']} {t['port']}")
    sh.expect(t["login_prompt"], 30, "got the telnet username prompt")
    sh.send(t["user"])
    sh.expect(t["password_prompt"], 20, "got the telnet password prompt")
    sh.send(tel_pw, secret=True)
    sh.expect(t["mml_prompt"], 30, "logged in to telnet, got the MML prompt")

    print("\n=== 3. Optional: one command ===")
    cmd = input("Type ONE command to test (or just press Enter to skip): ").strip()
    if cmd:
        sh.send(cmd)
        sh.expect(t["mml_prompt"], int(cfg["run"].get("command_timeout", "30")), "command answered")

    print("\n=== 4. Exit ===")
    sh.send(t.get("exit_command", "exit;"))
    time.sleep(1.5)
    if sh.chan.recv_ready():
        print(ANSI_RE.sub("", sh.chan.recv(65535).decode("utf-8", "replace")))
    print("\n[DONE] SSH + telnet login works with the current config.ini")
finally:
    client.close()
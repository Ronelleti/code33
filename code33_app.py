"""
Code 33 helper
--------------
1. Parse the Teams message (MSISDN / IMSI1 / IMSI2 / MSISDN2)
2. CRM: search 05X number -> open subscriber -> SIMS INVENTORY -> IMSI row -> עריכה -> מושהה
3. SSH to jump host -> telnet to CUDB (MML) -> run the 20 CUDBSUE commands x3
4. CRM: set the IMSI back to בשימוש (always attempted, even if step 3 failed)
5. Show a "finished" popup so the user can go and check

Passwords are kept per Windows user in Windows Credential Manager (keyring),
never in config.ini.
"""
import configparser
import datetime
import os
import queue
import re
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import messagebox, scrolledtext, simpledialog, ttk

import keyring
import paramiko
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

APP = "Code33"
BASE_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP)
PROFILE_DIR = os.path.join(DATA_DIR, "browser-profile")
LOG_DIR = os.path.join(DATA_DIR, "logs")
CONFIG_PATH = os.path.join(BASE_DIR, "config.ini")
# terminal colour codes + invisible control characters (the server ends its prompt with \x0f)
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][AB012]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class Cancelled(Exception):
    pass


# ----------------------------------------------------------------------------- parsing / commands
FIELD_PATTERNS = {
    "msisdn": r"\bMSISDN(?!\s*2)",
    "imsi1": r"\bIMSI\s*1",
    "imsi2": r"\bIMSI\s*2",
    "msisdn2": r"\bMSISDN\s*2",
}


def parse_message(text):
    out = {}
    for key, name in FIELD_PATTERNS.items():
        m = re.search(name + r"\s*[:=][\s:=\-]*(\d+)", text, re.I)
        out[key] = m.group(1) if m else ""
    return out


def validate(v):
    errors = []
    for k in ("msisdn", "msisdn2"):
        if not re.fullmatch(r"972\d{9}", v[k]):
            errors.append(f"{k.upper()} should be 12 digits starting with 972 (got '{v[k]}')")
    for k in ("imsi1", "imsi2"):
        if not re.fullmatch(r"425\d{12}", v[k]):
            errors.append(f"{k.upper()} should be 15 digits starting with 425 (got '{v[k]}')")
    return errors


def to_local(msisdn):
    return "0" + msisdn[3:]


def build_block(imsi, msisdn, realm):
    return [
        f"CUDBSUE:IMSI={imsi};",
        f"CUDBSUE:SECMSISDN={msisdn};",
        f"CUDBSUE:MSISDN={msisdn};",
        f"CUDBSUE:MSCID={msisdn};",
        f"CUDBSUE:IMSI={imsi},IDENTITYONLY;",
        f"CUDBSUE:SECMSISDN={msisdn},IDENTITYONLY;",
        f"CUDBSUE:MSISDN={msisdn},IDENTITYONLY;",
        f"CUDBSUE:MSCID={msisdn},IDENTITYONLY;",
        f"CUDBSUE:ASSOCID={msisdn};",
        f"CUDBSUE:IMPI={imsi}@{realm};",
    ]


def build_commands(v, realm):
    return build_block(v["imsi1"], v["msisdn"], realm) + build_block(v["imsi2"], v["msisdn2"], realm)


# ----------------------------------------------------------------------------- CRM (browser)
class CRM:
    def __init__(self, cfg, log):
        self.c = cfg["crm"]
        self.log = log
        self.pw = self.ctx = self.page = None

    def start(self):
        os.makedirs(PROFILE_DIR, exist_ok=True)
        self.pw = sync_playwright().start()
        self.ctx = self.pw.chromium.launch_persistent_context(
            PROFILE_DIR,
            channel=self.c.get("browser_channel", "msedge"),
            headless=False,
            chromium_sandbox=True,  # removes the "--no-sandbox" warning bar
            no_viewport=True,
            args=["--start-maximized"],
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.set_default_timeout(int(self.c.get("timeout_ms", "30000")))
        self.page.on("dialog", lambda d: d.accept())  # auto-confirm "are you sure?" popups

    def stop(self):
        try:
            if self.ctx:
                self.ctx.close()
        finally:
            if self.pw:
                self.pw.stop()

    def _search_box(self):
        return self.page.locator(self.c["search_selector"]).first

    def _login_form_visible(self):
        return self.page.locator("input[type=password]:visible").count() > 0

    def ensure_login(self, crm_password):
        p = self.page
        p.goto(self.c["url"])
        p.wait_for_load_state("domcontentloaded")
        p.wait_for_timeout(1500)
        if not self._login_form_visible():
            self.log("CRM: already logged in.")
            return
        if self.c.get("login_user_selector") and crm_password:
            self.log("CRM: logging in...")
            p.locator(self.c["login_user_selector"]).first.fill(self.c.get("login_user", ""))
            p.locator(self.c["login_password_selector"]).first.fill(crm_password)
            p.locator(self.c["login_button_selector"]).first.click()
            p.locator("input[type=password]:visible").first.wait_for(state="hidden", timeout=30000)
        else:
            self.log("CRM: please log in in the browser window (waiting up to 3 minutes)...")
            p.locator("input[type=password]:visible").first.wait_for(state="hidden", timeout=180000)
        p.wait_for_load_state("domcontentloaded")
        self.log("CRM: logged in.")

    def open_subscriber(self, local):
        p = self.page
        search_url = self.c.get("search_url", "").strip()
        if search_url:
            # open the search results page directly - no need to find the search box
            p.goto(search_url.format(query=local))
        else:
            p.goto(self.c["url"])
            box = self._search_box()
            box.fill(local)
            box.press("Enter")
        # the number also appears in the hidden "recently viewed" menu, so only take a visible link
        # skip the "recently viewed" side menu (it also shows the number) - take the link in the results
        link = p.locator("a:not(.recent-links-detail)", has_text=local).locator("visible=true").first
        link.wait_for(state="visible")
        link.click()
        # the same text also exists in the (hidden) top menu, so only look at visible matches
        p.get_by_text(self.c["sims_section_text"], exact=False).locator("visible=true").first.wait_for(
            state="visible"
        )

    def sim_row(self, imsi):
        p = self.page
        row = (p.locator("tr", has_text=imsi).filter(has=p.get_by_text(self.c["edit_text"]))
               .locator("visible=true").first)
        try:
            row.wait_for(state="visible", timeout=3000)  # section already open
            return row
        except PWTimeout:
            pass
        # SIMS INVENTORY is collapsed (+) -> click its header to open it, then wait for the table to load
        self.log("CRM: opening SIMS INVENTORY...")
        header = p.get_by_text(self.c["sims_section_text"], exact=False).locator("visible=true").first
        header.scroll_into_view_if_needed()
        header.click()
        try:
            row.wait_for(state="visible")
        except PWTimeout:
            raise RuntimeError(f"CRM: IMSI {imsi} was not found in SIMS INVENTORY for this number.")
        row.scroll_into_view_if_needed()
        return row

    def set_status(self, local, imsi, target):
        p = self.page
        self.log(f"CRM: {local} / IMSI {imsi} -> {target}")
        self.open_subscriber(local)
        row = self.sim_row(imsi)
        if target in row.inner_text():
            self.log(f"CRM: already '{target}', nothing to change.")
            return
        row.get_by_text(self.c["edit_text"]).first.click()

        select = p.locator("select:visible", has=p.locator("option", has_text=target)).first
        try:
            select.wait_for(state="visible", timeout=10000)
        except PWTimeout:
            raise RuntimeError(
                "CRM: could not find the status dropdown after pressing עריכה. "
                "Record the real selectors with 'playwright codegen' and adjust set_status()."
            )
        select.select_option(label=target)

        save_text = self.c.get("save_text", "").strip()
        if save_text:
            btn = p.get_by_role("button", name=save_text).or_(
                p.locator(f"input[type=submit][value='{save_text}'], input[type=button][value='{save_text}']")
            ).first
            btn.click()
        p.wait_for_timeout(1500)

        # verify by reloading the subscriber
        self.open_subscriber(local)
        text = self.sim_row(imsi).inner_text()
        if target not in text:
            raise RuntimeError(f"CRM: status did not change to '{target}'. Row now shows: {text!r}")
        self.log(f"CRM: verified '{target}'.")


# ----------------------------------------------------------------------------- SSH + telnet (MML)
class Shell:
    def __init__(self, chan, log):
        self.chan, self.log, self.buf = chan, log, ""

    def send(self, line, secret=False):
        self.log("  > " + ("********" if secret else line))
        self.chan.send(line + "\r")

    def expect(self, pattern, timeout):
        rx = re.compile(pattern)
        deadline = time.time() + timeout
        while True:
            m = rx.search(self.buf)
            if m:
                out, self.buf = self.buf[: m.end()], self.buf[m.end():]
                for ln in out.splitlines():
                    if ln.strip():
                        self.log("    " + ln.rstrip())
                return out
            if time.time() > deadline:
                raise TimeoutError(f"Timed out waiting for {pattern!r}. Last output:\n{self.buf[-600:]}")
            if self.chan.recv_ready():
                self.buf += ANSI_RE.sub("", self.chan.recv(65535).decode("utf-8", "replace"))
            elif self.chan.closed:
                raise ConnectionError("Connection closed.\n" + self.buf[-600:])
            else:
                time.sleep(0.1)


def run_mml(cfg, secrets, commands, log):
    s, t, r = cfg["ssh"], cfg["telnet"], cfg["run"]
    repeat, cmd_timeout = int(r.get("repeat", "3")), int(r.get("command_timeout", "30"))
    log(f"SSH: connecting to {s['user']}@{s['host']}...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(s["host"], int(s.get("port", "22")), s["user"], secrets["ssh"],
                   look_for_keys=False, allow_agent=False, timeout=20)
    try:
        sh = Shell(client.invoke_shell(width=300, height=200), log)
        sh.expect(s["shell_prompt"], 20)
        sh.send(f"telnet {t['host']} {t['port']}")
        sh.expect(t["login_prompt"], 30)
        sh.send(t["user"])
        sh.expect(t["password_prompt"], 20)
        sh.send(secrets["telnet"], secret=True)
        sh.expect(t["mml_prompt"], 30)
        log("TELNET: logged in.")
        for i in range(1, repeat + 1):
            log(f"--- round {i}/{repeat} ---")
            for cmd in commands:
                sh.send(cmd)
                out = sh.expect(t["mml_prompt"], cmd_timeout)
                if re.search(r"NOT ACCEPTED|FAULT", out, re.I):
                    log(f"  ⚠ {cmd} returned an error (see output above)")
        sh.send(t.get("exit_command", "exit;"))
        time.sleep(1)
    finally:
        client.close()
    log("TELNET: done.")


# ----------------------------------------------------------------------------- full flow
def run_flow(cfg, v, secrets, log, dry_run):
    local = to_local(v["msisdn"])
    cmds = build_commands(v, cfg["run"]["impi_realm"])
    c = cfg["crm"]
    if dry_run:
        log(f"[DRY RUN] would set {local} / {v['imsi1']} -> {c['status_suspended']}")
        for cmd in cmds:
            log("  " + cmd)
        log(f"[DRY RUN] ... x{cfg['run'].get('repeat', '3')}, then back to {c['status_in_use']}")
        return True

    crm = CRM(cfg, log)
    suspended = False
    try:
        crm.start()
        crm.ensure_login(secrets.get("crm"))
        suspended = True  # set before the attempt, so a half-done change is still reverted
        crm.set_status(local, v["imsi1"], c["status_suspended"])
        run_mml(cfg, secrets, cmds, log)
        return True
    finally:
        if suspended:
            try:
                crm.set_status(local, v["imsi1"], c["status_in_use"])
            except Exception as e:
                log(f"‼ COULD NOT RESTORE STATUS — set {v['imsi1']} back to {c['status_in_use']} MANUALLY! ({e})")
                raise
        crm.stop()


def test_crm(cfg, v, secrets, log):
    crm = CRM(cfg, log)
    try:
        crm.start()
        crm.ensure_login(secrets.get("crm"))
        local = to_local(v["msisdn"])
        crm.open_subscriber(local)
        row = crm.sim_row(v["imsi1"])
        row.highlight()
        log("CRM test OK. Row: " + " | ".join(x.strip() for x in row.inner_text().split("\t") if x.strip()))
        time.sleep(8)
    finally:
        crm.stop()


# ----------------------------------------------------------------------------- GUI
class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.busy = False
        self.cfg = configparser.ConfigParser(interpolation=None)
        if not self.cfg.read(CONFIG_PATH, encoding="utf-8"):
            messagebox.showerror(APP, f"config.ini not found next to the app:\n{CONFIG_PATH}")
            root.destroy()
            return
        os.makedirs(LOG_DIR, exist_ok=True)
        self.logfile = None

        root.title("Code 33")
        root.geometry("900x720")
        frm = ttk.Frame(root, padding=10)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Paste the Teams message:").pack(anchor="w")
        self.msg = tk.Text(frm, height=6)
        self.msg.pack(fill="x")
        ttk.Button(frm, text="Parse ↓", command=self.parse).pack(anchor="w", pady=4)

        grid = ttk.Frame(frm)
        grid.pack(fill="x", pady=4)
        self.vars = {}
        for i, (key, label) in enumerate([("msisdn", "MSISDN"), ("imsi1", "IMSI1"),
                                          ("imsi2", "IMSI2"), ("msisdn2", "MSISDN2")]):
            ttk.Label(grid, text=label, width=10).grid(row=i // 2, column=(i % 2) * 2, sticky="w", pady=2)
            self.vars[key] = tk.StringVar()
            ttk.Entry(grid, textvariable=self.vars[key], width=30).grid(row=i // 2, column=(i % 2) * 2 + 1, padx=6)

        bar = ttk.Frame(frm)
        bar.pack(fill="x", pady=6)
        self.dry = tk.BooleanVar(value=False)
        self.buttons = [
            ttk.Button(bar, text="▶ RUN code 33", command=self.run),
            ttk.Button(bar, text="Preview commands", command=self.preview),
            ttk.Button(bar, text="Test CRM (no changes)", command=self.test),
            ttk.Button(bar, text="Reset saved passwords", command=self.reset_passwords),
        ]
        for b in self.buttons:
            b.pack(side="left", padx=3)
        ttk.Checkbutton(bar, text="Dry run", variable=self.dry).pack(side="left", padx=10)

        self.logbox = scrolledtext.ScrolledText(frm, height=20, font=("Consolas", 9))
        self.logbox.pack(fill="both", expand=True)

        root.after(100, self.pump)

    # --- helpers
    def log(self, text):
        self.q.put(("log", text))

    def pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    line = f"{datetime.datetime.now():%H:%M:%S}  {payload}"
                    self.logbox.insert("end", line + "\n")
                    self.logbox.see("end")
                    if self.logfile:
                        self.logfile.write(line + "\n")
                        self.logfile.flush()
                elif kind == "done":
                    self.finish(*payload)
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def values(self):
        return {k: re.sub(r"\D", "", v.get()) for k, v in self.vars.items()}

    def parse(self):
        for k, val in parse_message(self.msg.get("1.0", "end")).items():
            self.vars[k].set(val)

    def secret(self, key, label):
        val = keyring.get_password(APP, key)
        if not val:
            val = simpledialog.askstring(APP, f"{label} password\n(saved in Windows Credential Manager):",
                                         show="*", parent=self.root)
            if not val:
                raise Cancelled()
            keyring.set_password(APP, key, val)
        return val

    def secret_keys(self):
        s, t = self.cfg["ssh"], self.cfg["telnet"]
        return {"ssh": (f"ssh:{s['user']}@{s['host']}", f"SSH {s['user']}@{s['host']}"),
                "telnet": (f"telnet:{t['user']}@{t['host']}", f"TELNET {t['user']}@{t['host']}"),
                "crm": ("crm:" + self.cfg["crm"].get("login_user", ""), "CRM")}

    def collect_secrets(self, need_mml=True):
        keys = self.secret_keys()
        out = {}
        if need_mml:
            out["ssh"] = self.secret(*keys["ssh"])
            out["telnet"] = self.secret(*keys["telnet"])
        if self.cfg["crm"].get("login_user_selector"):
            out["crm"] = self.secret(*keys["crm"])
        return out

    def checked_values(self):
        v = self.values()
        errs = validate(v)
        if errs:
            messagebox.showerror(APP, "\n".join(errs))
            return None
        return v

    def start_worker(self, action, target, *args):
        self.busy = True
        self.action = action
        for b in self.buttons:
            b.state(["disabled"])
        name = datetime.datetime.now().strftime("code33_%Y%m%d_%H%M%S.log")
        self.logfile = open(os.path.join(LOG_DIR, name), "w", encoding="utf-8")

        def work():
            try:
                target(*args)
                self.q.put(("done", (True, None)))
            except Exception as e:
                self.log("ERROR: " + str(e))
                self.log(traceback.format_exc())
                self.q.put(("done", (False, e)))

        threading.Thread(target=work, daemon=True).start()

    def finish(self, ok, err):
        self.busy = False
        for b in self.buttons:
            b.state(["!disabled"])
        if self.logfile:
            self.logfile.close()
            self.logfile = None
        self.bring_to_front()
        if not ok:
            messagebox.showerror(APP, f"❌ Code 33 FAILED\n\n{err}\n\nCheck the log (also saved in {LOG_DIR}).",
                                 parent=self.root)
        elif self.action == "run" and not self.dry.get():
            v = self.last_values
            c = self.cfg["crm"]
            messagebox.showinfo(
                APP,
                f"✅ Code 33 finished\n\n"
                f"Number: {to_local(v['msisdn'])}\n"
                f"IMSI1: {v['imsi1']}\n"
                f"IMSI2: {v['imsi2']}\n\n"
                f"CUDBSUE commands sent x{self.cfg['run'].get('repeat', '3')}\n"
                f"CRM status is back to {c['status_in_use']}\n\n"
                f"You can check now that it's working.",
                parent=self.root,
            )
        elif self.action == "run":
            messagebox.showinfo(APP, "Dry run finished — nothing was changed.", parent=self.root)

    def bring_to_front(self):
        """Pop the window over everything else, so the user sees it even if he switched to another app."""
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(500, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()
        self.root.bell()

    # --- actions
    def preview(self):
        v = self.checked_values()
        if v:
            self.log(f"CRM search: {to_local(v['msisdn'])}  |  IMSI row: {v['imsi1']}")
            for c in build_commands(v, self.cfg["run"]["impi_realm"]):
                self.log("  " + c)

    def run(self):
        v = self.checked_values()
        if not v or self.busy:
            return
        c = self.cfg["crm"]
        if not self.dry.get() and not messagebox.askyesno(
            APP,
            f"Run code 33?\n\nCRM: {to_local(v['msisdn'])} / IMSI {v['imsi1']}\n"
            f"{c['status_in_use']} → {c['status_suspended']} → CUDBSUE x{self.cfg['run'].get('repeat', '3')} "
            f"→ {c['status_in_use']}\n\nIMSI2: {v['imsi2']}   MSISDN2: {v['msisdn2']}",
        ):
            return
        try:
            secrets = {} if self.dry.get() else self.collect_secrets()
        except Cancelled:
            return
        self.last_values = v
        self.start_worker("run", run_flow, self.cfg, v, secrets, self.log, self.dry.get())

    def test(self):
        v = self.checked_values()
        if not v or self.busy:
            return
        try:
            secrets = self.collect_secrets(need_mml=False)
        except Cancelled:
            return
        self.start_worker("test", test_crm, self.cfg, v, secrets, self.log)

    def reset_passwords(self):
        for key, _ in self.secret_keys().values():
            try:
                keyring.delete_password(APP, key)
            except keyring.errors.PasswordDeleteError:
                pass
        messagebox.showinfo(APP, "Saved passwords cleared. You'll be asked again on the next run.")


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
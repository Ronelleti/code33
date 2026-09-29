"""
NOC Tools - Code 33 + Code 13 in one window
--------------------------------------------
- Uses the SAME working logic as code33_app.py and code13_app.py (they must be in the same folder).
- Its own settings file: noc_tools.ini (CRM + SSH + telnet + run settings for both codes).
- Shows every run as a live list of steps, so you can see where it is and where it stopped.

Run:  python noc_tools_app.py
"""
import configparser
import datetime
import os
import queue
import re
import threading
import traceback
from tkinter import messagebox, simpledialog

import customtkinter as ctk

from code33_app import (APP, CONFIG_PATH, LOG_DIR, Cancelled, build_commands, keyring, parse_message,
                        run_flow, test_crm, to_local, validate)
from code13_app import normalize_local, run_code13, test_code13

TOOLS_CONFIG = os.path.join(os.path.dirname(CONFIG_PATH), "noc_tools.ini")

# ----------------------------------------------------------------------------- look
C = {
    "bg": "#1B2230",        # window
    "side": "#151B26",      # sidebar
    "panel": "#242D3D",     # cards
    "field": "#2D3749",     # inputs
    "line": "#35405A",
    "text": "#E7EBF2",
    "muted": "#8C98AD",
    "primary": "#3F7BF2",
    "primary_h": "#346BD6",
    "ok": "#35B97A",
    "run": "#F0B545",
    "fail": "#E5574D",
    "idle": "#46526A",
}
UI = "Segoe UI"
MONO = "Consolas"


def font(size=13, weight="normal", family=UI):
    return ctk.CTkFont(family=family, size=size, weight=weight)


# ----------------------------------------------------------------------------- step list
class StepList(ctk.CTkFrame):
    """Vertical list of the procedure's steps with a live state for each one."""

    def __init__(self, master, **kw):
        super().__init__(master, fg_color=C["panel"], corner_radius=12, **kw)
        self.title = ctk.CTkLabel(self, text="Steps", font=font(15, "bold"), text_color=C["text"], anchor="w")
        self.title.pack(fill="x", padx=18, pady=(16, 6))
        self.banner = ctk.CTkLabel(self, text="", font=font(13, "bold"), corner_radius=8, height=40,
                                   text_color="#FFFFFF", wraplength=300, justify="left")
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.rows, self.current = [], None

    def set_steps(self, labels):
        for w in self.body.winfo_children():
            w.destroy()
        self.rows, self.current = [], None
        self.banner.pack_forget()
        for n, text in enumerate(labels, 1):
            row = ctk.CTkFrame(self.body, fg_color="transparent")
            row.pack(fill="x", pady=4)
            dot = ctk.CTkLabel(row, text=str(n), width=28, height=28, corner_radius=14, fg_color=C["idle"],
                               text_color=C["text"], font=font(12, "bold"))
            dot.pack(side="left", padx=(4, 10))
            col = ctk.CTkFrame(row, fg_color="transparent")
            col.pack(side="left", fill="x", expand=True)
            name = ctk.CTkLabel(col, text=text, font=font(13), text_color=C["muted"], anchor="w", justify="left")
            name.pack(fill="x")
            detail = ctk.CTkLabel(col, text="", font=font(11), text_color=C["muted"], anchor="w", justify="left")
            self.rows.append({"n": n, "dot": dot, "name": name, "detail": detail, "state": "idle"})

    def _paint(self, i, state):
        r = self.rows[i]
        r["state"] = state
        color = {"idle": C["idle"], "run": C["run"], "ok": C["ok"], "fail": C["fail"]}[state]
        mark = {"ok": "✓", "fail": "✕"}.get(state, str(r["n"]))
        r["dot"].configure(fg_color=color, text=mark, text_color="#1B2230" if state == "run" else C["text"])
        r["name"].configure(text_color=C["text"] if state != "idle" else C["muted"],
                            font=font(13, "bold") if state == "run" else font(13))

    def start(self, i, detail=None):
        if not (0 <= i < len(self.rows)):
            return
        if self.current is not None and self.current != i and self.rows[self.current]["state"] == "run":
            self._paint(self.current, "ok")
        self.current = i
        self._paint(i, "run")
        if detail:
            self.detail(i, detail)

    def done(self, i, detail=None):
        if 0 <= i < len(self.rows):
            self._paint(i, "ok")
            if detail:
                self.detail(i, detail)

    def detail(self, i, text):
        if 0 <= i < len(self.rows):
            d = self.rows[i]["detail"]
            d.configure(text=text)
            d.pack(fill="x")

    def fail(self):
        if self.current is not None:
            self._paint(self.current, "fail")

    def result(self, ok, text):
        self.banner.configure(text=text, fg_color=C["ok"] if ok else C["fail"])
        self.banner.pack(fill="x", padx=16, pady=(0, 8), after=self.title)


# ----------------------------------------------------------------------------- log -> steps
class Code33Tracker:
    LABELS = ["Log in to the CRM", "Suspend the SIM", "Send the CUDBSUE commands", "Put the SIM back in use"]

    def __init__(self, steps, cfg):
        self.s, self.S, self.U = steps, cfg["crm"]["status_suspended"], cfg["crm"]["status_in_use"]

    def feed(self, line):
        s = self.s
        if "[DRY RUN]" in line:
            return
        if line.startswith("CRM: please log in"):
            s.start(0, "Waiting for you to log in in the browser")
        elif line.startswith(("CRM: logged in", "CRM: already logged in")):
            s.done(0, "")
        elif line.startswith("CRM:") and f"-> {self.S}" in line:
            s.start(1, line.split(":", 1)[1].split("->")[0].strip())
        elif f"verified '{self.S}'" in line or f"already '{self.S}'" in line:
            s.done(1)
        elif line.startswith("SSH: connecting"):
            s.start(2, "Connecting")
        elif m := re.search(r"--- round (\d+)/(\d+) ---", line):
            s.detail(2, f"Round {m.group(1)} of {m.group(2)}")
        elif m := re.search(r"--- round (\d+): (\d+)/(\d+) OK", line):
            s.detail(2, f"Round {m.group(1)}: {m.group(2)} of {m.group(3)} OK")
        elif line.startswith("TELNET: done"):
            s.done(2)
        elif line.startswith("CRM:") and f"-> {self.U}" in line:
            s.start(3)
        elif f"verified '{self.U}'" in line:
            s.done(3)
        elif line.startswith("ERROR") or "COULD NOT RESTORE" in line:
            s.fail()


class Code13Tracker:
    LABELS = ["Log in to the CRM", "Read the line and its network status",
              "Suspend the correct SIM", "Remove the correct SIM from the line",
              "Attach the EDA SIM", "EDA SIM in use", "Suspend the EDA SIM", "Remove the EDA SIM from the line",
              "Attach the correct SIM back", "Correct SIM in use", "Check the network shows CONNECTED"]

    def __init__(self, steps, cfg):
        self.s = steps

    def feed(self, line):
        s = self.s
        if line.startswith("CRM: please log in"):
            s.start(0, "Waiting for you to log in in the browser")
        elif line.startswith(("CRM: logged in", "CRM: already logged in")):
            s.done(0, "")
            s.start(1)
        elif m := re.match(r"\*\*\* Correct CRM IMSI.*?(\d{15})", line):
            s.detail(1, f"Correct IMSI {m.group(1)}")
        elif line.startswith("CRM: network status") and s.current == 1:
            s.done(1)
            s.detail(1, (s.rows[1]["detail"].cget("text") + "   " + line.split("=", 1)[1].split("(")[0].strip()))
        elif m := re.match(r"=== step (\d+)/(\d+)", line):
            s.start(int(m.group(1)) + 1)
        elif line.startswith("CRM: network status") and s.current == 9:
            s.done(9)
            s.start(10, line.split("=", 1)[1].split("(")[0].strip())
        elif line.startswith("CRM: network status") and s.current == 10:
            s.detail(10, line.split("=", 1)[1].split("(")[0].strip())
        elif line.startswith("✅"):
            s.done(10, "CONNECTED")
        elif line.startswith("ERROR") or line.startswith("‼"):
            s.fail()


# ----------------------------------------------------------------------------- app
class NocTools(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.ready = False
        ctk.set_appearance_mode("dark")
        self.cfg = configparser.ConfigParser(interpolation=None)
        if not self.cfg.read(TOOLS_CONFIG, encoding="utf-8"):
            messagebox.showerror(APP, f"noc_tools.ini not found next to the app:\n{TOOLS_CONFIG}")
            self.destroy()
            return
        os.makedirs(LOG_DIR, exist_ok=True)
        self.q, self.busy, self.logfile, self.tracker = queue.Queue(), False, None, None

        self.title("NOC Tools")
        self.geometry("1180x800")
        self.minsize(1000, 680)
        self.configure(fg_color=C["bg"])
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._sidebar()
        self.main = ctk.CTkFrame(self, fg_color="transparent")
        self.main.grid(row=0, column=1, sticky="nsew", padx=24, pady=20)
        self.main.grid_columnconfigure(0, weight=3)
        self.main.grid_columnconfigure(1, weight=2)
        self.main.grid_rowconfigure(1, weight=1)

        self.pages = {"33": self._page33(), "13": self._page13()}
        self.steps = StepList(self.main)
        self.steps.grid(row=0, column=1, sticky="nsew", padx=(16, 0))
        self._logpanel()
        self.show("33")
        self.after(100, self.pump)
        self.ready = True

    # --- layout
    def _sidebar(self):
        side = ctk.CTkFrame(self, fg_color=C["side"], corner_radius=0, width=210)
        side.grid(row=0, column=0, sticky="ns")
        side.grid_propagate(False)
        ctk.CTkLabel(side, text="NOC Tools", font=font(20, "bold"), text_color=C["text"]).pack(
            anchor="w", padx=22, pady=(26, 2))
        ctk.CTkLabel(side, text="SIM fixes for the CRM", font=font(12), text_color=C["muted"]).pack(
            anchor="w", padx=22, pady=(0, 26))
        self.nav = {}
        for key, text in (("33", "Code 33"), ("13", "Code 13")):
            b = ctk.CTkButton(side, text=text, anchor="w", height=42, corner_radius=8, font=font(14),
                              fg_color="transparent", hover_color=C["panel"], text_color=C["muted"],
                              command=lambda k=key: self.show(k))
            b.pack(fill="x", padx=12, pady=3)
            self.nav[key] = b
        bottom = ctk.CTkFrame(side, fg_color="transparent")
        bottom.pack(side="bottom", fill="x", padx=12, pady=18)
        self.dry = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(bottom, text="Dry run (change nothing)", variable=self.dry, font=font(12),
                      text_color=C["text"], progress_color=C["run"]).pack(anchor="w", padx=8, pady=(0, 14))
        for text, cmd in (("Open log folder", lambda: os.startfile(LOG_DIR)),
                          ("Reset saved passwords", self.reset_passwords)):
            ctk.CTkButton(bottom, text=text, anchor="w", height=34, fg_color="transparent", hover_color=C["panel"],
                          text_color=C["muted"], font=font(12), command=cmd).pack(fill="x", pady=2)

    def _card(self):
        return ctk.CTkFrame(self.main, fg_color=C["panel"], corner_radius=12)

    def _header(self, parent, title, sub):
        ctk.CTkLabel(parent, text=title, font=font(24, "bold"), text_color=C["text"], anchor="w").pack(
            fill="x", padx=22, pady=(20, 2))
        ctk.CTkLabel(parent, text=sub, font=font(13), text_color=C["muted"], anchor="w", justify="left",
                     wraplength=560).pack(fill="x", padx=22, pady=(0, 16))

    def _entry(self, parent, label, var, row, col, width=250):
        ctk.CTkLabel(parent, text=label, font=font(12), text_color=C["muted"], anchor="w").grid(
            row=row * 2, column=col, sticky="w", padx=(0, 16), pady=(8, 2))
        e = ctk.CTkEntry(parent, textvariable=var, width=width, height=38, font=font(15, family=MONO),
                         fg_color=C["field"], border_color=C["line"], text_color=C["text"])
        e.grid(row=row * 2 + 1, column=col, sticky="w", padx=(0, 16))
        return e

    def _buttons(self, parent, spec):
        bar = ctk.CTkFrame(parent, fg_color="transparent")
        bar.pack(fill="x", padx=22, pady=(18, 22))
        out = []
        for text, cmd, kind in spec:
            primary = kind == "primary"
            b = ctk.CTkButton(bar, text=text, command=cmd, height=42, corner_radius=8,
                              font=font(14, "bold" if primary else "normal"),
                              fg_color=C["primary"] if primary else "transparent",
                              hover_color=C["primary_h"] if primary else C["field"],
                              border_width=0 if primary else 1, border_color=C["line"], text_color=C["text"])
            b.pack(side="left", padx=(0, 10))
            out.append(b)
        return out

    def _page33(self):
        card = self._card()
        self._header(card, "Code 33",
                     "Suspends the SIM in the CRM, sends the CUDBSUE commands three times, "
                     "then puts the SIM back in use.")
        ctk.CTkLabel(card, text="Teams message", font=font(12), text_color=C["muted"], anchor="w").pack(
            fill="x", padx=22)
        self.msg = ctk.CTkTextbox(card, height=110, font=font(13, family=MONO), fg_color=C["field"],
                                  border_color=C["line"], border_width=1, text_color=C["text"])
        self.msg.pack(fill="x", padx=22, pady=(4, 6))
        self.msg.bind("<KeyRelease>", lambda e: self.parse33())
        self.msg.bind("<<Paste>>", lambda e: self.after(50, self.parse33))
        ctk.CTkLabel(card, text="Paste the message and the fields below fill in by themselves.", font=font(11),
                     text_color=C["muted"], anchor="w").pack(fill="x", padx=22)
        grid = ctk.CTkFrame(card, fg_color="transparent")
        grid.pack(fill="x", padx=22, pady=(8, 0))
        self.v33 = {k: ctk.StringVar() for k in ("msisdn", "imsi1", "imsi2", "msisdn2")}
        self._entry(grid, "MSISDN", self.v33["msisdn"], 0, 0)
        self._entry(grid, "IMSI1", self.v33["imsi1"], 0, 1)
        self._entry(grid, "IMSI2", self.v33["imsi2"], 1, 0)
        self._entry(grid, "MSISDN2", self.v33["msisdn2"], 1, 1)
        self.btn33 = self._buttons(card, [("Run code 33", self.run33, "primary"),
                                          ("Test CRM", self.test33, "secondary"),
                                          ("Show commands", self.preview33, "secondary")])
        return card

    def _page13(self):
        card = self._card()
        self._header(card, "Code 13",
                     "For a line whose network IMSI (EDA) is different from the CRM. Swaps the SIMs on the line "
                     "and puts the correct one back, then checks the network shows CONNECTED.")
        grid = ctk.CTkFrame(card, fg_color="transparent")
        grid.pack(fill="x", padx=22)
        self.v13 = {"phone": ctk.StringVar(), "eda": ctk.StringVar()}
        self._entry(grid, "Phone number (05… or 972…)", self.v13["phone"], 0, 0, width=300)
        self._entry(grid, "IMSI shown in EDA", self.v13["eda"], 1, 0, width=300)
        ctk.CTkLabel(card, text="The correct IMSI is read from the CRM automatically. Closing the ticket stays with you.",
                     font=font(11), text_color=C["muted"], anchor="w", justify="left", wraplength=560).pack(
            fill="x", padx=22, pady=(10, 0))
        self.btn13 = self._buttons(card, [("Run code 13", self.run13, "primary"),
                                          ("Test code 13", self.test13, "secondary")])
        return card

    def _logpanel(self):
        box = ctk.CTkFrame(self.main, fg_color=C["panel"], corner_radius=12)
        box.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(16, 0))
        head = ctk.CTkFrame(box, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(10, 4))
        ctk.CTkLabel(head, text="Log", font=font(13, "bold"), text_color=C["text"]).pack(side="left")
        ctk.CTkButton(head, text="Clear", width=70, height=28, fg_color="transparent", border_width=1,
                      border_color=C["line"], hover_color=C["field"], text_color=C["muted"], font=font(12),
                      command=lambda: self.logbox.delete("1.0", "end")).pack(side="right")
        self.logbox = ctk.CTkTextbox(box, font=font(12, family=MONO), fg_color=C["bg"], text_color=C["text"],
                                     corner_radius=8)
        self.logbox.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    def show(self, key):
        if self.busy:
            return
        for k, page in self.pages.items():
            page.grid_forget()
            self.nav[k].configure(fg_color="transparent", text_color=C["muted"])
        self.pages[key].grid(row=0, column=0, sticky="nsew")
        self.nav[key].configure(fg_color=C["panel"], text_color=C["text"])
        self.page = key
        self.steps.set_steps(Code33Tracker.LABELS if key == "33" else Code13Tracker.LABELS)

    # --- plumbing
    def log(self, text):
        self.q.put(("log", text))

    def ask(self, question):
        evt, box = threading.Event(), {}
        self.q.put(("ask", (question, evt, box)))
        evt.wait()
        return box.get("answer", False)

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
                    if self.tracker:
                        first = str(payload).splitlines()[0].strip() if str(payload).strip() else ""
                        self.tracker.feed(first)
                elif kind == "ask":
                    q, evt, box = payload
                    self.front()
                    box["answer"] = messagebox.askyesno(APP, q, parent=self)
                    evt.set()
                elif kind == "done":
                    self.finish(*payload)
        except queue.Empty:
            pass
        self.after(100, self.pump)

    def front(self):
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self.after(500, lambda: self.attributes("-topmost", False))
        self.focus_force()
        self.bell()

    def all_buttons(self):
        return self.btn33 + self.btn13 + list(self.nav.values())

    def start_worker(self, action, tracker, target, *args):
        self.busy, self.action = True, action
        for b in self.all_buttons():
            b.configure(state="disabled")
        self.steps.set_steps(Code33Tracker.LABELS if self.page == "33" else Code13Tracker.LABELS)
        self.tracker = tracker
        name = datetime.datetime.now().strftime(f"noc_code{self.page}_%Y%m%d_%H%M%S.log")
        self.logfile = open(os.path.join(LOG_DIR, name), "w", encoding="utf-8")

        def work():
            try:
                target(*args)
                self.q.put(("done", (True, None)))
            except Cancelled:
                self.log("Stopped by the user.")
                self.q.put(("done", (True, "cancelled")))
            except Exception as e:
                self.log("ERROR: " + str(e))
                self.log(traceback.format_exc())
                self.q.put(("done", (False, e)))

        threading.Thread(target=work, daemon=True).start()

    def finish(self, ok, err):
        self.busy, self.tracker = False, None
        for b in self.all_buttons():
            b.configure(state="normal")
        if self.logfile:
            self.logfile.close()
            self.logfile = None
        self.front()
        code = "Code " + self.page
        if ok and err == "cancelled":
            self.steps.result(True, "Stopped. Nothing was changed.")
        elif not ok:
            self.steps.result(False, f"{code} stopped at the red step. The log says what to finish by hand.")
            messagebox.showerror(APP, f"{code} failed\n\n{err}\n\nThe log has the details.", parent=self)
        elif self.action.startswith("run") and self.dry.get():
            self.steps.result(True, "Dry run finished. Nothing was changed.")
        elif self.action == "run33":
            v = self.last
            self.steps.result(True, "Finished. Ask the customer to check the line.")
            messagebox.showinfo(APP, f"Code 33 finished\n\nNumber: {to_local(v['msisdn'])}\n"
                                     f"Commands sent x{self.cfg['run'].get('repeat', '3')}, SIM back "
                                     f"{self.cfg['crm']['status_in_use']}.\n\nYou can check now that it's working.",
                                parent=self)
        elif self.action == "run13":
            self.steps.result(True, "Fixed. The line is CONNECTED. Close the ticket.")
            messagebox.showinfo(APP, f"Code 13 fixed\n\nNumber: {self.last['local']}\nNetwork status: CONNECTED\n\n"
                                     f"Close the ticket (טופל) and assign it.", parent=self)
        else:
            self.steps.result(True, "Test passed. Nothing was changed.")

    # --- passwords (same saved passwords as the separate apps)
    def secret(self, key, label):
        val = keyring.get_password(APP, key)
        if not val:
            val = simpledialog.askstring(APP, f"{label} password\n(saved in Windows Credential Manager):",
                                         show="*", parent=self)
            if not val:
                raise Cancelled()
            keyring.set_password(APP, key, val)
        return val

    def secret_keys(self):
        s, t = self.cfg["ssh"], self.cfg["telnet"]
        return {"ssh": (f"ssh:{s['user']}@{s['host']}", f"SSH {s['user']}@{s['host']}"),
                "telnet": (f"telnet:{t['user']}@{t['host']}", f"TELNET {t['user']}@{t['host']}"),
                "crm": ("crm:" + self.cfg["crm"].get("login_user", ""), "CRM")}

    def secrets(self, need_mml):
        keys, out = self.secret_keys(), {}
        if need_mml:
            out["ssh"] = self.secret(*keys["ssh"])
            out["telnet"] = self.secret(*keys["telnet"])
        if self.cfg["crm"].get("login_user_selector"):
            out["crm"] = self.secret(*keys["crm"])
        return out

    def reset_passwords(self):
        for key, _ in self.secret_keys().values():
            try:
                keyring.delete_password(APP, key)
            except keyring.errors.PasswordDeleteError:
                pass
        messagebox.showinfo(APP, "Saved passwords cleared. You'll be asked again on the next run.", parent=self)

    # --- code 33
    def parse33(self):
        found = parse_message(self.msg.get("1.0", "end"))
        for k, val in found.items():
            if val:
                self.v33[k].set(val)

    def values33(self):
        v = {k: re.sub(r"\D", "", var.get()) for k, var in self.v33.items()}
        errs = validate(v)
        if errs:
            messagebox.showerror(APP, "\n".join(errs), parent=self)
            return None
        return v

    def preview33(self):
        v = self.values33()
        if v:
            self.log(f"CRM search: {to_local(v['msisdn'])}  |  IMSI row: {v['imsi1']}")
            for c in build_commands(v, self.cfg["run"]["impi_realm"]):
                self.log("  " + c)

    def run33(self):
        v = self.values33()
        if not v or self.busy:
            return
        c = self.cfg["crm"]
        if not self.dry.get() and not messagebox.askyesno(
                APP, f"Run code 33?\n\nNumber: {to_local(v['msisdn'])}\nIMSI1: {v['imsi1']}\n"
                     f"IMSI2: {v['imsi2']}   MSISDN2: {v['msisdn2']}\n\n"
                     f"The SIM goes {c['status_suspended']}, the commands run "
                     f"{self.cfg['run'].get('repeat', '3')} times, then the SIM goes back {c['status_in_use']}.",
                parent=self):
            return
        try:
            secrets = {} if self.dry.get() else self.secrets(need_mml=True)
        except Cancelled:
            return
        self.last = v
        self.start_worker("run33", Code33Tracker(self.steps, self.cfg), run_flow, self.cfg, v, secrets, self.log,
                          self.dry.get())

    def test33(self):
        v = self.values33()
        if not v or self.busy:
            return
        try:
            secrets = self.secrets(need_mml=False)
        except Cancelled:
            return
        self.start_worker("test33", Code33Tracker(self.steps, self.cfg), test_crm, self.cfg, v, secrets, self.log)

    # --- code 13
    def values13(self):
        local = normalize_local(self.v13["phone"].get())
        eda = re.sub(r"\D", "", self.v13["eda"].get())
        errs = []
        if not local:
            errs.append("Phone number: use 05XXXXXXXX or 9725XXXXXXXX.")
        if not re.fullmatch(r"425\d{12}", eda):
            errs.append("IMSI from EDA: 15 digits starting with 425.")
        if errs:
            messagebox.showerror(APP, "\n".join(errs), parent=self)
            return None
        return {"local": local, "eda": eda}

    def run13(self):
        v = self.values13()
        if not v or self.busy:
            return
        if not self.dry.get() and not messagebox.askyesno(
                APP, f"Run code 13?\n\nLine: {v['local']}\nIMSI from EDA: {v['eda']}\n\n"
                     f"The SIMs on this line are swapped in the CRM and the correct one is put back at the end.",
                parent=self):
            return
        try:
            secrets = {} if self.dry.get() else self.secrets(need_mml=False)
        except Cancelled:
            return
        self.last = v
        self.start_worker("run13", Code13Tracker(self.steps, self.cfg), run_code13, self.cfg, v, secrets,
                          self.log, self.ask, self.dry.get())

    def test13(self):
        v = self.values13()
        if not v or self.busy:
            return
        try:
            secrets = self.secrets(need_mml=False)
        except Cancelled:
            return
        self.start_worker("test13", Code13Tracker(self.steps, self.cfg), test_code13, self.cfg, v, secrets,
                          self.log)


if __name__ == "__main__":
    app = NocTools()
    if app.ready:
        app.mainloop()
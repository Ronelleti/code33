"""
Code 13 helper  (separate app - uses the same config.ini, CRM login and log folder as Code 33)
--------------
Line has code 13 = the IMSI in the network (EDA) is different from the IMSI in the CRM.
1. Read the line's SIM in the CRM (the correct IMSI) + network status
2. Correct IMSI -> מושהה -> remove Mobile Subscription
3. בחר EDA IMSI (row with ICCID) -> בשימוש -> מושהה -> remove Mobile Subscription
4. בחר correct IMSI (row with ICCID) -> בשימוש
5. סטטוס מנוי ברשת must show CONNECTED (if still code 13: מושהה -> בשימוש once more)
Closing the ticket stays manual.

Run:  python code13_app.py      (code33_app.py must be in the same folder)
"""
import datetime
import os
import queue
import re
import threading
import time
import traceback
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
import configparser

from playwright.sync_api import TimeoutError as PWTimeout

# shared parts from the code 33 app (browser/CRM login, config, passwords)
from code33_app import APP, CONFIG_PATH, CRM, LOG_DIR, Cancelled, keyring, simpledialog


IMSI_RE = re.compile(r"\b425\d{12}\b")
ICCID_RE = re.compile(r"\b89\d{15,18}\b")


def normalize_local(number):
    d = re.sub(r"\D", "", number)
    if re.fullmatch(r"9725\d{8}", d):
        return "0" + d[3:]
    if re.fullmatch(r"05\d{8}", d):
        return d
    return None


class CRM13(CRM):
    """Extra CRM actions for code 13: the line's SIM table, בחר window, detach, network status."""

    # --- the line's SIMS INVENTORY table
    def sims_table(self):
        sel = self.c.get("sims_table_selector", "table:has(#subs_mobile_inven_sims_search)")
        return self.page.locator(sel).locator("visible=true").first

    def ensure_sims_open(self):
        table = self.sims_table()
        try:
            table.wait_for(state="visible", timeout=3000)
            return table
        except PWTimeout:
            pass
        header = self.page.get_by_text(self.c["sims_section_text"], exact=False).locator("visible=true").first
        header.scroll_into_view_if_needed()
        header.click()
        table.wait_for(state="visible")
        self.page.wait_for_timeout(800)
        return table

    def sim_rows(self, local):
        """{imsi: row text} of the SIMs attached to the line."""
        self.open_subscriber(local)
        table = self.ensure_sims_open()
        rows = table.locator("tbody tr")
        out = {}
        for i in range(rows.count()):
            txt = rows.nth(i).inner_text()
            m = IMSI_RE.search(txt)
            if m:
                out[m.group(0)] = " | ".join(x.strip() for x in txt.split("\t") if x.strip())
        return out

    # --- edit form of one SIM
    def _open_edit(self, local, imsi):
        p = self.page
        self.open_subscriber(local)
        table = self.ensure_sims_open()
        row = table.locator("tr", has_text=imsi).filter(has=p.get_by_text(self.c["edit_text"])).first
        try:
            row.wait_for(state="visible", timeout=15000)
        except PWTimeout:
            raise RuntimeError(f"CRM: IMSI {imsi} is not in this line's SIMS INVENTORY.")
        row.get_by_text(self.c["edit_text"]).first.click()
        p.locator("select:visible", has=p.locator("option", has_text=self.c["status_in_use"])).first.wait_for()

    def _choose_status(self, target):
        p = self.page
        select = p.locator("select:visible", has=p.locator("option", has_text=target)).first
        select.wait_for(state="visible", timeout=10000)
        select.select_option(label=target)

    def _save(self):
        p = self.page
        t = self.c["save_text"]
        p.get_by_role("button", name=t).or_(
            p.locator(f"input[type=submit][value='{t}'], input[type=button][value='{t}']")
        ).first.click()
        p.wait_for_load_state("domcontentloaded")
        p.wait_for_timeout(1500)

    def _mobile_sub_field(self):
        p = self.page
        label = self.c.get("mobile_sub_label", "Mobile Subscription")
        item = p.locator(".edit-view-row-item", has=p.get_by_text(label, exact=False)).locator("visible=true").first
        btn = item.locator("button[name^='btn_clr_']").first
        if not item.count() or not btn.count():
            raise RuntimeError(f"CRM: could not find the X next to '{label}' in the edit form.")
        return item, btn

    def edit_sim(self, local, imsi, status=None, detach=False):
        what = f"status -> {status}" if status else "remove Mobile Subscription (X)"
        self.log(f"CRM: IMSI {imsi}: {what}")
        self._open_edit(local, imsi)
        if status:
            self._choose_status(status)
        if detach:
            item, btn = self._mobile_sub_field()
            btn.click()
            left = item.locator("input[type=text]").first.input_value().strip()
            if left:
                raise RuntimeError(f"CRM: Mobile Subscription was not cleared (still '{left}').")
        self._save()
        rows = self.sim_rows(local)
        if detach:
            if imsi in rows:
                raise RuntimeError(f"CRM: IMSI {imsi} is still on the line after removing Mobile Subscription.")
            self.log(f"CRM: verified {imsi} is no longer on the line.")
        else:
            if imsi not in rows or status not in rows[imsi]:
                raise RuntimeError(f"CRM: IMSI {imsi} did not change to '{status}'. Row: {rows.get(imsi)}")
            self.log(f"CRM: verified {imsi} = '{status}'.")

    # --- בחר window
    def _open_select_popup(self, local, imsi):
        self.open_subscriber(local)
        table = self.ensure_sims_open()
        t = self.c.get("select_text", "בחר")
        btn = table.locator(f"input[value='{t}'], button:text-is('{t}'), a:text-is('{t}')").locator("visible=true").first
        with self.ctx.expect_page() as info:
            btn.click()
        pop = info.value
        pop.on("dialog", lambda d: d.accept())
        pop.set_default_timeout(int(self.c.get("timeout_ms", "30000")))
        pop.wait_for_load_state("domcontentloaded")
        field = pop.locator("xpath=//*[normalize-space(text())='IMSI' or normalize-space(text())='IMSI:']"
                            "/following::input[@type='text'][1]").first
        field.fill(imsi)
        f = self.c.get("filter_text", "מסנן")
        pop.locator(f"input[value='{f}'], button:has-text('{f}')").first.click()
        try:
            pop.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass
        rows = pop.locator("tr", has_text=imsi)
        try:
            rows.first.wait_for(state="visible", timeout=15000)
        except PWTimeout:
            pop.close()
            raise RuntimeError(f"CRM: IMSI {imsi} was not found in the {t} window.")
        pop.wait_for_timeout(500)
        with_iccid = [rows.nth(i) for i in range(rows.count()) if ICCID_RE.search(rows.nth(i).inner_text())]
        if len(with_iccid) != 1:
            pop.close()
            raise RuntimeError(f"CRM: expected exactly 1 result with an ICCID for IMSI {imsi}, found {len(with_iccid)}.")
        return pop, with_iccid[0]

    def attach_sim(self, local, imsi):
        self.log(f"CRM: attaching IMSI {imsi} to {local} ({self.c.get('select_text', 'בחר')})")
        pop, row = self._open_select_popup(local, imsi)
        self.log("CRM: chosen row: " + " | ".join(x.strip() for x in row.inner_text().split("\t") if x.strip()))
        row.locator("a", has_text=imsi).first.click()
        try:
            if not pop.is_closed():
                pop.wait_for_event("close", timeout=15000)
        except PWTimeout:
            pass
        self.page.wait_for_timeout(1500)
        rows = self.sim_rows(local)
        if imsi not in rows:
            raise RuntimeError(f"CRM: IMSI {imsi} does not appear on the line after choosing it.")
        self.log(f"CRM: verified {imsi} is on the line: {rows[imsi]}")

    # --- סטטוס מנוי ברשת
    def network_status(self, local):
        p = self.page
        self.open_subscriber(local)
        t = self.c.get("net_status_text", "סטטוס מנוי ברשת")
        p.locator(f"input[value='{t}'], button:has-text('{t}'), a:has-text('{t}')").locator("visible=true").first.click()
        rx = re.compile(r"Code:\s*\d+|CONNECTED", re.I)
        text, deadline = "", time.time() + 30
        while time.time() < deadline and not text:
            dlg = p.locator(".ui-dialog, .modal-dialog, [role=dialog]").locator("visible=true")
            for i in range(dlg.count()):
                if rx.search(dlg.nth(i).inner_text()):
                    text = dlg.nth(i).inner_text()
            if not text:
                p.wait_for_timeout(500)
        if not text:
            raise RuntimeError("CRM: the network status window did not show a result.")
        try:
            p.get_by_text(self.c.get("close_text", "סגירה"), exact=True).locator("visible=true").first.click(timeout=3000)
        except Exception:
            pass
        flat = " ".join(text.split())
        if re.search(r"Code:\s*13\b", flat):
            status = "CODE 13"
        elif re.search(r"NOT\s+CONNECTED", flat, re.I):
            status = "NOT CONNECTED"
        elif re.search(r"CONNECTED", flat, re.I):
            status = "CONNECTED"
        else:
            status = "OTHER"
        self.log(f"CRM: network status = {status}   ({flat[:160]})")
        return status


def run_code13(cfg, v, secrets, log, ask, dry_run):
    local, eda = v["local"], v["eda"]
    c = cfg["crm"]
    S, U = c["status_suspended"], c["status_in_use"]
    plan = [
        f"read the line's SIM (the CRM IMSI = the correct one) and the network status",
        f"CRM IMSI -> {S}",
        f"CRM IMSI -> remove Mobile Subscription (X)",
        f"בחר -> EDA IMSI {eda} (the row with ICCID)",
        f"EDA IMSI -> {U}",
        f"EDA IMSI -> {S}",
        f"EDA IMSI -> remove Mobile Subscription (X)",
        f"בחר -> CRM IMSI (the row with ICCID)",
        f"CRM IMSI -> {U}",
        f"סטטוס מנוי ברשת -> must be CONNECTED (if still code 13: {S} -> {U} once more)",
    ]
    if dry_run:
        log(f"[DRY RUN] code 13 for {local}, EDA IMSI {eda}:")
        for n, step in enumerate(plan, 1):
            log(f"  {n}. {step}")
        return

    crm = CRM13(cfg, log)
    done, good = [], None
    try:
        crm.start()
        crm.ensure_login(secrets.get("crm"))
        rows = crm.sim_rows(local)
        for imsi, txt in rows.items():
            log(f"CRM: SIM on the line: {txt}")
        if eda in rows:
            raise RuntimeError(f"The EDA IMSI {eda} is already on this line in the CRM - the IMSIs match, "
                               f"this is not the code 13 case.")
        in_use = [i for i, t in rows.items() if U in t]
        if len(in_use) != 1:
            raise RuntimeError(f"Expected exactly one SIM '{U}' on the line, found {len(in_use)}. Nothing was changed.")
        good = in_use[0]
        log(f"*** Correct CRM IMSI (will be put back at the end): {good}")

        if crm.network_status(local) == "CONNECTED" and not ask(
                f"The line {local} already shows CONNECTED (no code 13).\n\nContinue with the code 13 fix anyway?"):
            raise Cancelled()

        steps = [
            (f"{good} -> {S}", lambda: crm.edit_sim(local, good, status=S)),
            (f"remove {good} from the line", lambda: crm.edit_sim(local, good, detach=True)),
            (f"attach EDA IMSI {eda}", lambda: crm.attach_sim(local, eda)),
            (f"{eda} -> {U}", lambda: crm.edit_sim(local, eda, status=U)),
            (f"{eda} -> {S}", lambda: crm.edit_sim(local, eda, status=S)),
            (f"remove {eda} from the line", lambda: crm.edit_sim(local, eda, detach=True)),
            (f"attach {good} back", lambda: crm.attach_sim(local, good)),
            (f"{good} -> {U}", lambda: crm.edit_sim(local, good, status=U)),
        ]
        for n, (name, fn) in enumerate(steps, 1):
            log(f"=== step {n}/{len(steps)}: {name} ===")
            fn()
            done.append(name)

        status = None
        for _ in range(3):
            crm.page.wait_for_timeout(5000)
            status = crm.network_status(local)
            if status == "CONNECTED":
                break
        if status == "CODE 13":
            log(f"Still code 13 -> {good}: {S} then {U}")
            crm.edit_sim(local, good, status=S)
            crm.edit_sim(local, good, status=U)
            done.append(f"{good} toggled {S} -> {U}")
            crm.page.wait_for_timeout(5000)
            status = crm.network_status(local)
        if status != "CONNECTED":
            raise RuntimeError(f"All CRM steps were done, but the network status is {status}.")
        log(f"✅ {local} is CONNECTED with IMSI {good}.")
    except Cancelled:
        raise
    except Exception:
        if good:
            log("‼ STOPPED - finish manually in the CRM:")
            log(f"   correct IMSI (must end up on the line, {U}): {good}")
            log(f"   EDA IMSI (must end up {S} and NOT on the line): {eda}")
            log("   steps done: " + ("; ".join(done) or "none - nothing was changed"))
        raise
    finally:
        crm.stop()


def test_code13(cfg, v, secrets, log):
    local, eda = v["local"], v["eda"]
    crm = CRM13(cfg, log)
    try:
        crm.start()
        crm.ensure_login(secrets.get("crm"))
        rows = crm.sim_rows(local)
        for txt in rows.values():
            log(f"CRM: SIM on the line: {txt}")
        crm.network_status(local)
        in_use = [i for i, t in rows.items() if cfg["crm"]["status_in_use"] in t]
        if len(in_use) == 1:
            crm._open_edit(local, in_use[0])
            item, btn = crm._mobile_sub_field()
            btn.highlight()
            log("CRM: found the X next to Mobile Subscription (NOT clicked).")
            crm.page.wait_for_timeout(3000)
        else:
            log(f"CRM: {len(in_use)} SIMs in use on the line - skipping the edit-form check.")
        pop, row = crm._open_select_popup(local, eda)
        row.highlight()
        log("CRM: בחר window would choose: " + " | ".join(x.strip() for x in row.inner_text().split("\t") if x.strip()))
        pop.wait_for_timeout(4000)
        pop.close()
        log("Code 13 test OK - nothing was changed.")
    finally:
        crm.stop()


# ----------------------------------------------------------------------------- GUI
class App13:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.busy = False
        self.logfile = None
        self.cfg = configparser.ConfigParser(interpolation=None)
        if not self.cfg.read(CONFIG_PATH, encoding="utf-8"):
            messagebox.showerror(APP, f"config.ini not found next to the app:\n{CONFIG_PATH}")
            root.destroy()
            return
        os.makedirs(LOG_DIR, exist_ok=True)

        root.title("Code 13")
        root.geometry("900x650")
        frm = ttk.Frame(root, padding=10)
        frm.pack(fill="both", expand=True)

        grid = ttk.Frame(frm)
        grid.pack(fill="x", pady=4)
        self.vars = {"phone": tk.StringVar(), "eda": tk.StringVar()}
        ttk.Label(grid, text="Phone (05X / 972)", width=18).grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(grid, textvariable=self.vars["phone"], width=30).grid(row=0, column=1, padx=6)
        ttk.Label(grid, text="IMSI from EDA", width=18).grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(grid, textvariable=self.vars["eda"], width=30).grid(row=1, column=1, padx=6)

        bar = ttk.Frame(frm)
        bar.pack(fill="x", pady=6)
        self.dry = tk.BooleanVar(value=False)
        self.buttons = [
            ttk.Button(bar, text="▶ RUN code 13", command=self.run),
            ttk.Button(bar, text="Test code 13 (no changes)", command=self.test),
        ]
        for b in self.buttons:
            b.pack(side="left", padx=3)
        ttk.Checkbutton(bar, text="Dry run", variable=self.dry).pack(side="left", padx=10)

        self.logbox = scrolledtext.ScrolledText(frm, height=25, font=("Consolas", 9))
        self.logbox.pack(fill="both", expand=True)
        root.after(100, self.pump)

    # --- helpers
    def log(self, text):
        self.q.put(("log", text))

    def ask(self, question):
        """Yes/No question from the worker thread (waits for the user's answer)."""
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
                elif kind == "ask":
                    question, evt, box = payload
                    self.bring_to_front()
                    box["answer"] = messagebox.askyesno(APP, question, parent=self.root)
                    evt.set()
                elif kind == "done":
                    self.finish(*payload)
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def secrets(self):
        """CRM password only if auto-login is configured (otherwise each person logs in by hand)."""
        if not self.cfg["crm"].get("login_user_selector"):
            return {}
        key = "crm:" + self.cfg["crm"].get("login_user", "")
        val = keyring.get_password(APP, key)
        if not val:
            val = simpledialog.askstring(APP, "CRM password\n(saved in Windows Credential Manager):",
                                         show="*", parent=self.root)
            if not val:
                raise Cancelled()
            keyring.set_password(APP, key, val)
        return {"crm": val}

    def values(self):
        local = normalize_local(self.vars["phone"].get())
        eda = re.sub(r"\D", "", self.vars["eda"].get())
        errs = []
        if not local:
            errs.append("Phone should be 05XXXXXXXX or 9725XXXXXXXX")
        if not re.fullmatch(r"425\d{12}", eda):
            errs.append("IMSI from EDA should be 15 digits starting with 425")
        if errs:
            messagebox.showerror(APP, "\n".join(errs))
            return None
        return {"local": local, "eda": eda}

    def start_worker(self, action, target, *args):
        self.busy = True
        self.action = action
        for b in self.buttons:
            b.state(["disabled"])
        name = datetime.datetime.now().strftime("code13_%Y%m%d_%H%M%S.log")
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
        self.busy = False
        for b in self.buttons:
            b.state(["!disabled"])
        if self.logfile:
            self.logfile.close()
            self.logfile = None
        self.bring_to_front()
        if ok and err == "cancelled":
            messagebox.showinfo(APP, "Stopped — nothing was changed.", parent=self.root)
        elif not ok:
            messagebox.showerror(APP, f"❌ Code 13 FAILED\n\n{err}\n\nCheck the log (also saved in {LOG_DIR}).",
                                 parent=self.root)
        elif self.action == "run" and self.dry.get():
            messagebox.showinfo(APP, "Dry run finished — nothing was changed.", parent=self.root)
        elif self.action == "run":
            messagebox.showinfo(
                APP,
                f"✅ Code 13 fixed\n\nNumber: {self.last['local']}\n"
                f"The correct SIM is back on the line ({self.cfg['crm']['status_in_use']})\n"
                f"Network status: CONNECTED\n\nClose the ticket (טופל) and assign it.",
                parent=self.root,
            )

    def bring_to_front(self):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(500, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()
        self.root.bell()

    # --- actions
    def run(self):
        v = self.values()
        if not v or self.busy:
            return
        if not self.dry.get() and not messagebox.askyesno(
            APP,
            f"Run code 13?\n\nLine: {v['local']}\nIMSI from EDA: {v['eda']}\n\n"
            f"The app will swap the SIMs on this line in the CRM and put the correct one back at the end.",
        ):
            return
        try:
            secrets = {} if self.dry.get() else self.secrets()
        except Cancelled:
            return
        self.last = v
        self.start_worker("run", run_code13, self.cfg, v, secrets, self.log, self.ask, self.dry.get())

    def test(self):
        v = self.values()
        if not v or self.busy:
            return
        try:
            secrets = self.secrets()
        except Cancelled:
            return
        self.start_worker("test", test_code13, self.cfg, v, secrets, self.log)


if __name__ == "__main__":
    root = tk.Tk()
    App13(root)
    root.mainloop()
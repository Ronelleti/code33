"""
Code 13 helper  (separate app with its own config13.ini - reuses the CRM login code from code33_app.py)
--------------
Line has code 13 = the IMSI in the network (EDA) is different from the IMSI in the CRM.
1. Read the line's SIM in the CRM (the correct IMSI) + network status
2. Correct IMSI -> מושהה -> remove Mobile Subscription
3. בחר EDA IMSI (row with ICCID) -> בשימוש -> מושהה -> remove Mobile Subscription
4. בחר correct IMSI (row with ICCID) -> בשימוש
5. סטטוס מנוי ברשת must show CONNECTED (if still code 13: מושהה -> בשימוש once more)
Closing the ticket stays manual.

Run:  python code13_app.py      (code33_app.py in the same folder or in ../code33)
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

import sys

# this app's own folder (next to the .py, or next to Code13.exe)
HERE = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
# code33_app.py can be next to this file or in the sibling folder ..\code33
sys.path.insert(0, os.path.join(HERE, "..", "code33"))

# shared parts from the code 33 app (browser/CRM login, config, passwords)
from code33_app import APP, CRM, LOG_DIR, Cancelled, keyring, simpledialog

# code 13 has its own settings file, next to this app
CONFIG13_PATH = os.path.join(HERE, "config13.ini")


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

    def snapshot(self, name):
        """Save a screenshot of the browser to the log folder (for debugging)."""
        try:
            path = os.path.join(LOG_DIR, datetime.datetime.now().strftime(f"code13_%H%M%S_{name}.png"))
            self.page.screenshot(path=path, full_page=True)
            self.log(f"   screenshot saved: {path}")
        except Exception:
            pass

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
        try:
            table.locator("tr").filter(has_text=IMSI_RE).first.wait_for(state="visible", timeout=10000)
        except PWTimeout:
            self.log("CRM: ⚠ no SIM rows found in the line's SIMS INVENTORY")
            self.snapshot("no_sim_rows")
        rows = table.locator("tr")
        out = {}
        for i in range(rows.count()):
            txt = rows.nth(i).inner_text()
            m = IMSI_RE.search(txt)
            if m:
                out[m.group(0)] = " | ".join(x.strip() for x in txt.split("\t") if x.strip())
        return out

    def log_line(self, local, rows):
        if not rows:
            self.log(f"CRM:    line {local} now has no SIM in SIMS INVENTORY")
        for txt in rows.values():
            self.log(f"CRM:    line {local} now has: {txt}")

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

    def _open_edit_direct(self, local, imsi):
        """Open the SIM record (click the IMSI) and press עריכה there.
        The subpanel עריכה re-links the SIM to the line on save, so detaching must be done this way."""
        p = self.page
        self.open_subscriber(local)
        table = self.ensure_sims_open()
        link = table.locator("a", has_text=imsi).locator("visible=true").first
        try:
            link.wait_for(state="visible", timeout=15000)
        except PWTimeout:
            raise RuntimeError(f"CRM: IMSI {imsi} is not in this line's SIMS INVENTORY.")
        link.click()
        p.wait_for_load_state("domcontentloaded")
        t = self.c["edit_text"]
        btn = p.locator(f"#edit_button, input[value='{t}'], button:text-is('{t}'), a:text-is('{t}')").locator(
            "visible=true").first
        btn.wait_for(state="visible")
        btn.click()
        p.locator("select:visible", has=p.locator("option", has_text=self.c["status_in_use"])).first.wait_for()
        # just in case: anything that would link the record back to a parent on save
        for n in ("relate_to", "relate_id", "return_relationship"):
            f = p.locator(f"input[name='{n}']")
            for i in range(f.count()):
                if f.nth(i).input_value():
                    self.log(f"CRM: clearing hidden '{n}' = {f.nth(i).input_value()}")
                    f.nth(i).evaluate("e => e.value = ''")

    def _choose_status(self, target):
        p = self.page
        select = p.locator("select:visible", has=p.locator("option", has_text=target)).first
        select.wait_for(state="visible", timeout=10000)
        try:
            before = select.locator("option:checked").inner_text().strip()
        except Exception:
            before = "?"
        select.select_option(label=target)
        self.log(f"CRM:    status in the form: {before} -> {target}")

    def _save(self):
        p = self.page
        t = self.c["save_text"]
        btn = p.get_by_role("button", name=t).or_(
            p.locator(f"input[type=submit][value='{t}'], input[type=button][value='{t}']")
        ).locator("visible=true").first
        btn.click()
        try:  # after a real save the CRM leaves the edit form, so the save button disappears
            btn.wait_for(state="hidden", timeout=15000)
        except PWTimeout:
            msgs = p.locator(".validation-message, .error, .alert-danger").locator("visible=true").all_inner_texts()
            self.snapshot("save_failed")
            raise RuntimeError("CRM: the save did not go through (still on the edit form). "
                               + (" / ".join(m.strip() for m in msgs if m.strip()) or ""))
        p.wait_for_load_state("domcontentloaded")
        p.wait_for_timeout(1500)
        self.log(f"CRM:    pressed {t} - saved (left the edit form)")

    def _mobile_sub_field(self, local):
        """The Mobile Subscription field = the text box that shows this line's number, and its X button."""
        p = self.page
        box = p.locator(f"input[type=text][value='{local}']").locator("visible=true").first
        if box.count():
            name = box.get_attribute("name") or ""
            btn = p.locator(f"button[name='btn_clr_{name}']").first
            if name and btn.count():
                self.log(f"CRM: Mobile Subscription field = '{name}' (shows {local})")
                return box, btn
        # fallback: by the label text
        label = self.c.get("mobile_sub_label", "Mobile Subscription")
        item = p.locator(".edit-view-row-item", has=p.get_by_text(label, exact=False)).locator("visible=true").last
        box = item.locator("input[type=text]").first
        btn = item.locator("button[name^='btn_clr_']").first
        if not box.count() or not btn.count():
            self.snapshot("no_mobile_sub_field")
            raise RuntimeError(f"CRM: could not find the X next to '{label}' in the edit form.")
        self.log(f"CRM: Mobile Subscription field (by label) = '{box.get_attribute('name')}' "
                 f"value '{box.input_value()}'")
        return box, btn

    def edit_sim(self, local, imsi, status=None, detach=False):
        what = f"status -> {status}" if status else "remove Mobile Subscription (X)"
        self.log(f"CRM: IMSI {imsi}: {what}")
        if detach:
            self._open_edit_direct(local, imsi)
            self.log(f"CRM:    opened the SIM's own page -> {self.c['edit_text']}")
        else:
            self._open_edit(local, imsi)
            self.log(f"CRM:    opened {self.c['edit_text']} from the line's SIMS INVENTORY")
        if status:
            self._choose_status(status)
        if detach:
            box, btn = self._mobile_sub_field(local)
            name = box.get_attribute("name") or ""
            btn.click()
            self.log(f"CRM:    pressed X next to Mobile Subscription ({local})")
            self.page.wait_for_timeout(300)
            left = box.input_value().strip()
            if left:
                raise RuntimeError(f"CRM: Mobile Subscription was not cleared (still '{left}').")
            # the matching hidden id field (xxx_name -> xxx_id) must be empty too
            if name.endswith("_name"):
                hid = self.page.locator(f"input[name='{name[:-5]}_id']").first
                if hid.count() and hid.input_value():
                    hid.evaluate("e => e.value = ''")
                    self.log(f"CRM: also cleared hidden field {name[:-5]}_id")
        self._save()
        rows = self.sim_rows(local)
        self.log_line(local, rows)
        if detach:
            if imsi in rows:
                self.page.wait_for_timeout(3000)
                rows = self.sim_rows(local)
            if imsi in rows:
                self.snapshot("still_on_line")
                raise RuntimeError(f"CRM: IMSI {imsi} is still on the line after removing Mobile Subscription.")
            self.log(f"CRM: verified {imsi} is no longer on the line.")
        else:
            if imsi not in rows or status not in rows[imsi]:
                raise RuntimeError(f"CRM: IMSI {imsi} did not change to '{status}'. Row: {rows.get(imsi)}")
            self.log(f"CRM: verified {imsi} = '{status}'.")

    # --- בחר window
    def _open_select_popup(self, local, imsi, iccid=None):
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
        self.log(f"CRM:    {t} window opened, typed IMSI {imsi}")
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
        self.log(f"CRM:    {rows.count()} result(s) for {imsi}:")
        for i in range(rows.count()):
            self.log("CRM:      - " + " | ".join(x.strip() for x in rows.nth(i).inner_text().split("\t") if x.strip()))
        with_iccid = [rows.nth(i) for i in range(rows.count()) if ICCID_RE.search(rows.nth(i).inner_text())]
        if iccid:
            with_iccid = [r for r in with_iccid if iccid in r.inner_text()]
        if len(with_iccid) != 1:
            pop.close()
            raise RuntimeError(f"CRM: expected exactly 1 result with an ICCID"
                               f"{' ' + iccid if iccid else ''} for IMSI {imsi}, found {len(with_iccid)}.")
        row_text = with_iccid[0].inner_text()
        other_lines = sorted(set(re.findall(r"\b05\d{8}\b", row_text)) - {local})
        if other_lines or self.c["status_in_use"] in row_text:
            pop.close()
            raise RuntimeError(
                f"CRM: SIM {imsi} is {self.c['status_in_use'] if self.c['status_in_use'] in row_text else 'in the list'}"
                + (f" and attached to another line ({', '.join(other_lines)})" if other_lines else "")
                + ". It must be suspended/freed first - the app will not take a SIM from another line."
            )
        return pop, with_iccid[0]

    def attach_sim(self, local, imsi, iccid=None):
        self.log(f"CRM: attaching IMSI {imsi}{' / ICCID ' + iccid if iccid else ''} to {local} "
                 f"({self.c.get('select_text', 'בחר')})")
        pop, row = self._open_select_popup(local, imsi, iccid)
        self.log("CRM: chosen row: " + " | ".join(x.strip() for x in row.inner_text().split("\t") if x.strip()))
        row.locator("a", has_text=imsi).first.click()
        try:
            if not pop.is_closed():
                pop.wait_for_event("close", timeout=15000)
        except PWTimeout:
            pass
        self.page.wait_for_timeout(1500)
        rows = self.sim_rows(local)
        self.log_line(local, rows)
        if imsi not in rows:
            raise RuntimeError(f"CRM: IMSI {imsi} does not appear on the line after choosing it.")
        if iccid and iccid not in rows[imsi]:
            raise RuntimeError(f"CRM: IMSI {imsi} is on the line but with a different ICCID (expected {iccid}).")
        self.log(f"CRM: verified {imsi} is on the line: {rows[imsi]}")

    # --- סטטוס מנוי ברשת
    def network_status(self, local):
        p = self.page
        self.open_subscriber(local)
        t = self.c.get("net_status_text", "סטטוס מנוי ברשת")
        btn = p.get_by_role("button", name=t).or_(
            p.locator(f"input[value*='{t}'], button:has-text('{t}'), a:has-text('{t}')")
        ).locator("visible=true").first
        try:
            btn.wait_for(state="visible", timeout=10000)
        except PWTimeout:
            self.snapshot("no_status_button")
            raise RuntimeError(f"CRM: could not find the '{t}' button on the line's page.")
        btn.click()
        rx = re.compile(r"Code:\s*\d+[^\n]*|NOT\s+CONNECTED|CONNECTED", re.I)
        text, deadline = "", time.time() + 30
        while time.time() < deadline and not text:
            p.wait_for_timeout(700)
            dlg = p.locator(".ui-dialog, .modal, .modal-dialog, .bootbox, [role=dialog]").locator("visible=true")
            for i in range(dlg.count()):
                if rx.search(dlg.nth(i).inner_text()):
                    text = dlg.nth(i).inner_text()
            if not text:  # fallback: look at the whole visible page
                m = rx.search(p.locator("body").inner_text())
                if m:
                    text = m.group(0)
        if not text:
            self.snapshot("no_network_status")
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


def restore_line(crm, local, good, good_iccid, eda, S, U, log):
    """Bring the line back to how it was: EDA SIM off the line, the original SIM (same IMSI + ICCID) on it, in use."""
    rows = crm.sim_rows(local)
    log("↩ restore - the line right now:")
    crm.log_line(local, rows)
    if eda in rows:
        log(f"↩ restore: removing the EDA SIM {eda} from the line")
        if U in rows[eda]:
            crm.edit_sim(local, eda, status=S)
        crm.edit_sim(local, eda, detach=True)
        rows = crm.sim_rows(local)
    if good not in rows:
        log(f"↩ restore: attaching the original SIM {good} / ICCID {good_iccid}")
        crm.attach_sim(local, good, good_iccid)
        rows = crm.sim_rows(local)
    if U not in rows.get(good, ""):
        log(f"↩ restore: setting {good} back to {U}")
        crm.edit_sim(local, good, status=U)
        rows = crm.sim_rows(local)
    if good_iccid and good_iccid not in rows.get(good, ""):
        raise RuntimeError(f"the original SIM {good} is back but its ICCID is not {good_iccid}")


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
    done, good, good_iccid, changing = [], None, None, False
    log(f"Code 13 started for {local}, EDA IMSI {eda}")
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
        m = ICCID_RE.search(rows[good])
        good_iccid = m.group(0) if m else None
        before = rows[good]
        log(f"*** Correct CRM IMSI (will be put back at the end): {good}")
        log(f"    original SIM: IMSI {good}, ICCID {good_iccid or 'unknown'}  ({before})")

        # check the EDA SIM BEFORE changing anything: it must exist with an ICCID and must not be
        # in use / attached to another line - otherwise stop now, while the line is still untouched
        log(f"CRM: checking the EDA SIM {eda} in the {c.get('select_text', 'בחר')} window (no changes)...")
        pop, row = crm._open_select_popup(local, eda)
        eda_row = row.inner_text()
        m = ICCID_RE.search(eda_row)
        eda_iccid = m.group(0) if m else None
        log("CRM: EDA SIM is OK to use: " + " | ".join(x.strip() for x in eda_row.split("\t") if x.strip()))
        log(f"    EDA SIM: IMSI {eda}, ICCID {eda_iccid or 'unknown'}")
        pop.close()

        if crm.network_status(local) == "CONNECTED" and not ask(
                f"The line {local} already shows CONNECTED (no code 13).\n\nContinue with the code 13 fix anyway?"):
            raise Cancelled()

        steps = [
            (f"{good} -> {S}", lambda: crm.edit_sim(local, good, status=S)),
            (f"remove {good} from the line", lambda: crm.edit_sim(local, good, detach=True)),
            (f"attach EDA IMSI {eda}", lambda: crm.attach_sim(local, eda, eda_iccid)),
            (f"{eda} -> {U}", lambda: crm.edit_sim(local, eda, status=U)),
            (f"{eda} -> {S}", lambda: crm.edit_sim(local, eda, status=S)),
            (f"remove {eda} from the line", lambda: crm.edit_sim(local, eda, detach=True)),
            (f"attach {good} back", lambda: crm.attach_sim(local, good, good_iccid)),
            (f"{good} -> {U}", lambda: crm.edit_sim(local, good, status=U)),
        ]
        changing = True
        t_all = time.time()
        for n, (name, fn) in enumerate(steps, 1):
            log(f"=== step {n}/{len(steps)}: {name} ===")
            t0 = time.time()
            fn()
            done.append(name)
            log(f"    step {n} done in {time.time() - t0:.0f}s")

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
        after = crm.sim_rows(local)
        log("──── summary ────")
        log(f"  line:        {local}")
        log(f"  before:      {before}")
        for txt in after.values():
            log(f"  after:       {txt}")
        log(f"  EDA SIM:     {eda} / {eda_iccid or '?'} -> {S}, not on the line")
        log(f"  total time:  {time.time() - t_all:.0f}s")
        log(f"✅ {local} is CONNECTED with IMSI {good}.")
    except Cancelled:
        raise
    except Exception as first_error:
        if good and changing:
            log(f"‼ Stopped in the middle ({first_error}). Putting the correct SIM {good} back on the line...")
            try:
                restore_line(crm, local, good, good_iccid, eda, S, U, log)
                log(f"↩ Restored: {good} is back on the line, {U}. The line is as it was before the run.")
                raise RuntimeError(f"{first_error}\n\nThe correct SIM {good} was put back on the line ({U}) - "
                                   f"the line is as it was before the run.")
            except Exception as e:
                if str(e).startswith(str(first_error)):
                    raise
                log(f"‼ The automatic restore also failed: {e}")
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
            crm._open_edit_direct(local, in_use[0])
            box, btn = crm._mobile_sub_field(local)
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
        if not self.cfg.read(CONFIG13_PATH, encoding="utf-8"):
            messagebox.showerror(APP, f"config13.ini not found next to the app:\n{CONFIG13_PATH}")
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
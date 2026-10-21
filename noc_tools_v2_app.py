"""
NOC Tools v2 - everything in noc_tools_app.py + Provisioning (SOAP) ported from the SoapUI project
--------------------------------------------
- Uses the SAME working logic as code33_app.py and code13_app.py (same folder, or ../code33 and ../code13).
- Its own settings file: noc_tools.ini (CRM + SSH + telnet + run settings for both codes, [soap] for provisioning).
- Shows every run as a live list of steps, so you can see where it is and where it stopped.
- New page "Provisioning (SOAP)": the Wecom SoapUI project (HSS/EPS, IMS, MMTel, ENUM; Get / Create / Delete)
  as Login -> request(s) -> Logout against the CAI3G gateway.

Run:  python noc_tools_v2_app.py
"""
import configparser
import datetime
import os
import queue
import re
import threading
import time
import traceback
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from tkinter import messagebox, simpledialog
from xml.sax.saxutils import escape

import customtkinter as ctk
import paramiko
from playwright.sync_api import TimeoutError as PWTimeout

import sys

# this app's own folder (next to the .py, or next to NOCTools.exe)
HERE = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
# the code 33 / code 13 files can be next to this file or in the sibling folders ../code33 and ../code13
for sub in ("code33", "code13"):
    sys.path.insert(0, os.path.join(HERE, "..", sub))

from code33_app import (APP, CRM, LOG_DIR, Cancelled, Shell, build_commands, keyring,
                        parse_message, run_flow, test_crm, to_local, validate)
from code13_app import normalize_local, run_code13, test_code13
import code33_app

# ----------------------------------------------------------------------------- remembered CRM login
# Each Windows user types their CRM username + password once; they are kept in Windows Credential Manager
# and filled in automatically whenever the CRM shows its login page. (Only inside NOC Tools - the separate
# Code33/Code13 apps keep their manual login.)
CRM_USER_KEY = "crm_login_user"
CRM_CREDS = {}          # set by the app right before each run: {"user": ..., "password": ...}
_manual_login = code33_app.CRM.ensure_login


def crm_pw_key(user):
    return f"crm_login_pw:{user}"


def _remembered_login(self, crm_password):
    p = self.page
    p.goto(self.c["url"])
    p.wait_for_load_state("domcontentloaded")
    p.wait_for_timeout(1500)
    if not self._login_form_visible():
        self.log("CRM: already logged in.")
        return
    if not CRM_CREDS.get("user") or not CRM_CREDS.get("password"):
        return _manual_login(self, crm_password)
    user_sel = self.c.get("login_user_selector") or "input[name='user_name'], #user_name"
    pw_sel = self.c.get("login_password_selector") or "input[type=password]"
    btn_sel = self.c.get("login_button_selector") or "#bigbutton, input[type=submit], button[type=submit]"
    user_box = p.locator(user_sel).locator("visible=true").first
    if not user_box.count():  # unknown login page -> let the person log in by hand
        return _manual_login(self, crm_password)
    self.log(f"CRM: logging in as {CRM_CREDS['user']}...")
    user_box.fill(CRM_CREDS["user"])
    p.locator(pw_sel).locator("visible=true").first.fill(CRM_CREDS["password"])
    p.locator(btn_sel).locator("visible=true").first.click()
    try:
        p.locator("input[type=password]:visible").first.wait_for(state="hidden", timeout=20000)
    except PWTimeout:
        try:
            keyring.delete_password(APP, crm_pw_key(CRM_CREDS["user"]))
        except Exception:
            pass
        raise RuntimeError(f"CRM login failed for '{CRM_CREDS['user']}'. The saved CRM password was removed - "
                           f"you'll be asked for it again on the next run.")
    p.wait_for_load_state("domcontentloaded")
    self.log("CRM: logged in.")


code33_app.CRM.ensure_login = _remembered_login   # CRM13 (code 13) inherits it too


# ----------------------------------------------------------------------------- CUDBSUE sending (NOC Tools' own copy)
# NOC Tools uses its own, up-to-date command sending, whatever version of code33_app.py sits next to it:
# - one command at a time, reading the result as soon as it arrives (it often comes together with ORDERED)
# - any answer from the CUDB counts as done (12001 "not defined", 2001 "format error", COMPLETED ...)
# - only "busy" (1093/1098) is retried, and after a few busy commands in a row it stops and the SIM is restored
STATUS_RE = re.compile(r"(\d+)\s+([A-Z]+)\s+CUDBSUE")
BUSY_FAULTS = ("1093", "1098")  # too many transactions at once / no free handler -> wait and retry


def final_re(job):
    # the job's final line, e.g. "443932 COMPLETED CUDBSUE" / "443933 FAILED CUDBSUE" (not ORDERED / RESULT / FAULT)
    return rf"(?<!\d){job}\s+(?!ORDERED\b|RESULT\b|FAULT\b)([A-Z]+)\s+CUDBSUE"


def run_one(sh, cmd, t, r, log):
    """Send one command and wait until the CUDB reports its FINAL result (not just ORDERED)."""
    timeout = int(r.get("result_timeout", "30"))
    sh.send(cmd)
    out = sh.expect(t["mml_prompt"], timeout)
    m = STATUS_RE.search(out)
    if not m:
        return "UNKNOWN", out
    job = m.group(1)
    done = re.search(final_re(job), out)
    if done:  # the result often arrives together with ORDERED, in the same output
        return done.group(1), out
    try:
        res = sh.expect(final_re(job), timeout)
        res += sh.expect(t["mml_prompt"], 15)
        out += res
        return re.search(final_re(job), out).group(1), out
    except TimeoutError:
        log(f"  ⚠ job {job}: no final result within {timeout}s")
        return "NO RESULT", out


class CudbBusy(RuntimeError):
    pass


def run_with_retry(sh, cmd, t, r, log):
    """Returns (status, busy, fault code, fault message)."""
    retries, wait = int(r.get("busy_retries", "2")), float(r.get("busy_wait", "3"))
    for attempt in range(retries + 1):
        status, out = run_one(sh, cmd, t, r, log)
        fault = re.search(r"FAULT CODE (\d+)", out)
        code = fault.group(1) if fault else ""
        msg = re.search(r"FAULT MSG ([^\r\n]+)", out)
        busy = status == "FAILED" and code in BUSY_FAULTS
        if busy and attempt < retries:
            log(f"  … CUDB busy (fault {code}), retry {attempt + 1}/{retries} in {wait:g}s")
            time.sleep(wait)
            continue
        return status, busy, code, (msg.group(1).strip() if msg else "")


def run_mml(cfg, secrets, commands, log):
    s, t, r = cfg["ssh"], cfg["telnet"], cfg["run"]
    repeat = int(r.get("repeat", "3"))
    delay = float(r.get("command_delay", "0.3"))
    log(f"SSH: connecting to {s['user']}@{s['host']}...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(s["host"], int(s.get("port", "22")), s["user"], secrets["ssh"],
                   look_for_keys=False, allow_agent=False, timeout=20)
    ok, faults = set(), {}
    ok_faults = tuple(x.strip() for x in r.get("ok_faults", "12001").split(",") if x.strip())
    max_busy = int(r.get("stop_after_busy_commands", "3"))
    busy_in_a_row = 0
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
            good = 0
            for n, cmd in enumerate(commands):
                status, busy, code, msg = run_with_retry(sh, cmd, t, r, log)
                if status == "FAILED" and code in ok_faults:
                    # e.g. 12001 IDENTIFIER NOT DEFINED = this identity is not in the CUDB -> nothing to do
                    log(f"  ✓ answered: fault {code} ({msg})")
                    ok.add(n)
                    good += 1
                    busy_in_a_row = 0
                elif status == "FAILED" and code and not busy:
                    # the CUDB answered with another fault (e.g. 2001 FORMAT ERROR) - noted, not retried
                    log(f"  ⚠ answered with fault {code} ({msg}): {cmd}")
                    faults[n] = f"{code} {msg}"
                    ok.add(n)
                    good += 1
                    busy_in_a_row = 0
                elif status in ("FAILED", "UNKNOWN", "NO RESULT"):
                    log(f"  ✖ {status}: {cmd}")
                    busy_in_a_row = busy_in_a_row + 1 if busy else 0
                    if busy_in_a_row >= max_busy:
                        raise CudbBusy(
                            f"The CUDB is busy: {busy_in_a_row} commands in a row failed with 'resource limitation' "
                            f"(fault 1093/1098), even after retries. Stopped early so the SIM is not left "
                            f"suspended - try again in a few minutes, or check the CUDB/PG.")
                else:
                    ok.add(n)
                    good += 1
                    busy_in_a_row = 0
                time.sleep(delay)
            log(f"--- round {i}: {good}/{len(commands)} OK ---")
        sh.send(t.get("exit_command", "exit;"))
        time.sleep(1)
    finally:
        client.close()
    failed = [c for n, c in enumerate(commands) if n not in ok]
    if failed:
        raise RuntimeError(f"{len(failed)} command(s) did not succeed in any round:\n" + "\n".join(failed))
    if faults:
        log(f"⚠ {len(faults)} command(s) were answered with a fault (see above):")
        for n, f in faults.items():
            log(f"    {commands[n]}  ->  {f}")
    log(f"TELNET: done - all {len(commands)} commands were answered by the CUDB.")




code33_app.run_mml = run_mml   # run_flow (code 33 + queue) now sends the commands this way

TOOLS_CONFIG = os.path.join(HERE, "noc_tools.ini")


def icon_path(name):
    """The app's .ico - inside the exe (PyInstaller) or in the icons folder next to the .py."""
    base = getattr(sys, "_MEIPASS", HERE)
    for p in (os.path.join(base, "icons", name), os.path.join(HERE, "icons", name)):
        if os.path.exists(p):
            return p
    return None


def short(err, n=110):
    text = str(err).strip().splitlines()[0] if str(err).strip() else type(err).__name__
    return text if len(text) <= n else text[: n - 1] + "…"


# ----------------------------------------------------------------------------- queue: several messages at once
def split_requests(text):
    """Cut pasted text into one request per MSISDN= (each Teams message starts with its MSISDN)."""
    starts = [m.start() for m in re.finditer(r"\bMSISDN(?!\s*2)\s*[:=]", text, re.I)]
    out = []
    for i, st in enumerate(starts):
        seg = text[st: starts[i + 1] if i + 1 < len(starts) else len(text)]
        out.append({k: re.sub(r"\D", "", v) for k, v in parse_message(seg).items()})
    return out


# ----------------------------------------------------------------------------- health check
HEALTH_LABELS = ["CRM opens and logs in", "SSH connects", "Telnet logs in"]


def health_check(cfg, secrets, report, log):
    """report(index, state, detail) with state in run / ok / fail. Changes nothing anywhere."""
    s, t = cfg["ssh"], cfg["telnet"]

    report(0, "run", "Opening the CRM")
    t0, crm = time.time(), CRM(cfg, log)
    try:
        crm.start()
        crm.ensure_login(secrets.get("crm"))
        report(0, "ok", f"Logged in ({time.time() - t0:.0f}s)")
    except Exception as e:
        log("CRM check failed: " + str(e))
        report(0, "fail", short(e))
    finally:
        try:
            crm.stop()
        except Exception:
            pass

    report(1, "run", f"Connecting to {s['user']}@{s['host']}")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        t0 = time.time()
        client.connect(s["host"], int(s.get("port", "22")), s["user"], secrets["ssh"],
                       look_for_keys=False, allow_agent=False, timeout=15)
        sh = Shell(client.invoke_shell(width=300, height=200), log)
        sh.expect(s["shell_prompt"], 20)
        report(1, "ok", f"{s['user']}@{s['host']} ({time.time() - t0:.0f}s)")
    except paramiko.AuthenticationException:
        report(1, "fail", "SSH password rejected. Use Reset saved passwords and run again.")
        report(2, "fail", "Not checked, SSH failed")
        client.close()
        return
    except Exception as e:
        log("SSH check failed: " + str(e))
        report(1, "fail", short(e))
        report(2, "fail", "Not checked, SSH failed")
        client.close()
        return

    report(2, "run", f"telnet {t['host']} {t['port']}")
    try:
        t0 = time.time()
        sh.send(f"telnet {t['host']} {t['port']}")
        sh.expect(t["login_prompt"], 30)
        sh.send(t["user"])
        sh.expect(t["password_prompt"], 20)
        sh.send(secrets["telnet"], secret=True)
        out = sh.expect(t["mml_prompt"], 30)
        prompt = out.strip().splitlines()[-1].strip() if out.strip() else "prompt"
        report(2, "ok", f"Logged in at {prompt} ({time.time() - t0:.0f}s)")
        sh.send(t.get("exit_command", "exit;"))
        time.sleep(1)
    except Exception as e:
        log("Telnet check failed: " + str(e))
        closed = "closed" in str(e).lower()
        report(2, "fail", "Login rejected (check the telnet password)" if closed else short(e))
    finally:
        client.close()

# ----------------------------------------------------------------------------- SOAP provisioning
# The requests of the Wecom SoapUI project (HSS/EPS, IMS, MMTel, ENUM), sent as Login -> request(s) -> Logout
# to the CAI3G gateway. The gateway user is in noc_tools.ini [soap]; its password is asked once and kept in
# Windows Credential Manager like the other passwords (nothing secret is stored in this file).
SOAP_DEFAULTS = {
    "endpoint": "http://10.21.18.76:8080/CAI3G1.2/services/CAI3G1.2",
    "user": "admin",
    "timeout": "30",
    "realm1": "ims.mnc009.mcc425.3gppnetwork.org",   # primary IMSI / MSISDN
    "realm2": "ims.mnc001.mcc425.3gppnetwork.org",   # secondary IMSI / MSISDN
    "srvcc_number": "97251000700",
}
SOAP_SERVICES = {"HSS / EPS": "eps", "IMS association": "ims", "MMTel (MTAS)": "mmtel", "ENUM (IPWorks)": "enum"}
SOAP_ACTIONS = ("Get", "Create", "Delete")
# EPS profile per IMSI (IMSI1, IMSI2), as in the project's Create_EPS_* requests
EPS_TYPES = {"VoLTE": ("HSS-Volte", "HSS-VolteRoaming"),
             "5G with VoLTE": ("HSS-5GProfileWithVoLTE", "HSS-5GWithVolteRoaming")}
NS_HSS = "http://schemas.ericsson.com/ma/HSS/"
NS_MTAS = "http://schemas.ericsson.com/ema/UserProvisioning/MTAS/"
NS_IPW = "http://schemas.ericsson.com/ema/UserProvisioning/IPWorks/5.0/"
NS_IPW_OLD = "http://schemas.ericsson.com/ma/IPWORKS/"   # the project's ENUM Get / Delete declare this one


def soap_cfg(cfg):
    out = dict(SOAP_DEFAULTS)
    if cfg.has_section("soap"):
        out.update({k: v.strip() for k, v in cfg["soap"].items() if v.strip()})
    return out


def soap_needs(service, action):
    """(required fields, hint) for a service + action."""
    if service == "eps":
        return (("imsi1", "common") if action == "Create" else ("imsi1",),
                "IMSI1 (and IMSI2 if both SIMs)" + (", Common MSISDN, EPS type." if action == "Create" else "."))
    if service == "ims":
        return (("msisdn1", "imsi1", "msisdn2", "imsi2") if action == "Create" else ("msisdn1",),
                "MSISDN1, IMSI1, MSISDN2, IMSI2." if action == "Create" else "MSISDN1 (and MSISDN2 to do both).")
    if service == "mmtel":
        return (("common",) if action == "Create" else ("msisdn1",),
                "Common MSISDN." if action == "Create" else "MSISDN1 (and MSISDN2 to do both).")
    return (("msisdn1",), "MSISDN1 (and MSISDN2 to do both).")


def validate_soap(service, action, v):
    errs = []
    need = soap_needs(service, action)[0]
    for key, label in (("msisdn1", "MSISDN 1"), ("msisdn2", "MSISDN 2"), ("common", "Common MSISDN")):
        if (v[key] or key in need) and not re.fullmatch(r"972\d{9}", v[key]):
            errs.append(f"{label}: 12 digits starting with 972.")
    for key, label in (("imsi1", "IMSI 1"), ("imsi2", "IMSI 2")):
        if (v[key] or key in need) and not re.fullmatch(r"425\d{12}", v[key]):
            errs.append(f"{label}: 15 digits starting with 425.")
    return errs


def envelope(sid, nsdecl, body, seq=True):
    header = f"<cai3:SessionId>{sid}</cai3:SessionId>"
    if seq:
        header = "<cai3:SequenceId>1</cai3:SequenceId><cai3:TransactionId>1</cai3:TransactionId>" + header
    return ('<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
            f'xmlns:cai3="http://schemas.ericsson.com/cai3g1.2/" {nsdecl}>'
            f"<soapenv:Header>{header}</soapenv:Header><soapenv:Body>{body}</soapenv:Body></soapenv:Envelope>")


def login_xml(user, password):
    return ('<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
            'xmlns:cai3="http://schemas.ericsson.com/cai3g1.2/"><soapenv:Header/><soapenv:Body>'
            f"<cai3:Login><cai3:userId>{escape(user)}</cai3:userId><cai3:pwd>{escape(password)}</cai3:pwd>"
            "</cai3:Login></soapenv:Body></soapenv:Envelope>")


def logout_xml(sid):
    return envelope(sid, "", f"<cai3:Logout><cai3:sessionId>{sid}</cai3:sessionId></cai3:Logout>", seq=False)


def mo(op, motype, moid, attrs=""):
    a = f"<cai3:MOAttributes>{attrs}</cai3:MOAttributes>" if attrs else ""
    return f"<cai3:{op}><cai3:MOType>{motype}</cai3:MOType><cai3:MOId>{moid}</cai3:MOId>{a}</cai3:{op}>"


def ims_create_attrs(v, sc):
    r1, r2 = sc["realm1"], sc["realm2"]
    m1, i1, m2, i2 = v["msisdn1"], v["imsi1"], v["msisdn2"], v["imsi2"]

    def private(imsi, realm, msisdn):
        pid = f"{imsi}@{realm}"
        return (f'<hss:privateUser privateUserId="{pid}"><hss:privateUserId>{pid}</hss:privateUserId>'
                f"<hss:userBarringInd>false</hss:userBarringInd><hss:roamingAllowed>true</hss:roamingAllowed>"
                f"<hss:msisdn>{msisdn}</hss:msisdn></hss:privateUser>")

    def public(pid, profile):
        return (f'<hss:publicData publicIdValue="{pid}"><hss:publicIdValue>{pid}</hss:publicIdValue>'
                f"<hss:implicitRegSet>1</hss:implicitRegSet><hss:serviceProfileId>{profile}</hss:serviceProfileId>"
                f"</hss:publicData>")

    def profile(msisdn, realm):
        pid = f"{msisdn}@{realm}"
        return (f'<hss:subscriberServiceProfile serviceProfileId="{pid}"><hss:serviceProfileId>{pid}'
                f'</hss:serviceProfileId><hss:configuredServiceProfile configuredServiceProfileId="VoLTE_PS">'
                f"<hss:configuredServiceProfileId>VoLTE_PS</hss:configuredServiceProfileId>"
                f"</hss:configuredServiceProfile><hss:maxNumberSessions>5</hss:maxNumberSessions>"
                f"</hss:subscriberServiceProfile>")

    return (f'<hss:CreateIMSAssociation associationId="{m1}"><hss:associationId>{m1}</hss:associationId>'
            f"<hss:chargingProfId>DefaultChargingProfile</hss:chargingProfId>"
            + private(i1, r1, m1) + private(i2, r2, m2)
            + public(f"sip:+{m1}@{r1}", f"{m1}@{r1}") + public(f"sip:{i1}@{r1}", f"{m1}@{r1}")
            + public(f"tel:+{m1}", f"{m1}@{r1}") + public(f"sip:{i2}@{r2}", f"{m2}@{r2}")
            + profile(m1, r1) + profile(m2, r2) + "</hss:CreateIMSAssociation>")


MMTEL_CREATE = (
    '<mtas:createSubscription publicId="{P}"><mtas:publicId>{P}</mtas:publicId><mtas:services>'
    "<mtas:communication-diversion><mtas:cdiv-operator-configuration><mtas:activated>true</mtas:activated>"
    "<mtas:user-no-reply-timer>activated</mtas:user-no-reply-timer><mtas:cdiv-op-conditions>"
    "<mtas:anonymous-condition>deactivated</mtas:anonymous-condition><mtas:busy-condition>activated</mtas:busy-condition>"
    "<mtas:identity-condition>deactivated</mtas:identity-condition><mtas:media-condition>deactivated</mtas:media-condition>"
    "<mtas:not-registered-condition>activated</mtas:not-registered-condition>"
    "<mtas:no-answer-condition>activated</mtas:no-answer-condition>"
    "<mtas:presence-status-condition>deactivated</mtas:presence-status-condition>"
    "<mtas:validity-condition>deactivated</mtas:validity-condition>"
    "<mtas:not-reachable-condition>activated</mtas:not-reachable-condition>"
    "<mtas:valid-periods-condition>deactivated</mtas:valid-periods-condition>"
    "<mtas:invalidity-condition>deactivated</mtas:invalidity-condition></mtas:cdiv-op-conditions>"
    "<mtas:cdiv-op-actions><mtas:notify-caller-action>deactivated</mtas:notify-caller-action>"
    "<mtas:notify-served-user-action>deactivated</mtas:notify-served-user-action>"
    "<mtas:notify-served-user-on-outbound-call-action>deactivated</mtas:notify-served-user-on-outbound-call-action>"
    "<mtas:reveal-identity-to-caller-action>deactivated</mtas:reveal-identity-to-caller-action>"
    "<mtas:reveal-identity-to-target-action>deactivated</mtas:reveal-identity-to-target-action>"
    "<mtas:rule-no-reply-timer>activated</mtas:rule-no-reply-timer>"
    "<mtas:do-not-disturb-action>deactivated</mtas:do-not-disturb-action>"
    "<mtas:play-announcement-action>deactivated</mtas:play-announcement-action></mtas:cdiv-op-actions>"
    "<mtas:rule-limit>0</mtas:rule-limit></mtas:cdiv-operator-configuration>"
    "<mtas:cdiv-user-configuration><mtas:active>true</mtas:active></mtas:cdiv-user-configuration>"
    "</mtas:communication-diversion>"
    "<mtas:communication-waiting><mtas:cw-operator-configuration><mtas:activated>true</mtas:activated>"
    "</mtas:cw-operator-configuration><mtas:cw-user-configuration><mtas:active>true</mtas:active>"
    "</mtas:cw-user-configuration></mtas:communication-waiting>"
    "<mtas:conference><mtas:conf-operator-configuration><mtas:activated>true</mtas:activated>"
    "<mtas:max-number-of-parties>5</mtas:max-number-of-parties></mtas:conf-operator-configuration></mtas:conference>"
    "<mtas:explicit-communication-transfer><mtas:ect-operator-configuration><mtas:activated>true</mtas:activated>"
    "</mtas:ect-operator-configuration></mtas:explicit-communication-transfer>"
    "<mtas:outgoing-communication-barring><mtas:ocb-operator-configuration><mtas:activated>true</mtas:activated>"
    '<mtas:ocb-ruleset><mtas:ocb-rule id="obo"><mtas:id>obo</mtas:id><mtas:ocb-conditions>'
    "<mtas:rule-deactivated>true</mtas:rule-deactivated></mtas:ocb-conditions><mtas:cb-actions>"
    "<mtas:allow>false</mtas:allow></mtas:cb-actions></mtas:ocb-rule></mtas:ocb-ruleset><mtas:ocb-op-conditions>"
    "<mtas:international-condition>activated</mtas:international-condition></mtas:ocb-op-conditions>"
    "</mtas:ocb-operator-configuration><mtas:ocb-user-configuration><mtas:active>false</mtas:active>"
    "</mtas:ocb-user-configuration></mtas:outgoing-communication-barring>"
    "<mtas:originating-identity-presentation><mtas:oip-operator-configuration><mtas:activated>true</mtas:activated>"
    "<mtas:restriction-override>override-not-active</mtas:restriction-override></mtas:oip-operator-configuration>"
    "<mtas:oip-user-configuration><mtas:active>true</mtas:active></mtas:oip-user-configuration>"
    "</mtas:originating-identity-presentation>"
    "<mtas:supplementary-service-codes><mtas:ssc-operator-configuration><mtas:activated>true</mtas:activated>"
    "</mtas:ssc-operator-configuration></mtas:supplementary-service-codes>"
    "<mtas:terminating-identity-presentation><mtas:tip-operator-configuration><mtas:activated>true</mtas:activated>"
    "<mtas:restriction-override>override-not-active</mtas:restriction-override></mtas:tip-operator-configuration>"
    "<mtas:tip-user-configuration><mtas:active>true</mtas:active></mtas:tip-user-configuration>"
    "</mtas:terminating-identity-presentation></mtas:services></mtas:createSubscription>")


def build_plan(service, action, v, eps_type, sc):
    """The requests (after the login) for one service + action: [{label, ns, body, seq}]."""
    plan = []
    if service == "eps":
        motype, ns = f"EPSMultiSC@{NS_HSS}", f'xmlns:hss="{NS_HSS}"'
        for n in (1, 2):
            imsi = v[f"imsi{n}"]
            if not imsi:
                continue
            moid = f"<hss:imsi>{imsi}</hss:imsi>"
            attrs = ""
            if action == "Create":
                attrs = (f'<hss:CreateEPSMultiSC imsi="{imsi}"><hss:imsi>{imsi}</hss:imsi>'
                         f"<hss:epsProfileId>{EPS_TYPES[eps_type][n - 1]}</hss:epsProfileId>"
                         f"<hss:epsRoamingAllowed>true</hss:epsRoamingAllowed>"
                         f"<hss:epsSessionTransferNumber>{sc['srvcc_number']}</hss:epsSessionTransferNumber>"
                         f"<hss:commonMsisdn>{v['common']}</hss:commonMsisdn></hss:CreateEPSMultiSC>")
            plan.append({"label": f"{action} EPS, IMSI{n} {imsi}", "ns": ns, "seq": action != "Create",
                         "body": mo(action, motype, moid, attrs)})
    elif service == "ims":
        motype, ns = f"IMSAssociation@{NS_HSS}", f'xmlns:hss="{NS_HSS}"'
        if action == "Create":
            plan.append({"label": f"Create IMS association {v['msisdn1']}", "ns": ns, "seq": True,
                         "body": mo("Create", motype, f"<hss:associationId>{v['msisdn1']}</hss:associationId>",
                                    ims_create_attrs(v, sc))})
        else:
            for n in (1, 2):
                if v[f"msisdn{n}"]:
                    plan.append({"label": f"{action} IMS association, MSISDN{n} {v[f'msisdn{n}']}", "ns": ns,
                                 "seq": True, "body": mo(action, motype,
                                                         f"<hss:associationId>{v[f'msisdn{n}']}</hss:associationId>")})
    elif service == "mmtel":
        motype, ns = f"Subscription@{NS_MTAS}", f'xmlns:mtas="{NS_MTAS}"'
        if action == "Create":
            pub = f"sip:+{v['common']}@{sc['realm1']}"
            plan.append({"label": f"Create MTAS subscription {v['common']}", "ns": ns, "seq": True,
                         "body": mo("Create", motype, f"<mtas:publicId>{pub}</mtas:publicId>",
                                    MMTEL_CREATE.replace("{P}", pub))})
        else:
            for n in (1, 2):
                if v[f"msisdn{n}"]:
                    pub = f"sip:+{v[f'msisdn{n}']}@{sc['realm1']}"
                    plan.append({"label": f"{action} MTAS subscription, MSISDN{n} {v[f'msisdn{n}']}", "ns": ns,
                                 "seq": True, "body": mo(action, motype, f"<mtas:publicId>{pub}</mtas:publicId>")})
    else:
        motype = f"DNSSubscription@{NS_IPW}"
        for n in (1, 2):
            msisdn = v[f"msisdn{n}"]
            if not msisdn:
                continue
            tel = f"tel:+{msisdn}"
            if action == "Create":
                pub = f"sip:+{msisdn}@{sc['realm1']}"
                ns, seq = f'xmlns:ns="{NS_IPW}"', False
                body = mo("Create", motype, f"<ns:msisdn>{tel}</ns:msisdn>",
                          f'<ns:createDNSSubscription msisdn="{tel}"><ns:msisdn>{tel}</ns:msisdn>'
                          f'<ns:records publicId="{pub}"><ns:publicId>{pub}</ns:publicId><ns:flags>nu</ns:flags>'
                          f"<ns:order>10</ns:order><ns:preference>10</ns:preference><ns:service>E2U+SIP</ns:service>"
                          f"</ns:records></ns:createDNSSubscription>")
            elif action == "Delete":
                ns, seq = f'xmlns:ns="{NS_IPW_OLD}"', True
                body = mo("Delete", motype, f"<ns:msisdn>{tel}</ns:msisdn>",
                          f'<ns:deleteDNSSubscription msisdn="{tel}"/>')
            else:
                ns, seq = f'xmlns:ns="{NS_IPW_OLD}"', True
                body = mo("Get", motype, f'<ns:getDNSSubscription msisdn="{tel}"/><ns:msisdn>{tel}</ns:msisdn>')
            plan.append({"label": f"{action} ENUM record, MSISDN{n} {msisdn}", "ns": ns, "seq": seq, "body": body})
    return plan


def soap_post(url, xml, timeout):
    req = urllib.request.Request(url, data=xml.encode("utf-8"),
                                 headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '""'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:   # a SOAP fault comes back as HTTP 500 with the fault in the body
        return e.code, e.read().decode("utf-8", "replace")


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def soap_fault(body):
    """Fault text of a SOAP / CAI3G error answer, or '' when the answer is not a fault."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return ""
    names = [_local(e.tag) for e in root.iter()]
    if "Fault" not in names:
        return ""
    parts = [e.text.strip() for e in root.iter() if e.text and e.text.strip()
             and _local(e.tag) in ("faultstring", "errorCode", "errorMessage", "Text")]
    return " | ".join(dict.fromkeys(parts)) or "SOAP fault"


def soap_session(body):
    try:
        for e in ET.fromstring(body).iter():
            if _local(e.tag).lower() == "sessionid" and e.text and e.text.strip():
                return e.text.strip()
    except ET.ParseError:
        pass
    return ""


def pretty_xml(text):
    from xml.dom import minidom
    try:
        out = minidom.parseString(text).toprettyxml(indent="  ")
    except Exception:
        return text
    return "\n".join(line for line in out.splitlines() if line.strip() and not line.startswith("<?xml"))


def pretty_body(body):
    try:
        root = ET.fromstring(body)
        inner = next((e for e in root.iter() if _local(e.tag) == "Body"), root)
        return "\n".join(pretty_xml(ET.tostring(c, encoding="unicode")) for c in inner) or body
    except Exception:
        return body


def run_soap(cfg, password, plan, dry, log, step0=0, total=None):
    """Login -> every request of the plan, in order -> Logout. An empty plan is just a login test.
    A job with "tolerate" (a delete) may fail - e.g. the record is not there - and the run goes on;
    a job with "stop" (a create) that fails ends the run. step0 = steps already shown before the login."""
    sc = soap_cfg(cfg)
    url, timeout = sc["endpoint"], int(sc["timeout"])
    total = total or len(plan) + 2 + step0
    if dry:
        log(f"[DRY RUN] gateway {url} - nothing is sent")
        for job in plan:
            log(f"[DRY RUN] {job['label']}\n" + pretty_xml(envelope("SESSION_ID", job["ns"], job["body"], job["seq"])))
        return
    n0 = step0 + 1
    log(f"[{n0}/{total}] Log in to the gateway")
    status, body = soap_post(url, login_xml(sc["user"], password), timeout)
    sid = soap_session(body)
    if status != 200 or not sid:
        raise RuntimeError(f"Gateway login failed (HTTP {status}): {soap_fault(body) or short(body, 200)}\n"
                           f"If the password is wrong, use Reset saved passwords and run again.")
    log(f"[{n0}/{total}] ✓ Logged in as {sc['user']}")
    failed, skipped, results = [], [], {}
    try:
        for n, job in enumerate(plan, n0 + 1):
            log(f"[{n}/{total}] {job['label']}")
            try:
                status, body = soap_post(url, envelope(sid, job["ns"], job["body"], job["seq"]), timeout)
            except OSError as e:
                log(f"[{n}/{total}] ✖ {short(e)}")
                results[job["label"]] = "no answer"
                failed.append(f"{job['label']}: {e}")
                break
            fault = soap_fault(body)
            if status == 200 and not fault:
                results[job["label"]] = "HTTP 200 OK"
                log(f"[{n}/{total}] ✓ HTTP 200 OK\n" + pretty_body(body))
            elif job.get("tolerate"):
                results[job["label"]] = f"HTTP {status} refused"
                log(f"[{n}/{total}] ⚠ HTTP {status} - not deleted (it may not exist) {fault}\n" + pretty_body(body))
                skipped.append(f"{job['label']}: HTTP {status} {fault}".strip())
            else:
                results[job["label"]] = f"HTTP {status} FAILED"
                log(f"[{n}/{total}] ✖ HTTP {status} {fault}\n" + pretty_body(body))
                failed.append(f"{job['label']}: HTTP {status} {fault}".strip())
                if job.get("stop"):
                    break
    finally:
        log(f"[{total}/{total}] Log out")
        try:
            soap_post(url, logout_xml(sid), timeout)
            log(f"[{total}/{total}] ✓ Logged out")
        except OSError as e:
            log(f"[{total}/{total}] ✖ logout failed: {short(e)}")
    if len(plan) > 1:
        log("Results (HTTP status of each request):\n" + "\n".join(
            f"  {n}. {j['label']}  ->  {results.get(j['label'], 'not run')}" for n, j in enumerate(plan, 1)))
    if skipped:
        log(f"⚠ {len(skipped)} delete(s) were refused by the gateway (usually: nothing there to delete):\n"
            + "\n".join("    " + s for s in skipped))
    if failed:
        raise RuntimeError(f"{len(failed)} request(s) failed:\n" + "\n".join(failed))


# ----------------------------------------------------------------------------- re-provision (delete, then create)
def reprov_plan(v, five_g, sc):
    """The NOC procedure: delete ENUM, MTAS, IMS, HSS (each one's second file first), then create
    HSS, IMS, MTAS, ENUM. The Common MSISDN of the creates is MSISDN 1. HSS profiles follow the CRM's 5G box."""
    def part(service, action, **keep):
        w = dict(v, common=v["msisdn1"])
        for k in ("msisdn1", "msisdn2", "imsi1", "imsi2"):
            w[k] = v[k] if k in keep.get("only", (k,)) else ""
        return build_plan(service, action, w, "5G with VoLTE" if five_g else "VoLTE", sc)
    plan = (part("enum", "Delete", only=("msisdn1",))
            + part("mmtel", "Delete", only=("msisdn2",)) + part("mmtel", "Delete", only=("msisdn1",))
            + part("ims", "Delete", only=("msisdn2",)) + part("ims", "Delete", only=("msisdn1",))
            # the project's "DELETE IMS 2" step deletes IMSI 1 and "DELETE IMS 1" deletes IMSI 2; the NOC runs 2 then 1
            + part("eps", "Delete", only=("imsi1",)) + part("eps", "Delete", only=("imsi2",)))
    for job in plan:
        job["tolerate"] = True
    creates = (part("eps", "Create", only=("imsi1",)) + part("eps", "Create", only=("imsi2",))
               + part("ims", "Create") + part("mmtel", "Create", only=("msisdn1",))
               + part("enum", "Create", only=("msisdn1",)))
    for job in creates:
        job["stop"] = True
    return plan + creates


READ_5G_JS = """() => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const labels = [...document.querySelectorAll('div,span,td,th,label,dt,b,strong')]
      .filter(e => vis(e) && e.children.length === 0 && /^\\s*5G\\s*:?\\s*$/i.test(e.textContent));
  for (const l of labels) {
    let node = l;
    for (let i = 0; i < 4 && node; i++, node = node.parentElement) {
      for (let sib = node.nextElementSibling, k = 0; sib && k < 2; sib = sib.nextElementSibling, k++) {
        const box = sib.matches('input[type=checkbox]') ? sib : sib.querySelector('input[type=checkbox]');
        if (box) return box.checked;
      }
    }
  }
  return null;
}"""


def read_5g(crm, local, log):
    """True / False for the '5G:' box on the subscriber page (X = set), None when it cannot be told."""
    crm.open_subscriber(local)
    crm.page.wait_for_timeout(1000)
    return crm.page.evaluate(READ_5G_JS)


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
        # fixed height + scrolling, so a long list (re-provision has 15 steps) never pushes the log off the window
        self.body = ctk.CTkScrollableFrame(self, fg_color="transparent", height=300)
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
            dot = ctk.CTkLabel(row, text=str(n), width=30, height=30, corner_radius=15, fg_color=C["idle"],
                               text_color=C["text"], font=font(11, "bold"))
            dot.pack(side="left", padx=(4, 10))
            dot.pack_propagate(False)
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
        try:   # keep the running step in view
            self.body._parent_canvas.yview_moveto(max(0, i - 2) / max(1, len(self.rows)))
        except Exception:
            pass

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

    def set_state(self, i, state, detail=None):
        if 0 <= i < len(self.rows):
            self._paint(i, state)
            if state == "run":
                self.current = i
            if detail is not None:
                self.detail(i, detail)

    def set_title(self, text):
        self.title.configure(text=text)

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
        elif line.startswith("CRM: checking the EDA SIM"):
            s.detail(1, s.rows[1]["detail"].cget("text") + "   checking the EDA SIM…")
        elif line.startswith("CRM: EDA SIM is OK to use"):
            s.detail(1, s.rows[1]["detail"].cget("text").replace("checking the EDA SIM…", "EDA SIM OK"))
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


class SoapTracker:
    """Steps come straight from the run's '[n/total] ...' log lines."""

    def __init__(self, steps, cfg):
        self.s = steps

    def feed(self, line):
        s = self.s
        if "[DRY RUN]" in line:
            return
        m = re.match(r"\[(\d+)/\d+\]\s*(✓|✖|⚠)?\s*(.*)", line)
        if m:
            i, mark, text = int(m.group(1)) - 1, m.group(2), m.group(3)
            if mark in ("✓", "⚠"):
                s.done(i, text)
            elif mark == "✖":
                s.set_state(i, "fail", text)
            else:
                s.start(i)
        elif line.startswith("ERROR") and s.current is not None and s.rows[s.current]["state"] == "run":
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

        self.soap_labels = ["Log in to the gateway", "Send the request", "Log out"]
        self.reprov_labels = ["Check 5G in the CRM", "Log in to the gateway", "Delete, then create", "Log out"]
        self.title("NOC Tools v2")
        ico = icon_path("noc_tools.ico")
        if ico:  # customtkinter sets its own icon after start, so set ours a moment later
            self.after(300, lambda: self.iconbitmap(ico))
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

        self.pages = {"33": self._page33(), "q": self._pageq(), "13": self._page13(),
                      "reprov": self._pagereprov(), "soap": self._pagesoap(), "hc": self._pagehc()}
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
        ctk.CTkLabel(side, text="SIM fixes + provisioning", font=font(12), text_color=C["muted"]).pack(
            anchor="w", padx=22, pady=(0, 26))
        self.nav = {}
        for key, text in (("33", "Code 33"), ("q", "Code 33 queue"), ("13", "Code 13"),
                          ("reprov", "Re-provision"), ("soap", "Provisioning (SOAP)"), ("hc", "Health check")):
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
        self.crm_user_label = ctk.CTkLabel(bottom, text="", font=font(11), text_color=C["muted"], anchor="w",
                                           justify="left")
        self.crm_user_label.pack(fill="x", padx=8, pady=(0, 6))
        self.refresh_crm_user()
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

    def _pageq(self):
        card = self._card()
        self._header(card, "Code 33 queue",
                     "Paste several Teams messages. Each one is checked, then they run one after another. "
                     "If one fails, the queue stops so you can look at it.")
        self.qtext = ctk.CTkTextbox(card, height=110, font=font(12, family=MONO), fg_color=C["field"],
                                    border_color=C["line"], border_width=1, text_color=C["text"])
        self.qtext.pack(fill="x", padx=22, pady=(0, 8))
        self.qtext.bind("<KeyRelease>", lambda e: self.read_queue())
        self.qtext.bind("<<Paste>>", lambda e: self.after(50, self.read_queue))
        self.qlist = ctk.CTkScrollableFrame(card, height=150, fg_color=C["bg"], corner_radius=8)
        self.qlist.pack(fill="x", padx=22)
        self.qitems, self.qrows = [], []
        self.qempty = ctk.CTkLabel(self.qlist, text="Requests appear here after you paste the messages.",
                                   font=font(12), text_color=C["muted"])
        self.qempty.pack(pady=18)
        self.btnq = self._buttons(card, [("Run all", self.runq, "primary"),
                                         ("Stop after this one", self.stopq, "secondary"),
                                         ("Clear", self.clearq, "secondary")])
        self.stop_flag = threading.Event()
        return card

    def _pagereprov(self):
        card = self._card()
        self._header(card, "Re-provision",
                     "Deletes the subscriber from ENUM, MTAS, IMS and HSS, then creates it again in HSS, IMS, MTAS "
                     "and ENUM. The HSS profiles depend on the 5G box in the CRM (X set = 5G profiles).")
        ctk.CTkLabel(card, text="Teams message (optional)", font=font(12), text_color=C["muted"], anchor="w").pack(
            fill="x", padx=22)
        self.remsg = ctk.CTkTextbox(card, height=80, font=font(13, family=MONO), fg_color=C["field"],
                                    border_color=C["line"], border_width=1, text_color=C["text"])
        self.remsg.pack(fill="x", padx=22, pady=(4, 6))
        self.remsg.bind("<KeyRelease>", lambda e: self.parse_reprov())
        self.remsg.bind("<<Paste>>", lambda e: self.after(50, self.parse_reprov))
        grid = ctk.CTkFrame(card, fg_color="transparent")
        grid.pack(fill="x", padx=22, pady=(4, 0))
        self.vre = {k: ctk.StringVar() for k in ("msisdn1", "imsi1", "msisdn2", "imsi2")}
        self._entry(grid, "MSISDN 1", self.vre["msisdn1"], 0, 0)
        self._entry(grid, "IMSI 1", self.vre["imsi1"], 0, 1)
        self._entry(grid, "MSISDN 2", self.vre["msisdn2"], 1, 0)
        self._entry(grid, "IMSI 2", self.vre["imsi2"], 1, 1)
        ctk.CTkLabel(card, text="The CRM is searched with MSISDN 1. The Common MSISDN of the creates is MSISDN 1.",
                     font=font(11), text_color=C["muted"], anchor="w", justify="left", wraplength=560).pack(
            fill="x", padx=22, pady=(10, 0))
        self.btnre = self._buttons(card, [("Run re-provision", self.runreprov, "primary"),
                                          ("Show the order", self.previewreprov, "secondary")])
        return card

    def _pagesoap(self):
        card = self._card()
        self._header(card, "Provisioning (SOAP)",
                     "The Wecom SoapUI project as a form: logs in to the CAI3G gateway, runs Get / Create / Delete "
                     "for the chosen service, then logs out. Create and Delete change the network - use Dry run "
                     "to see the requests first.")
        self.soap_service = ctk.StringVar(value="HSS / EPS")
        self.soap_action = ctk.StringVar(value="Get")
        self.soap_eps = ctk.StringVar(value="VoLTE")
        pick = ctk.CTkFrame(card, fg_color="transparent")
        pick.pack(fill="x", padx=22)
        for col, (label, var, values) in enumerate((("Service", self.soap_service, list(SOAP_SERVICES)),
                                                    ("Action", self.soap_action, list(SOAP_ACTIONS)),
                                                    ("EPS type (EPS Create only)", self.soap_eps, list(EPS_TYPES)))):
            ctk.CTkLabel(pick, text=label, font=font(12), text_color=C["muted"], anchor="w").grid(
                row=0, column=col, sticky="w", padx=(0, 16), pady=(8, 2))
            ctk.CTkOptionMenu(pick, variable=var, values=values, width=170, height=36, font=font(13),
                              fg_color=C["field"], button_color=C["line"], button_hover_color=C["primary"],
                              text_color=C["text"], command=lambda _=None: self.soap_hint()).grid(
                row=1, column=col, sticky="w", padx=(0, 16))
        grid = ctk.CTkFrame(card, fg_color="transparent")
        grid.pack(fill="x", padx=22, pady=(6, 0))
        self.vsoap = {k: ctk.StringVar() for k in ("msisdn1", "imsi1", "msisdn2", "imsi2", "common")}
        self._entry(grid, "MSISDN 1", self.vsoap["msisdn1"], 0, 0)
        self._entry(grid, "IMSI 1", self.vsoap["imsi1"], 0, 1)
        self._entry(grid, "MSISDN 2", self.vsoap["msisdn2"], 1, 0)
        self._entry(grid, "IMSI 2", self.vsoap["imsi2"], 1, 1)
        self._entry(grid, "Common MSISDN", self.vsoap["common"], 2, 0)
        self.soap_hint_label = ctk.CTkLabel(card, text="", font=font(11), text_color=C["muted"], anchor="w",
                                            justify="left", wraplength=560)
        self.soap_hint_label.pack(fill="x", padx=22, pady=(10, 0))
        self.soap_hint()
        self.btnsoap = self._buttons(card, [("Run", self.runsoap, "primary"),
                                            ("Show requests", self.previewsoap, "secondary"),
                                            ("Test login", lambda: self.runsoap(login_only=True), "secondary")])
        return card

    def _pagehc(self):
        card = self._card()
        self._header(card, "Health check",
                     "Checks that the CRM opens and logs in, SSH connects and telnet logs in. Nothing is changed. "
                     "Run it at the start of a shift, or when something suddenly stops working.")
        self.hc_summary = ctk.CTkLabel(card, text="Not checked yet.", font=font(15), text_color=C["muted"],
                                       anchor="w", justify="left", wraplength=560)
        self.hc_summary.pack(fill="x", padx=22, pady=(4, 0))
        self.btnhc = self._buttons(card, [("Run health check", self.run_health, "primary")])
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
        self.reset_steps()

    def reset_steps(self):
        labels = {"33": Code33Tracker.LABELS, "q": Code33Tracker.LABELS, "13": Code13Tracker.LABELS,
                  "reprov": self.reprov_labels, "soap": self.soap_labels, "hc": HEALTH_LABELS}[self.page]
        self.steps.set_title("Checks" if self.page == "hc" else "Steps")
        self.steps.set_steps(labels)

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
                elif kind == "qitem":
                    self.paint_qrow(*payload)
                elif kind == "hc":
                    self.steps.set_state(*payload)
                elif kind == "next33":
                    self.steps.set_steps(Code33Tracker.LABELS)
                    self.steps.set_title(payload)
                    self.tracker = Code33Tracker(self.steps, self.cfg)
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
        return self.btn33 + self.btn13 + self.btnre + self.btnsoap + self.btnhc + [self.btnq[0], self.btnq[2]] + list(self.nav.values())

    def start_worker(self, action, tracker, target, *args):
        self.busy, self.action = True, action
        for b in self.all_buttons():
            b.configure(state="disabled")
        self.reset_steps()
        self.tracker = tracker
        name = datetime.datetime.now().strftime(f"noc_{self.page}_%Y%m%d_%H%M%S.log")
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
        code = {"33": "Code 33", "q": "Code 33 queue", "13": "Code 13", "reprov": "Re-provision", "soap": "Provisioning",
                "hc": "Health check"}[self.page]
        if self.action == "queue":
            done, failed, left = self.qresult
            good = failed == 0 and left == 0
            text = f"{done} done" + (f", {failed} failed" if failed else "") + (f", {left} not run" if left else "")
            self.steps.set_title("Steps")
            self.steps.result(good, ("Dry run: " if self.dry.get() else "") + text + ".")
            (messagebox.showinfo if good else messagebox.showwarning)(
                APP, f"Code 33 queue finished\n\n{text}.\n\nThe list shows the result of each request.",
                parent=self)
            return
        if self.action == "health":
            bad = [HEALTH_LABELS[i] for i, r in enumerate(self.steps.rows) if r["state"] != "ok"]
            if bad:
                self.hc_summary.configure(text="Problem: " + ", ".join(bad) + ". The red line says what failed.",
                                          text_color=C["fail"])
                self.steps.result(False, f"{len(bad)} of 3 checks failed.")
            else:
                self.hc_summary.configure(text=f"All good at {datetime.datetime.now():%H:%M}.", text_color=C["ok"])
                self.steps.result(True, "All 3 checks passed.")
            return
        if ok and err == "cancelled":
            self.steps.result(True, "Stopped. Nothing was changed.")
        elif not ok:
            untouched = self.page == "13" and self.steps.current is not None and self.steps.current <= 1
            restored = "was put back on the line" in str(err)
            self.steps.result(False, "Stopped before changing anything. Nothing was changed." if untouched else
                              "Stopped in the middle - the correct SIM was put back. The line is as before."
                              if restored else
                              f"{code} stopped at the red step. The log says what to finish by hand.")
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
        elif self.action == "runsoap":
            self.steps.result(True, "Done. The log shows the gateway's answer.")
        elif self.action == "runreprov":
            self.steps.result(True, "Re-provisioned. Check the log for any delete the gateway refused (⚠).")
            messagebox.showinfo(APP, "Re-provision finished\n\nAll creates were accepted by the gateway.\n"
                                     "Deletes marked ⚠ in the log had nothing to delete.", parent=self)
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
                "crm": ("crm:" + self.cfg["crm"].get("login_user", ""), "CRM"),
                "soap": (f"soap:{soap_cfg(self.cfg)['user']}@{soap_cfg(self.cfg)['endpoint']}",
                         f"SOAP gateway {soap_cfg(self.cfg)['user']}")}

    def crm_login(self):
        """CRM username + password: asked once per Windows user, then remembered."""
        user = keyring.get_password(APP, CRM_USER_KEY) or ""
        pw = keyring.get_password(APP, crm_pw_key(user)) if user else None
        if not user or not pw:
            user = simpledialog.askstring(APP, "CRM username\n(asked once, then remembered on this PC):",
                                          initialvalue=user, parent=self)
            if not user:
                raise Cancelled()
            user = user.strip()
            pw = simpledialog.askstring(APP, f"CRM password for {user}\n(saved in Windows Credential Manager):",
                                        show="*", parent=self)
            if not pw:
                raise Cancelled()
            keyring.set_password(APP, CRM_USER_KEY, user)
            keyring.set_password(APP, crm_pw_key(user), pw)
            self.refresh_crm_user()
        CRM_CREDS.clear()
        CRM_CREDS.update(user=user, password=pw)

    def refresh_crm_user(self):
        user = keyring.get_password(APP, CRM_USER_KEY)
        self.crm_user_label.configure(text=f"CRM login: {user}" if user else "CRM login: not saved yet")

    def secrets(self, need_mml):
        keys, out = self.secret_keys(), {}
        if self.cfg["crm"].get("remember_login", "yes").lower() in ("yes", "true", "1"):
            self.crm_login()
        else:
            CRM_CREDS.clear()
        if need_mml:
            out["ssh"] = self.secret(*keys["ssh"])
            out["telnet"] = self.secret(*keys["telnet"])
        return out

    def reset_passwords(self):
        keys = [k for k, _ in self.secret_keys().values()]
        user = keyring.get_password(APP, CRM_USER_KEY)
        if user:
            keys += [crm_pw_key(user), CRM_USER_KEY]
        for key in keys:
            try:
                keyring.delete_password(APP, key)
            except keyring.errors.PasswordDeleteError:
                pass
        CRM_CREDS.clear()
        self.refresh_crm_user()
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

    # --- code 33 queue
    def read_queue(self):
        items = split_requests(self.qtext.get("1.0", "end"))
        seen = set()
        for v in items:
            errs = validate(v)
            v["_state"], v["_detail"] = ("ok_to_run", "Waiting") if not errs else ("invalid", errs[0])
            if not errs and v["msisdn"] in seen:
                v["_state"], v["_detail"] = "invalid", "Same number twice, skipped"
            seen.add(v["msisdn"])
        self.qitems = items
        for w in self.qlist.winfo_children():
            w.destroy()
        self.qrows = []
        if not items:
            ctk.CTkLabel(self.qlist, text="Requests appear here after you paste the messages.", font=font(12),
                         text_color=C["muted"]).pack(pady=18)
            return
        for i, v in enumerate(items):
            row = ctk.CTkFrame(self.qlist, fg_color="transparent")
            row.pack(fill="x", pady=2)
            ctk.CTkLabel(row, text=str(i + 1), width=26, font=font(12, "bold"), text_color=C["muted"]).pack(side="left")
            num = to_local(v["msisdn"]) if len(v["msisdn"]) == 12 else (v["msisdn"] or "no MSISDN")
            ctk.CTkLabel(row, text=num, width=120, anchor="w", font=font(13, family=MONO),
                         text_color=C["text"]).pack(side="left", padx=(6, 8))
            ctk.CTkLabel(row, text=v["imsi1"] or "-", width=170, anchor="w", font=font(12, family=MONO),
                         text_color=C["muted"]).pack(side="left")
            st = ctk.CTkLabel(row, text="", anchor="w", font=font(12), justify="left")
            st.pack(side="left", fill="x", expand=True, padx=(8, 0))
            self.qrows.append(st)
            self.paint_qrow(i, "idle" if v["_state"] == "ok_to_run" else "invalid", v["_detail"])

    def paint_qrow(self, i, state, detail):
        if 0 <= i < len(self.qrows):
            color = {"idle": C["muted"], "run": C["run"], "ok": C["ok"], "fail": C["fail"],
                     "invalid": C["fail"], "skip": C["muted"]}[state]
            mark = {"ok": "✓ ", "fail": "✕ ", "invalid": "✕ ", "run": "● "}.get(state, "")
            self.qrows[i].configure(text=mark + detail, text_color=color)

    def clearq(self):
        self.qtext.delete("1.0", "end")
        self.read_queue()

    def stopq(self):
        if self.busy and self.action == "queue":
            self.stop_flag.set()
            self.log("Queue: will stop after the current request.")

    def runq(self):
        if self.busy:
            return
        self.read_queue()
        todo = [i for i, v in enumerate(self.qitems) if v["_state"] == "ok_to_run"]
        if not todo:
            messagebox.showerror(APP, "No valid requests to run. Paste the Teams messages first.", parent=self)
            return
        nums = "\n".join(f"  {to_local(self.qitems[i]['msisdn'])}" for i in todo)
        if not self.dry.get() and not messagebox.askyesno(
                APP, f"Run code 33 for {len(todo)} request(s), one after another?\n\n{nums}", parent=self):
            return
        try:
            secrets = {} if self.dry.get() else self.secrets(need_mml=True)
        except Cancelled:
            return
        self.stop_flag.clear()
        self.qresult = (0, 0, len(todo))
        self.start_worker("queue", None, self.work_queue, todo, secrets, self.dry.get())

    def work_queue(self, todo, secrets, dry):
        done = failed = 0
        for pos, i in enumerate(todo):
            v = self.qitems[i]
            if self.stop_flag.is_set() or failed:
                for j in todo[pos:]:
                    self.q.put(("qitem", (j, "skip", "Not run")))
                break
            num = to_local(v["msisdn"])
            self.q.put(("qitem", (i, "run", "Running")))
            self.q.put(("next33", f"Request {pos + 1} of {len(todo)}: {num}"))
            self.log(f"===== Request {pos + 1}/{len(todo)}: {num} =====")
            try:
                run_flow(self.cfg, v, secrets, self.log, dry)
                done += 1
                self.q.put(("qitem", (i, "ok", "Dry run" if dry else "Done")))
            except Exception as e:
                failed += 1
                self.log("ERROR: " + str(e))
                self.log(traceback.format_exc())
                self.q.put(("qitem", (i, "fail", short(e, 70))))
        left = len(todo) - done - failed
        self.qresult = (done, failed, left)

    # --- re-provision (delete, then create)
    def parse_reprov(self):
        found = parse_message(self.remsg.get("1.0", "end"))
        for key, var in (("msisdn", "msisdn1"), ("imsi1", "imsi1"), ("msisdn2", "msisdn2"), ("imsi2", "imsi2")):
            if found.get(key):
                self.vre[var].set(found[key])

    def values_reprov(self):
        v = {k: re.sub(r"\D", "", var.get()) for k, var in self.vre.items()}
        errs = validate({"msisdn": v["msisdn1"], "msisdn2": v["msisdn2"], "imsi1": v["imsi1"], "imsi2": v["imsi2"]})
        if errs:
            messagebox.showerror(APP, "\n".join(errs), parent=self)
            return None
        return v

    def previewreprov(self):
        v = self.values_reprov()
        if v:
            self.log(f"CRM: open {to_local(v['msisdn1'])} and read the 5G box (X = 5G profiles, else VoLTE profiles)")
            for n, job in enumerate(reprov_plan(v, False, soap_cfg(self.cfg)), 1):
                self.log(f"  {n}. {job['label']}")

    def runreprov(self):
        v = self.values_reprov()
        if not v or self.busy:
            return
        dry = self.dry.get()
        if not dry and not messagebox.askyesno(
                APP, f"Re-provision {to_local(v['msisdn1'])} on the LIVE gateway?\n\n"
                     f"1. Delete ENUM, MTAS, IMS, HSS\n2. Create HSS, IMS, MTAS, ENUM\n\n"
                     f"The CRM is checked first; you confirm its 5G answer before anything is deleted.", parent=self):
            return
        try:
            self.secrets(need_mml=False)   # CRM login (the 5G box is read in the CRM, also in a dry run)
            password = None if dry else self.secret(*self.secret_keys()["soap"])
        except Cancelled:
            return
        plan = reprov_plan(v, False, soap_cfg(self.cfg))
        self.reprov_labels = ["Check 5G in the CRM", "Log in to the gateway"] + [j["label"] for j in plan] + ["Log out"]
        self.start_worker("runreprov", SoapTracker(self.steps, self.cfg), self.work_reprov, v, password, dry)

    def work_reprov(self, v, password, dry):
        local, log = to_local(v["msisdn1"]), self.log
        total = len(reprov_plan(v, False, soap_cfg(self.cfg))) + 3
        log(f"[1/{total}] Check the 5G box in the CRM ({local})")
        crm = CRM(self.cfg, log)
        try:
            crm.start()
            crm.ensure_login(None)
            five_g = read_5g(crm, local, log)
        finally:
            try:
                crm.stop()
            except Exception:
                pass
        if five_g is None:
            five_g = self.ask(f"Could not read the 5G box for {local} from the CRM page.\n\n"
                              f"Is the 5G box set (an X)?   Yes = 5G profiles, No = VoLTE profiles.")
        kind = "5G with VoLTE" if five_g else "VoLTE"
        log(f"[1/{total}] ✓ 5G is {'SET' if five_g else 'not set'} -> {kind} profiles: "
            + " / ".join(EPS_TYPES[kind]))
        if not dry and not self.ask(f"CRM: 5G is {'SET (X)' if five_g else 'NOT set'} for {local}.\n\n"
                                    f"HSS will be created with {' and '.join(EPS_TYPES[kind])}.\n\n"
                                    f"Start deleting and re-creating now?"):
            raise Cancelled()
        run_soap(self.cfg, password, reprov_plan(v, five_g, soap_cfg(self.cfg)), dry, log, step0=1, total=total)

    # --- provisioning (SOAP)
    def soap_hint(self):
        service, action = SOAP_SERVICES[self.soap_service.get()], self.soap_action.get()
        self.soap_hint_label.configure(text=f"{action} needs: {soap_needs(service, action)[1]}")

    def soap_plan(self):
        service, action = SOAP_SERVICES[self.soap_service.get()], self.soap_action.get()
        v = {k: re.sub(r"\D", "", var.get()) for k, var in self.vsoap.items()}
        errs = validate_soap(service, action, v)
        if errs:
            messagebox.showerror(APP, "\n".join(errs), parent=self)
            return None
        return build_plan(service, action, v, self.soap_eps.get(), soap_cfg(self.cfg))

    def previewsoap(self):
        plan = self.soap_plan()
        if plan is None:
            return
        self.log(f"Gateway: {soap_cfg(self.cfg)['endpoint']}  (user {soap_cfg(self.cfg)['user']})")
        for job in plan:
            self.log(f"--- {job['label']}\n" + pretty_xml(envelope("SESSION_ID", job["ns"], job["body"], job["seq"])))

    def runsoap(self, login_only=False):
        if self.busy:
            return
        plan = [] if login_only else self.soap_plan()
        if plan is None:
            return
        dry = self.dry.get() and not login_only
        action = self.soap_action.get()
        if not dry and not login_only and action != "Get" and not messagebox.askyesno(
                APP, f"{action} on the live gateway?\n\n" + "\n".join("  " + j["label"] for j in plan)
                     + f"\n\n{soap_cfg(self.cfg)['endpoint']}", parent=self):
            return
        try:
            password = None if dry else self.secret(*self.secret_keys()["soap"])
        except Cancelled:
            return
        self.soap_labels = ["Log in to the gateway"] + [j["label"] for j in plan] + ["Log out"]
        self.start_worker("testsoap" if login_only else "runsoap", SoapTracker(self.steps, self.cfg), run_soap,
                          self.cfg, password, plan, dry, self.log)

    # --- health check
    def run_health(self):
        if self.busy:
            return
        try:
            secrets = self.secrets(need_mml=True)
        except Cancelled:
            return
        self.hc_summary.configure(text="Checking…", text_color=C["run"])
        report = lambda i, state, detail: self.q.put(("hc", (i, state, detail)))
        self.start_worker("health", None, health_check, self.cfg, secrets, report, self.log)

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
        if eda.startswith("42501") and not messagebox.askyesno(
                APP, f"{eda} starts with 42501, which looks like an IMSI2.\n\n"
                     f"Code 13 needs the main IMSI from EDA (usually 42509…).\n\nUse {eda} anyway?", parent=self):
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
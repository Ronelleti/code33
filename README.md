# Code 33 helper

1. Copy `config.example.ini` to `config.ini` and fill in the CRM url, hosts, telnet user and selectors (`config.ini` is git-ignored).
2. Test from source: `pip install -r requirements.txt` then `python code33_app.py`
   - Tick **Dry run** first, then use **Test CRM (no changes)** until it finds the IMSI row.
   - If a CRM step fails, run `playwright codegen <crm-url>`, do the steps by hand, and copy the selectors it records into config.ini / `CRM.set_status()`.
3. Build: run `build.bat` -> copy `dist\Code33.exe` + `dist\config.ini` to each PC.

Each Windows user is asked for the SSH / telnet passwords once; they are saved in Windows Credential Manager.
CRM login is remembered in `%LOCALAPPDATA%\Code33\browser-profile`, logs go to `%LOCALAPPDATA%\Code33\logs`.
If the status can't be restored, the log says so in capital letters - fix it manually in the CRM.

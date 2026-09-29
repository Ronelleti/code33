@echo off
python -m pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --onefile --windowed --name Code33 --hidden-import keyring.backends.Windows code33_app.py
pyinstaller --noconfirm --onefile --windowed --name Code13 --hidden-import keyring.backends.Windows code13_app.py
pyinstaller --noconfirm --onefile --windowed --name NOCTools --hidden-import keyring.backends.Windows --collect-all customtkinter noc_tools_app.py
if exist config.ini (copy /Y config.ini dist\config.ini) else (copy /Y config.example.ini dist\config.ini)
if exist config13.ini (copy /Y config13.ini dist\config13.ini) else (copy /Y config13.example.ini dist\config13.ini)
if exist noc_tools.ini (copy /Y noc_tools.ini dist\noc_tools.ini) else (copy /Y noc_tools.example.ini dist\noc_tools.ini)
echo.
echo Done: Code33.exe + config.ini, Code13.exe + config13.ini, NOCTools.exe + noc_tools.ini
pause
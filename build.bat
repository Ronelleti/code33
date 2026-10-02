@echo off
rem Builds Code13.exe and NOCTools.exe (with their icons) into dist\
python -m pip install -r requirements.txt pyinstaller

pyinstaller --noconfirm --onefile --windowed --name Code13 ^
  --icon icons\code13.ico --add-data "icons\code13.ico;icons" ^
  --hidden-import keyring.backends.Windows code13_app.py

pyinstaller --noconfirm --onefile --windowed --name NOCTools ^
  --icon icons\noc_tools.ico --add-data "icons\noc_tools.ico;icons" ^
  --hidden-import keyring.backends.Windows --collect-all customtkinter noc_tools_app.py

if exist config13.ini   (copy /Y config13.ini   dist\config13.ini)
if exist noc_tools.ini  (copy /Y noc_tools.ini  dist\noc_tools.ini)
echo.
echo Done: dist\Code13.exe + config13.ini   and   dist\NOCTools.exe + noc_tools.ini
pause
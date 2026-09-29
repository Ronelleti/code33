@echo off
python -m pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --onefile --windowed --name Code33 ^
  --hidden-import keyring.backends.Windows ^
  code33_app.py
if exist config.ini (copy /Y config.ini dist\config.ini) else (copy /Y config.example.ini dist\config.ini)
echo.
echo Done: dist\Code33.exe + dist\config.ini  (copy both to each PC)
pause

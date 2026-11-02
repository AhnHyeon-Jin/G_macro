@echo off
chcp 65001 > nul
cd /d "%~dp0"
pip install -r requirements.txt pyinstaller
rem G_macro.exe: 설정 창(GUI), 실행 시 관리자 권한 요청 (DD 입력용)
pyinstaller --onefile --windowed --uac-admin --name G_macro gui.py
rem G_macro_cli.exe: 콘솔 모드 (config.json 그대로 실행)
pyinstaller --onefile --name G_macro_cli macro.py
copy /Y config.json dist\
xcopy /E /I /Y images dist\images
if exist dd xcopy /E /I /Y dd dist\dd
echo.
echo dist 폴더를 통째로 다른 PC에 복사하면 Python 없이 실행됩니다.
pause

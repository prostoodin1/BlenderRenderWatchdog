# Blender Render Watchdog 3.0.4

В этом релизе SSH-подключение сведено к одной готовой ссылке, а назначенная пачка кадров гарантированно рендерится одним непрерывным процессом Blender.

![Blender Render Watchdog 3.0.4 — Main PC](https://github.com/prostoodin1/BlenderRenderWatchdog/releases/download/v3.0.4/BlenderRenderWatchdog-3.0.4-Main-PC.jpg)

![Blender Render Watchdog 3.0.4 — Worker](https://github.com/prostoodin1/BlenderRenderWatchdog/releases/download/v3.0.4/BlenderRenderWatchdog-3.0.4-Worker.jpg)

## Простое подключение

- кнопка **Create ready SSH link** находится в верхней строке Main PC и сразу копирует готовое приглашение;
- ссылка переносит имя и ID группы, адрес, пользователя, порт и отдельный SSH-ключ;
- Worker показывает два отдельных поля: SSH invitation link и BRW/одноразовый LAN-код;
- ручные host, port и identity перенесены в **Advanced**;
- отсутствующий OpenSSH Client можно установить из автоматического запроса;
- приватный ключ после импорта исчезает из поля и хранится отдельным защищённым файлом.

## Непрерывные пачки

- последовательная пачка запускается одной Blender-командой `start/end/animation`;
- при назначении 20 кадров Blender не перезапускается между отдельными кадрами;
- добавлен отдельный регрессионный тест на единый процесс для всей пачки.

## Проверка и размер кода

- **107 автоматических тестов — все пройдены**;
- основной скрипт `blender_render_watchdog.py`: **6 851 строка**;
- runtime-модули Python: **12 443 строки**;
- Android `MainActivity.java`: **879 строк**;
- тесты: **1 587 строк**;
- Windows EXE визуально проверен в режимах Main PC и Worker;
- Windows EXE и Android APK собраны для версии 3.0.4.

SHA-256 `BlenderRenderWatchdog.exe`: `a91bba786a9166e80259c501ef96fa24613123315d89d9dcd3f6a84e48ed7141`

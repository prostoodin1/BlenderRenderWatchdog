# Blender Render Watchdog 2.5.1

Версия 2.5.1 добавляет приватный интернет-рендер через Tailscale, постоянные или одноразовые коды подключения, отдельный сетевой progress bar с ETA и исправление ложного Offline во время долгих кадров.

![Blender Render Watchdog 2.5.1 — рендер](https://github.com/prostoodin1/BlenderRenderWatchdog/releases/download/v2.5.1/BlenderRenderWatchdog-2.5.1-Render.png)

![Blender Render Watchdog 2.5.1 — сеть](https://github.com/prostoodin1/BlenderRenderWatchdog/releases/download/v2.5.1/BlenderRenderWatchdog-2.5.1-Network.png)

## Главное

- **Рендер из любой сети:** режим Internet via Tailscale использует приватный Tailscale-IP главного ПК и не требует открытия портов роутера.
- **Tailscale внутри приложения:** Watchdog определяет установку и состояние клиента, умеет скачать официальный Windows-инсталлятор и запустить вход в браузере.
- **Свой постоянный код:** Keep my code сохраняет пользовательский ключ, фиксированный порт и стабильный адрес; Generate every start создаёт новые реквизиты подключения.
- **Совместимый протокол:** новые интернет-коды используют `BRW3`, а локальные `BRW2` продолжают работать.
- **Прогресс сети:** над устройствами отображаются progress bar, готовые/всего кадров и примерное оставшееся время.
- **Без ложного Offline:** отдельный heartbeat работает во время загрузки проекта, рендера и отправки результата.
- **LAN остаётся:** Local network (LAN) работает без Tailscale для компьютеров в одной сети.

## Как подключить компьютеры

1. Установите Tailscale на каждом render-компьютере кнопкой **Install Tailscale** и завершите вход в браузере.
2. Убедитесь, что устройства находятся в одном tailnet или имеют взаимный доступ.
3. На главном ПК выберите **Internet via Tailscale**, настройте поведение кода и запустите контроллер.
4. Передайте код `BRW3` worker-компьютерам и подключите их во вкладке Network.

Watchdog не хранит логин, пароль или auth key Tailscale. Авторизация выполняется официальным клиентом Tailscale.

## Совместимость

- Windows 10/11 или Windows Server 2016+ для актуального Tailscale-клиента;
- Blender и Watchdog должны быть установлены на каждом render-компьютере;
- до пяти одновременно подключённых render-worker;
- старые локальные коды `BRW2` и постоянные ключи 2.4.1+ сохранены.

## Проверка и размер кода

- 75 автоматических тестов — все пройдены;
- основной скрипт `blender_render_watchdog.py`: **5 066 строк**;
- 16 runtime-модулей Python: **8 893 строки**;
- Android `MainActivity.java`: **879 строк**;
- тесты: **1 029 строк в 15 файлах**;
- Windows EXE собран и визуально проверен на вкладках Render и Network.

SHA-256 `BlenderRenderWatchdog.exe`: `d474efb6a9303dc9575ee92d5f7f91bea7d4f3033d70ce19e379a4a0d4680157`

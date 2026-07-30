# CheevoScope

Дашборд статистики игровой библиотеки: время в играх, редкость и тиры
достижений, стоимость библиотеки, отзывы — из Steam. Вкладка
RetroAchievements (очки, hardcore/softcore, замастеренные игры) и
«Общая статистика», объединяющая обе платформы. Работает и как PWA —
можно поставить на телефон как приложение.

Обновляется кнопкой «Обновить» вручную и само по себе раз в час
(systemd-таймер `cheevoscope-refresh`, quick-режим — список игр и новые
достижения). Цены, отзывы и картинки автопроверка не трогает — это
по-прежнему только «Обновить всё».

## Установка на сервер

```bash
curl -fsSL https://raw.githubusercontent.com/0xERR404/cheevoscope/main/install.sh -o install.sh && chmod +x install.sh && sudo ./install.sh
```

Один скрипт на чистый VPS (root) или уже настроенный сервер: пользователь,
SSH, файрвол, fail2ban, сам дашборд + почасовой таймер, в конце — домен и
HTTPS через Caddy (по желанию).

Нужно:
- `STEAM_API_KEY` — https://steamcommunity.com/dev/apikey
- `STEAM_ID` (SteamID64, 17 цифр) — https://steamid.io/

Опционально (вкладка RetroAchievements):
- `RA_USERNAME` — логин на retroachievements.org
- `RA_API_KEY` — retroachievements.org → Settings → Keys

Без RA дашборд работает, вкладка просто пустая. Добавить позже: дописать
`RA_USERNAME`/`RA_API_KEY` в `.env` на сервере и `sudo systemctl restart cheevoscope`.

Повторный запуск той же командой обновляет код, ничего заново не
переспрашивает. Единственная ручная пауза — после смены SSH-порта: скрипт
попросит зайти новым пользователем в новом окне терминала, прежде чем
заблокировать root.

## Локальный запуск

```bash
git clone https://github.com/0xERR404/cheevoscope.git cheevoscope
cd cheevoscope
mkdir -p static data cache
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # заполните STEAM_API_KEY/STEAM_ID (RA_* — опционально)
uvicorn web:app --reload
```

Открыть http://127.0.0.1:8000, нажать «Обновить».

## Если что-то не так

- **Домен не открывается**: `dig +short ваш-домен.ru` — должен показывать
  IP сервера; проверьте и облачный firewall в панели провайдера, `ufw`
  внутри сервера его не заменяет.
- **SSH не пускает после установки**: `sudo fail2ban-client status sshd`,
  `sudo ufw status verbose`. Если внутри всё чисто, а снаружи порт
  недоступен — проверьте через https://check-host.net/check-tcp,
  возможна фильтрация у провайдера.
- **Меньше игр, чем в клиенте Steam**: обычно штатно (F2P без запуска через
  клиент не видны). Игры, которых Steam не отдаёт вообще — впишите appid в
  `manual_appids.json`.
- **PWA не предлагает установку на телефон**: без HTTPS (домена/Caddy) не
  сработает в принципе — браузер требует безопасный контекст. С доменом
  тоже не всегда с первого захода — у Chrome своя эвристика.
- Логи: `journalctl -u cheevoscope -f`, `journalctl -u cheevoscope-refresh -f`
  (почасовая автопроверка), `journalctl -u caddy -f`, `tail -f logs.txt`.

## Безопасность

- Без домена дашборд слушает `0.0.0.0:8000` без авторизации. Задайте
  `DASHBOARD_LOGIN`/`DASHBOARD_PASSWORD` в `.env` — включит Basic Auth на
  уровне приложения (работает и без Caddy).
- С доменом Caddy добавляет свой Basic Auth поверх — это дополнительный,
  не единственный уровень защиты.
- Не коммитьте `.env` — там `STEAM_API_KEY`. Уже в `.gitignore`.

## Структура

```
install.sh             установочный скрипт
web.py                  FastAPI-приложение
app/                    вся логика (Steam/RA API, кэш, отчёты, почасовой refresh)
templates/index.html    страница дашборда
static/                 favicon, PWA-манифест/иконки, картинки игр
manual_appids.json      appid игр, которых нет в Steam API
.env.example            шаблон конфигурации
```

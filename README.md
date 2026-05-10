# slot-radar

Polls a booking site on a schedule and sends a Telegram alert when an earlier appointment slot becomes available.

## Как это работает

1. GitHub Actions запускает скрипт раз в час (`cron: "0 * * * *"`).
2. Playwright (headless Chromium со stealth-патчем) открывает сайт, заполняет форму данными заявителя и нажимает «Select date».
3. Сайт высылает на Gmail письмо с картинкой арифметической капчи.
4. Скрипт по IMAP вытаскивает картинку из письма, шлёт её в **2captcha** с флагом `math=1`, получает ответ-число.
5. Вводит число обратно на сайт, видит таблицу слотов, парсит первую ячейку (она всегда самая ранняя).
6. Сравнивает с `state.json`. Если новая дата раньше — шлёт сообщение в Telegram и коммитит обновлённый `state.json` обратно в репо.
7. Captcha-письмо удаляется из Gmail сразу после обработки, чтобы не забивать inbox.

## One-time setup

### 1. Сделай репозиторий приватным

В нём окажутся ФИО, паспорт и телефон через secrets. Использовать только приватный репо.

### 2. Gmail App Password

- На [myaccount.google.com](https://myaccount.google.com) включи 2FA (если ещё нет).
- Зайди на [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
- Создай пароль для "Mail" → скопируй 16 символов **без пробелов**.

### 3. Telegram-бот

- Открой [@BotFather](https://t.me/BotFather), команда `/newbot`. Получи токен (`123456:ABC-DEF...`).
- Найди созданного бота, нажми Start.
- Открой `https://api.telegram.org/bot<TOKEN>/getUpdates` в браузере — увидишь `chat.id` (число), это и есть `TELEGRAM_CHAT_ID`.

### 4. 2captcha API key

- Регистрация: [2captcha.com/auth/register](https://2captcha.com/auth/register).
- Минимальный депозит ~$3 (хватит надолго). Принимают карты, крипту, PayPal.
- API key — на главной странице после логина: [2captcha.com/setting](https://2captcha.com/setting).
- Расход ~$0.001 за решение математической капчи × 24/день = ~$0.024/день, $3 хватит на ~4 месяца непрерывной работы.

### 5. Добавь secrets в репозиторий

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Имя | Значение |
| --- | --- |
| `TWOCAPTCHA_API_KEY` | 32 hex символа из [2captcha.com/setting](https://2captcha.com/setting) |
| `GMAIL_USER` | `<your_email>@gmail.com` (тот же, что в форме!) |
| `GMAIL_APP_PASSWORD` | 16-значный пароль приложения |
| `TELEGRAM_BOT_TOKEN` | `123456:ABC-...` |
| `TELEGRAM_CHAT_ID` | число из getUpdates (опционально, если используешь fan-out) |
| `APPLICANT_NAME` | `<First Last>` (как в паспорте) |
| `APPLICANT_DOB` | `<DD/MM/YYYY>` |
| `APPLICANT_PHONE` | `<+phone>` |
| `APPLICANT_EMAIL` | `<your_email>@gmail.com` (как в Gmail) |
| `APPLICANT_PASSPORT` | `<passport_number>` |

### 6. Проверь, что `state.json` инициализирован

В репо должен лежать `state.json` с `"earliest_slot_date": "9999-12-31"` — он уже создан в этом коммите, ничего не делай.

### 7. Запусти первый прогон вручную

GitHub → **Actions → check-slot → Run workflow**. Открой логи, должно быть:

```
Captcha solved: 21
Found earliest slot: Slot(date='2026-06-22', time='10:00', weekday='Monday')
First real check — establishing baseline ...
```

В Telegram придёт первое сообщение «первый зафиксированный слот: 22-06-2026». Гmail-письмо с капчей удалится автоматически.

После этого крон запустится сам в начале каждого часа (плюс случайный джиттер 0–9 минут).

## Локальный запуск (для отладки)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
export TWOCAPTCHA_API_KEY=...
export GMAIL_USER=...
# и так далее (см. таблицу выше)
python -m src.main
```

При ошибке скрипт сохраняет скриншот и HTML страницы в `debug-screenshots/`.

## Что ломается чаще всего

| Симптом | Что делать |
| --- | --- |
| `Captcha field never appeared` | Сайт изменил CSS-селекторы. Открой `debug-screenshots/error-*.html`, посмотри атрибуты поля и поправь `_wait_for_captcha_field` в `src/booking.py`. |
| `No captcha email arrived within 90s` | Проверь `GMAIL_APP_PASSWORD`. Возможно отвалился из-за ротации, перевыпусти. |
| `2captcha returned non-numeric` | Воркер ошибся. Проверь баланс на 2captcha.com. Если регулярно — поменяй `math=1` на ручной промпт через `textinstructions`. |
| `2captcha submit failed: ERROR_KEY_DOES_NOT_EXIST` | Неверный `TWOCAPTCHA_API_KEY`. |
| `2captcha submit failed: ERROR_ZERO_BALANCE` | Закончились деньги, пополни на [2captcha.com/pay](https://2captcha.com/pay). |
| `Could not parse any slot from the page` | Изменилась разметка таблицы слотов. Поправь регекспу в `_read_first_slot`. |
| reCAPTCHA блокирует браузер | В логах будет ошибка ~ "I'm not a robot". Поднять `playwright-stealth` или добавить 2captcha fallback. |

## Где смотреть логи

- **Текущий прогон**: Actions → последний run → check-slot → шаг "Run slot check".
- **История изменений `state.json`**: вкладка Commits — все обновления делает бот `slot-bot`.
- **Скриншоты ошибок**: на упавшем run → Artifacts → `debug-<run-id>.zip`.

## Отключить мониторинг

Repo → Actions → check-slot → ⋯ → **Disable workflow**.

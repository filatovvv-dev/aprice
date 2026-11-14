# АвтоПрайс (APrice) — скелет проекта + APrice-Builder

Windows-приложение для оценки рыночной стоимости автомобиля по объявлениям
с пяти российских площадок. Комплекс состоит из трёх компонентов:

| Компонент | Назначение | Статус |
|---|---|---|
| **APrice-Builder** | Формирователь справочной базы (каталог марки → модели → поколения → КПП/топливо/объёмы/мощности) | ✅ реализован (задачи №1–2, включая доработки по код-ревью) |
| **APrice-Server** | Фоновый сборщик объявлений | 🔒 заглушка (задача №3); `common` подготовлен к переиспользованию |
| **APrice-Client** | GUI оценки | 🔒 заглушка (задача №4) |

**Стек:** Разработка и сборка — Python 3.11.x или 3.12.x (НЕ 3.15), PyQt6, Playwright (Chromium), SQLite, pandas/numpy, PyQt6.QtNetwork (IPC на QTcpServer).
**Целевая ОС:** Windows 10/11 x64 (разработка возможна на Linux).

---

## Отчёт об исследовании источников каталога (п. 4.1 ТЗ)

Проверено 3 ноября 2026 г. (Linux-среда разработки; запросы — прямой HTTPS без прокси).

### 1. Официальный API каталога auto.ru

**Вывод: публичного неавторизованного API каталога не обнаружено.**

* `https://api.auto.ru/...` — хост **не резолвится** (NXDOMAIN);
* `https://auto.ru/cars/all/` (страница каталога) с «голым» curl даже с
  реалистичным User-Agent отвечает **302 → `https://auto.ru/showcaptcha?cc=1&...`**,
  то есть антибот Yandex сразу требует CAPTCHA;
* `https://auto.ru/-/ajax/desktop/listing/` (AJAX выдачи) — тоже **302 на captcha**.

Страницы auto.ru — SPA (Next.js): данные каталога встраиваются в JSON-state
страницы (`__NEXT_DATA__` / инициализационные объекты), но получить их можно
только из «прогретого» браузерного контекста с cookie Yandex (Set-Cookie
`yandexuid` и др.). Это и реализовано как **HTML-фолбэк через Playwright**
(P-01..P-03): stealth-инъекции (`navigator.webdriver`, `plugins`, `languages`),
UA Chrome 124 Win10 + согласованные `sec-ch-ua*`, `ru-RU`, `Europe/Moscow`,
viewport 1920×1080, abort ресурсов `image/media/font/stylesheet`. При редиректе
на `showcaptcha` парсинг прекращается с понятным сообщением в GUI (P-08) —
ручное прохождения CAPTCHA в Builder не предусмотрено ТЗ.

### 2. Альтернативный источник `api.cars-base.ru` — **выбран основным**

Эндпоинты отдают **чистый JSON без браузера**; требуется параметр `token`,
и сервис сам публикует тестовый ключ (`?token=test` — указано в теле ответа 401).
Токен вынесен в `settings.json` (`builder.cars_base_token`) — при переходе на
постоянный ключ меняется только конфиг, не код (задача №2, п. 6).

| Эндпоинт | Ответ |
|---|---|
| `GET /full?token=test` | `{data: [марки], meta}` — 428 марок; марка: `id, name, cyrillic_name, numeric_id, year_from/to, popular, country, models[]`; модель: `id, mark_id, name, year_from/to, class` |
| `GET /generations?token=test` | Все поколения одним запросом: `id, model_id, mark_id, name («III», …), year_from, year_to` |
| `GET /configurations?token=test` | Кузовные конфигурации: `generation_id, body_type, doors_count` |
| `GET /modifications?model_id=BMW_X5&token=test` | Модификации: `generation_id, name ("35i 3.0 AT (306 л.с.) 4WD"), group_name, offers_price_from/to` |

Пример:

```bash
curl "https://api.cars-base.ru/modifications?model_id=BMW_X5&token=test"
# {"data":[{"id":"10382711_...","generation_id":"10382710","model_id":"BMW_X5",
#   "name":"35i 3.0 AT (306 л.с.) 4WD", ...}, ...]}
```

Данные cars-base.ru — это каталог auto.ru (id моделей совпадают со slug'ами
auto.ru, напр. `BMW_X5` ↔ `auto.ru/cars/bmw/x5/`), поэтому источник
соответствует предметной области ТЗ. Объём KПП/топлива/объёма/мощности
извлекается регулярным разбором имён модификаций (`AT→АКПП`, `MT→МКПП`,
`CVT→Вариатор`, `DSG`, `AMT→Робот`; `DT→Дизель`, `EV→Электро`, `PHEV→Гибрид`;
`3.0` → объём; `(306 л.с.)` → мощность) и агрегируется в диапазонные записи
таблицы `configurations` (min/max по группе КПП+топливо).

**Выбранная стратегия (P-04):** приоритет — API cars-base.ru (быстро, надёжно,
без уязвимости к антиботу); при его недоступности — автоматический переход на
Playwright-парсинг HTML auto.ru.

---

## Структура проекта

```
APrice/
├── builder/                    # APrice-Builder (реализован, задачи №1–2)
│   ├── main.py                 # точка входа: python -m builder.main
│   ├── parser.py               # парсинг каталога (API + Playwright-фолбэк)
│   ├── database.py             # схема SQLite v2 + UPSERT (P-09)
│   ├── migrations.py           # миграция схемы v1 → v2 (UNIQUE в configurations)
│   └── gui.py                  # окно PyQt6, QThread-парсинг, чекбокс лимита марок
├── server/__init__.py          # заглушка (задача №3)
├── client/__init__.py          # заглушка (задача №4)
├── common/                     # config, logger, window_manager, constants
│   └── constants.py            # + константы IPC и Server (подготовка к задаче №3)
├── data/aprice.db              # справочная база (схема v2)
├── data/aprice.db.bak-v1       # резервная копия БД перед миграцией v1→v2
├── data/cache/                 # JSON-кэш HTTP-ответов (P-06)
├── config/settings.json        # конфигурация (создаётся при первом запуске)
├── config/aprice_builder_settings.ini  # позиции окон Builder (QSettings INI)
├── logs/                       # aprice_<component>.log (+ _errors.log) с ротацией
├── tests/                      # pytest: детект CAPTCHA, миграция, конфиг, WM
└── requirements.txt
```

## Установка и запуск

Требования: **Python 3.11+** (Windows 10/11 x64 или Linux для разработки).

```bat
cd APrice
python -m venv .venv
.venv\Scripts\activate          :: Windows (Linux: source .venv/bin/activate)
pip install -r requirements.txt
playwright install chromium     :: браузер для HTML-фолбэка (опционален,
                                 :: если основной API доступен — не используется)
python -m builder.main
```

При первом запуске автоматически создаются `config/settings.json`,
`data/aprice.db` (со схемой v2) и каталог `logs/`. Если существует база старой
схемы v1, при открытии выполняется автоматическая миграция до v2
(`builder/migrations.py`) с обязательной резервной копией
`data/aprice.db.bak-v1` и записью `schema_version = 2` в `metadata`.

**Играет роль:** Chromium нужен Builder'у только если основной API
cars-base.ru окажется недоступен. Перед запуском HTML-фолбэка выполняется
предпроверка наличия браузера; при его отсутствии в GUI появляется дружелюбное
сообщение «Браузер Chromium для Playwright не установлен. Выполните:
`playwright install chromium`» — без трейсбека (задача №2, п. 1.1).

### Конфигурация (`config/settings.json`)

```json
{
  "builder": {
    "request_delay_sec": [1, 3],
    "cache_ttl_hours": 168,
    "source": "auto.ru",
    "limit_brands": 5,
    "cars_base_token": "test",
    "headless": true
  },
  "logging": {
    "level": "INFO",
    "max_file_size_mb": 10,
    "backup_count": 3
  }
}
```

| Ключ | Назначение |
|---|---|
| `builder.request_delay_sec` | Паузы между запросами (P-07) |
| `builder.cache_ttl_hours` | TTL JSON-кэша `data/cache/` (P-06) |
| `builder.limit_brands` | Лимит марок за прогон: `5` — валидация, `null` — полный обход (задача №2, п. 1.4). В GUI соответствует чекбокс **«Ограничить 5 марками (валидация)»** — состояние двусторонне синхронизировано с этим ключом |
| `builder.cars_base_token` | Токен API cars-base.ru (по умолчанию публичный `"test"`; для промышленного объёма — постоянный ключ) |
| `builder.headless` | Режим Chromium для HTML-фолбэка: `false` — видимое окно |
| `logging.*` | Уровень, размер и число резервных файлов логов |

### Работа в окне Builder

* **«Обновить базу»** — парсинг каталога в фоновом потоке (`QThread`), GUI не
  блокируется; прогресс и сообщения парсера — в лог-панели, статус — дата
  последнего обновления и количество марок/моделей/поколений в БД.
* **Чекбокс «Ограничить 5 марками (валидация)»** — снятие включает полный обход
  всех доступных марок; выбор сохраняется в `settings.json`.
* **«Остановить»** — корректное прерывание парсинга (stop-event).
* **«Сохранить лог»** — выгрузка лог-панели в файл.
* Повторный запуск не дублирует записи (UPSERT, P-09), а обновляет `updated_at`;
  неизжившие ответы берутся из кэша.

## Схема БД (`data/aprice.db`, версия 2)

Таблицы `manufacturers`, `models`, `generations`, `configurations`, `metadata`
(см. ТЗ, п. 3). Отличие схемы v2 от v1: в `configurations` добавлено
`UNIQUE (generation_id, transmission, fuel_type)` — бизнес-ключ конфигурации,
благодаря чему дедупликация выполняется честным
`INSERT ... ON CONFLICT ... DO UPDATE` вместо «SELECT перед INSERT».
Миграция существующих баз — `builder/migrations.py::migrate_to_v2`
(идемпотентна, дубликаты схлопываются `INSERT OR IGNORE`, копия — `.bak-v1`).

## Менеджер окон (W-01..W-06) — зафиксирующие решения

* Хранение геометрии: **`QSettings` в формате INI** — файл рядом с
  `.exe`/корнем проекта (W-04). Выбор вместо реестра: portable-совместимость,
  отсутствие UAC, переносимость между ОС при разработке.
* **Отдельный INI-файл на компонент** (задача №2, п. 2.1): параметр
  `WindowManager(settings_filename=...)`; Builder —
  `config/aprice_builder_settings.ini`, Server — `aprice_server_settings.ini`,
  Client — `aprice_client_settings.ini` (константы — в `common/constants.py`).
  Старый общий `aprice_settings.ini` автоматически мигрируется в файл Builder.
* W-01 — первое открытие строго в центр `availableGeometry()` экрана
  (учёт панели задач); W-02 — `pos()/size()` сохраняются в `closeEvent`
  **и** при сигнале `QApplication.aboutToQuit` (реестр всех зарегистрированных
  окон — задача №2, п. 1.5; переживает завершение через `QApplication.quit()`);
  W-03 — `WindowManager.center_error(dialog)` центрирует диалоги ошибок и
  никогда их не сохраняет; W-05 — при восстановлении проверяется пересечение
  с любым экраном, иначе — центрирование; W-06 — единый API
  `apply(window, window_id)` / `center_error(dialog)` для всех компонентов.

## Логирование

* `logs/aprice_<component>.log` с ротацией (`RotatingFileHandler`, параметры из
  `settings.json`) + отдельный error-лог `logs/aprice_<component>_errors.log`
  (ERROR+). `setup_logging(component="server")` даёт
  `aprice_server.log` / `aprice_server_errors.log` — нейтрально к компоненту
  (задача №2, п. 2.2).
* Дублирование в GUI-панель — потокобезопасный `QtLogEmitter` (сигнал → слот).

## SSL/прокси и парсер auto.ru (Server, 3.7)

Server читает секцию `server` из `config/settings.json`:

```json
"server": { "ignore_https_errors": true, "playwright_headless": true }
```

* `ignore_https_errors` (по умолчанию `true`) — Playwright игнорирует ошибки
  сертификатов. Нужно в сетях с MITM-прокси и self-signed сертификатом, иначе
  `net::ERR_CERT_AUTHORITY_INVALID`. Если MITM нет — поставьте `false`.
* `playwright_headless` — `false` покажет окно Chromium (удобно для отладки CAPTCHA).
* Браузер: `playwright install chromium` (в тестах не нужен — они на моках).
* auto.ru без прогретых cookie часто отдаёт CAPTCHA (Yandex SmartCaptcha) —
  это ожидаемое поведение: парсер сообщает о ней (`CaptchaDetectedError`), сервер
  показывает диалог; CAPTCHA не обходится. Разбор выдачи auto.ru реализован по
  публичным примерам и **не проверен в сети** — см. docstring `server/parsers/autoru.py`.
* Площадки: активны `auto.ru`, `drom.ru`, `avito.ru`. `bibika.ru` и `sberauto.com`
  отключены решением Заказчика: в запросе они принимаются, но по ним возвращается
  ошибка `unsupported`, остальные площадки собираются. Если `options.sites` не
  передан, берётся `server.default_sites` из `settings.json`.
* Все три парсера отбрасывают объявления с годом вне `[year_from, year_to]` на
  своей стороне, даже если сайт проигнорировал параметры года в URL.
* Опции заданий: `test_fast_mode` только сокращает паузы, а `use_stub_data` возвращает
  заглушку без сети (для автотестов). Реальный смоук: без `use_stub_data`.

## Подготовка к задаче №3 (Server)

В `common/constants.py` объявлены (без реализации): `IPC_HOST/IPC_PORT/
IPC_TIMEOUT_SEC`, паузы и объёмы сбора `DEFAULT_SERVER_*`, список площадок
`SITES` и флаг `SITE_SBERAUTO_DISABLED_BY_DEFAULT`.

## Тесты

```bat
pip install -r requirements-dev.txt
python -m pytest tests -q
```

Покрытие (задача №2): детект CAPTCHA без ложных срабатываний (JSON со словом
«captcha» парсится; showcaptcha-HTML/редирект/403/429 → `CaptchaDetectedError`;
битый не-JSON → `RuntimeError`), дружелюбное сообщение при отсутствии Chromium,
миграция v1→v2 (в т.ч. идемпотентность и схлопывание дубликатов), UPSERT
конфигураций через `ON CONFLICT`, чтение `limit_brands` из конфига,
реестр окон + сохранение по `aboutToQuit`, независимость INI-файлов компонентов,
логгер для произвольного компонента, константы IPC/Server.

Покрытие 3.1: контракт IPC (round-trip 11 типов, валидация spec/options, коды ошибок), транспорт (port=0, несколько соединений, ErrorMsg без разрыва, idle-таймаут, занятый порт), UTF-8 сквозь TCP.

## Допущения и открытые вопросы

1. **auto.ru без «прогревания» стабильно отдаёт CAPTCHA** — подтверждено
   экспериментально. Для боевого HTML-парсинга может потребоваться headful-
   режим (`builder.headless: false`) и повторные попытки; сейчас фолбэк честно
   сообщает о блокировке (P-08).
2. **`?token=test` cars-base.ru** — публичный тестовый ключ, заявленный самим
   сервисом. Для промышленного объёма (все 428 марок) рекомендуется купить
   постоянный токен и положить его в `settings.json` (`builder.cars_base_token`).
3. Поколения в cars-base.ru не содержат слова «рестайлинг» — имена нормализуются
   как `«IV (2018–2021)»`; решение ТЗ-разработчика — приемлемо для v1,
   обогащение рестайлингами — отдельная задача.
4. Полный обход всех марок включается снятием чекбокса в GUI (или
   `builder.limit_brands: null` в конфиге); по умолчанию — 5 марок для
   валидации, чтобы Заказчик включал полный обход явно.
5. Парсинг выполняется синхронным HTTP (stdlib) для API; Playwright подключается
   лениво — только в HTML-ветке, с предпроверкой установленного браузера.

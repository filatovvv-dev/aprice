# АвтоПрайс (APrice) — скелет проекта + APrice-Builder

Windows-приложение для оценки рыночной стоимости автомобиля по объявлениям
с пяти российских площадок. Комплекс состоит из трёх компонентов:

| Компонент | Назначение | Статус |
|---|---|---|
| **APrice-Builder** | Формирователь справочной базы (каталог марки → модели → поколения → КПП/топливо/объёмы/мощности) | ✅ реализован (задача №1) |
| **APrice-Server** | Фоновый сборщик объявлений | 🔒 заглушка (задача №3) |
| **APrice-Client** | GUI оценки | 🔒 заглушка (задача №4) |

**Стек:** Python 3.11+, PyQt6, Playwright (Chromium), SQLite, pandas/numpy.

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
├── builder/            # APrice-Builder (реализован)
│   ├── main.py         # точка входа: python -m builder.main
│   ├── parser.py       # парсинг каталога (API + Playwright-фолбэк)
│   ├── database.py     # схема SQLite + UPSERT (P-09)
│   └── gui.py          # окно PyQt6, QThread-парсинг
├── server/__init__.py  # заглушка (задача №3)
├── client/__init__.py  # заглушка (задача №4)
├── common/             # config, logger, window_manager, constants
├── data/aprice.db      # создаётся при первом запуске Builder
├── data/cache/         # JSON-кэш HTTP-ответов (P-06)
├── config/settings.json        # создаётся при первом запуске
├── config/aprice_settings.ini  # позиции окон (QSettings INI)
├── logs/               # aprice_builder.log (+errors) с ротацией
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
`data/aprice.db` (со схемой) и каталог `logs/`.

В окне: **«Обновить базу»** — парсинг в фоновом потоке (по умолчанию
обрабатываются 5 популярных марок — валидационный объём этапа 1; полный обход
включается параметром `limit_brands=None` в `ParseWorker` после подтверждения
Заказчиком). **«Остановить»** — корректный прерывание; **«Сохранить лог»** —
выгрузка лог-панели в файл.

## Менеджер окон (W-01..W-06) — зафиксирующие решения

* Хранение геометрии: **`QSettings` в формате INI** — файл
  `config/aprice_settings.ini` рядом с `.exe`/корнем проекта (W-04).
  Выбор вместо реестра: portable-совместимость, отсутствие UAC, переносимость
  между ОС при разработке.
* W-01 — первое открытие строго в центр `availableGeometry()` экрана
  (учёт панели задач); W-02 — `pos()/size()` сохраняются в `closeEvent`;
  W-03 — `WindowManager.center_error(dialog)` центрирует диалоги ошибок и
  никогда их не сохраняет; W-05 — при восстановлении проверяется пересечение
  с любым экраном, иначе — центрирование; W-06 — единый API
  `apply(window, window_id)` / `center_error(dialog)` для всех компонентов.

## Допущения и открытые вопросы

1. **auto.ru без «прогревания» стабильно отдаёт CAPTCHA** — подтверждено
   экспериментально. Для боевого HTML-парсинга может потребоваться headful-
   режим и повторные попытки; сейчас фолбэк честно сообщает о блокировке (P-08).
2. **`?token=test` cars-base.ru** — публичный тестовый ключ, заявленный самим
   сервисом. Для промышленного объёма (все 428 марок) рекомендуется купить
   постоянный токен и положить его в `settings.json` (открытый вопрос к ТЗ).
3. Поколения в cars-base.ru не содержат слова «рестайлинг» — имена нормализуются
   как `«IV (2018–2021)»`; при необходимости обогащения рестайлингами нужен
   дополнительный эндпоинт/источник.
4. Таблица `configurations` не имеет уникального бизнес-ключа в схеме ТЗ —
   дедупликация выполнена по кортежу значений (SELECT перед INSERT).
5. Полный обход всех марок оставлен на следующую итерацию (комментарий
   ТЗ-разработчика); каркас позволяет снять лимит одним параметром.
6. Парсинг выполняется синхронным HTTP (stdlib) для API; Playwright подключается
   лениво — только в HTML-ветке, чтобы Builder не требовал браузер, пока жив API.

# FreeGuard и Analytics: независимая проверка работающего UI

Дата: 2026-10-11, Europe/Moscow. База Git: `fb3a7bb6`, поверх неё — незакоммиченная реализация.
Проверены `FCC_FREEGUARD_ANALYTICS_SPEC.md`, `FCC_AGENT_HANDOFF.md`, код и сервер `http://localhost:8082/admin`.
Существующий `2026-10-11-freeguard-analytics-audit.md` оставлен без изменений.

## Вердикт

Принимать реализацию по представленному отчёту нельзя. Цвета карточек исправлены, но адаптивность сломана, периоды и два основных фильтра не работают, учёт запросов теряет все provider attempts и завершения. Free Models выдаёт разрешение вызова при выключенном Guard за подтверждённую бесплатность. Несколько обязательных разделов UI вообще отсутствуют.

Сервер запущен через `uv run fcc-server` из данного репозитория, слушающий Python PID при проверке — 63672. Полученные по HTTP `analytics.js` и `analytics.css` совпадают с файлами workspace после нормализации CRLF. Поэтому обнаруженные дефекты frontend нельзя объяснить старой установленной копией этих assets.

## Подтверждённые дефекты

### 1. P1: попытки и завершение запросов молча теряются

`api/handlers/messages.py:424` и `api/handlers/responses.py:189` создают callable, возвращающий результат `AnalyticsService.start_request`, то есть `LogicalRequestRecord`.
`application/execution.py:263` ищет `attempt_recorder` на возвращённом объекте. У записи такого метода нет, поэтому выбирается `_NullAttempts`.
`application/execution.py:366` аналогично ищет `complete_request` на записи и молча возвращается.
Начальный статус записи уже `success` (`application/analytics/models.py:157`).

Live API: 8 logical requests, 8 success, 0 downstream attempts. Все восемь записей имеют `ended_at_utc=null`, нет duration и final provider/model. Trace сообщает об отсутствии записанных попыток. Эти записи нельзя считать доказанными успешными завершениями.

Сервер продолжал получать трафик во время проверки: на более позднем сохранённом скриншоте уже 22 logical requests / 22 success, по-прежнему 0 attempts. Число 8 выше относится к первому HTTP-снимку, не к финальному screenshot.

Изолированное воспроизведение через настоящие `AnalyticsService`, `_messages_analytics_sink` и статические методы executor, без БД и provider calls:

```text
record_type: LogicalRequestRecord
attempt_sink: _NullAttempts
status_after_error_completion: success
ended_at: None
finalized: False
```

Нужен согласованный typed contract между сервисом, request record и executor, плюс mock E2E через настоящий API handler: успешный запрос, error, guard block, cancel, stream interruption и fallback. Старые неполные записи требуют явной маркировки; восстановить отсутствовавшие usage и attempts догадкой нельзя.

### 2. P1: Provider и Model возвращают HTTP 500

`application/analytics/queries.py:166` строит условия по `provider_id` и `upstream_model_id` в `logical_requests`. В миграции `runtime/sqlite_migrations/v0006_free_guard_analytics.py:82` эти поля называются `resolved_provider_id` и `resolved_upstream_model_id`.

Read-only воспроизведение:

```text
GET /admin/api/analytics/summary?provider=groq
500: no such column: provider_id

GET /admin/api/analytics/requests?model=foo
500: no such column: upstream_model_id
```

Нужно определить семантику фильтра при fallback: final target или любая попытка, и согласовать её в summary, history, timeseries, breakdown и export. Простая замена названий колонок не решает эту продуктовую неоднозначность сама по себе.

### 3. P1: выбор периода молча игнорируется

`api/admin_static/analytics.js:53` отправляет `from` и `to`. Все analytics routes объявляют параметры `from_utc` и `to_utc` (`api/analytics_routes.py`). Неизвестные query params не отклоняются, поэтому сервер использует default 24h.

Один и тот же явно заданный прошлый интервал:

```text
GET summary?from=2026-10-01T00:00:00Z&to=2026-10-02T00:00:00Z
response range: 2026-10-10...2026-10-11; logical_requests=8

GET summary?from_utc=2026-10-01T00:00:00Z&to_utc=2026-10-02T00:00:00Z
response range: 2026-10-01...2026-10-02; logical_requests=0
```

Это затрагивает и export, который использует тот же `analyticsFilters`. Проверять нужно фактический response range и результаты, а не выбранное значение select.

### 4. P1: исправление unknown → 0 не доведено до API

SQL в `queries.py` теперь действительно сохраняет NULL при отсутствии reported usage. Но `summary` немедленно применяет `_int(...) or 0` к четырём token fields (`queries.py:346`). Live summary возвращает 0 для input/output/cache, одновременно timeseries возвращает null и отображается как unknown.

На экране: input/output/cache KPI = 0, tokens в timeline = unknown. Отчёт «все агрегаты возвращают NULL» неверен на уровне публичного ответа.

Контракт должен различать отсутствие traffic, отсутствие измерений и частично известную сумму. NULL не должен теряться при сериализации; completeness нужна отдельно для каждого поля.

### 5. P1: Verified free означает Guard off

Live FreeGuard summary:

```text
mode=off
verified_free=1540
unknown=0
blocked_paid=0
```

Первая видимая строка: `cheaperinference / aion-3.0`, billing=`unknown`, access=`unknown`, status=`ELIGIBLE`, reason=`Guard off`, action=`Revoke`.

`runtime/free_guard_service.py:607` устанавливает `eligible=decision.allowed`; `summary` (`:641`, `:654`) считает такие строки verified_free. При mode=off allowed не доказывает бесплатность. UI дополнительно использует eligible для выбора Revoke/Attest, хотя наличие manual attestation — отдельное свойство.

Нужно разделить разрешение выполнения, доказанную бесплатность, временную доступность и ручное подтверждение. Проверить off/warn/strict на одной и той же смешанной inventory. Данная находка доказывает ложную семантику UI, а не факт платного вызова в strict.

### 6. P1: вся страница растягивается за пределы окна

Измерения браузером на реальных данных:

| View | Viewport | Document scrollWidth | Ширина секции |
|---|---:|---:|---:|
| Analytics | 900 | 1215 | 1195.17 |
| Analytics | 390 | 1215 | 1195.17 |
| Analytics | 1280 | 1619 | 1310.59 |
| Free Models | 900 | 1113 | выходит за viewport |
| Messaging, для сравнения | 900 | 892 | помещается |

`.form-sections` — grid без ограничения минимального размера track; `.settings-section` сохраняет grid-item min-width:auto. Intrinsic width таблицы расширяет секцию и вместе с ней filters, toolbar и KPI. `overflow-x:auto` у table wrapper не спасает, потому что его родитель уже растянулся.

Нужны ограниченные grid tracks, min-width:0 у соответствующих контейнеров, локальный scroll таблиц и перенос toolbar. Проверять содержимое таблиц, длинные IDs и крайние контролы при 1280/900/390 px.

Скриншоты: `.work/ui-audit-2026-10-11/analytics-900.jpg`, `.work/ui-audit-2026-10-11/free-models-900.jpg`.

### 7. P2: поиск Free Models остался нативным неоформленным input

Search использует `type=search`, но базовые form styles (`admin.css:563`) покрывают text/number/password/select/textarea. Live высота поиска — 27px, фон rgb(59,59,59); соседнего Provider — 41px, фон rgb(10,11,16). Это видимый дефект единого языка форм, который проверка background только KPI не ловит.

### 8. P1: обязательные возможности UI отсутствуют

В `index.html` и полном `analytics.js` нет вызова `/analytics/breakdown`, provider/model/client/error drilldown, custom range, server-side pagination controls или обработки `next_cursor` истории. История всегда запрашивается с limit=50.

Нет token timeline, cache-write KPI, cost coverage, Verified $0, отображения success rate и отдельного payment/rate-limit breakdown. Trace вставляется в конец общей страницы вместо drawer и не переводит туда фокус.

Это требования §8 и частично P0 §14, а не всё отложенное P2. Заявление предыдущего audit, что provider/model/client/error breakdowns присутствуют в UI, не соответствует коду.

### 9. P2: график не является stacked timeline

`analytics.js:232` рисует один rect на bucket и выбирает один цвет по составу результатов. aria-label обещает stacked by outcome, но цветных сегментов нет. Нет временных tick labels и легенды: время доступно лишь в SVG title и дублирующей таблице. С одной bucket сейчас получается большой сплошной прямоугольник.

`.slice(-48)` отбрасывает ранние buckets; после исправления периода hourly 7d останется усечённым. Нужны полный интервал, пустые buckets, корректный масштаб/агрегация, реальные сегменты и ось времени.

### 10. P2: local time и защита от race conditions не реализованы

`formatLocal` (`analytics.js:86`) извлекает компоненты UTC-строки regex и выводит их без timezone conversion и подписи UTC. Например, live request 08:48Z показывается 08:48, тогда как в Europe/Moscow это 11:48. Секунды также потеряны.

`refreshAnalytics` не имеет generation check, AbortController или запрета перекрывающихся refresh. Filter change и interval могут выполнить несколько запросов; поздний ответ старого фильтра может перерисовать новый. Это вывод по коду, искусственно задержанные ответы в текущей проверке не воспроизводились.

## Что в предыдущем отчёте подтверждается

- Карточки используют project tokens; белый фон плиток не воспроизвёлся.
- Обе вкладки помещены в form-sections/settings-section.
- Статусы используют CSS tokens, график локальный inline SVG.
- Request history сохраняет известные значения отдельно от unknown_usage_attempts по коду рендерера.

Это узкие исправления, не доказательство завершения задачи.

## Почему заявленные тесты не дают приёмки

`e2e/test_analytics_admin.py:88` проверяет при разных ширинах только видимость первой KPI. Достаточно, чтобы первый элемент был виден; все остальные могут уйти за экран. Нет phone width, assert ширины документа, populated history/long IDs, измерений toolbar.

`test_new_tabs_share_the_admin_design_language` проверяет конкретные background/color первых KPI и наличие секции. Он полезен против одного старого дефекта, но не проверяет layout, period contract, filters или lifecycle analytics.

Заявленные 250/8527 тестов и пять baseline failures в этой проверке не пересчитывались. В момент осмотра уже шли чужие pytest-процессы; их не останавливали и не запускали конкурирующий полный suite. Независимое доказательство здесь — браузер, read-only HTTP, код и изолированный in-memory repro.

## Порядок исправлений и приёмка

1. Восстановить контракт recorder → request/attempt lifecycle и truthful status. Mock E2E через Messages/Responses/native с проверкой сохранённых строк, завершения, fallback и ошибок; без платных provider calls.
2. Исправить API filters и period contract. Проверить все projections и export на различающихся диапазонах и mixed fallback targets.
3. Сохранить nullable usage и исправить семантику Verified free независимо от mode. Отдельные regressions для missing, zero, partial и off/warn/strict.
4. Исправить layout на заполненных данных при 1280/900/390 px. Весь документ помещается; scroll остаётся внутри таблиц; toolbar и controls доступны; Search оформлен единообразно.
5. Реализовать обязательные breakdown/drilldown, pagination, информативные графики, timezone и защиту от старых ответов. Явно перечислить оставшиеся P1/P2, не заявлять их готовыми.
6. Выполнить целевые проверки, затем требуемые repo gates, обновить отчёт и приложить скриншоты заполненных состояний.

## Ограничения и внесённые изменения

Это независимый осмотр UI и связанных API/telemetry, а не полный аудит каждого enforcement path, security, retention, pricing adapters или упаковки. Отсутствие платных outbound calls в strict не сертифицировано этой проверкой.

Production code, settings, DB, credentials и процессы не менялись. Никаких attest/revoke/refresh-price/probe/apply/provider generation действий не выполнялось. Созданы только этот отдельный отчёт и локальные screenshots. Browser viewport override восстановлен.

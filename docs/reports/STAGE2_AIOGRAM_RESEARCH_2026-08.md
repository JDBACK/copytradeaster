# Исследование aiogram для Stage 2

Дата исследования: 2026-08-23  
Статус: завершено; входные данные для плана Stage 2 собраны  
Ветка: `codex/stage-2-telegram`  
Область: Telegram-обвязка для одного master и 10–30, максимум 100 followers

## 1. Как проводилось исследование

Исследование выполнено тремя независимыми субагентами. Для всех трёх задач
использовались только модель `GPT-5.6-Luna` и уровень рассуждения `max`:

1. версия, совместимость, changelog, lifecycle, polling/webhook и миграция;
2. Router/Dispatcher, filters, CallbackData, keyboards, FSM, DI, middleware,
   errors, flags и formatting;
3. эксплуатация, безопасность credentials, concurrency, flood control,
   shutdown, тестирование и observability.

Каждый поток использовал Context7 для актуальных API/signature snippets и
Firecrawl для проверки официальных первичных источников. Сторонние блоги и
встроенный web search не использовались.

Дополнительно через Firecrawl перепроверена официальная Aster Agent reference,
потому что Stage 2 обязан валидировать права уже созданного V3 Agent до
сохранения credentials.

## 2. Версия и совместимость

На дату среза актуальная стабильная версия — **`aiogram==3.30.0`**, выпущенная
2026-07-17.

Подтверждённые границы:

- Python `>=3.10,<3.15`; Python 3.12 проекта поддерживается;
- Telegram Bot API 10.2;
- статус PyPI — Production/Stable;
- базовый пакет использует `aiohttp`, а Aster adapter проекта — `httpx`;
  HTTP-сессии должны иметь разных владельцев и независимый lifecycle;
- aiogram требует `pydantic>=2.4.1,<2.14`, что совместимо с Pydantic v2 проекта.

Для воспроизводимости Stage 2 фиксирует точную версию:

```toml
"aiogram==3.30.0"
```

Optional extras на этом масштабе не нужны. В частности:

- `redis` не нужен при одном процессе и коротком FSM, который допускается
  сбрасывать при рестарте;
- `fast`/uvloop не нужен без измеренной проблемы производительности;
- `i18n` не нужен, пока интерфейс только на русском;
- `proxy`, `mongo`, `cli` и `signature` не входят в Stage 2.

## 3. Существенные изменения, которые нельзя брать из старых примеров

| Устаревший паттерн | Актуальный паттерн aiogram 3.30 |
|---|---|
| `Dispatcher(bot)` | `Dispatcher(...)`, затем `start_polling(bot)` |
| `executor.start_polling(dp)` | `await dp.start_polling(bot)` |
| `skip_updates=True` | явный `bot.delete_webhook(drop_pending_updates=...)` |
| `@dp.message_handler(...)` | `@router.message(...)` |
| keyword filters | `Command(...)`, `CommandStart()`, `F.text`, `F.photo` |
| `Bot.get_current()` | DI-параметр `bot: Bot` или `data["bot"]` |
| `State.set()/next()` | `FSMContext.set_state(...)` |
| FSM proxy | `get_data/set_data/update_data/clear` |
| `.add()`/`.row()` на markup | `InlineKeyboardBuilder`/`ReplyKeyboardBuilder` |
| `Bot(parse_mode=...)` | `DefaultBotProperties(parse_mode=...)` |
| `RetryAfter` | `TelegramRetryAfter` |
| `process_update()` | `feed_update()`/`feed_raw_update()` |

Дополнительные ловушки:

- bot передаётся в `start_polling()` позиционно, не как `bot=...`;
- handler без фильтра принимает любые content types;
- state/content type больше не фильтруются автоматически;
- первый совпавший handler останавливает поиск, поэтому fallback регистрируется
  последним;
- `CallbackQuery.message` может отсутствовать или быть inaccessible;
- callback data ограничена 1–64 байтами;
- после каждого callback нужно вызвать `answer()`, иначе у пользователя остаётся
  progress indicator;
- `Bot.close()` — API-метод Telegram, а не закрытие локальной aiohttp-сессии;
  локально закрывается `bot.session` либо используется ownership
  `close_bot_session=True`.

## 4. Нужные Stage 2 механизмы aiogram

| Механизм | Актуальный API | Применение в проекте |
|---|---|---|
| Composition | `Bot`, `Dispatcher`, `Router` | один bot, один Dispatcher, feature routers |
| Polling | `Dispatcher.start_polling()` | один long-polling process |
| Ограничение update tasks | `tasks_concurrency_limit` | bounded concurrency, стартовое значение 16 |
| Типы updates | `resolve_used_update_types()` | все типы updates, используемые зарегистрированными handlers |
| Commands | `CommandStart`, `Command`, `BotCommand` | только `/start`, `/menu`, `/cancel`, `/help` shortcuts |
| Filters | `F`, custom `Filter` | private chat, text/state, admin/ownership |
| Callbacks | typed `CallbackData` | короткие безопасные действия без JSON/секретов |
| Keyboards | `InlineKeyboardBuilder` | меню, настройки, подтверждение emergency/disconnect |
| FSM | `StatesGroup`, `State`, `FSMContext` | короткие onboarding/risk формы |
| Local isolation | `SimpleEventIsolation` | не смешивать быстрые ответы одного пользователя |
| DI | Dispatcher workflow data | передача shared application service, не всего `App` |
| Middleware | `BaseMiddleware` | private-chat/auth context до filters |
| Error boundary | router/dispatcher error handlers | безопасная классификация, без raw exception пользователю |
| Formatting | `DefaultBotProperties`, `html.quote`, formatting tree | HTML без инъекций; core notifications — plain text |
| Telegram errors | `TelegramRetryAfter`, `TelegramForbiddenError`, и др. | bounded retry и изоляция получателей |
| Тестовый вход | `feed_raw_update()` | router/filter/FSM/DI тесты без polling |

## 5. Рекомендуемый lifecycle

Для текущего modular monolith подходит long polling:

```python
bot = Bot(
    token=token,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dispatcher = Dispatcher(
    storage=MemoryStorage(),
    events_isolation=SimpleEventIsolation(),
    facade=facade,
)
dispatcher.include_router(root_router)

await dispatcher.start_polling(
    bot,
    allowed_updates=dispatcher.resolve_used_update_types(),
    handle_as_tasks=True,
    tasks_concurrency_limit=16,
)
```

Операционные правила:

- один bot token обслуживает ровно один polling process;
- polling и webhook одновременно запрещены;
- webhook не нужен без публичного HTTPS и горизонтального масштабирования;
- старый webhook удаляется явно; pending updates по умолчанию не выбрасываются;
- Telegram `AiohttpSession` и Aster `httpx.AsyncClient` не переиспользуют друг
  друга и закрываются своими владельцами;
- aiogram сам управляет только polling tasks, своими startup/shutdown hooks и
  Bot session; notification worker, engine/WS, Aster HTTP, SQLite и уже
  запущенные per-update tasks обязан остановить project-level lifecycle owner;
- долгие финансовые операции не являются Telegram delivery job: они используют
  существующие journal, command keys и operation ids ядра.

## 6. FSM: принятое решение

Официальная документация предупреждает, что `MemoryStorage` теряет данные при
рестарте. Для этого проекта это приемлемо и намеренно:

- FSM хранит только незавершённый шаг, `user`/`signer` и не-secret значения;
- незавершённую форму после рестарта пользователь начинает заново;
- private key никогда не кладётся в FSM;
- готовые credentials, policy и ownership сохраняются сразу в SQLite;
- `SimpleEventIsolation` достаточно для одного процесса;
- custom SQLite FSM или Redis не дают продуктовой ценности в Stage 2.

Для direct onboarding секрет вводится последним сообщением, немедленно
обрабатывается и удаляется best effort. Успешный `deleteMessage` не означает,
что секрет исчез из push preview, screenshot, клиентского cache или уже
прочитанной инфраструктурой Telegram. Это ограничение должно быть явно показано
пользователю до начала формы.

Ограничение Telegram для обычного удаления: сообщение должно быть моложе
48 часов; входящие сообщения в private chat бот удалять может.

## 7. Concurrency, retry и уведомления

`handle_as_tasks=True` создаёт отдельную asyncio task на update. Без
`tasks_concurrency_limit` число задач не ограничено. Для Stage 2:

- начальный limit — 16; менять только после измерения;
- бизнес-авторизация повторяется внутри application service, а не полагается
  только на filter/callback payload;
- риск/pause/resume — короткие идемпотентные SQLite operations;
- emergency использует существующий durable execution journal и новый
  `operation_id`, а не generic Telegram job queue;
- Telegram failure не меняет результат торговой команды.

Официальные ориентиры Telegram: примерно 1 сообщение/секунду в один чат,
20 сообщений/минуту в группу и около 30 сообщений/секунду для bulk broadcast.
Точный `TelegramRetryAfter.retry_after` обязателен к соблюдению.

Принятый минимальный notification path:

```text
structured core event
        -> SQLite notification_outbox
        -> один Telegram worker
        -> per-chat isolation + bounded retry
```

`TelegramForbiddenError` отключает Telegram notifications конкретного
получателя. `TelegramBadRequest` не ретраится вслепую. Network/5xx имеют
ограниченный retry с backoff. Блокировка бота одним пользователем не влияет на
остальных и на trading core.

## 8. Тестирование

Подтверждённые официальные инструменты:

- `Dispatcher.feed_raw_update()` — handlers, filters, middleware, DI и FSM без
  polling;
- direct async handler tests с `AsyncMock`;
- локальный `aiohttp.test_utils.TestServer` возвращает Bot API-shaped
  responses, а `TelegramAPIServer.from_base(...)` только направляет aiogram на
  URL этого test server;
- отдельный aiogram `AiohttpSession`; существующий `respx` покрывает только
  httpx/Aster и сам по себе Telegram transport не мокает.

Минимальные проверки:

- private chat/allowlist/ownership;
- порядок handlers и `/cancel` до fallback;
- каждый callback получает `answer()` на success/error/deny;
- private key не оказывается в FSM, логах, exception text или outbox;
- failure удаления secret message безопасно классифицируется;
- `TelegramRetryAfter`, 400, 403, network/5xx и изоляция чатов;
- повторная обработка идемпотентных risk/pause callbacks;
- graceful shutdown и повторная доставка pending outbox rows;
- real Telegram test-bot smoke выполняется отдельно от offline suite.

## 9. Scenes Wizard: исследование и решение

Актуальные компоненты aiogram 3.30.0:

```python
from aiogram.fsm.scene import (
    After,
    Scene,
    SceneRegistry,
    ScenesManager,
    on,
)
```

Подтверждённые возможности:

- `Scene(..., state="...")` создаёт изолированный FSM context с handlers и
  enter/leave/exit actions;
- `ScenesManager.enter()` входит в Scene, `close()` завершает flow;
- `SceneWizard.goto()`, `back()`, `retake()`, `exit()` управляют переходами;
- `After.goto/back/exit` запускает переход после успешного handler;
- history хранит до 10 временных snapshots в отдельном FSM destiny;
- `SimpleEventIsolation` нужен для быстрых последовательных callbacks.

Важные ловушки текущей версии:

- Scenes официально остаются experimental;
- использовать нужно class keyword `state=`, не ошибочный `name=` из одного
  marker-snippet документации;
- `exit()` не очищает обычные FSM data автоматически;
- `reset_history_on_enter=True` на дочерней Scene ломает ожидаемый Back;
- enter action зависит от текущего update type: message/callback требуют своих
  handlers;
- parent/global handlers и Scene routers чувствительны к registration order;
- Scene history не является business journal и не может хранить private key.

**Verdict для Stage 2: Scenes не использовать.** Main inline menu — stateless
screen render через Router/CallbackData. Onboarding имеет только три линейных
input steps, risk — presets и максимум один text input; `StatesGroup` короче и
безопаснее. Scenes пересматриваются только если появятся минимум три реально
ветвящихся wizard flows, общая history/back и повторно используемые lifecycle
hooks. Даже тогда pilot — onboarding, не menu.

Подробный UX-verdict и критерии пересмотра:
[STAGE2_TELEGRAM_UX_BRIEF_2026-08.md](STAGE2_TELEGRAM_UX_BRIEF_2026-08.md).

## 10. Aster V3 permission gate для direct onboarding

Официальный endpoint:

```text
GET /fapi/v3/agent
```

Это USER_DATA query. Обязательные поля — `user`, `signer`, `nonce`,
`signature`; запрос подписывает уже авторизованный signer. Ответ содержит:

```text
agentAddress
agentName
ipWhitelist
expired
canRead
canSpotTrade
canPerpTrade
canWithdraw
```

Stage 2 сопоставляет `agentAddress` с адресом, вычисленным из введённого private
key, и принимает аккаунт только если:

- signer совпал;
- `canRead=true`;
- `canPerpTrade=true`;
- `canWithdraw=false`;
- срок действия не истёк;
- configured server egress IP входит в whitelist согласно policy;
- signed `balance`, `positionRisk` и `openOrders` успешно читаются;
- testnet/mainnet guard не нарушен.

Проверка выполняется в `onboarding/direct`, не в Telegram handler.

## 11. Решения для Stage 2

Используем:

- `aiogram==3.30.0`;
- long polling, один asyncio process;
- одно редактируемое inline dashboard message + feature routers;
- shared transport-neutral application service для Bot и будущего Mini App;
- `MemoryStorage` + `SimpleEventIsolation` только для ephemeral non-secret FSM;
- обычный `StatesGroup`; Scenes Wizard не входит в production path;
- numeric Telegram user ID, private chat и ownership checks;
- typed CallbackData и inline keyboards;
- `DefaultBotProperties` + HTML escaping;
- SQLite notification outbox и один worker process;
- `feed_raw_update` + local aiohttp Bot API stub;
- существующие SQLite/AES-GCM/execution journal/risk models.

Откладываем:

- webhook, Redis/PostgreSQL, Celery/Kafka;
- multi-bot/multi-process;
- custom persistent FSM/dialog framework;
- WebApp/Builder onboarding (Stage 3);
- i18n, payments, broadcast platform;
- Prometheus/OpenTelemetry без измеримой необходимости.

## 12. Официальные источники

- [PyPI: aiogram](https://pypi.org/project/aiogram/)
- [GitHub release v3.30.0](https://github.com/aiogram/aiogram/releases/tag/v3.30.0)
- [aiogram changelog 3.30.0](https://docs.aiogram.dev/en/v3.30.0/changelog.html)
- [Dispatcher](https://docs.aiogram.dev/en/v3.30.0/dispatcher/dispatcher.html)
- [Router](https://docs.aiogram.dev/en/v3.30.0/dispatcher/router.html)
- [Long polling](https://docs.aiogram.dev/en/v3.30.0/dispatcher/long_polling.html)
- [Migration 2.x -> 3.x](https://docs.aiogram.dev/en/v3.30.0/migration_2_to_3.html)
- [Dependency injection](https://docs.aiogram.dev/en/v3.30.0/dispatcher/dependency_injection.html)
- [Middlewares](https://docs.aiogram.dev/en/v3.30.0/dispatcher/middlewares.html)
- [FSM](https://docs.aiogram.dev/en/v3.30.0/dispatcher/finite_state_machine/index.html)
- [Scenes Wizard](https://docs.aiogram.dev/en/v3.30.0/dispatcher/finite_state_machine/scene.html)
- [Callback data](https://docs.aiogram.dev/en/v3.30.0/dispatcher/filters/callback_data.html)
- [Keyboard builder](https://docs.aiogram.dev/en/v3.30.0/utils/keyboard.html)
- [Errors](https://docs.aiogram.dev/en/v3.30.0/dispatcher/errors.html)
- [Formatting](https://docs.aiogram.dev/en/v3.30.0/utils/formatting.html)
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [Telegram Bots FAQ](https://core.telegram.org/bots/faq)
- [Aster Agent endpoints](https://asterdex.github.io/aster-api-website/asterCode/endpoints/)

## 13. Журнал

### 2026-08-23

- создана и активирована ветка `codex/stage-2-telegram` от `main`;
- прочитаны обязательные living docs, архитектура, corrective report и
  фактические boundary-модули Stage 1;
- три независимых `GPT-5.6-Luna/max` потока завершили Context7 + Firecrawl
  исследование;
- stable-version, lifecycle и API signatures сверены между отчётами;
- отдельный UI-pass перепроверил edit-message menu, MenuButton/WebApp,
  callback byte limits и Scenes Wizard API/history/lifecycle;
- принято решение: inline dashboard + `StatesGroup`, Scenes в Stage 2 не
  использовать;
- отдельно перепроверен официальный Aster `GET /fapi/v3/agent`;
- решения перенесены в отдельный implementation plan Stage 2.

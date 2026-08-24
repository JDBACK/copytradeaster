# Stage 2 — текущее состояние и готовность

Дата среза: 2026-08-24

Ветка: `codex/stage-2-telegram`

Кодовый срез: `b238407` (`fix: simplify follower controls and automate leverage`)

Среда: только Aster Futures testnet

## 1. Итоговый вердикт

Функциональная реализация Stage 2 завершена: Telegram-бот, выдача доступа,
direct V3 onboarding, пользовательское inline-меню, админка, риск на Stop,
pause/resume, уведомления, безопасные логи и подключение динамического follower
работают в одном процессе вместе с принятым trading core Stage 1.

Текущий код **готов к контролируемой эксплуатации и финальной торговой приёмке
на Aster testnet**. Stage 2 пока не помечается `CLOSED`, потому что на новом
Telegram-follower ещё не выполнен один полный owner-authorized цикл
`resume → copy open + Stop → reduction/close → final flat`. Это единственный
оставшийся acceptance-пункт; новая большая тестовая кампания не нужна.

Stage 2 не готов к mainnet/реальным средствам. Прямой ввод signer private key
через Telegram, testnet IP-whitelist override и текущий Cross margin относятся
к осознанным ограничениям этого этапа.

## 2. Фактический продуктовый scope

Масштаб и роли:

- один master trader;
- 10–30 пользователей в первом рабочем запуске, верхняя граница около 100;
- один Telegram user владеет не более чем одним активным follower account;
- администратор и master trader: `@DamienKub`, Telegram ID `203493943`;
- admin capability определяется immutable numeric ID из конфигурации и может
  совмещаться с ownership обычного follower account.

Пользователь получает:

- автоматическую заявку на доступ после простой inline-captcha;
- подключение отдельного Aster testnet account через V3 Agent credentials;
- dashboard, status, activity, risk, notifications, account и help;
- изменение только процента денежного риска на защитном Stop;
- pause/resume новых входов;
- замену или безопасное отключение своего подключения;
- персональные Telegram-уведомления.

Администратор получает:

- список пользователей, followers и заявок;
- approve/reject доступа и recovery-добавление по Telegram ID;
- targeted или общий reconcile;
- глобальную паузу/resume;
- глобальное аварийное закрытие через durable control-core.

Пользователь не управляет leverage, entry deviation или аварийным закрытием.
Эти действия удалены из меню и запрещены на общем application boundary, поэтому
будущая Mini App не сможет обойти ограничения Telegram UI.

## 3. Текущая архитектура

Проект остаётся небольшим async modular monolith:

```text
один Python 3.12 / asyncio process
├── Stage 1 trading core
│   └── master stream → projector → coordinator → risk → planner
│       → execution journal → executor → reconciliation
├── shared application layer
│   └── authorization / ownership / onboarding / risk / controls / admin
├── aiogram 3.30.0 polling runtime
│   └── middleware → routers → inline screens / short linear FSM
├── notification router + SQLite outbox + Telegram worker
└── SQLite WAL
    └── accounts / encrypted credentials / policies / execution journal /
        Telegram ownership / access requests / agent validations / outbox
```

Telegram handlers не вызывают Aster trading endpoints напрямую. Telegram Bot
и будущая Mini App используют один `UserApplicationService` и одинаковые
ownership/risk/control rules. Trading core не знает, пришли credentials из CLI,
Telegram direct onboarding или будущего Builder flow.

Отдельные Redis, PostgreSQL, Celery, Kafka, webhook-сервис и микросервисы для
текущего масштаба не нужны.

## 4. Доступ и регистрация

Фактический flow неизвестного private user:

```text
/start
  → одноразовая inline арифметическая captcha
  → durable PENDING request в SQLite
  → карточка configured admin
  → idempotent approve/reject
  → первое пользовательское меню подключения
```

Captcha ограничена TTL, числом ошибок и cooldown. После создания durable
заявки повторно решать её не нужно. Ошибка доставки прямой admin-карточки не
теряет заявку: она остаётся в разделе `Админка → Заявки`. Ручное добавление по
Telegram ID сохранено только как recovery-механизм.

## 5. Direct V3 onboarding

Бот последовательно запрашивает:

1. адрес обычного testnet-кошелька;
2. `API wallet address`;
3. private key именно этого API wallet.

До ввода пользователь получает фактические prerequisites Aster:

- Position mode = Hedge Mode;
- у Agent/API wallet включены чтение и `Perps`/`canPerpTrade`;
- withdrawal выключен;
- API wallet окончательно авторизован подписью обычного кошелька;
- на подключаемом account нет позиций и открытых ордеров.

Локально проверяются адреса и соответствие private key заявленному API wallet.
Затем ephemeral signed client читает Agent permissions, balance,
`positionRisk`, `openOrders` и position mode. Только после успешного preflight
credentials атомарно сохраняются, а follower публикуется в runtime на паузе.

Каждое из трёх пользовательских сообщений удаляется best-effort сразу после
чтения и до проверки. Private key не хранится в FSM, callback, UI, outbox или
логах; в SQLite он находится только внутри AES-GCM encrypted blob. Telegram всё
равно не гарантирует удаление из push-уведомлений, клиентского кэша и скриншотов,
поэтому direct flow остаётся testnet-only.

Ошибки проходят через typed `OnboardingErrorCode`: неверная key pair,
неоконченная authorization, отсутствующие permissions, Hedge mismatch,
непустой account и другие классы получают понятную remediation и кнопки retry.
Шаги 1/2/3 используют свежие карточки; предыдущие данные не остаются лежать в
чате рядом с повторной попыткой.

## 6. Telegram UI и aiogram

Основной интерфейс полностью inline-first. Slash-команды ограничены
`/start`, `/menu`, `/cancel`, `/help` и служат входом/recovery.

Пользовательские экраны:

- Dashboard — состояние копирования, счёт, риск, позиции и актуальность;
- Risk — только риск на Stop и быстрые пресеты;
- Status — Aster connectivity, права Agent и позиции;
- Activity — безопасный журнал операций;
- Notifications — один понятный toggle и состояние доставки;
- Account — замена/отключение credentials;
- Help/Security — последствия действий и testnet caveats.

Aiogram Scenes изучены и намеренно не используются. Scenes Wizard experimental
и не является системой постоянного меню; текущие линейные credential/risk flows
проще и прозрачнее реализованы обычными `StatesGroup`.

## 7. Риск, leverage и margin mode

Размер позиции определяется денежным риском до Stop:

```text
risk_budget = balance/equity base × risk_percent
quantity = function(entry, stop, fees/slippage, exchange filters)
```

Leverage не определяет loss до Stop; он влияет на требуемую маржу и liquidation.
Поэтому пользователь не вводит leverage.

Перед новым входом или реальным увеличением coordinator выполняет signed
`GET /fapi/v3/leverageBracket?symbol=...` для конкретного follower и symbol.
Adapter принимает документированный object и фактический testnet list, кэширует
ответ на короткий TTL и выбирает максимальный положительный
`initialLeverage`. Локального потолка `125x` нет: testnet уже возвращает более
высокие значения.

Если leverage endpoint недоступен или ответ некорректен, новый вход/увеличение
не выполняется. Закрытие, сокращение и поддержка защитных ордеров продолжаются
с leverage существующей позиции и не блокируются этим чтением.

Stage 2 не меняет `marginType`. На подключённом account фактически остаётся
`cross`. Принудительный Isolated вынесен в отдельную будущую задачу после
специальной проверки реального Aster testnet поведения.

## 8. Управление и safety boundaries

- Pause запрещает новые входы и увеличения, но не мешает сокращениям,
  закрытиям и защите.
- Resume требует готового runtime; если master уже находится в позиции,
  используется отдельное подтверждение и targeted reconciliation.
- Disconnect сначала ставит account на паузу и проверяет flat/open orders /
  in-flight commands; затем удаляет encrypted credentials и runtime binding.
- Follower emergency отсутствует в UI/router и отвергается application layer
  даже при forged confirmation.
- Global emergency остаётся admin-only и использует Stage 1 durable workflow:
  запрещает entries, закрывает followers, проверяет фактический flat и сохраняет
  аварийное состояние до отдельного admin resume.

Mutation locks и generation barriers не позволяют Telegram control operation
гоняться с торговым recompute.

## 9. Уведомления и operational logging

Trading command сначала получает собственный результат и durable journal
status. После подтверждения создаётся структурированное событие, которое
идемпотентно записывается в recipient-specific SQLite outbox. Отдельный worker
доставляет его в Telegram с lease/retry/backoff. Ошибка Telegram не превращает
успешный trade в FAILED.

Логи пишутся в console и rotating `data/logs/copytradeaster.log`. Onboarding и
state-changing controls имеют bounded `attempt/success/rejected` audit events.
Адреса, private keys, signatures, signed URL/query, confirmation tokens и raw
exchange payloads в логирование не передаются.

## 10. Фактическая приёмка владельца и testnet evidence

Владелец вручную подтвердил:

- captcha, создание заявки и выдачу доступа;
- approve через inline admin-card;
- direct onboarding второго account;
- понятные typed errors и повторное подключение;
- чистый Step 1/2/3 chat UX без накопления введённых адресов;
- notifications toggle;
- изменение риска на Stop;
- актуальное упрощённое меню без пользовательского leverage/deviation/emergency;
- общее текущее впечатление: «вроде всё пашет».

Это подтверждает UI/onboarding/control flow, но не считается доказательством
реальной торговой операции.

Отдельная signed testnet проверка подключённого flat/paused follower показала:

| Symbol | Максимум `leverageBracket` | POST leverage / `positionRisk` |
|---|---:|---|
| BTCUSDT | 200x | принято, подтверждено 200x |
| ETHUSDT | 200x | принято, подтверждено 200x |
| SOLUSDT | 100x | принято, подтверждено 100x |

Во всех трёх случаях `marginType=cross`, non-zero positions = 0, open orders =
0. Ордеров во время этой проверки не создавалось.

Текущий offline/coverage/static gate и единственные актуальные suite counters
зафиксированы в
[`ENGINE_TEST_STATUS_2026-08.md`](../ENGINE_TEST_STATUS_2026-08.md).

## 11. Текущий runtime snapshot

После restart на `b238407`:

- engine `ready=true`;
- `degraded_active=false`;
- глобальные новые входы запрещены: `allow_new_entries=false`;
- глобальная авария сохранена: `emergency_active=true`;
- Telegram-follower enabled, risk = 1%, собственные новые входы на паузе;
- in-flight execution commands = 0;
- последние `engine.startup` и `engine.ready` notifications доставлены (`SENT`).

В outbox остаются четыре исторические `FAILED` startup/ready записи с ранним
`telegram_bad_request`. Они не повторяются после исправлений и не отражают
текущую доставку; последние последовательные запуски имеют `SENT`.

## 12. Что намеренно отложено

- один реальный resume/copy acceptance cycle на новом Telegram-follower;
- принудительный Isolated margin mode;
- заполненный Agent `ipWhitelist` вместо testnet-only override;
- Builder/Aster Code onboarding и monetization — Stage 3;
- Mini App UI — использует уже готовый общий application contract позднее;
- mainnet и реальные средства;
- webhook, отдельные сервисы и внешняя БД без измеримой необходимости.

## 13. Единственный оставшийся acceptance-сценарий

После отдельного разрешения владельца нужен один небольшой testnet cycle:

1. убедиться, что master и Telegram-follower flat, open orders = 0;
2. снять global emergency/паузу и resume только в контролируемом окне;
3. открыть на master минимальную тестовую позицию с защитным Stop;
4. подтвердить follower position, автоматический leverage, Stop и notification;
5. выполнить одно сокращение либо сразу закрытие master;
6. подтвердить follower flat, отсутствие bot-owned orders и in-flight commands;
7. вернуть глобальную и follower pause.

После этого Stage 2 можно формально пометить `CLOSED` для testnet scope. Никакие
дополнительные 30/100-user кампании или сотни новых тестов для закрытия этапа
не требуются.

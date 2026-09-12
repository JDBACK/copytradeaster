# Aster DEX API: пригодность для копитрейдинга
> FREEZE 2026-09-10: исторический API fact-book. Как продуктовый план — superseded: актуальная граница продукта — `ARCHITECTURE.md` §4 + application service boundary (`only risk_percent can be changed`); §§5/8 не читать как инструкции (§5 follower controls и §8 OTOCO — out of scope для v1).

Статус исследования на 20 августа 2026 года. Версия включает глубокий разбор Aster Code / Builder (AsterCode, «билдер») и его сравнение с прямым Agent/API-wallet-путём.

## Цель и границы

Проверить по актуальной документации Aster:

- какая версия API является текущей и как создаются API-ключи;
- доступны ли баланс, позиции, плечо, открытие/закрытие, стоп-лосс, тейк-профит и частичное закрытие;
- как в реальном времени отслеживать действия мастер-аккаунта для копирования;
- можно ли пересчитывать размер позиции подписчика от риска на стоп;
- существует ли testnet/demo и как получить тестовый API-ключ.

Не рассматриваются региональные ограничения, маркетинг копитрейдинга и этические вопросы.

## Todo

- [x] Зафиксировать план и критерии проверки.
- [x] Найти официальную документацию API v3, changelog и инструкцию по ключам.
- [x] Проверить REST-методы чтения аккаунта и торговли.
- [x] Проверить WebSocket/user-data stream и типы событий.
- [x] Проверить SL/TP, изменение ордеров и частичное закрытие.
- [x] Проверить плечо, режим позиции, точность/лимиты и расчёт объёма.
- [x] Проверить testnet/demo, базовые URL и выдачу тестовых средств.
- [x] Сформировать итог: что поддерживается, что требует логики бота, что не подтверждено.
- [x] Сравнить выводы с `paralel_research.md` и перепроверить новые утверждения.
- [x] Проверить faucet, Strategy/Builder routes и спорные REST/WebSocket hostnames.
- [x] Включить подтверждённые дополнения и исправить найденные ошибки.
- [x] Прочитать и фактологически сравнить все девять `.md` из `reports/`.
- [x] Перепроверить найденные там Agent lifecycle, signing contexts, account history, Strategy-ограничения, `priceProtect`, withdrawals и sub-accounts.
- [x] Глубоко изучить Aster Code / Builder: integration flow, authentication, endpoints, оба официальных demo-репо.
- [x] Проверить Builder-маршруты на futures testnet живыми HTTP-запросами (400, а не 404).
- [x] Сравнить Builder vs прямой Agent/API-wallet для схемы копитрейда и дать вердикт (лучше ли Builder).
- [x] Обновить главный отчёт: новые разделы про Builder, testnet-подтверждение, вердикт.

## Матрица доказательств

| Вопрос | Статус | Официальный источник | Промежуточный вывод |
|---|---|---|---|
| Текущая версия API | Подтверждено | [Официальный индекс](https://github.com/asterdex/api-docs/blob/master/README.md) | V3 — Recommended; V1 — Legacy. Новые V1 Key/Secret не выдаются с 25.03.2026. |
| Создание и права API-ключа | Подтверждено | [API Wallet](https://www.asterdex.com/en/api-wallet), [registerAndApproveAgent](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-v3.md#register-and-approve-agent-public), [Agent management](https://asterdex.github.io/aster-api-website/asterCode/endpoints/) | V3 использует master `user`, API-wallet `signer` и private key signer; Agent можно создать, проверить, обновить и отозвать. Trading и registration используют разные chain-ID contexts. |
| Баланс и позиции | Подтверждено | [Futures API v3](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-v3.md) | Доступны баланс, account info, positionRisk, PnL, availableBalance, margin. |
| Открытие/закрытие позиции | Подтверждено | [New Order](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-v3.md#new-order--trade) | MARKET/LIMIT и условные типы; закрытие обратным ордером, частичное — quantity, где применимо `reduceOnly`. |
| SL/TP и их перестановка | Подтверждено с оговоркой | [New Order](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-v3.md#new-order--trade), [Strategy Order](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-v3.md#place-strategy-order-trade) | SL/TP есть. Обычный modify поддерживает только LIMIT; стандартный SL/TP переставляется cancel+new. Mainnet strategy OCO/OTOCO имеет update. |
| Частичное закрытие | Подтверждено | [Futures API v3](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-v3.md) | Закрывающий ордер с нужным quantity; семантика зависит от One-way/Hedge mode. |
| Плечо и режим позиции | Подтверждено | [Futures API v3](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-v3.md#change-initial-leverage-trade) | Плечо, Cross/Isolated, One-way/Hedge, isolated margin и leverage brackets доступны. |
| Поток событий мастер-аккаунта | Подтверждено с пробелом | [User Data Streams](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-v3.md#user-data-streams) | ORDER_TRADE_UPDATE, ACCOUNT_UPDATE, ACCOUNT_CONFIG_UPDATE, MARGIN_CALL. Поток и REST-история относятся ко всему account, а не к одному signer. Отдельное WS-событие изменения strategy order не документировано. |
| Изоляция средств и ключей | Подтверждено для mainnet | [Sub Accounts](https://docs.asterdex.com/trading/sub-accounts) | Sub-account имеет собственные assets, positions и V3 API keys; withdrawal permission для его ключей постоянно отключено. Лимит — до 10 ключей на sub-account. |
| Testnet/demo | Подтверждено | [Futures Testnet API v3](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-testnet.md), [Aster TestNet](https://docs.asterdex.com/aster-chain/testnet), [Faucet](https://www.asterdex-testnet.com/en/faucet) | Testnet работает; faucet выдаёт 1 000 test USDT и 1 000 test ASTER один раз в день. |
| Aster Code / Builder (готовность заменить передачу ключей) | Подтверждено | [Aster Code product docs](https://docs.asterdex.com/program-and-rewards/aster-code), [Futures Integration Flow](https://asterdex.github.io/aster-api-website/asterCode/integration-flow/), [Futures Authentication](https://asterdex.github.io/aster-api-website/asterCode/authentication/), [Endpoints](https://asterdex.github.io/aster-api-website/asterCode/endpoints/), [API-demo/aster-code-demo](https://github.com/asterdex/API-demo/tree/main/aster-code-demo) | Builder (AsterCode) — это broker-экосистема поверх той же V3 API. Пользователь не передаёт личный private key: однократно подписывает ApproveAgent + ApproveBuilder (EIP-712), а signer/API-wallet private key генерирует и хранит билдер. Builder выставляет ордера от имени пользователя с `builder`+`feeRate`. |
| Builder-маршруты на testnet | Подтверждено (live-проверка 20.08.2026) | [Futures Testnet general-info](https://asterdex.github.io/aster-api-website/futures-testnet/general-info/), прямые запросы к `fapi.asterdex-testnet.com` | `approveAgent/approveBuilder/updateBuilder/agent/builder/order/leverage/balance/positionRisk/openOrders/placeStrategyOrder/builder/userTrades/builder/approvedUserList` отвечают 400/-1102 (валидация параметров), а не 404: маршруты зарегистрированы на testnet. chainId для тестнет-подписи = 714. |

## Журнал исследования

### 2026-08-20 — Builder (Aster Code) deep-dive

- Перечитана вся доступная документация AsterCode: integration flow (Futures + Spot), authentication, endpoints, changelog 2026, testnet general-info, официальный GitHub api-docs (включая raw testnet reference) и product docs про Aster Code.
- Клонированы и изучены официальные demos: `asterdex/API-demo/aster-code-demo` (Node.js, полный цикл: approveAgent → approveBuilder → placeOrder) и `jupiter-hongc/aster-code-builder-demo` (фронтенд-флоу подключения кошелька и подписи ApproveAgent/ApproveBuilder через wagmi).
- Прямые check-запросы к `https://fapi.asterdex-testnet.com`: `/fapi/v3/builder`, `/fapi/v3/agent`, `/fapi/v3/approveBuilder`, `/fapi/v3/updateBuilder`, `/fapi/v3/approveAgent`, `/fapi/v3/placeStrategyOrder`, `/fapi/v3/order`, `/fapi/v3/leverage`, `/fapi/v3/balance`, `/fapi/v3/positionRisk`, `/fapi/v3/openOrders`, `/fapi/v3/builder/userTrades`, `/fapi/v3/builder/approvedUserList` — все вернули ошибки валидации параметров (400/-1102), а не 404. Т.е. Agent- и Builder-маршруты зарегистрированы и на futures testnet, 1:1 с mainnet.
- Вывод о модели: Builder НЕ требует у пользователя передачи личного private key. Пользователь однократно подписывает EIP-712 (ApproveAgent + ApproveBuilder) своим основным wallet; signer/API-wallet private key генерируется и хранится на backend билдера. Спот-версия использует те же принципы (Spot builder: max feeRate 0.01).
- Интеграционный флоу: Builder готовится (100 ASTER в Futures-аккаунте), пользователь approveAgent (можно сразу с builder), затем approveBuilder с maxFeeRate, после чего билдер выставляет ордера от имени пользователя за подписью signer с полями `builder` + `feeRate`.

### 2026-08-19 — старт

- Источники ищутся встроенным веб-поиском.
- Полное содержание найденных страниц извлекается Firecrawl.
- Приоритет доказательств: официальная документация Aster, официальный GitHub/SDK, официальные объявления; сторонние материалы используются только для поиска пробелов или явных расхождений.

## Краткий ответ

Да, технически Aster Futures API v3 покрывает MVP копитрейдинга:

- читать баланс, доступную маржу, позиции и PnL;
- открывать и закрывать позиции, в том числе частично;
- ставить обычные и условные ордера, SL, TP и trailing stop;
- менять плечо, margin mode и position mode;
- получать в реальном времени изменения ордеров, исполнений, позиций, баланса и плеча;
- торговать в отдельном futures testnet.

Высокоуровневого метода вида «подключить подписчика к мастеру» нет. Копирование, риск-менеджмент, сопоставление ордеров и повторное восстановление состояния — логика вашего сервиса.

Отдельно: для **онбординга пользователей** есть официальная broker-модель **Aster Code / Builder** — пользователь не передаёт личный ключ, а один раз подписывает ApproveAgent + ApproveBuilder, а signer-ключи для торговли создаёт и хранит билдер. Подробнее — раздел 9. Это лучше подходит для массового подключения подписчиков, чем требовать от каждого создания и ввода API-ключа.

## 1. Актуальная версия и модель ключей

### Что является актуальным

[Официальный индекс Aster API](https://github.com/asterdex/api-docs/blob/master/README.md) прямо говорит:

- V3 — `Recommended` для новых интеграций;
- V1 — `Legacy`;
- с 25 марта 2026 года новые V1 API Key/Secret больше не создаются;
- уже существующие V1 Key/Secret продолжают работать.

Это также отображает действующая [страница legacy API management](https://www.asterdex.com/en/api-management). Следовательно, новый бот нужно проектировать под V3, а не под HMAC V1.

### Что добавлялось в 2026 году

[Официальный changelog](https://github.com/asterdex/api-docs/blob/master/CHANGELOG.md) подтверждает актуальность нужных для проекта возможностей:

- 12 мая — `registerAndApproveAgent`;
- 20 мая — Strategy Order API: OTO, OCO и OTOCO;
- 30 июня — `builder` и `feeRate` в strategy orders;
- 3 июля — Builder trades и approved user list;
- 16 июля — пагинация списка approved users.

Это не старые скопированные Binance-compatible методы: ключевые Agent/Strategy/Builder части действительно добавлялись и менялись в 2026 году.

### Что пользователь должен передать боту в V3

V3 — это не классическая пара `apiKey + apiSecret`. Для подписанного вызова используются:

1. `user` — адрес основного/login wallet пользователя;
2. `signer` — адрес отдельного API wallet/agent wallet;
3. private key именно API wallet;
4. уникальный `nonce` в микросекундах;
5. EIP-712 подпись параметров private key API wallet.

Private key основного wallet передавать боту не требуется. Основной wallet нужен только для разовой авторизации API wallet/agent.

Создание через интерфейс:

1. открыть [Aster API Wallet](https://www.asterdex.com/en/api-wallet);
2. подключить основной wallet;
3. выбрать `Pro API` / `Authorize new API wallet`;
4. создать API wallet и разрешить чтение/торговлю perpetuals;
5. сохранить `main wallet address`, `API wallet address` и `API wallet private key`;
6. не включать withdrawal, потому что для копитрейдинга он не нужен;
7. задать срок действия и, если инфраструктура имеет постоянный исходящий IP, IP whitelist.

В документации также есть `POST /fapi/v3/registerAndApproveAgent`: сервис может зарегистрировать agent программно после того, как пользователь подпишет своим основным wallet одно сообщение авторизации. Параметры разрешений:

- `canSpotTrade`;
- `canPerpTrade`;
- `canWithdraw`;
- `expired`;
- `ipWhitelist`.

### Не смешивать trading-подпись и регистрацию Agent

В актуальной документации это два разных криптографических контекста:

- обычные V3 trading-запросы подписываются Agent private key с EIP-712 domain `chainId=1666` на mainnet или `714` на testnet;
- публичный `POST /fapi/v3/registerAndApproveAgent` подписывается основным wallet пользователя и принимает `signatureChainId=56` для EVM-адреса либо `101` для Solana. Документация прямо запрещает подставлять в эту registration-подпись фиксированный `1666`.

`registerAndApproveAgent` имеет weight 50 и объединяет регистрацию и выдачу прав. Для `ipWhitelist` этот endpoint использует список IP/CIDR через пробел; при `canWithdraw=true` whitelist обязателен и не может быть пустым.

[Отдельный официальный Agent-management reference](https://asterdex.github.io/aster-api-website/asterCode/endpoints/) также документирует lifecycle уже созданного Agent:

```text
POST   /fapi/v3/approveAgent
POST   /fapi/v3/updateAgent
DELETE /fapi/v3/agent
GET    /fapi/v3/agent
```

`GET /agent` возвращает адрес, имя, expiry, IP whitelist и флаги `canRead`, `canSpotTrade`, `canPerpTrade`, `canWithdraw`. Проверка без подписи 19.08.2026 получила от всех четырёх маршрутов ошибки обязательных параметров, а не 404: маршруты зарегистрированы. При этом создание не содержит отдельного настраиваемого `canRead`; доступ Agent с `canPerpTrade=false` к USER_DATA/listenKey документация однозначно не гарантирует — это отдельный signed test.

### Что означает право на withdrawal

Для копитрейдинга нужно оставлять `canWithdraw=false`. Текущий V3 withdrawal reference показывает дополнительную защиту EVM-вывода: кроме Agent-auth требуется отдельный `userSignature`, подписанный основным wallet владельца. Но для Solana `userSignature` сейчас помечен как необязательный с обещанием будущего обязательного enforcement. Поэтому нельзя считать, что одного Agent key универсально недостаточно для вывода во всех сетях; опираться нужно именно на запрет `canWithdraw` и изоляцию account.

Для Telegram-MVP это означает: бот принимает не «один API-ключ», а три значения V3. Чистый Telegram-чат технически можно использовать как первый интерфейс, но сервер всё равно должен хранить API-wallet private key в зашифрованном vault/secret storage. Для нормального онбординга позже удобнее небольшая WebApp-страница, которая просит wallet-подпись и не требует передачи private key основного wallet.

С появлением Aster Code / Builder (раздел 9) появляется **альтернатива онбординга без ввода ключей**: пользователь подписывает ApproveAgent + ApproveBuilder в кошельке (или через WebApp), а signer/API-wallet private key не выдаётся наружу вовсе — его создаёт и хранит backend билдера. Для схемы «Telegram-бот + подписчики» это более чистый UX и одновременно официальный способ монетизации.

### Sub-account как отдельный контур средств и ключей

[Актуальная продуктовая документация Sub Accounts](https://docs.asterdex.com/trading/sub-accounts) подтверждает:

- у каждого sub-account собственные assets, positions и API keys;
- функция доступна всем VIP-уровням; лимит составляет 10–50 sub-accounts в зависимости от VIP/MM tier;
- на один sub-account можно создать до 10 ключей, на master account — до 30;
- sub-account поддерживает только V3 API;
- withdrawal permission у всех его API keys постоянно отключено;
- sub-account может получать on-chain deposits и внутренние переводы со стороны master, но не может выводить средства напрямую;
- замороженный sub-account не открывает новые позиции: в One-way разрешены только reduce-only orders, в Hedge — только закрытие.

Mainnet V3 reference содержит `bind/create/list/update/subAccountTransfer` endpoints. Часть операций создания и управления требует подписей основного и дочернего wallet, поэтому это не «создать sub-account одним торговым Agent key». Для самого копирования sub-account не обязателен, но это официальная возможность отделить copy-баланс и навсегда убрать withdrawal permission у его API keys. Наличие полного этого flow на testnet отдельно не подтверждено.

## 2. Торговые возможности V3

Основной REST URL: `https://fapi.asterdex.com`.

| Задача | Endpoint/механизм | Вывод |
|---|---|---|
| Баланс | `GET /fapi/v3/balance` | Есть wallet balance, cross wallet balance, unrealized PnL, available balance, max withdraw amount. |
| Полная информация аккаунта | `GET /fapi/v3/accountWithJoinMargin` | Есть активы, общая маржа, initial/maintenance margin, позиции и плечо. |
| Позиции | `GET /fapi/v3/positionRisk` | Есть entry price, position amount, mark price, liquidation price, leverage, isolated margin, PnL. |
| Открытие | `POST /fapi/v3/order` | MARKET, LIMIT и условные типы. |
| Полное обычное закрытие | Встречный MARKET с количеством позиции | В One-way mode применяется `reduceOnly=true`; в Hedge mode передаётся нужный `positionSide` и встречная сторона без `reduceOnly`. |
| Частичное закрытие | Встречный ордер с меньшим `quantity` | Поддерживается; количество — абсолютное, не процент. |
| Stop Loss | `STOP`, `STOP_MARKET` | `stopPrice`, `workingType`, `priceProtect`; может закрывать заданное количество или всю позицию условным `closePosition=true`. |
| Take Profit | `TAKE_PROFIT`, `TAKE_PROFIT_MARKET` | Те же базовые механизмы. |
| Trailing stop | `TRAILING_STOP_MARKET` | `activationPrice` и `callbackRate` 0.1–5%. |
| Плечо | `POST /fapi/v3/leverage` | Текст reference указывает 1–125, но live testnet 24.08.2026 принял 200x для BTCUSDT/ETHUSDT; фактический максимум задаёт symbol-specific `leverageBracket`, а не локальный cap. |
| Cross/Isolated | `POST /fapi/v3/marginType` | Поддерживается. |
| Multi-Assets | signed `GET/POST /fapi/v3/multiAssetsMargin` | Account-wide `true` блокирует переход символа в `ISOLATED` (`-4168` на Testnet); для isolated нужен Single-Asset `false`. |
| Изменение isolated margin | `POST /fapi/v3/positionMargin` | Добавить или уменьшить маржу позиции. |
| One-way/Hedge | `POST /fapi/v3/positionSide/dual` | Режим меняется для аккаунта. |
| Перевод spot↔futures | `POST /fapi/v3/asset/wallet/transfer` | Поддерживается перевод средств между внутренними кошельками. |
| История account | `GET /fapi/v3/allOrders`, `/userTrades`, `/income` | Ордера, fills, комиссии, funding, realized PnL и transfers; это account-level, не signer-level audit. |
| Agent lifecycle | `approveAgent`, `updateAgent`, `GET/DELETE /agent` | Создание/выдача прав, проверка, изменение и отзыв Agent. |
| Sub-accounts | `createSubAccount`, `getSubAccountList`, `updateSubAccount`, `subAccountTransfer` | Отдельные assets/positions/V3 keys и внутреннее управление средствами. |
| Биржевые ограничения | `GET /fapi/v3/exchangeInfo` | Tick size, step size, min/max qty, min notional, типы ордеров, лимиты. |
| Лимиты плеча | `GET /fapi/v3/leverageBracket` | Проверка допустимого notional на выбранном плече. |

### Live testnet correction: leverage brackets — 2026-08-24

Signed `GET /fapi/v3/leverageBracket?symbol=...` на подключённом follower
показал расхождение с текстом reference:

- даже при переданном `symbol` testnet вернул list с одним symbol object, а не
  документированный одиночный object;
- максимальный положительный `initialLeverage`: BTCUSDT = 200, ETHUSDT = 200,
  SOLUSDT = 100;
- signed `POST /fapi/v3/leverage` на flat/paused account принял эти значения,
  а последующий `positionRisk` подтвердил их;
- позиции и open orders оставались нулевыми, `marginType` остался `cross`.

Следствие для Stage 2 на дату этого августовского среза (историческое,
superseded follow-up от 2026-09-12): adapter принимает обе формы ответа и не
применяет устаревший локальный ceiling 125x. Leverage читается только для
нового входа или увеличения; сбой endpoint не блокирует закрытие, сокращение и
защиту. Пользовательская поверхность Stage 2 по-прежнему не меняет
`marginType`, но текущий engine зеркалирует авторитетный режим master — см.
`Follow-up: margin mode на Testnet — 2026-09-12` и
`reports/MARGIN_MODE_MIRROR_2026-09.md`.

### Follow-up: leverage как control-plane — 2026-08-25

Механика потребления изменилась (баги #25–26, `trading/leverage.py`): bracket
GET больше не выполняется на hot path сделки. Кандидатный каталог по symbol
обновляется фоново; account-specific значение подтверждается `POST /fapi/v3/leverage`
(reject → follower-specific bracket GET → POST максимума аккаунта), а рестарт
восстанавливает его из authoritative `positionRisk`. Факты API выше
(формы ответа, максимумы 200/200/100, принятие POST) остаются в силе.

### Follow-up: margin mode на Testnet — 2026-09-12

[Aster Futures V3](https://asterdex.github.io/aster-api-website/futures-v3/account%26trades/)
публикует signed `marginType` и `multiAssetsMargin` GET/POST. На настоящих
Testnet-ключах `POST marginType(ISOLATED)` вернул `-4168`, пока аккаунт был
в Multi-Assets. После signed Single-Asset переключения биржа приняла оба
направления `CROSSED`/`ISOLATED`; signed `positionRisk` подтвердил режим,
фактический `isolatedMargin` и `liquidationPrice` после входа. Подробная
приёмка — `reports/MARGIN_MODE_MIRROR_2026-09.md`. Только код `-4046` означает
«режим уже установлен»; прежнее подавление `-4045` в адаптере удалено.

### Стопы, тейки и их перестановка

Обычный `PUT /fapi/v3/order` умеет менять только `LIMIT` и требует одновременно новые `quantity` и `price`. Он не является универсальным amend для STOP/TP.

Поэтому есть два варианта:

1. Обычные SL/TP: отменить старый условный ордер через `DELETE /fapi/v3/order`, затем создать новый с новым `stopPrice`.
2. Mainnet strategy order: `POST /fapi/v3/placeStrategyOrder` поддерживает `OTO`, `OCO`, `OTOCO`, а `POST /fapi/v3/updateStrategyOrder` позволяет менять sub-order, включая `quantity`, `price`, `stopPrice`, `workingType`, `reduceOnly`, `closePosition`, trailing-параметры.

Это означает, что перестановка стопа и тейка поддерживается. Но способ копирования зависит от того, как мастер создал защитные ордера: как обычные условные ордера или как strategy order.

Практические ограничения из той же спецификации:

- `placeStrategyOrder` и `updateStrategyOrder` имеют weight 50;
- `IOC` и `FOK` для strategy sub-orders не поддерживаются;
- `closePosition=true` допустим только для `STOP_MARKET`/`TAKE_PROFIT_MARKET`, закрывает всю соответствующую сторону и не может передаваться вместе с `quantity` или `reduceOnly`;
- `priceProtect` по умолчанию `false`. При `true` условный ордер сработает только если расхождение MARK и CONTRACT price не превышает `triggerProtect` символа; в волатильном рынке это может задержать защитный Stop. `workingType` и `priceProtect` нужно выбирать явно, а не считать защиту безусловно полезной.

Для чтения текущего состояния strategy order актуальный reference использует:

```text
GET /fapi/v3/strategyOpenOrder
GET /fapi/v3/strategyHistoryOrder
```

В changelog от 20 мая остались старые названия `/strategyOrder` и `/strategyOrder/history`. Проверка 19.08.2026 показала: актуальные маршруты существуют и требуют `nonce`, старые возвращают HTTP 404. В SDK нужно ориентироваться на текущий V3 reference, а не на старые имена из changelog.

`newClientOrderId` полезен для сопоставления и безопасной проверки после 503, но API гарантирует его уникальность только среди открытых ордеров; это не семидневный exchange-level idempotency key. Семидневная уникальность `clientTranId` относится к transfer endpoint. Поэтому долговременную дедупликацию финансовых операций сервис должен хранить сам.

## 3. Как следить за мастер-аккаунтом

### Правильный канал

REST polling возможен, но документация Aster прямо рекомендует WebSocket user data stream для статуса ордеров и позиций.

Последовательность:

1. подписанным `POST /fapi/v3/listenKey` получить `listenKey` мастер-аккаунта;
2. подключиться к `wss://fstream.asterdex.com/ws/<listenKey>`;
3. поддерживать listen key через `PUT /fapi/v3/listenKey` до истечения 60 минут;
4. переподключать WebSocket не позднее 24 часов;
5. после каждого разрыва сверять состояние через REST.

Основные события:

- `ORDER_TRADE_UPDATE` — создание, отмена, частичное/полное исполнение и expiration ордера. В payload есть symbol, side, type, quantity, price, average price, stop price, execution status, order ID, client order ID, last fill quantity/price, accumulated fill, `reduceOnly`, `positionSide`, `closePosition`, trailing-параметры и realized PnL.
- `ACCOUNT_UPDATE` — баланс и позиция: `positionAmt`, `entryPrice`, realized/unrealized PnL, margin type. Не приходит только из-за неисполненного или отменённого ордера, если позиция/баланс не изменились.
- `ACCOUNT_CONFIG_UPDATE` — изменение плеча, multi-assets и position mode.
- `MARGIN_CALL` — позиции и margin data в момент margin call; это предупреждение о риске, не подтверждение закрытия.
- `listenKeyExpired` — поток перестаёт отдавать user events до получения нового listen key.

### Поток относится к account, а не к одному API key

Термин «следить за ключом» неточен. `listenKey`, `allOrders`, `userTrades`, `income`, позиции и баланс относятся к account. В их параметрах нет фильтра по `signer`. Поэтому поток видит ручные действия из UI и действия всех авторизованных Agents этого account, но не сообщает надёжно «этот ордер создал именно Agent X».

Следствия:

- для маркировки собственных ордеров бота нужен контролируемый `newClientOrderId` и собственный журнал;
- отдельного официального audit-log по Agent нет;
- `TRADE_PRO` из параллельных отчётов нельзя использовать как такой audit-log: в официальном V3 reference это публичная symbol-подписка `<symbol>@tradepro`, а не персональное user-data событие;
- `GET /fapi/v3/allOrders` и `userTrades` требуют symbol и позволяют запрашивать временное окно не более 7 дней; без времени по умолчанию возвращаются последние 7 дней;
- canceled/expired ордер без единого fill исчезает из `allOrders`, когда ему больше 7 дней;
- `income` без временных параметров также возвращает recent 7-day data.

Значит, для долгого восстановления истории копитрейдер должен сохранять свои raw events/order mappings, а не рассчитывать, что биржа бесконечно хранит полный audit trail.

Публичный Aster Chain RPC не заменяет этот канал: Account Privacy скрывает чужие balances/positions, а публичного гарантированного realtime order stream произвольного wallet нет. Для собственного мастера production-источником остаётся private User Data Stream.

### Что именно копировать

Для надёжности мастер-поток лучше трактовать так:

- открытие/добавление позиции — по фактическим fills (`x=TRADE`) и подтверждённому изменению `positionAmt`;
- частичное/полное закрытие — по уменьшению позиции, а не только по `side`, особенно в Hedge mode;
- создание SL/TP — по `ORDER_TRADE_UPDATE` с соответствующим order type и `stopPrice`;
- перестановка обычного SL/TP — связка `CANCELED` старого ордера + `NEW` нового;
- изменение плеча — `ACCOUNT_CONFIG_UPDATE`;
- состояние после реконнекта — REST snapshot `positionRisk + openOrders + balance`.

### Копировать состояние, а не команды

WebSocket-событие должно быть быстрым триггером, но source of truth — итоговое состояние master account.

Например:

```text
master position: 1.0 BTC → 0.8 BTC
relative target: 80% от исходной позиции

follower position: 0.4 BTC
follower target: 0.32 BTC
close: 0.08 BTC
```

Такой state-based подход правильнее буквального «мастер нажал Close 0.2 BTC — follower закрывает 0.2 BTC», потому что абсолютные размеры аккаунтов различаются. Он также позволяет восстановиться после потерянного/дублированного события, partial fill, HTTP 503, реконнекта или перезапуска backend.

Практическая модель:

1. событие WS помечает symbol/account как требующий пересчёта;
2. state machine применяет событие и при необходимости получает REST snapshot;
3. рассчитывается желаемое follower state;
4. executor отправляет только разницу между фактическим и целевым состоянием;
5. результат подтверждается follower fill/position update.

Для нового входа и увеличения позиции target quantity определяет risk engine. Для сокращения уже открытой сделки target обычно масштабируется пропорционально изменению master position.

Нужно хранить отображение:

`master order/clientOrderId → follower account → follower order/clientOrderId`.

Без него нельзя правильно отменить или заменить конкретный стоп/тейк у каждого подписчика.

### Документированный пробел

Mainnet v3 умеет `updateStrategyOrder`, но раздел user-data WebSocket и официальный `aster-api-websocket-v3` reference не описывают отдельное событие обновления strategy order. Поэтому нельзя на основании одной документации гарантировать, что ручное изменение OCO/OTOCO в интерфейсе мастера будет однозначно видно как специальное WS-событие.

Практические варианты:

- для MVP создавать и изменять мастерские SL/TP через собственный бот — тогда все strategy IDs известны сервису;
- для обычных условных ордеров слушать cancel/new;
- после реконнекта и периодически сверять open orders;
- отдельно интеграционно протестировать ручное изменение UI strategy order на testnet/mainnet с минимальной суммой до обещания этой функции пользователям.

## 4. Расчёт размера позиции от риска на стоп

У Aster нет параметра «риск X% депозита на стоп». API принимает абсолютный `quantity`. Процентная модель реализуется в вашем risk engine.

Для линейного USDT perpetual базовая оценка:

```text
risk_budget = risk_base × risk_percent
distance = abs(expected_entry - stop_price)
quantity_raw = risk_budget / (distance + fee_and_slippage_per_unit)
quantity = floor_to_step_size(quantity_raw)
```

Где:

- `risk_base` — выбранная политика: обычно `availableBalance`, иногда wallet equity;
- `risk_percent` — например 0.01 для риска 1%;
- `expected_entry` — ожидаемая цена до сделки, затем фактическая средняя цена fill;
- fee/slippage buffer нужен, чтобы фактическая потеря не превысила лимит;
- `quantity` округляется вниз по `stepSize` из `exchangeInfo`.

После расчёта необходимо проверить:

1. `minQty`, `maxQty`, `stepSize`;
2. `minNotional`;
3. требуемую маржу примерно `quantity × entry / leverage`;
4. leverage bracket и максимальный notional;
5. что liquidation price не находится раньше stop price;
6. достаточность `availableBalance`;
7. фактический fill и реальный stop distance после проскальзывания.

### Master fill не равен follower fill

Если master вошёл по 65 000 со стопом 64 500, а follower исполнился по 65 080, расстояние до того же стопа стало 580, а не 500. Копирование исходного quantity нарушит установленный денежный риск.

Рабочий алгоритм:

1. до входа рассчитать предварительный quantity от ожидаемой цены и slippage buffer;
2. задать `max_entry_deviation`/`max_slippage` пользователя;
3. если отклонение превышено — не входить и уведомить в Telegram;
4. после fill пересчитать реальный риск по фактической средней цене follower;
5. сохранить master stop level; если риск превышен, уменьшить follower quantity до допустимого, а не молча передвигать стоп;
6. поставить/подтвердить защитный ордер и проверить итоговое follower state.

Формула допустимого количества после fill:

```text
max_quantity_after_fill =
    floor_to_step_size(
        risk_budget /
        (abs(follower_fill_price - master_stop_price) + fee_and_slippage_per_unit)
    )
```

Если допустимое количество ниже `minQty`/`minNotional`, политика должна закрыть/пропустить сделку, а не превышать риск. До подтверждения Strategy OTOCO на testnet у обычного market-entry остаётся короткое окно между fill и установкой SL; его нужно измерить отдельным тестом.

Плечо не задаёт риск на стоп. Оно в первую очередь меняет требуемую маржу и liquidation distance. Риск в деньгах определяют количество позиции и расстояние до стопа.

Если мастер переставил стоп после входа, API позволяет переставить follower stop, но решение «оставить quantity или пересчитать позицию для сохранения исходного процента риска» является политикой вашего бота.

## 5. Рекомендуемый поток MVP

```text
Master API wallet
    ↓ listenKey + user-data WebSocket
Normalizer / state machine
    ↓ master state + event/state diff
Risk engine per follower
    ↓ follower target state from balance, stop distance and actual fill
Follower executor
    ↓ signed requests for the difference: actual → target
State DB + Telegram notifications + reconciliation
```

Минимальные компоненты:

- Telegram-бот: онбординг, риск %, max leverage, max slippage, включить/выключить копирование, уведомления;
- encrypted credential storage;
- один мастерский user-data consumer;
- очередь событий с idempotency;
- отдельная последовательная очередь команд на каждого follower signer, чтобы nonce не дублировался;
- таблица соответствия master/follower order IDs;
- risk engine;
- executor и reconciliation worker;
- аварийная остановка новых входов без автоматического закрытия существующих позиций, если это отдельно не задано.

## 6. Ограничения, которые реально влияют на проект

### REST и ордера

Живой [`/fapi/v3/exchangeInfo`](https://fapi.asterdex.com/fapi/v3/exchangeInfo), проверенный 19.08.2026, вернул:

- `REQUEST_WEIGHT`: 2400 в минуту;
- `ORDERS`: 1200 в минуту на аккаунт;
- `ORDERS`: 300 за 10 секунд на аккаунт.

Документация уточняет: REST weight ограничивается по IP, order rate — по аккаунту. Поэтому один центральный сервер для большого количества подписчиков упрётся прежде всего в общий IP request-weight. Конкретные значения надо читать из `exchangeInfo`, а не хардкодить.

На момент проверки BTCUSDT имел, среди прочего:

- `minQty=0.001`, `stepSize=0.001`;
- market `maxQty=120`;
- `minNotional=5`;
- максимум 200 обычных открытых ордеров на символ;
- максимум 10 algo orders на символ.

Это снимок, а не постоянная спецификация. Для каждого symbol фильтры свои.

Дополнительно:

- `429` требует backoff; продолжение запросов приводит к IP ban `418` от 2 минут до 3 дней;
- `503` означает неизвестный результат исполнения, а не гарантированный fail: нельзя слепо повторять ордер, нужно проверять `clientOrderId`/WebSocket;
- `GET /fapi/v3/order` не является достаточным negative proof для response-lost
  MARKET: `GET /fapi/v3/allOrders` включает active/canceled/filled history. Полная
  страница из 1000 записей также не доказывает отсутствие более старого COID, поэтому
  production recovery сочетает history с сохранённым pre-position и актуальным
  `positionRisk`, а недоступность любого необходимого доказательства даёт
  `UNRESOLVED`, не retry;
- стандартный new order имеет weight 1, а place/update strategy order — weight 50;
- `nonce` должен быть уникальным для signer; сервер хранит последние 100 nonce на agent address;
- в документе есть несогласованность окна nonce: общий раздел говорит ±60 секунд, пример и registration notes — не более 10 секунд. Надёжная реализация должна синхронизировать часы и укладываться в 10 секунд;
- `newClientOrderId` уникален только среди открытых ордеров; он не заменяет долговременную идемпотентность сервиса;
- официальный base URL — `https://fapi.asterdex.com`. Некоторые Python-примеры используют `https://fapi3.asterdex.com`, но 19.08.2026 `fapi/ping` вернул HTTP 200, а `fapi3/ping` — HTTP 403. Base URL нужно держать в конфигурации и не использовать `fapi3` как production default.

### WebSocket

- входящие сообщения клиента: максимум 10/сек;
- максимум 200 streams на одно соединение;
- соединение живёт максимум 24 часа;
- listen key — 60 минут без keepalive;
- user events под нагрузкой не гарантированно приходят по порядку; нужно упорядочивать по event time `E` и делать идемпотентную обработку;
- публичного multi-account user stream в документации нет: отдельным follower-аккаунтам для полной сверки потребуются свои listen keys/соединения либо REST reconciliation.

### Функциональные границы

- нет готовой сущности «мастер → подписчики»;
- API не рассчитывает процент риска;
- quantity и цены нужно округлять самим;
- обычный STOP/TP нельзя amend через стандартный `PUT /order`;
- отдельное WS-событие strategy update не задокументировано;
- `priceProtect=true` способен задержать trigger при слишком большом расхождении MARK/CONTRACT price;
- account history не является бессрочным Agent audit-log;
- read-only поведение Agent с `canPerpTrade=false` для listenKey/USER_DATA не зафиксировано однозначно;
- follower может получить другую цену исполнения, поэтому одинаковый размер или одинаковый процент от депозита не гарантирует одинаковый фактический риск без post-fill reconciliation.

## 7. Futures testnet / демосчёт

Отдельный futures testnet существует и документирован:

- REST: `https://fapi.asterdex-testnet.com`;
- API wallet: [https://www.asterdex-testnet.com/en/api-wallet](https://www.asterdex-testnet.com/en/api-wallet);
- user-data WebSocket: `wss://fstream.asterdex-testnet.com/ws/<listenKey>`;
- EIP-712 chain ID для обычных testnet API-запросов: `714` вместо mainnet `1666`.

19.08.2026 были проверены без авторизации:

- `GET https://fapi.asterdex-testnet.com/fapi/v3/time` → HTTP 200;
- `GET https://fapi.asterdex-testnet.com/fapi/v3/ping` → HTTP 200;
- `GET /fapi/v3/exchangeInfo` → активные perpetual symbols и те же основные order types, включая STOP/TP/trailing stop.
- [Faucet](https://www.asterdex-testnet.com/en/faucet) → HTTP 200.

Testnet API содержит core-набор, нужный для MVP:

- баланс и позиции;
- MARKET/LIMIT;
- STOP/STOP_MARKET;
- TAKE_PROFIT/TAKE_PROFIT_MARKET;
- TRAILING_STOP_MARKET;
- leverage и margin mode;
- user-data WebSocket и listen key.
 - полный набор Agent/Builder-маршрутов (approveAgent/approveBuilder/updateBuilder/agent/builder/order/builder/userTrades и др.) — подтверждено живыми HTTP-проверками 20.08.2026 (см. раздел 9).

Оговорки testnet:

1. В testnet API reference нет разделов `placeStrategyOrder/updateStrategyOrder`. Однако live-проверка показала, что `strategyOpenOrder` и `strategyHistoryOrder` существуют и требуют `nonce`, а `placeStrategyOrder/updateStrategyOrder` зарегистрированы сервером. Это сильный признак parity, но успешное создание/изменение OTOCO и соответствующие WS-события нужно подтвердить подписанным testnet-тестом.
2. Документ расходится в hostname публичного market stream: указан и `fstream5.asterdex-testnet.com`, и `fstream.asterdex-testnet.com`. 19.08.2026 оба адреса приняли публичное WebSocket-соединение `btcusdt@trade`. Для user-data stream reference явно указывает `fstream.asterdex-testnet.com`; его и следует использовать по умолчанию, оставив hostname конфигурируемым.
3. [Официальная продуктовая документация Aster TestNet](https://docs.asterdex.com/aster-chain/testnet) описывает faucet: 1 000 test USDT + 1 000 test ASTER один раз в день. На текущей странице нет требования whitelist. При этом testnet пока не поддерживает пользовательские deposits, withdrawals и internal transfers, даже если некоторые transfer endpoints присутствуют в API reference.
4. Testnet `exchangeInfo` на момент проверки возвращал нестандартные sentinel-значения `-2` для части минутных rate limits; реальные тестовые лимиты не стоит переносить в production-конфигурацию.

## 8. Обязательный signed testnet-план

Публичные endpoints и WebSocket hostnames проверены без credentials. Следующие проверки требуют созданных testnet master/follower API wallets и должны быть выполнены перед production-разработкой:

1. создать отдельные master и follower agents с `canPerpTrade=true`, `canWithdraw=false`;
2. получить balance/position/open-orders snapshot обоих аккаунтов;
3. открыть master MARKET и скопировать follower MARKET;
4. получить actual follower fill и пересчитать реальный риск;
5. проверить skip при превышении `max_entry_deviation`;
6. полностью закрыть позицию в One-way mode через `reduceOnly`;
7. частично закрыть 25%/30% позиции в One-way mode;
8. отдельно проверить частичное закрытие в Hedge mode без `reduceOnly`, с правильным `positionSide`;
9. создать обычные STOP_MARKET и TAKE_PROFIT_MARKET;
10. переставить обычный SL/TP через cancel+new;
11. создать OTOCO через `placeStrategyOrder`;
12. изменить SL и TP через `updateStrategyOrder`;
13. вручную изменить Strategy SL/TP в Aster UI и записать все user-data WS payloads;
14. проверить, видны ли strategy sub-orders в обычном `openOrders` и после реконнекта;
15. воспроизвести partial fill и проверить target-state reconciliation;
16. проверить `listenKeyExpired`, принудительный WebSocket disconnect и восстановление snapshot;
17. повторно подать одно событие и событие с меньшим `E`, убедиться в идемпотентности;
18. перезапустить backend с открытой позицией и восстановить master/follower mapping;
19. проверить недостаточную маржу, `minQty`, `minNotional`, max leverage bracket;
20. проверить отказ/коррекцию сделки при большом follower slippage.
21. создать Agent с `canPerpTrade=false` и проверить `balance`, `positionRisk`, `listenKey` и user events;
22. проверить `priceProtect=false/true` и фактическое поведение `triggerProtect` на условном ордере;
23. проверить два signing contexts: trading `1666/714` и registration `signatureChainId=56/101`.
24. создать Builder (регистрация + 100 test ASTER), approveAgent с builder одной подписью, approveBuilder с maxFeeRate;
25. placeOrder с builder+feeRate от имени пользователя и проверить появление builderFee в `builder/userTrades`;
26. проверить обновление/отзыв Builder (`updateBuilder`/`DELETE /fapi/v3/builder`) и отображение в `GET /fapi/v3/builder`;
27. повторить пункты 24–26 на spot (`/api/v3/approveBuilder`, `DELETE /api/v3/delBuilder`), если spot-направление нужно.

Критерий прохождения: после каждого сценария фактические follower position и защитные ордера совпадают с рассчитанным target state, а повторная reconciliation не создаёт новых лишних команд.

## 9. Builder (Aster Code) — вывод по deep-dive


### Что такое Builder / Aster Code

[Продуктовая документация](https://docs.asterdex.com/program-and-rewards/aster-code) описывает Aster Code именно как **broker-экосистему**: билдер строит свой торговый UI поверх инфраструктуры Aster и получает комиссию (builder fee) с каждой сделки пользователя, прошедшей через его интерфейс. Требования к билдеру:

- зарегистрироваться на Aster под Web3-кошельком;
- держать минимум **100 ASTER** в Futures-аккаунте (для spot-направления — 100 ASTER в Spot-аккаунте; требования независимы);
- максимально допустимый `maxFeeRate` для Futures — **0.001** (0.1%), для Spot — **0.01** (1%);
- builder fee не начисляется на ADL и ликвидационные ордера.

### Как выглядит флоу (Futures Integration Flow)

Официальный [Futures Integration Flow](https://asterdex.github.io/aster-api-website/asterCode/integration-flow/) фиксирует последовательность:

1. **Builder preparation.** Билдер готовит свой builder address, регистрируется, держит 100 ASTER. Для каждого пользователя билдер генерирует API Wallet / Agent (`signer` address + private key); private key хранится на backend билдера (рекомендация — один signer на пользователя).
2. **Пользователь авторизует Agent.** `POST /fapi/v3/approveAgent`, подпись основным wallet пользователя; задаются права (spot/perp/withdraw), IP whitelist, срок.
3. **Пользователь авторизует Builder.** `POST /fapi/v3/approveBuilder`, подпись основным wallet; задаётся `maxFeeRate` (кап). Может выполняться одновременно с approveAgent (в одном запросе поля `builder`/`maxFeeRate`/`builderName`).
4. **Builder торгует от имени пользователя.** Пользователь инициирует действие (например, нажимает «Place Order»), backend билдера вызывает `POST /fapi/v3/order` с параметрами `builder` и `feeRate`, подписывая запрос приватным ключом signer.
5. **Поддержка.** `updateAgent` / `DELETE /agent`; `updateBuilder` / `DELETE /builder`.

### Ключевой вывод: передавать личный ключ пользователю не нужно

В модели Builder пользователь **не передаёт** своему сервису private key основного wallet и не вводит API-ключ. Вместо этого:

- пользователь **один раз** подписывает EIP-712 `ApproveAgent` (+ `ApproveBuilder`) своим основным wallet (management signing context, chainId `56` для EVM / `101` для Solana; domain name `AsterSignTransaction`);
- signer/API-wallet **создаёт сам билдер** и хранит его private key у себя на backend;
- все последующие торговые вызовы подписываются этим signer private key (trading context, chainId `1666` mainnet / `714` testnet), и в каждый ордер добавляются `builder` + `feeRate`.

Это подтверждается и исходниками официального [aster-code-demo](https://github.com/asterdex/API-demo/tree/main/aster-code-demo): `01_approveAgent.js` → `05_approveBuilder.js` → `09_placeOrder.js`, где `placeOrder` подписан `SIGNER_PRIVATE_KEY`, а в `env.example` отдельно лежат `MAIN_PRIVATE_KEY` (только для разрешений) и `SIGNER_PRIVATE_KEY` (для торговли). Фронтенд-demo ([aster-code-builder-demo](https://github.com/jupiter-hongc/aster-code-builder-demo)) показывает UX: подключение кошелька, подпись ApproveAgent/ApproveBuilder через wagmi.

### Полный набор эндпоинтов (Futures)

[Endpoints](https://asterdex.github.io/aster-api-website/asterCode/endpoints/):

```text
POST   /fapi/v3/approveAgent      — авторизация Agent (можно сразу с builder)
POST   /fapi/v3/updateAgent       — обновление прав/срока/IP
DELETE /fapi/v3/agent             — отзыв Agent
GET    /fapi/v3/agent             — список Agents
POST   /fapi/v3/approveBuilder    — авторизация Builder с maxFeeRate
POST   /fapi/v3/updateBuilder     — изменение maxFeeRate
DELETE /fapi/v3/builder           — отзыв Builder
GET    /fapi/v3/builder           — список approved builders
POST   /fapi/v3/order             — ордер от имени пользователя с builder+feeRate
```

Дополнительно в V3:

- `GET /fapi/v3/builder/userTrades` — сделки пользователей под builder code (`userAddress`, `orderId`, `realizedProfit`, `builderFee`);
- `GET /fapi/v3/builder/approvedUserList` — пользователи, одобрившие builder (`userAddress`, `builderAddress`, `maxFeeRate`, `builderName`, `approveTime`); с 16.07.2026 ответ пагинирован (`rows[]`, `total`, `currentPage`, `totalPages`, `pageSize`, `hasMore`);
- `placeStrategyOrder`/`updateStrategyOrder` принимают опциональные `builder` + `feeRate` (добавлены 30.06.2026).

### Testnet: Builder работает 1:1

Futures testnet базируется на том же `https://fapi.asterdex-testnet.com` (chainId `714` для торговой подписи). Прямые HTTP-проверки 20.08.2026 (без подписи, поэтому приходит ошибка валидации параметров, а не 404):

```text
GET  /fapi/v3/builder                  → 400 (валидация параметров)
GET  /fapi/v3/agent                    → 400
GET  /fapi/v3/builder/userTrades       → 400
GET  /fapi/v3/builder/approvedUserList → 400
POST /fapi/v3/approveBuilder           → 400 (-1102: нужен nonce)
POST /fapi/v3/updateBuilder            → 400 (-1102)
POST /fapi/v3/approveAgent             → 400 (-1102)
POST /fapi/v3/placeStrategyOrder       → 400 (-1102)
DELETE /fapi/v3/builder                → 400 (ошибка времени устройства)
```

То есть все Agent- и Builder-маршруты зарегистрированы на futures testnet. Требование 100 ASTER на testnet закрывается faucet: [Aster TestNet faucet](https://www.asterdex-testnet.com/en/faucet) выдаёт **1 000 test USDT + 1 000 test ASTER** раз в день.

**Важное различие UI vs API (проверено 21.08.2026):** в веб-интерфейсе тестнета `www.asterdex-testnet.com` **нет вкладки Aster Code / Builder** — пути `/en/aster-code` и `/en/builder-dashboard` отдают 404, в навигации тестнета только Trade / Faucet / Explorer / Agent wallets / Human VS AI. Эти вкладки существуют на mainnet (`www.asterdex.com/en/aster-code` = Builder Center, `/en/builder-dashboard`). При этом все Builder-REST-маршруты на `fapi.asterdex-testnet.com` живые (повторная проверка 21.08.2026 вернула 400/-1102 по `/fapi/v3/builder`, `/builder/userTrades`, `/builder/approvedUserList`, `/approveBuilder`, `/updateBuilder`, `/approveAgent`, `/order`). Итог: для подписанного прогона билдера на тестнете веб-вкладка не нужна — авторизация идёт подписью вашего бэкенда/демо-фронтенда через API.
### Спот-аналог

Spot использует те же принципы, но другие endpoint-ы: `POST /api/v3/approveBuilder`/`updateBuilder`, revoke через `DELETE /api/v3/delBuilder` (это другой путь, чем Futures `DELETE /fapi/v3/builder`), `POST /api/v3/order` с `builder`+`feeRate`, кап `maxFeeRate = 0.01`. Авторизация — также двумя способами: в одном запросе `approveAgent` с `spotBuilder`/`maxSpotFeeRate`, либо отдельно `POST /api/v3/approveBuilder`.

### Вердикт: Builder лучше для заявленной схемы?

**Да, для онбординга пользователей Aster Code выгоднее, чем требовать от них создания API-ключа.**

- Пользователю не нужно копировать/хранить/вводить API-ключ или личный private key — он один раз подписывает ApproveAgent + ApproveBuilder в кошельке (или через WebApp-страницу).
- Билдер сам генерирует и хранит signer-ключи, полностью контролирует торговлю от имени пользователя, а кап `maxFeeRate` защищает пользователя от произвольной комиссии.
- Встроенная монетизация (builder fee) — официально поддерживаемый механизм.
- Весь трейдинг идёт по тем же V3-эндпоинтам, поэтому ни одна из проверенных в этом отчёте возможностей (SL/TP, trailing, частичное закрытие, плечо, баланс, user-data stream) не теряется.

Важные оговорки:

- Aster Code **не заменяет** логику копирования: слежение за мастер-аккаунтом по-прежнему делается через user-data WebSocket мастера, а исполнение на follower-аккаунтах — ордерами от их signer. Builder решает только вопрос авторизации/комиссии, а не сам копитрейд-маппинг.
- Билдер должен соответствовать требованиям (регистрация + 100 ASTER) — это его собственное требование, а не пользователей; на testnet закрывается faucet.
- Для подписанного end-to-end прогона (approveAgent + builder одной подписью, ордер с feeRate, появление builderFee в userTrades) всё готово на testnet; его стоит включить как первый signed testnet-сценарий.


## 10. Итоговый вердикт по хотелкам

| Хотелка | Вердикт |
|---|---|
| Открывать сделки подписчиков по действиям мастера | Да. |
| Видеть ручные открытия/закрытия мастера | Да, через account user-data WebSocket. |
| Копировать частичное сокращение позиции | Да, по position delta/fills. |
| Ставить стоп и тейк | Да. |
| Переставлять обычный стоп/тейк | Да, cancel+new. |
| Переставлять OCO/OTOCO strategy order | API mainnet умеет; мониторинг ручного UI-update требует отдельного интеграционного теста. |
| Читать депозит/баланс/доступную маржу | Да. |
| Изолировать copy-средства отдельным sub-account | Да на mainnet: отдельные assets/positions/V3 keys, withdrawals у ключей постоянно запрещены. Testnet parity не подтверждена. |
| Ставить плечо и margin mode | Да. |
| Считать риск как % депозита на стоп | Да, но расчёт делает ваш бот, не Aster. |
| Запустить всё сначала как Telegram-бот | Да; v3-онбординг требует три credential values и безопасное хранилище. |
| Проверить без реальных денег | Да: futures testnet и faucet 1 000 test USDT + 1 000 test ASTER раз в день. |
| Онбординг пользователя без передачи API-ключа | Да — лучший вариант: Aster Code / Builder (пользователь однократно подписывает ApproveAgent + ApproveBuilder, signer создаёт билдер). |
| Builder-авторизация полностью на testnet | Да — все Builder/Agent-маршруты зарегистрированы на fapi.asterdex-testnet.com (проверено 20.08.2026): approveAgent/approveBuilder/updateBuilder/order/builder/* живые. |
| Монетизация копитрейдера | Да — builder fee (Futures кап maxFeeRate 0.001, Spot 0.01), начисление на исполненные ордера, вывод через Builder Center. |

Вывод: препятствий со стороны core Aster API для первого копитрейд-MVP нет. Для онбординга пользователей предпочтителен Aster Code / Builder (не требует передачи личного ключа) поверх той же V3. Test funds подтверждены. Главный оставшийся технический пробел — точное поведение user-data stream при ручном изменении strategy OCO/OTOCO; дополнительно подписанным testnet-прогоном нужно подтвердить фактическую Strategy parity и первый end-to-end Builder-прогон (approveAgent+builder, order с feeRate, builderFee в userTrades).

## Официальные источники

- [Aster API Docs — индекс и статус V3/V1](https://github.com/asterdex/api-docs/blob/master/README.md)
- [Aster Futures API V3](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-v3.md)
- [Aster Futures Testnet API V3](https://github.com/asterdex/api-docs/blob/master/V3%28Recommended%29/EN/aster-finance-futures-api-testnet.md)
- [Aster API Changelog](https://github.com/asterdex/api-docs/blob/master/CHANGELOG.md)
- [Aster TestNet и тестовые средства](https://docs.asterdex.com/aster-chain/testnet)
- [Aster Testnet Faucet](https://www.asterdex-testnet.com/en/faucet)
- [Aster Pro API Wallet](https://www.asterdex.com/en/api-wallet)
- [Aster Agent management endpoints](https://asterdex.github.io/aster-api-website/asterCode/endpoints/)
- [Aster Code product docs](https://docs.asterdex.com/program-and-rewards/aster-code)
- [Aster Code Futures Integration Flow](https://asterdex.github.io/aster-api-website/asterCode/integration-flow/)
- [Aster Code Futures Authentication](https://asterdex.github.io/aster-api-website/asterCode/authentication/)
- [Aster Code Spot Integration Flow](https://asterdex.github.io/aster-api-website/asterCode/spot-integration-flow/)
- [Aster Code API demo (github asterdex/API-demo/aster-code-demo)](https://github.com/asterdex/API-demo/tree/main/aster-code-demo)
- [Aster Code Builder frontend demo](https://github.com/jupiter-hongc/aster-code-builder-demo)
- [Aster V3 Deposit & Withdrawal](https://asterdex.github.io/aster-api-website/futures-v3/deposit%26withdrawal/)
- [Aster Sub Accounts](https://docs.asterdex.com/trading/sub-accounts)
- [Aster legacy API management](https://www.asterdex.com/en/api-management)
- [Aster Testnet API Wallet](https://www.asterdex-testnet.com/en/api-wallet)
- [Aster Skills Hub](https://github.com/asterdex/aster-skills-hub)
- [Live mainnet exchangeInfo](https://fapi.asterdex.com/fapi/v3/exchangeInfo)
- [Live testnet exchangeInfo](https://fapi.asterdex-testnet.com/fapi/v3/exchangeInfo)

### 2026-08-19 — завершение

- Встроенным веб-поиском найдены актуальный официальный репозиторий, V3 mainnet/testnet, API Wallet и legacy notice.
- Firecrawl извлёк полные mainnet/testnet документы, страницы управления ключами и официальный WebSocket reference.
- Публичные V3 mainnet/testnet `ping`, `time` и `exchangeInfo` проверены прямыми GET-запросами.
- Параллельный отчёт изучен; подтверждённые дополнения включены, универсальное применение `reduceOnly` к Hedge mode отвергнуто.
- Подтверждены faucet, текущие Strategy/Builder routes, оба testnet market-WS hostnames и нерабочий `fapi3` из Python-примеров.
- Полностью прочитаны девять новых отчётов из `reports/`; их фактологическое сравнение сохранено в `REPORTS_FACTUAL_COMPARISON_2026-08.md`.
- Из параллельных материалов приняты только подтверждённые детали Agent lifecycle/signing, account-scoped history, Strategy/Stop constraints, withdrawals и sub-accounts; ошибочные старые Strategy routes, `TRADE_PRO` как user event, universal Agent-withdraw protection и семидневный order idempotency не перенесены.
- Все вопросы из исходного todo закрыты; действия, требующие подписанных testnet credentials, вынесены в отдельный acceptance checklist.

### 2026-08-20 — Builder deep-dive завершён

- Изучены Aster Code integration flow (Futures/Spot), authentication, endpoints, changelog 2026 и оба официальных demo (API-demo/aster-code-demo и aster-code-builder-demo).
- Живыми HTTP-проверками подтверждено: все Agent/Builder-маршруты зарегистрированы на futures testnet (fapi.asterdex-testnet.com), 1:1 с mainnet.
- Модель установлена: пользователь не передаёт личный ключ — подписывает ApproveAgent + ApproveBuilder, а signer-ключи создаёт и хранит билдер.
- Главный отчёт обновлён: новый раздел 9 «Builder (Aster Code)», строки в матрице и в итоговом вердикте, testnet-пункт 24–27 в signed-плане, источники и журнал.

### 2026-08-21 — аудит полноты данных

- Сверены все пункты исходного запроса (V3-версия/ключи, баланс/плечо/проценты, открытие/закрытие, перестановка SL/TP, слежение за мастером через socket, сокращение позиции, риск % от депозита, Telegram-бот, testnet, Builder) с содержимым этого отчёта — каждый пункт покрыт разделом и статусом в матрице/вердикте.
- По информации Астера дополнительные данные не требуются. Единственные открытые пункты — не «недостающая информация», а проверки, требующие подписанных тестнет-ключей и выполнения acceptance-списка из раздела 8 (в т.ч. поведение user-data stream при ручной перестановке Strategy OCO/OTOCO и end-to-end Builder-прогон).

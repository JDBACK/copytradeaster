# UX brief Stage 2: Telegram inline control surface

Дата: 2026-08-23  
Статус: промежуточный shape-brief; готов к correction/approval перед кодом  
Режим: Operate  
Поверхность: private Telegram chat; будущий Mini App использует те же use cases

Текущая product boundary (уточнение 2026-08-24): follower меняет только
`risk_percent`, получает notifications и управляет своим pause/resume/account
lifecycle. Leverage автоматически выбирается как максимум для symbol и не
редактируется; entry deviation — внутренний safety gate. Follower emergency
отсутствует в UI/application boundary, global emergency — admin-only. Stage 2
пока не меняет `marginType`; isolated enforcement отложен до отдельной реальной
проверки Aster testnet.

## 1. Job и аудитория

Фолловер приходит не «общаться с ботом», а быстро ответить на четыре вопроса:

1. Aster account подключён и разрешения валидны?
2. Копирование сейчас включено, на паузе или заблокировано?
3. Какой риск установлен?
4. Требуется ли от пользователя действие?

Основной пользователь не должен помнить slash-синтаксис, понимать названия
внутренних модулей или читать журнал команд. Оператору нужен отдельный слой
глобального контроля, не смешанный с пользовательским меню.

Успех: за один экран понять состояние и выполнить следующий безопасный шаг.

## 2. Выбранное направление

**Одна тихая контрольная поверхность.** Бот поддерживает одно главное
редактируемое сообщение: текст — текущий экран, inline keyboard — доступные
действия. Навигация обновляет это сообщение через `edit_text`/
`edit_reply_markup`; новые сообщения создаются только для важных уведомлений и
неизбежного пользовательского ввода.

Структурный тезис:

- состояние выше функций;
- одна контекстная primary action;
- вторичные действия открываются progressive disclosure;
- опасные действия никогда не находятся на dashboard;
- Home/Back предсказуемы и не зависят от chat history;
- Telegram остаётся нативным Telegram, а не имитацией маленького сайта.

Mini App позже повторяет не Telegram screens, а общий набор use cases и
business DTO. У него будет собственный presenter/layout.

## 3. Общая application boundary для Bot и Mini App

```text
Telegram handlers/presenter ─┐
                             ├─> UserApplicationService -> core/repos
Mini App HTTP/presenter ─────┘
```

Application service не принимает `Message`, `CallbackQuery`, `Bot`,
`FSMContext`, HTTP request или Mini App component.

Минимальные use cases:

```text
dashboard(user_id)
begin_onboarding(user_id)
connect_account(user_id, direct_credentials)
status(user_id, refresh=False)
risk(user_id)
update_risk(user_id, values)
pause(user_id)
resume(user_id, confirmation)
activity(user_id, cursor)
notification_settings(user_id)
set_notifications(user_id, enabled)
begin_confirmation(user_id, action)
confirm_action(user_id, token)
admin_dashboard(admin_id)
```

Service возвращает typed business DTO. Telegram presenter строит
`TelegramMenuView`; будущий Mini App presenter строит JSON/UI model. Нельзя
класть Telegram `Screen`, keyboard rows или HTML в application DTO.

## 4. Primary navigation

Slash-команды — только вход/recovery shortcuts:

| Команда | Назначение |
|---|---|
| `/start` | открыть onboarding, если account нет; иначе dashboard |
| `/menu` | открыть свежий dashboard |
| `/cancel` | завершить временный ввод и вернуться на dashboard |
| `/help` | открыть help screen |

`/connect`, `/status`, `/risk`, `/pause`, `/resume`,
`/disconnect` и `/reconcile` не являются основным UI и не публикуются в
командном меню.

Stage 2 использует `MenuButtonCommands`. Когда появится Mini App,
`MenuButtonWebApp` может стать системной кнопкой кабинета, но inline dashboard
останется доступен через `/menu` и сообщения бота.

## 5. Dashboard

### Подключён, копирование активно

```text
Aster Copy

🟢 Всё работает
Копирование: включено
Счёт: 0x12…ABCD
Риск на стоп: 1,0%
Плечо: автоматически по активу
Открытые позиции: 2

Обновлено в 12:41
```

Клавиатура:

```text
[ ⏸ Поставить на паузу ]
[ ⚙️ Риск ] [ 📊 Статус ]
[ 🕘 Активность ] [ 🔔 Уведомления ]
[ ❓ Помощь ]
```

### Подключён, новые входы на паузе

```text
Aster Copy

⏸ Новые входы на паузе
Закрытия, сокращения и защита продолжают работать.

Счёт: 0x12…ABCD
Риск на стоп: 1,0%
Плечо: автоматически по активу
Открытые позиции: 1

Обновлено в 12:41
```

Primary action меняется на:

```text
[ ▶️ Возобновить копирование ]
```

### Account не подключён

```text
Aster Copy

Аккаунт пока не подключён.
Подключите выделенный Aster testnet account, затем проверьте риск и включите
копирование.
```

```text
[ 🔐 Подключить Aster ]
[ Как это работает ] [ Безопасность ]
```

### Degraded/stale

```text
Aster Copy

🟠 Нужна проверка
Последнее подтверждённое состояние получено 4 минуты назад.
Новые опасные действия временно недоступны.

Показаны последние сохранённые данные.
```

```text
[ ↻ Проверить снова ]
[ 📊 Подробный статус ] [ ❓ Что произошло ]
```

Статус никогда не обозначается только цветом/emoji: рядом всегда есть текст.

## 6. Screen map

```text
Dashboard
├── Status
│   └── Refresh Aster
├── Risk
│   └── Edit risk flow
├── Activity (5–10 rows/page)
├── Notifications
├── Help
│   ├── How copying works
│   ├── Pause vs emergency
│   └── Credential safety
├── Account
│   ├── Replace credentials
│   ├── Emergency -> Confirm
│   └── Disconnect -> Confirm
└── Admin (admins only)
    ├── Accounts
    ├── Runtime/outbox
    ├── Reconcile
    ├── Global pause -> Confirm
    └── Global emergency -> Confirm
```

Обычная глубина — не больше двух переходов от Dashboard. Onboarding —
исключение, потому что последовательный ввод неизбежен.

## 7. Onboarding flow

```text
Intro/consent
 -> user address
 -> signer address
 -> private key
 -> checking
 -> success (paused)
```

Label пользователь не вводит: он не должен придумывать внутренний ID. Stable
account ID выводится из Telegram user ID, display label — из masked address.

### Intro

```text
Подключение Aster testnet

Понадобятся user address, signer address и private key signer-кошелька.
Main wallet key не нужен.

Private key будет зашифрован. Сообщение с ним мы удалим после обработки, но
Telegram не гарантирует исчезновение из уведомлений, скриншотов и кэша.
Этот способ предназначен только для testnet.
```

```text
[ Понятно, продолжить ]
[ Назад ]
```

### Address steps

Главное menu message редактируется в prompt. Пользователь присылает значение
отдельным сообщением; после принятия bot по возможности удаляет input и
редактирует anchor на следующий шаг.

```text
Шаг 1 из 3
Пришлите user address одним сообщением.
```

```text
[ Отмена ]
```

На signer step доступны `Назад` и `Отмена`; хранить можно только non-secret
addresses.

### Private key step

```text
Шаг 3 из 3
Пришлите private key signer отдельным сообщением.

Мы не покажем его в ответе и удалим сообщение best effort.
```

Private key не попадает в FSM, callbacks, menu view, outbox или logs.

### Checking

```text
Проверяем подключение…

• signer и ключ
• права perpetuals
• запрет вывода
• срок действия и IP whitelist
• состояние счёта
```

Кнопки временно снимаются, чтобы нельзя было запустить второй flow.

### Success

```text
✅ Aster подключён

Счёт: 0x12…ABCD
Perpetuals: разрешены
Вывод: запрещён
IP whitelist: подтверждён

Копирование остаётся на паузе. Проверьте риск перед запуском.
```

```text
[ ⚙️ Проверить риск ]
[ ▶️ Начать копирование ]
[ На главную ]
```

## 8. Risk screen

```text
Риск

Риск на стоп: 1,0%
Максимальное плечо: 3×
Допустимое отклонение входа: 2,0%

Размер позиции рассчитывает торговое ядро по фактическому балансу и Stop.
```

```text
[ Изменить риск ]
[ Изменить плечо ] [ Изменить отклонение ]
[ Назад ] [ На главную ]
```

Частые значения показываются inline presets; «Другое значение» открывает один
text-input step. Save подтверждает итоговый summary. `Decimal`, никогда float.

## 9. Status, activity и notifications

### Status

Показывает понятные пользователю группы:

```text
Система: работает / нужна проверка
Копирование: включено / пауза / заблокировано
Aster: подключён / проверка устарела / ошибка
Разрешения: perpetuals да · withdrawal нет · whitelist да
Счёт: masked address
Позиции: count + safe summary
Последняя проверка: timestamp/age
```

Кнопка `Обновить Aster` запускает read-only refresh и временно показывает
loading state в том же сообщении.

### Activity

- 5–10 событий на странице;
- время, понятный action и result;
- никаких private keys, raw payloads, command hashes или stack traces;
- пагинация `◀`/`▶` появляется только когда нужна.

### Notifications

```text
Уведомления: включены
Последняя доставка: 12:41
Неразрешённых ошибок доставки: нет
```

Один toggle; сложные per-event preferences в Stage 2 не нужны.

## 10. Pause, resume и account lifecycle

### Pause

One-tap idempotent action:

```text
⏸ Новые входы выключены.
Закрытия, сокращения и защитные действия продолжаются.
```

### Resume

Если checks пройдены и master flat — обычное подтверждение действия. Если
master уже в позиции, отдельный экран объясняет, что targeted reconcile может
открыть follower по текущей цене и deviation policy.

### Emergency boundary

Follower emergency не публикуется в Telegram/Mini App и отвергается на
application boundary, включая forged confirmation. Внутренний per-account
control-core сохраняется для operator workflows; global emergency доступен
только admin.

### Disconnect

```text
Отключить счёт Aster?

Сначала проверим, что счёт на паузе, позиции закрыты, выставленные ботом заявки
и незавершённые операции отсутствуют. Зашифрованные данные подключения будут
удалены; журнал без секретов сохранится.
```

### Администрирование

Один immutable Telegram ID из env получает admin capability и одновременно
может иметь собственный follower account. Отдельный Telegram-профиль для
админа не требуется.

```text
Администрирование

Источник копирования: 0x12…ABCD
Система: работает
Счетов подписчиков: 12
Глобальная пауза: нет
Уведомления: ожидают 0, ошибок 0
```

```text
[ 👥 Пользователи ]
[ 📋 Счета подписчиков ]
[ ⏸ Глобальная пауза ]
[ 🚨 Закрыть все позиции ]
[ ↻ Перепроверить работу ]
```

Неизвестный пользователь получает собственный numeric Telegram ID. Админ
вводит этот ID через одношаговый `StatesGroup`, видит понятное подтверждение и
выдаёт обычный пользовательский доступ. Админские права через Telegram не
выдаются. Доступ можно отозвать независимо от уведомлений и торговли:
существующие позиции продолжает вести core. Users/followers показываются
страницами по 10; targeted reconcile использует стабильный opaque hash account
ID, а не индекс строки.

Confirmation uses short opaque single-use token with TTL. Token привязан к
user/chat/message/action и повторно проверяется application layer. После
restart старый token недействителен — это безопасно.

## 11. Inline interaction contract

- 1–2 buttons в строке, обычно не больше 6–8 actions на screen;
- primary action — отдельная первая строка;
- Back/Home — последняя строка;
- не использовать ReplyKeyboard;
- callback payload — short ASCII action, revision и optional opaque token;
- callback budget проверяется в UTF-8 bytes, максимум 64;
- user account для обычного flow выводится по owner, не передаётся в callback;
- admin account передаётся stable short opaque key, никогда page/index;
- каждый callback немедленно вызывает `answer()`;
- mutation callback сначала открывает confirmation, второй расходует token;
- после любого action данные читаются заново и screen перерисовывается;
- per-message asyncio lock предотвращает out-of-order edits при double tap;
- stale revision не выполняет action: показывается свежий screen;
- malformed/stale fallback зарегистрирован последним и тоже отвечает callback;
- `Message`, `InaccessibleMessage`, `None` и inline-message cases обрабатываются
  явно; private-only business flow не доверяет inline/group context;
- no-op `message is not modified` игнорируется узко, остальные
  `TelegramBadRequest` не скрываются.

Технические notifications отправляются `parse_mode=None`. HTML screens
экранируют любой внешний текст. Целевой screen text — до 800 символов;
Telegram limit 4096 остаётся hard guard, а не design target.

## 12. Scenes Wizard — обязательный verdict

Изучены актуальные `Scene`, `SceneRegistry`, `ScenesManager`, `SceneWizard`,
`After`, `goto/back/retake`, enter/leave/exit actions и history в aiogram
3.30.0.

**Решение: Scenes в Stage 2 не использовать.**

Причины:

1. Официальная документация всё ещё помечает Scenes как experimental.
2. Scene — временное FSM-пространство, а не menu/navigation engine.
3. History ограничена 10 snapshots и хранит FSM data; secret туда попадать не
   должен.
4. `exit()` не очищает обычные FSM data автоматически.
5. `reset_history_on_enter` легко ломает Back.
6. Enter action зависит от типа текущего update: message и callback требуют
   разных handlers.
7. Parent/global handlers и Scene routers чувствительны к registration order;
   `/cancel` и fallback требуют дополнительных tests.
8. Наш onboarding имеет три линейных input steps без ветвления; обычный
   `StatesGroup` короче, понятнее и безопаснее.
9. Risk editing — presets + максимум один text step, Scene не окупается.

Main dashboard всегда stateless view + fresh render. Onboarding/risk используют
`StatesGroup` и `FSMContext` только для non-secret ephemeral data, с
`SimpleEventIsolation`.

Scenes можно пересмотреть, если одновременно появятся реальные признаки:

- минимум три ветвящихся wizard flows;
- переходы между flow с общей Back history;
- повтор/repair отдельных шагов;
- общие enter/leave/cleanup hooks заметно сокращают существующий код;
- experimental status снят либо польза доказана targeted prototype.

Даже тогда первая область пилота — onboarding, никогда не dashboard/menu.

Официальные источники:

- [Scenes Wizard](https://docs.aiogram.dev/en/v3.30.0/dispatcher/finite_state_machine/scene.html)
- [aiogram Scene source](https://github.com/aiogram/aiogram/blob/v3.30.0/aiogram/fsm/scene.py)
- [CallbackData](https://docs.aiogram.dev/en/v3.30.0/dispatcher/filters/callback_data.html)
- [Keyboard builder](https://docs.aiogram.dev/en/v3.30.0/utils/keyboard.html)
- [CallbackQuery](https://docs.aiogram.dev/en/v3.30.0/api/types/callback_query.html)
- [setChatMenuButton](https://docs.aiogram.dev/en/v3.30.0/api/methods/set_chat_menu_button.html)

## 13. States and failure responses

| State | Поведение |
|---|---|
| Empty | один CTA, без пустой таблицы |
| Loading | то же сообщение, конфликтующие buttons сняты |
| Success | короткий итог + следующий полезный шаг |
| Validation error | показать конкретное исправимое поле, сохранить только non-secret progress |
| Aster unavailable | `Повторить`/`На главную`; persistence не меняется |
| Stale | age + warning; не обозначать как READY |
| Permission denied | нейтральный отказ без существования чужого account |
| Expired callback | toast + fresh dashboard |
| Degraded/emergency | disable/hide unsafe actions, объяснить причину |
| Telegram edit lost | отправить новый anchor menu, старый callback инвалидировать |
| Bot blocked | notifications off для recipient; core продолжает работу |

## 14. Anti-goals

- slash-first UI;
- dashboard из длинного технического отчёта;
- Scene на каждый screen;
- ReplyKeyboard и постоянная клавиатура, закрывающая ввод;
- одинаковые опасные и обычные buttons;
- больше двух уровней обычной навигации;
- callback JSON/full address/account label/private data;
- скрытые действия после stale callback;
- Telegram-specific business logic, которую потом придётся переписывать для
  Mini App;
- попытка заранее проектировать визуальный мир Mini App в рамках Telegram UX.

## 15. Acceptance UX checks

- Автоматические contract/rendering/security checks отмечены ниже; первые два
  perceptual пункта остаются за ручной owner acceptance в реальном Telegram.

- [ ] новый пользователь видит один CTA и понимает testnet/security boundary;
- [ ] connected пользователь за один screen видит system/copy/risk/action;
- [x] все основные функции доступны через inline buttons;
- [x] опубликованы только `/start`, `/menu`, `/cancel`, `/help`;
- [x] каждый screen имеет понятные Back/Home;
- [x] primary action одна и зависит от состояния;
- [x] пользовательский pause не смешан с admin-only emergency boundary;
- [x] callback `pack()` проверен на 64/65 UTF-8 bytes и Unicode;
- [x] malformed/stale/double-click/inaccessible callbacks безопасны;
- [x] private key отсутствует в FSM/view/callback/outbox/log/reply;
- [x] dashboard/risk/status/activity доступны через общий application service;
- [x] один operator ID видит inline users/followers, выдаёт доступ и запускает
  адресную пересверку без отдельного admin Telegram account;
- [x] Mini App не требует новой реализации ownership/risk/control rules;
- [x] Scenes отсутствуют из production dependency path и не создаются ради
  меню; решение пересматривается только по критериям §12.

## 16. Product-boundary correction — 2026-08-24

Текущая пользовательская поверхность намеренно ограничена исходной целью
малого бота:

- follower управляет только риском на Stop (`risk_percent`), notifications,
  собственными pause/resume и account lifecycle;
- leverage в risk screen объясняется как «автоматически, максимум для актива»;
  edit callbacks/parsing для leverage и entry deviation удалены;
- account screen не содержит follower emergency. Per-account emergency
  остаётся внутренним control-core, а global emergency — admin-only;
- onboarding напоминает включить Hedge Mode и при создании Agent/API wallet
  выбрать Perps/`canPerpTrade`; Stage 2 не меняет `marginType` аккаунта.
  Isolated enforcement отложен до отдельной реальной проверки Aster testnet;
- dashboard не обещает фиксированное плечо вроде `20x`. Full trading cycle и
  resume/copy flow не считаются проверенными только по UI/regression tests.

Текущий фактический UI/readiness verdict после owner-проверки ведётся в
[STAGE2_CURRENT_STATE_AND_READINESS_2026-08.md](STAGE2_CURRENT_STATE_AND_READINESS_2026-08.md).

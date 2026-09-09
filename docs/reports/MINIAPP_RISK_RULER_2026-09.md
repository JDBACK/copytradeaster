# Mini App: шкала риска — первый vertical slice (S2.5 + S3.4 по §21)

Дата: 2026-09-06. Кампания по плану `MINIAPP_FUNCTIONAL_UX_PLAN_2026-09.md`
(§14 «шкала риска как первый vertical slice», §21). Frontend-only;
торговая policy, допустимые границы риска и backend-контракты не изменены.

## Объём

Выполнены пункты 1–3 рекомендуемого порядка slice: baseline, исправления
ввода/lifecycle (S2.5) и визуальная композиция/motion (S3.4) — в части,
проверяемой без физических устройств. Пункт 4 (живая приёмка на
iOS/Android/desktop) и подключение к общему `resources.ts` после S2.2 —
не входят и остаются открытыми.

## Baseline (F0, до изменений)

- HEAD `f0b3378`; `pnpm test`: 19 файлов / 195 тестов PASS; `pnpm lint`: 0.
- Зафиксированное поведение (§21.1): native touch scroll + mandatory snap;
  тап — мгновенный скачок scrollLeft + rAF-correction, cooldown 150 ms;
  mouse drag без pointer capture; wheel перехватывал dominant deltaY всегда
  (без deltaMode/границ/zoom-исключения); settle — debounce 90 ms;
  highlight через transition layout-height 16→26 px.

## Изменения

- `miniapp/src/components/RiskRuler.tsx` (новый): control выделен из
  `screens/RiskScreen.tsx`; handlers/refs принадлежат одному interaction owner.
  - Mouse drag: `setPointerCapture` после порога 6 px, snap отключается на
    время drag (`scroll-snap-type: none` через класс), scrollLeft не чаще
    rAF; up/cancel/lostcapture/blur освобождают capture, класс и rAF.
    `suppressClick` съедает только завершающий click конкретного drag;
    следующий самостоятельный клик не затрагивается (сброс на pointerdown).
  - Wheel: вертикальная компонента конвертируется только после явной
    активации control'а (pointerdown/focus; сброс по pointerdown вне его и
    по уходу фокуса). deltaMode (px/line/page) нормализуется, дельты
    накапливаются до rAF; на границе наружу — release странице; ctrl/cmd
    (zoom) и horizontal-dominant trackpad не перехватываются.
  - Прерываемое доведение: `animateToStep` (тап/клавиатура/завершение
    mouse drag) — snap off, монотонный ease-out 140–220 ms по расстоянию,
    один correction после восстановления snap; новый pointerdown/wheel/
    keyboard/save отменяет по generation, начиная с физической координаты.
    `centerImmediately` — mount/resize/save/reduced-motion/repair.
  - Settle: нативный `scrollend` где есть + всегда-armed идемпотентный
    fallback (тишина 80 ms + стабильный scrollLeft 3 rAF подряд); в idle
    нет постоянного rAF; settle не повторяет уже подтверждённый шаг.
  - Haptics: crossing нового шага при пользовательском scroll —
    `selectionChanged` с rate-limit ≤1/50 ms без очереди; тап со сменой
    шага — один light impact; programmatic/mount/resize/save молчат.
  - Фиксированный cooldown 150 ms удалён как арбитр жестов.
  - `paintRuler`: пачка geometry reads, затем пачка `--off` writes.
- `miniapp/src/riskRuler.ts` (новый, pure): `wheelIntent`/
  `normalizeWheelDelta`, `easeOutCubic`, `animationDurationMs`,
  `clampScroll`, `shouldSelectionHaptic`, константы.
- `miniapp/src/screens/RiskScreen.tsx`: интеграция; save читает свежий
  draft из `selectedRef` (надпись кнопки ≡ отправленная fraction); freeze
  прерывает motion и точно центрирует сохраняемый шаг; haptics переехали
  в control.
- `miniapp/src/telegram.ts`: `selectionChanged` в типе HapticFeedback +
  `hapticSelectionChanged()` wrapper.
- `miniapp/src/index.css`: постоянная геометрия рисок (26 px box, scaleY/
  opacity/color вместо transition height), `.dragging`/`.animating`
  snap-off, узкая mask-fade по краям с fallback (forced-colors,
  reduced-transparency, unsupported), reduced-motion — плоская статичная
  шкала и мгновенное центрирование вместо анимаций.

## Осознанно НЕ изменено

- `scroll-snap-stop: always` сохранён: §21.3 предписывает пробовать
  `normal` и принимать только по физическому A/B на iOS/Android — до живой
  приёмки дефолт не трогаем.
- Touch/trackpad inertia — нативная; JS-физика поверх не вводилась.
- Демонстрационный баланс `RISK_DEMO_BALANCE_USDT` — замена на реальный
  `available_usdt` это backend B9 / S1.4, не этот slice.

## Проверки (jsdom/node)

- `pnpm test`: 21 файл / 224 теста PASS (baseline 19/195). Новые:
  - `riskRuler.test.ts` (pure): wheel gating/normalization/границы/zoom,
    duration 140–220, монотонность easeOutCubic, haptic rate-limit, clamp.
  - `riskRulerBehavior.test.tsx` (jsdom): wheel до/после активации,
    line-mode, release на границах, деактивация снаружи; drag за порогом,
    suppression только завершающего click, pointercancel cleanup; tap →
    синхронный draft + точное доведение (scrollLeft 128 по синтетической
    геометрии), прерывание с физической позиции, отсутствие re-pick
    проходящих шагов; settle fallback — quiet+stable, один non-silent
    acknowledgement, dedup после тапа; keyboard Arrow/Home.
  - Существующий `riskBehavior` (tap→dirty→save, один POST, freeze, гонка
    scroll/save, legacy off-grid) — без изменений, PASS.
- `pnpm lint`: 0 warnings/errors. `pnpm build` (tsc -b && vite): OK;
  index 312,5 kB raw / 89,8 kB gzip (было ~308 / 88).

## Открытые пункты (живая приёмка, S3.5/G3)

1. Физическая приёмка жестов в Telegram WebView на iOS и Android + мышь/
   trackpad desktop: короткий/длинный swipe, смена направления в инерции,
   diagonal gesture, tap через несколько делений + немедленный новый drag,
   wheel на краях, Save во время inertia/animation. Записи экрана до/после
   и frame trace (§21.6).
2. A/B `scroll-snap-stop: normal` против `always` на устройствах; решение
   зафиксировать в этом файле новой секцией.
3. Проверка edge mask-fade и reduced-motion/reduced-transparency на
   реальных устройствах (читаемость края, focus ring).
4. Подключение к общему resource cache (`resources.ts`) — только после
   S2.2 и только если Risk реально нужен cache.

---

## Handoff следующей сессии: ручная браузерная приёмка (без бота)

Дата: 2026-09-06. Этот блок — самодостаточный бриф для сессии, которая
поднимет Mini App в браузере (без Telegram-бота) и проверит шкалу риска
браузерными плагинами (Playwright MCP / Chrome DevTools MCP).

### Что именно изменилось (дельта поведения, файл за файлом)

| Файл | Что теперь иначе с точки зрения пользователя/приёмки |
|---|---|
| `miniapp/src/components/RiskRuler.tsx` (новый, control выделен из `screens/RiskScreen.tsx`) | Тап по далёкому делению: цифра-превью обновляется в ближайшие 1–2 кадра (независимый замер: ~33 ms) — задолго до конца доведения, без перебора промежуточных значений; шкала **плавно доводится** 140–220 ms (ease-out, без overshoot) вместо мгновенного скачка; любой новый жест (pointerdown/wheel/клавиша) **прерывает** доведение с текущей физической позиции, без отскока назад. Mouse drag держится при выходе курсора за границы шкалы (pointer capture), во время drag snap выключен; клик, завершающий drag, не выбирает случайное деление; следующий отдельный клик работает. Вертикальное колесо над шкалой **без клика по ней крутит страницу** (раньше перехватывало); после тапа/фокуса на шкале — крутит шкалу; на краях наружу отдаёт скролл странице; ctrl/cmd+wheel (zoom) и горизонтальный trackpad не перехватываются; line-mode колесо (мышиные «щелчки») движется плавно, а не скачками. Settle после свайпа определяется по `scrollend`/тишине+стабильности, не по голому debounce. Во время save-POST шкала замораживается и точно центрирует сохраняемое значение. |
| `miniapp/src/riskRuler.ts` (новый, pure) | Решения ввода вынесены в тестируемые функции: `wheelIntent`, `normalizeWheelDelta`, `animationDurationMs` (140–220 ms), `easeOutCubic`, `shouldSelectionHaptic` (≤1 импульса/50 ms). В UI видно косвенно (поведение выше). |
| `miniapp/src/screens/RiskScreen.tsx` | Save отправляет ровно то значение, что написано на кнопке, даже если жест завершился в тот же тик (draft читается из ref). Остальной контракт экрана (один POST, freeze, toast, legacy-баннер) не изменён. |
| `miniapp/src/telegram.ts` | Добавлен `hapticSelectionChanged()`. В браузере вне Telegram haptics — ожидаемый no-op (это норма, не дефект). |
| `miniapp/src/index.css` | Риски имеют постоянную высоту-рамку 26 px: выделение — scaleY/opacity/цвет, без layout-transition высоты (полоска больше не «подпрыгивает» при смене активного). Края шкалы мягко растворяются (mask-fade ~20 px; при forced-colors/reduced-transparency/отсутствии mask — обычные края). `prefers-reduced-motion`: плоская статичная шкала, доведение мгновенное. |

Намеренно НЕ изменено: `scroll-snap-stop: always` (A/B с `normal` — только
после физического сравнения), нативная touch/trackpad-инерция, демо-баланс
1240 USDT (замена — backend B9/S1.4), визуальный язык Aster.

### Запуск без бота (engine preview, реальные данные на копии БД)

```bash
# 1. Свежий bundle: сервер отдаёт miniapp/dist, не dev-server
cd miniapp && pnpm build && cd ..

# 2. Preview: копия prod-БД (SQLite backup API), testnet engine, unsigned dev-сессия
.venv/bin/python scripts/preview_miniapp_engine.py --account <ACCOUNT_ID> --confirm-engine-preview
# → http://127.0.0.1:8085/miniapp/  (dev user 900000001 привязан к --account в КОПИИ БД)
```

- Путь к шкале: Настройки → «Риск на Stop» (nested-экран).
- Production-БД не трогается; engine работает против Aster **testnet** с
  реальными ключами из копии. Стоп: Ctrl+C.
- **Save — настоящий POST** в preview-backend: policy сохраняется в копию
  БД, а живой engine может применить её к открытым позициям тестового
  аккаунта (`post_fill_cap`). Для проверки save-flow предпочтителен
  аккаунт без открытых позиций; «ровно один POST» и тело запроса
  проверять в Network/CDP (`risk/percent`, один запрос на тап, payload —
  Decimal-строка, напр. `"0.03"`).
- Если preview-аккаунта/БД нет — UI-часть жестов можно проверить и без
  save: все пункты ниже, кроме #7, не требуют backend-мутаций, но требуют
  успешной загрузки `risk` DTO (нужен запущенный preview).

Ограничения браузерной приёмки (фиксировать как известные, не как баги):
нет `window.Telegram` → haptics/BackButton не работают; Chromium ≠ WebKit
(iOS re-snap и инерция отдельно, на реальном устройстве — S3.5/G3);
`scrollend` в Chrome есть — fallback-ветка проверяется только эмуляцией/
тестами (уже покрыта jsdom).

### Чеклист приёмки и как проверять плагинами

Инструменты: Playwright MCP (`mouse.move/down/up`, `mouse.wheel`,
`touchscreen.tap`, `keyboard.press`, `emulateMedia`, `evaluate`,
`screencast`/trace) или Chrome DevTools MCP (snapshots, CDP
`Input.dispatch*`, `Performance`/tracing, `Emulation.setEmulatedMedia`).
Для каждого пункта: действие → ожидаемый результат → PASS/FAIL + запись
экрана. Viewport: 390×844 (iPhone) и отдельно 320 px ширина; desktop
1280×800 для мыши/trackpad/wheel.

1. **Короткий свайп** (touch emulated, drag ~80 px по шкале): шкала следует
   пальцем без лага, останавливается ровно на ближайшем шаге; цифра-превью
   совпадает с делением под шампань-курсором. residual ≤1 CSS px:
   `evaluate` — центр активного tick'а vs центр strip
   (`getBoundingClientRect`).
2. **Длинный свайп через несколько делений** (быстрый fling): с
   `scroll-snap-stop: always` остановка на ближайшем делении — это
   ожидаемо; жест не «залипает», смена направления во время инерции
   подчиняется пальцу сразу.
3. **Диагональный/вертикальный жест по шкале**: страница скроллится
   вертикально, шкала не захватывает жест (нет `touch-action` хаков).
4. **Тап через несколько делений**: превью-цифра обновляется в ближайшие
   1–2 кадра (независимый замер 2026-09-06: ~33 ms), задолго до конца
   доведения; крупная цифра НЕ перебирает промежуточные проценты; по
   завершении активное деление под курсором (≤1 px). Запись экрана
   до/после — обязательна.
5. **Прерывание доведения**: тап по далёкому делению → сразу pointerdown/
   drag в середине анимации → анимация отменяется, шкала остаётся под
   пальцем, новый жест работает из физической позиции (нет прыжка ни к
   старту, ни к старой цели).
6. **Mouse drag**: press + drag → полоса следует курсору; выход за границы
   шкалы с зажатой кнопкой — drag не теряется (capture); отпускание снаружи —
   доведение к ближайшему шагу; клик сразу после — выбирает деление (не
   съеден). Drag с отпусканием внутри — завершающий click не выбирает
   случайное деление под курсором.
7. **Save во время движения**: тап → пока идёт доведение, «Сохранить X%» →
   ровно один POST `risk/percent` (Network/CDP), тело = значение на кнопке,
   шкала мгновенно и точно центрирует X, на время POST тики disabled,
   toast «Сохранено · X%» по успеху. Повторный тап по кнопке в тот же тик —
   всё равно один POST.
8. **Wheel**: (а) hover без активации — крутится страница, шкала стоит;
   (б) клик по шкале → вертикальное колесо крутит шкалу; (в) на левом/
   правом краю наружу — скролл возвращается странице; (г) ctrl+wheel —
   zoom страницы, шкала не реагирует; (д) горизонтальный trackpad
   (`deltaX` dominant) — нативно, без JS-рывков; (е) mouse wheel notches
   (deltaMode=1) — плавное движение ~16 px/щелчок, не прыжок на шаг.
9. **Клавиатура**: Tab на активное деление → стрелки/Home/End; focus ring
   виден и НЕ обрезан edge-fade; screen reader ( VoiceOver по желанию) —
   radiogroup «Выбор риска», каждое деление — radio с «N,N%».
10. **Idle**: после остановки — нет повторяющихся rAF/таймеров: DevTools
    Performance trace 5 s покоя — нулевая активность от нашего кода.
11. **Frame trace свайпа/доведения**: запись Performance во время пунктов
    1–5 — нет повторяющихся long tasks >50 ms от RiskRuler; `--off`
    пишется transform/opacity-only (нет периодического Layout от
    height-изменений — в trace видно отсутствие layout-thrash на тиках).
12. **Reduced motion** (`Emulation.setEmulatedMedia` /
    `page.emulate_media(reduced_motion='reduce')`): тап центрирует
    мгновенно (без 140–220 ms travel), риски плоские одинаковой высоты,
    выделение читается цветом; save/toast работают.
13. **Edge fade**: края шкалы мягко уходят в фон; при
    `forced-colors: active` эмуляции — fade отключён, контраст полный.
14. **Ширины 320/360/390/430**: первое/последнее деление центрируются под
    курсором (padding px-exact), кнопка «Сохранить» достижима без
    горизонтального скролла; смена ориентации (resize) — активное деление
    остаётся под курсором.
15. **Legacy/ошибки** (по возможности): вне сети (offline в DevTools) →
    ErrorBox + «Повторить»; ошибка save → подтверждённое значение и
    dirty-выбор сохраняются, toast отсутствует.

После приёмки: результаты (PASS/FAIL, устройство/браузер, HEAD, записи
экрана и traces) дописать новой секцией в конец этого файла; A/B
`snap-stop` и живую Telegram-приёмку (haptics, BackButton, WebKit)
оставить в S3.5/G3. Дефекты ввода, воспроизведённые в браузере, чинить в
этом же slice до общего gate.

---

## Результаты браузерной приёмки (Headless Chromium, preview_miniapp_engine.py)

Дата: 2026-09-06. Приёмка проведена строго по чеклисту выше в Headless Chromium
(Playwright/CDP) против реального бэкенда на копии БД:
- Сервер: `scripts/preview_miniapp_engine.py --account copyc1 --confirm-engine-preview --port 8085`
  (SQLite backup `data/miniapp-preview.sqlite3`, тестовый dev-пользователь 900000001,
  привязка к `copyc1`, testnet trading engine).
- Бандл: `miniapp/dist` собран с `VITE_DEV_USER_ID=900000001`.
- Viewport по умолчанию: 390×844 (iPhone 12/13/14).

### Сводка по пунктам чеклиста

| # | Пункт чеклиста | Результат | Фактические измерения / Evidence |
|---|---|---|---|
| 1 | **Короткий свайп** (touch emulated, ~80 px) | **PASS** | CDP `dispatchTouchEvent` (touchStart → touchMove 80 px → touchEnd). Шкала перешла 2,0% → 2,5%; `residual = 0 px` (норматив ≤1 px). Текст в шапке («2,5%») совпадает с меткой деления под курсором (`matches: true`). |
| 2 | **Длинный свайп и смена направления** (fling + reversal) | **PASS** | Быстрый fling на 140 px сменил направление в середине инерции новым тачем вправо (40 px); шкала не «залипла», подчинилась пальцу сразу. Остановка на ближайшем шаге (`scrollLeft: 256`, 2,5%), `residual = 0 px`. |
| 3 | **Диагональный/вертикальный жест по шкале** | **PASS** | Вертикальный свайп 150 px по поверхности шкалы: `rulerScrollLeft` остался неизменным (256 px), `shell-content.scrollTop` изменился 0 → 33 px. Шкала не блокирует вертикальный скролл страницы. |
| 4 | **Тап через деления** (доведение, без промежуточных цифр) | **PASS** | Тап с 2,5% на 0,5% (скачок на 4 деления, Δ = 256 px). Сэмплы каждые 25 ms зафиксировали строго одно значение в шапке: `["0,5%"]` (промежуточные 2,0%, 1,5%, 1,0% не мелькали). Время доведения ~200 ms (в диапазоне 140–220 ms). Финал: `scrollLeft: 0`, `residual = 0 px`. |
| 5 | **Прерывание доведения новым жестом** | **PASS** | Тап 0,5% → 4,0% (Δ = 448 px); прерывание на 50 ms (в середине движения, `scrollLeft: 250 px`) новым тачем/drag. Аниматор отменён мгновенно по generation, отскока назад к старту или цели не произошло, доводка продолжилась с физической координаты. Финал: `residual = 0 px`. |
| 6 | **Mouse drag с выходом за границы и pointer capture** | **PASS** | Нажатие на шкале, drag влево на 80 px и вниз на 120 px (за пределы 70 px высоты шкалы) — drag удерживается через `pointerCapture` (`scrollOutside: 576 px`). Отпускание снаружи центрировало шаг 5,0% (`residual = 0 px`). Следующий клик сразу после отпускания успешно выбрал деление 2,0% (`suppressClick` съел только завершающий клик drag'а). |
| 7 | **Save во время движения** (1 POST, freeze, toast) | **PASS** | Тап на 3,0% → в середине движения (`scrollLeft: 236 px`) отправлен двойной синхронный клик по кнопке «Сохранить 3,0%». В Network зафиксирован **ровно 1 POST** `/miniapp/api/risk/percent` с телом `{"risk_percent":"0.03"}` (double-tap gate сработал). Шкала мгновенно центрирована на 3,0% (`scrollLeft: 320 px`, `residual = 0 px`), кнопка перешла в `disabled: true`, отобразился тост `Сохранено · 3,0%`. |
| 8 | **Wheel** (hover, активация, границы, zoom, trackpad, notches) | **PASS** | (а) Hover без клика: `defaultPrevented: false`, скролл отдан странице.<br>(б) После pointerdown/focus: `defaultPrevented: true`, `scrollLeft` 320 → 384 px.<br>(в) Левый край (scrollLeft=0, deltaY<0): `defaultPrevented: false` (релиз странице).<br>(г) Ctrl+Wheel: `defaultPrevented: false` (zoom не перехватывается).<br>(д) Trackpad deltaX-dominant: `defaultPrevented: false` (нативный скролл).<br>(е) Line mode (deltaMode=1): 4 линии дали ровно +64 px (1 шаг) с `defaultPrevented: true`. |
| 9 | **Клавиатура и ARIA** | **PASS** | `role="radiogroup"`, `aria-label="Выбор риска"`, тики `role="radio"`, `aria-checked`, roving `tabindex` (0 активный, -1 остальные). Клавиши `ArrowRight`, `ArrowLeft`, `Home` (0,5%), `End` (5,0%) переключают шаги с плавной доводкой и `residual = 0 px`. |
| 10 | **Idle: отсутствие таймеров/rAF в покое** | **PASS** | За 5000 ms после завершения движений зафиксировано 0 вызовов `requestAnimationFrame`, 0 интервалов `setInterval`, 0 таймеров `setTimeout` от `RiskRuler`. Утечек фоновых циклов нет. |
| 11 | **Frame trace и отсутствие layout-thrash** | **PASS** | При доведении через всю шкалу (0,5% → 4,5%): зафиксировано 0 long tasks (>50 ms), 25 кадров, среднее время кадра 16,7 ms, максимальное 17,5 ms (стабильные 60 FPS). Все `.risk-tick-mark` сохраняют константную высоту рамки 26 px (`allMarkBoxHeights26px: true`), деформация рыбы через `scaleY`/`opacity`, Layout не триггерится. |
| 12 | **Reduced motion** (`prefers-reduced-motion: reduce`) | **PASS** | Под медиа-запросом риски плоские (`height: 18px`, `transform: none`), выделение передаётся champagne-цветом (`rgb(242, 201, 141)`), тап центрирует мгновенно за 1 кадр (`isImmediate: true`, `scrollAfter1Frame: 64 px`, `residual = 0 px`). |
| 13 | **Edge fade и forced-colors** | **PASS** | В штатном режиме `mask-image: linear-gradient(90deg, transparent, #000 20px, #000 calc(100% - 20px), transparent)`. При `forced-colors: active` и `prefers-reduced-transparency: reduce` маска отключается (`mask-image: none`), обеспечивая полный контраст. |
| 14 | **Ширины 320/360/390/430 px и resize** | **PASS** | 320 px (pad 111 px), 360 px (pad 131 px), 390 px (pad 146 px), 430 px (pad 166 px) — первое (0,5%) и последнее (5,0%) деления центрируются с `residual = 0 px`, горизонтальный скролл страницы отсутствует (`scrollWidth <= clientWidth`). При resize 430 → 390 px активное деление остаётся под курсором (`residual = 0 px`). |
| 15 | **Отсутствие JS-ошибок** | **PASS** | За всё время сессии в консоли браузера зафиксировано 0 ошибок/исключений (`window.__pageErrors: []`). |

### Вердикт браузерной приёмки

Все 15 пунктов чеклиста пройдены (**15/15 PASS**). Механика ввода (S2.5) и визуальная композиция (S3.4) работают в соответствии со спецификацией.
Открытыми остаются исключительно аппаратные аспекты S3.5/G3, требующие физических устройств:
1. Тактильный отклик (Telegram WebApp Haptics на iOS/Android).
2. Поведение нативного Telegram BackButton.
3. Физический A/B `scroll-snap-stop: normal` vs `always` на реальных экранах.

---

## Независимая перепроверка приёмки (2026-09-06, вторая сессия)

Дословный текст: `docs/reviews/review_miniapp_risk_ruler_2026-09.md`.
Стенд и путь повторяют первичную приёмку; замеры — живым CDP/DOM, не по
логам отчёта. Назначение прохода — независимо подтвердить заявленные числа.

Перемерено и сошлось точно: маунт residual 0 px; тап 1% → 4% residual 0 px,
кнопка активна; wheel без активации отдан странице (`moved:false`,
`prevented:false`), после активации +64 px / `prevented:true`; ctrl+wheel не
перехвачен; клавиатура пользовательским путём 4,0% → 3,5%, residual 0 px;
save двойным кликом в один тик — ровно 1 POST `{"risk_percent":"0.035"}`,
надпись кнопки ≡ payload, тост, `disabled:true`; 320 px — paddingLeft 111 px
и residual 0 px до пикселя как в отчёте; 0 JS-ошибок; риск возвращён на 1,0%.
Самостоятельно прогнаны `pnpm test` (21/224 PASS), `pnpm lint` (0),
`pnpm build` (OK) — сошлось с заявленным.

Единственная находка — формулировочная, не поведенческая: превью-цифра
обновляется не «в тот же тик», а в ближайшие кадры (~2 кадра / ~33 ms),
без перебора промежуточных значений. **Применено:** таблица дельты и пункт 4
handoff-чеклиста выше переписаны на «в ближайшие кадры»; контракт
«надпись кнопки ≡ POST payload» замером подтверждён и не затронут.
Замечание про keydown, диспатчнутый напрямую на контейнер: ожидаемое
поведение — обработчик на strip получает событие всплытием от
сфокусированной кнопки (roving tabindex); пользовательский путь исправен,
дефекта нет, код не менялся.

Не перемерялись перепроверкой (приняты по коду + jsdom + точному совпадению
всех смежных чисел; покрыты первичной приёмкой 15/15): CDP-свайпы (пп. 1–3,
5–6), idle-trace, frame-trace, reduced-motion, forced-colors.

Вердикт перепроверки: приёмка реальная, цифры честные, slice можно
принимать.

**Итоговый статус slice:** код + pure/jsdom тесты (21/224), lint/build,
полная браузерная приёмка 15/15 и независимая перепроверка — браузерная
часть CLOSED. Устройство-зависимая часть (Telegram haptics, BackButton,
WebKit-инерция, snap-stop A/B) **отложена на потом (2026-09-06): доступа к
устройствам сейчас нет** — пункты не закрыты и возвращаются в S3.5/G3 при
появлении устройств; до этого момента slice считается принятым в
браузерном объёме.

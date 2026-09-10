/** Real-Chromium acceptance for the Mini App (no npm dependencies).
 *
 * jsdom proves logic, not the shipped gesture, and screenshots cannot prove that
 * a screen stopped moving. This drives headless Chrome over CDP against the
 * running bot and measures both: a real dispatched touch pull (indicator phases,
 * Telegram swipe handoff, the strong reads it triggers, the scrolled-away guard)
 * and every DOM mutation on the visible surface, so "no user-visible jitter" is
 * recorded rather than asserted.
 *
 * Gates: local and read-only. It talks to the running bot's Mini App API as the
 * session user (reads plus one explicit refresh) and never sends trading commands
 * or writes remote state.
 *
 * Needs a locally minted Telegram stub: a file that assigns `window.Telegram`
 * with a *signed* initData (see the live-mode plan, §8 "browser audit"). It is a
 * session credential — keep it out of git and out of logs. The page's own
 * `telegram-web-app.js` is blocked, because outside a Telegram client it would
 * overwrite the stub with an empty initData and silently audit the wrong state.
 *
 * Usage:
 *   node scripts/audit_miniapp_browser.mjs smoke|series|nested  --init <file>
 *   node scripts/audit_miniapp_browser.mjs swipe --init <file> [--out data/pull]
 *   node scripts/audit_miniapp_browser.mjs jitter --init <file> --seconds 190
 *   node scripts/audit_miniapp_browser.mjs status --init <file> [--after 95]
 *   node scripts/audit_miniapp_browser.mjs churn  --init <file>
 *   node scripts/audit_miniapp_browser.mjs shot   --init <file> [--out data/shot.png]
 *
 * Modes: smoke (what the surface shows), swipe (dispatched touch pull: indicator
 * phases, gap, Telegram swipe handoff, the refresh=true reads it fires, the
 * scrolled-away guard), series (four tabs), nested (every nested screen),
 * jitter (every DOM mutation for --seconds), status (samples System Status for
 * 30 s after opening: does the app re-prove permissions by itself), churn (50
 * tab switches plus nested navigation with a counter over WebSocket opens),
 * shot (PNG plus text of the current surface).
 *
 * Options: --url (default http://127.0.0.1:8080/miniapp/), --port 9333,
 * --seconds N (jitter window), --tab N and --closed (root tab to watch),
 * --after N (idle on the root tab first), --out <prefix> (swipe/shot output),
 * --chrome (default /Applications/Google Chrome.app/...; Chrome/Chromium only).
 */

import { spawn } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { writeFileSync } from 'node:fs'
import { setTimeout as delay } from 'node:timers/promises'

function parseArgs(argv) {
  const options = {
    mode: argv[0] ?? 'smoke',
    url: 'http://127.0.0.1:8080/miniapp/',
    port: 9333,
    seconds: 60,
    tab: 0,
    closed: false,
    out: null,
    after: 0,
    chrome: process.env.CHROME_BIN ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    init: null,
  }
  for (let index = 1; index < argv.length; index += 1) {
    const flag = argv[index]
    const value = argv[index + 1]
    if (flag === '--url') options.url = value
    else if (flag === '--port') options.port = Number(value)
    else if (flag === '--seconds') options.seconds = Number(value)
    else if (flag === '--tab') options.tab = Number(value)
    else if (flag === '--closed') options.closed = true
    else if (flag === '--out') options.out = value
    else if (flag === '--after') options.after = Number(value)
    else if (flag === '--chrome') options.chrome = value
    else if (flag === '--init') options.init = value
  }
  if (!options.init) throw new Error('--init <signed Telegram stub file> is required')
  return options
}

const options = parseArgs(process.argv.slice(2))

// The app's own staleness complaints. A bare «устарел» is not enough: the
// activity feed legitimately reports «сигнал мастера устарел» for a master
// entry that was skipped, and matching it would flag real content as UI noise.
const NOISE_SOURCE =
  "Обновить|обновите|Проверить состояние|требует проверки|Нет связи|Данные устарели|Снимок устарел|Проверка прав устарела"

async function main() {
  const chrome = spawn(
    options.chrome,
    [
      '--headless=new',
      `--remote-debugging-port=${options.port}`,
      // Unique per run: a SIGKILLed previous run leaves a profile lock behind,
      // and Chrome then exits instantly (leaving this script awaiting a browser
      // that is not there).
      `--user-data-dir=/tmp/chrome-miniapp-audit-${options.port}-${process.pid}`,
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-gpu',
      'about:blank',
    ],
    { stdio: 'ignore' },
  )

  const endpoint = await browserEndpoint(options.port)
  const socket = new WebSocket(endpoint)
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true })
    socket.addEventListener('error', reject, { once: true })
  })

  let nextId = 1
  const pending = new Map()
  const events = []
  socket.addEventListener('message', (message) => {
    const payload = JSON.parse(message.data)
    if (payload.id && pending.has(payload.id)) {
      const { resolve, reject } = pending.get(payload.id)
      pending.delete(payload.id)
      if (payload.error) reject(new Error(payload.error.message))
      else resolve(payload.result)
      return
    }
    if (payload.method) events.push(payload)
  })

  const send = (method, params = {}, sessionId) => {
    const id = nextId++
    const message = { id, method, params }
    if (sessionId) message.sessionId = sessionId
    socket.send(JSON.stringify(message))
    return new Promise((resolve, reject) => pending.set(id, { resolve, reject }))
  }

  const waitEvent = async (method, sessionId, timeoutMs = 20000) => {
    const started = Date.now()
    for (;;) {
      const found = events.find((item) => item.method === method && (!sessionId || item.sessionId === sessionId))
      if (found) return found
      if (Date.now() - started > timeoutMs) throw new Error(`timeout waiting for ${method}`)
      await delay(50)
    }
  }

  const { targetId } = await send('Target.createTarget', { url: 'about:blank' })
  const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true })
  await send('Page.enable', {}, sessionId)
  await send('Runtime.enable', {}, sessionId)
  await send('Network.enable', {}, sessionId)
  await send('Network.setBlockedURLs', { urls: ['https://telegram.org/*'] }, sessionId)
  await send('Emulation.setDeviceMetricsOverride',
    { width: 390, height: 780, deviceScaleFactor: 2, mobile: true }, sessionId)
  await send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 1 }, sessionId)
  await send('Page.addScriptToEvaluateOnNewDocument',
    { source: readFileSync(options.init, 'utf8') }, sessionId)
  // Count client sockets from the page itself: the plan forbids a tab walk
  // from multiplying live WebSocket sessions, and only the page can see how
  // many it opened or left open.
  await send('Page.addScriptToEvaluateOnNewDocument',
    { source: `window.__ws = { opened: 0, closed: 0, live: 0 };
      const OriginalWebSocket = window.WebSocket;
      window.WebSocket = function (...args) {
        const socket = new OriginalWebSocket(...args);
        window.__ws.opened += 1;
        window.__ws.live += 1;
        const close = socket.close.bind(socket);
        socket.close = (...closeArgs) => { if (socket.readyState !== 3) window.__ws.live -= 1; window.__ws.closed += 1; return close(...closeArgs); };
        socket.addEventListener('close', () => { if (!socket.__counted) { socket.__counted = true; window.__ws.live -= 1; window.__ws.closed += 1; } });
        return socket;
      };
      window.WebSocket.prototype = OriginalWebSocket.prototype;
    ` }, sessionId)

  // A wedged renderer used to hang every mode until the outer watchdog fired,
  // which read as "audit did not finish" instead of the real cause. Bound each
  // evaluate and say which expression stalled.
  const EVALUATE_TIMEOUT_MS = 15_000
  const evaluate = async (expression) => {
    const outcome = await Promise.race([
      send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, sessionId)
        .then((result) => ({ result })),
      delay(EVALUATE_TIMEOUT_MS).then(() => ({ timedOut: true })),
    ])
    if (outcome.timedOut) {
      throw new Error(`evaluate stalled for ${EVALUATE_TIMEOUT_MS} ms: ${expression.slice(0, 90)}`)
    }
    const { result } = outcome
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails).slice(0, 400))
    return result.result.value
  }
  const touch = (type, points) => send('Input.dispatchTouchEvent', { type, touchPoints: points }, sessionId)
  const posts = () => events
    .filter((item) => item.method === 'Network.requestWillBeSent'
      && item.params.request.url.includes('/miniapp/api/'))
    .map((item) => `${item.params.request.method} ${item.params.request.url.split('/miniapp/api/')[1]} ${item.params.request.postData || ''}`)
  const pull = async (x, fromY, steps) => {
    await touch('touchStart', [{ x, y: fromY }])
    for (const step of steps) {
      await touch('touchMove', [{ x, y: fromY + step }])
      await delay(60)
    }
  }
  const snapshot = async (path) => {
    const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false }, sessionId)
    writeFileSync(path, Buffer.from(shot.data, 'base64'))
  }
  // Pull geometry, measured against the shell rather than the viewport: the
  // indicator is only acceptable while it sits inside the gap the content
  // opens, and "it looks fine on my screenshot" is not a measurement.
  const PULL_GAP = `(() => {
    const shell = document.querySelector('.shell').getBoundingClientRect()
    const spinner = document.querySelector('.pull-refresh-spinner')
    const badge = spinner && spinner.getBoundingClientRect()
    const content = document.querySelector('.shell-content').getBoundingClientRect()
    const first = document.querySelector('.shell-content .screen > *')
    const row = first && first.getBoundingClientRect()
    return {
      shift: Math.round(content.top - shell.top),
      spinnerTop: badge ? Math.round(badge.top - shell.top) : null,
      spinnerSize: badge ? Math.round(badge.height) : null,
      firstRowTop: row ? Math.round(row.top - shell.top) : null,
      coversContent: Boolean(badge && row && badge.bottom > row.top),
    }
  })()`

  await send('Page.navigate', { url: options.url }, sessionId)
  await waitEvent('Page.loadEventFired', sessionId)
  await delay(4000)
  const shellReady = await evaluate(`new Promise((done) => { const t = setInterval(() => { if (document.querySelector('.shell-content')) { clearInterval(t); done(true) } }, 200); setTimeout(() => { clearInterval(t); done(false) }, 20000) })`)
  if (!shellReady) throw new Error('Mini App shell never mounted (session rejected?)')
  // An expired or unverifiable initData stub renders the gate instead of the
  // shell, and every later click would fail with an unrelated TypeError.
  if (await evaluate("document.body.innerText.includes('Откройте из бота')")) {
    throw new Error('Mini App rejected the signed session: re-mint data/preview-init.js before auditing')
  }
  // Measurement is per surface: the root tab complaint was about Позиции and
  // Активность, which the earlier runs never observed directly.
  if (options.tab) {
    await evaluate(`(() => { [...document.querySelectorAll('.nav-item')][${options.tab}].click(); return true })()`)
    await delay(2500)
  }
  // "Открытые -> Закрытые" is the exact tap a follower reported as "история
  // изменилась — обновите список", so it has to be observable, not assumed.
  if (options.closed) {
    await evaluate("(() => { const b = [...document.querySelectorAll('[role=tab]')].find((t) => /Закрытые/.test(t.innerText)); if (!b) throw new Error('no Закрытые tab'); b.click(); return true })()")
    await delay(3000)
  }

  if (options.mode === 'smoke') {
    const text = await evaluate('document.body.innerText')
    const state = await evaluate(`JSON.stringify({
      initLen: (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData || '').length,
      railVisible: !!document.querySelector('.live-status'),
      demands: (document.body.innerText.match(new RegExp(${JSON.stringify(NOISE_SOURCE)}, 'gi')) || []),
      updateButtons: [...document.querySelectorAll('button')].map((b) => b.innerText.trim()).filter((t) => /обнов|повтор/i.test(t)),
    })`)
    console.log(JSON.stringify({ text: text.slice(0, 700), ...JSON.parse(state), posts: posts().slice(-8) }, null, 1))
  }

  if (options.mode === 'swipe') {
    const box = JSON.parse(await evaluate(`(() => { const r = document.querySelector('.shell-content').getBoundingClientRect(); return JSON.stringify({ x: r.left + r.width / 2, y: r.top + 6 }) })()`))
    const before = posts().length
    await pull(box.x, box.y, [12, 26, 44, 62, 80, 96])
    const mid = JSON.parse(await evaluate(`JSON.stringify({
      phase: document.querySelector('.pull-refresh').dataset.phase,
      distance: document.querySelector('.shell').style.getPropertyValue('--pull-distance'),
      label: document.querySelector('.pull-refresh').innerText.trim(),
      gap: ${PULL_GAP},
      swipes: window.__audit.swipes.map((s) => s[0]),
    })`))
    if (options.out) await snapshot(`${options.out}.pulling.png`)
    await touch('touchEnd', [])
    await delay(300)
    const released = JSON.parse(await evaluate(`JSON.stringify({
      phase: document.querySelector('.pull-refresh').dataset.phase,
      label: document.querySelector('.pull-refresh').innerText.trim(),
      gap: ${PULL_GAP},
      swipes: window.__audit.swipes.map((s) => s[0]),
      haptics: window.__audit.haptics.map((h) => h[0] + ':' + h[1]),
    })`))
    if (options.out) await snapshot(`${options.out}.refreshing.png`)
    await delay(4000)
    const settled = JSON.parse(await evaluate(`JSON.stringify({ phase: document.querySelector('.pull-refresh').dataset.phase })`))
    console.log(JSON.stringify({ mid, released, settled, requests: posts().slice(before) }, null, 1))

    // Pulling while reading further down must stay a scroll. The viewport is
    // shrunk first so the surface genuinely scrolls instead of "passing" the
    // guard because it could not move at all.
    await send('Emulation.setDeviceMetricsOverride',
      { width: 390, height: 360, deviceScaleFactor: 2, mobile: true }, sessionId)
    await delay(400)
    const scrolled = JSON.parse(await evaluate(`(() => { const el = document.querySelector('.shell-content'); el.scrollTop = 200; return JSON.stringify({ scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight }) })()`))
    const scrolledBefore = posts().length
    await pull(box.x, 20, [20, 45, 70])
    const duringScroll = JSON.parse(await evaluate(`JSON.stringify({ phase: document.querySelector('.pull-refresh').dataset.phase, scrollTop: document.querySelector('.shell-content').scrollTop })`))
    await touch('touchEnd', [])
    await delay(2500)
    console.log(JSON.stringify({
      scrolledContext: scrolled,
      duringScroll,
      // Names, not just the count: a background TTL read is not the gesture.
      requestsWhileScrolled: posts().slice(scrolledBefore),
      swipes: JSON.parse(await evaluate(`JSON.stringify(window.__audit.swipes.map((s) => s[0]))`)),
    }, null, 1))
  }

  if (options.mode === 'series') {
    const labels = ['Главная', 'Позиции', 'Активность', 'Настройки']
    const report = {}
    for (const [index, label] of labels.entries()) {
      await evaluate(`(() => { [...document.querySelectorAll('.nav-item')][${index}].click(); return true })()`)
      await delay(2500)
      report[label] = JSON.parse(await evaluate(`JSON.stringify({
        text: document.body.innerText.replace(/\\n{2,}/g, '\\n').slice(0, 700),
        noise: (document.body.innerText.match(/Обновить|обновите|Проверить состояние|требует проверки|Нет связи|Данные устарели|Снимок устарел|Проверка прав устарела/gi) || []),
        rail: (document.querySelector('.live-status') || {}).innerText || null,
      })`))
    }
    console.log(JSON.stringify(report, null, 1))
  }

  if (options.mode === 'nested') {
    // Every nested surface a follower can open, reached the way a user reaches
    // it. Re-clicking a root tab is the only way back without the Telegram
    // BackButton, and it resets the nested stack.
    const report = {}
    const openHome = async () => {
      await evaluate("(() => { [...document.querySelectorAll('.nav-item')][0].click(); return true })()")
      await delay(1200)
    }
    const capture = async (label, clickExpression) => {
      await evaluate(clickExpression)
      await delay(2600)
      report[label] = JSON.parse(await evaluate(`JSON.stringify({
        heading: (document.querySelector('.screen h1') || {}).innerText || null,
        text: document.body.innerText.replace(/\\n{2,}/g, '\\n').slice(0, 900),
        noise: (document.body.innerText.match(new RegExp(${JSON.stringify(NOISE_SOURCE)}, 'gi')) || []),
        buttons: [...document.querySelectorAll('button')].map((b) => b.innerText.trim()).filter(Boolean).slice(0, 14),
      })`))
    }
    await openHome()
    await capture('Статус системы', "(() => { document.querySelector('.summary-status').click(); return true })()")
    await openHome()
    await capture('Риск на Stop', "(() => { [...document.querySelectorAll('.summary-row')].find((r) => /Риск на сделку/.test(r.innerText)).click(); return true })()")
    await openHome()
    await capture('Account Builder', "(() => { document.querySelector('.wallet-chip').click(); return true })()")
    await openHome()
    await evaluate("(() => { [...document.querySelectorAll('.nav-item')][3].click(); return true })()")
    await delay(1400)
    await evaluate("(() => { [...document.querySelectorAll('.row, button')].find((r) => /Уведомления в Telegram/.test(r.innerText)).click(); return true })()")
    await delay(2600)
    report['Экран уведомлений'] = JSON.parse(await evaluate(`JSON.stringify({
      text: document.body.innerText.replace(/\\n{2,}/g, '\\n').slice(0, 700),
      noise: (document.body.innerText.match(new RegExp(${JSON.stringify(NOISE_SOURCE)}, 'gi')) || []),
    })`))
    await openHome()
    await evaluate("(() => { [...document.querySelectorAll('.nav-item')][1].click(); return true })()")
    await delay(2000)
    await evaluate("(() => { [...document.querySelectorAll('[role=tab]')].find((b) => /Закрытые/.test(b.innerText)).click(); return true })()")
    await delay(2600)
    report['Закрытые сделки'] = JSON.parse(await evaluate(`JSON.stringify({
      text: document.body.innerText.replace(/\\n{2,}/g, '\\n').slice(0, 900),
      noise: (document.body.innerText.match(new RegExp(${JSON.stringify(NOISE_SOURCE)}, 'gi')) || []),
    })`))
    await openHome()
    for (const [label, needle] of [['Безопасность', 'Безопасность'], ['Помощь', 'Как работает копирование']]) {
      await evaluate("(() => { [...document.querySelectorAll('.nav-item')][3].click(); return true })()")
      await delay(1400)
      await evaluate(`(() => { [...document.querySelectorAll('.row, button')].find((r) => r.innerText.includes(${JSON.stringify(needle)})).click(); return true })()`)
      await delay(2200)
      report[label] = JSON.parse(await evaluate(`JSON.stringify({
        text: document.body.innerText.replace(/\\n{2,}/g, '\\n').slice(0, 500),
        noise: (document.body.innerText.match(new RegExp(${JSON.stringify(NOISE_SOURCE)}, 'gi')) || []),
      })`))
    }
    console.log(JSON.stringify(report, null, 1))
  }

  if (options.mode === 'jitter') {
    // Recording must start on a *loaded* surface: over the public tunnel the
    // first paint arrives in stages, and counting that as movement would blame
    // the app for drawing its own content once.
    await evaluate(`new Promise((done) => { const t = setInterval(() => {
      const stamp = document.querySelector('.freshness');
      if (stamp && stamp.innerText.trim()) { clearInterval(t); done(true) }
    }, 200); setTimeout(() => { clearInterval(t); done(false) }, 20000) })`, 25000)
    await delay(2000)

    await evaluate(`(() => {
      window.__mutations = [];
      const describe = (node) => {
        const el = node.nodeType === 3 ? node.parentElement : node;
        if (!el) return null;
        return (el.closest('.live-status, .summary-status, .freshness, .position-warning, .stale-note, .info-box, .error-box, .pull-refresh') || el).className || el.tagName;
      };
      new MutationObserver((records) => {
        for (const record of records) {
          const at = Date.now();
          if (record.type === 'characterData') {
            window.__mutations.push({ at, kind: 'text', where: describe(record.target), value: String(record.target.data).slice(0, 60) });
          } else if (record.type === 'childList') {
            const added = [...record.addedNodes].map((n) => (n.textContent || '').trim().slice(0, 60)).filter(Boolean);
            const removed = [...record.removedNodes].map((n) => (n.textContent || '').trim().slice(0, 60)).filter(Boolean);
            if (added.length || removed.length) window.__mutations.push({ at, kind: 'nodes', where: describe(record.target), added, removed });
          } else if (record.type === 'attributes') {
            window.__mutations.push({ at, kind: 'attr', where: describe(record.target), name: record.attributeName, value: String(record.target.getAttribute(record.attributeName)).slice(0, 60) });
          }
        }
      }).observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ['class', 'data-phase', 'style'] });
      return true;
    })()`)
    console.log(`recording ${options.seconds}s of DOM mutations on the visible surface...`)
    await delay(options.seconds * 1000)
    const log = JSON.parse(await evaluate('JSON.stringify(window.__mutations)'))
    // The freshness line stamps the wall clock (" · обновлено 15:41"), and a
    // minute rolling over is not jitter. Everything else that asks the user to
    // update, reconnect or distrust the data is.
    const warnings = log.filter((item) => {
      if (/^\s*·?\s*обновлено \d{2}:\d{2}\s*$/.test(item.value ?? '')) return false
      return /обнов|требует проверки|Нет связи|устарел|Восстанавлива|Подключаем/i.test(
        JSON.stringify(item),
      )
    })
    console.log(JSON.stringify({
      total: log.length,
      userVisibleWarnings: warnings,
      kinds: log.reduce((acc, item) => ({ ...acc, [item.kind]: (acc[item.kind] ?? 0) + 1 }), {}),
      sample: log.slice(0, 25),
    }, null, 1))
  }

  if (options.mode === 'churn') {
    // "50 переходов по вкладкам не создают несколько WebSocket sessions" (§6).
    // The counter is installed in the page, so both opens and leaks are seen.
    const before = JSON.parse(await evaluate('JSON.stringify(window.__ws)'))
    const labels = ['Главная', 'Позиции', 'Активность', 'Настройки']
    for (let index = 0; index < 50; index += 1) {
      await evaluate(`(() => { [...document.querySelectorAll('.nav-item')][${index % 4}].click(); return true })()`)
      await delay(120)
    }
    await delay(2500)
    const after = JSON.parse(await evaluate('JSON.stringify(window.__ws)'))
    const nested = []
    for (const [label, expression] of [
      ['Статус', "document.querySelector('.summary-status').click()"],
      ['назад', "[...document.querySelectorAll('.nav-item')][0].click()"],
      ['Риск', "[...document.querySelectorAll('.summary-row')].find((r) => /Риск на сделку/.test(r.innerText)).click()"],
      ['назад', "[...document.querySelectorAll('.nav-item')][0].click()"],
      ['Подключение', "document.querySelector('.wallet-chip').click()"],
      ['назад', "[...document.querySelectorAll('.nav-item')][0].click()"],
    ]) {
      await evaluate(`(() => { ${expression}; return true })()`)
      await delay(400)
    }
    await delay(1500)
    const final = JSON.parse(await evaluate('JSON.stringify(window.__ws)'))
    console.log(JSON.stringify({
      tabs: labels.length,
      switches: 50,
      before,
      after,
      final,
      liveSockets: posts().filter((p) => p.includes('live-ticket')).length,
      requests: posts().length,
    }, null, 1))
  }

  if (options.mode === 'status') {
    // Opening Статус must re-prove permissions by itself: the evidence TTL is a
    // minute, and a healthy account that reads "Разрешения: Устарели" is the nag
    // this mode exists to catch. Sampled, not assumed.
    // `--after N` idles on the root tab first, so the cached DTO is genuinely
    // older than the TTL when the screen opens.
    if (options.after) await delay(options.after * 1000)
    await evaluate("(() => { document.querySelector('.summary-status').click(); return true })()")
    const samples = []
    for (let step = 0; step <= 15; step += 1) {
      await delay(2000)
      samples.push(JSON.parse(await evaluate(`JSON.stringify({
        overall: (document.querySelector('.status-text') || {}).innerText || null,
        rows: [...document.querySelectorAll('.row')].map((r) => r.innerText.replace(/\\n/g, ': ')),
      })`)))
    }
    console.log(JSON.stringify(samples, null, 1))
  }

  if (options.mode === 'shot') {
    // Visual evidence for a human: the same read-only session the other modes
    // use, written to a PNG so a reviewer can see what the surface showed.
    const path = options.out ?? `/tmp/miniapp-${options.tab}-${Date.now()}.png`
    await snapshot(path)
    const text = await evaluate('document.body.innerText.replace(/\\n{2,}/g, "\\n")')
    console.log(JSON.stringify({ path, text }, null, 1))
  }

  socket.close()
  chrome.kill('SIGKILL')
}

async function browserEndpoint(port) {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/version`)
      if (response.ok) return (await response.json()).webSocketDebuggerUrl
    } catch {
      /* not up yet */
    }
    await delay(250)
  }
  throw new Error('Chrome did not expose a debugging endpoint')
}

const budgetMs =
  (options.mode === 'jitter' ? options.seconds * 1000 : 0) +
  options.after * 1000 +
  120_000
let watchdog
try {
  await Promise.race([
    main(),
    new Promise((_, reject) => {
      watchdog = setTimeout(
        () => reject(new Error(`audit did not finish within ${budgetMs} ms`)),
        budgetMs,
      )
    }),
  ])
} finally {
  clearTimeout(watchdog)
}

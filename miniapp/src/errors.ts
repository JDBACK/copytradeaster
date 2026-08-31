/** Typed-failure copy (UX plan §7.2): every server code gets a Russian
 * title and one actionable hint.  Raw payloads never reach the UI. */

export interface ErrorCopy {
  title: string
  hint: string
}

const COPY: Record<string, ErrorCopy> = {
  owner_conflict: {
    title: 'Аккаунт уже подключён',
    hint: 'Этот аккаунт подключён прямым вводом. Отключите его в боте (Аккаунт → Отключить) и вернитесь в мини-ап.',
  },
  master_address: {
    title: 'Это адрес мастера',
    hint: 'Нельзя подключить аккаунт-источник как фолловера. Используйте другой кошелёк.',
  },
  user_address_conflict: {
    title: 'Кошелёк уже используется',
    hint: 'Этот адрес уже подключён к другому пользователю. Введите другой кошелёк.',
  },
  builder_config_missing: {
    title: 'Builder недоступен',
    hint: 'Подключение через Builder временно не настроено. Попробуйте позже.',
  },
  approval_expired: {
    title: 'Заявка истекла',
    hint: 'На подпись давалось 10 минут. Начните подключение заново.',
  },
  approval_unknown: {
    title: 'Заявка не найдена',
    hint: 'Начните подключение заново.',
  },
  signature_mismatch: {
    title: 'Подпись не совпала',
    hint: 'Убедитесь, что подписываете тем же кошельком, что подключили, и попробуйте снова.',
  },
  agent_submit_failed: {
    title: 'Aster отклонил авторизацию',
    hint: 'Биржа не приняла подпись. Попробуйте ещё раз через минуту.',
  },
  agent_not_confirmed: {
    title: 'Agent не подтверждён',
    hint: 'Aster ещё не видит авторизацию. Подождите минуту и попробуйте снова.',
  },
  signed_read_failed: {
    title: 'Не удалось проверить счёт',
    hint: 'Aster временно недоступен. Попробуйте ещё раз.',
  },
  account_not_flat: {
    title: 'Счёт не пустой',
    hint: 'Закройте позиции и ордера на этом счёте, затем повторите подключение.',
  },
  open_orders_present: {
    title: 'Есть открытые ордера',
    hint: 'Отмените ордера на счёте и повторите подключение.',
  },
  position_mode_mismatch: {
    title: 'Не совпал режим позиций',
    hint: 'Переключите режим счёта в Aster (One-way/Hedge) как требует бот, и попробуйте снова.',
  },
  commit_failed: {
    title: 'Не удалось сохранить',
    hint: 'Авторизация могла пройти, но сохранение прервалось. Попробуйте подключиться заново.',
  },
  activation_failed: {
    title: 'Почти готово',
    hint: 'Аккаунт сохранён и стоит на паузе. Откройте мини-ап заново — подключение восстановится.',
  },
  testnet_required: {
    title: 'Только тестнет',
    hint: 'Подключение работает исключительно в тестовой среде Aster.',
  },
  invalid_input: {
    title: 'Проверьте данные',
    hint: 'Формат запроса неверен. Начните заново.',
  },
  invalid_session: {
    title: 'Сессия недействительна',
    hint: 'Откройте мини-ап заново из чата с ботом.',
  },
  confirmation_expired: {
    title: 'Подтверждение истекло',
    hint: 'Подтвердите действие ещё раз.',
  },
  not_authorized: {
    title: 'Нет доступа',
    hint: 'Действие недоступно для вашей роли.',
  },
  network_error: {
    title: 'Нет связи',
    hint: 'Проверьте интернет и попробуйте ещё раз.',
  },
  service_unavailable: {
    title: 'Сервис недоступен',
    hint: 'Попробуйте позже.',
  },
  wallet_rejected: {
    title: 'Подпись отменена',
    hint: 'Вы отклонили запрос в кошельке. Нажмите «Подписать», когда будете готовы.',
  },
  wrong_network: {
    title: 'Неверная сеть',
    hint: 'Переключите кошелёк на сеть BNB Smart Chain (BSC, сеть 56) и повторите.',
  },
  no_wallet: {
    title: 'Кошелёк не найден',
    hint: 'Установите MetaMask или другой EVM-кошелёк, либо откройте мини-ап из Telegram.',
  },
  wallet_unavailable: {
    title: 'Кошелёк недоступен',
    hint: 'Не удалось открыть подключение. Проверьте интернет и попробуйте ещё раз; на телефоне должен быть установлен кошелёк (TokenPocket, MetaMask…).',
  },
}

const FALLBACK: ErrorCopy = {
  title: 'Что-то пошло не так',
  hint: 'Попробуйте ещё раз. Если повторится — начните подключение заново.',
}

export function describeError(code: string | undefined): ErrorCopy {
  if (code && COPY[code]) return COPY[code]
  return FALLBACK
}

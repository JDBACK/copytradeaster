/** Access gate: server-derived read-only states before any wallet flow. */

import { closeMiniApp } from '../telegram'
import { Brand } from '../components/ui'

export type GateState = 'loading' | 'pending' | 'rejected' | 'invalid' | 'error' | 'recovery_required'

export function SessionGate({
  state,
  errorMessage,
  onRetry,
  onRecover,
  isRecovering,
}: {
  state: GateState
  errorMessage?: string
  onRetry?: () => void
  onRecover?: () => void
  isRecovering?: boolean
}) {
  if (state === 'loading') {
    return (
      <div className="screen">
        <Brand right={<span className="badge-testnet">Aster testnet</span>} />
        <div className="skeleton" style={{ minHeight: 220 }} aria-label="Загрузка" />
        <div className="skeleton" style={{ minHeight: 90 }} />
        <div className="footer-note">Проверяем доступ…</div>
      </div>
    )
  }

  if (state === 'invalid') {
    return (
      <div className="screen">
        <Brand right={<span className="badge-testnet">Aster testnet</span>} />
        <h1 className="h1">Откройте из бота</h1>
        <p className="lede">
          Сессия недействительна или устарела. Закройте это окно и запустите мини-ап кнопкой в чате с
          ботом.
        </p>
        <div className="btn-stack">
          <button type="button" className="btn btn-primary" onClick={closeMiniApp}>
            Вернуться в бота
          </button>
        </div>
      </div>
    )
  }

  if (state === 'error') {
    return (
      <div className="screen">
        <Brand right={<span className="badge-testnet">Aster testnet</span>} />
        <h1 className="h1">Ошибка подключения</h1>
        <p className="lede">
          {errorMessage || 'Не удалось связаться с сервером. Проверьте интернет-соединение.'}
        </p>
        <div className="btn-stack">
          {onRetry && (
            <button type="button" className="btn btn-primary" onClick={onRetry}>
              Попробовать снова
            </button>
          )}
          <button type="button" className="btn btn-secondary" onClick={closeMiniApp}>
            Вернуться в бота
          </button>
        </div>
      </div>
    )
  }

  if (state === 'recovery_required') {
    return (
      <div className="screen">
        <Brand right={<span className="badge-testnet">Aster testnet</span>} />
        <h1 className="h1">Подключение сохранено</h1>
        <p className="lede">
          Ключи подключения сохранены в безопасности, но запуск торгового контура был прерван.
          Нажмите кнопку ниже, чтобы завершить запуск без повторного подключения кошелька.
        </p>
        <div className="info-box">
          Повторный ввод подписи не требуется — все разрешения уже подтверждены на бирже.
        </div>
        {errorMessage && <div className="error-box">{errorMessage}</div>}
        <div className="btn-stack">
          <button
            type="button"
            className="btn btn-primary"
            disabled={isRecovering}
            onClick={onRecover}
          >
            {isRecovering ? 'Завершаем запуск…' : 'Завершить запуск'}
          </button>
          <button type="button" className="btn btn-secondary" onClick={closeMiniApp}>
            Вернуться в бота
          </button>
        </div>
      </div>
    )
  }

  const pending = state === 'pending'
  return (
    <div className="screen">
      <Brand right={<span className="badge-testnet">Aster testnet</span>} />
      <h1 className="h1">{pending ? 'Заявка на проверке' : 'Доступ отклонён'}</h1>
      <p className="lede">
        {pending
          ? 'Заявка на доступ уже у администратора. Отправлять повторно не нужно — вернитесь в мини-ап после одобрения.'
          : 'Заявка на доступ отклонена администратором. Причина — в чате с ботом.'}
      </p>
      <div className="info-box">
        Управление подключением и заявками доступно в боте.
      </div>
      <div className="btn-stack">
        <button type="button" className="btn btn-secondary" onClick={closeMiniApp}>
          Вернуться в бота
        </button>
      </div>
    </div>
  )
}

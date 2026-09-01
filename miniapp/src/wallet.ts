/** EVM wallet adapter: one interface, two transports.
 *
 * - `createInjectedAdapter` — EIP-1193 `window.ethereum` (Chrome + MetaMask,
 *   the local dev / desktop-browser acceptance path).
 * - `createWalletConnectAdapter` — WalletConnect v2 relay for Telegram
 *   webviews, which never inject a provider.  Built on
 *   `@walletconnect/ethereum-provider`; activated only when a Reown project
 *   id is provided at build time (`VITE_WC_PROJECT_ID`) — the code ships,
 *   the owner registers the free project id when mobile acceptance starts.
 *
 * Registration signatures always use EIP-712 domain chainId 56 (BSC), even on
 * testnet — mirrors the official Aster demo. */

export const REGISTRATION_CHAIN_ID = 56
export const REGISTRATION_CHAIN_HEX = '0x38'

const BSC_PARAMS = {
  chainId: REGISTRATION_CHAIN_HEX,
  chainName: 'BNB Smart Chain',
  nativeCurrency: { name: 'BNB', symbol: 'BNB', decimals: 18 },
  rpcUrls: ['https://bsc-dataseed.binance.org'],
  blockExplorerUrls: ['https://bscscan.com'],
}

interface EIP1193Provider {
  request(args: { method: string; params?: unknown[] | object }): Promise<unknown>
  on?(event: string, handler: (...args: never[]) => void): void
  removeListener?(event: string, handler: (...args: never[]) => void): void
}

export class WalletError extends Error {
  readonly code: string

  constructor(code: 'wallet_rejected' | 'wrong_network' | 'no_wallet' | 'wallet_unavailable', cause?: unknown) {
    super(code)
    this.code = code
    this.cause = cause
  }
}

export interface WalletAdapter {
  readonly kind: 'injected' | 'walletconnect'
  connect(): Promise<string>
  switchToRegistrationChain(): Promise<void>
  signTypedData(address: string, typedData: unknown): Promise<string>
  disconnect?(): Promise<void>
}

function isRejected(err: unknown): boolean {
  const code = (err as { code?: number })?.code
  return code === 4001 || code === -32000
}

function asProviderError(err: unknown): WalletError {
  const code = (err as { code?: number })?.code
  if (isRejected(err)) return new WalletError('wallet_rejected', err)
  if (code === 4902 || code === -32603) return new WalletError('wrong_network', err)
  return new WalletError('wallet_unavailable', err)
}

// --- injected (EIP-1193) ------------------------------------------------------

export function createInjectedAdapter(): WalletAdapter | null {
  const ethereum = (window as { ethereum?: EIP1193Provider }).ethereum
  if (!ethereum) return null
  const provider: EIP1193Provider = ethereum

  async function switchToRegistrationChain(): Promise<void> {
    try {
      await provider.request({
        method: 'wallet_switchEthereumChain',
        params: [{ chainId: REGISTRATION_CHAIN_HEX }],
      })
    } catch (err) {
      const code = (err as { code?: number })?.code
      if (code === 4902) {
        await provider.request({ method: 'wallet_addEthereumChain', params: [BSC_PARAMS] })
        return
      }
      if (isRejected(err)) throw new WalletError('wallet_rejected', err)
      throw asProviderError(err)
    }
  }

  return {
    kind: 'injected',
    async connect() {
      try {
        const accounts = (await provider.request({
          method: 'eth_requestAccounts',
        })) as string[]
        if (!accounts?.length) throw new WalletError('no_wallet')
        return accounts[0]
      } catch (err) {
        if (err instanceof WalletError) throw err
        throw asProviderError(err)
      }
    },
    switchToRegistrationChain,
    async signTypedData(address, typedData) {
      try {
        return (await provider.request({
          method: 'eth_signTypedData_v4',
          params: [address, JSON.stringify(typedData)],
        })) as string
      } catch (err) {
        if (err instanceof WalletError) throw err
        throw asProviderError(err)
      }
    },
  }
}

// --- WalletConnect (Telegram webviews) ---------------------------------------

export function walletConnectProjectId(): string {
  return (import.meta.env.VITE_WC_PROJECT_ID as string | undefined)?.trim() ?? ''
}

export async function createWalletConnectAdapter(projectId: string): Promise<WalletAdapter> {
  const { EthereumProvider } = await import('@walletconnect/ethereum-provider')
  const provider = await EthereumProvider.init({
    projectId,
    metadata: {
      name: 'ASTER COPY',
      description: 'Копитрейд-кабинет Aster: подключение Agent + Builder',
      url: window.location.origin,
      icons: [`${window.location.origin}/miniapp/favicon.svg`],
    },
    showQrModal: true,
    chains: [REGISTRATION_CHAIN_ID],
    methods: ['eth_requestAccounts', 'eth_signTypedData_v4', 'wallet_switchEthereumChain'],
    events: ['chainChanged', 'accountsChanged'],
  })

  return {
    kind: 'walletconnect',
    async connect() {
      try {
        // WC requires an explicit connect(): it opens the wallet-choice
        // modal / QR and resolves once the wallet approves the session.
        // eth_requestAccounts before connect() throws
        // "Please call connect() before request()".
        await provider.connect()
        const accounts = provider.accounts as string[]
        if (!accounts?.length) throw new WalletError('no_wallet')
        return accounts[0]
      } catch (err) {
        if (err instanceof WalletError) throw err
        throw asProviderError(err)
      }
    },
    async switchToRegistrationChain() {
      // Many mobile wallets switch chains inside their own UI; the request is
      // best-effort because the EIP-712 domain chainId (56) does not have to
      // match the wallet's active network for the signature to be valid.
      try {
        await provider.request({
          method: 'wallet_switchEthereumChain',
          params: [{ chainId: REGISTRATION_CHAIN_HEX }],
        })
      } catch {
        /* keep going: signing does not require the switch to succeed */
      }
    },
    async signTypedData(address, typedData) {
      try {
        return (await provider.request({
          method: 'eth_signTypedData_v4',
          params: [address, JSON.stringify(typedData)],
        })) as string
      } catch (err) {
        if (err instanceof WalletError) throw err
        throw asProviderError(err)
      }
    },
    async disconnect() {
      try {
        await provider.disconnect()
      } catch {
        /* already disconnected */
      }
    },
  }
}

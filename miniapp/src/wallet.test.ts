import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  createInjectedAdapter,
  REGISTRATION_CHAIN_HEX,
  WalletError,
  walletConnectProjectId,
} from './wallet'

interface ProviderMock {
  ethereum: {
    request: ReturnType<typeof vi.fn>
  }
}

function installProvider(request: ReturnType<typeof vi.fn>): ProviderMock {
  const mock = { ethereum: { request } }
  vi.stubGlobal('window', mock)
  return mock
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('injected wallet adapter', () => {
  it('returns null without window.ethereum', () => {
    vi.stubGlobal('window', {})
    expect(createInjectedAdapter()).toBeNull()
  })

  it('connects and returns the first account', async () => {
    const request = vi.fn(async () => ['0xabc', '0xdef'])
    installProvider(request)
    const adapter = createInjectedAdapter()
    expect(adapter).not.toBeNull()
    await expect(adapter!.connect()).resolves.toBe('0xabc')
    expect(request).toHaveBeenCalledWith({ method: 'eth_requestAccounts' })
  })

  it('signs typed data with the JSON-stringified payload', async () => {
    const request = vi.fn(async () => '0xsig')
    installProvider(request)
    const adapter = createInjectedAdapter()!
    const typed = { primaryType: 'ApproveAgent', message: { User: '0xabc' } }
    await expect(adapter.signTypedData('0xabc', typed)).resolves.toBe('0xsig')
    expect(request).toHaveBeenCalledWith({
      method: 'eth_signTypedData_v4',
      params: ['0xabc', JSON.stringify(typed)],
    })
  })

  it('switches to the registration chain 56 (0x38)', async () => {
    const request = vi.fn(async () => null)
    installProvider(request)
    const adapter = createInjectedAdapter()!
    await adapter.switchToRegistrationChain()
    expect(request).toHaveBeenCalledWith({
      method: 'wallet_switchEthereumChain',
      params: [{ chainId: REGISTRATION_CHAIN_HEX }],
    })
  })

  it('adds the BSC chain when the wallet does not know it (4902)', async () => {
    const request = vi
      .fn()
      .mockRejectedValueOnce(Object.assign(new Error('unrecognized chain'), { code: 4902 }))
      .mockResolvedValueOnce(null)
    installProvider(request)
    const adapter = createInjectedAdapter()!
    await adapter.switchToRegistrationChain()
    expect(request).toHaveBeenLastCalledWith({
      method: 'wallet_addEthereumChain',
      params: [expect.objectContaining({ chainId: REGISTRATION_CHAIN_HEX })],
    })
  })

  it('maps user rejection (4001) to the typed wallet_rejected error', async () => {
    const request = vi
      .fn()
      .mockRejectedValue(Object.assign(new Error('user rejected'), { code: 4001 }))
    installProvider(request)
    const adapter = createInjectedAdapter()!
    await expect(adapter.connect()).rejects.toMatchObject({ code: 'wallet_rejected' })
    await expect(
      adapter.signTypedData('0xabc', {}),
    ).rejects.toBeInstanceOf(WalletError)
  })

  it('throws no_wallet when the wallet returns an empty account list', async () => {
    installProvider(vi.fn(async () => []))
    const adapter = createInjectedAdapter()!
    await expect(adapter.connect()).rejects.toMatchObject({ code: 'no_wallet' })
  })
})

describe('walletconnect seam', () => {
  it('stays disabled until a Reown project id is provided', () => {
    expect(walletConnectProjectId()).toBe('')
  })
})

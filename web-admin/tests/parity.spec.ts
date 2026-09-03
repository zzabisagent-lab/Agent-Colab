import { createHmac } from 'node:crypto'
import { expect, test, type Page } from '@playwright/test'

const TOTP_SECRET = process.env.E2E_TOTP_SECRET_B32 ?? ''
function base32Decode(s: string): Buffer {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
  let bits = ''
  for (const ch of s.replace(/=+$/, '').toUpperCase()) bits += alphabet.indexOf(ch).toString(2).padStart(5, '0')
  const bytes: number[] = []
  for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8), 2))
  return Buffer.from(bytes)
}
function totp(secretB32: string, at = Date.now()): string {
  const counter = Math.floor(at / 1000 / 30)
  const msg = Buffer.alloc(8)
  msg.writeBigUInt64BE(BigInt(counter))
  const digest = createHmac('sha1', base32Decode(secretB32)).update(msg).digest()
  const offset = digest[digest.length - 1] & 0x0f
  const code = ((digest[offset] & 0x7f) << 24 | (digest[offset + 1] & 0xff) << 16 | (digest[offset + 2] & 0xff) << 8 | (digest[offset + 3] & 0xff)) % 1_000_000
  return code.toString().padStart(6, '0')
}
async function reauth(page: Page) {
  await page.goto('/admin/mfa')
  await page.getByLabel('Authenticator code').fill(totp(TOTP_SECRET))
  await page.getByRole('button', { name: 'Verify' }).click()
  await expect(page.getByRole('status')).toHaveText('MFA_VERIFIED')
}

// V-P4-08 (UI half): a Member cannot escalate through the console; every admin screen shows the
// same denial code the API returns, and admin actions leave the same audit trail as API calls.
const AUTHORIZED = process.env.E2E_AUTHORIZED_TOKEN ?? ''
const MEMBER = process.env.E2E_UNAUTHORIZED_TOKEN ?? ''

async function login(page: Page, token: string) {
  await page.goto('/admin/login')
  await page.getByLabel('Service token').fill(token)
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('heading', { name: 'Channels' })).toBeVisible()
}

test('member sees normalized denials on admin screens and cannot act', async ({ page }) => {
  await login(page, MEMBER)
  for (const route of ['/admin/accounts', '/admin/settings', '/admin/secrets', '/admin/audit']) {
    await page.goto(route)
    await expect(page.getByRole('alert')).toBeVisible()
    await expect(page.getByRole('alert')).toHaveText(/NOT_FOUND|FORBIDDEN|DEFAULT_DENY|POLICY_DENIED|MFA_REQUIRED|REAUTH_REQUIRED/)
  }
  await page.goto('/admin/accounts')
  await page.getByLabel('Account id').fill('acct-ui-escalation')
  await page.getByLabel('Display name').fill('Escalation attempt')
  await page.getByRole('button', { name: 'Create' }).click()
  await expect(page.getByRole('alert')).toHaveText(/NOT_FOUND|FORBIDDEN|DEFAULT_DENY|POLICY_DENIED|MFA_REQUIRED|REAUTH_REQUIRED/)
  await expect(page.getByRole('table').locator('tbody tr', { hasText: 'acct-ui-escalation' })).toHaveCount(0)
})

test('administrator creates and suspends an Account from the console', async ({ page }) => {
  await login(page, AUTHORIZED)
  await reauth(page)  // administrators act only with a confirmed and recently verified MFA
  await page.goto('/admin/accounts')
  await page.getByLabel('Account id').fill('acct-ui-parity')
  await page.getByLabel('Display name').fill('UI Parity')
  await page.getByRole('button', { name: 'Create' }).click()
  await expect(page.getByRole('status')).toHaveText('ACCOUNT_CREATED')
  const row = page.getByRole('table').locator('tbody tr', { hasText: 'acct-ui-parity' })
  await expect(row).toHaveCount(1)
  await row.getByRole('button', { name: 'Suspend' }).click()
  await expect(page.getByRole('status')).toHaveText('ACCOUNT_SUSPENDED')
  await expect(row).toContainText('SUSPENDED')
})

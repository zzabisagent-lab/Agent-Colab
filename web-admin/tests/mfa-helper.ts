import { createHmac } from 'node:crypto'
import { expect, type Page } from '@playwright/test'

// Administrators act only with a confirmed and recently verified TOTP factor (P4-09); the
// pytest driver enrolls the account through the API and passes the base32 secret in the env.
export function base32Decode(s: string): Buffer {
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
  let bits = ''
  for (const ch of s.replace(/=+$/, '').toUpperCase()) bits += alphabet.indexOf(ch).toString(2).padStart(5, '0')
  const bytes: number[] = []
  for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8), 2))
  return Buffer.from(bytes)
}

export function totp(secretB32: string, at = Date.now()): string {
  const counter = Math.floor(at / 1000 / 30)
  const msg = Buffer.alloc(8)
  msg.writeBigUInt64BE(BigInt(counter))
  const digest = createHmac('sha1', base32Decode(secretB32)).update(msg).digest()
  const offset = digest[digest.length - 1] & 0x0f
  const code = ((digest[offset] & 0x7f) << 24 | (digest[offset + 1] & 0xff) << 16 | (digest[offset + 2] & 0xff) << 8 | (digest[offset + 3] & 0xff)) % 1_000_000
  return code.toString().padStart(6, '0')
}

/** Verify MFA on the console's MFA screen when the driver supplied a secret. */
export async function reauthIfConfigured(page: Page) {
  const secret = process.env.E2E_TOTP_SECRET_B32 ?? ''
  if (!secret) return
  await page.goto('/admin/mfa')
  await page.getByLabel('Authenticator code').fill(totp(secret))
  await page.getByRole('button', { name: 'Verify' }).click()
  await expect(page.getByRole('status')).toHaveText('MFA_VERIFIED')
}

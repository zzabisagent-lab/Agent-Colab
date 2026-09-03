import { expect, test, type Page } from '@playwright/test'
import { reauthIfConfigured } from './mfa-helper'

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
  await reauthIfConfigured(page)
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

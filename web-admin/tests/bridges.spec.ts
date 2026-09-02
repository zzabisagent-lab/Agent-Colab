import { expect, test } from '@playwright/test'

// Driven by tests/e2e/test_admin_bridges_ui.py: the server seeds accounts and exports tokens.
const AUTHORIZED = process.env.E2E_AUTHORIZED_TOKEN ?? ''
const UNAUTHORIZED = process.env.E2E_UNAUTHORIZED_TOKEN ?? ''
const CHANNEL = process.env.E2E_CHANNEL_ID ?? ''
const INSTANCE = process.env.E2E_TG_INSTANCE ?? ''
const CHAT = process.env.E2E_TG_CHAT ?? '-1001234567890'

async function login(page: import('@playwright/test').Page, token: string) {
  await page.goto('/admin/login')
  await page.getByLabel('Service token').fill(token)
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('heading', { name: 'Channels' })).toBeVisible()
}

test('unauthorized account cannot change Bridges from the UI', async ({ page }) => {
  await login(page, UNAUTHORIZED)
  await page.goto(`/admin/channels/${CHANNEL}/bridges`)
  await page.getByLabel('Telegram provider instance id').fill(INSTANCE)
  await page.getByLabel('Telegram chat id').fill(CHAT)
  await page.getByRole('button', { name: 'Create' }).click()
  const alert = page.getByRole('alert')
  await expect(alert).toBeVisible()
  await expect(alert).not.toHaveText('')
  await expect(page.getByRole('table').locator('tbody tr')).toHaveCount(0)
})

test('authorized administrator creates, disables and re-enables a Bridge', async ({ page }) => {
  await login(page, AUTHORIZED)
  await page.goto(`/admin/channels/${CHANNEL}/bridges`)
  await page.getByLabel('Telegram provider instance id').fill(INSTANCE)
  await page.getByLabel('Telegram chat id').fill(CHAT)
  await page.getByRole('button', { name: 'Create' }).click()
  await expect(page.getByRole('status')).toHaveText('BRIDGE_CREATED')
  const rows = page.getByRole('table').locator('tbody tr')
  await expect(rows).toHaveCount(1)
  await rows.first().getByRole('button', { name: 'Disable' }).click()
  await expect(page.getByRole('status')).toHaveText('BRIDGE_DISABLED')
  await expect(rows.first()).toContainText('disabled')
  await rows.first().getByRole('button', { name: 'Enable' }).click()
  await expect(page.getByRole('status')).toHaveText('BRIDGE_ENABLED')
  await expect(rows.first()).toContainText('enabled')
})

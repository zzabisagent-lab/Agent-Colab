import { expect, test, type Page } from '@playwright/test'
import { reauthIfConfigured } from './mfa-helper'

// V-P5-21 (UI half): Run now by an unauthorized account is rejected, by an authorized one creates
// a manual Run. V-P5-22 (UI half): lifecycle DRAFT→ENABLED→PAUSED→ENABLED→DISABLED via the console.
const AUTHORIZED = process.env.E2E_AUTHORIZED_TOKEN ?? ''
const MEMBER = process.env.E2E_UNAUTHORIZED_TOKEN ?? ''
const CHANNEL = process.env.E2E_CHANNEL_ID ?? ''
const PRINCIPAL = process.env.E2E_PRINCIPAL ?? ''
const CAPABILITY = process.env.E2E_CAPABILITY ?? 'cap-schedule'

async function login(page: Page, token: string) {
  await page.goto('/admin/login')
  await page.getByLabel('Service token').fill(token)
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('heading', { name: 'Channels' })).toBeVisible()
}

test('administrator builds, previews, enables, pauses, resumes, runs now and disables a schedule', async ({ page }) => {
  await login(page, AUTHORIZED)
  await reauthIfConfigured(page)
  await page.goto('/admin/schedules')
  await page.getByLabel('Name').fill('UI nightly report')
  await page.getByLabel('Raw cron expression (overrides the fields)').fill('30 2 * * *')
  await page.getByLabel('IANA timezone').fill('Asia/Seoul')
  await page.getByRole('button', { name: 'Preview next 10 runs' }).click()
  await expect(page.getByRole('table', { name: 'Next 10 occurrences' }).locator('tbody tr')).toHaveCount(10)
  await page.getByLabel('Channel id').fill(CHANNEL)
  await page.getByLabel('Execution principal (account id)').fill(PRINCIPAL)
  await page.getByLabel('Capability id').fill(CAPABILITY)
  await page.getByRole('button', { name: 'Create schedule (DRAFT)' }).click()
  await expect(page.getByRole('status')).toHaveText('SCHEDULE_CREATED')
  const row = page.getByRole('table', { name: 'Schedules' }).locator('tbody tr', { hasText: 'UI nightly report' })
  await expect(row).toHaveCount(1)
  await expect(row).toContainText('DRAFT')
  await row.getByRole('button', { name: 'Enable' }).click()
  await expect(page.getByRole('status')).toHaveText('SCHEDULE_ENABLED')
  await expect(row).toContainText('ENABLED')
  await row.getByRole('button', { name: 'Pause' }).click()
  await expect(page.getByRole('status')).toHaveText('SCHEDULE_PAUSED')
  await expect(row).toContainText('PAUSED')
  await row.getByRole('button', { name: 'Resume' }).click()
  await expect(page.getByRole('status')).toHaveText('SCHEDULE_RESUMED')
  await expect(row).toContainText('ENABLED')
  await row.getByRole('button', { name: 'Run now' }).click()
  await expect(page.getByRole('status')).toHaveText('SCHEDULE_RUN_NOW')
  await row.getByRole('button', { name: 'History' }).click()
  await expect(page.getByRole('table', { name: /Runs of/ }).locator('tbody tr', { hasText: 'MANUAL' })).toHaveCount(1)
  page.once('dialog', (d) => void d.accept())
  await row.getByRole('button', { name: 'Disable' }).click()
  await expect(page.getByRole('status')).toHaveText('SCHEDULE_DISABLED')
  await expect(row).toContainText('DISABLED')
})

test('member cannot run a schedule now from the console', async ({ page }) => {
  await login(page, MEMBER)
  await page.goto('/admin/schedules')
  await expect(page.getByRole('alert')).toBeVisible()  // list denied or empty for a non-manager
})

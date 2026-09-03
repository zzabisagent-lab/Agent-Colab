import { expect, test } from '@playwright/test'

// V-P4-01: a clean environment is configured and locked through the Web Wizard alone.
const env = (k: string) => process.env[k] ?? ''

test('web setup wizard configures an empty environment to LOCKED', async ({ page }) => {
  test.setTimeout(180_000)
  await page.goto('/admin/setup')
  await expect(page.getByTestId('setup-state')).toHaveText('UNINITIALIZED')
  await page.getByRole('button', { name: 'Issue setup token' }).click()
  await expect(page.getByRole('status')).toContainText('SETUP_TOKEN_ISSUED')
  await page.getByLabel('Host').fill(env('E2E_DB_HOST'))
  await page.getByLabel('Port').fill(env('E2E_DB_PORT'))
  await page.getByLabel('Database name').fill(env('E2E_DB_NAME'))
  await page.getByLabel('User').fill(env('E2E_DB_USER'))
  await page.getByLabel('Master key file path (owner-only, outside backups)').fill(env('E2E_KEY_PATH'))
  await page.getByLabel('Mattermost URL').fill('http://mattermost.test:8065')
  await page.getByLabel('Mattermost team').fill('colab')
  await page.getByLabel('Mattermost bot token').fill('mm-bot-secret-0001')
  await page.getByLabel('Artifact storage root').fill(env('E2E_ARTIFACT_ROOT'))
  await page.getByLabel('Document storage root').fill(env('E2E_DOCUMENT_ROOT'))
  await page.getByLabel('Ops channel id').fill('ops-channel')
  await page.getByRole('button', { name: 'Save sections and run preflight' }).click()
  await expect(page.getByRole('status')).toHaveText('SETUP_PREFLIGHT_PASSED')
  await expect(page.getByRole('table')).toContainText('passed')
  page.once('dialog', (d) => void d.accept())
  await page.getByRole('button', { name: '5. Apply and lock' }).click()
  await expect(page.getByTestId('setup-state')).toHaveText('LOCKED', { timeout: 120_000 })
  await expect(page.getByTestId('recovery-code')).not.toBeEmpty()
})

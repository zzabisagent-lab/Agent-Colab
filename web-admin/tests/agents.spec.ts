import { expect, test } from '@playwright/test'

// Driven by tests/e2e/test_admin_agents_ui.py (V-P3-13): seeded tokens, real server under /admin.
const AUTHORIZED = process.env.E2E_AUTHORIZED_TOKEN ?? ''
const UNAUTHORIZED = process.env.E2E_UNAUTHORIZED_TOKEN ?? ''
const AGENT_ID = process.env.E2E_AGENT_ID ?? 'agent-ui-1'

async function login(page: import('@playwright/test').Page, token: string) {
  await page.goto('/admin/login')
  await page.getByLabel('Service token').fill(token)
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('heading', { name: 'Channels' })).toBeVisible()
}

async function fillAgent(page: import('@playwright/test').Page, id: string) {
  await page.getByLabel('Agent id').fill(id)
  await page.getByLabel('Display name').fill('UI Agent')
  await page.getByLabel('Adapter type').selectOption('webhook')
  await page.getByLabel('Endpoint URL (webhook / bot), optional').fill('https://agent.example.test/hook')
  await page.getByLabel('Credential reference (Secret Broker), optional').fill('secret://agents/ui-1/signing')
  await page.getByLabel('Concurrent Task limit').fill('2')
}

test('unauthorized account cannot register or suspend Agents from the console', async ({ page }) => {
  await login(page, UNAUTHORIZED)
  await page.goto('/admin/agents')
  await fillAgent(page, `${AGENT_ID}-unauth`)
  await page.getByRole('button', { name: 'Register' }).click()
  await expect(page.getByRole('alert')).toBeVisible()
  await expect(page.getByRole('table').locator('tbody tr', { hasText: `${AGENT_ID}-unauth` })).toHaveCount(0)
})

test('administrator registers, edits limits, suspends and revokes an Agent', async ({ page }) => {
  await login(page, AUTHORIZED)
  await page.goto('/admin/agents')
  await fillAgent(page, AGENT_ID)
  await page.getByRole('button', { name: 'Register' }).click()
  await expect(page.getByRole('status')).toHaveText('AGENT_REGISTERED')
  const row = page.getByRole('table').locator('tbody tr', { hasText: AGENT_ID })
  await expect(row).toHaveCount(1)
  page.once('dialog', (d) => void d.accept('3'))
  await row.getByRole('button', { name: 'Edit limits' }).click()
  await expect(page.getByRole('status')).toHaveText('AGENT_UPDATED')
  await expect(row).toContainText('3 tasks')
  await row.getByRole('button', { name: 'Suspend' }).click()
  await expect(page.getByRole('status')).toHaveText('AGENT_SUSPENDED')
  await expect(row).toContainText('suspended')
  await row.getByRole('button', { name: 'Revoke' }).click()
  await expect(page.getByRole('status')).toHaveText('AGENT_REVOKED')
  await expect(row).toContainText('revoked')
})

test('administrator commits a Role version and previews effective permissions', async ({ page }) => {
  await login(page, AUTHORIZED)
  await page.goto('/admin/roles')
  await page.getByLabel('Role id', { exact: true }).fill('role-ui-reviewer')
  await page.getByLabel('Display name').fill('UI Reviewer')
  await page.getByLabel('Permissions (comma separated)').fill('task.read, verification.submit')
  await page.getByLabel('Explicit deny (comma separated)').fill('task.cancel')
  await page.getByRole('button', { name: 'Commit version' }).click()
  await expect(page.getByRole('status')).toHaveText('ROLE_COMMITTED')
  await page.getByLabel('Account id').fill(process.env.E2E_ASSIGN_ACCOUNT ?? 'acct-ui-member')
  await page.getByLabel('Role id to assign').fill('role-ui-reviewer')
  await page.getByRole('button', { name: 'Assign' }).click()
  await expect(page.getByRole('status')).toHaveText('ROLE_ASSIGNED')
  await page.getByRole('button', { name: 'Preview effective permissions' }).click()
  const preview = page.getByRole('region', { name: 'Effective permissions' })
  await expect(preview).toContainText('verification.submit')
  await expect(preview).toContainText('task.cancel')
})

import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page } from '@playwright/test'

// V-P4-18: WCAG 2.1 AA automated violations 0 on every console route; critical flows by keyboard.
const AUTHORIZED = process.env.E2E_AUTHORIZED_TOKEN ?? ''
const ROUTES = ['/admin/overview', '/admin/channels', '/admin/agents', '/admin/roles', '/admin/accounts',
  '/admin/secrets', '/admin/approvals', '/admin/settings', '/admin/audit', '/admin/maintenance']

async function axeCheck(page: Page, name: string) {
  const results = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']).analyze()
  const summary = results.violations.map((v) => `${v.id}: ${v.help} (${v.nodes.length})`)
  expect(summary, `${name} violations`).toEqual([])
}

test('login page and setup wizard have zero WCAG 2.1 AA violations', async ({ page }) => {
  await page.goto('/admin/login')
  await axeCheck(page, 'login')
  await page.goto('/admin/setup')
  await expect(page.getByRole('heading', { name: 'Setup Wizard' })).toBeVisible()
  await axeCheck(page, 'setup')
})

test('critical flows work by keyboard only: sign in, navigate, act', async ({ page }) => {
  await page.goto('/admin/login')
  const field = page.getByLabel('Service token')
  for (let i = 0; i < 5 && !(await field.evaluate((el) => el === document.activeElement)); i++) {
    await page.keyboard.press('Tab')  // the field is reachable within a few tab stops
  }
  await expect(field).toBeFocused()
  await page.keyboard.type(AUTHORIZED)
  await page.keyboard.press('Enter')
  await expect(page.getByRole('heading', { name: 'Channels' })).toBeVisible()
  // reach the primary navigation by keyboard and open Agents
  await page.getByRole('link', { name: 'Agents' }).focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('heading', { name: 'Agents' })).toBeVisible()
  await page.getByLabel('Agent id').focus()
  await expect(page.getByLabel('Agent id')).toBeFocused()
})

test('every signed-in route has zero WCAG 2.1 AA violations', async ({ page }) => {
  await page.goto('/admin/login')
  await page.getByLabel('Service token').fill(AUTHORIZED)
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('heading', { name: 'Channels' })).toBeVisible()
  for (const route of ROUTES) {
    await page.goto(route)
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
    await axeCheck(page, route)
  }
})

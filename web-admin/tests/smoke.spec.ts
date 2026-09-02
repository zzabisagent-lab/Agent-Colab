import { expect, test } from '@playwright/test'

test('login page renders with an accessible form', async ({ page }) => {
  await page.goto('/login')
  await expect(page.getByRole('heading', { name: 'Agent-Colab Admin' })).toBeVisible()
  await expect(page.getByLabel('Service token')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()
})

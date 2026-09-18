import { expect, test } from '@playwright/test';

async function signIn(page) {
  await page.goto('/test-login?user=7');
  await expect(page).toHaveURL(/\/eventos\/pipeline$/);
}

test('grupos de consumo teórico abrem e fecham com estado acessível sincronizado', async ({ page }) => {
  await signIn(page);
  await page.goto('/eurokg/consumo');

  const toggle = page.locator('[data-consumo-family-toggle]').first();
  await expect(toggle).toContainText('Palito');

  const detailId = await toggle.getAttribute('aria-controls');
  expect(detailId).toBeTruthy();
  const details = page.locator(`tbody#${detailId}`);

  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await expect(details).toBeHidden();
  await expect(details.locator('tr')).toHaveCount(2);

  await toggle.press('Enter');
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  await expect(details).toBeVisible();
  await expect(details).toHaveAttribute('id', detailId);

  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await expect(details).toBeHidden();
});
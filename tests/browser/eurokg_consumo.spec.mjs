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

test('grupos de consumo teórico continuam sincronizados depois de trocar de loja', async ({ page }) => {
  await signIn(page);
  await page.goto('/eurokg/consumo');

  await expect(page.getByRole('link', { name: 'Bolhão', exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: 'Matosinhos', exact: true })).toBeVisible();

  await page.getByRole('link', { name: 'Matosinhos', exact: true }).click();
  await expect(page).toHaveURL(/\/eurokg\/consumo\?loja=Matosinhos/);

  const storeToggle = page.locator('[data-consumo-family-toggle]').first();
  await expect(storeToggle).toContainText('Palito');

  const detailId = await storeToggle.getAttribute('aria-controls');
  expect(detailId).toBeTruthy();
  const storeDetails = page.locator(`tbody#${detailId}`);

  await expect(storeToggle).toHaveAttribute('aria-expanded', 'false');
  await expect(storeDetails).toBeHidden();
  await expect(storeDetails.locator('tr')).toHaveCount(2);

  await storeToggle.press('Enter');
  await expect(storeToggle).toHaveAttribute('aria-expanded', 'true');
  await expect(storeDetails).toBeVisible();
  await expect(storeDetails).toHaveAttribute('id', detailId);

  await storeToggle.click();
  await expect(storeToggle).toHaveAttribute('aria-expanded', 'false');
  await expect(storeDetails).toBeHidden();
});

test('filtro de consumos incompletos mantém o estado ao abrir e fechar famílias', async ({ page }) => {
  await signIn(page);
  await page.goto('/eurokg/consumo');

  const filter = page.locator('[data-consumo-filter-incompletos]');
  await expect(filter).toBeVisible();
  const toggle = page.locator('[data-consumo-family-toggle]').first();
  const detailId = await toggle.getAttribute('aria-controls');
  const details = page.locator(`tbody#${detailId}`);
  await toggle.press('Enter');
  await filter.check();
  await expect(filter).toBeChecked();
  await filter.uncheck();
  await expect(details).toBeVisible();
  await toggle.click();
  await expect(filter).not.toBeChecked();
  await expect(details).toBeHidden();
});

import { expect, test } from '@playwright/test';

async function signIn(page) {
  await page.goto('/test-login?user=7');
  await expect(page).toHaveURL(/\/eventos\/pipeline$/);
}

function storeCard(page, storeName) {
  return page.locator('.card').filter({
    hasText: `🛒 Loja — ${storeName}`,
  });
}

function dashboardRow(card) {
  return card.locator('.tile-rename-row:has([data-tile-id="dashboard"])');
}

test('personalizar uma loja mantém a outra sem alterações', async ({ page }) => {
  await signIn(page);
  await page.goto('/gestor/gestao-tiles');

  const bolhao = storeCard(page, 'Bolhão');
  const matosinhos = storeCard(page, 'Matosinhos');
  await expect(bolhao).toBeVisible();
  await expect(matosinhos).toBeVisible();

  const bolhaoRow = dashboardRow(bolhao);
  const matosinhosRow = dashboardRow(matosinhos);
  await expect(bolhaoRow.locator('.tile-label-input')).toHaveValue('Resumo Diário');
  await expect(matosinhosRow.locator('.tile-label-input')).toHaveValue('Resumo Diário');
  await expect(matosinhosRow.locator('.icon-btn')).toHaveText('📊');
  await expect(matosinhosRow.locator('.tile-toggle')).toHaveAttribute('data-visible', '1');

  const renameResponse = page.waitForResponse(
    response => response.url().endsWith('/gestao-tiles/rename') &&
      response.request().method() === 'POST',
  );
  await bolhaoRow.locator('.tile-label-input').fill('Resumo Bolhão');
  await bolhaoRow.locator('button[title="Guardar nome"]').click();
  await expect(await (await renameResponse).json()).toMatchObject({
    ok: true,
    store_id: 1,
    tile_id: 'dashboard',
    label: 'Resumo Bolhão',
  });

  const toggleResponse = page.waitForResponse(
    response => response.url().endsWith('/gestao-tiles/toggle') &&
      response.request().method() === 'POST',
  );
  await bolhaoRow.getByRole('button', { name: /Visível/ }).click();
  await expect(await (await toggleResponse).json()).toMatchObject({
    ok: true,
    store_id: 1,
    tile_id: 'dashboard',
    visible: false,
  });

  const iconResponse = page.waitForResponse(
    response => response.url().endsWith('/gestao-tiles/icon') &&
      response.request().method() === 'POST',
  );
  await bolhaoRow.locator('.icon-btn').click();
  await page.locator('#icon-grid button[title="🎯"]').click();
  await expect(await (await iconResponse).json()).toMatchObject({
    ok: true,
    store_id: 1,
    tile_id: 'dashboard',
    icon: '🎯',
  });

  await page.reload();

  const reloadedBolhao = dashboardRow(storeCard(page, 'Bolhão'));
  const reloadedMatosinhos = dashboardRow(storeCard(page, 'Matosinhos'));
  await expect(reloadedBolhao.locator('.tile-label-input')).toHaveValue('Resumo Bolhão');
  await expect(reloadedBolhao.locator('.icon-btn')).toHaveText('🎯');
  await expect(reloadedBolhao.locator('.tile-toggle')).toHaveAttribute('data-visible', '0');

  await expect(reloadedMatosinhos.locator('.tile-label-input')).toHaveValue('Resumo Diário');
  await expect(reloadedMatosinhos.locator('.icon-btn')).toHaveText('📊');
  await expect(reloadedMatosinhos.locator('.tile-toggle')).toHaveAttribute('data-visible', '1');
  await expect(reloadedMatosinhos.locator('.tile-toggle')).toContainText('Visível');
});
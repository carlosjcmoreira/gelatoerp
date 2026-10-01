import { expect, test } from '@playwright/test';

const captureSnapshot = (eventTarget, eventName) => eventTarget.evaluate(
  (element, eventType) => {
    element.addEventListener(eventType, () => {
      const table = document.querySelector('.table-responsive');
      const snapshot = {
        x: window.scrollX,
        y: window.scrollY,
        tableScrollLeft: table ? table.scrollLeft : 0,
        openDetails: Array.from(
          document.querySelectorAll('.collapse.show[id^="artigo-detalhe-"]')
        ).map((detail) => detail.id),
      };
      sessionStorage.setItem(
        '__compras_browser_expected_context',
        JSON.stringify(snapshot)
      );
    }, { capture: true, once: true });
  },
  eventName
);

const savedSnapshot = async (page) => JSON.parse(
  await page.evaluate(() =>
    sessionStorage.getItem('__compras_browser_expected_context')
  )
);

const activeScrollTokens = async (page) => page.evaluate(() =>
  Object.keys(sessionStorage).filter((key) =>
    key.startsWith('compras-artigos-scroll:')
  )
);

const clampedScroll = async (page, snapshot) => page.evaluate((saved) => {
  const root = document.documentElement;
  const maxY = Math.max(0, root.scrollHeight - window.innerHeight);
  const table = document.querySelector('.table-responsive');
  return {
    y: window.scrollY,
    expectedY: Math.min(saved.y, maxY),
    tableScrollLeft: table ? table.scrollLeft : 0,
  };
}, snapshot);

test.beforeEach(async ({ request }) => {
  await request.post('/_test-reset');
});

test('supplier replacement keeps filters, open details, and scroll when the row disappears', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 850 });
  await page.goto('/test-login');
  await page.goto('/compras/artigos?q=Fornecedor+Antigo&revisao=por_rever');

  for (const articleId of [1, 2]) {
    const productButton = page.getByRole('button', {
      name: new RegExp(`Artigo ${String(articleId).padStart(3, '0')}`),
    });
    await productButton.click();
    await expect(page.locator(`#artigo-detalhe-${articleId}`))
      .toHaveClass(/show/);
  }

  const targetButton = page.getByRole('button', { name: /Artigo 090/ });
  await targetButton.scrollIntoViewIfNeeded();
  await targetButton.click();
  await expect(page.locator('#artigo-detalhe-90')).toHaveClass(/show/);

  const supplierButton = page.locator(
    '#artigo-detalhe-90 [data-bs-target="#fornecedorOficialModal"]'
  );
  await supplierButton.scrollIntoViewIfNeeded();

  await supplierButton.click();
  const modal = page.locator('#fornecedorOficialModal');
  await expect(modal).toBeVisible();
  await modal.getByRole('button', { name: 'Cancelar' }).click();
  await expect(modal).not.toBeVisible();
  expect(await activeScrollTokens(page)).toEqual([]);

  await supplierButton.scrollIntoViewIfNeeded();
  await captureSnapshot(supplierButton, 'click');
  await supplierButton.click();
  await expect(modal).toBeVisible();
  await modal.locator('#fornecedorOficialSupplierId').selectOption('2');
  await modal.getByRole('button', { name: 'Guardar ligação' }).click();

  await expect(page).toHaveURL(/revisao=por_rever/);
  expect(new URL(page.url()).searchParams.get('q')).toBe('fornecedor antigo');
  await expect(page.getByRole('searchbox')).toHaveValue('fornecedor antigo');
  await expect(page.getByLabel('Filtrar pelo estado da revisão manual'))
    .toHaveValue('por_rever');
  await expect(page.locator('#artigo-detalhe-90')).toHaveCount(0);
  for (const articleId of [1, 2]) {
    await expect(page.locator(`#artigo-detalhe-${articleId}`))
      .toHaveClass(/show/);
  }
  await expect(page.locator('[role="alert"]'))
    .toContainText('Fornecedor registado atualizado');

  const snapshot = await savedSnapshot(page);
  await expect.poll(async () => {
    const current = await clampedScroll(page, snapshot);
    return Math.abs(current.y - current.expectedY);
  }).toBeLessThanOrEqual(3);
  const restored = await clampedScroll(page, snapshot);
  expect(restored.tableScrollLeft).toBe(snapshot.tableScrollLeft);
  expect(snapshot.openDetails).toContain('artigo-detalhe-1');
  expect(snapshot.openDetails).toContain('artigo-detalhe-2');
  expect(snapshot.openDetails).toContain('artigo-detalhe-90');
  expect(await activeScrollTokens(page)).toEqual([]);

  await page.goto('/compras/artigos');
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
  expect(await activeScrollTokens(page)).toEqual([]);
});

test('deactivation preserves a narrow table position after cancelling and accepting the unsaved-changes prompt', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 780 });
  await page.goto('/test-login');
  await page.goto('/compras/artigos?q=Fornecedor+Antigo&revisao=por_rever');

  await page.locator('[name="categoria_artigo_1"]').selectOption('Bebidas e café');
  const toggleForm = page.locator('form[data-unsaved-navigation]')
    .filter({ has: page.locator('[name="action"][value="toggle"]') })
    .filter({ has: page.locator('[name="artigo_id"][value="90"]') });
  const deactivateButton = toggleForm.getByRole('button', { name: 'Desativar' });

  await page.evaluate(() => {
    const table = document.querySelector('.table-responsive');
    if (table) table.scrollLeft = table.scrollWidth;
    window.scrollTo(
      0,
      Math.max(0, document.documentElement.scrollHeight - window.innerHeight - 180)
    );
  });
  await deactivateButton.scrollIntoViewIfNeeded();

  let confirmCount = 0;
  page.on('dialog', async (dialog) => {
    confirmCount += 1;
    await dialog.dismiss();
  });
  await deactivateButton.click();
  expect(confirmCount).toBe(1);
  expect(await activeScrollTokens(page)).toEqual([]);
  await expect(page).toHaveURL(/revisao=por_rever/);

  await captureSnapshot(toggleForm, 'submit');
  page.removeAllListeners('dialog');
  page.once('dialog', (dialog) => dialog.accept());
  await deactivateButton.click();

  await expect(page).toHaveURL(/revisao=por_rever/);
  expect(new URL(page.url()).searchParams.get('q')).toBe('fornecedor antigo');
  await expect(page.getByRole('searchbox')).toHaveValue('fornecedor antigo');
  await expect(page.getByLabel('Filtrar pelo estado da revisão manual'))
    .toHaveValue('por_rever');

  const productRow = page.locator('tr').filter({
    has: page.getByRole('button', { name: /Artigo 090/ }),
  });
  await expect(productRow).toContainText('Inativo');
  const snapshot = await savedSnapshot(page);
  await expect.poll(async () => {
    const current = await clampedScroll(page, snapshot);
    return Math.abs(current.y - current.expectedY);
  }).toBeLessThanOrEqual(3);
  const restored = await clampedScroll(page, snapshot);
  expect(restored.tableScrollLeft).toBe(snapshot.tableScrollLeft);
  expect(snapshot.openDetails).toEqual([]);
  expect(await activeScrollTokens(page)).toEqual([]);

  await page.goto('/compras/artigos');
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
  expect(await activeScrollTokens(page)).toEqual([]);
});

test('add keeps unmatched articles behind active filters while search and clear still work', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 850 });
  await page.goto('/test-login');
  await page.goto('/compras/artigos?q=Fornecedor+Antigo&revisao=por_rever');

  const addForm = page.locator('form[data-unsaved-navigation]')
    .filter({ has: page.locator('[name="action"][value="add"]') });
  await addForm.getByRole('button', { name: 'Adicionar' }).click();

  await expect(page).toHaveURL(/revisao=por_rever/);
  expect(await activeScrollTokens(page)).toEqual([]);
  await expect(addForm.locator('[name="scroll_context"]')).toHaveValue('');

  await page.getByRole('searchbox').fill('Artigo 090');
  await page.getByRole('button', { name: 'Pesquisar' }).click();
  expect(new URL(page.url()).searchParams.get('q')).toBe('Artigo 090');
  await expect(page.locator('.collapse[id^="artigo-detalhe-"]')).toHaveCount(1);

  await addForm.locator('[name="supplier_id"]').selectOption('2');
  await addForm.locator('[name="produto"]').fill('Feijão manteiga');
  await addForm.locator('[name="categoria_artigo"]').selectOption('Bebidas e café');
  await addForm.locator('[name="unidade"]').fill('kg');
  await addForm.getByRole('button', { name: 'Adicionar' }).click();

  expect(new URL(page.url()).searchParams.get('q')).toBe('artigo 090');
  expect(new URL(page.url()).searchParams.get('revisao')).toBe('por_rever');
  await expect(page.locator('.collapse[id^="artigo-detalhe-"]')).toHaveCount(1);
  await expect(page.getByRole('button', { name: /Artigo 090/ })).toHaveCount(1);
  expect(await activeScrollTokens(page)).toEqual([]);

  await page.getByRole('link', { name: 'Limpar' }).click();
  await expect(page).toHaveURL(/\/compras\/artigos$/);
  await expect(page.locator('.collapse[id^="artigo-detalhe-"]')).toHaveCount(91);
  await expect(page.getByRole('button', { name: /Feijão manteiga/ })).toHaveCount(1);

  await page.goto('/compras/artigos');
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
  expect(await activeScrollTokens(page)).toEqual([]);
});
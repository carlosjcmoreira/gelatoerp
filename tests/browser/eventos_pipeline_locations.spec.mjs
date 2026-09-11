import { expect, test } from '@playwright/test';

async function signIn(page, user = 7) {
  await page.goto(`/test-login?user=${user}`);
  await expect(page).toHaveURL(/\/eventos\/pipeline$/);
}

test('pesquisa, ordenação e preferências de colunas persistem por utilizador', async ({ page }) => {
  await signIn(page);

  await page.locator('input[name="q"]').fill('Pequena');
  await expect(page).toHaveURL(/q=Pequena/);
  await expect(page.locator('#pipeline-table tbody tr')).toHaveCount(1);
  await expect(page.locator('#pipeline-table tbody tr')).toContainText('Festa Pequena');

  await page.goto('/eventos/pipeline');
  await page.getByRole('button', { name: /^Orçamento/ }).click();
  await expect(page).toHaveURL(/sort=budget.*direction=asc/);
  await expect(page.locator('#pipeline-table tbody tr').first()).toContainText('Festa Pequena');

  await page.getByRole('button', { name: /^Orçamento/ }).click();
  await expect(page).toHaveURL(/sort=budget.*direction=desc/);
  await expect(page.locator('#pipeline-table tbody tr').first()).toContainText('Festa Grande');

  await page.getByText('Colunas', { exact: true }).click();
  const emailColumn = page.locator('#column-picker label', { hasText: 'Email' }).locator('input');
  await expect(emailColumn).not.toBeChecked();
  const saved = page.waitForResponse(
    response => response.url().endsWith('/eventos/pipeline/preferencias') && response.status() === 200,
  );
  await emailColumn.check();
  await saved;
  await expect(page.locator('td[data-column="email"]').first()).toBeVisible();

  const saveReorderedColumns = page.waitForResponse(
    response => response.url().endsWith('/eventos/pipeline/preferencias') && response.status() === 200,
  );
  await page.locator('#column-picker label', { hasText: 'Email' }).dragTo(
    page.locator('#column-picker label', { hasText: 'Evento' }),
  );
  await saveReorderedColumns;
  await expect(page.locator('#pipeline-table thead th').evaluateAll(
    headers => headers.filter(header => !header.hidden).slice(0, 2).map(
      header => header.dataset.column,
    ),
  )).resolves.toEqual(['email', 'event']);

  await page.reload();
  await expect(page.locator('td[data-column="email"]').first()).toBeVisible();
  await expect(page.locator('#pipeline-table thead th').evaluateAll(
    headers => headers.filter(header => !header.hidden).slice(0, 2).map(
      header => header.dataset.column,
    ),
  )).resolves.toEqual(['email', 'event']);

  await signIn(page, 8);
  await expect(page.locator('td[data-column="email"]').first()).toBeHidden();
});

test('o painel lateral abre e a edição de orçamento recebe a navegação', async ({ page }) => {
  await signIn(page);

  await page.locator('#pipeline-table tbody tr', { hasText: 'Festa Grande' }).click();
  const panel = page.locator('#event-panel');
  await expect(panel).toHaveClass(/show/);
  await expect(panel).toContainText('Festa Grande');

  const editQuote = panel.getByRole('link', { name: 'Editar orçamento' });
  await expect(editQuote).toHaveAttribute('href', '/eventos/evento/1#quote');
  await editQuote.click();
  await expect(page).toHaveURL(/\/eventos\/evento\/1#quote$/);
  await expect(page.getByRole('heading', { name: 'Editar orçamento do evento 1' })).toBeVisible();
});

test('associação parcial exige confirmação e mantém o texto histórico', async ({ page }) => {
  await signIn(page);
  await page.goto('/eventos/locais');

  await expect(page.getByText('Revisão de locais incompletos')).toBeVisible();
  await expect(page.getByText(/Registo histórico: Quinta da Serra/)).toBeVisible();

  const selectVenue = page.locator('select[name="venue_id"]');
  const confirmLink = page.getByRole('button', { name: 'Confirmar associação' });
  await expect(selectVenue).toHaveValue('');
  await confirmLink.click();
  await expect(selectVenue).toHaveValue('');

  await selectVenue.selectOption('3');
  await Promise.all([
    page.waitForURL(/\/eventos\/locais$/),
    confirmLink.click(),
  ]);
  await expect(page.getByText('O texto histórico não foi alterado.')).toBeVisible();

  const state = await (await page.request.get('/_test-state')).json();
  expect(state.historical_occurrence).toMatchObject({
    id: 9,
    venue: 'Quinta da Serra',
    venue_address: '',
    venue_id: 3,
  });
});
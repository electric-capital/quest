import { test, expect, closeOnboarding } from './fixtures';

test('sign in through the account picker, persist a session, and sign out', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText('AI-powered assistant')).toBeVisible();
  expect((await page.request.get('/app/api/me')).status()).toBe(401);

  await page.getByRole('button', { name: /Bob Rivera.*bob@quest.local/ }).click();
  await closeOnboarding(page);
  await expect(page.getByText('bob@quest.local', { exact: true })).toBeVisible();
  await page.reload();
  await closeOnboarding(page);
  await expect(page.getByText('bob@quest.local', { exact: true })).toBeVisible();

  await page.getByRole('button', { name: 'Open settings', exact: true }).click();
  await page.getByRole('button', { name: 'Sign Out', exact: true }).click();
  await page.getByRole('button', { name: 'Logout', exact: true }).click();
  await expect(page.getByText('AI-powered assistant')).toBeVisible();
  expect((await page.request.get('/app/api/me')).status()).toBe(401);
  await page.reload();
  await expect(page.getByRole('button', { name: 'Dev Login', exact: true })).toBeVisible();
});

test('sign in with a fresh email and keep sending disabled without credentials', async ({ page, account }) => {
  await page.goto('/');
  await page.getByPlaceholder(/email address/).fill(account);
  await page.getByRole('button', { name: 'Dev Login', exact: true }).click();
  await closeOnboarding(page);
  await expect(page.getByText(account, { exact: true })).toBeVisible();
  // A non-admin gets the disabled composer with the notice, never the setup card.
  await expect(page.getByText('No model yet', { exact: false })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeDisabled();
  await expect(page.getByRole('region', { name: 'Connect a model' })).toHaveCount(0);
});

test('an admin sees the model setup card in place of the composer without credentials', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: /Ada Admin.*admin@quest.local/ }).click();
  await closeOnboarding(page);
  await expect(page.getByRole('region', { name: 'Connect a model' })).toBeVisible();
  await expect(page.getByRole('radio', { name: 'Anthropic' })).toBeChecked();
  await expect(page.getByRole('button', { name: 'Connect', exact: true })).toBeDisabled();
  await page.getByRole('radio', { name: 'Self-hosted' }).click();
  await expect(page.getByPlaceholder('http://localhost:11434')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toHaveCount(0);
});

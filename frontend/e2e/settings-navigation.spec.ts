import { test, expect, closeOnboarding } from './fixtures';

test('save appearance on the server, not only in browser storage', async ({ signedIn: page }) => {
  await page.getByRole('button', { name: 'Open settings', exact: true }).click();
  await page.getByRole('button', { name: 'Appearance', exact: true }).click();
  const saved = page.waitForResponse((response) => response.url().endsWith('/app/api/settings') && response.request().method() === 'PUT');
  await page.getByRole('radio', { name: /^Dark/ }).click();
  expect((await saved).ok()).toBeTruthy();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  await closeOnboarding(page);
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
});

test('navigate inbox tabs, reload deep links, and return using browser history', async ({ signedIn: page }) => {
  await page.getByRole('button', { name: 'Open inbox', exact: true }).click();
  await expect(page).toHaveURL(/\/inbox$/);
  await expect(page.getByText(/No requests yet\./)).toBeVisible();
  await page.getByRole('tab', { name: 'Memories', exact: true }).click();
  await expect(page).toHaveURL(/\/inbox\/memories$/);
  await expect(page.getByRole('tab', { name: 'Memories' })).toHaveAttribute('aria-selected', 'true');
  await page.reload();
  await closeOnboarding(page);
  await expect(page.getByRole('tab', { name: 'Memories' })).toHaveAttribute('aria-selected', 'true');
  await page.goBack();
  await expect(page).toHaveURL(/\/inbox$/);
  await expect(page.getByRole('tab', { name: 'Requests' })).toHaveAttribute('aria-selected', 'true');
  await page.getByRole('tab', { name: 'Other', exact: true }).click();
  await expect(page).toHaveURL(/\/inbox\/other$/);
  await expect(page.getByText('No new notifications.')).toBeVisible();
  await page.reload();
  await closeOnboarding(page);
  await expect(page.getByRole('tab', { name: 'Other' })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByText('No new notifications.')).toBeVisible();
});

for (const feature of ['Docs', 'Tables']) {
  test(`deny ${feature} deep links when the account lacks feature access`, async ({ signedIn: page }) => {
    await page.goto(`/${feature.toLowerCase()}`);
    await closeOnboarding(page);
    await expect(page.getByRole('status')).toContainText(`Quest ${feature} is turned off for your account.`);
    expect((await page.request.get(`/app/api/${feature.toLowerCase()}`)).status()).toBe(403);
  });
}

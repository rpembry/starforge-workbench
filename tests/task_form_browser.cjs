const assert = require('node:assert/strict');
const {chromium} = require(process.env.WB_PLAYWRIGHT_MODULE);
(async () => {
  const browser = await chromium.launch({executablePath: process.env.WB_CHROME, headless: true, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({extraHTTPHeaders: {Authorization: 'Bearer ' + 'o'.repeat(40)}});
    await page.goto(process.env.WB_TEST_URL);
    for (const title of ['First browser fixture', 'Second browser fixture']) {
      await page.locator('[name=title]').fill(title);
      await page.locator('[name=details]').fill('Fixture details');
      await page.getByRole('button', {name: 'Create task'}).click();
      await page.waitForFunction(() => document.querySelector('[name=title]').value === '');
      assert.equal(await page.locator('[name=details]').inputValue(), '');
      assert.equal(await page.locator('[name=title]').evaluate(el => document.activeElement === el), true);
      assert.equal(await page.locator('#action-result').textContent(), 'Human action created.');
    }
    await page.route('**/ui/actions', route => route.fulfill({status: 422, contentType: 'application/json', body: '{}'}));
    await page.locator('[name=title]').fill('Keep this draft');
    await page.locator('[name=details]').fill('Keep these details');
    await page.getByRole('button', {name: 'Create task'}).click();
    await page.waitForFunction(() => document.querySelector('#action-result').textContent.includes('draft is preserved'));
    assert.equal(await page.locator('[name=title]').inputValue(), 'Keep this draft');
    assert.equal(await page.locator('[name=details]').inputValue(), 'Keep these details');
    await page.unroute('**/ui/actions');
    let release;
    const held = new Promise(resolve => { release = resolve; });
    let started;
    const requestStarted = new Promise(resolve => { started = resolve; });
    await page.route('**/ui/actions', async route => {
      started();
      await held;
      await route.fulfill({status: 200, contentType: 'text/html', body: 'Human action created.'});
    });
    await page.getByRole('button', {name: 'Create task'}).click();
    await requestStarted;
    await page.locator('[name=title]').fill('Newer unsaved draft');
    release();
    await page.waitForFunction(() => document.querySelector('#action-result').textContent === 'Human action created.');
    assert.equal(await page.locator('[name=title]').inputValue(), 'Newer unsaved draft');
    await page.unroute('**/ui/actions');
    await page.route('**/ui/actions', route => route.abort('connectionfailed'));
    await page.getByRole('button', {name: 'Create task'}).click();
    await page.waitForFunction(() => document.querySelector('#action-result').textContent.includes('draft is preserved'));
    assert.equal(await page.locator('[name=title]').inputValue(), 'Newer unsaved draft');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });

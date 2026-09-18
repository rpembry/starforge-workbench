const assert = require('node:assert/strict');
const {chromium} = require(process.env.WB_PLAYWRIGHT_MODULE);

(async () => {
  const browser = await chromium.launch({executablePath: process.env.WB_CHROME, headless: true, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 360, height: 740}, deviceScaleFactor: 3,
      extraHTTPHeaders: {Authorization: 'Bearer ' + 'o'.repeat(40)}});
    for (const path of ['/sessions', '/sessions/registered_session_0001']) {
      await page.goto(process.env.WB_TEST_URL + path);
      assert.equal(await page.locator('main').count(), 1);
      const dimensions = await page.evaluate(() => ({scroll: document.documentElement.scrollWidth,
        width: document.documentElement.clientWidth}));
      assert.ok(dimensions.scroll <= dimensions.width, `${path} has horizontal scrolling: ${JSON.stringify(dimensions)}`);
      assert.equal(await page.getByRole('heading', {level: 1}).count(), 1);
      assert.equal(await page.getByRole('navigation', {name: 'Main navigation'}).count(), 1);
    }
    await page.goto(process.env.WB_TEST_URL + '/sessions');
    await page.getByRole('link', {name: /Synthetic agent/}).focus();
    assert.equal(await page.getByRole('link', {name: /Synthetic agent/}).evaluate(el => document.activeElement === el), true);
    await page.keyboard.press('Enter');
    await page.waitForURL('**/sessions/registered_session_0001');
    assert.equal(await page.getByRole('heading', {name: 'Observed status'}).count(), 1);
    const instruction = 'Synthetic dictation with Unicode ✓ and $HOME; no merge.';
    await page.getByLabel('Instruction text').fill(instruction);
    assert.equal(await page.locator('#instruction-count').textContent(), String(Array.from(instruction).length));
    await page.getByLabel(/Confirm this exact session/).check();
    await page.getByRole('button', {name: 'Queue instruction'}).click();
    await page.waitForTimeout(500);
    assert.equal(page.url(), process.env.WB_TEST_URL + '/sessions/registered_session_0001?notice=queued',
      await page.locator('body').textContent());
    assert.equal(await page.getByRole('status').filter({hasText: 'Instruction queued'}).count(), 1);
    assert.equal(await page.getByText(instruction, {exact: true}).count(), 1);
    assert.equal(await page.locator('.instruction h3 .badge').filter({hasText: 'Queued'}).count(), 1);
    assert.equal(await page.getByRole('button', {name: /Pause|Continue|Stop/}).count(), 0);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });

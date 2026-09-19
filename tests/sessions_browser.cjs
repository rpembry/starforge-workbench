const assert = require('node:assert/strict');
const {chromium} = require(process.env.WB_PLAYWRIGHT_MODULE);

(async () => {
  const browser = await chromium.launch({executablePath: process.env.WB_CHROME, headless: true, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 360, height: 740}, deviceScaleFactor: 3,
      extraHTTPHeaders: {Authorization: 'Bearer ' + 'o'.repeat(40)}});
    await page.addInitScript(() => {
      class SyntheticEventSource {
        constructor() { window.syntheticPreviewSource = this; }
        close() {}
      }
      window.EventSource = SyntheticEventSource;
      window.emitSyntheticPreview = event => window.syntheticPreviewSource.onmessage({data: JSON.stringify(event)});
    });
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
    await page.evaluate(() => window.emitSyntheticPreview({
      instruction_id: 'instruction_other', registered_session_id: 'registered_session_0002',
      outcome: 'provider_response_without_error', excerpt: 'MUST NOT APPEAR'}));
    assert.equal(await page.getByText('MUST NOT APPEAR', {exact: true}).count(), 0);
    for (let index = 1; index <= 6; index++) {
      await page.evaluate(index => window.emitSyntheticPreview({
        instruction_id: 'instruction_' + index, registered_session_id: 'registered_session_0001',
        outcome: 'provider_response_without_error', excerpt: 'Session preview ' + index}), index);
    }
    assert.equal(await page.locator('#response-preview-feed article').count(), 5);
    assert.equal(await page.getByText('Session preview 1', {exact: true}).count(), 0);
    assert.equal(await page.getByText('Session preview 6', {exact: true}).count(), 1);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });

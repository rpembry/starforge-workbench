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
    await page.getByRole('status').filter({hasText: 'Instruction queued for this exact session'}).waitFor();
    assert.equal(page.url(), process.env.WB_TEST_URL + '/sessions/registered_session_0001');
    await page.getByText(instruction, {exact: true}).waitFor({timeout: 8000});
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

    await page.evaluate(() => window.syntheticPreviewSource.onopen());
    assert.match(await page.locator('#response-preview-state').textContent(), /connected/i);
    const countBeforeGap = await page.locator('#response-preview-feed article').count();
    await page.evaluate(() => {
      window.previousSyntheticPreviewSource = window.syntheticPreviewSource;
      window.syntheticPreviewSource.onerror();
    });
    assert.match(await page.locator('#response-preview-state').textContent(), /disconnected.*missed output cannot be replayed/i);
    await page.waitForFunction(() => window.syntheticPreviewSource !== window.previousSyntheticPreviewSource);
    assert.match(await page.locator('#response-preview-state').textContent(), /reconnecting.*cannot be replayed/i);
    assert.equal(await page.locator('#response-preview-feed article').count(), countBeforeGap);
    await page.evaluate(() => window.syntheticPreviewSource.onopen());
    assert.match(await page.locator('#response-preview-state').textContent(), /connected/i);

    const statusUrl = process.env.WB_TEST_URL + '/ui/sessions/registered_session_0001/status';
    const fresh = await (await page.request.get(statusUrl)).json();
    const draft = 'Draft retained during refresh, ✓';
    await page.getByLabel('Instruction text').fill(draft);
    await page.getByLabel('Expires after').selectOption('30');
    await page.getByLabel(/Confirm this exact session/).check();
    await page.locator('#instruction-text').evaluate(el => { el.focus(); el.setSelectionRange(5, 5); });
    const lostStatus = route => route.abort('failed');
    await page.route(statusUrl, lostStatus);
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await page.getByRole('status').filter({hasText: 'Status refresh unavailable'}).waitFor();
    assert.equal(await page.getByLabel('Instruction text').inputValue(), draft);
    assert.equal(await page.getByLabel('Expires after').inputValue(), '30');
    assert.equal(await page.getByLabel(/Confirm this exact session/).isChecked(), true);
    assert.equal(await page.locator('#instruction-text').evaluate(el => document.activeElement === el && el.selectionStart === 5), true);
    assert.equal(await page.locator('button[type="submit"]').isDisabled(), true);
    await page.unroute(statusUrl, lostStatus);

    let seenFirst;
    const firstSeen = new Promise(resolve => { seenFirst = resolve; });
    let releaseOld;
    let statusRequests = 0;
    const outOfOrder = async route => {
      statusRequests++;
      if (statusRequests === 1) {
        await new Promise(resolve => {
          releaseOld = async () => { await route.fulfill({json: fresh}); resolve(); };
          seenFirst();
        });
      } else {
        await route.fulfill({json: {...fresh, visibility: 'offline', status_label: 'Offline visibility', send_allowed: false}});
      }
    };
    await page.route(statusUrl, outOfOrder);
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await firstSeen;
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await page.locator('[data-status-label]').filter({hasText: 'Offline visibility'}).waitFor();
    await releaseOld();
    await page.waitForTimeout(100);
    assert.equal(await page.locator('[data-status-label]').textContent(), 'Offline visibility');
    assert.equal(await page.getByLabel('Instruction text').inputValue(), draft);
    await page.unroute(statusUrl, outOfOrder);

    const expiredAuth = route => route.fulfill({status: 401, json: {error: {code: 'unauthorized'}}});
    await page.route(statusUrl, expiredAuth);
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await page.getByRole('status').filter({hasText: 'Authentication expired'}).waitFor();
    assert.equal(await page.getByLabel('Instruction text').inputValue(), draft);
    await page.unroute(statusUrl, expiredAuth);
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await page.locator('[data-status-label]').filter({hasText: 'Present'}).waitFor();

    const listUrl = process.env.WB_TEST_URL + '/ui/sessions/status?limit=100&offset=0';
    const liveList = await (await page.request.get(listUrl)).json();
    const added = {...liveList.items[0], id: 'registered_session_0003', display_name: 'New synthetic agent'};
    const expandedList = route => route.fulfill({json: {...liveList, items: [...liveList.items, added]}});
    await page.route(listUrl, expandedList);
    await page.locator('#instruction-text').evaluate(el => { el.focus(); el.setSelectionRange(7, 7); });
    await page.goto(process.env.WB_TEST_URL + '/sessions');
    await page.getByRole('link', {name: 'New synthetic agent'}).waitFor();
    await page.unroute(listUrl, expandedList);
    await page.goBack();
    await page.waitForURL('**/sessions/registered_session_0001');
    assert.equal(await page.getByLabel('Instruction text').inputValue(), draft);
    assert.equal(await page.getByLabel('Expires after').inputValue(), '30');
    assert.equal(await page.locator('#instruction-text').evaluate(el => el.selectionStart === 7), true);

    await page.getByRole('button', {name: 'Start new attempt'}).click();
    const lostResponseText = 'Accepted once, response lost';
    await page.getByLabel('Instruction text').fill(lostResponseText);
    await page.getByLabel(/Confirm this exact session/).check();
    const acceptedKey = await page.locator('input[name="idempotency_key"]').inputValue();
    let acceptedPosts = 0;
    const acceptedThenLost = async route => {
      acceptedPosts++;
      await route.fetch();
      await route.abort('failed');
    };
    await page.route('**/api/instructions', acceptedThenLost);
    await page.getByRole('button', {name: 'Queue instruction'}).click();
    await page.getByRole('status').filter({hasText: 'Instruction recorded as queued'}).waitFor();
    assert.equal(acceptedPosts, 1);
    assert.equal(await page.locator('input[name="idempotency_key"]').inputValue(), acceptedKey);
    await page.unroute('**/api/instructions', acceptedThenLost);

    await page.getByRole('button', {name: 'Start new attempt'}).click();
    const retryText = 'Retry only after checking original key';
    await page.getByLabel('Instruction text').fill(retryText);
    await page.getByLabel(/Confirm this exact session/).check();
    const retryKey = await page.locator('input[name="idempotency_key"]').inputValue();
    let droppedPosts = 0;
    const droppedBeforeServer = route => { droppedPosts++; return route.abort('failed'); };
    await page.route('**/api/instructions', droppedBeforeServer);
    await page.getByRole('button', {name: 'Queue instruction'}).click();
    await page.getByRole('button', {name: 'Retry same attempt'}).waitFor();
    assert.equal(droppedPosts, 1);
    await page.getByLabel('Instruction text').fill(retryText + ' changed');
    assert.equal(await page.getByRole('button', {name: 'Retry same attempt'}).isDisabled(), true);
    await page.getByLabel('Instruction text').fill(retryText);
    await page.unroute('**/api/instructions', droppedBeforeServer);
    await page.getByRole('button', {name: 'Retry same attempt'}).click();
    await page.getByRole('status').filter({hasText: 'Instruction queued for this exact session'}).waitFor();
    assert.equal(await page.locator('input[name="idempotency_key"]').inputValue(), retryKey);

    await page.getByRole('button', {name: 'Start new attempt'}).click();
    await page.getByLabel('Instruction text').fill('Key conflict after identity changed');
    await page.getByLabel(/Confirm this exact session/).check();
    const conflict = route => route.fulfill({status: 409,
      json: {error: {code: 'idempotency_conflict'}}});
    await page.route('**/api/instructions', conflict);
    await page.getByRole('button', {name: 'Queue instruction'}).click();
    await page.getByRole('status').filter({hasText: 'Outcome remains unknown; do not start another attempt'}).waitFor();
    assert.equal(await page.getByRole('button', {name: 'Retry same attempt'}).isVisible(), false);
    assert.equal(await page.getByRole('button', {name: 'Start new attempt'}).isVisible(), false);
    await page.unroute('**/api/instructions', conflict);

    for (const [entered, normalized, loseResponse] of [
      ['  \tUnicode ✓ café\n', 'Unicode ✓ café', false],
      ['\nQA whitespace\n', 'QA whitespace', true]]) {
      const whitespacePage = await browser.newPage({viewport: {width: 360, height: 740},
        extraHTTPHeaders: {Authorization: 'Bearer ' + 'o'.repeat(40)}});
      await whitespacePage.goto(process.env.WB_TEST_URL + '/sessions/registered_session_0001');
      await whitespacePage.locator('#session-poll-state').filter({hasText: 'Server status checked now'}).waitFor();
      await whitespacePage.getByLabel('Instruction text').fill(entered);
      await whitespacePage.getByLabel(/Confirm this exact session/).check();
      const key = await whitespacePage.locator('input[name="idempotency_key"]').inputValue();
      let posts = 0;
      if (loseResponse) await whitespacePage.route('**/api/instructions', async route => {
        posts++;
        await route.fetch();
        await route.abort('failed');
      });
      await whitespacePage.getByRole('button', {name: 'Queue instruction'}).click();
      await whitespacePage.getByRole('status').filter({hasText: loseResponse
        ? 'Instruction recorded as queued' : 'Instruction queued for this exact session'}).waitFor();
      const record = await (await whitespacePage.request.get(process.env.WB_TEST_URL +
        '/api/instructions/by-key/' + key)).json();
      assert.equal(record.text, normalized);
      assert.equal(record.idempotency_key, key);
      assert.equal(await whitespacePage.getByRole('button', {name: 'Retry same attempt'}).isVisible(), false);
      if (loseResponse) assert.equal(posts, 1);
      await whitespacePage.close();
    }

    const blocked = await browser.newPage({viewport: {width: 360, height: 740},
      extraHTTPHeaders: {Authorization: 'Bearer ' + 'o'.repeat(40)}});
    await blocked.goto(process.env.WB_TEST_URL + '/sessions/registered_session_0001');
    await blocked.locator('#session-poll-state').filter({hasText: 'Server status checked now'}).waitFor();
    await blocked.evaluate(() => {
      sessionStorage.clear();
      Storage.prototype.setItem = function () { throw new DOMException('quota', 'QuotaExceededError'); };
    });
    await blocked.getByLabel('Instruction text').fill('Synthetic draft while storage is blocked');
    await blocked.getByLabel(/Confirm this exact session/).check();
    assert.equal(await blocked.getByRole('button', {name: 'Queue instruction'}).isDisabled(), true);
    let blockedPosts = 0;
    await blocked.route('**/api/instructions', route => { blockedPosts++; return route.abort('failed'); });
    await blocked.locator('[data-instruction-form]').evaluate(form => form.dispatchEvent(
      new Event('submit', {bubbles: true, cancelable: true})));
    await blocked.getByRole('status').filter({hasText: 'Tab storage is unavailable. No instruction was sent'}).waitFor();
    assert.equal(blockedPosts, 0);
    assert.equal(await blocked.getByLabel('Instruction text').inputValue(), 'Synthetic draft while storage is blocked');
    await blocked.goto(process.env.WB_TEST_URL + '/sessions');
    await blocked.goBack();
    await blocked.waitForURL('**/sessions/registered_session_0001');
    assert.equal(blockedPosts, 0);
    await blocked.close();

    const slow = await browser.newPage({viewport: {width: 360, height: 740},
      extraHTTPHeaders: {Authorization: 'Bearer ' + 'o'.repeat(40)}});
    const slowStatusUrl = process.env.WB_TEST_URL + '/ui/sessions/registered_session_0001/status';
    const slowStatus = await (await slow.request.get(slowStatusUrl)).json();
    let slowResponses = 0;
    await slow.route(slowStatusUrl, async route => {
      await new Promise(resolve => setTimeout(resolve, 12000));
      slowResponses++;
      await route.fulfill({json: slowStatus});
    });
    await slow.goto(process.env.WB_TEST_URL + '/sessions/registered_session_0001');
    await slow.locator('#session-poll-state').filter({hasText: 'Server status checked now'}).waitFor({timeout: 18000});
    assert.ok(slowResponses >= 1);
    assert.equal(await slow.getByRole('button', {name: 'Queue instruction'}).isDisabled(), false);
    await slow.close();
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });

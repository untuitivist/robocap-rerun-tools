// Run with Node and Playwright installed, passing a generated report HTML path.
const assert = require('node:assert/strict');
const { pathToFileURL } = require('node:url');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(pathToFileURL(process.argv[2]).href);
    assert.equal(await page.locator('.column-sort').count(), 13);
    await page.evaluate(() => {
      const seed = report.events[0];
      report.events = Array.from({ length: 120 }, (_, i) => {
        const event = [...seed];
        event[0] = 120 - i;
        event[4] = i;
        event[7] = 1000 + i;
        event[12] = i === 0 ? null : i;
        return event;
      });
      applyFilters();
    });
    await page.locator('.column-sort').nth(0).click();
    assert.equal(await page.locator('#eventRows tr td').first().innerText(), '1');
    await page.locator('#next').click();
    assert.equal(await page.locator('#eventRows tr td').first().innerText(), '101');
    await page.locator('.column-sort').nth(0).click();
    assert.equal(await page.locator('#eventRows tr td').first().innerText(), '120');
    await page.locator('.column-sort').nth(9).click();
    assert.equal(await page.evaluate(() => filtered.at(-1)[12]), null);
    await page.locator('.column-sort').nth(9).click();
    assert.equal(await page.evaluate(() => filtered.at(-1)[12]), null);
    const diff = page.locator('.column-filter').nth(9);
    await diff.locator('summary').click();
    await diff.locator('input').nth(0).fill('10');
    await diff.locator('input').nth(1).fill('20');
    assert.equal(await page.evaluate(() => filtered.length), 11);
    const position = page.locator('.column-filter').nth(4);
    await position.locator('summary').click();
    await position.locator('select').first().selectOption('1');
    await position.locator('input').first().fill('1015');
    assert.equal(await page.evaluate(() => filtered.length), 6);
    await page.locator('#search').fill('no-such-file-or-detail');
    assert.equal(await page.locator('#eventRows tr').count(), 0);
    await page.locator('#resetEventFilters').click();
    assert.equal(await page.evaluate(() => filtered.length), 120);
    assert.equal(await page.locator('#eventRows tr td').first().innerText(), '120');
    await diff.locator('select').selectOption('missing');
    assert.equal(await page.evaluate(() => filtered.length), 1);
    await page.reload();
    await page.screenshot({ path: process.argv[3] || 'anomaly-table-desktop.png' });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.deepEqual(errors, []);
    console.log('PASS: sorting, pagination, nulls, numeric ranges, frame basis, combined filters, reset, mobile overflow');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });

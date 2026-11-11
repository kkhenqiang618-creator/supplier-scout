// Optional UI regression. Start server first, install Playwright for testing only.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert=require('node:assert/strict');
(async()=>{
  const browser=await chromium.launch({channel:'chrome',headless:true});
  try {
    const page=await browser.newPage();
    await page.goto(process.env.SCOUT_TEST_URL || 'http://127.0.0.1:8765');
    await page.getByRole('button',{name:'回放示例流程'}).click();
    await page.locator('#run-scope').selectOption('all');
    await page.getByRole('button',{name:'开始运行',exact:true}).click();
    await page.waitForFunction(()=>state.job.running);
    await page.locator('.supplier-cell[data-detail="1"]').click();
    await page.waitForFunction(()=>!state.job.running);
    await page.getByRole('button',{name:'关闭',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('.page-actions').textContent.includes('回放示例流程'),undefined,{timeout:5000});
    assert(await page.getByRole('button',{name:'回放示例流程'}).isEnabled());
    console.log('PASS: completed batch renders after evidence drawer closes');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exit(1)});

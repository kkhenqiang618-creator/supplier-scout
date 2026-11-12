// Run against an isolated local fixture server. This test never saves or changes records.
const {chromium}=require('playwright'),assert=require('node:assert/strict');
(async()=>{
 const base=process.env.SCOUT_TEST_URL;
 if(!base||!/^http:\/\/127\.0\.0\.1:\d+$/.test(base))throw Error('SCOUT_TEST_URL must be an isolated localhost fixture server');
 const browser=await chromium.launch({channel:'chrome',headless:true});
 try{
  const page=await browser.newPage();await page.goto(base+'/?mode=real&page=contacts');await page.locator('[data-entry-edit]').first().waitFor();
  const row=await page.evaluate(()=>{const row=state.suppliers.find(r=>r.mode==='real');row._summary=true;return {...row,_summary:false}});
  let entered;const pending=new Promise(resolve=>entered=resolve);
  await page.route('**/api/supplier?id='+row.id,async route=>{entered();await new Promise(resolve=>setTimeout(resolve,1000));await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(row)})});
  await page.locator('[data-entry-edit]').first().click();await pending;
  await page.locator('[data-nav="suppliers"]').first().click();await page.waitForTimeout(1400);
  assert.equal(await page.locator('#entry-new-url').count(),0,'stale editor must not reopen after navigation');
  console.log('PASS: delayed entry editor cannot override a later navigation');
 }finally{await browser.close()}
})().catch(e=>{console.error(e.message);process.exitCode=1});

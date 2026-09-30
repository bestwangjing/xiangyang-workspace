import {chromium} from 'playwright';
import fs from 'node:fs/promises';

const OUT='docs/screenshots';
await fs.mkdir(OUT,{recursive:true});
const browser=await chromium.launch({channel:'msedge',headless:true});
const page=await browser.newPage({viewport:{width:1440,height:960},deviceScaleFactor:1.5});
const base='http://127.0.0.1:8766/#';
const shot=async(name,{full=true}={})=>{await page.screenshot({path:`${OUT}/${name}.png`,fullPage:full});console.log('saved',name)};

await page.goto(base+'home');
await page.waitForSelector('.account-grid',{timeout:15000});
await page.waitForTimeout(1800);
await shot('home');

await page.goto(base+'creation');
await page.waitForTimeout(1800);
await shot('creation');

const openModal=async(buttonName,screenshotName,expand=false)=>{
  const btn=page.locator('button',{hasText:buttonName}).first();
  await btn.click();
  await page.waitForTimeout(2200);
  if(expand){
    await page.evaluate(()=>{
      const modal=document.querySelector('.modal');
      if(modal){
        modal.style.maxHeight='none';
        const body=modal.querySelector('.modal-body');
        if(body)body.style.overflow='visible';
      }
    });
    await page.waitForTimeout(400);
  }
  await shot(screenshotName,{full:expand});
  await page.evaluate(()=>{const b=document.querySelector('.modal [aria-label="关闭"]');if(b)b.click()});
  await page.locator('.overlay').waitFor({state:'detached',timeout:5000});
};

await openModal('查看详情','creation-detail',true);
await openModal('编辑','creation-metrics');
await openModal('更新数据','update-modal');

await page.goto(base+'post-reviews');
await page.waitForTimeout(1800);
await shot('post-reviews');

await page.goto(base+'hot-topics');
await page.waitForTimeout(2000);
await shot('hot-topics');

await page.goto(base+'profile');
await page.waitForTimeout(1500);
await shot('profile');

await browser.close();
console.log('done');

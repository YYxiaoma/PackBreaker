// 使用已安装的 Playwright，默认检查本机 5173，不连接外部站点。
const { chromium } = require(process.env.PB_PLAYWRIGHT || '../frontend/node_modules/playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async()=>{
  const browser=await chromium.launch({channel:process.env.PB_BROWSER||'msedge',headless:true});
  const page=await browser.newPage({viewport:{width:1440,height:1050}});
  const errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  const output=path.resolve(__dirname,'../frontend/test-results');fs.mkdirSync(output,{recursive:true});
  try {
    // 默认 E2E 不依赖真实后端，只固定一个已认证管理员会话；下载器真实 API 由 Vitest/后端契约测试覆盖。
    await page.route('**/api/v1/auth/me',route=>route.fulfill({
      status:200,
      contentType:'application/json',
      body:JSON.stringify({configured:true,authenticated:true,permissions:['admin'],expires_at:'2099-01-01T00:00:00Z'}),
    }));
    await page.goto('http://127.0.0.1:5173/',{waitUntil:'networkidle'});
    await page.getByRole('heading',{name:'任务中心',exact:true}).waitFor();
    await page.screenshot({path:path.join(output,'desktop.png'),fullPage:true});
    await page.getByPlaceholder('搜索任务名称、ID…').fill('不存在');
    await page.getByText('没有符合条件的任务，请调整筛选或新建任务。').waitFor();
    await page.getByPlaceholder('搜索任务名称、ID…').fill('');
    await page.getByRole('button',{name:'深空纪事 · 第一季',exact:true}).click();
    await page.getByRole('button',{name:'批准模拟执行',exact:true}).click();
    await page.locator('.el-message-box').getByRole('button',{name:'批准模拟执行',exact:true}).click();
    await page.getByRole('button',{name:'模拟校验完成',exact:true}).click();
    await page.getByRole('button',{name:'已确认做种',exact:true}).waitFor();
    await page.locator('.el-message').last().waitFor({state:'hidden'}); await page.screenshot({path:path.join(output,'preflight.png'),fullPage:true});
    await page.keyboard.press('Escape');
    await page.getByRole('button',{name:'远山回声 · 2024',exact:true}).click();
    assert.equal(await page.getByRole('button',{name:'批准模拟执行',exact:true}).isDisabled(),true);
    await page.getByRole('tab',{name:'文件映射',exact:true}).click();
    await page.locator('.mapping-fix .el-select__wrapper').click();
    await page.getByRole('option',{name:'Movie.01.BluRay.mkv · 10.8 GB',exact:true}).click();
    await page.getByRole('button',{name:'应用映射',exact:true}).click();
    await page.getByRole('tab',{name:'文件映射',exact:true}).click();
    await page.getByRole('button',{name:'重新模拟验证',exact:true}).click();
    assert.equal(await page.getByRole('button',{name:'批准模拟执行',exact:true}).isEnabled(),true);
    await page.keyboard.press('Escape');
    assert.match(await page.locator('.task-table .el-table__row').filter({hasText:'远山回声'}).locator('.progress-cell').innerText(),/100%/,'重新验证后列表进度应同步');
    for(const name of ['总览','预演与确认','历史辅种','站点管理','下载器','规则配置','清理与对账','日志','系统设置','升级中心']){
      await page.locator('nav').getByRole('button',{name,exact:false}).click();
      await page.getByRole('heading',{name,exact:true,level:1}).waitFor();
      assert.equal(await page.locator('main').evaluate(el=>el.scrollWidth<=el.clientWidth+1),true,`${name} 桌面溢出`);
    }
    await page.locator('nav').getByRole('button',{name:'历史辅种',exact:true}).click();
    await page.getByRole('button',{name:'新建扫描',exact:true}).click();
    await page.getByRole('button',{name:'开始演示扫描',exact:true}).click();
    await page.getByRole('button',{name:'推进扫描',exact:true}).click();
    await page.getByRole('button',{name:'推进扫描',exact:true}).click();
    await page.locator('nav').getByRole('button',{name:'清理与对账',exact:true}).click();
    await page.getByRole('button',{name:'运行模拟对账',exact:true}).click();
    await page.getByText('OP-022 · 目录所有权无法确认').waitFor();
    await page.getByRole('button',{name:'预览并清理登记资源',exact:true}).click();
    await page.getByRole('button',{name:'清理登记资源',exact:true}).click();
    await page.locator('nav').getByRole('button',{name:'任务中心',exact:false}).click();
    await page.setViewportSize({width:390,height:844});
    await page.waitForFunction(()=>document.querySelector('.sidebar').getBoundingClientRect().right<=0); await page.locator('.el-message').last().waitFor({state:'hidden'}); await page.screenshot({path:path.join(output,'mobile.png'),fullPage:true});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true,'移动页横向溢出');
    await page.getByRole('button',{name:'展开导航',exact:true}).click();
    await page.locator('nav').getByRole('button',{name:'站点管理',exact:true}).click();
    await page.getByRole('heading',{name:'站点管理',exact:true}).waitFor();
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true,'移动配置页横向溢出');
    await page.getByRole('button',{name:'切换深色主题',exact:true}).click();
    await page.screenshot({path:path.join(output,'mobile-dark.png'),fullPage:true});
    for(const name of ['总览','任务中心','预演与确认','历史辅种','下载器','规则配置','清理与对账','日志','系统设置','升级中心']){
      await page.getByRole('button',{name:'展开导航',exact:true}).click();
      await page.locator('nav').getByRole('button',{name,exact:false}).click();
      await page.getByRole('heading',{name,exact:true,level:1}).waitFor();
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true,`${name} 移动页溢出`);
    }
    assert.deepEqual(errors,[]);
    console.log('通过：任务筛选、审核、阻断与人工映射、11 页导航、历史扫描、清理确认、390px 移动布局与深色主题。');
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1});

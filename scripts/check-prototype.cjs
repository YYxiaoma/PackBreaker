// 使用已安装的 Playwright，默认检查本机 5173，不连接外部站点。
const { chromium } = require(process.env.PB_PLAYWRIGHT || '../frontend/node_modules/playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async()=>{
  const requestedBrowser=process.env.PB_BROWSER||'msedge';
  const launchOptions={headless:true};
  if(requestedBrowser!=='bundled') launchOptions.channel=requestedBrowser;
  const browser=await chromium.launch(launchOptions);
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
    const now=()=>new Date().toISOString();
    const realTasks={
      'task-e2e-execute':{id:'task-e2e-execute',type:'MOVIE',source_downloader_id:'source-qb',source_hash:'source-execute',normalized_unit_key:'movie:execute',status:'AWAITING_CONFIRMATION',version:1,error_code:null,created_at:now(),updated_at:now()},
      'task-e2e-cancel':{id:'task-e2e-cancel',type:'MOVIE',source_downloader_id:'source-qb',source_hash:'source-cancel',normalized_unit_key:'movie:cancel',status:'LINKING',version:2,error_code:null,created_at:now(),updated_at:now()},
    };
    const executeKeys=[];
    const cancelKeys=[];
    const cancelBodies=[];
    let executeAttempts=0;
    const taskEvents={
      'task-e2e-execute':[{id:'event-execute-1',task_id:'task-e2e-execute',from_status:'PREFLIGHT',to_status:'AWAITING_CONFIRMATION',event_type:'REVIEW_OPENED',reason:'E2E 初始审核已确认',created_at:now()}],
      'task-e2e-cancel':[{id:'event-cancel-1',task_id:'task-e2e-cancel',from_status:'AWAITING_CONFIRMATION',to_status:'LINKING',event_type:'LINKING_STARTED',reason:'E2E 初始链接阶段',created_at:now()}],
    };
    const appendTaskEvent=(id,eventType,fromStatus,toStatus,reason)=>{
      const event={id:`event-${id}-${taskEvents[id].length+1}`,task_id:id,from_status:fromStatus,to_status:toStatus,event_type:eventType,reason,created_at:now()};
      taskEvents[id].push(event);
      return event;
    };
    const transitionTask=(id,toStatus,eventType,reason)=>{
      const task=realTasks[id],fromStatus=task.status;
      task.status=toStatus;task.version+=1;task.updated_at=now();
      appendTaskEvent(id,eventType,fromStatus,toStatus,reason);
    };
    const eventsAfter=(id,cursor)=>{
      if(!cursor)return taskEvents[id];
      const index=taskEvents[id].findIndex(event=>event.id===cursor);
      return index<0?taskEvents[id]:taskEvents[id].slice(index+1);
    };
    const scheduleExecuteProgress=()=>{
      setTimeout(()=>appendTaskEvent('task-e2e-execute','QBITTORRENT_ADD_INTENT_RECORDED','ADDING','ADDING','qBittorrent 添加任务：已记录 operation journal intent'),150);
      setTimeout(()=>appendTaskEvent('task-e2e-execute','QBITTORRENT_ADD_APPLIED','ADDING','ADDING','qBittorrent 添加任务：已由 operation journal 与完成后证据确认副作用完成'),300);
      setTimeout(()=>transitionTask('task-e2e-execute','CLIENT_VERIFYING','QBITTORRENT_ADD_CONFIRMED','E2E 暂停添加已确认'),450);
      setTimeout(()=>appendTaskEvent('task-e2e-execute','QBITTORRENT_RECHECK_APPLIED','CLIENT_VERIFYING','CLIENT_VERIFYING','qBittorrent 强制校验：已由 operation journal 与完成后证据确认副作用完成'),650);
      setTimeout(()=>transitionTask('task-e2e-execute','SEEDING','CLIENT_VERIFICATION_CONFIRMED','E2E 客户端校验完成'),850);
      setTimeout(()=>appendTaskEvent('task-e2e-execute','QBITTORRENT_START_APPLIED','SEEDING','SEEDING','qBittorrent 启动作种：已由 operation journal 与完成后证据确认副作用完成'),1050);
      setTimeout(()=>transitionTask('task-e2e-execute','DONE','QBITTORRENT_SEEDING_CONFIRMED','E2E 做种状态已确认'),1250);
    };
    const taskUnit=id=>({
      id:`unit-${id}`,
      task_id:id,
      normalized_unit_key:realTasks[id].normalized_unit_key,
      source_inventory_digest:`inventory-${id}`,
      unit_type:'MOVIE',
      display_name:id,
      evidence:{},
      created_at:now(),
    });
    const candidate=id=>({
      id:`candidate-${id}`,
      snapshot_id:`snapshot-${id}`,
      normalized_unit_key:realTasks[id].normalized_unit_key,
      site_id:'site-e2e',
      torrent_id:`torrent-${id}`,
      display_name:`E2E ${id}`,
      score:99,
      rejected:false,
      selected_for_verification:true,
      verification_level:id==='task-e2e-execute'?'CLIENT_CHECK_REQUIRED':'FULL_VERIFIED',
      error_code:null,
      metainfo_digest:`meta-${id}`,
      evidence:{mappings:[]},
      created_at:now(),
    });
    const preflight=id=>({
      id:`preflight-${id}`,
      task_id:id,
      snapshot_digest:`snapshot-digest-${id}`,
      payload:{source_inventory_digest:`inventory-${id}`},
      current:true,
      stale_reasons:[],
      created_at:now(),
    });
    const review=id=>({
      id:`review-${id}`,
      task_unit_id:`unit-${id}`,
      version:1,
      approved_candidate_id:`candidate-${id}`,
      rejected_candidate_ids:[],
      manual_mappings:[],
      note:null,
      requires_reverification:false,
      actor_kind:'ADMIN',
      created_at:now(),
    });
    const verification=id=>({
      id:`verification-${id}`,
      task_unit_id:`unit-${id}`,
      review_revision_id:`review-${id}`,
      verification_level:id==='task-e2e-execute'?'CLIENT_CHECK_REQUIRED':'FULL_VERIFIED',
      verification_digest:`verification-digest-${id}`,
      created_at:now(),
    });
    const gate=id=>({
      id:`gate-${id}`,
      review_revision_id:`review-${id}`,
      review_version:1,
      candidate_id:`candidate-${id}`,
      metainfo_digest:`meta-${id}`,
      verification_level:id==='task-e2e-execute'?'CLIENT_CHECK_REQUIRED':'FULL_VERIFIED',
      verification_source:'REVIEW_VERIFICATION',
      client_check_required:id==='task-e2e-execute',
      current:true,
      eligible:true,
      blocked_reasons:[],
      preflight_stale_reasons:[],
      gate_digest:`gate-digest-${id}`,
      side_effects_started:false,
      created_at:now(),
    });
    const plan=id=>({
      id:`plan-${id}`,
      actions:[{kind:'HARDLINK',length:1073741824,source_relative_path:`source/${id}.mkv`,torrent_path:`${id}.mkv`}],
      blocked_reasons:[],
      client_check_required:id==='task-e2e-execute',
      client_fetch_count:id==='task-e2e-execute'?1:0,
      create_directory_count:1,
      created_at:now(),
      current:true,
      current_reasons:[],
      estimated_download_bytes_upper_bound:id==='task-e2e-execute'?134217728:0,
      execution_allowed:true,
      hardlink_count:1,
      plan_digest:`plan-digest-${id}-abcdefghijklmnopqrstuvwxyz`,
      ready:true,
      side_effects_started:realTasks[id].status!=='AWAITING_CONFIRMATION',
      target_device:123,
      target_downloader_id:'qb-e2e',
      target_downloader_version:7,
      target_remote_save_path:'/downloads/e2e',
      target_root:'seeding/e2e',
      verification_level:id==='task-e2e-execute'?'CLIENT_CHECK_REQUIRED':'FULL_VERIFIED',
    });
    const fulfillJson=(route,body,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
    const e2eDownloader={
      id:'qb-e2e',name:'qB E2E',type:'QBITTORRENT',base_url:'http://qb-e2e.local',credential_configured:true,
      monitor_rules:{category:'packbreaker'},path_mappings:[{remote_prefix:'/downloads',container_prefix:'/data'}],
      capabilities:{webapi_version:'2.15.1',application_version:'5.2.3'},connection_status:'OK',path_mapping_status:'OK',
      enabled:true,version:7,last_test_at:now(),last_path_diagnostic_at:now(),created_at:now(),updated_at:now(),
    };
    await page.route('**/api/v1/downloaders',route=>fulfillJson(route,{items:[e2eDownloader]}));
    await page.route('**/api/v1/task-units/**',async route=>{
      const url=new URL(route.request().url());
      const match=url.pathname.match(/\/api\/v1\/task-units\/unit-(task-e2e-(?:execute|cancel))\/(.+)$/);
      if(!match)return route.fallback();
      const id=match[1],tail=match[2];
      if(tail==='decision')return fulfillJson(route,review(id));
      if(tail==='decision/verification')return fulfillJson(route,verification(id));
      if(tail==='execution-gate')return fulfillJson(route,gate(id));
      if(tail==='execution-plan')return fulfillJson(route,plan(id));
      return fulfillJson(route,{code:'NOT_FOUND',detail:'E2E route not found'},404);
    });
    await page.route('**/api/v1/tasks**',async route=>{
      const request=route.request();
      const url=new URL(request.url());
      if(url.pathname==='/api/v1/tasks'&&request.method()==='GET')return fulfillJson(route,{items:Object.values(realTasks)});
      const match=url.pathname.match(/\/api\/v1\/tasks\/(task-e2e-(?:execute|cancel))(?:\/(.+))?$/);
      if(!match)return route.fallback();
      const id=match[1],tail=match[2]||'';
      if(request.method()==='GET'&&!tail)return fulfillJson(route,realTasks[id]);
      if(request.method()==='GET'&&tail==='events'){
        return fulfillJson(route,{items:eventsAfter(id,url.searchParams.get('after_event_id'))});
      }
      if(request.method()==='GET'&&tail==='events/stream'){
        const cursor=request.headers()['last-event-id']||url.searchParams.get('after_event_id');
        const deadline=Date.now()+10000;
        let pending=eventsAfter(id,cursor);
        while(!pending.length&&Date.now()<deadline){
          await new Promise(resolve=>setTimeout(resolve,50));
          pending=eventsAfter(id,cursor);
        }
        const body=pending.length
          ? pending.map(event=>`id: ${event.id}\nevent: task-event\nretry: 1000\ndata: ${JSON.stringify(event)}\n\n`).join('')
          : 'retry: 1000\n: keep-alive\n\n';
        return route.fulfill({status:200,contentType:'text/event-stream',headers:{'Cache-Control':'no-cache, no-store','X-Accel-Buffering':'no'},body});
      }
      if(request.method()==='GET'&&tail==='preflight')return fulfillJson(route,preflight(id));
      if(request.method()==='GET'&&tail==='candidates')return fulfillJson(route,{items:[candidate(id)]});
      if(request.method()==='GET'&&tail==='units')return fulfillJson(route,{items:[taskUnit(id)]});
      if(request.method()==='POST'&&tail==='actions'){
        const body=request.postDataJSON();
        const key=request.headers()['idempotency-key'];
        assert.ok(key,'公开副作用动作必须携带 Idempotency-Key');
        if(body.action==='execute'){
          executeKeys.push(key);executeAttempts+=1;
          if(executeAttempts===1){
            transitionTask(id,'ADDING','LINKING_COMPLETED','E2E 文件系统动作完成，进入添加阶段');
            return route.abort('connectionreset');
          }
          scheduleExecuteProgress();
          return fulfillJson(route,{action:'execute',task_id:id,status:'ADDING',task_version:2,execution_plan_id:`plan-${id}`,operation_replayed:true,receipt_id:'receipt-execute',idempotency_replayed:true});
        }
        if(body.action==='cancel'){
          cancelKeys.push(key);cancelBodies.push(body);
          transitionTask(id,'ROLLING_BACK','ROLLBACK_STARTED','E2E 取消请求已冻结并开始回滚');
          setTimeout(()=>appendTaskEvent(id,'QBITTORRENT_REMOVE_INTENT_RECORDED','ROLLING_BACK','ROLLING_BACK','qBittorrent 移除任务：已记录 operation journal intent'),150);
          setTimeout(()=>appendTaskEvent(id,'QBITTORRENT_REMOVE_APPLIED','ROLLING_BACK','ROLLING_BACK','qBittorrent 移除任务：已由 operation journal 与完成后证据确认副作用完成'),350);
          setTimeout(()=>transitionTask(id,'CANCELLED','ROLLBACK_COMPLETED','E2E journal-owned 回滚完成'),750);
          return fulfillJson(route,{action:'cancel',task_id:id,status:'ROLLING_BACK',task_version:realTasks[id].version,execution_plan_id:`plan-${id}`,operation_replayed:false,receipt_id:'receipt-cancel',idempotency_replayed:false});
        }
      }
      return fulfillJson(route,{code:'NOT_FOUND',detail:'E2E route not found'},404);
    });
    await page.goto('http://127.0.0.1:5173/',{waitUntil:'networkidle'});
    await page.getByRole('heading',{name:'任务中心',exact:true}).waitFor();
    await page.screenshot({path:path.join(output,'desktop.png'),fullPage:true});
    await page.getByPlaceholder('搜索 UUID、source hash、unit key…').fill('不存在');
    await page.getByText('数据库中暂无真实任务').waitFor();
    await page.getByPlaceholder('搜索 UUID、source hash、unit key…').fill('');
    await page.getByText('task-e2e-execute',{exact:true}).waitFor();

    // 真实动作 UI：首次 execute 响应丢失后必须复用同一幂等键，随后由 TaskEvent SSE 自动收敛到 DONE。
    await page.locator('nav').getByRole('button',{name:'预演与确认',exact:true}).click();
    await page.getByRole('heading',{name:'预演与确认',exact:true,level:1}).waitFor();
    const executeRow=page.locator('.el-table__row').filter({hasText:'task-e2e-execute'});
    await executeRow.getByRole('button',{name:'审核 / 证据',exact:true}).click();
    await page.getByRole('button',{name:'确认并执行当前计划',exact:true}).click();
    await page.locator('.el-message-box').getByRole('button',{name:'执行当前计划',exact:true}).click();
    await page.getByText(/API_UNAVAILABLE/).waitFor();
    await page.getByRole('button',{name:'重试确认执行结果',exact:true}).click();
    await page.getByText(/执行结果已确认：ADDING/).waitFor();
    assert.equal(executeKeys.length,2,'响应丢失后的 execute 应重试一次');
    assert.equal(executeKeys[0],executeKeys[1],'响应丢失后的 execute 必须复用相同 Idempotency-Key');
    await page.getByText('QBITTORRENT_ADD_APPLIED',{exact:true}).waitFor({timeout:12000});
    await page.getByText('QBITTORRENT_RECHECK_APPLIED',{exact:true}).waitFor({timeout:12000});
    await page.getByText('QBITTORRENT_START_APPLIED',{exact:true}).waitFor({timeout:12000});
    await page.getByText('QBITTORRENT_SEEDING_CONFIRMED',{exact:true}).waitFor({timeout:12000});
    await page.locator('.review-identity').filter({hasText:'task-e2e-execute'}).getByText(/DONE · v5/).waitFor({timeout:12000});
    await page.keyboard.press('Escape');

    // 取消 UI：禁止仅回滚文件；显式 qB remove + rollback 后由 TaskEvent SSE 收敛到 CANCELLED。
    const cancelRow=page.locator('.el-table__row').filter({hasText:'task-e2e-cancel'});
    await cancelRow.getByRole('button',{name:'审核 / 证据',exact:true}).click();
    const rollbackCheckbox=page.locator('.cancellation-option').filter({hasText:'回滚 PackBreaker 创建的 hardlink'}).locator('.el-checkbox');
    const removeCheckbox=page.locator('.cancellation-option').filter({hasText:'移除 PackBreaker 创建的 qBittorrent 任务'}).locator('.el-checkbox');
    await rollbackCheckbox.click();
    await page.getByText('回滚文件前必须同时移除下载器任务',{exact:true}).waitFor();
    assert.equal(await page.getByRole('button',{name:'按已确认范围取消任务',exact:true}).isDisabled(),true,'rollback-only 必须阻断');
    await removeCheckbox.click();
    await page.getByText(/我已确认上面的移除\/保留范围/).click();
    await page.getByRole('button',{name:'按已确认范围取消任务',exact:true}).click();
    await page.locator('.el-message-box').getByRole('button',{name:'按以上范围取消',exact:true}).click();
    await page.getByText(/取消动作已完成：ROLLING_BACK/).waitFor();
    assert.equal(cancelKeys.length,1,'取消动作应只提交一次');
    assert.equal(cancelBodies[0].remove_downloader_task,true);
    assert.equal(cancelBodies[0].rollback_created_resources,true);
    await page.getByText('QBITTORRENT_REMOVE_APPLIED',{exact:true}).waitFor({timeout:7000});
    await page.getByText('ROLLBACK_COMPLETED',{exact:true}).waitFor({timeout:7000});
    await page.locator('.review-identity').filter({hasText:'task-e2e-cancel'}).getByText(/CANCELLED/).waitFor({timeout:7000});
    await page.keyboard.press('Escape');

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
    console.log('通过：任务筛选、审核、真实执行/取消幂等确认、状态自动刷新、11 页导航、历史扫描、清理确认、390px 移动布局与深色主题。');
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1});

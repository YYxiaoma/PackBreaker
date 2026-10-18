// 使用已安装的 Playwright bundled Chromium；可用 PB_BROWSER 覆盖，不连接外部站点。
const { chromium } = require(process.env.PB_PLAYWRIGHT || '../frontend/node_modules/playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async()=>{
  const requestedBrowser=process.env.PB_BROWSER||'bundled';
  const launchOptions={headless:true};
  if(requestedBrowser!=='bundled') launchOptions.channel=requestedBrowser;
  const browser=await chromium.launch(launchOptions);
  const page=await browser.newPage({viewport:{width:2560,height:1440},deviceScaleFactor:1});
  await page.addInitScript(()=>{
    const proto=globalThis.Crypto?.prototype;
    if(proto){
      try{Object.defineProperty(proto,'randomUUID',{value:undefined,configurable:true});}catch{}
    }
  });
  const errors=[];
  const unmockedApiCalls=[];
  await page.route('**/api/v1/**',route=>{
    const request=route.request();
    const url=new URL(request.url());
    unmockedApiCalls.push(`${request.method()} ${url.pathname}`);
    return route.fulfill({
      status:501,
      contentType:'application/problem+json',
      body:JSON.stringify({code:'E2E_UNMOCKED_API',detail:'浏览器门禁禁止访问未显式 mock 的 API'}),
    });
  });
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
    await page.route('**/api/v1/notifications/inbox/unread-count',route=>fulfillJson(route,{count:0}));
    await page.route('**/api/v1/notifications/inbox**',route=>{
      const request=route.request();
      if(request.method()==='GET')return fulfillJson(route,{items:[]});
      return fulfillJson(route,{code:'METHOD_NOT_ALLOWED',detail:'E2E inbox method'},405);
    });
    const realTasks={
      'task-e2e-execute':{id:'task-e2e-execute',type:'MOVIE',source_downloader_id:'source-qb',source_hash:'source-execute',normalized_unit_key:'movie:execute',status:'AWAITING_CONFIRMATION',version:1,error_code:null,created_at:now(),updated_at:now()},
      'task-e2e-cancel':{id:'task-e2e-cancel',type:'MOVIE',source_downloader_id:'source-qb',source_hash:'source-cancel',normalized_unit_key:'movie:cancel',status:'LINKING',version:2,error_code:null,created_at:now(),updated_at:now()},
      'task-e2e-reconcile':{id:'task-e2e-reconcile',type:'MOVIE',source_downloader_id:'source-qb',source_hash:'source-reconcile',normalized_unit_key:'movie:reconcile',status:'LINKING',version:2,error_code:null,created_at:now(),updated_at:now()},
      'task-e2e-pre-cancel':{id:'task-e2e-pre-cancel',type:'MOVIE',source_downloader_id:'source-qb',source_hash:'source-pre-cancel',normalized_unit_key:'movie:pre-cancel',status:'PENDING',version:1,error_code:null,created_at:now(),updated_at:now()},
      'task-e2e-analysis-cancel':{id:'task-e2e-analysis-cancel',type:'MOVIE',source_downloader_id:'source-qb',source_hash:'source-analysis-cancel',normalized_unit_key:'movie:analysis-cancel',status:'SEARCHING',version:3,error_code:null,created_at:now(),updated_at:now()},
    };
    const executeKeys=[];
    const cancelKeys=[];
    const cancelBodies=[];
    const reconcileKeys=[];
    const retentionKeys=[];
    const retentionBodies=[];
    const preCancelKeys=[];
    const preCancelBodies=[];
    const analysisCancelKeys=[];
    const analysisCancelBodies=[];
    let preCancelAttempts=0;
    let reconcileAttempts=0;
    let retentionAttempts=0;
    let retentionPurged=false;
    let executeAttempts=0;
    let siteHealthReads=0;
    let siteEnableCalls=0;
    let siteTestCalls=0;
    let siteSyncCalls=0;
    const taskEvents={
      'task-e2e-execute':[{id:'event-execute-1',task_id:'task-e2e-execute',from_status:'PREFLIGHT',to_status:'AWAITING_CONFIRMATION',event_type:'REVIEW_OPENED',reason:'E2E 初始审核已确认',created_at:now()}],
      'task-e2e-cancel':[{id:'event-cancel-1',task_id:'task-e2e-cancel',from_status:'AWAITING_CONFIRMATION',to_status:'LINKING',event_type:'LINKING_STARTED',reason:'E2E 初始链接阶段',created_at:now()}],
      'task-e2e-reconcile':[{id:'event-reconcile-1',task_id:'task-e2e-reconcile',from_status:'AWAITING_CONFIRMATION',to_status:'LINKING',event_type:'FILESYSTEM_HARDLINK_RECONCILE_REQUIRED',reason:'文件系统硬链接创建：当前证据不足以自动确认真实状态，已要求安全对账',created_at:now()}],
      'task-e2e-pre-cancel':[{id:'event-pre-cancel-1',task_id:'task-e2e-pre-cancel',from_status:null,to_status:'PENDING',event_type:'TASK_CREATED',reason:'E2E 零副作用取消初始任务',created_at:now()}],
      'task-e2e-analysis-cancel':[{id:'event-analysis-cancel-1',task_id:'task-e2e-analysis-cancel',from_status:'ANALYZING',to_status:'SEARCHING',event_type:'ANALYSIS_SEARCHING',reason:'E2E 只读分析正在搜索站点',created_at:now()}],
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
    const taskOperations={
      'task-e2e-execute':[],
      'task-e2e-cancel':[
        {id:'journal-maintenance-manual',task_id:'task-e2e-cancel',kind:'OTHER',status:'RECONCILE_REQUIRED',attention_required:true,reconcile_supported:false,created_at:now(),updated_at:now()},
        {id:'journal-maintenance-blocked',task_id:'task-e2e-cancel',kind:'FILESYSTEM_HARDLINK',status:'ROLLED_BACK',attention_required:false,reconcile_supported:false,created_at:now(),updated_at:now()},
      ],
      'task-e2e-reconcile':[
        {id:'journal-reconcile-fs',task_id:'task-e2e-reconcile',kind:'FILESYSTEM_HARDLINK',status:'RECONCILE_REQUIRED',attention_required:true,reconcile_supported:true,created_at:now(),updated_at:now()},
        {id:'journal-reconcile-qb',task_id:'task-e2e-reconcile',kind:'QBITTORRENT_ADD',status:'ROLLBACK_BLOCKED',attention_required:true,reconcile_supported:false,created_at:now(),updated_at:now()},
      ],
      'task-e2e-pre-cancel':[
        {id:'journal-maintenance-noop',task_id:'task-e2e-pre-cancel',kind:'FILESYSTEM_DIRECTORY',status:'NOOP',attention_required:false,reconcile_supported:false,created_at:now(),updated_at:now()},
      ],
      'task-e2e-analysis-cancel':[],
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
    let selectedExecutionPlanDownloader='qb-e2e';
    const requestedPlanTargets=[];
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
      target_downloader_id:id==='task-e2e-execute'?selectedExecutionPlanDownloader:'qb-e2e',
      target_downloader_version:id==='task-e2e-execute'&&selectedExecutionPlanDownloader==='tr-e2e'?4:7,
      target_remote_save_path:'/downloads/e2e',
      target_root:'seeding/e2e',
      verification_level:id==='task-e2e-execute'?'CLIENT_CHECK_REQUIRED':'FULL_VERIFIED',
    });
    const fulfillJson=(route,body,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)});
    await page.route('**/api/v1/system/health',route=>fulfillJson(route,{
      status:'warning',generated_at:now(),version:'0.1.4',checks:[
        {name:'runtime',status:'ok',code:'RUNTIME_READY',detail:'数据库正常',metrics:{}},
        {name:'storage',status:'ok',code:'STORAGE_OK',detail:'存储正常',metrics:{data_total_bytes:1099511627776,data_free_bytes:549755813888}},
        {name:'resources',status:'ok',code:'RESOURCES_READABLE',detail:'资源正常',metrics:{
          memory_total_bytes:17179869184,memory_available_bytes:8589934592,cpu_load_percent:18.0,
          cpu_load_history_count:3,cpu_load_history_0_percent:12.0,cpu_load_history_1_percent:15.0,cpu_load_history_2_percent:18.0,
        }},
        {name:'backups',status:'warning',code:'BACKUP_MISSING',detail:'尚无普通备份',metrics:{}},
        {name:'tasks',status:'ok',code:'TASK_BACKLOG_OK',detail:'任务正常',metrics:{running:1,awaiting_confirmation:60,retry:12}},
        {name:'statistics',status:'ok',code:'OVERVIEW_STATISTICS_READY',detail:'本地统计',metrics:{
          unpack_total:1286,unpack_success:1158,unpack_failed:42,unpack_running:26,
          unpack_review:60,unpack_no_match:0,unpack_cancelled:0,
          unpack_seeded_bytes:53436265109913,unpack_seeded_torrents:1100,unpack_volume_unknown:1,
          dedup_jobs:32,dedup_jobs_completed:28,dedup_jobs_running:2,dedup_jobs_failed:1,
          dedup_jobs_pending:1,dedup_jobs_cancelled:0,dedup_scan_records:2840,
          dedup_verified_targets:248,dedup_completed_targets:186,dedup_failed_targets:1,
          dedup_reclaimed_estimate_bytes:3760329766993,dedup_pending_estimate_bytes:1297423720775,
          dedup_actual_reclaimed_bytes:null,
        }},
        {name:'operations',status:'warning',code:'OPERATION_RECONCILE_REQUIRED',detail:'对账',metrics:{reconcile_required:2}},
        {name:'sites',status:'ok',code:'SITE_OK',detail:'站点正常',metrics:{configured:1,connection_ok:1}},
        {name:'downloaders',status:'ok',code:'DOWNLOADER_OK',detail:'下载器正常',metrics:{configured:2,connection_ok:2}},
        {name:'workers',status:'ok',code:'BACKGROUND_WORKERS_OK',detail:'调度正常',metrics:{}},
      ],
    }));
    await page.route('**/api/v1/system/release/preflight',route=>fulfillJson(route,{
      status:'ready',app_version:'0.1.4',checks:[
        {name:'config_dir',status:'ok',code:'CONFIG_DIR_OK',detail:'配置目录权限与可写性通过'},
        {name:'database',status:'ok',code:'DATABASE_OK',detail:'SQLite integrity_check 与 migration head 通过（0023_backup_policy）'},
        {name:'secret_key',status:'ok',code:'SECRET_KEY_OK',detail:'主密钥存在且安全自检通过'},
        {name:'data_root',status:'ok',code:'DATA_ROOT_OK',detail:'数据根目录可见；预检未遍历或修改任何媒体文件'},
        {name:'docker_socket',status:'ok',code:'DOCKER_SOCKET_ABSENT',detail:'未检测到 docker.sock，符合默认最小权限部署'},
        {name:'backup_exercise',status:'warning',code:'BACKUP_EXERCISE_SKIPPED',detail:'页面只读预检显式跳过一致性备份创建/验证演练'},
      ],
    }));
    const upgradeDigest=`sha256:${'8'.repeat(64)}`;
    const upgradeImage=`ghcr.io/yyxiaoma/packbreaker@${upgradeDigest}`;
    const upgradeKeys=[];
    let upgradeHelperStatus={protocol_version:1,helper_version:'0.1.4',phase:'idle',message:'升级 helper 已就绪',request_id:null,current_version:null,target_version:null,target_image:null,backup_database_file:null,started_at:null,updated_at:now(),finished_at:null,rollback_performed:false};
    let upgradeStatusDelayMs=0;
    let upgradeReleaseFailure=false;
    await page.route('**/api/v1/system/upgrade',async route=>{
      if(upgradeStatusDelayMs) await new Promise(resolve=>setTimeout(resolve,upgradeStatusDelayMs));
      if(upgradeReleaseFailure)return fulfillJson(route,{
        current_version:'0.1.4',latest_version:null,update_available:false,target_tag:null,target_image_digest:null,immutable_image:null,platform:null,release_error_code:'RELEASE_NETWORK_FAILED',helper_available:false,helper_status:null,can_upgrade:false,blocked_reasons:['UPDATER_HELPER_UNAVAILABLE','RELEASE_NETWORK_FAILED'],
      });
      return fulfillJson(route,{
        current_version:'0.1.4',latest_version:'0.1.5',update_available:true,target_tag:'v0.1.5',target_image_digest:upgradeDigest,immutable_image:upgradeImage,platform:'linux/amd64',release_error_code:null,helper_available:true,helper_status:upgradeHelperStatus,can_upgrade:upgradeHelperStatus.phase==='idle',blocked_reasons:upgradeHelperStatus.phase==='idle'?[]:['UPDATER_BUSY'],
      });
    });
    await page.route('**/api/v1/system/upgrade/actions',route=>{
      const request=route.request();
      assert.equal(request.method(),'POST');
      const key=request.headers()['idempotency-key'];
      assert.ok(key,'Docker 升级必须携带 Idempotency-Key');
      upgradeKeys.push(key);
      assert.deepEqual(request.postDataJSON(),{action:'upgrade',target_version:'0.1.5',target_image_digest:upgradeDigest});
      upgradeHelperStatus={...upgradeHelperStatus,phase:'accepted',message:'升级请求已由独立 helper 接管',request_id:key,current_version:'0.1.4',target_version:'0.1.5',target_image:upgradeImage,backup_database_file:'packbreaker-e2e.db',started_at:now(),updated_at:now()};
      return fulfillJson(route,{request_id:key,current_version:'0.1.4',target_version:'0.1.5',target_image:upgradeImage,backup_database_file:'packbreaker-e2e.db',helper_status:upgradeHelperStatus,idempotency_replayed:false},202);
    });
    await page.route('**/api/v1/system/logs**',route=>fulfillJson(route,{
      window_minutes:60,limit:200,count:0,truncated:false,max_file_bytes:2097152,backup_count:4,approximate_capacity_bytes:10485760,items:[],
    }));
    await page.route('**/api/v1/notification-channels',route=>fulfillJson(route,{items:[]}));
    let backupRuns=0;
    let backupPolicy={
      enabled:false,interval_hours:24,retention_days:30,keep_latest:3,version:1,last_attempt_at:null,last_success_at:null,last_error_code:null,driver_running:true,driver_consecutive_errors:0,
    };
    await page.route('**/api/v1/system/backups/policy',route=>{
      const request=route.request();
      if(request.method()==='GET')return route.fulfill({status:200,contentType:'application/json',headers:{ETag:`\"${backupPolicy.version}\"`},body:JSON.stringify(backupPolicy)});
      if(request.method()==='PUT'){
        assert.equal(request.headers()['if-match'],`\"${backupPolicy.version}\"`,'备份策略更新必须绑定当前强 If-Match');
        backupPolicy={...backupPolicy,...request.postDataJSON(),version:backupPolicy.version+1};
        return route.fulfill({status:200,contentType:'application/json',headers:{ETag:`\"${backupPolicy.version}\"`},body:JSON.stringify(backupPolicy)});
      }
      return fulfillJson(route,{code:'METHOD_NOT_ALLOWED',detail:'E2E backup policy method'},405);
    });
    await page.route('**/api/v1/system/backups/actions',route=>{
      const request=route.request();
      assert.equal(request.method(),'POST');
      assert.deepEqual(request.postDataJSON(),{action:'run_now'});
      backupRuns+=1;
      const createdAt=now();
      backupPolicy={...backupPolicy,last_attempt_at:createdAt,last_success_at:createdAt,last_error_code:null};
      return fulfillJson(route,{created:true,created_at:createdAt,database_file:'packbreaker-e2e.db',database_size_bytes:4096,retention_deleted_count:0,retention_blocked_count:0,retention_error_code:null,skipped_reason:null});
    });
    const taskDefinitions=[
      {
        id:'definition-manual-e2e',name:'手动拆包 E2E',kind:'MANUAL',status:'ENABLED',site_id:'site-e2e-mteam',site_name:'M-Team E2E',site_available:true,
        source_kind:'DIRECTORY',source_downloader_id:null,source_downloader_name:null,source_directory:'downloads/manual-e2e',source_available:true,
        source_config:{selected_files:[{relative_path:'Movie.E2E.mkv',size_bytes:1073741824,device:1,inode:11,mtime_ns:1}]},cron_expression:null,timezone:null,last_scan_at:null,last_successful_scan_at:null,next_run_at:null,
        file_types:['VIDEO'],video_extensions:['.mkv','.mp4'],archive_extensions:[],min_size_bytes:null,max_size_bytes:null,include_name:null,exclude_names:['sample','trailer'],ignore_temp_files:true,temp_patterns:['*.part','*.tmp','*.!qB'],include_subdirectories:true,max_scan_depth:null,
        output_directory:'output/manual-e2e',storage_mode:'HARDLINK',preserve_structure:true,conflict_policy:'VERIFY_REUSE_OR_STOP',stability_detection_enabled:true,stability_wait_seconds:30,only_completed_downloads:true,initial_scope:'NEW_ONLY',debounce_seconds:30,overlap_policy:'SKIP',auto_retry_enabled:true,max_auto_retries:3,retry_intervals_seconds:[60,300,900],
        high_risk_preauthorization_enabled:false,high_risk_allowed_action_kinds:[],
        latest_execution:{id:'execution-manual-e2e',status:'RUNNING',phase:'AWAITING_CONFIRMATION',trigger:'MANUAL',success_count:0,failed_count:0,skipped_count:0,created_at:now(),started_at:now(),finished_at:null},version:1,created_at:now(),updated_at:now(),
      },
      {
        id:'definition-monitor-e2e',name:'监控拆包 E2E',kind:'MONITOR',status:'ENABLED',site_id:'site-e2e-mteam',site_name:'M-Team E2E',site_available:true,
        source_kind:'DIRECTORY',source_downloader_id:null,source_downloader_name:null,source_directory:'downloads/monitor-e2e',source_available:true,
        source_config:{},cron_expression:'0 */2 * * *',timezone:'Asia/Shanghai',last_scan_at:now(),last_successful_scan_at:now(),next_run_at:now(),
        file_types:['VIDEO'],video_extensions:['.mkv','.mp4'],archive_extensions:[],min_size_bytes:null,max_size_bytes:null,include_name:null,exclude_names:['sample','trailer'],ignore_temp_files:true,temp_patterns:['*.part','*.tmp','*.!qB'],include_subdirectories:true,max_scan_depth:null,
        output_directory:'output/monitor-e2e',storage_mode:'HARDLINK',preserve_structure:true,conflict_policy:'VERIFY_REUSE_OR_STOP',stability_detection_enabled:true,stability_wait_seconds:30,only_completed_downloads:true,initial_scope:'INCLUDE_EXISTING',debounce_seconds:30,overlap_policy:'SKIP',auto_retry_enabled:true,max_auto_retries:3,retry_intervals_seconds:[60,300,900],
        high_risk_preauthorization_enabled:false,high_risk_allowed_action_kinds:[],
        latest_execution:null,version:1,created_at:now(),updated_at:now(),
      },
    ];
    const taskDefinitionExecution=()=>({
      id:'execution-manual-e2e',
      status:'RUNNING',
      phase:'AWAITING_CONFIRMATION',
      trigger:'MANUAL',
      success_count:0,
      failed_count:0,
      skipped_count:0,
      created_at:now(),
      started_at:now(),
      finished_at:null,
      task_definition_id:'definition-manual-e2e',
      task_name:'手动拆包 E2E',
      source_execution_id:null,
      discovered_count:4,
      trace_id:'trace-execution-manual-e2e',
      config_snapshot:{site_id:'site-e2e-mteam'},
      items:[
        {
          id:'execution-item-manual-e2e',
          unpack_task_id:'task-e2e-execute',
          source_object_key:'movie:execute',
          name:'E2E task execute',
          source:'downloads/manual-e2e',
          size_bytes:1073741824,
          phase:'AWAITING_CONFIRMATION',
          progress:40,
          result:null,
          error_code:null,
          error_summary_zh:null,
          technical_detail:null,
          retryable:false,
          retry_count:0,
          lifecycle_stage:'REVIEW',
          risk_level:'MEDIUM',
          authorization_status:'REVIEW_REQUIRED',
          execution_plan_id:'plan-task-e2e-execute',
          execution_plan_ready:true,
          side_effects_started:false,
          lifecycle_blocked_reasons:[],
          risk_summary:null,
          approval:null,
          closure:{
            status:'OPEN',
            filesystem_status:'PENDING',
            downloader_status:'PENDING',
            operation_attention_count:0,
            reconcile_required_count:0,
            rollback_blocked_count:0,
            retention_candidate_count:0,
            manual_attention_required:false,
            issue_codes:[],
          },
        },
        {
          id:'execution-item-cancel-e2e',
          unpack_task_id:'task-e2e-cancel',
          source_object_key:'movie:cancel',
          name:'E2E task cancel',
          source:'downloads/manual-e2e',
          size_bytes:1073741824,
          phase:'LINKING',
          progress:55,
          result:null,
          error_code:null,
          error_summary_zh:null,
          technical_detail:null,
          retryable:false,
          retry_count:0,
          lifecycle_stage:'LINKING',
          risk_level:'MEDIUM',
          authorization_status:'AUTHORIZED',
          execution_plan_id:'plan-task-e2e-cancel',
          execution_plan_ready:true,
          side_effects_started:true,
          lifecycle_blocked_reasons:[],
          risk_summary:null,
          approval:null,
          closure:{
            status:'OPEN',
            filesystem_status:'ACTIVE',
            downloader_status:'ACTIVE',
            operation_attention_count:0,
            reconcile_required_count:0,
            rollback_blocked_count:0,
            retention_candidate_count:0,
            manual_attention_required:false,
            issue_codes:[],
          },
        },
        {
          id:'execution-item-reconcile-e2e',
          unpack_task_id:'task-e2e-reconcile',
          source_object_key:'movie:reconcile',
          name:'E2E task reconcile',
          source:'downloads/manual-e2e',
          size_bytes:1073741824,
          phase:'LINKING',
          progress:55,
          result:null,
          error_code:null,
          error_summary_zh:null,
          technical_detail:null,
          retryable:false,
          retry_count:0,
          lifecycle_stage:'LINKING',
          risk_level:'MEDIUM',
          authorization_status:'AUTHORIZED',
          execution_plan_id:'plan-task-e2e-reconcile',
          execution_plan_ready:true,
          side_effects_started:true,
          lifecycle_blocked_reasons:['OPERATION_RECONCILE_REQUIRED'],
          risk_summary:null,
          approval:null,
          closure:{
            status:'NEEDS_ATTENTION',
            filesystem_status:'RECONCILE_REQUIRED',
            downloader_status:'ROLLBACK_BLOCKED',
            operation_attention_count:2,
            reconcile_required_count:1,
            rollback_blocked_count:1,
            retention_candidate_count:0,
            manual_attention_required:true,
            issue_codes:['OPERATION_RECONCILE_REQUIRED'],
          },
        },
        {
          id:'execution-item-retention-e2e',
          unpack_task_id:'task-e2e-pre-cancel',
          source_object_key:'movie:retention',
          name:'E2E task retention',
          source:'downloads/manual-e2e',
          size_bytes:1073741824,
          phase:'DONE',
          progress:100,
          result:'CANCELLED',
          error_code:null,
          error_summary_zh:null,
          technical_detail:null,
          retryable:false,
          retry_count:0,
          lifecycle_stage:'CANCELLED',
          risk_level:'LOW',
          authorization_status:'NOT_REQUIRED',
          execution_plan_id:null,
          execution_plan_ready:null,
          side_effects_started:false,
          lifecycle_blocked_reasons:[],
          risk_summary:null,
          approval:null,
          closure:{
            status:'CLOSED',
            filesystem_status:'CLEAN',
            downloader_status:'CLEAN',
            operation_attention_count:0,
            reconcile_required_count:0,
            rollback_blocked_count:0,
            retention_candidate_count:1,
            manual_attention_required:false,
            issue_codes:[],
          },
        },
      ],
      events:[],
    });
    await page.route('**/api/v1/task-definitions',route=>{
      const request=route.request();
      if(request.method()==='GET')return fulfillJson(route,{items:taskDefinitions});
      return fulfillJson(route,{code:'METHOD_NOT_ALLOWED',detail:'E2E task definition method'},405);
    });
    await page.route('**/api/v1/task-definitions/definition-manual-e2e/executions**',route=>{
      const request=route.request();
      const url=new URL(request.url());
      if(request.method()!=='GET')return route.fallback();
      if(url.pathname==='/api/v1/task-definitions/definition-manual-e2e/executions'){
        const execution=taskDefinitionExecution();
        return fulfillJson(route,{
          items:[{
            id:execution.id,status:execution.status,phase:execution.phase,trigger:execution.trigger,
            success_count:execution.success_count,failed_count:execution.failed_count,skipped_count:execution.skipped_count,
            created_at:execution.created_at,started_at:execution.started_at,finished_at:execution.finished_at,
            task_definition_id:execution.task_definition_id,task_name:execution.task_name,
            source_execution_id:execution.source_execution_id,discovered_count:execution.discovered_count,trace_id:execution.trace_id,
          }],
          page:1,page_size:20,total:1,
        });
      }
      if(url.pathname==='/api/v1/task-definitions/definition-manual-e2e/executions/execution-manual-e2e'){
        return fulfillJson(route,taskDefinitionExecution());
      }
      return route.fallback();
    });
    const e2eDownloader={
      id:'qb-e2e',name:'qB E2E',type:'QBITTORRENT',base_url:'http://qb-e2e.local',credential_configured:true,
      monitor_rules:{category:'packbreaker'},path_mappings:[{remote_prefix:'/downloads',container_prefix:'/data'}],
      capabilities:{webapi_version:'2.15.1',application_version:'5.2.3'},connection_status:'OK',path_mapping_status:'OK',
      enabled:true,version:7,last_test_at:now(),last_path_diagnostic_at:now(),created_at:now(),updated_at:now(),
    };
    const e2eTransmission={
      ...e2eDownloader,id:'tr-e2e',name:'Transmission E2E',type:'TRANSMISSION',base_url:'http://tr-e2e.local',
      version:4,capabilities:{application_version:'4.1.3',supports_skip_checking:false},
    };
    await page.route('**/api/v1/downloaders',route=>fulfillJson(route,{items:[e2eDownloader,e2eTransmission]}));
    await page.route('**/api/v1/downloaders/qb-e2e/metrics',route=>fulfillJson(route,{
      upload_speed_bytes_per_second:1024,
      download_speed_bytes_per_second:2048,
      total_content_size_bytes:3221225472,
      free_space_bytes:107374182400,
      active_torrent_count:1,
      total_torrent_count:2,
      sampled_at:now(),
    }));
    await page.route('**/api/v1/downloaders/tr-e2e/metrics',route=>fulfillJson(route,{
      upload_speed_bytes_per_second:1024,
      download_speed_bytes_per_second:2048,
      total_content_size_bytes:3221225472,
      free_space_bytes:107374182400,
      active_torrent_count:1,
      total_torrent_count:2,
      sampled_at:now(),
    }));
    const siteCanary='PACKBREAKER-SITE-E2E-CREDENTIAL-CANARY';
    const e2eSite={
      id:'site-e2e-mteam',name:'M-Team E2E',type:'MTEAM',base_url:'https://api.m-team.cc',credential_kind:'API_KEY',credential_configured:true,
      download_credential_configured:false,request_timeout_seconds:15,search_interval_seconds:2,user_agent:null,browser_emulation_enabled:false,
      proxy_enabled:false,proxy_host:null,proxy_port:null,proxy_username:null,proxy_credential_configured:false,
      capabilities:{supports_imdb_id:true,min_request_interval_seconds:2},support_status:'SUPPORTED',connection_status:'OK',enabled:false,version:1,last_test_at:now(),created_at:now(),updated_at:now(),
      user_summary:{username:'demo001',user_level:'Power User',uploaded_bytes:100000000000,downloaded_bytes:10000000000,ratio:10.0,seeding_count:256,fetched_at:now()},
    };
    const extraE2ESites=[
      {...e2eSite,id:'site-e2e-mteam-2',name:'M-Team E2E 2',user_summary:{username:'demo005',user_level:'Power User',uploaded_bytes:88000000000,downloaded_bytes:11000000000,ratio:8,seeding_count:210,fetched_at:now()}},
      {...e2eSite,id:'site-e2e-carpt-2',name:'CarPT E2E 2',type:'CARPT',base_url:'https://carpt.net',credential_kind:'COOKIE',user_summary:{username:'demo006',user_level:'种子评论',uploaded_bytes:44000000000,downloaded_bytes:11000000000,ratio:4,seeding_count:90,fetched_at:now()}},
      {...e2eSite,id:'site-e2e-hdfans-2',name:'HDFans E2E 2',type:'HDFANS',base_url:'https://hdfans.org',credential_kind:'COOKIE',user_summary:{username:'demo007',user_level:'Elite User',uploaded_bytes:66000000000,downloaded_bytes:11000000000,ratio:6,seeding_count:150,fetched_at:now()}},
      {...e2eSite,id:'site-e2e-rousi-2',name:'Rousi Pro E2E 2',type:'ROUSI_PRO',base_url:'https://rousi.pro',credential_kind:'API_KEY',user_summary:{username:'demo008',user_level:'Lv.1',uploaded_bytes:33000000000,downloaded_bytes:11000000000,ratio:3,seeding_count:45,fetched_at:now()}},
    ];
    let siteDetailFailures=0;
    let delayNextHistoryResponse=false;
    let delayedHistoryResponseStarted=false;
    const e2eSiteDetail={
      site:e2eSite,
      account:{uid:'123456',username:'demo001',user_level:'Power User',join_time:now(),last_access_at:now(),hnr_status:'正常',hnr_count:0,message_count:2,remark:null},
      statistics:{
        real_uploaded_bytes:100000000000,real_downloaded_bytes:10000000000,uploaded_bytes:100000000000,
        downloaded_bytes:10000000000,ratio:10.0,torrents_posted:42,seeding_count:256,
        seeding_size_bytes:2147483648000,bonus:123456.5,seeding_points:321.5,bonus_per_hour:12.5,
      },
      connection:{
        credential_configured:true,credential_kind:'API_KEY',last_test_at:e2eSite.last_test_at,
        last_sync_at:e2eSite.user_summary.fetched_at,last_sync_duration_ms:128,circuit_state:'CLOSED',last_error_code:null,
      },
      support_status:'SUPPORTED',
      site_version:null,
      config_version:1,
      capabilities:{
        search:true,torrent_detail:true,torrent_download:true,user_info:true,seeding_info:false,
        api:true,cookie:false,browser_emulation:false,proxy:true,
      },
    };
    let e2eSyncHistory=[{
      id:'sync-e2e-1',status:'SUCCESS',started_at:e2eSite.user_summary.fetched_at,finished_at:e2eSite.user_summary.fetched_at,
      duration_ms:128,field_count:12,error_code:null,error_message:null,
    }];
    const e2eSeeding={supported:false,items:[],next_cursor:null};
    let e2eSiteHealth={
      config_version:1,circuit_state:'OPEN',failure_count:3,retry_after_seconds:15,half_open_probe_in_flight:false,rate_limit_wait_seconds:0,
      cache_entries:2,cache_hits:3,cache_misses:1,cache_evictions:0,requests_started:4,requests_succeeded:1,requests_failed:3,retries_scheduled:2,last_error_code:'SITE_TEMPORARY_FAILURE',
    };
    await page.route('**/api/v1/sites**',async route=>{
      const request=route.request();
      const url=new URL(request.url());
      if(url.pathname==='/api/v1/sites'&&request.method()==='GET')return fulfillJson(route,{items:[e2eSite,{...e2eSite,id:'site-e2e-carpt',name:'CarPT E2E',type:'CARPT',base_url:'https://carpt.net',credential_kind:'COOKIE',user_summary:{username:'demo002',user_level:'Power User',uploaded_bytes:50000000000,downloaded_bytes:5000000000,ratio:10,seeding_count:128,fetched_at:now()}},{...e2eSite,id:'site-e2e-hdfans',name:'HDFans E2E',type:'HDFANS',base_url:'https://hdfans.org',credential_kind:'COOKIE',user_summary:{username:'demo003',user_level:'Elite User',uploaded_bytes:25000000000,downloaded_bytes:5000000000,ratio:5,seeding_count:64,fetched_at:now()}},{...e2eSite,id:'site-e2e-rousi',name:'Rousi Pro E2E',type:'ROUSI_PRO',base_url:'https://rousi.pro',credential_kind:'API_KEY',user_summary:{username:'demo004',user_level:'Lv.1',uploaded_bytes:12000000000,downloaded_bytes:4000000000,ratio:3,seeding_count:12,fetched_at:now()}},...extraE2ESites]});
      if(url.pathname==='/api/v1/sites/profiles'&&request.method()==='GET')return fulfillJson(route,{items:[{
        kind:'MTEAM',display_name:'M-TEAM',base_url:'https://api.m-team.cc',credential_kind:'API_KEY',
        request_timeout_seconds:15,search_interval_seconds:2,supports_user_agent:false,
        supports_browser_emulation:false,supports_proxy:true,support_status:'SUPPORTED',
      }]});
      const match=url.pathname.match(/\/api\/v1\/sites\/site-e2e-mteam(?:\/(health|test|actions|detail|sync|sync-history|seeding))?$/);
      if(!match)return route.fallback();
      const tail=match[1]||'';
      if(request.method()==='GET'&&!tail)return fulfillJson(route,e2eSite);
      if(request.method()==='GET'&&tail==='health'){
        siteHealthReads+=1;
        return fulfillJson(route,e2eSiteHealth);
      }
      if(request.method()==='GET'&&tail==='detail'){
        if(siteDetailFailures>0){
          siteDetailFailures-=1;
          return fulfillJson(route,{code:'SITE_TEMPORARY_FAILURE',detail:'E2E 详情刷新失败'},502);
        }
        return fulfillJson(route,e2eSiteDetail);
      }
      if(request.method()==='GET'&&tail==='sync-history'){
        if(delayNextHistoryResponse){
          delayNextHistoryResponse=false;
          delayedHistoryResponseStarted=true;
          await new Promise(resolve=>setTimeout(resolve,900));
          return fulfillJson(route,{items:[{
            id:'sync-e2e-stale',status:'FAILURE',started_at:now(),finished_at:now(),duration_ms:999,field_count:0,
            error_code:'STALE_RESPONSE',error_message:'过期历史响应不应覆盖当前站点数据',
          }]});
        }
        return fulfillJson(route,{items:e2eSyncHistory});
      }
      if(request.method()==='GET'&&tail==='seeding'){
        return fulfillJson(route,e2eSeeding);
      }
      if(request.method()==='POST'&&tail==='sync'){
        siteSyncCalls+=1;
        const syncedAt=now();
        e2eSite.user_summary.fetched_at=syncedAt;
        e2eSiteDetail.account.last_access_at=syncedAt;
        e2eSiteDetail.connection.last_sync_at=syncedAt;
        e2eSiteDetail.connection.last_sync_duration_ms=96;
        e2eSyncHistory.unshift({
          id:'sync-e2e-'+(e2eSyncHistory.length+1),status:'SUCCESS',started_at:syncedAt,finished_at:syncedAt,
          duration_ms:96,field_count:12,error_code:null,error_message:null,
        });
        return fulfillJson(route,e2eSiteDetail);
      }
      if(request.method()==='POST'&&tail==='test'){
        siteTestCalls+=1;
        assert.equal(request.postData(),null,'只读连接测试不应从浏览器提交已保存站点凭证');
        e2eSite.connection_status='OK';e2eSite.last_test_at=now();e2eSiteDetail.connection.last_test_at=e2eSite.last_test_at;
        return fulfillJson(route,{status:'ok',capabilities:e2eSite.capabilities});
      }
      if(request.method()==='POST'&&tail==='actions'){
        const body=request.postDataJSON();
        assert.equal(request.headers()['if-match'],'"'+e2eSite.version+'"','站点动作必须绑定当前强 If-Match');
        if(body.action==='enable'){
          siteEnableCalls+=1;
          e2eSite.enabled=true;e2eSite.version+=1;e2eSite.updated_at=now();
          return fulfillJson(route,e2eSite);
        }
        if(body.action==='disable'){
          e2eSite.enabled=false;e2eSite.version+=1;e2eSite.updated_at=now();
          return fulfillJson(route,e2eSite);
        }
      }
      return fulfillJson(route,{code:'NOT_FOUND',detail:'E2E site route not found'},404);
    });    await page.route('**/api/v1/operations/maintenance-report**',route=>fulfillJson(route,{
      generated_at:now(),
      summary:{total_journals:5,attention_required:2,reconcile_supported:1,manual_only:1,retention_candidates:1,truncated:false},
      repair_items:[
        {journal_id:'journal-maintenance-reconcile',task_id:'task-e2e-reconcile',kind:'FILESYSTEM_HARDLINK',status:'RECONCILE_REQUIRED',reason_code:'SAFE_RECONCILE_AVAILABLE',reason:'journal 已要求重新验证，且存在只读取当前资源状态的安全对账器。',recommended_action:'优先执行只读 reconcile；若重新证明失败，继续保留现场并转人工检查。',action:'RECONCILE',reconcile_supported:true,manual_required:false,created_at:now(),updated_at:now()},
        {journal_id:'journal-maintenance-manual',task_id:'task-e2e-cancel',kind:'OTHER',status:'RECONCILE_REQUIRED',reason_code:'MANUAL_RECONCILE_REQUIRED',reason:'缺少可安全自动证明的完成证据，或该操作类型没有自动对账器。',recommended_action:'人工核对外部资源与历史记录；不要根据当前资源存在或缺失反推历史副作用。',action:'MANUAL_INSPECTION',reconcile_supported:false,manual_required:true,created_at:now(),updated_at:now()},
      ],
      cleanup_candidates:[
        {journal_id:'journal-maintenance-noop',task_id:'task-e2e-pre-cancel',kind:'FILESYSTEM_DIRECTORY',status:'NOOP',reason_code:'NO_SIDE_EFFECT',reason:'journal 已确认没有执行外部副作用，因此没有仍由该 journal 创建并需要保留的资源。',recommendation:'先通过 retention-plan 重新证明任务终态、保留期与零恢复引用；只有证明通过后才可使用带 Idempotency-Key 的 purge 动作删除 payload 并保留 tombstone。',created_at:now(),updated_at:now()},
      ],
    }));
    await page.route('**/api/v1/operations/retention-plan**',route=>fulfillJson(route,{
      generated_at:now(),cutoff:new Date(Date.now()-30*86400000).toISOString(),retention_days:30,
      summary:{candidates:2,inspected:retentionPurged?1:2,eligible:retentionPurged?0:1,blocked:1,truncated:false},
      items:[
        ...(!retentionPurged?[{journal_id:'journal-maintenance-noop',task_id:'task-e2e-pre-cancel',kind:'FILESYSTEM_DIRECTORY',status:'NOOP',eligible:true,reason_code:'ELIGIBLE',updated_at:new Date(Date.now()-60*86400000).toISOString()}]:[]),
        {journal_id:'journal-maintenance-blocked',task_id:'task-e2e-cancel',kind:'FILESYSTEM_HARDLINK',status:'ROLLED_BACK',eligible:false,reason_code:'TASK_CHECKPOINT_REFERENCE',updated_at:new Date(Date.now()-60*86400000).toISOString()},
      ],
    }));
    await page.route('**/api/v1/task-units/**',async route=>{
      const request=route.request();
      const url=new URL(request.url());
      const match=url.pathname.match(/\/api\/v1\/task-units\/unit-(task-e2e-(?:execute|cancel|reconcile|pre-cancel))\/(.+)$/);
      if(!match)return route.fallback();
      const id=match[1],tail=match[2];
      if(id==='task-e2e-pre-cancel'&&tail==='decision')return fulfillJson(route,{code:'REVIEW_NOT_FOUND',detail:'E2E retention task has no review'},404);
      if(tail==='decision')return fulfillJson(route,review(id));
      if(tail==='decision/verification')return fulfillJson(route,verification(id));
      if(tail==='execution-gate')return fulfillJson(route,gate(id));
      if(tail==='execution-plan'){
        if(request.method()==='POST'){
          const body=request.postDataJSON();
          assert.equal(body.target_root,'seeding/e2e','Web 执行计划应保留用户选定的安全目标根');
          assert.ok(['qb-e2e','tr-e2e'].includes(body.target_downloader_id));
          if(id==='task-e2e-execute'){
            requestedPlanTargets.push(body.target_downloader_id);
            selectedExecutionPlanDownloader=body.target_downloader_id;
          }
        }
        return fulfillJson(route,plan(id));
      }
      return fulfillJson(route,{code:'NOT_FOUND',detail:'E2E route not found'},404);
    });
    await page.route('**/api/v1/tasks**',async route=>{
      const request=route.request();
      const url=new URL(request.url());
      if(url.pathname==='/api/v1/tasks'&&request.method()==='GET')return fulfillJson(route,{items:Object.values(realTasks)});
      const match=url.pathname.match(/\/api\/v1\/tasks\/(task-e2e-(?:execute|cancel|reconcile|pre-cancel|analysis-cancel))(?:\/(.+))?$/);
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
      if(request.method()==='GET'&&tail==='operations')return fulfillJson(route,{items:taskOperations[id]});
      if(request.method()==='POST'&&tail==='operations/journal-maintenance-noop/actions'){
        const body=request.postDataJSON();
        const key=request.headers()['idempotency-key'];
        assert.equal(body.action,'purge');
        assert.equal(body.retention_days,30);
        assert.ok(key,'operation retention purge 必须携带 Idempotency-Key');
        retentionKeys.push(key);retentionBodies.push(body);retentionAttempts+=1;
        if(retentionAttempts===1){
          retentionPurged=true;
          return route.abort('connectionreset');
        }
        return fulfillJson(route,{action:'purge',task_id:id,journal_id:'journal-maintenance-noop',kind:'FILESYSTEM_DIRECTORY',final_status:'NOOP',purged:true,receipt_id:'receipt-retention',idempotency_replayed:true});
      }
      if(request.method()==='POST'&&tail==='operations/journal-reconcile-fs/actions'){
        const body=request.postDataJSON();
        const key=request.headers()['idempotency-key'];
        assert.equal(body.action,'reconcile');
        assert.ok(key,'operation reconcile 必须携带 Idempotency-Key');
        reconcileKeys.push(key);reconcileAttempts+=1;
        if(reconcileAttempts===1){
          const operation=taskOperations[id].find(item=>item.id==='journal-reconcile-fs');
          operation.status='APPLIED';operation.attention_required=false;operation.reconcile_supported=false;operation.updated_at=now();
          appendTaskEvent(id,'OPERATION_RECONCILE_CONFIRMED','LINKING','LINKING','FILESYSTEM_HARDLINK 已通过当前资源与已登记完成快照重新验证；未创建、删除或覆盖文件系统资源');
          return route.abort('connectionreset');
        }
        return fulfillJson(route,{action:'reconcile',task_id:id,journal_id:'journal-reconcile-fs',kind:'FILESYSTEM_HARDLINK',status:'APPLIED',operation_replayed:true,receipt_id:'receipt-reconcile',idempotency_replayed:true});
      }
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
          if(id==='task-e2e-pre-cancel'){
            preCancelKeys.push(key);preCancelBodies.push(body);preCancelAttempts+=1;
            assert.equal(body.remove_downloader_task,false);
            assert.equal(body.rollback_created_resources,false);
            if(preCancelAttempts===1){
              transitionTask(id,'CANCELLING','CANCELLATION_STARTED','E2E 零副作用取消已开始');
              transitionTask(id,'CANCELLED','CANCELLATION_COMPLETED','E2E 未发现 operation journal，副作用开始前安全取消');
              return route.abort('connectionreset');
            }
            return fulfillJson(route,{action:'cancel',task_id:id,status:'CANCELLED',task_version:realTasks[id].version,execution_plan_id:null,operation_replayed:true,receipt_id:'receipt-pre-cancel',idempotency_replayed:true});
          }
          if(id==='task-e2e-analysis-cancel'){
            analysisCancelKeys.push(key);analysisCancelBodies.push(body);
            assert.equal(body.remove_downloader_task,false);
            assert.equal(body.rollback_created_resources,false);
            transitionTask(id,'CANCELLING','CANCELLATION_STARTED','E2E 协作式分析取消请求已登记');
            setTimeout(()=>transitionTask(id,'CANCELLED','CANCELLATION_COMPLETED','E2E 只读分析已在安全检查点停止'),350);
            return fulfillJson(route,{action:'cancel',task_id:id,status:'CANCELLING',task_version:realTasks[id].version,execution_plan_id:null,operation_replayed:false,receipt_id:'receipt-analysis-cancel',idempotency_replayed:false});
          }
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
    upgradeStatusDelayMs=2000;
    const overviewStartedAt=Date.now();
    await page.goto('http://127.0.0.1:5173/',{waitUntil:'domcontentloaded'});
    assert.equal(await page.evaluate(()=>typeof globalThis.crypto?.randomUUID),'undefined','E2E 必须真实模拟 HTTP 内网缺少 crypto.randomUUID 的浏览器环境');
    await page.getByRole('heading',{name:'PackBreaker 总览',exact:true,level:1}).waitFor();
    const overviewFirstPaintMs=Date.now()-overviewStartedAt;
    assert.ok(overviewFirstPaintMs<1500,`总览首屏不应等待慢速 Release 查询，实际 ${overviewFirstPaintMs}ms`);
    const overviewViewports=[
      {width:2560,height:1440,name:'overview-2560'},
      {width:1920,height:1080,name:'overview-1920'},
      {width:1440,height:900,name:'overview-1440'},
      {width:1280,height:800,name:'overview-1280'},
      {width:390,height:844,name:'overview-390'},
    ];
    for(const viewport of overviewViewports){
      await page.setViewportSize({width:viewport.width,height:viewport.height});
      await page.waitForTimeout(80);
      const metrics=await page.evaluate(()=>{
        const cards=[...document.querySelectorAll('.overview-monitor-grid > .monitor-card')].map(element=>{
          const box=element.getBoundingClientRect();
          return {left:box.left,right:box.right,top:box.top,bottom:box.bottom,width:box.width,height:box.height};
        });
        const lifecycle=document.querySelector('.lifecycle')?.getBoundingClientRect();
        return {innerWidth:window.innerWidth,scrollWidth:document.documentElement.scrollWidth,
          cardCount:cards.length,cards,lifeTop:lifecycle?.top??0,lifeWidth:lifecycle?.width??0,
          headingPresent:Boolean(document.querySelector('.overview-monitor-grid h2'))};
      });
      assert.ok(metrics.scrollWidth<=metrics.innerWidth+1,`${viewport.name} 不应横向溢出`);
      assert.equal(metrics.cardCount,6,`${viewport.name} 必须有六张独立系统状态卡`);
      assert.ok(metrics.headingPresent,`${viewport.name} 独立卡片应包含各自标题`);
      assert.ok(metrics.lifeWidth>0,`${viewport.name} 应展示生命周期`);
      for(const card of metrics.cards){
        assert.ok(card.width>0&&card.height>0,`${viewport.name} 状态卡必须可见`);
        assert.ok(card.bottom<=metrics.lifeTop+1,`${viewport.name} 生命周期必须位于六张状态卡下方`);
      }
      if(viewport.width>=1440&&viewport.width<2000){
        for(let i=1;i<3;i+=1)assert.ok(Math.abs(metrics.cards[i].top-metrics.cards[0].top)<3,`${viewport.name} 第一行三列应对齐`);
        for(let i=4;i<6;i+=1)assert.ok(Math.abs(metrics.cards[i].top-metrics.cards[3].top)<3,`${viewport.name} 第二行三列应对齐`);
      }
      if(viewport.width===2560){
        for(const card of metrics.cards)assert.ok(Math.abs(card.top-metrics.cards[0].top)<3,'2560px 六张监控卡必须横向一行');
        const body=await page.locator('.overview-dashboard').boundingBox();
        assert.ok(body.height<1300,'2560×1440 总览主要内容应保持一屏内');
      }
      assert.equal(await page.locator('.overview-business-grid > .business-card').count(),2);
      assert.ok((await page.locator('.overview-kpi-grid').innerText()).includes('1,158'),'成功项使用真实聚合接口');
      assert.ok((await page.locator('.overview-kpi-grid').innerText()).includes('96.5%'),'成功率排除未结束项');
      assert.ok((await page.locator('.overview-kpi-grid').innerText()).includes('≥'),'缺少历史体积时标注下界');
      if(viewport.width===390){
        for(let i=1;i<6;i+=1)assert.ok(metrics.cards[i].top>=metrics.cards[i-1].bottom-1,'390px 状态卡应单列');
      }
      assert.equal(await page.getByText('最近动态',{exact:true}).count(),0,'总览不再展示最近动态卡');
      assert.equal(await page.getByText('快捷操作',{exact:true}).count(),0,'总览不再展示快捷操作卡');
      await page.screenshot({path:path.join(output,`${viewport.name}.png`),fullPage:true});
    }
    upgradeStatusDelayMs=0;
    // 低视口下菜单应保持可滚动，但原生 Y 轴滚动条不应遮挡侧栏。
    await page.setViewportSize({width:1440,height:380});
    const sidebarScroll=await page.locator('.sidebar > nav').evaluate(nav=>{
      const scrollable=nav.scrollHeight>nav.clientHeight;
      const scrollbarWidth=getComputedStyle(nav).scrollbarWidth;
      nav.scrollTop=nav.scrollHeight;
      const last=nav.querySelector('.nav-item:last-child');
      const navBounds=nav.getBoundingClientRect();
      const lastBounds=last?.getBoundingClientRect();
      return {scrollable,scrollbarWidth,scrolled:nav.scrollTop>0,
        lastVisible:Boolean(lastBounds&&lastBounds.bottom<=navBounds.bottom+1&&lastBounds.top>=navBounds.top-1)};
    });
    assert.equal(sidebarScroll.scrollable,true,'低视口侧栏菜单应保持可滚动');
    assert.equal(sidebarScroll.scrollbarWidth,'none','侧栏导航不应出现原生 Y 轴滚动条');
    assert.equal(sidebarScroll.scrolled,true,'低视口侧栏菜单仍应能滚动到底部');
    assert.equal(sidebarScroll.lastVisible,true,'低视口侧栏最后一项必须可访问');
    await page.setViewportSize({width:1440,height:900});
    // v1.0.13 顶栏删除重复的系统状态提示；系统状态仅在总览中展示。
    assert.equal(await page.locator('.header-health').count(),0,'右上角用户框左侧不应再显示系统状态提示');
    assert.equal(await page.getByText('系统需要关注',{exact:true}).count(),0,'顶栏不应继续显示系统需要关注文本');
    await page.getByRole('button',{name:'查看通知',exact:true}).click();
    await page.locator('.notification-inbox-drawer').waitFor({state:'visible'});
    assert.equal(await page.locator('.user-menu-drawer:visible').count(),0,'通知和用户信息抽屉必须独立');
    await page.keyboard.press('Escape');
    await page.locator('.notification-inbox-drawer').waitFor({state:'hidden'});
    const globalSearch=page.getByRole('searchbox',{name:'搜索页面或功能',exact:true});
    await globalSearch.fill('日志');
    await page.getByRole('listbox',{name:'可前往的功能'}).getByRole('option',{name:'日志浏览',exact:true}).click();
    await page.getByRole('heading',{name:'运行日志',exact:true,level:2}).waitFor();
    await globalSearch.fill('站点');
    await globalSearch.press('Enter');
    await page.locator('.site-toolbar').waitFor();
    assert.equal(await globalSearch.inputValue(),'','快捷跳转后应清空搜索框');
    await page.locator('nav').getByRole('button',{name:'总览',exact:true}).click();
    await page.getByRole('heading',{name:'PackBreaker 总览',exact:true,level:1}).waitFor();
    const systemCard=page.locator('.overview-monitor-grid > .monitor-card').filter({hasText:'系统状态'}).first();
    await systemCard.getByText('运行正常',{exact:true}).waitFor();
    assert.equal(await systemCard.getByText('需要关注',{exact:true}).count(),0,'全局 health warning 不应覆盖四个可见子服务均正常的系统状态卡');
    const runningCard=page.locator('.overview-task-strip > span').filter({hasText:'运行中任务'}).first();
    assert.equal((await runningCard.locator('b').textContent())?.trim(),'1','总览运行中任务应使用统一任务执行层计数');
    assert.equal(await page.locator('.cpu-bars span:not(.is-pending)').count(),3,'CPU 图应直接加载后端已积累的历史采样');
    await page.getByText('8.0 GiB',{exact:false}).first().waitFor();
    assert.equal(await page.locator('nav').getByRole('button',{name:'升级中心',exact:true}).count(),0,'主导航不应再展示升级中心');
    assert.equal(await page.getByText('本地工作空间',{exact:true}).count(),0,'侧边栏不应再展示本地工作空间组件');
    const versionTrigger=page.getByRole('button',{name:'当前版本 v0.1.4',exact:true});
    await versionTrigger.waitFor();
    const updateDot=versionTrigger.locator('.version-dot');
    await updateDot.waitFor({state:'visible',timeout:5000});
    assert.equal(await updateDot.evaluate(element=>getComputedStyle(element).backgroundColor),'rgb(229, 72, 77)','后台版本检查发现更新后应显示红点');
    await versionTrigger.click();
    const versionPanel=page.locator('.packbreaker-version-popover');
    await versionPanel.getByText('发现新版本 v0.1.5',{exact:true}).waitFor();
    const releaseLink=versionPanel.getByRole('link',{name:/查看发布/});
    assert.equal(await releaseLink.getAttribute('href'),'https://github.com/YYxiaoma/PackBreaker/releases/tag/v0.1.5','版本弹窗应链接到目标正式 Release');
    await versionPanel.getByRole('button',{name:'立即升级到 v0.1.5',exact:true}).click();
    assert.equal(await page.locator('.el-message-box').count(),0,'立即升级不得再弹出二次确认框');
    await versionPanel.getByText('已接管升级',{exact:true}).waitFor();
    const upgradeProgress=versionPanel.getByRole('progressbar',{name:'在线升级进度'});
    await upgradeProgress.waitFor();
    assert.equal(await upgradeProgress.getAttribute('aria-valuenow'),'8','已接管阶段应显示真实阶段映射的升级进度');
    await versionPanel.getByText('升级请求已由独立 helper 接管',{exact:true}).waitFor();
    assert.equal(upgradeKeys.length,1,'版本弹窗一键升级应只提交一次 Docker 升级请求');
    assert.ok(upgradeKeys[0].startsWith('pb-upgrade-'),'版本弹窗一键升级应生成明确幂等键');
    assert.equal(await versionPanel.getByText('高级：手动 Docker 升级',{exact:true}).count(),0,'版本弹窗不应再展示手动 Docker 升级组件');
    assert.equal(await versionPanel.getByText(/docker pull ghcr\.io\/yyxiaoma\/packbreaker@sha256:/).count(),0,'版本弹窗不应泄露手动 Docker 升级命令');
    assert.equal(await versionPanel.getByRole('button',{name:'版本回退',exact:true}).count(),0,'v1.0.3 版本弹窗不应再展示手动版本回退入口');
    assert.equal(await versionPanel.getByText(/回退不能只替换旧镜像/).count(),0,'v1.0.3 版本弹窗不应再展示历史回退说明');
    assert.equal(upgradeKeys.length,1,'移除手动回退入口不得改变自动升级请求幂等性');
    upgradeHelperStatus={...upgradeHelperStatus,phase:'idle',message:'单容器一次性 updater 已就绪',request_id:null,current_version:null,target_version:null,target_image:null,backup_database_file:null,started_at:null,updated_at:now(),finished_at:null};
    await page.keyboard.press('Escape');
    await versionTrigger.click();
    await versionPanel.waitFor({state:'visible'});
    upgradeReleaseFailure=true;
    await versionPanel.getByRole('button',{name:'检查更新',exact:true}).click();
    await versionPanel.getByText('无法检查最新版本',{exact:true}).waitFor();
    await versionPanel.getByText('RELEASE_NETWORK_FAILED',{exact:true}).waitFor();
    assert.equal(await versionPanel.getByText('已是最新版本',{exact:true}).count(),0,'Release 网络失败时不得误报已是最新版本');
    upgradeReleaseFailure=false;
    await page.keyboard.press('Escape');
    const userMenuTrigger=page.getByRole('button',{name:'打开管理员菜单',exact:true});
    await userMenuTrigger.click();
    const userDrawer=page.locator('.user-menu-drawer');
    await userDrawer.getByText('深色',{exact:true}).click();
    await page.keyboard.press('Escape');
    const darkCards=page.locator('.overview-monitor-grid > .monitor-card');
    assert.equal(await darkCards.count(),6,'深色主题仍需展示六张独立状态卡');
    assert.ok((await darkCards.first().evaluate(el=>getComputedStyle(el).backgroundColor))!=='rgba(0, 0, 0, 0)','深色状态卡背景须可见');
    await page.screenshot({path:path.join(output,'overview-dark-1440.png'),fullPage:true});
    await userMenuTrigger.click();
    await userDrawer.getByText('浅色',{exact:true}).click();
    await page.keyboard.press('Escape');
    const lightCards=page.locator('.overview-monitor-grid > .monitor-card');
    assert.equal(await lightCards.count(),6,'浅色主题仍需展示六张独立状态卡');
    assert.ok((await lightCards.first().evaluate(el=>getComputedStyle(el).backgroundColor))!=='rgba(0, 0, 0, 0)','浅色状态卡背景须可见');
    await page.screenshot({path:path.join(output,'overview-light-1440.png'),fullPage:true});
    console.log(`总览首屏 ${overviewFirstPaintMs}ms；已检查 2560/1920/1440/1280/390 五档布局与明暗主题`);
    await page.route('**/api/v1/system/health',route=>fulfillJson(route,{
      status:'ok',generated_at:now(),version:'0.1.4',checks:[],
    }));
    await page.reload({waitUntil:'domcontentloaded'});
    await page.getByRole('heading',{name:'PackBreaker 总览',exact:true,level:1}).waitFor();
    const missingStatistics=await page.locator('.overview-kpi-grid').innerText();
    assert.ok(missingStatistics.includes('—'),'缺失统计显示未知，不能伪造零值或演示数据');
    assert.ok(!missingStatistics.includes('1,158'),'缺失统计不得沿用示例成果');
    // v1.1.4：一级菜单仅负责展开/折叠；多个一级菜单可以同时展开；二级菜单负责实际导航。
    await page.route('**/api/v1/unpack/definitions',route=>fulfillJson(route,{items:[]}));
    await page.route('**/api/v1/unpack/executions?**',route=>fulfillJson(route,{items:[]}));
    await page.route('**/api/v1/movie-dedup/jobs**',route=>fulfillJson(route,{items:[]}));
    await page.route('**/api/v1/cookiecloud/cron-preview',route=>fulfillJson(route,{
      cron_expression:'*/30 * * * *',timezone:'Asia/Shanghai',description:'每 30 分钟',next_runs:[now()],
    }));
    await page.route('**/api/v1/cookiecloud/config',route=>fulfillJson(route,{
      enabled:false,server_url:'',uuid:'',password_configured:false,auto_sync:true,sync_cron_expression:'*/30 * * * *',request_timeout_seconds:15,
      connection_status:'UNTESTED',last_test_at:null,last_sync_at:null,last_sync_status:'NEVER',last_sync_error_code:null,
      source_domains:0,source_cookies:0,eligible_sites:0,matched_sites:0,updated_sites:0,unchanged_sites:0,unmatched_domains:0,version:1,created_at:now(),updated_at:now(),
    }));
    await page.route('**/api/v1/ai-agent/settings',route=>fulfillJson(route,{
      enabled:false,provider_kind:'OPENAI',base_url:'https://api.openai.com/v1',api_key_configured:false,model:'',request_timeout_seconds:30,max_context_messages:20,
      data_scopes:['SYSTEM_HEALTH'],connection_status:'UNTESTED',last_test_at:null,version:1,created_at:now(),updated_at:now(),
    }));
    await page.route('**/api/v1/ai-agent/status',route=>fulfillJson(route,{
      enabled:false,provider_kind:'OPENAI',connection_status:'UNTESTED',api_key_configured:false,model:'',last_test_at:null,
      telegram_enabled:false,telegram_approval_enabled:false,telegram_driver_running:false,telegram_last_update_id:0,telegram_consecutive_errors:0,telegram_last_error_code:null,
    }));
    await page.route('**/api/v1/ai-agent/telegram',route=>fulfillJson(route,{
      notification_channel_id:null,enabled:false,approval_enabled:false,allowed_chat_ids:[],allowed_user_ids:[],idle_timeout_minutes:60,max_context_messages:20,last_update_id:0,version:1,created_at:now(),updated_at:now(),
    }));    const nav=page.locator('nav');
    const taskPrimary=nav.getByRole('button',{name:'任务中心',exact:true});
    const configPrimary=nav.getByRole('button',{name:'配置中心',exact:true});
    const systemPrimary=nav.getByRole('button',{name:'系统管理',exact:true});
    const initialHash=await page.evaluate(()=>location.hash);
    await taskPrimary.click();
    assert.equal(await taskPrimary.getAttribute('aria-expanded'),'true','任务中心应展开');
    assert.equal(await page.evaluate(()=>location.hash),initialHash,'点击有子菜单的一级菜单不得导航');
    await configPrimary.click();
    assert.equal(await taskPrimary.getAttribute('aria-expanded'),'true','展开配置中心不得收缩任务中心');
    assert.equal(await configPrimary.getAttribute('aria-expanded'),'true','配置中心应展开');
    await systemPrimary.click();
    assert.equal(await taskPrimary.getAttribute('aria-expanded'),'true','展开系统管理不得收缩任务中心');
    assert.equal(await configPrimary.getAttribute('aria-expanded'),'true','展开系统管理不得收缩配置中心');
    assert.equal(await systemPrimary.getAttribute('aria-expanded'),'true','系统管理应展开');
    await configPrimary.click();
    assert.equal(await taskPrimary.getAttribute('aria-expanded'),'true','收缩配置中心不得影响任务中心');
    assert.equal(await configPrimary.getAttribute('aria-expanded'),'false','再次点击只收缩当前一级菜单');
    assert.equal(await systemPrimary.getAttribute('aria-expanded'),'true','收缩配置中心不得影响系统管理');

    await nav.getByRole('button',{name:'数据拆包',exact:true}).click();
    await page.getByRole('heading',{name:'数据拆包',exact:true,level:2}).waitFor();
    assert.equal(decodeURIComponent(await page.evaluate(()=>location.hash.slice(1))),'任务中心/数据拆包','二级菜单应导航到数据拆包');
    assert.equal(await nav.getByRole('button',{name:'任务中心',exact:true}).getAttribute('aria-expanded'),'true','进入子页面后父级应保持展开');

    await nav.getByRole('button',{name:'数据去重',exact:true}).click();
    await page.locator('.movie-dedup-panel').waitFor();
    assert.equal(decodeURIComponent(await page.evaluate(()=>location.hash.slice(1))),'任务中心/数据去重','二级菜单应导航到数据去重');

    await configPrimary.click();
    await nav.getByRole('button',{name:'CookieCloud配置',exact:true}).click();
    await page.locator('.cookiecloud-management').waitFor();
    assert.equal(await page.locator('.site-mode-card').count(),0,'配置中心不应再展示旧模式卡片');

    await nav.getByRole('button',{name:'站点配置',exact:true}).click();
    await page.locator('.site-toolbar').waitFor();
    await page.setViewportSize({width:2560,height:1440});
    const siteCard=page.locator('.site-card').filter({hasText:/M-Team E2E(?! 2)/});
    await siteCard.waitFor();
    assert.equal(await page.locator('.site-card').count(),8,'2K 卡片验收需要两行合成站点卡片');
    assert.equal(await page.evaluate(()=>window.innerWidth),2560,'2K 截图必须使用 2560 CSS px 视口');
    await page.screenshot({path:path.join(output,'sites-scheme-a-2560.png'),fullPage:false});
    for (const width of [1920,1440,1280,390]) {
      await page.setViewportSize({width,height:width===390?844:1080});
      await page.waitForTimeout(300);
      if(width===390){
        const mobileCardBounds=await page.locator('.site-card').first().boundingBox();
        assert.ok(mobileCardBounds&&mobileCardBounds.x>=0&&mobileCardBounds.x+mobileCardBounds.width<=391,'390px 站点卡片必须完整位于视口内，不能被折叠侧栏遮挡');
        await siteCard.getByRole('button',{name:'查看详情',exact:true}).click();
        const mobileDetailDrawer=page.locator('.site-detail-drawer');
        await mobileDetailDrawer.waitFor();
        let mobileDrawerBounds=null;
        for(let attempt=0;attempt<30;attempt+=1){
          mobileDrawerBounds=await mobileDetailDrawer.boundingBox();
          if(mobileDrawerBounds&&mobileDrawerBounds.x>=0&&mobileDrawerBounds.x+mobileDrawerBounds.width<=391)break;
          await page.waitForTimeout(50);
        }
        assert.ok(mobileDrawerBounds&&mobileDrawerBounds.x>=0&&mobileDrawerBounds.x+mobileDrawerBounds.width<=391,'390px 详情抽屉必须完整位于移动视口内');
        assert.equal(await mobileDetailDrawer.getByRole('button',{name:/编辑/}).count(),0,'移动端详情抽屉也不得出现编辑按钮');
        await page.screenshot({path:path.join(output,'site-detail-scheme-d-390.png'),fullPage:false});
        await mobileDetailDrawer.getByRole('button',{name:'关闭详情',exact:true}).click();
        await mobileDetailDrawer.waitFor({state:'hidden'});
      }
      await page.screenshot({path:path.join(output,`sites-scheme-a-${width}.png`),fullPage:false});
    }
    await page.setViewportSize({width:2560,height:1440});
    assert.equal(await page.evaluate(()=>window.innerWidth),2560,'恢复 2K 视口后继续交互验收');
    assert.equal(await siteCard.locator('.site-status-dot').getAttribute('title'),'已停用','停用状态应只通过简洁状态点表达');
    assert.equal(siteHealthReads,0,'打开站点列表不得读取内部 health/circuit 状态');
    assert.equal(await siteCard.getByText(/熔断/).count(),0,'站点卡片不得显示熔断状态');
    assert.equal(await siteCard.getByRole('button',{name:'重置熔断',exact:true}).count(),0,'站点管理不得提供重置熔断入口');
    assert.equal(await page.getByText(siteCanary,{exact:true}).count(),0,'站点页不得回显已保存凭证明文');

    const siteSwitch=siteCard.getByRole('switch',{name:'启用M-Team E2E',exact:true});
    assert.equal(await siteSwitch.getAttribute('aria-checked'),'false','站点初始应保持停用');
    await siteCard.locator('.el-switch').click();
    await page.getByText('M-Team E2E 已启用',{exact:true}).waitFor();
    assert.equal(await siteSwitch.getAttribute('aria-checked'),'true','启用成功后 switch 应采用服务端状态');
    assert.equal(siteEnableCalls,1,'站点启用应只调用一次');
    await siteCard.getByRole('button',{name:'测试连接',exact:true}).click();
    await page.getByText('M-Team E2E 只读连接测试通过',{exact:true}).waitFor();
    assert.equal(siteTestCalls,1,'站点连接测试应只调用一次');
    assert.equal(siteHealthReads,0,'站点连接测试后前端仍不得读取内部 health/circuit 状态');
    await page.waitForTimeout(3200); // Let success toasts disappear before capturing the Drawer reference.

    assert.equal(await siteCard.getByRole('button',{name:'查看详情',exact:true}).count(),1,'站点卡片必须提供查看详情');
    await siteCard.getByRole('button',{name:'查看详情',exact:true}).click();
    const detailDrawer=page.locator('.site-detail-drawer');
    await detailDrawer.waitFor();
    let drawerBounds=null;
    for(let attempt=0;attempt<30;attempt+=1){
      drawerBounds=await detailDrawer.boundingBox();
      if(drawerBounds&&drawerBounds.x>=0&&drawerBounds.x+drawerBounds.width<=2561)break;
      await page.waitForTimeout(50);
    }
    assert.ok(drawerBounds && drawerBounds.x>=0 && drawerBounds.x+drawerBounds.width<=2561,'方案 D 抽屉动画结束后必须完整位于 2560px 视口内');
    await page.screenshot({path:path.join(output,'site-detail-scheme-d-2560.png'),fullPage:false});
    await detailDrawer.getByText('demo001',{exact:true}).first().waitFor();
    assert.equal(await detailDrawer.getByText('Power User',{exact:true}).count(),1,'详情应展示用户等级');
    assert.equal(await detailDrawer.getByText('123456',{exact:true}).count(),1,'详情应展示 UID');
    await detailDrawer.getByText('最后访问',{exact:true}).waitFor();
    await detailDrawer.getByText('消息数',{exact:true}).waitFor();
    await detailDrawer.getByText('做种积分',{exact:true}).waitFor();
    await detailDrawer.getByText('321.5',{exact:true}).waitFor();
    assert.equal(await detailDrawer.getByText('10.00',{exact:true}).count(),1,'详情应展示分享率');
    assert.equal(await detailDrawer.getByRole('button',{name:/编辑/}).count(),0,'详情 Drawer 不得出现编辑按钮');
    assert.equal(await detailDrawer.getByText(siteCanary,{exact:true}).count(),0,'详情 Drawer 不得返回敏感凭证');
    await detailDrawer.getByRole('tab',{name:'详细信息',exact:true}).click();
    await page.screenshot({path:path.join(output,'site-detail-info-scheme-d-2560.png'),fullPage:false});
    await detailDrawer.getByText('请求超时',{exact:true}).waitFor();
    await detailDrawer.getByText('Base URL',{exact:true}).waitFor();
    await detailDrawer.getByText('MTEAM（综合类）',{exact:true}).waitFor();
    await detailDrawer.getByText('API',{exact:true}).waitFor();
    await detailDrawer.getByText('种子详情',{exact:true}).waitFor();
    await detailDrawer.getByText('站点版本',{exact:true}).waitFor();
    assert.equal(await detailDrawer.getByText('创建时间',{exact:true}).count(),0,'详细信息不得展示文档未要求的创建时间');
    assert.equal(await detailDrawer.getByText('更新时间',{exact:true}).count(),0,'详细信息不得展示文档未要求的更新时间');
    assert.equal(await detailDrawer.getByRole('button',{name:/编辑/}).count(),0,'详细信息 Tab 也不得出现编辑按钮');
    await detailDrawer.getByRole('tab',{name:'做种列表',exact:true}).click();
    await detailDrawer.getByText('当前站点适配器未提供做种列表能力',{exact:true}).waitFor();
    await detailDrawer.getByRole('tab',{name:'历史同步',exact:true}).click();
    await detailDrawer.getByText('成功',{exact:true}).waitFor();
    await detailDrawer.getByRole('button',{name:'立即同步',exact:true}).click();
    for(let attempt=0;attempt<50&&siteSyncCalls<1;attempt+=1) await page.waitForTimeout(100);
    assert.equal(siteSyncCalls,1,'立即同步只应调用一次');
    await detailDrawer.getByRole('tab',{name:'历史同步',exact:true}).click();
    assert.equal(await detailDrawer.getByText('成功',{exact:true}).count() >= 1,true,'同步后历史记录应存在');

    // Explicitly exercise a late history response after the drawer closes and reopens.
    delayNextHistoryResponse=true;
    await detailDrawer.getByRole('tab',{name:'概览',exact:true}).click();
    await detailDrawer.getByRole('tab',{name:'历史同步',exact:true}).click();
    for(let attempt=0;attempt<30&&!delayedHistoryResponseStarted;attempt+=1) await page.waitForTimeout(20);
    assert.equal(delayedHistoryResponseStarted,true,'竞态场景必须确认旧历史请求已进入延迟响应');
    await detailDrawer.getByRole('button',{name:'关闭详情',exact:true}).click();
    await detailDrawer.waitFor({state:'hidden'});
    await siteCard.getByRole('button',{name:'查看详情',exact:true}).click();
    await detailDrawer.waitFor();
    for(let attempt=0;attempt<30&&await detailDrawer.getByText('成功',{exact:true}).count()===0;attempt+=1) await page.waitForTimeout(20);
    assert.ok(await detailDrawer.getByText('成功',{exact:true}).count()>=1,'重开抽屉应加载当前站点的成功历史记录');
    await page.waitForTimeout(1000);
    assert.equal(await detailDrawer.getByText('过期历史响应不应覆盖当前站点数据',{exact:true}).count(),0,'关闭后到达的旧历史响应不得覆盖重开后的数据');
    assert.equal(await detailDrawer.getByText('成功',{exact:true}).count()>=1,true,'重开抽屉应保留当前站点的成功历史记录');
    await detailDrawer.getByRole('button',{name:'关闭详情',exact:true}).click();
    await detailDrawer.waitFor({state:'hidden'});

    // A transient detail refresh failure must retain this site's last successful data.
    siteDetailFailures=1;
    await siteCard.getByRole('button',{name:'查看详情',exact:true}).click();
    await detailDrawer.waitFor();
    await detailDrawer.getByRole('tab',{name:'概览',exact:true}).click();
    await detailDrawer.getByText('demo001',{exact:true}).first().waitFor();
    assert.equal(await detailDrawer.getByText('Power User',{exact:true}).count(),1,'详情刷新失败时应保留最近一次成功数据');
    await detailDrawer.getByText('E2E 详情刷新失败',{exact:true}).waitFor();
    await detailDrawer.getByRole('button',{name:'关闭详情',exact:true}).click();
    await detailDrawer.waitFor({state:'hidden'});

    await page.setViewportSize({width:1440,height:900});
    const secondaryRoutes=[
      ['下载器配置','.real-downloader-cards'],
      ['通知配置','.notification-toolbar'],
      ['日志浏览','.operational-logs'],
      ['Agent配置','.ai-settings'],
      ['备份恢复','.backup-management'],
    ];
    for(const [name,selector] of secondaryRoutes){
      await nav.getByRole('button',{name,exact:true}).click();
      await page.locator(selector).waitFor();
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true,name+' 桌面横向溢出');
    }

    await page.getByRole('button',{name:'关于 PackBreaker',exact:true}).click();
    assert.equal(await page.getByRole('button',{name:'关于 PackBreaker',exact:true}).getAttribute('aria-current'),'page','右下角关于入口应标记为当前页');

    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true,'移动页横向溢出');
    await page.getByRole('button',{name:'展开导航',exact:true}).click();
    const mobileNav=page.locator('nav');
    const mobileTask=mobileNav.getByRole('button',{name:'任务中心',exact:true});
    const mobileConfig=mobileNav.getByRole('button',{name:'配置中心',exact:true});
    await mobileTask.click();
    await mobileConfig.click();
    assert.equal(await mobileTask.getAttribute('aria-expanded'),'true','移动端允许多个一级菜单同时展开');
    assert.equal(await mobileConfig.getAttribute('aria-expanded'),'true','移动端配置中心应展开');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true,'移动导航展开不得横向溢出');
    await mobileConfig.click();
    assert.equal(await mobileTask.getAttribute('aria-expanded'),'true','移动端收缩当前菜单不得影响其他菜单');
    assert.equal(await mobileConfig.getAttribute('aria-expanded'),'false','移动端再次点击只收缩当前菜单');
    await mobileNav.getByRole('button',{name:'数据拆包',exact:true}).click();
    await page.locator('.v2-task-center').waitFor();
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true,'移动数据拆包页横向溢出');

    assert.deepEqual(unmockedApiCalls,[],'浏览器门禁不得把未显式 mock 的 API 请求转发到真实后端');
    assert.deepEqual(errors,[]);
    console.log('通过：v1.1.4 多级导航、多个一级菜单同时展开、二级路由直达、站点卡片方案 A、详情 Drawer 方案 D、立即同步/历史同步、关于入口及 390px 移动布局；未显式 mock 的 API 请求全部失败关闭。');  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1});

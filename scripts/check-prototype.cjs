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
  const page=await browser.newPage({viewport:{width:1440,height:1050}});
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
    let siteResetCalls=0;
    let siteEnableCalls=0;
    let siteTestCalls=0;
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
    await page.route('**/api/v1/system/health',route=>fulfillJson(route,{
      status:'ok',generated_at:now(),version:'0.1.4',checks:[],
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
    await page.route('**/api/v1/downloaders',route=>fulfillJson(route,{items:[e2eDownloader]}));
    await page.route('**/api/v1/downloaders/qb-e2e/metrics',route=>fulfillJson(route,{
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
      request_timeout_seconds:15,search_interval_seconds:2,user_agent:null,browser_emulation_enabled:false,
      proxy_enabled:false,proxy_host:null,proxy_port:null,proxy_username:null,proxy_credential_configured:false,
      capabilities:{supports_imdb_id:true,min_request_interval_seconds:2},connection_status:'OK',enabled:false,version:1,last_test_at:now(),created_at:now(),updated_at:now(),
    };
    const e2eSiteProfiles=[{
      kind:'MTEAM',display_name:'M-TEAM',base_url:'https://api.m-team.cc',credential_kind:'API_KEY',
      request_timeout_seconds:15,search_interval_seconds:2,supports_user_agent:false,
      supports_browser_emulation:false,supports_proxy:true,support_status:'SUPPORTED',
    }];
    let e2eSiteHealth={
      config_version:1,circuit_state:'OPEN',failure_count:3,retry_after_seconds:15,half_open_probe_in_flight:false,rate_limit_wait_seconds:0,
      cache_entries:2,cache_hits:3,cache_misses:1,cache_evictions:0,requests_started:4,requests_succeeded:1,requests_failed:3,retries_scheduled:2,last_error_code:'SITE_TEMPORARY_FAILURE',
    };
    await page.route('**/api/v1/sites**',async route=>{
      const request=route.request();
      const url=new URL(request.url());
      if(url.pathname==='/api/v1/sites'&&request.method()==='GET')return fulfillJson(route,{items:[e2eSite]});
      if(url.pathname==='/api/v1/sites/profiles'&&request.method()==='GET')return fulfillJson(route,{items:e2eSiteProfiles});
      const match=url.pathname.match(/\/api\/v1\/sites\/site-e2e-mteam(?:\/(health|test|actions))?$/);
      if(!match)return route.fallback();
      const tail=match[1]||'';
      if(request.method()==='GET'&&!tail)return fulfillJson(route,e2eSite);
      if(request.method()==='GET'&&tail==='health')return fulfillJson(route,e2eSiteHealth);
      if(request.method()==='POST'&&tail==='test'){
        siteTestCalls+=1;
        assert.equal(request.postData(),null,'只读连接测试不应从浏览器提交已保存站点凭证');
        e2eSite.connection_status='OK';e2eSite.last_test_at=now();
        return fulfillJson(route,{status:'ok',capabilities:e2eSite.capabilities});
      }
      if(request.method()==='POST'&&tail==='actions'){
        const body=request.postDataJSON();
        assert.equal(request.headers()['if-match'],`"${e2eSite.version}"`,'站点动作必须绑定当前强 If-Match');
        if(body.action==='reset_circuit'){
          siteResetCalls+=1;
          e2eSiteHealth={...e2eSiteHealth,circuit_state:'CLOSED',failure_count:0,retry_after_seconds:null,half_open_probe_in_flight:false,last_error_code:null};
          return fulfillJson(route,e2eSiteHealth);
        }
        if(body.action==='enable'){
          siteEnableCalls+=1;
          e2eSite.enabled=true;e2eSite.version+=1;e2eSite.updated_at=now();
          e2eSiteHealth={...e2eSiteHealth,config_version:e2eSite.version,circuit_state:'CLOSED',failure_count:0,last_error_code:null};
          return fulfillJson(route,e2eSite);
        }
        if(body.action==='disable'){
          e2eSite.enabled=false;e2eSite.version+=1;e2eSite.updated_at=now();
          e2eSiteHealth={...e2eSiteHealth,config_version:e2eSite.version};
          return fulfillJson(route,e2eSite);
        }
      }
      return fulfillJson(route,{code:'NOT_FOUND',detail:'E2E site route not found'},404);
    });
    await page.route('**/api/v1/operations/maintenance-report**',route=>fulfillJson(route,{
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
      const url=new URL(route.request().url());
      const match=url.pathname.match(/\/api\/v1\/task-units\/unit-(task-e2e-(?:execute|cancel|reconcile|pre-cancel))\/(.+)$/);
      if(!match)return route.fallback();
      const id=match[1],tail=match[2];
      if(id==='task-e2e-pre-cancel'&&tail==='decision')return fulfillJson(route,{code:'REVIEW_NOT_FOUND',detail:'E2E retention task has no review'},404);
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
    await page.getByRole('heading',{name:'PackBreaker 总览',exact:true,level:1}).waitFor();
    const overviewFirstPaintMs=Date.now()-overviewStartedAt;
    assert.ok(overviewFirstPaintMs<1500,`总览首屏不应等待慢速 Release 查询，实际 ${overviewFirstPaintMs}ms`);
    const overviewViewports=[
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
      if(viewport.width>=1440){
        for(let i=1;i<3;i+=1)assert.ok(Math.abs(metrics.cards[i].top-metrics.cards[0].top)<3,`${viewport.name} 第一行三列应对齐`);
        for(let i=4;i<6;i+=1)assert.ok(Math.abs(metrics.cards[i].top-metrics.cards[3].top)<3,`${viewport.name} 第二行三列应对齐`);
      }
      if(viewport.width===390){
        for(let i=1;i<6;i+=1)assert.ok(metrics.cards[i].top>=metrics.cards[i-1].bottom-1,'390px 状态卡应单列');
      }
      assert.equal(await page.getByText('最近动态',{exact:true}).count(),0,'总览不再展示最近动态卡');
      assert.equal(await page.getByText('快捷操作',{exact:true}).count(),0,'总览不再展示快捷操作卡');
      await page.screenshot({path:path.join(output,`${viewport.name}.png`),fullPage:true});
    }
    upgradeStatusDelayMs=0;
    await page.setViewportSize({width:1440,height:900});
    // 顶栏状态使用现有只读健康快照；搜索是页面/功能快捷导航，通知复用已有管理员通知抽屉。
    await page.getByRole('button',{name:'系统运行正常',exact:true}).waitFor();
    await page.getByRole('button',{name:'查看通知',exact:true}).click();
    await page.locator('.user-menu-drawer').waitFor({state:'visible'});
    await page.keyboard.press('Escape');
    await page.locator('.user-menu-drawer').waitFor({state:'hidden'});
    const globalSearch=page.getByRole('searchbox',{name:'搜索页面或功能',exact:true});
    await globalSearch.fill('日志');
    await page.getByRole('listbox',{name:'可前往的功能'}).getByRole('option',{name:'日志',exact:true}).click();
    await page.getByRole('heading',{name:'运行日志',exact:true,level:2}).waitFor();
    await globalSearch.fill('站点');
    await globalSearch.press('Enter');
    await page.locator('.site-toolbar').waitFor();
    assert.equal(await globalSearch.inputValue(),'','快捷跳转后应清空搜索框');
    await page.locator('nav').getByRole('button',{name:'总览',exact:true}).click();
    await page.getByRole('heading',{name:'PackBreaker 总览',exact:true,level:1}).waitFor();
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
    await page.locator('.el-message-box').getByRole('button',{name:'立即升级到 v0.1.5',exact:true}).click();
    await page.locator('.el-overlay.is-message-box').waitFor({state:'detached'});
    await versionTrigger.click();
    await versionPanel.waitFor({state:'visible'});
    await versionPanel.getByText('已接管升级',{exact:true}).waitFor();
    assert.equal(upgradeKeys.length,1,'版本弹窗一键升级应只提交一次 Docker 升级请求');
    assert.ok(upgradeKeys[0].startsWith('pb-upgrade-'),'版本弹窗一键升级应生成明确幂等键');
    assert.equal(await versionPanel.getByText('高级：手动 Docker 升级',{exact:true}).count(),0,'版本弹窗不应再展示手动 Docker 升级组件');
    assert.equal(await versionPanel.getByText(/docker pull ghcr\.io\/yyxiaoma\/packbreaker@sha256:/).count(),0,'版本弹窗不应泄露手动 Docker 升级命令');
    await versionPanel.getByRole('button',{name:'版本回退',exact:true}).click();
    await versionPanel.getByText(/回退不能只替换旧镜像/).waitFor();
    assert.equal(upgradeKeys.length,1,'版本回退说明不得额外调用 Docker 升级 API');
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
    console.log(`总览首屏 ${overviewFirstPaintMs}ms；已检查 1920/1440/1280/390 四档布局与深色六张独立状态卡`);
    await page.getByRole('button',{name:'任务中心',exact:true}).click();
    await page.getByRole('heading',{name:'任务中心',exact:true,level:2}).waitFor();
    await page.getByText('手动拆包 E2E',{exact:true}).waitFor();
    await page.getByText('监控拆包任务',{exact:true}).click();
    await page.getByText('监控拆包 E2E',{exact:true}).waitFor();
    assert.equal(await page.getByRole('button',{name:'立即扫描',exact:true}).count(),1,'监控拆包任务应提供立即扫描入口');
    await page.getByText('手动拆包任务',{exact:true}).click();

    // v0.1.8 已移除独立“预演与确认 / 清理与对账”入口；审核、执行、对账和收尾都必须从任务中心当前 Run 进入。
    assert.equal(await page.locator('nav').getByRole('button',{name:'预演与确认',exact:true}).count(),0,'旧“预演与确认”导航必须删除');
    assert.equal(await page.locator('nav').getByRole('button',{name:'清理与对账',exact:true}).count(),0,'旧“清理与对账”导航必须删除');
    assert.equal(await page.getByText('现有执行引擎任务（兼容区）',{exact:true}).count(),0,'兼容区不应继续暴露为独立页面区块');
    assert.equal(await page.getByRole('button',{name:'新增',exact:true}).count(),0,'手动/监控列表不得重复提供新增入口');
    await page.getByRole('heading',{name:'任务中心',exact:true,level:2}).waitFor();

    // 真实动作 UI：从统一“任务执行详情 → 审核 / 对账”进入；首次 execute 响应丢失后必须复用同一幂等键，随后由 TaskEvent SSE 自动收敛到 DONE。
    const definitionRow=page.locator('.el-table__row').filter({hasText:'手动拆包 E2E'}).first();
    await definitionRow.getByRole('button',{name:'查看',exact:true}).click();
    const definitionDrawer=page.locator('.el-drawer').filter({hasText:'任务详情 · 手动拆包 E2E'}).last();
    await definitionDrawer.getByRole('tab',{name:'执行记录',exact:true}).click();
    const executionHistoryRow=definitionDrawer.locator('.el-table__row').filter({hasText:'手动执行'}).first();
    await executionHistoryRow.getByRole('button',{name:'查看',exact:true}).click();
    const executionDrawer=page.locator('.el-drawer').filter({hasText:'执行记录详情'}).last();
    await executionDrawer.getByRole('tab',{name:'审核 / 对账',exact:true}).click();
    await executionDrawer.getByRole('heading',{name:'审核、校验与对账',exact:true,level:3}).waitFor();
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
    await page.getByText('任务当前为 DONE，不允许修改审核结果',{exact:true}).waitFor({timeout:12000});

    // 同一统一执行详情中切换到底层 cancel Run：禁止仅回滚文件；显式 qB remove + rollback 后由 TaskEvent SSE 收敛到 CANCELLED。
    const evidenceSelector=executionDrawer.locator('.execution-evidence-selector .el-select');
    await evidenceSelector.click();
    await page.getByText('E2E task cancel · OPEN',{exact:true}).click();
    await executionDrawer.getByRole('heading',{name:'审核、校验与对账',exact:true,level:3}).waitFor();
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
    await page.getByText('任务当前为 CANCELLED，不允许修改审核结果',{exact:true}).waitFor({timeout:7000});

    // 当前 cancel Run 的 operation/retention 证据仍按任务隔离展示，不再依赖独立清理页。
    await page.getByText('journal-maintenance-manual',{exact:false}).waitFor();
    await page.getByText('当前状态没有可自动证明的安全恢复动作',{exact:true}).waitFor();
    await page.getByText('journal-maintenance-blocked',{exact:false}).waitFor();
    await page.getByText('任务仍引用该 journal',{exact:true}).first().waitFor();
    assert.equal(await page.getByText('/private/maintenance/secret',{exact:true}).count(),0,'任务操作中心不得暴露私有路径');

    // 阻断/对账：切换到 reconcile Run。响应丢失后即使 SSE 已显示 APPLIED，也只能使用原 journal + 原幂等键确认结果。
    await evidenceSelector.click();
    await page.getByText('E2E task reconcile · NEEDS_ATTENTION',{exact:true}).click();
    await executionDrawer.getByRole('heading',{name:'校验 / 对账 / 收尾',exact:true,level:3}).waitFor();
    assert.equal(await page.getByText('/private/reconcile/movie.mkv',{exact:true}).count(),0,'operation 摘要不得暴露路径');
    await page.getByText('qB 添加',{exact:true}).waitFor();
    const fsReconcileButton=page.getByRole('button',{name:'重新检查',exact:true});
    assert.equal(await fsReconcileButton.count(),1,'只有支持安全快照重验的 journal 才显示对账按钮');
    await fsReconcileButton.click();
    await page.locator('.el-message-box').getByRole('button',{name:'重新验证证据',exact:true}).click();
    await page.getByText(/API_UNAVAILABLE/).waitFor();
    await page.getByText('OPERATION_RECONCILE_CONFIRMED',{exact:true}).waitFor({timeout:7000});
    await page.getByRole('button',{name:'重试确认对账结果',exact:true}).click();
    await page.getByText(/证据重新验证完成：APPLIED/).waitFor();
    assert.equal(reconcileKeys.length,2,'响应丢失后的 reconcile 应重试一次');
    assert.equal(reconcileKeys[0],reconcileKeys[1],'响应丢失后的 reconcile 必须复用相同 Idempotency-Key');

    // 统一任务生命周期内的 retention/purge：切换到已终态且满足 retention-plan 的 Run。
    await evidenceSelector.click();
    await page.getByText('E2E task retention · CLOSED',{exact:true}).click();
    await executionDrawer.getByRole('heading',{name:'校验 / 对账 / 收尾',exact:true,level:3}).waitFor();
    await page.getByText('journal-maintenance-noop',{exact:false}).waitFor();
    const retentionPurgeButton=page.getByRole('button',{name:'清理历史 payload',exact:true});
    assert.equal(await retentionPurgeButton.count(),1,'只有 retention-plan eligible journal 才显示 purge 按钮');
    assert.equal(await page.getByText('/private/maintenance/secret',{exact:true}).count(),0,'任务操作中心不得暴露私有路径');
    await retentionPurgeButton.click();
    await page.locator('.el-message-box').getByRole('button',{name:'重新验证并清理 payload',exact:true}).click();
    await page.getByText('清理响应结果未知；请使用当前操作继续重试确认',{exact:true}).waitFor();
    await page.getByRole('button',{name:'重试确认清理结果',exact:true}).click();
    await page.getByText(/Journal payload 已安全清理/).waitFor();
    assert.equal(retentionKeys.length,2,'retention purge 响应丢失后应重试一次');
    assert.equal(retentionKeys[0],retentionKeys[1],'retention purge 必须复用相同 Idempotency-Key');
    assert.equal(retentionBodies[0].retention_days,30);
    assert.equal(await page.getByRole('button',{name:'清理历史 payload',exact:true}).count(),0,'purge 确认后实时预览不得继续显示已清理 journal');

    await executionDrawer.locator('.el-drawer__close-btn').click();
    await executionDrawer.waitFor({state:'hidden'});
    await definitionDrawer.locator('.el-drawer__close-btn').click();
    await definitionDrawer.waitFor({state:'hidden'});

    // 真实站点管理：health 不伪造、reset 仅重置熔断器、启用使用当前强版本，保存凭证不回显。
    await page.locator('nav').getByRole('button',{name:'站点管理',exact:true}).click();
    await page.locator('.site-toolbar').waitFor();
    const siteCard=page.locator('.connection-card').filter({hasText:'M-Team E2E'});
    await siteCard.getByText('熔断 已打开',{exact:true}).waitFor();
    assert.equal(await page.getByText(siteCanary,{exact:true}).count(),0,'站点页不得回显已保存凭证明文');
    assert.equal(await page.getByRole('button',{name:/HHClub/}).count(),0,'HHClub 未确认前不得出现可执行创建动作');
    await page.getByRole('button',{name:'重置熔断',exact:true}).click();
    await page.locator('.el-message-box').getByRole('button',{name:'仅重置熔断器',exact:true}).click();
    await page.getByText('熔断器已重置；站点是否恢复以之后的请求或连接测试为准',{exact:true}).waitFor();
    assert.equal(siteResetCalls,1,'reset-circuit 应只调用一次');
    await siteCard.getByText('熔断 关闭',{exact:true}).waitFor();
    const siteSwitch=siteCard.getByRole('switch',{name:'启用M-Team E2E',exact:true});
    assert.equal(await siteSwitch.getAttribute('aria-checked'),'false','站点初始应保持停用');
    await siteCard.locator('.el-switch').click();
    await page.getByText('M-Team E2E 已启用',{exact:true}).waitFor();
    assert.equal(await siteSwitch.getAttribute('aria-checked'),'true','启用成功后 switch 应采用服务端状态');
    assert.equal(siteEnableCalls,1,'站点启用应只调用一次');
    await siteCard.getByRole('button',{name:'测试连接',exact:true}).click();
    await page.getByText('M-Team E2E 只读连接测试通过',{exact:true}).waitFor();
    assert.equal(siteTestCalls,1,'站点连接测试应只调用一次');
    assert.equal(await page.getByText(/连接恢复/).count(),0,'reset-circuit 不得伪造远端连接恢复文案');

    for(const name of ['总览','任务中心','站点管理','下载器','日志','系统设置','关于']){
      await page.locator('nav').getByRole('button',{name,exact:false}).click();
      await page.waitForTimeout(50);
      if(name!=='总览')assert.equal(await page.locator('.page-hero').count(),0,`${name} 不应继续显示顶部说明卡片`);
      assert.equal(await page.locator('main').evaluate(el=>el.scrollWidth<=el.clientWidth+1),true,`${name} 桌面溢出`);
    }
    await page.locator('nav').getByRole('button',{name:'系统设置',exact:true}).click();
    await page.getByRole('tab',{name:'备份恢复',exact:true}).click();
    await page.getByText('SQLite 一致性备份',{exact:true}).waitFor();
    await page.getByRole('button',{name:'立即备份',exact:true}).click();
    await page.locator('.el-message-box').getByRole('button',{name:'创建一致性备份',exact:true}).click();
    await page.getByText('一致性备份已创建：packbreaker-e2e.db',{exact:true}).waitFor();
    assert.equal(backupRuns,1,'备份管理页立即备份只应提交一次');
    assert.equal(await page.getByRole('button',{name:/恢复/}).count(),0,'在线管理页不得提供数据库恢复按钮');

    await page.locator('nav').getByRole('button',{name:'任务中心',exact:false}).click();
    await page.setViewportSize({width:390,height:844});
    await page.waitForFunction(()=>document.querySelector('.sidebar').getBoundingClientRect().right<=0); await page.locator('.el-message').last().waitFor({state:'hidden'}); await page.screenshot({path:path.join(output,'mobile.png'),fullPage:true});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true,'移动页横向溢出');
    await page.getByRole('button',{name:'展开导航',exact:true}).click();
    await page.locator('nav').getByRole('button',{name:'站点管理',exact:true}).click();
    await page.locator('.site-toolbar').waitFor();
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true,'移动配置页横向溢出');
    await userMenuTrigger.click();
    await userDrawer.getByText('深色',{exact:true}).click();
    await page.keyboard.press('Escape');
    await page.screenshot({path:path.join(output,'mobile-dark.png'),fullPage:true});
    for(const name of ['总览','任务中心','站点管理','下载器','日志','系统设置','关于']){
      await page.getByRole('button',{name:'展开导航',exact:true}).click();
      await page.locator('nav').getByRole('button',{name,exact:false}).click();
      await page.waitForTimeout(50);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true,`${name} 移动页溢出`);
    }
    assert.deepEqual(unmockedApiCalls,[],'浏览器门禁不得把未显式 mock 的 API 请求转发到真实后端');
    assert.deepEqual(errors,[]);
    console.log('通过：任务筛选、审核、真实执行/取消幂等确认、真实站点 health/reset/启用、状态自动刷新、任务中心大目录监控能力、计划备份管理、品牌版本后台检查红点与单容器一次性 helper 一键升级、清理/对账 retention 安全预览与同键 purge 确认、390px 移动布局与深色主题；未显式 mock 的 API 请求全部失败关闭。');
  } finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1});

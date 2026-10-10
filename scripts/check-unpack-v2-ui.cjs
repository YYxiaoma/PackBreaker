// v1.0.15 数据拆包 v2 专用浏览器烟测；只使用显式 mock，不访问真实后端。
const { chromium } = require(process.env.PB_PLAYWRIGHT || '../frontend/node_modules/playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const { spawn } = require('node:child_process');

const root = path.resolve(__dirname, '..');
const frontendUrl = 'http://127.0.0.1:5175';
const productionPreview = process.env.PB_UNPACK_V2_E2E_PRODUCTION === '1';
const children = [];
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitReady(url) {
  for (let i = 0; i < 60; i += 1) {
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch {
      // Vite 还未开始监听。
    }
    await sleep(250);
  }
  throw new Error(`v1.0.15 UI 测试服务未就绪：${url}`);
}

function startFrontend() {
  const child = spawn(
    'corepack',
    [
      'pnpm', 'exec', 'vite',
      ...(productionPreview ? ['preview'] : []),
      '--host', '127.0.0.1', '--port', '5175', '--strictPort',
    ],
    {
      cwd: path.join(root, 'frontend'),
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env },
    },
  );
  let output = '';
  for (const stream of [child.stdout, child.stderr]) {
    stream.on('data', (chunk) => {
      output = (output + chunk.toString()).slice(-8000);
    });
  }
  children.push({ child, readOutput: () => output });
}

const fulfillJson = (route, body, status = 200) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

(async () => {
  let listening = false;
  try {
    await fetch(frontendUrl, { signal: AbortSignal.timeout(500) });
    listening = true;
  } catch {
    // 预期端口未占用。
  }
  assert.equal(listening, false, `拒绝使用已有前端服务：${frontendUrl}`);
  if (productionPreview) {
    assert.equal(
      require('node:fs').existsSync(path.join(root, 'frontend/dist/index.html')),
      true,
      '生产构建产物不存在，拒绝静默回退到开发服务器',
    );
  }
  startFrontend();
  await waitReady(frontendUrl);

  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 980 } });
  const errors = [];
  const unmocked = [];
  const now = '2026-10-07T12:00:00Z';

  page.on('pageerror', (error) => errors.push(error.message));

  await page.route('**/api/v1/**', (route) => {
    const request = route.request();
    unmocked.push(request.method() + ' ' + new URL(request.url()).pathname);
    return fulfillJson(route, { code: 'E2E_UNMOCKED_API', detail: 'v1.0.15 UI 烟测禁止未显式 mock API' }, 501);
  });

  await page.route('**/api/v1/auth/me', (route) =>
    fulfillJson(route, {
      configured: true,
      authenticated: true,
      permissions: ['admin'],
      expires_at: '2099-01-01T00:00:00Z',
    }),
  );
  await page.route('**/api/v1/notifications/inbox/unread-count', (route) =>
    fulfillJson(route, { count: 0 }),
  );
  await page.route('**/api/v1/system/health', (route) =>
    fulfillJson(route, { status: 'ok', generated_at: now, version: '1.0.15', checks: [] }),
  );
  await page.route('**/api/v1/system/upgrade', (route) =>
    fulfillJson(route, {
      current_version: '1.0.15',
      latest_version: '1.0.15',
      update_available: false,
      can_upgrade: false,
      blocked_reasons: [],
      release_error_code: null,
      target_image_digest: null,
      target_tag: 'v1.0.15',
      helper_status: null,
    }),
  );

  const sites = [
    { id: 'site-mteam', name: 'M-TEAM', enabled: true },
    { id: 'site-hdtime', name: 'HDTime', enabled: true },
  ];
  const downloaders = [
    { id: 'qb-main', name: 'qBittorrent · 主下载器', enabled: true },
    { id: 'tr-main', name: 'Transmission · 备用下载器', enabled: true },
  ];
  await page.route('**/api/v1/sites', (route) => fulfillJson(route, { items: sites }));
  await page.route('**/api/v1/downloaders', (route) => fulfillJson(route, { items: downloaders }));
  const createdDefinitionBodies = [];
  const scanSelectedKeys = new Set();
  let largePageMode = false;
  const observedPageLimits = [];
  const observedExecutionFilters = [];
  let updatedDefinitionBody = null;
  let updatedMonitorBody = null;
  let deletedDefinitionVersion = null;
  let bulkRetryBody = null;
  await page.route('**/api/v1/movie-dedup/jobs', (route) =>
    fulfillJson(route, { items: [] }),
  );

  const manualDefinition = {
    id: 'definition-manual',
    name: '电影库手动拆包',
    trigger_kind: 'MANUAL',
    status: 'PENDING_EXECUTION',
    source_kind: 'DIRECTORY',
    execution_scope_kind: 'ALL_MATCHING_MEDIA',
    source_config: { directory_path: '/downloads/movies' },
    file_filter: { extensions: ['.mkv'], min_size_bytes: null, max_size_bytes: null, include_name: null, exclude_names: [], include_subdirectories: true },
    site_ids: ['site-mteam'],
    output_config: { output_directory: '/downloads/seeding', storage_mode: 'HARDLINK', conflict_policy: 'VERIFY_REUSE_OR_STOP', target_downloader_id: 'qb-main' },
    retry_enabled: true,
    max_retries: 3,
    auto_match_threshold_bps: 10000,
    cron_expression: null,
    timezone: null,
    next_run_at: null,
    last_triggered_at: null,
    selected_source_count: 0,
    version: 1,
    created_at: now,
    updated_at: now,
  };
  const monitorDefinition = {
    ...manualDefinition,
    id: 'definition-monitor',
    name: '新番持续监控',
    trigger_kind: 'MONITOR',
    status: 'ENABLED',
    source_kind: 'DOWNLOADER',
    source_config: { downloader_id: 'qb-main', name_contains: null, categories: ['anime'], tags: ['ready'] },
    site_ids: ['site-mteam', 'site-hdtime'],
    cron_expression: '*/10 * * * *',
    timezone: 'Asia/Shanghai',
  };
  const failedDefinition = {
    ...manualDefinition,
    id: 'definition-failed',
    name: '匹配失败重试测试',
  };
  const failedExecution = {
    id: 'execution-failed',
    definition_id: 'definition-failed',
    trigger: 'MANUAL',
    status: 'FAILED',
    total_count: 1,
    matched_auto_count: 0,
    no_match_count: 1,
    review_count: 0,
    content_verified_count: 0,
    content_mismatch_count: 0,
    timeout_count: 0,
    error_count: 0,
    completed_count: 0,
    version: 5,
    created_at: now,
    updated_at: now,
  };
  await page.route('**/api/v1/unpack/definitions/definition-manual', (route) => {
    const req = route.request();
    if (req.method() === 'PUT') {
      updatedDefinitionBody = req.postDataJSON();
      assert.equal(req.headers()['if-match'], '1');
      return fulfillJson(route, { ...manualDefinition, ...updatedDefinitionBody, version: 2 });
    }
    if (req.method() === 'DELETE') {
      deletedDefinitionVersion = req.headers()['if-match'];
      return route.fulfill({ status: 204, body: '' });
    }
    return fulfillJson(route, { code: 'UNMOCKED_DEFINITION', detail: '未授权操作' }, 501);
  });
  await page.route('**/api/v1/unpack/definitions/definition-monitor', (route) => {
    const req = route.request();
    assert.equal(req.method(), 'PUT');
    assert.equal(req.headers()['if-match'], '1');
    updatedMonitorBody = req.postDataJSON();
    return fulfillJson(route, { ...monitorDefinition, ...updatedMonitorBody, version: 2 });
  });
  await page.route('**/api/v1/unpack/executions/execution-failed/actions', (route) => {
    bulkRetryBody = route.request().postDataJSON();
    assert.equal(route.request().headers()['if-match'], '5');
    return fulfillJson(route, { execution_id: 'execution-failed', retried_count: 1 });
  });
  const execution = {
    id: 'execution-review',
    definition_id: 'definition-monitor',
    trigger: 'MONITOR',
    status: 'REVIEW_REQUIRED',
    discovery_complete: true,
    discovery_cursor: null,
    total_count: 3,
    matched_auto_count: 1,
    no_match_count: 0,
    review_count: 1,
    content_verified_count: 0,
    content_mismatch_count: 0,
    timeout_count: 0,
    error_count: 1,
    completed_count: 0,
    started_at: now,
    finished_at: null,
    version: 5,
    created_at: now,
    updated_at: now,
  };
  await page.route('**/api/v1/unpack/definitions', async (route) => {
    if (route.request().method() === 'POST') {
      const body = route.request().postDataJSON();
      createdDefinitionBodies.push(body);
      return fulfillJson(route, {
        ...manualDefinition,
        id: 'definition-created',
        name: body.name,
        execution_scope_kind: body.execution_scope_kind,
        selected_source_count: body.execution_scope_kind === 'SELECTED_MEDIA' ? scanSelectedKeys.size : 0,
        source_config: body.source_config,
        file_filter: body.file_filter,
        site_ids: body.site_ids,
        output_config: body.output_config,
      }, 201);
    }
    return fulfillJson(route, { items: [manualDefinition, monitorDefinition, failedDefinition] });
  });
  await page.route('**/api/v1/unpack/executions?**', (route) =>
    fulfillJson(route, { items: [execution, failedExecution] }),
  );
  await page.route('**/api/v1/unpack/executions/execution-review', (route) =>
    fulfillJson(route, execution),
  );

  const sourceBase = {
    execution_id: 'execution-review',
    source_snapshot: { path: '/downloads/anime/Show.S01E01.mkv', relative_path: 'Show.S01E01.mkv' },
    media_identity: {
      raw_name: 'Show.S01E01.1080p.WEB-DL',
      title_tokens: ['show'],
      year: 2026,
      resolution: '1080p',
      release_source: 'WEB-DL',
      codec: 'H.265',
      release_group: 'Group',
      total_size: 1073741824,
      external_ids: [{ namespace: 'imdb', value: 'tt1234567' }],
    },
    candidate_generation: 1,
    retry_count: 0,
    content_verification_level: null,
    torrent_metainfo_digest: null,
    auxiliary_state: null,
    review_allowed: false,
    last_error_code: null,
    last_error_message: null,
    match_started_at: now,
    match_finished_at: now,
    version: 3,
    created_at: now,
    updated_at: now,
  };
  const executionItems = [
    {
      ...sourceBase,
      id: 'item-auto',
      source_object_key: 'source-auto',
      status: 'MATCHED_AUTO',
      selected_candidate_id: 'candidate-auto',
      match_origin: 'AUTO',
      review_allowed: true,
    },
    {
      ...sourceBase,
      id: 'item-review',
      source_object_key: 'source-review',
      status: 'REVIEW_REQUIRED',
      selected_candidate_id: null,
      match_origin: null,
      review_allowed: true,
      media_identity: { ...sourceBase.media_identity, raw_name: 'Project.Hail.Mary.2026.1080p.BluRay', external_ids: [{ namespace: 'douban', value: '35428968' }] },
    },
    {
      ...sourceBase,
      id: 'item-error',
      source_object_key: 'source-error',
      status: 'MATCH_ERROR',
      selected_candidate_id: 'candidate-auto',
      match_origin: null,
      last_error_code: 'DOWNLOADER_UNAVAILABLE',
      last_error_message: '辅助文件已下载，查询 Transmission 暂时失败',
      auxiliary_state: { state: 'DOWNLOADING', missing_paths: ['Release/Movie.nfo'] },
    },
  ];
  await page.route('**/api/v1/unpack/executions/execution-review/items**', (route) => {
    const url = new URL(route.request().url());
    const limit = Number(url.searchParams.get('limit'));
    observedPageLimits.push(limit);
    observedExecutionFilters.push({
      status: url.searchParams.get('item_status'),
      q: url.searchParams.get('q'),
    });
    if (!largePageMode) {
      return fulfillJson(route, { items: executionItems, next_cursor: null, has_more: false });
    }
    const offset = Number(url.searchParams.get('cursor') || 0);
    const sample = Array.from({ length: 61 }, (_, i) => ({
      ...sourceBase,
      id: 'page-item-' + i,
      source_object_key: 'page-object-' + i,
      status: 'NO_MATCH',
      selected_candidate_id: null,
      match_origin: null,
      review_allowed: false,
      media_identity: { ...sourceBase.media_identity, raw_name: '分页影片-' + i },
    }));
    const next = offset + limit < sample.length ? String(offset + limit) : null;
    return fulfillJson(route, { items: sample.slice(offset, offset + limit), next_cursor: next, has_more: Boolean(next) });
  });

  const candidateList = (itemId) => ({
    item_id: itemId,
    generation: 1,
    item_version: 3,
    default_candidate_id: 'candidate-auto',
    candidates: [
      {
        id: 'candidate-auto',
        item_id: itemId,
        generation: 1,
        site_id: 'site-mteam',
        candidate_key: 'torrent-1',
        title: 'Show.S01E01.1080p.WEB-DL.H265-Group',
        size_bytes: 1073741824,
        imdb_id: 'tt1234567',
        douban_id: null,
        seeders: 48,
        score_bps: 10000,
        is_exact_match: true,
        evidence: { hard_conflicts: [], exact: { title: true, external_id: true, year: true, episode: true, resolution: true, release_source: true, codec: true, release_group: true, size: true } },
        verification_status: 'NOT_CHECKED',
        verification_level: null,
        verification_error_code: null,
        created_at: now,
      },
      {
        id: 'candidate-2',
        item_id: itemId,
        generation: 1,
        site_id: 'site-hdtime',
        candidate_key: 'torrent-2',
        title: 'Show.S01E01.2160p.WEB-DL.H265',
        size_bytes: 2147483648,
        imdb_id: 'tt1234567',
        douban_id: null,
        seeders: 112,
        score_bps: 8240,
        is_exact_match: false,
        evidence: { hard_conflicts: [], exact: { title: true, external_id: true, year: true, episode: true, resolution: false, release_source: true, codec: true, release_group: false, size: false } },
        verification_status: 'NOT_CHECKED',
        verification_level: null,
        verification_error_code: null,
        created_at: now,
      },
    ],
  });
  await page.route('**/api/v1/unpack/items/**', (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === '/api/v1/unpack/items/item-error/actions' && route.request().method() === 'POST') {
      assert.deepEqual(route.request().postDataJSON(), { action: 'retry_match' });
      assert.equal(route.request().headers()['if-match'], '3');
      return fulfillJson(route, {
        item_id: 'item-error',
        generation: 1,
        retry_count: 0,
        item_version: 4,
        item_status: 'AUXILIARY_FETCHING',
      });
    }
    const match = pathname.match(/\/unpack\/items\/([^/]+)\/candidates\/?$/);
    if (!match) {
      return fulfillJson(
        route,
        { code: 'E2E_UNMOCKED_ITEM_API', detail: 'v1.0.15 item API 未显式 mock' },
        501,
      );
    }
    return fulfillJson(route, candidateList(decodeURIComponent(match[1])));
  });

  await page.route('**/api/v1/unpack/source-scans**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const pathname = url.pathname;
    if (request.method() === 'POST' && pathname.endsWith('/unpack/source-scans')) {
      return fulfillJson(
        route,
        {
          id: 'scan-1',
          directory_path: '/downloads',
          discovered_count: 2,
          selected_count: scanSelectedKeys.size,
          expires_at: '2099-01-01T00:00:00Z',
          version: 1,
        },
        201,
      );
    }
    if (request.method() === 'GET' && pathname.endsWith('/source-scans/scan-1/items')) {
      const items = [
        {
          source_object_key: 'movie-a',
          relative_path: 'Movie.A.2026.2160p.mkv',
          filename: 'Movie.A.2026.2160p.mkv',
          extension: '.mkv',
          resolution: '2160p',
          size_bytes: 2147483648,
          selected: scanSelectedKeys.has('movie-a'),
        },
        {
          source_object_key: 'movie-b',
          relative_path: 'archive/Movie.B.2026.1080p.mkv',
          filename: 'Movie.B.2026.1080p.mkv',
          extension: '.mkv',
          resolution: '1080p',
          size_bytes: 1073741824,
          selected: scanSelectedKeys.has('movie-b'),
        },
      ];
      return fulfillJson(route, { items, next_cursor: null, has_more: false });
    }
    if (
      request.method() === 'GET' &&
      pathname.endsWith('/source-scans/scan-1/selection-summary')
    ) {
      return fulfillJson(route, {
        discovered_count: 2,
        selected_count: scanSelectedKeys.size,
      });
    }
    if (
      request.method() === 'PUT' &&
      pathname.endsWith('/source-scans/scan-1/selection')
    ) {
      const body = request.postDataJSON();
      for (const key of body.source_object_keys) {
        if (body.selected) scanSelectedKeys.add(key);
        else scanSelectedKeys.delete(key);
      }
      return fulfillJson(route, {
        discovered_count: 2,
        selected_count: scanSelectedKeys.size,
      });
    }
    return fulfillJson(
      route,
      { code: 'E2E_UNMOCKED_SCAN_API', detail: 'source scan API 未显式 mock' },
      501,
    );
  });

  await page.route('**/api/v1/files/tree/roots', (route) =>
    fulfillJson(route, {
      items: [
        { name: 'downloads', display_path: '/downloads', selection_token: 'root-downloads' },
        { name: 'downloads2', display_path: '/downloads2', selection_token: 'root-downloads2' },
      ],
    }),
  );
  await page.route('**/api/v1/files/tree?**', (route) => {
    const token = new URL(route.request().url()).searchParams.get('selection_token');
    if (token === 'root-downloads') {
      return fulfillJson(route, {
        display_path: '/downloads',
        selection_token: token,
        entries: [
          { name: 'movies', display_path: '/downloads/movies', selection_token: 'movies-token' },
          { name: 'seeding', display_path: '/downloads/seeding', selection_token: 'seeding-token' },
        ],
      });
    }
    return fulfillJson(route, { display_path: '/downloads2', selection_token: token, entries: [] });
  });

  try {
    await page.goto(`${frontendUrl}/#%E4%BB%BB%E5%8A%A1%E4%B8%AD%E5%BF%83`, { waitUntil: 'domcontentloaded' });
    await page.getByRole('heading', { name: '任务中心', exact: true, level: 2 }).waitFor();
    assert.equal(await page.locator('aside.sidebar nav').getByText('关于', { exact: true }).count(), 0);
    const aboutCorner = page.getByRole('button', { name: '关于 PackBreaker' });
    await aboutCorner.waitFor({ state: 'visible' });
    const cornerRect = await aboutCorner.boundingBox();
    assert.ok(cornerRect, '关于入口必须在可访问区域');
    assert.ok(cornerRect.x + cornerRect.width >= 1440 - 44, '关于入口应位于页面右侧');
    assert.ok(cornerRect.y + cornerRect.height >= 980 - 44, '关于入口应位于页面底部');
    await page.setViewportSize({ width: 390, height: 844 });
    const mobileCorner = await aboutCorner.boundingBox();
    assert.ok(mobileCorner, '移动端必须显示关于入口');
    assert.ok(mobileCorner.x >= 0 && mobileCorner.x + mobileCorner.width <= 390, '移动端关于入口超出屏幕');
    assert.ok(mobileCorner.y >= 0 && mobileCorner.y + mobileCorner.height <= 844, '移动端关于入口超出屏幕');
    await aboutCorner.click();
    await page.waitForURL(/#(?:%E5%85%B3%E4%BA%8E|关于)/i);
    await page.goto(`${frontendUrl}/#%E4%BB%BB%E5%8A%A1%E4%B8%AD%E5%BF%83`, { waitUntil: 'domcontentloaded' });
    await page.getByRole('heading', { name: '任务中心', exact: true, level: 2 }).waitFor();
    await page.setViewportSize({ width: 1440, height: 980 });

    const modeCards = page.locator('.mode-card');
    assert.equal(await modeCards.count(), 2, 'v1.0.15 顶部必须只保留两个任务卡片');
    assert.equal(await modeCards.filter({ hasText: '数据拆包' }).count(), 1);
    assert.equal(await modeCards.filter({ hasText: '数据去重' }).count(), 1);
    assert.equal(await modeCards.filter({ hasText: '数据拆包' }).evaluate((el) => el.classList.contains('active')), true);
    assert.equal(
      await page.getByRole('button', { name: '清理历史 payload', exact: true }).count(),
      0,
      'v2 任务中心不得暴露旧 retention/purge 入口',
    );

    await page.getByText('电影库手动拆包', { exact: true }).waitFor();
    await page.getByText('新番持续监控', { exact: true }).waitFor();
    const manualRow = page.locator('.unpack-table .el-table__row').filter({ hasText: '电影库手动拆包' }).first();
    const monitorRow = page.locator('.unpack-table .el-table__row').filter({ hasText: '新番持续监控' }).first();
    const failedRow = page.locator('.unpack-table .el-table__row').filter({ hasText: '匹配失败重试测试' }).first();
    assert.equal(await manualRow.getByText('待执行', { exact: true }).count(), 1);
    assert.equal(await monitorRow.getByText('待人工审核', { exact: true }).count(), 1);
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 980 });
      const actions = failedRow.locator('.task-row-actions');
      const positions = await actions.locator('button').evaluateAll((buttons) =>
        buttons.map((button) => {
          const bounds = button.getBoundingClientRect();
          return { left: bounds.left, right: bounds.right, width: bounds.width };
        }),
      );
      assert.equal(positions.length, 3, '失败任务应仅展示查看、重新匹配、更多操作');
      for (let index = 1; index < positions.length; index++) {
        assert.ok(positions[index].left >= positions[index - 1].right - 1, width + 'px 操作按钮发生重叠');
      }
      assert.ok(positions.every((bounds) => bounds.width >= 28), width + 'px 操作按钮触发区域过窄');
      await failedRow.getByRole('button', { name: '更多操作' }).click();
      await page.getByRole('menuitem', { name: '编辑', exact: true }).waitFor();
      await page.getByRole('menuitem', { name: '删除', exact: true }).waitFor();
      await page.keyboard.press('Escape');
    }
    await page.setViewportSize({ width: 1440, height: 980 });

    await page.getByRole('button', { name: '新增任务', exact: true }).click();
    const createDialog = page.locator('.el-dialog').filter({ hasText: '新增数据拆包任务' }).last();
    await createDialog.waitFor({ state: 'visible' });
    for (const section of ['1. 基本信息', '2. 来源', '3. 文件过滤', '4. 输出与文件冲突', '5. 高级执行规则']) {
      assert.equal(await createDialog.getByText(section, { exact: true }).count(), 1, '缺少创建分区：' + section);
    }
    assert.equal(await createDialog.getByRole('button', { name: '保存', exact: true }).count(), 1);
    assert.equal(await createDialog.getByRole('button', { name: '保存并执行', exact: true }).count(), 0);
    await createDialog.getByRole('button', { name: '监控拆包', exact: true }).click();
    await createDialog.getByRole('button', { name: '每 5 分钟', exact: true }).click();
    const scheduleField = createDialog.locator('.el-form-item').filter({ hasText: '执行时间' }).first();
    assert.equal(await scheduleField.locator('input').inputValue(), '*/5 * * * *');
    await createDialog.getByRole('button', { name: '下载器', exact: true }).click();
    assert.equal(await createDialog.getByText('来源下载器', { exact: true }).count(), 1);
    assert.equal(await createDialog.getByText('自动匹配阈值（%）', { exact: true }).count(), 1);
    await createDialog.locator('.el-dialog__headerbtn').click();

    await monitorRow.getByRole('button', { name: '查看', exact: true }).click();
    const executionDialog = page.locator('.el-dialog').filter({ hasText: '执行详情 · 新番持续监控' }).last();
    await executionDialog.waitFor({ state: 'visible' });
    for (const step of ['分页获取影片', '分批影片匹配', '自动匹配 / 人工审核', '种子内容校验', '辅种执行', '客户端校验 / 完成']) {
      assert.equal(await executionDialog.getByText(new RegExp(step)).count() >= 1, true, '缺少执行阶段：' + step);
    }
    assert.equal(await executionDialog.getByText('自动匹配成功', { exact: true }).count(), 1);
    assert.equal(await executionDialog.getByText('匹配错误', { exact: true }).count(), 1);
    assert.equal(await executionDialog.getByRole('button', { name: '审核', exact: true }).count(), 2, '自动匹配成功与待人工审核都必须提供审核按钮');
    assert.equal(await executionDialog.getByRole('button', { name: '继续补齐', exact: true }).count(), 1);
    assert.equal(await executionDialog.getByRole('button', { name: '删除', exact: true }).count(), 3);
    const deletedDisabled = await executionDialog
      .getByRole('button', { name: '删除', exact: true })
      .evaluateAll((buttons) => buttons.map((button) => button.disabled));
    assert.deepEqual(deletedDisabled, [true, true, true], '待审核的执行中禁止删除影片记录');
    await executionDialog.getByRole('button', { name: '继续补齐', exact: true }).click();
    await page.getByText('已恢复原有辅助文件补齐，将校验已下载文件，不会重复添加种子').waitFor();

    await executionDialog.getByRole('button', { name: '审核', exact: true }).first().click();
    const reviewDialog = page.locator('.el-dialog').filter({ hasText: '人工审核候选' }).last();
    await reviewDialog.waitFor({ state: 'visible' });
    await reviewDialog.getByText('默认选中匹配度最高的一条，但必须点击确认后才会继续。', { exact: true }).waitFor();
    const candidateCards = reviewDialog.locator('.candidate-card');
    await candidateCards.first().waitFor({ state: 'visible' });
    assert.equal(await candidateCards.count(), 2, '审核弹窗必须展示当前候选代次');
    assert.match((await candidateCards.first().textContent()) || '', /100\.0%/);
    assert.equal(await reviewDialog.getByRole('button', { name: /确认所选候选并继续/ }).count(), 1);
    const selectedRadio = reviewDialog.locator('.candidate-card.selected input[type="radio"]');
    assert.equal(await selectedRadio.isChecked(), true, '最高候选应该只作为 UI 默认预选');

    await reviewDialog.locator('.el-dialog__headerbtn').click();
    await executionDialog.locator('.el-dialog__headerbtn').click();

    // Read-only cursor pagination must round-trip to the backend with the
    // selected page size; changing it resets the cursor to page 1.
    largePageMode = true;
    await monitorRow.getByRole('button', { name: '查看', exact: true }).click();
    await executionDialog.getByText('分页影片-0', { exact: true }).waitFor();
    assert.equal(await executionDialog.locator('.el-table__body .el-table__row').count(), 20);
    assert.equal(
      await executionDialog.getByRole('button', { name: '重新匹配', exact: true }).count(),
      20,
      '每个无匹配影片都应提供受任务重试策略约束的重试入口',
    );
    await executionDialog.getByRole('button', { name: '下一页' }).click();
    await executionDialog.getByText('分页影片-20', { exact: true }).waitFor();
    assert.equal(await executionDialog.getByText('第 2 页', { exact: false }).count(), 1);
    await executionDialog.getByRole('button', { name: '上一页' }).click();
    await executionDialog.getByText('分页影片-0', { exact: true }).waitFor();
    await executionDialog.locator('.pager .el-select').click();
    await page.getByRole('option', { name: '50', exact: true }).click();
    await executionDialog.getByText('分页影片-49', { exact: true }).waitFor();
    assert.equal(await executionDialog.locator('.el-table__body .el-table__row').count(), 50);
    await executionDialog.locator('.pager .el-select').click();
    await page.getByRole('option', { name: '100', exact: true }).click();
    await executionDialog.getByText('分页影片-60', { exact: true }).waitFor();
    assert.equal(await executionDialog.locator('.el-table__body .el-table__row').count(), 61);
    assert.deepEqual(observedPageLimits.slice(-4), [20, 20, 50, 100]);
    await executionDialog.locator('.execution-item-filters .el-select').click();
    await page.getByRole('option', { name: '无匹配', exact: true }).click();
    assert.equal(observedExecutionFilters.at(-1)?.status, 'NO_MATCH');
    await executionDialog.getByPlaceholder('搜索影片名或来源路径').fill('九品芝麻官');
    await executionDialog.getByPlaceholder('搜索影片名或来源路径').press('Enter');
    assert.equal(observedExecutionFilters.at(-1)?.q, '九品芝麻官');
    await executionDialog.locator('.execution-item-filters').getByRole('button', { name: '刷新' }).click();
    assert.equal(observedExecutionFilters.at(-1)?.status, 'NO_MATCH');
    assert.equal(observedExecutionFilters.at(-1)?.q, '九品芝麻官');
    await executionDialog.locator('.el-dialog__headerbtn').click();

    assert.match(await failedRow.textContent(), /无匹配 1/);
    assert.match(await failedRow.textContent(), /异常 1/);
    const bulkRetryResponse = page.waitForResponse(
      (response) =>
        response.url().includes('/api/v1/unpack/executions/execution-failed/actions') &&
        response.request().method() === 'POST' &&
        response.status() === 200,
    );
    await failedRow.getByRole('button', { name: '重新匹配', exact: true }).click();
    const completedBulkRetry = await bulkRetryResponse;
    // The request event fires before the async mock route handler runs. Wait
    // for its successful response before examining captured request data.
    assert.deepEqual(completedBulkRetry.request().postDataJSON(), {
      action: 'retry_failed_matches',
    });
    assert.deepEqual(bulkRetryBody, { action: 'retry_failed_matches' });
    await manualRow.getByRole('button', { name: '更多操作' }).click();
    await page.getByRole('menuitem', { name: '编辑', exact: true }).click();
    const editDialog = page.locator('.el-dialog').filter({ hasText: '编辑数据拆包任务' }).last();
    await editDialog.waitFor({ state: 'visible' });
    await editDialog.locator('.el-form-item').filter({ hasText: '任务名称' }).locator('input').fill('人工编辑 E2E');
    await editDialog.getByRole('button', { name: '保存', exact: true }).click();
    assert.equal(updatedDefinitionBody?.name, '人工编辑 E2E');
    await monitorRow.getByRole('button', { name: '更多操作' }).click();
    await page.getByRole('menuitem', { name: '编辑', exact: true }).click();
    await editDialog.waitFor({ state: 'visible' });
    await editDialog.getByRole('button', { name: '保存', exact: true }).click();
    assert.equal(
      updatedMonitorBody?.timezone,
      'Asia/Shanghai',
      '编辑监控任务不得无意重置原时区',
    );
    await manualRow.getByRole('button', { name: '更多操作' }).click();
    await page.getByRole('menuitem', { name: '删除', exact: true }).click();
    const deleteResponse = page.waitForResponse(
      (response) =>
        response.url().includes('/api/v1/unpack/definitions/definition-manual') &&
        response.request().method() === 'DELETE' &&
        response.status() === 204,
    );
    await page.getByRole('button', { name: '确认删除', exact: true }).click();
    await deleteResponse;
    assert.equal(deletedDefinitionVersion, '1');

    await page.getByRole('button', { name: '新增任务', exact: true }).click();
    const manualDialog = page.locator('.el-dialog').filter({ hasText: '新增数据拆包任务' }).last();
    await manualDialog.waitFor({ state: 'visible' });
    await manualDialog.locator('.el-form-item').filter({ hasText: '任务名称' }).locator('input').fill('指定影片拆包 E2E');

    const siteField = manualDialog.locator('.el-form-item').filter({ hasText: '扫描站点' }).first();
    await siteField.locator('.el-select').click();
    await page.getByRole('option', { name: 'M-TEAM', exact: true }).click();
    await page.keyboard.press('Escape');

    const sourceField = manualDialog.locator('.el-form-item').filter({ hasText: '来源目录' }).first();
    await sourceField.getByRole('button', { name: '选择目录', exact: true }).click();
    let treeDialog = page.locator('.el-dialog').filter({ hasText: '选择目录' }).last();
    await treeDialog.waitFor({ state: 'visible' });
    await treeDialog.getByText('downloads', { exact: true }).click();
    await treeDialog.getByRole('button', { name: '使用此目录', exact: true }).click();

    const outputField = manualDialog.locator('.el-form-item').filter({ hasText: '输出目录' }).first();
    await outputField.getByRole('button', { name: '选择目录', exact: true }).click();
    treeDialog = page.locator('.el-dialog').filter({ hasText: '选择目录' }).last();
    await treeDialog.waitFor({ state: 'visible' });
    await treeDialog.getByText('downloads2', { exact: true }).click();
    await treeDialog.getByRole('button', { name: '使用此目录', exact: true }).click();

    const targetDownloaderField = manualDialog
      .locator('.el-form-item')
      .filter({ hasText: '目标下载器' })
      .first();
    await targetDownloaderField.locator('.el-select').click();
    await page.getByRole('option', { name: 'qBittorrent · 主下载器', exact: true }).click();
    await manualDialog.getByRole('button', { name: '保存', exact: true }).click();

    const scopeDialog = page.locator('.el-dialog').filter({ hasText: '确认拆包范围' }).last();
    await scopeDialog.waitFor({ state: 'visible' });
    await scopeDialog.getByRole('button', { name: '否，选择影片', exact: true }).click();
    const mediaDialog = page.locator('.el-dialog').filter({ hasText: '选择需要拆包的影视文件' }).last();
    await mediaDialog.waitFor({ state: 'visible' });
    await mediaDialog.locator('b').filter({ hasText: 'Movie.A.2026.2160p.mkv' }).waitFor();
    await mediaDialog.getByRole('button', { name: '勾选当前结果', exact: true }).click();
    await mediaDialog.getByText('已选择 2 / 2', { exact: true }).waitFor();
    await mediaDialog.getByRole('button', { name: '保存任务', exact: true }).click();
    assert.equal(createdDefinitionBodies.length, 1);
    assert.equal(createdDefinitionBodies[0].execution_scope_kind, 'SELECTED_MEDIA');
    assert.equal(createdDefinitionBodies[0].source_scan_id, 'scan-1');
    assert.equal(createdDefinitionBodies[0].source_kind, 'DIRECTORY');

    await modeCards.filter({ hasText: '数据去重' }).click();
    await page.getByRole('button', { name: '新增任务', exact: true }).click();
    const dedupDialog = page.locator('.el-dialog').filter({ hasText: '新增影片去重任务' }).last();
    await dedupDialog.waitFor({ state: 'visible' });
    await dedupDialog.locator('.el-dialog__headerbtn').click();
    await modeCards.filter({ hasText: '数据拆包' }).click();

    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(100);
    assert.equal(
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1),
      true,
      'v1.0.15 任务中心 390px 不应产生页面级横向溢出',
    );

    assert.deepEqual(unmocked, [], 'v1.0.15 UI 烟测不得请求未显式 mock 的 API');
    assert.deepEqual(errors, []);
    console.log(`通过：v1.0.15 ${productionPreview ? '生产构建' : '开发构建'}两卡任务中心、Cron 辅助、下载器来源、保存语义、自动匹配审核、重试与 390px 响应式烟测。`);
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  for (const child of children) console.error(child.readOutput().slice(-2200));
  process.exitCode = 1;
}).finally(async () => {
  for (const { child } of children.reverse()) {
    if (child.exitCode === null && child.signalCode === null) {
      const exited = new Promise((resolve) => child.once('exit', resolve));
      child.kill('SIGTERM');
      await Promise.race([exited, sleep(5000)]);
      if (child.exitCode === null && child.signalCode === null) {
        child.kill('SIGKILL');
        await exited;
      }
    }
  }
});

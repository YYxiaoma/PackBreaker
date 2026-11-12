// v1.1.4 数据拆包 v2 专用浏览器烟测；只使用显式 mock，不访问真实后端。
const { chromium } = require(
  process.env.PB_PLAYWRIGHT || "../frontend/node_modules/playwright",
);
const assert = require("node:assert/strict");
const path = require("node:path");
const { spawn } = require("node:child_process");

const root = path.resolve(__dirname, "..");
const frontendUrl = "http://127.0.0.1:5175";
const productionPreview =
  process.env.PB_UNPACK_V2_E2E_PRODUCTION === "1" ||
  process.argv.includes("--production");
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
  throw new Error(`v1.1.4 UI 测试服务未就绪：${url}`);
}

function startFrontend() {
  const child = spawn(
    "corepack",
    [
      "pnpm",
      "exec",
      "vite",
      ...(productionPreview ? ["preview"] : []),
      "--host",
      "127.0.0.1",
      "--port",
      "5175",
      "--strictPort",
    ],
    {
      cwd: path.join(root, "frontend"),
      stdio: ["ignore", "pipe", "pipe"],
      env: { ...process.env },
    },
  );
  let output = "";
  for (const stream of [child.stdout, child.stderr]) {
    stream.on("data", (chunk) => {
      output = (output + chunk.toString()).slice(-8000);
    });
  }
  children.push({ child, readOutput: () => output });
}

const fulfillJson = (route, body, status = 200) =>
  route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });

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
      require("node:fs").existsSync(
        path.join(root, "frontend/dist/index.html"),
      ),
      true,
      "生产构建产物不存在，拒绝静默回退到开发服务器",
    );
  }
  startFrontend();
  await waitReady(frontendUrl);

  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 980 },
  });
  const errors = [];
  const unmocked = [];
  const now = "2026-10-07T12:00:00Z";

  page.on("pageerror", (error) => errors.push(error.message));

  await page.route("**/api/v1/**", (route) => {
    const request = route.request();
    unmocked.push(request.method() + " " + new URL(request.url()).pathname);
    return fulfillJson(
      route,
      { code: "E2E_UNMOCKED_API", detail: "v1.1.4 UI 烟测禁止未显式 mock API" },
      501,
    );
  });

  await page.route("**/api/v1/auth/me", (route) =>
    fulfillJson(route, {
      configured: true,
      authenticated: true,
      permissions: ["admin"],
      expires_at: "2099-01-01T00:00:00Z",
    }),
  );
  await page.route("**/api/v1/notifications/inbox/unread-count", (route) =>
    fulfillJson(route, { count: 0 }),
  );
  await page.route("**/api/v1/system/health", (route) =>
    fulfillJson(route, {
      status: "ok",
      generated_at: now,
      version: "1.1.4",
      checks: [],
    }),
  );
  await page.route("**/api/v1/system/upgrade", (route) =>
    fulfillJson(route, {
      current_version: "1.1.4",
      latest_version: "1.1.4",
      update_available: false,
      can_upgrade: false,
      blocked_reasons: [],
      release_error_code: null,
      target_image_digest: null,
      target_tag: "v1.1.4",
      helper_status: null,
    }),
  );
  const aiSettings = {
    enabled: false,
    provider_kind: "OPENAI_COMPATIBLE",
    base_url: "https://provider.example/v1",
    api_key_configured: true,
    model: "synthetic-model",
    request_timeout_seconds: 30,
    max_context_messages: 20,
    data_scopes: ["SYSTEM_HEALTH"],
    connection_status: "UNTESTED",
    last_test_at: null,
    version: 1,
    created_at: now,
    updated_at: now,
  };
  const aiStatus = {
    enabled: false,
    provider_kind: "OPENAI_COMPATIBLE",
    connection_status: "UNTESTED",
    api_key_configured: true,
    model: "synthetic-model",
    last_test_at: null,
    telegram_enabled: false,
    telegram_approval_enabled: false,
    telegram_driver_running: false,
    telegram_last_update_id: 0,
    telegram_consecutive_errors: 0,
    telegram_last_error_code: null,
  };
  const telegramBinding = {
    notification_channel_id: null,
    enabled: false,
    approval_enabled: false,
    allowed_chat_ids: [],
    allowed_user_ids: [],
    idle_timeout_minutes: 60,
    max_context_messages: 20,
    last_update_id: 0,
    version: 1,
    created_at: now,
    updated_at: now,
  };
  await page.route("**/api/v1/ai-agent/settings", (route) =>
    fulfillJson(route, aiSettings),
  );
  await page.route("**/api/v1/ai-agent/status", (route) =>
    fulfillJson(route, aiStatus),
  );
  await page.route("**/api/v1/ai-agent/telegram", (route) =>
    fulfillJson(route, telegramBinding),
  );
  await page.route("**/api/v1/notification-channels", (route) =>
    fulfillJson(route, {
      items: [
        {
          id: "telegram-channel",
          name: "Telegram Bot",
          type: "TELEGRAM",
          credential_configured: true,
          event_types: [],
          proxy_enabled: false,
          proxy_host: null,
          proxy_port: null,
          proxy_username: null,
          proxy_credential_configured: false,
          connection_status: "UNTESTED",
          enabled: true,
          version: 1,
          last_test_at: null,
          created_at: now,
          updated_at: now,
        },
      ],
    }),
  );
  await page.route("**/api/v1/system/backups/policy", (route) =>
    fulfillJson(route, {
      enabled: false,
      interval_hours: 24,
      retention_days: 30,
      keep_latest: 3,
      version: 1,
      last_attempt_at: null,
      last_success_at: null,
      last_error_code: null,
      driver_running: true,
      driver_consecutive_errors: 0,
    }),
  );
  await page.route("**/api/v1/system/logs*", (route) =>
    fulfillJson(route, {
      window_minutes: 60,
      limit: 100,
      count: 0,
      truncated: false,
      max_file_bytes: 1048576,
      backup_count: 0,
      approximate_capacity_bytes: 0,
      items: [],
    }),
  );

  const sites = [
    { id: "site-mteam", name: "M-TEAM", enabled: true },
    { id: "site-hdtime", name: "HDTime", enabled: true },
  ];
  const downloaders = [
    { id: "qb-main", name: "qBittorrent · 主下载器", enabled: true },
    { id: "tr-main", name: "Transmission · 备用下载器", enabled: true },
  ];
  await page.route("**/api/v1/sites", (route) =>
    fulfillJson(route, { items: sites }),
  );
  await page.route("**/api/v1/downloaders", (route) =>
    fulfillJson(route, { items: downloaders }),
  );
  const templates = [
    {
      id: "template-mteam",
      name: "Synthetic M-TEAM template",
      description: null,
      current_version: 1,
      status: "READY",
      expected_seed_count: 2,
      seed_count: 2,
      mapped_media_count: 2,
      site_keys: ["mteam"],
      site_ids: ["site-mteam"],
      content_digest: "a".repeat(64),
      source_provenance: {},
      created_at: now,
      updated_at: now,
    },
    {
      id: "template-cross-site",
      name: "跨站模板不应显示",
      description: null,
      current_version: 1,
      status: "READY",
      expected_seed_count: 2,
      seed_count: 2,
      mapped_media_count: 2,
      site_keys: ["mteam", "hdtime"],
      site_ids: ["site-mteam", "site-hdtime"],
      content_digest: "b".repeat(64),
      source_provenance: {},
      created_at: now,
      updated_at: now,
    },
  ];
  await page.route("**/api/v1/unpack/templates", (route) =>
    fulfillJson(route, { items: templates }),
  );
  const createdDefinitionBodies = [];
  const scanSelectedKeys = new Set();
  let largePageMode = false;
  let executionDetailGetCount = 0;
  let executionItemsDelayMs = 0;
  let executionItemsResponseResolved = false;
  const observedPageLimits = [];
  const observedExecutionFilters = [];
  let updatedDefinitionBody = null;
  let updatedMonitorBody = null;
  let deletedDefinitionVersion = null;
  let bulkRetryBody = null;
  const movieDedupJobs = [
    {
      id: "dedup-main",
      name: "电影库 4K 去重",
      source_root: "/downloads/movies-main",
      target_root: "/downloads2/movies-copy",
      mode: "AUTO",
      cross_filesystem_policy: "STOP",
      include_subdirectories: true,
      min_size_bytes: 0,
      video_extensions: [".mkv"],
      status: "COMPLETED",
      phase: "COMPLETED",
      source_scan_cursor: null,
      target_scan_cursor: null,
      source_file_count: 10,
      target_file_count: 8,
      candidate_count: 3,
      verified_count: 3,
      deduplicated_count: 2,
      failed_count: 0,
      logical_duplicate_bytes: 4294967296,
      estimated_reclaimable_bytes: 4294967296,
      version: 1,
      error_code: null,
      created_at: now,
      updated_at: now,
      finished_at: now,
    },
    {
      id: "dedup-anime",
      name: "动漫库重复扫描",
      source_root: "/downloads/anime",
      target_root: "/downloads2/anime",
      mode: "SCAN_ONLY",
      cross_filesystem_policy: "STOP",
      include_subdirectories: true,
      min_size_bytes: 0,
      video_extensions: [".mkv", ".mp4"],
      status: "FAILED",
      phase: "FAILED",
      source_scan_cursor: null,
      target_scan_cursor: null,
      source_file_count: 12,
      target_file_count: 12,
      candidate_count: 0,
      verified_count: 0,
      deduplicated_count: 0,
      failed_count: 1,
      logical_duplicate_bytes: 0,
      estimated_reclaimable_bytes: 0,
      version: 1,
      error_code: "SAMPLE_FAILURE",
      created_at: now,
      updated_at: now,
      finished_at: now,
    },
  ];
  await page.route("**/api/v1/movie-dedup/jobs", (route) =>
    fulfillJson(route, { items: movieDedupJobs }),
  );

  const manualDefinition = {
    id: "definition-manual",
    name: "电影库手动拆包",
    trigger_kind: "MANUAL",
    status: "PENDING_EXECUTION",
    source_kind: "DIRECTORY",
    execution_scope_kind: "ALL_MATCHING_MEDIA",
    source_config: { directory_path: "/downloads/movies" },
    file_filter: {
      extensions: [".mkv"],
      min_size_bytes: null,
      max_size_bytes: null,
      include_name: null,
      exclude_names: [],
      include_subdirectories: true,
    },
    site_ids: ["site-mteam"],
    output_config: {
      output_directory: "/downloads/seeding",
      storage_mode: "HARDLINK",
      conflict_policy: "VERIFY_REUSE_OR_STOP",
      target_downloader_id: "qb-main",
    },
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
    id: "definition-monitor",
    name: "新番持续监控",
    trigger_kind: "MONITOR",
    status: "ENABLED",
    source_kind: "DOWNLOADER",
    source_config: {
      downloader_id: "qb-main",
      name_contains: null,
      categories: ["anime"],
      tags: ["ready"],
    },
    site_ids: ["site-mteam", "site-hdtime"],
    cron_expression: "*/10 * * * *",
    timezone: "Asia/Shanghai",
  };
  const failedDefinition = {
    ...manualDefinition,
    id: "definition-failed",
    name: "匹配失败重试测试",
  };
  const failedExecution = {
    id: "execution-failed",
    definition_id: "definition-failed",
    trigger: "MANUAL",
    status: "FAILED",
    total_count: 1,
    match_pending_count: 0,
    matching_count: 0,
    match_progress_count: 1,
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
  await page.route(
    "**/api/v1/unpack/definitions/definition-manual",
    (route) => {
      const req = route.request();
      if (req.method() === "PUT") {
        updatedDefinitionBody = req.postDataJSON();
        assert.equal(req.headers()["if-match"], "1");
        return fulfillJson(route, {
          ...manualDefinition,
          ...updatedDefinitionBody,
          version: 2,
        });
      }
      if (req.method() === "DELETE") {
        deletedDefinitionVersion = req.headers()["if-match"];
        return route.fulfill({ status: 204, body: "" });
      }
      return fulfillJson(
        route,
        { code: "UNMOCKED_DEFINITION", detail: "未授权操作" },
        501,
      );
    },
  );
  await page.route(
    "**/api/v1/unpack/definitions/definition-monitor",
    (route) => {
      const req = route.request();
      assert.equal(req.method(), "PUT");
      assert.equal(req.headers()["if-match"], "1");
      updatedMonitorBody = req.postDataJSON();
      return fulfillJson(route, {
        ...monitorDefinition,
        ...updatedMonitorBody,
        version: 2,
      });
    },
  );
  await page.route(
    "**/api/v1/unpack/executions/execution-failed/actions",
    (route) => {
      bulkRetryBody = route.request().postDataJSON();
      assert.equal(route.request().headers()["if-match"], "5");
      return fulfillJson(route, {
        execution_id: "execution-failed",
        retried_count: 1,
        skipped_unsafe_count: 0,
      });
    },
  );
  const execution = {
    id: "execution-review",
    definition_id: "definition-monitor",
    trigger: "MONITOR",
    status: "REVIEW_REQUIRED",
    discovery_complete: true,
    discovery_cursor: null,
    total_count: 3,
    match_pending_count: 0,
    matching_count: 0,
    match_progress_count: 3,
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
  await page.route("**/api/v1/unpack/definitions", async (route) => {
    if (route.request().method() === "POST") {
      const body = route.request().postDataJSON();
      createdDefinitionBodies.push(body);
      return fulfillJson(
        route,
        {
          ...manualDefinition,
          id: "definition-created",
          name: body.name,
          execution_scope_kind: body.execution_scope_kind,
          selected_source_count:
            body.execution_scope_kind === "SELECTED_MEDIA"
              ? scanSelectedKeys.size
              : 0,
          source_config: body.source_config,
          file_filter: body.file_filter,
          site_ids: body.site_ids,
          output_config: body.output_config,
        },
        201,
      );
    }
    return fulfillJson(route, {
      items: [manualDefinition, monitorDefinition, failedDefinition],
    });
  });
  await page.route("**/api/v1/unpack/executions?**", (route) =>
    fulfillJson(route, { items: [execution, failedExecution] }),
  );
  await page.route(
    "**/api/v1/unpack/executions/execution-review",
    async (route) => {
      executionDetailGetCount += 1;
      return fulfillJson(route, execution);
    },
  );

  const sourceBase = {
    execution_id: "execution-review",
    source_snapshot: {
      path: "/downloads/anime/Show.S01E01.mkv",
      relative_path: "Show.S01E01.mkv",
    },
    media_identity: {
      raw_name: "Show.S01E01.1080p.WEB-DL",
      title_tokens: ["show"],
      year: 2026,
      resolution: "1080p",
      release_source: "WEB-DL",
      codec: "H.265",
      release_group: "Group",
      total_size: 1073741824,
      external_ids: [{ namespace: "imdb", value: "tt1234567" }],
    },
    candidate_generation: 1,
    highest_candidate_score_bps: null,
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
      id: "item-auto",
      source_object_key: "source-auto",
      status: "MATCHED_AUTO",
      highest_candidate_score_bps: 10000,
      selected_candidate_id: "candidate-auto",
      match_origin: "AUTO",
      review_allowed: true,
    },
    {
      ...sourceBase,
      id: "item-review",
      source_object_key: "source-review",
      status: "REVIEW_REQUIRED",
      highest_candidate_score_bps: 10000,
      selected_candidate_id: null,
      match_origin: null,
      review_allowed: true,
      media_identity: {
        ...sourceBase.media_identity,
        raw_name: "Project.Hail.Mary.2026.1080p.BluRay",
        external_ids: [{ namespace: "douban", value: "35428968" }],
      },
    },
    {
      ...sourceBase,
      id: "item-error",
      source_object_key: "source-error",
      status: "MATCH_ERROR",
      selected_candidate_id: "candidate-auto",
      match_origin: null,
      has_external_operations: true,
      last_error_code: "DOWNLOADER_UNAVAILABLE",
      last_error_message: "辅助文件已下载，查询 Transmission 暂时失败",
      auxiliary_state: {
        state: "DOWNLOADING",
        missing_paths: ["Release/Movie.nfo"],
      },
    },
  ];
  await page.route(
    "**/api/v1/unpack/executions/execution-review/items**",
    async (route) => {
      const url = new URL(route.request().url());
      const limit = Number(url.searchParams.get("limit"));
      observedPageLimits.push(limit);
      observedExecutionFilters.push({
        status: url.searchParams.get("item_status"),
        q: url.searchParams.get("q"),
      });
      if (!largePageMode) {
        if (executionItemsDelayMs > 0) {
          await new Promise((resolve) =>
            setTimeout(resolve, executionItemsDelayMs),
          );
        }
        executionItemsResponseResolved = true;
        return fulfillJson(route, {
          items: executionItems,
          next_cursor: null,
          has_more: false,
        });
      }
      const offset = Number(url.searchParams.get("cursor") || 0);
      const sample = Array.from({ length: 61 }, (_, i) => ({
        ...sourceBase,
        id: "page-item-" + i,
        source_object_key: "page-object-" + i,
        status: "NO_MATCH",
        selected_candidate_id: null,
        match_origin: null,
        review_allowed: false,
        media_identity: {
          ...sourceBase.media_identity,
          raw_name: "分页影片-" + i,
        },
      }));
      const next =
        offset + limit < sample.length ? String(offset + limit) : null;
      return fulfillJson(route, {
        items: sample.slice(offset, offset + limit),
        next_cursor: next,
        has_more: Boolean(next),
      });
    },
  );

  const candidateList = (itemId) => ({
    item_id: itemId,
    generation: 1,
    item_version: 3,
    default_candidate_id: "candidate-auto",
    candidates: [
      {
        id: "candidate-auto",
        item_id: itemId,
        generation: 1,
        site_id: "site-mteam",
        candidate_key: "torrent-1",
        title: "Show.S01E01.1080p.WEB-DL.H265-Group",
        size_bytes: 1073741824,
        imdb_id: "tt1234567",
        douban_id: null,
        seeders: 48,
        score_bps: 10000,
        is_exact_match: true,
        evidence: {
          hard_conflicts: [],
          exact: {
            title: true,
            external_id: true,
            year: true,
            episode: true,
            resolution: true,
            release_source: true,
            codec: true,
            release_group: true,
            size: true,
          },
        },
        verification_status: "NOT_CHECKED",
        verification_level: null,
        verification_error_code: null,
        created_at: now,
      },
      {
        id: "candidate-2",
        item_id: itemId,
        generation: 1,
        site_id: "site-hdtime",
        candidate_key: "torrent-2",
        title: "Show.S01E01.2160p.WEB-DL.H265",
        size_bytes: 2147483648,
        imdb_id: "tt1234567",
        douban_id: null,
        seeders: 112,
        score_bps: 8240,
        is_exact_match: false,
        evidence: {
          hard_conflicts: [],
          exact: {
            title: true,
            external_id: true,
            year: true,
            episode: true,
            resolution: false,
            release_source: true,
            codec: true,
            release_group: false,
            size: false,
          },
        },
        verification_status: "NOT_CHECKED",
        verification_level: null,
        verification_error_code: null,
        created_at: now,
      },
    ],
  });
  const longDiagnosticTargetPath =
    "/isolated/synthetic-test/" +
    "nested-folder/".repeat(25) +
    "Thelma.and.Louise.1991.1080p.mkv";
  await page.route("**/api/v1/unpack/items/**", (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (
      pathname === "/api/v1/unpack/items/item-error/actions" &&
      route.request().method() === "POST"
    ) {
      assert.deepEqual(route.request().postDataJSON(), {
        action: "retry_match",
      });
      assert.equal(route.request().headers()["if-match"], "3");
      return fulfillJson(route, {
        item_id: "item-error",
        generation: 1,
        retry_count: 0,
        item_version: 4,
        item_status: "AUXILIARY_FETCHING",
      });
    }
    if (
      pathname === "/api/v1/unpack/items/item-error/diagnostics" &&
      route.request().method() === "GET"
    ) {
      const log = (type, time, status = "APPLIED", error = null) => ({
        operation_type: type,
        status,
        last_error_code: error,
        downloader_id: type.includes("ADD") ? "tr-main" : null,
        torrent_hash: type.includes("ADD") ? "a".repeat(40) : null,
        target_path: longDiagnosticTargetPath,
        last_observed_state: null,
        recorded_file_type: type.includes("HARDLINK") ? "regular" : null,
        recorded_device: null,
        recorded_inode: null,
        recorded_size: type.includes("HARDLINK") ? 123456 : null,
        updated_at: time,
      });
      return fulfillJson(route, {
        item_id: "item-error",
        status: "MATCH_ERROR",
        item_version: 3,
        execution_stage: "FILES_MATERIALIZED",
        plan_status: "READY",
        plan_blocked_reasons: [],
        acceptance_read_only: false,
        last_error_code: "DOWNLOADER_UNAVAILABLE",
        last_error_message: "下载器暂时无法访问，等待重新连接。",
        requires_manual_reconciliation: false,
        reconciliation_reasons: [],
        downloader_state_live_checked: false,
        filesystem_state_live_checked: false,
        operations_truncated: false,
        operations: [
          log("UNPACK_EXEC_CREATE_DIRECTORY", "2026-10-23T10:01:00Z"),
          log("UNPACK_EXEC_MATERIALIZE_HARDLINK", "2026-10-23T10:01:01Z"),
          log("UNPACK_AUX_TORRENT_ADD", "2026-10-23T10:02:02Z"),
        ],
      });
    }
    if (
      pathname === "/api/v1/unpack/items/item-error/diagnostics/live" &&
      route.request().method() === "GET"
    ) {
      return fulfillJson(route, {
        status: "NO_ADD_EVIDENCE",
        downloader_id: null,
        client_kind: null,
        torrent_hash: null,
        current_state: null,
        progress: null,
        ownership_verified: false,
        save_path_matches: false,
        error_code: null,
      });
    }
    const match = pathname.match(/\/unpack\/items\/([^/]+)\/candidates\/?$/);
    if (!match) {
      return fulfillJson(
        route,
        {
          code: "E2E_UNMOCKED_ITEM_API",
          detail: "v1.1.4 item API 未显式 mock",
        },
        501,
      );
    }
    return fulfillJson(route, candidateList(decodeURIComponent(match[1])));
  });

  await page.route("**/api/v1/unpack/source-scans**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const pathname = url.pathname;
    if (
      request.method() === "POST" &&
      pathname.endsWith("/unpack/source-scans")
    ) {
      return fulfillJson(
        route,
        {
          id: "scan-1",
          directory_path: "/downloads",
          discovered_count: 2,
          selected_count: scanSelectedKeys.size,
          expires_at: "2099-01-01T00:00:00Z",
          version: 1,
        },
        201,
      );
    }
    if (
      request.method() === "GET" &&
      pathname.endsWith("/source-scans/scan-1/items")
    ) {
      const items = [
        {
          source_object_key: "movie-a",
          relative_path: "Movie.A.2026.2160p.mkv",
          filename: "Movie.A.2026.2160p.mkv",
          extension: ".mkv",
          resolution: "2160p",
          size_bytes: 2147483648,
          selected: scanSelectedKeys.has("movie-a"),
        },
        {
          source_object_key: "movie-b",
          relative_path: "archive/Movie.B.2026.1080p.mkv",
          filename: "Movie.B.2026.1080p.mkv",
          extension: ".mkv",
          resolution: "1080p",
          size_bytes: 1073741824,
          selected: scanSelectedKeys.has("movie-b"),
        },
      ];
      return fulfillJson(route, { items, next_cursor: null, has_more: false });
    }
    if (
      request.method() === "GET" &&
      pathname.endsWith("/source-scans/scan-1/selection-summary")
    ) {
      return fulfillJson(route, {
        discovered_count: 2,
        selected_count: scanSelectedKeys.size,
      });
    }
    if (
      request.method() === "PUT" &&
      pathname.endsWith("/source-scans/scan-1/selection")
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
      { code: "E2E_UNMOCKED_SCAN_API", detail: "source scan API 未显式 mock" },
      501,
    );
  });

  await page.route("**/api/v1/files/tree/roots", (route) =>
    fulfillJson(route, {
      items: [
        {
          name: "downloads",
          display_path: "/downloads",
          selection_token: "root-downloads",
        },
        {
          name: "downloads2",
          display_path: "/downloads2",
          selection_token: "root-downloads2",
        },
      ],
    }),
  );
  await page.route("**/api/v1/files/tree?**", (route) => {
    const token = new URL(route.request().url()).searchParams.get(
      "selection_token",
    );
    if (token === "root-downloads") {
      return fulfillJson(route, {
        display_path: "/downloads",
        selection_token: token,
        entries: [
          {
            name: "movies",
            display_path: "/downloads/movies",
            selection_token: "movies-token",
          },
          {
            name: "seeding",
            display_path: "/downloads/seeding",
            selection_token: "seeding-token",
          },
        ],
      });
    }
    return fulfillJson(route, {
      display_path: "/downloads2",
      selection_token: token,
      entries: [],
    });
  });

  try {
    await page.goto(`${frontendUrl}/#%E6%80%BB%E8%A7%88`, {
      waitUntil: "domcontentloaded",
    });
    await page
      .getByRole("heading", { name: "PackBreaker 总览", exact: true })
      .waitFor();
    assert.equal(
      await page.locator(".overview-task-strip").count(),
      0,
      "总览不应显示待审核/运行中/可重试/待对账摘要行",
    );
    assert.equal(
      await page.locator(".lifecycle").count(),
      1,
      "删除摘要行后应保留任务生命周期区域",
    );
    await page.goto(`${frontendUrl}/#%E4%BB%BB%E5%8A%A1%E4%B8%AD%E5%BF%83`, {
      waitUntil: "domcontentloaded",
    });
    await page
      .getByRole("heading", { name: "数据拆包", exact: true, level: 2 })
      .waitFor();
    const sidebar = page.locator("aside.sidebar");
    const sidebarNav = page.locator("aside.sidebar nav");
    assert.equal(
      await page.locator(".sidebar-slogan").count(),
      0,
      "侧栏 Slogan 应由关于入口替换",
    );
    assert.equal(
      await page.locator(".about-corner-button").count(),
      0,
      "不应再显示右下角关于按钮",
    );
    assert.equal(
      await page.locator("main footer").count(),
      0,
      "页面底部不应再显示统一页脚文本",
    );
    assert.equal(
      await sidebarNav.getByText("关于", { exact: true }).count(),
      0,
    );
    const aboutButton = page.getByRole("button", { name: "关于 PackBreaker" });
    await aboutButton.waitFor({ state: "visible" });
    assert.equal(
      await sidebar.locator(".sidebar-about").count(),
      1,
      "关于入口应位于侧栏底部",
    );
    const sidebarRect = await sidebar.boundingBox();
    const aboutRect = await aboutButton.boundingBox();
    assert.ok(sidebarRect && aboutRect, "关于入口和侧栏必须可测量");
    assert.ok(
      aboutRect.x >= sidebarRect.x &&
        aboutRect.x + aboutRect.width <= sidebarRect.x + sidebarRect.width,
      "关于入口应位于侧栏横向范围内",
    );
    assert.ok(
      aboutRect.y + aboutRect.height >= 980 - 70,
      "关于入口应贴近侧栏底部",
    );
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole("button", { name: "展开导航" }).click();
    await page.locator("aside.sidebar.visible").waitFor();
    await page.waitForTimeout(250);
    const mobileAbout = await aboutButton.boundingBox();
    assert.ok(mobileAbout, "移动端打开菜单后必须显示关于入口");
    assert.ok(
      mobileAbout.x >= 0 && mobileAbout.x + mobileAbout.width <= 390,
      "移动端关于入口超出屏幕",
    );
    assert.ok(
      mobileAbout.y >= 0 && mobileAbout.y + mobileAbout.height <= 844,
      "移动端关于入口超出屏幕",
    );
    await aboutButton.click();
    await page.waitForURL(/#(?:%E5%85%B3%E4%BA%8E|关于)/i);
    await page.goto(`${frontendUrl}/#%E4%BB%BB%E5%8A%A1%E4%B8%AD%E5%BF%83`, {
      waitUntil: "domcontentloaded",
    });
    await page
      .getByRole("heading", { name: "数据拆包", exact: true, level: 2 })
      .waitFor();
    await page.setViewportSize({ width: 1440, height: 980 });

    assert.equal(
      await sidebarNav.getByText("数据拆包", { exact: true }).count(),
      1,
      "任务中心应通过二级菜单提供数据拆包",
    );
    assert.equal(
      await sidebarNav.getByText("数据去重", { exact: true }).count(),
      1,
      "数据去重应是独立二级菜单",
    );
    assert.equal(
      await page.locator("main .movie-dedup-panel").count(),
      0,
      "数据拆包视图不得混入影片去重页面",
    );
    assert.equal(
      await page
        .getByRole("button", { name: "清理历史 payload", exact: true })
        .count(),
      0,
      "数据拆包视图不得暴露旧 retention/purge 入口",
    );

    await page.getByText("电影库手动拆包", { exact: true }).waitFor();
    await page.getByText("新番持续监控", { exact: true }).waitFor();
    const unpackFilterBar = page.locator(
      ".v2-task-center .unpack-filter-toolbar",
    );
    assert.equal(
      await unpackFilterBar.getByText("数据拆包任务", { exact: true }).count(),
      0,
    );
    assert.equal(
      await unpackFilterBar.evaluate(
        (element) => element.closest(".table-card") !== null,
      ),
      false,
      "数据拆包过滤栏应位于白色列表卡片之外",
    );
    for (const label of ["刷新", "新增任务"]) {
      assert.equal(
        await unpackFilterBar
          .getByRole("button", { name: label, exact: true })
          .count(),
        1,
      );
    }
    for (const label of ["搜索", "重置"]) {
      assert.equal(
        await unpackFilterBar
          .getByRole("button", { name: label, exact: true })
          .count(),
        0,
        `数据拆包过滤栏不应显示“${label}”按钮`,
      );
    }
    const unpackFilterStyle = await unpackFilterBar
      .locator(".filters")
      .evaluate((element) => {
        const style = window.getComputedStyle(element);
        return {
          backgroundColor: style.backgroundColor,
          borderTopWidth: style.borderTopWidth,
          boxShadow: style.boxShadow,
          paddingTop: style.paddingTop,
        };
      });
    assert.equal(unpackFilterStyle.backgroundColor, "rgba(0, 0, 0, 0)");
    assert.equal(unpackFilterStyle.borderTopWidth, "0px");
    assert.equal(unpackFilterStyle.boxShadow, "none");
    assert.equal(unpackFilterStyle.paddingTop, "0px");
    const unpackFiltersBounds = await unpackFilterBar
      .locator(".filters")
      .boundingBox();
    const unpackActionsBounds = await unpackFilterBar
      .locator(".toolbar-actions")
      .boundingBox();
    assert.ok(
      unpackFiltersBounds &&
        unpackActionsBounds &&
        unpackActionsBounds.x > unpackFiltersBounds.x,
      "数据拆包过滤器应在左侧，刷新和新增任务应位于操作按钮组右侧",
    );
    await unpackFilterBar
      .getByPlaceholder("搜索任务名称 / 来源")
      .fill("新番持续监控");
    await unpackFilterBar
      .getByPlaceholder("搜索任务名称 / 来源")
      .press("Enter");
    assert.equal(
      await page.locator(".unpack-table .el-table__row").count(),
      1,
      "搜索应只保留匹配任务",
    );
    assert.equal(
      await page.getByText("电影库手动拆包", { exact: true }).count(),
      0,
    );
    await unpackFilterBar.getByPlaceholder("搜索任务名称 / 来源").fill("");
    await unpackFilterBar
      .getByPlaceholder("搜索任务名称 / 来源")
      .press("Enter");
    assert.equal(
      await page.locator(".unpack-table .el-table__row").count(),
      3,
      "清空关键词并回车应恢复全部任务",
    );

    await sidebarNav.getByText("数据去重", { exact: true }).click();
    await page
      .getByRole("heading", { name: "数据去重", exact: true, level: 2 })
      .waitFor();
    const dedupPanel = page.locator(".movie-dedup-panel");
    const dedupFilterBar = dedupPanel.locator(".dedup-filter-toolbar");
    assert.equal(
      await dedupPanel.getByText("影片去重", { exact: true }).count(),
      0,
      "页面标题应统一为数据去重",
    );
    assert.equal(
      await dedupFilterBar.evaluate(
        (element) => element.closest(".table-card") !== null,
      ),
      false,
      "数据去重过滤栏应位于白色列表卡片之外",
    );
    for (const label of ["刷新", "新增任务"]) {
      assert.equal(
        await dedupFilterBar
          .getByRole("button", { name: label, exact: true })
          .count(),
        1,
      );
    }
    for (const label of ["搜索", "重置"]) {
      assert.equal(
        await dedupFilterBar
          .getByRole("button", { name: label, exact: true })
          .count(),
        0,
        `数据去重过滤栏不应显示“${label}”按钮`,
      );
    }
    const dedupFilterStyle = await dedupFilterBar
      .locator(".filters")
      .evaluate((element) => {
        const style = window.getComputedStyle(element);
        return {
          backgroundColor: style.backgroundColor,
          borderTopWidth: style.borderTopWidth,
          boxShadow: style.boxShadow,
          paddingTop: style.paddingTop,
        };
      });
    assert.equal(dedupFilterStyle.backgroundColor, "rgba(0, 0, 0, 0)");
    assert.equal(dedupFilterStyle.borderTopWidth, "0px");
    assert.equal(dedupFilterStyle.boxShadow, "none");
    assert.equal(dedupFilterStyle.paddingTop, "0px");
    assert.equal(
      await dedupFilterBar
        .getByRole("button", { name: "新增影片去重任务", exact: true })
        .count(),
      0,
    );
    assert.equal(
      await dedupPanel.locator(".movie-job-table .el-table__row").count(),
      2,
    );
    await dedupFilterBar.getByPlaceholder("搜索任务名称 / 目录").fill("动漫");
    await dedupFilterBar.getByPlaceholder("搜索任务名称 / 目录").press("Enter");
    assert.equal(
      await dedupPanel.locator(".movie-job-table .el-table__row").count(),
      1,
      "数据去重搜索应匹配任务名和目录",
    );
    assert.equal(
      await dedupPanel.getByText("动漫库重复扫描", { exact: true }).count(),
      1,
    );
    await dedupFilterBar.getByPlaceholder("搜索任务名称 / 目录").fill("");
    await dedupFilterBar.getByPlaceholder("搜索任务名称 / 目录").press("Enter");
    assert.equal(
      await dedupPanel.locator(".movie-job-table .el-table__row").count(),
      2,
      "数据去重清空关键词并回车应恢复全部任务",
    );
    await dedupFilterBar.locator(".filters .el-select").nth(1).click();
    await page.getByRole("option", { name: "失败", exact: true }).click();
    assert.equal(
      await dedupPanel.locator(".movie-job-table .el-table__row").count(),
      1,
      "数据去重状态过滤应生效",
    );
    await sidebarNav.getByText("数据拆包", { exact: true }).click();
    await page.getByText("电影库手动拆包", { exact: true }).waitFor();

    // 新增拆包任务四步向导：验证手动/监控条件、来源目标、过滤高级折叠及确认摘要。
    await unpackFilterBar
      .getByRole("button", { name: "新增任务", exact: true })
      .click();
    const wizardDialog = page
      .locator(".el-dialog")
      .filter({ hasText: "新增数据拆包任务" })
      .last();
    const wizard = wizardDialog.locator(".unpack-wizard");
    await wizard.waitFor({ state: "visible" });
    assert.equal(
      await wizard.locator(".wizard-step-heading h3").textContent(),
      "基本信息",
    );
    assert.equal(
      await wizard.locator(".cron-panel").count(),
      0,
      "手动拆包不显示 Cron 执行时间",
    );

    // 模板拆包只显示单站点 READY 模板，并且只能选择该模板绑定的站点。
    await wizard
      .locator(".trigger-card")
      .filter({ hasText: "模板拆包" })
      .click();
    const templateField = wizard
      .locator(".el-form-item")
      .filter({ hasText: "拆包模板" })
      .first();
    await templateField.locator(".el-select__wrapper").click();
    await page
      .getByRole("option", { name: /Synthetic M-TEAM template/ })
      .waitFor();
    assert.equal(
      await page.getByRole("option", { name: /跨站模板不应显示/ }).count(),
      0,
      "绑定多个站点的模板不应出现在可选模板列表",
    );
    await page
      .getByRole("option", { name: /Synthetic M-TEAM template/ })
      .click();
    assert.match(
      (await wizard.locator(".template-site-summary").textContent()) || "",
      /M-TEAM/,
      "模板摘要应显示其绑定站点",
    );
    assert.match(
      (await wizard.locator(".template-site-summary").textContent()) || "",
      /2 个影片目录/,
      "模板摘要应显示已关联影片目录数量",
    );
    const templateSiteField = wizard
      .locator(".el-form-item")
      .filter({ hasText: "使用的站点配置" })
      .first();
    await templateSiteField.locator(".el-select__wrapper").click();
    assert.equal(
      await page.getByRole("option", { name: "M-TEAM", exact: true }).count(),
      1,
      "模板绑定的 M-TEAM 站点应可选",
    );
    assert.equal(
      await page.getByRole("option", { name: "HDTime", exact: true }).count(),
      0,
      "模板站点选择不得暴露未绑定的 HDTime",
    );
    await page.getByRole("option", { name: "M-TEAM", exact: true }).click();
    await wizard
      .locator(".trigger-card")
      .filter({ hasText: "手动拆包" })
      .click();
    assert.equal(
      await wizard
        .locator(".el-form-item")
        .filter({ hasText: "拆包模板" })
        .count(),
      0,
      "切回普通手动拆包后应隐藏模板专属字段",
    );
    await wizard
      .locator(".el-form-item")
      .filter({ hasText: "任务名称" })
      .locator("input")
      .fill("向导 UI 校验");
    const wizardSiteField = wizard
      .locator(".el-form-item")
      .filter({ hasText: "扫描站点" })
      .first();
    const wizardSiteLabelBounds = await wizardSiteField
      .locator(".el-form-item__label")
      .boundingBox();
    assert.ok(wizardSiteLabelBounds, "扫描站点标签应可测量");
    await page.mouse.click(
      wizardSiteLabelBounds.x + wizardSiteLabelBounds.width / 2,
      wizardSiteLabelBounds.y + wizardSiteLabelBounds.height / 2,
    );
    assert.equal(
      await page.locator(".el-select__popper:visible").count(),
      0,
      "点击下拉字段标题不应展开选项",
    );
    await wizardSiteField.locator(".el-form-item__content small").click();
    assert.equal(
      await page.locator(".el-select__popper:visible").count(),
      0,
      "点击辅助说明不应展开选项",
    );
    await wizardSiteField.locator(".el-select__wrapper").click();
    await page.getByRole("option", { name: "M-TEAM", exact: true }).click();
    await page.keyboard.press("Escape");
    await wizard
      .locator(".trigger-card")
      .filter({ hasText: "监控拆包" })
      .click();
    assert.equal(
      await wizard.locator(".cron-panel").count(),
      1,
      "监控拆包应显示 Cron 配置",
    );
    await wizard
      .locator(".cron-panel")
      .getByRole("button", { name: "每小时", exact: true })
      .click();
    assert.equal(
      await wizard.locator(".cron-panel input").inputValue(),
      "0 * * * *",
    );
    await wizard
      .locator(".trigger-card")
      .filter({ hasText: "手动拆包" })
      .click();
    assert.equal(
      await wizard.locator(".cron-panel").count(),
      0,
      "切回手动拆包应隐藏 Cron 配置",
    );
    // 切换任务类型会清空扫描站点，回到手动拆包后重新选择，避免用旧选择绕过校验。
    const resetWizardSiteField = wizard
      .locator(".el-form-item")
      .filter({ hasText: "扫描站点" })
      .first();
    await resetWizardSiteField.locator(".el-select__wrapper").click();
    await page.getByRole("option", { name: "M-TEAM", exact: true }).click();
    await page.keyboard.press("Escape");
    await wizardDialog
      .getByRole("button", { name: "下一步", exact: false })
      .click();
    assert.equal(
      await wizard.locator(".wizard-step-heading h3").textContent(),
      "来源与目标",
    );
    assert.equal(
      await wizard
        .locator(".source-choice")
        .getByRole("button", { name: "下载器", exact: false })
        .count(),
      0,
      "手动拆包不允许选择下载器来源",
    );
    const wizardSourceField = wizard
      .locator(".el-form-item")
      .filter({ hasText: "来源目录" })
      .first();
    await wizardSourceField
      .getByRole("button", { name: "选择目录", exact: true })
      .click();
    let wizardTreeDialog = page
      .locator(".el-dialog")
      .filter({ hasText: "选择目录" })
      .last();
    await wizardTreeDialog.waitFor({ state: "visible" });
    await wizardTreeDialog.getByText("downloads", { exact: true }).click();
    await wizardTreeDialog
      .getByRole("button", { name: "使用此目录", exact: true })
      .click();
    const wizardOutputField = wizard
      .locator(".el-form-item")
      .filter({ hasText: "输出目录" })
      .first();
    await wizardOutputField
      .getByRole("button", { name: "选择目录", exact: true })
      .click();
    wizardTreeDialog = page
      .locator(".el-dialog")
      .filter({ hasText: "选择目录" })
      .last();
    await wizardTreeDialog.waitFor({ state: "visible" });
    await wizardTreeDialog.getByText("downloads2", { exact: true }).click();
    await wizardTreeDialog
      .getByRole("button", { name: "使用此目录", exact: true })
      .click();
    const wizardTargetField = wizard
      .locator(".el-form-item")
      .filter({ hasText: "目标下载器" })
      .first();
    await wizardTargetField.locator(".el-select").click();
    await page
      .getByRole("option", { name: "qBittorrent · 主下载器", exact: true })
      .click();
    const skipCheckPanel = wizard.locator(".skip-downloader-check-panel");
    assert.equal(
      await skipCheckPanel.count(),
      1,
      "来源与目标步骤应展示跳过下载器检查开关",
    );
    assert.equal(
      await skipCheckPanel.locator(".el-switch input").isChecked(),
      false,
      "跳过下载器检查默认关闭",
    );
    await wizardDialog
      .getByRole("button", { name: "下一步", exact: false })
      .click();
    assert.equal(
      await wizard.locator(".wizard-step-heading h3").textContent(),
      "文件过滤",
    );
    assert.equal(
      await wizard.locator(".advanced-rule-content").count(),
      0,
      "高级执行规则默认折叠",
    );
    const advancedToggle = wizard.getByRole("button", { name: /高级执行规则/ });
    assert.equal(await advancedToggle.getAttribute("aria-expanded"), "false");
    await advancedToggle.click();
    assert.equal(await wizard.locator(".advanced-rule-content").count(), 1);
    const extensionFilterField = wizard.locator(".extension-filter-item");
    assert.equal(
      await extensionFilterField.locator(".extension-label-hint").textContent(),
      "多个后缀使用逗号分隔。",
      "后缀说明应以内联提示展示在标签右侧",
    );
    assert.equal(
      await extensionFilterField
        .locator(".el-form-item__content small")
        .count(),
      0,
      "后缀说明不应留在输入框下方",
    );
    const advancedFieldLabels = await wizard
      .locator(".advanced-rule-content .form-grid.three .el-form-item__label")
      .allTextContents();
    assert.deepEqual(
      advancedFieldLabels.map((label) => label.trim()),
      ["自动匹配", "自动重试", "存放方式", "自动匹配阈值（%）", "重试次数"],
    );
    const autoMatchField = wizard
      .locator(".advanced-rule-content .el-form-item")
      .filter({ hasText: "自动匹配" })
      .first();
    assert.equal(
      await autoMatchField.locator(".el-switch input").isChecked(),
      true,
      "新增任务的自动匹配默认开启",
    );
    assert.equal(
      await autoMatchField.locator(".el-switch").count(),
      1,
      "自动匹配应使用与自动重试相同的开关样式",
    );
    assert.equal(await autoMatchField.locator(".el-radio-group").count(), 0);
    assert.equal(
      await wizard
        .locator(".advanced-rule-content .el-form-item")
        .filter({ hasText: "自动重试" })
        .locator(".el-switch")
        .count(),
      1,
    );
    const thresholdField = wizard
      .locator(".advanced-rule-content .el-form-item")
      .filter({ hasText: "自动匹配阈值（%）" });
    assert.equal(
      await thresholdField.locator("input").inputValue(),
      "99.0",
      "自动匹配默认阈值应为 99.0%",
    );

    await wizard.getByRole("button", { name: /高级执行规则/ }).click();
    assert.equal(await wizard.locator(".advanced-rule-content").count(), 0);
    await wizardDialog
      .getByRole("button", { name: "下一步", exact: false })
      .click();
    assert.equal(
      await wizard.locator(".wizard-step-heading h3").textContent(),
      "确认创建",
    );
    assert.equal(
      await wizard.locator(".review-card").count(),
      4,
      "确认页应分组展示四类配置摘要",
    );
    assert.match(
      await wizard.locator(".review-card").first().textContent(),
      /向导 UI 校验/,
    );
    await wizard
      .locator(".review-card")
      .filter({ hasText: "文件过滤" })
      .getByRole("button", { name: "编辑", exact: true })
      .click();
    assert.equal(
      await wizard.locator(".wizard-step-heading h3").textContent(),
      "文件过滤",
      "确认页编辑应返回对应步骤",
    );
    await wizardDialog
      .getByRole("button", { name: "下一步", exact: false })
      .click();
    await wizardDialog.locator(".el-dialog__headerbtn").click();
    assert.equal(createdDefinitionBodies.length, 0, "取消向导演练不得提交任务");

    // 监控任务第二步应开放下载器来源及任务名/分类/标签筛选。
    await unpackFilterBar
      .getByRole("button", { name: "新增任务", exact: true })
      .click();
    const monitorWizardDialog = page
      .locator(".el-dialog")
      .filter({ hasText: "新增数据拆包任务" })
      .last();
    const monitorWizard = monitorWizardDialog.locator(".unpack-wizard");
    await monitorWizard.waitFor({ state: "visible" });
    await monitorWizard
      .locator(".el-form-item")
      .filter({ hasText: "任务名称" })
      .locator("input")
      .fill("监控向导 UI 校验");
    const monitorSiteField = monitorWizard
      .locator(".el-form-item")
      .filter({ hasText: "扫描站点" })
      .first();
    await monitorSiteField.locator(".el-select").click();
    await page.getByRole("option", { name: "M-TEAM", exact: true }).click();
    await page.keyboard.press("Escape");
    await monitorWizard
      .locator(".trigger-card")
      .filter({ hasText: "监控拆包" })
      .click();
    // 任务类型切换会重置扫描站点，选择监控模式后重新设置站点再继续。
    const resetMonitorSiteField = monitorWizard
      .locator(".el-form-item")
      .filter({ hasText: "扫描站点" })
      .first();
    await resetMonitorSiteField.locator(".el-select__wrapper").click();
    await page.getByRole("option", { name: "M-TEAM", exact: true }).click();
    await page.keyboard.press("Escape");
    assert.equal(await monitorWizard.locator(".cron-panel").count(), 1);
    await monitorWizardDialog
      .getByRole("button", { name: "下一步", exact: false })
      .click();
    assert.equal(
      await monitorWizard
        .locator(".source-choice")
        .getByRole("button", { name: "下载器", exact: false })
        .count(),
      1,
    );
    await monitorWizard
      .locator(".source-choice")
      .getByRole("button", { name: "下载器", exact: false })
      .click();
    const monitorSourceSection = monitorWizard.locator(".form-section").first();
    assert.equal(
      await monitorSourceSection
        .locator(".el-form-item")
        .filter({ hasText: "来源下载器" })
        .count(),
      1,
    );
    assert.equal(
      await monitorSourceSection
        .locator(".el-form-item")
        .filter({ hasText: "任务名称包含" })
        .count(),
      1,
    );
    assert.equal(
      await monitorSourceSection
        .locator(".el-form-item")
        .filter({ hasText: "分类" })
        .count(),
      1,
    );
    assert.equal(
      await monitorSourceSection
        .locator(".el-form-item")
        .filter({ hasText: "标签" })
        .count(),
      1,
    );
    await monitorWizardDialog.locator(".el-dialog__headerbtn").click();
    const manualRow = page
      .locator(".unpack-table .el-table__row")
      .filter({ hasText: "电影库手动拆包" })
      .first();
    const monitorRow = page
      .locator(".unpack-table .el-table__row")
      .filter({ hasText: "新番持续监控" })
      .first();
    const failedRow = page
      .locator(".unpack-table .el-table__row")
      .filter({ hasText: "匹配失败重试测试" })
      .first();
    assert.equal(
      await manualRow.getByText("待执行", { exact: true }).count(),
      1,
    );
    assert.equal(
      await monitorRow.getByText("待人工审核", { exact: true }).count(),
      1,
    );
    const activeDefinitionDelete = monitorRow.getByRole("button", {
      name: "删除",
      exact: true,
    });
    await page.waitForFunction(
      () =>
        document
          .querySelector(".table-card")
          ?.getAttribute("data-actions-mode") === "inline",
    );
    assert.equal(
      await activeDefinitionDelete.isEnabled(),
      true,
      "存在待审核子项的任务应提供删除操作；后端校验是否仍有匹配中子项",
    );

    await page.waitForFunction(
      () =>
        document
          .querySelector(".table-card")
          ?.getAttribute("data-actions-mode") === "inline",
    );
    assert.equal(
      await manualRow.locator(".task-row-actions button").count(),
      3,
      "宽屏待执行任务应直接显示执行、编辑和删除，不显示省略按钮",
    );
    assert.equal(
      await manualRow
        .getByRole("button", { name: "执行", exact: false })
        .count(),
      1,
    );
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 980 });
      const expectedMode = width === 1440 ? "inline" : "overflow";
      await page.waitForFunction(
        (mode) =>
          document
            .querySelector(".table-card")
            ?.getAttribute("data-actions-mode") === mode,
        expectedMode,
      );
      const actions = failedRow.locator(".task-row-actions");
      const positions = await actions.locator("button").evaluateAll((buttons) =>
        buttons.map((button) => {
          const bounds = button.getBoundingClientRect();
          return {
            left: bounds.left,
            right: bounds.right,
            width: bounds.width,
          };
        }),
      );
      assert.equal(
        positions.length,
        expectedMode === "inline" ? 4 : 3,
        expectedMode === "inline"
          ? "宽屏失败任务应直接展示查看、重新匹配、编辑和删除"
          : "窄屏失败任务应展示查看、重新匹配和更多操作",
      );
      assert.equal(
        await failedRow
          .getByRole("button", { name: "编辑", exact: true })
          .count(),
        expectedMode === "inline" ? 1 : 0,
        "编辑按钮应根据操作列宽度决定直接显示或收纳",
      );
      assert.equal(
        await failedRow
          .getByRole("button", { name: "删除", exact: true })
          .count(),
        expectedMode === "inline" ? 1 : 0,
        "删除按钮应根据操作列宽度决定直接显示或收纳",
      );
      assert.equal(
        await failedRow.getByRole("button", { name: "更多操作" }).count(),
        expectedMode === "inline" ? 0 : 1,
        "只有操作列宽度不足时才显示省略按钮",
      );
      for (let index = 1; index < positions.length; index++) {
        assert.ok(
          positions[index].left >= positions[index - 1].right - 1,
          width + "px 操作按钮发生重叠",
        );
      }
      assert.ok(
        positions.every((bounds) => bounds.width >= 28),
        width + "px 操作按钮触发区域过窄",
      );
      if (expectedMode === "overflow") {
        await failedRow.getByRole("button", { name: "更多操作" }).click();
        await page
          .getByRole("menuitem", { name: "编辑", exact: true })
          .waitFor();
        await page
          .getByRole("menuitem", { name: "删除", exact: true })
          .waitFor();
        await page.keyboard.press("Escape");
      }
    }
    await page.setViewportSize({ width: 1440, height: 980 });

    // 独立回归：模板拆包选择下载器来源时，下载器/种子控件应对齐，安全提示位于“来源种子”标题右侧。
    await page.getByRole("button", { name: "新增任务", exact: true }).click();
    const createDialog = page
      .locator(".el-dialog")
      .filter({ hasText: "新增数据拆包任务" })
      .last();
    await createDialog.waitFor({ state: "visible" });
    const createWizard = createDialog.locator(".unpack-wizard");
    assert.equal(
      await createWizard.locator(".wizard-progress .wizard-step").count(),
      4,
    );
    assert.equal(
      await createWizard.locator(".wizard-step-heading h3").textContent(),
      "基本信息",
    );
    assert.equal(
      await createWizard.locator(".cron-panel").count(),
      0,
      "默认手动拆包不显示 Cron 配置",
    );
    assert.equal(
      await createWizard
        .getByRole("button", { name: "监控拆包", exact: false })
        .count(),
      1,
    );
    assert.equal(
      await createDialog
        .getByRole("button", { name: "下一步", exact: false })
        .count(),
      1,
    );
    assert.equal(
      await createWizard.locator(".review-card").count(),
      0,
      "初始步骤不应提前显示确认摘要",
    );
    await createWizard
      .locator(".trigger-card")
      .filter({ hasText: "模板拆包" })
      .click();
    const createTemplateField = createWizard
      .locator(".el-form-item")
      .filter({ hasText: "拆包模板" })
      .first();
    await createTemplateField.locator(".el-select__wrapper").click();
    await page
      .getByRole("option", { name: /Synthetic M-TEAM template/ })
      .click();
    const createTemplateSiteField = createWizard
      .locator(".el-form-item")
      .filter({ hasText: "使用的站点配置" })
      .first();
    await createTemplateSiteField.locator(".el-select__wrapper").click();
    await page.getByRole("option", { name: "M-TEAM", exact: true }).click();
    await createWizard
      .locator(".el-form-item")
      .filter({ hasText: "任务名称" })
      .locator("input")
      .fill("模板来源对齐校验");
    await createDialog
      .getByRole("button", { name: "下一步", exact: false })
      .click();
    assert.equal(
      await createWizard.locator(".wizard-step-heading h3").textContent(),
      "选择来源与目标",
    );
    assert.equal(
      await createWizard.locator(".skip-downloader-check-panel").count(),
      1,
      "模板拆包来源与目标步骤也应展示跳过下载器检查开关",
    );
    await createWizard
      .locator(".source-choice")
      .getByRole("button", { name: "下载器", exact: false })
      .click();
    const createSourceDownloaderField = createWizard
      .locator(".el-form-item")
      .filter({ hasText: "来源下载器" })
      .first();
    const createSourceSeedField = createWizard
      .locator(".el-form-item")
      .filter({ hasText: "来源种子" })
      .first();
    assert.equal(
      await createSourceSeedField.locator(".source-seed-hint").textContent(),
      "仅读取已完成种子；创建任务不会修改下载器任务。",
    );
    assert.equal(
      await createSourceSeedField
        .locator(".el-form-item__content small")
        .count(),
      0,
      "安全提示应放在来源种子标题右侧，而不是输入框下方",
    );
    const sourceDownloaderBox = await createSourceDownloaderField
      .locator(".el-select__wrapper")
      .boundingBox();
    const sourceSeedBox = await createSourceSeedField
      .locator(".el-select__wrapper")
      .boundingBox();
    assert.ok(
      sourceDownloaderBox && sourceSeedBox,
      "来源下载器和来源种子选择框应可测量",
    );
    assert.ok(
      Math.abs(sourceDownloaderBox.y - sourceSeedBox.y) <= 1,
      "来源下载器与来源种子选择框应水平对齐",
    );
    const sourceSeedLabelLayout = await createSourceSeedField.evaluate(
      (field) => {
        const title = field.querySelector(
          ".source-seed-label-row .extension-label-title",
        );
        const hint = field.querySelector(".source-seed-hint");
        if (!title || !hint) return null;
        return {
          titleRight: title.getBoundingClientRect().right,
          hintLeft: hint.getBoundingClientRect().left,
        };
      },
    );
    assert.ok(
      sourceSeedLabelLayout &&
        sourceSeedLabelLayout.hintLeft >= sourceSeedLabelLayout.titleRight - 1,
      "安全提示应显示在来源种子标题右侧",
    );
    await createDialog.locator(".el-dialog__headerbtn").click();

    executionItemsDelayMs = 900;
    executionItemsResponseResolved = false;
    const executionItemsRequestStarted = page.waitForRequest((request) => {
      return (
        new URL(request.url()).pathname ===
        "/api/v1/unpack/executions/execution-review/items"
      );
    });
    await monitorRow.getByRole("button", { name: "查看", exact: true }).click();
    const executionDialog = page
      .locator(".el-dialog")
      .filter({ hasText: "执行详情 · 新番持续监控" })
      .last();
    await executionItemsRequestStarted;
    await executionDialog.waitFor({ state: "visible" });
    assert.equal(
      executionItemsResponseResolved,
      false,
      "执行详情弹窗应先显示，不等待影片列表接口返回",
    );
    assert.equal(
      executionDetailGetCount,
      0,
      "打开执行详情不应再串行请求重复的执行汇总接口",
    );
    const executionDialogHeader = executionDialog.locator(
      ".execution-dialog-header",
    );
    const executionDialogTitle = executionDialogHeader.locator(
      ".execution-dialog-title",
    );
    const matchingNotice = executionDialogHeader.locator(
      ".execution-matching-notice",
    );
    assert.equal(
      await matchingNotice.count(),
      1,
      "匹配阶段提示应移动到弹窗标题右侧",
    );
    const titleBounds = await executionDialogTitle.boundingBox();
    const noticeBounds = await matchingNotice.boundingBox();
    assert.ok(
      titleBounds &&
        noticeBounds &&
        noticeBounds.x >= titleBounds.x + titleBounds.width - 2,
    );
    assert.ok(
      Math.abs(
        titleBounds.y +
          titleBounds.height / 2 -
          (noticeBounds.y + noticeBounds.height / 2),
      ) <= 6,
      "标题与匹配阶段提示应在同一行",
    );
    assert.equal(
      await matchingNotice
        .locator(".el-alert__title")
        .evaluate((element) => getComputedStyle(element).whiteSpace),
      "nowrap",
      "匹配阶段提示应单行展示",
    );
    for (const step of [
      "分页获取影片",
      "分批影片匹配",
      "自动匹配 / 人工审核",
      "种子内容校验",
      "辅种执行",
      "下载器处理 / 完成",
    ]) {
      assert.equal(
        (await executionDialog.getByText(new RegExp(step)).count()) >= 1,
        true,
        "缺少执行阶段：" + step,
      );
    }
    const executionMetrics = executionDialog.locator(".metric-grid");
    await executionDialog
      .locator(".el-table__body .el-table__row")
      .first()
      .waitFor({ state: "visible" });
    assert.equal(
      executionItemsResponseResolved,
      true,
      "匹配弹窗先展示后，影片列表应正常完成加载",
    );
    executionItemsDelayMs = 0;
    const executionOperationHeader = executionDialog
      .locator(".el-table__header-wrapper th")
      .filter({ hasText: "操作" })
      .last();
    const executionOperationBounds =
      await executionOperationHeader.boundingBox();
    assert.ok(
      executionOperationBounds &&
        executionOperationBounds.width >= 295 &&
        executionOperationBounds.width <= 305,
      "执行详情操作列应固定为约 300px",
    );
    assert.equal(
      await executionDialog
        .locator(".execution-item-actions")
        .first()
        .evaluate((element) => getComputedStyle(element).flexWrap),
      "nowrap",
      "四个操作按钮应保持单行，不再因操作列变窄而换行",
    );
    const actionAlignment = await executionDialog
      .locator(".execution-item-actions")
      .first()
      .evaluate((element) => {
        const container = element.getBoundingClientRect();
        const buttons = Array.from(element.children);
        const left = buttons[0]?.getBoundingClientRect().left ?? container.left;
        const right =
          buttons.at(-1)?.getBoundingClientRect().right ?? container.right;
        return {
          justifyContent: getComputedStyle(element).justifyContent,
          leftSpace: left - container.left,
          rightSpace: container.right - right,
        };
      });
    assert.equal(
      actionAlignment.justifyContent,
      "center",
      "操作列按钮应水平居中排列",
    );
    assert.ok(
      Math.abs(actionAlignment.leftSpace - actionAlignment.rightSpace) <= 3,
      "左右留白应基本相等，操作按钮应真正居中：" +
        JSON.stringify(actionAlignment),
    );
    const fourActionGroup = executionDialog
      .locator(".execution-item-actions")
      .nth(2);
    const fourActionWidth = await fourActionGroup.evaluate((element) => {
      const reviewGroup = document
        .querySelectorAll(".execution-item-actions")[1]
        ?.querySelector(".review-action-group")
        ?.cloneNode(true);
      if (!reviewGroup) return null;
      element.insertBefore(reviewGroup, element.lastElementChild);
      const result = {
        buttons: element.querySelectorAll("button").length,
        clientWidth: element.clientWidth,
        scrollWidth: element.scrollWidth,
        children: Array.from(element.children).map((child) => ({
          text: child.textContent?.trim(),
          width: child.getBoundingClientRect().width,
        })),
      };
      reviewGroup.remove();
      return result;
    });
    assert.ok(
      fourActionWidth &&
        fourActionWidth.buttons === 4 &&
        fourActionWidth.scrollWidth <= fourActionWidth.clientWidth + 1,
      "300px 操作列应刚好容纳四个按钮而不产生横向溢出：" +
        JSON.stringify(fourActionWidth),
    );
    assert.deepEqual(
      await executionMetrics.locator("small").allTextContents(),
      [
        "影片总数",
        "待匹配",
        "匹配中",
        "自动匹配",
        "无匹配",
        "待人工审核",
        "内容校验通过",
        "匹配超时",
        "异常 / 不一致",
      ],
      "执行详情应在自动匹配前分别展示待匹配和匹配中的数量",
    );
    assert.deepEqual(
      (await executionMetrics.locator("b").allTextContents()).slice(0, 4),
      ["3", "0", "0", "1"],
    );
    await executionDialog
      .locator(".execution-item-filters .el-select__wrapper")
      .click();
    for (const statusLabel of ["待匹配", "匹配中"]) {
      await page
        .getByRole("option", { name: statusLabel, exact: true })
        .waitFor({ state: "visible" });
    }
    await page.keyboard.press("Escape");
    assert.equal(
      await executionDialog.getByText("自动匹配成功", { exact: true }).count(),
      1,
    );
    assert.equal(
      await executionDialog.getByText("匹配错误", { exact: true }).count(),
      1,
    );
    const replaceCandidateButton = executionDialog.getByRole("button", {
      name: "更换候选",
      exact: true,
    });
    assert.equal(
      await replaceCandidateButton.count(),
      1,
      "自动匹配成功的影片只提供可选的更换候选入口",
    );
    assert.equal(
      await executionDialog
        .getByRole("button", { name: "审核", exact: true })
        .count(),
      1,
      "只有需要人工处理的影片显示审核入口",
    );
    await replaceCandidateButton.hover();
    await page
      .locator(".el-popper:visible")
      .filter({ hasText: "自动匹配结果会自动进入内容验证" })
      .waitFor({ state: "visible" });
    await page.mouse.move(0, 0);
    assert.equal(
      await executionDialog.getByText("最高 100.0%", { exact: true }).count(),
      2,
      "审核入口旁应展示本轮候选队列最高匹配度",
    );
    const reviewRowOffsets = await executionDialog
      .locator(".review-action-group")
      .evaluateAll((groups) =>
        groups.map((group) => {
          const button = group.querySelector("button");
          const score = group.querySelector(".best-candidate-score");
          if (!button || !score) return Number.POSITIVE_INFINITY;
          const buttonBox = button.getBoundingClientRect();
          const scoreBox = score.getBoundingClientRect();
          return Math.abs(
            buttonBox.y +
              buttonBox.height / 2 -
              (scoreBox.y + scoreBox.height / 2),
          );
        }),
      );
    const reviewRowOrder = await executionDialog
      .locator(".review-action-group")
      .evaluateAll((groups) =>
        groups.map((group) => {
          const score = group.querySelector(".best-candidate-score");
          const button = group.querySelector("button");
          return Boolean(
            score &&
              button &&
              score.getBoundingClientRect().right <
                button.getBoundingClientRect().left,
          );
        }),
      );
    assert.deepEqual(
      reviewRowOrder,
      [true, true],
      "最高匹配度应展示在审核或更换候选入口左侧",
    );
    assert.equal(reviewRowOffsets.length, 2);
    assert(
      reviewRowOffsets.every((offset) => offset <= 1),
      "操作入口与最高匹配度必须处于同一水平中心线",
    );
    assert.equal(
      await executionDialog
        .getByRole("button", { name: "继续补齐", exact: true })
        .count(),
      1,
    );
    assert.equal(
      await executionDialog
        .getByRole("button", { name: "删除", exact: true })
        .count(),
      3,
    );
    await executionDialog
      .getByRole("button", { name: "执行明细", exact: true })
      .click();
    const historyDialog = page
      .locator(".el-dialog.execution-detail-dialog")
      .last();
    await historyDialog.waitFor({ state: "visible" });
    await historyDialog
      .locator(".execution-detail-stages")
      .waitFor({ state: "visible" });
    assert.equal(
      await historyDialog.locator(".detail-summary-card").count(),
      0,
      "执行明细不应再展示顶部摘要卡",
    );
    assert.equal(
      await historyDialog.locator(".execution-detail-overview").count(),
      0,
    );
    assert.equal(
      await historyDialog.locator(".execution-detail-explanation").count(),
      0,
    );
    for (const heading of ["执行阶段", "操作记录"]) {
      assert.equal(
        await historyDialog.getByText(heading, { exact: true }).count(),
        1,
        "执行明细缺少 " + heading,
      );
    }
    assert.equal(
      await historyDialog.locator(".detail-timeline .detail-step").count(),
      3,
    );
    assert.equal(
      await historyDialog.getByText("请求下载器校验", { exact: true }).count(),
      0,
      "历史中无 VERIFY 不应虚构校验成功",
    );
    assert.equal(
      await historyDialog
        .getByText("请求开始下载或做种", { exact: true })
        .count(),
      0,
      "历史中无 START 不应虚构启动成功",
    );
    assert.equal(
      await historyDialog
        .locator(".detail-history-table .el-table__row")
        .count(),
      3,
    );
    for (const removed of [
      "只按已保存的操作记录展示；未发生的步骤不会标记成功",
      "历史中的“成功”仅表示当时记录成功，不保证文件或种子现在仍处于相同状态。",
      "仅查询，不修改种子或文件",
    ]) {
      assert.equal(
        await historyDialog.getByText(removed, { exact: true }).count(),
        0,
        "执行明细不应再展示：" + removed,
      );
    }
    assert.equal(
      await historyDialog
        .getByRole("button", { name: "查询下载器状态（只读）" })
        .count(),
      0,
      "执行明细弹窗不再提供查询下载器状态按钮",
    );
    const clippedTarget = historyDialog.locator(".detail-target").first();
    const targetLayout = await clippedTarget.evaluate((node) => ({
      scrollWidth: node.scrollWidth,
      clientWidth: node.clientWidth,
      textOverflow: getComputedStyle(node).textOverflow,
      whiteSpace: getComputedStyle(node).whiteSpace,
    }));
    assert.ok(
      targetLayout.scrollWidth > targetLayout.clientWidth,
      "长路径应超出容器并以省略号截断",
    );
    assert.equal(targetLayout.textOverflow, "ellipsis");
    assert.equal(targetLayout.whiteSpace, "nowrap");
    await clippedTarget.hover();
    const fullPathTooltip = page
      .locator(".execution-detail-target-tooltip:visible")
      .last();
    await fullPathTooltip.waitFor({ state: "visible" });
    assert(
      (await fullPathTooltip.innerText()).includes(longDiagnosticTargetPath),
      "悬停后应展示位置或下载器列的完整内容",
    );
    await page.mouse.move(0, 0);
    const detailFooter = historyDialog.locator(".detail-footer");
    assert.equal(await detailFooter.getByRole("button").count(), 1);
    const closeAlignment = await detailFooter.evaluate((footer) => {
      const button = footer.querySelector("button");
      if (!button) return null;
      return {
        rightGap: Math.abs(
          footer.getBoundingClientRect().right -
            button.getBoundingClientRect().right,
        ),
        justify: getComputedStyle(footer).justifyContent,
      };
    });
    assert(
      closeAlignment && closeAlignment.rightGap <= 2,
      "关闭按钮应该位于弹窗右下角",
    );
    assert.equal(closeAlignment.justify, "flex-end");
    await historyDialog
      .getByRole("button", { name: "关闭", exact: true })
      .click();
    await historyDialog.waitFor({ state: "hidden" });
    const deletedDisabled = await executionDialog
      .getByRole("button", { name: "删除", exact: true })
      .evaluateAll((buttons) => buttons.map((button) => button.disabled));
    assert.deepEqual(
      deletedDisabled,
      [false, false, false],
      "非待匹配/匹配中子项，即使执行未结束也应允许删除",
    );
    await executionDialog
      .getByRole("button", { name: "继续补齐", exact: true })
      .click();
    await page
      .getByText("已恢复原有辅助文件补齐，将校验已下载文件，不会重复添加种子")
      .waitFor();

    await executionDialog
      .getByRole("button", { name: "审核", exact: true })
      .first()
      .click();
    const reviewDialog = page
      .locator(".el-dialog")
      .filter({ hasText: "人工审核候选" })
      .last();
    await reviewDialog.waitFor({ state: "visible" });
    await reviewDialog
      .getByText("默认选中匹配度最高的一条，但必须点击确认后才会继续。", {
        exact: true,
      })
      .waitFor();
    const manualAddButton = reviewDialog.getByRole("button", {
      name: "手动添加",
      exact: true,
    });
    await manualAddButton.waitFor({ state: "visible" });
    assert.equal(
      await reviewDialog
        .locator(".el-dialog__header .manual-add-candidate-trigger")
        .count(),
      0,
      "手动添加不应继续显示在弹窗标题旁",
    );
    assert.equal(
      await reviewDialog
        .locator(".review-heading-actions > .manual-add-candidate-trigger")
        .count(),
      1,
      "手动添加必须位于候选列表右侧的人工审核文字旁",
    );
    const reviewHeadingOrder = await reviewDialog
      .locator(".review-heading-actions")
      .evaluate((group) => {
        const tag = group.querySelector(".el-tag");
        const button = group.querySelector(".manual-add-candidate-trigger");
        if (!tag || !button) return null;
        const tagBox = tag.getBoundingClientRect();
        const buttonBox = button.getBoundingClientRect();
        return {
          tagRight: tagBox.right,
          buttonLeft: buttonBox.left,
          centerDiff: Math.abs(
            tagBox.top +
              tagBox.height / 2 -
              (buttonBox.top + buttonBox.height / 2),
          ),
        };
      });
    assert(reviewHeadingOrder, "人工审核标识及按钮必须都存在");
    assert(
      reviewHeadingOrder.tagRight < reviewHeadingOrder.buttonLeft,
      "手动添加必须在人工审核文字右侧",
    );
    assert(
      reviewHeadingOrder.centerDiff <= 1,
      "人工审核文字与手动添加按钮必须垂直居中",
    );
    assert.equal(
      await reviewDialog
        .getByRole("button", { name: "稍后处理", exact: true })
        .count(),
      0,
      "人工审核候选弹窗不应再有稍后处理按钮",
    );
    const manualAddColor = await manualAddButton.evaluate(
      (button) => getComputedStyle(button).color,
    );
    assert.notEqual(
      manualAddColor,
      "rgb(255, 255, 255)",
      "手动添加按钮默认态文字不应为白色而不可见",
    );
    await manualAddButton.click();
    const manualCandidateDialog = page
      .locator(".el-dialog")
      .filter({ hasText: "手动添加种子候选" })
      .last();
    await manualCandidateDialog.waitFor({ state: "visible" });
    await manualCandidateDialog
      .getByRole("radio", { name: "种子详情页 URL" })
      .waitFor();
    await manualCandidateDialog
      .getByRole("radio", { name: "种子文件" })
      .waitFor();
    await manualCandidateDialog
      .getByPlaceholder("https://站点/details.php?id=12345")
      .waitFor();
    await manualCandidateDialog
      .getByRole("button", { name: "预览并对比", exact: true })
      .waitFor();
    await manualCandidateDialog
      .getByRole("button", { name: "确认并进入内容校验", exact: true })
      .waitFor();
    await manualCandidateDialog.getByText("种子文件", { exact: true }).click();
    await manualCandidateDialog
      .locator('input[type="file"][accept*=".torrent"]')
      .waitFor();
    await manualCandidateDialog
      .getByRole("button", { name: "取消", exact: true })
      .click();
    const candidateCards = reviewDialog.locator(".candidate-card");
    await candidateCards.first().waitFor({ state: "visible" });
    assert.equal(
      await candidateCards.count(),
      2,
      "审核弹窗必须展示当前候选代次",
    );
    assert.match((await candidateCards.first().textContent()) || "", /100\.0%/);
    assert.equal(
      await reviewDialog
        .getByRole("button", { name: /确认所选候选并继续/ })
        .count(),
      1,
    );
    const selectedRadio = reviewDialog.locator(
      '.candidate-card.selected input[type="radio"]',
    );
    assert.equal(
      await selectedRadio.isChecked(),
      true,
      "最高候选应该只作为 UI 默认预选",
    );

    await reviewDialog.locator(".el-dialog__headerbtn").click();
    await executionDialog.locator(".el-dialog__headerbtn").click();

    // Read-only cursor pagination must round-trip to the backend with the
    // selected page size; changing it resets the cursor to page 1.
    largePageMode = true;
    await monitorRow.getByRole("button", { name: "查看", exact: true }).click();
    await executionDialog.getByText("分页影片-0", { exact: true }).waitFor();
    assert.equal(
      await executionDialog.locator(".el-table__body .el-table__row").count(),
      20,
    );
    assert.equal(
      await executionDialog
        .getByRole("button", { name: "重新匹配", exact: true })
        .count(),
      20,
      "每个无匹配影片都应提供受任务重试策略约束的重试入口",
    );
    await executionDialog.getByRole("button", { name: "下一页" }).click();
    await executionDialog.getByText("分页影片-20", { exact: true }).waitFor();
    const secondPageLabel = executionDialog.getByText("第 2 页", {
      exact: false,
    });
    await secondPageLabel.waitFor({ state: "visible" });
    assert.equal(await secondPageLabel.count(), 1);
    await executionDialog.getByRole("button", { name: "上一页" }).click();
    await executionDialog.getByText("分页影片-0", { exact: true }).waitFor();
    await executionDialog.locator(".pager .el-select").click();
    await page.getByRole("option", { name: "50", exact: true }).click();
    await executionDialog.getByText("分页影片-49", { exact: true }).waitFor();
    assert.equal(
      await executionDialog.locator(".el-table__body .el-table__row").count(),
      50,
    );
    await executionDialog.locator(".pager .el-select").click();
    await page.getByRole("option", { name: "100", exact: true }).click();
    await executionDialog.getByText("分页影片-60", { exact: true }).waitFor();
    assert.equal(
      await executionDialog.locator(".el-table__body .el-table__row").count(),
      61,
    );
    assert.deepEqual(observedPageLimits.slice(-4), [20, 20, 50, 100]);
    await executionDialog.locator(".execution-item-filters .el-select").click();
    await page.getByRole("option", { name: "无匹配", exact: true }).click();
    assert.equal(observedExecutionFilters.at(-1)?.status, "NO_MATCH");
    const expectedQuery = (response) => {
      const url = new URL(response.url());
      return (
        url.pathname === "/api/v1/unpack/executions/execution-review/items" &&
        url.searchParams.get("item_status") === "NO_MATCH" &&
        url.searchParams.get("q") === "九品芝麻官" &&
        response.request().method() === "GET" &&
        response.status() === 200
      );
    };
    const searchResponse = page.waitForResponse(expectedQuery);
    await executionDialog
      .getByPlaceholder("搜索影片名或来源路径")
      .fill("九品芝麻官");
    await executionDialog
      .getByPlaceholder("搜索影片名或来源路径")
      .press("Enter");
    await searchResponse;
    assert.equal(observedExecutionFilters.at(-1)?.q, "九品芝麻官");
    const filterCountBeforeRefresh = observedExecutionFilters.length;
    const refreshResponse = page.waitForResponse(expectedQuery);
    await executionDialog
      .locator(".execution-item-filters")
      .getByRole("button", { name: "刷新" })
      .click();
    await refreshResponse;
    assert.equal(
      observedExecutionFilters.length,
      filterCountBeforeRefresh + 1,
      "刷新必须重新发出过滤后的查询",
    );
    assert.equal(observedExecutionFilters.at(-1)?.status, "NO_MATCH");
    assert.equal(observedExecutionFilters.at(-1)?.q, "九品芝麻官");
    await executionDialog.locator(".el-dialog__headerbtn").click();

    assert.equal(
      await monitorRow
        .getByRole("button", { name: "重试", exact: true })
        .count(),
      1,
      "运行中但包含待审核和异常影片的拆包任务，也应显示批量重试",
    );
    assert.match(await failedRow.textContent(), /无匹配 1/);
    assert.match(await failedRow.textContent(), /异常 0/);
    await failedRow.getByRole("button", { name: "重试", exact: true }).click();
    const retryDialog = page.locator(".el-dialog.unpack-retry-dialog").last();
    await retryDialog.waitFor({ state: "visible" });
    assert.equal(
      await retryDialog.locator(".el-select").count(),
      1,
      "弹窗必须只有一个多选状态框",
    );
    assert.equal(
      await retryDialog
        .getByRole("button", { name: "重试", exact: true })
        .count(),
      1,
    );
    assert.equal(
      await retryDialog
        .getByRole("button", { name: "重试", exact: true })
        .isDisabled(),
      true,
      "未选择状态禁止重试",
    );
    await retryDialog.locator(".el-select").click();
    for (const stateLabel of ["无匹配", "匹配错误"]) {
      await page.getByRole("option", { name: stateLabel, exact: true }).click();
    }
    assert.equal(
      await retryDialog
        .getByRole("button", { name: "重试", exact: true })
        .isEnabled(),
      true,
    );
    // Element Plus keeps multi-select popovers open while selecting several
    // values. Close the popover before clicking the modal's submit footer.
    await retryDialog.locator(".el-dialog__header").click();
    const bulkRetryResponse = page.waitForResponse(
      (response) =>
        response
          .url()
          .includes("/api/v1/unpack/executions/execution-failed/actions") &&
        response.request().method() === "POST" &&
        response.status() === 200,
      { timeout: 15000 },
    );
    await retryDialog
      .getByRole("button", { name: "重试", exact: true })
      .click();
    const completedBulkRetry = await bulkRetryResponse;
    await retryDialog.waitFor({ state: "hidden" });
    // The request event fires before the async mock route handler runs. Wait
    // for its successful response before examining captured request data.
    assert.deepEqual(completedBulkRetry.request().postDataJSON(), {
      action: "retry_eligible_items",
      item_statuses: ["NO_MATCH", "MATCH_ERROR"],
    });
    assert.deepEqual(bulkRetryBody, {
      action: "retry_eligible_items",
      item_statuses: ["NO_MATCH", "MATCH_ERROR"],
    });
    await failedRow.getByRole("button", { name: "重试", exact: true }).click();
    await retryDialog.waitFor({ state: "visible" });
    assert.equal(
      await retryDialog
        .getByRole("button", { name: "重试", exact: true })
        .isDisabled(),
      true,
      "重新打开不得保留之前选中状态",
    );
    await retryDialog.locator(".el-dialog__headerbtn").click();
    await retryDialog.waitFor({ state: "hidden" });
    await manualRow.getByRole("button", { name: "编辑", exact: true }).click();
    const editDialog = page
      .locator(".el-dialog")
      .filter({ hasText: "编辑数据拆包任务" })
      .last();
    await editDialog.waitFor({ state: "visible" });
    await editDialog
      .locator(".el-form-item")
      .filter({ hasText: "任务名称" })
      .locator("input")
      .fill("人工编辑 E2E");
    for (let step = 1; step < 4; step += 1) {
      await editDialog
        .getByRole("button", { name: "下一步", exact: false })
        .click();
    }
    const manualEditResponse = page.waitForResponse(
      (response) =>
        response
          .url()
          .endsWith("/api/v1/unpack/definitions/definition-manual") &&
        response.request().method() === "PUT" &&
        response.status() === 200,
    );
    await editDialog.getByRole("button", { name: "保存", exact: true }).click();
    await manualEditResponse;
    assert.equal(updatedDefinitionBody?.name, "人工编辑 E2E");
    await monitorRow.getByRole("button", { name: "编辑", exact: true }).click();
    await editDialog.waitFor({ state: "visible" });
    for (let step = 1; step < 4; step += 1) {
      await editDialog
        .getByRole("button", { name: "下一步", exact: false })
        .click();
    }
    const monitorEditResponse = page.waitForResponse(
      (response) =>
        response
          .url()
          .endsWith("/api/v1/unpack/definitions/definition-monitor") &&
        response.request().method() === "PUT" &&
        response.status() === 200,
    );
    await editDialog.getByRole("button", { name: "保存", exact: true }).click();
    const monitorUpdate = await monitorEditResponse;
    assert.equal(
      monitorUpdate.request().postDataJSON()?.timezone,
      "Asia/Shanghai",
      "监控任务更新 PUT 必须带上编辑前的原始时区",
    );
    assert.equal(
      updatedMonitorBody?.timezone,
      "Asia/Shanghai",
      "编辑监控任务不得无意重置原时区",
    );
    await manualRow.getByRole("button", { name: "删除", exact: true }).click();
    const deleteResponse = page.waitForResponse(
      (response) =>
        response
          .url()
          .includes("/api/v1/unpack/definitions/definition-manual") &&
        response.request().method() === "DELETE" &&
        response.status() === 204,
    );
    await page.getByRole("button", { name: "确认删除", exact: true }).click();
    await deleteResponse;
    assert.equal(deletedDefinitionVersion, "1");

    await page.getByRole("button", { name: "新增任务", exact: true }).click();
    const manualDialog = page
      .locator(".el-dialog")
      .filter({ hasText: "新增数据拆包任务" })
      .last();
    await manualDialog.waitFor({ state: "visible" });
    await manualDialog
      .locator(".el-form-item")
      .filter({ hasText: "任务名称" })
      .locator("input")
      .fill("指定影片拆包 E2E");

    const siteField = manualDialog
      .locator(".el-form-item")
      .filter({ hasText: "扫描站点" })
      .first();
    await siteField.locator(".el-select").click();
    await page.getByRole("option", { name: "M-TEAM", exact: true }).click();
    await page.keyboard.press("Escape");
    await manualDialog
      .getByRole("button", { name: "下一步", exact: false })
      .click();

    const sourceField = manualDialog
      .locator(".el-form-item")
      .filter({ hasText: "来源目录" })
      .first();
    await sourceField
      .getByRole("button", { name: "选择目录", exact: true })
      .click();
    let treeDialog = page
      .locator(".el-dialog")
      .filter({ hasText: "选择目录" })
      .last();
    await treeDialog.waitFor({ state: "visible" });
    const pickerTitleBounds = await treeDialog
      .locator(".directory-picker-header > span")
      .boundingBox();
    const pickerActionBounds = await treeDialog
      .getByRole("button", { name: "使用此目录", exact: true })
      .boundingBox();
    assert.ok(
      pickerTitleBounds &&
        pickerActionBounds &&
        pickerActionBounds.x > pickerTitleBounds.x + pickerTitleBounds.width,
      "目录弹窗按钮应位于标题右侧",
    );
    assert.ok(
      pickerTitleBounds &&
        pickerActionBounds &&
        Math.abs(pickerActionBounds.y - pickerTitleBounds.y) < 12,
      "目录弹窗按钮应与标题位于同一行",
    );
    await treeDialog.getByText("downloads", { exact: true }).click();
    await treeDialog
      .getByRole("button", { name: "使用此目录", exact: true })
      .click();

    const outputField = manualDialog
      .locator(".el-form-item")
      .filter({ hasText: "输出目录" })
      .first();
    await outputField
      .getByRole("button", { name: "选择目录", exact: true })
      .click();
    treeDialog = page
      .locator(".el-dialog")
      .filter({ hasText: "选择目录" })
      .last();
    await treeDialog.waitFor({ state: "visible" });
    await treeDialog.getByText("downloads2", { exact: true }).click();
    await treeDialog
      .getByRole("button", { name: "使用此目录", exact: true })
      .click();

    const targetDownloaderField = manualDialog
      .locator(".el-form-item")
      .filter({ hasText: "目标下载器" })
      .first();
    await targetDownloaderField.locator(".el-select").click();
    await page
      .getByRole("option", { name: "qBittorrent · 主下载器", exact: true })
      .click();
    const manualSkipCheckPanel = manualDialog.locator(
      ".skip-downloader-check-panel",
    );
    assert.equal(
      await manualSkipCheckPanel.locator(".el-switch input").isChecked(),
      false,
    );
    await manualSkipCheckPanel.locator(".el-switch").click();
    await manualDialog
      .getByRole("button", { name: "下一步", exact: false })
      .click();
    assert.equal(
      await manualDialog.locator(".advanced-rule-content").count(),
      0,
      "新建任务文件过滤步骤的高级规则应默认折叠",
    );
    await manualDialog
      .getByRole("button", { name: "下一步", exact: false })
      .click();
    await manualDialog
      .getByRole("button", { name: "创建任务", exact: true })
      .click();

    const scopeDialog = page
      .locator(".el-dialog")
      .filter({ hasText: "确认拆包范围" })
      .last();
    await scopeDialog.waitFor({ state: "visible" });
    await scopeDialog
      .getByRole("button", { name: "否，选择影片", exact: true })
      .click();
    const mediaDialog = page
      .locator(".el-dialog")
      .filter({ hasText: "选择需要拆包的影视文件" })
      .last();
    await mediaDialog.waitFor({ state: "visible" });
    await mediaDialog
      .locator("b")
      .filter({ hasText: "Movie.A.2026.2160p.mkv" })
      .waitFor();
    await mediaDialog
      .getByRole("button", { name: "勾选当前结果", exact: true })
      .click();
    await mediaDialog.getByText("已选择 2 / 2", { exact: true }).waitFor();
    await mediaDialog
      .getByRole("button", { name: "保存任务", exact: true })
      .click();
    assert.equal(createdDefinitionBodies.length, 1);
    assert.equal(
      createdDefinitionBodies[0].execution_scope_kind,
      "SELECTED_MEDIA",
    );
    assert.equal(createdDefinitionBodies[0].source_scan_id, "scan-1");
    assert.equal(createdDefinitionBodies[0].source_kind, "DIRECTORY");
    assert.equal(
      createdDefinitionBodies[0].output_config.skip_downloader_check,
      true,
      "跳过下载器检查开关应写入任务配置",
    );

    await sidebarNav.getByText("数据去重", { exact: true }).click();
    await page.locator("main .movie-dedup-panel").waitFor({ state: "visible" });
    await page
      .getByRole("heading", { name: "数据去重", exact: true, level: 2 })
      .waitFor();
    await page.getByRole("button", { name: "新增任务", exact: true }).click();
    const dedupDialog = page
      .locator(".el-dialog")
      .filter({ hasText: "新增数据去重任务" })
      .last();
    await dedupDialog.waitFor({ state: "visible" });
    await dedupDialog.locator(".el-dialog__headerbtn").click();

    await sidebarNav.getByText("系统管理", { exact: true }).click();
    await sidebarNav.getByText("Agent配置", { exact: true }).click();
    const aiSettingsPage = page.locator(".ai-settings");
    const aiPageHeading = aiSettingsPage.locator(".ai-page-heading");
    await aiPageHeading
      .getByRole("heading", { name: "AI 助手", exact: true, level: 2 })
      .waitFor();
    assert.equal(await aiSettingsPage.locator(".ai-status-panel").count(), 0);
    assert.equal(await aiPageHeading.locator(".panel").count(), 0);
    assert.equal(
      await aiPageHeading
        .locator("h2")
        .evaluate((element) => window.getComputedStyle(element).fontSize),
      "20px",
      "Agent 页面标题应直接放在底色上并采用较大字号",
    );
    await aiSettingsPage
      .getByRole("heading", { name: "Provider 配置", exact: true })
      .waitFor();
    await aiSettingsPage
      .getByRole("heading", { name: "Telegram AI", exact: true })
      .waitFor();
    for (const removedText of [
      "AI 助手首版严格只读",
      "AI 可以查询健康、任务、脱敏日志",
      "v0.1.6 仅提供 Telegram AI 对话",
      "尚未测试",
      "Telegram 默认拒绝所有未授权来源",
      "复用“通知”中的 Telegram Bot Token 与代理",
      "OpenAI-compatible 的 Base URL 与 Model 由真实 Provider 决定是否可用",
      "无论如何选择，Cookie、API Key、密码、Session/CSRF、SecretStore",
    ]) {
      assert.equal(
        await aiSettingsPage.getByText(removedText, { exact: false }).count(),
        0,
        `Agent 配置页面不应再显示：${removedText}`,
      );
    }
    const providerHeading = aiSettingsPage.locator(".section-heading");
    const providerTitleBounds = await providerHeading
      .locator("h3")
      .boundingBox();
    const providerSwitchBounds = await providerHeading
      .locator(".el-switch")
      .boundingBox();
    assert.ok(
      providerTitleBounds &&
        providerSwitchBounds &&
        providerSwitchBounds.x >=
          providerTitleBounds.x + providerTitleBounds.width &&
        providerSwitchBounds.x -
          (providerTitleBounds.x + providerTitleBounds.width) <
          24 &&
        Math.abs(providerSwitchBounds.y - providerTitleBounds.y) < 14,
      "AI 助手启用开关应紧邻 Provider 配置标题右侧同一行",
    );
    const telegramHeading = aiSettingsPage.locator(".telegram-heading");
    const telegramTitleRow = telegramHeading.locator(".telegram-title-row");
    const telegramTitleBounds = await telegramTitleRow
      .locator("h3")
      .boundingBox();
    const telegramSwitchBounds = await telegramTitleRow
      .locator(".el-switch")
      .boundingBox();
    assert.ok(
      telegramTitleBounds &&
        telegramSwitchBounds &&
        telegramSwitchBounds.x >=
          telegramTitleBounds.x + telegramTitleBounds.width &&
        telegramSwitchBounds.x -
          (telegramTitleBounds.x + telegramTitleBounds.width) <
          24 &&
        Math.abs(telegramSwitchBounds.y - telegramTitleBounds.y) < 18,
      "启用 Telegram AI 开关应紧邻 Telegram AI 标题右侧同一行",
    );
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(100);
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth + 1,
      ),
      true,
      "Agent 配置页面 390px 不应产生页面级横向溢出",
    );
    await page.setViewportSize({ width: 1440, height: 980 });

    await sidebarNav.getByText("备份恢复", { exact: true }).click();
    const backupPage = page.locator(".backup-management");
    await backupPage
      .getByRole("heading", { name: "备份策略", exact: true })
      .waitFor();
    assert.equal(
      await backupPage.locator(":scope > .panel").count(),
      4,
      "备份恢复内容应采用与 Agent 配置一致的分区卡片布局",
    );
    assert.equal(await backupPage.locator(".backup-card").count(), 0);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(100);
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth + 1,
      ),
      true,
      "备份恢复页面 390px 不应产生页面级横向溢出",
    );
    await page.setViewportSize({ width: 1440, height: 980 });

    await sidebarNav.getByText("日志浏览", { exact: true }).click();
    const logsPage = page.locator(".operational-logs");
    await logsPage
      .getByRole("heading", { name: "运行日志", exact: true })
      .waitFor();
    const logFilterBar = logsPage.locator(".log-filter-toolbar");
    assert.equal(
      await logFilterBar.evaluate(
        (element) => element.closest(".panel") !== null,
      ),
      false,
    );
    const logFilterStyle = await logFilterBar.evaluate((element) => {
      const style = window.getComputedStyle(element);
      return {
        backgroundColor: style.backgroundColor,
        borderTopWidth: style.borderTopWidth,
        boxShadow: style.boxShadow,
        paddingTop: style.paddingTop,
      };
    });
    assert.equal(logFilterStyle.backgroundColor, "rgba(0, 0, 0, 0)");
    assert.equal(logFilterStyle.borderTopWidth, "0px");
    assert.equal(logFilterStyle.boxShadow, "none");
    assert.equal(logFilterStyle.paddingTop, "0px");
    await logFilterBar
      .getByPlaceholder("搜索日志内容、logger 或 trace_id")
      .waitFor();
    assert.equal(
      await logFilterBar
        .getByRole("button", { name: "刷新", exact: true })
        .count(),
      1,
    );
    for (const label of ["搜索", "重置"]) {
      assert.equal(
        await logFilterBar
          .getByRole("button", { name: label, exact: true })
          .count(),
        0,
      );
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(100);
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth + 1,
      ),
      true,
      "日志浏览过滤栏 390px 不应产生页面级横向溢出",
    );
    await page.setViewportSize({ width: 1440, height: 980 });

    await sidebarNav.getByText("数据拆包", { exact: true }).click();
    await page
      .getByRole("heading", { name: "数据拆包", exact: true, level: 2 })
      .waitFor();

    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(100);
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth + 1,
      ),
      true,
      "v1.1.4 数据拆包视图 390px 不应产生页面级横向溢出",
    );

    assert.deepEqual(unmocked, [], "v1.1.4 UI 烟测不得请求未显式 mock 的 API");
    assert.deepEqual(errors, []);
    console.log(
      `通过：v1.1.4 ${productionPreview ? "生产构建" : "开发构建"}独立数据拆包/去重路由、影片去重新增入口、Cron 辅助、下载器来源、保存语义、自动匹配审核、重试与 390px 响应式烟测。`,
    );
  } finally {
    await browser.close();
  }
})()
  .catch((error) => {
    console.error(error);
    for (const child of children)
      console.error(child.readOutput().slice(-2200));
    process.exitCode = 1;
  })
  .finally(async () => {
    for (const { child } of children.reverse()) {
      if (child.exitCode === null && child.signalCode === null) {
        const exited = new Promise((resolve) => child.once("exit", resolve));
        child.kill("SIGTERM");
        await Promise.race([exited, sleep(5000)]);
        if (child.exitCode === null && child.signalCode === null) {
          child.kill("SIGKILL");
          await exited;
        }
      }
    }
  });

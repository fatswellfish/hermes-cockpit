/**
 * Locale bundles for the project-hub plugin.
 *
 * Why this file exists
 * --------------------
 * Every user-visible string in this plugin used to be a Chinese literal inlined
 * at its render site. That is unmaintainable and it strands the UI in one
 * language no matter what the host app is set to. All display copy now lives
 * here, keyed, and `plugin.js` only ever references keys.
 *
 * Rules for this file:
 *   - keys are English identifiers; values are the display copy in that locale
 *   - no keys with behaviour baked in: e.g. `col.projects` carries a `{n}`
 *     placeholder instead of a pre-formatted "14 项目" string
 *   - code identifiers, comments and file names stay English everywhere else
 *
 * Resolution: the host's active locale picks the bundle (`zh` → `zh`,
 * anything else → `en`), so the plugin follows the app's UI language instead
 * of being pinned to one. `en` is the fallback so an unknown locale still
 * renders something readable rather than key names.
 */

export const DEFAULT_LOCALE = 'en'

export const MESSAGES = {
  en: {
    // status
    'status.doing': 'In progress',
    'status.todo': 'To do',
    'status.done': 'Done',
    'status.blocked': 'Blocked',

    // categories
    'category.software': 'Software',
    'category.media': 'Media',
    'category.infra': 'Infrastructure',
    'category.agent': 'Agents',
    'category.knowledge': 'Knowledge',
    'category.other': 'Other',

    // overview bar
    'stat.projects': '{n} projects',
    'stat.sessions': '{n} sessions',
    'stat.tasks': '{n} tasks',
    'stat.boards': '{n} boards',
    'stat.unassigned': '{n} unassigned',

    // left column
    'col.projects': '{n} projects',
    'col.sessions': '{n} sessions',
    'col.unassigned': 'Unassigned',
    'col.orphanTasks': '{n} tasks (slug not in project table)',
    'col.fileFlags.doing': '{n} in progress',
    'col.fileFlags.blocked': '{n} blocked',
    'col.item.sessions': '{n} sessions',
    'col.item.tasks': '{n} tasks',
    'col.item.cards': '{n} cards',

    // middle column
    'mid.sessions': 'Sessions',
    'mid.allSessions': 'All sessions',
    'mid.unassignedSessions': 'Unassigned sessions',
    'mid.projectTitle': 'Project: {slug}',
    'mid.newSession': '+ New session',
    'mid.newSessionHint': 'Create a session in this project directory (cwd_explicit, verified after create)',
    'mid.empty': 'No sessions yet',
    'mid.noCwd': '(no cwd)',
    'mid.manual': '· manual',
    'mid.pickProject': 'Pick a project…',
    'mid.classify': 'Classify',
    'mid.classifyHint': 'Attach this session to a project (writes META.yaml only, never touches cwd)',
    'mid.more': '{rest} more, click to show {step} more',
    'mid.moreHint': 'Load the next batch of sessions',
    'mid.classifiedTo': 'Classified to {slug}',
    'mid.openFailed': 'Failed to open session: {error}',
    'mid.untitled': '(untitled session)',

    // right column / tasks
    'right.tasks': 'Tasks',
    'right.sessionTasks': "This session's tasks",
    'right.orphanTasks': 'Unassigned tasks',
    'right.expandHint': 'Show subtasks',
    'right.collapseHint': 'Hide subtasks',
    // settings page (Settings -> Plugins)
    'settings.title': 'Project Hub settings',
    'settings.intro': 'Where the cockpit gets its data, and how often it refreshes.',
    'settings.refresh': 'Refresh interval',
    'settings.refreshHint': 'How often the cockpit re-reads projects, sessions and tasks.',
    'settings.seconds': '{n}s',
    'settings.confirmWrites': 'Confirm before ticking',
    'settings.confirmHint': 'Ask for confirmation before a subtask is marked done.',
    'settings.dataSource': 'Task files read by this plugin:',
    'right.orphanHint': 'proj not in project table',
    'right.slugMismatch': 'slug does not match project',
    'right.empty': 'No tasks here',
    'right.compatWarning': 'Compatibility notice',
    'right.snapshot': 'Snapshot:',
    'right.toggleHint': 'Click to check / uncheck',
    'right.readonlyHint': 'Read-only — writes disabled',
    'right.missingStamp': 'Missing file stamp: /overview returned no stamp, refusing to write',

    // setup / onboarding
    'setup.title': 'Project Hub is not initialized yet',
    'setup.body':
      'Current state: {status}. Pick one way to start (this cannot be switched back afterwards).',
    'setup.fresh': '🆕 Start fresh',
    'setup.imported': '📥 Import existing',
    'setup.changed': 'Onboarding state changed, refreshing…',

    // misc
    'common.loading': 'Loading…',
    'common.readFailed': 'Load failed (HTTP {status})',
    'common.networkError': 'Network error: {error}',
    'common.stale': 'File changed on disk, refreshing…',
    'common.loadFailed': 'Could not load project hub data',
    'common.readonlyBadge': '· read-only (onboarding incomplete)',
    'common.readonlyTip': 'Read-only (onboarding incomplete) — writes disabled',
    'common.on': 'On',
    'common.off': 'Off',

    // plugin identity (sidebar / palette / status chip)
    'plugin.name': 'Project Hub',
    'plugin.open': 'Open Project Hub'
  },

  zh: {
    'status.doing': '进行中',
    'status.todo': '待开始',
    'status.done': '已完成',
    'status.blocked': '阻塞',

    'category.software': '软件开发',
    'category.media': '媒体制作',
    'category.infra': '基础架构',
    'category.agent': '智能体',
    'category.knowledge': '智库',
    'category.other': '其他',

    'stat.projects': '{n} 项目',
    'stat.sessions': '{n} 会话',
    'stat.tasks': '{n} 任务',
    'stat.boards': '{n} 看板',
    'stat.unassigned': '{n} 未归属',

    'col.projects': '项目 {n}',
    'col.sessions': '{n} 会话',
    'col.unassigned': '未归属',
    'col.orphanTasks': '{n} 任务（slug 不在项目表里）',
    'col.fileFlags.doing': '{n} 进行',
    'col.fileFlags.blocked': '{n} 阻塞',
    'col.item.sessions': '{n} 会话',
    'col.item.tasks': '· {n} 任务',
    'col.item.cards': '· {n} 卡',

    'mid.sessions': '会话',
    'mid.allSessions': '全部会话',
    'mid.unassignedSessions': '未归属会话',
    'mid.projectTitle': '项目：{slug}',
    'mid.newSession': '＋ 新建会话',
    'mid.newSessionHint': '在该项目目录下新建会话（cwd_explicit，建完回查）',
    'mid.empty': '这里还没有会话',
    'mid.noCwd': '（无 cwd）',
    'mid.manual': '· 人工',
    'mid.pickProject': '选择项目…',
    'mid.classify': '归类',
    'mid.classifyHint': '把该会话归到某个项目（只写 META.yaml，不改 cwd）',
    'mid.more': '还有 {rest} 条，点击多显示 {step} 条',
    'mid.moreHint': '再加载一批会话',
    'mid.classifiedTo': '已归类到 {slug}',
    'mid.openFailed': '打开会话失败：{error}',
    'mid.untitled': '（无标题会话）',

    'right.tasks': '任务',
    'right.sessionTasks': '本会话引出的 todo',
    'right.orphanTasks': '未归属任务',
    'right.expandHint': '展开看子步骤',
    'right.collapseHint': '收起子步骤',
    // 设置页（设置 → 插件）
    'settings.title': '项目中枢设置',
    'settings.intro': '这里控制中枢从哪里取数、多久刷新一次。',
    'settings.refresh': '刷新间隔',
    'settings.refreshHint': '中枢多久重新读一次项目、会话和任务。',
    'settings.seconds': '{n} 秒',
    'settings.confirmWrites': '勾选前先确认',
    'settings.confirmHint': '把子任务标记为完成之前，先弹确认。',
    'settings.dataSource': '本插件读取的任务文件：',
    'right.orphanHint': 'proj 不在项目表',
    'right.slugMismatch': 'slug 不匹配项目',
    'right.empty': '这里没有任务',
    'right.compatWarning': '兼容性提示',
    'right.snapshot': '快照：',
    'right.toggleHint': '点击勾选/取消',
    'right.readonlyHint': '只读模式 — 写操作已禁用',
    'right.missingStamp': '缺少文件戳：/overview 没下发 stamp，无法安全写入',

    'setup.title': '项目中枢还没初始化',
    'setup.body': '当前引导状态：{status}。选择一种方式开始（选定后不可反向切换）。',
    'setup.fresh': '🆕 从零开始',
    'setup.imported': '📥 导入已有',
    'setup.changed': '引导状态已被改动，正在刷新…',

    'common.loading': '加载中…',
    'common.readFailed': '读取失败（HTTP {status}）',
    'common.networkError': '网络错误：{error}',
    'common.stale': '文件已被改动，正在刷新…',
    'common.loadFailed': '读不到项目中枢数据',
    'common.readonlyBadge': '· 只读（引导未完成）',
    'common.readonlyTip': '只读模式（引导未完成）— 写操作已禁用',
    'common.on': '开',
    'common.off': '关',

    'plugin.name': '项目中枢',
    'plugin.open': '打开项目中枢'
  }
}

/**
 * Normalize a host locale tag to a bundle key.
 *
 * The host's option ids are `en`, `zh`, `zh-hant`, `ja`, … (i18n/languages.ts
 * LOCALE_OPTIONS), and the config stores that same id in `display.language`.
 * Traditional Chinese is a distinct UI language, so it must not silently fall
 * back to the Simplified copy — until it has its own bundle it resolves to
 * English, which is honest, rather than showing 简体 to a 繁體 user.
 */
export function normalizeLocale(tag) {
  const raw = String(tag || '').toLowerCase().replace('_', '-')
  if (raw === 'zh' || raw === 'zh-cn' || raw === 'zh-sg') return 'zh'
  return DEFAULT_LOCALE
}

/**
 * Build a translator for `locale`.
 *
 * Missing key falls back to English, then to the key itself — a missing string
 * must never render as `undefined` in the UI.
 */
export function makeT(locale) {
  const primary = MESSAGES[normalizeLocale(locale)] || MESSAGES[DEFAULT_LOCALE]
  const fallback = MESSAGES[DEFAULT_LOCALE]
  return function t(key, vars) {
    let out = primary[key]
    if (out === undefined) out = fallback[key]
    if (out === undefined) return key
    if (!vars) return out
    return String(out).replace(/\{(\w+)\}/g, (whole, name) =>
      Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : whole
    )
  }
}

/** Locale-tag based translation, for code that never enters React. */
export const translateNow = (tag, key, vars) => makeT(tag)(key, vars)

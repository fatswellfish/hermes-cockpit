/**
 * hermes-cockpit — 项目中枢（C 模块桌面界面）
 *
 * 一屏三栏：左「分类 + 项目」· 中「会话」· 右「任务」。
 * 数据一律走 `/api/plugins/hermes-cockpit/*`（后端 dashboard/plugin_api.py），
 * 前端不直连宿主库、不改 cwd、不能合并/拆分会话。
 *
 * 无 JSX 语法（宿主不编译本文件），UI 一律 jsx() / jsxs() 调用。
 * 颜色只用主题 CSS 变量，禁硬编码（红色警示沿用现役 todo-panel 的 #d9534f 用法）。
 */

import { host, Tip, cn, SIDEBAR_NAV_AREA, PALETTE_AREA, ROUTES_AREA } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useState, useCallback, useRef, useMemo } from 'react'

const ID = 'project-hub'
const API = `/api/plugins/${ID}`

/**
 * 桌面插件的取数通道。
 *
 * ⛔ 绝不能用裸 fetch 打相对路径（例如引号里直接写 /api/plugins/...）：
 * 打包版桌面 App 的页面是 `file:///.../app.asar.unpacked/dist/index.html` 协议
 * （没有 dev server），相对路径会被当成**本地文件**去找，得到
 * `net::ERR_FILE_NOT_FOUND`，界面显示「读不到清单 / Failed to fetch」，
 * 而后端其实完全正常（DevTools ▸ Network 里一眼可见）。
 *
 * 真实通道是 preload 暴露的 `window.hermesDesktop.api({ path, method?, body?, priority? })`
 * （Electron IPC，与页面协议无关；App 内核自己也用它 —— src/api/client.ts 的 hermesApi）。
 *
 * 本函数把 IPC 结果**包装成 fetch Response 的形状**（ok / status / json()），
 * 这样上层的错误分支（409 stale / 403 越界 / 428 缺戳）一个字都不用改。
 */
async function apiFetch(path, { method = 'GET', body } = {}) {
  const bridge = globalThis.window && globalThis.window.hermesDesktop

  if (!bridge || typeof bridge.api !== 'function') {
    // 浏览器标签页形态（没有 Electron preload）：退回 fetch。此时页面是 http/https，相对路径可用。
    const r = await fetch(path, {
      body: body === undefined ? undefined : JSON.stringify(body),
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      method
    })
    return { json: () => r.json(), ok: r.ok, status: r.status }
  }

  // IPC 通道：非 2xx 会 reject，错误里带 status 与响应体，这里统一翻译回 Response 形状。
  try {
    const data = await bridge.api({ body, method, path, priority: 'foreground' })
    return { json: async () => data, ok: true, status: 200 }
  } catch (e) {
    const status = Number(e && (e.status || e.statusCode)) || 0
    const payload = (e && (e.body || e.payload || e.data)) || null
    const detail = (e && (e.message || e.error)) || String(e)
    return {
      json: async () => (payload && typeof payload === 'object' ? payload : { detail }),
      ok: false,
      status
    }
  }
}
const POLL_MS = 4000
const WARN = '#d9534f'

const STATUS_STYLE = {
  doing: { dot: 'var(--ui-accent)', label: '进行中' },
  todo: { dot: 'var(--ui-text-quaternary)', label: '待开始' },
  done: { dot: 'var(--ui-text-secondary)', label: '已完成' },
  blocked: { dot: WARN, label: '阻塞' }
}

const SESSION_PAGE = 60          // 中栏单次渲染上限，1479 条一次画完会卡死界面

// ── 取数 ────────────────────────────────────────────────────────────────────

function useOverview(intervalMs) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)

  const load = useCallback(async () => {
    try {
      const r = await apiFetch(`${API}/overview`)
      const j = await r.json()
      // 后端两种错误形态：REST 的 {detail} 与 handle() 的 {ok:false, error}
      if (!r.ok || (j && j.ok === false)) {
        setErr((j && (j.detail || j.error)) || `读取失败（HTTP ${r.status}）`)
      } else {
        setErr(null)
      }
      if (j && j.projects) setData(j)
    } catch (e) {
      setErr(String((e && e.message) || e))
    }
  }, [])

  useEffect(() => {
    let dead = false
    const tick = () => { if (!dead) load() }
    tick()
    const t = setInterval(tick, intervalMs || POLL_MS)
    return () => { dead = true; clearInterval(t) }
  }, [load, intervalMs])

  return { data, err, reload: load }
}

/**
 * 写操作统一入口（任务 2 · C4）。
 *
 * 三条硬语义，一条都不能丢：
 *   · 409 stale   → 提示「文件已被改动，正在刷新」并自动 reload，**不静默丢用户操作**
 *   · 403 越界    → 把后端给的原因原样显示（fresh 下对存量会话归类）
 *   · 428 缺戳    → 说明是插件缺陷，不当成用户错误
 * 成功返回 {ok:true, data}，失败返回 {ok:false, code, reason, message}。
 */
async function postJSON(path, body, onStale) {
  let r, j
  try {
    r = await apiFetch(`${API}${path}`, {
      method: 'POST',
      body: body || {}
    })
  } catch (e) {
    return { ok: false, code: 0, message: `网络错误：${(e && e.message) || e}` }
  }
  try {
    j = await r.json()
  } catch (e) {
    j = null
  }
  const code = r.status
  const reason = (j && (j.code || j.reason)) || ''
  const message = (j && (j.detail || j.error)) || `HTTP ${code}`

  if (code === 409 && reason === 'stale') {
    if (onStale) onStale()
    host.notify({ kind: 'warning', message: '文件已被改动，正在刷新…' })
    return { ok: false, code, reason, message: '文件已被改动，正在刷新…' }
  }
  if (!r.ok || (j && j.ok === false)) {
    host.notify({ kind: 'error', message })
    return { ok: false, code, reason, message }
  }
  return { ok: true, code, reason, data: j }
}

// ── 小部件 ──────────────────────────────────────────────────────────────────

function Bar({ pct, status }) {
  const color = (STATUS_STYLE[status] || STATUS_STYLE.todo).dot
  const w = Math.max(0, Math.min(100, pct || 0))
  return jsx('div', {
    className: 'h-1 w-full overflow-hidden rounded-full',
    style: { backgroundColor: 'var(--ui-stroke-secondary)' },
    children: jsx('div', {
      className: 'h-full rounded-full transition-all',
      style: { width: `${w}%`, backgroundColor: color }
    })
  })
}

function Empty({ children }) {
  return jsx('div', {
    className: 'px-2 py-3 text-[0.6875rem]',
    style: { color: 'var(--ui-text-quaternary)' },
    children
  })
}

/** 统一写按钮：readonly 时 disabled 并给 tooltip（任务 3 · C5）。 */
function ActBtn({ label, title, onClick, disabled, readonly, tone }) {
  const off = disabled || readonly
  const btn = jsx('button', {
    type: 'button',
    disabled: off,
    onClick: ev => {
      if (ev && ev.stopPropagation) ev.stopPropagation()
      if (!off) onClick()
    },
    className: cn(
      'shrink-0 rounded px-1.5 py-0.5 text-[0.6875rem]',
      'text-(--ui-text-quaternary) hover:bg-(--chrome-action-hover) hover:text-foreground',
      off && 'cursor-not-allowed opacity-40 hover:bg-transparent'
    ),
    style: tone === 'warn' ? { color: WARN } : undefined,
    children: label
  })
  const tip = readonly ? '只读模式（引导未完成）— 写操作已禁用' : (title || label)
  return jsx(Tip, { label: tip, children: btn })
}

// ── 左栏：分类 + 项目 ───────────────────────────────────────────────────────

function projectCategory(name) {
  const s = String(name || '')
  if (s.indexOf('🖥') === 0) return '软件开发'
  if (s.indexOf('🎬') === 0) return '媒体制作'
  if (s.indexOf('🏗') === 0) return '基础架构'
  if (s.indexOf('🤖') === 0) return '智能体'
  if (s.indexOf('👤') === 0 || s.indexOf('🧠') === 0) return '智库'
  return '其他'
}

function LeftCol({ data, selected, onSelect, stats }) {
  const projects = (data && data.projects) || []
  const unassignedN = stats.unassigned_sessions || 0

  const groups = useMemo(() => {
    const m = new Map()
    for (const p of projects) {
      const c = projectCategory(p.name)
      if (!m.has(c)) m.set(c, [])
      m.get(c).push(p)
    }
    return Array.from(m.entries())
  }, [projects])

  const isSel = (kind, slug) => Boolean(selected) && selected.kind === kind && (!slug || selected.slug === slug)

  return jsxs('div', {
    className: 'flex h-full w-[15rem] shrink-0 flex-col overflow-y-auto border-r',
    style: { borderColor: 'var(--ui-stroke-secondary)' },
    children: [
      jsx('div', {
        className: 'px-3 py-2 text-[0.6875rem] font-medium',
        style: { color: 'var(--ui-text-secondary)' },
        children: `项目 ${stats.projects || 0} · 会话 ${stats.sessions || 0}`
      }),

      jsx('div', {
        className: 'flex flex-col',
        children: groups.map(([cat, list]) => jsxs('div', {
          className: 'flex flex-col',
          children: [
            jsx('div', {
              className: 'px-3 pb-1 pt-2 text-[0.625rem]',
              style: { color: 'var(--ui-text-quaternary)' },
              children: `${cat} · ${list.length}`
            }),
            jsx('div', {
              children: list.map(p => {
                const on = isSel('project', p.slug)
                const flags = []
                if (p.task_doing) flags.push(`${p.task_doing} 进行`)
                if (p.task_blocked) flags.push(`${p.task_blocked} 阻塞`)
                return jsxs('div', {
                  className: cn('cursor-pointer px-3 py-1.5 text-[0.75rem]',
                    'hover:bg-(--chrome-action-hover)', on && 'bg-(--chrome-action-hover)'),
                  title: p.path || p.slug,
                  onClick: () => onSelect({ kind: 'project', slug: p.slug, path: p.path }),
                  children: [
                    jsxs('div', {
                      className: 'flex items-center gap-1.5',
                      children: [
                        jsx('span', {
                          className: 'min-w-0 flex-1 truncate',
                          style: { color: 'var(--ui-text-primary)' },
                          children: p.name
                        }),
                        p.task_blocked
                          ? jsx('span', {
                              className: 'h-1.5 w-1.5 shrink-0 rounded-full',
                              style: { backgroundColor: WARN }
                            })
                          : null
                      ]
                    }),
                    jsxs('div', {
                      className: 'mt-0.5 flex items-center gap-1 text-[0.625rem]',
                      style: { color: 'var(--ui-text-quaternary)' },
                      children: [
                        jsx('span', { children: `${p.session_count} 会话` }),
                        jsx('span', { children: `· ${p.task_count} 任务` }),
                        p.board_cards ? jsx('span', { children: `· ${p.board_cards} 卡` }) : null,
                        flags.length
                          ? jsx('span', { style: { color: 'var(--ui-text-tertiary)' }, children: flags.join(' ') })
                          : null
                      ]
                    })
                  ]
                }, p.slug)
              })
            })
          ]
        }, cat))
      }),

      jsxs('div', {
        className: 'mt-1 border-t pt-1',
        style: { borderColor: 'var(--ui-stroke-secondary)' },
        children: [
          jsx('div', {
            className: cn('cursor-pointer px-3 py-1.5 text-[0.75rem] hover:bg-(--chrome-action-hover)',
              isSel('unassigned') && 'bg-(--chrome-action-hover)'),
            onClick: () => onSelect({ kind: 'unassigned' }),
            children: jsxs('div', {
              className: 'flex items-center gap-1.5',
              children: [
                jsx('span', { className: 'flex-1', style: { color: 'var(--ui-text-primary)' }, children: '未归属' }),
                jsx('span', {
                  className: 'text-[0.625rem]',
                  style: { color: 'var(--ui-text-quaternary)' },
                  children: `${unassignedN} 会话`
                })
              ]
            })
          }),
          jsx('div', {
            className: 'px-3 pb-1 text-[0.625rem]',
            style: { color: 'var(--ui-text-quaternary)' },
            children: `${stats.orphan_tasks || 0} 任务（slug 不在项目表里）`
          })
        ]
      })
    ]
  })
}

// ── 中栏：会话 ──────────────────────────────────────────────────────────────

function sessionLabel(s) {
  if (s.title) return s.title
  if (s.cwd) {
    const parts = String(s.cwd).split(/[\\/]/).filter(Boolean)
    return parts.length ? parts[parts.length - 1] : s.cwd
  }
  return s.id ? String(s.id).slice(0, 16) : '（无标题会话）'
}

function fmtTime(epoch) {
  if (!epoch) return ''
  const d = new Date(epoch * 1000)
  const p = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

function MidCol({ data, selected, readonly, reload, projects }) {
  const [limit, setLimit] = useState(SESSION_PAGE)
  const [picking, setPicking] = useState(null)      // 正在归类的 session id

  useEffect(() => { setLimit(SESSION_PAGE) }, [selected && (selected.slug || selected.kind)])

  const view = useMemo(() => {
    if (!data) return { list: [], total: 0, title: '会话' }
    if (!selected) {
      const all = (data.assigned_sessions || []).concat(data.unassigned_sessions || [])
      return { list: all, total: all.length, title: '全部会话' }
    }
    if (selected.kind === 'unassigned') {
      const l = data.unassigned_sessions || []
      return { list: l, total: l.length, title: '未归属会话' }
    }
    const l = (data.assigned_sessions || []).filter(s => s.project === selected.slug)
    return { list: l, total: l.length, title: '会话' }
  }, [data, selected])

  const openSession = useCallback(sid => {
    try {
      host.openSession(sid, { intent: 'in-place' })
    } catch (e) {
      host.notify({ kind: 'error', message: `打开会话失败：${e}` })
    }
  }, [])

  const assign = useCallback(async (sid, slug) => {
    setPicking(null)
    if (!slug) return
    const r = await postJSON('/sessions/assign', { session_id: sid, project: slug, source: 'manual' }, reload)
    if (r.ok) {
      host.notify({ kind: 'info', message: `已归类到 ${slug}` })
      reload()
    }
  }, [reload])

  const createSession = useCallback(async () => {
    const res = await postJSON('/sessions/new', {
      cwd: selected && selected.path,
      title: selected ? `项目：${selected.slug}` : null
    }, reload)
    if (res.ok) {
      reload()
      const sid = res.data && res.data.session_id
      if (sid) openSession(sid)
    }
  }, [selected, reload, openSession])

  const shown = view.list.slice(0, limit)

  return jsxs('div', {
    className: 'flex h-full min-w-0 flex-1 flex-col overflow-hidden border-r',
    style: { borderColor: 'var(--ui-stroke-secondary)' },
    children: [
      jsxs('div', {
        className: 'flex shrink-0 items-center gap-2 border-b px-3 py-2',
        style: { borderColor: 'var(--ui-stroke-secondary)' },
        children: [
          jsx('div', {
            className: 'min-w-0 flex-1 truncate text-[0.75rem] font-medium',
            style: { color: 'var(--ui-text-primary)' },
            children: `${view.title} · ${view.total}`
          }),
          selected && selected.kind === 'project'
            ? jsx(ActBtn, {
                label: '＋ 新建会话',
                title: '在该项目目录下新建会话（cwd_explicit，建完回查）',
                readonly,
                onClick: createSession
              })
            : null
        ]
      }),

      jsx('div', {
        className: 'min-h-0 flex-1 overflow-y-auto py-1',
        children: shown.length === 0
          ? jsx(Empty, { children: '这里还没有会话' })
          : jsx('div', {
              children: shown.map(s => jsxs('div', {
                className: 'flex items-start gap-2 px-3 py-1.5 hover:bg-(--chrome-action-hover)',
                children: [
                  jsxs('div', {
                    className: 'min-w-0 flex-1 cursor-pointer',
                    onClick: () => openSession(s.id),
                    children: [
                      jsx('div', {
                        className: 'truncate text-[0.75rem]',
                        style: { color: 'var(--ui-text-primary)' },
                        children: sessionLabel(s)
                      }),
                      jsxs('div', {
                        className: 'mt-0.5 flex items-center gap-1 text-[0.625rem]',
                        style: { color: 'var(--ui-text-quaternary)' },
                        children: [
                          s.cwd
                            ? jsx('span', { className: 'truncate', title: s.cwd, children: s.cwd })
                            : jsx('span', { children: '（无 cwd）' }),
                          jsx('span', { children: `· ${fmtTime(s.last_activity)}` }),
                          s.via === 'manual'
                            ? jsx('span', { style: { color: 'var(--ui-text-tertiary)' }, children: '· 人工' })
                            : null
                        ]
                      })
                    ]
                  }),
                  selected && selected.kind === 'unassigned'
                    ? jsx('div', {
                        className: 'flex shrink-0 items-center gap-1',
                        children: picking === s.id
                          ? jsx('select', {
                              className: 'max-w-[9rem] rounded px-1 py-0.5 text-[0.625rem]',
                              style: {
                                backgroundColor: 'var(--ui-stroke-secondary)',
                                color: 'var(--ui-text-primary)'
                              },
                              value: '',
                              autoFocus: true,
                              onChange: ev => assign(s.id, ev.target.value),
                              children: [
                                jsx('option', { value: '', children: '选择项目…' }),
                                projects.map(p => jsx('option', { value: p.slug, children: p.name }, p.slug))
                              ]
                            })
                          : jsx(ActBtn, {
                              label: '归类',
                              title: '把该会话归到某个项目（只写 META.yaml，不改 cwd）',
                              readonly,
                              onClick: () => setPicking(s.id)
                            })
                      })
                    : null
                ]
              }, s.id))
            })
      }),

      view.total > limit
        ? jsx('div', {
            className: 'shrink-0 border-t px-3 py-1.5',
            style: { borderColor: 'var(--ui-stroke-secondary)' },
            children: jsx('button', {
              type: 'button',
              className: 'text-[0.6875rem] text-(--ui-text-quaternary) hover:text-foreground',
              onClick: () => setLimit(v => v + SESSION_PAGE * 4),
              children: `还有 ${view.total - limit} 条，点击多显示 ${SESSION_PAGE * 4} 条`
            })
          })
        : null
    ]
  })
}

// ── 右栏：任务树 ────────────────────────────────────────────────────────────

function Subtask({ task, s, readonly, onToggle }) {
  return jsxs('div', {
    className: cn('flex cursor-pointer items-start gap-2 rounded px-1.5 py-1 text-[0.75rem]',
      'hover:bg-(--chrome-action-hover)', readonly && 'cursor-not-allowed'),
    title: readonly ? '只读模式 — 写操作已禁用' : '点击勾选/取消',
    onClick: ev => {
      ev.stopPropagation()
      if (!readonly) onToggle(task, s)
    },
    children: [
      jsx('span', {
        className: 'mt-[1px] shrink-0',
        style: { color: s.done ? 'var(--ui-text-secondary)' : 'var(--ui-text-quaternary)' },
        children: s.done ? '✔' : '□'
      }),
      jsx('span', {
        className: cn('flex-1 leading-snug', s.done && 'line-through'),
        style: { color: s.done ? 'var(--ui-text-quaternary)' : 'var(--ui-text-secondary)' },
        children: `${s.id} ${s.title}`
      })
    ]
  })
}

function TaskCard({ t, readonly, onToggle }) {
  const [open, setOpen] = useState(t.status !== 'done')
  const st = STATUS_STYLE[t.status] || STATUS_STYLE.todo
  const isBlocked = t.blocker && t.blocker !== '无' && t.blocker !== '无。'

  return jsxs('div', {
    className: 'rounded-lg border p-2',
    style: { borderColor: 'var(--ui-stroke-secondary)' },
    children: [
      jsxs('div', {
        className: 'flex items-start gap-2',
        children: [
          jsx('span', {
            className: 'mt-[5px] h-2 w-2 shrink-0 rounded-full',
            style: { backgroundColor: st.dot }
          }),
          jsxs('div', {
            className: 'min-w-0 flex-1 cursor-pointer',
            onClick: () => setOpen(v => !v),
            children: [
              jsx('div', {
                className: 'text-[0.8125rem] font-medium leading-snug',
                style: { color: 'var(--ui-text-primary)' },
                children: `${t.id} ${t.title}`
              }),
              jsxs('div', {
                className: 'mt-0.5 flex items-center gap-1 text-[0.625rem]',
                style: { color: 'var(--ui-text-quaternary)' },
                children: [
                  jsx('span', { children: st.label }),
                  jsx('span', { children: `· ${t.progress || 0}%` }),
                  t.project ? jsx('span', { children: `· ${t.project}` }) : null
                ]
              })
            ]
          })
        ]
      }),

      jsx('div', { className: 'mt-1.5', children: jsx(Bar, { pct: t.progress, status: t.status }) }),

      isBlocked
        ? jsxs('div', {
            className: 'mt-1.5 rounded px-2 py-1 text-[0.625rem]',
            style: { backgroundColor: 'rgba(217,83,79,0.12)', color: 'var(--ui-text-secondary)' },
            children: ['⛔ ', t.blocker]
          })
        : null,

      open && t.subtasks && t.subtasks.length
        ? jsx('div', {
            className: 'mt-1 flex flex-col',
            children: t.subtasks.map(s => jsx(Subtask, { task: t, s, readonly, onToggle }, s.id))
          })
        : null,

      open && t.snapshot
        ? jsxs('div', {
            className: 'mt-1.5 rounded px-2 py-1 text-[0.625rem] leading-relaxed',
            style: { backgroundColor: 'var(--ui-stroke-secondary)', color: 'var(--ui-text-tertiary)' },
            children: ['快照：', t.snapshot]
          })
        : null
    ]
  })
}

function RightCol({ data, selected, readonly, reload, warnings }) {
  const view = useMemo(() => {
    if (!data) return { tasks: [], orphans: [], title: '任务', orphan: false }
    if (selected && selected.kind === 'project') {
      // 项目任务：后端把 slug 不在项目表里的任务单列在 orphan_tasks。
      // ⚠️ 真实数据里两条任务（T-001/T-002）的 proj 都是 hermes-cockpit，而 14 个项目里没有这个 slug——
      //    只按 slug 过滤会让它们在任何项目视图下都看不见，用户会以为任务丢了。
      //    所以这里分两组：本项目任务 + 全部未归属任务（后者始终带出来，标明来源）。
      const hit = (data.orphan_tasks || []).filter(t => t.project === selected.slug)
      const rest = (data.orphan_tasks || []).filter(t => t.project !== selected.slug)
      return { tasks: hit, orphans: rest, title: '任务', orphan: hit.length > 0 }
    }
    return { tasks: [], orphans: data.orphan_tasks || [], title: '未归属任务', orphan: true }
  }, [data, selected])

  const onToggle = useCallback(async (task, s) => {
    // 并发戳来自 /overview 的 stamp（(lstat, sha256) 二元组）。
    // 不带它后端一律 428 拒绝盲写；带错了 409 stale → postJSON 会提示并自动刷新。
    const stamp = data && data.stamp
    const r = await postJSON('/tasks/toggle', {
      id: task.id,
      subtask_id: s.id,
      done: !s.done,
      expect_mtime: stamp
    }, reload)
    if (r.ok) {
      reload()
    } else if (r.code === 428) {
      host.notify({ kind: 'error', message: '缺少文件戳：/overview 没下发 stamp，无法安全写入' })
    }
  }, [data, reload])

  return jsxs('div', {
    className: 'flex h-full w-[20rem] shrink-0 flex-col overflow-y-auto px-2 py-2',
    children: [
      warnings.length
        ? jsxs('div', {
            className: 'mb-2 shrink-0 rounded px-2 py-1.5 text-[0.6875rem]',
            style: { backgroundColor: 'rgba(217,83,79,0.12)', color: 'var(--ui-text-secondary)' },
            children: [
              jsx('div', { style: { color: WARN }, children: '兼容性提示' }),
              warnings.map((w, i) => jsx('div', { className: 'mt-0.5', children: w }, `w-${i}`))
            ]
          })
        : null,

      jsxs('div', {
        className: 'mb-1.5 flex shrink-0 items-center justify-between gap-2',
        children: [
          jsx('div', {
            className: 'text-[0.6875rem] font-medium',
            style: { color: 'var(--ui-text-secondary)' },
            children: `${view.title} · ${view.tasks.length}`
          }),
          view.orphan
            ? jsx('div', {
                className: 'text-[0.625rem]',
                style: { color: 'var(--ui-text-quaternary)' },
                children: 'slug 不匹配项目'
              })
            : null
        ]
      }),

      view.tasks.length === 0
        ? (view.orphans.length
            ? null
            : jsx(Empty, { children: '这里没有任务' }))
        : jsx('div', {
            className: 'flex flex-col gap-2',
            children: view.tasks.map(t => jsx(TaskCard, { t, readonly, onToggle }, t.id))
          }),

      // 未归属任务：proj 的 slug 不在项目表里。必须始终可见 ——
      // 否则这些任务在任何项目视图下都不出现，用户会以为任务丢了。
      view.orphans && view.orphans.length
        ? jsxs('div', {
            className: 'mt-2 flex flex-col gap-2',
            children: [
              jsxs('div', {
                className: 'flex items-center justify-between',
                children: [
                  jsx('div', {
                    className: 'text-[0.6875rem] font-medium',
                    style: { color: 'var(--ui-text-secondary)' },
                    children: `未归属任务 · ${view.orphans.length}`
                  }),
                  jsx('div', {
                    className: 'text-[0.625rem]',
                    style: { color: 'var(--ui-text-quaternary)' },
                    children: 'proj 不在项目表'
                  })
                ]
              }),
              jsx('div', {
                className: 'flex flex-col gap-2',
                children: view.orphans.map(t => jsx(TaskCard, { t, readonly, onToggle }, t.id))
              })
            ]
          })
        : null
    ]
  })
}

// ── 主页面：顶部总览条 + 三栏 ──────────────────────────────────────────────

/**
 * 首启引导卡片（缺陷 3 / GAP-01）。
 *
 * 后端 `POST /setup/init` 早就能用，但前端只有一个「只读（引导未完成）」提示，
 * **没有任何操作能完成引导** → D12 决策不可达，用户看到灰按钮却无路可走。
 * 这里给两个按钮：文案固定（任务书要求，不许自由发挥）。
 *
 * 失败分支复用 postJSON 的既有语义（409 stale 自动刷新 / 403 原样透出原因），
 * 不另写一套判断——两套判断迟早会打架。
 */
function SetupCard({ setup, reload }) {
  const [busy, setBusy] = useState(null)
  const [msg, setMsg] = useState(null)

  async function choose(mode) {
    if (busy) return
    setBusy(mode)
    setMsg(null)
    const r = await postJSON('/setup/init', { mode }, reload)
    setBusy(null)
    if (r.ok) {
      reload()                                  // 成功后重取，界面自己会切出只读态
      return
    }
    // 409 stale 已在 postJSON 里提示 + 自动 reload；这里只补一条不重复的说明
    setMsg(r.code === 409 && r.reason === 'stale'
      ? '引导状态已被改动，正在刷新…'
      : r.message)
  }

  const btn = (mode, label) =>
    jsx('button', {
      type: 'button',
      disabled: Boolean(busy),
      onClick: () => choose(mode),
      className: cn('rounded px-3 py-1.5 text-xs',
        'hover:bg-(--chrome-action-hover) hover:text-foreground'),
      style: {
        border: '1px solid var(--ui-stroke-secondary)',
        color: 'var(--ui-text-primary)',
        opacity: busy && busy !== mode ? 0.5 : 1
      },
      children: busy === mode ? `${label}…` : label
    })

  return jsxs('div', {
    className: 'flex h-full flex-col items-center justify-center gap-3 p-6 text-center',
    children: [
      jsx('div', {
        className: 'text-sm',
        style: { color: 'var(--ui-text-primary)' },
        children: '项目中枢还没初始化'
      }),
      jsx('div', {
        className: 'text-xs',
        style: { color: 'var(--ui-text-quaternary)' },
        children: `当前引导状态：${(setup && setup.status) || 'pending'}。选择一种方式开始（选定后不可反向切换）。`
      }),
      jsxs('div', {
        className: 'flex gap-2',
        children: [
          btn('fresh', '🆕 从零开始'),
          btn('imported', '📥 导入已有')
        ]
      }),
      msg ? jsx('div', { className: 'text-xs', style: { color: WARN }, children: msg }) : null
    ]
  })
}

function Cockpit({ readonly }) {
  const { data, err, reload } = useOverview(POLL_MS)
  const [selected, setSelected] = useState(null)
  const firstPick = useRef(false)

  // 首帧默认落在会话数最多的项目上，第一版必须一眼看到东西。
  //
  // 双保险：effect 负责常规路径；下面 render 时再兜一次，
  // 因为实测只靠 effect 会停在 selected=null，首屏一个写入口都不渲染。
  useEffect(() => {
    if (firstPick.current || !data || !data.projects) return
    firstPick.current = true
    let best = null
    for (const p of data.projects) {
      if (!best || p.session_count > best.session_count) best = p
    }
    if (best) setSelected({ kind: 'project', slug: best.slug, path: best.path })
  }, [data])

  // 兜底：数据已在但还没选中 → 本次渲染用同一个规则临时定一个
  const autoPick =
    selected ||
    (data && data.projects && data.projects.length
      ? (() => {
          let b = data.projects[0]
          for (const p of data.projects) if (p.session_count > b.session_count) b = p
          return { kind: 'project', slug: b.slug, path: b.path }
        })()
      : null)
  const eff = autoPick

  if (err && !data) {
    return jsxs('div', {
      className: 'flex h-full flex-col gap-1 p-4 text-xs',
      children: [
        jsx('div', { style: { color: 'var(--ui-text-secondary)' }, children: '读不到项目中枢数据' }),
        jsx('div', { style: { color: 'var(--ui-text-quaternary)' }, children: err })
      ]
    })
  }
  if (!data) {
    return jsx('div', {
      className: 'p-4 text-xs',
      style: { color: 'var(--ui-text-quaternary)' },
      children: '加载中…'
    })
  }

  const stats = data.stats || {}
  const warnings = data.warnings || []
  const projects = data.projects || []
  // C5/S8：引导未完成（setup.status !== 'done'）→ 后端会 409 scope_blocked，
  // 界面必须同步进只读态，不能等用户点了才失败。
  const isReadonly = Boolean(readonly) || Boolean(data.setup && data.setup.status !== 'done')

  // 缺陷 3 / GAP-01：引导未完成时**给界面一个真正的入口**。
  // 只把三栏画成灰的，用户没有任何操作能完成引导 → D12 决策不可达。
  if (data.setup && data.setup.status !== 'done') {
    return jsx(SetupCard, { setup: data.setup, reload })
  }

  const head = [
    `${stats.projects || 0} 项目`,
    `${stats.sessions || 0} 会话`,
    `${stats.tasks || 0} 任务`,
    `${stats.boards || 0} 看板`,
    `${stats.unassigned_sessions || 0} 未归属`
  ]

  return jsxs('div', {
    className: 'flex h-full flex-col overflow-hidden',
    children: [
      jsxs('div', {
        className: 'flex shrink-0 items-center gap-2 border-b px-3 py-1.5 text-[0.6875rem]',
        style: { borderColor: 'var(--ui-stroke-secondary)', color: 'var(--ui-text-tertiary)' },
        children: [
          jsx('span', { style: { color: 'var(--ui-text-primary)' }, children: '项目中枢' }),
          jsx('span', { style: { color: 'var(--ui-text-quaternary)' }, children: head.join(' · ') }),
          isReadonly ? jsx('span', { style: { color: WARN }, children: '· 只读（引导未完成）' }) : null,
          err ? jsx('span', { style: { color: WARN }, children: `· ${err}` }) : null
        ]
      }),

      jsxs('div', {
        className: 'flex min-h-0 flex-1 overflow-hidden',
        children: [
          jsx(LeftCol, { data, selected: eff, onSelect: setSelected, stats }),
          jsx(MidCol, { data, selected: eff, readonly: isReadonly, reload, projects }),
          jsx(RightCol, { data, selected: eff, readonly: isReadonly, reload, warnings })
        ]
      })
    ]
  })
}

function CockpitPage() {
  return jsx('div', { className: 'h-full', children: jsx(Cockpit, {}) })
}

function CockpitPane() {
  return jsx('div', {
    className: 'flex h-full flex-col overflow-hidden',
    children: jsx(Cockpit, { readonly: false })
  })
}

function CockpitChip() {
  const { data } = useOverview(8000)
  const st = (data && data.stats) || {}
  const bad = (st.orphan_tasks || 0) > 0
  return jsx(Tip, {
    label: '项目中枢',
    children: jsxs('button', {
      type: 'button',
      className: cn('inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem]',
        'text-(--ui-text-tertiary) hover:bg-(--chrome-action-hover) hover:text-foreground'),
      onClick: () => host.navigate('/cockpit'),
      children: [
        jsx('span', {
          className: 'h-1.5 w-1.5 rounded-full',
          style: { backgroundColor: bad ? WARN : 'var(--ui-accent)' }
        }),
        jsx('span', { children: `${st.projects || 0}/${st.sessions || 0}` })
      ]
    })
  })
}

export default {
  id: ID,
  name: '项目中枢',
  defaultEnabled: true,
  register(ctx) {
    // ① 右侧面板快捷视图
    ctx.register({
      id: 'cockpit-pane',
      area: 'panes',
      render: () => jsx(CockpitPane, {}),
      data: { placement: 'right', title: '项目中枢', width: '520px' }
    })

    // ② 状态栏小胶囊
    ctx.register({
      id: 'cockpit-chip',
      area: 'statusBar.right',
      render: () => jsx(CockpitChip, {})
    })

    // ③ 独立页面（三栏主视图）
    ctx.register({
      id: 'cockpit-page',
      area: ROUTES_AREA,
      data: { path: '/cockpit' },
      render: () => jsx(CockpitPage, {})
    })

    // ④ 侧栏入口
    ctx.register({
      id: 'cockpit-nav',
      area: SIDEBAR_NAV_AREA,
      data: { path: '/cockpit', label: '项目中枢', codicon: 'project' }
    })

    // ⑤ ⌘K 命令
    ctx.register({
      id: 'cockpit-cmd',
      area: PALETTE_AREA,
      data: { label: '打开项目中枢', run: () => host.navigate('/cockpit') }
    })
  }
}

/**
 * 自测钩子（__check.mjs 用）。
 *
 * 宿主只读 `export default`，这个具名导出不会挡任何东西；
 * 它把写操作入口暴露出来，好让自测喂真 409 / 403 / 200 响应验语义
 * （409 必须 reload、403 必须透出原因、200 必须零误报）。
 */
export const __writeProbe = { postJSON }

/**
 * 自测钩子（__check.mjs 用）：引导卡片。
 *
 * 缺陷 3 的验收点是「引导未完成时界面里真的有两个按钮能发 setup/init」，
 * 而 render() 出来的只是一棵普通对象树，断言可以直接在树上做。
 */
export const __setupProbe = { SetupCard, LABELS: ['🆕 从零开始', '📥 导入已有'] }

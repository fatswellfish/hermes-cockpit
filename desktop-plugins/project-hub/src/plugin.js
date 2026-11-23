/**
 * project-hub — desktop UI for the project hub plugin.
 *
 * Three columns on one screen: left "categories + projects", middle "sessions",
 * right "tasks".
 * All data goes through `/api/plugins/project-hub/*` (backend: dashboard/plugin_api.py).
 * The frontend never touches host databases directly, never changes cwd, and
 * cannot merge or split sessions.
 *
 * No JSX syntax (the host does not compile this file); UI is built with
 * jsx() / jsxs() calls only.
 * Colors come from theme CSS variables only; no hardcoded values (the red
 * warning reuses the #d9534f already used by the shipped todo-panel).
 *
 * All user-visible copy lives in `./i18n.js` and is resolved against the host's
 * active locale, so this file must never contain a display string literal.
 */

import { host, Tip, cn, atom, useValue, SIDEBAR_NAV_AREA, PALETTE_AREA, ROUTES_AREA } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'
import { useEffect, useState, useCallback, useRef, useMemo } from 'react'
import { MESSAGES } from './i18n.js'

const ID = 'project-hub'
const API = `/api/plugins/${ID}`

/**
 * Data channel for this desktop plugin.
 *
 * NEVER use a bare fetch against a relative path (e.g. the literal
 * /api/plugins/... inside quotes): the packaged desktop app serves its page
 * from the `file:///.../app.asar.unpacked/dist/index.html` protocol (there is
 * no dev server), so a relative path is resolved as a LOCAL FILE and fails
 * with `net::ERR_FILE_NOT_FOUND` while the backend is perfectly healthy
 * (visible in DevTools ▸ Network).
 *
 * The real channel is `window.hermesDesktop.api({ path, method?, body?, priority? })`,
 * exposed by preload (Electron IPC, independent of the page protocol; the app
 * core uses it too — src/api/client.ts hermesApi).
 *
 * This wrapper reshapes the IPC result into a fetch Response
 * (ok / status / json()) so every error branch above it
 * (409 stale / 403 out-of-scope / 428 missing stamp) stays untouched.
 */
async function apiFetch(path, { method = 'GET', body } = {}) {
  const bridge = globalThis.window && globalThis.window.hermesDesktop

  if (!bridge || typeof bridge.api !== 'function') {
    // Browser-tab form (no Electron preload): fall back to fetch. The page is
    // served over http/https there, so a relative path resolves correctly.
    const r = await fetch(path, {
      body: body === undefined ? undefined : JSON.stringify(body),
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      method
    })
    return { json: () => r.json(), ok: r.ok, status: r.status }
  }

  // IPC channel: non-2xx rejects, carrying status and body; reshape it back
  // into a Response-shaped object here.
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

/**
 * Translator handed down to components.
 *
 * `ctx.i18n.t` (see contrib/plugin.ts → createPluginI18n) reads the host's
 * active locale on every call, so a locale switch re-renders in the new
 * language without the plugin tracking anything itself. Before register()
 * runs — or under a test stub with no ctx.i18n — this falls back to English
 * so a component never renders `undefined` for copy.
 */
const FALLBACK_LOCALE = 'en'

function makeTranslator(i18n) {
  const fromBundle = (key, vars) => {
    const msg = MESSAGES[FALLBACK_LOCALE][key]
    if (msg === undefined) return key
    return vars
      ? String(msg).replace(/\{(\w+)\}/g, (whole, name) =>
          Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : whole
        )
      : msg
  }
  return function t(key, vars) {
    if (i18n && typeof i18n.t === 'function') {
      try {
        const out = i18n.t(key, vars)
        // A registered-but-incomplete bundle returns the raw key back; prefer
        // the bundled English over showing `mid.empty` in the UI.
        if (typeof out === 'string' && out !== key) return out
      } catch {
        // fall through to the bundled copy
      }
    }
    return fromBundle(key, vars)
  }
}

/**
 * Literal values TASKS.md uses to mean "no blocker".
 *
 * This is DATA, not display copy: it is what the Markdown author writes in the
 * blocker column, so it must not be routed through `t`. Kept in one place so a
 * project following a different convention only has to change one line.
 */
const BLOCKER_EMPTY = new Set(['', '无', '无。'])

/**
 * English-only translator shared by every component.
 *
 * Components take `t` as a prop so they stay pure, but a missing prop must not
 * crash the tree — the host renders panes during hydration and a self-test may
 * call a component directly. Fall back to the bundled English copy.
 */
const DEFAULT_T = makeTranslator(null)

/** Locale-agnostic status → dot colour. Labels are resolved through `t`. */
const STATUS_DOT = {
  doing: 'var(--ui-accent)',
  todo: 'var(--ui-text-quaternary)',
  done: 'var(--ui-text-secondary)',
  blocked: WARN
}

const SESSION_PAGE = 60          // Cap on middle-column renders; painting all 1479 at once freezes the UI

// ── data fetching ───────────────────────────────────────────────────────────

function useOverview(intervalMs, t = DEFAULT_T) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState(null)

  const load = useCallback(async () => {
    try {
      const r = await apiFetch(`${API}/overview`)
      const j = await r.json()
      // Two backend error shapes: REST {detail} and handle() {ok:false, error}
      if (!r.ok || (j && j.ok === false)) {
        setErr((j && (j.detail || j.error)) || t('common.readFailed', { status: r.status }))
      } else {
        setErr(null)
      }
      if (j && j.projects) setData(j)
    } catch (e) {
      setErr(String((e && e.message) || e))
    }
  }, [t])

  useEffect(() => {
    let dead = false
    const tick = () => { if (!dead) load() }
    tick()
    const timer = setInterval(tick, intervalMs || POLL_MS)
    return () => { dead = true; clearInterval(timer) }
  }, [load, intervalMs])

  return { data, err, reload: load }
}

/**
 * Single entry point for every write (task 2 · C4).
 *
 * Three hard semantics, none may be dropped:
 *   · 409 stale   → tell the user the file changed and auto-reload; never
 *                   silently discard their action
 *   · 403 out-of-scope → surface the backend's own reason verbatim
 *   · 428 no stamp     → a plugin defect, not a user error
 * Returns {ok:true, data} on success, {ok:false, code, reason, message} otherwise.
 */
async function postJSON(path, body, onStale, t = DEFAULT_T) {
  let r, j
  try {
    r = await apiFetch(`${API}${path}`, {
      method: 'POST',
      body: body || {}
    })
  } catch (e) {
    return { ok: false, code: 0, message: t('common.networkError', { error: (e && e.message) || e }) }
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
    host.notify({ kind: 'warning', message: t('common.stale') })
    return { ok: false, code, reason, message: t('common.stale') }
  }
  if (!r.ok || (j && j.ok === false)) {
    host.notify({ kind: 'error', message })
    return { ok: false, code, reason, message }
  }
  return { ok: true, code, reason, data: j }
}

// ── small widgets ───────────────────────────────────────────────────────────

function Bar({ pct, status }) {
  const color = STATUS_DOT[status] || STATUS_DOT.todo
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

/** One write-button shape: disabled with a tooltip while readonly (task 3 · C5). */
function ActBtn({ label, title, onClick, disabled, readonly, tone, t = DEFAULT_T }) {
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
  const tip = readonly ? t('common.readonlyTip') : (title || label)
  return jsx(Tip, { label: tip, children: btn })
}

// ── left column: categories + projects ─────────────────────────────────────

/**
 * Emoji prefix in the project name -> category message key.
 * Returns a key (not a label) so the caller renders it through `t`.
 */
function projectCategoryKey(name) {
  const s = String(name || '')
  if (s.indexOf('🖥') === 0) return 'category.software'
  if (s.indexOf('🎬') === 0) return 'category.media'
  if (s.indexOf('🏗') === 0) return 'category.infra'
  if (s.indexOf('🤖') === 0) return 'category.agent'
  if (s.indexOf('👤') === 0 || s.indexOf('🧠') === 0) return 'category.knowledge'
  return 'category.other'
}

function LeftCol({ data, selected, onSelect, stats, t = DEFAULT_T }) {
  const projects = (data && data.projects) || []
  const unassignedN = stats.unassigned_sessions || 0

  const groups = useMemo(() => {
    const m = new Map()
    for (const p of projects) {
      const c = projectCategoryKey(p.name)
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
        children: `${t('col.projects', { n: stats.projects || 0 })} · ${t('col.sessions', { n: stats.sessions || 0 })}`
      }),

      jsx('div', {
        className: 'flex flex-col',
        children: groups.map(([cat, list]) => jsxs('div', {
          className: 'flex flex-col',
          children: [
            jsx('div', {
              className: 'px-3 pb-1 pt-2 text-[0.625rem]',
              style: { color: 'var(--ui-text-quaternary)' },
              children: `${t(cat)} · ${list.length}`
            }),
            jsx('div', {
              children: list.map(p => {
                const on = isSel('project', p.slug)
                const flags = []
                if (p.task_doing) flags.push(t('col.fileFlags.doing', { n: p.task_doing }))
                if (p.task_blocked) flags.push(t('col.fileFlags.blocked', { n: p.task_blocked }))
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
                        jsx('span', { children: t('col.item.sessions', { n: p.session_count }) }),
                        jsx('span', { children: t('col.item.tasks', { n: p.task_count }) }),
                        p.board_cards ? jsx('span', { children: t('col.item.cards', { n: p.board_cards }) }) : null,
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
                jsx('span', { className: 'flex-1', style: { color: 'var(--ui-text-primary)' }, children: t('col.unassigned') }),
                jsx('span', {
                  className: 'text-[0.625rem]',
                  style: { color: 'var(--ui-text-quaternary)' },
                  children: t('col.sessions', { n: unassignedN })
                })
              ]
            })
          }),
          jsx('div', {
            className: 'px-3 pb-1 text-[0.625rem]',
            style: { color: 'var(--ui-text-quaternary)' },
            children: t('col.orphanTasks', { n: stats.orphan_tasks || 0 })
          })
        ]
      })
    ]
  })
}

// ── middle column: sessions ────────────────────────────────────────────────

function sessionLabel(s, t) {
  if (s.title) return s.title
  if (s.cwd) {
    const parts = String(s.cwd).split(/[\\/]/).filter(Boolean)
    return parts.length ? parts[parts.length - 1] : s.cwd
  }
  return s.id ? String(s.id).slice(0, 16) : t('mid.untitled')
}

function fmtTime(epoch) {
  if (!epoch) return ''
  const d = new Date(epoch * 1000)
  const p = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

function MidCol({ data, selected, readonly, reload, projects, onSelect, t = DEFAULT_T }) {
  const [limit, setLimit] = useState(SESSION_PAGE)
  const [picking, setPicking] = useState(null)      // session id currently being classified

  useEffect(() => { setLimit(SESSION_PAGE) }, [selected && (selected.slug || selected.kind)])

  const view = useMemo(() => {
    if (!data) return { list: [], total: 0, title: t('mid.sessions') }
    if (!selected) {
      const all = (data.assigned_sessions || []).concat(data.unassigned_sessions || [])
      return { list: all, total: all.length, title: t('mid.allSessions') }
    }
    if (selected.kind === 'unassigned') {
      const l = data.unassigned_sessions || []
      return { list: l, total: l.length, title: t('mid.unassignedSessions') }
    }
    // A selected session must keep its project's sibling list in this column:
    // otherwise clicking a session empties the middle column and the user can
    // neither go back nor see the other sessions. Map session -> project here.
    const slug = selected.kind === 'session' ? selected.project : selected.slug
    if (!slug) {
      const all = (data.assigned_sessions || []).concat(data.unassigned_sessions || [])
      return { list: all, total: all.length, title: t('mid.allSessions') }
    }
    const l = (data.assigned_sessions || []).filter(s => s.project === slug)
    return { list: l, total: l.length, title: t('mid.sessions') }
  }, [data, selected, t])

  const openSession = useCallback(sid => {
    try {
      host.openSession(sid, { intent: 'in-place' })
    } catch (e) {
      host.notify({ kind: 'error', message: t('mid.openFailed', { error: e }) })
    }
  }, [t])

  const assign = useCallback(async (sid, slug) => {
    setPicking(null)
    if (!slug) return
    const r = await postJSON('/sessions/assign', { session_id: sid, project: slug, source: 'manual' }, reload, t)
    if (r.ok) {
      host.notify({ kind: 'info', message: t('mid.classifiedTo', { slug }) })
      reload()
    }
  }, [reload])

  const createSession = useCallback(async () => {
    const res = await postJSON('/sessions/new', {
      cwd: selected && selected.path,
      title: selected ? t('mid.projectTitle', { slug: selected.slug }) : null
    }, reload, t)
    if (res.ok) {
      reload()
      const sid = res.data && res.data.session_id
      if (sid) openSession(sid)
    }
  }, [selected, reload, openSession, t])

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
                label: t('mid.newSession'),
                title: t('mid.newSessionHint'),
                readonly,
                onClick: createSession,
                t
              })
            : null
        ]
      }),

      jsx('div', {
        className: 'min-h-0 flex-1 overflow-y-auto py-1',
        children: shown.length === 0
          ? jsx(Empty, { children: t('mid.empty') })
          : jsx('div', {
              children: shown.map(s => jsxs('div', {
                className: 'flex items-start gap-2 px-3 py-1.5 hover:bg-(--chrome-action-hover)',
                children: [
                  jsxs('div', {
                    className: 'min-w-0 flex-1 cursor-pointer',
                    onClick: () => { onSelect({ kind: 'session', id: s.id, project: s.project, path: selected && selected.path }); openSession(s.id) },
                    children: [
                      jsx('div', {
                        className: 'truncate text-[0.75rem]',
                        style: { color: 'var(--ui-text-primary)' },
                        children: sessionLabel(s, t)
                      }),
                      jsxs('div', {
                        className: 'mt-0.5 flex items-center gap-1 text-[0.625rem]',
                        style: { color: 'var(--ui-text-quaternary)' },
                        children: [
                          s.cwd
                            ? jsx('span', { className: 'truncate', title: s.cwd, children: s.cwd })
                            : jsx('span', { children: t('mid.noCwd') }),
                          jsx('span', { children: `· ${fmtTime(s.last_activity)}` }),
                          s.via === 'manual'
                            ? jsx('span', { style: { color: 'var(--ui-text-tertiary)' }, children: t('mid.manual') })
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
                                jsx('option', { value: '', children: t('mid.pickProject') }),
                                projects.map(p => jsx('option', { value: p.slug, children: p.name }, p.slug))
                              ]
                            })
                          : jsx(ActBtn, {
                              label: t('mid.classify'),
                              title: t('mid.classifyHint'),
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
              title: t('mid.moreHint'),
              onClick: () => setLimit(v => v + SESSION_PAGE * 4),
              children: t('mid.more', { rest: view.total - limit, step: SESSION_PAGE * 4 })
            })
          })
        : null
    ]
  })
}

// ── right column: task tree ────────────────────────────────────────────────

function Subtask({ task, s, readonly, onToggle, t = DEFAULT_T }) {
  return jsxs('div', {
    className: cn('flex cursor-pointer items-start gap-2 rounded px-1.5 py-1 text-[0.75rem]',
      'hover:bg-(--chrome-action-hover)', readonly && 'cursor-not-allowed'),
    title: readonly ? t('right.readonlyHint') : t('right.toggleHint'),
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

function TaskCard({ task, readonly, onToggle, t = DEFAULT_T, tr = DEFAULT_T }) {
  const [open, setOpen] = useState(task.status !== 'done')
  const st = STATUS_DOT[task.status] || STATUS_DOT.todo
  const blockerText = String(task.blocker || '').trim()
  const isBlocked = !BLOCKER_EMPTY.has(blockerText)

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
            title: t(open ? 'right.collapseHint' : 'right.expandHint'),
            onClick: () => setOpen(v => !v),
            children: [
              jsx('div', {
                className: 'text-[0.8125rem] font-medium leading-snug',
                style: { color: 'var(--ui-text-primary)' },
                children: `${task.id} ${task.title}`
              }),
              jsxs('div', {
                className: 'mt-0.5 flex items-center gap-1 text-[0.625rem]',
                style: { color: 'var(--ui-text-quaternary)' },
                children: [
                  jsx('span', { children: tr(`status.${task.status || 'todo'}`) }),
                  jsx('span', { children: `· ${task.progress || 0}%` }),
                  task.project ? jsx('span', { children: `· ${task.project}` }) : null
                ]
              })
            ]
          })
        ]
      }),

      jsx('div', { className: 'mt-1.5', children: jsx(Bar, { pct: task.progress, status: task.status }) }),

      isBlocked
        ? jsxs('div', {
            className: 'mt-1.5 rounded px-2 py-1 text-[0.625rem]',
            style: { backgroundColor: 'rgba(217,83,79,0.12)', color: 'var(--ui-text-secondary)' },
            children: ['⛔ ', blockerText]
          })
        : null,

      open && task.subtasks && task.subtasks.length
        ? jsx('div', {
            className: 'mt-1 flex flex-col',
            children: task.subtasks.map(s => jsx(Subtask, { task, s, readonly, onToggle, t }, s.id))
          })
        : null,

      open && task.snapshot
        ? jsxs('div', {
            className: 'mt-1.5 rounded px-2 py-1 text-[0.625rem] leading-relaxed',
            style: { backgroundColor: 'var(--ui-stroke-secondary)', color: 'var(--ui-text-tertiary)' },
            children: [t('right.snapshot'), task.snapshot]
          })
        : null
    ]
  })
}

function RightCol({ data, selected, readonly, reload, warnings, t = DEFAULT_T }) {
  const view = useMemo(() => {
    if (!data) return { tasks: [], orphans: [], title: t('right.tasks'), orphan: false, scope: 'none' }
    const all = data.tasks || []

    // Three states, matching the project -> session -> todo cascade:
    //   project  a project is selected -> every task in it (all sessions) plus
    //            the unassigned ones, which are always shown
    //   session  a session is selected -> only the tasks that session raised,
    //            which is the third level of the cascade
    //   none     nothing selected      -> unassigned tasks
    //
    // Session matching is exact: a task stores the full session id and the UI
    // row carries the same one. An earlier version only filtered by slug, so
    // clicking a session left this column unchanged and the per-session tasks
    // could not be reached at all.
    if (selected && selected.kind === 'session') {
      const sid = selected.id
      const hit = all.filter(x => x.session && x.session === sid)
      // Tasks with no session id fall back to their project (established rule).
      const fallback = selected.project
        ? all.filter(x => (!x.session) && x.project === selected.project)
        : []
      return {
        tasks: hit.concat(fallback),
        orphans: [],
        title: t('right.sessionTasks'),
        orphan: false,
        scope: 'session'
      }
    }

    if (selected && selected.kind === 'project') {
      const hit = all.filter(x => x.project === selected.slug)
      // Unassigned tasks always show, even while a project is selected: real
      // data has tasks carrying a session but no proj (their session cwd is the
      // home directory, so no project claims them). Filtering them out here made
      // every task vanish from the UI and the user thought the list was lost.
      const ownIds = {}
      for (const x of hit) ownIds[x.id] = true
      const rest = (data.orphan_tasks || []).filter(x => !ownIds[x.id])
      return { tasks: hit, orphans: rest, title: t('right.tasks'), orphan: false, scope: 'project' }
    }

    return { tasks: [], orphans: data.orphan_tasks || [], title: t('right.orphanTasks'), orphan: true, scope: 'none' }
  }, [data, selected, t])

  const onToggle = useCallback(async (task, s) => {
    // The concurrency stamp comes from /overview's `stamp` ((lstat, sha256)).
    // Without it the backend refuses blind writes with 428; a wrong one yields
    // 409 stale, and postJSON notifies and auto-reloads.
    const stamp = data && data.stamp
    const r = await postJSON('/tasks/toggle', {
      id: task.id,
      subtask_id: s.id,
      done: !s.done,
      expect_mtime: stamp
    }, reload, t)
    if (r.ok) {
      reload()
    } else if (r.code === 428) {
      host.notify({ kind: 'error', message: t('right.missingStamp') })
    }
  }, [data, reload, t])

  return jsxs('div', {
    className: 'flex h-full w-[20rem] shrink-0 flex-col overflow-y-auto px-2 py-2',
    children: [
      warnings.length
        ? jsxs('div', {
            className: 'mb-2 shrink-0 rounded px-2 py-1.5 text-[0.6875rem]',
            style: { backgroundColor: 'rgba(217,83,79,0.12)', color: 'var(--ui-text-secondary)' },
            children: [
              jsx('div', { style: { color: WARN }, children: t('right.compatWarning') }),
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
                children: t('right.slugMismatch')
              })
            : null
        ]
      }),

      view.tasks.length === 0
        ? (view.orphans.length
            ? null
            : jsx(Empty, { children: t('right.empty') }))
        : jsx('div', {
            className: 'flex flex-col gap-2',
            children: view.tasks.map(task => jsx(TaskCard, { task, readonly, onToggle, t, tr: t }, task.id))
          }),

      // Unassigned tasks: proj's slug is not in the project table. These must
      // always be visible — otherwise they vanish from every project view and
      // the user concludes their tasks are gone.
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
                    children: `${t('right.orphanTasks')} · ${view.orphans.length}`
                  }),
                  jsx('div', {
                    className: 'text-[0.625rem]',
                    style: { color: 'var(--ui-text-quaternary)' },
                    children: t('right.orphanHint')
                  })
                ]
              }),
              jsx('div', {
                className: 'flex flex-col gap-2',
                children: view.orphans.map(task => jsx(TaskCard, { task, readonly, onToggle, t, tr: t }, task.id))
              })
            ]
          })
        : null
    ]
  })
}

// ── main view: overview bar + three columns ────────────────────────────────

/**
 * First-run onboarding card (defect 3 / GAP-01).
 *
 * `POST /setup/init` already worked, but the UI only showed a
 * "read-only (onboarding incomplete)" hint with NO action that could finish
 * onboarding — decision D12 was unreachable. These two buttons close that gap.
 *
 * Failure handling reuses postJSON's existing semantics (409 stale auto-
 * reloads, 403 surfaces the backend reason verbatim) instead of a second set
 * of checks — two sets always drift apart eventually.
 */
function SetupCard({ setup, reload, t = DEFAULT_T }) {
  const [busy, setBusy] = useState(null)
  const [msg, setMsg] = useState(null)

  async function choose(mode) {
    if (busy) return
    setBusy(mode)
    setMsg(null)
    const r = await postJSON('/setup/init', { mode }, reload, t)
    setBusy(null)
    if (r.ok) {
      reload()                                  // refetch on success; the view leaves read-only itself
      return
    }
    // postJSON already notified and reloaded on 409 stale; only add a
    // non-duplicating note here.
    setMsg(r.code === 409 && r.reason === 'stale'
      ? t('setup.changed')
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
        children: t('setup.title')
      }),
      jsx('div', {
        className: 'text-xs',
        style: { color: 'var(--ui-text-quaternary)' },
        children: t('setup.body', { status: (setup && setup.status) || 'pending' })
      }),
      jsxs('div', {
        className: 'flex gap-2',
        children: [
          btn('fresh', t('setup.fresh')),
          btn('imported', t('setup.imported'))
        ]
      }),
      msg ? jsx('div', { className: 'text-xs', style: { color: WARN }, children: msg }) : null
    ]
  })
}

/**
 * Settings page — the one official home for plugin preferences.
 *
 * The SDK is explicit about this: "Don't build a preferences dialog, pane, or
 * sidebar row for settings." Settings → Plugins is where a desktop plugin is
 * allowed to put its own knobs, so this component is registered through
 * ctx.registerSettingsPage and never linked from the cockpit view.
 *
 * Two knobs, both persisted through ctx.storage (namespaced hermes.plugin.<id>.*):
 *   · refreshSeconds — poll interval for /overview (default 4s)
 *   · confirmWrites  — ask before ticking a subtask (default off)
 */
function SettingsPage({ t = DEFAULT_T, settings, setSettings, storage }) {
  const cur = settings || {}
  const secs = cur.refreshSeconds || Math.round(POLL_MS / 1000)
  const confirmWrites = cur.confirmWrites === true

  const save = (patch) => {
    const next = Object.assign({}, cur, patch)
    setSettings(next)
    try {
      if (storage && typeof storage.set === 'function') storage.set('settings', next)
    } catch (e) {
      // Storage is best-effort: a failed write must not break the settings page.
    }
  }

  const row = (labelKey, hintKey, control) => jsxs('div', {
    className: 'flex items-start justify-between gap-4 border-b border-(--ui-border) py-3',
    children: [
      jsxs('div', {
        className: 'min-w-0',
        children: [
          jsx('div', { className: 'text-[0.8125rem] font-medium', children: t(labelKey) }),
          jsx('div', {
            className: 'mt-0.5 text-[0.75rem]',
            style: { color: 'var(--ui-text-tertiary)' },
            children: t(hintKey)
          })
        ]
      }),
      jsx('div', { className: 'shrink-0', children: control })
    ]
  })

  const choices = [2, 4, 8, 15, 30]
  const secBtn = (s) => {
    const on = secs === s
    return jsx('button', {
      type: 'button',
      'data-refresh': String(s),
      onClick: () => save({ refreshSeconds: s }),
      className: cn(
        'rounded px-2 py-1 text-[0.75rem]',
        on ? 'bg-(--ui-bg-selected)' : 'hover:bg-(--chrome-action-hover)'
      ),
      style: { color: on ? 'var(--ui-text-primary)' : 'var(--ui-text-tertiary)' },
      children: t('settings.seconds', { n: s })
    })
  }

  const toggle = jsx('button', {
    type: 'button',
    'data-confirm-writes': confirmWrites ? 'on' : 'off',
    onClick: () => save({ confirmWrites: !confirmWrites }),
    className: cn(
      'rounded px-2 py-1 text-[0.75rem]',
      confirmWrites ? 'bg-(--ui-bg-selected)' : 'hover:bg-(--chrome-action-hover)'
    ),
    style: { color: confirmWrites ? 'var(--ui-text-primary)' : 'var(--ui-text-tertiary)' },
    children: confirmWrites ? t('common.on') : t('common.off')
  })

  return jsxs('div', {
    className: 'flex max-w-2xl flex-col gap-1 p-4 text-sm',
    children: [
      jsx('div', { className: 'text-[0.9375rem] font-semibold', children: t('settings.title') }),
      jsx('div', {
        className: 'mb-2 text-[0.75rem]',
        style: { color: 'var(--ui-text-tertiary)' },
        children: t('settings.intro')
      }),
      row('settings.refresh', 'settings.refreshHint',
        jsx('div', { className: 'flex gap-1', children: choices.map(secBtn) })),
      row('settings.confirmWrites', 'settings.confirmHint', toggle),
      jsxs('div', {
        className: 'mt-3 rounded border border-(--ui-border) p-2 text-[0.6875rem]',
        style: { color: 'var(--ui-text-quaternary)' },
        children: [
          jsx('div', { children: t('settings.dataSource') }),
          jsx('code', { children: 'TASKS.agent.md · TASKS.work.md · TASKS.home.md' })
        ]
      })
    ]
  })
}

function Cockpit({ readonly, t = DEFAULT_T }) {
  const { data, err, reload } = useOverview(POLL_MS, t)
  const [selected, setSelected] = useState(null)
  const firstPick = useRef(false)

  // First paint defaults to the project with the most sessions: the first
  // version has to show something at a glance.
  //
  // Belt and braces: the effect covers the normal path, and the render-time
  // fallback below covers the case where the effect alone leaves
  // selected=null, which measured as "no write entry point on first paint".
  useEffect(() => {
    if (firstPick.current || !data || !data.projects) return
    firstPick.current = true
    let best = null
    for (const p of data.projects) {
      if (!best || p.session_count > best.session_count) best = p
    }
    if (best) setSelected({ kind: 'project', slug: best.slug, path: best.path })
  }, [data])

  // Fallback: data arrived but nothing is selected yet — pick one for this
  // render using the same rule.
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
        jsx('div', { style: { color: 'var(--ui-text-secondary)' }, children: t('common.loadFailed') }),
        jsx('div', { style: { color: 'var(--ui-text-quaternary)' }, children: err })
      ]
    })
  }
  if (!data) {
    return jsx('div', {
      className: 'p-4 text-xs',
      style: { color: 'var(--ui-text-quaternary)' },
      children: t('common.loading')
    })
  }

  const stats = data.stats || {}
  const warnings = data.warnings || []
  const projects = data.projects || []
  // C5/S8: onboarding incomplete (setup.status !== 'done') makes the backend
  // answer 409 scope_blocked, so the view must go read-only up front instead
  // of failing only once the user clicks.
  const isReadonly = Boolean(readonly) || Boolean(data.setup && data.setup.status !== 'done')

  // Defect 3 / GAP-01: when onboarding is incomplete the view needs a REAL
  // entry point. Greying out the columns left the user with no way to finish
  // onboarding, making decision D12 unreachable.
  if (data.setup && data.setup.status !== 'done') {
    return jsx(SetupCard, { setup: data.setup, reload, t })
  }

  const head = [
    t('stat.projects', { n: stats.projects || 0 }),
    t('stat.sessions', { n: stats.sessions || 0 }),
    t('stat.tasks', { n: stats.tasks || 0 }),
    t('stat.boards', { n: stats.boards || 0 }),
    t('stat.unassigned', { n: stats.unassigned_sessions || 0 })
  ]

  return jsxs('div', {
    className: 'flex h-full flex-col overflow-hidden',
    children: [
      jsxs('div', {
        className: 'flex shrink-0 items-center gap-2 border-b px-3 py-1.5 text-[0.6875rem]',
        style: { borderColor: 'var(--ui-stroke-secondary)', color: 'var(--ui-text-tertiary)' },
        children: [
          jsx('span', { style: { color: 'var(--ui-text-primary)' }, children: t('plugin.name') }),
          jsx('span', { style: { color: 'var(--ui-text-quaternary)' }, children: head.join(' · ') }),
          isReadonly ? jsx('span', { style: { color: WARN }, children: t('common.readonlyBadge') }) : null,
          err ? jsx('span', { style: { color: WARN }, children: `· ${err}` }) : null
        ]
      }),

      jsxs('div', {
        className: 'flex min-h-0 flex-1 overflow-hidden',
        children: [
          jsx(LeftCol, { data, selected: eff, onSelect: setSelected, stats, t }),
          jsx(MidCol, { data, selected: eff, readonly: isReadonly, reload, projects, onSelect: setSelected, t }),
          jsx(RightCol, { data, selected: eff, readonly: isReadonly, reload, warnings, t })
        ]
      })
    ]
  })
}

function CockpitPage({ t = DEFAULT_T }) {
  return jsx('div', { className: 'h-full', children: jsx(Cockpit, { t }) })
}

function CockpitChip({ t = DEFAULT_T }) {
  const { data } = useOverview(8000, t)
  const st = (data && data.stats) || {}
  const bad = (st.orphan_tasks || 0) > 0
  return jsx(Tip, {
    label: t('plugin.name'),
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
  // Read by the host at registration time, before any locale context exists,
  // so it cannot be localized here (and `id` must stay the ASCII slug because
  // the host derives the API mount path from the manifest). The sidebar and
  // palette labels come from `plugin.name` via `t` instead, re-registered in
  // `register` when the locale changes.
  name: 'Project Hub',
  defaultEnabled: true,
  register(ctx) {
    // No `area: 'panes'` registration on purpose.
    // The three columns need ~900px; squeezed into the 520px right rail they
    // fight todo-panel and the file tree and the user ends up seeing nothing —
    // the page was never broken, it was compressed away. The full-width view
    // is the /cockpit route reached from the sidebar entry below.
    // To bring back a right-rail form, write a separate width-aware compact
    // component; do not reuse the three-column Cockpit here.

    // Locale bundles: ctx.i18n.t resolves against the host's active locale,
    // so every label below follows the app's UI language automatically.
    if (ctx.i18n && typeof ctx.i18n.register === 'function') {
      ctx.i18n.register(MESSAGES)
    }
    const t = makeTranslator(ctx.i18n)

    // ① status-bar chip — its text is rendered by React on every tick, so it
    //    re-reads `t` on its own and needs no re-registration.
    ctx.register({
      id: 'cockpit-chip',
      area: 'statusBar.right',
      render: () => jsx(CockpitChip, { t })
    })

    // ② full-width route page (the three-column main view)
    ctx.register({
      id: 'cockpit-page',
      area: ROUTES_AREA,
      data: { path: '/cockpit' },
      render: () => jsx(CockpitPage, { t })
    })

    // ③ settings page — Settings → Plugins. The SDK forbids putting plugin
    //    preferences anywhere else (no dialog, no pane, no sidebar row), so
    //    this is the only correct home for the refresh interval and the
    //    write-confirmation toggle. Read back through ctx.storage on load.
    let savedSettings = {}
    try {
      if (ctx.storage && typeof ctx.storage.get === 'function') {
        savedSettings = ctx.storage.get('settings', {}) || {}
      }
    } catch (e) {
      savedSettings = {}
    }
    const settingsAtom = atom(savedSettings)
    if (typeof ctx.registerSettingsPage === 'function') {
      ctx.registerSettingsPage({
        id: 'settings',
        title: t('plugin.name'),
        icon: 'settings-gear',
        render: () => jsx(SettingsPage, {
          t,
          settings: useValue(settingsAtom),
          setSettings: (next) => { settingsAtom.set(next) },
          storage: ctx.storage
        })
      })
    }

    // ④ sidebar entry + ⑤ command palette — their labels are read ONCE by the
    //    host at registration time, so a contributor registered in English
    //    stays English after the user switches the UI language. The host's
    //    hook for this is ctx.i18n.onLocaleChange: dispose the contribution and
    //    register it again with the new `t`. (Built-in plugins do exactly this
    //    — see apps/desktop/src/plugins/kanban/plugin.tsx registerLabels.)
    const navContribs = (tr) => [
      {
        id: 'cockpit-nav',
        area: SIDEBAR_NAV_AREA,
        data: { path: '/cockpit', label: tr('plugin.name'), codicon: 'project' }
      },
      {
        id: 'cockpit-cmd',
        area: PALETTE_AREA,
        data: { label: tr('plugin.open'), run: () => host.navigate('/cockpit') }
      }
    ]

    let disposeLabels = ctx.registerMany(navContribs(t))
    if (ctx.i18n && typeof ctx.i18n.onLocaleChange === 'function') {
      ctx.i18n.onLocaleChange(() => {
        // Rebuild the translator too: the old one closed over the previous
        // locale. Disposing first keeps the host from showing two entries.
        if (typeof disposeLabels === 'function') disposeLabels()
        disposeLabels = ctx.registerMany(navContribs(makeTranslator(ctx.i18n)))
      })
    }
  }
}

/**
 * Test hook for __check.mjs.
 *
 * The host only reads `export default`, so a named export here shadows
 * nothing; it exposes the write entry point so the self-test can feed real
 * 409 / 403 / 200 responses and assert the semantics (409 must reload, 403
 * must surface the reason verbatim, 200 must not reload).
 */
export const __writeProbe = { postJSON }

/**
 * Test hook for __check.mjs: the onboarding card.
 *
 * The acceptance point is "onboarding incomplete renders two buttons that
 * really POST setup/init", and render() yields a plain object tree the test
 * can assert on directly.
 */
export const __setupProbe = { SetupCard }

/**
 * Test hook for __check.mjs: the settings page component.
 *
 * ctx.registerSettingsPage keeps render() as a closure, and the self-test's jsx
 * stub drops the component reference from the element it builds, so the test
 * cannot reach the component through the registry. Exporting it lets the suite
 * call it directly and assert the real controls and the storage write.
 */
export const __settingsProbe = { SettingsPage }

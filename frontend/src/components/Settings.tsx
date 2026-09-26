import { useEffect, useState } from 'react'
import { useStore } from '../state/store'
import { apiFetch } from '../lib/api'

/**
 * Slots that fall back to another slot when left blank (mirrors
 * backend/jarvis/models/registry.py). Shown as placeholder text so an empty
 * field reads as "deliberately inherited" rather than "broken".
 */
const SLOT_DEFERS_TO: Record<string, string | undefined> = {
  reasoning: 'general',
  specialist: 'reasoning',
}

/**
 * Configuration.
 *
 * Every field here maps to a value in ~/JARVIS/config/config.json. Nothing in
 * the application reads a hard-coded model name, voice or path, so changing a
 * value here is enough — no restart, no code edit.
 */
export function Settings() {
  const show = useStore((s) => s.showSettings)
  const setShow = useStore((s) => s.setShowSettings)
  const status = useStore((s) => s.status)
  const config = status?.config
  const [draft, setDraft] = useState<any>(null)
  const [saving, setSaving] = useState(false)
  const [voices, setVoices] = useState<{ name: string; locale: string }[]>([])
  const [models, setModels] = useState<string[]>([])
  const [skills, setSkills] = useState<SkillRow[]>([])
  const [audits, setAudits] = useState<number | null>(null)

  useEffect(() => {
    if (show && config) setDraft(JSON.parse(JSON.stringify(config)))
  }, [show, config])

  useEffect(() => {
    if (!show) return
    apiFetch('/api/voice/voices').then((r) => r.json()).then((d) => setVoices(d.voices ?? [])).catch(() => {})
    apiFetch('/api/models').then((r) => r.json()).then((d) => {
      const installed = Object.values(d.providers ?? {}).flatMap((p: any) => p.models ?? [])
      setModels(installed as string[])
    }).catch(() => {})
    apiFetch('/api/skills').then((r) => r.json()).then((d) => setSkills(d.skills ?? [])).catch(() => {})
    apiFetch('/api/audit').then((r) => r.json()).then((d) => setAudits((d.records ?? []).length))
      .catch(() => {})
  }, [show])

  const forget = async (id: string) => {
    const response = await apiFetch(`/api/skills/${encodeURIComponent(id)}`, { method: 'DELETE' })
    if (response.ok) setSkills((current) => current.filter((skill) => skill.id !== id))
  }

  if (!show || !draft) return null

  const set = (path: string[], value: any) => {
    setDraft((current: any) => {
      const next = { ...current }
      let cursor = next
      for (const key of path.slice(0, -1)) {
        cursor[key] = { ...cursor[key] }
        cursor = cursor[key]
      }
      cursor[path[path.length - 1]] = value
      return next
    })
  }

  const save = async () => {
    setSaving(true)
    try {
      // Only the fields actually touched in this sheet, not the whole
      // draft: `draft` is a snapshot taken when the sheet opened, so
      // sending it whole would silently revert anything the backend
      // changed on its own since then (a config-version migration moving
      // an old default forward, another client's edit) back to what it
      // was when this sheet was opened — not what the user asked for.
      const changes = diffConfig(config, draft)
      if (changes) {
        await apiFetch('/api/config', {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(stripSecrets(changes)),
        })
      }
      setShow(false)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="sheet" role="dialog" aria-modal="true">
      <div className="sheet__card">
        <header className="sheet__header">
          <h2>Configuration</h2>
          <button className="sheet__close" onClick={() => setShow(false)} aria-label="Close">×</button>
        </header>

        <div className="sheet__body">
          <Group title="Voice">
            <Field label="Wake word">
              <input value={draft.voice.wake_word}
                     onChange={(e) => set(['voice', 'wake_word'], e.target.value)} />
            </Field>
            <Field label="Wake engine">
              <select value={draft.voice.wake_engine}
                      onChange={(e) => set(['voice', 'wake_engine'], e.target.value)}>
                <option value="openwakeword">openWakeWord (neural, offline)</option>
                <option value="whisper">Whisper keyword spotting</option>
                <option value="off">Off</option>
              </select>
            </Field>
            <Field label="Speech voice">
              <select value={draft.voice.tts_voice}
                      onChange={(e) => set(['voice', 'tts_voice'], e.target.value)}>
                <option value={draft.voice.tts_voice}>{draft.voice.tts_voice}</option>
                {voices.filter((v) => v.name !== draft.voice.tts_voice).map((v) => (
                  <option key={v.name + v.locale} value={v.name}>{v.name} · {v.locale}</option>
                ))}
              </select>
            </Field>
            <Field label={`Speech rate — ${draft.voice.tts_rate} wpm`}>
              <input type="range" min={120} max={260} value={draft.voice.tts_rate}
                     onChange={(e) => set(['voice', 'tts_rate'], Number(e.target.value))} />
            </Field>
            <Field label="Whisper model">
              <select value={draft.voice.stt_model}
                      onChange={(e) => set(['voice', 'stt_model'], e.target.value)}>
                <option value="">Automatic — best for this Mac</option>
                {['tiny.en', 'base.en', 'small.en', 'medium.en', 'large-v3-turbo', 'large-v3'].map((m) => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </select>
            </Field>
            <Toggle label="Voice enabled" value={draft.voice.enabled}
                    onChange={(v) => set(['voice', 'enabled'], v)} />
            <Toggle label="Stop speaking when I start talking" value={draft.voice.barge_in}
                    onChange={(v) => set(['voice', 'barge_in'], v)} />
          </Group>

          <Group title="Models">
            <Field label="Ollama endpoint">
              <input value={draft.models.providers.ollama.base_url}
                     onChange={(e) => set(['models', 'providers', 'ollama', 'base_url'], e.target.value)} />
            </Field>
            {(['fast', 'general', 'reasoning', 'vision', 'specialist'] as const).map((slot) => (
              <Field key={slot} label={`${slot} model`}>
                <input list="installed-models" value={draft.models[slot]?.model ?? ''}
                       placeholder={SLOT_DEFERS_TO[slot] ? `uses the ${SLOT_DEFERS_TO[slot]} model` : ''}
                       onChange={(e) => set(['models', slot, 'model'], e.target.value)} />
              </Field>
            ))}
            <datalist id="installed-models">
              {models.map((m) => <option key={m} value={m} />)}
            </datalist>
          </Group>

          <Group title="Behaviour">
            <Field label="Address me as">
              <input value={draft.personality.address_user_as}
                     onChange={(e) => set(['personality', 'address_user_as'], e.target.value)} />
            </Field>
            <Field label="Workspace">
              <input value={draft.workspace}
                     onChange={(e) => set(['workspace'], e.target.value)} />
            </Field>
            <Field label="Confirm before">
              <select value={draft.security.always_confirm.join(',')}
                      onChange={(e) => set(['security', 'always_confirm'], e.target.value.split(',').filter(Boolean))}>
                <option value="high">High-risk actions only</option>
                <option value="medium,high">Medium and high risk</option>
                <option value="low,medium,high">Everything</option>
              </select>
            </Field>
            <Toggle label="Allow shell commands" value={draft.security.allow_shell}
                    onChange={(v) => set(['security', 'allow_shell'], v)} />
            <Toggle label="Allow screen capture" value={draft.security.allow_screen_capture}
                    onChange={(v) => set(['security', 'allow_screen_capture'], v)} />
          </Group>

          <Group title="Safety">
            <Field label="Ask me before">
              <select value={draft.security.autonomy ?? 'consequential_only'}
                      onChange={(e) => set(['security', 'autonomy'], e.target.value)}>
                <option value="consequential_only">Paying, ordering, sending, deleting, installing</option>
                <option value="confirm_start">…and before each errand starts</option>
                <option value="confirm_each_step">Every step that changes anything</option>
              </select>
            </Field>
            <ListField label="Never read or operate these apps" items={draft.security.blocked_apps ?? []}
                       onChange={(v) => set(['security', 'blocked_apps'], v)} />
            <ListField label="…these windows (App: part of its title)"
                       items={draft.security.blocked_windows ?? []}
                       onChange={(v) => set(['security', 'blocked_windows'], v)} />
            <ListField label="…or these sites (“bank” matches any bank)"
                       items={draft.security.blocked_sites ?? []}
                       onChange={(v) => set(['security', 'blocked_sites'], v)} />
            <Toggle label="Keep a record of every action" value={draft.security.audit ?? true}
                    onChange={(v) => set(['security', 'audit'], v)} />
            <Toggle label="With a picture of the page after each action"
                    value={draft.security.audit_screenshots ?? false}
                    onChange={(v) => set(['security', 'audit_screenshots'], v)} />
            <p className="sheet__hint">
              Records are kept for {draft.security.audit_days ?? 30} days in {draft.workspace}/audit
              {audits ? ` — ${audits} recent` : ''}. JARVIS never types passwords or card details.
            </p>
          </Group>

          <Group title="Browsers">
            <Toggle label="Run errands in JARVIS Chrome" value={draft.browser?.jarvis_browser ?? true}
                    onChange={(v) => set(['browser', 'jarvis_browser'], v)} />
            <ListField label="Sites always in one browser (site = everyday or jarvis)"
                       items={Object.entries(draft.browser?.site_overrides ?? {}).map(([k, v]) => `${k} = ${v}`)}
                       onChange={(lines) => set(['browser', 'site_overrides'], Object.fromEntries(
                         lines.map((line) => line.split('=').map((part) => part.trim()))
                           .filter(([site, where]) => site && (where === 'everyday' || where === 'jarvis'))))} />
            <p className="sheet__hint">
              Your own browser is used for the page you're on; errands that go somewhere run in a
              separate JARVIS Chrome window, so your tabs are never touched.
            </p>
          </Group>

          <Group title="Privacy and free cloud models">
            <ListField label="Keep on this Mac (local models only)"
                       items={draft.models?.cloud_exclusions ?? []}
                       onChange={(v) => set(['models', 'cloud_exclusions'], v)} />
            <ListField label="Try these first when operating (provider: model)"
                       items={(draft.models?.operator?.chain ?? []).map((link: any) => `${link.provider}: ${link.model}`)}
                       onChange={(lines) => set(['models', 'operator', 'chain'], lines
                         .map((line) => line.split(':'))
                         .filter((parts) => parts.length >= 2 && parts[0].trim())
                         .map(([provider, ...model]) => ({ provider: provider.trim(), model: model.join(':').trim() })))} />
            <p className="sheet__hint">
              {CLOUD.map(([key, env]) => `${key}: ${draft.models?.providers?.[key]?.enabled ? 'key set' : `set ${env}`}`)
                .join(' · ')}. Optional and free-tier; the local model is always the last resort, and
              anything listed above stays on this Mac.
            </p>
          </Group>

          <Group title="Intelligence">
            <Toggle label="Agentic reasoning" value={draft.intelligence?.enabled ?? true}
                    onChange={(v) => set(['intelligence', 'enabled'], v)} />
            <Field label="Actions per request">
              <input type="number" min={1} max={12} value={draft.intelligence?.max_steps ?? 6}
                     onChange={(e) => set(['intelligence', 'max_steps'], Number(e.target.value))} />
            </Field>
            <Field label="Actions per errand">
              <input type="number" min={5} max={200} value={draft.automation?.max_steps ?? 50}
                     onChange={(e) => set(['automation', 'max_steps'], Number(e.target.value))} />
            </Field>
            <Field label="Minutes per errand">
              <input type="number" min={1} max={60}
                     value={Math.round((draft.automation?.max_wall_s ?? 600) / 60)}
                     onChange={(e) => set(['automation', 'max_wall_s'], Number(e.target.value) * 60)} />
            </Field>
            <Toggle label="Publish the reasoning trace" value={draft.intelligence?.trace ?? true}
                    onChange={(v) => set(['intelligence', 'trace'], v)} />
          </Group>

          <Group title="Skills">
            <Toggle label="Use recipes for common errands" value={draft.skills?.enabled ?? true}
                    onChange={(v) => set(['skills', 'enabled'], v)} />
            <Toggle label="Learn recipes from errands that worked" value={draft.skills?.learn ?? true}
                    onChange={(v) => set(['skills', 'learn'], v)} />
            {skills.filter((skill) => skill.source === 'learned').length === 0 ? (
              <p className="sheet__hint">No learned recipes yet. {skills.length} built in.</p>
            ) : (
              <ul className="skills">
                {skills.filter((skill) => skill.source === 'learned').map((skill) => (
                  <li key={skill.id} data-set-aside={skill.set_aside}>
                    <span className="skills__title">{skill.title.replace(/^Learned: /, '')}</span>
                    <span className="skills__meta">
                      {[...skill.sites, ...skill.apps].join(', ')}
                      {skill.uses ? ` · used ${skill.uses}×` : ''}
                      {skill.set_aside ? ' · set aside (kept failing)' : ''}
                    </span>
                    <button className="skills__forget" onClick={() => forget(skill.id)}>Forget</button>
                  </li>
                ))}
              </ul>
            )}
          </Group>

          <Group title="Capabilities">
            {Object.entries(draft.capabilities).map(([key, value]) => (
              <Toggle key={key} label={key} value={Boolean(value)}
                      onChange={(v) => set(['capabilities', key], v)} />
            ))}
          </Group>

          <Group title="Interface">
            <Toggle label="Developer mode" value={draft.ui.developer_mode}
                    onChange={(v) => set(['ui', 'developer_mode'], v)} />
            <Toggle label="Reduced motion" value={draft.ui.reduced_motion}
                    onChange={(v) => set(['ui', 'reduced_motion'], v)} />
            <Field label="Log level">
              <select value={draft.log_level} onChange={(e) => set(['log_level'], e.target.value)}>
                {['DEBUG', 'INFO', 'WARNING', 'ERROR'].map((l) => <option key={l}>{l}</option>)}
              </select>
            </Field>
          </Group>
        </div>

        <footer className="sheet__footer">
          <span className="sheet__path">Stored in {status?.workspace}/config/config.json</span>
          <div>
            <button className="btn btn--ghost" onClick={() => setShow(false)}>Cancel</button>
            <button className="btn btn--primary" onClick={save} disabled={saving}>
              {saving ? 'Saving…' : 'Save'}
            </button>
          </div>
        </footer>
      </div>
    </div>
  )
}

function Group({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="settings__group">
      <h3>{title}</h3>
      <div className="settings__fields">{children}</div>
    </section>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="settings__field">
      <span>{label}</span>
      {children}
    </label>
  )
}

/**
 * A list edited as lines of text. Committed when the field loses focus, so
 * typing a new line isn't undone by the empty line being tidied away.
 */
function ListField({ label, items, onChange }: {
  label: string
  items: string[]
  onChange: (items: string[]) => void
}) {
  return (
    <label className="settings__field settings__field--list">
      <span>{label}</span>
      <textarea rows={Math.min(6, Math.max(2, items.length + 1))} defaultValue={items.join('\n')}
                spellCheck={false}
                onBlur={(e) => onChange(e.target.value.split('\n').map((line) => line.trim()).filter(Boolean))} />
    </label>
  )
}

/** The free cloud tiers JARVIS knows, and where each one's key comes from. */
const CLOUD: [string, string][] = [
  ['groq', 'JARVIS_GROQ_API_KEY'], ['openrouter', 'JARVIS_OPENROUTER_API_KEY'],
  ['cerebras', 'JARVIS_CEREBRAS_API_KEY'], ['gemini', 'JARVIS_GEMINI_API_KEY'],
]

function Toggle({ label, value, onChange }: {
  label: string
  value: boolean
  onChange: (value: boolean) => void
}) {
  return (
    <label className="settings__toggle">
      <input type="checkbox" checked={value} onChange={(e) => onChange(e.target.checked)} />
      <span>{label}</span>
    </label>
  )
}

/** API keys live in the environment, never in the config file. */
interface SkillRow {
  id: string
  title: string
  source: 'builtin' | 'learned'
  sites: string[]
  apps: string[]
  uses: number
  set_aside: boolean
}

/**
 * The paths where `draft` actually differs from `original`, as a sparse
 * object with the same nesting — suitable for a PATCH the backend merges in
 * (core/config.py's ConfigStore.update()), which leaves everything not
 * mentioned exactly as it already was. Arrays and other non-plain-object
 * values are compared and replaced wholesale, never merged field-by-field.
 */
function diffConfig(original: any, draft: any): any {
  if (original === draft) return undefined
  const plainObject = (value: any) =>
    value !== null && typeof value === 'object' && !Array.isArray(value)
  if (!plainObject(original) || !plainObject(draft)) {
    return JSON.stringify(original) === JSON.stringify(draft) ? undefined : draft
  }
  const changes: any = {}
  let anyChanged = false
  for (const key of Object.keys(draft)) {
    const sub = diffConfig(original[key], draft[key])
    if (sub !== undefined) {
      changes[key] = sub
      anyChanged = true
    }
  }
  return anyChanged ? changes : undefined
}

function stripSecrets(draft: any) {
  const clone = JSON.parse(JSON.stringify(draft))
  for (const provider of Object.values<any>(clone.models?.providers ?? {})) {
    delete provider.api_key
  }
  delete clone.research?.brave_api_key
  return clone
}

import { useEffect, useState } from 'react'
import { useStore } from '../state/store'

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

  useEffect(() => {
    if (show && config) setDraft(JSON.parse(JSON.stringify(config)))
  }, [show, config])

  useEffect(() => {
    if (!show) return
    fetch('/api/voice/voices').then((r) => r.json()).then((d) => setVoices(d.voices ?? [])).catch(() => {})
    fetch('/api/models').then((r) => r.json()).then((d) => {
      const installed = Object.values(d.providers ?? {}).flatMap((p: any) => p.models ?? [])
      setModels(installed as string[])
    }).catch(() => {})
  }, [show])

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
      await fetch('/api/config', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(stripSecrets(draft)),
      })
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
                {['tiny.en', 'base.en', 'small.en', 'medium.en', 'large-v3'].map((m) => (
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
            {(['fast', 'general', 'vision'] as const).map((slot) => (
              <Field key={slot} label={`${slot} model`}>
                <input list="installed-models" value={draft.models[slot].model}
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
function stripSecrets(draft: any) {
  const clone = JSON.parse(JSON.stringify(draft))
  for (const provider of Object.values<any>(clone.models?.providers ?? {})) {
    delete provider.api_key
  }
  delete clone.research?.brave_api_key
  return clone
}

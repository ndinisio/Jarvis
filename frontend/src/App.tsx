import { Activity } from './components/Activity'
import { Composer } from './components/Composer'
import { ConfirmDialog } from './components/ConfirmDialog'
import { Conversation } from './components/Conversation'
import { Core } from './components/Core'
import { DevPanel } from './components/DevPanel'
import { Notices } from './components/Notices'
import { Onboarding } from './components/Onboarding'
import { ResultPanel } from './components/ResultPanel'
import { Settings } from './components/Settings'
import { StatusBar } from './components/StatusBar'
import { useBrowserSpeech } from './hooks/useBrowserSpeech'
import { useSocket } from './hooks/useSocket'
import { useStore } from './state/store'

export default function App() {
  const { send } = useSocket()
  useBrowserSpeech(send)
  const devMode = useStore((s) => s.devMode)
  const assistantState = useStore((s) => s.assistantState)

  return (
    <div className="app" data-state={assistantState}>
      <div className="app__grid" aria-hidden="true" />
      <StatusBar send={send} />

      <main className="app__body" data-dev={devMode}>
        {devMode && (
          <aside className="app__rail app__rail--left">
            <DevPanel />
          </aside>
        )}

        <section className="app__center">
          <Core />
          <Conversation />
          <Composer send={send} />
        </section>

        <aside className="app__rail app__rail--right">
          <Activity send={send} />
          <ResultPanel />
        </aside>
      </main>

      <Notices />
      <ConfirmDialog send={send} />
      <Settings />
      <Onboarding />
    </div>
  )
}

import { useStore } from '../state/store'
import type { Task } from '../lib/events'

/**
 * What JARVIS is doing *right now*, and what it is doing in the background.
 * This is the panel that makes a long task feel supervised rather than stalled.
 */
export function ActivityPanel({ send }: { send: (m: Record<string, unknown>) => boolean }) {
  const activities = useStore((s) => s.activities)
  const tasks = useStore((s) => Object.values(s.tasks))

  const active = tasks
    .filter((t) => t.status === 'running' || t.status === 'pending')
    .sort((a, b) => b.started - a.started)
  const recent = tasks
    .filter((t) => t.status !== 'running' && t.status !== 'pending')
    .sort((a, b) => (b.finished ?? 0) - (a.finished ?? 0))
    .slice(0, 4)

  const lines = activities.slice(-9).reverse()

  return (
    <section className="activity">
      <header className="panel__header">
        <h2>Activity</h2>
        {active.length > 0 && <span className="activity__count">{active.length} running</span>}
      </header>

      {active.map((task) => (
        <TaskCard key={task.id} task={task} send={send} />
      ))}

      {lines.length === 0 && active.length === 0 && (
        <p className="activity__idle">Nothing in progress.</p>
      )}

      {lines.length > 0 && (
        <ul className="activity__log">
          {lines.map((entry) => (
            <li key={entry.id}>
              <span className="activity__tag">{(entry.tool ?? entry.category ?? 'system')
                .replace(/_/g, ' ').slice(0, 18)}</span>
              <span className="activity__message">{entry.message}</span>
            </li>
          ))}
        </ul>
      )}

      {recent.length > 0 && (
        <div className="activity__recent">
          <h3>Recent tasks</h3>
          <ul>
            {recent.map((task) => (
              <li key={task.id} data-status={task.status}>
                <span className="activity__dot" data-status={task.status} />
                <span className="activity__title">{task.title}</span>
                <span className="activity__elapsed">{task.elapsed_s.toFixed(1)}s</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}

function TaskCard({ task, send }: { task: Task; send: (m: Record<string, unknown>) => boolean }) {
  const step = task.steps?.[task.steps.length - 1]
  return (
    <div className="task">
      <div className="task__head">
        <span className="task__kind">{task.kind.toUpperCase()}</span>
        <span className="task__elapsed">{task.elapsed_s.toFixed(1)}s</span>
      </div>
      <p className="task__title">{task.title}</p>
      {step && <p className="task__step">{step.message}</p>}
      <div className="task__bar">
        <span style={{ width: `${Math.max(4, task.progress * 100)}%` }} />
      </div>
      {task.cancellable && (
        <button className="task__cancel" onClick={() => send({ type: 'cancel', task_id: task.id })}>
          Cancel
        </button>
      )}
    </div>
  )
}

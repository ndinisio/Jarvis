import { useStore } from '../state/store'
import type { Task } from '../lib/events'

/**
 * One surface for "what is JARVIS doing right now" — replaces the previous
 * split between a per-turn reasoning trace (ReasoningPanel) and a separate
 * task/tool log (ActivityPanel), which sat in the same rail answering the
 * same question at two different levels of detail. Quiet at rest: no boxed
 * "nothing in progress" placeholder, just the header and a one-line state.
 *
 * Every line here is something the backend actually reported — an objective
 * it understood, a step it planned, a tool it ran. Nothing is invented to
 * fill the space.
 */
export function Activity({ send }: { send: (m: Record<string, unknown>) => boolean }) {
  const reasoning = useStore((s) => s.reasoning)
  const activities = useStore((s) => s.activities)
  const tasks = useStore((s) => Object.values(s.tasks))

  const active = tasks
    .filter((t) => t.status === 'running' || t.status === 'pending')
    .sort((a, b) => b.started - a.started)
  const recent = tasks
    .filter((t) => t.status !== 'running' && t.status !== 'pending')
    .sort((a, b) => (b.finished ?? 0) - (a.finished ?? 0))
    .slice(0, 3)
  const lines = activities.slice(-6).reverse()

  const hasReasoning = Boolean(reasoning && reasoning.objective)
  const quiet = !hasReasoning && active.length === 0 && lines.length === 0 && recent.length === 0

  return (
    <section className="activity" data-quiet={quiet}>
      <header className="panel__header">
        <h2>Activity</h2>
        {active.length > 0 && <span className="activity__count">{active.length} running</span>}
      </header>

      {quiet && <p className="activity__quiet">Ready.</p>}

      {hasReasoning && reasoning && (
        <div className="reason" data-done={reasoning.done}>
          {reasoning.done && reasoning.elapsedMs > 0 && (
            <span className="reason__cost">
              {reasoning.toolCalls} tool · {reasoning.modelCalls} model ·{' '}
              {(reasoning.elapsedMs / 1000).toFixed(1)}s
            </span>
          )}
          <p className="reason__objective">{reasoning.objective}</p>
          {reasoning.context && <p className="reason__context">{reasoning.context}</p>}
          {reasoning.steps.length > 0 && (
            <ol className="reason__steps">
              {reasoning.steps.map((step, index) => (
                <li key={`${index}-${step.label}`} data-state={step.state}>
                  <span className="reason__branch">
                    {index === reasoning.steps.length - 1 ? '└─' : '├─'}
                  </span>
                  <span className="reason__label">{step.label}</span>
                </li>
              ))}
            </ol>
          )}
          {reasoning.question && <p className="reason__question">"{reasoning.question}"</p>}
        </div>
      )}

      {active.map((task) => (
        <TaskCard key={task.id} task={task} send={send} />
      ))}

      {lines.length > 0 && (
        <ul className="activity__log">
          {lines.map((entry) => (
            <li key={entry.id}>
              <span className="activity__tag">
                {(entry.tool ?? entry.category ?? 'system').replace(/_/g, ' ').slice(0, 18)}
              </span>
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

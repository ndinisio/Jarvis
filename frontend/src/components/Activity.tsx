import { useStore } from '../state/store'
import type { ChecklistItem, Task } from '../lib/events'

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

  // reasoning.done never goes back to null after a turn finishes — the only
  // reset action, clearConversation(), is never actually called anywhere —
  // so hasReasoning must exclude a *finished* trace itself, or the panel
  // would stay in its "something's happening" state for the rest of the
  // page's life after the very first turn. lines/recent are historical log
  // entries, not "currently active" signals, so they're deliberately left
  // out of quiet too — quiet means "nothing live right now", not "no history".
  const hasReasoning = Boolean(reasoning && reasoning.objective && !reasoning.done)
  const quiet = !hasReasoning && active.length === 0

  return (
    <section className="activity" data-quiet={quiet}>
      <header className="panel__header">
        <h2>Activity</h2>
        {active.length > 0 && <span className="activity__count">{active.length} running</span>}
      </header>

      {quiet && <p className="activity__quiet">Ready.</p>}

      {hasReasoning && reasoning && (
        // hasReasoning already excludes a finished trace (see above), so
        // this only ever renders mid-turn — the cost-summary this block
        // used to show once done is gone with it; it never had a chance to
        // render for more than an instant anyway, since done and the panel
        // going quiet happen in the same store update.
        <div className="reason">
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
          {reasoning.checklist.length > 0 && <Checklist items={reasoning.checklist} />}
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
  // The latest checklist any step reported: what "done" means for this
  // errand, ticked off only as each item is proven.
  const checklist = [...(task.steps ?? [])].reverse().find((s) => s.checklist?.length)?.checklist
  return (
    <div className="task">
      <div className="task__head">
        <span className="task__kind">{task.kind.toUpperCase()}</span>
        <span className="task__elapsed">{task.elapsed_s.toFixed(1)}s</span>
      </div>
      <p className="task__title">{task.title}</p>
      {checklist && <Checklist items={checklist} />}
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


function Checklist({ items }: { items: ChecklistItem[] }) {
  return (
    <ul className="checklist" aria-label="What done means">
      {items.map((item, index) => (
        <li key={`${index}-${item.text}`} data-done={item.done}
            title={item.done && item.evidence ? `Shown by “${item.evidence}”` : undefined}>
          <span className="checklist__mark" aria-hidden="true">{item.done ? '✓' : '○'}</span>
          <span className="checklist__text">{item.text}</span>
          <span className="visually-hidden">{item.done ? ' (done)' : ' (not yet)'}</span>
        </li>
      ))}
    </ul>
  )
}

import { useStore } from '../state/store'

/**
 * What JARVIS is working out, while it works it out.
 *
 * Every line here is something the backend actually reported — an objective it
 * understood, a step it planned, a tool it ran, a check that passed or didn't.
 * Nothing is invented to fill the space: if there is no plan, no plan is drawn,
 * and a step only turns green once verification said so.
 */
export function ReasoningPanel() {
  const reasoning = useStore((s) => s.reasoning)
  if (!reasoning || !reasoning.objective) return null

  const { objective, context, steps, question, done, toolCalls, modelCalls, elapsedMs } = reasoning

  return (
    <section className="reason" data-done={done}>
      <header className="panel__header">
        <h2>Working on it</h2>
        {done && elapsedMs > 0 && (
          <span className="reason__cost">
            {toolCalls} tool · {modelCalls} model · {(elapsedMs / 1000).toFixed(1)}s
          </span>
        )}
      </header>

      <p className="reason__objective">{objective}</p>
      {context && <p className="reason__context">{context}</p>}

      {steps.length > 0 && (
        <ol className="reason__steps">
          {steps.map((step, index) => (
            <li key={`${index}-${step.label}`} data-state={step.state}>
              <span className="reason__branch">{index === steps.length - 1 ? '└─' : '├─'}</span>
              <span className="reason__label">{step.label}</span>
            </li>
          ))}
        </ol>
      )}

      {question && <p className="reason__question">“{question}”</p>}
    </section>
  )
}

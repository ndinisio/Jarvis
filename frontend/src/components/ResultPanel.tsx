import { useState } from 'react'
import { useStore } from '../state/store'
import type { Panel } from '../lib/events'

/**
 * Rich results. Anything a tool wants to show — a table of processes, a
 * screenshot, a research report with sources, an email triage — is rendered
 * here rather than being read aloud or flattened into chat text.
 */
export function ResultPanel() {
  const panels = useStore((s) => s.panels)
  if (!panels.length) return null
  const latest = panels.slice(-4).reverse()
  return (
    <section className="results">
      <header className="panel__header">
        <h2>Results</h2>
      </header>
      <div className="results__list">
        {latest.map((panel) => (
          <PanelCard key={panel.id} panel={panel} />
        ))}
      </div>
    </section>
  )
}

function PanelCard({ panel }: { panel: Panel }) {
  const [open, setOpen] = useState(true)
  return (
    <article className="result" data-kind={panel.kind}>
      <button className="result__head" onClick={() => setOpen(!open)}>
        <span className="result__kind">{panel.kind}</span>
        <span className="result__title">{panel.title ?? ''}</span>
        <span className="result__chevron">{open ? '−' : '+'}</span>
      </button>
      {open && <div className="result__body">{renderBody(panel)}</div>}
    </article>
  )
}

function renderBody(panel: Panel) {
  switch (panel.kind) {
    case 'image':
      return (
        <figure className="result__image">
          <img src={panel.image} alt={panel.title ?? 'Screen capture'} />
          {panel.text && <figcaption>{panel.text}</figcaption>}
        </figure>
      )

    case 'facts':
      return (
        <dl className="result__facts">
          {(panel.facts ?? []).map((fact: [string, string], index: number) => (
            <div key={index}>
              <dt>{fact[0]}</dt>
              <dd>{fact[1]}</dd>
            </div>
          ))}
        </dl>
      )

    case 'gauge':
      return (
        <div className="result__gauge">
          <div className="gauge__value">
            {panel.value}
            <span>{panel.unit}</span>
          </div>
          <div className="gauge__track">
            <span style={{ width: `${Math.min(100, Number(panel.value) || 0)}%` }} />
          </div>
          {panel.facts && (
            <dl className="result__facts">
              {panel.facts.map((fact: [string, string], index: number) => (
                <div key={index}>
                  <dt>{fact[0]}</dt>
                  <dd>{fact[1]}</dd>
                </div>
              ))}
            </dl>
          )}
        </div>
      )

    case 'table':
      return (
        <div className="result__tablewrap">
          <table className="result__table">
            <thead>
              <tr>{(panel.columns ?? []).map((column: string) => <th key={column}>{column}</th>)}</tr>
            </thead>
            <tbody>
              {(panel.rows ?? []).slice(0, 25).map((row: any[], index: number) => (
                <tr key={index}>
                  {row.map((cell, cellIndex) => <td key={cellIndex}>{String(cell)}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )

    case 'list':
      return (
        <ul className="result__list">
          {(panel.items ?? []).slice(0, 40).map((item: string, index: number) => (
            <li key={index}>{item}</li>
          ))}
        </ul>
      )

    case 'sources':
    case 'research':
      return (
        <div className="result__research">
          {panel.markdown && <Markdown text={panel.markdown} />}
          {(panel.sources ?? []).length > 0 && (
            <ol className="result__sources">
              {panel.sources.map((source: any, index: number) => (
                <li key={index}>
                  <a href={source.url} target="_blank" rel="noreferrer">
                    {source.title || source.domain}
                  </a>
                  <span className="result__domain">{source.domain}</span>
                </li>
              ))}
            </ol>
          )}
          {panel.file && <p className="result__file">Saved to {panel.file}</p>}
        </div>
      )

    case 'diagnostics':
      return (
        <ul className="result__findings">
          {(panel.findings ?? []).map((finding: any, index: number) => (
            <li key={index} data-severity={finding.severity}>
              <span className="finding__area">{finding.area}</span>
              <span className="finding__text">{finding.observation}</span>
            </li>
          ))}
        </ul>
      )

    case 'email':
      return (
        <ul className="result__email">
          {(panel.messages ?? []).map((message: any, index: number) => (
            <li key={index} data-urgency={message.urgency}>
              <div className="email__head">
                <span className="email__sender">{message.sender}</span>
                <span className="email__date">{message.date?.slice(0, 22)}</span>
              </div>
              <div className="email__subject">{message.subject}</div>
              {message.preview && <p className="email__preview">{message.preview}</p>}
            </li>
          ))}
        </ul>
      )

    case 'calendar':
      return (
        <ul className="result__calendar">
          {(panel.events ?? []).length === 0 && <li className="is-empty">Nothing scheduled.</li>}
          {(panel.events ?? []).map((event: any, index: number) => (
            <li key={index}>
              <span className="event__time">{formatEventTime(event)}</span>
              <span className="event__title">{event.title}</span>
              {event.location && <span className="event__where">{event.location}</span>}
            </li>
          ))}
        </ul>
      )

    case 'draft':
      return (
        <div className="result__draft">
          <p><strong>To</strong> {(panel.to ?? []).join(', ')}</p>
          <p><strong>Subject</strong> {panel.subject}</p>
          <pre>{panel.body}</pre>
        </div>
      )

    case 'memory':
      return (
        <div className="result__memory">
          {Object.keys(panel.preferences ?? {}).length > 0 && (
            <dl className="result__facts">
              {Object.entries(panel.preferences).map(([key, value]) => (
                <div key={key}>
                  <dt>{key.replace(/_/g, ' ')}</dt>
                  <dd>{String(value)}</dd>
                </div>
              ))}
            </dl>
          )}
          <ul className="result__list">
            {(panel.facts ?? []).map((fact: any) => <li key={fact.id}>{fact.text}</li>)}
          </ul>
          {panel.path && <p className="result__file">{panel.path}</p>}
        </div>
      )

    case 'code':
    case 'text':
      return <pre className="result__pre">{panel.text}</pre>

    case 'file':
      return (
        <div>
          <p className="result__file">{panel.path}</p>
          {panel.preview && <pre className="result__pre">{panel.preview}</pre>}
        </div>
      )

    case 'link':
      return (
        <a className="result__link" href={panel.url} target="_blank" rel="noreferrer">
          {panel.url}
        </a>
      )

    case 'page':
      return (
        <div>
          <a className="result__link" href={panel.url} target="_blank" rel="noreferrer">
            {panel.url}
          </a>
          <pre className="result__pre">{panel.text}</pre>
        </div>
      )

    case 'app':
      return <p className="result__app">{panel.name}</p>

    default:
      return <pre className="result__pre">{JSON.stringify(panel, null, 2).slice(0, 2000)}</pre>
  }
}

function formatEventTime(event: any): string {
  if (event.all_day) return 'All day'
  const raw = String(event.start ?? '')
  const match = raw.match(/(\d{1,2}):(\d{2})/)
  return match ? `${match[1]}:${match[2]}` : raw.slice(0, 16)
}

/** Just enough markdown for research reports — headings, bold, lists, links. */
function Markdown({ text }: { text: string }) {
  const blocks = text.split(/\n{2,}/)
  return (
    <div className="markdown">
      {blocks.map((block, index) => {
        if (/^#{1,3}\s/.test(block)) {
          return <h4 key={index}>{block.replace(/^#{1,3}\s/, '')}</h4>
        }
        if (/^\s*[-*•]\s/m.test(block)) {
          return (
            <ul key={index}>
              {block.split('\n').filter(Boolean).map((line, lineIndex) => (
                <li key={lineIndex}>{inline(line.replace(/^\s*[-*•]\s/, ''))}</li>
              ))}
            </ul>
          )
        }
        return <p key={index}>{inline(block)}</p>
      })}
    </div>
  )
}

function inline(text: string) {
  const parts = text.split(/(\*\*[^*]+\*\*|\[\d+\])/g)
  return parts.map((part, index) => {
    if (/^\*\*[^*]+\*\*$/.test(part)) return <strong key={index}>{part.slice(2, -2)}</strong>
    if (/^\[\d+\]$/.test(part)) return <sup key={index} className="citation">{part}</sup>
    return <span key={index}>{part}</span>
  })
}

/** What the status bar's version label says, and whether this interface is older than its backend. */

export interface VersionStatus {
  version?: string
  ledger?: string
  commit?: string
}

export interface VersionLabel {
  /** The version in the latest commit's title (v8.58) - or the package version from a backend that
   *  predates it. */
  main: string
  /** The package version, small, beside it ("" when there is nothing more to say). */
  sub: string
  title: string
  /** Why the interface is out of date, or "" when it matches the backend. */
  stale: string
}

export function versionLabel(status: VersionStatus | undefined, uiLedger: string): VersionLabel | null {
  if (!status?.ledger && !status?.version) return null
  const ledger = status.ledger ?? ''
  const main = ledger || `v${status.version}`
  const sub = ledger && status.version ? status.version : ''
  const details = [status.version && `package ${status.version}`, status.commit && `commit ${status.commit}`]
    .filter(Boolean)
    .join(', ')
  const title =
    `JARVIS ${main}` +
    (ledger ? " - the version in the latest commit's title on GitHub" : '') +
    (details ? ` (${details})` : '')
  const stale =
    ledger && uiLedger && uiLedger !== 'unknown' && uiLedger !== ledger
      ? `This interface was built from ${uiLedger} but the backend is ${ledger}. Rebuild it: cd frontend && npm run build`
      : ''
  return { main, sub, title, stale }
}

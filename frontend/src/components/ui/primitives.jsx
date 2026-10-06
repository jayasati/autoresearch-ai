/**
 * Small presentational building blocks.
 *
 * No data fetching, no routing, no business rules -- so they stay trivially
 * reusable as the real pages arrive.
 */

/** A titled panel. The unit the whole layout is built from. */
export function Card({ title, description, action, children, className = '' }) {
  return (
    <section className={`card ${className}`.trim()}>
      {(title || action) && (
        <header className="card__header">
          <div>
            {title && <h2 className="card__title">{title}</h2>}
            {description && <p className="card__description">{description}</p>}
          </div>
          {action}
        </header>
      )}
      {children}
    </section>
  )
}

/** Page title block. One per page, rendered by the page itself. */
export function PageHeader({ title, description, action }) {
  return (
    <header className="page-header">
      <div>
        <h1 className="page-header__title">{title}</h1>
        {description && <p className="page-header__description">{description}</p>}
      </div>
      {action}
    </header>
  )
}

/**
 * Status pill.
 *
 * `tone` carries meaning, so the label text always states the status too -- colour
 * alone would exclude anyone who cannot distinguish it.
 */
export function Badge({ tone = 'neutral', children }) {
  return <span className={`badge badge--${tone}`}>{children}</span>
}

export function Button({ variant = 'primary', type = 'button', children, ...rest }) {
  return (
    <button type={type} className={`btn btn--${variant}`} {...rest}>
      {children}
    </button>
  )
}

/** A single number with a label. Used on the dashboard. */
export function StatTile({ label, value, hint }) {
  return (
    <div className="stat">
      <p className="stat__label">{label}</p>
      <p className="stat__value">{value}</p>
      {hint && <p className="stat__hint">{hint}</p>}
    </div>
  )
}

/**
 * A labelled list of what a future page will contain.
 *
 * Used inside `NotImplemented` to describe structure without inventing data: it
 * says what the columns will be, never what the rows will say.
 */
export function PlannedContents({ heading = 'What will appear here', items }) {
  return (
    <div className="planned">
      <p className="planned__heading">{heading}</p>
      <ul className="planned__list">
        {items.map((item) => (
          <li key={typeof item === 'string' ? item : item.label}>
            {typeof item === 'string' ? (
              item
            ) : (
              <>
                <strong>{item.label}</strong>
                {item.detail ? ` — ${item.detail}` : null}
              </>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}

import { useEffect, useRef, useState } from 'react';
import { api } from '../../services/api';
import type { AdminQueryResult, AdminQuerySchemaTable } from '../../services/api';

function quoteIdent(name: string): string {
  return `"${name.replace(/"/g, '""')}"`;
}

function Cell({ value }: { value: unknown }) {
  const [open, setOpen] = useState(false);
  if (value == null) return <span className="admin-query__null">null</span>;
  const text = typeof value === 'string' ? value : String(value);
  if (text.length <= 180) return <>{text}</>;
  return (
    <span>
      {open ? text : `${text.slice(0, 180)}…`}{' '}
      <button type="button" className="admin-query__more" onClick={() => setOpen((v) => !v)}>
        {open ? 'less' : 'more'}
      </button>
    </span>
  );
}

export function QueryPanel() {
  const [tables, setTables] = useState<AdminQuerySchemaTable[] | null>(null);
  const [schemaError, setSchemaError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [sql, setSql] = useState('SELECT id, username, email\nFROM players\nORDER BY username\nLIMIT 50');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<AdminQueryResult | null>(null);
  const sqlRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .adminQuerySchema()
      .then((r) => {
        if (!cancelled) setTables(r.tables ?? []);
      })
      .catch((e: unknown) => {
        if (!cancelled) setSchemaError(e instanceof Error ? e.message : 'Could not load tables');
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const insertAtCursor = (ident: string) => {
    const el = sqlRef.current;
    const start = el?.selectionStart ?? sql.length;
    const end = el?.selectionEnd ?? sql.length;
    const before = sql.slice(0, start);
    const needsSpace = before.length > 0 && !/\s$/.test(before);
    const chunk = `${needsSpace ? ' ' : ''}${ident}`;
    const next = before + chunk + sql.slice(end);
    setSql(next);
    const pos = before.length + chunk.length;
    requestAnimationFrame(() => {
      const node = sqlRef.current;
      if (!node) return;
      node.focus();
      node.setSelectionRange(pos, pos);
    });
  };

  const run = async () => {
    const statement = sql.trim();
    if (!statement) {
      setError('Enter a SELECT statement.');
      setResult(null);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const next = await api.adminQuery(statement);
      setResult(next);
    } catch (e) {
      setResult(null);
      setError(e instanceof Error ? e.message : 'Query failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="admin-query">
      <aside className="admin-query__schema" aria-label="Tables">
        <h2 className="admin-query__schema-title">Tables</h2>
        <p className="admin-query__hint">Click a name to insert it. Expand a table to see its columns.</p>
        {schemaError ? <div className="admin-page__error">{schemaError}</div> : null}
        {tables == null && !schemaError ? <p className="admin-query__hint">Loading tables…</p> : null}
        {tables && tables.length === 0 ? <p className="admin-query__hint">No tables.</p> : null}
        <ul className="admin-query__tables">
          {(tables ?? []).map((table) => {
            const open = !!expanded[table.name];
            return (
              <li key={table.name} className="admin-query__table">
                <div className="admin-query__table-row">
                  <button
                    type="button"
                    className="admin-query__expand"
                    aria-expanded={open}
                    aria-label={`${open ? 'Collapse' : 'Expand'} ${table.name}`}
                    onClick={() => setExpanded((cur) => ({ ...cur, [table.name]: !cur[table.name] }))}
                  >
                    {open ? '▾' : '▸'}
                  </button>
                  <button
                    type="button"
                    className="admin-query__name"
                    onClick={() => insertAtCursor(quoteIdent(table.name))}
                  >
                    {table.name}
                    {table.kind === 'view' ? <span className="admin-query__kind">view</span> : null}
                  </button>
                </div>
                {open ? (
                  <ul className="admin-query__columns">
                    {table.columns.map((col) => (
                      <li key={col.name}>
                        <button
                          type="button"
                          className="admin-query__column"
                          onClick={() => insertAtCursor(`${quoteIdent(table.name)}.${quoteIdent(col.name)}`)}
                        >
                          <span className="admin-query__column-name">{col.name}</span>
                          <span className="admin-query__column-type">{col.type}</span>
                        </button>
                      </li>
                    ))}
                  </ul>
                ) : null}
              </li>
            );
          })}
        </ul>
      </aside>
      <div className="admin-query__main">
        <label className="admin-query__label" htmlFor="admin-query-sql">
          SELECT
        </label>
        <textarea
          id="admin-query-sql"
          ref={sqlRef}
          className="admin-query__sql"
          value={sql}
          spellCheck={false}
          onChange={(e) => setSql(e.target.value)}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
              e.preventDefault();
              if (!busy) void run();
            }
          }}
        />
        <div className="admin-query__run-row">
          <button type="button" className="admin-page__btn admin-page__btn--primary" onClick={() => void run()} disabled={busy}>
            {busy ? 'Running…' : 'Run'}
          </button>
          <p className="admin-query__hint">
            SELECT only, one statement. Runs on this server&apos;s database. At most 200 rows come back.
            {' '}⌘/Ctrl+Enter to run.
          </p>
        </div>
        {error ? <div className="admin-page__error">{error}</div> : null}
        {result ? (
          <div className="admin-query__results">
            <p className="admin-query__hint">
              {result.row_count} {result.row_count === 1 ? 'row' : 'rows'}
              {result.truncated ? ' (more rows were not returned)' : ''}
            </p>
            {result.columns.length === 0 ? (
              <p className="admin-query__hint">The statement returned no columns.</p>
            ) : (
              <div className="admin-query__table-wrap">
                <table>
                  <thead>
                    <tr>
                      {result.columns.map((col, colIndex) => (
                        <th key={`${col}-${colIndex}`}>{col}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {result.rows.length === 0 ? (
                      <tr>
                        <td colSpan={result.columns.length} className="admin-query__null">
                          No rows
                        </td>
                      </tr>
                    ) : (
                      result.rows.map((row, i) => (
                        <tr key={i}>
                          {row.map((cell, j) => (
                            <td key={`${result.columns[j] ?? j}-${i}`}>
                              <Cell value={cell} />
                            </td>
                          ))}
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        ) : null}
      </div>
    </div>
  );
}

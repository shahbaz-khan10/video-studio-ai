interface ErrorStateProps {
  message: string;
  onRetry?: () => void;
}

export function LoadingState({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="state-panel" role="status">
      <span className="spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

export function ErrorState({ message, onRetry }: ErrorStateProps) {
  return (
    <div className="state-panel state-error" role="alert">
      <div>
        <strong>Unable to load</strong>
        <p>{message}</p>
      </div>
      {onRetry && (
        <button className="button button-quiet" type="button" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

export function EmptyState({ title, message }: { title: string; message: string }) {
  return (
    <div className="empty-state">
      <h3>{title}</h3>
      <p>{message}</p>
    </div>
  );
}

export function StatusBadge({ value }: { value: string | null }) {
  const normalized = (value ?? "none").toLowerCase();
  const className = `badge badge-${normalized.replace(/[^a-z]/g, "-")}`;
  return <span className={className}>{value ?? "none"}</span>;
}

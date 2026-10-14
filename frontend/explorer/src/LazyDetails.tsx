import { useState, type ReactNode } from 'react';

/** Native keyboard disclosure; closed bodies are absent from the DOM. */
export default function LazyDetails({ summary, children, className, initialOpen = false }: {
  summary: ReactNode;
  children: () => ReactNode;
  className?: string;
  initialOpen?: boolean;
}) {
  const [open, setOpen] = useState(initialOpen);
  return <details className={className} open={open} onToggle={event => {
    if (event.target === event.currentTarget) setOpen(event.currentTarget.open);
  }}>
    <summary>{summary}</summary>
    {open && children()}
  </details>;
}

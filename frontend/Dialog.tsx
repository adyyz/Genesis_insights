import React, { useEffect, useRef } from 'react';

export function Dialog({ title, description, onClose, children, wide = false, returnFocus }: { title: string; description: string; onClose: () => void; children: React.ReactNode; wide?: boolean; returnFocus?: HTMLElement | null }) {
  const ref = useRef<HTMLDialogElement>(null);
  const id = React.useId();
  useEffect(() => {
    const previous = returnFocus || document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    const dialog = ref.current!;
    dialog.showModal(); document.body.style.overflow = 'hidden';
    return () => { dialog.close(); document.body.style.overflow = overflow; previous?.focus(); };
  }, []);
  return <dialog ref={ref} className={wide ? 'dialog-wide' : undefined} aria-labelledby={id + '-title'} aria-describedby={id + '-description'} onCancel={event => { event.preventDefault(); onClose(); }}>
    <button className="modal-close" autoFocus onClick={onClose} aria-label="Close dialog">×</button>
    <h2 id={id + '-title'}>{title}</h2><p id={id + '-description'}>{description}</p>{children}
  </dialog>;
}

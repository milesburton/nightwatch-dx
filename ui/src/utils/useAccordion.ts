import { useEffect, useState } from 'react';

/**
 * Persists an open/closed accordion state in localStorage.
 * Default is closed (false) unless a saved value exists.
 */
export function useAccordion(key: string): [boolean, () => void] {
  const [open, setOpen] = useState<boolean>(() => {
    const saved = localStorage.getItem(key);
    return saved === null ? false : saved === 'true';
  });

  useEffect(() => {
    localStorage.setItem(key, String(open));
  }, [key, open]);

  const toggle = () => setOpen((v) => !v);

  return [open, toggle];
}

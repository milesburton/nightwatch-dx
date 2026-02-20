import { type DragEvent, type ReactNode, useRef, useState } from 'react';

interface Props {
  accept: string;
  onFile: (file: File) => void;
  processing: boolean;
  icon: ReactNode;
  hint: string;
  inputId: string;
  children?: ReactNode;
}

export function DropZone({ accept, onFile, processing, icon, hint, inputId, children }: Props) {
  const [dragActive, setDragActive] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleDrag = (e: DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(e.type === 'dragenter' || e.type === 'dragover');
  };

  const handleDrop = (e: DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setDragActive(false);
    const file = e.dataTransfer.files[0];
    if (file) onFile(file);
  };

  const handleSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) onFile(file);
  };

  return (
    <section
      className={`border-2 border-dashed rounded-xl p-8 transition-all mb-4 min-h-50 flex flex-col justify-center ${
        dragActive
          ? 'border-[var(--color-primary)] bg-[rgba(124,140,248,0.06)] scale-[1.01]'
          : 'border-white/20 bg-white/[0.03]'
      } ${processing ? 'opacity-50 pointer-events-none' : ''}`}
      onDragEnter={handleDrag}
      onDragLeave={handleDrag}
      onDragOver={handleDrag}
      onDrop={handleDrop}
    >
      <input
        ref={inputRef}
        type="file"
        id={inputId}
        accept={accept}
        onChange={handleSelect}
        disabled={processing}
        className="hidden"
      />
      <label htmlFor={inputId} className="cursor-pointer block text-center">
        {processing ? (
          <>
            <div className="spinner" />
            <p className="text-white/50 text-sm">Processing…</p>
          </>
        ) : (
          <>
            <div className="flex justify-center mb-4 text-white/30">{icon}</div>
            <p className="text-white/40 text-sm mb-3">Drag &amp; drop or</p>
            <button
              type="button"
              className="px-6 py-2.5 bg-[var(--color-primary)] text-white text-sm font-semibold rounded-lg cursor-pointer hover:bg-[var(--color-primary-dark)] hover:-translate-y-0.5 transition-all"
              onClick={(e) => {
                e.preventDefault();
                inputRef.current?.click();
              }}
            >
              Choose File
            </button>
            {hint && <p className="text-white/30 text-xs mt-3">{hint}</p>}
            {children}
          </>
        )}
      </label>
    </section>
  );
}

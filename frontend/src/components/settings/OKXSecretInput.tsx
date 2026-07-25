import { Eye, EyeOff, KeyRound } from "lucide-react";

export const okxFieldClass =
  "w-full rounded-md border bg-background px-3 py-2 text-sm outline-none transition focus:border-primary focus:ring-2 focus:ring-primary/20 disabled:cursor-not-allowed disabled:opacity-60";
export const okxLabelClass = "text-sm font-medium";
export const okxHintClass = "text-xs text-muted-foreground";

interface OKXSecretInputProps {
  id: string;
  label: string;
  ariaLabel?: string;
  environment: "Demo" | "live";
  value: string;
  visible: boolean;
  configured: boolean;
  disabled: boolean;
  required?: boolean;
  onChange: (value: string) => void;
  onToggleVisibility: () => void;
}

export function OKXSecretInput({
  id,
  label,
  ariaLabel,
  environment,
  value,
  visible,
  configured,
  disabled,
  required = false,
  onChange,
  onToggleVisibility,
}: OKXSecretInputProps) {
  const placeholder = configured ? "Configured" : `Enter OKX ${environment} ${label}`;

  return (
    <label className="grid gap-2" htmlFor={id}>
      <span className={okxLabelClass}>{label}</span>
      <div className="relative">
        <KeyRound className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
        <input
          id={id}
          aria-label={ariaLabel}
          type={visible ? "text" : "password"}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          className={`${okxFieldClass} px-10`}
          placeholder={placeholder}
          autoComplete="new-password"
          disabled={disabled}
          required={required}
          spellCheck={false}
        />
        <button
          type="button"
          onClick={onToggleVisibility}
          disabled={disabled}
          className="absolute right-1 top-1 inline-flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground transition hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"
          aria-label={`${visible ? "Hide" : "Show"} ${ariaLabel || label}`}
          title={`${visible ? "Hide" : "Show"} ${ariaLabel || label}`}
        >
          {visible ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
        </button>
      </div>
    </label>
  );
}

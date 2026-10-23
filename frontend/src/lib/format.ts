const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

export function money(cents: number | null | undefined): string {
  return cents == null ? "—" : usd.format(cents / 100);
}

export function tinyMoney(dollars: number | null | undefined): string {
  if (dollars == null) return "—";
  if (dollars === 0) return "$0";
  if (dollars < 0.01) return `$${dollars.toFixed(5).replace(/0+$/, "")}`;
  return usd.format(dollars);
}

export function duration(ms: number): string {
  if (ms < 1) return "<1 ms";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`;
}

export function count(n: number): string {
  return n.toLocaleString("en-US");
}

const relative = new Intl.RelativeTimeFormat("en", { numeric: "auto", style: "short" });

export function ago(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "—";
  const seconds = Math.round((new Date(iso).getTime() - now) / 1000);
  const abs = Math.abs(seconds);
  if (abs < 45) return "just now";
  if (abs < 3600) return relative.format(Math.round(seconds / 60), "minute");
  if (abs < 86400) return relative.format(Math.round(seconds / 3600), "hour");
  return relative.format(Math.round(seconds / 86400), "day");
}

export function withinLastDay(iso: string | null | undefined, now: number = Date.now()): boolean {
  return !!iso && now - new Date(iso).getTime() < 86_400_000;
}

export function timestamp(iso: string): string {
  return new Date(iso).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

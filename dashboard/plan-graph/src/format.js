// Shared formatting helpers for the live view and (DM-06) the completed
// snapshot viewer, so both render the DM-01 tri-state metric shapes
// identically. Absent data always renders as "Unavailable", never as 0.
export const numberFormat = new Intl.NumberFormat('en-US');

export const title = (value) => String(value || 'Unavailable').replace(/[_-]+/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());

export const tokens = (value) => (value === null || value === undefined ? 'Unavailable' : numberFormat.format(value));

export const duration = (milliseconds) => {
  if (milliseconds === null || milliseconds === undefined) return 'Unavailable';
  if (milliseconds < 1_000) return `${Math.round(milliseconds)} ms`;
  if (milliseconds < 60_000) return `${(milliseconds / 1_000).toFixed(1)} s`;
  const hours = Math.floor(milliseconds / 3_600_000);
  const minutes = Math.floor((milliseconds % 3_600_000) / 60_000);
  const seconds = Math.floor((milliseconds % 60_000) / 1_000);
  return hours ? `${hours}h ${minutes}m` : `${minutes}m ${seconds}s`;
};

export const money = (cost) => (
  cost?.state === 'available' ? `$${Number(cost.usd).toFixed(4)}`
    : cost?.state === 'estimated' ? `≈$${Number(cost.usd).toFixed(4)}`
      : cost?.state === 'partial' && cost.usd !== null && cost.usd !== undefined ? `≥$${Number(cost.usd).toFixed(4)}`
        : 'Unavailable'
);

export const usd = (value) => (value === null || value === undefined ? 'Unavailable' : `$${Number(value).toFixed(4)}`);

export const compactId = (value) => {
  const parts = String(value || '').split('/');
  return parts.length > 1 ? parts.slice(-3).join(' / ') : value;
};

// A tri-state `{state, value, reason}` metric (plan DM-01/DM-04): a
// `partial` state is a verified lower bound, rendered with a `≥` prefix
// rather than presented as if it were the true total.
export function metricValue(metric, formatter = tokens) {
  if (!metric || metric.state === 'unavailable' || metric.value === null || metric.value === undefined) return 'Unavailable';
  const formatted = formatter(metric.value);
  return metric.state === 'partial' ? `≥${formatted}` : formatted;
}

export function metricReason(metric) {
  return metric?.reason || null;
}

// The token block shares the tri-state convention but reports four counts
// instead of one `value` (plan:130-136).
export function tokenBlockValue(block, field) {
  if (!block || block.state === 'unavailable') return 'Unavailable';
  const value = block[field];
  if (value === null || value === undefined) return 'Unavailable';
  const formatted = tokens(value);
  return block.state === 'partial' ? `≥${formatted}` : formatted;
}

// The retry-budget ledger's four counters are `unavailable` together (the
// ledger is absent for pre-ledger historical runs) — never partially so.
export function ledgerValue(block, field) {
  if (!block || block.state !== 'available') return 'Unavailable';
  const value = block[field];
  return value === null || value === undefined ? 'Unavailable' : tokens(value);
}

// mean/median/max per-FeatureRun distribution (plan:181-186): heavy-tailed
// data means a mean alone would mislead, so all three are always shown
// together. Only `max` carries the `≥` lower-bound prefix under a partial
// state -- a subset mean/median is not a bound on the full-population value,
// only a subset max is (plan:163-168).
export function distributionSummary(distribution, formatter = tokens) {
  if (!distribution || distribution.state === 'unavailable' || distribution.mean === null || distribution.mean === undefined) return 'Unavailable';
  const maxPrefix = distribution.state === 'partial' ? '≥' : '';
  return `mean ${formatter(distribution.mean)} · median ${formatter(distribution.median)} · max ${maxPrefix}${formatter(distribution.max)}`;
}

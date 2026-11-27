export function effortLabel(effort) {
  return effort === 'transport_default' ? 'Default' : effort;
}

export function matchesConfiguration(entry, { query = '', vendors }) {
  if (vendors?.size && !vendors.has(entry.vendor)) return false;
  const needle = query.trim().toLowerCase();
  return !needle || [entry.name, entry.model_id, entry.vendor_label, entry.transport ?? entry.transport_label,
    entry.transport_id, entry.reasoning_effort, effortLabel(entry.reasoning_effort), entry.id]
    .join(' ').toLowerCase().includes(needle);
}

/**
 * ConversationUsageCell - per-conversation token-usage breakdown cell.
 *
 * Shared by the System Reports tables (Latest Conversations, Top Conversations):
 * a bold all-in total line plus one provider-native row per model, with the
 * exact raw fields (incl. the Anthropic 5m/1h cache-write TTL split) in each
 * row's tooltip.
 */

import type {
  AdminConversationModelUsage,
  AdminConversationUsageTotal,
  AdminCostSource,
} from '../api/types';
import { formatNumber } from '../utils/formatters';
import './ConversationUsageCell.css';

/**
 * Compact dollar format for costs. Two decimals is plenty at scan
 * granularity; anything positive below a cent shows as "<$0.01" rather
 * than "$0.00".
 */
export function formatUsd(v: number): string {
  if (v > 0 && v < 0.01) return '<$0.01';
  return `$${v.toFixed(2)}`;
}

/**
 * Scan-format a cost with its provenance: a figure built entirely from
 * provider-reported amounts (OpenRouter's `usage.cost`) shows as-is, while
 * anything that includes a list-price estimate keeps the "~" prefix.
 */
export function formatCost(v: number, source: AdminCostSource | null): string {
  const usd = formatUsd(v);
  return source === 'reported' ? usd : `~${usd}`;
}

/** Tooltip wording for where a cost figure came from. */
export function describeCostSource(source: AdminCostSource | null): string {
  switch (source) {
    case 'reported':
      return 'reported by the provider';
    case 'mixed':
      return 'partly provider-reported, partly list-price estimate';
    default:
      return 'list-price estimate';
  }
}

/**
 * Compact provider-native scan format for a per-model usage row. The token
 * columns are each provider's raw fields (no normalized in/out/cached
 * buckets), so the row text labels them per provider.
 */
function formatModelUsageRow(m: AdminConversationModelUsage): string {
  if (m.provider === 'gemini') {
    const g = m.metrics;
    let text =
      `prompt ${formatNumber(g.prompt_token_count)} ` +
      `(cached ${formatNumber(g.cached_content_token_count)}) · ` +
      `out ${formatNumber(g.candidates_token_count)} ` +
      `(+${formatNumber(g.thoughts_token_count)} thoughts)`;
    if (g.tool_use_prompt_token_count > 0) {
      text += ` · tool-use ${formatNumber(g.tool_use_prompt_token_count)}`;
    }
    return text;
  }
  if (m.provider === 'openrouter') {
    const o = m.metrics;
    let text =
      `prompt ${formatNumber(o.prompt_tokens)} ` +
      `(cached ${formatNumber(o.cached_prompt_tokens)}) · ` +
      `out ${formatNumber(o.completion_tokens)}`;
    if (o.reasoning_tokens > 0) {
      text += ` (incl. ${formatNumber(o.reasoning_tokens)} reasoning)`;
    }
    return text;
  }
  const a = m.metrics;
  return (
    `in ${formatNumber(a.input_tokens)} · ` +
    `cache-r ${formatNumber(a.cache_read_input_tokens)} · ` +
    `cache-w ${formatNumber(a.cache_creation_input_tokens)} · ` +
    `out ${formatNumber(a.output_tokens)}`
  );
}

/**
 * Full native field names + values for the row tooltip -- this is where an
 * admin reads exact raw numbers (incl. the Anthropic 5m/1h cache-write TTL
 * split, which is tooltip-only).
 */
function formatModelUsageTooltip(m: AdminConversationModelUsage): string {
  const calls = `${m.call_count} call${m.call_count === 1 ? '' : 's'}`;
  // 4 decimals in the tooltip: cheap Flash/Haiku calls are often fractions
  // of a cent, and this is where an admin reads exact numbers.
  const cost =
    m.estimated_cost_usd === null
      ? 'cost: n/a (no pricing entry for this model)'
      : `cost: ${m.cost_source === 'reported' ? '' : '~'}$${m.estimated_cost_usd.toFixed(4)} ` +
        `(${describeCostSource(m.cost_source)})`;
  if (m.provider === 'gemini') {
    const g = m.metrics;
    return (
      `${m.model} (${calls})\n${cost}\n` +
      `prompt_token_count: ${formatNumber(g.prompt_token_count)}\n` +
      `cached_content_token_count: ${formatNumber(g.cached_content_token_count)}\n` +
      `candidates_token_count: ${formatNumber(g.candidates_token_count)}\n` +
      `thoughts_token_count: ${formatNumber(g.thoughts_token_count)}\n` +
      `tool_use_prompt_token_count: ${formatNumber(g.tool_use_prompt_token_count)}`
    );
  }
  if (m.provider === 'openrouter') {
    const o = m.metrics;
    return (
      `${m.model} (${calls})\n${cost}\n` +
      `prompt_tokens: ${formatNumber(o.prompt_tokens)}\n` +
      `cached_prompt_tokens: ${formatNumber(o.cached_prompt_tokens)}\n` +
      `completion_tokens: ${formatNumber(o.completion_tokens)}\n` +
      `reasoning_tokens: ${formatNumber(o.reasoning_tokens)}`
    );
  }
  const a = m.metrics;
  return (
    `${m.model} (${calls})\n${cost}\n` +
    `input_tokens: ${formatNumber(a.input_tokens)}\n` +
    `output_tokens: ${formatNumber(a.output_tokens)}\n` +
    `cache_read_input_tokens: ${formatNumber(a.cache_read_input_tokens)}\n` +
    `cache_creation_input_tokens: ${formatNumber(a.cache_creation_input_tokens)}\n` +
    `cache_creation_5m_input_tokens: ${formatNumber(a.cache_creation_5m_input_tokens)}\n` +
    `cache_creation_1h_input_tokens: ${formatNumber(a.cache_creation_1h_input_tokens)}`
  );
}

interface ConversationUsageCellProps {
  usageByModel: AdminConversationModelUsage[];
  usageTotal: AdminConversationUsageTotal;
}

export function ConversationUsageCell({
  usageByModel,
  usageTotal,
}: ConversationUsageCellProps) {
  if (usageByModel.length === 0) {
    return <span className="tokens-cell-empty">&mdash;</span>;
  }
  return (
    <div className="tokens-cell">
      <div
        className="tokens-total"
        title={
          'All-in token magnitude across all models and providers; ' +
          `$ is ${describeCostSource(usageTotal.cost_source)} (~ marks an estimate)`
        }
      >
        {formatNumber(usageTotal.total_tokens)} tokens ·{' '}
        {usageTotal.call_count} call
        {usageTotal.call_count === 1 ? '' : 's'}
        {usageTotal.estimated_cost_usd !== null && (
          <span className="tokens-cost">
            {' '}· {formatCost(usageTotal.estimated_cost_usd, usageTotal.cost_source)}
          </span>
        )}
      </div>
      <div className="tokens-by-model">
        {usageByModel.map((m) => (
          <div
            key={m.model}
            className="tokens-model-row"
            title={formatModelUsageTooltip(m)}
          >
            <span className="tokens-model-name">{m.model}</span>
            <span className="tokens-model-counts">
              {formatModelUsageRow(m)}
              {m.estimated_cost_usd !== null && (
                <span className="tokens-cost">
                  {' '}· {formatCost(m.estimated_cost_usd, m.cost_source)}
                </span>
              )}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

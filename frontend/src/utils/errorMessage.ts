/**
 * Human-readable message from a caught value of unknown type.
 *
 * `catch` bindings are `unknown` under strict TypeScript, so callers that
 * want to surface `err.message` need a narrowing step; this keeps the
 * fallback text next to the call instead of a cast to `any`.
 */
export function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

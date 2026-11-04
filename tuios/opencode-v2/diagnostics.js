import { JournalFile } from "./transcript.js";

const token = (value) => typeof value === "string" && /^[A-Za-z][A-Za-z0-9_.-]{0,63}$/.test(value) ? value : undefined;

export function errorSummary(error) {
  const status = error?.status ?? error?.statusCode;
  const frame = typeof error?.stack === "string"
    ? error.stack.match(/(?:bridge|transcript|reporter|tui)\.js:\d+:\d+/)?.[0] : undefined;
  return {
    name: token(error?.name) ?? token(error?._tag) ?? "UnknownError",
    code: token(error?.code) ?? token(error?.cause?.code),
    status: Number.isInteger(status) && status >= 100 && status <= 599 ? status : undefined,
    frame,
  };
}

export function isCancellation(error) {
  return ["AbortError", "InterruptedException", "CanceledError", "CancelledError"].includes(error?.name)
    || ["ABORT_ERR", "ERR_CANCELED"].includes(error?.code);
}

// One small overwritten diagnostic per pane, not an accumulating log. Never
// persist error.message/stack wholesale: they can contain prompts, URLs or keys.
export async function writeDiagnostic(path, stage, error) {
  const file = new JournalFile(`${path}.diagnostic.json`);
  try {
    await file.replace(JSON.stringify({ time: new Date().toISOString(), stage, ...errorSummary(error) }) + "\n");
  } finally { await file.close(); }
}

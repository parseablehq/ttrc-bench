// Experiment for parseablehq/parseable#1807, not a pinned server: runs the locked parseable-mcp-server with
// get_dataset_schema asking for `?format=compact`, as Parseable asked us to try. Nothing else changes, so the TSV
// reaches the agent through the server's usual JSON.stringify of tool results.
//   node pr1807-compact-schema.mjs <path to parseable-mcp-server/dist> run
import { pathToFileURL } from "node:url";
import { join } from "node:path";

const dist = process.argv[2];
const { ParseableClient } = await import(pathToFileURL(join(dist, "client.js")).href);
ParseableClient.prototype.getDatasetSchema = function (name) {
  return this.request("GET", `/logstream/${encodeURIComponent(name)}/schema`, undefined, {
    query: { format: "compact" },
  });
};
process.argv.splice(2, 1);
await import(pathToFileURL(join(dist, "server.js")).href);

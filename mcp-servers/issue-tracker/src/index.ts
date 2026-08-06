import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListResourcesRequestSchema,
  ListToolsRequestSchema,
  ReadResourceRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { readFileSync, writeFileSync, mkdirSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const DATA_DIR = join(__dirname, "..", "data");
const DATA_FILE = process.env.ISSUE_DATA_FILE || join(DATA_DIR, "issues.json");

type IssueStatus = "open" | "in_progress" | "done";

interface Issue {
  id: string;
  title: string;
  description: string;
  status: IssueStatus;
  labels: string[];
}

function load(): Issue[] {
  if (!existsSync(DATA_FILE)) return [];
  return JSON.parse(readFileSync(DATA_FILE, "utf-8")) as Issue[];
}

function save(issues: Issue[]): void {
  mkdirSync(DATA_DIR, { recursive: true });
  writeFileSync(DATA_FILE, JSON.stringify(issues, null, 2), "utf-8");
}

const server = new Server(
  { name: "alpha-swe-issue-tracker", version: "0.1.0" },
  { capabilities: { tools: {}, resources: {} } },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "list_issues",
      description: "列出 Issue（可按状态过滤）",
      inputSchema: {
        type: "object",
        properties: {
          status: {
            type: "string",
            enum: ["open", "in_progress", "done"],
            description: "按状态过滤（可选）",
          },
        },
      },
    },
    {
      name: "create_issue",
      description: "创建新 Issue",
      inputSchema: {
        type: "object",
        properties: {
          title: { type: "string" },
          description: { type: "string" },
          labels: { type: "array", items: { type: "string" } },
        },
        required: ["title", "description"],
      },
    },
    {
      name: "update_issue_status",
      description: "更新 Issue 状态",
      inputSchema: {
        type: "object",
        properties: {
          id: { type: "string" },
          status: { type: "string", enum: ["open", "in_progress", "done"] },
        },
        required: ["id", "status"],
      },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  const input = (args ?? {}) as Record<string, unknown>;

  if (name === "list_issues") {
    const status = input.status ? String(input.status) : "";
    const issues = load().filter((i) => status === "" || i.status === status);
    return { content: [{ type: "text", text: JSON.stringify(issues, null, 2) }] };
  }

  if (name === "create_issue") {
    const issues = load();
    const id = String(issues.length + 1);
    issues.push({
      id,
      title: String(input.title ?? ""),
      description: String(input.description ?? ""),
      status: "open",
      labels: Array.isArray(input.labels) ? (input.labels as string[]) : [],
    });
    save(issues);
    return { content: [{ type: "text", text: `已创建 Issue #${id}` }] };
  }

  if (name === "update_issue_status") {
    const id = String(input.id ?? "");
    const status = String(input.status ?? "") as IssueStatus;
    const issues = load();
    const issue = issues.find((i) => i.id === id);
    if (!issue) {
      return { content: [{ type: "text", text: `Issue 不存在: ${id}` }], isError: true };
    }
    issue.status = status;
    save(issues);
    return { content: [{ type: "text", text: `Issue #${id} 状态已更新为 ${status}` }] };
  }

  return { content: [{ type: "text", text: `未知工具: ${name}` }], isError: true };
});

server.setRequestHandler(ListResourcesRequestSchema, async () => ({
  resources: [
    { uri: "issue://all", name: "全部 Issue", description: "所有 Issue 的摘要" },
    { uri: "issue://open", name: "未关闭 Issue", description: "open/in_progress 的 Issue" },
  ],
}));

server.setRequestHandler(ReadResourceRequestSchema, async (request) => {
  const uri = request.params.uri;
  const issues = load();
  const pick = uri === "issue://open"
    ? issues.filter((i) => i.status !== "done")
    : issues;
  const text = pick
    .map((i) => `#${i.id} [${i.status}] ${i.title} (${i.labels.join(",")})`)
    .join("\n");
  return { contents: [{ uri, text: text || "(empty)" }] };
});

const transport = new StdioServerTransport();
await server.connect(transport);

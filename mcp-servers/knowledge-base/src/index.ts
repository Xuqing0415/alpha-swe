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
const DATA_FILE = process.env.KB_DATA_FILE || join(DATA_DIR, "kb.json");

interface KbEntry {
  id: string;
  topic: string;
  content: string;
  tags: string[];
}

function load(): KbEntry[] {
  if (!existsSync(DATA_FILE)) return [];
  return JSON.parse(readFileSync(DATA_FILE, "utf-8")) as KbEntry[];
}

function save(entries: KbEntry[]): void {
  mkdirSync(DATA_DIR, { recursive: true });
  writeFileSync(DATA_FILE, JSON.stringify(entries, null, 2), "utf-8");
}

const server = new Server(
  { name: "alpha-swe-knowledge-base", version: "0.1.0" },
  { capabilities: { tools: {}, resources: {} } },
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "search_kb",
      description: "按关键词搜索团队知识库（匹配主题/内容/标签）",
      inputSchema: {
        type: "object",
        properties: {
          query: { type: "string", description: "搜索关键词" },
          tag: { type: "string", description: "按标签过滤（可选）" },
        },
        required: ["query"],
      },
    },
    {
      name: "add_kb_entry",
      description: "向团队知识库新增一条知识",
      inputSchema: {
        type: "object",
        properties: {
          topic: { type: "string" },
          content: { type: "string" },
          tags: { type: "array", items: { type: "string" } },
        },
        required: ["topic", "content"],
      },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  const input = (args ?? {}) as Record<string, unknown>;

  if (name === "search_kb") {
    const q = String(input.query ?? "").toLowerCase();
    const tag = input.tag ? String(input.tag) : "";
    const hits = load().filter((e) => {
      const hay = `${e.topic} ${e.content} ${e.tags.join(" ")}`.toLowerCase();
      const matched = q === "" || hay.includes(q);
      const tagged = tag === "" || e.tags.includes(tag);
      return matched && tagged;
    });
    return {
      content: [{ type: "text", text: JSON.stringify(hits, null, 2) }],
    };
  }

  if (name === "add_kb_entry") {
    const entries = load();
    const id = String(entries.length + 1);
    entries.push({
      id,
      topic: String(input.topic ?? ""),
      content: String(input.content ?? ""),
      tags: Array.isArray(input.tags) ? (input.tags as string[]) : [],
    });
    save(entries);
    return {
      content: [{ type: "text", text: `已新增知识条目 #${id}` }],
    };
  }

  return { content: [{ type: "text", text: `未知工具: ${name}` }], isError: true };
});

server.setRequestHandler(ListResourcesRequestSchema, async () => ({
  resources: [
    { uri: "kb://topics", name: "知识主题列表", description: "团队知识库全部主题" },
    ...load().map((e) => ({
      uri: `kb://entry/${e.id}`,
      name: e.topic,
      description: `知识条目 #${e.id}`,
    })),
  ],
}));

server.setRequestHandler(ReadResourceRequestSchema, async (request) => {
  const uri = request.params.uri;
  const entries = load();
  if (uri === "kb://topics") {
    const topics = entries.map((e) => `${e.id}. ${e.topic} [${e.tags.join(",")}]`);
    return { contents: [{ uri, text: topics.join("\n") || "(empty)" }] };
  }
  const match = /^kb:\/\/entry\/(\d+)$/.exec(uri);
  if (match) {
    const entry = entries.find((e) => e.id === match[1]);
    if (entry) {
      return {
        contents: [{ uri, text: `# ${entry.topic}\n${entry.content}\n标签: ${entry.tags.join(", ")}` }],
      };
    }
  }
  return { contents: [{ uri, text: `资源不存在: ${uri}` }] };
});

const transport = new StdioServerTransport();
await server.connect(transport);

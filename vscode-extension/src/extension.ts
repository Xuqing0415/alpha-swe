// -*- coding: utf-8 -*-
/**
 * Alpha-SWE VS Code 扩展 MVP（方向二阶段三 3.4）。
 *
 * 功能：选中代码 + 输入自然语言指令 -> 提交 POST /api/v1/tasks ->
 * 轮询任务状态 -> 完成后在侧栏打开工作区 git diff。
 *
 * 依赖：仅 vscode API（Node >= 18 全局 fetch）。
 */
import * as vscode from "vscode";
import { exec } from "child_process";

interface SubmitTaskBody {
  instruction: string;
  workspace?: string;
  timeout?: number;
}

interface TaskPayload {
  id: string;
  status: string;
  error?: string | null;
  workspace?: string;
  result?: Record<string, unknown>;
}

function cfg(): vscode.WorkspaceConfiguration {
  return vscode.workspace.getConfiguration("alphaSwe");
}

function baseUrl(): string {
  const url = cfg().get<string>("baseUrl", "http://127.0.0.1:8000");
  return url.replace(/\/+$/, "");
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {};
  const body = init.body;
  if (typeof body === "string") {
    headers["Content-Type"] = "application/json";
  }
  const key = cfg().get<string>("apiKey", "");
  if (key) {
    headers["Authorization"] = `Bearer ${key}`;
  }
  const resp = await fetch(`${baseUrl()}${path}`, { ...init, headers });
  const text = await resp.text();
  if (!resp.ok) {
    throw new Error(`HTTP ${resp.status}: ${text.slice(0, 300)}`);
  }
  return (text ? JSON.parse(text) : {}) as T;
}

function workspacePath(): string | undefined {
  const folders = vscode.workspace.workspaceFolders;
  return folders && folders.length > 0 ? folders[0].uri.fsPath : undefined;
}

function gitDiff(workspace: string): Promise<string> {
  return new Promise((resolve, reject) => {
    exec(
      "git diff --stat && git diff",
      { cwd: workspace, maxBuffer: 8 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err && !stdout) {
          reject(new Error(stderr || err.message));
          return;
        }
        resolve(stdout || "(无改动)");
      }
    );
  });
}

async function showDiff(workspace?: string): Promise<void> {
  if (!workspace) {
    return;
  }
  try {
    const diff = await gitDiff(workspace);
    const doc = await vscode.workspace.openTextDocument({
      content: diff,
      language: "diff",
    });
    await vscode.window.showTextDocument(doc, {
      preview: true,
      viewColumn: vscode.ViewColumn.Beside,
    });
  } catch (e) {
    vscode.window.showWarningMessage(`无法读取 git diff：${(e as Error).message}`);
  }
}

export function activate(context: vscode.ExtensionContext): void {
  const disposable = vscode.commands.registerCommand(
    "alphaSwe.runOnSelection",
    async () => {
      const editor = vscode.window.activeTextEditor;
      if (!editor) {
        vscode.window.showWarningMessage("请先打开一个文件并选中代码。");
        return;
      }
      const code = editor.document.getText(editor.selection).trim();
      if (!code) {
        vscode.window.showWarningMessage("请先选中要处理的代码。");
        return;
      }
      const instruction = await vscode.window.showInputBox({
        prompt: "输入要 Agent 执行的自然语言指令",
        placeHolder: "重构这个函数，保持行为不变",
        value: "重构这段代码，保持行为不变",
      });
      if (instruction === undefined) {
        return;
      }
      const workspace = workspacePath();
      const snippet = code.length > 8000 ? `${code.slice(0, 8000)}\n...(截断)` : code;
      const fullInstruction = `[VS Code 选中代码]\n\`\`\`\n${snippet}\n\`\`\`\n\n指令：${instruction}`;

      try {
        const created = await request<{ id: string; status: string }>("/api/v1/tasks", {
          method: "POST",
          body: JSON.stringify({
            instruction: fullInstruction,
            workspace,
            timeout: cfg().get<number>("timeout", 1800),
          } as SubmitTaskBody),
        });
        await vscode.window.withProgress(
          {
            location: vscode.ProgressLocation.Notification,
            title: `Alpha-SWE 任务 ${created.id}`,
            cancellable: true,
          },
          async (_progress, token) => {
            let task = await request<TaskPayload>(`/api/v1/tasks/${created.id}`);
            while (
              (task.status === "queued" || task.status === "running") &&
              !token.isCancellationRequested
            ) {
              await new Promise((r) => setTimeout(r, 2000));
              task = await request<TaskPayload>(`/api/v1/tasks/${created.id}`);
            }
            if (token.isCancellationRequested) {
              await request(`/api/v1/tasks/${created.id}/cancel`, {
                method: "POST",
              }).catch(() => undefined);
              return;
            }
            if (task.status !== "completed") {
              vscode.window.showErrorMessage(
                `Alpha-SWE 任务失败：${task.status}${task.error ? ` - ${task.error}` : ""}`
              );
              return;
            }
            vscode.window.showInformationMessage("Alpha-SWE 任务已完成。");
            await showDiff(workspace);
          }
        );
      } catch (e) {
        vscode.window.showErrorMessage(`Alpha-SWE 请求失败：${(e as Error).message}`);
      }
    }
  );
  context.subscriptions.push(disposable);
}

export function deactivate(): void {}

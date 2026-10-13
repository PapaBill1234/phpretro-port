// Owner-gated project companion view. Mounted by the pinned DynamicViewLoader
// (packages/app/src/renderer/App.tsx) via the plugin's view declaration
// (bundlePath "dist/views/bundle.js", componentExport "ProjectCompanionView").
//
// Read-only. It loads bounded state through the host authenticated API client
// (@elizaos/ui `client`) — the same transport the shell uses — so cookie/bearer
// owner auth is handled by the host. It never holds an auth token, never writes, and
// never dispatches work.

import * as React from "react";
import { client } from "@elizaos/ui";
import { loadCompanionJson, type CompanionTransport } from "./companion-transport";

type Worker = { id: string; state: string };
type Task = { id: string; state: string; escalation: { required: boolean; state: string } };
type StatusView = {
  availability: string;
  reason?: string;
  stale?: boolean;
  observedAt?: string;
  counts?: Record<string, number | boolean> | null;
  workers: Worker[];
  sessions: Worker[];
};

const ENDPOINTS = {
  status: "/api/project-companion/status",
  tasks: "/api/project-companion/tasks",
};

function useRemote<T>(url: string): { data: T | null; error: boolean; loading: boolean } {
  const [data, setData] = React.useState<T | null>(null);
  const [error, setError] = React.useState(false);
  const [loading, setLoading] = React.useState(true);
  React.useEffect(() => {
    let active = true;
    const controller = new AbortController();
    loadCompanionJson(client as unknown as CompanionTransport, url, controller.signal)
      .then((body) => { if (active) setData(body as T); })
      .catch(() => { if (active) setError(true); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; controller.abort(); };
  }, [url]);
  return { data, error, loading };
}

function AvailabilityBadge({ value }: { value: string }) {
  return React.createElement("span", { "data-availability": value }, value);
}

export function ProjectCompanionView(): React.ReactElement {
  const status = useRemote<{ ok: boolean; status: StatusView }>(ENDPOINTS.status);
  const tasks = useRemote<{ ok: boolean; availability: string; tasks: Task[] }>(ENDPOINTS.tasks);

  const statusView = status.data?.status;
  const tasksView = tasks.data;

  return React.createElement(
    "section",
    { "data-view": "project-companion", style: { padding: "16px", fontFamily: "system-ui, sans-serif" } },
    React.createElement("h1", null, "Project Companion"),
    status.loading || tasks.loading ? React.createElement("p", { "data-state": "loading" }, "Loading…") : null,
    status.error || (statusView && statusView.availability === "unavailable")
      ? React.createElement(
          "p",
          { "data-state": "unavailable" },
          "Companion unavailable",
          statusView?.reason ? ` (${statusView.reason})` : "",
        )
      : null,
    statusView?.stale ? React.createElement("p", { "data-state": "stale" }, "Showing last known state (stale)") : null,
    statusView && statusView.counts
      ? React.createElement(
          "ul",
          { "data-role": "counts" },
          Object.entries(statusView.counts).map(([key, value]) => React.createElement("li", { key }, `${key}: ${String(value)}`)),
        )
      : null,
    statusView && statusView.workers.length
      ? React.createElement(
          "ul",
          { "data-role": "workers" },
          statusView.workers.map((worker) => React.createElement("li", { key: worker.id }, `${worker.id}: `, AvailabilityBadge({ value: worker.state }))),
        )
      : null,
    tasksView && tasksView.availability !== "available"
      ? React.createElement("p", { "data-state": "unavailable" }, "Tasks unavailable")
      : null,
    tasksView && tasksView.tasks.length
      ? React.createElement(
          "ul",
          { "data-role": "tasks" },
          tasksView.tasks.map((task) =>
            React.createElement(
              "li",
              { key: task.id },
              `${task.id}: `,
              AvailabilityBadge({ value: task.state }),
              task.escalation?.required ? React.createElement("span", { "data-escalation": task.escalation.state }, ` escalation:${task.escalation.state}`) : null,
            ),
          ),
        )
      : null,
  );
}

export default ProjectCompanionView;

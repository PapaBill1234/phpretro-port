// The project companion plugin: a real elizaOS Plugin with a default export, one
// owner-gated view declaration, and three private read-only routes.
//
// Discovery/registration follows the pinned drop-in loader (packages/agent/src/
// runtime/plugin-types.ts scanDropInPlugins + findRuntimePluginExport): an ordinary
// physical package whose immediate child directory holds a package.json and exposes
// this plugin as the default export. No symlinked plugin directory.

import { createCompanionProvider } from "./companion-client.js";
import { createProjectCompanionRoutes } from "./routes.js";

export const PLUGIN_NAME = "@phpretro/plugin-project-companion";
export const VIEW_ID = "project-companion";

/**
 * @param {{ provider?: ReturnType<typeof createCompanionProvider> }} [options]
 */
export function createProjectCompanionPlugin(options = {}) {
  const provider = options.provider ?? createCompanionProvider(options.providerOptions ?? {});

  return {
    name: PLUGIN_NAME,
    description:
      "Read-only, owner-gated view of the local PHPRetro builder companion: tasks, worker state and receipt status. Never mutates, dispatches, or calls a model.",
    packageName: PLUGIN_NAME,
    routes: createProjectCompanionRoutes(provider),
    views: [
      {
        id: VIEW_ID,
        label: "Project Companion",
        roleGate: { minRole: "OWNER" },
        description: "Owner-only board of builder tasks, worker state and receipt status.",
        icon: "Activity",
        path: "/project-companion",
        order: 930,
        viewKind: "release",
        modalities: ["gui"],
        tags: ["builder", "project", "tasks", "companion"],
        bundlePath: "dist/views/bundle.js",
        componentExport: "ProjectCompanionView",
      },
    ],
  };
}

export default createProjectCompanionPlugin();

// Public entry for @phpretro/plugin-project-companion.
export {
  createProjectCompanionPlugin,
  PLUGIN_NAME,
  VIEW_ID,
  default,
} from "./plugin.js";
export { resolveStrictOwner, requestHasOrigin, AUTHORIZATION_OPTIONS } from "./owner-authority.js";
export { createCompanionProvider, COMPANION_HOST, COMPANION_PORT } from "./companion-client.js";
export { loadControlToken, resolveControlTokenPath, CONTROL_TOKEN_RELATIVE } from "./control-token.js";
export { projectStatus, projectTask, projectTasks, projectReceipts } from "./redact.js";
export { deriveEscalation, ESCALATION_THRESHOLD } from "./escalation.js";
export { createProjectCompanionRoutes } from "./routes.js";

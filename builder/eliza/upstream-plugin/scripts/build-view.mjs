// Build the view bundle with the pinned upstream Vite toolchain.
// Usage: node scripts/build-view.mjs   (run from the package directory)
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const pkgRoot = path.resolve(here, "..");
const viteBin = process.env.PHP_RETRO_VITE_BIN;
if (!viteBin) {
  console.error("Set PHP_RETRO_VITE_BIN to the pinned upstream vite binary path.");
  process.exit(2);
}
const result = spawnSync(viteBin, ["build", "--config", path.join(pkgRoot, "vite.config.mjs")], {
  cwd: pkgRoot,
  stdio: "inherit",
});
process.exit(result.status ?? 1);

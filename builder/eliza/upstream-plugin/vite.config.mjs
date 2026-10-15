// Small single-module view bundle. Mirrors the pinned upstream view config
// (packages/scripts/view-bundle-vite.config.ts): React and the other host
// externals are left as bare specifiers that DynamicViewLoader
// (packages/ui/src/components/views/DynamicViewLoader.tsx) resolves to the host
// singletons. React is never re-bundled into this view.
const EXTERNALS = [
  "react",
  "react-dom",
  "react/jsx-runtime",
  "react/jsx-dev-runtime",
  "lucide-react",
  "@elizaos/ui",
  "@elizaos/app",
  "@elizaos/core/protocol",
  "@elizaos/contracts",
  "@elizaos/host/protocol",
];

export default {
  build: {
    lib: { entry: "views/main.tsx", formats: ["es"], fileName: () => "bundle.js" },
    outDir: "dist/views",
    emptyOutDir: true,
    minify: false,
    rollupOptions: {
      external: (id) => EXTERNALS.includes(id) || EXTERNALS.some((external) => id.startsWith(`${external}/`)),
      output: { exports: "named", inlineDynamicImports: true },
    },
    define: {
      "import.meta.env.DEV": JSON.stringify(false),
      "import.meta.env.PROD": JSON.stringify(true),
      "import.meta.env.MODE": JSON.stringify("production"),
      "process.env.NODE_ENV": JSON.stringify("production"),
    },
  },
};

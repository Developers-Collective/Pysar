#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";


const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const repositoryRoot = path.resolve(scriptDirectory, "..");
const sourceDirectory = path.join(repositoryRoot, "pysar", "gui");
const buildDirectory = path.join(repositoryRoot, "build");
const outputDirectory = path.resolve(
  repositoryRoot,
  process.argv[2] || path.join("build", "packaged_gui"),
);

const relativeOutput = path.relative(buildDirectory, outputDirectory);
if (
  relativeOutput === "" ||
  relativeOutput === ".." ||
  relativeOutput.startsWith(`..${path.sep}`) ||
  path.isAbsolute(relativeOutput)
) {
  throw new Error(`Packaged GUI output must be inside ${buildDirectory}`);
}

const sourceShellPath = path.join(sourceDirectory, "shell.html");
const babelPath = path.join(sourceDirectory, "vendor", "babel.min.js");
const sourceShell = fs.readFileSync(sourceShellPath, "utf8");
const jsxScriptPattern = /<script\s+type=["']text\/babel["']\s+src=["']([^"']+)["']\s*><\/script>/g;
const jsxSources = Array.from(sourceShell.matchAll(jsxScriptPattern), (match) => match[1]);

if (jsxSources.length === 0) {
  throw new Error(`No JSX entry points were found in ${sourceShellPath}`);
}

const require = createRequire(import.meta.url);
const Babel = require(babelPath);
const compiledSections = jsxSources.map((relativeSource) => {
  const sourcePath = path.resolve(sourceDirectory, relativeSource);
  const relativePath = path.relative(sourceDirectory, sourcePath);
  if (
    relativePath === ".." ||
    relativePath.startsWith(`..${path.sep}`) ||
    path.isAbsolute(relativePath)
  ) {
    throw new Error(`JSX source escapes the GUI directory: ${relativeSource}`);
  }

  const source = fs.readFileSync(sourcePath, "utf8");
  const result = Babel.transform(source, {
    filename: relativeSource,
    sourceType: "script",
    presets: [
      ["react", { development: false, runtime: "classic" }],
      ["env", { modules: false, targets: { chrome: "80", safari: "13" } }],
    ],
    // The development shell transforms each file into a classic script. Turn
    // top-level lexical declarations into vars so concatenating those scripts
    // preserves the same shared-global behaviour (including repeated names).
    plugins: ["transform-block-scoping"],
    comments: false,
    compact: true,
    sourceMaps: false,
  });
  if (!result || typeof result.code !== "string") {
    throw new Error(`Babel did not produce output for ${relativeSource}`);
  }
  return `/* ${relativeSource.replaceAll("*/", "* /")} */\n${result.code}`;
});

fs.rmSync(outputDirectory, { recursive: true, force: true });
fs.mkdirSync(path.dirname(outputDirectory), { recursive: true });
fs.cpSync(sourceDirectory, outputDirectory, {
  recursive: true,
  filter(sourcePath) {
    const relativePath = path.relative(sourceDirectory, sourcePath);
    const parts = relativePath.split(path.sep);
    if (parts.includes("__pycache__")) return false;
    if (sourcePath.endsWith(".pyc") || sourcePath.endsWith(".jsx")) return false;
    return path.basename(sourcePath) !== "babel.min.js";
  },
});

let bundleInserted = false;
let packagedShell = sourceShell.replace(jsxScriptPattern, () => {
  if (bundleInserted) return "";
  bundleInserted = true;
  return '<script src="app.bundle.js"></script>';
});
packagedShell = packagedShell.replace(
  /\s*<script\s+src=["']vendor\/babel\.min\.js["']\s*><\/script>/,
  "",
);
packagedShell = packagedShell.replace(
  "<head>",
  "<head>\n<!-- Packaged GUI: JSX was compiled during the release build. -->",
);

if (packagedShell.includes('type="text/babel"') || packagedShell.includes("babel.min.js")) {
  throw new Error("The packaged shell still references runtime Babel");
}

const bundlePath = path.join(outputDirectory, "app.bundle.js");
const packagedShellPath = path.join(outputDirectory, "shell.html");
fs.writeFileSync(bundlePath, `${compiledSections.join("\n")}\n`, "utf8");
fs.writeFileSync(packagedShellPath, packagedShell, "utf8");

const sourceBytes = jsxSources.reduce(
  (total, relativeSource) => total + fs.statSync(path.join(sourceDirectory, relativeSource)).size,
  0,
);
const bundleBytes = fs.statSync(bundlePath).size;
console.log(
  `Prepared packaged GUI: ${jsxSources.length} JSX files, ${sourceBytes} source bytes -> ${bundleBytes} compiled bytes`,
);
console.log(`Output: ${outputDirectory}`);

// Recreates the @tina4/* links after npm install.
//
// tina4-nodejs ships its internal workspace packages as SOURCE, with no
// package.json of their own, so `@tina4/...` imports do not resolve until these
// links exist. Without this the app fails at first import with a module-not-
// found that names a package you can plainly see on disk.
import { mkdirSync, symlinkSync, existsSync, readdirSync } from "fs";
import { join, resolve, dirname } from "path";
import { fileURLToPath } from "url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const packagesDir = join(root, "node_modules/tina4-nodejs/packages");
const scopeDir = join(root, "node_modules/@tina4");

if (!existsSync(packagesDir)) {
  console.log("postinstall: tina4-nodejs/packages not found, skipping.");
  process.exit(0);
}

mkdirSync(scopeDir, { recursive: true });

for (const pkg of readdirSync(packagesDir)) {
  const src = join(packagesDir, pkg);
  const dest = join(scopeDir, pkg);
  if (existsSync(dest)) continue;
  // Windows cannot make a directory symlink without elevation; a junction can.
  const type = process.platform === "win32" ? "junction" : "dir";
  symlinkSync(src, dest, type);
  console.log(`postinstall: linked @tina4/${pkg}`);
}

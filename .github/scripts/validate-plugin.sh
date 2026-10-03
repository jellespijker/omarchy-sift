#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MANIFEST="$ROOT_DIR/manifest.json"

echo "=== 1. Validating required files ==="
REQUIRED_FILES=(manifest.json README.md LICENSE preview.png Panel.qml Model.qml Content.qml bin/sift tree.json config.json.example)
for file in "${REQUIRED_FILES[@]}"; do
  if [[ ! -f "$ROOT_DIR/$file" ]]; then
    echo "::error::Missing required file: $file"
    exit 1
  fi
  echo "✓ Found $file"
done
if [[ ! -x "$ROOT_DIR/bin/sift" ]]; then
  echo "::error::bin/sift is not executable"
  exit 1
fi
echo "✓ bin/sift is executable"

echo ""
echo "=== 2. Validating Python syntax ==="
python3 -m compileall -q "$ROOT_DIR/src"
echo "✓ src/ compiled cleanly"

echo ""
echo "=== 3. Validating JSON files ==="
for f in manifest.json tree.json config.json.example; do
  jq -e . "$ROOT_DIR/$f" >/dev/null || { echo "::error::$f is not valid JSON"; exit 1; }
  echo "✓ $f"
done
for f in "$ROOT_DIR"/src/sift/i18n/*.json; do
  jq -e . "$f" >/dev/null || { echo "::error::$(basename "$f") is not valid JSON"; exit 1; }
done
echo "✓ translation catalogues"

echo ""
echo "=== 4. Validating manifest.json ==="
jq -e '.schemaVersion == 1' "$MANIFEST" >/dev/null || { echo "::error::schemaVersion must be 1"; exit 1; }
for field in id name version author license description kinds entryPoints; do
  jq -e --arg f "$field" 'has($f) and (.[$f] | tostring | length > 0)' "$MANIFEST" >/dev/null || {
    echo "::error::manifest missing non-empty field '$field'"
    exit 1
  }
done
ID=$(jq -r '.id' "$MANIFEST")
if [[ ! "$ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ || "$ID" == *".."* || "$ID" == omarchy.* ]]; then
  echo "::error::Invalid plugin id: $ID"
  exit 1
fi
echo "✓ ID is valid: $ID"
VERSION=$(jq -r '.version' "$MANIFEST")
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z0-9.]+)?$ ]]; then
  echo "::error::Version '$VERSION' is not valid semantic versioning (expected X.Y.Z)"
  exit 1
fi
echo "✓ Version is semver: $VERSION"
PYV=$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT_DIR/pyproject.toml" | head -1)
if [[ "$PYV" != "$VERSION" ]]; then
  echo "::error::pyproject.toml version ($PYV) differs from manifest.json ($VERSION)"
  exit 1
fi
echo "✓ pyproject.toml matches"
while IFS= read -r ep; do
  [[ -n "$ep" ]] || continue
  if [[ "$ep" == /* || "$ep" == *".."* ]]; then
    echo "::error::Entry point must be a safe relative path: $ep"
    exit 1
  fi
  if [[ ! -f "$ROOT_DIR/$ep" ]]; then
    echo "::error::Entry point file not found: $ep"
    exit 1
  fi
  echo "✓ Entry point exists: $ep"
done < <(jq -r '.entryPoints | to_entries[] | .value' "$MANIFEST")

echo ""
echo "✓ All plugin validations passed successfully!"

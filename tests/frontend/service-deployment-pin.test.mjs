import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL(
  "../../src/static/src/components/deployment_image_overrides_parser.js", import.meta.url
), "utf8");
const { parseImageOverrides } = await import(
  `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`
);

test("JSON editor preserves an immutable image reference", () => {
  const value = { image: { repository: "registry.example/open-design",
    tag: `candidate@sha256:${"a".repeat(64)}` } };
  assert.deepEqual(parseImageOverrides(JSON.stringify(value, null, 2)), value);
});

test("empty or whitespace input clears overrides", () => {
  for (const value of ["", "   ", "{}", "\n {} \n"]) {
    assert.deepEqual(parseImageOverrides(value), {});
  }
});

for (const value of ["{", "null", "[]", "true", "42", '"image"']) {
  test(`invalid object input ${JSON.stringify(value)} is rejected`, () => {
    assert.throws(() => parseImageOverrides(value));
  });
}

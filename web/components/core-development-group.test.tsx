import test from "node:test";
import assert from "node:assert/strict";
import { renderToStaticMarkup } from "react-dom/server";

import { groupBlockRows } from "./structured-plan-renderer";
import type { StructuredBlock } from "@/lib/types";

const block = (name: string, tags: string[] | null = null) =>
  ({ block_type: "accessory", display_name: name, tags } as unknown as StructuredBlock);

const render = (blocks: StructuredBlock[]) =>
  renderToStaticMarkup(
    <div>{groupBlockRows(blocks, (item, index) => <p key={index} data-row={index}>{item.display_name}</p>)}</div>,
  );

test("consecutive core_development members render under one collapsible header", () => {
  const html = render([
    block("Trap Bar Deadlift"),
    block("Pallof Press", ["core_development"]),
    block("Ab Wheel Rollout", ["core_development"]),
    block("Suitcase Carry", ["core_development"]),
  ]);
  assert.equal(html.match(/<details/g)?.length, 1);
  assert.match(html, /Core Development/);
  assert.match(html, /3 exercises/);
  // Every member keeps its own row and its own index (swap, edit, log).
  assert.deepEqual([...html.matchAll(/data-row="(\d)"/g)].map(match => match[1]), ["0", "1", "2", "3"]);
  assert.ok(html.indexOf("Trap Bar Deadlift") < html.indexOf("<details"));
});

test("a lone tagged block or untagged plan renders unchanged", () => {
  assert.doesNotMatch(render([block("Plank", ["core_development"]), block("Squat")]), /<details/);
  assert.doesNotMatch(render([block("Squat"), block("Row")]), /<details/);
});

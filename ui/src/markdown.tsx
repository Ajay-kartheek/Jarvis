// A deliberately small markdown subset: paragraphs, headings, bullet and numbered lists,
// **bold**, `code` and [links](https://…). Rendering takes a character budget, so a typewriter
// can reveal the text without ever showing raw markup.

import { Fragment, type ReactNode, useMemo } from "react";

type Inline =
  { t: "text" | "code"; s: string } | { t: "bold"; kids: Inline[] } | { t: "link"; href: string; kids: Inline[] };

type Block = { t: "p" | "h"; kids: Inline[] } | { t: "ul" | "ol"; start: number; items: Inline[][] };

const TOKEN = /\*\*(.+?)\*\*|`([^`]+)`|\[([^\]]+)\]\(([^)\s]+)\)/g;
const SAFE_LINK = /^(https?:\/\/|mailto:)/i;
const LIST_ITEM = /^\s*(?:[-*•]|(\d+)[.)])\s+(.*)$/;
const HEADING = /^#{1,6}\s+(.*)$/;

function parseInline(src: string): Inline[] {
  const out: Inline[] = [];
  let last = 0;
  for (const m of src.matchAll(TOKEN)) {
    const at = m.index ?? 0;
    if (at > last) out.push({ t: "text", s: src.slice(last, at) });
    if (m[1] !== undefined) out.push({ t: "bold", kids: parseInline(m[1]) });
    else if (m[2] !== undefined) out.push({ t: "code", s: m[2] });
    else if (SAFE_LINK.test(m[4])) out.push({ t: "link", href: m[4], kids: parseInline(m[3]) });
    else out.push({ t: "text", s: m[3] }); // unsafe target: keep the words, drop the link
    last = at + m[0].length;
  }
  if (last < src.length) out.push({ t: "text", s: src.slice(last) });
  return out;
}

function parseBlocks(src: string): Block[] {
  const blocks: Block[] = [];
  let para: string[] = [];
  const endPara = () => {
    if (para.length) blocks.push({ t: "p", kids: parseInline(para.join("\n")) });
    para = [];
  };
  for (const line of src.split("\n")) {
    const item = LIST_ITEM.exec(line);
    const heading = HEADING.exec(line);
    if (item) {
      endPara();
      const t = item[1] ? "ol" : "ul";
      const prev = blocks[blocks.length - 1];
      if (prev && prev.t === t) prev.items.push(parseInline(item[2]));
      else blocks.push({ t, start: Number(item[1] ?? 1), items: [parseInline(item[2])] });
    } else if (heading) {
      endPara();
      blocks.push({ t: "h", kids: parseInline(heading[1]) });
    } else if (line.trim()) {
      para.push(line.trim());
    } else {
      endPara();
    }
  }
  endPara();
  return blocks;
}

const inlineLength = (nodes: Inline[]): number =>
  nodes.reduce((n, node) => n + ("s" in node ? node.s.length : inlineLength(node.kids)), 0);

/** Number of visible characters in `text` once its markup is rendered. */
export function textLength(text: string): number {
  return parseBlocks(text).reduce(
    (n, block) =>
      n + ("items" in block ? block.items.reduce((k, item) => k + inlineLength(item), 0) : inlineLength(block.kids)),
    0,
  );
}

function renderInline(nodes: Inline[], budget: { left: number }): ReactNode[] {
  const out: ReactNode[] = [];
  nodes.forEach((node, i) => {
    if (budget.left <= 0) return;
    if ("s" in node) {
      const s = node.s.slice(0, budget.left);
      budget.left -= s.length;
      out.push(node.t === "code" ? <code key={i}>{s}</code> : <Fragment key={i}>{s}</Fragment>);
    } else if (node.t === "bold") {
      out.push(<strong key={i}>{renderInline(node.kids, budget)}</strong>);
    } else {
      out.push(
        <a key={i} href={node.href} target="_blank" rel="noreferrer noopener">
          {renderInline(node.kids, budget)}
        </a>,
      );
    }
  });
  return out;
}

/** One line of inline markdown, showing at most `limit` visible characters. */
export function InlineText({ text, limit = Infinity }: { text: string; limit?: number }) {
  const nodes = useMemo(() => parseInline(text), [text]);
  return <>{renderInline(nodes, { left: limit })}</>;
}

/** Block markdown, showing at most `limit` visible characters. */
export function Markdown({ text, limit = Infinity }: { text: string; limit?: number }) {
  const blocks = useMemo(() => parseBlocks(text), [text]);
  const budget = { left: limit };
  return (
    <>
      {blocks.map((block, i) => {
        if (budget.left <= 0) return null;
        if (!("items" in block)) {
          const kids = renderInline(block.kids, budget);
          return block.t === "h" ? <h4 key={i}>{kids}</h4> : <p key={i}>{kids}</p>;
        }
        const items = block.items.map((item, j) =>
          budget.left > 0 ? <li key={j}>{renderInline(item, budget)}</li> : null,
        );
        return block.t === "ol" ? (
          <ol key={i} start={block.start}>
            {items}
          </ol>
        ) : (
          <ul key={i}>{items}</ul>
        );
      })}
    </>
  );
}

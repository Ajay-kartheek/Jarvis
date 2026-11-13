// A deliberately small markdown subset: paragraphs, headings, bullet and numbered lists (one level of
// nesting), quotes, rules, code fences, **bold**, *italic*, `code` and [links](https://…). What the
// daemon writes (prep sheets, notes, briefs) stays inside it. Inline text can be rendered with a
// character budget, so a typewriter can reveal it without ever showing raw markup.

import { Fragment, type ReactNode, useMemo } from "react";

type Inline =
  | { t: "text" | "code"; s: string }
  | { t: "bold"; kids: Inline[] }
  | { t: "em"; kids: Inline[] }
  | { t: "link"; href: string; kids: Inline[] };

interface Item {
  depth: number;
  kids: Inline[];
}

type Block =
  | { t: "p" | "quote"; kids: Inline[] }
  | { t: "h"; level: number; kids: Inline[] }
  | { t: "ul" | "ol"; start: number; items: Item[] }
  | { t: "pre"; text: string }
  | { t: "hr" };

// bold, code, link, then italic: a single asterisk with no space inside it, and no word right after it (2*3*4 stays)
const TOKEN = /\*\*(.+?)\*\*|`([^`]+)`|\[([^\]]+)\]\(([^)\s]+)\)|\*(?![\s*])([^*\n]*[^*\s\n])\*(?!\w)/g;
const SAFE_LINK = /^(https?:\/\/|mailto:)/i;

/** A link from the server as it may be put on the page: a web or mail address, never a script. */
export const safeLink = (href: string | null | undefined): string | null => (href && SAFE_LINK.test(href) ? href : null);
const LIST_ITEM = /^(\s*)(?:[-*•]|(\d+)[.)])\s+(.*)$/;
const HEADING = /^(#{1,6})\s+(.*)$/;
const RULE = /^\s*([-*_])(?:\s*\1){2,}\s*$/;
const QUOTE = /^\s*>\s?(.*)$/;
const FENCE = /^\s*```/;

function parseInline(src: string): Inline[] {
  const out: Inline[] = [];
  let last = 0;
  for (const m of src.matchAll(TOKEN)) {
    const at = m.index ?? 0;
    if (at > last) out.push({ t: "text", s: src.slice(last, at) });
    if (m[1] !== undefined) out.push({ t: "bold", kids: parseInline(m[1]) });
    else if (m[2] !== undefined) out.push({ t: "code", s: m[2] });
    else if (m[5] !== undefined) out.push({ t: "em", kids: parseInline(m[5]) });
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
  let quote: string[] = [];
  let fence: string[] | null = null;
  const end = () => {
    if (para.length) blocks.push({ t: "p", kids: parseInline(para.join("\n")) });
    if (quote.length) blocks.push({ t: "quote", kids: parseInline(quote.join("\n")) });
    para = [];
    quote = [];
  };
  for (const line of src.split("\n")) {
    if (fence) {
      if (FENCE.test(line)) {
        blocks.push({ t: "pre", text: fence.join("\n") });
        fence = null;
      } else {
        fence.push(line);
      }
      continue;
    }
    const heading = HEADING.exec(line);
    const item = LIST_ITEM.exec(line);
    const quoted = QUOTE.exec(line);
    if (FENCE.test(line)) {
      end();
      fence = [];
    } else if (RULE.test(line)) {
      end();
      blocks.push({ t: "hr" });
    } else if (heading) {
      end();
      blocks.push({ t: "h", level: heading[1].length, kids: parseInline(heading[2]) });
    } else if (item) {
      end();
      const t = item[2] ? "ol" : "ul";
      const entry = { depth: Math.min(1, Math.floor(item[1].length / 2)), kids: parseInline(item[3]) };
      const prev = blocks[blocks.length - 1];
      if (prev && prev.t === t) prev.items.push(entry);
      else blocks.push({ t, start: Number(item[2] ?? 1), items: [entry] });
    } else if (quoted) {
      if (para.length) end();
      quote.push(quoted[1]);
    } else if (line.trim()) {
      if (quote.length) end();
      para.push(line.trim());
    } else {
      end();
    }
  }
  if (fence) blocks.push({ t: "pre", text: fence.join("\n") });
  end();
  return blocks;
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
    } else if (node.t === "em") {
      out.push(<em key={i}>{renderInline(node.kids, budget)}</em>);
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

const NO_LIMIT = { left: Infinity };

function renderBlock(block: Block, i: number): ReactNode {
  switch (block.t) {
    case "p":
      return <p key={i}>{renderInline(block.kids, NO_LIMIT)}</p>;
    case "quote":
      return <blockquote key={i}>{renderInline(block.kids, NO_LIMIT)}</blockquote>;
    case "h": {
      // The page's own heading is the h1: a markdown title becomes h2, and everything below it h3 or h4.
      const Tag = block.level <= 1 ? "h2" : block.level === 2 ? "h3" : "h4";
      return <Tag key={i}>{renderInline(block.kids, NO_LIMIT)}</Tag>;
    }
    case "pre":
      return (
        <pre key={i}>
          <code>{block.text}</code>
        </pre>
      );
    case "hr":
      return <hr key={i} />;
    default: {
      const items = block.items.map((item, j) => (
        <li key={j} data-depth={item.depth || undefined}>
          {renderInline(item.kids, NO_LIMIT)}
        </li>
      ));
      return block.t === "ol" ? (
        <ol key={i} start={block.start}>
          {items}
        </ol>
      ) : (
        <ul key={i}>{items}</ul>
      );
    }
  }
}

/** Block markdown. */
export function Markdown({ text }: { text: string }) {
  const blocks = useMemo(() => parseBlocks(text), [text]);
  return <>{blocks.map(renderBlock)}</>;
}

/**
 * Meeting notes begin with their own title and a line with the date; the card or the reader
 * already shows both, so the body is shown without them.
 */
export function withoutTitle(body: string): string {
  return body.replace(/^#[ \t]+[^\n]*\n+(?:[^\n#]*(?:\b\d{4}\b|\b\d{1,2}:\d{2}\b)[^\n]*\n+)?/, "");
}

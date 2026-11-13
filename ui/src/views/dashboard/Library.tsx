// Library: everything Jarvis made and kept. A list on one side, the thing itself on the other.

import { useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { type Item, LIBRARY_KINDS, type Library as LibraryData, type LibraryKind } from "../../api";
import { OpenIcon } from "../../components/icons";
import { ago, fileUrl, hhmm, longDate } from "../../format";
import { useSwap } from "../../hooks";
import { Markdown, safeLink, withoutTitle } from "../../markdown";
import type { DashboardData } from "./data";
import { Empty, PageHead } from "./parts";

const SHELF: Record<LibraryKind, { label: string; one: string }> = {
  preps: { label: "Prep sheets", one: "Prep sheet" },
  notes: { label: "Meeting notes", one: "Meeting notes" },
  briefs: { label: "Briefs", one: "Brief" },
  drafts: { label: "Drafts", one: "Draft" },
  files: { label: "Files", one: "File" },
};

/** An item to open straight away, when another section sends the reader here. */
export interface Focus {
  shelf: LibraryKind;
  id: Item["id"];
}

const firstFilled = (library: LibraryData | null) => LIBRARY_KINDS.find((kind) => library?.[kind].length);

export function Library({ data, focus }: { data: DashboardData; focus: Focus | null }) {
  const library = data.library;
  const [chosen, setChosen] = useState<LibraryKind | null>(focus?.shelf ?? null);
  const [pick, setPick] = useState<Item["id"] | null>(focus?.id ?? null);
  const [query, setQuery] = useState("");
  const shelf = chosen ?? firstFilled(library) ?? "preps";
  const all = library?.[shelf] ?? [];
  const needle = query.trim().toLowerCase();
  const items = needle
    ? all.filter((item) => `${item.title} ${item.engagement ?? ""}`.toLowerCase().includes(needle))
    : all;
  const selected = items.find((item) => item.id === pick) ?? items[0];
  const swap = useSwap(8);

  return (
    <>
      <PageHead title="Library" sub="What Jarvis prepared, wrote and saved." />

      <div className="tabs" role="tablist">
        {LIBRARY_KINDS.map((kind) => (
          <button
            key={kind}
            role="tab"
            className="tab"
            aria-selected={kind === shelf}
            onClick={() => {
              setChosen(kind);
              setPick(null);
            }}
          >
            {SHELF[kind].label}
            <span className="tab-count">{library?.[kind].length ?? 0}</span>
            {kind === shelf && <motion.i layoutId="library-tab" className="tab-line" />}
          </button>
        ))}
      </div>

      <div className="library">
        <div className="shelf-pane">
          <input
            className="search"
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={`Search ${SHELF[shelf].label.toLowerCase()}`}
            aria-label="Search this shelf"
          />
          {items.length === 0 ? (
            <Empty>
              {library ? (needle ? "Nothing matches." : `No ${SHELF[shelf].label.toLowerCase()} yet.`) : "Reading…"}
            </Empty>
          ) : (
            <ul className="shelf">
              {items.map((item, i) => (
                <motion.li
                  key={item.id}
                  initial={{ opacity: 0, x: -8 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ delay: Math.min(i, 8) * 0.03 }}
                >
                  <button className="shelf-item" aria-current={item === selected} onClick={() => setPick(item.id)}>
                    {item === selected && <motion.i layoutId="shelf-bar" className="shelf-bar" />}
                    <strong>{item.title}</strong>
                    <span className="shelf-meta">
                      {item.engagement && <span className="chip">{item.engagement}</span>}
                      {item.at && <time>{ago(item.at)}</time>}
                    </span>
                  </button>
                </motion.li>
              ))}
            </ul>
          )}
        </div>

        <div className="reader-pane">
          <AnimatePresence mode="wait" initial={false}>
            {selected ? (
              <motion.div key={`${shelf}:${selected.id}`} {...swap}>
                <Reader item={selected} shelf={shelf} />
              </motion.div>
            ) : (
              <Empty key="none">Pick something from the list to read it here.</Empty>
            )}
          </AnimatePresence>
        </div>
      </div>
    </>
  );
}

function Reader({ item, shelf }: { item: Item; shelf: LibraryKind }) {
  const [copied, setCopied] = useState(false);
  const body = item.body ? withoutTitle(item.body) : "";
  const href = safeLink(item.link) ?? (item.path ? fileUrl(item.path) : null);

  const copy = () => {
    void navigator.clipboard?.writeText(body).then(
      () => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      },
      () => undefined,
    );
  };

  return (
    <article className="reader">
      <header>
        <span className="tag">{SHELF[shelf].one}</span>
        <h2>{item.title}</h2>
        <p className="meta">
          {item.engagement && <span className="chip">{item.engagement}</span>}
          {item.at && (
            <time>
              {longDate(item.at)}, {hhmm(item.at)}
            </time>
          )}
        </p>
        {(href || body) && (
          <div className="actions">
            {href && (
              <a className="btn" data-augmented-ui="tl-clip br-clip border" href={href} target="_blank" rel="noreferrer">
                {safeLink(item.link) ? "Open" : "Open file"} <OpenIcon />
              </a>
            )}
            {body && (
              <button className="btn ghost" data-augmented-ui="tl-clip br-clip border" onClick={copy}>
                {copied ? "Copied" : "Copy text"}
              </button>
            )}
          </div>
        )}
      </header>
      {item.path && <p className="path">{item.path}</p>}
      {body ? (
        <div className="md read">
          <Markdown text={body} />
        </div>
      ) : (
        <Empty>There is no text to show for this one{item.path ? "; it is saved at the path above" : ""}.</Empty>
      )}
    </article>
  );
}

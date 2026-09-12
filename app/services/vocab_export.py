"""Anki-importable TSV export of the vocab list."""
from __future__ import annotations

import re
from typing import Any

HEADER = "#separator:tab\n#html:true\n#tags column:6\n"


def _clean(s: Any) -> str:
    return re.sub(r"[\t\r\n]+", " ", str(s or "")).strip()


def _bold(sentence: str, surface: str | None) -> str:
    s = _clean(sentence)
    if surface and surface.strip():
        pat = re.compile(re.escape(surface.strip()), re.IGNORECASE)
        s, n = pat.subn(lambda m: f"<b>{m.group(0)}</b>", s, count=1)
    return s


def build_tsv(rows: list[dict[str, Any]]) -> str:
    """rows: lemma, gloss_ko, gloss_en, sentence_de, sentence_ko, surface, video_id, start_ms, handle."""
    lines = [HEADER.rstrip("\n")]
    for r in rows:
        back_parts = [p for p in (_clean(r.get("gloss_ko")), _clean(r.get("gloss_en"))) if p]
        back = " / ".join(back_parts)
        vid = r.get("video_id")
        url = ""
        if vid:
            url = f"https://www.youtube.com/shorts/{vid}"
            if r.get("start_ms") is not None:
                url += f"?t={int(r['start_ms']) // 1000}"
        tags = "deutsch-shorts" + (f" {_clean(r['handle']).lstrip('@')}" if r.get("handle") else "")
        lines.append("\t".join([
            _clean(r.get("lemma")), back, _bold(r.get("sentence_de") or "", r.get("surface")),
            _clean(r.get("sentence_ko")), url, tags,
        ]))
    return "\n".join(lines) + "\n"

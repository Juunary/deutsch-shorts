"""Server side: public parallel corpora for the translation sub-task.

    python ml/corpora.py download           # OPUS zips -> ml/data/raw/
    python ml/corpora.py clean              # -> ml/data/work/pairs_de_{ko,en}.jsonl
    python ml/corpora.py sample --ko 150000 --en 50000   # -> translate_de_{ko,en}.jsonl (short, conversational first)
    python ml/corpora.py eval-sets          # Flores-200 devtest via huggingface_hub -> ml/data/work/flores_de_{ko,en}.jsonl

Sources (check the URLs before use; OPUS moves versions occasionally):
  OpenSubtitles v2018 de-ko / de-en  https://opus.nlpl.eu/OpenSubtitles  (Lison & Tiedemann 2016)
  TED2020 v1 de-ko                   https://opus.nlpl.eu/TED2020        (CC BY-NC-ND 4.0)
  Tatoeba v2023-04-12 de-ko          https://opus.nlpl.eu/Tatoeba        (CC BY 2.0 FR)
"""
from __future__ import annotations

import argparse
import io
import re
import zipfile
from pathlib import Path

from common import RAW, WORK, ensure_dirs, read_jsonl, sentence_split, stable_hash, write_jsonl  # noqa: E402

SOURCES = {
    "opensubtitles_de_ko": ("https://object.pouta.csc.fi/OPUS-OpenSubtitles/v2018/moses/de-ko.txt.zip", "de", "ko"),
    "opensubtitles_de_en": ("https://object.pouta.csc.fi/OPUS-OpenSubtitles/v2018/moses/de-en.txt.zip", "de", "en"),
    "ted2020_de_ko": ("https://object.pouta.csc.fi/OPUS-TED2020/v1/moses/de-ko.txt.zip", "de", "ko"),
    "tatoeba_de_ko": ("https://object.pouta.csc.fi/OPUS-Tatoeba/v2023-04-12/moses/de-ko.txt.zip", "de", "ko"),
}
HANGUL = re.compile(r"[가-힣]")
LATIN = re.compile(r"[A-Za-zÄÖÜäöüß]")
URL_OR_HTML = re.compile(r"https?://|www\.|<[^>]+>")


def download(names: list[str] | None = None) -> None:
    import httpx
    ensure_dirs()
    for name, (url, _, _) in SOURCES.items():
        if names and name not in names:
            continue
        dest = RAW / f"{name}.zip"
        if dest.exists():
            print("exists", dest)
            continue
        print("downloading", url)
        with httpx.stream("GET", url, follow_redirects=True, timeout=None) as r:
            r.raise_for_status()
            with dest.open("wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        print("saved", dest, dest.stat().st_size // (1 << 20), "MB")


def _moses_pairs(zip_path: Path, src: str, tgt: str):
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        f_src = next(n for n in names if n.endswith(f".{src}"))
        f_tgt = next(n for n in names if n.endswith(f".{tgt}"))
        with z.open(f_src) as a, z.open(f_tgt) as b:
            for la, lb in zip(io.TextIOWrapper(a, encoding="utf-8", errors="ignore"), io.TextIOWrapper(b, encoding="utf-8", errors="ignore")):
                yield la.strip(), lb.strip()


def keep_pair(de: str, tgt: str, lang: str) -> bool:
    if not (3 <= len(de) <= 200 and 3 <= len(tgt) <= 200):
        return False
    if de == tgt or URL_OR_HTML.search(de) or URL_OR_HTML.search(tgt):
        return False
    ratio = max(len(de), len(tgt)) / max(1, min(len(de), len(tgt)))
    if ratio > 3:
        return False
    if sum(c.isdigit() for c in de) > 0.3 * len(de):
        return False
    if not LATIN.search(de):
        return False
    if lang == "ko" and not HANGUL.search(tgt):
        return False
    if lang == "en" and not LATIN.search(tgt):
        return False
    return True


def clean() -> None:
    ensure_dirs()
    for lang in ("ko", "en"):
        seen: set[str] = set()
        rows = []
        for name, (_, src, tgt) in SOURCES.items():
            if tgt != lang or not (RAW / f"{name}.zip").exists():
                continue
            for de, t in _moses_pairs(RAW / f"{name}.zip", src, tgt):
                key = re.sub(r"\s+", " ", de.lower())
                if key in seen or not keep_pair(de, t, lang):
                    continue
                seen.add(key)
                rows.append({"de": de, lang: t, "source": name})
        n = write_jsonl(WORK / f"pairs_de_{lang}.jsonl", rows)
        print(f"{lang}: {n} clean pairs")


def sample(n_ko: int, n_en: int, seed: int = 13) -> None:
    import random
    rng = random.Random(seed)
    for lang, n in (("ko", n_ko), ("en", n_en)):
        rows = read_jsonl(WORK / f"pairs_de_{lang}.jsonl")
        if not rows:
            print(f"{lang}: no pairs (run clean first)")
            continue
        # prefer short conversational sentences: weight 1/(1+len/40)
        weighted = [(rng.random() ** (1.0 + len(r["de"]) / 40.0), r) for r in rows]
        weighted.sort(key=lambda x: x[0], reverse=True)
        picked = [r for _, r in weighted[:n]]
        for r in picked:
            r["split"] = sentence_split(r["de"], val_fraction=0.01, test_fraction=0.01)
        print(f"{lang}: sampled {write_jsonl(WORK / f'translate_de_{lang}.jsonl', picked)}")


def eval_sets() -> None:
    """Flores-200 devtest (de -> ko/en) as a clean translation benchmark."""
    from huggingface_hub import hf_hub_download  # heavy import kept local
    ensure_dirs()
    files = {}
    for code in ("deu_Latn", "kor_Hang", "eng_Latn"):
        files[code] = Path(hf_hub_download("facebook/flores", f"devtest/{code}.devtest", repo_type="dataset"))
    de = files["deu_Latn"].read_text(encoding="utf-8").splitlines()
    for lang, code in (("ko", "kor_Hang"), ("en", "eng_Latn")):
        tgt = files[code].read_text(encoding="utf-8").splitlines()
        write_jsonl(WORK / f"flores_de_{lang}.jsonl", [{"de": d, lang: t} for d, t in zip(de, tgt)])
        print(lang, len(tgt))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download"); d.add_argument("--only", nargs="*")
    sub.add_parser("clean")
    s = sub.add_parser("sample"); s.add_argument("--ko", type=int, default=150000); s.add_argument("--en", type=int, default=50000)
    sub.add_parser("eval-sets")
    a = ap.parse_args()
    if a.cmd == "download":
        download(a.only)
    elif a.cmd == "clean":
        clean()
    elif a.cmd == "sample":
        sample(a.ko, a.en)
    elif a.cmd == "eval-sets":
        eval_sets()


if __name__ == "__main__":
    main()

from __future__ import annotations

import json
import os
import re
import unicodedata

from config import ServerConfig


def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default).strip()


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)).strip())
    except Exception:
        return default


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key, "1" if default else "0").strip().lower()
    return v in ("1", "true", "yes", "on")

_CFG = None


def init(cfg) -> None:
    global _CFG
    _CFG = cfg if cfg is not None else ServerConfig()


def _cfg():
    return _CFG if _CFG is not None else ServerConfig()


def _open_phrases_str() -> str:
    cfg = _cfg()
    default = cfg.open_phrases or cfg.open_phrases
    # env has priority (main.py passes cfg via env)
    return _env_str(
        "DVC_OPEN_PHRASES",
        _env_str("DVC_OPEN_PHRASES", str(default)),
    )


def _open_score_thr() -> float:
    return _env_float("DVC_OPEN_SCORE_THR", float(_cfg().open_score_thr))


def _close_phrases_str() -> str:
    default = _cfg().close_phrases
    return _env_str("DVC_CLOSE_PHRASES", str(default))


def _close_enable_voice() -> bool:
    return _env_bool("DVC_CLOSE_ENABLE_VOICE", bool(_cfg().close_enable_voice))


def _close_score_thr() -> float:
    # Backward-compatible: old behavior effectively used 0.5 for multi-token phrases.
    return _env_float("DVC_CLOSE_SCORE_THR", float(_cfg().close_score_thr))


def _min_score() -> float:
    return _env_float("DVC_DIALOGUE_SELECT_SCORE_THR", float(_cfg().dialogue_select_score_thr))


def _min_diff() -> float:
    return _env_float("DVC_DIALOGUE_SELECT_MIN_DIFF", float(_cfg().dialogue_select_min_diff))


def open_score_threshold() -> float:
    return _open_score_thr()


def close_score_threshold() -> float:
    return _close_score_thr()


def min_score_threshold() -> float:
    return _min_score()


def min_diff_threshold() -> float:
    return _min_diff()


def normalize(text: str) -> str:
    s = unicodedata.normalize("NFKC", text or "")
    s = s.lower()
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[?\!\.:,;\"'\(\)\[\]\{\}<>/\\-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def tokens(text: str) -> list[str]:
    s = normalize(text)
    return [t for t in s.split(" ") if t]


def _dialogue_score(asr_text: str, option: str) -> tuple[float, int]:
    r = set(tokens(asr_text))
    o = set(tokens(option))
    overlap = len(r & o)
    score = overlap / max(1, len(r))
    return score, overlap


def _best_match_scores(text: str, options: list[str]) -> list[tuple[float, int, str, int]]:
    scores: list[tuple[float, int, str, int]] = []
    for i, opt in enumerate(options):
        sc, overlap = _dialogue_score(text, opt)
        scores.append((sc, i, opt, overlap))
    scores.sort(reverse=True, key=lambda x: x[0])
    return scores


def get_open_phrases_list() -> list[str]:
    s = _open_phrases_str()
    phrases: list[str] = []
    for p in str(s).split(","):
        n = normalize(p)
        if n:
            phrases.append(n)
    return phrases


def get_close_phrases_list() -> list[str]:
    s = _close_phrases_str()
    phrases: list[str] = []
    for p in str(s).split(","):
        n = normalize(p)
        if n:
            phrases.append(n)
    return phrases


def _phrases_from_csv(value: str) -> list[str]:
    phrases: list[str] = []
    seen: set[str] = set()
    for p in str(value or "").split(","):
        n = normalize(p)
        if n and n not in seen:
            seen.add(n)
            phrases.append(n)
    return phrases


def get_pause_phrases_list() -> list[str]:
    cfg = _cfg()
    return _phrases_from_csv(_env_str("DVC_PAUSE_PHRASES", str(cfg.pause_phrases)))


def get_resume_phrases_list() -> list[str]:
    cfg = _cfg()
    return _phrases_from_csv(_env_str("DVC_RESUME_PHRASES", str(cfg.resume_phrases)))


def match_exact_phrase(text: str, phrase_list: list[str]) -> tuple[bool, float, str, float, str]:
    ntext = normalize(text)
    best_score = 0.0
    best_phrase = ""
    if not ntext:
        return False, 0.0, "", 0.0, ""

    text_tokens = set(tokens(ntext))
    for phrase in phrase_list:
        pnorm = normalize(phrase)
        if not pnorm:
            continue
        if ntext == pnorm:
            return True, 1.0, pnorm, 1.0, pnorm
        ptokens = set(tokens(pnorm))
        if not ptokens:
            continue
        score = len(text_tokens & ptokens) / len(ptokens)
        if score > best_score:
            best_score = float(score)
            best_phrase = pnorm

    return False, best_score, best_phrase, 1.0, best_phrase


def match_pause(text: str) -> tuple[bool, float, str, float, str]:
    return match_exact_phrase(text, get_pause_phrases_list())


def match_resume(text: str) -> tuple[bool, float, str, float, str]:
    return match_exact_phrase(text, get_resume_phrases_list())


def _evaluate_phrase_match(ntext: str, text_tokens: set[str], phrase: str) -> tuple[bool, float, str]:
    pnorm = normalize(phrase)
    if not pnorm:
        return False, 0.0, ""

    ptokens = [t for t in pnorm.split(" ") if t]
    if not ptokens:
        return False, 0.0, ""

    if len(ptokens) == 1:
        hit = (ptokens[0] in text_tokens)
        return hit, (1.0 if hit else 0.0), pnorm

    if pnorm in ntext:
        return True, 1.0, pnorm

    overlap = len(text_tokens & set(ptokens)) / len(ptokens)
    return False, overlap, pnorm


def _match_phrase_by_overlap(text: str, phrase_list: list[str], threshold: float) -> tuple[bool, float, str]:
    ntext = normalize(text)
    if not ntext:
        return False, 0.0, ""

    text_tokens = {t for t in ntext.split(" ") if t}
    if not text_tokens:
        return False, 0.0, ""

    best_score = 0.0
    best_phrase = ""

    for phrase in phrase_list:
        matched, score, pnorm = _evaluate_phrase_match(ntext, text_tokens, phrase)
        if not pnorm:
            continue

        if matched:
            return True, score, pnorm

        if score > best_score:
            best_score = score
            best_phrase = pnorm

    return best_score >= threshold, best_score, best_phrase


def match_open_phrase(text: str, open_list: list[str]) -> tuple[bool, float, str]:
    return _match_phrase_by_overlap(text, open_list, _open_score_thr())


def match_open(text: str) -> tuple[bool, float, str]:
    return match_open_phrase(text, get_open_phrases_list())


def match_close_phrase(text: str, close_list: list[str]) -> tuple[bool, float, str]:
    ntext = normalize(text)
    if not ntext:
        return False, 0.0, ""

    text_tokens = {t for t in ntext.split(" ") if t}
    if not text_tokens:
        return False, 0.0, ""

    threshold = _close_score_thr()
    best_score = 0.0
    best_phrase = ""

    for phrase in close_list:
        pnorm = normalize(phrase)
        if not pnorm:
            continue

        ptokens = [t for t in pnorm.split(" ") if t]
        if not ptokens:
            continue

        if len(ptokens) == 1:
            if ptokens[0] in text_tokens:
                return True, 1.0, pnorm
            continue

        if pnorm in ntext:
            return True, 1.0, pnorm

        overlap_cnt = len(text_tokens & set(ptokens))
        score = overlap_cnt / len(ptokens)

        if overlap_cnt >= 2 and score >= threshold:
            return True, score, pnorm

        if score > best_score:
            best_score = score
            best_phrase = pnorm

    return False, best_score, best_phrase


def match_close(text: str) -> tuple[bool, float, str]:
    return match_close_phrase(text, get_close_phrases_list())


def is_close(text: str) -> bool:
    if not _close_enable_voice():
        return False
    return match_close(text)[0]


def best_dialogue_option(text: str, options: list[str]) -> tuple[int, float]:
    if not options:
        return -1, 0.0

    scores = _best_match_scores(text, options)
    if not scores:
        return -1, 0.0

    sc1, idx0, _opt0, overlap0 = scores[0]
    sc2 = scores[1][0] if len(scores) > 1 else 0.0
    diff = sc1 - sc2

    if (overlap0 >= 2 or sc1 >= _min_score()) and diff >= _min_diff():
        return idx0, float(sc1)

    return -1, 0.0


def dialogue_match_diagnostics(text: str, options: list[str]) -> dict:
    ntext = normalize(text)
    if not ntext:
        return {"reason": "empty_text", "score": 0.0}
    if not options:
        return {"reason": "no_options", "score": 0.0}

    scores = _best_match_scores(text, options)
    if not scores:
        return {"reason": "no_options", "score": 0.0}

    sc1, idx0, opt0, overlap0 = scores[0]
    sc2 = scores[1][0] if len(scores) > 1 else 0.0
    diff = sc1 - sc2
    min_score = _min_score()
    min_diff = _min_diff()
    score_ok = overlap0 >= 2 or sc1 >= min_score
    diff_ok = diff >= min_diff

    reason = "ok"
    threshold = None
    if not score_ok:
        reason = "below_threshold"
        threshold = min_score
    elif not diff_ok:
        reason = "ambiguous"

    return {
        "reason": reason,
        "index": idx0,
        "option": opt0,
        "score": float(sc1),
        "threshold": threshold,
        "second_score": float(sc2),
        "diff": float(diff),
        "min_diff": float(min_diff),
        "overlap": int(overlap0),
    }


def rank_dialogue_options(text: str, options: list[str]) -> list[tuple[float, int, str]]:
    return [(sc, idx, opt) for sc, idx, opt, _overlap in _best_match_scores(text, options)]


def _append_unique_phrase(phrases: list[str], seen: set[str], parts: list[str]) -> None:
    phrase = " ".join(parts)
    if phrase and phrase not in seen:
        seen.add(phrase)
        phrases.append(phrase)


def _tail_tokens(tok: list[str], size: int) -> list[str] | None:
    if len(tok) < size:
        return None
    return tok[-size:]


def build_dialog_grammar_phrases(options: list[str]) -> list[str]:
    phrases: list[str] = []
    seen: set[str] = set()
    for opt in options:
        if not isinstance(opt, str):
            continue
        tok = tokens(opt)
        if not tok:
            continue

        _append_unique_phrase(phrases, seen, tok)
        for size in (2, 3):
            tail = _tail_tokens(tok, size)
            if tail is not None:
                _append_unique_phrase(phrases, seen, tail)

    return phrases


def dialog_grammar_json(options: list[str]) -> str | None:
    phrases = build_dialog_grammar_phrases(options)
    if not phrases:
        return None
    return json.dumps(phrases, ensure_ascii=False)

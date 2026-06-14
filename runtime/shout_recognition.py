from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np

from log_utils import setup_timestamped_print, log_debug, log_warn

setup_timestamped_print()

SR = 16000

# Limits for grammar building
MAX_SHOUT_WORDS = 3
MAX_VARIANTS_PER_ATOM = 12
MAX_PHRASES_PER_SHOUT = 120

# Two-phase recognition thresholds
PHASE_B_MIN_SCORE_1WORD = 0.5
PHASE_B_MIN_SCORE_2WORD = 0.45
PHASE_B_MIN_SCORE_3WORD = 0.40


def _runtime_dir() -> Path:
    raw = os.environ.get("DVC_APP_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    if bool(getattr(sys, "frozen", False)):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


# -----------------------------------------------------------------------------
# Grammar loading and validation
# -----------------------------------------------------------------------------


def _normalize_variant_list(variants: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for s in variants:
        if not isinstance(s, str):
            continue
        s2 = " ".join(s.strip().lower().split())
        if not s2 or s2 in seen:
            continue
        seen.add(s2)
        out.append(s2)
    return out


def _sanitize_atoms(atoms: object) -> dict[str, list[str]]:
    if not isinstance(atoms, dict):
        return {}
    atoms_out: dict[str, list[str]] = {}
    for tok, variants in atoms.items():
        if not isinstance(tok, str) or not tok.strip() or not isinstance(variants, list):
            continue
        vv = _normalize_variant_list(variants)
        if vv:
            atoms_out[tok.strip().upper()] = vv
    return atoms_out


def _sanitize_langs(langs_map: object) -> dict[str, dict[str, list[str]]]:
    if not isinstance(langs_map, dict):
        return {}
    out: dict[str, dict[str, list[str]]] = {}
    for lang, atoms in langs_map.items():
        if not isinstance(lang, str) or not lang.strip():
            continue
        atoms_out = _sanitize_atoms(atoms)
        if atoms_out:
            out[lang.strip().lower()] = atoms_out
    return out


def load_shout_grammar(path: str | Path) -> dict[str, dict[str, dict[str, list[str]]]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise FileNotFoundError(f"Grammar file not found: {path}")

    if not isinstance(data, dict):
        raise ValueError("shout_grammar.json must be a JSON object")

    out: dict[str, dict[str, dict[str, list[str]]]] = {}
    for shout_name, langs_map in data.items():
        if not isinstance(shout_name, str) or not shout_name.strip():
            raise ValueError("Grammar keys must be non-empty strings")
        if not isinstance(langs_map, dict):
            raise ValueError(f"Grammar entry '{shout_name}' must be an object mapping language->atoms")

        langs = _sanitize_langs(langs_map)
        if langs:
            out[shout_name.strip().upper()] = langs

    return out


def load_shouts_map(path: str | Path) -> dict[str, dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise FileNotFoundError(f"Shouts map file not found: {path}")

    if not isinstance(data, dict):
        raise ValueError("shouts_map.json must be a JSON object")

    out: dict[str, dict] = {}
    for k, v in data.items():
        if isinstance(k, str) and isinstance(v, dict):
            out[k.strip().upper()] = v
    return out


# -----------------------------------------------------------------------------
# Helper functions
# -----------------------------------------------------------------------------


def _norm_word(w: str) -> str:
    w = (w or "").strip().lower()
    w = w.replace("ё", "е")
    w = re.sub(r"[^a-zа-я]", "", w)
    return w


def _cap_variants(variants: list[str], cap: int) -> list[str]:
    if len(variants) <= cap:
        return variants
    return variants[:cap]


def _build_shout_phrases(words: list[list[str]], max_phrases: int) -> list[str]:
    max_levels = max(1, min(MAX_SHOUT_WORDS, len(words)))
    out: list[str] = []

    level_phrases: list[list[str]] = [[]]
    for i in range(max_levels):
        new_level: list[list[str]] = []
        for prev in level_phrases:
            for v in words[i]:
                p = prev + [str(v).strip()]
                new_level.append(p)
                out.append(" ".join(p))
                if len(out) >= max_phrases:
                    return out[:max_phrases]
        level_phrases = new_level
    return out[:max_phrases]


def _build_alias_map_from_atoms(atoms: dict[str, list[str]] | None, shout_tokens: list[str]) -> dict[str, str]:
    if not atoms:
        return {}

    al: dict[str, str] = {}
    for tok in shout_tokens:
        for v in atoms.get(tok) or []:
            k = _norm_word(str(v))
            if k:
                al[k] = tok
        # token itself as fallback
        k2 = _norm_word(tok)
        if k2:
            al[k2] = tok
    return al


def _normalize_shout_words(words: list[str], shout_tokens: list[str], atoms: dict[str, list[str]] | None) -> list[str]:
    aliases = _build_alias_map_from_atoms(atoms, shout_tokens)
    mapped: list[str] = []
    for w in words or []:
        k = _norm_word(str(w))
        if not k:
            continue
        tok = aliases.get(k)
        if tok:
            mapped.append(tok)
    if not mapped:
        return []
    # de-dup consecutive
    dedup: list[str] = []
    for tok in mapped:
        if not dedup or dedup[-1] != tok:
            dedup.append(tok)
    return dedup


def _matched_prefix_len(tokens: list[str], shout_tokens: list[str]) -> int:
    seq = [str(t).strip().upper() for t in (tokens or []) if str(t).strip()]
    exp = [str(t).strip().upper() for t in (shout_tokens or []) if str(t).strip()]
    if not seq or not exp:
        return 0
    if seq[0] != exp[0]:
        return 0
    m = 0
    for i in range(min(len(seq), len(exp), MAX_SHOUT_WORDS)):
        if seq[i] != exp[i]:
            break
        m += 1
    return int(max(0, min(MAX_SHOUT_WORDS, m)))


def _extract_first_word(raw_text: str, raw_words: list[str]) -> str | None:
    if raw_words:
        return str(raw_words[0]).strip().lower() or None
    if raw_text:
        parts = str(raw_text).split()
        if parts:
            return parts[0].strip().lower() or None
    return None


def _collect_phase_a_candidates(first_word: str | None, first_word_to_shouts: dict[str, list[str]]) -> list[str]:
    key = (first_word or "").strip().lower()
    if not key or key not in first_word_to_shouts:
        return []
    out: list[str] = []
    for shout in first_word_to_shouts.get(key, []):
        if shout not in out:
            out.append(shout)
    return out


def _add_unique_phrases(target: list[str], seen: set[str], phrases: list[str]) -> None:
    for ph in phrases:
        key = " ".join(str(ph).strip().lower().split())
        if not key or key in seen:
            continue
        seen.add(key)
        target.append(key)


def _build_phase_b_payload(candidates: list[str], all_grammar: dict, lang: str):
    use_lang = (lang or "").strip().lower()
    phase_b_shouts: list[str] = []
    per_shout_tokens: dict[str, list[str]] = {}
    per_shout_atoms: dict[str, dict[str, list[str]]] = {}
    combined_phrases: list[str] = []
    seen_phrase: set[str] = set()

    for shout_name in candidates:
        shout_u = str(shout_name).strip().upper()
        langs_map = (all_grammar or {}).get(shout_u) or {}
        atoms = (langs_map or {}).get(use_lang)
        if not isinstance(atoms, dict) or not atoms:
            continue

        phase_b_grammar, shout_tokens = build_shout_specific_grammar(shout_u, atoms)
        if not phase_b_grammar:
            continue

        phase_b_shouts.append(shout_u)
        per_shout_tokens[shout_u] = list(shout_tokens)
        per_shout_atoms[shout_u] = atoms
        _add_unique_phrases(combined_phrases, seen_phrase, phase_b_grammar)

    return phase_b_shouts, per_shout_tokens, per_shout_atoms, combined_phrases


def _phase_b_min_score(word_count: int) -> float:
    if word_count == 1:
        return PHASE_B_MIN_SCORE_1WORD
    if word_count == 2:
        return PHASE_B_MIN_SCORE_2WORD
    return PHASE_B_MIN_SCORE_3WORD


def _extract_first_token_variants(shout_name: str, langs_map: dict, use_lang: str) -> list[str]:
    atoms = (langs_map or {}).get(use_lang)
    if not isinstance(atoms, dict) or not atoms:
        return []

    tokens = [t for t in str(shout_name).split("_") if t][:MAX_SHOUT_WORDS]
    if not tokens:
        return []

    first_token = tokens[0]
    return list(atoms.get(first_token) or [])


def _pick_best_phase_b_result(
    *,
    phase_b_shouts: list[str],
    per_shout_tokens: dict[str, list[str]],
    per_shout_atoms: dict[str, dict[str, list[str]]],
    phase_b_words: list[str],
    score_a: float,
    score_b: float,
    candidates: list[str],
) -> Optional[TwoPhaseResult]:
    best_result: Optional[TwoPhaseResult] = None
    best_len = -1

    for shout_u in phase_b_shouts:
        atoms = per_shout_atoms.get(shout_u)
        shout_tokens = per_shout_tokens.get(shout_u) or [t for t in shout_u.split("_") if t][:MAX_SHOUT_WORDS]
        canon = _normalize_shout_words(phase_b_words, shout_tokens, atoms)
        matched_len = int(len(canon))
        word_count = int(matched_len)
        valid = bool(word_count > 0)
        reason = "OK" if valid else "PHASE_B_NO_PREFIX"

        min_score = _phase_b_min_score(word_count)
        if valid and float(score_b) < float(min_score):
            valid = False
            reason = f"PHASE_B_LOW_SCORE ({float(score_b):.2f} < {min_score})"

        candidate_result = TwoPhaseResult(
            shout_name=shout_u,
            word_count=word_count,
            matched_len=matched_len,
            tokens=list(canon),
            score_a=float(score_a),
            score_b=float(score_b),
            candidates=list(candidates),
            valid=bool(valid),
            reason=str(reason),
        )
        if candidate_result.valid and matched_len > best_len:
            best_len = matched_len
            best_result = candidate_result

    return best_result


# -----------------------------------------------------------------------------
# Grammar building for two-phase recognition
# -----------------------------------------------------------------------------


def build_first_word_grammar(
    all_grammar: dict,
    lang: str,
) -> tuple[list[str], dict[str, list[str]]]:
    first_words: set[str] = set()
    first_word_to_shouts: dict[str, list[str]] = {}

    use_lang = (lang or "").strip().lower()
    for shout_name, langs_map in (all_grammar or {}).items():
        variants = _extract_first_token_variants(str(shout_name), langs_map or {}, use_lang)
        if not variants:
            continue

        vv = _cap_variants(list(variants), MAX_VARIANTS_PER_ATOM)
        for v in vv:
            first_words.add(v)
            lst = first_word_to_shouts.setdefault(v, [])
            if shout_name not in lst:
                lst.append(shout_name)

    return sorted(first_words), first_word_to_shouts


def build_shout_specific_grammar(
    shout_name: str,
    atoms: dict[str, list[str]],
) -> tuple[list[str], list[str]]:
    shout_tokens = [t for t in (shout_name or "").split("_") if t][:MAX_SHOUT_WORDS]
    words: list[list[str]] = []
    for tok in shout_tokens:
        variants = (atoms or {}).get(tok)
        if not variants:
            return [], shout_tokens
        words.append(_cap_variants(list(variants), MAX_VARIANTS_PER_ATOM))

    grammar_list = _build_shout_phrases(words, max_phrases=MAX_PHRASES_PER_SHOUT)
    return grammar_list, shout_tokens


# -----------------------------------------------------------------------------
# Vosk recognition helpers
# -----------------------------------------------------------------------------


def _vosk_recognize_raw(
    *,
    model,  # VoskModel
    pcm16: np.ndarray,
    grammar_list: list[str],
    sr: int = SR,
) -> tuple[str, list[str], list[float], float, dict]:
    from vosk import KaldiRecognizer

    rec = KaldiRecognizer(model, int(sr), json.dumps(grammar_list, ensure_ascii=False))
    rec.SetWords(True)

    rec.AcceptWaveform(np.asarray(pcm16, dtype=np.int16).tobytes())
    raw = json.loads(rec.FinalResult() or "{}")

    raw_text = str(raw.get("text") or "").strip()

    items = raw.get("result") or []
    raw_words: list[str] = []
    confs: list[float] = []
    for it in items:
        try:
            w = it.get("word")
            if w:
                raw_words.append(str(w))
            c = it.get("conf")
            if isinstance(c, (int, float)):
                confs.append(float(c))
        except Exception:
            continue

    mean_score = (float(sum(confs)) / float(len(confs))) if confs else 0.0
    return raw_text, raw_words, confs, float(mean_score), raw


# -----------------------------------------------------------------------------
# Two-phase Vosk recognition result
# -----------------------------------------------------------------------------


@dataclass
class TwoPhaseResult:

    shout_name: str = ""
    word_count: int = 0
    matched_len: int = 0
    tokens: list[str] = field(default_factory=list)
    score_a: float = 0.0
    score_b: float = 0.0
    candidates: list[str] = field(default_factory=list)
    valid: bool = False
    reason: str = "NOT_RUN"

    @property
    def combined_score(self) -> float:
        if not self.valid:
            return 0.0
        ml = int(self.matched_len or self.word_count)
        if ml <= 1:
            return float(self.score_a)
        return float(self.score_b)


def two_phase_recognize(
    model,  # VoskModel
    pcm16: np.ndarray,
    all_grammar: dict,
    lang: str,
    phase_a_grammar: list[str],
    first_word_to_shouts: dict[str, list[str]],
    sr: int = SR,
) -> TwoPhaseResult:
    result = TwoPhaseResult()
    if pcm16.size == 0:
        result.reason = "EMPTY_AUDIO"
        return result

    # Phase A
    raw_text_a, raw_words_a, _confs_a, score_a, _ = _vosk_recognize_raw(
        model=model,
        pcm16=pcm16,
        grammar_list=phase_a_grammar,
        sr=sr,
    )
    result.score_a = float(score_a)

    first_word = _extract_first_word(raw_text_a, raw_words_a)

    if not first_word:
        result.reason = "PHASE_A_NO_WORD"
        return result

    candidates = _collect_phase_a_candidates(first_word, first_word_to_shouts)

    if not candidates:
        result.reason = f"PHASE_A_UNKNOWN_WORD ({first_word})"
        return result

    result.candidates = list(candidates)
    phase_a_shout = str(candidates[0]).strip().upper() if candidates else ""

    phase_b_shouts, per_shout_tokens, per_shout_atoms, combined_phrases = _build_phase_b_payload(candidates, all_grammar, lang)

    if not combined_phrases or not phase_b_shouts:
        result.reason = "PHASE_B_EMPTY_GRAMMAR"
        return result

    # Phase B
    raw_text_b, raw_words_b, _confs_b, score_b, _ = _vosk_recognize_raw(
        model=model,
        pcm16=pcm16,
        grammar_list=combined_phrases,
        sr=sr,
    )
    result.score_b = float(score_b)

    phase_b_words_raw = [str(w) for w in (raw_words_b or str(raw_text_b).split()) if str(w).strip()]
    phase_b_words = [" ".join(str(w).strip().lower().split()) for w in phase_b_words_raw if str(w).strip()]

    best_result = _pick_best_phase_b_result(
        phase_b_shouts=phase_b_shouts,
        per_shout_tokens=per_shout_tokens,
        per_shout_atoms=per_shout_atoms,
        phase_b_words=phase_b_words,
        score_a=score_a,
        score_b=score_b,
        candidates=candidates,
    )

    if best_result is None or not bool(best_result.valid):
        result.reason = "PHASE_B_NO_VALID_CANDIDATE"
        return result

    # power==1 rule: final shout_id comes ONLY from Phase A
    if int(best_result.word_count) == 1 and phase_a_shout:
        best_result.shout_name = phase_a_shout
        phase_a_tokens = per_shout_tokens.get(phase_a_shout) or [t for t in phase_a_shout.split("_") if t][:MAX_SHOUT_WORDS]
        if phase_a_tokens:
            best_result.tokens = [phase_a_tokens[0]]
            best_result.word_count = 1
            best_result.matched_len = 1
            best_result.reason = "OK_POWER1_PHASE_A"

    return best_result


# -----------------------------------------------------------------------------
# Oracle one-phase for shout validation
# -----------------------------------------------------------------------------


def oracle_one_phase_for_shout(
    *,
    pcm16: np.ndarray,
    sr: int,
    model,  # VoskModel
    all_grammar: dict,
    lang: str,
    shout_id: str,
) -> tuple[int, float, list[str]]:
    sid, atoms, shout_tokens = _resolve_shout_atoms_and_tokens(all_grammar=all_grammar, lang=lang, shout_id=shout_id)
    if not sid or not atoms or not shout_tokens:
        return 0, 0.0, []

    grammar_list = _build_single_shout_grammar_list(shout_tokens, atoms)
    if not grammar_list:
        return 0, 0.0, []

    raw_text, raw_words, _confs, score, _ = _vosk_recognize_raw(
        model=model,
        pcm16=pcm16,
        grammar_list=grammar_list,
        sr=sr,
    )

    if (not raw_words) and raw_text:
        raw_words = [w for w in raw_text.split() if str(w).strip()]

    canon = _normalize_shout_words(raw_words, shout_tokens, atoms)
    matched_len = _matched_prefix_len(canon, shout_tokens)
    return matched_len, float(score), canon[:matched_len]


def build_oracle_grammar_list_for_shout(
    *,
    all_grammar: dict,
    lang: str,
    shout_id: str,
) -> list[str]:
    sid, atoms, shout_tokens = _resolve_shout_atoms_and_tokens(all_grammar=all_grammar, lang=lang, shout_id=shout_id)
    if not sid or not atoms or not shout_tokens:
        return []
    return list(_build_single_shout_grammar_list(shout_tokens, atoms) or [])


def _resolve_shout_atoms_and_tokens(*, all_grammar: dict, lang: str, shout_id: str):
    sid = str(shout_id or "").strip().upper()
    use_lang = str(lang or "").strip().lower()
    if not sid or not isinstance(all_grammar, dict) or sid not in all_grammar:
        return "", None, []

    langs_map = all_grammar.get(sid) or {}
    atoms = langs_map.get(use_lang) if isinstance(langs_map, dict) else None
    if not atoms and isinstance(langs_map, dict):
        try:
            first_lang = next(iter(langs_map.keys()))
            atoms = langs_map.get(first_lang)
        except Exception:
            atoms = None

    shout_tokens = [t for t in sid.split("_") if t][:MAX_SHOUT_WORDS]
    if not atoms or not shout_tokens:
        return "", None, []
    return sid, atoms, shout_tokens


def _build_single_shout_grammar_list(shout_tokens: list[str], atoms: dict[str, list[str]]) -> list[str]:
    words: list[list[str]] = []
    for tok in shout_tokens:
        variants = atoms.get(tok)
        if not variants:
            return []
        vv = [str(v).strip() for v in (variants or []) if str(v).strip()]
        vv = _cap_variants(vv, MAX_VARIANTS_PER_ATOM)
        if not vv:
            return []
        words.append(vv)
    return _build_shout_phrases(words, max_phrases=MAX_PHRASES_PER_SHOUT)


def _parse_hex_formid(fid_value: object) -> int | None:
    fid = str(fid_value or "").strip()
    if not fid:
        return None

    raw = fid[2:] if fid.lower().startswith("0x") else fid
    try:
        return int(raw, 16)
    except Exception:
        return None


def _canonical_formid_key(fid_value: object) -> str | None:
    val = _parse_hex_formid(fid_value)
    if val is None:
        raw = str(fid_value or "").strip()
        return raw.strip().lower() or None
    return f"0x{val:08x}"


def _normalize_plugin_name(raw: object) -> str:
    return str(raw or "").strip().lower()


def _canonical_shout_key(plugin_value: object, formid_value: object) -> str | None:
    plugin = _normalize_plugin_name(plugin_value)
    if not plugin:
        return None

    val = _parse_hex_formid(formid_value)
    if val is None:
        return None

    base = val & 0x00FFFFFF
    return f"{plugin}|0x{base:06x}"


def _format_baseid(val: int) -> str:
    base = int(val) & 0x00FFFFFF
    return f"0x{base:06x}"


def _split_shout_key(key: str) -> tuple[str, str] | None:
    raw = str(key or "").strip().lower()
    if "|" not in raw:
        return None
    plugin, base = raw.split("|", 1)
    if not plugin or not base:
        return None
    base_val = _parse_hex_formid(base)
    if base_val is None:
        return None
    return plugin, _format_baseid(base_val)


# -----------------------------------------------------------------------------
# Main ShoutRecognizer class
# -----------------------------------------------------------------------------


class ShoutRecognizer:

    def __init__(
        self,
        *,
        backend: str = "vosk",
        lang: str = "ru",
        vosk_model=None,
        grammar_path: str | Path | None = None,
        shouts_map_path: str | Path | None = None,
    ):
        self.backend = "vosk"
        self.lang = lang.strip().lower()
        self._vosk_model = vosk_model

        self._grammar_all: dict | None = None
        self._grammar: dict | None = None  # effective grammar (possibly filtered)
        self._shouts_map: dict | None = None
        self._grammar_path = grammar_path
        self._shouts_map_path = shouts_map_path
        self._effective_lang: str | None = None
        self._logged_lang_fallback = False

        self._allowed_formids: set[str] | None = None
        self._allowed_shout_ids: set[str] | None = None
        self._formid_to_shout_id: dict[str, str] | None = None
        self._dynamic_formid_to_shout_id: dict[str, str] | None = None  # from pipe entries
        self._dynamic_shout_info: dict[str, tuple[str, str]] | None = None  # shout_id -> (plugin, baseid)

        # Phase A grammar (built lazily)
        self._phase_a_grammar: list[str] | None = None
        self._first_word_to_shouts: dict[str, list[str]] | None = None

    def _ensure_grammar(self) -> dict:
        if self._grammar is not None:
            return self._grammar

        if self._grammar_all is None:
            if self._grammar_path:
                self._grammar_all = load_shout_grammar(self._grammar_path)
            else:
                default_path = _runtime_dir() / "shout_grammar.json"
                self._grammar_all = load_shout_grammar(default_path) if default_path.exists() else {}

        # Build effective grammar (filtered if allow-list is configured)
        if self._allowed_shout_ids is None:
            self._grammar = dict(self._grammar_all or {})
        else:
            base = self._grammar_all or {}
            self._grammar = {k: v for k, v in base.items() if str(k).strip().upper() in self._allowed_shout_ids}

        self._effective_lang = None
        return self._grammar

    def _available_grammar_langs(self, grammar: dict) -> set[str]:
        langs: set[str] = set()
        for _sid, langs_map in (grammar or {}).items():
            if not isinstance(langs_map, dict):
                continue
            for lang in langs_map.keys():
                if isinstance(lang, str) and lang.strip():
                    langs.add(lang.strip().lower())
        return langs

    def _resolve_effective_lang(self) -> str:
        if self._effective_lang:
            return self._effective_lang

        grammar = self._ensure_grammar()
        available = self._available_grammar_langs(grammar)
        requested = (self.lang or "").strip().lower()

        if not requested:
            requested = "en"

        if requested in available or not available:
            self._effective_lang = requested
            return self._effective_lang

        fallback = "en" if "en" in available else ("ru" if "ru" in available else next(iter(available), requested))

        if fallback != requested and not self._logged_lang_fallback:
            log_warn(f"[SHOUT][WARN] Shouts grammar fallback: requested={requested} -> {fallback}")
            self._logged_lang_fallback = True

        self._effective_lang = fallback
        return self._effective_lang

    def _invalidate_phase_a(self) -> None:
        self._phase_a_grammar = None
        self._first_word_to_shouts = None

    def _ensure_formid_index(self) -> dict[str, str]:
        if self._formid_to_shout_id is not None:
            return self._formid_to_shout_id

        idx: dict[str, str] = {}

        # Load from shouts_map.json if available (legacy fallback)
        shouts_map = self._ensure_shouts_map()
        for sid, entry in (shouts_map or {}).items():
            try:
                plugin = (entry or {}).get("plugin", "")
                baseid = (entry or {}).get("baseid", (entry or {}).get("formid", ""))
                key = _canonical_shout_key(plugin, baseid)
                if not key:
                    continue
                idx[key] = str(sid).strip().upper()
            except Exception:
                continue

        # Override with dynamic entries from pipe (higher priority)
        if self._dynamic_formid_to_shout_id:
            idx.update(self._dynamic_formid_to_shout_id)

        self._formid_to_shout_id = idx
        return self._formid_to_shout_id

    def set_allowed_formids(self, formids: set[str] | None) -> None:
        self._allowed_formids = None if formids is None else set(formids)

        if formids is None:
            self._allowed_shout_ids = None
        else:
            idx = self._ensure_formid_index()
            allowed: set[str] = set()
            for fid in set(formids or set()):
                composite = _split_shout_key(fid)
                if composite:
                    key = f"{composite[0]}|{composite[1]}"
                    sid = idx.get(key)
                    if sid:
                        allowed.add(str(sid).strip().upper())
                    continue

                base_val = _parse_hex_formid(fid)
                if base_val is None:
                    continue
                base_key = _format_baseid(base_val)

                for idx_key, sid in idx.items():
                    pair = _split_shout_key(idx_key)
                    if pair and pair[1] == base_key:
                        allowed.add(str(sid).strip().upper())

            self._allowed_shout_ids = allowed

        # Reset effective grammar and derived caches
        self._grammar = None
        self._invalidate_phase_a()

    def set_allowed_shout_entries(self, entries: list[tuple[str, str, str, str]] | None) -> None:
        """Accept (plugin, formid, name, editorID) entries from pipe and build dynamic formid->shout_id mapping."""
        if entries is None:
            self._dynamic_formid_to_shout_id = None
            self._dynamic_shout_info = None
            self._formid_to_shout_id = None  # Force rebuild
            self.set_allowed_formids(None)
            return

        if len(entries) == 0:
            self._dynamic_formid_to_shout_id = None
            self._dynamic_shout_info = None
            self._formid_to_shout_id = None  # Force rebuild
            self.set_allowed_formids(set())
            return

        dyn_idx: dict[str, str] = {}
        formids: set[str] = set()
        info: dict[str, tuple[str, str]] = {}

        for plugin, formid, _name, editor_id in entries:
            key = _canonical_shout_key(plugin, formid)
            if not key:
                continue
            formids.add(key)
            # editorID is the shout_grammar.json key (e.g., FUS_RO_DAH)
            sid = str(editor_id or "").strip().upper()
            if sid:
                dyn_idx[key] = sid
                plugin_norm = _normalize_plugin_name(plugin)
                base_val = _parse_hex_formid(formid)
                if plugin_norm and base_val is not None:
                    info[sid] = (plugin_norm, _format_baseid(base_val))

        self._dynamic_formid_to_shout_id = dyn_idx if dyn_idx else None
        self._dynamic_shout_info = info if info else None
        self._formid_to_shout_id = None  # Force rebuild

        # Apply formid filtering using the dynamic index
        self.set_allowed_formids(formids)

    def _ensure_shouts_map(self) -> dict:
        if self._shouts_map is not None:
            return self._shouts_map
        if self._shouts_map_path:
            self._shouts_map = load_shouts_map(self._shouts_map_path)
        else:
            # Try default path
            default_path = _runtime_dir() / "shouts_map.json"
            if default_path.exists():
                self._shouts_map = load_shouts_map(default_path)
            else:
                self._shouts_map = {}
        return self._shouts_map

    def _ensure_phase_a(self) -> tuple[list[str], dict[str, list[str]]]:
        if self._phase_a_grammar is not None and self._first_word_to_shouts is not None:
            return self._phase_a_grammar, self._first_word_to_shouts
        grammar = self._ensure_grammar()
        use_lang = self._resolve_effective_lang()
        self._phase_a_grammar, self._first_word_to_shouts = build_first_word_grammar(grammar, use_lang)
        return self._phase_a_grammar, self._first_word_to_shouts

    def get_debug_grammar(self) -> tuple[int, list[str]]:
        grammar = self._ensure_grammar()
        phase_a_grammar, _ = self._ensure_phase_a()
        phrases = sorted(set(phase_a_grammar or []))
        return int(len(grammar or {})), phrases

    def get_debug_grammar_detail(self) -> tuple[int, list[str], dict[str, list[str]]]:
        grammar = self._ensure_grammar()
        phase_a_grammar, _ = self._ensure_phase_a()
        phrases = sorted(set(phase_a_grammar or []))
        use_lang = self._resolve_effective_lang()
        per_shout: dict[str, list[str]] = {}
        for shout_name, langs_map in (grammar or {}).items():
            variants = _extract_first_token_variants(str(shout_name), langs_map or {}, use_lang)
            if not variants:
                continue
            per_shout[str(shout_name).strip().upper()] = _cap_variants(list(variants), MAX_VARIANTS_PER_ATOM)
        return int(len(grammar or {})), phrases, per_shout

    def _ensure_vosk(self):
        if self._vosk_model is not None:
            return self._vosk_model
        raise RuntimeError("Vosk model not initialized for shout recognition")

    def set_vosk_model(self, model) -> None:
        self._vosk_model = model

    def _resolve_formid_for_shout(self, shout_name: str) -> tuple[str, str] | None:
        """Resolve a shout_name (e.g. FUS_RO_DAH) to (plugin, baseid) tuple."""
        sid = str(shout_name or "").strip().upper()
        if not sid:
            return None

        # Check dynamic index first (from pipe entries)
        if self._dynamic_shout_info:
            info = self._dynamic_shout_info.get(sid)
            if info:
                return info

        # Fall back to shouts_map.json
        shouts_map = self._ensure_shouts_map()
        entry = shouts_map.get(sid)
        if entry:
            plugin = _normalize_plugin_name((entry or {}).get("plugin", ""))
            baseid = (entry or {}).get("baseid", (entry or {}).get("formid", ""))
            base_val = _parse_hex_formid(baseid)
            if plugin and base_val is not None:
                return plugin, _format_baseid(base_val)

        return None

    def _debug_fail(self, dbg: dict[str, Any], reason: str, score: float = 0.0) -> tuple[tuple[str | None, str | None, int, float, str], dict]:
        dbg["reason"] = reason
        return (None, None, 0, float(score), ""), dbg

    def _update_two_phase_debug(self, dbg: dict[str, Any], result: TwoPhaseResult) -> None:
        dbg.update(
            {
                "two_phase_valid": bool(result.valid),
                "two_phase_reason": str(result.reason),
                "two_phase_shout": str(result.shout_name or "").strip().upper(),
                "two_phase_word_count": int(result.word_count or 0),
                "two_phase_matched_len": int(result.matched_len or 0),
                "two_phase_score_a": float(result.score_a or 0.0),
                "two_phase_score_b": float(result.score_b or 0.0),
                "two_phase_score": float(result.combined_score or 0.0),
            }
        )

    def recognize(self, pcm16: np.ndarray, sr: int = SR) -> tuple[str | None, int, float, str]:
        if pcm16 is None or pcm16.size == 0:
            return None, 0, 0.0, ""

        return self._recognize_vosk_two_phase(pcm16, sr)

    def recognize_debug(self, pcm16: np.ndarray, sr: int = SR) -> tuple[tuple[str | None, int, float, str], dict]:
        use_lang = self._resolve_effective_lang()
        dbg: dict[str, Any] = {
            "backend": self.backend,
            "lang": use_lang,
        }

        if pcm16 is None or pcm16.size == 0:
            return self._debug_fail(dbg, "empty_audio")

        try:
            model = self._ensure_vosk()
        except Exception as e:
            dbg["reason"] = "vosk_missing"
            dbg["error"] = str(e)
            return (None, None, 0, 0.0, ""), dbg

        grammar = self._ensure_grammar()
        phase_a_grammar, first_word_to_shouts = self._ensure_phase_a()
        dbg["grammar_entries"] = int(len(grammar or {}))
        dbg["phrases"] = int(len(set(phase_a_grammar or [])))
        if not phase_a_grammar:
            return self._debug_fail(dbg, "phase_a_grammar_empty")

        result = two_phase_recognize(
            model=model,
            pcm16=pcm16,
            all_grammar=grammar,
            lang=use_lang,
            phase_a_grammar=phase_a_grammar,
            first_word_to_shouts=first_word_to_shouts,
            sr=sr,
        )

        self._update_two_phase_debug(dbg, result)

        if not result.valid or not result.shout_name:
            return self._debug_fail(dbg, "no_valid", score=float(result.combined_score))

        formid_info = self._resolve_formid_for_shout(result.shout_name)
        if not formid_info:
            return self._debug_fail(dbg, "map_missing", score=float(result.combined_score))

        plugin, baseid = formid_info

        power = max(1, min(3, int(result.word_count)))
        raw_text = " ".join(result.tokens) if result.tokens else ""
        dbg["reason"] = "ok"
        return (plugin, baseid, power, float(result.combined_score), raw_text), dbg

    def _recognize_vosk_two_phase(self, pcm16: np.ndarray, sr: int) -> tuple[str | None, int, float, str]:
        model = self._ensure_vosk()
        grammar = self._ensure_grammar()
        phase_a_grammar, first_word_to_shouts = self._ensure_phase_a()
        use_lang = self._resolve_effective_lang()

        if not phase_a_grammar:
            return None, 0, 0.0, ""

        result = two_phase_recognize(
            model=model,
            pcm16=pcm16,
            all_grammar=grammar,
            lang=use_lang,
            phase_a_grammar=phase_a_grammar,
            first_word_to_shouts=first_word_to_shouts,
            sr=sr,
        )

        if not result.valid or not result.shout_name:
            return None, 0, result.combined_score, ""

        # Map shout_name to FormID
        formid_info = self._resolve_formid_for_shout(result.shout_name)
        if not formid_info:
            return None, 0, result.combined_score, ""

        plugin, baseid = formid_info

        try:
            log_debug(
                f"[SHOUT][MAP] shout={result.shout_name.upper()} -> plugin={plugin} baseid={baseid}",
            )
        except Exception:
            pass

        power = max(1, min(3, result.word_count))
        raw_text = " ".join(result.tokens) if result.tokens else ""

        return plugin, baseid, power, result.combined_score, raw_text

"""Versioned, traceable name preprocessing views (Completion Step 10).

Each profile is an ordered list of named, versioned text transforms. The
``v1-compatible`` profile reproduces :func:`watchlist.normalize.normalize_name`
exactly and is the control view. Other profiles are experimental views that
must be measured separately; none replaces the original text.

Every result keeps the original input, an ordered trace of each transform
(name, version, before, after), whitespace tokens, script evidence from the
original text, and organisation/vessel designators as a SEPARATE output.
Designators are never deleted from ``text`` or ``tokens``; ``core_tokens`` is
only an additional view. No language inference, token reordering or identity
claim happens here. Optional transliteration imports ``anyascii`` lazily.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import TextIO
import unicodedata

from watchlist.normalize import MAX_NAME_LENGTH
from watchlist.scripts import SCRIPT_METHOD, UNKNOWN, script_profile

PREPROCESSING_VERSION = "preprocessing-v1"
CONFIG_VERSION = "preprocessing-config-v1"
CONTROL_PROFILE = "v1-compatible"
ENTITY_TYPES = frozenset({"organization", "vessel", "person", "unknown"})
_ENTITY_TYPE_SPELLINGS = {"organisation": "organization"}

# Hyphen/dash, apostrophe and quotation variants mapped to one ASCII form.
# HYPHEN_VARIANTS is shared with structured_input registration-ID keys.
HYPHEN_VARIANTS = "‐‑‒–—―−﹘﹣－"
_APOSTROPHES = "‘’‚‛ʼʻʹ`´′＇"
_QUOTES = "“”„‟«»″＂"
_PUNCTUATION_MAP = str.maketrans(
    {
        **{c: "-" for c in HYPHEN_VARIANTS},
        **{c: "'" for c in _APOSTROPHES},
        **{c: '"' for c in _QUOTES},
    }
)
# Stripped from designator-key edges in EVERY profile, so «ООО» is recognised
# with or without punctuation unification. Full-width forms become ASCII via NFKC.
_KEY_EDGE_PUNCTUATION = (
    ",;:()[]{}\"'" + _APOSTROPHES + _QUOTES + "‹›「」『』【】〔〕〈〉《》〖〗"
)
# Fixture partitions the evaluate CLI may read. Test partitions are refused;
# legacy ``ambiguity`` records are read as development diagnostics, matching
# the explicit mapping in watchlist.compare_retrieval.
EVALUATE_PARTITIONS = {"development": ("development", "ambiguity")}


class PreprocessingError(ValueError):
    """Explicit, structured preprocessing failure with a stable ``code``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TransformStep:
    """One applied transform; consecutive steps chain before -> after."""

    name: str
    version: str
    before: str
    after: str

    @property
    def changed(self) -> bool:
        return self.before != self.after


@dataclass(frozen=True)
class Designator:
    """A legal-form or vessel-prefix token separated from the core name view.

    ``token_index`` points into ``PreprocessedName.tokens``. ``rule`` is
    ``leading``, ``trailing`` or ``han_suffix`` (split from the end of a token).
    """

    token_index: int
    text: str
    key: str
    category: str
    rule: str


@dataclass(frozen=True)
class PreprocessedName:
    """Original evidence plus one profile's derived view and full provenance."""

    original: str
    profile: str
    profile_version: str
    pipeline_version: str
    designator_rules_version: str
    entity_type: str
    text: str
    tokens: tuple[str, ...]
    core_tokens: tuple[str, ...]
    designators: tuple[Designator, ...]
    scripts: frozenset[str]
    script_method: str
    support_status: str
    trace: tuple[TransformStep, ...]

    def to_dict(self) -> dict[str, object]:
        """JSON-ready, deterministic representation."""
        return {
            "original": self.original,
            "profile": self.profile,
            "profile_version": self.profile_version,
            "pipeline_version": self.pipeline_version,
            "designator_rules_version": self.designator_rules_version,
            "entity_type": self.entity_type,
            "text": self.text,
            "tokens": list(self.tokens),
            "core_tokens": list(self.core_tokens),
            "designators": [
                {
                    "token_index": d.token_index,
                    "text": d.text,
                    "key": d.key,
                    "category": d.category,
                    "rule": d.rule,
                }
                for d in self.designators
            ],
            "scripts": sorted(self.scripts),
            "script_method": self.script_method,
            "support_status": self.support_status,
            "trace": [
                {
                    "name": s.name,
                    "version": s.version,
                    "before": s.before,
                    "after": s.after,
                }
                for s in self.trace
            ],
        }


@dataclass(frozen=True)
class Profile:
    """A named, versioned ordered transform list."""

    name: str
    version: str
    status: str
    max_output_length: int
    transforms: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class DesignatorCategory:
    """Designator keys that apply only to the listed entity types."""

    category: str
    entity_types: frozenset[str]
    edge_tokens: frozenset[str]
    han_suffixes: tuple[str, ...]


@dataclass(frozen=True)
class PreprocessingConfig:
    """Validated configuration: profiles plus designator rules."""

    config_version: str
    default_profile: str
    profiles: tuple[Profile, ...]
    rules_version: str
    categories: tuple[DesignatorCategory, ...]

    def profile(self, name: str | None = None) -> Profile:
        wanted = self.default_profile if name is None else name
        for profile in self.profiles:
            if profile.name == wanted:
                return profile
        raise PreprocessingError(
            "unknown_profile", f"unknown preprocessing profile: {wanted}"
        )


# --- transforms -------------------------------------------------------------


def _nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _casefold(text: str) -> str:
    # Matches normalize_name: a second NFKC recomposes casefold output.
    return unicodedata.normalize("NFKC", text.casefold())


def _whitespace(text: str) -> str:
    return " ".join(text.split())


def _unify_punctuation(text: str) -> str:
    return text.translate(_PUNCTUATION_MAP)


def _is_latin_letter(character: str) -> bool:
    return unicodedata.category(character).startswith("L") and (
        "LATIN" in unicodedata.name(character, "").split()
    )


def _fold_latin_diacritics(text: str) -> str:
    """Drop nonspacing marks whose base letter is Latin; keep all other marks.

    Cyrillic, Arabic and other marks are distinct letters or vocalization and
    stay untouched. Latin letters without a decomposition (ø, ł, đ) also stay.
    """
    decomposed = unicodedata.normalize("NFD", text)
    kept: list[str] = []
    base_is_latin = False
    for character in decomposed:
        if unicodedata.category(character) == "Mn":
            if base_is_latin:
                continue
        else:
            base_is_latin = _is_latin_letter(character)
        kept.append(character)
    return unicodedata.normalize("NFC", "".join(kept))


def _anyascii_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("anyascii")
    except PackageNotFoundError:
        return "unknown"


def _transliterate_anyascii(text: str) -> str:
    """Lossy romanization via the optional ``anyascii`` package (lazy import)."""
    try:
        from anyascii import anyascii
    except ImportError as exc:
        raise PreprocessingError(
            "dependency_unavailable",
            "transliterate_anyascii requires the optional anyascii package",
        ) from exc
    return anyascii(text)


_TRANSFORMS: dict[str, tuple[str, Callable[[str], str]]] = {
    "nfkc": ("1", _nfkc),
    "casefold": ("1", _casefold),
    "whitespace": ("1", _whitespace),
    "unify_punctuation": ("1", _unify_punctuation),
    "fold_latin_diacritics": ("1", _fold_latin_diacritics),
    "transliterate_anyascii": ("1", _transliterate_anyascii),
}
TRANSFORM_NAMES = frozenset(_TRANSFORMS)


def _transform_version(name: str) -> str:
    version = _TRANSFORMS[name][0]
    if name == "transliterate_anyascii":
        return f"{version}+anyascii-{_anyascii_version()}"
    return version


# --- configuration ----------------------------------------------------------


def designator_key(token: str) -> str:
    """Comparison key: NFKC+casefold, dots removed, edge punctuation stripped."""
    key = _casefold(_nfkc(token)).replace(".", "")
    return key.strip(_KEY_EDGE_PUNCTUATION)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PreprocessingError("config_invalid", message)


def _string_list(value: object, label: str) -> list[str]:
    _require(
        isinstance(value, list) and all(isinstance(v, str) and v for v in value),
        f"{label} must be a list of nonempty strings",
    )
    assert isinstance(value, list)
    _require(len(set(value)) == len(value), f"{label} contains duplicates")
    return value


def parse_config(raw: Mapping[str, object]) -> PreprocessingConfig:
    """Validate a configuration mapping; any unknown or malformed field fails."""
    _require(isinstance(raw, Mapping), "configuration must be a JSON object")
    _require(
        set(raw) == {"config_version", "default_profile", "profiles", "designators"},
        "configuration has missing or unknown top-level fields",
    )
    _require(
        raw["config_version"] == CONFIG_VERSION,
        f"config_version must be {CONFIG_VERSION}",
    )
    raw_profiles = raw["profiles"]
    _require(
        isinstance(raw_profiles, list) and bool(raw_profiles),
        "profiles must be a nonempty list",
    )
    assert isinstance(raw_profiles, list)
    profiles: list[Profile] = []
    for item in raw_profiles:
        _require(
            isinstance(item, dict)
            and set(item)
            == {
                "name",
                "version",
                "status",
                "max_output_length",
                "transforms",
                "description",
            },
            "each profile needs exactly name, version, status, max_output_length, "
            "transforms, description",
        )
        _require(
            all(
                isinstance(item[k], str) and item[k]
                for k in ("name", "version", "status", "description")
            ),
            "profile text fields must be nonempty strings",
        )
        _require(
            item["status"] in {"control", "experimental"},
            "profile status must be control or experimental",
        )
        limit = item["max_output_length"]
        _require(
            type(limit) is int and limit >= MAX_NAME_LENGTH,
            f"max_output_length must be an integer >= {MAX_NAME_LENGTH}",
        )
        transforms = _string_list(item["transforms"], f"{item['name']} transforms")
        unknown = sorted(set(transforms) - TRANSFORM_NAMES)
        _require(not unknown, f"unknown transforms: {unknown}")
        profiles.append(
            Profile(
                item["name"],
                item["version"],
                item["status"],
                limit,
                tuple(transforms),
                item["description"],
            )
        )
    names = [p.name for p in profiles]
    _require(len(set(names)) == len(names), "profile names must be unique")
    _require(raw["default_profile"] in names, "default_profile must name a profile")
    control = [p for p in profiles if p.name == CONTROL_PROFILE]
    _require(
        bool(control)
        and control[0].transforms == ("nfkc", "casefold", "whitespace")
        and control[0].max_output_length == MAX_NAME_LENGTH
        and control[0].status == "control",
        f"{CONTROL_PROFILE} must keep the exact normalize_name transforms and limit",
    )

    designators = raw["designators"]
    _require(
        isinstance(designators, dict)
        and set(designators) == {"rules_version", "categories"},
        "designators needs exactly rules_version and categories",
    )
    assert isinstance(designators, dict)
    _require(
        isinstance(designators["rules_version"], str)
        and bool(designators["rules_version"]),
        "rules_version must be a nonempty string",
    )
    _require(isinstance(designators["categories"], list), "categories must be a list")
    categories: list[DesignatorCategory] = []
    for item in designators["categories"]:
        _require(
            isinstance(item, dict)
            and set(item)
            == {"category", "entity_types", "edge_tokens", "han_suffixes"},
            "each designator category needs exactly category, entity_types, "
            "edge_tokens, han_suffixes",
        )
        _require(
            isinstance(item["category"], str) and bool(item["category"]),
            "category must be a nonempty string",
        )
        types = _string_list(item["entity_types"], "entity_types")
        _require(set(types) <= ENTITY_TYPES, "entity_types contain an unknown type")
        tokens = _string_list(item["edge_tokens"], "edge_tokens")
        _require(
            all(designator_key(t) == t for t in tokens),
            "edge_tokens must already be in designator-key form",
        )
        suffixes = _string_list(item["han_suffixes"], "han_suffixes")
        _require(
            all(all(_is_han(c) for c in s) for s in suffixes),
            "han_suffixes must contain only Han characters",
        )
        # Longest suffix first so 股份有限公司 wins over 有限公司.
        ordered = tuple(sorted(suffixes, key=lambda s: (-len(s), s)))
        categories.append(
            DesignatorCategory(
                item["category"], frozenset(types), frozenset(tokens), ordered
            )
        )
    labels = [c.category for c in categories]
    _require(len(set(labels)) == len(labels), "designator categories must be unique")
    return PreprocessingConfig(
        CONFIG_VERSION,
        raw["default_profile"],
        tuple(profiles),
        designators["rules_version"],
        tuple(categories),
    )


def load_config(path: Path) -> PreprocessingConfig:
    """Read and validate a UTF-8 JSON configuration file."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreprocessingError(
            "config_invalid", f"cannot read configuration: {exc}"
        ) from exc
    return parse_config(raw)


# --- pipeline ---------------------------------------------------------------


def _is_han(character: str) -> bool:
    return unicodedata.name(character, "").startswith(
        ("CJK UNIFIED IDEOGRAPH-", "CJK COMPATIBILITY IDEOGRAPH-")
    )


def _support_status(scripts: frozenset[str]) -> str:
    """Visible support status from watchlist.scripts evidence on the original."""
    if not scripts:
        return "no_script_evidence"
    if UNKNOWN not in scripts:
        return "supported"
    return "partially_supported" if scripts - {UNKNOWN} else "unsupported"


def _canonical_entity_type(entity_type: str) -> str:
    canonical = _ENTITY_TYPE_SPELLINGS.get(entity_type, entity_type)
    if canonical not in ENTITY_TYPES:
        raise PreprocessingError(
            "unknown_entity_type", f"unknown entity_type: {entity_type!r}"
        )
    return canonical


def separate_designators(
    tokens: Sequence[str], entity_type: str, config: PreprocessingConfig
) -> tuple[tuple[str, ...], tuple[Designator, ...]]:
    """Split leading/trailing designator runs and Han suffixes from the core view.

    Only edge positions are considered, a name made only of designator tokens
    keeps all its tokens as core with no designators (there is no non-designator
    word to separate them from), and rules apply only to their configured
    entity types. Matching uses :func:`designator_key`; the token text itself is
    preserved in the output. ``core_tokens`` depends on the profile (for example
    Han suffixes are not recognised after transliteration), so core views from
    different profiles are not directly comparable.
    """
    active = [c for c in config.categories if entity_type in c.entity_types]
    if not active or not tokens:
        return tuple(tokens), ()

    def category_of(token: str) -> str | None:
        key = designator_key(token)
        for category in active:
            if key in category.edge_tokens:
                return category.category
        return None

    if all(category_of(token) is not None for token in tokens):
        return tuple(tokens), ()

    start, end = 0, len(tokens)
    found: list[Designator] = []
    while start < end - 1 and (label := category_of(tokens[start])) is not None:
        found.append(
            Designator(
                start, tokens[start], designator_key(tokens[start]), label, "leading"
            )
        )
        start += 1
    while end - 1 > start and (label := category_of(tokens[end - 1])) is not None:
        found.append(
            Designator(
                end - 1,
                tokens[end - 1],
                designator_key(tokens[end - 1]),
                label,
                "trailing",
            )
        )
        end -= 1
    core = list(tokens[start:end])
    last = core[-1]
    for category in active:
        # Longest matching suffix wins; a token that IS a suffix is not split.
        suffix = next((s for s in category.han_suffixes if last.endswith(s)), None)
        if suffix is not None and len(last) > len(suffix):
            core[-1] = last[: -len(suffix)]
            found.append(
                Designator(
                    end - 1,
                    suffix,
                    designator_key(suffix),
                    category.category,
                    "han_suffix",
                )
            )
            break
    return tuple(core), tuple(sorted(found, key=lambda d: (d.token_index, d.rule)))


def preprocess(
    text: str,
    config: PreprocessingConfig,
    profile: str | None = None,
    *,
    entity_type: str = "unknown",
) -> PreprocessedName:
    """Apply one profile and return the traced view; never mutates the original.

    Input validation matches normalize_name: a string of at most 200 code
    points that is not blank after the pipeline. Lone surrogates are rejected.
    """
    if not isinstance(text, str):
        raise PreprocessingError("not_text", "name must be a string")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PreprocessingError(
            "invalid_unicode", "name must be valid Unicode text"
        ) from exc
    if len(text) > MAX_NAME_LENGTH:
        raise PreprocessingError(
            "too_long", f"name must contain at most {MAX_NAME_LENGTH} characters"
        )
    kind = _canonical_entity_type(entity_type)
    chosen = config.profile(profile)
    current = text
    trace: list[TransformStep] = []
    for name in chosen.transforms:
        after = _TRANSFORMS[name][1](current)
        trace.append(TransformStep(name, _transform_version(name), current, after))
        current = after
    tokens = tuple(current.split())
    if not tokens:
        raise PreprocessingError("blank", "name must not be blank")
    if len(current) > chosen.max_output_length:
        raise PreprocessingError(
            "output_too_long",
            f"{chosen.name} output must contain at most {chosen.max_output_length} characters",
        )
    core, designators = separate_designators(tokens, kind, config)
    scripts = script_profile(text).scripts
    return PreprocessedName(
        original=text,
        profile=chosen.name,
        profile_version=chosen.version,
        pipeline_version=PREPROCESSING_VERSION,
        designator_rules_version=config.rules_version,
        entity_type=kind,
        text=current,
        tokens=tokens,
        core_tokens=core,
        designators=designators,
        scripts=scripts,
        script_method=SCRIPT_METHOD,
        support_status=_support_status(scripts),
        trace=tuple(trace),
    )


def verify_trace(result: PreprocessedName) -> bool:
    """True when the trace chains exactly from ``original`` to ``text``."""
    current = result.original
    for step in result.trace:
        if step.before != current:
            return False
        current = step.after
    return current == result.text


def view_collisions(
    names: Sequence[str], config: PreprocessingConfig, profile: str | None = None
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Groups of distinct original names that one profile maps to the same text.

    This measures merging (for example transliteration collisions) instead of
    assuming a transform helps. Output is sorted for determinism.
    """
    groups: dict[str, set[str]] = {}
    for name in names:
        groups.setdefault(preprocess(name, config, profile).text, set()).add(name)
    return tuple(
        sorted(
            (view, tuple(sorted(originals)))
            for view, originals in groups.items()
            if len(originals) > 1
        )
    )


def evaluate_views(
    names: Sequence[str], config: PreprocessingConfig
) -> dict[str, dict[str, object]]:
    """Per-profile mechanics on the SAME input names (identical-view ablation).

    Reports counts only; there are no labels, scores or retrieval claims here.
    """
    report: dict[str, dict[str, object]] = {}
    for profile in config.profiles:
        results = [preprocess(name, config, profile.name) for name in names]
        statuses: dict[str, int] = {}
        for item in results:
            statuses[item.support_status] = statuses.get(item.support_status, 0) + 1
        collisions = view_collisions(names, config, profile.name)
        report[profile.name] = {
            "profile_version": profile.version,
            "status": profile.status,
            "inputs": len(results),
            "changed_from_control": sum(
                item.text != preprocess(item.original, config, CONTROL_PROFILE).text
                for item in results
            ),
            "distinct_views": len({item.text for item in results}),
            "collision_groups": len(collisions),
            "collided_inputs": sum(len(group) for _, group in collisions),
            "support_status": dict(sorted(statuses.items())),
        }
    return report


def _fixture_names(path: Path, partition: str) -> list[str]:
    """Names from the authored fictional multilingual fixture for one partition.

    Only partitions in :data:`EVALUATE_PARTITIONS` are allowed (never test), and
    zero support is an explicit error rather than an empty report.
    """
    if partition not in EVALUATE_PARTITIONS:
        raise PreprocessingError(
            "partition_not_allowed",
            f"partition must be one of {sorted(EVALUATE_PARTITIONS)}; "
            "test partitions are never read here",
        )
    sources = EVALUATE_PARTITIONS[partition]
    records = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise PreprocessingError("input_invalid", "fixture must be a JSON list")
    names: list[str] = []
    for record in records:
        if not isinstance(record, dict) or record.get("synthetic") is not True:
            raise PreprocessingError(
                "input_invalid", "fixture records must be synthetic objects"
            )
        if record.get("partition") in sources:
            names.extend(
                record[k]
                for k in ("english", "arabic")
                if isinstance(record.get(k), str)
            )
    if not names:
        raise PreprocessingError(
            "zero_support", f"no names found for partition {partition!r}"
        )
    return sorted(set(names))


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: ``show`` one name, or ``evaluate`` views over a fictional fixture."""
    parser = argparse.ArgumentParser(prog="python -m watchlist.preprocessing")
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("show", help="Preprocess one name and print the traced view")
    show.add_argument("--config", type=Path, required=True)
    show.add_argument("--profile")
    show.add_argument("--entity-type", default="unknown")
    show.add_argument("--name", required=True)
    evaluate = sub.add_parser(
        "evaluate", help="Profile mechanics on fictional fixture names"
    )
    evaluate.add_argument("--config", type=Path, required=True)
    evaluate.add_argument(
        "--input", type=Path, default=Path("eval/fixtures/multilingual_entities.json")
    )
    evaluate.add_argument("--partition", default="development")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "show":
            output: dict[str, object] = preprocess(
                args.name, config, args.profile, entity_type=args.entity_type
            ).to_dict()
        else:
            names = _fixture_names(args.input, args.partition)
            output = {
                "evidence": "fictional authored fixture; regression mechanics only, "
                "not a real-data preprocessing result",
                "input": args.input.as_posix(),
                "partition": args.partition,
                "source_partitions": list(EVALUATE_PARTITIONS[args.partition]),
                "config_version": config.config_version,
                "pipeline_version": PREPROCESSING_VERSION,
                "profiles": evaluate_views(names, config),
            }
    except (PreprocessingError, OSError, json.JSONDecodeError) as exc:
        code = getattr(exc, "code", "error")
        _emit(
            json.dumps({"error": code, "message": str(exc)}, ensure_ascii=False),
            sys.stderr,
        )
        return 2
    _emit(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True), sys.stdout)
    return 0


def _emit(text: str, stream: TextIO) -> None:
    """Write UTF-8 regardless of the Windows console code page."""
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        stream.write(text + "\n")
    else:
        stream.flush()
        buffer.write((text + "\n").encode("utf-8"))
        buffer.flush()


if __name__ == "__main__":
    raise SystemExit(main())

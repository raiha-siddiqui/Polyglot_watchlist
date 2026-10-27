"""Lexical and phonetic baseline arms behind one retriever contract (Step 11).

Every arm keeps the established ranker contract: score each supported alias,
keep one candidate per entity through its best alias (ties by alias ID), and
order entities by descending score with entity-ID ties. Scores are raw ranking
signals on each arm's own scale, never probabilities.

Some representations are undefined for some text. Phonetic codes, for example,
are not defined here for Han ideographs or Arabic abjad text. Such cases are
reported explicitly: an unsupported query returns no candidates plus a reason,
and unsupported reference aliases are listed in the arm's coverage record.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol
import unicodedata

from rapidfuzz import fuzz, process
from rapidfuzz.distance import JaroWinkler, Levenshtein

from watchlist.entities import Entity
from watchlist.matching import Candidate
from watchlist.normalize import normalize_name
from watchlist.scripts import UNKNOWN, script_set

BASELINE_PROTOCOL = "baseline-arms-v2"
# Scripts whose anyascii romanization is used before English-oriented phonetic codes.
PHONETIC_SCRIPTS = frozenset({"LATIN", "CYRILLIC"})


class UnsupportedText(ValueError):
    """A representation is undefined for this text; the reason is reportable."""


@dataclass(frozen=True)
class RetrievalResult:
    """Ranked candidates, or an explicit reason when the query is unsupported.

    ``component_status`` is used by fused retrievers to report each component.
    """

    candidates: tuple
    unsupported_reason: str | None = None
    component_status: tuple[tuple[str, str | None], ...] = ()


@dataclass(frozen=True)
class Coverage:
    """Reference aliases and entities this arm can actually score."""

    entity_count: int
    alias_count: int
    indexed_alias_count: int
    retrievable_entity_count: int
    unsupported_aliases: tuple[tuple[str, str], ...] = field(default=())


class Retriever(Protocol):
    """Common contract: text in, ranked entity candidates out; no labels."""

    arm: str
    scorer_name: str
    coverage: Coverage

    def retrieve(self, query: str, top_k: int) -> RetrievalResult: ...


def check_top_k(top_k: object) -> int:
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
        raise ValueError("top_k must be a positive integer")
    return top_k


def check_entities(entities: Sequence[Entity]) -> tuple[Entity, ...]:
    """Reject duplicate IDs or alias-free records before building any arm."""
    result = tuple(entities)
    if len({entity.entity_id for entity in result}) != len(result):
        raise ValueError("duplicate entity IDs")
    if any(not entity.aliases for entity in result):
        raise ValueError("entity has no aliases")
    return result


def _transliterate(text: str) -> str:
    """Romanize normalized text with anyascii (lazy optional dependency).

    Only text with recognized letters (Latin/Cyrillic/Arabic/Han) and no
    non-ASCII symbols is accepted; anyascii would otherwise turn symbols such
    as emoji into ``:name:`` tokens that are not name representations.
    """
    normalized = normalize_name(text)
    scripts = script_set(normalized)
    if not scripts or UNKNOWN in scripts:
        raise UnsupportedText("transliteration needs recognized letters")
    if any(
        ord(char) > 127 and unicodedata.category(char).startswith("S")
        for char in normalized
    ):
        raise UnsupportedText("transliteration undefined for non-ASCII symbols")
    try:
        from anyascii import anyascii
    except ImportError as exc:
        raise RuntimeError(
            "transliteration arms require the preprocessing extra (anyascii)"
        ) from exc
    romanized = " ".join(anyascii(normalized).casefold().split())
    if not romanized:
        raise UnsupportedText("transliteration produced no characters")
    return romanized


def transliterated(text: str) -> str:
    """Representation for the transliterate+Levenshtein arm.

    anyascii is a deterministic character table, not a linguistic standard.
    Han romanizes to Mandarin readings and Arabic loses unwritten vowels; both
    remain supported but are documented limitations of this representation.
    """
    return _transliterate(text)


def phonetic_code(text: str, algorithm: str) -> str:
    """Token-wise Metaphone or NYSIIS codes on Latin/Cyrillic text.

    Cyrillic is romanized with anyascii first. Han, Arabic and unknown scripts
    are unsupported because these English-oriented codes would be meaningless.
    Blank or invalid text raises ``ValueError`` like every other arm.
    """
    scripts = script_set(normalize_name(text))
    if UNKNOWN in scripts or not scripts:
        raise UnsupportedText("phonetic code needs recognized letters")
    if not scripts <= PHONETIC_SCRIPTS:
        unsupported = "+".join(sorted(scripts - PHONETIC_SCRIPTS))
        raise UnsupportedText(f"phonetic code undefined for script {unsupported}")
    try:
        import jellyfish
    except ImportError as exc:
        raise RuntimeError(
            "phonetic arms require the preprocessing extra (jellyfish)"
        ) from exc
    encode = {"metaphone": jellyfish.metaphone, "nysiis": jellyfish.nysiis}[algorithm]
    tokens = [
        "".join(char for char in token if "a" <= char <= "z")
        for token in _transliterate(text).split()
    ]
    codes = [encode(token) for token in tokens if token]
    codes = [code for code in codes if code]
    if not codes:
        raise UnsupportedText("phonetic code is empty")
    return " ".join(codes)


class AliasScoringRetriever:
    """Precompute alias representations once; score them per query.

    ``score`` is a RapidFuzz similarity scorer (called in one C-level batch per
    query) multiplied by ``scale``; ``None`` means exact representation
    equality. Exact equality returns only matches (score 100), so non-matches
    are never presented as a tie-ordered list.
    """

    def __init__(
        self,
        entities: Sequence[Entity],
        *,
        arm: str,
        scorer_name: str,
        represent: Callable[[str], str],
        score: Callable | None,
        scale: float = 1.0,
    ):
        self.entities = check_entities(entities)
        self.arm, self.scorer_name = arm, scorer_name
        self._represent, self._score, self._scale = represent, score, scale
        self._texts: list[str] = []
        self._owners: list[tuple[int, object]] = []
        unsupported, retrievable = [], set()
        for number, entity in enumerate(self.entities):
            for alias in entity.aliases:
                try:
                    self._texts.append(represent(alias.name))
                except UnsupportedText as exc:
                    unsupported.append((alias.alias_id, str(exc)))
                    continue
                self._owners.append((number, alias))
                retrievable.add(number)
        alias_count = sum(len(entity.aliases) for entity in self.entities)
        self.coverage = Coverage(
            len(self.entities),
            alias_count,
            alias_count - len(unsupported),
            len(retrievable),
            tuple(unsupported),
        )

    def _alias_scores(self, representation: str) -> list[tuple[int, float]]:
        """(alias row, score) pairs; exact equality keeps only matching rows."""
        if self._score is None:
            return [
                (row, 100.0)
                for row, text in enumerate(self._texts)
                if text == representation
            ]
        matches = process.extract(
            representation,
            self._texts,
            scorer=self._score,
            processor=None,
            limit=None,
            score_cutoff=None,
        )
        return [(row, self._scale * score) for _, score, row in matches]

    def retrieve(self, query: str, top_k: int) -> RetrievalResult:
        """Return up to top_k entities, or an explicit unsupported-query reason."""
        check_top_k(top_k)
        try:
            representation = self._represent(query)
        except UnsupportedText as exc:
            return RetrievalResult((), str(exc))
        best: dict[int, tuple[float, object]] = {}
        for row, score in self._alias_scores(representation):
            number, alias = self._owners[row]
            current = best.get(number)
            if current is None or (-score, alias.alias_id) < (
                -current[0],
                current[1].alias_id,
            ):
                best[number] = (score, alias)
        candidates = []
        for number, (score, alias) in best.items():
            entity = self.entities[number]
            candidates.append(
                Candidate(
                    entity.entity_id,
                    entity.name,
                    entity.entity_type,
                    alias.alias_id,
                    alias.name,
                    score,
                    entity.synthetic,
                    entity.source,
                    entity.provenance,
                )
            )
        candidates.sort(
            key=lambda candidate: (-candidate.similarity_score, candidate.entity_id)
        )
        return RetrievalResult(tuple(candidates[:top_k]))


_LEV, _JW = Levenshtein.normalized_similarity, JaroWinkler.normalized_similarity
# arm -> (scorer name, representation, RapidFuzz scorer or None for equality, scale)
ARMS: dict[str, tuple[str, Callable[[str], str], Callable | None, float]] = {
    "exact_normalized": ("normalized_exact_equality_x100", normalize_name, None, 1.0),
    "levenshtein": (
        "rapidfuzz.normalized_levenshtein_similarity_x100",
        normalize_name,
        _LEV,
        100.0,
    ),
    "jaro_winkler": (
        "rapidfuzz.jaro_winkler_normalized_similarity_x100",
        normalize_name,
        _JW,
        100.0,
    ),
    "token_sort": (
        "rapidfuzz.fuzz.token_sort_ratio",
        normalize_name,
        fuzz.token_sort_ratio,
        1.0,
    ),
    "token_set": (
        "rapidfuzz.fuzz.token_set_ratio",
        normalize_name,
        fuzz.token_set_ratio,
        1.0,
    ),
    "phonetic_metaphone": (
        "jellyfish.metaphone_tokens_levenshtein_x100",
        lambda text: phonetic_code(text, "metaphone"),
        _LEV,
        100.0,
    ),
    "phonetic_nysiis": (
        "jellyfish.nysiis_tokens_levenshtein_x100",
        lambda text: phonetic_code(text, "nysiis"),
        _LEV,
        100.0,
    ),
    "translit_levenshtein": (
        "anyascii_then_normalized_levenshtein_x100",
        transliterated,
        _LEV,
        100.0,
    ),
}


def build_retriever(arm: str, entities: Sequence[Entity]) -> AliasScoringRetriever:
    """Construct one named lexical/phonetic arm over a reference view."""
    if arm not in ARMS:
        raise ValueError(f"unknown baseline arm: {arm}")
    scorer_name, represent, score, scale = ARMS[arm]
    return AliasScoringRetriever(
        entities,
        arm=arm,
        scorer_name=scorer_name,
        represent=represent,
        score=score,
        scale=scale,
    )

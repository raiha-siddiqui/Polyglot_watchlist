"""Contrastive and binary losses with explicit shape, label and finiteness guards.

These functions compute training objectives only. A loss value or logit is
never an identity probability; calibration is a separate, later step.
"""

from collections.abc import Sequence

import torch
import torch.nn.functional as F


class NonFiniteLossError(FloatingPointError):
    """Raised instead of silently propagating NaN/inf into optimizer state."""


def _require_finite(tensor: torch.Tensor, label: str) -> None:
    if not torch.isfinite(tensor).all():
        raise NonFiniteLossError(f"{label} contains NaN or infinite values")


def multiple_negatives_ranking_loss(
    queries: torch.Tensor,
    candidates: torch.Tensor,
    *,
    scale: float = 20.0,
    group_ids: Sequence[str] | None = None,
    hard_negatives: torch.Tensor | None = None,
    hard_negative_group_ids: Sequence[str] | None = None,
) -> torch.Tensor:
    """In-batch softmax over scaled cosine similarities (MultipleNegativesRanking).

    Row ``i`` of ``candidates`` is the positive for query ``i``; every other
    candidate is a negative unless it shares the query's group, in which case it
    is masked as a possible false negative. Optional hard negatives append extra
    columns with the same masking rule.
    """
    if queries.ndim != 2 or queries.shape != candidates.shape or len(queries) < 2:
        raise ValueError(
            "queries and candidates must be matching [batch>=2, dim] matrices"
        )
    if (
        not isinstance(scale, (int, float))
        or isinstance(scale, bool)
        or not 0 < scale < 1000
    ):
        raise ValueError("scale must be a positive finite number below 1000")
    _require_finite(queries, "query embeddings")
    _require_finite(candidates, "candidate embeddings")
    columns = F.normalize(candidates, dim=-1)
    column_groups = list(group_ids) if group_ids is not None else None
    if hard_negatives is not None:
        if hard_negatives.ndim != 2 or hard_negatives.shape[1] != queries.shape[1]:
            raise ValueError("hard negatives must be a [n, dim] matrix")
        _require_finite(hard_negatives, "hard-negative embeddings")
        columns = torch.cat([columns, F.normalize(hard_negatives, dim=-1)])
        if column_groups is not None:
            if hard_negative_group_ids is None or len(hard_negative_group_ids) != len(
                hard_negatives
            ):
                raise ValueError(
                    "hard-negative group IDs are required when group masking is used"
                )
            column_groups += list(hard_negative_group_ids)
    logits = scale * F.normalize(queries, dim=-1) @ columns.T
    if column_groups is not None:
        if len(group_ids) != len(queries):
            raise ValueError("one group ID is required per query")
        mask = torch.tensor(
            [
                [
                    column_groups[j] == group_ids[i] and j != i
                    for j in range(len(column_groups))
                ]
                for i in range(len(queries))
            ]
        )
        logits = logits.masked_fill(mask, float("-inf"))
    loss = F.cross_entropy(logits, torch.arange(len(queries)))
    _require_finite(loss, "contrastive loss")
    return loss


def binary_cross_entropy_loss(
    logits: torch.Tensor, labels: torch.Tensor
) -> torch.Tensor:
    """Mean BCE on raw logits; a sigmoid must not be applied before this call."""
    if logits.ndim != 1 or logits.shape != labels.shape or not len(logits):
        raise ValueError("logits and labels must be matching nonempty vectors")
    if not torch.all((labels == 0) | (labels == 1)):
        raise ValueError("binary labels must be exactly 0 or 1")
    _require_finite(logits, "logits")
    loss = F.binary_cross_entropy_with_logits(logits, labels.to(logits.dtype))
    _require_finite(loss, "binary cross-entropy loss")
    return loss


def check_finite_gradients(parameters) -> float:
    """Return the global gradient L2 norm, refusing NaN/inf before an optimizer step."""
    norms = [p.grad.detach().norm() for p in parameters if p.grad is not None]
    if not norms:
        raise ValueError("no gradients were produced")
    total = torch.linalg.vector_norm(torch.stack(norms))
    _require_finite(total, "gradient norm")
    return float(total)

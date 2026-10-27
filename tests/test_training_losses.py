"""Loss values are checked against hand calculations and non-finite guards."""

import math

import pytest

torch = pytest.importorskip("torch")

from watchlist.training_losses import (
    NonFiniteLossError,
    binary_cross_entropy_loss,  # noqa: E402
    check_finite_gradients,
    multiple_negatives_ranking_loss,
)


def test_mnrl_matches_hand_calculation() -> None:
    eye = torch.eye(2)
    loss = multiple_negatives_ranking_loss(eye, eye, scale=1.0)
    # Each row: softmax over logits [1, 0]; loss = log(1 + e^-1).
    assert float(loss) == pytest.approx(math.log(1 + math.exp(-1)), abs=1e-6)


def test_mnrl_masks_same_group_false_negatives() -> None:
    eye = torch.eye(2)
    masked = multiple_negatives_ranking_loss(eye, eye, scale=1.0, group_ids=["g", "g"])
    assert float(masked) == pytest.approx(0.0, abs=1e-7)
    distinct = multiple_negatives_ranking_loss(
        eye, eye, scale=1.0, group_ids=["g", "h"]
    )
    assert float(distinct) == pytest.approx(math.log(1 + math.exp(-1)), abs=1e-6)


def test_mnrl_hard_negatives_add_columns_with_masking() -> None:
    eye = torch.eye(2)
    hard = torch.tensor([[1.0, 0.0]])
    loss = multiple_negatives_ranking_loss(
        eye,
        eye,
        scale=1.0,
        group_ids=["g", "h"],
        hard_negatives=hard,
        hard_negative_group_ids=["x"],
    )
    e = math.e
    expected = (-math.log(e / (2 * e + 1)) - math.log(e / (e + 2))) / 2
    assert float(loss) == pytest.approx(expected, abs=1e-6)
    same_group = multiple_negatives_ranking_loss(
        eye,
        eye,
        scale=1.0,
        group_ids=["g", "h"],
        hard_negatives=hard,
        hard_negative_group_ids=["g"],
    )
    # Row 0 shares group g with the hard negative, so that column is masked.
    assert float(same_group) == pytest.approx(
        (-math.log(e / (e + 1)) - math.log(e / (e + 2))) / 2, abs=1e-6
    )
    with pytest.raises(ValueError, match="group IDs"):
        multiple_negatives_ranking_loss(
            eye, eye, group_ids=["g", "h"], hard_negatives=hard
        )


def test_bce_matches_hand_calculation_on_raw_logits() -> None:
    loss = binary_cross_entropy_loss(torch.tensor([0.0, 2.0]), torch.tensor([1.0, 0.0]))
    expected = (math.log(2) + math.log(1 + math.exp(2))) / 2
    assert float(loss) == pytest.approx(expected, abs=1e-6)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_inputs_are_refused(bad: float) -> None:
    values = torch.tensor([[1.0, 0.0], [0.0, bad]])
    with pytest.raises(NonFiniteLossError):
        multiple_negatives_ranking_loss(values, torch.eye(2))
    with pytest.raises(NonFiniteLossError):
        binary_cross_entropy_loss(torch.tensor([0.0, bad]), torch.tensor([1.0, 0.0]))


def test_shape_label_and_scale_validation() -> None:
    with pytest.raises(ValueError, match="matching"):
        multiple_negatives_ranking_loss(torch.eye(2), torch.eye(3))
    with pytest.raises(ValueError, match="batch"):
        multiple_negatives_ranking_loss(torch.ones(1, 2), torch.ones(1, 2))
    with pytest.raises(ValueError, match="scale"):
        multiple_negatives_ranking_loss(torch.eye(2), torch.eye(2), scale=0.0)
    with pytest.raises(ValueError, match="0 or 1"):
        binary_cross_entropy_loss(torch.tensor([0.0]), torch.tensor([0.5]))


def test_gradient_step_reduces_loss_and_guard_catches_nan_gradients() -> None:
    torch.manual_seed(0)
    queries = torch.nn.Parameter(torch.randn(4, 3))
    candidates = torch.randn(4, 3)
    loss = multiple_negatives_ranking_loss(queries, candidates)
    loss.backward()
    assert check_finite_gradients([queries]) > 0
    with torch.no_grad():
        queries -= 0.05 * queries.grad
    assert multiple_negatives_ranking_loss(queries, candidates).item() < loss.item()
    queries.grad[0, 0] = float("nan")
    with pytest.raises(NonFiniteLossError, match="gradient"):
        check_finite_gradients([queries])
    with pytest.raises(ValueError, match="no gradients"):
        check_finite_gradients([torch.nn.Parameter(torch.zeros(1))])

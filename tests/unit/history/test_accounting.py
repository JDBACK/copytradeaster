from decimal import Decimal as D

from copytradeaster.aster.models import IncomeDTO
from copytradeaster.history.accounting import AccountingProof, account_episode
from tests.unit.history.test_projector import fill, project


def flow(id, kind="COMMISSION", amount="-0.1", trade="1", asset="USDT"):
    return IncomeDTO(
        symbol="BTCUSDT",
        incomeType=kind,
        income=amount,
        asset=asset,
        time=1,
        tranId=str(id),
        tradeId=trade,
    )


PROOF = AccountingProof(income_complete=True, builder_policy="included_in_commission")


def test_signed_income_counted_once_and_realized_income_not_duplicated():
    fills = [fill(1, "BUY", "1"), fill(2, "SELL", "1", "110", "10")]
    (episode,) = project(fills)
    costs = [flow(1), flow(2, trade="2"), flow(3, "REALIZED_PNL", "10")]
    result = account_episode(episode, fills, costs + costs, proof=PROOF)
    assert result.gross == D(10)
    assert result.commission == D("-0.2")
    assert result.net == D("9.8") and not result.issues


def test_rebate_retains_positive_income_sign():
    fills = [fill(1, "BUY", "1")]
    result = account_episode(project(fills)[0], fills, [flow(1, amount="0.1")], proof=PROOF)
    assert result.commission == D("0.1") and result.net == D("0.1")


def test_missing_commission_and_unknown_builder_do_not_become_zero_cost_net():
    fills = [fill(1, "BUY", "1")]
    result = account_episode(project(fills)[0], fills, [], proof=AccountingProof())
    assert result.net is None
    assert set(result.issues) == {"income_incomplete", "builder_unknown", "commission_missing"}


def test_hedge_funding_requires_explicit_attribution():
    fills = [fill(1, "BUY", "1")]
    episode = project(fills)[0]
    flows = [flow(1), flow(2, "FUNDING_FEE", "-2", trade="")]
    result = account_episode(episode, fills, flows, proof=PROOF)
    assert result.net is None and result.funding == 0
    assert "funding_unattributed" in result.issues
    result = account_episode(
        episode,
        fills,
        flows,
        proof=PROOF,
        attributed_flows={("FUNDING_FEE", "2"): episode.public_ref},
    )
    assert result.net == D("-2.1")


def test_foreign_asset_never_converted_with_current_fx():
    fills = [fill(1, "BUY", "1")]
    result = account_episode(project(fills)[0], fills, [flow(1, asset="BNB")], proof=PROOF)
    assert result.net is None and "non_usdt_cost" in result.issues


def test_flip_shared_fill_commission_not_guessed():
    fills = [fill(1, "BUY", "1"), fill(2, "SELL", "2", "110", "10")]
    for episode in project(fills):
        result = account_episode(episode, fills, [flow(1), flow(2, trade="2")], proof=PROOF)
        assert result.net is None and "shared_fill_commission" in result.issues


def test_separate_builder_only_once_with_explicit_proof():
    fills = [fill(1, "BUY", "1")]
    episode = project(fills)[0]
    result = account_episode(
        episode,
        fills,
        [flow(1), flow(2, "BUILDER_FEE", "-1", trade="")],
        proof=AccountingProof(True, "separate", True),
        attributed_flows={("BUILDER_FEE", "2"): episode.public_ref},
        builder_income_types=frozenset({"BUILDER_FEE"}),
    )
    assert result.net == D("-1.1")

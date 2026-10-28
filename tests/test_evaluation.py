from chess_agents.evaluation import evaluate


async def test_historical_fixture_gates():
    result = await evaluate("evals/cases.json")
    assert result["correctness_gates_pass"]
    assert len(result["runs"]) >= 36
    assert result["mode"] == "offline_synthetic"
    assert all(
        row["invalid_citations"] == row["illegal_lines"] == row["post_cutoff_evidence"] == 0
        for row in result["runs"]
    )
    assert result["strategies"]["two_stage"]["unknown_reservations_usd"] != "0"

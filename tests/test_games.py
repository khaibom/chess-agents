import json

import chess
import httpx
import pytest
from conftest import record

from chess_agents.games import Archives, evidence_tree, position_key, select_games


def test_filters_and_provenance():
    valid = record()
    records = [
        valid,
        valid,
        record(2, color="white"),
        record(3, rules="chess960"),
        record(4, end=200),
        record(5, rated=False),
        record(6, time_class="blitz"),
        record(7, pgn='[Result "*"]\n\n1. d4 *'),
        record(8, moves="1. d4 d5 2. BadMove"),
    ]
    games, excluded = select_games(records, "opponent", "black", 200, 0)
    assert len(games) == 1
    assert sum(excluded.values()) == 8
    tree = evidence_tree(games)
    assert len(tree) == 4
    assert next(iter(tree.values()))["games"] == {valid["url"]: 0}


def test_malformed_player_records_are_skipped_without_losing_valid_game():
    valid = record()
    bad_result_white = record(2, white={"username": "hero", "result": {}})
    bad_result_black = record(3, black={"username": "opponent", "result": []})
    bad_username = record(
        4,
        white={"username": 42, "result": "win"},
        pgn='[White "42"]\n[Black "opponent"]\n[Result "1-0"]\n\n1. d4 d5 1-0',
    )
    games, excluded = select_games(
        [valid, bad_result_white, bad_result_black, bad_username], "opponent", "black", 200, 0
    )
    assert [game["id"] for game in games] == [valid["url"]]
    assert excluded == {"malformed player metadata": 3}


def test_transpositions_repetitions_and_keys():
    records = [
        record(1, moves="1. Nf3 Nf6 2. g3 g6 3. Bg2"),
        record(2, moves="1. g3 g6 2. Nf3 Nf6 3. Bg2"),
    ]
    games, _ = select_games(records, "opponent", "black", 200, 0)
    tree = evidence_tree(games)
    assert any(len(value["games"]) == 2 and len(value["prefix"]) == 4 for value in tree.values())
    board = chess.Board()
    other = board.copy()
    other.castling_rights = 0
    assert position_key(board) != position_key(other)
    ep = chess.Board("8/8/8/3pP3/8/8/8/K6k w - d6 0 1")
    no_ep = ep.copy()
    no_ep.ep_square = None
    assert position_key(ep) != position_key(no_ep)
    repeated, _ = select_games(
        [record(moves="1. Nf3 Nf6 2. Ng1 Ng8 3. Nf3")], "opponent", "black", 200, 0
    )
    assert len(next(iter(evidence_tree(repeated).values()))["games"]) == 1


async def test_archive_retry_cache_and_ssrf(store):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"archives": []}, headers={"etag": "version"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = Archives(store, "test", client)
    url = "https://api.chess.com/pub/player/opponent/games/archives"
    assert (await api.games("opponent", "white", 200))[0] == []
    assert len(calls) == 2
    assert store.archive(url)["etag"] == "version"
    await client.aclose()

    def timeout(request):
        raise httpx.ReadTimeout("timeout")

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        assert (await Archives(store, "test", client).fetch(url))[1] is True
    store.save_archive(url, {"archives": ["https://evil.invalid/payload"]}, None, None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(ValueError, match="Unsafe"):
            await Archives(store, "test", client).games("opponent", "white", 200)
    assert json.loads(store.archive(url)["payload"])["archives"][0].startswith("https://evil")

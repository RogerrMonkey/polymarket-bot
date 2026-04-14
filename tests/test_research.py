from prediction_bot.research.relevance import relevance_score
from prediction_bot.research.sentiment import sentiment_score


def test_sentiment_positive_and_negative() -> None:
    positive = sentiment_score("strong growth and upside improve win chance")
    negative = sentiment_score("deprecated api and blocked access increase risk")
    assert positive > 0
    assert negative < 0


def test_relevance_score_project_context() -> None:
    q = "Will Polymarket CLOB volume increase this month?"
    evidence = "Polymarket API changelog mentions orderbook and liquidity updates"
    score = relevance_score(q, evidence)
    assert score > 0.15


def test_relevance_score_low_for_unrelated_text() -> None:
    q = "Will inflation decrease next quarter?"
    evidence = "Recipe tips for baking bread and pasta"
    score = relevance_score(q, evidence)
    assert score < 0.05

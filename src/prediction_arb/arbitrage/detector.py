from __future__ import annotations

from dataclasses import dataclass

from prediction_arb.exchanges.fees import FeeModel, ZeroFeeModel
from prediction_arb.models.market import Market
from prediction_arb.models.opportunity import ArbitrageOpportunity, MarketMatch
from prediction_arb.models.orderbook import OrderBook
from prediction_arb.money import CONTRACT_PAYOUT_CENTS


@dataclass
class Leg:
    market: Market
    side: str
    ask: int
    ask_size: int


class ArbitrageDetector:
    """Binary YES/NO cross-market detector.

    Uses executable ask prices and displayed size, not last-traded prices.
    V1 uses top-of-book size only. Multi-level blending is left for later.
    """

    def detect(
        self,
        market_a: Market,
        market_b: Market,
        match: MarketMatch | None = None,
        book_a: OrderBook | None = None,
        book_b: OrderBook | None = None,
        fee_a: FeeModel | None = None,
        fee_b: FeeModel | None = None,
    ) -> list[ArbitrageOpportunity]:
        fee_a = fee_a or ZeroFeeModel()
        fee_b = fee_b or ZeroFeeModel()
        a_yes, a_no = _asks(market_a, book_a)
        b_yes, b_no = _asks(market_b, book_b)
        opportunities: list[ArbitrageOpportunity] = []
        if a_yes and b_no:
            opp = self._evaluate(
                yes=Leg(market_a, "yes", *a_yes),
                no=Leg(market_b, "no", *b_no),
                other_market=market_b,
                strategy="BUY_YES_A_BUY_NO_B",
                match=match,
                fee_yes=fee_a,
                fee_no=fee_b,
            )
            if opp:
                opportunities.append(opp)
        if b_yes and a_no:
            opp = self._evaluate(
                yes=Leg(market_b, "yes", *b_yes),
                no=Leg(market_a, "no", *a_no),
                other_market=market_a,
                strategy="BUY_YES_B_BUY_NO_A",
                match=match,
                fee_yes=fee_b,
                fee_no=fee_a,
            )
            if opp:
                opportunities.append(opp)
        return opportunities

    def _evaluate(
        self,
        yes: Leg,
        no: Leg,
        other_market: Market,
        strategy: str,
        match: MarketMatch | None,
        fee_yes: FeeModel,
        fee_no: FeeModel,
    ) -> ArbitrageOpportunity | None:
        if yes.ask <= 0 or no.ask <= 0:
            return None
        if yes.ask + no.ask >= CONTRACT_PAYOUT_CENTS:
            return None
        quantity = min(yes.ask_size, no.ask_size)
        if quantity <= 0:
            return None

        capital = quantity * (yes.ask + no.ask)
        payout = quantity * CONTRACT_PAYOUT_CENTS
        gross = payout - capital
        if gross <= 0:
            return None

        fee_yes_cents = fee_yes.calculate_fee(yes.market, yes.side, yes.ask, quantity)
        fee_no_cents = fee_no.calculate_fee(no.market, no.side, no.ask, quantity)
        fees = fee_yes_cents + fee_no_cents
        net = gross - fees
        if net <= 0:
            return None

        roi_bps = int(round((net / capital) * 10_000)) if capital else 0
        notes = "; ".join(
            part
            for part in (fee_yes.notes(yes.market), fee_no.notes(no.market))
            if part
        )
        return ArbitrageOpportunity(
            strategy=strategy,
            market_a_exchange=yes.market.exchange,
            market_a_id=yes.market.exchange_market_id,
            market_b_exchange=no.market.exchange,
            market_b_id=no.market.exchange_market_id,
            yes_exchange=yes.market.exchange,
            yes_market_id=yes.market.exchange_market_id,
            no_exchange=no.market.exchange,
            no_market_id=no.market.exchange_market_id,
            yes_ask=yes.ask,
            no_ask=no.ask,
            yes_ask_size=yes.ask_size,
            no_ask_size=no.ask_size,
            max_quantity=quantity,
            capital_required=capital,
            guaranteed_payout=payout,
            gross_profit=gross,
            estimated_fees=fees,
            net_profit=net,
            roi_bps=roi_bps,
            fees_are_estimated=bool(fee_yes.estimated or fee_no.estimated),
            fee_notes=notes,
            match_score=match.match_score if match else None,
            match_type=match.match_type if match else None,
            match_reason=match.reason if match else None,
            details={
                "yes_title": yes.market.title,
                "no_title": no.market.title,
                "fee_yes_cents": fee_yes_cents,
                "fee_no_cents": fee_no_cents,
                "prices_are_executable_asks": True,
                "used_last_trade": False,
                "liquidity_source": "top_of_book",
                "other_market_title": other_market.title,
            },
        )


def _asks(market: Market, book: OrderBook | None) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    yes_ask = market.yes_ask
    yes_size = market.yes_ask_size
    no_ask = market.no_ask
    no_size = market.no_ask_size
    if book is not None:
        yes_level = book.best("yes", "ask")
        no_level = book.best("no", "ask")
        if yes_level:
            yes_ask, yes_size = yes_level.price, yes_level.quantity
        if no_level:
            no_ask, no_size = no_level.price, no_level.quantity
    yes = (yes_ask, yes_size or 0) if yes_ask is not None else None
    no = (no_ask, no_size or 0) if no_ask is not None else None
    return yes, no

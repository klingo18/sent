"""Deliverable payload builders — standard ACP payload formats.
Section 15 of sentinel-plan.md.

Matches SDK models exactly (virtuals_acp.models):
  GenericPayload { type: PayloadType, data: T }
  PayloadModel uses alias_generator=to_camel (snake_case → camelCase in JSON).

SDK PayloadTypes:
  FUND_RESPONSE, OPEN_POSITION, CLOSE_POSITION,
  POSITION_FULFILLED, UNFULFILLED_POSITION, CLOSE_JOB_AND_WITHDRAW
"""

import json


def fund_response(wallet_address: str, reporting_endpoint: str = "") -> str:
    """Acknowledge funds received at hot wallet.
    SDK model: FundResponsePayload(reporting_api_endpoint, wallet_address)
    """
    return json.dumps({
        "type": "fund_response",
        "data": {
            "reportingApiEndpoint": reporting_endpoint,
            "walletAddress": wallet_address,
        },
    })


def open_position(
    token_address: str,
    token_symbol: str,
    amount_tokens: float,
    tp_price: float = 0,
    sl_price: float = 0,
    chain: str = "base",
    direction: str = "long",
) -> str:
    """Entry limit filled — tokens acquired.
    SDK model: OpenPositionPayload(symbol, amount, chain, contract_address,
                                    direction, tp: TPSLConfig, sl: TPSLConfig)
    """
    return json.dumps({
        "type": "open_position",
        "data": {
            "symbol": token_symbol,
            "amount": amount_tokens,
            "chain": chain,
            "contractAddress": token_address,
            "direction": direction,
            "tp": {"price": tp_price} if tp_price else {"price": None},
            "sl": {"price": sl_price} if sl_price else {"price": None},
        },
    })


def position_fulfilled(
    token_address: str,
    token_symbol: str,
    amount_usdc: float,
    exit_type: str,
    pnl: float,
    entry_price: float,
    exit_price: float,
) -> str:
    """TP/SL hit — position closed, USDC returned.
    SDK model: PositionFulfilledPayload(symbol, amount, contract_address,
                                         type: TP|SL|CLOSE, pnl, entry_price, exit_price)
    """
    return json.dumps({
        "type": "position_fulfilled",
        "data": {
            "symbol": token_symbol,
            "amount": amount_usdc,
            "contractAddress": token_address,
            "type": exit_type,
            "pnl": pnl,
            "entryPrice": entry_price,
            "exitPrice": exit_price,
        },
    })


def unfulfilled_position(
    token_address: str,
    token_symbol: str,
    amount_usdc: float,
    reason: str,
    error_type: str = "ERROR",
) -> str:
    """Error — order could not be filled.
    SDK model: UnfulfilledPositionPayload(symbol, amount, contract_address,
                                           type: ERROR|PARTIAL, reason)
    """
    return json.dumps({
        "type": "unfulfilled_position",
        "data": {
            "symbol": token_symbol,
            "amount": amount_usdc,
            "contractAddress": token_address,
            "type": error_type,
            "reason": reason,
        },
    })


def close_job_and_withdraw(message: str) -> str:
    """Cancel — order cancelled, funds returned.
    SDK model: CloseJobAndWithdrawPayload(message)
    """
    return json.dumps({
        "type": "close_job_and_withdraw",
        "data": {
            "message": message,
        },
    })

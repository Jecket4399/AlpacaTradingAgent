from ..schemas import (
    ExecutableAction,
    RiskDecision,
    build_trade_intent_from_risk_decision,
    render_risk_decision,
)
from ..utils.agent_trading_modes import (
    ensure_final_transaction_proposal,
    get_trading_mode_context,
    get_agent_specific_context,
    extract_recommendation,
)
from ..utils.memory import TradingMemoryLog
from ..utils.report_context import (
    get_agent_context_bundle,
    build_debate_digest,
)
from ..utils.structured import bind_structured, invoke_structured_object_or_freetext
from tradingagents.dataflows.alpaca_utils import AlpacaUtils
from tradingagents.prompts import render_prompt

import json
import logging
import re

logger = logging.getLogger(__name__)

# Import prompt capture utility
try:
    from webui.utils.prompt_capture import capture_agent_prompt
except ImportError:
    # Fallback for when webui is not available
    def capture_agent_prompt(report_type, prompt_content, symbol=None):
        pass


def create_risk_manager(llm, memory, config=None):
    structured_llm = bind_structured(llm, RiskDecision, "Risk Manager")
    decision_log = TradingMemoryLog(config)

    def risk_manager_node(state) -> dict:

        company_name = state["company_of_interest"]

        history = state["risk_debate_state"]["history"]
        risk_debate_state = state["risk_debate_state"]
        trader_plan = state["investment_plan"]

        # Get trading mode from config
        allow_shorts = config.get("allow_shorts", False) if config else False

        # Determine live position from Alpaca
        current_position = AlpacaUtils.get_current_position_state(company_name)
        state["current_position"] = current_position

        # ---------------------------------------------------------
        # NEW: Fetch richer live account & position metrics from Alpaca
        # ---------------------------------------------------------
        positions_data = AlpacaUtils.get_positions_data()
        account_info = AlpacaUtils.get_account_info()

        # Build summary for specific symbol
        position_stats_desc = ""
        symbol_key = company_name.upper().replace("/", "")
        for pos in positions_data:
            if pos["Symbol"].upper() == symbol_key:
                qty = pos["Qty"]
                avg_entry = pos["Avg Entry"]
                today_pl_dollars = pos["Today's P/L ($)"]
                today_pl_percent = pos["Today's P/L (%)"]
                total_pl_dollars = pos["Total P/L ($)"]
                total_pl_percent = pos["Total P/L (%)"]

                position_stats_desc = (
                    f"Position Details for {company_name}:\n"
                    f"- Quantity: {qty}\n"
                    f"- Average Entry Price: {avg_entry}\n"
                    f"- Today's P/L: {today_pl_dollars} ({today_pl_percent})\n"
                    f"- Total P/L: {total_pl_dollars} ({total_pl_percent})"
                )
                break
        if not position_stats_desc:
            position_stats_desc = "No open position details available for this symbol."

        buying_power = account_info.get("buying_power", 0.0)
        cash = account_info.get("cash", 0.0)
        daily_change_dollars = account_info.get("daily_change_dollars", 0.0)
        daily_change_percent = account_info.get("daily_change_percent", 0.0)
        account_status_desc = (
            "Account Status:\n"
            f"- Buying Power: ${buying_power:,.2f}\n"
            f"- Cash: ${cash:,.2f}\n"
            f"- Daily Change: ${daily_change_dollars:,.2f} ({daily_change_percent:.2f}%)"
        )
        # ---------------------------------------------------------
        # END NEW BLOCK
        # ---------------------------------------------------------

        open_pos_desc = (
            f"We currently have an open {current_position} position in {company_name}."
            if current_position != "NEUTRAL"
            else f"We do not have any open position in {company_name}."
        )
        
        # Get centralized trading mode context
        trading_context = get_trading_mode_context(config, current_position)
        agent_context = get_agent_specific_context("manager", trading_context)
        
        # Get mode-specific terms for the prompt
        actions = trading_context["actions"]
        mode_name = trading_context["mode_name"]
        decision_format = trading_context["decision_format"]
        final_format = trading_context["final_format"]
        output_language = (config or {}).get("output_language", "English")
        context_bundle = get_agent_context_bundle(
            state,
            agent_role="managers/risk_manager",
            objective=(
                f"Judge risk debate and finalize risk-adjusted trade decision for {company_name}. "
                f"Trader plan: {trader_plan}"
            ),
            config=config,
        )
        claim_matrix = context_bundle.get("decision_claim_matrix", "")
        risk_debate_digest = build_debate_digest(risk_debate_state, "risk", config=config)
        all_reports_text = context_bundle.get("all_reports_text", "")

        curr_situation = context_bundle["memory_context"]
        past_memories = memory.get_memories(curr_situation, n_matches=2)

        past_memory_str = ""
        for i, rec in enumerate(past_memories, 1):
            past_memory_str += rec["recommendation"] + "\n\n"
        decision_memory_str = decision_log.get_past_context(company_name)

        prompt = render_prompt(
            "managers/risk_manager",
            agent_context=agent_context,
            decision_format=decision_format,
            open_pos_desc=open_pos_desc,
            position_stats_desc=position_stats_desc,
            account_status_desc=account_status_desc,
            trader_plan=trader_plan,
            claim_matrix=claim_matrix,
            all_reports_text=all_reports_text,
            risk_debate_digest=risk_debate_digest,
            history=history,
            past_memory_str=past_memory_str,
            decision_memory_str=decision_memory_str,
            actions=actions,
            final_format=final_format,
            output_language=output_language,
        )

        # Capture the COMPLETE prompt that gets sent to the LLM
        capture_agent_prompt("final_trade_decision", prompt, company_name)

        response_content, structured_decision = invoke_structured_object_or_freetext(
            structured_llm,
            llm,
            prompt,
            render_risk_decision,
            "Risk Manager",
        )

        # Extract the recommendation from the response
        trading_mode = trading_context["mode"]
        extracted_recommendation = extract_recommendation(response_content, trading_mode)
        if not extracted_recommendation:
            extracted_recommendation = "NEUTRAL" if trading_mode == "trading" else "HOLD"
        
        final_decision_content = ensure_final_transaction_proposal(
            response_content, extracted_recommendation, trading_mode
        )

        if structured_decision is None:
            # DeepSeek free-text 降级：尝试从响应中提取 JSON 止损止盈价格
            stop_loss_text = None
            take_profit_text = None
            try:
                # 匹配 ```json {...} ``` 代码块 或 裸 JSON 对象
                json_match = re.search(
                    r'```(?:json)?\s*(\{[^`]+\})\s*```',
                    response_content,
                )
                json_str = json_match.group(1) if json_match else None
                if not json_str:
                    json_match = re.search(
                        r'(\{\s*"stop_loss"[^}]+\})',
                        response_content,
                    )
                    json_str = json_match.group(1) if json_match else None
                if json_str:
                    prices = json.loads(json_str)
                    sl = prices.get("stop_loss")
                    tp = prices.get("take_profit")
                    if sl is not None and isinstance(sl, (int, float)):
                        stop_loss_text = f"${sl:.2f}"
                    if tp is not None and isinstance(tp, (int, float)):
                        take_profit_text = f"${tp:.2f}"
                    if stop_loss_text or take_profit_text:
                        logger.info(
                            "从 free-text 响应中提取到价格: stop_loss=%s, take_profit=%s",
                            stop_loss_text, take_profit_text,
                        )
            except Exception:
                pass

            structured_decision = RiskDecision(
                action=ExecutableAction(extracted_recommendation),
                confidence="unknown",
                risk_rationale=(
                    "Structured risk output was unavailable; execution intent was derived "
                    "from the final transaction proposal line."
                ),
                required_controls=(
                    "Review the Markdown risk report manually. Broker stop-loss and "
                    + ("take-profit controls were extracted from free-text JSON." if (stop_loss_text or take_profit_text)
                       else "take-profit controls are not inferred from free text.")
                ),
                stop_loss=stop_loss_text,
                take_profit=take_profit_text,
            )

        trade_intent = build_trade_intent_from_risk_decision(
            symbol=company_name,
            trading_mode=trading_mode,
            current_position=current_position,
            decision=structured_decision,
            allow_shorts=allow_shorts,
            trade_date=state.get("trade_date"),
        ).model_dump(mode="json")

        new_risk_debate_state = {
            "judge_decision": final_decision_content,
            "history": risk_debate_state["history"],
            "risky_history": risk_debate_state["risky_history"],
            "safe_history": risk_debate_state["safe_history"],
            "neutral_history": risk_debate_state["neutral_history"],
            "risky_messages": risk_debate_state.get("risky_messages", []),
            "safe_messages": risk_debate_state.get("safe_messages", []),
            "neutral_messages": risk_debate_state.get("neutral_messages", []),
            "latest_speaker": "Judge",
            "current_risky_response": risk_debate_state["current_risky_response"],
            "current_safe_response": risk_debate_state["current_safe_response"],
            "current_neutral_response": risk_debate_state["current_neutral_response"],
            "count": risk_debate_state["count"],
        }

        return {
            "risk_debate_state": new_risk_debate_state,
            "final_trade_decision": final_decision_content,
            "final_trade_intent": trade_intent,
            "trading_mode": trading_mode,
            "current_position": current_position,
            "recommended_action": extracted_recommendation,
        }

    return risk_manager_node

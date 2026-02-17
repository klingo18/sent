// MVP Resource 4: get_order_status
// Single order details by order_id.
// Section 32.6 — Params: order_id

import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });

  try {
    const supabase = createClient(
      Deno.env.get("SUPABASE_URL") ?? "",
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "",
    );

    const url = new URL(req.url);
    const orderId = url.searchParams.get("order_id") ?? "";

    if (!orderId) {
      return new Response(
        JSON.stringify({ error: "Missing order_id parameter" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" }, status: 400 },
      );
    }

    const { data: order, error } = await supabase
      .from("orders")
      .select("*")
      .eq("id", orderId)
      .single();

    if (error || !order) {
      return new Response(
        JSON.stringify({ error: "Order not found" }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" }, status: 404 },
      );
    }

    // Get fills for this order
    const { data: fills } = await supabase
      .from("fills")
      .select("*")
      .eq("order_id", orderId)
      .order("filled_at", { ascending: true });

    return new Response(
      JSON.stringify({
        order: {
          id: order.id,
          jobId: order.job_id,
          buyerAddress: order.buyer_address,
          tokenAddress: order.token_address,
          tokenSymbol: order.token_symbol,
          side: order.side,
          status: order.status,
          usdcAmount: order.usdc_amount,
          limitPriceUsd: order.limit_price_usd,
          virtualHeld: order.virtual_held,
          tokensReceived: order.tokens_received,
          transferOk: order.transfer_ok,
          slippagePct: order.slippage_pct,
          leg1TxHash: order.leg1_tx_hash,
          leg2TxHash: order.leg2_tx_hash,
          cancelTxHash: order.cancel_tx_hash,
          usdcRecovered: order.usdc_recovered,
          routeKey: order.route_key,
          evaluationStatus: order.evaluation_status,
          createdAt: order.created_at,
          completedAt: order.completed_at,
          cancelledAt: order.cancelled_at,
          deliveredAt: order.delivered_at,
          failedAt: order.failed_at,
        },
        fills: (fills ?? []).map((f: any) => ({
          fillType: f.fill_type,
          txHash: f.tx_hash,
          amountIn: f.amount_in,
          actualOut: f.actual_out,
          triggerPriceUsd: f.trigger_price_usd,
          slippagePct: f.slippage_pct,
          filledAt: f.filled_at,
        })),
      }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" }, status: 200 },
    );
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : String(error);
    return new Response(
      JSON.stringify({ error: message }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" }, status: 500 },
    );
  }
});
